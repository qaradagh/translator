"""Configuration: dataclass schema, TOML file loading, env-var overrides.

Every knob that affects latency lives here so it can be tuned without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10 only
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_NAMES = ("config.toml", "gametrans.toml")


@dataclass
class RegionConfig:
    """Screen rectangle to watch, in physical pixels."""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    monitor: int = 1  # 1-based, matches mss numbering

    def as_tuple(self) -> tuple:
        return (self.left, self.top, self.width, self.height)

    @property
    def is_set(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class CaptureConfig:
    backend: str = "auto"  # auto | mss | dxcam
    target_fps: int = 12  # capture polling rate; higher = faster pickup, more CPU
    # Change gate. A frame whose perceptual hash is within `hash_threshold` bits and
    # whose mean pixel delta is under `pixel_threshold` is treated as "same frame"
    # and never reaches OCR. This is the single biggest latency/quota saver.
    hash_threshold: int = 4
    pixel_threshold: float = 2.5
    # Downscale used for change detection only (not for OCR).
    hash_width: int = 64
    hash_height: int = 16
    # Compare a text signature rather than the raw picture. Without this, any
    # region containing moving scenery looks different every frame and the gate
    # saves nothing - which is most games, since subtitles are usually drawn
    # straight over the scene rather than onto an opaque bar.
    text_mask: bool = True
    mask_pixel_threshold: float = 0.03
    mask_blur: int = 9
    # A pixel counts as text when it differs from its neighbourhood by more than
    # mask_edge AND is brighter than mask_bright. Lower mask_bright for dim or
    # coloured subtitles; raise it if bright scenery is tripping the gate.
    mask_edge: float = 26.0
    mask_bright: float = 170.0


@dataclass
class OcrConfig:
    backend: str = "auto"  # auto | windows | rapidocr | tesseract
    languages: List[str] = field(default_factory=lambda: ["en"])
    # Upscale small subtitle text before OCR; 2.0 helps a lot on 1080p subtitles.
    upscale: float = 2.0
    # Drop OCR lines whose engine confidence is below this (0..1). None = keep all.
    min_confidence: float = 0.45
    # Treat the region as a single text block rather than scattered words.
    single_block: bool = True
    # Optional binarisation for high-contrast subtitle text.
    binarize: bool = False
    binarize_threshold: int = 160
    # Tesseract only: path to the binary if it is not on PATH.
    tesseract_cmd: Optional[str] = None


@dataclass
class StabilityConfig:
    """Controls how long we wait for OCR output to settle before translating."""

    # Number of consecutive identical OCR reads required before translating.
    # 1 = translate immediately (lowest latency, risks catching a half-drawn line).
    frames_required: int = 2
    # Even if unstable, force a translation after this many milliseconds.
    max_wait_ms: int = 220
    # Similarity (0..1) above which two OCR reads count as "the same line".
    similarity_threshold: float = 0.93
    # Ignore reads shorter than this - usually OCR noise.
    min_chars: int = 2


@dataclass
class ProviderConfig:
    """One entry in the translation provider chain."""

    name: str = ""
    kind: str = "openai"  # gemini | openai | anthropic
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    # Client-side throttle so we fail over *before* the server 429s us.
    rpm_limit: int = 0  # 0 = unlimited
    timeout_s: float = 12.0
    max_output_tokens: int = 512
    temperature: float = 0.2
    enabled: bool = True
    # Provider-specific request fields merged into the request body verbatim.
    # e.g. {"reasoning_effort": "none"} on Groq reasoning models.
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TranslateConfig:
    target_language: str = "Persian (Farsi)"
    source_language: str = "auto"
    # Provider chain: tried in order, with automatic failover on rate limits/errors.
    providers: List[ProviderConfig] = field(default_factory=list)
    # Stream partial output to the overlay as it arrives.
    stream: bool = True
    # How many translations may be in flight at once. >1 stops a slow line from
    # blocking the next one.
    concurrency: int = 3
    # Drop a translation whose source line is already off-screen.
    stale_after_ms: int = 6000
    # Extra instructions appended to the system prompt (game name, tone, etc).
    context_hint: str = ""
    # Glossary of terms that must always translate the same way: {source: target}
    glossary: Dict[str, str] = field(default_factory=dict)
    # Persistent cache location; empty string disables the on-disk cache.
    cache_path: str = "translation-cache.sqlite3"
    cache_memory_size: int = 2048


@dataclass
class OverlayConfig:
    # Where to draw. "region" anchors under the captured area; "custom" uses x/y.
    anchor: str = "region"
    x: int = 0
    y: int = 0
    width: int = 0  # 0 = match region width
    font_family: str = "Vazirmatn, Segoe UI, Tahoma, sans-serif"
    font_size: int = 26
    font_weight: int = 600
    text_color: str = "#FFFFFF"
    background_color: str = "#000000"
    background_opacity: float = 0.62
    padding: int = 14
    corner_radius: int = 10
    outline_color: str = "#000000"
    outline_width: float = 2.4
    line_spacing: float = 1.35
    max_lines: int = 4
    # Keep the previous line visible for this long after the source text disappears.
    linger_ms: int = 1400
    # Also show the original text under the translation.
    show_source: bool = False
    show_latency: bool = False


@dataclass
class HotkeyConfig:
    toggle_overlay: str = "ctrl+alt+h"
    pick_region: str = "ctrl+alt+r"
    toggle_pause: str = "ctrl+alt+p"
    quit: str = "ctrl+alt+q"
    enabled: bool = True


@dataclass
class AppConfig:
    region: RegionConfig = field(default_factory=RegionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    log_level: str = "INFO"
    metrics: bool = True

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return _asdict(self)


def default_provider_chain() -> List[ProviderConfig]:
    """The out-of-the-box chain: best free Persian quality first, highest free
    throughput second, local model last so the app still works offline."""
    return [
        ProviderConfig(
            name="gemini-flash-lite",
            kind="gemini",
            model="gemini-3.5-flash-lite",
            api_key_env="GEMINI_API_KEY",
            rpm_limit=15,
            timeout_s=10.0,
        ),
        ProviderConfig(
            name="groq-qwen",
            kind="openai",
            model="qwen/qwen3.6-27b",
            api_key_env="GROQ_API_KEY",
            base_url="https://api.groq.com/openai/v1",
            rpm_limit=30,
            timeout_s=10.0,
            # Qwen 3.6 has a thinking mode that is on by default. For a one-line
            # subtitle it adds latency and buys nothing, so turn it off.
            extra={"reasoning_effort": "none"},
        ),
        ProviderConfig(
            name="ollama-local",
            kind="openai",
            model="qwen3:8b",
            api_key_env="",
            base_url="http://127.0.0.1:11434/v1",
            rpm_limit=0,
            timeout_s=20.0,
            enabled=False,
        ),
    ]


# -- loading ----------------------------------------------------------------


def _asdict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _asdict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_asdict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort coercion of a TOML scalar into the dataclass field type."""
    if value is None:
        return None
    origin = getattr(target_type, "__origin__", None)
    if origin is list:
        return list(value)
    if origin is dict:
        return dict(value)
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _apply(section: Any, data: Dict[str, Any]) -> None:
    """Overlay a plain dict onto a dataclass instance, ignoring unknown keys."""
    type_map = {f.name: f.type for f in fields(section)}
    for key, value in data.items():
        if key not in type_map:
            continue
        current = getattr(section, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        elif isinstance(value, (dict, list)) and not is_dataclass(current):
            setattr(section, key, value)
        else:
            declared = type_map[key]
            # Field types come back as strings under `from __future__ import
            # annotations`; fall back to the runtime type of the default.
            runtime_type = type(current) if current is not None else str
            if isinstance(declared, type):
                runtime_type = declared
            setattr(section, key, _coerce(value, runtime_type))


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load config from `path`, or the first default name found in the cwd.

    A missing file is not an error - defaults plus environment variables are
    enough to run. A `.env` sitting beside the config is loaded first so API
    keys can live in one editable file rather than the shell environment.
    """
    from .dotenv import load_dotenv

    load_dotenv(path)

    cfg = AppConfig()

    resolved: Optional[Path] = None
    if path:
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Config file not found: {resolved}")
    else:
        for name in DEFAULT_CONFIG_NAMES:
            candidate = Path(name)
            if candidate.exists():
                resolved = candidate
                break

    raw: Dict[str, Any] = {}
    if resolved is not None:
        with open(resolved, "rb") as fh:
            raw = tomllib.load(fh)

    for section_name in ("region", "capture", "ocr", "stability", "overlay", "hotkeys"):
        if section_name in raw and isinstance(raw[section_name], dict):
            _apply(getattr(cfg, section_name), raw[section_name])

    if "log_level" in raw:
        cfg.log_level = str(raw["log_level"])
    if "metrics" in raw:
        cfg.metrics = bool(raw["metrics"])

    translate_raw = raw.get("translate", {}) or {}
    providers_raw = translate_raw.pop("providers", None)
    if isinstance(translate_raw, dict):
        _apply(cfg.translate, translate_raw)

    if providers_raw:
        chain: List[ProviderConfig] = []
        for entry in providers_raw:
            provider = ProviderConfig()
            _apply(provider, entry)
            chain.append(provider)
        cfg.translate.providers = chain
    else:
        cfg.translate.providers = default_provider_chain()

    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: AppConfig) -> None:
    """A handful of env vars for quick experiments without editing the file."""
    env = os.environ
    if env.get("GAMETRANS_REGION"):
        # Format: left,top,width,height
        try:
            left, top, width, height = (
                int(part) for part in env["GAMETRANS_REGION"].split(",")
            )
            cfg.region.left, cfg.region.top = left, top
            cfg.region.width, cfg.region.height = width, height
        except ValueError:
            pass
    if env.get("GAMETRANS_OCR_BACKEND"):
        cfg.ocr.backend = env["GAMETRANS_OCR_BACKEND"]
    if env.get("GAMETRANS_LOG_LEVEL"):
        cfg.log_level = env["GAMETRANS_LOG_LEVEL"]
    if env.get("GAMETRANS_TARGET_FPS"):
        try:
            cfg.capture.target_fps = int(env["GAMETRANS_TARGET_FPS"])
        except ValueError:
            pass


def save_region(path: str, region: RegionConfig) -> None:
    """Persist just the [region] block, preserving the rest of the file.

    Written as a small line-oriented edit rather than a full TOML re-serialise so
    user comments survive.
    """
    target = Path(path)
    block = (
        "[region]\n"
        f"left = {region.left}\n"
        f"top = {region.top}\n"
        f"width = {region.width}\n"
        f"height = {region.height}\n"
        f"monitor = {region.monitor}\n"
    )

    if not target.exists():
        target.write_text(block, encoding="utf-8")
        return

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    out: List[str] = []
    in_region = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_region:
                in_region = False
            if stripped == "[region]":
                in_region = True
                replaced = True
                out.append(block)
                continue
        if in_region:
            continue
        out.append(line)

    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append("\n" + block)

    target.write_text("".join(out), encoding="utf-8")
