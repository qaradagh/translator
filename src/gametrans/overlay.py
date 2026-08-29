"""Transparent, click-through subtitle overlay with correct Persian rendering.

Three things make Persian text hard and are handled explicitly here:

* **Shaping** - Arabic-script letters change form by position. Qt shapes through
  HarfBuzz automatically, but only if the text goes through a real text layout;
  drawing glyph-by-glyph would produce disconnected letters.
* **Bidirectional text** - a Persian line containing an English name or a number
  mixes directions. `QTextOption.setTextDirection(RightToLeft)` sets the correct
  base direction so the bidi algorithm places the runs properly.
* **Legibility over video** - the text is drawn as an outline pass plus a fill
  pass so it stays readable against a bright or busy frame.

On Windows the window is also excluded from screen capture, so the pipeline can
never OCR its own output back into the translator.
"""

from __future__ import annotations

import logging
import platform
import sys
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QPainter,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QWidget

from .config import OverlayConfig, RegionConfig

log = logging.getLogger(__name__)


class SubtitleLine:
    """One translated line and the clock it lives by.

    Each line ages independently, so a burst of quick dialogue leaves a
    readable stack rather than one line flickering through several values.
    """

    def __init__(self, translation: str, source: str, partial: bool) -> None:
        self.translation = translation
        self.source = source
        self.partial = partial
        self.born = time.monotonic()
        self.settled_at: Optional[float] = None if partial else self.born

    def settle(self) -> None:
        """Mark the line finished; its display clock starts from here."""
        self.partial = False
        self.settled_at = time.monotonic()

    def age_ms(self) -> float:
        """Milliseconds since the line finished arriving. 0 while streaming."""
        if self.settled_at is None:
            return 0.0
        return (time.monotonic() - self.settled_at) * 1000.0

    def opacity(self, linger_ms: int, fade_ms: int) -> float:
        """1.0 while fresh, falling to 0.0 across the fade window."""
        if self.partial or linger_ms <= 0:
            return 1.0
        overdue = self.age_ms() - linger_ms
        if overdue <= 0:
            return 1.0
        if fade_ms <= 0:
            return 0.0
        return max(0.0, 1.0 - overdue / fade_ms)

    def expired(self, linger_ms: int, fade_ms: int) -> bool:
        return self.opacity(linger_ms, fade_ms) <= 0.0

# Right-to-left mark. Prefixed to each paragraph so a line that happens to start
# with a Latin word or a digit still lays out right-to-left overall.
RLM = "‏"


