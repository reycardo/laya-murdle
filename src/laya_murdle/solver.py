"""The constraint model: variables, clue constraints and the search."""

from __future__ import annotations

from constraint import AllDifferentConstraint, Problem

from laya_murdle.models import PEOPLE_CATEGORY, Clue, Mention, Pairing, Puzzle


def var_name(category: str, person: str) -> str:
    return f"{category}::{person}"


def _pairing_predicate(pairing: Pairing, people: list[str]):
    """Variables plus a test for 'some suspect satisfies both sides of this pairing'."""
    left, right = pairing.left, pairing.right

    if PEOPLE_CATEGORY in (left.category, right.category):
        persons, other = (left, right) if left.category == PEOPLE_CATEGORY else (right, left)
        names = [person for person in people if person in persons.members]
        if not names or other.category == PEOPLE_CATEGORY:
            return None
        variables = [var_name(other.category, person) for person in names]

        def holds(values, wanted=other.members):
            return any(value in wanted for value in values)

        return variables, holds

    variables = [var_name(left.category, person) for person in people]
    variables += [var_name(right.category, person) for person in people]
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
        variables = [var_name(category, person) for person in people]
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


def accuse(solution: dict[str, str], people: list[str], scene: Mention) -> str | None:
    """The murderer is whoever the solution places at the murder scene."""
    member = next(iter(scene.members))
    if scene.category == PEOPLE_CATEGORY:
        return member
    for person in people:
        if solution.get(var_name(scene.category, person)) == member:
            return person
    return None
