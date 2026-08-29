import numpy as np
import pytest

from gametrans.changedet import (
    ChangeDetector,
    dhash,
    downscale,
    hamming,
    is_probably_blank,
    to_grayscale,
)


def blank(h=120, w=800, value=0):
    return np.full((h, w, 3), value, dtype=np.uint8)


def with_text(base=None):
    frame = blank() if base is None else base.copy()
    frame[40:80, 100:600] = 255
    return frame


def test_first_frame_always_counts_as_changed():
    detector = ChangeDetector()
    assert detector.check(blank()).changed is True


def test_identical_frames_are_skipped():
    detector = ChangeDetector()
    frame = with_text()
    detector.check(frame)
    result = detector.check(frame)
    assert result.changed is False
    assert result.hash_distance == 0


def test_new_text_is_detected():
    detector = ChangeDetector()
    detector.check(blank())
    assert detector.check(with_text()).changed is True


def test_reference_only_advances_on_accepted_change():
    """A slow fade must not creep past the gate one sub-threshold step at a time."""
    detector = ChangeDetector(hash_threshold=4, pixel_threshold=6.0)
    base = blank()
    detector.check(base)

    faded = base
    changed_at = None
    for step in range(1, 40):
        faded = blank()
        faded[40:80, 100:600] = step * 4  # ramp the text in very gradually
        if detector.check(faded).changed:
            changed_at = step
            break

    assert changed_at is not None, "a gradual fade-in must eventually be detected"


def test_reset_clears_history():
    detector = ChangeDetector()
    frame = with_text()
    detector.check(frame)
    assert detector.check(frame).changed is False
    detector.reset()
    assert detector.check(frame).changed is True


def test_blank_detection():
    assert is_probably_blank(blank()) is True
    assert is_probably_blank(with_text()) is False


@pytest.mark.parametrize("shape", [(32, 128), (97, 533), (16, 64), (200, 1920)])
def test_downscale_shapes(shape):
    gray = to_grayscale(np.random.randint(0, 255, (*shape, 3)).astype(np.uint8))
    assert downscale(gray, 64, 16).shape == (16, 64)


def test_dhash_and_hamming():
    gray = downscale(to_grayscale(with_text()), 64, 16)
    other = downscale(to_grayscale(blank()), 64, 16)
    assert hamming(dhash(gray), dhash(gray)) == 0
    assert hamming(dhash(gray), dhash(other)) > 0


def test_grayscale_accepts_single_channel():
    gray_in = np.full((10, 10), 128, dtype=np.uint8)
    assert to_grayscale(gray_in).shape == (10, 10)
