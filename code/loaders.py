"""UTF-8 CSV loaders with schema validation for the participant-facing dataset.

Everything here is read-only: files are opened for reading only and never
rewritten. Timestamps stay as validated ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ``
strings; in that fixed format lexicographic order equals chronological order,
so callers can compare them directly.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"

REPORTED_STATUSES = frozenset({"COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"})
RULE_ACTIONS = frozenset({"DELETE", "REDACT", "ANONYMIZE", "RETAIN"})
AUDIT_FIELDS = frozenset({"email", "email_hash", "phone"})

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SchemaError(ValueError):
    """The file does not match the documented structure."""


def validate_timestamp(value: str, where: str) -> str:
    if not _TS_RE.match(value):
        raise SchemaError(f"{where}: bad timestamp {value!r} (expected YYYY-MM-DDTHH:MM:SSZ)")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SchemaError(f"{where}: impossible timestamp {value!r}") from exc
    return value


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AuditEvent:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True, slots=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: str


@dataclass(frozen=True, slots=True)
class FileSpec:
    filename: str
    record: type
    key: str
    timestamps: tuple[str, ...] = ()
    enums: tuple[tuple[str, frozenset[str]], ...] = ()
    required: tuple[str, ...] = ()  # columns that must be non-blank


SPECS = {
    "requests": FileSpec(
        "deletion_requests.csv", Request, "case_id",
        timestamps=("requested_at", "reported_at", "audit_at"),
        enums=(("reported_status", REPORTED_STATUSES),),
        required=("case_id", "email", "request_text"),
    ),
    "sample_requests": FileSpec(
        "sample_deletion_requests.csv", Request, "case_id",
        timestamps=("requested_at", "reported_at", "audit_at"),
        enums=(("reported_status", REPORTED_STATUSES),),
        required=("case_id", "email", "request_text"),
    ),
    "identities": FileSpec(
        "user_identities.csv", Identity, "user_id",
        timestamps=("updated_at",),
        required=("user_id", "email", "email_hash"),
    ),
    "audit_log": FileSpec(
        "user_audit_log.csv", AuditEvent, "audit_id",
        timestamps=("event_time",),
        enums=(("field_name", AUDIT_FIELDS),),
        required=("audit_id", "user_id", "field_name"),
    ),
    "activity": FileSpec(
        "system_activity.csv", Activity, "activity_id",
        timestamps=("event_time",),
        required=("activity_id", "event_type", "resource_id"),
    ),
    "rules": FileSpec(
        "deletion_rules.csv", Rule, "rule_id",
        enums=(("required_action", RULE_ACTIONS),),
        required=("rule_id", "data_category"),
    ),
    "evidence_files": FileSpec(
        "evidence_files.csv", EvidenceFile, "file_id",
        timestamps=("captured_at",),
        required=("file_id", "file_path"),
    ),
}

# --------------------------------------------------------------------------- loading


def read_rows(path: Path, expected_columns: Iterable[str]) -> list[dict[str, str]]:
    """Read a CSV as UTF-8 (a BOM is tolerated) and check header and row shape."""
    expected = list(expected_columns)
    try:
        handle = open(path, "r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise SchemaError(f"{path.name}: file not found") from exc
    with handle:
        try:
            reader = csv.DictReader(handle, strict=True)
            header = reader.fieldnames
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SchemaError(f"{path.name}: unreadable CSV ({exc})") from exc
        if header != expected:
            raise SchemaError(f"{path.name}: header {header} != expected {expected}")
        rows: list[dict[str, str]] = []
        try:
            for row in reader:
                line = reader.line_num
                if None in row:
                    raise SchemaError(f"{path.name} line {line}: more cells than columns")
                if any(value is None for value in row.values()):
                    raise SchemaError(f"{path.name} line {line}: fewer cells than columns")
                rows.append(row)
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SchemaError(f"{path.name}: unreadable CSV ({exc})") from exc
    return rows


def load_records(spec: FileSpec, directory: Path = DATASET_DIR) -> list:
    """Load one dataset file into validated, immutable records."""
    columns = [f.name for f in fields(spec.record)]
    path = directory / spec.filename
    rows = read_rows(path, columns)
    seen: set[str] = set()
    records = []
    for number, row in enumerate(rows, start=2):  # header is line 1
        where = f"{spec.filename} row {number}"
        for column in spec.required:
            if not row[column].strip():
                raise SchemaError(f"{where}: blank required column {column!r}")
        for column in spec.timestamps:
            validate_timestamp(row[column], f"{where} {column}")
        for column, allowed in spec.enums:
            if row[column] not in allowed:
                raise SchemaError(f"{where}: {column}={row[column]!r} not in {sorted(allowed)}")
        key = row[spec.key]
        if key in seen:
            raise SchemaError(f"{where}: duplicate {spec.key} {key!r}")
        seen.add(key)
        records.append(spec.record(**row))
    return records


def check_output_template(directory: Path = DATASET_DIR) -> None:
    """The blank template must have exactly the contract header and no rows."""
    rows = read_rows(directory / "output.csv", ["case_id", "deletion_status", "steps_json"])
    if rows:
        raise SchemaError(f"output.csv template unexpectedly has {len(rows)} data rows")


class Issue(NamedTuple):
    level: str  # "ERROR" or "WARN"
    message: str


@dataclass(frozen=True, slots=True)
class Dataset:
    requests: list[Request]
    sample_requests: list[Request]
    identities: list[Identity]
    audit_log: list[AuditEvent]
    activity: list[Activity]
    rules: list[Rule]
    evidence_files: list[EvidenceFile]
    issues: list[Issue]


def cross_check(ds: dict[str, list], directory: Path = DATASET_DIR) -> list[Issue]:
    """Data-quality checks across files. Never raises; callers decide what to do."""
    issues: list[Issue] = []
    users = {i.user_id for i in ds["identities"]}
    for event in ds["audit_log"]:
        if event.user_id not in users:
            issues.append(Issue("ERROR", f"{event.audit_id}: unknown user_id {event.user_id}"))
    for request in ds["requests"] + ds["sample_requests"]:
        if request.audit_at < request.requested_at:
            issues.append(Issue("ERROR", f"{request.case_id}: audit_at precedes requested_at"))
        if request.reported_at > request.audit_at:
            issues.append(Issue("WARN", f"{request.case_id}: reported_at is after audit_at"))
    overlap = {r.case_id for r in ds["requests"]} & {r.case_id for r in ds["sample_requests"]}
    if overlap:
        issues.append(Issue("WARN", f"{len(overlap)} case_ids appear in both request files"))
    for item in ds["evidence_files"]:
        if not (directory / item.file_path).is_file():
            issues.append(Issue("ERROR", f"{item.file_id}: missing file {item.file_path}"))
    return issues


def load_dataset(directory: Path = DATASET_DIR) -> Dataset:
    """Load and validate every participant-facing CSV. Raises SchemaError on structure."""
    check_output_template(directory)
    loaded = {name: load_records(spec, directory) for name, spec in SPECS.items()}
    return Dataset(issues=cross_check(loaded, directory), **loaded)
