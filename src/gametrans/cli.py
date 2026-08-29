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
    sub.add_parser("models", help="list the models each configured provider offers")

    setkey = sub.add_parser("setkey", help="store an API key in the .env file")
    setkey.add_argument(
        "provider",
        nargs="?",
        choices=["gemini", "groq", "anthropic"],
        help="which key to set; omit to be asked",
    )

    translate = sub.add_parser("translate", help="translate one string (no screen capture)")
    translate.add_argument("text", nargs="+", help="text to translate")
    translate.add_argument(
        "--preview",
        action="store_true",
        help="also show the result in the real overlay window",
    )

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
        "models": lambda: _cmd_models(cfg),
        "setkey": lambda: _cmd_setkey(args),
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

    import gametrans

    ok = True
    needs_region = False
    print(f"gametrans {__version__} on {platform.system()} {platform.release()}")

    # More than one copy of the project on disk is common, and a virtualenv can
    # point at a different one than the folder you are standing in. Say which
    # code is actually loaded so a stale install is obvious.
    module_dir = os.path.dirname(os.path.abspath(gametrans.__file__))
    print(f"running from: {module_dir}")
    if os.path.commonpath([module_dir, os.getcwd()]) != os.getcwd():
        print("  note: that is outside this folder - this venv may point at "
              "another copy of the project")
    print()

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

    from .dotenv import env_file_path, source_of

    print("\nTranslation providers")
    env_file = env_file_path()
    if env_file.is_file():
        print(f"  keys file: {env_file}")
    found_provider = False
    for entry in cfg.translate.providers:
        status = "disabled" if not entry.enabled else ""
        key_state = "no key needed"
        if entry.api_key_env:
            origin = source_of(entry.api_key_env)
            key_state = f"key set (from {origin})" if origin else \
                f"MISSING ${entry.api_key_env}"
        mark = "ok" if (entry.enabled and "MISSING" not in key_state) else "--"
        if mark == "ok":
            found_provider = True
        print(f"  [{mark}]   {entry.name:<20} {entry.model:<28} {key_state} {status}")
    if not found_provider:
        ok = False
        print("  -> get a free key: https://aistudio.google.com/apikey "
              "or https://console.groq.com/keys")
        print(f"  -> then put it in {env_file_path()} as:")
        print("       GEMINI_API_KEY=your-key-here")

    print("\nRegion")
    if cfg.region.is_set:
        print(f"  [ok]   {cfg.region.as_tuple()} on monitor {cfg.region.monitor}")
    else:
        # Not a blocker: starting the app with no region opens the picker.
        needs_region = True
        print("  [..]   not set yet - you will be asked to draw it on first run")

    print("\nHotkeys")
    from .hotkeys import HotkeyManager

    manager = HotkeyManager(cfg.hotkeys)
    if manager.available:
        print(f"  [ok]   {cfg.hotkeys.toggle_pause} pause · "
              f"{cfg.hotkeys.pick_region} pick region · {cfg.hotkeys.quit} quit")
    else:
        print(f"  [--]   {manager.reason} (optional)")

    print()
    if not ok:
        print("Something required is missing - see the [--] lines above.")
        return 1

    if needs_region:
        print("Ready. Next: choose the area of the screen to watch")
        print("  menu option 3, or:  gametrans pick-region")
    else:
        print("Everything is ready. Start with menu option 4, or:  gametrans run")
    print()
    print("Optional extras, only if you want them:")
    print("  a second provider so translation continues past Gemini's")
    print("  per-minute limit:  gametrans setkey groq")
    return 0


# Google issues two key formats: older standard keys ("AIza...") and the newer
# auth keys ("AQ.Ab..."), which is what AI Studio hands out now. Accept both.
_KEY_ENV_VARS = {
    "gemini": (
        "GEMINI_API_KEY",
        "https://aistudio.google.com/apikey",
        ("AQ.", "AIza"),
    ),
    "groq": ("GROQ_API_KEY", "https://console.groq.com/keys", ("gsk_",)),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "https://console.anthropic.com/settings/keys",
        ("sk-ant-",),
    ),
}


