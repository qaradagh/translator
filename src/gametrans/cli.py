"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List, Optional

from . import __version__
from .config import AppConfig, RegionConfig, load_config, save_region

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gametrans",
        description="Translate on-screen game text to Persian, in real time.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to the config file (default: ./config.toml if present)",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--version", action="version", version=f"gametrans {__version__}")

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the overlay translator (default)")
    run.add_argument("--region", help="left,top,width,height - overrides the config")
    run.add_argument("--fps", type=int, help="capture polling rate")
    run.add_argument("--show-source", action="store_true", help="also draw the original text")
    run.add_argument("--stats", action="store_true", help="draw live latency numbers")

    sub.add_parser("pick-region", help="select the subtitle area and save it")
    sub.add_parser("monitors", help="list monitors with their pixel geometry")
    sub.add_parser("check", help="verify OCR engines, providers and keys")

    translate = sub.add_parser("translate", help="translate one string (no screen capture)")
    translate.add_argument("text", nargs="+", help="text to translate")

    bench = sub.add_parser("bench", help="measure per-stage latency on the current region")
    bench.add_argument("--iterations", type=int, default=20)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Where region edits get written back to, whether or not the file exists yet.
    args.config = args.config or "config.toml"

    _setup_logging(args.log_level or cfg.log_level)

    command = args.command or "run"
    handlers = {
        "run": lambda: _cmd_run(cfg, args),
        "pick-region": lambda: _cmd_pick_region(cfg, args),
        "monitors": lambda: _cmd_monitors(),
        "check": lambda: _cmd_check(cfg),
        "translate": lambda: _cmd_translate(cfg, args),
        "bench": lambda: _cmd_bench(cfg, args),
    }
    return handlers[command]()


# -- commands ---------------------------------------------------------------


