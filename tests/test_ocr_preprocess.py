"""OCR preprocessing: the upscale ceiling and image preparation."""

import numpy as np
import pytest

from gametrans.config import OcrConfig
from gametrans.ocr import effective_upscale, preprocess


def frame(width, height):
    return np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)


# -- the pixel ceiling -------------------------------------------------------


def test_small_band_gets_the_full_requested_upscale():
    """A 1080p subtitle band is where upscaling actually buys accuracy."""
    cfg = OcrConfig(upscale=2.0, max_pixels=2_000_000)
    assert effective_upscale(1600, 90, cfg) == 2.0


def test_large_region_is_capped_below_the_request():
    """On a high-resolution display the glyphs are already big; enlarging them
    costs time for nothing."""
    cfg = OcrConfig(upscale=2.0, max_pixels=2_000_000)
    factor = effective_upscale(2580, 480, cfg)
    assert 1.0 < factor < 2.0
    assert 2580 * factor * 480 * factor <= 2_000_000 * 1.01


def test_region_already_over_the_ceiling_is_not_upscaled():
    cfg = OcrConfig(upscale=2.0, max_pixels=2_000_000)
    assert effective_upscale(3200, 1000, cfg) == 1.0


def test_upscale_of_one_or_less_is_left_alone():
    for requested in (1.0, 0.0, 0.5):
        cfg = OcrConfig(upscale=requested, max_pixels=2_000_000)
        assert effective_upscale(100, 100, cfg) == 1.0


def test_ceiling_can_be_disabled():
    cfg = OcrConfig(upscale=2.0, max_pixels=0)
    assert effective_upscale(2580, 480, cfg) == 2.0


def test_ceiling_never_shrinks_the_image():
    """Capping must reduce the upscale, never turn it into a downscale."""
    cfg = OcrConfig(upscale=3.0, max_pixels=1000)
    assert effective_upscale(2000, 1000, cfg) == 1.0


# -- preprocess --------------------------------------------------------------


def test_preprocess_enlarges_a_small_band():
    cfg = OcrConfig(upscale=2.0, max_pixels=2_000_000)
    out = preprocess(frame(800, 60), cfg)
    assert out.shape[:2] == (120, 1600)


def test_preprocess_bounds_the_output_of_a_large_region():
    cfg = OcrConfig(upscale=2.0, max_pixels=2_000_000)
    out = preprocess(frame(2580, 480), cfg)
    assert out.shape[0] * out.shape[1] <= 2_000_000 * 1.01


def test_preprocess_keeps_three_channels():
    cfg = OcrConfig(upscale=2.0)
    assert preprocess(frame(400, 40), cfg).shape[2] == 3


def test_binarize_produces_two_values():
    cfg = OcrConfig(upscale=1.0, binarize=True, binarize_threshold=128)
    out = preprocess(frame(400, 40), cfg)
    assert set(np.unique(out)).issubset({0, 255})
    assert out.shape[2] == 3


@pytest.mark.parametrize("size", [(1, 1), (5, 3), (1920, 1)])
def test_preprocess_survives_degenerate_sizes(size):
    cfg = OcrConfig(upscale=2.0)
    out = preprocess(frame(*size), cfg)
    assert out.ndim == 3 and out.shape[2] == 3