class SubtitleOverlay(QWidget):
    """Frameless always-on-top window that paints the current translation."""

    # Pipeline threads emit these; Qt delivers them on the GUI thread.
    text_changed = Signal(str, str, bool)  # translation, source, is_partial
    cleared = Signal()
    status_changed = Signal(str)

    def __init__(
        self,
        cfg: OverlayConfig,
        region: RegionConfig,
        parent: Optional[QWidget] = None,
        click_through: bool = True,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.region = region
        # In-game the window must never swallow a click. A preview window wants
        # the opposite, so it can be dismissed.
        self.click_through = click_through

        self._lines: Deque[SubtitleLine] = deque(maxlen=max(cfg.history_lines, 1))
        self._status = ""
        self._latency_note = ""

        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        if click_through:
            flags |= Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, click_through)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, click_through)
        # The whole window is right-to-left; child widgets inherit it.
        self.setLayoutDirection(Qt.RightToLeft)

        self._font = _resolve_font(cfg)
        self._metrics = QFontMetricsF(self._font)

        # Lines expire on their own clocks, so a single shot timer cannot express
        # it - tick instead, and stop ticking when nothing is on screen.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(50)
        self._tick_timer.timeout.connect(self._on_tick)

        self.text_changed.connect(self._on_text_changed)
        self.cleared.connect(self._on_cleared)
        self.status_changed.connect(self._on_status)

        self.apply_geometry(region)

    # -- geometry ------------------------------------------------------------

    def apply_geometry(self, region: RegionConfig) -> None:
        """Place the overlay, converting physical capture pixels to Qt's
        logical coordinates so it lands correctly under display scaling."""
        self.region = region
        screen = screen_for_physical_point(region.left, region.top)
        ratio = screen.devicePixelRatio() if screen else 1.0

        anchor = (self.cfg.anchor or "region").lower()
        width = (self.cfg.width or region.width or 900) / ratio
        height = self._preferred_height()
        available = screen.geometry() if screen is not None else None

        if anchor == "custom":
            left = self.cfg.x / ratio
            top = self.cfg.y / ratio
        elif anchor in ("bottom", "top") and available is not None:
            left = available.left() + (available.width() - width) / 2
            margin = 40
            top = (
                available.bottom() - height - margin
                if anchor == "bottom"
                else available.top() + margin
            )
        else:
            left = region.left / ratio
            # Sit just below the watched band. Keeping the overlay out of the
            # capture rectangle is the first line of defence against reading our
            # own output; the Windows capture exclusion below is the second.
            top = (region.top + region.height + 8) / ratio

        if available is not None:
            left = max(available.left(), min(left, available.right() - width))
            if top + height > available.bottom():
                # No room underneath - flip above the region instead.
                top = max(available.top(), (region.top / ratio) - height - 8)

        self.setGeometry(int(left), int(top), int(width), int(height))

    def _preferred_height(self) -> int:
        line_height = self._metrics.height() * self.cfg.line_spacing
        # Room for every retained line, each of which may itself wrap.
        per_entry = self.cfg.max_lines + (1 if self.cfg.show_source else 0)
        entries = max(self.cfg.history_lines, 1) * per_entry
        return int(line_height * entries + self.cfg.padding * 2)

    # -- public API (thread-safe via signals) --------------------------------

    def show_translation(self, translation: str, source: str = "", partial: bool = False) -> None:
        self.text_changed.emit(translation, source, partial)

    def clear(self) -> None:
        self.cleared.emit()

    def set_status(self, status: str) -> None:
        self.status_changed.emit(status)

    def set_latency_note(self, note: str) -> None:
        self._latency_note = note

    # -- slots ---------------------------------------------------------------

    def _on_text_changed(self, translation: str, source: str, partial: bool) -> None:
        # A streaming line updates in place; a finished one settles and the next
        # partial starts a new entry.
        if self._lines and self._lines[-1].partial:
            current = self._lines[-1]
            current.translation = translation
            current.source = source
            if not partial:
                current.settle()
        else:
            self._lines.append(SubtitleLine(translation, source, partial))

        if not self._tick_timer.isActive():
            self._tick_timer.start()
        if not self.isVisible():
            self.show()
        self.update()

    def _on_cleared(self) -> None:
        # Let what is on screen finish its fade rather than snapping away; the
        # player may still be mid-sentence.
        for line in self._lines:
            if line.partial:
                line.settle()
        self.update()

    def _on_status(self, status: str) -> None:
        self._status = status
        self.update()

    def _on_tick(self) -> None:
        before = len(self._lines)
        while self._lines and self._lines[0].expired(self.cfg.linger_ms, self.cfg.fade_ms):
            self._lines.popleft()
        if not self._lines:
            self._tick_timer.stop()
        if before != len(self._lines) or self._lines:
            self.update()

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._lines and not self._status:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        padding = self.cfg.padding
        max_width = max(self.width() - padding * 2, 40)

        blocks = self._visible_blocks()
        if not blocks:
            painter.end()
            return

        # Lay everything out first: the panel has to be sized around the text,
        # and the text right-aligned inside the panel.
        laid_out = []
        total_height = 0.0
        content_width = 0.0
        for text, font, colour, opacity in blocks:
            layout, (width, height) = _build_layout(text, font, max_width, self.cfg.line_spacing)
            laid_out.append((layout, height, colour, opacity))
            total_height += height
            content_width = max(content_width, width)

        panel_width = min(content_width + padding * 2, float(self.width()))
        panel_height = total_height + padding * 2
        # Right-aligned panel: RTL text grows leftwards from the right edge.
        panel = QRectF(self.width() - panel_width, 0.0, panel_width, panel_height)

        # The panel follows the strongest line, so it fades out with the text
        # instead of leaving an empty box behind.
        self._paint_panel(painter, panel, max((b[3] for b in blocks), default=1.0))

        text_origin_x = self.width() - padding - max_width
        y = padding
        for layout, height, colour, opacity in laid_out:
            faded = QColor(colour)
            faded.setAlphaF(max(0.0, min(1.0, opacity)))
            self._paint_layout(painter, layout, QPointF(text_origin_x, y), faded, opacity)
            y += height

        painter.end()

    def _visible_blocks(self) -> List[Tuple[str, QFont, QColor, float]]:
        """(text, font, colour, opacity) for everything to draw, oldest first."""
        blocks: List[Tuple[str, QFont, QColor, float]] = []
        text_colour = QColor(self.cfg.text_color)
        newest_index = len(self._lines) - 1

        for index, line in enumerate(self._lines):
            if not line.translation:
                continue
            opacity = line.opacity(self.cfg.linger_ms, self.cfg.fade_ms)
            if opacity <= 0.0:
                continue
            # Dim everything but the newest line, so the current subtitle still
            # reads as the one to look at.
            if index != newest_index:
                opacity *= max(0.0, min(1.0, self.cfg.history_dim))
            blocks.append((line.translation, self._font, text_colour, opacity))

            if self.cfg.show_source and line.source:
                source_font = QFont(self._font)
                source_font.setPointSizeF(max(self._font.pointSizeF() * 0.7, 8.0))
                source_font.setWeight(QFont.Normal)
                blocks.append(
                    (line.source, source_font, text_colour.darker(130), opacity * 0.8)
                )

        note = " · ".join(part for part in (self._status, self._latency_note) if part)
        if note and self.cfg.show_latency:
            note_font = QFont(self._font)
            note_font.setPointSizeF(max(self._font.pointSizeF() * 0.55, 7.0))
            blocks.append((note, note_font, QColor("#B8C4D0"), 1.0))

        return blocks

    def _paint_panel(self, painter: QPainter, rect: QRectF, strength: float = 1.0) -> None:
        opacity = max(0.0, min(self.cfg.background_opacity, 1.0)) * max(0.0, min(strength, 1.0))
        if opacity <= 0.0:
            return
        color = QColor(self.cfg.background_color)
        color.setAlphaF(opacity)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(rect, self.cfg.corner_radius, self.cfg.corner_radius)

    def _paint_layout(
        self,
        painter: QPainter,
        layout: QTextLayout,
        origin: QPointF,
        color: QColor,
        opacity: float = 1.0,
    ) -> None:
        """Outline pass then fill pass, so text stays readable on any frame."""
        width = self.cfg.outline_width
        if width > 0:
            outline = QColor(self.cfg.outline_color)
            # The outline has to fade with the text, or a ghost edge remains.
            outline.setAlphaF(max(0.0, min(opacity, 1.0)))
            painter.setPen(outline)
            # Eight offsets approximate a stroke far more cheaply than converting
            # the shaped run to a QPainterPath on every frame.
            offsets = (
                (-width, 0.0), (width, 0.0), (0.0, -width), (0.0, width),
                (-width * 0.7, -width * 0.7), (width * 0.7, -width * 0.7),
                (-width * 0.7, width * 0.7), (width * 0.7, width * 0.7),
            )
            for dx, dy in offsets:
                layout.draw(painter, QPointF(origin.x() + dx, origin.y() + dy))

        painter.setPen(color)
        layout.draw(painter, origin)


