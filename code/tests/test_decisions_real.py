"""Output invariants over every real request (518 evaluation + 32 sample), and general-purpose guards.

Nothing here compares against the sample answers; that is the evaluation's job. These are properties that
must hold for ANY dataset: valid rows, temporal safety, determinism, no dependence on reported status.
"""

import ast
import dataclasses
import json
import re
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import policy
from engine import DecisionEngine
from loaders import load_dataset
from tests.helpers import NEEDS_DATASET
from validate_output import Validator

CODE = Path(__file__).resolve().parent.parent
DECISION_MODULES = ("policy.py", "assessment.py", "planner.py", "engine.py", "intent.py", "footprint.py", "state.py",
                    "identity.py", "activity.py")


@NEEDS_DATASET
class RealDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.engine = DecisionEngine(cls.ds)
        cls.cases = cls.ds.requests + cls.ds.sample_requests
        cls.decisions = {c.case_id: cls.engine.decide(c) for c in cls.cases}
        cls.validator = Validator(cls.ds)

    def test_there_is_one_decision_per_request(self):
        self.assertEqual(len(self.cases), 550)
        self.assertEqual(len(self.decisions), 550)
        self.assertEqual(len(self.ds.requests), 518)

    def test_every_row_satisfies_the_output_contract(self):
        problems = [p for c in self.cases for p in self.validator.check_row(
            c.case_id, self.decisions[c.case_id].status, self.decisions[c.case_id].steps_json, c.audit_at)]
        self.assertEqual(problems, [])

    def test_status_matches_the_internal_assessment(self):
        mapping = {"NOT_AUTHORIZED": "NOT_APPLICABLE", "PROVEN_VIOLATION": "INCOMPLETE", "UNRESOLVED": "INCOMPLETE",
                   "PROVEN_COMPLETE": "COMPLETE"}
        for d in self.decisions.values():
            self.assertEqual(d.status, mapping[d.assessment.assessment], d.case_id)

    def test_not_applicable_rows_are_empty_and_only_those(self):
        for d in self.decisions.values():
            self.assertEqual(d.status == "NOT_APPLICABLE", not d.assessment.intent.authorized, d.case_id)
            if d.status == "NOT_APPLICABLE":
                self.assertEqual(d.steps_json, "[]", d.case_id)

    def test_a_complete_case_has_every_gate_passed_and_no_violation(self):
        for d in self.decisions.values():
            if d.status == "COMPLETE":
                self.assertEqual(d.assessment.failed_gates, [], d.case_id)
                self.assertTrue(d.steps, d.case_id)
                self.assertFalse([v for v in d.assessment.verdicts if v.kind != "COMPLIANT"], d.case_id)

    def test_a_proven_violation_reports_at_least_one_finding_and_unresolved_reports_none_for_unresolved_resources(self):
        for d in self.decisions.values():
            findings = [s for s in d.steps if s["phase"] == "FINDING"]
            if d.assessment.assessment == "PROVEN_VIOLATION":
                self.assertTrue(findings, d.case_id)
            unresolved = {v.resource_id for v in d.assessment.verdicts if v.kind == "UNRESOLVED"}
            self.assertFalse([s for s in d.steps if s["resource_id"] in unresolved and s["phase"] != "VERIFICATION"], d.case_id)

    def test_every_step_resource_belongs_to_the_subjects_footprint(self):
        for case in self.cases:
            d = self.decisions[case.case_id]
            _, footprint = self.engine.assess(case)
            self.assertTrue({s["resource_id"] for s in d.steps} <= set(footprint.resources), case.case_id)

    def test_cited_evidence_is_never_after_the_audit(self):
        files = {e.file_id: e.captured_at for e in self.ds.evidence_files}
        for case in self.cases:
            for s in self.decisions[case.case_id].steps:
                if s["evidence_id"] in files:
                    self.assertLessEqual(files[s["evidence_id"]], case.audit_at, (case.case_id, s["evidence_id"]))

    def test_decisions_are_deterministic(self):
        again = DecisionEngine(self.ds, self.engine.store)
        for case in self.cases[::7]:
            self.assertEqual(again.decide(case).steps_json, self.decisions[case.case_id].steps_json, case.case_id)

    def test_reported_status_and_case_id_do_not_influence_the_decision(self):
        for case in self.cases[::11]:
            other = dataclasses.replace(case, case_id="case_zzz", reported_status="NOT_STARTED", reported_at="2000-01-01T00:00:00Z")
            self.assertEqual(self.engine.decide(other).steps_json, self.decisions[case.case_id].steps_json, case.case_id)

    def test_all_assessment_kinds_occur_and_none_is_empty_by_accident(self):
        kinds = Counter(d.assessment.assessment for d in self.decisions.values())
        self.assertEqual(set(kinds), {"NOT_AUTHORIZED", "PROVEN_VIOLATION", "UNRESOLVED", "PROVEN_COMPLETE"})
        for d in self.decisions.values():
            if d.assessment.assessment in ("PROVEN_VIOLATION", "PROVEN_COMPLETE"):
                self.assertTrue(d.steps, d.case_id)

    def test_rows_sort_by_case_id_as_plain_strings(self):
        ids = sorted(c.case_id for c in self.ds.requests)
        self.assertEqual(Validator.check_rows([(i, "COMPLETE", "[]") for i in ids], ids), [])

    def test_a_case_whose_email_repeats_is_decided_per_case(self):
        by_email = {}
        for c in self.ds.requests:
            by_email.setdefault(c.email.lower(), []).append(c)
        repeated = [cs for cs in by_email.values() if len(cs) > 1]
        for group in repeated[:5]:
            for c in group:
                self.assertIn(c.case_id, self.decisions)


