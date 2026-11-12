"""Case assessment: per-resource verdicts, the evidence-coverage gate (G0-G8), and the internal assessment.

Four internal assessments are kept distinct, even though two map to the same output status:

  NOT_AUTHORIZED   the request wording does not authorize deletion          -> NOT_APPLICABLE
  PROVEN_VIOLATION a firm, condition-established violation exists            -> INCOMPLETE
  UNRESOLVED       no proven violation, but the gate is not passed           -> INCOMPLETE
  PROVEN_COMPLETE  the gate is passed with firm, citable evidence            -> COMPLETE

An unknown, contradictory, provisional or condition-unestablished state is UNRESOLVED. It is never
reported as a violation: only firm evidence of a state that the (established) rule forbids is a violation.

Gate for COMPLETE (every condition must hold; each failure carries reason codes):
  G0 authorization and a resolved subject      G5 every discovered retained record proven present
  G1 account established and proven absent     G6 no unsupported record type among owned resources
  G2 no proven violation                       G7 no unestablished rule condition that could change a verdict
  G3 every owned resource proven compliant     G8 no hiding-risk signal (late link, uncertain identifier,
  G4 flows disabled and backups blocked           screenshot-only/contradicted state, ambiguous owner, ...)

Residual assumption (A1), recorded in every report: the footprint is exact-identifier discovery over firm
evidence at or before audit_at. A violation exposes an identifier, so a violation outside the footprint would
normally leave a hiding-risk signal; categories with no evidence at all (cache, email delivery, telemetry)
cannot be assessed and are listed as gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import policy
from activity import ActivityIndex
from evidence.facts import Fact
from evidence.store import EvidenceStore
from footprint import Footprint, OwnedResource
from identity import Resolution, SubjectIdentifiers
from intent import IntentResult
from state import Observation, ResourceState

ASSESSMENTS = ("NOT_AUTHORIZED", "PROVEN_VIOLATION", "UNRESOLVED", "PROVEN_COMPLETE")
OUTPUT_STATUS = {"NOT_AUTHORIZED": "NOT_APPLICABLE", "PROVEN_VIOLATION": "INCOMPLETE",
                 "UNRESOLVED": "INCOMPLETE", "PROVEN_COMPLETE": "COMPLETE"}
GATES = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")

# reason code -> gate, for resources that are neither compliant nor violating
_CODE_GATE = {
    "state_unknown": "G3", "provisional_only": "G3", "provisional_contradiction": "G3", "no_citable_evidence": "G3",
    "unexpected_state": "G3", "unmapped_type": "G6", "condition_unestablished": "G7",
    "retained_absent_condition_unestablished": "G7", "restore_access_unknown": "G4",
}
_RETAIN_TYPES = frozenset({"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"})
_LIVE_PATH_TYPES = frozenset({"DATA_FLOW", "BACKUP_ARCHIVE"})
_UNCONDITIONAL = frozenset({"always", "restore_blockable"})


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    resource_id: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Copy:
    """One store's copy of a resource that violates its rule (primary or a replica).

    ``system_id`` is the system recorded on the evidence row itself. A replica is another store of the same
    system, so it shares the resource's ``system_id``; activity rows are claims and never name where a record lives.
    """

    system_id: str
    role: str
    state: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class ResourceVerdict:
    resource_id: str
    record_type: str | None
    system_id: str | None
    rule_id: str | None
    kind: str  # COMPLIANT | VIOLATION | RETAINED_MISSING | UNRESOLVED
    state: str
    provisional: bool
    absence: str | None
    reasons: tuple[Reason, ...]
    condition: str  # established | unestablished | refuted | n/a
    condition_detail: str
    copies: tuple[Copy, ...]  # violating copies (VIOLATION only)
    compliant_evidence: str | None  # file proving the compliant state
    verify: bool
    verify_class: str | None  # existing_compliant | post_remediation | unresolved_check


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    passed: bool
    reasons: tuple[Reason, ...] = ()


@dataclass(slots=True)
class CaseAssessment:
    case_id: str
    assessment: str
    intent: IntentResult
    user_id: str | None
    audit_at: str
    verdicts: tuple[ResourceVerdict, ...] = ()
    gates: tuple[GateResult, ...] = ()
    notes: list[str] = field(default_factory=list)
    gaps: list[Reason] = field(default_factory=list)  # evidence gaps recorded for the report

    @property
    def status(self) -> str:
        return OUTPUT_STATUS[self.assessment]

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]


# ------------------------------------------------------------------------------ conditions


@dataclass(slots=True)
class Ctx:
    resource_id: str
    state: ResourceState
    owned: OwnedResource
    facts: list[Fact]  # firm (non-provisional) facts about this resource at or before audit_at
    footprint: Footprint
    as_of: str


def _texts(facts: list[Fact]) -> list[str]:
    return [str(f.attributes[a]) for f in facts for a in policy.TEXT_ATTRIBUTES if f.attributes.get(a)]


def _cond_always(ctx: Ctx):
    return "established", "unconditional rule"


def _cond_shared_context(ctx: Ctx):
    texts = _texts(ctx.facts)
    for text in texts:
        if any(p.search(text) for p in policy.SHARED_CONTEXT_PATTERNS):
            return "established", f"message text shows order/agent context ({text[:60]!r})"
    if texts:
        return "unestablished", "message text shows no order, agent or shared-customer context"
    return "unestablished", "no message text in any firm observation"


def _cond_sole_ownership(ctx: Ctx):
    if ctx.owned.exclusive:
        return "established", "a link event states the attachment belongs only to the subject"
    return "unestablished", "no explicit statement that the attachment belongs only to the subject"


def _cond_attributable(ctx: Ctx):
    if ctx.state.state == "IDENTIFIABLE":
        return "established", "the firm observation shows identifiers"
    return "refuted", "the firm observation shows no identifier"


def _dates(facts: list[Fact], names: tuple[str, ...]) -> list[str]:
    return [str(f.attributes[n]) for f in facts for n in names if f.attributes.get(n)]


def _period(names: tuple[str, ...], what: str):
    def condition(ctx: Ctx):
        dates = _dates(ctx.facts, names)
        if not dates:
            return "unestablished", f"no {what} end date in evidence"
        if max(dates) > ctx.as_of:
            return "established", f"{what} runs to {max(dates)[:10]}"
        return "refuted", f"{what} ended {max(dates)[:10]}"
    return condition


def _cond_restore_blockable(ctx: Ctx):
    return "established", "BLOCK_RESTORE is a contract operation for the affected archive"


def _cond_tombstone(ctx: Ctx):
    deleted = any(f.attributes.get("account_status") == "DELETED" for f in ctx.facts) or any(
        r.state.record_type == "USER_PROFILE" and r.state.state == "NOT_FOUND" and r.state.absence
        for r in ctx.footprint.resources.values())
    retained = any(r.state.record_type in _RETAIN_TYPES and r.state.state == "FOUND" and not r.state.provisional
                   for r in ctx.footprint.resources.values())
    if deleted and retained:
        return "established", "account deleted and a retained record still references the user id"
    missing = [n for n, ok in (("account deletion", deleted), ("a retained record referencing the user id", retained)) if not ok]
    return "unestablished", "not shown: " + " and ".join(missing)


CONDITIONS = {
    "always": _cond_always, "shared_context": _cond_shared_context, "sole_ownership": _cond_sole_ownership,
    "attributable": _cond_attributable, "retention_period": _period(policy.RETENTION_ATTRIBUTES, "retention"),
    "hold_active": _period(policy.HOLD_ATTRIBUTES, "hold"), "restore_blockable": _cond_restore_blockable,
    "tombstone": _cond_tombstone,
}


# --------------------------------------------------------------------------- evidence picks


def _newest(observations: list[Observation]) -> Observation | None:
    """Newest observation; ties prefer the primary store, then the smallest evidence id (deterministic)."""
    if not observations:
        return None
    top = max((o.observed_at, o.captured_at) for o in observations)
    tied = [o for o in observations if (o.observed_at, o.captured_at) == top]
    return min(tied, key=lambda o: (o.role != "primary", o.evidence_id))


def _violating_copies(entry: policy.Category, state: ResourceState) -> tuple[Copy, ...]:
    firm = [o for o in state.observations if not o.provisional]
    latest: dict[str, Observation] = {}
    for role in sorted({o.role for o in firm}):
        mine = [o for o in firm if o.role == role]
        newest = max(o.observed_at for o in mine)
        pick = [o for o in mine if o.observed_at == newest]
        latest[role] = next((o for o in pick if o.state in entry.violating), pick[0])
    negatives = [o.observed_at for o in latest.values() if o.state == "NOT_FOUND"]
    newest_negative = max(negatives, default=None)
    copies = []
    for role, obs in sorted(latest.items()):
        if obs.state not in entry.violating or (newest_negative and obs.observed_at < newest_negative):
            continue
        copies.append(Copy(state.system or "", role, obs.state, obs.evidence_id))
    return tuple(copies)


# ------------------------------------------------------------------------------- verdicts


def _verdict(owned: OwnedResource, footprint: Footprint, store: EvidenceStore) -> ResourceVerdict:
    rs = owned.state
    rid, rtype = owned.resource_id, rs.record_type
    base = dict(resource_id=rid, record_type=rtype, system_id=rs.system, provisional=rs.provisional, absence=rs.absence,
                state=rs.state)

    def verdict(kind, rule_id=None, reasons=(), condition="n/a", detail="", copies=(), evidence=None, verify=False,
                verify_class=None):
        return ResourceVerdict(kind=kind, rule_id=rule_id, reasons=tuple(reasons), condition=condition,
                               condition_detail=detail, copies=copies, compliant_evidence=evidence, verify=verify,
                               verify_class=verify_class, **base)

    entry = policy.CATEGORIES.get(rtype or "")
    if entry is None:
        why = policy.UNSUPPORTED.get(rtype or "", "the record type could not be determined")
        return verdict("UNRESOLVED", reasons=[Reason("unmapped_type", rid, f"{rtype}: {why}")])

    firm_facts = [f for f in store.facts_for_resource(rid, footprint.as_of) if not f.attributes.get("provisional")]
    ctx = Ctx(rid, rs, owned, firm_facts, footprint, footprint.as_of)
    cond, detail = CONDITIONS[entry.condition](ctx)
    common = dict(rule_id=entry.rule_id, condition=cond, detail=detail)
    unconditional = entry.condition in _UNCONDITIONAL

    if rs.provisional_conflict:
        return _unknown(verdict, entry, rid, "provisional_contradiction", "; ".join(rs.conflicts), common, unconditional)
    if rs.state == "UNKNOWN":
        return _unknown(verdict, entry, rid, "state_unknown", rs.basis, common, unconditional)
    if rs.provisional:
        return _unknown(verdict, entry, rid, "provisional_only", "the state rests only on screenshots", common, unconditional)

    if rs.state in entry.violating:
        if cond == "established" or entry.condition == "always":
            copies = _violating_copies(entry, rs)
            if not copies:  # a violating aggregate with no attributable firm copy cannot be cited
                return verdict("UNRESOLVED", reasons=[Reason("no_citable_evidence", rid, "no firm observation to cite")], **common)
            return verdict("VIOLATION", copies=copies, verify=entry.verify_op is not None, verify_class="post_remediation", **common)
        return verdict("UNRESOLVED", reasons=[Reason("condition_unestablished", rid, f"{entry.condition}: {detail}")], **common)

    if rs.state in entry.compliant:
        evidence = _newest([o for o in rs.observations if not o.provisional and o.state in entry.compliant])
        if evidence is None:
            return verdict("UNRESOLVED", reasons=[Reason("no_citable_evidence", rid, "no firm observation to cite")], **common)
        needed = (entry.verify_op is not None and (unconditional or cond == "established" or rs.state in entry.verify_unconditional))
        return verdict("COMPLIANT", evidence=evidence.evidence_id, verify=needed,
                       verify_class="existing_compliant" if needed else None, **common)

    if rs.state in entry.missing:
        if cond == "established":
            return verdict("RETAINED_MISSING", reasons=[Reason("retained_record_missing", rid, detail)], **common)
        return verdict("UNRESOLVED", reasons=[Reason("retained_absent_condition_unestablished", rid, f"{entry.condition}: {detail}")], **common)

    code = "restore_access_unknown" if rtype == "BACKUP_ARCHIVE" and rs.state == "FOUND" else "unexpected_state"
    return verdict("UNRESOLVED", reasons=[Reason(code, rid, f"state {rs.state} is not defined for {rtype}")], **common)


def _unknown(verdict, entry, rid, code, detail, common, unconditional):
    """Unknown, contradictory or provisional state: UNRESOLVED. A required end state may still be checked later."""
    check = entry.verify_op is not None and unconditional
    return verdict("UNRESOLVED", reasons=[Reason(code, rid, detail)], verify=check,
                   verify_class="unresolved_check" if check else None, **common)


# ---------------------------------------------------------------------------------- gate


def _gate_of(verdict: ResourceVerdict) -> str:
    if verdict.record_type in _LIVE_PATH_TYPES:
        return "G4"
    if verdict.record_type in _RETAIN_TYPES:
        return "G5"
    return _CODE_GATE.get(verdict.reasons[0].code, "G3") if verdict.reasons else "G3"


def evaluate_gates(intent: IntentResult, footprint: Footprint, verdicts: tuple[ResourceVerdict, ...]) -> tuple[GateResult, ...]:
    reasons: dict[str, list[Reason]] = {g: [] for g in GATES}
    if not intent.authorized:
        reasons["G0"].append(Reason("not_authorized", None, "the request wording does not authorize deletion"))
    if footprint.user_id is None:
        reasons["G0"].append(Reason("subject_unresolved", None, "the request email did not resolve to one subject"))

    accounts = [v for v in verdicts if v.record_type == "USER_PROFILE"]
    if not accounts:
        reasons["G1"].append(Reason("account_not_established", None, "no account resource is tied to the subject by strict ownership"))
    for v in accounts:
        if not (v.kind == "COMPLIANT" and v.absence in ("explicit", "table_dump")) and v.kind != "VIOLATION":
            reasons["G1"].append(Reason("account_not_proven_absent", v.resource_id, v.state))

    for v in verdicts:
        if v.kind in ("VIOLATION", "RETAINED_MISSING"):
            reasons["G2"].append(Reason(v.reasons[0].code if v.reasons else "violation", v.resource_id, v.state))
        elif v.kind == "UNRESOLVED":
            reasons[_gate_of(v)] += list(v.reasons)

    for item in footprint.unresolved:
        if item.kind in policy.HIDING_RISK_KINDS:
            reasons["G8"].append(Reason(item.kind, item.resource_id, item.detail))
    for fact in footprint.unlinked_facts:
        if fact.record_type in policy.UNSUPPORTED and any(i.kind in policy.DIRECT_PII_KINDS for i in fact.identifiers):
            reasons["G6"].append(Reason("unmapped_document_with_direct_pii", fact.resource_id,
                                        f"{fact.record_type} in {fact.evidence_id} names the subject directly"))
    return tuple(GateResult(g, not reasons[g], tuple(reasons[g])) for g in GATES)


# -------------------------------------------------------------------------------- assessor


class Assessor:
    def __init__(self, store: EvidenceStore, activity: ActivityIndex):
        self.store, self.activity = store, activity

    def assess(self, case, resolution: Resolution, footprint: Footprint, intent: IntentResult) -> CaseAssessment:
        """``case`` is the request (``case_id`` and ``audit_at`` are used)."""
        result = CaseAssessment(case.case_id, "NOT_AUTHORIZED", intent, footprint.user_id, case.audit_at)
        if not intent.authorized:
            result.gates = evaluate_gates(intent, footprint, ())
            result.notes.append("request wording does not authorize deletion: " + (", ".join(intent.blockers) or intent.basis))
            return result
        verdicts = tuple(_verdict(o, footprint, self.store) for _, o in sorted(footprint.resources.items()))
        result.verdicts = verdicts
        result.gates = evaluate_gates(intent, footprint, verdicts)
        violated = any(v.kind in ("VIOLATION", "RETAINED_MISSING") for v in verdicts)
        result.assessment = ("PROVEN_VIOLATION" if violated else "PROVEN_COMPLETE" if all(g.passed for g in result.gates)
                             else "UNRESOLVED")
        result.notes += [f"{u.kind}: {u.detail}" for u in footprint.unresolved]
        for v in verdicts:
            if v.condition == "unestablished":
                result.notes.append(f"{v.resource_id}: rule condition '{v.condition}' unestablished ({v.condition_detail})")
            if v.kind == "UNRESOLVED":
                result.gaps += list(v.reasons)
        for fact in footprint.unlinked_facts:
            result.gaps.append(Reason("unlinked_fact", None, f"{fact.record_type} in {fact.evidence_id} names the subject but has no resource id"))
        return result
