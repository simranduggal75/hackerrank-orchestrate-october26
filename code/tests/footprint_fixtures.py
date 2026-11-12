"""Synthetic worlds for ownership and state tests."""

from activity import ActivityIndex
from evidence.facts import Coverage, Fact, Identifier, ParseResult, Provenance, classify_value
from evidence.store import EvidenceStore
from footprint import FootprintBuilder
from identity import IdentityIndex, email_hash
from loaders import Activity, AuditEvent, EvidenceFile, Identity

AUDIT = "2026-09-30T00:00:00Z"
EARLY = "2026-09-01T00:00:00Z"

SYSTEMS = {
    "USER_PROFILE": "account_service", "SEARCH_INDEX_ENTRY": "search_service", "ANALYTICS_PROFILE": "analytics_platform",
    "SUPPORT_MESSAGE": "support_platform", "SUPPORT_ATTACHMENT": "support_platform", "USER_EXPORT": "export_service",
    "DATA_FLOW": "integration_service", "BACKUP_ARCHIVE": "backup_service", "SECURITY_AUDIT_LOG": "security_service",
    "LEGAL_HOLD_RECORD": "legal_service", "ACCOUNT_TOMBSTONE": "identity_service",
}


def ident(field, value, kind=None, via="record"):
    return Identifier(field, value, kind or classify_value(field, value), via)


def fact(evidence_id="evidence_01", resource="account_1", record_type="USER_PROFILE", state="FOUND", system=None,
         idents=(), captured=EARLY, observed=None, store=None, attrs=None, parser="json", locator="records[0]"):
    return Fact(evidence_id, captured, observed or captured, system or SYSTEMS.get(record_type, "other_service"),
                resource, record_type, state, tuple(idents), dict(attrs or {}), store,
                Provenance(evidence_id, f"evidence/{evidence_id}.x", locator, parser))


def image_fact(**kw):
    attrs = dict(kw.pop("attrs", None) or {}, provisional=True, source="screenshot_ocr")
    return fact(parser="image", attrs=attrs, **kw)


def coverage(evidence_id="evidence_02", system="account_service", record_type="USER_PROFILE", store="customer_data_primary",
             observed=EARLY, rows=1, parser="sql"):
    return Coverage(evidence_id, observed, observed, system, record_type, store, rows,
                    Provenance(evidence_id, f"evidence/{evidence_id}.sql", f"table {store}", parser))


def make_store(facts=(), coverages=()):
    by_file = {}
    for f in facts:
        by_file.setdefault(f.evidence_id, ParseResult(f.evidence_id, "test")).facts.append(f)
    for c in coverages:
        by_file.setdefault(c.evidence_id, ParseResult(c.evidence_id, "test")).coverage.append(c)
    manifest = [EvidenceFile(i, f"evidence/{i}.x", "o", "2026-09-01T00:00:00Z") for i in by_file]
    return EvidenceStore(by_file.values(), manifest)


def users():
    def u(uid, name, email, phone):
        return Identity(uid, name, email, phone, email_hash(email), "2026-08-01T00:00:00Z")
    return [u("user_1", "Sam Lee", "sam.lee@example.test", "+44 7700 900001"),
            u("user_2", "Pat Roe", "pat.roe@example.test", "+44 7700 900002"),
            u("user_3", "Sam Lee Jr", "sam.lee.jr@example.test", "+44 7700 900003"),
            u("user_12", "Ina Twelve", "ina.twelve@example.test", "+44 7700 900012")]


def audit_history():
    old, new = "sam.old@example.test", "sam.lee@example.test"
    return [AuditEvent("audit_1", "user_1", "2026-06-01T00:00:00Z", "email", old, new),
            AuditEvent("audit_2", "user_1", "2026-06-01T00:00:00Z", "email_hash", email_hash(old), email_hash(new))]


def activity_row(activity_id, time, event_type, source, target, resource, result="SUCCESS", details=""):
    return Activity(activity_id, time, event_type, source, target, resource, result, details or f"{event_type} for {resource}.")


def link_row(activity_id, time, resource, system, alias, email, user, exclusive=False):
    verb = "belongs only to" if exclusive else "to"
    tail = "; no other subject is linked" if exclusive else ""
    details = f"Data catalog linked {resource} ({alias or resource}) {verb} {email} and {user}{tail}."
    return activity_row(activity_id, time, "LINK_RESOURCE_IDENTITY", "data_catalog", system, resource, details=details)


class World:
    """A builder over a synthetic store, identity table and activity log."""

    def __init__(self, facts=(), coverages=(), activity=()):
        self.identity = IdentityIndex(users(), audit_history())
        self.store = make_store(facts, coverages)
        self.activity = ActivityIndex(list(activity))
        self.builder = FootprintBuilder(self.store, self.identity, self.activity)

    def footprint(self, email="sam.lee@example.test", audit_at=AUDIT, policy="strict"):
        return self.builder.build(self.identity.resolve_email(email, audit_at, "case_x"), policy)
