"""Model comparison: timing, reporting, and the HTML output."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from gametrans.benchmark import (
    SAMPLE_LINES,
    ModelResult,
    benchmark_model,
    benchmark_models,
    format_console_report,
    write_html_report,
)

REPLIES = {
    "fast-model": "سلام مسافر",
    "slow-model": "درود بر تو ای رهگذر",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": name} for name in REPLIES]}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        model = json.loads(self.rfile.read(length))["model"]
        if model == "broken-model":
            self.send_response(500)
            self.send_header("content-length", "5")
            self.end_headers()
            self.wfile.write(b"boom!")
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for word in REPLIES.get(model, "ترجمه").split():
            payload = {"choices": [{"delta": {"content": word + " "}}]}
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture(scope="module")
def endpoint():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


# -- measuring ---------------------------------------------------------------


def test_benchmark_translates_every_sample_line(endpoint):
    result = benchmark_model("fast-model", endpoint)
    assert result.ok
    assert len(result.translations) == len(SAMPLE_LINES)
    assert all(t for t in result.translations)
    assert len(result.first_token_ms) == len(SAMPLE_LINES)


def test_benchmark_records_timings(endpoint):
    result = benchmark_model("fast-model", endpoint, lines=SAMPLE_LINES[:2])
    assert result.median_total > 0
    assert result.worst_total >= result.median_total


def test_a_failing_model_is_reported_not_raised(endpoint):
    result = benchmark_model("broken-model", endpoint)
    assert result.ok is False
    assert result.error


def test_an_unreachable_endpoint_is_reported(endpoint):
    result = benchmark_model("any", "http://127.0.0.1:1/v1")
    assert result.ok is False
    assert result.error


def test_benchmark_models_runs_each_candidate(endpoint):
    results = benchmark_models(
        ["fast-model", "slow-model"], endpoint, lines=SAMPLE_LINES[:1]
    )
    assert [r.model for r in results] == ["fast-model", "slow-model"]
    assert all(r.ok for r in results)


def test_reasoning_is_disabled_by_default(endpoint):
    """Local reasoning models will think for seconds per line otherwise, which
    measures the wrong thing entirely."""
    from gametrans.config import ProviderConfig

    captured = {}
    real_build = __import__("gametrans.providers.base", fromlist=["build_provider"])

    def spy(entry: ProviderConfig):
        captured["extra"] = dict(entry.extra)
        return real_build.build_provider(entry)

    import gametrans.benchmark as bench

    original = bench.build_provider
    bench.build_provider = spy
    try:
        benchmark_model("fast-model", endpoint, lines=SAMPLE_LINES[:1])
    finally:
        bench.build_provider = original

    assert captured["extra"].get("reasoning_effort") == "none"


# -- reporting ---------------------------------------------------------------


def test_console_report_sorts_fastest_first():
    slow = ModelResult(model="slow", translations=["الف"], first_token_ms=[400.0], total_ms=[400.0])
    fast = ModelResult(model="fast", translations=["ب"], first_token_ms=[100.0], total_ms=[100.0])
    report = format_console_report([slow, fast])
    assert report.index("fast") < report.index("slow")
    assert "Fastest: fast" in report


def test_console_report_lists_failures_last_without_crashing():
    good = ModelResult(model="good", translations=["الف"], first_token_ms=[100.0], total_ms=[100.0])
    bad = ModelResult(model="bad", error="out of memory")
    report = format_console_report([bad, good])
    assert "out of memory" in report
    assert report.index("good") < report.index("bad")


def test_console_report_handles_all_models_failing():
    report = format_console_report([ModelResult(model="a", error="nope")])
    assert "nope" in report
    assert "Fastest" not in report


def test_html_report_renders_rtl_and_every_model(tmp_path):
    results = [
        ModelResult(model="alpha", translations=["سلام", "خداحافظ"],
                    first_token_ms=[100.0, 110.0], total_ms=[120.0, 130.0]),
        ModelResult(model="beta", translations=["درود", "بدرود"],
                    first_token_ms=[200.0, 210.0], total_ms=[220.0, 230.0]),
    ]
    path = write_html_report(results, tmp_path / "report.html", lines=["Hello", "Goodbye"])
    body = path.read_text(encoding="utf-8")

    assert "direction: rtl" in body, "Persian must be laid out right to left"
    for expected in ("alpha", "beta", "سلام", "بدرود", "Hello", "Goodbye"):
        assert expected in body


def test_html_report_escapes_content(tmp_path):
    results = [ModelResult(model="x<script>", translations=["<b>hi</b>"],
                           first_token_ms=[1.0], total_ms=[1.0])]
    path = write_html_report(results, tmp_path / "r.html", lines=["a & b"])
    body = path.read_text(encoding="utf-8")
    assert "<script>" not in body.replace("<script", "&lt;script")
    assert "&amp;" in body


def test_html_report_marks_failed_models(tmp_path):
    results = [ModelResult(model="dead", error="timed out")]
    path = write_html_report(results, tmp_path / "r.html", lines=["Hello"])
    body = path.read_text(encoding="utf-8")
    assert "failed" in body
    assert "—" in body
