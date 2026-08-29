"""Router behaviour: cache, failover, streaming, sanitisation, staleness."""

import time

import pytest

from gametrans.config import ProviderConfig, TranslateConfig
from gametrans.providers.base import AuthError, Provider, ProviderError, RateLimitError


class FakeProvider(Provider):
    """Scripted provider: yields chunks, or raises whatever it was given."""

    def __init__(self, name, chunks=None, error=None, rpm_limit=0):
        super().__init__(ProviderConfig(name=name, kind="openai", rpm_limit=rpm_limit))
        self._chunks = chunks or []
        self._error = error
        self.calls = 0

    def stream(self, request):
        self.calls += 1
        if self._error is not None:
            raise self._error
        for chunk in self._chunks:
            yield chunk


def make_translator(providers, **cfg_kwargs):
    from gametrans.translator import Translator

    cfg = TranslateConfig(cache_path="", **cfg_kwargs)
    return Translator(cfg, providers=providers)


def test_successful_translation_joins_streamed_chunks():
    provider = FakeProvider("p1", chunks=["سلام", " ", "مسافر"])
    translator = make_translator([provider])
    outcome = translator.translate("Hello traveller")
    assert outcome.ok
    assert outcome.text == "سلام مسافر"
    assert outcome.provider == "p1"
    assert outcome.cached is False


def test_streaming_callback_receives_progressive_text():
    provider = FakeProvider("p1", chunks=["سلام", " ", "مسافر"])
    translator = make_translator([provider])
    seen = []
    translator.translate("Hello traveller", on_chunk=lambda acc, chunk: seen.append(acc))
    assert seen == ["سلام", "سلام ", "سلام مسافر"]


def test_second_call_is_served_from_cache_without_touching_the_provider():
    provider = FakeProvider("p1", chunks=["سلام"])
    translator = make_translator([provider])
    translator.translate("Hello")
    outcome = translator.translate("hello!!")  # same after normalisation
    assert outcome.cached is True
    assert outcome.text == "سلام"
    assert provider.calls == 1


def test_cache_hit_still_fires_the_chunk_callback():
    provider = FakeProvider("p1", chunks=["سلام"])
    translator = make_translator([provider])
    translator.translate("Hello")
    seen = []
    translator.translate("Hello", on_chunk=lambda acc, chunk: seen.append(acc))
    assert seen == ["سلام"]


def test_rate_limited_provider_fails_over_to_the_next():
    first = FakeProvider("gemini", error=RateLimitError("429", retry_after=30.0))
    second = FakeProvider("groq", chunks=["سلام"])
    translator = make_translator([first, second])

    outcome = translator.translate("Hello")
    assert outcome.ok
    assert outcome.provider == "groq"
    assert outcome.attempts == ["gemini", "groq"]


def test_rate_limited_provider_is_skipped_on_the_next_line():
    first = FakeProvider("gemini", error=RateLimitError("429", retry_after=30.0))
    second = FakeProvider("groq", chunks=["سلام"])
    translator = make_translator([first, second])

    translator.translate("Hello")
    translator.translate("Something else")
    # The first provider is blocked, so it must not be retried immediately.
    assert first.calls == 1
    assert second.calls == 2


def test_provider_error_fails_over():
    first = FakeProvider("a", error=ProviderError("boom"))
    second = FakeProvider("b", chunks=["سلام"])
    translator = make_translator([first, second])
    assert translator.translate("Hello").provider == "b"


def test_auth_error_disables_the_provider_for_the_session():
    first = FakeProvider("a", error=AuthError("bad key"))
    second = FakeProvider("b", chunks=["سلام"])
    translator = make_translator([first, second])

    translator.translate("One")
    translator.translate("Two")
    assert first.calls == 1


def test_all_providers_failing_reports_an_error():
    providers = [
        FakeProvider("a", error=ProviderError("down")),
        FakeProvider("b", error=RateLimitError("429")),
    ]
    translator = make_translator(providers)
    outcome = translator.translate("Hello")
    assert outcome.ok is False
    assert outcome.error


def test_empty_model_output_falls_through_to_the_next_provider():
    first = FakeProvider("a", chunks=["", "  "])
    second = FakeProvider("b", chunks=["سلام"])
    translator = make_translator([first, second])
    assert translator.translate("Hello").provider == "b"


def test_output_is_sanitised_and_persian_normalised():
    provider = FakeProvider("p", chunks=['"كتاب ', 'ياد"'])
    translator = make_translator([provider])
    assert translator.translate("book").text == "کتاب یاد"


