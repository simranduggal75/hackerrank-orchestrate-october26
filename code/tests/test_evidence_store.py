"""Store tests: dispatch, crash containment, timestamp filtering, exact matching, provenance."""

import unittest

from evidence import image_parser
from evidence import store as store_mod
from evidence.ocr import OcrUnavailable, available as ocr_available
from evidence.facts import Coverage, Fact, Identifier, ParseResult, Provenance
from evidence.store import EvidenceStore, extract_all, extract_file
from identity import IdentityIndex, email_hash
from loaders import DATASET_DIR, AuditEvent, EvidenceFile, Identity, load_dataset
from tests.helpers import CAPTURED, NEEDS_DATASET, TmpCase


def fact(evidence_id="evidence_01", *, captured="2026-09-01T00:00:00Z", observed=None, resource="account_1",
         idents=(), record_type="USER_PROFILE", state="FOUND", system="account_service"):
    prov = Provenance(evidence_id, f"evidence/{evidence_id}.json", "records[0]", "test")
    return Fact(evidence_id, captured, observed or captured, system, resource, record_type, state,
                tuple(Identifier(f, v, k) for f, v, k in idents), {}, None, prov)


def store_of(*facts, coverage=()):
    by_file = {}
    for f in facts:
        by_file.setdefault(f.evidence_id, ParseResult(f.evidence_id, "test")).facts.append(f)
    for c in coverage:
        by_file.setdefault(c.evidence_id, ParseResult(c.evidence_id, "test")).coverage.append(c)
    manifest = [EvidenceFile(i, f"evidence/{i}.json", "o", "2026-09-01T00:00:00Z") for i in by_file]
    return EvidenceStore(by_file.values(), manifest)


def subject(audit_at="2026-09-30T00:00:00Z"):
    users = [Identity("user_2", "Sam Lee", "sam.lee@example.test", "+44 7700 900002", email_hash("sam.lee@example.test"), "2026-08-01T00:00:00Z"),
             Identity("user_20", "Other", "other@example.test", "+44 7700 900020", email_hash("other@example.test"), "2026-08-01T00:00:00Z")]
    events = [AuditEvent("audit_1", "user_2", "2026-06-01T00:00:00Z", "email", "sam.old@example.test", "sam.lee@example.test"),
              AuditEvent("audit_2", "user_2", "2026-06-01T00:00:00Z", "email_hash", email_hash("sam.old@example.test"), email_hash("sam.lee@example.test"))]
    return IdentityIndex(users, events).identifiers("user_2", audit_at)


class DispatchTests(TmpCase):
    def test_png_goes_to_the_image_parser_and_an_unreadable_one_is_reported(self):
        self.write("evidence/images/x.png", b"not a png")
        original = store_mod.PARSERS[".png"]
        def no_ocr(path):
            raise OcrUnavailable("no engine here")
        store_mod.PARSERS[".png"] = lambda path, ev: image_parser.parse(path, ev, ocr=no_ocr)
        try:
            result = extract_file(EvidenceFile("evidence_09", "evidence/images/x.png", "o", CAPTURED), self.dir)
        finally:
            store_mod.PARSERS[".png"] = original
        self.assertEqual([(i.level, i.code) for i in result.issues], [("ERROR", "OCR_UNAVAILABLE")])
        self.assertEqual(result.parser, "image")

    def test_unsupported_and_missing_files_are_errors(self):
        self.write("x.xyz", "data")
        self.assertEqual(self.codes(extract_file(EvidenceFile("e1", "evidence/x.xyz", "o", CAPTURED), self.dir)), ["UNSUPPORTED_FORMAT"])
        self.assertEqual(self.codes(extract_file(EvidenceFile("e2", "evidence/none.json", "o", CAPTURED), self.dir)), ["MISSING_FILE"])

    def test_parser_crash_is_contained_and_reported(self):
        self.write("evidence/x.json", "{}")
        original = store_mod.PARSERS[".json"]
        store_mod.PARSERS[".json"] = lambda path, ev: 1 / 0
        try:
            result = extract_file(EvidenceFile("e3", "evidence/x.json", "o", CAPTURED), self.dir)
        finally:
            store_mod.PARSERS[".json"] = original
        self.assertEqual(self.codes(result), ["PARSER_CRASH"])
        self.assertIn("ZeroDivisionError", result.issues[0].message)

    def test_record_after_its_files_capture_is_flagged_once(self):
        doc = {"exported_at": "2026-08-12T09:00:00Z", "origin": "o", "record_count": 1, "records": [
            {"recorded_at": "2026-08-14T09:00:00Z", "system": "account_service", "resource_id": "account_1417",
             "record_type": "USER_PROFILE", "result": "NOT_FOUND", "subject_reference": "a@x.com"}]}
        self.write_json("evidence/f.json", doc)
        result = extract_file(EvidenceFile("e4", "evidence/f.json", "o", "2026-08-12T09:00:00Z"), self.dir)
        self.assertEqual(self.codes(result), ["RECORD_AFTER_CAPTURE"])

    def test_end_to_end_over_several_formats(self):
        self.write("evidence/a.txt", f"Snapshot: {CAPTURED}\n- {CAPTURED} | account_service | account_1 | USER_PROFILE | FOUND | a@x.com\n")
        self.write("evidence/b.log", f"{CAPTURED} level=INFO service=s trace_id=t event=record_lookup system=account_service "
                                     'resource_id=account_2 record_type=USER_PROFILE result=NOT_FOUND subject_reference=b@x.com\n')
        manifest = [EvidenceFile("e1", "evidence/a.txt", "o", CAPTURED), EvidenceFile("e2", "evidence/b.log", "o", CAPTURED)]
        store = extract_all(manifest, self.dir)
        self.assertEqual({f.resource_id for f in store.facts}, {"account_1", "account_2"})
        self.assertTrue(any("total: 2 facts" in line for line in store.summary()))


