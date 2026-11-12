"""Participant-written evaluation cases.

Each case is a small synthetic world (fictional subject, resources and evidence) decided by the real
assessment and planner. They cover situations the 32 public samples cannot show us reliably: a request that
does not authorize deletion, replica drift, an unestablished rule condition, retained records, a data flow
that must be stopped first, and evidence that arrives after the audit. Expected results are written from the
challenge rules, not copied from the engine's output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tests.decision_fixtures import LATER, Scenario, compliant_world, owned
from tests.footprint_fixtures import AUDIT, EARLY, activity_row

Op = tuple[str, str, str]  # (phase, operation, system_id) of the steps expected, in order


@dataclass(frozen=True)
class CustomCase:
    name: str
    description: str
    build: Callable[[], Scenario]
    status: str
    assessment: str
    steps: tuple[Op, ...]


def _retention() -> dict:
    return {"retention_until": "2027-01-01T00:00:00Z"}


def _replica_drift() -> Scenario:
    named = activity_row("activity_7", EARLY, "SYNC_REPLICA", "replication_service", "account_failover_replica", "account_1")
    return Scenario([owned(state="NOT_FOUND", store="customer_data_primary"),
                     owned(state="FOUND", evidence_id="evidence_02", store="customer_data_failover_replica")], activity=[named])


CUSTOM_CASES: tuple[CustomCase, ...] = (
    CustomCase(
        "access_request_not_deletion",
        "The person asks for a copy of their data. Every other field looks like a deletion case (an account that "
        "is still present), but the wording does not authorize deletion.",
        lambda: Scenario([owned(state="FOUND")], text="Could you send me a copy of the personal data you hold about me?"),
        "NOT_APPLICABLE", "NOT_AUTHORIZED", ()),
    CustomCase(
        "replica_drift",
        "The primary account store reports the account absent, but a failover replica captured at the same time "
        "still holds it, and a replication event names a replication system as its target. The copy is reported at "
        "the system recorded on the evidence row (`account_service`): the activity row is a claim, not evidence of "
        "where the record lives.",
        _replica_drift, "INCOMPLETE", "PROVEN_VIOLATION",
        (("FINDING", "REPORT_REMAINING_RECORD", "account_service"),
         ("REMEDIATION", "DELETE_RECORD", "account_service"),
         ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"))),
    CustomCase(
        "unestablished_support_context",
        "The account is gone, but an identifiable support message carries no text showing shared order or agent "
        "context. Rule 05 is not shown to apply: the case is UNRESOLVED, with no finding invented.",
        lambda: Scenario(compliant_world() + [owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03")]),
        "INCOMPLETE", "UNRESOLVED", (("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"),)),
    CustomCase(
        "complete_with_retention_and_blocked_paths",
        "Account absent, a security audit record still inside its retention period, the sync flow disabled and the "
        "backup restore blocked: everything is proven, so the case is COMPLETE with cited verifications only.",
        lambda: Scenario(compliant_world() + [
            owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", "evidence_03", attrs=_retention()),
            owned("sync_1", "DATA_FLOW", "DISABLED", "evidence_04"), owned("backup_1", "BACKUP_ARCHIVE", "BLOCKED", "evidence_05")]),
        "COMPLETE", "PROVEN_COMPLETE",
        (("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"), ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service"),
         ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service"), ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_service"))),
    CustomCase(
        "flow_stopped_before_record_changes",
        "The account still exists and an active sync flow can recreate it. The flow is disabled first, then the "
        "record is deleted; both are verified afterwards.",
        lambda: Scenario([owned(state="FOUND"), owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_03")]),
        "INCOMPLETE", "PROVEN_VIOLATION",
        (("FINDING", "REPORT_REMAINING_RECORD", "account_service"), ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "integration_service"),
         ("REMEDIATION", "DISABLE_DATA_FLOW", "integration_service"), ("REMEDIATION", "DELETE_RECORD", "account_service"),
         ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"), ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service"))),
    CustomCase(
        "evidence_after_audit_is_ignored",
        "A file captured after audit_at shows the account present again. It must not change the answer for this "
        "case: the state at audit_at is absent, so the case is COMPLETE.",
        lambda: Scenario(compliant_world() + [owned(state="FOUND", evidence_id="evidence_02", captured=LATER)], audit_at=AUDIT),
        "COMPLETE", "PROVEN_COMPLETE", (("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"),)),
    CustomCase(
        "no_account_evidence",
        "The request authorizes deletion but no evidence ties any account record to the person. Absence of evidence "
        "is not proof of deletion: the case is UNRESOLVED and no steps are invented.",
        lambda: Scenario([]), "INCOMPLETE", "UNRESOLVED", ()),
)


def run(case: CustomCase) -> dict:
    decision = case.build().decide()
    got = tuple((s["phase"], s["operation"], s["system_id"]) for s in decision.steps)
    return {"name": case.name, "description": case.description, "expected_status": case.status, "status": decision.status,
            "expected_assessment": case.assessment, "assessment": decision.assessment.assessment,
            "expected_steps": case.steps, "steps": got,
            "passed": (decision.status, decision.assessment.assessment, got) == (case.status, case.assessment, case.steps)}


def run_all() -> list[dict]:
    return [run(c) for c in CUSTOM_CASES]
