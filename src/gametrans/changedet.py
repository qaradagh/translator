"""Frame change detection.

Games render at 60-144 fps but subtitles change maybe once every few seconds.
Running OCR on every captured frame would waste most of the CPU budget and, more
importantly, would push duplicate lines at the translation API and burn the free
tier quota. This module answers one question as cheaply as possible: *is this
frame meaningfully different from the last one?*

The check costs well under a millisecond: downscale to 64x16 greyscale, then
compare both a difference hash (structure) and the mean absolute pixel delta
(brightness/fades). Structure catches new text; the pixel delta catches a
subtitle fading in or out.
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
    ) -> None:
        self.hash_threshold = hash_threshold
        self.pixel_threshold = pixel_threshold
        self.hash_width = hash_width
        self.hash_height = hash_height
        self._prev_hash: Optional[np.ndarray] = None
        self._prev_small: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Forget history - call after a region change or an unpause."""
        self._prev_hash = None
        self._prev_small = None

    def _signature(self, frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        gray = to_grayscale(frame)
        small = downscale(gray, self.hash_width, self.hash_height)
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


def is_probably_blank(frame: np.ndarray, std_threshold: float = 3.0) -> bool:
    """Cheap early-out for an empty subtitle band (no text drawn at all).

    A region containing text has high local contrast; an empty one is close to
    flat. Skipping these saves an OCR call per frame during gameplay silence.
    """
    gray = to_grayscale(frame)
    return float(gray.std()) < std_threshold
