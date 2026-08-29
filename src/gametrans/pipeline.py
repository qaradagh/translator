"""The orchestrator: capture -> change gate -> OCR -> stability -> translate.

Threading model
---------------
* One capture thread owns the capture backend and the OCR engine. Both are
  cheap and neither is thread-safe, so keeping them on a single thread avoids
  locks entirely on the hot path.
* A small worker pool runs translations, so a slow line never blocks the next
  one from starting.
* The overlay is only ever touched through Qt signals, which marshal onto the
  GUI thread.

Every stage can drop work: an unchanged frame never reaches OCR, a noisy read
never reaches the API, and a translation whose subtitle has already left the
screen is discarded instead of being drawn late.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from .capture import CaptureBackend, RegionChanged, create_capture
from .changedet import ChangeDetector, is_probably_blank
from .config import AppConfig, RegionConfig
from .metrics import Metrics
from .ocr import OcrEngineFacade
from .stability import StabilityTracker
from .textnorm import contains_persian, is_noise
from .translator import Translator

log = logging.getLogger(__name__)


@dataclass
class PipelineCallbacks:
    """Everything the pipeline reports outwards. All are optional."""

    on_partial: Optional[Callable[[str, str], None]] = None   # (translation, source)
    on_final: Optional[Callable[[str, str, float], None]] = None  # (+ total ms)
    on_clear: Optional[Callable[[], None]] = None
    on_status: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class Pipeline:
    def __init__(
        self,
        cfg: AppConfig,
        translator: Translator,
        callbacks: Optional[PipelineCallbacks] = None,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self.cfg = cfg
        self.translator = translator
        self.callbacks = callbacks or PipelineCallbacks()
        self.metrics = metrics or translator.metrics

        self._region = cfg.region
        self._detector = ChangeDetector(
            hash_threshold=cfg.capture.hash_threshold,
            pixel_threshold=cfg.capture.pixel_threshold,
            hash_width=cfg.capture.hash_width,
            hash_height=cfg.capture.hash_height,
        )
        self._stability = StabilityTracker(cfg.stability)

        self._capture: Optional[CaptureBackend] = None
        self._ocr: Optional[OcrEngineFacade] = None

        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._region_lock = threading.Lock()

        # Monotonically increasing id for the line currently on screen. A
        # translation whose generation is stale is dropped rather than drawn.
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._blank_since: Optional[float] = None
        self._had_text = False
        # Set when the capture backend must be rebuilt (dxcam pins its region at
        # construction time). Initialised here so set_region() is safe to call
        # before start().
        self._rebuild_capture = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        if not self._region.is_set:
            raise ValueError(
                "No capture region set. Run `gametrans pick-region` first, "
                "or set [region] in config.toml."
            )

        self._running.set()
        self._paused.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=max(self.cfg.translate.concurrency, 1),
            thread_name_prefix="translate",
        )
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        log.info("pipeline started on region %s", self._region.as_tuple())

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        self._teardown_devices()

    def pause(self) -> None:
        self._paused.set()
        self._notify_status("paused")

    def resume(self) -> None:
        self._detector.reset()
        self._stability.reset()
        self._paused.clear()
        self._notify_status("running")

    def toggle_pause(self) -> bool:
        if self._paused.is_set():
            self.resume()
        else:
            self.pause()
        return self._paused.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def set_region(self, region: RegionConfig) -> None:
        """Swap the watched rectangle without restarting the app."""
        with self._region_lock:
            self._region = region
            self.cfg.region = region
        self._detector.reset()
        self._stability.reset()
        # Force the capture backend to be rebuilt on the next tick; dxcam pins
        # its region at construction time.
        self._rebuild_capture = True
        log.info("region changed to %s", region.as_tuple())

    # -- main loop -----------------------------------------------------------

    def _run(self) -> None:
        interval = 1.0 / max(self.cfg.capture.target_fps, 1)

        try:
            self._capture = create_capture(self.cfg.capture, self._region)
            self._ocr = OcrEngineFacade(self.cfg.ocr)
        except Exception as exc:
            log.error("pipeline could not start: %s", exc)
            self._notify_error(str(exc))
            self._running.clear()
            return

        self._notify_status("running")

        while self._running.is_set():
            tick_started = time.perf_counter()

            if self._paused.is_set():
                time.sleep(0.1)
                continue

            try:
                self._tick()
            except RegionChanged:
                self._rebuild_capture = True
            except Exception as exc:
                log.exception("capture tick failed")
                self._notify_error(str(exc))
                time.sleep(0.25)

            if self._rebuild_capture:
                self._swap_capture_backend()

            elapsed = time.perf_counter() - tick_started
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

        self._teardown_devices()

    def _tick(self) -> None:
        with self._region_lock:
            region = self._region

        assert self._capture is not None and self._ocr is not None

        grab_started = time.perf_counter()
        frame = self._capture.grab(region)
        self.metrics.record("capture", (time.perf_counter() - grab_started) * 1000.0)

        if frame is None:
            # dxcam reports "desktop unchanged" this way; treat it as no change.
            return

        change = self._detector.check(frame)
        if not change.changed:
            self.metrics.increment("frames_skipped")
            return
        self.metrics.increment("frames_processed")

        if is_probably_blank(frame):
            self._handle_blank()
            return

        result = self._ocr.read(frame)
        self.metrics.record("ocr", result.elapsed_ms)
        text = result.text

        if not text or is_noise(text, min_chars=self.cfg.stability.min_chars):
            self._handle_blank()
            return

        # Feedback-loop guard: if the overlay ever ends up inside the capture
        # region, we would otherwise read our own Persian output and translate it
        # back and forth forever.
        if self.translator.targets_persian() and contains_persian(text):
            log.debug("ignoring Arabic-script read (likely our own overlay)")
            self.metrics.increment("self_read_rejected")
            return

        self._blank_since = None
        decision = self._stability.feed(text)
        if not decision.ready:
            return

        self.metrics.record("stability_wait", decision.waited_ms)
        self._submit(decision.text)

    def _handle_blank(self) -> None:
        """The region has no readable text - clear the overlay after a beat."""
        now = time.monotonic()
        if self._blank_since is None:
            self._blank_since = now
            return
        if self._had_text and (now - self._blank_since) * 1000.0 >= self.cfg.overlay.linger_ms:
            self._had_text = False
            self._stability.clear_current()
            if self.callbacks.on_clear:
                self.callbacks.on_clear()

    # -- translation ---------------------------------------------------------

    def _submit(self, text: str) -> None:
        if self._pool is None:
            return
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
        self._had_text = True
        try:
            self._pool.submit(self._translate_job, text, generation)
        except RuntimeError:
            # Pool already shutting down.
            pass

    def _translate_job(self, text: str, generation: int) -> None:
        submitted_at = time.monotonic()

        def is_stale() -> bool:
            with self._generation_lock:
                superseded = generation != self._generation
            expired = (
                (time.monotonic() - submitted_at) * 1000.0
                > self.cfg.translate.stale_after_ms
            )
            return superseded or expired or not self._running.is_set()

        def on_chunk(accumulated: str, _chunk: str) -> None:
            if is_stale():
                return
            if self.callbacks.on_partial:
                self.callbacks.on_partial(accumulated, text)

        outcome = self.translator.translate(text, on_chunk=on_chunk, is_cancelled=is_stale)

        if is_stale():
            self.metrics.increment("dropped_stale")
            return

        if outcome.ok:
            self.metrics.increment("cache_served" if outcome.cached else "api_served")
            if self.callbacks.on_final:
                self.callbacks.on_final(outcome.text, text, outcome.total_ms)
        elif outcome.error and outcome.error != "cancelled":
            log.warning("translation failed: %s", outcome.error)
            self._notify_error(outcome.error)

    # -- helpers -------------------------------------------------------------

    def _swap_capture_backend(self) -> None:
        self._rebuild_capture = False
        with self._region_lock:
            region = self._region
        try:
            if self._capture is not None:
                self._capture.close()
            self._capture = create_capture(self.cfg.capture, region)
        except Exception as exc:
            log.error("could not rebuild capture backend: %s", exc)
            self._notify_error(str(exc))

    def _teardown_devices(self) -> None:
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        if self._ocr is not None:
            self._ocr.close()
            self._ocr = None

    def _notify_status(self, status: str) -> None:
        if self.callbacks.on_status:
            self.callbacks.on_status(status)

    def _notify_error(self, message: str) -> None:
        if self.callbacks.on_error:
            self.callbacks.on_error(message)
