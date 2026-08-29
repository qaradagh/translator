"""OCR backends.

Three engines, auto-selected fastest-first:

* `windows`   - Windows.Media.Ocr. Built into Win10/11, no model download,
                ~10-20 ms on a subtitle band. The default on Windows.
* `rapidocr`  - ONNX PP-OCR. Cross-platform, ~30-80 ms, better on stylised or
                low-contrast game fonts.
* `tesseract` - Widely available fallback, ~50-150 ms.

All three return the same `OcrResult`, so the pipeline never knows which is live.
"""

from __future__ import annotations

import logging
import math
import platform
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from .config import OcrConfig
from .textnorm import join_lines

log = logging.getLogger(__name__)


@dataclass
class OcrLine:
    text: str
    confidence: float = 1.0
    # (left, top, width, height) within the captured region, when the engine
    # reports it. Used to sort lines top-to-bottom.
    box: Optional[tuple] = None


@dataclass
class OcrResult:
    lines: List[OcrLine] = field(default_factory=list)
    elapsed_ms: float = 0.0
    backend: str = ""

    @property
    def text(self) -> str:
        return join_lines(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


# -- preprocessing ----------------------------------------------------------


def preprocess(frame: np.ndarray, cfg: OcrConfig) -> np.ndarray:
    """Prepare a captured BGR frame for OCR.

    Subtitle text is usually small and light-on-dark, and upscaling makes small
    glyphs far easier to recognise. It is not free though - both the resize and
    the recognition scale with pixel count - so the factor is capped so that a
    large region on a high-resolution display does not spend 100 ms enlarging
    text that was already perfectly legible.
    """
    img = frame
    factor = effective_upscale(frame.shape[1], frame.shape[0], cfg)
    if factor != 1.0:
        img = _resize_nearest(img, factor)
    if cfg.binarize:
        img = _binarize(img, cfg.binarize_threshold)
    return img


def effective_upscale(width: int, height: int, cfg: OcrConfig) -> float:
    """The configured upscale, reduced to stay under the pixel ceiling."""
    requested = cfg.upscale or 1.0
    if requested <= 1.0:
        return 1.0

    if not cfg.max_pixels:
        return requested  # ceiling disabled

    pixels = max(width * height, 1)
    if pixels >= cfg.max_pixels:
        # Already at or over the ceiling - enlarging would only cost time.
        return 1.0

    allowed = math.sqrt(cfg.max_pixels / pixels)
    return max(1.0, min(requested, allowed))


def _resize_nearest(img: np.ndarray, factor: float) -> np.ndarray:
    """Nearest-neighbour upscale via index arrays - no cv2 dependency.

    Nearest rather than bilinear is deliberate: it keeps glyph edges crisp, which
    OCR engines handle better than a smoothed upscale.
    """
    h, w = img.shape[:2]
    new_h, new_w = max(int(h * factor), 1), max(int(w * factor), 1)
    rows = (np.arange(new_h) / factor).astype(np.int32).clip(0, h - 1)
    cols = (np.arange(new_w) / factor).astype(np.int32).clip(0, w - 1)
    return img[rows][:, cols]


def _binarize(img: np.ndarray, threshold: int) -> np.ndarray:
    from .changedet import to_grayscale

    gray = to_grayscale(img)
    binary = np.where(gray > threshold, 255, 0).astype(np.uint8)
    return np.repeat(binary[:, :, None], 3, axis=2)


def _sort_lines(lines: Sequence[OcrLine]) -> List[OcrLine]:
    """Order lines top-to-bottom, then left-to-right, when boxes are available."""
    def key(line: OcrLine):
        if not line.box:
            return (0, 0)
        return (round(line.box[1] / 8.0), line.box[0])

    if any(line.box for line in lines):
        return sorted(lines, key=key)
    return list(lines)


# -- backends ---------------------------------------------------------------


class OcrBackend(ABC):
    name = "base"

    @abstractmethod
    def recognize(self, image: np.ndarray) -> List[OcrLine]:
        """Run OCR on a preprocessed BGR image."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class WindowsOcrBackend(OcrBackend):
    """Windows.Media.Ocr via the winsdk projection.

    The engine object and its event loop are created once and reused; per-call
    cost is then just the bitmap copy plus recognition.
    """

    name = "windows"

    def __init__(self, languages: Sequence[str]) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("Windows OCR is only available on Windows")

        import asyncio

        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        self._asyncio = asyncio
        self._loop = asyncio.new_event_loop()

        engine = None
        for tag in languages:
            try:
                candidate = OcrEngine.try_create_from_language(Language(tag))
            except Exception:
                candidate = None
            if candidate is not None:
                engine = candidate
                break
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError(
                "No Windows OCR language pack installed. Add one under "
                "Settings > Time & Language > Language & region."
            )
        self._engine = engine

        from winsdk.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winsdk.windows.security.cryptography import CryptographicBuffer

        self._SoftwareBitmap = SoftwareBitmap
        self._BitmapPixelFormat = BitmapPixelFormat
        self._BitmapAlphaMode = BitmapAlphaMode
        self._CryptographicBuffer = CryptographicBuffer

    def _to_bitmap(self, image: np.ndarray):
        h, w = image.shape[:2]
        # Windows wants BGRA8; our frames are BGR.
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, :3] = image
        bgra[:, :, 3] = 255
        buffer = self._CryptographicBuffer.create_from_byte_array(bgra.tobytes())
        return self._SoftwareBitmap.create_copy_from_buffer(
            buffer,
            self._BitmapPixelFormat.BGRA8,
            w,
            h,
            self._BitmapAlphaMode.PREMULTIPLIED,
        )

    def recognize(self, image: np.ndarray) -> List[OcrLine]:
        bitmap = self._to_bitmap(image)
        result = self._loop.run_until_complete(self._engine.recognize_async(bitmap))

        lines: List[OcrLine] = []
        for line in result.lines:
            words = list(line.words)
            box = None
            if words:
                left = min(w.bounding_rect.x for w in words)
                top = min(w.bounding_rect.y for w in words)
                right = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
                bottom = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
                box = (int(left), int(top), int(right - left), int(bottom - top))
            # Windows OCR exposes no per-line confidence; treat a successful read
            # as confident and let the change/stability gates filter noise.
            lines.append(OcrLine(text=line.text, confidence=1.0, box=box))
        return lines

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass


class RapidOcrBackend(OcrBackend):
    """PP-OCR through onnxruntime. Best accuracy on stylised game fonts."""

    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def recognize(self, image: np.ndarray) -> List[OcrLine]:
        # RapidOCR expects RGB.
        result, _elapsed = self._engine(image[:, :, ::-1])
        if not result:
            return []

        lines: List[OcrLine] = []
        for entry in result:
            box_points, text, score = entry[0], entry[1], entry[2]
            xs = [float(p[0]) for p in box_points]
            ys = [float(p[1]) for p in box_points]
            box = (
                int(min(xs)),
                int(min(ys)),
                int(max(xs) - min(xs)),
                int(max(ys) - min(ys)),
            )
            lines.append(OcrLine(text=text, confidence=float(score), box=box))
        return lines


class TesseractOcrBackend(OcrBackend):
    """Fallback engine. Slowest of the three but available almost everywhere."""

    name = "tesseract"

    def __init__(self, languages: Sequence[str], single_block: bool, cmd: Optional[str]) -> None:
        import pytesseract
        from PIL import Image

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd

        self._pytesseract = pytesseract
        self._Image = Image
        self._lang = "+".join(_to_tesseract_langs(languages))
        # PSM 6 = one uniform block of text, which is what a subtitle band is.
        self._config = "--psm 6" if single_block else "--psm 11"

    def recognize(self, image: np.ndarray) -> List[OcrLine]:
        pil = self._Image.fromarray(image[:, :, ::-1])
        data = self._pytesseract.image_to_data(
            pil,
            lang=self._lang,
            config=self._config,
            output_type=self._pytesseract.Output.DICT,
        )

        # Group words into their source lines using tesseract's own indices.
        grouped: dict = {}
        for i, word in enumerate(data["text"]):
            if not word or not word.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            conf = float(data["conf"][i])
            if conf < 0:
                conf = 0.0
            entry = grouped.setdefault(key, {"words": [], "conf": [], "box": None})
            entry["words"].append(word)
            entry["conf"].append(conf / 100.0)
            left, top = data["left"][i], data["top"][i]
            width, height = data["width"][i], data["height"][i]
            if entry["box"] is None:
                entry["box"] = [left, top, left + width, top + height]
            else:
                box = entry["box"]
                box[0] = min(box[0], left)
                box[1] = min(box[1], top)
                box[2] = max(box[2], left + width)
                box[3] = max(box[3], top + height)

        lines: List[OcrLine] = []
        for entry in grouped.values():
            confidences = entry["conf"]
            box = entry["box"]
            lines.append(
                OcrLine(
                    text=" ".join(entry["words"]),
                    confidence=sum(confidences) / len(confidences) if confidences else 0.0,
                    box=(box[0], box[1], box[2] - box[0], box[3] - box[1]) if box else None,
                )
            )
        return lines


_TESSERACT_LANG_MAP = {
    "en": "eng",
    "ja": "jpn",
    "ko": "kor",
    "zh": "chi_sim",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "ru": "rus",
    "pt": "por",
    "tr": "tur",
    "ar": "ara",
    "fa": "fas",
}


def _to_tesseract_langs(languages: Sequence[str]) -> List[str]:
    out = []
    for lang in languages:
        base = lang.split("-")[0].lower()
        out.append(_TESSERACT_LANG_MAP.get(base, base))
    return out or ["eng"]


# -- engine facade ----------------------------------------------------------


class OcrEngineFacade:
    """Owns a backend plus preprocessing, and reports timing."""

    def __init__(self, cfg: OcrConfig) -> None:
        self.cfg = cfg
        self.backend = create_backend(cfg)

    def read(self, frame: np.ndarray) -> OcrResult:
        started = time.perf_counter()
        image = preprocess(frame, self.cfg)
        try:
            lines = self.backend.recognize(image)
        except Exception as exc:
            log.warning("OCR failed on %s backend: %s", self.backend.name, exc)
            lines = []

        if self.cfg.min_confidence is not None:
            lines = [ln for ln in lines if ln.confidence >= self.cfg.min_confidence]

        lines = _sort_lines(lines)
        elapsed = (time.perf_counter() - started) * 1000.0
        return OcrResult(lines=lines, elapsed_ms=elapsed, backend=self.backend.name)

    def close(self) -> None:
        self.backend.close()


def create_backend(cfg: OcrConfig) -> OcrBackend:
    """Instantiate the requested backend, or auto-detect the fastest available."""
    requested = (cfg.backend or "auto").lower()

    builders = {
        "windows": lambda: WindowsOcrBackend(cfg.languages),
        "rapidocr": lambda: RapidOcrBackend(),
        "tesseract": lambda: TesseractOcrBackend(
            cfg.languages, cfg.single_block, cfg.tesseract_cmd
        ),
    }

    if requested in builders:
        return builders[requested]()

    # auto: fastest first, falling through on any import or init failure.
    order = ["windows", "rapidocr", "tesseract"] if platform.system() == "Windows" else [
        "rapidocr",
        "tesseract",
    ]
    errors = []
    for name in order:
        try:
            backend = builders[name]()
            log.info("OCR backend: %s", name)
            return backend
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    raise RuntimeError(
        "No OCR backend available. Install one of:\n"
        "  pip install gametrans[windows]    (Windows, fastest)\n"
        "  pip install gametrans[rapidocr]   (cross-platform, most accurate)\n"
        "  pip install gametrans[tesseract]  (fallback)\n"
        "Tried -> " + "; ".join(errors)
    )
