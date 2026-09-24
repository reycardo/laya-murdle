"""Parse a murdle.com puzzle with Laya and solve it with a constraint solver."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from constraint import AllDifferentConstraint, Problem

MURDLE_URL = "https://murdle.com"

CATEGORY_HEADERS = {
    "suspects": "suspects",
    "suspect": "suspects",
    "weapons": "weapons",
    "weapon": "weapons",
    "locations": "locations",
    "location": "locations",
    "motives": "motives",
    "motive": "motives",
}

CLUE_HEADERS = ("clues", "the detective's notes", "detective's notes", "notes")

QUESTION_MARKERS = ("who", "whodunit", "accuse", "murderer")

# murdle.com lists every category member in the accusation dropdowns at the bottom.
ACCUSATION_SELECTS = {
    "suspect": "suspects",
    "weapon": "weapons",
    "room": "locations",
    "motive": "motives",
}

# The category whose members the other categories are assigned to.
PEOPLE_CATEGORY = "suspects"

NEGATION_CUE = re.compile(
    r"\b(?:not|n't|never|neither|nor|no|none|nobody|no one|except|besides|"
    r"other than|away from|avoid(?:s|ed)?|ruled out)\b",
    re.IGNORECASE,
)

# "X was flirting with the person who had Y" means X is *not* that person.
THIRD_PARTY_CUE = re.compile(
    r"\b(?:the person who|the suspect who|the one who|the individual who|whoever)\b",
    re.IGNORECASE,
)

XOR_CLUE = re.compile(r"\beither\b(?P<first>.+?)\bor\b(?P<second>.+)", re.IGNORECASE)
XOR_MARKER = re.compile(r"but not both", re.IGNORECASE)

# Attribute words that murdle clues use in place of a member name, per category.
ROLE_TERMS = {
    "clergy": ("clergy", "religious"),
    "business": ("business",),
    "government": ("government", "politician"),
    "education": ("education", "educator", "teacher"),
    "army": ("army", "military", "soldier"),
    "noble": ("noble", "nobility", "royal"),
}

FEATURE_PREFIXES = ("in", "at", "on", "by", "beneath", "under", "near", "a", "an", "the", "some")

# Laya is strongly biased towards one option for any single phrasing, so each clue is
# probed with several claim wordings (one inverted) and the results are averaged.
CLAIM_PROBES: dict[str, tuple[str, str, bool]] = {
    "pair": ("{a} - {b}", "The clue confirms this pairing rather than ruling it out.", False),
    "linked": ("{a} is linked to {b}", "The clue supports the claim.", False),
    "was_with": ("{a} was with {b}", "The clue supports the claim.", False),
    "ruled_out": ("{a} and {b} are together", "The clue rules out the claim.", True),
}

# A lexical negation makes the clue negative unless Laya is overwhelmingly sure it is not.
NEGATED_OVERRIDE = 0.90
AFFIRMED_FLOOR = 0.10

SAMPLE_PUZZLE = """
SUSPECTS
Mayor Honey
Miss Saffron
Dr. Crimson

WEAPONS
Candlestick
Poisoned Tea
Heavy Wrench

LOCATIONS
Library
Greenhouse
Boathouse

