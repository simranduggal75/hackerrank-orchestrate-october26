"""Declarative deletion policy: categories, conditions, states, operations, vocabulary and ordering.

Everything the decision engine knows about *what the rules require* is in this file as data. The engine
(``assessment.py``, ``planner.py``) contains no per-category logic beyond evaluating the named conditions.
``deletion_rules.csv`` stays the authority for each rule's action; a test checks the two agree.

How a category is read
----------------------
* ``compliant`` / ``violating`` / ``missing``: observed record states that satisfy the rule, breach it,
  or (for RETAIN rules) show a record that must remain is absent.
* ``condition``: the rule's condition, evaluated from firm evidence by ``assessment.CONDITIONS``:
  ``established`` | ``unestablished`` | ``refuted``. A violation is proven only when the condition is
  established (or the rule is unconditional). An unestablished condition leaves the resource UNRESOLVED:
  the rules table is the full specification, so a rule whose condition is not shown to hold is not shown
  to require anything.
* ``verify_unconditional``: compliant states whose verification is required even when the condition is
  unestablished (the end state is the same under either value of the condition).
* Operations name the finding / remediation / verification step the contract defines for the category.

Unsupported record types are listed explicitly (``UNSUPPORTED``) so they are *recorded*, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ output vocabulary (contract)

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# (operation, state) in the contract's order within each phase.
FINDING_OPS = (
    ("REPORT_REMAINING_RECORD", "PRESENT"),
    ("REPORT_EXPOSED_PII", "PRESENT"),
    ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("REPORT_RESTORE_RISK", "ACTIVE"),
)
REMEDIATION_OPS = (
    ("DISABLE_DATA_FLOW", "DISABLED"),
    ("BLOCK_RESTORE", "BLOCKED"),
    ("DELETE_RECORD", "ABSENT"),
    ("REDACT_RECORD", "REDACTED"),
    ("ANONYMIZE_RECORD", "ANONYMIZED"),
)
VERIFICATION_OPS = (
    ("VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)

OPS_BY_PHASE = {"FINDING": FINDING_OPS, "REMEDIATION": REMEDIATION_OPS, "VERIFICATION": VERIFICATION_OPS}
OP_PHASE = {op: phase for phase, ops in OPS_BY_PHASE.items() for op, _ in ops}
OP_STATE = {op: state for ops in OPS_BY_PHASE.values() for op, state in ops}
OP_RANK = {op: i for ops in OPS_BY_PHASE.values() for i, (op, _) in enumerate(ops)}
PHASE_RANK = {phase: i for i, phase in enumerate(PHASES)}
ALLOWED_PAIRS = frozenset((phase, op, state) for phase, ops in OPS_BY_PHASE.items() for op, state in ops)
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

# ------------------------------------------------------------------------------- categories


@dataclass(frozen=True, slots=True)
class Category:
    record_type: str
    rule_id: str | None
    data_category: str | None  # as named in deletion_rules.csv (None: no rule row)
    action: str  # DELETE | REDACT | ANONYMIZE | RETAIN | DISABLE | BLOCK
    condition: str
    compliant: frozenset[str]
    violating: frozenset[str] = frozenset()
    missing: frozenset[str] = frozenset()
    finding_op: str | None = None
    remediation_op: str | None = None
    verify_op: str | None = None
    verify_unconditional: frozenset[str] = frozenset()


def _delete(record_type, rule_id, condition="always", unconditional=frozenset()):
    return Category(record_type, rule_id, record_type, "DELETE", condition, frozenset({"NOT_FOUND"}), frozenset({"FOUND"}),
                    finding_op="REPORT_REMAINING_RECORD", remediation_op="DELETE_RECORD", verify_op="VERIFY_RECORD_ABSENT",
                    verify_unconditional=unconditional)


CATEGORIES: dict[str, Category] = {c.record_type: c for c in (
    _delete("USER_PROFILE", "rule_01"),
    _delete("SEARCH_INDEX_ENTRY", "rule_03"),
    _delete("USER_EXPORT", "rule_09"),
    _delete("MARKETING_PROFILE", "rule_10"),
    _delete("SUPPORT_ATTACHMENT", "rule_06", condition="sole_ownership"),
    Category("SUPPORT_MESSAGE", "rule_05", "SUPPORT_MESSAGE", "REDACT", "shared_context",
             frozenset({"REDACTED", "NO_DIRECT_IDENTIFIER"}), frozenset({"IDENTIFIABLE"}),
             finding_op="REPORT_EXPOSED_PII", remediation_op="REDACT_RECORD", verify_op="VERIFY_RECORD_REDACTED",
             # A message that shows no direct identifier is in the end state redaction produces, whichever way the
             # shared-context condition falls, and VERIFY_RECORD_REDACTED means "required identifying fields removed".
             verify_unconditional=frozenset({"REDACTED", "NO_DIRECT_IDENTIFIER"})),
    Category("ACCOUNT_TOMBSTONE", "rule_15", "ACCOUNT_TOMBSTONE", "REDACT", "tombstone",
             frozenset({"REDACTED"}), frozenset({"IDENTIFIABLE"}),
             finding_op="REPORT_EXPOSED_PII", remediation_op="REDACT_RECORD", verify_op="VERIFY_RECORD_REDACTED",
             verify_unconditional=frozenset({"REDACTED"})),
    Category("ANALYTICS_PROFILE", "rule_07", "ANALYTICS_PROFILE", "ANONYMIZE", "attributable",
             frozenset({"ANONYMIZED"}), frozenset({"IDENTIFIABLE"}),
             finding_op="REPORT_EXPOSED_PII", remediation_op="ANONYMIZE_RECORD", verify_op="VERIFY_RECORD_ANONYMIZED",
             verify_unconditional=frozenset({"ANONYMIZED"})),
    Category("SECURITY_AUDIT_LOG", "rule_12", "SECURITY_AUDIT_LOG", "RETAIN", "retention_period",
             frozenset({"FOUND"}), missing=frozenset({"NOT_FOUND"}), verify_op="VERIFY_RECORD_PRESENT",
             verify_unconditional=frozenset({"FOUND"})),
    Category("LEGAL_HOLD_RECORD", "rule_13", "LEGAL_HOLD_RECORD", "RETAIN", "hold_active",
             frozenset({"FOUND"}), missing=frozenset({"NOT_FOUND"}), verify_op="VERIFY_RECORD_PRESENT",
             verify_unconditional=frozenset({"FOUND"})),
    Category("BACKUP_ARCHIVE", "rule_14", "BACKUP_ARCHIVE", "RETAIN", "restore_blockable",
             frozenset({"BLOCKED"}), frozenset({"ACTIVE"}),
             finding_op="REPORT_RESTORE_RISK", remediation_op="BLOCK_RESTORE", verify_op="VERIFY_RESTORE_BLOCKED"),
    # Not a rule row: the contract's operation vocabulary defines how an active data flow is handled.
    Category("DATA_FLOW", None, None, "DISABLE", "always", frozenset({"DISABLED"}), frozenset({"ACTIVE"}),
             finding_op="REPORT_ACTIVE_DATA_FLOW", remediation_op="DISABLE_DATA_FLOW", verify_op="VERIFY_DATA_FLOW_DISABLED"),
)}

# Record types the engine will not assess, with the recorded reason. Nothing is inferred for them.
UNSUPPORTED: dict[str, str] = {
    "UNMAPPED_LOOKUP": "the evidence schema does not define the record type, owner or state this lookup reports",
    "CUSTOMER_STATEMENT": "no rule category covers a customer statement document",
    "SECONDARY_IDENTIFIER": "no evidence in the dataset establishes what a secondary-identifier record is",
    "CACHE_ENTRY": "cache entries exist only as activity-log ids; no evidence can show their state",
    "PRODUCT_TELEMETRY": "no telemetry records appear in any evidence",
    "EMAIL_DELIVERY_LOG": "email delivery logs exist only as activity-log ids; no evidence can show their content",
}

# Rule 05: text evidencing shared customer, agent or order context that must be retained.
SHARED_CONTEXT_PATTERNS = (
    re.compile(r"\border(s)?\b", re.I),
    re.compile(r"\bORD-\d+\b"),
    re.compile(r"\bagent\b", re.I),
    re.compile(r"\banother customer\b", re.I),
    re.compile(r"\bshared\b", re.I),
)
TEXT_ATTRIBUTES = ("message_text", "message_excerpt")

# Rules 12 and 13: attributes that carry the supplied retention / hold end dates.
RETENTION_ATTRIBUTES = ("retention_until", "retain_until")
HOLD_ATTRIBUTES = ("retained_until", "retain_until")

# Footprint unresolved kinds that could hide a violation: they block COMPLETE (gate G8).
HIDING_RISK_KINDS = frozenset({
    "late_file_link", "late_link_event", "uncertain_identifier", "provisional_contradiction", "provisional_only",
    "mention_only", "no_observation", "stale_conflict", "ambiguous_owner", "alias_other_system",
})

# Identifier kinds that make an unmapped document a direct-PII exposure.
DIRECT_PII_KINDS = frozenset({"email", "email_hash", "phone", "name"})