def test_blank_input_is_a_no_op():
    provider = FakeProvider("p", chunks=["سلام"])
    translator = make_translator([provider])
    outcome = translator.translate("   ")
    assert outcome.text == ""
    assert provider.calls == 0


def test_cancellation_stops_before_calling_a_provider():
    provider = FakeProvider("p", chunks=["سلام"])
    translator = make_translator([provider])
    outcome = translator.translate("Hello", is_cancelled=lambda: True)
    assert outcome.error == "cancelled"
    assert provider.calls == 0


def test_client_side_rpm_limit_moves_to_the_next_provider():
    first = FakeProvider("slow", chunks=["اول"], rpm_limit=1)
    second = FakeProvider("fast", chunks=["دوم"])
    translator = make_translator([first, second])

    assert translator.translate("Line one").provider == "slow"
    # `slow` has spent its single request for this minute.
    assert translator.translate("Line two").provider == "fast"


def test_metrics_track_cache_hits_and_misses():
    provider = FakeProvider("p", chunks=["سلام"])
    translator = make_translator([provider])
    translator.translate("Hello")
    translator.translate("Hello")
    assert translator.metrics.counter("cache_miss") == 1
    assert translator.metrics.counter("cache_hit") == 1


def test_empty_provider_chain_is_rejected_at_construction():
    from gametrans.translator import Translator

    with pytest.raises(RuntimeError, match="No translation provider"):
        Translator(TranslateConfig(cache_path="", providers=[]))


# -- rate-limit waiting ------------------------------------------------------


def test_waits_for_a_slot_rather_than_dropping_the_line(monkeypatch):
    """Hitting a free tier's per-minute limit is routine during a talkative
    scene. Dropping the subtitle loses it; waiting a moment keeps it."""
    import gametrans.translator as translator_module

    provider = FakeProvider("only", chunks=["سلام"], rpm_limit=1)
    translator = make_translator([provider], max_wait_for_slot_ms=2500)

    assert translator.translate("First line").ok

    slept = []
    monkeypatch.setattr(translator_module.time, "sleep", lambda s: slept.append(s))
    # The limiter still reports a long wait, so pretend the window rolled over
    # once the translator has decided to wait.
    original = provider.limiter.seconds_until_available
    calls = {"n": 0}

    def fake_wait():
        calls["n"] += 1
        return 0.2 if calls["n"] == 1 else 0.0

    provider.limiter.seconds_until_available = fake_wait
    provider.limiter.reset()

    outcome = translator.translate("Second line")
    provider.limiter.seconds_until_available = original

    assert outcome.ok, outcome.error
    assert provider.calls == 2


def test_gives_up_when_the_wait_exceeds_the_budget():
    provider = FakeProvider("only", chunks=["سلام"], rpm_limit=1)
    translator = make_translator([provider], max_wait_for_slot_ms=50)

    assert translator.translate("First line").ok
    outcome = translator.translate("Second line")

    assert outcome.ok is False
    assert "rate limited" in outcome.error
    assert "setkey groq" in outcome.error, "the error should say how to fix it"


def test_waiting_is_skipped_when_a_provider_is_free():
    fast = FakeProvider("fast", chunks=["سلام"])
    translator = make_translator([fast], max_wait_for_slot_ms=5000)
    started = time.monotonic()
    assert translator.translate("Hello").ok
    assert time.monotonic() - started < 0.5, "should not have waited at all"


def test_cancellation_interrupts_the_wait():
    provider = FakeProvider("only", chunks=["سلام"], rpm_limit=1)
    translator = make_translator([provider], max_wait_for_slot_ms=5000)
    translator.translate("First line")

    started = time.monotonic()
    outcome = translator.translate("Second line", is_cancelled=lambda: True)
    assert time.monotonic() - started < 1.0
    assert outcome.error == "cancelled"


def test_seconds_until_available_reports_a_sane_delay():
    from gametrans.providers.base import RateLimiter

    limiter = RateLimiter(2)
    assert limiter.seconds_until_available() == 0.0
    limiter.record()
    assert limiter.seconds_until_available() == 0.0
    limiter.record()
    delay = limiter.seconds_until_available()
    assert 55.0 < delay <= 60.0

    limiter.reset()
    limiter.block_for(30.0)
    assert 25.0 < limiter.seconds_until_available() <= 30.0
