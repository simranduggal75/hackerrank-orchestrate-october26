"""The decision engine: request -> intent -> identity -> footprint -> assessment -> planned steps.

Strict temporal ownership is the only mode used here. Nothing is written to disk.
"""

from __future__ import annotations

from activity import ActivityIndex
from assessment import Assessor, CaseAssessment
from evidence.store import EvidenceStore, extract_all
from footprint import Footprint, FootprintBuilder
from identity import IdentityIndex
from intent import classify_intent
from loaders import Dataset, Request
from planner import Decision, plan


class DecisionEngine:
    def __init__(self, ds: Dataset, store: EvidenceStore | None = None):
        self.ds = ds
        self.identity = IdentityIndex(ds.identities, ds.audit_log)
        self.store = store or extract_all(ds.evidence_files)
        self.activity = ActivityIndex(ds.activity)
        self._footprints = FootprintBuilder(self.store, self.identity, self.activity)
        self._assessor = Assessor(self.store, self.activity)

    def _source_of(self, audit_at: str):
        def source_of(resource_id: str) -> str | None:
            for fact in self.store.facts_for_resource(resource_id, audit_at):
                if fact.attributes.get("source_resource_id"):
                    return fact.attributes["source_resource_id"]
            return None
        return source_of

    def assess(self, case: Request) -> tuple[CaseAssessment, Footprint]:
        intent = classify_intent(case.request_text)
        resolution = self.identity.resolve_request(case)
        footprint = (self._footprints.build(resolution, "strict") if intent.authorized
                     else Footprint(None, case.audit_at, "strict"))
        return self._assessor.assess(case, resolution, footprint, intent), footprint

    def decide(self, case: Request) -> Decision:
        assessment, footprint = self.assess(case)
        return plan(assessment, footprint, self._source_of(case.audit_at))

    def decide_all(self, cases: list[Request]) -> dict[str, Decision]:
        return {case.case_id: self.decide(case) for case in cases}
