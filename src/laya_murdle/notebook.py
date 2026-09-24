"""The logic grid: the solved notebook and the step-by-step deduction frames."""

from __future__ import annotations

import re

from laya_murdle.config import GridConfig
from laya_murdle.models import PEOPLE_CATEGORY, Clue, Puzzle
from laya_murdle.solver import build_problem, var_name


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


def _short(name: str, width: int) -> str:
    return re.sub(r"^(?:a|an|the)\s+", "", name.strip(), flags=re.IGNORECASE)[:width]


def _grid_categories(puzzle: Puzzle) -> list[str]:
    return [PEOPLE_CATEGORY] + [
        name
        for name, members in puzzle.categories.items()
        if name != PEOPLE_CATEGORY and members
    ]


def owners_of(puzzle: Puzzle, solution: dict[str, str], people: list[str]) -> dict[str, dict[str, str]]:
    """Which suspect each member belongs to, in one solution."""
    owners = {PEOPLE_CATEGORY: {person: person for person in people}}
    for category in _grid_categories(puzzle)[1:]:
        owners[category] = {solution[var_name(category, person)]: person for person in people}
    return owners


def render_grid(puzzle: Puzzle, people: list[str], mark, grid: GridConfig = GridConfig()) -> str:
    """Draw the logic grid, asking `mark` for the contents of every cell."""
    categories = _grid_categories(puzzle)
    if len(categories) < 2:
        return ""

    def short(name: str) -> str:
        return _short(name, grid.name_width)

    groups = [(category, puzzle.categories[category]) for category in categories[1:]]
    cell = max(len(short(member)) for _, members in groups for member in members) + 1
    label = max(
        len(short(member))
        for category in categories[:-1]
        for member in puzzle.categories[category]
    ) + 1

    titles = " " * label
    heads = " " * label
    for category, members in groups:
        titles += "|" + category.upper().center(len(members) * cell)
        heads += "|" + "".join(short(member).center(cell) for member in members)
    rule = "-" * len(heads)

    lines = [titles, heads, rule]
    for depth, row_category in enumerate(categories[:-1]):
        for member in puzzle.categories[row_category]:
            row = short(member).ljust(label)
            for category, members in groups:
                if categories.index(category) <= depth:
                    row += "|" + " " * (len(members) * cell)
                    continue
                row += "|" + "".join(
                    mark(row_category, member, category, other).center(cell)
                    for other in members
                )
            lines.append(row.rstrip())
        lines.append(rule)

    return "\n".join(lines)


def format_notebook(
    puzzle: Puzzle,
    solution: dict[str, str],
    people: list[str],
    grid: GridConfig = GridConfig(),
) -> str:
    """Render the filled-in logic grid, the way murdle's own notebook lays it out."""
    owners = owners_of(puzzle, solution, people)

    def mark(row_category, member, category, other):
        same = owners[row_category][member] == owners[category][other]
        return grid.yes if same else grid.no

    return render_grid(puzzle, people, mark, grid)


def deduction_frames(
    puzzle: Puzzle,
    clues: list[Clue],
    grid: GridConfig = GridConfig(),
) -> list[tuple[str, str]]:
    """One grid per clue, showing only what the clues so far actually force.

    A cell is ticked or crossed when every remaining solution agrees on it, and left
    blank while both are still possible - the same reasoning a player writes down.
    """
    people = puzzle.categories[PEOPLE_CATEGORY]
    frames = []

    for step in range(len(clues) + 1):
        problem, _ = build_problem(puzzle, clues[:step])
        solutions = problem.getSolutions()
        if not solutions:
            break

        owners = [owners_of(puzzle, solution, people) for solution in solutions]

        def mark(row_category, member, category, other, owners=owners):
            agree = {o[row_category][member] == o[category][other] for o in owners}
            if agree == {True}:
                return grid.yes
            if agree == {False}:
                return grid.no
            return grid.unknown

        title = "No clues yet" if step == 0 else clues[step - 1].text
        plural = "" if len(solutions) == 1 else "s"
        caption = f"{step}/{len(clues)}  {len(solutions)} possible solution{plural}"
        frames.append((f"{title}\n{caption}", render_grid(puzzle, people, mark, grid)))

    return frames
