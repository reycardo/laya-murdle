"""Reading each clue: what it refers to, and whether it affirms or denies it."""

from __future__ import annotations

import logging
import re

from laya_murdle.attributes import build_attribute_index
from laya_murdle.checkpoint import LAYA_MODEL
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

# Asked once per category when the text matching finds nothing to pair. Each option carries
# the member's card terms, which lets Laya resolve "the clergyman" or "the stained glass".
REFERENCE_QUESTION = "Which {kind} is named or described in the clue?"
NO_REFERENCE = "none"

Attributes = dict[str, dict[str, frozenset[str]]]

log = logging.getLogger(__name__)


def _show(mention: Mention) -> str:
    """'locations:Chapel', or 'weapons:metal (Candlestick, Heavy Wrench)' for an attribute."""
    shown = f"{mention.category}:{mention.label}"
    if not mention.named:
        shown += f" ({', '.join(sorted(mention.members))})"
    return shown


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
    answers = router.predict({"clue": clue}, questions, model=LAYA_MODEL)["answers"]
    scores = [
        1.0 - answers[name]["noul"] if inverted else answers[name]["noul"]
        for name, (_, _, inverted) in CLAIM_PROBES.items()
    ]
    log.debug(
        "  probes: %s -> %.2f",
        " ".join(
            f"{name}={'1-' if inverted else ''}{answers[name]['noul']:.2f}"
            for name, (_, _, inverted) in CLAIM_PROBES.items()
        ),
        sum(scores) / len(scores),
    )
    return sum(scores) / len(scores)


def guess_mentions(
    clue: str,
    categories: dict[str, list[str]],
    attributes: Attributes,
    router,
    floor: float,
) -> list[Mention]:
    """Ask Laya which member of each category the clue refers to, most confident first."""
    terms_of: dict[tuple[str, str], dict[str, str]] = {}
    for category, terms in attributes.items():
        for term, members in terms.items():
            for member in members:
                # "left-handed" and "left handed" read the same, so keep one.
                terms_of.setdefault((category, member), {}).setdefault(term.replace("-", " "), term)

    questions = {}
    for category, members in categories.items():
        kind = category.removesuffix("s")
        criteria = {}
        for member in members:
            terms = terms_of.get((category, member), {}).values()
            detail = f" ({', '.join(terms)})" if terms else ""
            criteria[member] = f"the clue is about {member}{detail}"
        criteria[NO_REFERENCE] = f"no {kind} is mentioned"
        questions[category] = {
            "type": "choice",
            "instructions": REFERENCE_QUESTION.format(kind=kind),
            "criteria": criteria,
        }

    answers = router.predict({"clue": clue}, questions, model=LAYA_MODEL)["answers"]
    guesses = []
    for category, answer in answers.items():
        member = answer["choice"]
        probability = answer["probabilities"][member]
        ranked = sorted(answer["probabilities"].items(), key=lambda item: -item[1])
        accepted = member != NO_REFERENCE and probability >= floor
        log.debug(
            "  laya %s: %s -> %s",
            category,
            ", ".join(f"{option} {p:.2f}" for option, p in ranked[:3]),
            member if accepted else f"nothing (floor {floor:.2f})",
        )
        if accepted:
            guesses.append(Mention(category, frozenset({member}), member, True, probability))
    return sorted(guesses, key=lambda mention: -mention.guessed)


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
            log.debug("  either-or half has no pairing, read as a plain clue: %s", half.strip())
            return None
        pairings.append(pairing)
    return pairings


