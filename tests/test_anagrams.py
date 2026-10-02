from pathlib import Path

import pytest

from laya_murdle.anagrams import candidates, decode_anagrams, load_dictionary
from laya_murdle.config import AnagramConfig
from laya_murdle.models import Puzzle

SYSTEM_WORDS = AnagramConfig().dictionary
NO_WORDS = AnagramConfig(dictionary="/nonexistent/words")

needs_words = pytest.mark.skipif(
    not Path(SYSTEM_WORDS).is_file(), reason=f"no word list at {SYSTEM_WORDS}"
)


def puzzle(*clues: str) -> Puzzle:
    return Puzzle(
        categories={
            "suspects": ["Captain Slate", "Brother Brownstone"],
            "locations": ["the Cement Truck", "the Chapel"],
        },
        clues=list(clues),
    )


@needs_words
def test_decodes_scrambled_note():
    clue = (
        "A messenger from The Church of the Rolling Gem gave Logico a note that read: "
        "HTE LLSME FO NOLMASD WSA SECROVDIED EIDHBN A ENCEMT RUTCK."
    )
    decoded, changed = decode_anagrams(puzzle(clue), {})
    assert decoded.clues == [
        "A messenger from The Church of the Rolling Gem gave Logico a note that read: "
        "THE SMELL OF ALMONDS WAS DISCOVERED BEHIND A CEMENT TRUCK."
    ]
    assert changed == [(clue, decoded.clues[0])]


@needs_words
def test_plural_missing_from_word_list():
    assert candidates("NOLMASD", {}, load_dictionary(SYSTEM_WORDS))[0] == "almonds"


def test_member_names_decode_without_a_word_list():
    decoded, _ = decode_anagrams(puzzle("It read: NIATPAC ESLAT SAW NI HET LACHPE."), {}, NO_WORDS)
    assert decoded.clues == ["It read: CAPTAIN SLATE WAS IN THE CHAPEL."]


def test_attribute_terms_are_vocabulary():
    attributes = {"weapons": {"bitter almonds": frozenset({"Poisoned Wine"})}}
    decoded, _ = decode_anagrams(puzzle("TTERIB LANDMOS EEWR NI HET LACHPE."), attributes, NO_WORDS)
    assert decoded.clues == ["BITTER ALMONDS WERE IN THE CHAPEL."]


def test_plain_clues_are_untouched():
    clues = (
        "Captain Slate was not in the Chapel.",
        "THE BUTLER DID IT, said the note.",
        "Brother Brownstone saw the CEMENT TRUCK.",
    )
    original = puzzle(*clues)
    decoded, changed = decode_anagrams(original, {}, NO_WORDS)
    assert decoded.clues == list(clues)
    assert changed == []
