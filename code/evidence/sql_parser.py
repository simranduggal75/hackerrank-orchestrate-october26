"""SQLite snapshot exports -> facts.

Four table families exist in the evidence:
  * generic replica tables: (resource_id, origin_system, collection_name, record_json, updated_at)
    in customer_data_primary / *_read_replica / *_replica_<region> / *_reporting_replica /
    *_failover_replica, so replication drift shows up as the same resource in some stores only;
  * typed inventory tables plus a ``snapshot_scope`` table giving each system's capture time;
  * account profile tables customer_profiles_primary / customer_profiles_failover_replica;
  * search_documents_primary.

A dump that does not execute cleanly is recovered statement by statement; every failed
statement is reported, and what could be read is still used.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from loaders import EvidenceFile

from .facts import (
    COLLECTION_TYPES, TYPED_TABLES, Coverage, ParseResult, Provenance, read_text_file, record_fact,
)

PARSER = "sql"
_HEADER_RE = re.compile(r"^--\s*([A-Za-z ]+?):\s*(.+?)\s*$")


def _header(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        if not line.startswith("--"):
            if line.strip():
                break
            continue
        match = _HEADER_RE.match(line)
        if match:
            found[match.group(1).strip().lower()] = match.group(2)
    return found


def _load(text: str, result: ParseResult) -> sqlite3.Connection:
    """Execute the dump; on failure rebuild the database statement by statement."""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(text)
        return db
    except sqlite3.Error as exc:
        db.close()
        result.issue("WARN", "SQL_EXEC_ERROR", "script", f"dump did not execute cleanly ({exc}); recovering statement by statement")
    db = sqlite3.connect(":memory:")
    buffer, start_line = "", 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not buffer and (not line.strip() or line.startswith("--")):
            continue
        if not buffer:
            start_line = number
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement.upper().startswith(("BEGIN", "COMMIT", "END", "PRAGMA")):
                continue
            try:
                db.execute(statement)
                result.count("statements_ok")
            except sqlite3.Error as exc:
                result.count("statements_failed")
                result.issue("ERROR", "SQL_STATEMENT_FAILED", f"line {start_line}", f"{exc}: {statement[:80]}")
    if buffer.strip():
        result.count("statements_failed")
        result.issue("ERROR", "SQL_TRUNCATED", f"line {start_line}", "file ends inside a statement; trailing text ignored")
    return db


def _tables(db: sqlite3.Connection) -> dict[str, list[str]]:
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: [c[1] for c in db.execute(f'PRAGMA table_info("{n}")')] for n in names}


def _rows(db: sqlite3.Connection, table: str, columns: list[str]):
    cols = ", ".join(f'"{c}"' for c in columns)
    for row in db.execute(f'SELECT rowid, {cols} FROM "{table}" ORDER BY rowid'):
        yield row[0], dict(zip(columns, row[1:]))


def parse(path: Path, ev: EvidenceFile) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    text = read_text_file(path, result)
    if text is None:
        return result
    header = _header(text)
    if "captured at" in header and header["captured at"] != ev.captured_at:
        result.issue("WARN", "TIMESTAMP_MISMATCH", "header",
                     f"header says captured {header['captured at']} but manifest says {ev.captured_at}")
    topology = header.get("database topology")

    db = _load(text, result)
    tables = _tables(db)
    if not tables:
        result.issue("ERROR", "NO_TABLES", "file", "no tables could be loaded from this SQL file")
        return result

    scope: dict[str, str] = {}
    if "snapshot_scope" in tables:
        for rowid, row in _rows(db, "snapshot_scope", tables["snapshot_scope"]):
            scope[row["origin_system"]] = row["captured_at"]
            result.coverage.append(Coverage(
                ev.file_id, ev.captured_at, row["captured_at"], row["origin_system"], None,
                "snapshot_scope", 1, Provenance(ev.file_id, ev.file_path, f"table snapshot_scope rowid {rowid}", PARSER)))

    for table, columns in tables.items():
        if table == "snapshot_scope":
            continue
        if "record_json" in columns:
            _generic_table(db, table, columns, ev, topology, result)
        elif table in TYPED_TABLES:
            _typed_table(db, table, columns, ev, scope, result)
        elif re.fullmatch(r"customer_profiles_(primary|failover_replica)", table):
            _account_table(db, table, columns, ev, result)
        elif table == "search_documents_primary":
            _search_table(db, table, columns, ev, result)
        else:
            count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            result.count("rows_unmapped", count)
            result.issue("WARN", "UNKNOWN_TABLE", f"table {table}", f"unrecognised table with {count} row(s) was not extracted")
    db.close()
    return result


def _emit(result, ev, *, table, rowid, system, record_type, raw, observed, resource_id=None,
          updated=None, extra_attrs=None, stamp_name="updated_at"):
    """Build one fact; a record stamped after its snapshot is flagged and kept out of earlier cases."""
    observed_at = observed or ev.captured_at
    if updated and updated > observed_at:
        result.issue("WARN", "RECORD_AFTER_CAPTURE", f"table {table} rowid {rowid}",
                     f"record {stamp_name} {updated} is after the snapshot time {observed_at}")
        observed_at = updated
    attrs = dict(extra_attrs or {})
    if updated:
        attrs[stamp_name] = updated
    result.facts.append(record_fact(
        evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=observed_at, system=system,
        record_type=record_type, raw=raw, store=table, resource_id=resource_id,
        provenance=Provenance(ev.file_id, ev.file_path, f"table {table} rowid {rowid}", PARSER),
        extra_attributes=attrs))
    result.count("facts")


def _generic_table(db, table, columns, ev, topology, result):
    role = table.removeprefix("customer_data_")
    combos: dict[tuple[str, str | None], int] = {}
    total = 0
    for rowid, row in _rows(db, table, columns):
        total += 1
        key = (row["origin_system"], row["collection_name"])
        record_type = COLLECTION_TYPES.get(key)
        locator = f"table {table} rowid {rowid}"
        try:
            raw = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            result.count("rows_skipped")
            result.issue("ERROR", "BAD_RECORD_JSON", locator, f"record_json is not valid JSON: {exc}")
            continue
        if not isinstance(raw, dict):
            result.count("rows_skipped")
            result.issue("ERROR", "BAD_RECORD_JSON", locator, "record_json is not an object")
            continue
        if record_type is None:
            result.count("rows_unmapped")
            result.issue("WARN", "UNKNOWN_COLLECTION", locator, f"unmapped collection {key}; row not extracted")
            continue
        if raw.get("resource_id") != row["resource_id"]:
            result.issue("WARN", "RESOURCE_ID_MISMATCH", locator,
                         f"row resource_id {row['resource_id']!r} differs from record_json {raw.get('resource_id')!r}")
        combos[(key[0], record_type)] = combos.get((key[0], record_type), 0) + 1
        _emit(result, ev, table=table, rowid=rowid, system=key[0], record_type=record_type, raw=raw,
              observed=ev.captured_at, resource_id=row["resource_id"], updated=row["updated_at"],
              extra_attrs={"replica_role": role, "topology": topology})
    prov = Provenance(ev.file_id, ev.file_path, f"table {table}", PARSER)
    result.coverage.append(Coverage(ev.file_id, ev.captured_at, ev.captured_at, "*", None, table, total, prov))
    for (system, record_type), n in sorted(combos.items()):
        result.coverage.append(Coverage(ev.file_id, ev.captured_at, ev.captured_at, system, record_type, table, n, prov))


def _typed_table(db, table, columns, ev, scope, result):
    system, record_type = TYPED_TABLES[table]
    stamp = next((c for c in columns if c in ("updated_at", "indexed_at")), None)
    observed = scope.get(system)
    populated = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] > 0
    if observed is None and populated:
        result.issue("WARN", "NO_SNAPSHOT_SCOPE", f"table {table}", f"no snapshot_scope row for {system}; using file capture time")
    count = 0
    for rowid, row in _rows(db, table, columns):
        count += 1
        raw = {k: v for k, v in row.items() if k != stamp}
        _emit(result, ev, table=table, rowid=rowid, system=system, record_type=record_type, raw=raw,
              observed=observed, resource_id=row["resource_id"], updated=row.get(stamp) if stamp else None,
              stamp_name=stamp or "updated_at")
    result.coverage.append(Coverage(ev.file_id, ev.captured_at, observed or ev.captured_at, system, record_type,
                                    table, count, Provenance(ev.file_id, ev.file_path, f"table {table}", PARSER)))


def _account_table(db, table, columns, ev, result):
    count = 0
    for rowid, row in _rows(db, table, columns):
        count += 1
        raw = {k: v for k, v in row.items() if k != "updated_at"}
        _emit(result, ev, table=table, rowid=rowid, system="account_service", record_type="USER_PROFILE",
              raw=raw, observed=ev.captured_at, resource_id=row["resource_id"], updated=row["updated_at"],
              extra_attrs={"replica_role": table.removeprefix("customer_profiles_")})
    result.coverage.append(Coverage(ev.file_id, ev.captured_at, ev.captured_at, "account_service", "USER_PROFILE",
                                    table, count, Provenance(ev.file_id, ev.file_path, f"table {table}", PARSER)))


def _search_table(db, table, columns, ev, result):
    count = 0
    for rowid, row in _rows(db, table, columns):
        count += 1
        raw = {k: v for k, v in row.items() if k not in ("indexed_at", "source_resource_id")}
        _emit(result, ev, table=table, rowid=rowid, system="search_service", record_type="SEARCH_INDEX_ENTRY",
              raw=raw, observed=ev.captured_at, resource_id=row["resource_id"], updated=row["indexed_at"],
              extra_attrs={"source_resource_id": row["source_resource_id"]}, stamp_name="indexed_at")
    result.coverage.append(Coverage(ev.file_id, ev.captured_at, ev.captured_at, "search_service", "SEARCH_INDEX_ENTRY",
                                    table, count, Provenance(ev.file_id, ev.file_path, f"table {table}", PARSER)))
