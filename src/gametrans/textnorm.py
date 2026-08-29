"""OCR text cleanup, normalisation and similarity.

Two jobs:

1. Turn a raw OCR read into a clean line worth translating (or reject it as noise).
2. Produce a stable cache key so that "Hello, traveller." and "Hello,  traveller"
   - the same subtitle read one frame apart - hit the same cache entry instead of
   costing two API calls.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, List

# Characters OCR engines commonly hallucinate around glyph edges.
_NOISE_CHARS = "|¦`´^~¬«»_—–·•"

_WS_RE = re.compile(r"\s+")
_REPEAT_PUNCT_RE = re.compile(r"([!?.,;:])\1{2,}")
# A line that is mostly punctuation/symbols is almost always an OCR artefact of
# a HUD icon rather than real dialogue.
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def clean_line(text: str) -> str:
    """Normalise a single OCR line without changing its meaning."""
    if not text:
        return ""
    # Normalise unicode first so lookalike glyphs collapse to one form.
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")
    text = text.strip(_NOISE_CHARS + " \t\r\n")
    text = _REPEAT_PUNCT_RE.sub(r"\1\1\1", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def join_lines(lines: Iterable[str]) -> str:
    """Join OCR lines into one subtitle block.

    Lines are kept separate (newline) rather than space-joined so the model can
    see the original line breaks, which often carry speaker/timing information.
    """
    cleaned = [clean_line(line) for line in lines]
    return "\n".join(line for line in cleaned if line)


def letter_ratio(text: str) -> float:
    """Fraction of characters that are actual letters."""
    if not text:
        return 0.0
    letters = len(_LETTER_RE.findall(text))
    return letters / len(text)


def is_noise(text: str, min_chars: int = 2, min_letter_ratio: float = 0.4) -> bool:
    """True when a read is not worth sending to a translation model."""
    stripped = text.strip()
    if len(stripped) < min_chars:
        return True
    if letter_ratio(stripped) < min_letter_ratio:
        return True
    return False


def cache_key(text: str) -> str:
    """Aggressively normalised key for cache lookups.

    Case, punctuation spacing and repeated whitespace all vary between frames of
    the same subtitle; none of them should cause a cache miss.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def similarity(a: str, b: str) -> float:
    """0..1 similarity between two OCR reads, on their normalised forms."""
    ka, kb = cache_key(a), cache_key(b)
    if not ka and not kb:
        return 1.0
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def is_extension_of(previous: str, current: str) -> bool:
    """True when `current` looks like `previous` with more text appended.

    Games that type dialogue out character by character produce exactly this
    pattern. Detecting it lets the pipeline wait for the line to finish instead
    of translating - and paying for - every intermediate prefix.
    """
    prev_key, cur_key = cache_key(previous), cache_key(current)
    if not prev_key or prev_key == cur_key:
        return False
    return cur_key.startswith(prev_key)


def split_sentences(text: str) -> List[str]:
    """Split a block into sentences, keeping terminators attached."""
    parts = re.split(r"(?<=[.!?…])\s+", text.replace("\n", " "))
    return [p.strip() for p in parts if p.strip()]


# -- Persian output normalisation -------------------------------------------

# Models occasionally emit the Arabic forms of these letters. They render with
# the wrong shapes in Persian text and break word search, so fold them back.
_ARABIC_TO_PERSIAN = {
    "ي": "ی",  # ARABIC YEH -> FARSI YEH
    "ى": "ی",  # ALEF MAKSURA -> FARSI YEH
    "ك": "ک",  # ARABIC KAF -> KEHEH
    "ة": "ه",  # TEH MARBUTA -> HEH
    "ھ": "ه",  # HEH DOACHASHMEE -> HEH
    "ً": "",        # tanween marks - noise in modern Persian
    "ٌ": "",
    "ٍ": "",
    "ـ": "",        # tatweel / kashida
}

_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹")

# Wrappers a model may add around the answer despite being told not to.
_WRAPPER_PREFIXES = (
    "translation:",
    "translated:",
    "persian:",
    "farsi:",
    "ترجمه:",
    "ترجمه فارسی:",
)


def normalize_persian(text: str) -> str:
    """Fold Arabic letter forms to their Persian equivalents."""
    if not text:
        return ""
    for src, dst in _ARABIC_TO_PERSIAN.items():
        text = text.replace(src, dst)
    return text.translate(_ARABIC_INDIC_DIGITS)


def sanitize_translation(text: str, persian: bool = True) -> str:
    """Strip the wrappers models add around a translation.

    Cheap insurance: the system prompt forbids all of these, but a model that
    slips once would otherwise put `"سلام"` - quotes included - on screen.
    """
    if not text:
        return ""

    text = text.strip()

    # Fenced code block.
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

    lowered = text.lower()
    for prefix in _WRAPPER_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    # Matched surrounding quotes, only when they wrap the whole thing.
    for opener, closer in (('"', '"'), ("'", "'"), ("«", "»"), ("“", "”")):
        if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
            inner = text[1:-1]
            if opener not in inner and closer not in inner:
                text = inner.strip()
            break

    if persian:
        text = normalize_persian(text)

    return text.strip()


_PERSIAN_RANGE_RE = re.compile(r"[؀-ۿﭐ-﷿ﹰ-﻿]")


def contains_persian(text: str) -> bool:
    """True when the text contains Arabic-script characters.

    Used as a feedback-loop guard: if the overlay somehow ends up inside the
    captured region, its own Persian output would be re-OCR'd and re-translated
    forever. Any Arabic-script read is rejected before it reaches the API.
    """
    return bool(_PERSIAN_RANGE_RE.search(text))