def screen_for_physical_point(x: int, y: int):
    """Find the screen containing a point given in physical pixels.

    Qt reports screen geometry in logical pixels while the capture backends work
    in physical ones. On a mixed-DPI multi-monitor setup, using the primary
    screen's scale factor for a region on a secondary monitor puts the overlay in
    the wrong place, so the containing screen has to be resolved explicitly.
    """
    screens = QApplication.screens()
    for screen in screens:
        ratio = screen.devicePixelRatio()
        geometry = screen.geometry()
        left = geometry.left() * ratio
        top = geometry.top() * ratio
        if (
            left <= x < left + geometry.width() * ratio
            and top <= y < top + geometry.height() * ratio
        ):
            return screen
    return QApplication.primaryScreen()


def _build_layout(
    text: str, font: QFont, max_width: float, line_spacing: float
) -> Tuple[QTextLayout, Tuple[float, float]]:
    """Lay out one text block RTL, wrapped to `max_width`.

    Returns the layout plus its (width, height). Going through QTextLayout is
    what gives correct Arabic shaping and bidi ordering - the alternative,
    drawing the string directly, produces disconnected letter forms.
    """
    # A leading RLM forces the paragraph's base direction even when the first
    # character is Latin (an English character name, say).
    display_text = "\n".join(RLM + line for line in text.split("\n"))

    layout = QTextLayout(display_text, font)
    option = QTextOption(Qt.AlignRight | Qt.AlignVCenter)
    option.setTextDirection(Qt.RightToLeft)
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)

    metrics = QFontMetricsF(font)
    leading = metrics.height() * (line_spacing - 1.0)

    layout.beginLayout()
    y = 0.0
    width = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max_width)
        line.setPosition(QPointF(0.0, y))
        y += line.height() + leading
        width = max(width, line.naturalTextWidth())
    layout.endLayout()

    return layout, (width, y)