class TimeFilteringTests(unittest.TestCase):
    AUDIT = "2026-09-10T00:00:00Z"

    def setUp(self):
        self.early = fact("evidence_01", captured="2026-09-05T00:00:00Z", resource="account_1")
        self.late_file = fact("evidence_02", captured="2026-09-11T00:00:00Z", resource="account_1")
        self.late_record = fact("evidence_03", captured="2026-09-09T00:00:00Z", observed="2026-09-12T00:00:00Z", resource="account_1")
        self.on_cutoff = fact("evidence_04", captured=self.AUDIT, resource="account_1")
        self.store = store_of(self.early, self.late_file, self.late_record, self.on_cutoff)

    def test_available_facts_excludes_late_files_and_late_records(self):
        ids = {f.evidence_id for f in self.store.available_facts(self.AUDIT)}
        self.assertEqual(ids, {"evidence_01", "evidence_04"})

    def test_resource_lookup_is_filtered_too(self):
        ids = {f.evidence_id for f in self.store.facts_for_resource("account_1", self.AUDIT)}
        self.assertEqual(ids, {"evidence_01", "evidence_04"})
        self.assertEqual(self.store.facts_for_resource("account_1", "2026-01-01T00:00:00Z"), [])
        self.assertEqual(self.store.facts_for_resource("nope_1", self.AUDIT), [])

    def test_later_audit_sees_more(self):
        self.assertEqual(len(self.store.available_facts("2026-09-30T00:00:00Z")), 4)

    def test_coverage_is_filtered_by_both_times(self):
        prov = Provenance("evidence_01", "p", "t", "test")
        cov = [Coverage("evidence_01", "2026-09-05T00:00:00Z", "2026-09-05T00:00:00Z", "s", None, "t", 0, prov),
               Coverage("evidence_02", "2026-09-05T00:00:00Z", "2026-09-11T00:00:00Z", "s", None, "t", 0, prov)]
        store = store_of(coverage=cov)
        self.assertEqual([c.evidence_id for c in store.coverage_at(self.AUDIT)], ["evidence_01"])


