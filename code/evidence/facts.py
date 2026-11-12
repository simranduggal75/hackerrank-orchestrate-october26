"""Shared evidence model: facts, provenance, issues, and record normalisation.

A ``Fact`` says "evidence file X shows record R of system S in state T, with these
identifiers". Facts are observations only; no deletion decision is made here.

Time rule: a fact may be used for a case only if BOTH the evidence file was captured
and the record itself was observed at or before the case's ``audit_at``
(``Fact.available_by``). Timestamps are fixed-width ISO strings, so ``<=`` is
chronological.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# ------------------------------------------------------------------ vocabulary

# Record types and states mirror the vocabulary the evidence itself uses
# (handover tables, lookup logs) and the data categories in deletion_rules.csv.
STATES = frozenset({
    "FOUND", "NOT_FOUND", "ACTIVE", "DISABLED", "BLOCKED",
    "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "NO_DIRECT_IDENTIFIER", "UNKNOWN",
})

# (system, collection) -> record_type for document-style collections.
COLLECTION_TYPES = {
    ("account_service", "customer_profiles"): "USER_PROFILE",
    ("search_service", "customer_search_documents"): "SEARCH_INDEX_ENTRY",
    ("analytics_platform", "customer_analytics_reports"): "ANALYTICS_PROFILE",
    ("support_platform", "support_conversations"): "SUPPORT_MESSAGE",
    ("support_platform", "ticket_attachments"): "SUPPORT_ATTACHMENT",
    ("export_service", "customer_export_jobs"): "USER_EXPORT",
    ("integration_service", "connector_configurations"): "DATA_FLOW",
    ("backup_service", "backup_catalog"): "BACKUP_ARCHIVE",
    ("security_service", "security_audit_events"): "SECURITY_AUDIT_LOG",
    ("legal_service", "retained_order_records"): "LEGAL_HOLD_RECORD",
    ("identity_service", "deleted_customer_references"): "ACCOUNT_TOMBSTONE",
}

# Typed inventory tables (one table per record type).
TYPED_TABLES = {
    "customer_profiles": ("account_service", "USER_PROFILE"),
    "customer_search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "customer_analytics_profiles": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support_records": ("support_platform", "SUPPORT_MESSAGE"),
    "customer_exports": ("export_service", "USER_EXPORT"),
    "connector_subscriptions": ("integration_service", "DATA_FLOW"),
    "backup_restore_entries": ("backup_service", "BACKUP_ARCHIVE"),
    "security_audit_records": ("security_service", "SECURITY_AUDIT_LOG"),
}

# Per-service lists inside a service_resource_export JSON.
SERVICE_EXPORT_LISTS = {
    ("account_service", "profile_lookups"): "USER_PROFILE",
    ("analytics_platform", "profiles"): "ANALYTICS_PROFILE",
    ("backup_service", "restore_entries"): "BACKUP_ARCHIVE",
    ("export_service", "exports"): "USER_EXPORT",
    ("integration_service", "connectors"): "DATA_FLOW",
    ("search_service", "index_queries"): "SEARCH_INDEX_ENTRY",
    ("security_service", "audit_records"): "SECURITY_AUDIT_LOG",
    ("support_platform", "tickets"): "SUPPORT_MESSAGE",
}

# Fields that can carry an identifier. Their values are classified by shape, not by name.
IDENTIFIER_FIELDS = frozenset({
    "user_id", "customer_reference", "subject_reference", "subject_selector", "subject_ref",
    "email", "phone", "email_hash", "owner_email", "owner_phone", "customer_email",
    "requester_email", "requester_phone", "requester_name", "full_name", "display_name", "name",
    "uploaded_by", "lookup_value", "lookup_key", "correlation_key", "matched_value", "query",
    "subject_key", "owner_key", "scan_key", "match_key",
})
NAME_FIELDS = frozenset({"requester_name", "full_name", "display_name", "name"})
TEXT_FIELDS = frozenset({"message_text", "message_excerpt"})
DIRECT_KINDS = frozenset({"email", "email_hash", "phone", "name"})

REDACTED_TEXT = "[REDACTED]"

_USER_ID_RE = re.compile(r"^user_\d+$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")
# An email inside free text. Both parts match greedily, so a longer lookalike such as
# ``x.jr@example.test`` is one whole token and never a substring hit on ``x@example.test``;
# a sentence-ending "." is not part of the address.
EMAIL_TOKEN_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ----------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class Provenance:
    evidence_id: str
    path: str  # file_path as listed in evidence_files.csv
    locator: str  # where inside the file (table/row, JSON path, line number, page)
    parser: str


@dataclass(frozen=True, slots=True)
class Identifier:
    field: str  # source field name, e.g. "requester_email"
    value: str  # raw value exactly as found
    kind: str  # user_id | email | email_hash | phone | name | other | uncertain (an unconfirmed OCR read)
    via: str = "record"  # record | text (found inside free text) | trace (related log line) | ocr (screenshot)


@dataclass(frozen=True, slots=True)
class Fact:
    evidence_id: str
    captured_at: str  # file-level capture time (manifest)
    observed_at: str  # record-level time; equals captured_at when the record has none
    system: str
    resource_id: str | None
    record_type: str
    state: str
    identifiers: tuple[Identifier, ...]
    attributes: dict[str, Any]
    store: str | None  # table / replica / collection the record came from
    provenance: Provenance

    def available_by(self, audit_at: str) -> bool:
        """Usable for a case only if the file AND the record predate the audit cutoff."""
        return self.captured_at <= audit_at and self.observed_at <= audit_at


@dataclass(frozen=True, slots=True)
class Coverage:
    """A container the file reports on, even when it holds no rows (absence evidence)."""

    evidence_id: str
    captured_at: str
    observed_at: str
    system: str
    record_type: str | None
    store: str
    row_count: int
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ParseIssue:
    level: str  # ERROR | WARN | INFO
    code: str
    evidence_id: str
    locator: str
    message: str


@dataclass(slots=True)
class ParseResult:
    evidence_id: str
    parser: str
    facts: list[Fact] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def count(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def issue(self, level: str, code: str, locator: str, message: str) -> None:
        self.issues.append(ParseIssue(level, code, self.evidence_id, locator, message))


# ------------------------------------------------------------------ small helpers


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def valid_timestamp(value: Any) -> bool:
    """True for a real, fixed-width ISO-8601 UTC timestamp (the only form safe to compare as text)."""
    if not isinstance(value, str) or not _TS_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def is_null(value: Any) -> bool:
    """None, empty, or the literal text null/none that some logs use for missing values."""
    return value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none"))


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    return None


def classify_value(field_name: str, value: str) -> str:
    text = value.strip()
    if _USER_ID_RE.match(text):
        return "user_id"
    if _EMAIL_RE.match(text):
        return "email"
    if _HEX64_RE.match(text):
        return "email_hash"
    if _PHONE_RE.match(text) and sum(ch.isdigit() for ch in text) >= 7:
        return "phone"
    if field_name in NAME_FIELDS:
        return "name"
    return "other"


def make_identifier(field_name: str, value: Any, via: str = "record") -> Identifier | None:
    if is_null(value) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    return Identifier(field_name, text, classify_value(field_name, text), via)


def text_identifiers(field_name: str, text: Any) -> list[Identifier]:
    """Emails found inside free text, as exact tokens (never substrings of longer addresses)."""
    if is_null(text) or not isinstance(text, str):
        return []
    return [Identifier(field_name, m.group(0), "email", "text") for m in EMAIL_TOKEN_RE.finditer(text)]


def _flatten(raw: dict) -> dict:
    """Pull one level of nested identifier dicts (search ``document``, profile ``profile``) up."""
    flat = {}
    for key, value in raw.items():
        if key in ("document", "profile") and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def extract_identifiers(raw: dict) -> tuple[Identifier, ...]:
    flat = _flatten(raw)
    found: list[Identifier] = []
    for key, value in flat.items():
        if key in IDENTIFIER_FIELDS:
            ident = make_identifier(key, value)
            if ident:
                found.append(ident)
        elif key in TEXT_FIELDS:
            found.extend(text_identifiers(key, value))
    seen, unique = set(), []
    for ident in found:
        marker = (ident.field, ident.value, ident.via)
        if marker not in seen:
            seen.add(marker)
            unique.append(ident)
    return tuple(unique)


def _scalar_attributes(raw: dict) -> dict[str, Any]:
    flat = _flatten(raw)
    attrs: dict[str, Any] = {}
    for key, value in flat.items():
        if key in IDENTIFIER_FIELDS or key == "resource_id":
            continue
        if key in TEXT_FIELDS:
            # Free text is kept as evidence: rule 05's "shared context" condition can only be judged from it.
            if isinstance(value, str) and value.strip():
                attrs[key] = value
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            attrs[key] = value
        elif key == "included_sections" and isinstance(value, list):
            attrs[key] = list(value)
    return attrs


def state_for(record_type: str, raw: dict, identifiers: Iterable[Identifier]) -> str:
    """Observed condition of one stored record. No policy is applied here."""
    flat = _flatten(raw)
    idents = list(identifiers)
    direct = [i for i in idents if i.kind in DIRECT_KINDS]
    if record_type == "ANALYTICS_PROFILE":
        return "IDENTIFIABLE" if idents else "ANONYMIZED"
    if record_type == "ACCOUNT_TOMBSTONE":
        return "IDENTIFIABLE" if direct else "REDACTED"
    if record_type == "SUPPORT_MESSAGE":
        if direct:
            return "IDENTIFIABLE"
        text = flat.get("message_text", flat.get("message_excerpt"))
        if isinstance(text, str) and text.strip() == REDACTED_TEXT:
            return "REDACTED"
        return "NO_DIRECT_IDENTIFIER"
    if record_type == "DATA_FLOW":
        enabled = to_bool(flat.get("enabled"))
        return "UNKNOWN" if enabled is None else ("ACTIVE" if enabled else "DISABLED")
    if record_type == "BACKUP_ARCHIVE":
        allowed = to_bool(flat.get("restore_allowed"))
        return "UNKNOWN" if allowed is None else ("ACTIVE" if allowed else "BLOCKED")
    return "FOUND"


def record_fact(
    *, evidence_id: str, captured_at: str, observed_at: str | None, system: str,
    record_type: str, raw: dict, store: str | None, provenance: Provenance,
    resource_id: str | None = None, extra_identifiers: Iterable[Identifier] = (),
    state: str | None = None, extra_attributes: dict | None = None,
) -> Fact:
    """Normalise one raw stored record into a Fact."""
    identifiers = extract_identifiers(raw) + tuple(extra_identifiers)
    attributes = _scalar_attributes(raw)
    if extra_attributes:
        attributes.update(extra_attributes)
    rid = resource_id if resource_id is not None else raw.get("resource_id")
    return Fact(
        evidence_id=evidence_id,
        captured_at=captured_at,
        observed_at=observed_at or captured_at,
        system=system,
        resource_id=rid if isinstance(rid, str) and rid else None,
        record_type=record_type,
        state=state or state_for(record_type, raw, identifiers),
        identifiers=identifiers,
        attributes=attributes,
        store=store,
        provenance=provenance,
    )


def lookup_fact(
    *, evidence_id: str, captured_at: str, observed_at: str | None, system: str,
    resource_id: str | None, record_type: str, result: str, subject: Any, provenance: Provenance,
    subject_field: str = "subject_reference", store: str | None = None,
    extra_identifiers: Iterable[Identifier] = (), attributes: dict | None = None,
) -> Fact:
    """A fact from an explicit lookup row: (system, resource, record_type, result, subject).

    ``result`` is already in the evidence's own vocabulary (FOUND, REDACTED, ...). A result
    outside that vocabulary becomes UNKNOWN with the original kept in ``raw_state``.
    """
    attrs = dict(attributes or {})
    state = result
    if result not in STATES:
        state = "UNKNOWN"
        attrs["raw_state"] = result
    note = isinstance(subject, str) and subject.strip().lower() == "no direct identifier"
    ident = None if note else make_identifier(subject_field, subject)
    if note:
        attrs["subject_note"] = "no direct identifier"
    identifiers = tuple(i for i in (ident,) if i) + tuple(extra_identifiers)
    return Fact(
        evidence_id=evidence_id, captured_at=captured_at, observed_at=observed_at or captured_at,
        system=system, resource_id=resource_id or None, record_type=record_type, state=state,
        identifiers=identifiers, attributes=attrs, store=store, provenance=provenance,
    )


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def read_text_file(path, result: ParseResult) -> str | None:
    """Read UTF-8 text. Unreadable files and bad bytes become issues; nothing is dropped silently."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        result.issue("ERROR", "UNREADABLE_FILE", "file", f"cannot read file: {exc}")
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        result.issue("ERROR", "DECODE_ERROR", f"byte {exc.start}", "invalid UTF-8; undecodable bytes replaced")
        return data.decode("utf-8-sig", errors="replace")
