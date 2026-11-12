"""Synthetic scenarios for the assessment, planner and custom evaluation cases.

Each scenario is a small world (one subject, a handful of evidence facts, optional activity rows) decided by
the real ``Assessor`` and ``plan``. Resource ids and identifiers are test data only.
"""

from assessment import Assessor
from engine import DecisionEngine
from intent import classify_intent
from loaders import Request
from planner import plan
from tests.footprint_fixtures import AUDIT, EARLY, World, fact, ident

DELETE_TEXT = "Please delete my account and all of my personal data."
SAM = ident("email", "sam.lee@example.test")
LATER = "2026-10-05T00:00:00Z"  # after the default audit time


def owned(resource="account_1", record_type="USER_PROFILE", state="FOUND", evidence_id="evidence_01", **kw):
    """A fact that names the subject by exact email, so strict ownership links it."""
    kw.setdefault("idents", (SAM,))
    return fact(evidence_id, resource, record_type, state, **kw)


def request(text=DELETE_TEXT, audit_at=AUDIT, email="sam.lee@example.test", case_id="case_x"):
    return Request(case_id, email, "2026-08-30T00:00:00Z", "COMPLETE", "2026-09-01T00:00:00Z", audit_at, text)


class Scenario:
    def __init__(self, facts=(), coverages=(), activity=(), text=DELETE_TEXT, audit_at=AUDIT):
        self.world = World(list(facts), list(coverages), list(activity))
        self.case = request(text, audit_at)
        self.assessor = Assessor(self.world.store, self.world.activity)

    def footprint(self):
        resolution = self.world.identity.resolve_request(self.case)
        return resolution, self.world.builder.build(resolution, "strict")

    def assess(self):
        intent = classify_intent(self.case.request_text)
        resolution, footprint = self.footprint()
        if not intent.authorized:
            from footprint import Footprint
            footprint = Footprint(None, self.case.audit_at, "strict")
        return self.assessor.assess(self.case, resolution, footprint, intent), footprint

    def decide(self):
        assessment, footprint = self.assess()
        return plan(assessment, footprint)


def compliant_world():
    """An account proven absent and nothing else: the smallest world that passes the gate."""
    return [owned(state="NOT_FOUND", evidence_id="evidence_01", captured=EARLY)]
