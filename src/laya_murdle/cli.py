"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from laya_murdle.animation import write_gif
from laya_murdle.attributes import build_attribute_index
from laya_murdle.clues import classify_clues, describe, find_scene
from laya_murdle.config import Config, load_config
from laya_murdle.models import PEOPLE_CATEGORY, Puzzle
from laya_murdle.notebook import deduction_frames, format_notebook, format_solution
from laya_murdle.solver import accuse, solve
from laya_murdle.sources import (
    SAMPLE_PUZZLE,
    fetch_puzzle_html,
    html_to_text,
    parse_murdle_dom,
    parse_puzzle,
    render_puzzle,
)


def load_puzzle(args: argparse.Namespace, config: Config) -> Puzzle:
    if args.sample:
        return parse_puzzle(SAMPLE_PUZZLE)

    url = args.url or config.fetch.url

    if args.render and not args.file:
        return render_puzzle(url, config.fetch)

    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
        if not args.file.endswith((".html", ".htm")):
            return parse_puzzle(raw)
    else:
        raw = fetch_puzzle_html(url, config.fetch.timeout)

    return parse_murdle_dom(raw) or parse_puzzle(html_to_text(raw))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve a murdle.com puzzle.")
    parser.add_argument("--url", help="puzzle URL to fetch")
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
    parser.add_argument(
        "--gif",
        nargs="?",
        const="",
        metavar="PATH",
        help="write an animated grid, one frame per clue (needs the 'gif' extra)",
    )
    parser.add_argument("--config", type=Path, help="settings file (default: config.toml)")
    return parser.parse_args()


def gif_path(requested: str, config: Config) -> Path:
    if requested:
        return Path(requested)
    return config.output_dir / f"murdle-{date.today():%Y-%m-%d}.gif"


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    puzzle = load_puzzle(args, config)

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
    attributes = build_attribute_index(puzzle)
    clues, skipped = classify_clues(puzzle, router, attributes, config.laya)
    scene = find_scene(puzzle, attributes, skipped)

    print("\nClassified clues:")
    for clue in clues:
        print(describe(clue))
    for text in skipped:
        print(f"  [skipped] {text}")

    solutions, people, dropped = solve(puzzle, clues, config.solver.max_drops)

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

    notebook = format_notebook(puzzle, solutions[0], people, config.grid)
    if notebook:
        print(f"\n{notebook}")

    accusation = None
    if scene:
        murderer = accuse(solutions[0], people, scene)
        if murderer:
            items = ", ".join(
                value
                for key, value in sorted(solutions[0].items())
                if key.endswith(f"::{murderer}")
            )
            accusation = f"Accuse {murderer} ({items})."
            print(f"\n{accusation}")
            print(f"  scene: {scene.label} <- {puzzle.clues[-1]}")

    if args.gif is not None:
        path = gif_path(args.gif, config)
        kept = [clue for clue in clues if clue not in dropped]
        frames = deduction_frames(puzzle, kept, config.grid)
        if accusation and frames:
            frames.append((accusation, frames[-1][1]))
        write_gif(frames, path, config.gif)
        print(f"\nWrote {path} ({len(frames)} frames).")


if __name__ == "__main__":
    main()
