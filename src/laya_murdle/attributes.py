"""Turning the card data into the attribute phrases murdle clues use."""

from __future__ import annotations

from laya_murdle.models import Puzzle

ROLE_TERMS = {
    "clergy": ("clergy", "religious"),
    "business": ("business",),
    "government": ("government", "politician"),
    "education": ("education", "educator", "teacher"),
    "army": ("army", "military", "soldier"),
    "noble": ("noble", "nobility", "royal"),
}

FEATURE_PREFIXES = ("in", "at", "on", "by", "beneath", "under", "near", "a", "an", "the", "some")


def _strip_feature(feature: str) -> str:
    words = feature.lower().split()
    while words and words[0] in FEATURE_PREFIXES:
        words.pop(0)
    return " ".join(words)


def suspect_terms(characteristics: dict) -> list[str]:
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
    terms += list(details.get("materials") or [])
    terms += list(details.get("method") or [])
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
        "suspects": suspect_terms,
        "weapons": weapon_terms,
        "locations": location_terms,
    }

    index: dict[str, dict[str, set[str]]] = {}
    for category, builder in builders.items():
        for name, detail in (puzzle.details.get(category) or {}).items():
            for term in builder(detail):
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
