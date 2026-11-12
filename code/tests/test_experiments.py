"""Experimental G1 alternatives: they must not alter production, must only ever turn UNRESOLVED into COMPLETE, and must
never waive the late-file or account-owned causes."""

import unittest

from assessment import GateResult
from evaluation import diagnostics as d
from evaluation.experiments import VARIANTS, ExperimentEngine, waive_g1
from loaders import load_dataset
from tests.decision_fixtures import LATER, Scenario, compliant_world, owned
from tests.footprint_fixtures import coverage
from tests.helpers import NEEDS_DATASET


def audit_record():
    return owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", "evidence_03", attrs={"retention_until": "2027-01-01T00:00:00Z"})


def waive(scenario, causes):
    assessment, footprint = scenario.assess()
    return waive_g1(assessment, footprint, scenario.world.store, frozenset(causes)), assessment


class WaiveTests(unittest.TestCase):
    def empty(self):
        return Scenario([audit_record()], coverages=[coverage("evidence_03", store="account_service/customer_profiles", rows=0)])

    def test_baseline_waives_nothing(self):
        done, a = waive(self.empty(), ())
        self.assertFalse(done)
        self.assertEqual(a.assessment, "UNRESOLVED")

    def test_an_empty_collection_completes_when_waived_and_the_steps_cite_their_proof(self):
        scenario = self.empty()
        assessment, footprint = scenario.assess()
        self.assertTrue(waive_g1(assessment, footprint, scenario.world.store, frozenset({d.EMPTY_STORE})))
        self.assertEqual((assessment.assessment, assessment.status), ("PROVEN_COMPLETE", "COMPLETE"))
        from planner import plan
        steps = plan(assessment, footprint).steps
        self.assertEqual([(s["operation"], s["evidence_id"]) for s in steps], [("VERIFY_RECORD_PRESENT", "evidence_03")])

    def test_a_cause_outside_the_waived_set_is_untouched(self):
        done, a = waive(self.empty(), {d.NO_EVIDENCE})
        self.assertFalse(done)
        self.assertEqual(a.assessment, "UNRESOLVED")

    def test_a_case_with_nothing_to_verify_is_not_completed_with_an_empty_answer(self):
        scenario = Scenario([], coverages=[])
        done, a = waive(scenario, set(d.CAUSES))
        self.assertFalse(done)
        self.assertEqual(a.assessment, "UNRESOLVED")

    def test_the_late_file_and_account_owned_causes_are_never_waivable_by_any_variant(self):
        late = Scenario([owned(state="FOUND", evidence_id="evidence_09", captured=LATER, observed="2026-09-09T00:00:00Z")])
        self.assertFalse(waive(late, set(d.CAUSES))[0])
        unknown = Scenario([owned("account_1", state="UNKNOWN")])
        self.assertFalse(waive(unknown, set(d.CAUSES))[0])
        for variant in VARIANTS.values():
            self.assertFalse({d.ACCOUNT_OWNED, d.LATE_FILE} & variant.waive, variant.name)

    def test_a_proven_violation_is_never_turned_into_complete(self):
        scenario = Scenario([audit_record(), owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_04")],
                            coverages=[coverage("evidence_03", store="account_service/customer_profiles", rows=0)])
        done, a = waive(scenario, set(d.CAUSES))
        self.assertFalse(done)
        self.assertEqual(a.assessment, "PROVEN_VIOLATION")

    def test_another_failed_gate_still_blocks_complete(self):
        scenario = Scenario([audit_record(), owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03")],
                            coverages=[coverage("evidence_03", store="account_service/customer_profiles", rows=0)])
        done, a = waive(scenario, {d.EMPTY_STORE})
        self.assertFalse(done)
        self.assertEqual((a.assessment, [g.gate for g in a.failed_gates]), ("UNRESOLVED", ["G7"]))

    def test_variant_waiver_sets_are_nested(self):
        sets = [VARIANTS[n].waive for n in ("baseline", "empty_store", "store_absence", "closed_world")]
        for smaller, larger in zip(sets, sets[1:]):
            self.assertLess(smaller, larger)

    def test_only_record_time_is_marked_contract_excluded(self):
        self.assertEqual([n for n, v in VARIANTS.items() if not v.contract_supported], ["record_time"])


@NEEDS_DATASET
class ProductionUnchangedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engine import DecisionEngine
        cls.ds = load_dataset()
        cls.production = DecisionEngine(cls.ds)
        cls.cases = cls.ds.requests + cls.ds.sample_requests
        cls.base = {c.case_id: cls.production.decide(c) for c in cls.cases}
        cls.variants = {n: ExperimentEngine(cls.ds, cls.production.store, n) for n in VARIANTS}

    def test_the_baseline_variant_reproduces_production_exactly(self):
        engine = self.variants["baseline"]
        for case in self.cases:
            self.assertEqual(engine.decide(case).steps_json, self.base[case.case_id].steps_json, case.case_id)

    def test_contract_supported_variants_only_turn_unresolved_into_complete(self):
        for name, engine in self.variants.items():
            if not VARIANTS[name].contract_supported:
                continue
            for case in self.cases:
                got, before = engine.decide(case), self.base[case.case_id]
                if got.assessment.assessment != before.assessment.assessment:
                    self.assertEqual((before.assessment.assessment, got.assessment.assessment), ("UNRESOLVED", "PROVEN_COMPLETE"), (name, case.case_id))
                    self.assertTrue(got.steps and all(s["phase"] == "VERIFICATION" and s["evidence_id"] for s in got.steps), (name, case.case_id))
                else:
                    self.assertEqual(got.steps_json, before.steps_json, (name, case.case_id))

    def test_flipped_sets_are_nested_across_the_supported_variants(self):
        flipped = {n: {c.case_id for c in self.cases if self.variants[n].decide(c).status != self.base[c.case_id].status}
                   for n in ("empty_store", "store_absence", "closed_world")}
        self.assertLessEqual(flipped["empty_store"], flipped["store_absence"])
        self.assertLessEqual(flipped["store_absence"], flipped["closed_world"])

    def test_no_variant_ever_returns_an_empty_complete_answer(self):
        for name, engine in self.variants.items():
            for case in self.cases:
                got = engine.decide(case)
                self.assertFalse(got.status == "COMPLETE" and not got.steps, (name, case.case_id))

    def test_the_decision_modules_do_not_import_the_experiments(self):
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for name in ("engine.py", "assessment.py", "planner.py", "policy.py", "main.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("evaluation.experiments"):
                    self.assertEqual(name, "main.py")  # only the CLI may call the experiment path


if __name__ == "__main__":
    unittest.main()
