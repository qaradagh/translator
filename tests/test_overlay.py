"""Overlay text-layout tests.

These are the checks that catch the classic Persian rendering failures:
disconnected letter forms, and a right-to-left line laid out left-to-right.
They run headless via Qt's offscreen platform plugin.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is not installed")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - guarded by importorskip
    QApplication = None


@pytest.fixture(scope="module")
def qt_app():
    try:
        app = QApplication.instance() or QApplication(["-platform", "offscreen"])
    except Exception as exc:  # pragma: no cover - missing system Qt libraries
        pytest.skip(f"Qt could not start: {exc}")
    return app


@pytest.fixture(scope="module")
def font(qt_app):
    from gametrans.config import OverlayConfig
    from gametrans.overlay import _resolve_font, load_bundled_fonts

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_bundled_fonts(os.path.join(root, "assets", "fonts"))
    return _resolve_font(OverlayConfig(font_size=28, font_weight=600))


def test_bundled_persian_font_is_registered(qt_app, font):
    from PySide6.QtGui import QFontDatabase

    assert "Vazirmatn" in QFontDatabase.families(), (
        "the bundled Persian font must be available, otherwise Persian falls "
        "back to a font that may not shape Arabic script"
    )


def test_persian_text_lays_out_with_non_zero_width(qt_app, font):
    from gametrans.overlay import _build_layout

    _layout, (width, height) = _build_layout("سلام مسافر", font, 700, 1.35)
    assert width > 0 and height > 0


def test_long_persian_text_wraps_onto_multiple_lines(qt_app, font):
    from gametrans.overlay import _build_layout

    long_text = "این یک جمله‌ی بسیار طولانی است که باید در چند خط شکسته شود تا کامل دیده شود"
    layout, (_width, height) = _build_layout(long_text, font, 260, 1.35)
    assert layout.lineCount() > 1
    assert height > 0


def test_base_direction_is_right_to_left(qt_app, font):
    """The first logical character must sit at the right edge of the line."""
    from gametrans.overlay import RLM, _build_layout

    layout, _size = _build_layout("Geralt گفت: بدهی.", font, 700, 1.35)
    line = layout.lineAt(0)
    x_of_first_char = line.cursorToX(1)[0]
    assert x_of_first_char == pytest.approx(700, abs=2), (
        "an RTL paragraph must start at the right edge"
    )


def test_latin_run_is_placed_right_of_the_persian_run(qt_app, font):
    """Bidi check: in `Geralt گفت: بدهی.` the Latin name comes first logically,
    so visually it belongs on the right."""
    from gametrans.overlay import RLM, _build_layout

    text = "Geralt گفت: بدهی."
    layout, _size = _build_layout(text, font, 700, 1.35)
    line = layout.lineAt(0)

    latin_runs = line.glyphRuns(1, 6)                       # "Geralt"
    persian_runs = line.glyphRuns(8, len(RLM + text) - 8)   # the Persian tail
    assert latin_runs and persian_runs

    latin_left = min(run.boundingRect().left() for run in latin_runs)
    persian_right = max(run.boundingRect().right() for run in persian_runs)
    assert latin_left >= persian_right - 1


def test_persian_letters_are_shaped_not_isolated(qt_app, font):
    """Connected script check.

    A correctly shaped Arabic-script word is narrower than the same letters set
    in isolated forms, because joined glyphs overlap. Comparing the two widths
    catches a font or text engine that fails to apply the joining rules.
    """
    from PySide6.QtGui import QFontMetricsF

    from gametrans.overlay import _build_layout

    joined = "سلام"
    # Zero-width non-joiner between each letter forces isolated forms.
    isolated = "‌".join(joined)

    _joined_layout, (joined_width, _h1) = _build_layout(joined, font, 2000, 1.0)
    _iso_layout, (isolated_width, _h2) = _build_layout(isolated, font, 2000, 1.0)

    assert joined_width < isolated_width, (
        "shaped Persian must be narrower than the isolated letter forms"
    )
    assert QFontMetricsF(font).horizontalAdvance(joined) > 0


def test_rendering_actually_puts_ink_on_the_canvas(qt_app, font):
    """Guards against a layout that measures fine but draws nothing."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter

    from gametrans.overlay import _build_layout

    image = QImage(600, 120, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0))

    painter = QPainter(image)
    painter.setPen(QColor(255, 255, 255))
    layout, _size = _build_layout("سلام مسافر", font, 560, 1.35)
    layout.draw(painter, QPointF(20, 20))
    painter.end()

    white_pixels = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).red() > 128
    )
    assert white_pixels > 200, "expected visible glyphs to be drawn"


