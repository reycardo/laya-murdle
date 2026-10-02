"""Unscrambling notes whose words are anagrams ("HTE LLSME FO NOLMASD ...")."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from laya_murdle.clues import Attributes
from laya_murdle.config import AnagramConfig
from laya_murdle.models import Puzzle

# Most frequent English words, most frequent first. Settles collisions the word list
# can't rank: was/saw, the/het, of/fo, on/no, who/how, from/form.
COMMON_WORDS: tuple[str, ...] = (
    "the", "of", "and", "to", "a", "in", "is", "it", "you", "that", "he", "was", "for",
    "on", "are", "with", "as", "i", "his", "they", "be", "at", "one", "have", "this",
    "from", "or", "had", "by", "not", "but", "what", "some", "we", "can", "out", "other",
    "were", "all", "there", "when", "up", "use", "your", "who", "how", "said", "an",
    "each", "she", "which", "do", "their", "time", "if", "will", "way", "about", "many",
    "then", "them", "would", "like", "so", "these", "her", "long", "make", "thing", "see",
    "him", "two", "has", "its", "look", "more", "day", "could", "go", "come", "did", "no",
    "most", "people", "my", "over", "know", "than", "call", "first", "may", "down",
    "side", "been", "now", "find", "any", "new", "work", "part", "take", "get", "place",
    "made", "live", "where", "after", "back", "little", "only", "man", "year", "came",
    "show", "every", "good", "me", "give", "our", "under", "name", "very", "through",
    "just", "great", "think", "say", "help", "before", "turn", "same", "mean", "right",
    "old", "too", "does", "tell", "three", "want", "well", "also", "small", "end", "put",
    "home", "read", "hand", "large", "even", "here", "must", "big", "high", "such", "why",
    "ask", "went", "light", "off", "need", "house", "again", "near", "own", "below",
    "last", "never", "left", "saw",
)

# The system word list has few inflections: "almond" but not "almonds".
SUFFIXES = ("s", "es", "ed", "d", "ing")

UPPER_WORD = re.compile(r"[A-Z]+(?:'[A-Z]+)?")
# A run of upper-case words, like the body of "a note that read: HTE LLSME FO ...".
UPPER_RUN = re.compile(rf"\b{UPPER_WORD.pattern}(?:[ ,;\-]+{UPPER_WORD.pattern})+\b")
# Ordinary prose; upper-case words are left out since they may be the scrambled ones.
PROSE_WORD = re.compile(r"\b[A-Za-z]*[a-z][A-Za-z]*\b")

Index = dict[str, list[str]]

log = logging.getLogger(__name__)


def signature(word: str) -> str:
    """Anagrams share their sorted letters: 'hte' and 'the' are both 'eht'."""
    return "".join(sorted(word.lower()))


def _add(index: Index, word: str) -> None:
    words = index.setdefault(signature(word), [])
    if word not in words:
        words.append(word)


@lru_cache
def load_dictionary(path: str) -> Index:
    """Index a word list by signature. A missing file gives an empty index."""
    file = Path(path).expanduser()
    if not file.is_file():
        log.debug("no word list at %s", file)
        return {}
    words = [word for word in file.read_text(errors="ignore").split() if word.isalpha()]
    # Capitalised entries are mostly proper nouns ("Fo", "Het"), so they rank last.
    words.sort(key=lambda word: not word.islower())
    index: Index = {}
    for word in words:
        _add(index, word.lower())
    log.debug("%d words from %s", len(words), file)
    return index


def build_vocabulary(puzzle: Puzzle, attributes: Attributes) -> Index:
    """Index the puzzle's own words first, then common words, then words from its prose."""
    names = [member for members in puzzle.categories.values() for member in members]
    names += [term for terms in attributes.values() for term in terms]
    words = re.findall(r"[a-z]+", " ".join(names).lower())
    words += COMMON_WORDS
    words += [word.lower() for clue in puzzle.clues for word in PROSE_WORD.findall(clue)]

    vocabulary: Index = {}
    for word in words:
        _add(vocabulary, word)
    return vocabulary


def _lookup(key: str, vocabulary: Index, dictionary: Index) -> list[str]:
    return list(dict.fromkeys([*vocabulary.get(key, ()), *dictionary.get(key, ())]))


def _without(letters: str, suffix: str) -> str | None:
    for letter in suffix:
        if letter not in letters:
            return None
        letters = letters.replace(letter, "", 1)
    return letters


def candidates(word: str, vocabulary: Index, dictionary: Index) -> list[str]:
    """Every known word spelled with exactly the letters of `word`, best first."""
    letters = signature(word)
    found = _lookup(letters, vocabulary, dictionary)
    if found:
        return found
    for suffix in SUFFIXES:
        base = _without(letters, suffix)
        if base:
            found += [stem + suffix for stem in _lookup(base, vocabulary, dictionary)]
    return list(dict.fromkeys(found))


def _stem(word: str) -> tuple[str, str]:
    # "TCEPSUS'S": unscramble the part before the apostrophe and keep the rest.
    stem, apostrophe, rest = word.partition("'")
    return stem, apostrophe + rest


def _is_word(word: str, vocabulary: Index, dictionary: Index) -> bool:
    stem, _ = _stem(word)
    return stem.lower() in candidates(stem, vocabulary, dictionary)


def _unscramble(word: str, vocabulary: Index, dictionary: Index) -> str:
    stem, rest = _stem(word)
    found = candidates(stem, vocabulary, dictionary)
    if len(found) > 1:
        log.debug("  %s: %s", stem, ", ".join(found))
    elif not found:
        log.debug("  %s: no known word, kept", stem)
    return found[0].upper() + rest if found else word


def decode_clue(text: str, vocabulary: Index, dictionary: Index, config: AnagramConfig) -> str:
    """Unscramble each run of upper-case words that is mostly not real words."""

    def decode_run(match: re.Match) -> str:
        run = match.group()
        words = UPPER_WORD.findall(run)
        unknown = sum(not _is_word(word, vocabulary, dictionary) for word in words)
        scrambled = (
            len(words) >= config.min_words and unknown >= config.scrambled_share * len(words)
        )
        log.debug(
            "%r: %d of %d not words -> %s",
            run, unknown, len(words), "unscramble" if scrambled else "keep",
        )
        if not scrambled:
            return run
        return UPPER_WORD.sub(
            lambda word: _unscramble(word.group(), vocabulary, dictionary), run
        )

    return UPPER_RUN.sub(decode_run, text)


def decode_anagrams(
    puzzle: Puzzle,
    attributes: Attributes,
    config: AnagramConfig = AnagramConfig(),
) -> tuple[Puzzle, list[tuple[str, str]]]:
    """Rewrite scrambled notes in the clues, returning the puzzle and what changed."""
    if not any(UPPER_RUN.search(clue) for clue in puzzle.clues):
        return puzzle, []

    vocabulary = build_vocabulary(puzzle, attributes)
    dictionary = load_dictionary(config.dictionary)
    clues = [decode_clue(text, vocabulary, dictionary, config) for text in puzzle.clues]
    changed = [(old, new) for old, new in zip(puzzle.clues, clues) if old != new]
    return replace(puzzle, clues=clues), changed
