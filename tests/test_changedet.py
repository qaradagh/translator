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
    detector = ChangeDetector(hash_threshold=4)
    base = blank()
    detector.check(base)

    changed_at = None
    for step in range(1, 40):
        faded = blank()
        # Ramp all the way to full brightness, as a real subtitle fade does.
        faded[40:80, 100:600] = min(step * 8, 255)
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



# -- text-signature gate -----------------------------------------------------


def rendered_scene(shift=0, text_id=None, seed=5, h=140, w=900):
    """A frame that looks rendered rather than random: smooth gradients plus
    surface texture, optionally with bright glyph-like strokes over the top.

    White noise is the pathological case for any high-pass filter and nothing
    like a game frame, so it would make this gate look worse than it is.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 60 + 40 * np.sin(xx / 90.0) + 30 * np.cos(yy / 60.0)
    coarse = rng.integers(0, 40, (h // 16 + 1, w // 16 + 1)).astype(np.float32)
    coarse = np.repeat(np.repeat(coarse, 16, 0), 16, 1)[:h, :w]
    frame = np.roll(np.clip(base + coarse, 0, 255), shift, axis=1)
    image = np.repeat(frame[:, :, None], 3, axis=2).astype(np.uint8)

    if text_id is not None:
        strokes = np.random.default_rng(1000 + text_id)
        for x in strokes.integers(40, w - 40, 40):
            image[60:94, x - 2:x + 11] = 10     # dark outline
            image[62:92, x:x + 9] = 245         # bright stroke
    return image


def test_text_signature_isolates_glyphs_from_scenery():
    from gametrans.changedet import text_pixel_ratio

    empty = text_pixel_ratio(rendered_scene(text_id=None))
    with_text = text_pixel_ratio(rendered_scene(text_id=1))
    assert with_text > empty * 5, (
        f"text should dominate the signature (empty={empty:.5f}, text={with_text:.5f})"
    )


def test_panning_background_does_not_trip_the_gate():
    """The whole point: a moving camera behind unchanged text is not a change."""
    detector = ChangeDetector(text_mask=True)
    fired = [
        step
        for step in range(30)
        if detector.check(rendered_scene(shift=step * 3, text_id=1)).changed
    ]
    assert fired == [0], f"only the first frame should count, got {fired}"


def test_new_text_is_still_detected_through_a_moving_background():
    detector = ChangeDetector(text_mask=True)
    for step in range(10):
        detector.check(rendered_scene(shift=step * 3, text_id=1))
    result = detector.check(rendered_scene(shift=10 * 3, text_id=2))
    assert result.changed is True


def test_raw_image_gate_fires_on_every_panning_frame():
    """Documents why the text signature exists rather than the naive compare."""
    detector = ChangeDetector(text_mask=False)
    fired = sum(
        detector.check(rendered_scene(shift=step * 3, text_id=1)).changed
        for step in range(30)
    )
    assert fired > 20, "raw-image comparison should be defeated by camera motion"


def test_blank_detection_sees_through_busy_scenery():
    """A subtitle band over textured ground is not blank by contrast, but it is
    blank by content - and content is the question that matters."""
    assert is_probably_blank(rendered_scene(text_id=None)) is True
    assert is_probably_blank(rendered_scene(text_id=1)) is False


def test_blank_detection_falls_back_to_contrast_when_masking_is_off():
    assert is_probably_blank(blank(), text_mask=False) is True
    assert is_probably_blank(with_text(), text_mask=False) is False


def test_box_blur_preserves_shape():
    from gametrans.changedet import box_blur

    for shape in [(140, 900), (37, 101), (16, 16)]:
        gray = to_grayscale(np.random.randint(0, 255, (*shape, 3)).astype(np.uint8))
        assert box_blur(gray, 9).shape == shape
        assert box_blur(gray, 1).shape == shape