def _cmd_setkey(args) -> int:
    """Store an API key in `.env`.

    The key is read from a prompt rather than a command-line argument so it does
    not end up in shell history.
    """
    import getpass

    from .dotenv import set_key

    provider = getattr(args, "provider", None)
    if not provider:
        print("Which key do you want to set?")
        for index, name in enumerate(_KEY_ENV_VARS, start=1):
            print(f"  {index}) {name}")
        choice = input("Number: ").strip()
        names = list(_KEY_ENV_VARS)
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            provider = names[int(choice) - 1]
        elif choice.lower() in _KEY_ENV_VARS:
            provider = choice.lower()
        else:
            print("error: unrecognised choice", file=sys.stderr)
            return 2

    env_var, signup_url, prefixes = _KEY_ENV_VARS[provider]
    shown = " or ".join(f"'{p}'" for p in prefixes)
    print(f"\nGet a key at: {signup_url}")
    print(f"It starts with {shown}. Nothing is echoed as you paste.\n")

    try:
        value = getpass.getpass(f"{env_var}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return 1

    if not value:
        print("error: no key entered", file=sys.stderr)
        return 1

    if not value.startswith(prefixes):
        print(
            f"warning: a {provider} key usually starts with {shown} - "
            "double-check you pasted the whole thing."
        )

    path = set_key(env_var, value, getattr(args, "config", None))
    print(f"\nSaved {env_var} to {path}")
    print("Verify it with:  gametrans translate \"Hello traveller\"")
    return 0


def _cmd_models(cfg: AppConfig) -> int:
    """Ask each configured provider what it currently offers.

    Providers retire model names on their own schedule, so this is the reliable
    way to find a working one rather than trusting a name in a config file.
    """
    import os

    from .providers.base import ProviderError, build_provider

    any_listed = False
    for entry in cfg.translate.providers:
        print(f"\n{entry.name}  ({entry.kind}, configured model: {entry.model})")

        if entry.api_key_env and not os.environ.get(entry.api_key_env):
            print(f"  no key - set ${entry.api_key_env} first")
            continue

        try:
            provider = build_provider(entry)
        except Exception as exc:
            print(f"  unavailable: {str(exc)[:120]}")
            continue

        try:
            models = provider.list_models()
        except NotImplementedError:
            print("  this provider does not expose a model list")
            continue
        except ProviderError as exc:
            print(f"  could not list models: {str(exc)[:120]}")
            continue
        finally:
            provider.close()

        any_listed = True
        if not models:
            print("  (none returned)")
            continue

        for name in models:
            mark = " <- configured" if name == entry.model else ""
            print(f"  {name}{mark}")

        if entry.model not in models:
            print(
                f"\n  WARNING: '{entry.model}' is not in this list. "
                f"Pick one above and set it as `model` for [{entry.name}] "
                "in config.toml."
            )

    if not any_listed:
        print("\nNo provider could be queried. Set an API key and try again.")
        return 1
    return 0


def _cmd_translate(cfg: AppConfig, args) -> int:
    from .translator import Translator

    text = " ".join(args.text)
    try:
        translator = Translator(cfg.translate)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    translator.warmup()
    streamed = {"text": "", "announced": False}
    started = time.perf_counter()

    def on_chunk(accumulated: str, chunk: str) -> None:
        if not streamed["announced"]:
            streamed["announced"] = True
            elapsed = (time.perf_counter() - started) * 1000.0
            print(f"[first token in {elapsed:.0f} ms]")
        streamed["text"] = accumulated
        print(chunk, end="", flush=True)

    outcome = translator.translate(text, on_chunk=on_chunk)
    print()

    if not outcome.ok:
        print(f"error: {outcome.error}", file=sys.stderr)
        translator.close()
        return 1

    # The streamed text is raw; the final text has been sanitised. Only reprint
    # when cleaning actually changed something.
    if outcome.text != streamed["text"]:
        print(f"\n{outcome.text}")

    source = "cache" if outcome.cached else outcome.provider
    print(
        f"\n[{source}] first token {outcome.first_token_ms:.0f} ms, "
        f"total {outcome.total_ms:.0f} ms"
    )
    if len(outcome.attempts) > 1:
        print(f"[failed over through: {' -> '.join(outcome.attempts)}]")

    _warn_about_console_rendering(outcome.text, preview=getattr(args, "preview", False))
    translator.close()

    if getattr(args, "preview", False):
        from .overlay import show_preview

        show_preview(outcome.text, text, cfg.overlay)

    return 0


def _warn_about_console_rendering(text: str, preview: bool = False) -> None:
    """Windows consoles print right-to-left text in logical order.

    The characters are correct but come out reversed and with the letters
    unjoined, which reads as a broken translation when it is only a broken
    terminal. Say so, rather than letting someone conclude the app is wrong.
    """
    import platform

    from .textnorm import contains_persian

    if preview or not contains_persian(text) or platform.system() != "Windows":
        return

    print(
        "\nNote: this console shows Persian reversed, with the letters "
        "unjoined - it\n"
        "      does not implement bidirectional text. The in-game overlay "
        "renders it\n"
        "      correctly. Add --preview to see how it will really look."
    )


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
