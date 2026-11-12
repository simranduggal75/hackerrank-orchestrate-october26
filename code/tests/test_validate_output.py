"""The output invariants, exercised with both valid and deliberately broken rows."""

import json
import unittest

from loaders import load_dataset
from tests.helpers import NEEDS_DATASET
from validate_output import HEADER, Validator

AUDIT = "2026-09-30T00:00:00Z"


def s(n, phase, op, state, system="account_service", resource="account_1", evidence=""):
    return {"step_no": n, "phase": phase, "system_id": system, "resource_id": resource, "operation": op, "state": state,
            "evidence_id": evidence}


@NEEDS_DATASET
class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.v = Validator(cls.ds)
        cls.early = min(cls.ds.evidence_files, key=lambda e: e.captured_at)
        cls.eid = cls.early.file_id
        cls.late_audit = "2099-01-01T00:00:00Z"

    def check(self, status, steps, audit_at=None):
        return self.v.check_row("case_x", status, json.dumps(steps), audit_at or self.late_audit)

    def good_incomplete(self):
        return [s(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", evidence=self.eid),
                s(2, "REMEDIATION", "DELETE_RECORD", "ABSENT"), s(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT")]

    def test_header(self):
        self.assertEqual(HEADER, ["case_id", "deletion_status", "steps_json"])

    def test_valid_rows_pass(self):
        self.assertEqual(self.check("INCOMPLETE", self.good_incomplete()), [])
        self.assertEqual(self.check("COMPLETE", [s(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", evidence=self.eid)]), [])
        self.assertEqual(self.check("NOT_APPLICABLE", []), [])

    def test_bad_status_json_and_shape(self):
        self.assertTrue(self.v.check_row("c", "DONE", "[]", AUDIT))
        self.assertTrue(self.v.check_row("c", "INCOMPLETE", "{not json", AUDIT))
        self.assertTrue(self.v.check_row("c", "INCOMPLETE", '{"a": 1}', AUDIT))
        self.assertTrue(self.check("NOT_APPLICABLE", self.good_incomplete()))

    def test_extra_or_missing_fields(self):
        steps = self.good_incomplete()
        steps[0]["note"] = "x"
        self.assertTrue(self.check("INCOMPLETE", steps))
        steps = self.good_incomplete()
        del steps[1]["state"]
        self.assertTrue(self.check("INCOMPLETE", steps))

    def test_step_numbers_must_be_consecutive_from_one(self):
        steps = self.good_incomplete()
        steps[1]["step_no"] = 5
        self.assertTrue(self.check("INCOMPLETE", steps))

    def test_phase_operation_state_must_be_an_allowed_triple(self):
        steps = self.good_incomplete()
        steps[1]["state"] = "PRESENT"
        self.assertTrue(self.check("INCOMPLETE", steps))
        steps = self.good_incomplete()
        steps[1]["operation"] = "WIPE_EVERYTHING"
        self.assertTrue(self.check("INCOMPLETE", steps))

    def test_duplicates_and_order(self):
        steps = self.good_incomplete() + [dict(self.good_incomplete()[2], step_no=4)]
        self.assertTrue(any("duplicates" in p for p in self.check("INCOMPLETE", steps)))
        steps = self.good_incomplete()
        steps[0], steps[2] = dict(steps[2], step_no=1), dict(steps[0], step_no=3)
        self.assertTrue(any("out of order" in p for p in self.check("INCOMPLETE", steps)))

    def test_empty_ids_are_rejected(self):
        steps = self.good_incomplete()
        steps[1]["system_id"] = ""
        self.assertTrue(self.check("INCOMPLETE", steps))

    def test_citation_rules(self):
        steps = self.good_incomplete()
        steps[0]["evidence_id"] = ""
        self.assertTrue(any("must cite" in p for p in self.check("INCOMPLETE", steps)))
        steps = self.good_incomplete()
        steps[1]["evidence_id"] = self.eid
        self.assertTrue(any("empty evidence_id" in p for p in self.check("INCOMPLETE", steps)))
        steps = self.good_incomplete()
        steps[0]["evidence_id"] = "evidence_99999"
        self.assertTrue(any("not a known" in p for p in self.check("INCOMPLETE", steps)))
        complete = [s(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", evidence="")]
        self.assertTrue(any("must cite" in p for p in self.check("COMPLETE", complete)))
        findings_in_complete = [s(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", evidence=self.eid)]
        self.assertTrue(any("only verification" in p for p in self.check("COMPLETE", findings_in_complete)))

    def test_evidence_captured_after_the_audit_cannot_be_cited(self):
        steps = self.good_incomplete()
        before = self.v.check_row("c", "INCOMPLETE", json.dumps(steps), "1990-01-01T00:00:00Z")
        self.assertTrue(any("after audit_at" in p for p in before))

    def test_row_level_checks(self):
        rows = [("case_1", "COMPLETE", "[]"), ("case_2", "COMPLETE", "[]")]
        self.assertEqual(Validator.check_rows(rows, ["case_1", "case_2"]), [])
        self.assertTrue(Validator.check_rows(rows, ["case_1", "case_2", "case_3"]))
        self.assertTrue(Validator.check_rows(rows + rows[:1], ["case_1", "case_2"]))
        self.assertTrue(Validator.check_rows(list(reversed(rows)), ["case_1", "case_2"]))


if __name__ == "__main__":
    unittest.main()
