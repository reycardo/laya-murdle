"""Getting a puzzle out of murdle.com, a saved page, or plain text."""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from laya_murdle.config import FetchConfig
from laya_murdle.models import PEOPLE_CATEGORY, Puzzle

log = logging.getLogger(__name__)

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


def fetch_puzzle_html(url: str, timeout: float) -> str:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    log.debug("GET %s -> %d, %d bytes", response.url, response.status_code, len(response.text))
    response.raise_for_status()
    return response.text


def render_puzzle(url: str, fetch: FetchConfig) -> Puzzle:
    """Load the page in a headless browser so the client-side puzzle is generated."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "--render needs Playwright:\n"
            "  uv sync --extra render\n"
            "  uv run playwright install chromium"
        ) from exc

    timeout = fetch.render_timeout * 1000
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            log.debug("page loaded, waiting for the clue list")
            page.wait_for_selector("#evidence p strong", timeout=timeout)
            payload = page.evaluate(EXTRACT_JS)
            log.debug(
                "page lists %s; %d clues",
                ", ".join(f"{len(m)} {c}" for c, m in payload["categories"].items()),
                len(payload["clues"]),
            )
            log.debug(
                "card data for %s",
                ", ".join(f"{len(d)} {c}" for c, d in payload["details"].items()),
            )
            owner, clues = _resolve_evidence_links(
                page,
                payload["clues"],
                payload["categories"],
                payload["details"].get("suspects", {}),
                fetch.reveal_wait_ms,
            )
        finally:
            browser.close()

    if owner:
        clues = [re.sub(r"This fingerprint", f"{owner}'s fingerprint", c) for c in clues]

    return Puzzle(categories=payload["categories"], clues=clues, details=payload["details"])


# A clue can hide a second, nested link inside its evidence-link anchor - e.g. a real
# fingerprint print, or a quoted phrase like "...famous 'unauthorized autobiography'."
# Clicking it swaps the whole page for a reveal (a print image, or a book excerpt) that
# names what the clue text itself never states outright.
EVIDENCE_LINK_SELECTOR = "#evidence strong a a"

BACK_BUTTON_SELECTOR = "input[value='BACK TO MAIN']"


def _match_member(text: str, categories: dict[str, list[str]]) -> str | None:
    """The longest category member named in the text, e.g. an excerpt naming a location."""
    lowered = text.lower()
    best: str | None = None
    for members in categories.values():
        for member in members:
            if re.search(rf"\b{re.escape(member.lower())}\b", lowered) and (
                best is None or len(member) > len(best)
            ):
                best = member
    return best


def _resolve_evidence_links(
    page,
    clues: list[str],
    categories: dict[str, list[str]],
    suspects: dict[str, dict],
    wait_ms: int,
) -> tuple[str | None, list[str]]:
    """Click each nested evidence link once, reading a print reveal or an excerpt reveal."""
    clues = list(clues)
    owner: str | None = None
    count = len(page.query_selector_all(EVIDENCE_LINK_SELECTOR))
    log.debug("%d nested evidence links", count)

    for _ in range(count):
        links = page.query_selector_all(EVIDENCE_LINK_SELECTOR)
        if not links:
            break
        link = links[0]
        index = link.evaluate(
            "el => Array.prototype.indexOf.call("
            "document.querySelectorAll('#evidence strong'), el.closest('strong'))"
        )
        link.click()
        page.wait_for_timeout(wait_ms)

        found_print = page.evaluate(FOUND_PRINT_JS)
        if found_print:
            filename = found_print.rsplit("/", 1)[-1]
            for name, characteristics in suspects.items():
                if characteristics.get("print") == filename:
                    owner = name
                    log.debug("clue %d: print %s is %s's", index + 1, filename, name)
                    break
            else:
                log.debug("clue %d: print %s matches no suspect", index + 1, filename)
        else:
            member = _match_member(page.inner_text("body"), categories)
            log.debug("clue %d: reveal names %s", index + 1, member or "no member")
            if member and 0 <= index < len(clues):
                clues[index] = f"{clues[index]} (This coincidentally refers to {member}.)"

        back = page.query_selector(BACK_BUTTON_SELECTOR)
        if back:
            back.click()
            page.wait_for_timeout(wait_ms)

    return owner, clues


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
    log.debug(
        "DOM has %d category dropdowns and %d evidence bullets",
        len(puzzle.categories),
        len(puzzle.clues),
    )
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
            else:
                log.debug("too short for a clue: %r", line)
            continue

        if current and _looks_like_item(line):
            if line not in puzzle.categories[current]:
                puzzle.categories[current].append(line)
        elif current:
            log.debug("not a %s member: %r", current.removesuffix("s"), line)

    return puzzle
