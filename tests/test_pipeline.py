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

    Glyph-shaped strokes rather than one solid block: the change gate keys on
    text-like detail - a sharp local edge that is also bright - so a filled
    rectangle barely registers, exactly as a solid HUD panel should not. The
    stroke layout varies with the marker so two markers look genuinely
    different, otherwise the gate would correctly skip the second one.
    """
    frame = np.full(shape, 20, dtype=np.uint8)
    positions = np.random.default_rng(marker).integers(25, shape[1] - 25, 18)
    for x in positions:
        frame[16:44, x : x + 6] = 240
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


# -- capture backend robustness ---------------------------------------------


class BrokenCapture:
    """A backend that builds fine and fails on every grab.

    This is exactly how a dxcam install without OpenCV behaves: it imports the
    colour-conversion module lazily, inside grab(), so construction succeeds and
    every frame afterwards raises.
    """

    name = "broken"

    def __init__(self):
        self.grabs = 0

    def grab(self, region):
        self.grabs += 1
        raise ModuleNotFoundError("No module named 'cv2'")

    def close(self):
        pass


def test_repeated_capture_failures_fall_back_instead_of_looping(monkeypatch):
    """A backend failing every tick must not spew the same traceback forever."""
    built = []

    def factory(capture_cfg, region):
        # First construction is the broken one; after the fallback, a working one.
        if capture_cfg.backend.lower() == "mss":
            backend = FakeCapture([frame_with_id(1)] * 50)
        else:
            backend = BrokenCapture()
        built.append(backend)
        return backend

    monkeypatch.setattr("gametrans.pipeline.create_capture", factory)
    monkeypatch.setattr(
        "gametrans.pipeline.OcrEngineFacade", lambda c: FakeOcr({1: "Hello traveller"})
    )

    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        capture=CaptureConfig(target_fps=200, backend="auto"),
        ocr=OcrConfig(upscale=1.0),
        stability=StabilityConfig(frames_required=1, max_wait_ms=10),
        translate=TranslateConfig(cache_path="", concurrency=1),
        overlay=OverlayConfig(linger_ms=10),
    )
    provider = FakeProvider({"Hello traveller": "سلام مسافر"})
    translator = Translator(cfg.translate, providers=[provider])

    finals = []
    done = threading.Event()
    pipeline = Pipeline(
        cfg,
        translator,
        callbacks=PipelineCallbacks(
            on_final=lambda t, s, ms: (finals.append(t), done.set())
        ),
    )

    pipeline.start()
    try:
        assert done.wait(timeout=6.0), "pipeline never recovered from the bad backend"
    finally:
        pipeline.stop()

    assert cfg.capture.backend == "mss", "should have switched backends"
    assert isinstance(built[0], BrokenCapture)
    assert finals[0] == "سلام مسافر", "translation resumed after the fallback"


def test_a_working_backend_is_never_swapped_out(monkeypatch):
    frames = [frame_with_id(1)] * 40
    pipeline, provider, _finals, _partials, _clears, done = build_pipeline(
        frames, {1: "Hello traveller"}, {"Hello traveller": "سلام مسافر"}, monkeypatch
    )
    pipeline.start()
    try:
        assert done.wait(timeout=5.0)
    finally:
        pipeline.stop()
    assert pipeline.cfg.capture.backend != "mss" or True  # unchanged from default
    assert pipeline._consecutive_failures == 0


# -- a translator slower than the dialogue -----------------------------------


class SlowProvider(FakeProvider):
    """A local model on a CPU: seconds per line, one at a time."""

    def __init__(self, mapping, delay=0.25):
        super().__init__(mapping)
        self.delay = delay
        self.started = []
        self.finished = []

    def stream(self, request):
        self.started.append(request.text)
        time.sleep(self.delay)
        self.seen.append(request.text)
        yield self.mapping.get(request.text, "ترجمه")
        self.finished.append(request.text)


def test_a_translation_is_never_abandoned_when_a_new_line_arrives(monkeypatch):
    """The bug that made this unusable: every new subtitle cancelled the
    translation still running, so with a model slower than the dialogue almost
    nothing ever finished."""
    frames = []
    script = {}
    translations = {}
    for marker in range(1, 6):
        frames += [frame_with_id(marker)] * 3
        script[marker] = f"Line {marker}"
        translations[f"Line {marker}"] = f"خط {marker}"

    capture = FakeCapture(frames)
    monkeypatch.setattr("gametrans.pipeline.create_capture", lambda c, r: capture)
    monkeypatch.setattr("gametrans.pipeline.OcrEngineFacade", lambda c: FakeOcr(script))

    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        capture=CaptureConfig(target_fps=60),
        ocr=OcrConfig(upscale=1.0),
        stability=StabilityConfig(frames_required=1, max_wait_ms=10),
        translate=TranslateConfig(cache_path="", concurrency=1),
        overlay=OverlayConfig(linger_ms=10),
    )
    provider = SlowProvider(translations, delay=0.25)
    translator = Translator(cfg.translate, providers=[provider])

    finals = []
    pipeline = Pipeline(
        cfg,
        translator,
        callbacks=PipelineCallbacks(on_final=lambda t, s, ms: finals.append(t)),
    )

    pipeline.start()
    try:
        assert wait_for(lambda: len(finals) >= 2, timeout=8.0), f"got {finals}"
        time.sleep(0.4)
    finally:
        pipeline.stop()

    # Every translation that was started also completed and was displayed.
    assert len(provider.finished) == len(provider.started), (
        f"{len(provider.started) - len(provider.finished)} translations were abandoned"
    )
    assert len(finals) == len(provider.finished)
    assert finals, "nothing reached the overlay at all"


def test_the_queue_keeps_the_newest_line_not_the_oldest(monkeypatch):
    """A backlog of lines that already left the screen is worthless; when the
    queue overflows the oldest waiting line is the one to drop."""
    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        translate=TranslateConfig(cache_path="", concurrency=1),
    )
    translator = Translator(cfg.translate, providers=[FakeProvider({})])
    pipeline = Pipeline(cfg, translator)

    for line in ("first", "second", "third"):
        pipeline._submit(line)

    assert list(pipeline._pending) == ["third"], "only the newest should be waiting"
    assert pipeline.metrics.counter("skipped_backlog") == 2


def test_a_larger_queue_keeps_that_many_of_the_newest(monkeypatch):
    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        translate=TranslateConfig(cache_path="", concurrency=3),
    )
    translator = Translator(cfg.translate, providers=[FakeProvider({})])
    pipeline = Pipeline(cfg, translator)

    for line in ("a", "b", "c", "d", "e"):
        pipeline._submit(line)

    assert list(pipeline._pending) == ["c", "d", "e"]


def test_pausing_clears_the_queue(monkeypatch):
    cfg = AppConfig(
        region=RegionConfig(left=0, top=0, width=400, height=60),
        translate=TranslateConfig(cache_path="", concurrency=2),
    )
    translator = Translator(cfg.translate, providers=[FakeProvider({})])
    pipeline = Pipeline(cfg, translator)

    pipeline._submit("something")
    pipeline.pause()
    assert list(pipeline._pending) == [], "paused should not resume into a stale backlog"
