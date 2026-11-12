"""Build and inspect ``code.zip``: the runnable solution, README, requirements and the two evaluation reports.

Excluded on purpose: the dataset, ``output.csv`` and ``log.txt`` (submitted separately), caches (``__pycache__``,
OCR caches), virtual environments, nested archives, environment files and anything that looks like a secret.
The archive is reproducible: members are sorted and carry a fixed timestamp.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = ROOT / "code.zip"

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "env", "node_modules", ".git", ".ocr_cache", "ocr_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar", ".gz", ".7z", ".rar", ".log", ".csv", ".sqlite", ".db"}
SKIP_NAMES = {".env", "log.txt", "output.csv"}
REQUIRED = ("README.md", "requirements.txt", "code/main.py", "code/README.md", "code/requirements.txt",
            "code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"""(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['"][^'"\s]{8,}['"]"""),
)
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def wanted(relative: Path) -> bool:
    parts = relative.parts
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return False
    name = relative.name
    return name not in SKIP_NAMES and not name.startswith(".env") and relative.suffix.lower() not in SKIP_SUFFIXES


def members(root: Path) -> list[tuple[str, Path]]:
    """(archive name, source file) pairs, sorted by archive name."""
    chosen: dict[str, Path] = {}
    for path in (root / "code").rglob("*"):
        if path.is_file() and wanted(path.relative_to(root)):
            chosen[path.relative_to(root).as_posix()] = path
    chosen["README.md"] = root / "code" / "README.md"
    chosen["requirements.txt"] = root / "code" / "requirements.txt"
    return sorted(chosen.items())


def secret_hits(root: Path, items: list[tuple[str, Path]]) -> list[str]:
    hits = []
    for name, path in items:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += [f"{name}: matches {pattern.pattern[:30]}" for pattern in SECRET_PATTERNS if pattern.search(text)]
    return hits


def build(root: Path = ROOT, archive: Path = DEFAULT_ARCHIVE) -> list[str]:
    """Write the archive; returns problems (and writes nothing) if a required file is missing or a secret is found."""
    items = members(root)
    names = {name for name, _ in items}
    problems = [f"missing required file {r}" for r in REQUIRED if r not in names] + secret_hits(root, items)
    if problems:
        return problems
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in items:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return inspect(archive)


def inspect(archive: Path) -> list[str]:
    """Problems with an existing archive; empty means it satisfies the packaging rules."""
    problems = []
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad:
            problems.append(f"corrupt member {bad}")
        names = zf.namelist()
        for name in names:
            path = Path(name)
            if name.startswith(("/", "\\")) or ".." in path.parts:
                problems.append(f"unsafe path {name}")
            if path.parts[0] not in ("code", "README.md", "requirements.txt"):
                problems.append(f"unexpected top-level entry {name}")
            if any(part in SKIP_DIRS for part in path.parts[:-1]) or path.parts[0] == "dataset":
                problems.append(f"excluded directory in archive: {name}")
            if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES or path.name.startswith(".env"):
                problems.append(f"excluded file in archive: {name}")
        problems += [f"missing required file {r}" for r in REQUIRED if r not in names]
        for name in names:
            data = zf.read(name)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            problems += [f"{name}: looks like a secret ({pattern.pattern[:30]})" for pattern in SECRET_PATTERNS if pattern.search(text)]
    return problems
