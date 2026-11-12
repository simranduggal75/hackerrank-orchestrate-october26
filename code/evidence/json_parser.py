"""JSON evidence -> facts.

Four document shapes exist:
  * snapshot bundle   {exported_at, source, snapshots:[{captured_at, data_store, collection, records}]}
  * resource export   {exported_at, region, service_exports:{service:{list:[record]}}}
  * flat lookup list  {exported_at, origin, record_count, records:[{recorded_at, system, resource_id,
                       record_type, result, subject_reference}]}
  * collection file   {captured_at, source, collection, records:[record]}

Each record carries its own timestamp (snapshot ``captured_at``, ``observed_at`` or
``recorded_at``); that becomes the fact's ``observed_at``. Malformed records are
skipped *with an issue* so nothing disappears silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from loaders import EvidenceFile

from .facts import (
    COLLECTION_TYPES, SERVICE_EXPORT_LISTS, Coverage, ParseResult, Provenance, lookup_fact,
    read_text_file, record_fact, valid_timestamp,
)

PARSER = "json"
_COLLECTION_SYSTEM = {collection: system for (system, collection) in COLLECTION_TYPES}


def parse(path: Path, ev: EvidenceFile) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    text = read_text_file(path, result)
    if text is None:
        return result
    try:
        doc = json.loads(text)
    except ValueError as exc:
        result.issue("ERROR", "INVALID_JSON", "file", f"file is not valid JSON: {exc}")
        return result
    if not isinstance(doc, dict):
        result.issue("ERROR", "UNKNOWN_SHAPE", "$", "top level is not a JSON object")
        return result

    stamp = doc.get("exported_at", doc.get("captured_at"))
    if valid_timestamp(stamp) and stamp != ev.captured_at:
        result.issue("WARN", "TIMESTAMP_MISMATCH", "$", f"document says {stamp} but manifest says {ev.captured_at}")
    elif not valid_timestamp(stamp):
        result.issue("WARN", "NO_FILE_TIMESTAMP", "$", "no usable exported_at/captured_at; manifest time is used")

    if isinstance(doc.get("snapshots"), list):
        _bundle(doc, ev, result)
    elif isinstance(doc.get("service_exports"), dict):
        _resource_export(doc, ev, result)
    elif isinstance(doc.get("records"), list) and "collection" in doc:
        _collection_file(doc, ev, result)
    elif isinstance(doc.get("records"), list):
        _flat_lookups(doc, ev, result)
    else:
        result.issue("ERROR", "UNKNOWN_SHAPE", "$", f"unrecognised document layout (keys: {sorted(doc)})")
    return result


# ------------------------------------------------------------------------ helpers


def _prov(ev: EvidenceFile, locator: str) -> Provenance:
    return Provenance(ev.file_id, ev.file_path, locator, PARSER)


def _skip(result: ParseResult, locator: str, message: str, code: str = "BAD_RECORD") -> None:
    result.count("records_skipped")
    result.issue("ERROR", code, locator, message)


def _check_record(result, record, locator, needs=("resource_id",)) -> bool:
    if not isinstance(record, dict):
        _skip(result, locator, "record is not an object")
        return False
    missing = [k for k in needs if not isinstance(record.get(k), str) or not record.get(k)]
    if missing:
        _skip(result, locator, f"record is missing {missing}")
        return False
    return True


def _lookup_record(result, ev, record, locator, store, subject_field, subject_key) -> None:
    needs = ("recorded_at", "system", "resource_id", "record_type", "result")
    if not _check_record(result, record, locator, needs):
        return
    if not valid_timestamp(record["recorded_at"]):
        _skip(result, locator, f"bad recorded_at {record['recorded_at']!r}", "BAD_TIMESTAMP")
        return
    fact = lookup_fact(
        evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=record["recorded_at"],
        system=record["system"], resource_id=record["resource_id"], record_type=record["record_type"],
        result=record["result"], subject=record.get(subject_key), subject_field=subject_field,
        provenance=_prov(ev, locator), store=store)
    if fact.state == "UNKNOWN":
        result.issue("WARN", "UNKNOWN_STATE", locator, f"unrecognised result {record['result']!r}")
    result.facts.append(fact)
    result.count("facts")


# ---------------------------------------------------------------------- shapes


def _bundle(doc, ev, result):
    exported = doc.get("exported_at")
    for i, snap in enumerate(doc["snapshots"]):
        loc = f"snapshots[{i}]"
        if not isinstance(snap, dict) or not isinstance(snap.get("records"), list):
            _skip(result, loc, "snapshot has no records list", "BAD_SNAPSHOT")
            continue
        captured = snap.get("captured_at")
        system, collection = snap.get("data_store"), snap.get("collection")
        if not valid_timestamp(captured):
            _skip(result, loc, f"snapshot has bad captured_at {captured!r}; its records were not used", "BAD_TIMESTAMP")
            continue
        if valid_timestamp(exported) and captured > exported:
            result.issue("WARN", "SNAPSHOT_AFTER_EXPORT", loc, f"snapshot time {captured} is after export {exported}")
        history = collection == "operational_record_history"
        record_type = COLLECTION_TYPES.get((system, collection))
        if not history and record_type is None:
            result.count("records_unmapped", len(snap["records"]))
            result.issue("WARN", "UNKNOWN_COLLECTION", loc, f"unmapped collection ({system}, {collection}); {len(snap['records'])} record(s) not extracted")
            continue
        for j, record in enumerate(snap["records"]):
            rloc = f"{loc}.records[{j}]"
            if history:
                _lookup_record(result, ev, record, rloc, f"{system}/{collection}", "lookup_value", "lookup_value")
                continue
            if not _check_record(result, record, rloc):
                continue
            result.facts.append(record_fact(
                evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=captured, system=system,
                record_type=record_type, raw=record, store=f"{system}/{collection}", provenance=_prov(ev, rloc)))
            result.count("facts")
        result.coverage.append(Coverage(ev.file_id, ev.captured_at, captured, system, record_type,
                                        f"{system}/{collection}", len(snap["records"]), _prov(ev, loc)))


def _resource_export(doc, ev, result):
    for service, lists in doc["service_exports"].items():
        if not isinstance(lists, dict):
            _skip(result, f"service_exports.{service}", "service entry is not an object", "BAD_SNAPSHOT")
            continue
        for list_name, records in lists.items():
            loc = f"service_exports.{service}.{list_name}"
            record_type = SERVICE_EXPORT_LISTS.get((service, list_name))
            if record_type is None or not isinstance(records, list):
                n = len(records) if isinstance(records, list) else 0
                result.count("records_unmapped", n)
                result.issue("WARN", "UNKNOWN_COLLECTION", loc, f"unmapped list ({service}, {list_name}); {n} record(s) not extracted")
                continue
            for i, record in enumerate(records):
                rloc = f"{loc}[{i}]"
                if not _check_record(result, record, rloc):
                    continue
                observed = record.get("observed_at")
                if not valid_timestamp(observed):
                    _skip(result, rloc, f"bad observed_at {observed!r}", "BAD_TIMESTAMP")
                    continue
                _export_record(result, ev, service, list_name, record_type, record, observed, rloc)
            result.coverage.append(Coverage(ev.file_id, ev.captured_at, ev.captured_at, service, record_type,
                                            f"{service}/{list_name}", len(records), _prov(ev, loc)))


def _export_record(result, ev, service, list_name, record_type, record, observed, loc):
    raw = {k: v for k, v in record.items() if k != "observed_at"}
    common = dict(evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=observed, system=service,
                  record_type=record_type, store=f"{service}/{list_name}", provenance=_prov(ev, loc))
    if list_name == "profile_lookups":
        found = record.get("profile") is not None and record.get("status_code") == 200
        result.facts.append(record_fact(raw=raw, state="FOUND" if found else "NOT_FOUND",
                                        extra_attributes={"status_code": record.get("status_code")}, **common))
    elif list_name == "index_queries":
        hits = record.get("hits") if isinstance(record.get("hits"), dict) else {}
        documents = [d for d in hits.get("documents", []) if isinstance(d, dict)]
        query = record.get("query")
        if not documents:
            result.facts.append(record_fact(raw={"resource_id": record["resource_id"], "query": query},
                                            state="NOT_FOUND", extra_attributes={"total_hits": 0}, **common))
        for doc in documents:
            # The query is context, not an identifier of the hit: a search for one address can also
            # return a lookalike document, which must not inherit the searched address.
            result.facts.append(record_fact(raw=doc, state="FOUND", resource_id=doc.get("resource_id") or record["resource_id"],
                                            extra_attributes={"total_hits": hits.get("total"), "query": query}, **common))
            result.count("facts")
        result.count("facts", 0 if documents else 1)
        return
    else:
        result.facts.append(record_fact(raw=raw, **common))
    result.count("facts")


def _collection_file(doc, ev, result):
    collection, captured = doc.get("collection"), doc.get("captured_at")
    system = _COLLECTION_SYSTEM.get(collection)
    if system is None:
        result.count("records_unmapped", len(doc["records"]))
        result.issue("WARN", "UNKNOWN_COLLECTION", "$", f"unmapped collection {collection!r}")
        return
    if not valid_timestamp(captured):
        result.issue("ERROR", "BAD_TIMESTAMP", "$", f"bad captured_at {captured!r}; records not used")
        return
    record_type = COLLECTION_TYPES[(system, collection)]
    for i, record in enumerate(doc["records"]):
        loc = f"records[{i}]"
        if not _check_record(result, record, loc):
            continue
        result.facts.append(record_fact(
            evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=captured, system=system,
            record_type=record_type, raw=record, store=f"{system}/{collection}", provenance=_prov(ev, loc)))
        result.count("facts")
    result.coverage.append(Coverage(ev.file_id, ev.captured_at, captured, system, record_type,
                                    f"{system}/{collection}", len(doc["records"]), _prov(ev, "$")))


def _flat_lookups(doc, ev, result):
    declared = doc.get("record_count")
    if isinstance(declared, int) and declared != len(doc["records"]):
        result.issue("WARN", "COUNT_MISMATCH", "$", f"record_count says {declared} but {len(doc['records'])} records are present")
    for i, record in enumerate(doc["records"]):
        _lookup_record(result, ev, record, f"records[{i}]", doc.get("origin"), "subject_reference", "subject_reference")
