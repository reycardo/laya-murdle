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

## Usage

```bash
uv run laya-murdle --sample          # built-in puzzle, good for a smoke test
uv run laya-murdle                   # fetch https://murdle.com
uv run laya-murdle --url <puzzle-url>
uv run laya-murdle --file puzzle.txt # or a saved .html page
uv run laya-murdle --no-preload      # lazy-load the Laya checkpoints
```

The command prints the parsed categories, each clue as Laya classified it (with its
probability), and the solution found by the constraint solver.

murdle.com builds its puzzle in the browser, so a plain HTTP fetch often returns no
puzzle text. When that happens, save the rendered page (or paste the puzzle into a
`.txt` file) and pass it with `--file`.

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
