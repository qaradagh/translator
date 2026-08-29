"""GUI application wiring: overlay + pipeline + hotkeys on one event loop."""

from __future__ import annotations

import logging
import os
import signal
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .config import AppConfig, RegionConfig, save_region
from .hotkeys import HotkeyManager
from .metrics import Metrics
from .overlay import (
    SubtitleOverlay,
    apply_platform_window_flags,
    create_application,
    load_bundled_fonts,
)
from .pipeline import Pipeline, PipelineCallbacks
from .region import show_picker
from .translator import Translator

log = logging.getLogger(__name__)


def bundled_font_dir() -> str:
    """Locate the bundled Persian fonts.

    Checked in order so the app works from a source checkout, from an installed
    package, and from a directory the user points at explicitly.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("GAMETRANS_FONT_DIR", ""),
        os.path.join(here, "assets", "fonts"),
        os.path.normpath(os.path.join(here, "..", "..", "assets", "fonts")),
        os.path.join(os.getcwd(), "assets", "fonts"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return candidates[2]


class _HotkeyBridge(QObject):
    """Hotkey callbacks fire on the `keyboard` hook thread; these signals hop
    them onto the Qt GUI thread, where touching widgets is legal."""

    toggle_overlay = Signal()
    pick_region = Signal()
    toggle_pause = Signal()
    quit_app = Signal()


class Application:
    def __init__(self, cfg: AppConfig, config_path: str = "config.toml") -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.metrics = Metrics()

        self.qt_app: QApplication = create_application()
        load_bundled_fonts(bundled_font_dir())

        self.translator = Translator(cfg.translate, metrics=self.metrics)
        self.overlay = SubtitleOverlay(cfg.overlay, cfg.region)
        self.overlay.show()
        apply_platform_window_flags(self.overlay)

        self.pipeline = Pipeline(
            cfg,
            self.translator,
            callbacks=PipelineCallbacks(
                on_partial=self._on_partial,
                on_final=self._on_final,
                on_clear=self._on_clear,
                on_status=self._on_status,
                on_error=self._on_error,
            ),
            metrics=self.metrics,
        )

        self._bridge = _HotkeyBridge()
        self._bridge.toggle_overlay.connect(self._toggle_overlay)
        self._bridge.pick_region.connect(self._pick_region)
        self._bridge.toggle_pause.connect(self._toggle_pause)
        self._bridge.quit_app.connect(self.shutdown)

        self.hotkeys = HotkeyManager(cfg.hotkeys)
        self.hotkeys.register(
            {
                cfg.hotkeys.toggle_overlay: self._bridge.toggle_overlay.emit,
                cfg.hotkeys.pick_region: self._bridge.pick_region.emit,
                cfg.hotkeys.toggle_pause: self._bridge.toggle_pause.emit,
                cfg.hotkeys.quit: self._bridge.quit_app.emit,
            }
        )

        self._picker = None
        self._overlay_visible = True

        if cfg.overlay.show_latency and cfg.metrics:
            self._stats_timer = QTimer()
            self._stats_timer.timeout.connect(self._refresh_latency_note)
            self._stats_timer.start(1000)

        # Let Ctrl+C in the launching terminal still kill the app: Qt's event
        # loop otherwise swallows SIGINT until a Qt event arrives.
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        self._sigint_timer = QTimer()
        self._sigint_timer.timeout.connect(lambda: None)
        self._sigint_timer.start(300)

    # -- run -----------------------------------------------------------------

    def run(self) -> int:
        self.translator.warmup()

        if not self.cfg.region.is_set:
            log.info("no region configured - opening the picker")
            QTimer.singleShot(150, self._pick_region)
        else:
            QTimer.singleShot(0, self._start_pipeline)

        return self.qt_app.exec()

    def _start_pipeline(self) -> None:
        try:
            self.pipeline.start()
        except Exception as exc:
            log.error("could not start pipeline: %s", exc)
            self.overlay.set_status(str(exc))

    def shutdown(self) -> None:
        log.info("shutting down")
        self.hotkeys.unregister_all()
        self.pipeline.stop()
        self.translator.close()
        if self.cfg.metrics:
            report = self.metrics.report()
            if report.strip():
                print("\n" + report)
        self.qt_app.quit()

    # -- pipeline callbacks (worker threads -> Qt signals) -------------------

    def _on_partial(self, translation: str, source: str) -> None:
        self.overlay.show_translation(translation, source, partial=True)

    def _on_final(self, translation: str, source: str, total_ms: float) -> None:
        self.overlay.show_translation(translation, source, partial=False)
        log.debug("translated in %.0f ms: %s", total_ms, translation[:60])

    def _on_clear(self) -> None:
        self.overlay.clear()

    def _on_status(self, status: str) -> None:
        self.overlay.set_status(status)

    def _on_error(self, message: str) -> None:
        log.debug("pipeline error: %s", message)
        self.overlay.set_status(message[:80])

    # -- hotkey actions ------------------------------------------------------

    def _toggle_overlay(self) -> None:
        self._overlay_visible = not self._overlay_visible
        self.overlay.setVisible(self._overlay_visible)
        if self._overlay_visible:
            apply_platform_window_flags(self.overlay)

    def _toggle_pause(self) -> None:
        paused = self.pipeline.toggle_pause()
        self.overlay.set_status("paused" if paused else "running")

    def _pick_region(self) -> None:
        was_running = self.pipeline.is_running
        if was_running:
            self.pipeline.pause()
        self.overlay.hide()

        def on_selected(region: Optional[RegionConfig]) -> None:
            self.overlay.show()
            apply_platform_window_flags(self.overlay)
            self._picker = None

            if region is None:
                if was_running:
                    self.pipeline.resume()
                return

            self.cfg.region = region
            self.overlay.apply_geometry(region)
            try:
                save_region(self.config_path, region)
                log.info("region saved to %s", self.config_path)
            except OSError as exc:
                log.warning("could not save region: %s", exc)

            if self.pipeline.is_running:
                self.pipeline.set_region(region)
                self.pipeline.resume()
            else:
                self._start_pipeline()

        self._picker = show_picker(on_selected, self.cfg.region)

    # -- stats ---------------------------------------------------------------

    def _refresh_latency_note(self) -> None:
        ocr = self.metrics.stat("ocr")
        first = self.metrics.stat("translate_first_token")
        hit_rate = self.translator.cache.hit_rate * 100.0
        self.overlay.set_latency_note(
            f"ocr {ocr.p50_ms:.0f}ms · api {first.p50_ms:.0f}ms · cache {hit_rate:.0f}%"
        )
