"""The scorer (on synthetic rows with known answers) and the report renderer."""

import unittest

from evaluation import scoring
from loaders import load_dataset
from tests.helpers import NEEDS_DATASET


def st(n, phase, op, state, system="svc", resource="r1", evidence=""):
    return {"step_no": n, "phase": phase, "system_id": system, "resource_id": resource, "operation": op, "state": state,
            "evidence_id": evidence}


REF_STEPS = [st(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", evidence="evidence_01"),
             st(2, "REMEDIATION", "DELETE_RECORD", "ABSENT"), st(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT")]


def renumber(steps):
    return [dict(s, step_no=i) for i, s in enumerate(steps, 1)]


class ScoringTests(unittest.TestCase):
    def score(self, predicted_steps, predicted_status="INCOMPLETE"):
        return scoring.score({"c": ("INCOMPLETE", REF_STEPS)}, {"c": (predicted_status, predicted_steps)})

    def test_a_perfect_prediction_scores_one_everywhere(self):
        m = self.score(REF_STEPS)
        self.assertEqual(m["status_accuracy"], 1.0)
        self.assertEqual((m["steps"]["precision"], m["steps"]["recall"], m["steps"]["f1"]), (1.0, 1.0, 1.0))
        self.assertEqual(m["sequence"]["exact_case_sequences"], 1)
        self.assertEqual(m["sequence"]["pairwise_order_agreement"], 1.0)
        self.assertEqual(m["citations"]["accuracy"], 1.0)
        self.assertTrue(all(v == 1.0 for v in m["fields"]["aligned_accuracy"].values()))

    def test_a_missing_step_lowers_recall_but_not_precision(self):
        m = self.score(renumber(REF_STEPS[:2]))
        self.assertEqual(m["steps"]["precision"], 1.0)
        self.assertAlmostEqual(m["steps"]["recall"], 2 / 3)
        self.assertEqual(m["sequence"]["exact_case_sequences"], 0)
        self.assertEqual(m["sequence"]["pairwise_order_agreement"], 1.0)

    def test_an_extra_step_lowers_precision_but_not_recall(self):
        extra = REF_STEPS + [st(4, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", resource="r2")]
        m = self.score(extra)
        self.assertEqual(m["steps"]["recall"], 1.0)
        self.assertAlmostEqual(m["steps"]["precision"], 3 / 4)

    def test_wrong_order_is_seen_by_the_sequence_metrics_only(self):
        m = self.score(renumber([REF_STEPS[2], REF_STEPS[1], REF_STEPS[0]]))
        self.assertEqual(m["steps"]["f1"], 1.0)
        self.assertEqual(m["sequence"]["exact_case_sequences"], 0)
        self.assertLess(m["sequence"]["pairwise_order_agreement"], 1.0)

    def test_a_wrong_system_id_is_a_field_error_not_a_matched_step(self):
        wrong = [dict(s, system_id="other") if s["phase"] == "REMEDIATION" else s for s in REF_STEPS]
        m = self.score(wrong)
        self.assertAlmostEqual(m["steps"]["recall"], 2 / 3)
        self.assertAlmostEqual(m["fields"]["aligned_accuracy"]["system_id"], 2 / 3)
        self.assertEqual(m["fields"]["aligned"], 3)

    def test_a_wrong_citation_keeps_the_step_but_not_the_citation(self):
        wrong = [dict(s, evidence_id="evidence_09") if s["phase"] == "FINDING" else s for s in REF_STEPS]
        m = self.score(wrong)
        self.assertEqual(m["steps"]["recall"], 1.0)
        self.assertEqual(m["citations"]["accuracy"], 0.0)
        self.assertAlmostEqual(m["steps"]["recall_with_citation"], 2 / 3)

    def test_status_confusion(self):
        m = scoring.score({"a": ("COMPLETE", []), "b": ("INCOMPLETE", [])}, {"a": ("INCOMPLETE", []), "b": ("INCOMPLETE", [])})
        self.assertEqual(m["status_correct"], 1)
        self.assertEqual(m["confusion"], {"COMPLETE -> INCOMPLETE": 1, "INCOMPLETE -> INCOMPLETE": 1})

    def test_a_missing_prediction_counts_as_wrong(self):
        m = scoring.score({"a": ("INCOMPLETE", REF_STEPS)}, {})
        self.assertEqual((m["status_correct"], m["steps"]["recall"]), (0, 0.0))

    def test_empty_inputs_do_not_divide_by_zero(self):
        m = scoring.score({"a": ("NOT_APPLICABLE", [])}, {"a": ("NOT_APPLICABLE", [])})
        self.assertEqual(m["status_accuracy"], 1.0)


@NEEDS_DATASET
class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from evaluation.report import render
        from evaluation.samples import evaluate_samples
        cls.ds = load_dataset()
        cls.result = evaluate_samples(cls.ds)
        cls.text = render(cls.result, cls.ds)

    def test_the_samples_are_scored_with_no_invariant_violations(self):
        self.assertEqual(self.result["problems"], [])
        self.assertEqual(self.result["all"]["cases"], 32)
        self.assertEqual(self.result["development"]["cases"] + self.result["blind"]["cases"], 32)

    def test_the_report_contains_every_required_section(self):
        for heading in ("Public-sample accuracy", "Why steps are missed", "Internal assessments", "Custom evaluation cases",
                        "Unresolved evidence gaps", "Failures found and improvements made", "Known limitations"):
            self.assertIn(heading, self.text)
        for metric in ("Status accuracy", "Step precision", "Step recall", "Exact sequences", "Citations", "Field level"):
            self.assertIn(metric, self.text)

    def test_the_report_runs_the_custom_cases_and_all_pass(self):
        self.assertNotIn("**NO**", self.text)

    def test_the_report_leaks_nothing_it_should_not(self):
        lowered = self.text.lower()
        for word in ("api_key", "password", "secret", "@gmail.com", "@outlook.com"):
            self.assertNotIn(word, lowered)

    def test_the_report_is_reproducible(self):
        from evaluation.report import render
        from evaluation.samples import evaluate_samples
        again = render(evaluate_samples(self.ds), self.ds)
        self.assertEqual(again, self.text)


if __name__ == "__main__":
    unittest.main()
