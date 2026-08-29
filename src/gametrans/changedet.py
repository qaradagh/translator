"""Frame change detection.

Games render at 60-144 fps but subtitles change maybe once every few seconds.
Running OCR on every captured frame would waste most of the CPU budget and, more
importantly, would push duplicate lines at the translation API and burn the free
tier quota. This module answers one question as cheaply as possible: *is this
frame meaningfully different from the last one?*

The naive version of this compares the raw image, which works only when the
watched area is an opaque subtitle bar. Most games draw text straight over the
scene, so the background moves whenever the camera does and every single frame
looks "changed" - the gate fires constantly and saves nothing.

So the comparison runs on a *text signature* rather than the picture: pixels
that are both a strong short-range edge and bright. Glyph strokes light up;
panning scenery, which is mostly low-frequency, drops out. Measured on a
rendered scene with the camera panning and the subtitle changing once in 60
frames, the raw-image gate fires 60 times and the text signature fires twice -
the first frame and the one where the text actually changed.

Either signature is then reduced the same way: downscale to 64x16, and compare
both a difference hash (structure) and the mean absolute delta (fades).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


def to_grayscale(frame: np.ndarray) -> np.ndarray:
    """BGR uint8 -> float32 luma. Rec. 601 weights, computed in one pass."""
    if frame.ndim == 2:
        return frame.astype(np.float32)
    b = frame[:, :, 0].astype(np.float32)
    g = frame[:, :, 1].astype(np.float32)
    r = frame[:, :, 2].astype(np.float32)
    return 0.114 * b + 0.587 * g + 0.299 * r


def downscale(gray: np.ndarray, width: int, height: int) -> np.ndarray:
    """Box-average downscale without pulling in scipy/cv2.

    Uses striding when the source divides evenly, and nearest-neighbour indexing
    otherwise. Both are fast enough that this never shows up in a latency profile.
    """
    src_h, src_w = gray.shape[:2]
    if src_h == height and src_w == width:
        return gray
    if src_h >= height and src_w >= width and src_h % height == 0 and src_w % width == 0:
        fh, fw = src_h // height, src_w // width
        return gray.reshape(height, fh, width, fw).mean(axis=(1, 3))
    rows = np.linspace(0, src_h - 1, height).astype(np.int32)
    cols = np.linspace(0, src_w - 1, width).astype(np.int32)
    return gray[rows][:, cols]


def dhash(gray_small: np.ndarray) -> np.ndarray:
    """Difference hash: one bit per horizontally-adjacent pixel pair."""
    return (gray_small[:, 1:] > gray_small[:, :-1]).astype(np.uint8).ravel()


def hamming(a: np.ndarray, b: np.ndarray) -> int:
    if a.shape != b.shape:
        return a.size
    return int(np.count_nonzero(a != b))


def box_blur(image: np.ndarray, size: int = 9) -> np.ndarray:
    """Separable box blur via integral sums - no scipy or cv2 needed."""
    if size < 2:
        return image
    pad = size // 2
    padded = np.pad(image, pad, mode="edge")

    rows = np.cumsum(padded, axis=0)
    rows = np.vstack([np.zeros((1, rows.shape[1]), rows.dtype), rows])
    rows = rows[size:, :] - rows[:-size, :]

    cols = np.cumsum(rows, axis=1)
    cols = np.hstack([np.zeros((cols.shape[0], 1), cols.dtype), cols])
    cols = cols[:, size:] - cols[:, :-size]

    return cols / float(size * size)


def text_signature(
    frame: np.ndarray,
    blur: int = 9,
    edge_threshold: float = 26.0,
    bright_threshold: float = 170.0,
) -> np.ndarray:
    """A 0/1 map of text-like pixels.

    Two conditions together, because either alone picks up scenery: the pixel
    must differ sharply from its local neighbourhood (a glyph stroke, not a
    gradient) *and* be bright. Subtitles are drawn to stay readable, so they are
    light-on-dark or dark-with-a-light-outline; both leave bright strokes. Broad
    scene detail satisfies at most one condition.
    """
    gray = to_grayscale(frame)
    highpass = np.abs(gray - box_blur(gray, blur))
    return ((highpass > edge_threshold) & (gray > bright_threshold)).astype(np.float32)


def text_pixel_ratio(frame: np.ndarray, **kwargs) -> float:
    """Fraction of the frame that looks like text. Near zero means no subtitle."""
    return float(text_signature(frame, **kwargs).mean())


@dataclass
class ChangeResult:
    changed: bool
    hash_distance: int
    pixel_delta: float


class ChangeDetector:
    """Stateful gate: feed it frames, it tells you which ones are worth OCR-ing."""

    def __init__(
        self,
        hash_threshold: int = 4,
        pixel_threshold: float = 2.5,
        hash_width: int = 64,
        hash_height: int = 16,
        text_mask: bool = True,
        mask_pixel_threshold: float = 0.03,
        mask_blur: int = 9,
        mask_edge: float = 26.0,
        mask_bright: float = 170.0,
    ) -> None:
        self.hash_threshold = hash_threshold
        self.hash_width = hash_width
        self.hash_height = hash_height
        self.text_mask = text_mask
        self.mask_blur = mask_blur
        self.mask_edge = mask_edge
        self.mask_bright = mask_bright
        # The two signatures live on different scales - 0-255 luma versus a 0/1
        # mask - so they need their own delta thresholds.
        self.pixel_threshold = mask_pixel_threshold if text_mask else pixel_threshold
        self._prev_hash: Optional[np.ndarray] = None
        self._prev_small: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Forget history - call after a region change or an unpause."""
        self._prev_hash = None
        self._prev_small = None

    def _signature(self, frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if self.text_mask:
            source = text_signature(
                frame,
                blur=self.mask_blur,
                edge_threshold=self.mask_edge,
                bright_threshold=self.mask_bright,
            )
        else:
            source = to_grayscale(frame)
        small = downscale(source, self.hash_width, self.hash_height)
        return small, dhash(small)

    def check(self, frame: np.ndarray) -> ChangeResult:
        small, current_hash = self._signature(frame)

        if self._prev_hash is None or self._prev_small is None:
            self._prev_hash, self._prev_small = current_hash, small
            return ChangeResult(changed=True, hash_distance=current_hash.size, pixel_delta=255.0)

        distance = hamming(current_hash, self._prev_hash)
        delta = float(np.abs(small - self._prev_small).mean())

        changed = distance > self.hash_threshold or delta > self.pixel_threshold
        if changed:
            # Only advance the reference on a real change. Holding the last
            # *accepted* frame stops a slow fade from creeping past the gate one
            # imperceptible step at a time.
            self._prev_hash, self._prev_small = current_hash, small

        return ChangeResult(changed=changed, hash_distance=distance, pixel_delta=delta)


def is_probably_blank(
    frame: np.ndarray,
    std_threshold: float = 3.0,
    text_mask: bool = True,
    min_text_ratio: float = 0.0006,
    **mask_kwargs,
) -> bool:
    """Cheap early-out when the watched area holds no text at all.

    Standard deviation alone only works over a flat background: a subtitle band
    sitting on busy scenery has plenty of contrast and no text whatsoever. The
    text signature answers the actual question - are there any glyph-like
    pixels - so silence during gameplay still skips OCR.
    """
    if text_mask:
        return text_pixel_ratio(frame, **mask_kwargs) < min_text_ratio
    return float(to_grayscale(frame).std()) < std_threshold