def _resolve_font(cfg: OverlayConfig) -> QFont:
    """Pick the first configured family that is actually installed.

    Vazirmatn is the preferred Persian UI face; Segoe UI and Tahoma both ship
    with Windows and shape Persian correctly, so there is always a fallback.
    """
    families = [name.strip() for name in cfg.font_family.split(",") if name.strip()]
    available = set(QFontDatabase.families())

    chosen = None
    for family in families:
        if family in available:
            chosen = family
            break
    if chosen is None:
        for fallback in ("Vazirmatn", "Segoe UI", "Tahoma", "Noto Naskh Arabic", "DejaVu Sans"):
            if fallback in available:
                chosen = fallback
                break
    if chosen is None:
        chosen = QApplication.font().family()
        log.warning("No Persian-capable font found; falling back to %s", chosen)

    font = QFont(chosen)
    font.setPointSize(cfg.font_size)
    font.setWeight(QFont.Weight(cfg.font_weight))
    font.setKerning(True)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def load_bundled_fonts(font_dir: str) -> List[str]:
    """Register any .ttf/.otf files shipped alongside the app."""
    import os

    loaded: List[str] = []
    if not os.path.isdir(font_dir):
        return loaded
    for name in sorted(os.listdir(font_dir)):
        if not name.lower().endswith((".ttf", ".otf")):
            continue
        font_id = QFontDatabase.addApplicationFont(os.path.join(font_dir, name))
        if font_id >= 0:
            loaded.extend(QFontDatabase.applicationFontFamilies(font_id))
    if loaded:
        log.info("loaded bundled fonts: %s", ", ".join(sorted(set(loaded))))
    return loaded


# -- platform integration ----------------------------------------------------


def apply_platform_window_flags(widget: QWidget) -> None:
    """Windows-specific window styling.

    Two things matter here:

    * `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE` make clicks pass through to the game
      and stop the overlay from ever stealing focus mid-fight.
    * `WDA_EXCLUDEFROMCAPTURE` hides the window from screen-capture APIs. Without
      it the pipeline would capture its own Persian output, OCR it, and translate
      it again in a loop. (Side effect: OBS and other capture software will not
      see the overlay either.)
    """
    if platform.system() != "Windows":
        return

    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080

        # GetWindowLongPtrW only exists in 64-bit builds.
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_long.restype = ctypes.c_void_p
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        set_long.restype = ctypes.c_void_p
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

        style = get_long(hwnd, GWL_EXSTYLE) or 0
        set_long(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )

        # Windows 10 2004+; harmless no-op (returns 0) on older builds.
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            log.info(
                "Could not exclude the overlay from screen capture "
                "(needs Windows 10 2004+). The overlay is placed outside the "
                "capture region, and Persian reads are rejected, so the "
                "feedback loop is still prevented."
            )
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("platform window flags not applied: %s", exc)


def create_application() -> QApplication:
    """Build the QApplication with the settings the overlay depends on."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("gametrans")
    app.setLayoutDirection(Qt.RightToLeft)
    return app


def show_preview(text: str, source: str, cfg: OverlayConfig, seconds: float = 8.0) -> None:
    """Show one translation in the real overlay, centred on screen.

    A Windows console prints right-to-left text in logical order, so Persian
    comes out reversed and unshaped there - which looks like a broken
    translation when it is only a broken terminal. Rendering the same string
    through the overlay shows what the player will actually see.
    """
    app = create_application()
    load_bundled_fonts_from_default_locations()

    screen = QApplication.primaryScreen()
    width = 900
    preview_cfg = OverlayConfig(**{**vars(cfg), "anchor": "custom", "width": width})
    preview_cfg.linger_ms = 0  # the timer below owns the lifetime

    window = SubtitleOverlay(preview_cfg, RegionConfig(), click_through=False)
    window.resize(width, window._preferred_height())

    if screen is not None:
        geometry = screen.geometry()
        window.move(
            geometry.center().x() - width // 2,
            int(geometry.height() * 0.62),
        )

    window.show_translation(text, source, partial=False)
    window.show()
    window.raise_()
    window.activateWindow()

    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()


def load_bundled_fonts_from_default_locations() -> List[str]:
    """Load the bundled fonts without importing the app module (avoids a cycle)."""
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.environ.get("GAMETRANS_FONT_DIR", ""),
        os.path.join(here, "assets", "fonts"),
        os.path.normpath(os.path.join(here, "..", "..", "assets", "fonts")),
        os.path.join(os.getcwd(), "assets", "fonts"),
    ):
        if candidate and os.path.isdir(candidate):
            return load_bundled_fonts(candidate)
    return []
