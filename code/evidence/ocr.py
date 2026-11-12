"""OCR backend: word boxes from several independent passes over one screenshot.

Only the Windows built-in engine is wired up (``Windows.Media.Ocr`` through PowerShell), because it
needs no extra install on Windows. On any other host, or when the engine/PowerShell is missing,
``OcrUnavailable`` is raised and the image parser reports it; nothing is guessed.

Each pass OCRs the image upscaled by a different factor. Errors differ between scales, so two
passes agreeing on a string is real (if imperfect) evidence the text was read correctly.
All boxes are in ORIGINAL image pixels.

Cache (optional, off by default): set ``GHOST_OCR_CACHE`` to a directory and the raw engine output
(word text plus boxes, for every pass) is stored there, keyed by a hash of the image bytes, the helper
script and the scales. A cache hit returns exactly what the engine returned, so uncertainty is
preserved: voting, confirmation and provenance are recomputed from the raw words on every run, and a
cache never stores a parsed or "corrected" value. It lets a non-Windows host reuse a Windows reading.
The cache is generated data: keep it outside the repository (the default is no cache) and do not put it
in ``code.zip``; it can be deleted at any time and is rebuilt on demand.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT = Path(__file__).with_name("ocr_windows.ps1")
SCALES = (2, 3, 4)
CACHE_ENV = "GHOST_OCR_CACHE"  # optional directory for raw OCR output, keyed by image hash


class OcrUnavailable(RuntimeError):
    """No usable OCR engine on this machine."""


class OcrFailed(RuntimeError):
    """The engine ran but did not return usable output."""


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True, slots=True)
class OcrPass:
    scale: int
    width: int
    height: int
    words: tuple[OcrWord, ...]


def available() -> bool:
    """True when the engine can run here (Windows with PowerShell). A cache hit needs neither."""
    return os.name == "nt" and _powershell() is not None and SCRIPT.is_file()


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("powershell.exe")


def _cache_file(path: Path, scales: tuple[int, ...]) -> Path | None:
    directory = os.environ.get(CACHE_ENV)
    if not directory:
        return None
    digest = hashlib.sha256(path.read_bytes() + SCRIPT.read_bytes() + bytes(scales)).hexdigest()[:32]
    return Path(directory) / f"{digest}.json"


def run_ocr(path: Path, scales: tuple[int, ...] = SCALES) -> list[OcrPass]:
    """OCR with an optional on-disk cache of the raw engine output (set GHOST_OCR_CACHE).

    The cache holds exactly what the engine returned for that image, so a cache hit is the same
    machine reading, never a hand transcription; it also lets a non-Windows host reuse it.
    """
    cached = _cache_file(path, scales) if path.is_file() else None
    if cached is not None and cached.is_file():
        try:
            return passes_from_json(json.loads(cached.read_text(encoding="utf-8")))
        except (ValueError, OcrFailed):
            pass  # corrupt cache entry: fall through and re-run the engine
    passes = run_windows_ocr(path, scales)
    if cached is not None:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({
                "width": passes[0].width, "height": passes[0].height,
                "passes": [{"scale": p.scale, "words": [[w.text, w.x0, w.y0, w.x1 - w.x0, w.y1 - w.y0] for w in p.words]}
                           for p in passes]}), encoding="utf-8")
        except (OSError, IndexError):
            pass
    return passes


def run_windows_ocr(path: Path, scales: tuple[int, ...] = SCALES, timeout: int = 240) -> list[OcrPass]:
    """OCR ``path`` once per scale with the Windows engine."""
    if os.name != "nt":
        raise OcrUnavailable("the Windows OCR engine is only available on Windows")
    shell = _powershell()
    if shell is None:
        raise OcrUnavailable("powershell.exe was not found")
    if not SCRIPT.is_file():
        raise OcrFailed(f"missing helper script {SCRIPT.name}")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.json"
        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                   "-Path", str(path), "-Out", str(out), "-Scales", ",".join(str(s) for s in scales)]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed(f"OCR timed out after {timeout}s") from exc
        except OSError as exc:
            raise OcrUnavailable(f"cannot start PowerShell: {exc}") from exc
        if proc.returncode != 0 or not out.is_file():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            message = detail[0] if detail else f"exit code {proc.returncode}"
            if "no OCR engine" in (proc.stderr or ""):
                raise OcrUnavailable(message)
            raise OcrFailed(message[:200])
        try:
            data = json.loads(out.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise OcrFailed(f"unreadable OCR output: {exc}") from exc
    return passes_from_json(data)


def passes_from_json(data: dict) -> list[OcrPass]:
    try:
        width, height = int(data["width"]), int(data["height"])
        return [
            OcrPass(int(p["scale"]), width, height, tuple(
                OcrWord(str(w[0]), float(w[1]), float(w[2]), float(w[1]) + float(w[3]), float(w[2]) + float(w[4]))
                for w in (p["words"] or [])))
            for p in data["passes"]
        ]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise OcrFailed(f"unexpected OCR output shape: {exc}") from exc