# -- widget-level painting ---------------------------------------------------


def _render_overlay(text, width=860, height=90, **cfg_kwargs):
    """Render the real overlay widget and return (image, natural text width)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    from gametrans.config import OverlayConfig, RegionConfig
    from gametrans.overlay import SubtitleOverlay, _build_layout, _resolve_font

    options = dict(
        font_size=27,
        font_weight=600,
        background_opacity=0.0,  # text only, so ink == glyphs
        outline_width=0.0,
        padding=14,
        max_lines=2,
    )
    options.update(cfg_kwargs)
    cfg = OverlayConfig(**options)

    overlay = SubtitleOverlay(cfg, RegionConfig(left=0, top=0, width=width, height=44))
    overlay.resize(width, height)
    overlay._translation = text

    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    overlay.render(image)

    _layout, (natural_width, _h) = _build_layout(
        text, _resolve_font(cfg), width - cfg.padding * 2, cfg.line_spacing
    )
    return image, natural_width, cfg


def _ink_bounds(image):
    """Horizontal extent of visible pixels, as (min_x, max_x, count)."""
    xs = [
        x
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 40
    ]
    return (min(xs), max(xs), len(xs)) if xs else (None, None, 0)


def test_overlay_draws_the_whole_line_without_clipping(qt_app, font):
    """Regression: the layout right-aligns inside its line width, so applying a
    second right-alignment when positioning it pushed the text off the widget
    and cut off the start of every Persian line."""
    text = "مسافر، باید پیش از شب به قلعه برسی."
    image, natural_width, cfg = _render_overlay(text)

    min_x, max_x, count = _ink_bounds(image)
    assert count > 0, "nothing was drawn"
    assert min_x >= 0
    assert max_x <= image.width() - 1

    drawn_width = max_x - min_x
    assert drawn_width >= natural_width * 0.9, (
        f"text appears clipped: drew {drawn_width:.0f}px of {natural_width:.0f}px"
    )


def test_overlay_text_is_right_aligned_within_its_padding(qt_app, font):
    image, _natural_width, cfg = _render_overlay("سلام مسافر")
    _min_x, max_x, count = _ink_bounds(image)
    assert count > 0
    # The right edge of the glyphs should sit roughly one padding in from the
    # widget's right edge.
    expected_right = image.width() - cfg.padding
    assert abs(max_x - expected_right) <= cfg.padding, (
        f"right edge at {max_x}, expected near {expected_right}"
    )


def test_overlay_wraps_long_text_inside_the_widget(qt_app, font):
    long_text = (
        "این یک جمله‌ی بسیار طولانی است که باید در چند خط شکسته شود "
        "تا کامل روی صفحه دیده شود و هیچ بخشی از آن بریده نشود."
    )
    image, _natural_width, _cfg = _render_overlay(long_text, width=520, height=200)
    min_x, max_x, count = _ink_bounds(image)
    assert count > 0
    assert min_x >= 0 and max_x <= image.width() - 1


def test_overlay_draws_nothing_when_there_is_no_translation(qt_app, font):
    image, _natural_width, _cfg = _render_overlay("")
    assert _ink_bounds(image)[2] == 0
