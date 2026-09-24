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

# The category whose members the other categories are assigned to.
PEOPLE_CATEGORY = "suspects"

NEGATION_CUE = re.compile(
    r"\b(?:not|n't|never|neither|nor|no|none|nobody|no one|except|besides|"
    r"other than|away from|avoid(?:s|ed)?|ruled out)\b",
    re.IGNORECASE,
)

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


@dataclass
class Clue:
    text: str
    left: tuple[str, str]
    right: tuple[str, str]
    positive: bool
    confidence: float
    negation_cue: bool


def fetch_puzzle_html(url: str = MURDLE_URL, timeout: float = 20.0) -> str:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


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


def find_mentions(clue: str, categories: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Return (category, member) pairs mentioned in the clue, in order of appearance."""
    lowered = clue.lower()
    mentions: list[tuple[int, str, str]] = []
    for category, members in categories.items():
        for member in members:
            index = lowered.find(member.lower())
            if index >= 0:
                mentions.append((index, category, member))
    mentions.sort()
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for _, category, member in mentions:
        if (category, member) not in seen:
            seen.add((category, member))
            ordered.append((category, member))
    return ordered


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


def classify_clues(puzzle: Puzzle, router) -> tuple[list[Clue], list[str]]:
    """Turn each clue into a positive or negative pairing of two category members."""
    parsed: list[Clue] = []
    skipped: list[str] = []

    for clue in puzzle.clues:
        mentions = find_mentions(clue, puzzle.categories)
        if len(mentions) < 2 or mentions[0][0] == mentions[1][0]:
            skipped.append(clue)
            continue

        left, right = mentions[0], mentions[1]
        score = score_pairing(clue, left[1], right[1], router)
        negated = bool(NEGATION_CUE.search(clue))
        positive = score >= (NEGATED_OVERRIDE if negated else AFFIRMED_FLOOR)

        parsed.append(
            Clue(
                text=clue,
                left=left,
                right=right,
                positive=positive,
                confidence=score if positive else 1.0 - score,
                negation_cue=negated,
            )
        )

    return parsed, skipped


def _var(category: str, person: str) -> str:
    return f"{category}::{person}"


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
        left_cat, right_cat = clue.left[0], clue.right[0]

        if PEOPLE_CATEGORY in (left_cat, right_cat):
            if left_cat == PEOPLE_CATEGORY:
                person, (category, value) = clue.left[1], clue.right
            else:
                person, (category, value) = clue.right[1], clue.left
            problem.addConstraint(
                lambda got, want=value, holds=clue.positive: (got == want) is holds,
                [_var(category, person)],
            )
            continue

        left_vars = [_var(left_cat, person) for person in people]
        right_vars = [_var(right_cat, person) for person in people]

        def pairing(
            *values,
            a=clue.left[1],
            b=clue.right[1],
            n=len(people),
            holds=clue.positive,
        ):
            matched = any(values[i] == a and values[n + i] == b for i in range(n))
            return matched is holds

        problem.addConstraint(pairing, left_vars + right_vars)

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


def load_text(args: argparse.Namespace) -> str:
    if args.sample:
        return SAMPLE_PUZZLE
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
        return html_to_text(raw) if args.file.endswith((".html", ".htm")) else raw
    return html_to_text(fetch_puzzle_html(args.url))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=MURDLE_URL, help="puzzle URL to fetch")
    parser.add_argument("--file", help="local .html or .txt puzzle instead of fetching")
    parser.add_argument("--sample", action="store_true", help="use the built-in puzzle")
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="lazy-load Laya checkpoints instead of preloading them",
    )
    args = parser.parse_args()

    puzzle = parse_puzzle(load_text(args))

    if not puzzle.categories.get(PEOPLE_CATEGORY) or not puzzle.clues:
        print(
            "Could not find suspects and clues in the page text. murdle.com renders its\n"
            "puzzle in the browser, so save the rendered page and pass it with --file,\n"
            "or try --sample to check the pipeline.",
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
        relation = "=" if clue.positive else "!="
        cue = " (negation cue)" if clue.negation_cue else ""
        print(
            f"  [{clue.confidence:.2f}] {clue.left[1]} {relation} {clue.right[1]}{cue}"
            f"  <- {clue.text}"
        )
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
