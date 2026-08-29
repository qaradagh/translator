"""End-to-end pipeline test with the screen and the network stubbed out.

Drives real capture -> change gate -> OCR -> stability -> translate -> callback
wiring; only the two edges that need hardware (screen grab, OCR engine) and the
network are replaced.
"""

import threading
import time

import numpy as np
import pytest

from gametrans.config import (
    AppConfig,
    CaptureConfig,
    OcrConfig,
    OverlayConfig,
    ProviderConfig,
    RegionConfig,
    StabilityConfig,
    TranslateConfig,
)
from gametrans.ocr import OcrLine, OcrResult
from gametrans.pipeline import Pipeline, PipelineCallbacks
from gametrans.providers.base import Provider
from gametrans.translator import Translator


class FakeCapture:
    """Replays a scripted list of frames, then repeats the final one."""

    name = "fake"

    def __init__(self, frames):
        self._frames = frames
        self.index = 0

    def grab(self, region):
        if self.index < len(self._frames):
            frame = self._frames[self.index]
            self.index += 1
            return frame
        return self._frames[-1] if self._frames else None

    def close(self):
        pass


class FakeOcr:
    """Returns the scripted read for the marker baked into the frame."""

    def __init__(self, script):
        self.script = script

    def read(self, frame):
        text = self.script.get(int(frame[0, 0, 0]), "")
        lines = [OcrLine(text=text, confidence=0.99)] if text else []
        return OcrResult(lines=lines, elapsed_ms=1.0, backend="fake")

    def close(self):
        pass


class FakeProvider(Provider):
    def __init__(self, mapping):
        super().__init__(ProviderConfig(name="fake", kind="openai"))
        self.mapping = mapping
        self.seen = []

    def stream(self, request):
        self.seen.append(request.text)
        yield self.mapping.get(request.text, "ترجمه")


def frame_with_id(marker, shape=(60, 400, 3)):
    """A subtitle-like frame carrying an identifying marker.

    The bar width varies with the marker so that two different markers also look
    structurally different - otherwise the change gate would (correctly) treat
    the second one as the same frame and skip it.
    """
    frame = np.full(shape, 20, dtype=np.uint8)
    bar_width = 100 + (marker % 5) * 50
    frame[10:50, 20 : 20 + bar_width] = 235
    frame[0, 0, 0] = marker  # read back by FakeOcr
    return frame


def build_pipeline(frames, ocr_script, translations, monkeypatch, **overrides):
    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        capture=CaptureConfig(target_fps=200),
        ocr=OcrConfig(upscale=1.0),
        stability=StabilityConfig(frames_required=1, max_wait_ms=10),
        translate=TranslateConfig(cache_path="", concurrency=1),
        overlay=OverlayConfig(linger_ms=10),
    )
    for section, values in overrides.items():
        for key, value in values.items():
            setattr(getattr(cfg, section), key, value)

    capture = FakeCapture(frames)
    monkeypatch.setattr("gametrans.pipeline.create_capture", lambda c, r: capture)
    monkeypatch.setattr(
        "gametrans.pipeline.OcrEngineFacade", lambda c: FakeOcr(ocr_script)
    )

    provider = FakeProvider(translations)
    translator = Translator(cfg.translate, providers=[provider])

    finals = []
    partials = []
    clears = []
    done = threading.Event()

    def on_final(translation, source, total_ms):
        finals.append((translation, source))
        done.set()

    callbacks = PipelineCallbacks(
        on_final=on_final,
        on_partial=lambda t, s: partials.append(t),
        on_clear=lambda: clears.append(True),
    )
    pipeline = Pipeline(cfg, translator, callbacks=callbacks)
    return pipeline, provider, finals, partials, clears, done


