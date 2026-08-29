"""Latency instrumentation.

Every stage records into the same registry so `--stats` can show where the
end-to-end time actually goes, rather than where it is assumed to go.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List


@dataclass
class Stat:
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


class Metrics:
    """Bounded rolling window of timings per named stage."""

    def __init__(self, window: int = 200) -> None:
        self._window = window
        self._samples: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._counters: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def record(self, stage: str, ms: float) -> None:
        with self._lock:
            self._samples[stage].append(ms)

    def increment(self, counter: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[counter] += amount

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def stat(self, stage: str) -> Stat:
        with self._lock:
            values: List[float] = sorted(self._samples.get(stage, ()))
        if not values:
            return Stat(0, 0.0, 0.0, 0.0, 0.0)
        count = len(values)
        return Stat(
            count=count,
            mean_ms=sum(values) / count,
            p50_ms=values[int(count * 0.50)] if count > 1 else values[0],
            p95_ms=values[min(int(count * 0.95), count - 1)],
            max_ms=values[-1],
        )

    def stages(self) -> List[str]:
        with self._lock:
            return sorted(self._samples.keys())

    def counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def report(self) -> str:
        lines = ["stage                     n     mean      p50      p95      max"]
        for stage in self.stages():
            s = self.stat(stage)
            lines.append(
                f"{stage:<22} {s.count:>5} {s.mean_ms:>8.1f} {s.p50_ms:>8.1f} "
                f"{s.p95_ms:>8.1f} {s.max_ms:>8.1f}"
            )
        counters = self.counters()
        if counters:
            lines.append("")
            lines.append("counters: " + "  ".join(f"{k}={v}" for k, v in sorted(counters.items())))
        return "\n".join(lines)