CLUES
Mayor Honey was not in the Library.
The Candlestick was in the Greenhouse.
Dr. Crimson had the Heavy Wrench.
Miss Saffron was not in the Greenhouse.
Dr. Crimson was not in the Library.
"""


@dataclass
class Puzzle:
    categories: dict[str, list[str]] = field(default_factory=dict)
    clues: list[str] = field(default_factory=list)
    # category -> member -> raw attributes scraped from the page's card data
    details: dict[str, dict[str, dict]] = field(default_factory=dict)


@dataclass(frozen=True)
class Mention:
    """One side of a clue: a named member, or every member matching an attribute."""

    category: str
    members: frozenset[str]
    label: str
    named: bool


@dataclass(frozen=True)
class Pairing:
    left: Mention
    right: Mention


@dataclass
class Clue:
    text: str
    pairings: list[Pairing]
    positive: bool
    confidence: float
    cues: list[str]
    kind: str = "pair"


def fetch_puzzle_html(url: str = MURDLE_URL, timeout: float = 20.0) -> str:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


EXTRACT_JS = """() => {
  const selects = {suspect: 'suspects', weapon: 'weapons', room: 'locations', motive: 'motives'};
  const categories = {};
  for (const [id, category] of Object.entries(selects)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const members = Array.from(el.options).slice(1).map(o => o.textContent.trim());
    if (members.length) categories[category] = members;
  }

  const clues = Array.from(document.querySelectorAll('#evidence strong'))
    .map(s => s.textContent.replace(/\\s+/g, ' ').replace(/^\\u2022\\s*/, '').trim())
    .filter(Boolean);

  // The card attributes live in the page's own script scope, not on window.
  const details = {suspects: {}, weapons: {}, locations: {}};
  try {
    (categories.suspects || []).forEach(name => {
      const found = suspect_details[name];
      if (found) details.suspects[name] = found.characteristics;
    });
  } catch (e) {}
  try {
    major_setting.weapons.forEach(w => {
      if ((categories.weapons || []).includes(w.name)) {
        details.weapons[w.name] = {weight: w.weight, materials: w.materials, method: w.method, clue: w.clue};
      }
    });
  } catch (e) {}
  try {
    major_setting.rooms.forEach(r => {
      if ((categories.locations || []).includes(r.name)) {
        details.locations[r.name] = {feature: r.feature, indoors: r.indoors};
      }
    });
  } catch (e) {}

  return {categories, clues, details};
}"""

FOUND_PRINT_JS = """() => {
  const src = Array.from(document.querySelectorAll('img'))
    .map(i => i.getAttribute('src') || '')
    .find(s => s.startsWith('prints/'));
  return src || null;
}"""


def render_puzzle(url: str = MURDLE_URL, timeout: float = 60.0) -> Puzzle:
    """Load the page in a headless browser so the client-side puzzle is generated."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "--render needs Playwright:\n"
            "  uv sync --extra render\n"
            "  uv run playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_selector("#evidence p strong", timeout=timeout * 1000)
            payload = page.evaluate(EXTRACT_JS)
            owner = _resolve_fingerprint(page, payload["details"].get("suspects", {}))
        finally:
            browser.close()

    clues = payload["clues"]
    if owner:
        clues = [re.sub(r"This fingerprint", f"{owner}'s fingerprint", c) for c in clues]

    return Puzzle(categories=payload["categories"], clues=clues, details=payload["details"])


def _resolve_fingerprint(page, suspects: dict[str, dict]) -> str | None:
    """Open the fingerprint evidence page and match the print against the suspects."""
    link = page.query_selector("#evidence object a")
    if not link:
        return None
    link.click()
    page.wait_for_timeout(1000)
    found = page.evaluate(FOUND_PRINT_JS)
    if not found:
        return None
    filename = found.rsplit("/", 1)[-1]
    for name, characteristics in suspects.items():
        if characteristics.get("print") == filename:
            return name
    return None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


def parse_murdle_dom(html: str) -> Puzzle | None:
    """Read a rendered murdle page: the category members live in the accusation dropdowns."""
    soup = BeautifulSoup(html, "html.parser")
    puzzle = Puzzle()

    for select_id, category in ACCUSATION_SELECTS.items():
        select = soup.find("select", id=select_id)
        if not select:
            continue
        # The first option is the placeholder label ("WHO?", "HOW?", ...).
        members = [option.get_text(strip=True) for option in select.find_all("option")[1:]]
        if members:
            puzzle.categories[category] = members

    evidence = soup.find(id="evidence")
    if evidence:
        for bullet in evidence.find_all("strong"):
            text = re.sub(r"\s+", " ", bullet.get_text(" ", strip=True)).lstrip("• ").strip()
            if text:
                puzzle.clues.append(text)

    if puzzle.categories.get(PEOPLE_CATEGORY) and puzzle.clues:
        return puzzle
    return None


def _looks_like_item(line: str) -> bool:
    return 0 < len(line) <= 40 and len(line.split()) <= 5 and not line.endswith(".")