def classify_clues(
    puzzle: Puzzle,
    router,
    attributes: Attributes | None = None,
    laya: LayaConfig = LayaConfig(),
) -> tuple[list[Clue], list[str]]:
    """Turn each clue into pairings of members, with a polarity decided by Laya.

    Members come from the clue text. When it names nothing to pair, Laya is asked what the
    clue refers to - except for the last clue, which usually names the scene (`find_scene`).
    """
    attributes = build_attribute_index(puzzle) if attributes is None else attributes
    parsed: list[Clue] = []
    skipped: list[str] = []
    last = len(puzzle.clues) - 1

    for index, text in enumerate(puzzle.clues):
        log.debug("clue %d: %s", index + 1, text)
        alternatives = split_xor(text, puzzle.categories, attributes)
        if alternatives:
            log.debug("  either-or, laya not asked")
            parsed.append(
                Clue(text=text, pairings=alternatives, positive=True, confidence=1.0,
                     cues=["xor"], kind="xor")
            )
            continue

        mentions = find_mentions(text, puzzle.categories, attributes)
        log.debug("  mentions: %s", ", ".join(map(_show, mentions)) or "none")
        pairing = first_pairing(mentions)
        if not pairing and index != last:
            log.debug("  nothing to pair, asking laya what it refers to")
            seen = {mention.category for mention in mentions}
            guesses = guess_mentions(
                text, puzzle.categories, attributes, router, laya.reference_floor
            )
            pairing = first_pairing(mentions + [m for m in guesses if m.category not in seen])
        if not pairing:
            log.debug("  skipped%s", " (last clue, read as the scene)" if index == last else "")
            skipped.append(text)
            continue

        log.debug("  pairing: %s ~ %s", _show(pairing.left), _show(pairing.right))
        score = score_pairing(text, pairing.left.label, pairing.right.label, router)

        cues = []
        if NEGATION_CUE.search(text):
            cues.append("negation")
        if THIRD_PARTY_CUE.search(text) and any(
            mention.named and mention.category == PEOPLE_CATEGORY
            for mention in (pairing.left, pairing.right)
        ):
            cues.append("third-party")

        threshold = laya.negated_override if cues else laya.affirmed_floor
        positive = score >= threshold
        log.debug(
            "  %s: %.2f %s %.2f -> %s",
            f"cues {', '.join(cues)}" if cues else "no cues",
            score,
            ">=" if positive else "<",
            threshold,
            "affirms" if positive else "denies",
        )
        confidence = score if positive else 1.0 - score
        # A guessed member is less certain than a named one, so the solver drops it sooner.
        for mention in (pairing.left, pairing.right):
            if mention.guessed is not None:
                confidence *= mention.guessed
        parsed.append(
            Clue(
                text=text,
                pairings=[pairing],
                positive=positive,
                confidence=confidence,
                cues=cues,
            )
        )

    return parsed, skipped


def find_scene(
    puzzle: Puzzle,
    attributes: Attributes,
    skipped: list[str],
    router=None,
    laya: LayaConfig = LayaConfig(),
) -> Mention | None:
    """The final clue names where/how the murder happened, which names the murderer.

    Only a final clue that yielded no pairing is treated this way - otherwise it is an
    ordinary grid clue. If the text names no single member, Laya is asked.
    """
    if not puzzle.clues or puzzle.clues[-1] not in skipped:
        log.debug("no scene: the last clue is an ordinary grid clue")
        return None
    for mention in find_mentions(puzzle.clues[-1], puzzle.categories, attributes):
        if len(mention.members) == 1:
            log.debug("scene from the text: %s", _show(mention))
            return mention
    if router is None:
        log.debug("no scene: the last clue names no single member")
        return None
    log.debug("last clue names no single member, asking laya: %s", puzzle.clues[-1])
    guesses = guess_mentions(
        puzzle.clues[-1], puzzle.categories, attributes, router, laya.reference_floor
    )
    # A guessed suspect would name the murderer outright, bypassing the grid.
    guesses = [mention for mention in guesses if mention.category != PEOPLE_CATEGORY]
    log.debug("scene from laya: %s", _show(guesses[0]) if guesses else "none")
    return guesses[0] if guesses else None


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
        if mention.guessed is not None:
            detail += f"  [laya read {mention.label}: {mention.guessed:.2f}]"
        elif not mention.named:
            detail += f"  [{mention.label} -> {', '.join(sorted(mention.members))}]"
    return (
        f"  [{clue.confidence:.2f}] {left.label} {relation} {right.label}{cues}"
        f"  <- {clue.text}{detail}"
    )
