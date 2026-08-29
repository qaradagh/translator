import time

import pytest

from gametrans.config import ProviderConfig
from gametrans.providers.base import (
    RateLimiter,
    TranslationRequest,
    build_provider,
    build_system_prompt,
    resolve_api_key,
)
from gametrans.providers.gemini import _deep_merge, _extract_text
from gametrans.providers.openai_compat import _delta_text


# -- prompt -----------------------------------------------------------------


def test_prompt_includes_persian_rules_for_a_persian_target():
    prompt = build_system_prompt(TranslationRequest(text="hi", target_language="Persian (Farsi)"))
    assert "Persian specifics" in prompt
    assert "ک" in prompt


def test_prompt_omits_persian_rules_for_other_targets():
    prompt = build_system_prompt(TranslationRequest(text="hi", target_language="German"))
    assert "Persian specifics" not in prompt
    assert "Translate into: German" in prompt


def test_prompt_carries_glossary_and_context():
    prompt = build_system_prompt(
        TranslationRequest(
            text="hi",
            context_hint="Elden Ring",
            glossary={"Site of Grace": "جایگاه فیض"},
        )
    )
    assert "Elden Ring" in prompt
    assert "Site of Grace -> جایگاه فیض" in prompt


def test_prompt_is_byte_stable_across_calls():
    """Providers that cache prompt prefixes need identical bytes every time."""
    request = TranslationRequest(text="hi", glossary={"b": "2", "a": "1"})
    assert build_system_prompt(request) == build_system_prompt(request)


# -- rate limiter -----------------------------------------------------------


def test_rate_limiter_allows_up_to_the_limit():
    limiter = RateLimiter(3)
    for _ in range(3):
        assert limiter.available() is True
        limiter.record()
    assert limiter.available() is False


def test_zero_limit_means_unlimited():
    limiter = RateLimiter(0)
    for _ in range(100):
        limiter.record()
    assert limiter.available() is True


def test_block_for_marks_the_provider_unusable():
    limiter = RateLimiter(0)
    limiter.block_for(60.0)
    assert limiter.available() is False
    limiter.reset()
    assert limiter.available() is True


def test_window_expires(monkeypatch):
    limiter = RateLimiter(2)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    limiter.record()
    limiter.record()
    assert limiter.available() is False
    monkeypatch.setattr(time, "monotonic", lambda: base + 61.0)
    assert limiter.available() is True


# -- SSE payload parsing ----------------------------------------------------


def test_gemini_chunk_extraction():
    chunk = {"candidates": [{"content": {"parts": [{"text": "سلام"}, {"text": " دنیا"}]}}]}
    assert list(_extract_text(chunk)) == ["سلام", " دنیا"]


def test_gemini_extraction_tolerates_empty_shapes():
    assert list(_extract_text({})) == []
    assert list(_extract_text({"candidates": []})) == []
    assert list(_extract_text({"candidates": [{}]})) == []
    assert list(_extract_text({"candidates": [{"content": {"parts": [{}]}}]})) == []


def test_deep_merge_overrides_nested_keys():
    target = {"generationConfig": {"temperature": 0.2, "maxOutputTokens": 512}}
    _deep_merge(target, {"generationConfig": {"temperature": 0.9}})
    assert target["generationConfig"] == {"temperature": 0.9, "maxOutputTokens": 512}


def test_openai_delta_extraction():
    assert _delta_text({"choices": [{"delta": {"content": "سلام"}}]}) == "سلام"
    assert _delta_text({"choices": [{"delta": {}}]}) == ""
    assert _delta_text({"choices": []}) == ""
    assert _delta_text({}) == ""


def test_openai_delta_handles_content_parts_and_non_streaming():
    parts = {"choices": [{"delta": {"content": [{"text": "سلام"}, {"text": "!"}]}}]}
    assert _delta_text(parts) == "سلام!"
    non_streaming = {"choices": [{"message": {"content": "سلام"}}]}
    assert _delta_text(non_streaming) == "سلام"


# -- construction -----------------------------------------------------------


def test_resolve_api_key(monkeypatch):
    monkeypatch.setenv("MY_KEY", "  abc  ")
    assert resolve_api_key(ProviderConfig(api_key_env="MY_KEY")) == "abc"
    monkeypatch.setenv("EMPTY_KEY", "   ")
    assert resolve_api_key(ProviderConfig(api_key_env="EMPTY_KEY")) is None
    assert resolve_api_key(ProviderConfig(api_key_env="")) is None


def test_unknown_provider_kind_is_rejected():
    with pytest.raises(ValueError, match="Unknown provider kind"):
        build_provider(ProviderConfig(kind="not-a-thing"))


def test_openai_provider_requires_a_base_url():
    with pytest.raises(ValueError, match="base_url is required"):
        build_provider(ProviderConfig(name="x", kind="openai", model="m"))


def test_local_openai_provider_needs_no_api_key():
    provider = build_provider(
        ProviderConfig(
            name="ollama",
            kind="openai",
            model="qwen3:8b",
            base_url="http://127.0.0.1:11434/v1",
        )
    )
    assert provider.name == "ollama"
    provider.close()