@NEEDS_DATASET
class AntiHardcodingTests(unittest.TestCase):
    """The decision code names no case, user, evidence, activity, resource or email, and never reads answers."""

    ID_LITERAL = re.compile(r"^(case|user|evidence|activity|audit|account|object|support|tombstone|audit_record|"
                            r"backup|sync|search|export|analytics|marketing|legal|hold)_\d+$")
    EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    FORBIDDEN_WORDS = ("golden", "organizer", "generator", "sample_output", "sample_deletion", "ground_truth")

    @staticmethod
    def string_literals(path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                yield node.value

    def literals(self):
        for name in DECISION_MODULES:
            for text in self.string_literals(CODE / name):
                yield name, text

    def test_no_literal_looks_like_a_dataset_identifier_or_an_email(self):
        for name, text in self.literals():
            self.assertIsNone(self.ID_LITERAL.match(text), (name, text))
            self.assertIsNone(self.EMAIL.fullmatch(text), (name, text))

    def test_no_literal_equals_any_real_identifier_or_value_in_the_dataset(self):
        ds = load_dataset()
        real = {c.case_id for c in ds.requests + ds.sample_requests} | {c.email.lower() for c in ds.requests + ds.sample_requests}
        real |= {i.user_id for i in ds.identities} | {i.email.lower() for i in ds.identities} | {e.file_id for e in ds.evidence_files}
        real |= {a.activity_id for a in ds.activity} | {a.audit_id for a in ds.audit_log} | {a.resource_id for a in ds.activity}
        for name, text in self.literals():
            self.assertNotIn(text, real, (name, text))

    def test_no_decision_module_names_organizer_or_answer_files(self):
        for name, text in self.literals():
            for word in self.FORBIDDEN_WORDS:
                self.assertNotIn(word, text.lower(), (name, text))

    def test_only_the_evaluation_reads_the_sample_answers(self):
        readers = [p for p in CODE.rglob("*.py") if "tests" not in p.parts and "sample_output" in p.read_text(encoding="utf-8")]
        self.assertTrue(readers)
        for path in readers:  # scoring code only: never a decision module, the engine, the loaders or the CLI
            self.assertIn("evaluation", path.relative_to(CODE).parts[:-1], path.name)
            self.assertNotIn(path.name, DECISION_MODULES)

    def test_no_decision_module_imports_the_evaluation_package(self):
        for name in DECISION_MODULES:
            tree = ast.parse((CODE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("evaluation"), (name, node.module))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.startswith("evaluation") for a in node.names), name)


class PolicyMutationTests(unittest.TestCase):
    """The tests are sensitive to the policy table: each mutation must change an outcome."""

    def decide(self, facts, text=None):
        from tests.decision_fixtures import Scenario
        return Scenario(facts).decide() if text is None else Scenario(facts, text=text).decide()

    def mutated(self, record_type, **changes):
        return mock.patch.dict(policy.CATEGORIES, {record_type: dataclasses.replace(policy.CATEGORIES[record_type], **changes)})

    def test_dropping_the_violating_state_removes_the_finding(self):
        from tests.decision_fixtures import owned
        facts = [owned(state="FOUND")]
        self.assertEqual(self.decide(facts).status, "INCOMPLETE")
        with self.mutated("USER_PROFILE", violating=frozenset()):
            self.assertNotIn("FINDING", {s["phase"] for s in self.decide(facts).steps})

    def test_changing_the_remediation_operation_changes_the_plan(self):
        from tests.decision_fixtures import owned
        facts = [owned(state="FOUND")]
        with self.mutated("USER_PROFILE", remediation_op="REDACT_RECORD"):
            ops = [s["operation"] for s in self.decide(facts).steps]
        self.assertIn("REDACT_RECORD", ops)
        self.assertNotIn("DELETE_RECORD", ops)

    def test_treating_a_conditional_rule_as_unconditional_flips_unresolved_to_violation(self):
        from tests.decision_fixtures import compliant_world, owned
        facts = compliant_world() + [owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03")]
        self.assertEqual(self.decide(facts).assessment.assessment, "UNRESOLVED")
        with self.mutated("SUPPORT_MESSAGE", condition="always"):
            self.assertEqual(self.decide(facts).assessment.assessment, "PROVEN_VIOLATION")

    def test_removing_a_category_makes_its_records_unresolved_not_silently_compliant(self):
        from tests.decision_fixtures import compliant_world, owned
        facts = compliant_world() + [owned("export_1", "USER_EXPORT", "NOT_FOUND", "evidence_03")]
        self.assertEqual(self.decide(facts).assessment.assessment, "PROVEN_COMPLETE")
        trimmed = {k: v for k, v in policy.CATEGORIES.items() if k != "USER_EXPORT"}
        with mock.patch.object(policy, "CATEGORIES", trimmed):
            self.assertEqual(self.decide(facts).assessment.assessment, "UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