def _cmd_run(cfg: AppConfig, args) -> int:
    if getattr(args, "region", None):
        try:
            left, top, width, height = (int(p) for p in args.region.split(","))
        except ValueError:
            print("error: --region must be left,top,width,height", file=sys.stderr)
            return 2
        cfg.region = RegionConfig(left=left, top=top, width=width, height=height)
    if getattr(args, "fps", None):
        cfg.capture.target_fps = args.fps
    if getattr(args, "show_source", False):
        cfg.overlay.show_source = True
    if getattr(args, "stats", False):
        cfg.overlay.show_latency = True

    from .app import Application

    try:
        app = Application(cfg, config_path=args.config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return app.run()


def _cmd_pick_region(cfg: AppConfig, args) -> int:
    from .region import pick_region_blocking

    print("Drag over the area where the game draws its subtitles (Esc to cancel).")
    region = pick_region_blocking(cfg.region)
    if region is None:
        print("cancelled")
        return 1

    save_region(args.config, region)
    print(
        f"region saved to {args.config}: "
        f"left={region.left} top={region.top} "
        f"width={region.width} height={region.height} monitor={region.monitor}"
    )
    return 0


def _cmd_monitors() -> int:
    from .capture import list_monitors

    for index, monitor in enumerate(list_monitors()):
        label = "all screens" if index == 0 else f"monitor {index}"
        print(
            f"{label:<14} left={monitor['left']:<6} top={monitor['top']:<6} "
            f"width={monitor['width']:<6} height={monitor['height']}"
        )
    return 0


def _cmd_check(cfg: AppConfig) -> int:
    import os
    import platform

    ok = True
    print(f"gametrans {__version__} on {platform.system()} {platform.release()}\n")

    print("OCR engines")
    from .ocr import create_backend
    from .config import OcrConfig

    found_ocr = False
    for name in ("windows", "rapidocr", "tesseract"):
        probe = OcrConfig(backend=name, languages=cfg.ocr.languages)
        try:
            backend = create_backend(probe)
            backend.close()
            print(f"  [ok]   {name}")
            found_ocr = True
        except Exception as exc:
            print(f"  [--]   {name}: {str(exc)[:90]}")
    if not found_ocr:
        ok = False
        print("  -> install one: pip install \"gametrans[windows]\" (or [rapidocr])")

    print("\nTranslation providers")
    found_provider = False
    for entry in cfg.translate.providers:
        status = "disabled" if not entry.enabled else ""
        key_state = "no key needed"
        if entry.api_key_env:
            key_state = "key set" if os.environ.get(entry.api_key_env) else \
                f"MISSING ${entry.api_key_env}"
        mark = "ok" if (entry.enabled and "MISSING" not in key_state) else "--"
        if mark == "ok":
            found_provider = True
        print(f"  [{mark}]   {entry.name:<20} {entry.model:<28} {key_state} {status}")
    if not found_provider:
        ok = False
        print("  -> get a free key: https://aistudio.google.com/apikey "
              "or https://console.groq.com/keys")

    print("\nRegion")
    if cfg.region.is_set:
        print(f"  [ok]   {cfg.region.as_tuple()} on monitor {cfg.region.monitor}")
    else:
        ok = False
        print("  [--]   not set - run: gametrans pick-region")

    print("\nHotkeys")
    from .hotkeys import HotkeyManager

    manager = HotkeyManager(cfg.hotkeys)
    if manager.available:
        print(f"  [ok]   {cfg.hotkeys.toggle_pause} pause · "
              f"{cfg.hotkeys.pick_region} pick region · {cfg.hotkeys.quit} quit")
    else:
        print(f"  [--]   {manager.reason} (optional)")

    print("\n" + ("All required components are ready." if ok else "Fix the [--] items above."))
    return 0 if ok else 1


def _cmd_translate(cfg: AppConfig, args) -> int:
    from .translator import Translator

    text = " ".join(args.text)
    try:
        translator = Translator(cfg.translate)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    translator.warmup()
    started = time.perf_counter()
    printed = {"n": 0}

    def on_chunk(accumulated: str, chunk: str) -> None:
        if printed["n"] == 0:
            print(f"[first token in {(time.perf_counter()-started)*1000:.0f} ms]")
        printed["n"] += 1
        print(chunk, end="", flush=True)

    outcome = translator.translate(text, on_chunk=on_chunk)
    print()
    if outcome.ok:
        source = "cache" if outcome.cached else outcome.provider
        print(f"\n{outcome.text}")
        print(f"\n[{source}] first token {outcome.first_token_ms:.0f} ms, "
              f"total {outcome.total_ms:.0f} ms")
        if outcome.attempts and len(outcome.attempts) > 1:
            print(f"[failed over through: {' -> '.join(outcome.attempts)}]")
    else:
        print(f"error: {outcome.error}", file=sys.stderr)
    translator.close()
    return 0 if outcome.ok else 1


def _cmd_bench(cfg: AppConfig, args) -> int:
    if not cfg.region.is_set:
        print("error: no region set - run `gametrans pick-region` first", file=sys.stderr)
        return 2

    from .capture import create_capture
    from .changedet import ChangeDetector
    from .metrics import Metrics
    from .ocr import OcrEngineFacade

    metrics = Metrics()
    capture = create_capture(cfg.capture, cfg.region)
    ocr = OcrEngineFacade(cfg.ocr)
    detector = ChangeDetector(
        hash_threshold=cfg.capture.hash_threshold,
        pixel_threshold=cfg.capture.pixel_threshold,
    )

    print(f"benchmarking {args.iterations} frames on region {cfg.region.as_tuple()}\n")
    last_text = ""
    for _ in range(args.iterations):
        started = time.perf_counter()
        frame = capture.grab(cfg.region)
        metrics.record("capture", (time.perf_counter() - started) * 1000.0)
        if frame is None:
            continue

        started = time.perf_counter()
        change = detector.check(frame)
        metrics.record("change_gate", (time.perf_counter() - started) * 1000.0)
        if not change.changed:
            metrics.increment("frames_skipped")
            time.sleep(1.0 / max(cfg.capture.target_fps, 1))
            continue

        result = ocr.read(frame)
        metrics.record("ocr", result.elapsed_ms)
        if result.text:
            last_text = result.text
        time.sleep(1.0 / max(cfg.capture.target_fps, 1))

    capture.close()
    ocr.close()
    print(metrics.report())
    print(f"\nlast OCR read: {last_text[:200]!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
