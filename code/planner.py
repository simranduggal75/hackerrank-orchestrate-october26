"""Turn a case assessment into the contract's steps.

Phases and ordering (README): FINDING, REMEDIATION, VERIFICATION; within a phase the contract's operation
order, then ``system_id``, then ``resource_id``, both as plain ascending strings. The only reordering is an
evidence-backed dependency inside the remediation phase (AGENTS.md section 6.5: downstream copies before
their source records when the dependency requires it): a search document before the account it names as its
source. A replica is another store of the same system, so it shares the resource's step. Without such an
edge the plain sort stands.

Citations (problem_statement.md lines 91 and 127; AGENTS.md lines 258 and 314-315):
  * a finding cites the firm observation that shows the violation;
  * in a COMPLETE case every verification step cites the file proving the current compliant state;
  * in an INCOMPLETE case remediation steps and EVERY verification step are post-remediation checks and
    carry an empty evidence_id. Each check keeps an internal class (existing_compliant, post_remediation,
    unresolved_check) and the evidence that shows its current state, for the evaluation report.

Nothing is invented: a step is emitted only for a resource id that the footprint owns and a category the
policy table maps. Unmapped or unsupported evidence yields no step and is recorded as a gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import policy
from assessment import CaseAssessment, ResourceVerdict
from footprint import Footprint


@dataclass(frozen=True, slots=True)
class PlannedStep:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str
    check_class: str | None = None  # verification only
    supporting_evidence: str | None = None  # evidence for the CURRENT state, even when the citation is empty


@dataclass(slots=True)
class Decision:
    case_id: str
    status: str
    assessment: CaseAssessment
    planned: list[PlannedStep] = field(default_factory=list)

    @property
    def steps(self) -> list[dict]:
        return [{"step_no": n, "phase": s.phase, "system_id": s.system_id, "resource_id": s.resource_id,
                 "operation": s.operation, "state": s.state, "evidence_id": s.evidence_id}
                for n, s in enumerate(self.planned, start=1)]

    @property
    def steps_json(self) -> str:
        return json.dumps(self.steps, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------------------------ ordering

Edge = tuple[tuple[str, str], tuple[str, str]]  # ((system, resource) of the downstream copy, (system, resource) of its source)


def sort_key(step: PlannedStep):
    return (policy.PHASE_RANK[step.phase], policy.OP_RANK[step.operation], step.system_id, step.resource_id)


def order_steps(steps: list[PlannedStep], edges: set[Edge] = frozenset()) -> list[PlannedStep]:
    """The contract sort, then an evidence-backed dependency exception inside each remediation operation group."""
    ordered = sorted(steps, key=sort_key)
    if not edges:
        return ordered
    for _ in range(len(ordered) ** 2 + 1):  # bounded: edges could in principle form a cycle
        moved = False
        for down, src in sorted(edges):
            for i, a in enumerate(ordered):
                if a.phase != "REMEDIATION" or (a.system_id, a.resource_id) != src:
                    continue
                j = next((k for k, b in enumerate(ordered) if (b.system_id, b.resource_id) == down and b.phase == a.phase
                          and b.operation == a.operation), None)
                if j is not None and j > i:
                    ordered.insert(i, ordered.pop(j))
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    return ordered


def dependency_edges(verdicts: tuple[ResourceVerdict, ...], source_of: Callable[[str], str | None],
                     systems: dict[str, str]) -> set[Edge]:
    """Evidence-backed edges: a document -> the resource it names as its source."""
    edges: set[Edge] = set()
    for v in verdicts:
        primary = (v.system_id or "", v.resource_id)
        source = source_of(v.resource_id)
        if source and source in systems:
            edges.add((primary, (systems[source], source)))
    return edges


# -------------------------------------------------------------------------------------- planning


def plan(assessment: CaseAssessment, footprint: Footprint,
         source_of: Callable[[str], str | None] = lambda rid: None) -> Decision:
    decision = Decision(assessment.case_id, assessment.status, assessment)
    if assessment.assessment == "NOT_AUTHORIZED":
        return decision
    complete = assessment.status == "COMPLETE"
    steps: list[PlannedStep] = []
    for v in assessment.verdicts:
        entry = policy.CATEGORIES.get(v.record_type or "")
        if entry is None:
            continue
        if v.kind == "VIOLATION":
            for copy in v.copies:
                if entry.finding_op:
                    steps.append(_step("FINDING", copy.system_id, v.resource_id, entry.finding_op, copy.evidence_id))
                if entry.remediation_op:
                    steps.append(_step("REMEDIATION", copy.system_id, v.resource_id, entry.remediation_op, ""))
        if v.verify and entry.verify_op:
            steps += _verifications(v, entry, complete)
    systems = {v.resource_id: v.system_id or "" for v in assessment.verdicts}
    edges = dependency_edges(assessment.verdicts, source_of, systems)
    decision.planned = order_steps(_unique(steps), edges)
    return decision


def _step(phase, system_id, resource_id, operation, evidence_id, check_class=None, supporting=None) -> PlannedStep:
    return PlannedStep(phase, system_id, resource_id, operation, policy.OP_STATE[operation], evidence_id, check_class, supporting)


def _verifications(v: ResourceVerdict, entry: policy.Category, complete: bool) -> list[PlannedStep]:
    """Verification of the required end state, one step per copy that must be checked."""
    systems: dict[str, tuple[str, str | None]] = {}
    if v.kind == "VIOLATION":
        for copy in v.copies:
            systems[copy.system_id] = ("post_remediation", copy.evidence_id)
        if v.system_id and v.system_id not in systems:
            systems[v.system_id] = ("existing_compliant", v.compliant_evidence)  # the primary home copy
    else:
        systems[v.system_id or ""] = (v.verify_class or "existing_compliant", v.compliant_evidence)
    steps = []
    for system, (check_class, supporting) in sorted(systems.items()):
        cite = (supporting or "") if complete else ""  # INCOMPLETE: every verification is post-remediation
        if complete and not cite:
            raise ValueError(f"COMPLETE verification of {v.resource_id} has no citable evidence")
        steps.append(_step("VERIFICATION", system, v.resource_id, entry.verify_op, cite, check_class, supporting))
    return steps


def _unique(steps: list[PlannedStep]) -> list[PlannedStep]:
    seen, out = set(), []
    for s in steps:
        key = (s.phase, s.system_id, s.resource_id, s.operation)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out
