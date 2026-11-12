"""Plain-text handover table -> facts.

Layout: a ``Snapshot: <timestamp>`` header, prose, then one row per check:
    - <time> | <system> | <resource_id> | <record_type> | <state> | <subject>
The subject is an email (any letter case), a user id, a hash, empty, or "no direct identifier".
"""

from __future__ import annotations

import re
from pathlib import Path

from loaders import EvidenceFile

from .facts import ParseResult, Provenance, lookup_fact, read_text_file, valid_timestamp

PARSER = "txt"
_ROW = re.compile(r"^- (\S+) \| (\S+) \| (\S+) \| (\S+) \| (\S+) \|[ \t]*(.*?)[ \t]*$")
_SNAPSHOT = re.compile(r"^Snapshot:\s*(\S+)\s*$")


def parse(path: Path, ev: EvidenceFile) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    text = read_text_file(path, result)
    if text is None:
        return result
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if (m := _SNAPSHOT.match(line)):
            if m.group(1) != ev.captured_at:
                result.issue("WARN", "TIMESTAMP_MISMATCH", f"line {number}", f"snapshot {m.group(1)} but manifest says {ev.captured_at}")
            continue
        if not line.startswith("- "):
            result.count("prose_lines")
            continue
        row = _ROW.match(line)
        if row is None:
            result.count("rows_skipped")
            result.issue("WARN", "UNPARSEABLE_LINE", f"line {number}", f"table row not understood: {line[:60]!r}")
            continue
        when, system, resource_id, record_type, state, subject = row.groups()
        if not valid_timestamp(when):
            result.count("rows_skipped")
            result.issue("ERROR", "BAD_TIMESTAMP", f"line {number}", f"bad row time {when!r}")
            continue
        if when > ev.captured_at:
            result.issue("WARN", "LINE_AFTER_CAPTURE", f"line {number}", f"row time {when} is after file capture {ev.captured_at}")
        fact = lookup_fact(
            evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=when, system=system,
            resource_id=resource_id, record_type=record_type, result=state, subject=subject or None,
            provenance=Provenance(ev.file_id, ev.file_path, f"line {number}", PARSER))
        if fact.state == "UNKNOWN":
            result.issue("WARN", "UNKNOWN_STATE", f"line {number}", f"unrecognised state {state!r}")
        result.facts.append(fact)
        result.count("facts")
    if not result.facts:
        result.issue("ERROR", "NO_ROWS", "file", "no table rows could be read")
    return result
