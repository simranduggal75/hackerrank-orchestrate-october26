"""JSON parser tests: four document families, record-level times, malformed documents."""

import unittest

from evidence import json_parser
from loaders import DATASET_DIR, load_dataset
from tests.helpers import CAPTURED, NEEDS_DATASET, TmpCase

PROFILE = {"resource_id": "account_1", "customer_reference": "user_1", "full_name": "Ann Bee",
           "email": "ann@x.com", "phone": "+1 202 555 0101", "account_status": "ACTIVE", "signup_channel": "organic"}
HISTORY = {"recorded_at": "2026-08-31T09:00:00Z", "system": "account_service", "resource_id": "account_9",
           "record_type": "USER_PROFILE", "result": "NOT_FOUND", "lookup_value": "OLD.ADDRESS@Example.test"}


def snap(captured, store, collection, *records):
    return {"captured_at": captured, "data_store": store, "collection": collection, "records": list(records)}


def bundle(*snapshots, exported=CAPTURED):
    return {"exported_at": exported, "source": "records_assurance_service", "snapshots": list(snapshots)}


class BundleTests(TmpCase):
    def parse(self, doc):
        return json_parser.parse(self.write_json("b.json", doc), self.ev("b.json"))

    def test_records_get_snapshot_time_provenance_and_identifiers(self):
        result = self.parse(bundle(
            snap("2026-08-30T00:00:00Z", "account_service", "customer_profiles", PROFILE),
            snap("2026-08-31T00:00:00Z", "backup_restore_service", "operational_record_history", HISTORY)))
        self.assertEqual(self.codes(result), [])
        profile, history = result.facts
        self.assertEqual((profile.observed_at, profile.captured_at), ("2026-08-30T00:00:00Z", CAPTURED))
        self.assertEqual((profile.system, profile.record_type, profile.state), ("account_service", "USER_PROFILE", "FOUND"))
        self.assertEqual(profile.store, "account_service/customer_profiles")
        self.assertEqual(profile.provenance.locator, "snapshots[0].records[0]")
        self.assertEqual({i.kind for i in profile.identifiers}, {"user_id", "email", "phone", "name"})
        self.assertEqual((history.observed_at, history.state, history.identifiers[0].field),
                         ("2026-08-31T09:00:00Z", "NOT_FOUND", "lookup_value"))
        self.assertEqual(history.identifiers[0].value, "OLD.ADDRESS@Example.test")  # raw value preserved
        self.assertEqual(history.provenance.locator, "snapshots[1].records[0]")

    def test_coverage_is_emitted_per_snapshot_even_when_empty(self):
        result = self.parse(bundle(snap("2026-08-30T00:00:00Z", "account_service", "customer_profiles")))
        self.assertEqual([(c.system, c.store, c.row_count, c.observed_at) for c in result.coverage],
                         [("account_service", "account_service/customer_profiles", 0, "2026-08-30T00:00:00Z")])

    def test_snapshot_with_bad_time_is_skipped_but_others_survive(self):
        result = self.parse(bundle(
            snap("yesterday", "account_service", "customer_profiles", PROFILE),
            snap("2026-08-30T00:00:00Z", "account_service", "customer_profiles", dict(PROFILE, resource_id="account_2"))))
        self.assertIn("BAD_TIMESTAMP", self.codes(result))
        self.assertEqual([f.resource_id for f in result.facts], ["account_2"])

    def test_snapshot_after_export_is_flagged(self):
        result = self.parse(bundle(snap("2026-09-02T00:00:00Z", "account_service", "customer_profiles", PROFILE)))
        self.assertIn("SNAPSHOT_AFTER_EXPORT", self.codes(result))

    def test_bad_records_are_skipped_with_issues(self):
        result = self.parse(bundle(snap("2026-08-30T00:00:00Z", "account_service", "customer_profiles",
                                        PROFILE, {"email": "no-resource@x.com"}, "not an object")))
        self.assertEqual(self.codes(result).count("BAD_RECORD"), 2)
        self.assertEqual(result.stats["records_skipped"], 2)
        self.assertEqual(len(result.facts), 1)

    def test_unknown_collection_is_reported(self):
        result = self.parse(bundle(snap("2026-08-30T00:00:00Z", "mystery", "stuff", {"resource_id": "x_1"})))
        self.assertIn("UNKNOWN_COLLECTION", self.codes(result))
        self.assertEqual(result.stats["records_unmapped"], 1)

    def test_history_record_with_bad_fields(self):
        result = self.parse(bundle(snap("2026-08-30T00:00:00Z", "backup_restore_service", "operational_record_history",
                                        dict(HISTORY, recorded_at="soon"), dict(HISTORY, result="WEIRD"))))
        self.assertEqual(self.codes(result), ["BAD_TIMESTAMP", "UNKNOWN_STATE"])
        self.assertEqual(result.facts[0].state, "UNKNOWN")

    def test_snapshot_without_records_list(self):
        result = self.parse({"exported_at": CAPTURED, "snapshots": [{"captured_at": CAPTURED, "records": "oops"}]})
        self.assertIn("BAD_SNAPSHOT", self.codes(result))


