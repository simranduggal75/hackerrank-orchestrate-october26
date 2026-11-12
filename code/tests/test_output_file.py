"""The generated submission file, checked by code that does not reuse ``validate_output`` or ``submission``.

Skipped when ``output.csv`` has not been generated. Every rule is taken from problem_statement.md / AGENTS.md.
"""

import csv
import json
import unittest
from pathlib import Path

from tests.helpers import NEEDS_DATASET

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT = ROOT / "output.csv"
DATASET = ROOT / "dataset"

PAIRS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"), "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"), "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"), "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"), "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"), "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"), "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"), "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OP_ORDER = list(PAIRS)  # the contract table order
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


@unittest.skipUnless(OUTPUT.exists() and (DATASET / "deletion_requests.csv").exists(), "output.csv not generated")
class OutputFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = read_csv(OUTPUT)
        cls.header, cls.rows = raw[0], raw[1:]
        with open(DATASET / "deletion_requests.csv", encoding="utf-8", newline="") as handle:
            cls.requests = {r["case_id"]: r for r in csv.DictReader(handle)}
        cls.times = {}
        with open(DATASET / "evidence_files.csv", encoding="utf-8", newline="") as handle:
            cls.times.update({r["file_id"]: r["captured_at"] for r in csv.DictReader(handle)})
        with open(DATASET / "system_activity.csv", encoding="utf-8", newline="") as handle:
            cls.times.update({r["activity_id"]: r["event_time"] for r in csv.DictReader(handle)})
        with open(DATASET / "user_audit_log.csv", encoding="utf-8", newline="") as handle:
            cls.times.update({r["audit_id"]: r["event_time"] for r in csv.DictReader(handle)})

    def steps(self, row):
        return json.loads(row[2])

    def test_header_and_row_count(self):
        self.assertEqual(self.header, ["case_id", "deletion_status", "steps_json"])
        self.assertEqual(len(self.rows), 518)
        self.assertTrue(all(len(r) == 3 for r in self.rows))

    def test_one_row_per_request_sorted_by_case_id(self):
        ids = [r[0] for r in self.rows]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(set(ids), set(self.requests))
        self.assertEqual(len(ids), len(set(ids)))

    def test_statuses_and_json(self):
        for row in self.rows:
            self.assertIn(row[1], ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"), row[0])
            self.assertIsInstance(self.steps(row), list, row[0])

    def test_not_applicable_rows_are_an_empty_array_and_complete_rows_never_are(self):
        for row in self.rows:
            if row[1] == "NOT_APPLICABLE":
                self.assertEqual(row[2], "[]", row[0])
            if row[1] == "COMPLETE":
                self.assertNotEqual(row[2], "[]", row[0])

    def test_an_incomplete_row_with_no_steps_is_an_unresolved_case_with_nothing_owned(self):
        # Schema-valid but thin: reported as a limitation. It must be a small minority, never invented content.
        empty = [r for r in self.rows if r[1] == "INCOMPLETE" and r[2] == "[]"]
        self.assertLess(len(empty), len(self.rows) // 4)

    def test_step_schema_pairs_numbering_and_order(self):
        for row in self.rows:
            previous = None
            for n, step in enumerate(self.steps(row), start=1):
                self.assertEqual(list(step), FIELDS, row[0])
                self.assertEqual(step["step_no"], n, row[0])
                self.assertEqual((step["phase"], step["state"]), PAIRS[step["operation"]], (row[0], n))
                self.assertTrue(step["system_id"] and step["resource_id"], (row[0], n))
                key = (PHASE_ORDER[step["phase"]], OP_ORDER.index(step["operation"]), step["system_id"], step["resource_id"])
                if previous is not None:
                    self.assertLessEqual(previous, key, (row[0], n))
                previous = key

    def test_citations_exist_are_not_after_the_audit_and_follow_the_status(self):
        for row in self.rows:
            audit_at = self.requests[row[0]]["audit_at"]
            for step in self.steps(row):
                cite = step["evidence_id"]
                if cite:
                    self.assertIn(cite, self.times, (row[0], cite))
                    self.assertLessEqual(self.times[cite], audit_at, (row[0], cite))
                if step["phase"] == "FINDING":
                    self.assertTrue(cite, row[0])
                if row[1] == "INCOMPLETE" and step["phase"] != "FINDING":
                    self.assertEqual(cite, "", row[0])
                if row[1] == "COMPLETE":
                    self.assertEqual(step["phase"], "VERIFICATION", row[0])
                    self.assertTrue(cite, row[0])

    def test_no_complete_row_without_evidence_backed_verification(self):
        complete = [r for r in self.rows if r[1] == "COMPLETE"]
        self.assertTrue(complete)
        for row in complete:
            steps = self.steps(row)
            self.assertTrue(steps, row[0])
            self.assertTrue(all(s["phase"] == "VERIFICATION" and s["evidence_id"] for s in steps), row[0])

    def test_an_incomplete_row_reports_a_finding_or_is_an_unresolved_verification_plan(self):
        for row in self.rows:
            if row[1] == "INCOMPLETE":
                self.assertTrue(all(s["phase"] in PHASE_ORDER for s in self.steps(row)), row[0])

    def test_the_file_uses_unix_line_endings_and_utf8(self):
        data = OUTPUT.read_bytes()
        self.assertNotIn(b"\r\n", data)
        data.decode("utf-8")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(OUTPUT.exists() and (DATASET / "deletion_requests.csv").exists(), "output.csv not generated")
class OutputConsistencyTests(unittest.TestCase):
    """output.csv must be what the current code produces, and the written reports must describe it."""

    def test_the_file_is_exactly_what_the_production_engine_produces_now(self):
        import submission
        from loaders import load_dataset
        self.assertEqual(submission.read_output(OUTPUT), submission.build_rows(load_dataset()))

    def test_the_usage_and_evaluation_reports_quote_the_actual_file(self):
        import hashlib
        from collections import Counter
        digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
        counts = Counter(row[1] for row in read_csv(OUTPUT)[1:])
        usage = (ROOT / "code" / "evaluation" / "usage_report.md").read_text(encoding="utf-8")
        report = (ROOT / "code" / "evaluation" / "evaluation_report.md").read_text(encoding="utf-8")
        self.assertIn(digest, usage)
        self.assertIn(digest, report)
        self.assertIn(f"COMPLETE {counts['COMPLETE']}, INCOMPLETE {counts['INCOMPLETE']}, NOT_APPLICABLE {counts['NOT_APPLICABLE']}", usage)
        self.assertIn(f"COMPLETE {counts['COMPLETE']}", report)
