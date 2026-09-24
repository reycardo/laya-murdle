"""The puzzle and the structures a clue is reduced to."""

from __future__ import annotations

from dataclasses import dataclass, field

# The category whose members the other categories are assigned to.
PEOPLE_CATEGORY = "suspects"


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
