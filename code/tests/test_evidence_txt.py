"""Handover-table parser tests."""

import unittest

from evidence import txt_parser
from loaders import DATASET_DIR, load_dataset
from tests.helpers import CAPTURED, NEEDS_DATASET, TmpCase

HEADER = f"CUSTOMER PLATFORM - SHIFT HANDOVER\nSnapshot: {CAPTURED}\n\nChecks completed during the shift:\n\n"


def row(system, resource, rtype, state, subject, when=CAPTURED):
    return f"- {when} | {system} | {resource} | {rtype} | {state} | {subject}"


class TxtParserTests(TmpCase):
    def parse(self, body, captured=CAPTURED):
        return txt_parser.parse(self.write("h.txt", HEADER + body + "\n"), self.ev("h.txt", captured))

    def test_rows_become_lookup_facts_with_line_provenance(self):
        result = self.parse("\n".join([
            row("account_service", "account_1810", "USER_PROFILE", "NOT_FOUND", "CJ.QR4729135@EXAMPLE.TEST"),
            row("integration_service", "data_flow_2901", "DATA_FLOW", "DISABLED", "user_39"),
            row("backup_service", "backup_3870", "BACKUP_ARCHIVE", "ACTIVE", "yt.538264@example.test")]))
        self.assertEqual(self.codes(result), [])
        a, b, c = result.facts
        self.assertEqual((a.system, a.resource_id, a.record_type, a.state), ("account_service", "account_1810", "USER_PROFILE", "NOT_FOUND"))
        self.assertEqual((a.identifiers[0].value, a.identifiers[0].kind), ("CJ.QR4729135@EXAMPLE.TEST", "email"))  # raw case kept
        self.assertEqual((b.identifiers[0].kind, c.state), ("user_id", "ACTIVE"))
        self.assertEqual([f.provenance.locator for f in result.facts], ["line 6", "line 7", "line 8"])
        self.assertEqual(a.provenance.path, "evidence/h.txt")
        self.assertEqual(result.stats["prose_lines"], 2)

    def test_blank_and_note_subjects(self):
        result = self.parse("\n".join([
            row("search_service", "search_1308", "SEARCH_INDEX_ENTRY", "FOUND", ""),
            row("analytics_platform", "analytics_6830", "ANALYTICS_PROFILE", "ANONYMIZED", "no direct identifier")]))
        blank, note = result.facts
        self.assertEqual(blank.identifiers, ())
        self.assertEqual((note.identifiers, note.attributes["subject_note"]), ((), "no direct identifier"))

    def test_row_time_is_the_record_time(self):
        result = self.parse(row("account_service", "account_1", "USER_PROFILE", "FOUND", "a@x.com", when="2026-08-20T00:00:00Z"))
        self.assertEqual((result.facts[0].observed_at, result.facts[0].captured_at), ("2026-08-20T00:00:00Z", CAPTURED))

    def test_row_after_capture_is_flagged(self):
        result = self.parse(row("account_service", "account_1", "USER_PROFILE", "FOUND", "a@x.com", when="2026-09-02T00:00:00Z"))
        self.assertIn("LINE_AFTER_CAPTURE", self.codes(result))
        self.assertFalse(result.facts[0].available_by(CAPTURED))

    def test_malformed_rows_are_reported_not_dropped_silently(self):
        result = self.parse("\n".join([
            "- 2026-09-01T12:00:00Z | account_service | account_1 | USER_PROFILE",
            row("account_service", "account_2", "USER_PROFILE", "FOUND", "a@x.com", when="not-a-time"),
            row("account_service", "account_3", "USER_PROFILE", "STRANGE", "a@x.com"),
            row("account_service", "account_4", "USER_PROFILE", "FOUND", "ok@x.com")]))
        self.assertEqual(sorted(self.codes(result)), ["BAD_TIMESTAMP", "UNKNOWN_STATE", "UNPARSEABLE_LINE"])
        self.assertEqual(result.stats["rows_skipped"], 2)
        self.assertEqual([(f.resource_id, f.state) for f in result.facts], [("account_3", "UNKNOWN"), ("account_4", "FOUND")])

    def test_snapshot_header_disagreeing_with_manifest(self):
        result = self.parse(row("account_service", "account_1", "USER_PROFILE", "FOUND", "a@x.com"), captured="2026-09-05T00:00:00Z")
        self.assertIn("TIMESTAMP_MISMATCH", self.codes(result))

    def test_file_without_rows_is_an_error(self):
        self.assertEqual(self.codes(self.parse("nothing useful")), ["NO_ROWS"])

    def test_missing_file(self):
        result = txt_parser.parse(self.dir / "absent.txt", self.ev("absent.txt"))
        self.assertEqual(self.codes(result), ["UNREADABLE_FILE"])


@NEEDS_DATASET
class RealTxtTests(unittest.TestCase):
    def test_handover_file(self):
        ds = load_dataset()
        e = next(x for x in ds.evidence_files if x.file_path.endswith(".txt"))
        result = txt_parser.parse(DATASET_DIR / e.file_path, e)
        self.assertEqual([i for i in result.issues if i.level != "INFO"], [])
        rows = sum(1 for line in (DATASET_DIR / e.file_path).read_text(encoding="utf-8").splitlines() if line.startswith("- "))
        self.assertEqual(len(result.facts), rows)
        self.assertTrue(any(f.identifiers and f.identifiers[0].value.isupper() for f in result.facts))  # upper-case emails kept


if __name__ == "__main__":
    unittest.main()
