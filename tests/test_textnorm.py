import pytest

from gametrans.textnorm import (
    cache_key,
    clean_line,
    contains_persian,
    is_extension_of,
    is_noise,
    join_lines,
    normalize_persian,
    sanitize_translation,
    similarity,
    split_sentences,
)


def test_clean_line_strips_ocr_noise():
    assert clean_line("  |Hello, traveller!  ") == "Hello, traveller!"
    assert clean_line("Hello   there") == "Hello there"
    assert clean_line("") == ""


def test_clean_line_collapses_repeated_punctuation():
    assert clean_line("What?????") == "What???"


def test_join_lines_drops_empties():
    assert join_lines(["one", "", "  ", "two"]) == "one\ntwo"


@pytest.mark.parametrize(
    "text,expected",
    [("...", True), ("->", True), ("a", True), ("Hi", False), ("Go north", False)],
)
def test_is_noise(text, expected):
    assert is_noise(text) is expected


def test_cache_key_ignores_case_and_punctuation():
    assert cache_key("Hello, TRAVELLER!!") == cache_key("hello traveller")


def test_similarity():
    assert similarity("Hello, traveller.", "Hello  traveller") == 1.0
    assert similarity("Hello there", "Goodbye now") < 0.4
    assert similarity("", "") == 1.0
    assert similarity("x", "") == 0.0


def test_is_extension_of_detects_typewriter_text():
    assert is_extension_of("You must go", "You must go to the castle") is True
    assert is_extension_of("You must go", "You must run") is False
    assert is_extension_of("Same", "Same") is False


def test_normalize_persian_folds_arabic_forms():
    assert normalize_persian("كتاب ياد") == "کتاب یاد"
    assert normalize_persian("٤٢") == "۴۲"


def test_sanitize_strips_quotes_prefixes_and_fences():
    assert sanitize_translation('"سلام"') == "سلام"
    assert sanitize_translation("Translation: سلام") == "سلام"
    assert sanitize_translation("```\nسلام\n```") == "سلام"
    assert sanitize_translation("«سلام»") == "سلام"


def test_sanitize_keeps_internal_quotes():
    text = 'او گفت "برو" و رفت'
    assert sanitize_translation(text) == text


def test_contains_persian_guards_the_feedback_loop():
    assert contains_persian("سلام مسافر") is True
    assert contains_persian("Hello traveller") is False
    assert contains_persian("Press E") is False


def test_split_sentences():
    assert split_sentences("One thing. Two things! Three?") == [
        "One thing.",
        "Two things!",
        "Three?",
    ]
