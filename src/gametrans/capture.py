"""Screen capture backends.

Only the watched region is ever grabbed - capturing a 1920x120 subtitle band
costs ~1-3 ms, while a full 4K grab costs ~20 ms. Region-limited capture is the
first of the three latency wins in this pipeline (the others are the change gate
in `changedet.py` and the translation cache in `cache.py`).
"""

from __future__ import annotations

import logging
import platform
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np

from .config import CaptureConfig, RegionConfig

log = logging.getLogger(__name__)


class CaptureBackend(ABC):
    """Grabs a rectangle of the screen as a BGR uint8 array."""

    name = "base"

    @abstractmethod
    def grab(self, region: RegionConfig) -> Optional[np.ndarray]:
        """Return an (H, W, 3) BGR array, or None if the frame was not ready."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self) -> "CaptureBackend":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class MSSBackend(CaptureBackend):
    """Cross-platform capture via python-mss. Fast enough for a subtitle band."""

    name = "mss"

    def __init__(self) -> None:
        import mss  # imported lazily so the module can be inspected without it

        self._mss_module = mss
        # mss objects are not thread-safe; each backend instance owns exactly one
        # and the pipeline only ever grabs from the capture thread.
        self._sct = mss.mss()

    def grab(self, region: RegionConfig) -> Optional[np.ndarray]:
        box = {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
        raw = self._sct.grab(box)
        # mss returns BGRA; drop alpha without copying the whole buffer twice.
        frame = np.frombuffer(raw.rgb, dtype=np.uint8)
        frame = frame.reshape((raw.height, raw.width, 3))
        return frame

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass


class DXCamBackend(CaptureBackend):
    """Windows Desktop Duplication capture - lower latency than mss on Win10/11.

    dxcam hands back frames from the GPU copy queue, so it also survives
    borderless-fullscreen games better than GDI-based grabbing.
    """

    name = "dxcam"

    def __init__(self, region: RegionConfig) -> None:
        import dxcam

        self._region_key = region.as_tuple()
        # dxcam wants an absolute (left, top, right, bottom) box.
        box = (
            region.left,
            region.top,
            region.left + region.width,
            region.top + region.height,
        )
        self._camera = dxcam.create(output_idx=max(region.monitor - 1, 0), region=box)
        if self._camera is None:
            raise RuntimeError("dxcam.create() returned None")

    def grab(self, region: RegionConfig) -> Optional[np.ndarray]:
        if region.as_tuple() != self._region_key:
            # Region changed under us (user re-picked); caller rebuilds the backend.
            raise RegionChanged(region)
        frame = self._camera.grab()
        if frame is None:
            # dxcam returns None when the desktop has not changed since the last
            # grab. That is itself a "no change" signal - the caller treats it as
            # a skip, which is exactly what the change gate would have decided.
            return None
        # dxcam yields RGB; the rest of the pipeline expects BGR ordering.
        return frame[:, :, ::-1]

    def close(self) -> None:
        try:
            self._camera.release()
        except Exception:  # pragma: no cover - shutdown best effort
            pass


class RegionChanged(Exception):
    """Raised when a backend pinned to a fixed region sees a different one."""

    def __init__(self, region: RegionConfig) -> None:
        super().__init__(f"capture region changed to {region.as_tuple()}")
        self.region = region


def create_capture(cfg: CaptureConfig, region: RegionConfig) -> CaptureBackend:
    """Pick the fastest capture backend available on this machine."""
    requested = (cfg.backend or "auto").lower()

    if requested == "mss":
        return MSSBackend()
    if requested == "dxcam":
        return DXCamBackend(region)

    # auto
    if platform.system() == "Windows":
        try:
            backend = DXCamBackend(region)
            log.info("capture backend: dxcam")
            return backend
        except Exception as exc:
            log.info("dxcam unavailable (%s); falling back to mss", exc)

    log.info("capture backend: mss")
    return MSSBackend()


def list_monitors() -> list:
    """Return mss monitor descriptors, index 0 being the virtual 'all screens'."""
    import mss

    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors]
