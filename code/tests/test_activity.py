"""Identity links and claims from the system-activity log."""

import dataclasses
import unittest

from activity import ActivityIndex, parse_identity_links
from loaders import DATASET_DIR, load_dataset
from tests.footprint_fixtures import activity_row, link_row
from tests.helpers import NEEDS_DATASET


class LinkParsingTests(unittest.TestCase):
    def test_a_link_names_resource_alias_email_and_user(self):
        links, problems = parse_identity_links([
            link_row("activity_1", "2026-09-01T00:00:00Z", "export_1260", "export_service", "export_1260", "Ik.Dm48271@Example.test", "user_18")])
        self.assertEqual(problems, [])
        link = links[0]
        self.assertEqual((link.resource_id, link.system, link.email, link.user_id, link.alias, link.exclusive),
                         ("export_1260", "export_service", "ik.dm48271@example.test", "user_18", None, False))

    def test_alias_and_exclusivity(self):
        links, _ = parse_identity_links([
            link_row("activity_2", "2026-09-01T00:00:00Z", "object_19", "object_storage", "delivery_confirmation_19.pdf",
                     "pr.37294@example.test", "user_07", exclusive=True)])
        self.assertEqual((links[0].alias, links[0].exclusive), ("delivery_confirmation_19.pdf", True))

    def test_unreadable_link_rows_are_reported_not_dropped(self):
        bad = activity_row("activity_3", "2026-09-01T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "account_service",
                           "account_1", details="Something unexpected happened.")
        mismatch = link_row("activity_4", "2026-09-01T00:00:00Z", "account_2", "account_service", None, "a@x.com", "user_1")
        mismatch = dataclasses.replace(mismatch, resource_id="account_9")  # the row's resource differs from the text
        links, problems = parse_identity_links([bad, mismatch])
        self.assertEqual(links, [])
        self.assertEqual(len(problems), 2)

    def test_other_event_types_never_become_links(self):
        row = activity_row("activity_5", "2026-09-01T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_1",
                           details="Data catalog linked account_1 (account_1) to a@x.com and user_1.")
        self.assertEqual(parse_identity_links([row])[0], [])


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            link_row("activity_1", "2026-09-01T00:00:00Z", "account_5", "account_service", None, "sam.lee@example.test", "user_1"),
            link_row("activity_2", "2026-10-10T00:00:00Z", "search_5", "search_service", None, "sam.lee@example.test", "user_1"),
            link_row("activity_3", "2026-09-01T00:00:00Z", "account_6", "account_service", None, "pat.roe@example.test", "user_2"),
            activity_row("activity_4", "2026-09-02T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_5", "FAILED"),
            activity_row("activity_5", "2026-10-20T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_5"),
            activity_row("activity_6", "2026-09-03T00:00:00Z", "SYNC_RECORD", "integration_service", "search_service", "search_9",
                         details="Active connector repopulated downstream resource search_9."),
            activity_row("activity_7", "2026-09-04T00:00:00Z", "SYNC_RECORD", "account_service", "account_replication_cluster", "account_5"),
            activity_row("activity_8", "2026-09-05T00:00:00Z", "CLASSIFY_REQUEST", "privacy_intake", "privacy_case_management", "account_7"),
            activity_row("activity_9", "2026-09-06T00:00:00Z", "CHECK_RESTORE_ACCESS", "deletion_worker", "backup_service", "backup_1"),
            activity_row("activity_10", "2026-09-07T00:00:00Z", "REFRESH_CACHE", "account_service", "cache_service", "cache_1"),
        ]
        self.index = ActivityIndex(self.rows)

    def test_links_by_user_or_email_and_time(self):
        by_user = self.index.links_for_user("user_1", {"sam.lee@example.test"}, "2026-09-30T00:00:00Z")
        self.assertEqual([l.resource_id for l in by_user], ["account_5"])  # the October link is not yet recorded
        by_email_only = self.index.links_for_user("user_99", {"pat.roe@example.test"}, "2026-09-30T00:00:00Z")
        self.assertEqual([l.resource_id for l in by_email_only], ["account_6"])
        later = self.index.later_links_for_user("user_1", {"sam.lee@example.test"}, "2026-09-30T00:00:00Z")
        self.assertEqual([l.resource_id for l in later], ["search_5"])

    def test_claims_are_time_filtered_and_kept_apart_from_links(self):
        claims = self.index.claims_for("account_5", "2026-09-30T00:00:00Z")
        self.assertEqual([(c.activity_id, c.kind, c.result) for c in claims],
                         [("activity_4", "worker", "FAILED"), ("activity_7", "replication", "SUCCESS")])
        self.assertEqual(self.index.claims_for("account_5", "2026-12-31T00:00:00Z")[-1].activity_id, "activity_5")
        self.assertEqual(self.index.claims_for("account_6", "2026-12-31T00:00:00Z"), [])  # link rows are not claims

    def test_claim_kinds(self):
        kinds = {r: self.index.claims_for(r, "2026-12-31T00:00:00Z")[0].kind
                 for r in ("search_9", "account_7", "backup_1", "cache_1")}
        self.assertEqual(kinds, {"search_9": "recreation", "account_7": "classification", "backup_1": "check", "cache_1": "other"})

    def test_activity_only_resources_are_known_but_have_no_owner(self):
        self.assertIn("cache_1", self.index.resources)
        self.assertEqual(self.index.links_for_resource("cache_1", "2026-12-31T00:00:00Z"), [])

    def test_links_for_resource(self):
        self.assertEqual([l.user_id for l in self.index.links_for_resource("account_5", "2026-12-31T00:00:00Z")], ["user_1"])


@NEEDS_DATASET
class RealActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.index = ActivityIndex(cls.ds.activity)

    def test_all_twelve_link_events_parse(self):
        self.assertEqual((len(self.index.links), self.index.problems), (12, []))

    def test_link_events_resolve_what_screenshots_could_not_read(self):
        by_resource = {l.resource_id: l for l in self.index.links}
        self.assertEqual(by_resource["account_5762"].user_id, "user_11")
        self.assertEqual(by_resource["tombstone_17"].user_id, "user_17")
        self.assertTrue(by_resource["object_19"].exclusive)
        self.assertEqual(by_resource["object_19"].alias, "delivery_confirmation_19.pdf")

    def test_a_deletion_claim_does_not_identify_its_subject(self):
        claims = self.index.claims_for("account_001", "2026-12-31T00:00:00Z")
        self.assertEqual([(c.kind, c.result) for c in claims], [("worker", "SUCCESS")])
        self.assertEqual(self.index.links_for_resource("account_001", "2026-12-31T00:00:00Z"), [])

    def test_cache_and_email_delivery_resources_are_activity_only(self):
        caches = [r for r in self.index.resources if r.startswith(("cache_", "email_delivery_"))]
        self.assertGreater(len(caches), 500)
        self.assertEqual([r for r in caches if self.index.links_for_resource(r, "2999-01-01T00:00:00Z")], [])


if __name__ == "__main__":
    unittest.main()
