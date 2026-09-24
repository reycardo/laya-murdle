"""Reading each clue: what it refers to, and whether it affirms or denies it."""

from __future__ import annotations

import re

from laya_murdle.attributes import build_attribute_index
from laya_murdle.config import LayaConfig
from laya_murdle.models import PEOPLE_CATEGORY, Clue, Mention, Pairing, Puzzle

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

# Laya is strongly biased towards one option for any single phrasing, so each clue is
# probed with several claim wordings (one inverted) and the results are averaged.
CLAIM_PROBES: dict[str, tuple[str, str, bool]] = {
    "pair": ("{a} - {b}", "The clue confirms this pairing rather than ruling it out.", False),
    "linked": ("{a} is linked to {b}", "The clue supports the claim.", False),
    "was_with": ("{a} was with {b}", "The clue supports the claim.", False),
    "ruled_out": ("{a} and {b} are together", "The clue rules out the claim.", True),
}

Attributes = dict[str, dict[str, frozenset[str]]]


def find_mentions(
    clue: str,
    categories: dict[str, list[str]],
    attributes: Attributes | None = None,
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
    attributes: Attributes,
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


def classify_clues(
    puzzle: Puzzle,
    router,
    attributes: Attributes | None = None,
    laya: LayaConfig = LayaConfig(),
) -> tuple[list[Clue], list[str]]:
    """Turn each clue into pairings of members, with a polarity decided by Laya."""
    attributes = build_attribute_index(puzzle) if attributes is None else attributes
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

        positive = score >= (laya.negated_override if cues else laya.affirmed_floor)
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


def find_scene(puzzle: Puzzle, attributes: Attributes, skipped: list[str]) -> Mention | None:
    """The final clue names where/how the murder happened, which names the murderer.

    Only a final clue that yielded no pairing is treated this way - otherwise it is an
    ordinary grid clue.
    """
    if not puzzle.clues or puzzle.clues[-1] not in skipped:
        return None
    for mention in find_mentions(puzzle.clues[-1], puzzle.categories, attributes):
        if len(mention.members) == 1:
            return mention
    return None


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
