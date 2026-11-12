"""Loader tests. Run from the repo root:  python -m unittest discover -s code/tests -t code"""

import tempfile
import unittest
from pathlib import Path

from loaders import (
    DATASET_DIR, SPECS, SchemaError, check_output_template, load_dataset, load_records,
    read_rows, validate_timestamp,
)

REQ_HEADER = "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text"
GOOD_REQ = "case_1,a@x.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-02T00:00:00Z,2026-01-03T00:00:00Z,Please delete"


class CsvCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> None:
        (self.dir / name).write_bytes(text.encode(encoding))

    def load_requests(self, *rows: str):
        self.write("deletion_requests.csv", "\n".join([REQ_HEADER, *rows]) + "\n")
        return load_records(SPECS["requests"], self.dir)


class ReadRowsTests(CsvCase):
    def test_valid_file_loads(self):
        records = self.load_requests(GOOD_REQ)
        self.assertEqual(records[0].case_id, "case_1")
        self.assertEqual(records[0].audit_at, "2026-01-03T00:00:00Z")

    def test_non_ascii_text_roundtrips_as_utf8(self):
        text = "Intenté eliminar mi cuenta; 削除してください; suppression définitive"
        records = self.load_requests(GOOD_REQ.replace("Please delete", f'"{text}"'))
        self.assertEqual(records[0].request_text, text)

    def test_bom_is_tolerated(self):
        self.write("deletion_requests.csv", "﻿" + REQ_HEADER + "\n" + GOOD_REQ + "\n")
        self.assertEqual(len(load_records(SPECS["requests"], self.dir)), 1)

    def test_quoted_commas_and_newlines_stay_in_one_cell(self):
        records = self.load_requests(GOOD_REQ.replace("Please delete", '"Hi, please\ndelete"'))
        self.assertEqual(records[0].request_text, "Hi, please\ndelete")

    def test_invalid_utf8_is_rejected(self):
        self.write("deletion_requests.csv", REQ_HEADER + "\n" + GOOD_REQ + "\n", "utf-8")
        (self.dir / "deletion_requests.csv").write_bytes(
            (REQ_HEADER + "\n" + GOOD_REQ.replace("delete", "d\xe9l\xe8te") + "\n").encode("latin-1"))
        with self.assertRaises(SchemaError):
            load_records(SPECS["requests"], self.dir)

    def test_missing_file(self):
        with self.assertRaises(SchemaError):
            read_rows(self.dir / "nope.csv", ["a"])

    def test_wrong_header(self):
        self.write("deletion_requests.csv", "case_id,mail\nx,y\n")
        with self.assertRaisesRegex(SchemaError, "header"):
            load_records(SPECS["requests"], self.dir)

    def test_reordered_header_is_rejected(self):
        self.write("deletion_requests.csv", "email,case_id,requested_at,reported_status,reported_at,audit_at,request_text\n")
        with self.assertRaisesRegex(SchemaError, "header"):
            load_records(SPECS["requests"], self.dir)

    def test_short_row(self):
        with self.assertRaisesRegex(SchemaError, "fewer cells"):
            self.load_requests("case_1,a@x.com")

    def test_long_row(self):
        with self.assertRaisesRegex(SchemaError, "more cells"):
            self.load_requests(GOOD_REQ + ",extra")


class ValidationTests(CsvCase):
    def test_bad_timestamp_formats(self):
        # Unpadded values parse with strptime but would break lexicographic time ordering.
        for bad in ("2026-01-03", "2026-01-03T00:00:00", "2026-13-03T00:00:00Z", "2026-02-30T00:00:00Z", "",
                    "2026-1-3T0:0:0Z", "2026-01-03T00:00:00+00:00"):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate_timestamp(bad, "t")
        self.assertEqual(validate_timestamp("2026-01-03T00:00:00Z", "t"), "2026-01-03T00:00:00Z")

    def test_bad_timestamp_in_file(self):
        with self.assertRaisesRegex(SchemaError, "audit_at"):
            self.load_requests(GOOD_REQ.replace("2026-01-03T00:00:00Z", "yesterday"))

    def test_unknown_reported_status(self):
        with self.assertRaisesRegex(SchemaError, "reported_status"):
            self.load_requests(GOOD_REQ.replace("COMPLETE", "DONE"))

    def test_duplicate_key(self):
        with self.assertRaisesRegex(SchemaError, "duplicate case_id"):
            self.load_requests(GOOD_REQ, GOOD_REQ)

    def test_blank_required_column(self):
        with self.assertRaisesRegex(SchemaError, "blank required"):
            self.load_requests(GOOD_REQ.replace("a@x.com", ""))

    def test_rule_action_enum(self):
        self.write("deletion_rules.csv", "rule_id,data_category,condition,required_action\nr1,X,Always,SHRED\n")
        with self.assertRaisesRegex(SchemaError, "required_action"):
            load_records(SPECS["rules"], self.dir)

    def test_audit_field_enum(self):
        self.write("user_audit_log.csv",
                   "audit_id,user_id,event_time,field_name,old_value,new_value\n"
                   "a1,u1,2026-01-01T00:00:00Z,name,x,y\n")
        with self.assertRaisesRegex(SchemaError, "field_name"):
            load_records(SPECS["audit_log"], self.dir)

    def test_output_template_must_be_empty_with_exact_header(self):
        self.write("output.csv", "case_id,deletion_status,steps_json\n")
        check_output_template(self.dir)
        self.write("output.csv", "case_id,steps_json\n")
        with self.assertRaises(SchemaError):
            check_output_template(self.dir)
        self.write("output.csv", "case_id,deletion_status,steps_json\ncase_1,COMPLETE,[]\n")
        with self.assertRaises(SchemaError):
            check_output_template(self.dir)


@unittest.skipUnless((DATASET_DIR / "deletion_requests.csv").exists(), "dataset/ not present")
class RealDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()

    def test_documented_row_counts(self):
        counts = {name: len(getattr(self.ds, name)) for name in
                  ("requests", "sample_requests", "identities", "audit_log", "activity", "rules", "evidence_files")}
        self.assertEqual(counts, {"requests": 518, "sample_requests": 32, "identities": 850,
                                  "audit_log": 306, "activity": 4011, "rules": 15, "evidence_files": 94})

    def test_no_error_level_issues(self):
        self.assertEqual([i for i in self.ds.issues if i.level == "ERROR"], [])

    def test_multilingual_text_survives(self):
        self.assertTrue(any(any(ord(ch) > 127 for ch in r.request_text) for r in self.ds.requests))

    def test_request_ids_are_unique_and_disjoint_from_samples(self):
        ids = [r.case_id for r in self.ds.requests]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(set(ids) & {r.case_id for r in self.ds.sample_requests})


if __name__ == "__main__":
    unittest.main()
