import pytest

from laya_murdle.checkpoint import LAYA_MODEL
from laya_murdle.clues import NO_REFERENCE, classify_clues, find_scene
from laya_murdle.models import Puzzle

CATEGORIES = {
    "suspects": ["Captain Slate", "Brother Brownstone"],
    "weapons": ["Candlestick", "Heavy Wrench"],
    "locations": ["Cement Truck", "Chapel"],
}


class FakeRouter:
    """Answers reference questions from `picks` and every polarity probe with `noul`."""

    def __init__(self, picks: dict[str, tuple[str, float]], noul: float = 0.9):
        self.picks = picks
        self.noul = noul
        self.calls = []

    def predict(self, state, questions, model=None):
        self.calls.append((state["clue"], questions, model))
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"noul": self.noul}
                continue
            choice, probability = self.picks.get(name, (NO_REFERENCE, 0.9))
            rest = (1.0 - probability) / (len(question["criteria"]) - 1)
            probabilities = {option: rest for option in question["criteria"]}
            probabilities[choice] = probability
            answers[name] = {"choice": choice, "probabilities": probabilities}
        return {"answers": answers}


def test_laya_fills_the_side_the_text_missed():
    puzzle = Puzzle(CATEGORIES, ["Slate was in the Chapel.", "The end."])
    router = FakeRouter({"suspects": ("Captain Slate", 0.7)})
    clues, skipped = classify_clues(puzzle, router, {})

    [clue] = clues
    pairing = clue.pairings[0]
    assert {pairing.left.label, pairing.right.label} == {"Chapel", "Captain Slate"}
    assert clue.positive
    # Probes average to 0.7 (one is inverted), then the 0.7 guess discounts it.
    assert clue.confidence == pytest.approx(0.7 * 0.7)
    assert skipped == ["The end."]
    assert {model for _, _, model in router.calls} == {LAYA_MODEL}


def test_unsure_guesses_are_ignored():
    puzzle = Puzzle(CATEGORIES, ["Slate was in the Chapel.", "The end."])
    clues, skipped = classify_clues(puzzle, FakeRouter({"suspects": ("Captain Slate", 0.4)}), {})
    assert clues == []
    assert skipped == puzzle.clues


def test_text_pairings_skip_the_reference_question():
    puzzle = Puzzle(CATEGORIES, ["Captain Slate was in the Chapel.", "The end."])
    router = FakeRouter({})
    classify_clues(puzzle, router, {})
    assert all("suspects" not in questions for _, questions, _ in router.calls)


def test_last_clue_is_left_for_the_scene():
    puzzle = Puzzle(CATEGORIES, ["The body was found next to the truck."])
    router = FakeRouter({"suspects": ("Captain Slate", 0.8), "locations": ("Cement Truck", 0.7)})
    clues, skipped = classify_clues(puzzle, router, {})
    assert clues == []

    scene = find_scene(puzzle, {}, skipped, router)
    assert (scene.category, scene.label, scene.guessed) == ("locations", "Cement Truck", 0.7)


def test_attribute_terms_describe_the_options():
    puzzle = Puzzle(CATEGORIES, ["The clergyman was in the Chapel.", "The end."])
    router = FakeRouter({"suspects": ("Brother Brownstone", 0.8)})
    attributes = {"suspects": {"clergy": frozenset({"Brother Brownstone"})}}
    classify_clues(puzzle, router, attributes)
    criteria = router.calls[0][1]["suspects"]["criteria"]
    assert criteria["Brother Brownstone"] == "the clue is about Brother Brownstone (clergy)"
    assert criteria["Captain Slate"] == "the clue is about Captain Slate"
