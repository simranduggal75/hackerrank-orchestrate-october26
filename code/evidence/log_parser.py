"""Log evidence -> facts.

Seven line syntaxes appear (key=value with ``level=`` or a severity word, JSON lines,
``[LVL] [svc] [trace] event:``, pipe-separated, syslog ``<pri>1``, and a cloud wrapper whose
``message="..."`` holds escaped key=value pairs). All are normalised to one dict per line.

A lookup spans several lines tied by ``trace_id``: the *started* line carries the lookup key
(an email, user id or hash) and the *completed* line carries the resource and its result.
Facts are built from the completed/observation lines and borrow the trace's lookup keys as
identifiers (marked ``via="trace"``), so a resource can be tied to a person without
substring matching.

Every line is accounted for: parsed into a fact, counted as known operational noise, or
reported as an issue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loaders import EvidenceFile

from .facts import (
    IDENTIFIER_FIELDS, TEXT_FIELDS, Identifier, ParseResult, Provenance, is_null, lookup_fact,
    make_identifier, read_text_file, record_fact, to_bool, valid_timestamp,
)

PARSER = "log"

_KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S*)')
_TS = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
_BRACKET = re.compile(r"\[(\w+)\] \[([\w.-]+)\] \[([\w.-]+)\] (\w+): ?(.*)$")
_PIPE = re.compile(r"\| (\w+) \| ([\w.-]+) \| ([\w.-]+) \| (\w+) \| ?(.*)$")
_SYSLOG = re.compile(r"<\d+>1 (\S+) ([\w.-]+) ([\w.-]+) - - (.*)$")
_CLOUD = re.compile(r' message="((?:[^"\\]|\\.)*)"\s*$')
_SEVERITY = re.compile(r"(INFO|WARN|ERROR|DEBUG) ")

# Keys on "started" lines that name the subject being looked up.
START_KEYS = ("lookup_key", "match_key", "subject_ref", "subject_key", "owner_key", "scan_key")

NOISE_EVENTS = frozenset({
    "export_started", "export_completed", "cache_lookup", "connection_acquired", "dependency_retry",
    "dependency_recovered", "consumer_lag", "cache_refresh", "lease_extended", "flag_snapshot_loaded",
    "properties_reloaded", "timer_tick", "pool_sample", "checkpoint_saved", "batch_started",
    "delivery_abandoned", "certificate_checked", "slow_call_rate", "disk_latency",
    "connection_pool_pressure", "telemetry_flushed", "lease_renewed",
})


# ------------------------------------------------------------------- line parsing


def _kvs(text: str) -> dict[str, str]:
    out = {}
    for key, value in _KV.findall(text):
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key] = value
    return out


def parse_line(line: str) -> dict[str, Any] | None:
    """One log line -> dict with at least ts, event; None when the line is not understood."""
    match = re.match(_TS + r" (.*)$", line)
    if not match:
        return None
    ts, rest = match.groups()
    if rest.startswith("{"):
        data = json.loads(rest)
        if not isinstance(data, dict):
            return None
        d = {k: (None if v is None else str(v)) for k, v in data.items()}
        d["level"] = d.pop("severity", None)
        d.pop("logger", None)
    elif (m := _BRACKET.match(rest)):
        level, service, trace, event, tail = m.groups()
        d = _kvs(tail) | {"level": level, "service": service, "trace_id": trace, "event": event}
    elif (m := _PIPE.match(rest)):
        level, service, trace, event, tail = m.groups()
        d = _kvs(tail) | {"level": level, "service": service, "trace_id": trace, "event": event}
    elif (m := _SYSLOG.match(rest)):
        node, service, trace, tail = m.groups()
        d = _kvs(tail) | {"service": service, "trace_id": trace, "node": node}
    elif "requestId=" in rest and (m := _CLOUD.search(rest)):
        outer = _kvs(rest[: m.start()])
        d = _kvs(m.group(1).replace('\\"', '"')) | {
            "trace_id": outer.get("requestId"), "level": outer.get("level"), "service": outer.get("function")}
    else:
        d = _kvs(rest)
        if (m := _SEVERITY.match(rest)):
            d["level"] = m.group(1)
    if "event" not in d:
        return None
    d["ts"] = ts
    return d


# --------------------------------------------------------------- event -> fact rules


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


def _s_profile(d):
    ok = (_int(d.get("rows")) or 0) >= 1 and d.get("http_status") in (None, "200")
    return "FOUND" if ok else "NOT_FOUND"


def _s_search(d):
    result = (d.get("result") or "").lower()
    if result == "hit":
        return "FOUND"
    if result == "miss":
        return "NOT_FOUND"
    return "FOUND" if (_int(d.get("total_hits")) or 0) >= 1 else "UNKNOWN"


def _s_analytics(d):
    direct = _int(d.get("direct_identifiers"))
    if direct is not None:
        return "IDENTIFIABLE" if direct > 0 else "ANONYMIZED"
    fields = ("customer_reference", "email", "phone", "email_hash")
    return "IDENTIFIABLE" if any(not is_null(d.get(k)) for k in fields) else "ANONYMIZED"


def _s_rows(d):
    return "FOUND" if (_int(d.get("rows")) or 0) >= 1 else "NOT_FOUND"


def _s_enabled(d):
    enabled = to_bool(d.get("enabled"))
    return "UNKNOWN" if enabled is None else ("ACTIVE" if enabled else "DISABLED")


def _s_restore_policy(d):
    allowed = to_bool(d.get("restore_allowed"))
    return "UNKNOWN" if allowed is None else ("ACTIVE" if allowed else "BLOCKED")


def _s_restore_decision(d):
    return {"deny": "BLOCKED", "allow": "ACTIVE"}.get((d.get("decision") or "").lower(), "UNKNOWN")


def _s_restore_candidate(d):
    if to_bool(d.get("deletion_block")) is True:
        return "BLOCKED"
    return "ACTIVE" if to_bool(d.get("eligible")) is True else "UNKNOWN"


def _s_marker(d):
    return "FOUND" if to_bool(d.get("marker_found")) is False else "UNKNOWN"


def _const(state):
    return lambda d: state


# event -> (system, record_type, state function, attribute keys)
EVENT_RULES = {
    "profile_lookup_completed": ("account_service", "USER_PROFILE", _s_profile, ("rows", "http_status")),
    "index_query_completed": ("search_service", "SEARCH_INDEX_ENTRY", _s_search, ("index", "result", "total_hits")),
    "analytics_profile_read": ("analytics_platform", "ANALYTICS_PROFILE", _s_analytics, ("direct_identifiers",)),
    "analytics_profile_read_completed": ("analytics_platform", "ANALYTICS_PROFILE", _s_analytics, ("audience_cohort",)),
    "analytics_identity_scan": ("analytics_platform", "ANALYTICS_PROFILE", _s_analytics, ("direct_identifiers", "detector")),
    # The evidence schema does not say what record a contact lookup returns, who owns it, or what
    # state it is in, and a lookup event alone does not establish current state. Kept as an
    # unresolved lookup (state UNKNOWN); no record type is inferred.
    "contact_lookup_completed": ("analytics_platform", "UNMAPPED_LOOKUP", _const("UNKNOWN"), ("rows", "email_opt_in")),
    "restore_policy_read_completed": ("backup_service", "BACKUP_ARCHIVE", _s_restore_policy, ("archive_present", "restore_allowed", "archive_key")),
    "restore_authorization_completed": ("backup_service", "BACKUP_ARCHIVE", _s_restore_decision, ("decision", "policy")),
    "restore_candidate_evaluated": ("backup_service", "BACKUP_ARCHIVE", _s_restore_candidate, ("eligible", "deletion_block")),
    "connector_config_read_completed": ("integration_service", "DATA_FLOW", _s_enabled, ("enabled", "last_delivery_status", "connector_name")),
    "connector_config_loaded": ("integration_service", "DATA_FLOW", _s_enabled, ("enabled", "scheduler_registered")),
    "connector_heartbeat": ("integration_service", "DATA_FLOW", _s_enabled, ("enabled", "scheduler_registered", "last_run")),
    "export_metadata_read_completed": ("export_service", "USER_EXPORT", _const("FOUND"), ("available", "object_key")),
    "export_download_check_completed": ("export_service", "USER_EXPORT", _const("FOUND"), ("available", "file")),
    "audit_record_read_completed": ("security_service", "SECURITY_AUDIT_LOG", _s_rows, ("rows", "integrity_check", "event_code", "retention_until")),
    "message_pii_scan": ("support_platform", "SUPPORT_MESSAGE", _const("IDENTIFIABLE"), ("detector",)),
    "attachment_pii_scan": ("support_platform", "SUPPORT_ATTACHMENT", _const("FOUND"), ("file", "detector")),
    "retained_order_loaded": ("legal_service", "LEGAL_HOLD_RECORD", _const("FOUND"), ("reason", "access")),
    # Support-replica contact lookups: the schema does not define the record type (the support_ prefix
    # fits a message or an attachment) or the meaning of the result. Unresolved, state UNKNOWN.
    "contact_query_completed": ("support_platform", "UNMAPPED_LOOKUP", _const("UNKNOWN"), ("rows", "source")),
    "deletion_marker_check": ("support_platform", "UNMAPPED_LOOKUP", _const("UNKNOWN"), ("marker_found", "last_write")),
}
# Events whose own fields decide the state through the shared record normaliser.
RECORD_EVENTS = {
    "message_read": ("support_platform", "SUPPORT_MESSAGE"),
    "support_ticket_read_completed": ("support_platform", "SUPPORT_MESSAGE"),
}


def _event_raw(d: dict) -> dict:
    """Only identifier/text fields of a log event, with literal 'null' values removed."""
    raw = {k: v for k, v in d.items() if (k in IDENTIFIER_FIELDS or k in TEXT_FIELDS) and not is_null(v)}
    if d.get("resource_id"):
        raw["resource_id"] = d["resource_id"]
    return raw


# --------------------------------------------------------------------------- parse


def parse(path: Path, ev: EvidenceFile) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    text = read_text_file(path, result)
    if text is None:
        return result

    events: list[tuple[int, dict]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        result.count("lines")
        try:
            d = parse_line(line)
        except ValueError as exc:
            d = None
            result.issue("WARN", "UNPARSEABLE_LINE", f"line {number}", f"bad JSON line: {exc}")
        else:
            if d is None:
                result.issue("WARN", "UNPARSEABLE_LINE", f"line {number}", f"line not understood: {line[:60]!r}")
        if d is None:
            result.count("lines_unparsed")
            continue
        events.append((number, d))

    _integrity(events, ev, result)
    traces: dict[str, list[tuple[int, dict]]] = {}
    for number, d in events:
        if d.get("trace_id"):
            traces.setdefault(d["trace_id"], []).append((number, d))
    for number, d in events:
        _line_fact(number, d, traces, ev, result)
    return result


def _integrity(events, ev, result):
    """Flag logs that look truncated or that report dropped events."""
    started = [d for _, d in events if d["event"] == "export_started"]
    completed = [d for _, d in events if d["event"] == "export_completed"]
    if started and not completed:
        last = max(d["ts"] for _, d in events)
        if last >= ev.captured_at:
            result.issue("INFO", "NO_COMPLETION_MARKER", "file",
                         "no export_completed line, but the log runs up to its capture time")
        else:
            result.issue("WARN", "LOG_MAY_BE_TRUNCATED", "file",
                         f"no export_completed line and the log ends at {last}, before capture {ev.captured_at}")
    for s in started:
        expected = _int(s.get("expected_source_events"))
        for c in completed:
            actual = _int(c.get("source_events"))
            if expected is not None and actual is not None and expected != actual:
                result.issue("WARN", "COUNT_MISMATCH", "file", f"expected {expected} source events but export reports {actual}")
    for c in completed:
        dropped = _int(c.get("dropped_auxiliary")) or _int(c.get("dropped")) or 0
        if dropped:
            result.issue("INFO", "DROPPED_EVENTS", "file", f"export reports {dropped} dropped event(s)")


def _trace_resources(trace_id, traces) -> set[str]:
    """Distinct resources that lines of this trace report on."""
    return {o["resource_id"] for _, o in traces.get(trace_id or "", [])
            if o.get("resource_id") and (o["event"] in EVENT_RULES or o["event"] in RECORD_EVENTS)}


def _trace_identifiers(number, d, traces):
    """Lookup keys from the started lines of the same trace, plus the line numbers used."""
    found, lines = [], []
    for other_no, other in traces.get(d.get("trace_id") or "", []):
        if other_no == number:
            continue
        for key in START_KEYS:
            ident = make_identifier(key, other.get(key), via="trace")
            if ident:
                found.append(ident)
                lines.append(other_no)
    return found, sorted(set(lines))


def _line_fact(number, d, traces, ev, result):
    event, ts = d["event"], d["ts"]
    if not valid_timestamp(ts):
        result.issue("ERROR", "BAD_TIMESTAMP", f"line {number}", f"bad timestamp {ts!r}")
        return
    if ts > ev.captured_at:
        result.issue("WARN", "LINE_AFTER_CAPTURE", f"line {number}", f"line time {ts} is after file capture {ev.captured_at}")
    if event == "record_lookup":
        _record_lookup(number, d, ev, result)
        return
    rule = EVENT_RULES.get(event)
    record_event = RECORD_EVENTS.get(event)
    if rule is None and record_event is None:
        if event in NOISE_EVENTS or event.endswith("_started"):
            result.count("noise_lines")
        else:
            result.count("lines_unmapped")
            result.issue("INFO", "UNMAPPED_EVENT", f"line {number}", f"event {event!r} (resource {d.get('resource_id')}) has no mapping")
        return
    resource_id = d.get("resource_id")
    if not resource_id:
        result.count("lines_unmapped")
        result.issue("WARN", "NO_RESOURCE_ID", f"line {number}", f"{event} line has no resource_id; skipped")
        return
    borrowed, via_lines = _trace_identifiers(number, d, traces)
    if borrowed and len(_trace_resources(d.get("trace_id"), traces)) > 1:
        # One lookup key must never be copied onto every result of a multi-result trace.
        borrowed, via_lines = [], []
        result.issue("WARN", "AMBIGUOUS_TRACE", f"line {number}",
                     f"trace {d.get('trace_id')} reports on several resources; its lookup key was not attached")
    own = {x.value for x in (make_identifier(k, d.get(k)) for k in IDENTIFIER_FIELDS) if x}
    borrowed = [b for b in borrowed if b.value not in own]
    where = f"line {number}" + (f" (trace {d['trace_id']}, key line(s) {','.join(map(str, via_lines))})" if via_lines else "")
    common = dict(evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=ts,
                  provenance=Provenance(ev.file_id, ev.file_path, where, PARSER), store=d.get("service"),
                  extra_identifiers=borrowed)
    if rule is not None:
        system, record_type, state_fn, attr_keys = rule
        attrs = {k: d[k] for k in attr_keys if not is_null(d.get(k))}
        if d.get("region"):
            attrs["region"] = d["region"]
        result.facts.append(record_fact(system=system, record_type=record_type, raw=_event_raw(d),
                                        state=state_fn(d), extra_attributes=attrs, **common))
    else:
        system, record_type = record_event
        result.facts.append(record_fact(system=system, record_type=record_type, raw=_event_raw(d),
                                        extra_attributes={"event": event}, **common))
    result.count("facts")


def _record_lookup(number, d, ev, result):
    missing = [k for k in ("system", "resource_id", "record_type", "result") if not d.get(k)]
    if missing:
        result.count("lines_unmapped")
        result.issue("ERROR", "BAD_RECORD", f"line {number}", f"record_lookup is missing {missing}")
        return
    fact = lookup_fact(
        evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=d["ts"], system=d["system"],
        resource_id=d["resource_id"], record_type=d["record_type"], result=d["result"],
        subject=d.get("subject_reference"), provenance=Provenance(ev.file_id, ev.file_path, f"line {number}", PARSER),
        store=d.get("service"))
    if fact.state == "UNKNOWN":
        result.issue("WARN", "UNKNOWN_STATE", f"line {number}", f"unrecognised result {d['result']!r}")
    result.facts.append(fact)
    result.count("facts")