class ExactMatchingTests(unittest.TestCase):
    def matches(self, *facts, audit_at="2026-09-30T00:00:00Z"):
        return store_of(*facts).facts_for_subject(subject(audit_at), audit_at)

    def test_exact_current_and_historical_identifiers_match(self):
        current = fact("evidence_01", idents=[("email", "SAM.LEE@EXAMPLE.TEST", "email")])
        historical = fact("evidence_02", idents=[("lookup_key", "sam.old@example.test", "email")])
        by_hash = fact("evidence_03", idents=[("correlation_key", email_hash("sam.lee@example.test"), "email_hash")])
        by_user = fact("evidence_04", idents=[("subject_reference", "user_2", "user_id")])
        by_phone = fact("evidence_05", idents=[("owner_phone", "+44 7700 900002", "phone")])
        hits = self.matches(current, historical, by_hash, by_user, by_phone)
        self.assertEqual(sorted(m.fact.evidence_id for m in hits),
                         ["evidence_01", "evidence_02", "evidence_03", "evidence_04", "evidence_05"])
        self.assertEqual({m.fact.evidence_id: m.matched[0][1] for m in hits},
                         {"evidence_01": "email", "evidence_02": "email", "evidence_03": "email_hash",
                          "evidence_04": "user_id", "evidence_05": "phone"})

    def test_lookalikes_and_names_never_match(self):
        decoys = [fact("evidence_01", idents=[("email", "sam.lee.jr@example.test", "email")]),
                  fact("evidence_02", idents=[("email", "xsam.lee@example.test", "email")]),
                  fact("evidence_03", idents=[("subject_reference", "user_20", "user_id")]),
                  fact("evidence_04", idents=[("full_name", "Sam Lee", "name")]),
                  fact("evidence_05", idents=[("email", "other@example.test", "email")]),
                  fact("evidence_06", idents=[("note", "sam.lee@example.test", "other")])]  # wrong kind is ignored
        self.assertEqual(self.matches(*decoys), [])

    def test_identity_known_only_after_audit_does_not_match(self):
        # The fact predates both audits, so only the identity timeline decides (email adopted June 1).
        early = fact("evidence_01", captured="2026-04-20T00:00:00Z", idents=[("lookup_key", "sam.lee@example.test", "email")])
        self.assertEqual(self.matches(early, audit_at="2026-05-01T00:00:00Z"), [])
        self.assertEqual(len(self.matches(early, audit_at="2026-07-01T00:00:00Z")), 1)

    def test_matching_respects_the_time_rule(self):
        late = fact("evidence_01", captured="2026-10-02T00:00:00Z", idents=[("email", "sam.lee@example.test", "email")])
        self.assertEqual(self.matches(late, audit_at="2026-09-30T00:00:00Z"), [])

    def test_resource_with_no_identifier_is_not_matched_by_guessing(self):
        self.assertEqual(self.matches(fact("evidence_01", resource="account_5334", idents=[])), [])


@NEEDS_DATASET
class RealStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.store = extract_all(cls.ds.evidence_files)

    def test_no_error_issues_on_the_real_evidence(self):
        errors = [i for i in self.store.issues if i.level == "ERROR"]
        if not ocr_available():  # without OCR the screenshots fail explicitly; everything else must still be clean
            self.assertEqual({i.code for i in errors}, {"OCR_UNAVAILABLE"})
            errors = [i for i in errors if i.code != "OCR_UNAVAILABLE"]
        self.assertEqual(errors, [])

    def test_every_file_produced_facts_or_an_explicit_error(self):
        for e in self.ds.evidence_files:
            result = self.store.results[e.file_id]
            if e.file_path.endswith(".png") and not ocr_available():
                self.assertEqual([i.code for i in result.issues], ["OCR_UNAVAILABLE"], e.file_id)
            else:
                self.assertTrue(result.facts or result.coverage, e.file_id)

    def test_provenance_traces_every_fact_to_its_manifest_entry(self):
        manifest = {e.file_id: e for e in self.ds.evidence_files}
        for f in self.store.facts:
            e = manifest[f.evidence_id]
            self.assertEqual((f.provenance.evidence_id, f.provenance.path, f.captured_at),
                             (e.file_id, e.file_path, e.captured_at))
            self.assertTrue(f.provenance.locator)
            self.assertTrue((DATASET_DIR / f.provenance.path).is_file())
            self.assertIn(f.state, {"FOUND", "NOT_FOUND", "ACTIVE", "DISABLED", "BLOCKED", "IDENTIFIABLE",
                                    "ANONYMIZED", "REDACTED", "NO_DIRECT_IDENTIFIER", "UNKNOWN"})

    def test_facts_are_unique_per_location(self):
        seen = {(f.evidence_id, f.provenance.locator, f.resource_id) for f in self.store.facts}
        self.assertEqual(len(seen), len(self.store.facts))

    def test_known_sample_subject_matches_only_exact_evidence_before_audit(self):
        index = IdentityIndex(self.ds.identities, self.ds.audit_log)
        case = next(r for r in self.ds.sample_requests if r.case_id == "case_05")
        subj = index.resolve_request(case).identifiers
        hits = self.store.facts_for_subject(subj)
        self.assertTrue(hits)
        for m in hits:
            self.assertTrue(m.fact.available_by(case.audit_at))
        self.assertIn(("evidence_54", "account_005"), {(m.fact.evidence_id, m.fact.resource_id) for m in hits})

    def test_late_record_cannot_be_used_before_it_exists(self):
        late = [f for f in self.store.facts if f.observed_at > f.captured_at]
        # two JSON records, plus (when OCR ran) the Search Console screenshot whose "Executed" time
        # is 14 seconds after its manifest capture time
        expected = {"evidence_13", "evidence_36"} | ({"evidence_09"} if ocr_available() else set())
        self.assertEqual({f.evidence_id for f in late}, expected)
        for f in late:
            self.assertNotIn(f, self.store.available_facts(f.captured_at))
            self.assertIn(f, self.store.available_facts(f.observed_at))


if __name__ == "__main__":
    unittest.main()