class ResourceExportTests(TmpCase):
    def doc(self, **services):
        return {"exported_at": CAPTURED, "region": "global", "service_exports": services}

    def parse(self, doc):
        return json_parser.parse(self.write_json("e.json", doc), self.ev("e.json"))

    def test_profile_lookups(self):
        result = self.parse(self.doc(account_service={"profile_lookups": [
            {"observed_at": "2026-09-01T09:00:00Z", "profile": {"customer_reference": None, "email": "keiko@x.com",
             "email_hash": None, "full_name": None, "phone": None}, "resource_id": "account_5334", "status_code": 200},
            {"observed_at": "2026-09-01T09:05:00Z", "profile": None, "resource_id": "account_3136", "status_code": 404}]}))
        found, missing = result.facts
        self.assertEqual((found.state, [i.value for i in found.identifiers], found.attributes["status_code"]),
                         ("FOUND", ["keiko@x.com"], 200))
        self.assertEqual((missing.state, missing.identifiers, missing.observed_at), ("NOT_FOUND", (), "2026-09-01T09:05:00Z"))
        self.assertEqual(missing.provenance.locator, "service_exports.account_service.profile_lookups[1]")

    def test_index_queries_hit_and_miss(self):
        hit = {"observed_at": "2026-09-01T09:00:00Z", "query": "noah@x.com", "resource_id": "search_1993",
               "hits": {"total": 1, "documents": [{"customer_reference": None, "display_name": None, "email": "noah@x.com",
                                                   "email_hash": None, "phone": None, "resource_id": "search_1993"}]}}
        miss = {"observed_at": "2026-09-01T09:01:00Z", "query": "gone@x.com", "resource_id": "search_77", "hits": {"total": 0, "documents": []}}
        result = self.parse(self.doc(search_service={"index_queries": [hit, miss]}))
        self.assertEqual([(f.resource_id, f.state) for f in result.facts], [("search_1993", "FOUND"), ("search_77", "NOT_FOUND")])
        self.assertEqual({i.field for i in result.facts[0].identifiers}, {"email"})  # the query is context, not identity
        self.assertEqual(result.facts[0].attributes["query"], "noah@x.com")
        self.assertEqual([i.value for i in result.facts[1].identifiers], ["gone@x.com"])  # a miss has only the query
        self.assertEqual(result.stats["facts"], 2)

    def test_lookalike_hit_does_not_inherit_the_searched_address(self):
        decoy = {"observed_at": "2026-09-01T09:00:00Z", "query": "r.pl27@example.test", "resource_id": "search_1874",
                 "hits": {"total": 1, "documents": [{"customer_reference": None, "display_name": None, "email": "r.pl27.jr@example.test",
                                                     "email_hash": None, "phone": None, "resource_id": "search_1874"}]}}
        result = self.parse(self.doc(search_service={"index_queries": [decoy]}))
        self.assertEqual([i.value for i in result.facts[0].identifiers], ["r.pl27.jr@example.test"])
        self.assertEqual(result.facts[0].attributes["query"], "r.pl27@example.test")

    def test_service_records_use_their_own_vocabulary(self):
        result = self.parse(self.doc(
            analytics_platform={"profiles": [{"observed_at": CAPTURED, "resource_id": "analytics_1", "audience_cohort": "home",
                                              "customer_reference": None, "email": None, "email_hash": None, "phone": None}]},
            backup_service={"restore_entries": [{"observed_at": CAPTURED, "resource_id": "backup_1", "archive_key": "k",
                                                 "customer_email": "a@x.com", "customer_reference": None, "restore_allowed": False}]},
            integration_service={"connectors": [{"observed_at": CAPTURED, "resource_id": "data_flow_1", "connector_name": "c",
                                                 "customer_email": None, "customer_reference": "user_4", "enabled": True,
                                                 "last_delivery_status": "ok"}]},
            support_platform={"tickets": [{"observed_at": CAPTURED, "resource_id": "support_1", "customer_reference": "user_2",
                                           "message_excerpt": "[REDACTED]", "requester_email": None, "requester_name": None,
                                           "requester_phone": None}]}))
        self.assertEqual({f.resource_id: f.state for f in result.facts},
                         {"analytics_1": "ANONYMIZED", "backup_1": "BLOCKED", "data_flow_1": "ACTIVE", "support_1": "REDACTED"})

    def test_bad_observed_at_skips_the_record(self):
        result = self.parse(self.doc(account_service={"profile_lookups": [
            {"observed_at": "later", "profile": None, "resource_id": "account_1", "status_code": 404}]}))
        self.assertEqual((self.codes(result), result.facts), (["BAD_TIMESTAMP"], []))

    def test_unknown_list_and_malformed_service(self):
        result = self.parse(self.doc(mystery_service={"things": [{"resource_id": "m_1"}]}, account_service="oops"))
        self.assertEqual(sorted(self.codes(result)), ["BAD_SNAPSHOT", "UNKNOWN_COLLECTION"])


