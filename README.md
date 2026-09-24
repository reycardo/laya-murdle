# laya-murdle

Solve [murdle.com](https://murdle.com) puzzles automatically.

The pipeline has two stages:

1. **Parse** — the puzzle text (suspects, weapons, locations and the detective's clues)
   is turned into structured, typed data using [Laya](https://github.com/NandhaKishorM/laya),
   a small non-autoregressive decision model. Laya scores a fixed set of options in a
   single forward pass instead of generating text, so clue classification is fast and
   always lands on a valid value.
2. **Solve** — the structured clues are translated into variables and constraints and
   handed to a constraint solver ([python-constraint](https://github.com/python-constraint/python-constraint)),
   which returns the unique assignment of suspect, weapon and location — i.e. the murderer.

## Requirements

- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Python 3.12+ (uv will fetch it if needed)

Laya's model weights are **not** bundled with the package. They are downloaded from
Hugging Face on first use and cached locally (`~/.cache/huggingface`).

## Setup

```bash
git clone <this-repo>
cd laya-murdle
uv sync
```

`uv sync` creates `.venv/` and installs all dependencies from `pyproject.toml` /
`uv.lock`.

To fetch the live puzzle, also install the headless browser used to render it:

```bash
uv sync --extra render
uv run playwright install chromium
```

## Usage

```bash
uv run laya-murdle --sample          # built-in puzzle, good for a smoke test
uv run laya-murdle --render          # today's murdle, rendered in headless Chromium
uv run laya-murdle --file puzzle.html  # a page you saved yourself
uv run laya-murdle --url <puzzle-url>
uv run laya-murdle --no-preload      # lazy-load the Laya checkpoints
```

The command prints the parsed categories, each clue as Laya classified it (with its
probability), and the solution found by the constraint solver.

### Why a plain fetch does not work

murdle.com ships an empty shell and generates the puzzle in the browser ("PLEASE WAIT
WHILE THE DAILY MURDLE IS GENERATED"), so `httpx.get` returns no suspects and no clues.
You need the *rendered* DOM. Two ways to get it:

- `--render` runs the page in headless Chromium and waits for the clue list.
- `--file` takes a page you saved yourself. In Chrome or Edge, open murdle.com, wait
  for the puzzle to appear, then either use **File → Save Page As… → Webpage, Complete**,
  or open DevTools and run `copy(document.documentElement.outerHTML)` in the console and
  paste the result into `puzzle.html`. (Safari's `.webarchive` format will not work.)

Once rendered, the puzzle is read straight from the DOM: every category member appears
in the accusation dropdowns (`select#suspect`, `#weapon`, `#room`, `#motive`) and the
clues are the bullets inside `#evidence`. A plain `.txt` file with `SUSPECTS` /
`WEAPONS` / `LOCATIONS` / `CLUES` headings also works.

To pre-download the Laya checkpoints instead of lazy-loading them on the first
question:

```bash
uv run python -c "from laya import Router; Router(preload=True)"
```

## How it works

### Parsing with Laya

Entity mentions are matched deterministically against the category members scraped from
the page. The ambiguous part — whether a clue *affirms* or *denies* the pairing it
mentions — is scored by Laya.

A single `choice` question does not work here: Laya is very sensitive to phrasing and
will happily answer `affirms` for every clue with one wording and `denies` for every
clue with another. Instead each clue is probed with several differently worded `noul`
(boolean) claims — one of them deliberately inverted — batched into a single
`router.predict` call, and the probabilities are averaged:

```python
questions = {
    "pair":      {"type": "noul", "instructions": "The clue confirms this pairing rather than ruling it out. Claim: Dr. Crimson - Library."},
    "linked":    {"type": "noul", "instructions": "The clue supports the claim. Claim: Dr. Crimson is linked to Library."},
    "was_with":  {"type": "noul", "instructions": "The clue supports the claim. Claim: Dr. Crimson was with Library."},
    "ruled_out": {"type": "noul", "instructions": "The clue rules out the claim. Claim: Dr. Crimson and Library are together."},
}

answers = router.predict({"clue": clue}, questions)["answers"]
```

The averaged score is combined with a lexical negation cue (`not`, `never`, `neither`,
`other than`, …): a clue containing a negation is read as negative unless Laya is
overwhelmingly confident otherwise, and a clue without one is read as positive unless
Laya is confident otherwise. Clues that mention fewer than two categories (ordering or
comparison clues, for example) are reported as skipped.

### Solving with constraints

Each suspect gets one variable per remaining category (weapon, location, motive) over
that category's members, with an all-different constraint per category and one
constraint per classified clue:

```python
from constraint import AllDifferentConstraint, Problem

problem = Problem()
problem.addVariables(["weapons::Dr. Crimson", "weapons::Miss Saffron"], ["knife", "candlestick"])
problem.addConstraint(AllDifferentConstraint())
solution = problem.getSolution()
```

Clues linking a suspect to an item become a simple equality/inequality; clues linking
two items (e.g. "the candlestick was in the greenhouse") become a constraint asserting
that some suspect does — or does not — hold both.

A well-formed murdle has exactly one solution. If the clue set is contradictory the
solver retries after dropping the least confident clues (up to two), and reports which
ones it dropped — those are the clues Laya most likely misread.

### The awkward clue types

The daily murdle does not only use plain "X was in Y" clues:

- **Card-attribute clues** ("a medium-weight weapon", "a bald suspect", "grey eyes",
  "a drafty room") name a *property* rather than a member. `--render` scrapes the card
  data out of the page's own script scope (`suspect_details`, `major_setting.weapons`,
  `major_setting.rooms`) and turns it into an attribute vocabulary, so "medium-weight"
  resolves to the set of weapons with `weight == "medium"`. Every clue side is
  therefore a *set* of members, and a pairing means "some suspect matches both sides".
- **Fingerprint clues** ("This fingerprint was found in the gift shop") are resolved by
  opening the fingerprint evidence page, reading the discovered print image and
  matching it against each suspect's `print` characteristic; the clue text is rewritten
  to name that suspect before Laya sees it.
- **Exclusive-or clues** ("Either … or … (but not both!)") are split on `either … or`
  and become a real XOR constraint over the two alternative pairings. Laya is not asked
  about these — the structure is explicit.
- **Third-party clues** ("Uncle Midnight was flirting with the person who had a
  chainsaw") imply that Midnight is *not* that person. A named suspect plus a
  third-party phrase ("the person who", "whoever", …) is treated like a negation cue.

Attribute clues need the scraped card data, so they only work with `--render`. With
`--file` or a plain `.txt` puzzle they are reported as skipped.

## Development

```bash
uv sync            # install/refresh the environment
uv add <package>   # add a dependency (updates pyproject.toml + uv.lock)
uv run <command>   # run anything inside the project environment
```

## Notes

- Be respectful of murdle.com: fetch a puzzle once and cache it locally rather than
  polling the site.
- Laya is a young project and its API may change; pin the version in `pyproject.toml`
  if you hit breakage.

## Limitations

- Attribute clues depend on the card data scraped by `--render`; without it they are
  skipped and the puzzle usually stays underconstrained.
- The attribute vocabulary is derived from the card fields (weight, materials, hair,
  eyes, handedness, star sign, room feature, …). A murdle phrasing that uses a property
  not covered there will be skipped rather than misread.
- Comparative and ordering clues ("taller than", "north of") are not modelled, beyond
  "tallest" and "shortest".
- The murder-scene clue ("the body was found beneath some housing flyers") is ignored;
  it identifies the scene, not a grid pairing.
