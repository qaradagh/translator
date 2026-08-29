"""The native Ollama provider: newline-delimited JSON and the speed knobs."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gametrans.config import ProviderConfig
from gametrans.providers.base import ProviderError, TranslationRequest, build_provider

STATE = {"bodies": [], "status": 200, "chunks": ["سلام ", "مسافر"], "error": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(
            {"models": [{"name": "aya-expanse:8b"}, {"name": "gemma3:4b"}]}
        ).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        STATE["bodies"].append(json.loads(self.rfile.read(length) or b"{}"))

        if STATE["status"] >= 400:
            payload = b'{"error":"model not found"}'
            self.send_response(STATE["status"])
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.end_headers()

        if STATE["error"]:
            self.wfile.write(json.dumps({"error": STATE["error"]}).encode() + b"\n")
            return

        for piece in STATE["chunks"]:
            line = {"message": {"role": "assistant", "content": piece}, "done": False}
            self.wfile.write(json.dumps(line).encode() + b"\n")
            self.wfile.flush()
        self.wfile.write(json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n")


@pytest.fixture
def ollama():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    STATE.update(bodies=[], status=200, chunks=["سلام ", "مسافر"], error=None)
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def provider(base_url, **kwargs):
    options = dict(name="ollama", kind="ollama", model="aya-expanse:8b", base_url=base_url)
    options.update(kwargs)
    return build_provider(ProviderConfig(**options))


REQUEST = TranslationRequest(text="Hello traveller")


# -- streaming ---------------------------------------------------------------


def test_streams_newline_delimited_json(ollama):
    """Ollama's native API streams NDJSON, not server-sent events."""
    p = provider(ollama)
    try:
        assert list(p.stream(REQUEST)) == ["سلام ", "مسافر"]
    finally:
        p.close()


def test_stops_at_the_done_marker(ollama):
    p = provider(ollama)
    try:
        list(p.stream(REQUEST))
    finally:
        p.close()
    assert STATE["bodies"][0]["stream"] is True


def test_an_error_object_mid_stream_is_raised(ollama):
    STATE["error"] = "out of memory"
    p = provider(ollama)
    try:
        with pytest.raises(ProviderError, match="out of memory"):
            list(p.stream(REQUEST))
    finally:
        p.close()


def test_http_errors_are_reported(ollama):
    STATE["status"] = 404
    p = provider(ollama)
    try:
        with pytest.raises(ProviderError, match="404"):
            list(p.stream(REQUEST))
    finally:
        p.close()


def test_a_stopped_ollama_says_so(monkeypatch):
    p = provider("http://127.0.0.1:1")
    try:
        with pytest.raises(ProviderError, match="is Ollama running"):
            list(p.stream(REQUEST))
    finally:
        p.close()


# -- the settings that decide local speed ------------------------------------


def test_thinking_is_off_by_default(ollama):
    """A reasoning model otherwise spends seconds per line on thoughts nobody
    reads."""
    p = provider(ollama)
    try:
        list(p.stream(REQUEST))
    finally:
        p.close()
    assert STATE["bodies"][0]["think"] is False


def test_options_reach_the_request(ollama):
    p = provider(
        ollama,
        extra={"keep_alive": "30m", "options": {"num_ctx": 2048, "num_thread": 6}},
    )
    try:
        list(p.stream(REQUEST))
    finally:
        p.close()

    body = STATE["bodies"][0]
    assert body["keep_alive"] == "30m"
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["num_thread"] == 6
    # The provider's own defaults survive alongside the user's.
    assert "num_predict" in body["options"]
    assert "temperature" in body["options"]


def test_user_options_win_over_defaults(ollama):
    p = provider(ollama, max_output_tokens=512, extra={"options": {"num_predict": 64}})
    try:
        list(p.stream(REQUEST))
    finally:
        p.close()
    assert STATE["bodies"][0]["options"]["num_predict"] == 64


def test_compact_prompt_is_much_shorter(ollama):
    p = provider(ollama, compact_prompt=True)
    try:
        list(p.stream(TranslationRequest(text="Hello", compact_prompt=True)))
        compact = STATE["bodies"][-1]["messages"][0]["content"]
        STATE["bodies"] = []
        list(p.stream(TranslationRequest(text="Hello", compact_prompt=False)))
        full = STATE["bodies"][-1]["messages"][0]["content"]
    finally:
        p.close()

    assert len(compact) < len(full) / 2, "the compact prompt should be far smaller"
    # It must still carry the rules that keep Persian output correct.
    assert "ک" in compact and "ی" in compact
    assert "only the translation" in compact.lower()


# -- addressing --------------------------------------------------------------


def test_an_openai_style_url_is_accepted(ollama):
    """Switching `kind` in an existing config should not also require editing
    the address."""
    p = provider(ollama + "/v1")
    try:
        assert list(p.stream(REQUEST)) == ["سلام ", "مسافر"]
    finally:
        p.close()


def test_list_models_reads_the_tags_endpoint(ollama):
    p = provider(ollama)
    try:
        assert p.list_models() == ["aya-expanse:8b", "gemma3:4b"]
    finally:
        p.close()


def test_list_models_on_a_stopped_ollama_says_so():
    p = provider("http://127.0.0.1:1")
    try:
        with pytest.raises(ProviderError, match="is Ollama running"):
            p.list_models()
    finally:
        p.close()