class FlatAndCollectionTests(TmpCase):
    def test_flat_lookup_list(self):
        rec = {"recorded_at": "2026-08-31T00:00:00Z", "system": "account_service", "resource_id": "account_1",
               "record_type": "USER_PROFILE", "result": "NOT_FOUND", "subject_reference": "ANN@X.COM"}
        doc = {"exported_at": CAPTURED, "origin": "integration_management_service", "record_count": 1, "records": [rec]}
        result = json_parser.parse(self.write_json("f.json", doc), self.ev("f.json"))
        fact = result.facts[0]
        self.assertEqual((fact.state, fact.store, fact.identifiers[0].value), ("NOT_FOUND", "integration_management_service", "ANN@X.COM"))

    def test_record_count_mismatch_is_flagged(self):
        doc = {"exported_at": CAPTURED, "origin": "o", "record_count": 3, "records": []}
        self.assertIn("COUNT_MISMATCH", self.codes(json_parser.parse(self.write_json("f.json", doc), self.ev("f.json"))))

    def test_collection_file(self):
        doc = {"captured_at": CAPTURED, "source": "analytics_identity_store", "collection": "customer_analytics_reports",
               "records": [{"resource_id": "analytics_1", "user_id": "user_1", "email": "a@x.com", "phone": None, "email_hash": None}]}
        result = json_parser.parse(self.write_json("c.json", doc), self.ev("c.json"))
        self.assertEqual([(f.system, f.record_type, f.state) for f in result.facts],
                         [("analytics_platform", "ANALYTICS_PROFILE", "IDENTIFIABLE")])

    def test_unknown_collection_file(self):
        doc = {"captured_at": CAPTURED, "collection": "mystery", "records": [{"resource_id": "x"}]}
        self.assertIn("UNKNOWN_COLLECTION", self.codes(json_parser.parse(self.write_json("c.json", doc), self.ev("c.json"))))


class MalformedDocumentTests(TmpCase):
    def parse(self, content):
        return json_parser.parse(self.write("m.json", content), self.ev("m.json"))

    def test_invalid_json(self):
        result = self.parse('{"exported_at": "2026-09-01T12:00:00Z", "snapshots": [')
        self.assertEqual((self.codes(result), result.facts), (["INVALID_JSON"], []))

    def test_top_level_list(self):
        self.assertEqual(self.codes(self.parse("[1, 2]")), ["UNKNOWN_SHAPE"])

    def test_unknown_layout(self):
        self.assertIn("UNKNOWN_SHAPE", self.codes(self.parse('{"hello": "world"}')))

    def test_document_time_disagreeing_with_manifest(self):
        result = self.parse('{"exported_at": "2026-09-09T00:00:00Z", "snapshots": []}')
        self.assertIn("TIMESTAMP_MISMATCH", self.codes(result))

    def test_missing_document_time(self):
        self.assertIn("NO_FILE_TIMESTAMP", self.codes(self.parse('{"snapshots": []}')))

    def test_non_utf8_bytes(self):
        result = self.parse(b'{"exported_at": "2026-09-01T12:00:00Z", "snapshots": [], "note": "caf\xe9"}')
        self.assertIn("DECODE_ERROR", self.codes(result))

    def test_missing_file(self):
        result = json_parser.parse(self.dir / "absent.json", self.ev("absent.json"))
        self.assertEqual(self.codes(result), ["UNREADABLE_FILE"])


@NEEDS_DATASET
class RealJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.ev = {e.file_id: e for e in cls.ds.evidence_files}

    def parse(self, file_id):
        return json_parser.parse(DATASET_DIR / self.ev[file_id].file_path, self.ev[file_id])

    def test_all_json_files_parse_without_errors(self):
        for file_id, e in self.ev.items():
            if e.file_path.endswith(".json"):
                errors = [i for i in self.parse(file_id).issues if i.level == "ERROR"]
                self.assertEqual(errors, [], file_id)

    def test_record_stamped_after_its_file_is_kept_with_its_own_time(self):
        late = [f for f in self.parse("evidence_13").facts if f.observed_at > f.captured_at]
        self.assertEqual(len(late), 1)
        self.assertEqual((late[0].resource_id, late[0].observed_at), ("account_1417", "2026-08-14T09:00:00Z"))
        self.assertFalse(late[0].available_by("2026-08-13T00:00:00Z"))
        self.assertTrue(late[0].available_by("2026-08-14T09:00:00Z"))


if __name__ == "__main__":
    unittest.main()
