"""Wire-level tests for the streaming providers.

The SSE parsing and the HTTP error mapping are the parts that only break in
production, so they are exercised against a real local HTTP server rather than
by calling the parse helpers directly.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gametrans.config import ProviderConfig
from gametrans.providers.base import (
    AuthError,
    ProviderError,
    RateLimitError,
    TranslationRequest,
    build_provider,
)

# Set per-test to steer the handler below.
SCRIPT = {"status": 200, "lines": [], "body": b""}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the default stderr logging
        pass

    def _respond(self):
        status = SCRIPT["status"]
        if status >= 400:
            body = SCRIPT.get("body", b"error")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            if status == 429:
                self.send_header("retry-after", "7")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for line in SCRIPT["lines"]:
            self.wfile.write(line.encode("utf-8") + b"\n\n")
            self.wfile.flush()

    do_POST = _respond
    do_GET = _respond


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def gemini_provider(server, monkeypatch, **kw):
    monkeypatch.setenv("TEST_GEMINI_KEY", "fake-key")
    return build_provider(
        ProviderConfig(
            name="gemini-test",
            kind="gemini",
            model="gemini-2.5-flash-lite",
            api_key_env="TEST_GEMINI_KEY",
            base_url=server,
            timeout_s=5.0,
            **kw,
        )
    )


def openai_provider(server, monkeypatch, **kw):
    monkeypatch.setenv("TEST_OAI_KEY", "fake-key")
    return build_provider(
        ProviderConfig(
            name="oai-test",
            kind="openai",
            model="llama-3.3-70b-versatile",
            api_key_env="TEST_OAI_KEY",
            base_url=server,
            timeout_s=5.0,
            **kw,
        )
    )


REQUEST = TranslationRequest(text="Hello traveller")


# -- Gemini -----------------------------------------------------------------


def test_gemini_streams_sse_chunks(server, monkeypatch):
    SCRIPT.update(
        status=200,
        lines=[
            "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "سلام"}]}}]}),
            "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": " مسافر"}]}}]}),
        ],
    )
    provider = gemini_provider(server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام", " مسافر"]
    finally:
        provider.close()


def test_gemini_ignores_keepalives_and_malformed_lines(server, monkeypatch):
    SCRIPT.update(
        status=200,
        lines=[
            ": keepalive",
            "data: not-json",
            "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "سلام"}]}}]}),
            "data: [DONE]",
        ],
    )
    provider = gemini_provider(server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()


def test_gemini_disables_thinking_and_sends_the_system_prompt(server, monkeypatch):
    """Reasoning tokens are the biggest latency cost on the 2.5 models."""
    provider = gemini_provider(server, monkeypatch)
    try:
        payload = provider._payload(REQUEST)
    finally:
        provider.close()

    assert payload["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    assert "Persian specifics" in payload["systemInstruction"]["parts"][0]["text"]
    assert payload["contents"][0]["parts"][0]["text"] == "Hello traveller"


def test_gemini_extra_config_is_deep_merged(server, monkeypatch):
    provider = gemini_provider(
        server, monkeypatch, extra={"generationConfig": {"temperature": 0.9}}
    )
    try:
        payload = provider._payload(REQUEST)
    finally:
        provider.close()

    assert payload["generationConfig"]["temperature"] == 0.9
    # The merge must not wipe out the sibling keys.
    assert payload["generationConfig"]["maxOutputTokens"] == 512


# -- OpenAI-compatible ------------------------------------------------------


def test_openai_streams_sse_chunks(server, monkeypatch):
    SCRIPT.update(
        status=200,
        lines=[
            "data: " + json.dumps({"choices": [{"delta": {"content": "سلام"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": " مسافر"}}]}),
            "data: [DONE]",
        ],
    )
    provider = openai_provider(server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام", " مسافر"]
    finally:
        provider.close()


def test_openai_stops_at_done_marker(server, monkeypatch):
    SCRIPT.update(
        status=200,
        lines=[
            "data: " + json.dumps({"choices": [{"delta": {"content": "سلام"}}]}),
            "data: [DONE]",
            "data: " + json.dumps({"choices": [{"delta": {"content": "IGNORED"}}]}),
        ],
    )
    provider = openai_provider(server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()


def test_openai_extra_fields_reach_the_request_body(server, monkeypatch):
    provider = openai_provider(server, monkeypatch, extra={"reasoning_effort": "none"})
    try:
        payload = provider._payload(REQUEST)
    finally:
        provider.close()

    assert payload["reasoning_effort"] == "none"
    assert payload["stream"] is True
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"] == "Hello traveller"


# -- error mapping ----------------------------------------------------------


@pytest.mark.parametrize("make", [gemini_provider, openai_provider])
def test_429_becomes_a_rate_limit_error_with_retry_after(server, monkeypatch, make):
    SCRIPT.update(status=429, body=b'{"error":"quota exceeded"}')
    provider = make(server, monkeypatch)
    try:
        with pytest.raises(RateLimitError) as excinfo:
            list(provider.stream(REQUEST))
        assert excinfo.value.retry_after == 7.0
    finally:
        provider.close()


@pytest.mark.parametrize("make", [gemini_provider, openai_provider])
@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_become_auth_errors(server, monkeypatch, make, status):
    SCRIPT.update(status=status, body=b'{"error":"bad key"}')
    provider = make(server, monkeypatch)
    try:
        with pytest.raises(AuthError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_become_provider_errors(server, monkeypatch, status):
    SCRIPT.update(status=status, body=b"upstream down")
    provider = gemini_provider(server, monkeypatch)
    try:
        with pytest.raises(ProviderError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()


def test_unreachable_host_becomes_a_provider_error(monkeypatch):
    provider = openai_provider("http://127.0.0.1:1", monkeypatch)
    try:
        with pytest.raises(ProviderError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()


def test_missing_api_key_is_reported_at_construction(monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    with pytest.raises(AuthError, match="ABSENT_KEY"):
        build_provider(
            ProviderConfig(
                name="gemini-test", kind="gemini", model="m", api_key_env="ABSENT_KEY"
            )
        )


# -- live model listing ------------------------------------------------------


class ModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(SCRIPT["models_body"]).encode()
        self.send_response(SCRIPT.get("models_status", 200))
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def models_server():
    httpd = HTTPServer(("127.0.0.1", 0), ModelsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_openai_list_models_returns_sorted_ids(models_server, monkeypatch):
    SCRIPT["models_status"] = 200
    SCRIPT["models_body"] = {
        "data": [
            {"id": "qwen/qwen3.6-27b"},
            {"id": "openai/gpt-oss-120b"},
        ]
    }
    provider = openai_provider(models_server, monkeypatch)
    try:
        assert provider.list_models() == ["openai/gpt-oss-120b", "qwen/qwen3.6-27b"]
    finally:
        provider.close()


def test_gemini_list_models_strips_the_models_prefix(models_server, monkeypatch):
    SCRIPT["models_status"] = 200
    SCRIPT["models_body"] = {
        "models": [
            {
                "name": "models/gemini-2.5-flash-lite",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/text-embedding-004",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    provider = gemini_provider(models_server, monkeypatch)
    try:
        # The embedding model cannot generate text, so it must be filtered out.
        assert provider.list_models() == ["gemini-2.5-flash-lite"]
    finally:
        provider.close()


def test_list_models_maps_errors_like_any_other_call(models_server, monkeypatch):
    SCRIPT["models_status"] = 401
    SCRIPT["models_body"] = {"error": "bad key"}
    provider = openai_provider(models_server, monkeypatch)
    try:
        with pytest.raises(AuthError):
            provider.list_models()
    finally:
        provider.close()


def test_list_models_on_an_unreachable_host_is_a_provider_error(monkeypatch):
    provider = openai_provider("http://127.0.0.1:1", monkeypatch)
    try:
        with pytest.raises(ProviderError):
            provider.list_models()
    finally:
        provider.close()


# -- Google's two key formats ------------------------------------------------


class AuthProbeHandler(BaseHTTPRequestHandler):
    """Accepts exactly one auth header, records what each request carried."""

    def log_message(self, *args):
        pass

    def _handle(self):
        AUTH_SCRIPT["seen"].append(
            {
                "x-goog-api-key": self.headers.get("x-goog-api-key"),
                "authorization": self.headers.get("authorization"),
            }
        )
        accepted = AUTH_SCRIPT["accepts"]
        supplied = (
            "bearer" if self.headers.get("authorization") else "api-key"
        )

        if supplied != accepted:
            body = b'{"error":"invalid authentication credential"}'
            self.send_response(401)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.command == "GET":
            body = json.dumps(
                {"models": [{"name": "models/gemini-2.5-flash-lite",
                             "supportedGenerationMethods": ["generateContent"]}]}
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        payload = {"candidates": [{"content": {"parts": [{"text": "سلام"}]}}]}
        self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")

    do_POST = _handle
    do_GET = _handle


AUTH_SCRIPT = {"accepts": "api-key", "seen": []}


@pytest.fixture
def auth_server():
    httpd = HTTPServer(("127.0.0.1", 0), AuthProbeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    AUTH_SCRIPT["seen"] = []
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_standard_key_uses_the_documented_header(auth_server, monkeypatch):
    AUTH_SCRIPT["accepts"] = "api-key"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()

    assert len(AUTH_SCRIPT["seen"]) == 1, "no retry should be needed"
    assert AUTH_SCRIPT["seen"][0]["x-goog-api-key"] == "fake-key"
    assert AUTH_SCRIPT["seen"][0]["authorization"] is None


def test_auth_key_rejected_as_header_falls_back_to_bearer(auth_server, monkeypatch):
    """The newer AQ. keys are rejected on some paths unless sent as a bearer
    token; the provider must recover instead of failing the translation."""
    AUTH_SCRIPT["accepts"] = "bearer"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()

    assert len(AUTH_SCRIPT["seen"]) == 2, "expected one retry"
    assert AUTH_SCRIPT["seen"][0]["x-goog-api-key"] == "fake-key"
    assert AUTH_SCRIPT["seen"][1]["authorization"] == "Bearer fake-key"


def test_the_two_auth_headers_are_never_sent_together(auth_server, monkeypatch):
    """The API rejects a request carrying both as an ambiguous credential."""
    AUTH_SCRIPT["accepts"] = "bearer"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        list(provider.stream(REQUEST))
    finally:
        provider.close()

    for attempt in AUTH_SCRIPT["seen"]:
        assert not (attempt["x-goog-api-key"] and attempt["authorization"])


def test_working_auth_mode_is_remembered_for_later_requests(auth_server, monkeypatch):
    AUTH_SCRIPT["accepts"] = "bearer"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        list(provider.stream(REQUEST))
        AUTH_SCRIPT["seen"] = []
        list(provider.stream(REQUEST))
    finally:
        provider.close()

    assert len(AUTH_SCRIPT["seen"]) == 1, "second call must not retry"
    assert AUTH_SCRIPT["seen"][0]["authorization"] == "Bearer fake-key"


def test_a_genuinely_bad_key_still_raises_after_both_modes(auth_server, monkeypatch):
    AUTH_SCRIPT["accepts"] = "neither"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        with pytest.raises(AuthError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()

    assert len(AUTH_SCRIPT["seen"]) == 2, "both modes should be attempted once"


def test_list_models_also_falls_back(auth_server, monkeypatch):
    AUTH_SCRIPT["accepts"] = "bearer"
    provider = gemini_provider(auth_server, monkeypatch)
    try:
        assert provider.list_models() == ["gemini-2.5-flash-lite"]
    finally:
        provider.close()
    assert len(AUTH_SCRIPT["seen"]) == 2


# -- retired models ----------------------------------------------------------


class RetiredModelHandler(BaseHTTPRequestHandler):
    """404s the old model with Google's real message, serves the new one."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        MODEL_SCRIPT["requested"].append(self.path)
        if MODEL_SCRIPT["always_404"] or MODEL_SCRIPT["live_model"] not in self.path:
            body = json.dumps(
                {
                    "error": {
                        "code": 404,
                        "message": (
                            "This model models/gemini-2.5-flash-lite is no longer "
                            "available to new users. Please update your code to use "
                            f"models/{MODEL_SCRIPT['live_model']} for the latest "
                            "features and improvements."
                        ),
                        "status": "NOT_FOUND",
                    }
                }
            ).encode()
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get("content-length", 0))
        MODEL_SCRIPT["bodies"].append(json.loads(self.rfile.read(length) or b"{}"))

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        payload = {"candidates": [{"content": {"parts": [{"text": "سلام"}]}}]}
        self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")