def wait_for(predicate, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pipeline_translates_a_subtitle_end_to_end(monkeypatch):
    frames = [frame_with_id(1)] * 6
    pipeline, provider, finals, partials, _clears, done = build_pipeline(
        frames,
        {1: "Hello traveller"},
        {"Hello traveller": "سلام مسافر"},
        monkeypatch,
    )
    pipeline.start()
    try:
        assert done.wait(timeout=5.0), "no translation was produced"
    finally:
        pipeline.stop()

    assert finals[0][0] == "سلام مسافر"
    assert finals[0][1] == "Hello traveller"
    assert partials and partials[-1] == "سلام مسافر"
    assert provider.seen == ["Hello traveller"]


def test_unchanged_frames_never_reach_the_translator(monkeypatch):
    frames = [frame_with_id(1)] * 40
    pipeline, provider, _finals, _partials, _clears, done = build_pipeline(
        frames,
        {1: "Hello traveller"},
        {"Hello traveller": "سلام مسافر"},
        monkeypatch,
    )
    pipeline.start()
    try:
        assert done.wait(timeout=5.0)
        time.sleep(0.3)  # let many more identical frames go by
    finally:
        pipeline.stop()

    # One subtitle on screen for 40 frames must cost exactly one API call.
    assert provider.seen == ["Hello traveller"]
    assert pipeline.metrics.counter("frames_skipped") > 0


def test_a_second_line_is_translated_after_the_first(monkeypatch):
    frames = [frame_with_id(1)] * 5 + [frame_with_id(2)] * 20
    pipeline, provider, finals, _partials, _clears, _done = build_pipeline(
        frames,
        {1: "Hello traveller", 2: "Go north"},
        {"Hello traveller": "سلام مسافر", "Go north": "به شمال برو"},
        monkeypatch,
    )
    pipeline.start()
    try:
        assert wait_for(lambda: len(finals) >= 2, timeout=5.0), f"only got {finals}"
    finally:
        pipeline.stop()

    assert [f[0] for f in finals[:2]] == ["سلام مسافر", "به شمال برو"]


def test_persian_reads_are_rejected_to_break_the_feedback_loop(monkeypatch):
    """If the overlay ever lands inside the captured region, its own output must
    not be read back in and re-translated."""
    frames = [frame_with_id(1)] * 15
    pipeline, provider, finals, _partials, _clears, _done = build_pipeline(
        frames,
        {1: "سلام مسافر"},  # our own Persian output, captured back
        {},
        monkeypatch,
    )
    pipeline.start()
    try:
        time.sleep(0.5)
    finally:
        pipeline.stop()

    assert provider.seen == []
    assert finals == []
    assert pipeline.metrics.counter("self_read_rejected") > 0


def test_ocr_noise_is_never_sent_to_the_api(monkeypatch):
    frames = [frame_with_id(1)] * 15
    pipeline, provider, finals, _partials, _clears, _done = build_pipeline(
        frames,
        {1: "|.-"},
        {},
        monkeypatch,
    )
    pipeline.start()
    try:
        time.sleep(0.4)
    finally:
        pipeline.stop()

    assert provider.seen == []
    assert finals == []


def test_pause_stops_translation_and_resume_restarts_it(monkeypatch):
    frames = [frame_with_id(1)] * 40
    pipeline, provider, finals, _partials, _clears, done = build_pipeline(
        frames,
        {1: "Hello traveller"},
        {"Hello traveller": "سلام مسافر"},
        monkeypatch,
    )
    pipeline.start()
    try:
        assert done.wait(timeout=5.0)
        pipeline.pause()
        assert pipeline.is_paused is True
        count_at_pause = len(provider.seen)
        time.sleep(0.25)
        assert len(provider.seen) == count_at_pause
        pipeline.resume()
        assert pipeline.is_paused is False
    finally:
        pipeline.stop()


def test_starting_without_a_region_is_an_error(monkeypatch):
    pipeline, *_ = build_pipeline([frame_with_id(1)], {}, {}, monkeypatch)
    pipeline.cfg.region = RegionConfig()
    pipeline._region = RegionConfig()
    with pytest.raises(ValueError, match="No capture region"):
        pipeline.start()
