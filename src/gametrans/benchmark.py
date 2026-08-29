"""Compare translation models on the machine that will run them.

Which local model is best for Persian game subtitles has no general answer: it
depends on the GPU, the quantisation Ollama pulled, and how the model handles
this particular kind of short, colloquial text. Guessing from parameter counts
is how people end up with a model that is either too slow to keep up or
translates like a manual.

So measure it. Each candidate translates the same set of game lines, and the
report gives first-token latency, total time, and the actual Persian output to
judge. Results are also written to an HTML file, because a Windows console
prints right-to-left text reversed and unjoined - unreadable exactly when you
are trying to compare translation quality.
"""

from __future__ import annotations

import html
import logging
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .config import ProviderConfig, TranslateConfig
from .providers.base import Provider, ProviderError, TranslationRequest, build_provider
from .textnorm import sanitize_translation

log = logging.getLogger(__name__)

# Deliberately varied: a plain line, one with a name and a number, an idiom that
# punishes literal translation, an imperative, and a fragment.
SAMPLE_LINES = [
    "You must reach the castle before nightfall, traveller.",
    "Geralt, I'll pay you 500 crowns for the contract.",
    "Don't push your luck, kid.",
    "Press E to open the gate.",
    "It's a trap!",
]


@dataclass
class ModelResult:
    model: str
    translations: List[str] = field(default_factory=list)
    first_token_ms: List[float] = field(default_factory=list)
    total_ms: List[float] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.translations)

    @property
    def median_first_token(self) -> float:
        return statistics.median(self.first_token_ms) if self.first_token_ms else 0.0

    @property
    def median_total(self) -> float:
        return statistics.median(self.total_ms) if self.total_ms else 0.0

    @property
    def worst_total(self) -> float:
        return max(self.total_ms) if self.total_ms else 0.0


def benchmark_model(
    model: str,
    base_url: str,
    kind: str = "openai",
    api_key_env: str = "",
    lines: Sequence[str] = SAMPLE_LINES,
    target_language: str = "Persian (Farsi)",
    timeout_s: float = 60.0,
    extra: Optional[dict] = None,
) -> ModelResult:
    """Translate every sample line with one model and time each one."""
    result = ModelResult(model=model)

    entry = ProviderConfig(
        name=model,
        kind=kind,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_s=timeout_s,
        # Local reasoning models will think for many seconds per line unless
        # told not to, which is not what this is measuring.
        extra=extra if extra is not None else {"reasoning_effort": "none"},
    )

    try:
        provider: Provider = build_provider(entry)
    except Exception as exc:
        result.error = str(exc)
        return result

    try:
        # Ollama loads a model into VRAM on first use, which for a 12B model can
        # take longer than every translation combined. Timing that would rank
        # models by file size rather than by speed, so pay it before the clock
        # starts. Failures here are ignored: the timed pass will report them
        # properly.
        try:
            warm = TranslationRequest(text="Hello.", target_language=target_language)
            for _ in provider.stream(warm):
                pass
        except Exception as exc:
            log.debug("warmup for %s failed: %s", model, exc)

        for line in lines:
            request = TranslationRequest(text=line, target_language=target_language)
            started = time.perf_counter()
            first_token = 0.0
            pieces: List[str] = []
            try:
                for chunk in provider.stream(request):
                    if not pieces:
                        first_token = (time.perf_counter() - started) * 1000.0
                    pieces.append(chunk)
            except ProviderError as exc:
                result.error = str(exc)
                return result

            total = (time.perf_counter() - started) * 1000.0
            result.translations.append(sanitize_translation("".join(pieces)))
            result.first_token_ms.append(first_token)
            result.total_ms.append(total)
    finally:
        provider.close()

    return result


def benchmark_models(
    models: Sequence[str],
    base_url: str,
    lines: Sequence[str] = SAMPLE_LINES,
    **kwargs,
) -> List[ModelResult]:
    results = []
    for model in models:
        log.info("benchmarking %s", model)
        results.append(benchmark_model(model, base_url, lines=lines, **kwargs))
    return results


def format_console_report(results: Sequence[ModelResult]) -> str:
    """A speed table. Quality goes in the HTML report, where Persian renders."""
    lines = [
        f"{'model':<28} {'first token':>12} {'per line':>10} {'worst':>9}",
        "-" * 62,
    ]
    for result in sorted(results, key=lambda r: (not r.ok, r.median_total)):
        if not result.ok:
            lines.append(f"{result.model:<28} failed: {(result.error or '')[:60]}")
            continue
        lines.append(
            f"{result.model:<28} {result.median_first_token:>10.0f}ms "
            f"{result.median_total:>8.0f}ms {result.worst_total:>7.0f}ms"
        )

    usable = [r for r in results if r.ok]
    if usable:
        fastest = min(usable, key=lambda r: r.median_total)
        lines.append("")
        lines.append(f"Fastest: {fastest.model} ({fastest.median_total:.0f}ms per line)")
        lines.append(
            "Speed is only half the question - open the HTML report to compare"
        )
        lines.append("the actual Persian, which this console cannot display correctly.")
    return "\n".join(lines)


def write_html_report(
    results: Sequence[ModelResult],
    path: Path,
    lines: Sequence[str] = SAMPLE_LINES,
) -> Path:
    """Write a side-by-side report that renders Persian properly."""
    rows = []
    for index, source in enumerate(lines):
        cells = []
        for result in results:
            if result.ok and index < len(result.translations):
                cells.append(
                    f'<td class="fa">{html.escape(result.translations[index])}</td>'
                )
            else:
                cells.append('<td class="bad">—</td>')
        rows.append(
            f"<tr><td class=\"src\">{html.escape(source)}</td>{''.join(cells)}</tr>"
        )

    headers = "".join(
        f"<th>{html.escape(r.model)}<br><span class='ms'>"
        f"{r.median_total:.0f} ms/line</span></th>"
        if r.ok
        else f"<th>{html.escape(r.model)}<br><span class='bad'>failed</span></th>"
        for r in results
    )

    document = f"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<title>gametrans - model comparison</title>
<style>
  body {{ font-family: Segoe UI, system-ui, sans-serif; margin: 32px;
         background: #14161c; color: #e8ecf2; }}
  h1 {{ font-size: 20px; font-weight: 600; }}
  p.note {{ color: #9aa5b4; max-width: 70ch; line-height: 1.6; }}
  table {{ border-collapse: collapse; margin-top: 20px; width: 100%; }}
  th, td {{ border: 1px solid #2b303b; padding: 10px 12px;
            vertical-align: top; text-align: left; }}
  th {{ background: #1b1f27; font-weight: 600; }}
  .ms {{ color: #7fb3ff; font-weight: 400; font-size: 12px; }}
  .src {{ color: #9aa5b4; width: 28%; }}
  .fa {{ direction: rtl; text-align: right;
         font-family: Vazirmatn, Segoe UI, Tahoma, sans-serif;
         font-size: 17px; line-height: 1.9; }}
  .bad {{ color: #ff8a8a; }}
</style>
</head>
<body>
<h1>Which model translates your game best?</h1>
<p class="note">
  Same lines through every model, on this machine. The numbers are speed;
  the columns are quality, which only you can judge. A model that reads
  naturally at 400&nbsp;ms beats a stiff one at 200&nbsp;ms - the pipeline
  hides latency with caching, but it cannot fix a bad translation.
</p>
<table>
  <thead><tr><th class="src">English</th>{headers}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
    return path