def parse_puzzle(text: str) -> Puzzle:
    """Split the puzzle text into category members and clue sentences.

    murdle.com renders its puzzle client-side and changes markup often, so this is a
    best-effort text heuristic rather than a stable scraper.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    puzzle = Puzzle()
    current: str | None = None
    in_clues = False

    for line in lines:
        key = line.lower().strip(":. ")

        if key in CATEGORY_HEADERS:
            current = CATEGORY_HEADERS[key]
            puzzle.categories.setdefault(current, [])
            in_clues = False
            continue

        if key in CLUE_HEADERS:
            current = None
            in_clues = True
            continue

        if in_clues:
            if key.split(" ")[0] in QUESTION_MARKERS:
                in_clues = False
                continue
            if len(line) > 10:
                puzzle.clues.append(line)
            continue

        if current and _looks_like_item(line):
            if line not in puzzle.categories[current]:
                puzzle.categories[current].append(line)

    return puzzle


def _strip_feature(feature: str) -> str:
    words = feature.lower().split()
    while words and words[0] in FEATURE_PREFIXES:
        words.pop(0)
    return " ".join(words)


def suspect_terms(name: str, characteristics: dict) -> list[str]:
    terms = []
    hair = characteristics.get("hair")
    if hair == "no":
        terms.append("bald")
    elif hair:
        terms += [f"{hair} hair", f"{hair}-haired"]
    eyes = characteristics.get("eyes")
    if eyes:
        terms += [f"{eyes} eyes", f"{eyes}-eyed"]
    hand = characteristics.get("hand")
    if hand:
        terms += [f"{hand}-handed", f"{hand} handed"]
    if characteristics.get("sign"):
        terms.append(characteristics["sign"])
    if characteristics.get("element"):
        terms.append(f"{characteristics['element']} sign")
    for key, words in ROLE_TERMS.items():
        if characteristics.get(key):
            terms += list(words)
    if characteristics.get("feature"):
        terms.append(_strip_feature(characteristics["feature"]))
    return terms


def weapon_terms(details: dict) -> list[str]:
    terms = []
    weight = details.get("weight")
    if weight:
        terms += [f"{weight}-weight", f"{weight} weight"]
    terms += [m for m in details.get("materials") or []]
    terms += [m for m in details.get("method") or []]
    if details.get("clue"):
        terms.append(_strip_feature(details["clue"]))
    return terms


def location_terms(details: dict) -> list[str]:
    terms = []
    if details.get("feature"):
        terms.append(_strip_feature(details["feature"]))
    terms.append("indoors" if details.get("indoors") else "outdoors")
    return terms


def build_attribute_index(puzzle: Puzzle) -> dict[str, dict[str, frozenset[str]]]:
    """Map an attribute phrase to every member of a category that has it."""
    builders = {
        "suspects": lambda name, detail: suspect_terms(name, detail),
        "weapons": lambda name, detail: weapon_terms(detail),
        "locations": lambda name, detail: location_terms(detail),
    }

    index: dict[str, dict[str, set[str]]] = {}
    for category, builder in builders.items():
        members = puzzle.details.get(category) or {}
        for name, detail in members.items():
            for term in builder(name, detail):
                term = term.strip().lower()
                if term:
                    index.setdefault(category, {}).setdefault(term, set()).add(name)

    heights = {
        name: detail.get("height")
        for name, detail in (puzzle.details.get("suspects") or {}).items()
        if detail.get("height")
    }
    if len(heights) > 1:
        ordered = sorted(heights, key=lambda name: int(heights[name]))
        index.setdefault("suspects", {}).setdefault("shortest", set()).add(ordered[0])
        index.setdefault("suspects", {}).setdefault("tallest", set()).add(ordered[-1])

    return {
        category: {term: frozenset(names) for term, names in terms.items()}
        for category, terms in index.items()
    }


def find_mentions(
    clue: str,
    categories: dict[str, list[str]],
    attributes: dict[str, dict[str, frozenset[str]]] | None = None,
) -> list[Mention]:
    """Find every member or attribute group the clue refers to, in order of appearance."""
    lowered = clue.lower()
    hits: list[tuple[int, int, Mention]] = []

    for category, members in categories.items():
        for member in members:
            index = lowered.find(member.lower())
            if index >= 0:
                hits.append(
                    (index, -len(member), Mention(category, frozenset({member}), member, True))
                )

    for category, terms in (attributes or {}).items():
        for term, members in terms.items():
            match = re.search(rf"\b{re.escape(term)}\b", lowered)
            if match:
                hits.append(
                    (match.start(), -len(term), Mention(category, members, term, False))
                )

    hits.sort(key=lambda hit: (hit[0], hit[1]))

    ordered: list[Mention] = []
    taken: set[tuple[str, frozenset[str]]] = set()
    for _, _, mention in hits:
        key = (mention.category, mention.members)
        if key not in taken:
            taken.add(key)
            ordered.append(mention)
    return ordered


def first_pairing(mentions: list[Mention]) -> Pairing | None:
    if not mentions:
        return None
    left = mentions[0]
    for right in mentions[1:]:
        if right.category != left.category:
            return Pairing(left, right)
    return None


def score_pairing(clue: str, a: str, b: str, router) -> float:
    """Probability that the clue puts `a` and `b` together, averaged over probes."""
    questions = {
        name: {"type": "noul", "instructions": f"{instructions} Claim: {claim.format(a=a, b=b)}."}
        for name, (claim, instructions, _) in CLAIM_PROBES.items()
    }
    answers = router.predict({"clue": clue}, questions)["answers"]
    scores = [
        1.0 - answers[name]["noul"] if inverted else answers[name]["noul"]
        for name, (_, _, inverted) in CLAIM_PROBES.items()
    ]
    return sum(scores) / len(scores)


def split_xor(
    clue: str,
    categories: dict[str, list[str]],
    attributes: dict[str, dict[str, frozenset[str]]],
) -> list[Pairing] | None:
    """Split 'Either A or B (but not both!)' into its two alternative pairings."""
    if not XOR_MARKER.search(clue):
        return None
    match = XOR_CLUE.search(clue)
    if not match:
        return None

    whole = find_mentions(clue, categories, attributes)
    pairings = []
    for half in (match.group("first"), match.group("second")):
        mentions = find_mentions(half, categories, attributes)
        # A half often drops the shared subject ("...or brought a laptop").
        seen = {mention.category for mention in mentions}
        mentions += [mention for mention in whole if mention.category not in seen]
        pairing = first_pairing(mentions)
        if not pairing:
            return None
        pairings.append(pairing)
    return pairings


def classify_clues(puzzle: Puzzle, router) -> tuple[list[Clue], list[str]]:
    """Turn each clue into pairings of members, with a polarity decided by Laya."""
    attributes = build_attribute_index(puzzle)
    parsed: list[Clue] = []
    skipped: list[str] = []

    for text in puzzle.clues:
        alternatives = split_xor(text, puzzle.categories, attributes)
        if alternatives:
            parsed.append(
                Clue(text=text, pairings=alternatives, positive=True, confidence=1.0,
                     cues=["xor"], kind="xor")
            )
            continue

        mentions = find_mentions(text, puzzle.categories, attributes)
        pairing = first_pairing(mentions)
        if not pairing:
            skipped.append(text)
            continue

        score = score_pairing(text, pairing.left.label, pairing.right.label, router)

        cues = []
        if NEGATION_CUE.search(text):
            cues.append("negation")
        if THIRD_PARTY_CUE.search(text) and any(
            mention.named and mention.category == PEOPLE_CATEGORY
            for mention in (pairing.left, pairing.right)
        ):
            cues.append("third-party")

        positive = score >= (NEGATED_OVERRIDE if cues else AFFIRMED_FLOOR)
        parsed.append(
            Clue(
                text=text,
                pairings=[pairing],
                positive=positive,
                confidence=score if positive else 1.0 - score,
                cues=cues,
            )
        )

    return parsed, skipped


def _var(category: str, person: str) -> str:
    return f"{category}::{person}"


def _pairing_predicate(pairing: Pairing, people: list[str]):
    """Variables plus a test for 'some suspect satisfies both sides of this pairing'."""
    left, right = pairing.left, pairing.right

    if PEOPLE_CATEGORY in (left.category, right.category):
        persons, other = (left, right) if left.category == PEOPLE_CATEGORY else (right, left)
        names = [person for person in people if person in persons.members]
        if not names or other.category == PEOPLE_CATEGORY:
            return None
        variables = [_var(other.category, person) for person in names]

        def holds(values, wanted=other.members):
            return any(value in wanted for value in values)

        return variables, holds

    variables = [_var(left.category, person) for person in people]
    variables += [_var(right.category, person) for person in people]
    count = len(people)

    def holds(values, a=left.members, b=right.members, n=count):
        return any(values[i] in a and values[n + i] in b for i in range(n))

    return variables, holds


def build_problem(puzzle: Puzzle, clues: list[Clue]) -> tuple[Problem, list[str]]:
    people = puzzle.categories[PEOPLE_CATEGORY]
    item_categories = {
        name: members
        for name, members in puzzle.categories.items()
        if name != PEOPLE_CATEGORY and members
    }

    problem = Problem()
    for category, members in item_categories.items():
        variables = [_var(category, person) for person in people]
        problem.addVariables(variables, list(members))
        problem.addConstraint(AllDifferentConstraint(), variables)

    for clue in clues:
        predicates = [_pairing_predicate(pairing, people) for pairing in clue.pairings]
        if any(predicate is None for predicate in predicates):
            continue

        if clue.kind == "xor":
            (vars_a, holds_a), (vars_b, holds_b) = predicates
            union = list(dict.fromkeys(vars_a + vars_b))
            index_a = [union.index(name) for name in vars_a]
            index_b = [union.index(name) for name in vars_b]

            def exclusive(*values, ia=index_a, ib=index_b, ha=holds_a, hb=holds_b):
                return ha([values[i] for i in ia]) != hb([values[i] for i in ib])

            problem.addConstraint(exclusive, union)
            continue

        variables, holds = predicates[0]
        problem.addConstraint(
            lambda *values, test=holds, want=clue.positive: test(values) is want,
            variables,
        )

    return problem, people


def solve(puzzle: Puzzle, clues: list[Clue], max_drops: int = 2):
    """Solve, dropping the least confident clues if the clue set is contradictory."""
    ranked = sorted(clues, key=lambda clue: clue.confidence)
    for dropped in range(max_drops + 1):
        kept = ranked[dropped:]
        problem, people = build_problem(puzzle, kept)
        solutions = problem.getSolutions()
        if solutions:
            return solutions, people, ranked[:dropped]
    return [], puzzle.categories[PEOPLE_CATEGORY], []


def format_solution(solution: dict[str, str], people: list[str]) -> str:
    lines = []
    for person in people:
        items = [
            f"{key.split('::')[0]}={value}"
            for key, value in sorted(solution.items())
            if key.endswith(f"::{person}")
        ]
        lines.append(f"  {person}: " + ", ".join(items))
    return "\n".join(lines)


def load_puzzle(args: argparse.Namespace) -> Puzzle:
    if args.sample:
        return parse_puzzle(SAMPLE_PUZZLE)

    if args.render and not args.file:
        return render_puzzle(args.url)

    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
        if not args.file.endswith((".html", ".htm")):
            return parse_puzzle(raw)
    else:
        raw = fetch_puzzle_html(args.url)

    return parse_murdle_dom(raw) or parse_puzzle(html_to_text(raw))


def describe(clue: Clue) -> str:
    cues = f" ({', '.join(clue.cues)})" if clue.cues else ""
    if clue.kind == "xor":
        pairs = " XOR ".join(
            f"{pairing.left.label} = {pairing.right.label}" for pairing in clue.pairings
        )
        return f"  [xor ] {pairs}{cues}  <- {clue.text}"
    pairing = clue.pairings[0]
    relation = "=" if clue.positive else "!="
    left, right = pairing.left, pairing.right
    detail = ""
    for mention in (left, right):
        if not mention.named:
            detail += f"  [{mention.label} -> {', '.join(sorted(mention.members))}]"
    return (
        f"  [{clue.confidence:.2f}] {left.label} {relation} {right.label}{cues}"
        f"  <- {clue.text}{detail}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=MURDLE_URL, help="puzzle URL to fetch")
    parser.add_argument("--file", help="local .html or .txt puzzle instead of fetching")
    parser.add_argument("--sample", action="store_true", help="use the built-in puzzle")
    parser.add_argument(
        "--render",
        action="store_true",
        help="render the page with headless Chromium (needs the 'render' extra)",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="lazy-load Laya checkpoints instead of preloading them",
    )
    args = parser.parse_args()

    puzzle = load_puzzle(args)

    if not puzzle.categories.get(PEOPLE_CATEGORY) or not puzzle.clues:
        print(
            "Could not find suspects and clues. murdle.com builds the puzzle in the\n"
            "browser, so a plain HTTP fetch returns an empty shell. Either:\n"
            "  uv run laya-murdle --render          (headless Chromium)\n"
            "  uv run laya-murdle --file puzzle.html  (a page you saved yourself)\n"
            "  uv run laya-murdle --sample          (check the pipeline offline)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("Parsed puzzle:")
    for category, members in puzzle.categories.items():
        print(f"  {category}: {', '.join(members)}")
    print(f"  clues: {len(puzzle.clues)}")

    from laya import Router

    router = Router(preload=not args.no_preload)
    clues, skipped = classify_clues(puzzle, router)

    print("\nClassified clues:")
    for clue in clues:
        print(describe(clue))
    for text in skipped:
        print(f"  [skipped] {text}")

    solutions, people, dropped = solve(puzzle, clues)

    print()
    if not solutions:
        print("No solution: too many clues were parsed or classified incorrectly.")
        raise SystemExit(2)
    for clue in dropped:
        print(f"Dropped as likely misread [{clue.confidence:.2f}]: {clue.text}")
    if len(solutions) > 1:
        print(f"{len(solutions)} solutions - the clues are underconstrained. First one:")
    else:
        print("Unique solution:")
    print(format_solution(solutions[0], people))


if __name__ == "__main__":
    main()