MODEL_SCRIPT = {
    "live_model": "gemini-3.5-flash-lite",
    # When set, every request 404s - even the suggested replacement - which is
    # how a model name nothing recognises behaves.
    "always_404": False,
    "requested": [],
    "bodies": [],
}


@pytest.fixture
def retired_model_server():
    httpd = HTTPServer(("127.0.0.1", 0), RetiredModelHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    MODEL_SCRIPT["requested"] = []
    MODEL_SCRIPT["bodies"] = []
    MODEL_SCRIPT["always_404"] = False
    MODEL_SCRIPT["live_model"] = "gemini-3.5-flash-lite"
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_retired_model_switches_to_the_suggested_replacement(
    retired_model_server, monkeypatch
):
    """Google retires models and names the successor in the 404 body; using it
    beats failing a translation the player is waiting on."""
    provider = gemini_provider(retired_model_server, monkeypatch)
    provider.cfg.model = "gemini-2.5-flash-lite"
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
        assert provider.cfg.model == "gemini-3.5-flash-lite"
    finally:
        provider.close()

    assert "gemini-2.5-flash-lite" in MODEL_SCRIPT["requested"][0]
    assert "gemini-3.5-flash-lite" in MODEL_SCRIPT["requested"][-1]


def test_the_switch_happens_only_once(retired_model_server, monkeypatch):
    """When even the suggested replacement 404s, give up instead of looping."""
    MODEL_SCRIPT["always_404"] = True
    provider = gemini_provider(retired_model_server, monkeypatch)
    provider.cfg.model = "gemini-2.5-flash-lite"
    try:
        with pytest.raises(ProviderError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()

    assert len(MODEL_SCRIPT["requested"]) == 2, "one original attempt plus one retry"


def test_a_404_without_a_suggestion_is_not_retried(retired_model_server, monkeypatch):
    from gametrans.providers.base import ModelNotFoundError

    provider = gemini_provider(retired_model_server, monkeypatch)
    provider.cfg.model = "gemini-2.5-flash-lite"
    monkeypatch.setattr(
        "gametrans.providers.http_util.suggested_model", lambda body: None
    )
    try:
        with pytest.raises(ModelNotFoundError):
            list(provider.stream(REQUEST))
    finally:
        provider.close()

    assert len(MODEL_SCRIPT["requested"]) == 1, "nothing to switch to, so no retry"


# -- thinking configuration --------------------------------------------------


def test_gemini_3_uses_thinking_level_not_budget(retired_model_server, monkeypatch):
    """Gemini 3.x replaced thinkingBudget with thinkingLevel, and sending both
    in one request is a 400."""
    provider = gemini_provider(retired_model_server, monkeypatch)
    provider.cfg.model = "gemini-3.5-flash-lite"
    try:
        list(provider.stream(REQUEST))
    finally:
        provider.close()

    thinking = MODEL_SCRIPT["bodies"][0]["generationConfig"]["thinkingConfig"]
    assert thinking == {"thinkingLevel": "minimal"}
    assert "thinkingBudget" not in thinking


def test_pre_gemini_3_still_uses_thinking_budget():
    from gametrans.providers.gemini import _thinking_config

    assert _thinking_config("gemini-2.5-flash-lite") == {"thinkingBudget": 0}
    assert _thinking_config("gemini-3.5-flash-lite") == {"thinkingLevel": "minimal"}
    assert _thinking_config("gemini-3.1-flash-lite") == {"thinkingLevel": "minimal"}
    # An unrecognisable name must not crash; fall back to the older parameter.
    assert _thinking_config("") == {"thinkingBudget": 0}


def test_suggested_model_extraction():
    from gametrans.providers.http_util import suggested_model

    body = (
        "This model models/gemini-2.5-flash-lite is no longer available to new "
        "users. Please update your code to use models/gemini-3.5-flash-lite for "
        "the latest features."
    )
    assert suggested_model(body) == "gemini-3.5-flash-lite"
    assert suggested_model("unrelated failure") is None
    assert suggested_model("") is None


# -- servers that reject provider-specific request fields --------------------


class PickyHandler(BaseHTTPRequestHandler):
    """Rejects any request carrying `reasoning_effort`, like some servers do."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        PICKY["bodies"].append(body)

        if "reasoning_effort" in body:
            payload = b'{"error":{"message":"unknown parameter: reasoning_effort"}}'
            self.send_response(400)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {"choices": [{"delta": {"content": "سلام"}}]}
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


PICKY = {"bodies": []}


@pytest.fixture
def picky_server():
    httpd = HTTPServer(("127.0.0.1", 0), PickyHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    PICKY["bodies"] = []
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_unsupported_extra_fields_are_dropped_rather_than_failing(picky_server, monkeypatch):
    """Which optional fields a server accepts varies by product and version.
    A rejected field must not take the whole translation down with it."""
    provider = openai_provider(picky_server, monkeypatch, extra={"reasoning_effort": "none"})
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()

    assert len(PICKY["bodies"]) == 2, "one rejected attempt, then one without"
    assert "reasoning_effort" in PICKY["bodies"][0]
    assert "reasoning_effort" not in PICKY["bodies"][1]


def test_the_field_is_not_sent_again_for_the_rest_of_the_session(picky_server, monkeypatch):
    provider = openai_provider(picky_server, monkeypatch, extra={"reasoning_effort": "none"})
    try:
        list(provider.stream(REQUEST))
        PICKY["bodies"] = []
        list(provider.stream(REQUEST))
    finally:
        provider.close()

    assert len(PICKY["bodies"]) == 1, "the second call should not retry"
    assert "reasoning_effort" not in PICKY["bodies"][0]


def test_a_server_that_accepts_the_field_keeps_receiving_it(server, monkeypatch):
    SCRIPT.update(
        status=200,
        lines=["data: " + json.dumps({"choices": [{"delta": {"content": "سلام"}}]})],
    )
    provider = openai_provider(server, monkeypatch, extra={"reasoning_effort": "none"})
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
        assert provider._blocked_extras == set()
    finally:
        provider.close()


def test_only_the_named_field_is_dropped(picky_server, monkeypatch):
    """Other extras may be doing real work - keep_alive stops a local model
    unloading mid-game - so a rejection must not discard them too."""
    provider = openai_provider(
        picky_server,
        monkeypatch,
        extra={"reasoning_effort": "none", "keep_alive": "30m"},
    )
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()

    final = PICKY["bodies"][-1]
    assert "reasoning_effort" not in final, "the rejected field is gone"
    assert final.get("keep_alive") == "30m", "the innocent field survived"
    assert provider._blocked_extras == {"reasoning_effort"}


def test_an_unhelpful_error_falls_back_to_dropping_everything(picky_server, monkeypatch):
    """Not every server names the offending parameter."""
    import gametrans.providers.openai_compat as module

    monkeypatch.setattr(module, "_rejected_fields", lambda message, candidates: set())
    provider = openai_provider(
        picky_server,
        monkeypatch,
        extra={"reasoning_effort": "none", "keep_alive": "30m"},
    )
    try:
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()

    assert "reasoning_effort" not in PICKY["bodies"][-1]
    assert "keep_alive" not in PICKY["bodies"][-1]


def test_retrying_gives_up_rather_than_looping(picky_server, monkeypatch):
    """If removing fields never helps, fail instead of retrying forever."""
    import gametrans.providers.openai_compat as module

    # Pretend the server dislikes something we are not actually sending.
    monkeypatch.setattr(module, "_rejected_fields", lambda message, candidates: set())
    provider = openai_provider(picky_server, monkeypatch, extra={})
    PICKY["bodies"] = []
    try:
        # With no extras at all the request is accepted, so nothing to retry.
        assert list(provider.stream(REQUEST)) == ["سلام"]
    finally:
        provider.close()
    assert len(PICKY["bodies"]) == 1
