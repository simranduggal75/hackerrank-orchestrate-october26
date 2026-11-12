"""Shared fixtures for evidence tests."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from loaders import DATASET_DIR, EvidenceFile

HAS_DATASET = (DATASET_DIR / "evidence_files.csv").exists()

# Real OCR takes ~5 s per screenshot. Cache the raw engine output between test runs (same image hash ->
# same machine reading); delete this folder to force a fresh OCR run.
os.environ.setdefault("GHOST_OCR_CACHE", str(Path(tempfile.gettempdir()) / "ghost_ocr_test_cache"))
CAPTURED = "2026-09-01T12:00:00Z"


class TmpCase(unittest.TestCase):
    """A temp directory plus helpers to write evidence files and describe them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, content, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode(encoding))
        return path

    def write_json(self, name: str, doc) -> Path:
        return self.write(name, json.dumps(doc))

    def ev(self, name: str, captured: str = CAPTURED, file_id: str = "evidence_01") -> EvidenceFile:
        return EvidenceFile(file_id, f"evidence/{name}", "test_origin", captured)

    def codes(self, result) -> list[str]:
        return [i.code for i in result.issues]


def sql_insert(table: str, columns: int, *values) -> str:
    """INSERT statement with SQL-escaped literals; dict/list values are stored as JSON text."""
    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"))
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    return f'INSERT INTO "{table}" VALUES({",".join(lit(v) for v in values)});'


GENERIC_DDL = (
    "CREATE TABLE {t} (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, "
    "collection_name TEXT NOT NULL, record_json TEXT NOT NULL CHECK (json_valid(record_json)), "
    "updated_at TEXT NOT NULL);"
)

NEEDS_DATASET = unittest.skipUnless(HAS_DATASET, "dataset/ not present")


def digest(value: str) -> str:
    """SHA-256 of the exact text. Tests that check real evidence compare digests so no personal data is shipped."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
