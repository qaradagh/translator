"""Interactive screen-region picker.

Drag a rectangle over the area where the game draws its subtitles. The result is
stored in *physical* pixels because that is what the capture backends work in -
Qt reports logical pixels, which differ whenever Windows display scaling is not
100%.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .config import RegionConfig

log = logging.getLogger(__name__)

_HINT_FA = "ناحیه‌ی زیرنویس را با ماوس بکشید  ·  Esc برای انصراف"
_HINT_EN = "Drag over the subtitle area  ·  Esc to cancel"


class RegionPicker(QWidget):
    """Full-desktop translucent window used to drag out a capture rectangle."""

    region_selected = Signal(object)  # RegionConfig or None

    def __init__(self, initial: Optional[RegionConfig] = None) -> None:
        super().__init__()
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self._initial = initial

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

        # Cover every screen so a region can be dragged on any monitor.
        virtual = QRect()
        for screen in QGuiApplication.screens():
            virtual = virtual.united(screen.geometry())
        self._virtual_origin = virtual.topLeft()
        self.setGeometry(virtual)

    # -- events --------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        self._current = event.position().toPoint()
        rect = QRect(self._origin, self._current).normalized()
        self._origin = None

        if rect.width() < 12 or rect.height() < 8:
            log.info("selection too small; ignoring")
            self.update()
            return

        self.region_selected.emit(self._to_region(rect))
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.region_selected.emit(None)
            self.close()

    # -- conversion ----------------------------------------------------------

    def _to_region(self, rect: QRect) -> RegionConfig:
        """Widget-local logical rect -> absolute physical-pixel region."""
        global_top_left = rect.topLeft() + self._virtual_origin
        screen = QGuiApplication.screenAt(global_top_left) or QGuiApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen else 1.0

        monitor_index = 1
        for index, candidate in enumerate(QGuiApplication.screens(), start=1):
            if candidate is screen:
                monitor_index = index
                break

        return RegionConfig(
            left=int(round(global_top_left.x() * ratio)),
            top=int(round(global_top_left.y() * ratio)),
            width=int(round(rect.width() * ratio)),
            height=int(round(rect.height() * ratio)),
            monitor=monitor_index,
        )

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        dim = QColor(0, 0, 0, 110)
        painter.fillRect(self.rect(), dim)

        self._draw_previous_region(painter)

        selection = self._selection_rect()
        if selection is not None:
            # Punch the selection back to fully transparent so the game shows
            # through exactly as the capture will see it.
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(selection, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

            painter.setPen(QPen(QColor("#4CC2FF"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(selection)

            label = f"{selection.width()} x {selection.height()}"
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(QFont("Segoe UI", 11))
            painter.drawText(
                selection.adjusted(0, -26, 0, 0),
                Qt.AlignLeft | Qt.AlignTop,
                label,
            )

        self._draw_hint(painter)
        painter.end()

    def _draw_previous_region(self, painter: QPainter) -> None:
        """Outline the region currently in the config, so re-picking is easier."""
        if self._initial is None or not self._initial.is_set:
            return

        screen = QGuiApplication.screenAt(
            QPoint(self._initial.left, self._initial.top)
        ) or QGuiApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen else 1.0

        rect = QRect(
            int(self._initial.left / ratio) - self._virtual_origin.x(),
            int(self._initial.top / ratio) - self._virtual_origin.y(),
            int(self._initial.width / ratio),
            int(self._initial.height / ratio),
        )
        pen = QPen(QColor(255, 255, 255, 110), 1, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

    def _draw_hint(self, painter: QPainter) -> None:
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 15))
        area = self.rect().adjusted(0, 40, 0, 0)
        painter.drawText(area, Qt.AlignHCenter | Qt.AlignTop, _HINT_FA)
        painter.setFont(QFont("Segoe UI", 11))
        painter.drawText(area.adjusted(0, 34, 0, 0), Qt.AlignHCenter | Qt.AlignTop, _HINT_EN)

    def _selection_rect(self) -> Optional[QRect]:
        if self._origin is None or self._current is None:
            return None
        return QRect(self._origin, self._current).normalized()


def pick_region_blocking(initial: Optional[RegionConfig] = None) -> Optional[RegionConfig]:
    """Run a standalone picker and return the chosen region.

    Only for the `gametrans pick-region` subcommand - inside the running app the
    picker is driven through signals so the event loop is never nested.
    """
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication([])

    result = {"region": None}

    def on_selected(region: Optional[RegionConfig]) -> None:
        result["region"] = region
        if owns_app:
            app.quit()

    picker = RegionPicker(initial)
    picker.region_selected.connect(on_selected)
    picker.showFullScreen()

    if owns_app:
        app.exec()

    return result["region"]


def show_picker(
    on_selected: Callable[[Optional[RegionConfig]], None],
    initial: Optional[RegionConfig] = None,
) -> RegionPicker:
    """Non-blocking picker for use inside the running application."""
    picker = RegionPicker(initial)
    picker.region_selected.connect(on_selected)
    picker.showFullScreen()
    picker.raise_()
    picker.activateWindow()
    return picker
