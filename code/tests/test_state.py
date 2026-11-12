"""State reconstruction: temporal selection, absence proof, replica drift, provisional evidence."""

import unittest

from activity import ActivityClaim
from state import aggregate, infer_type, reconstruct, role_of_store
from tests.footprint_fixtures import coverage, fact, image_fact

A02 = ("evidence_02", "account_service", "USER_PROFILE")  # the subject has an account record in evidence_02
A54 = ("evidence_54", "account_service", "USER_PROFILE")


def recon(facts, coverages=(), anchors=(), claims=(), rid="account_1", hint=None):
    return reconstruct(rid, list(facts), list(coverages), set(anchors), list(claims), hint)


def claim(result="SUCCESS", kind="worker"):
    return ActivityClaim("activity_1", "2026-09-02T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service",
                         "account_1", result, kind, "Deletion worker reported the requested operation as completed.")


class StoreRoleTests(unittest.TestCase):
    def test_roles(self):
        cases = {None: "primary", "customer_data_primary": "primary", "customer_profiles_primary": "primary",
                 "customer_profiles": "primary", "account_service/profile_lookups": "primary", "account_service": "primary",
                 "customer_data_read_replica": "read_replica", "customer_data_replica_ap_south": "replica_ap_south",
                 "customer_profiles_failover_replica": "failover_replica", "customer_data_failover_replica": "failover_replica",
                 "customer_data_reporting_replica": "reporting_replica"}
        for store, role in cases.items():
            self.assertEqual(role_of_store(store), role, store)

    def test_resource_prefixes(self):
        self.assertEqual(infer_type("account_5"), ("account_service", "USER_PROFILE"))
        self.assertEqual(infer_type("audit_record_9"), ("security_service", "SECURITY_AUDIT_LOG"))
        self.assertEqual(infer_type("support_5"), ("support_platform", None))  # a message or an attachment: ambiguous
        self.assertEqual(infer_type("mystery_5"), (None, None))


class TemporalSelectionTests(unittest.TestCase):
    def test_latest_observation_in_the_same_store_wins(self):
        found = fact("evidence_01", state="FOUND", captured="2026-09-01T00:00:00Z")
        gone = fact("evidence_02", state="NOT_FOUND", captured="2026-09-05T00:00:00Z")
        s = recon([found, gone])
        self.assertEqual((s.state, s.absence, s.exists), ("NOT_FOUND", "explicit", False))

    def test_a_later_sighting_beats_an_earlier_absence(self):
        s = recon([fact("evidence_01", state="NOT_FOUND"), fact("evidence_02", state="FOUND", captured="2026-09-07T00:00:00Z")])
        self.assertEqual((s.state, s.absence), ("FOUND", None))

    def test_observation_order_does_not_depend_on_input_order(self):
        a, b = fact("evidence_01", state="FOUND"), fact("evidence_02", state="NOT_FOUND", captured="2026-09-05T00:00:00Z")
        self.assertEqual(recon([a, b]).state, recon([b, a]).state)

    def test_record_time_not_file_time_orders_observations(self):
        early_record = fact("evidence_01", state="FOUND", captured="2026-09-10T00:00:00Z", observed="2026-09-02T00:00:00Z")
        later_record = fact("evidence_02", state="NOT_FOUND", captured="2026-09-06T00:00:00Z", observed="2026-09-05T00:00:00Z")
        self.assertEqual(recon([early_record, later_record]).state, "NOT_FOUND")

    def test_no_observation_is_unknown_not_absent(self):
        s = recon([])
        self.assertEqual((s.state, s.exists, s.absence), ("UNKNOWN", None, None))
        self.assertIn("no usable observation", s.basis)

    def test_unknown_only_observations_stay_unknown(self):
        self.assertEqual(recon([fact(state="UNKNOWN")]).state, "UNKNOWN")


class ReplicaDriftTests(unittest.TestCase):
    def drift(self, replica_time="2026-09-02T23:00:00Z", primary_time="2026-09-02T23:00:00Z"):
        replica = fact("evidence_54", state="FOUND", captured=replica_time, store="customer_profiles_failover_replica",
                       attrs={"replica_role": "failover_replica"}, parser="sql")
        primary_absent = coverage("evidence_54", store="customer_profiles_primary", observed=primary_time)
        return replica, primary_absent

    def test_replica_only_record_is_a_remaining_copy_with_the_drift_recorded(self):
        replica, absent = self.drift()
        s = recon([replica], [absent], anchors={A54})
        self.assertEqual(s.state, "FOUND")
        self.assertIn("primary reports absent at 2026-09-02T23:00:00Z", s.conflicts)
        self.assertEqual({o.role for o in s.observations}, {"failover_replica", "primary"})
        self.assertEqual({o.kind for o in s.observations}, {"fact", "table_absence"})

    def test_a_positive_older_than_a_later_absence_from_another_store_is_stale(self):
        replica, _ = self.drift()
        later_primary = fact("evidence_70", state="NOT_FOUND", captured="2026-09-10T00:00:00Z")
        s = recon([replica, later_primary])
        self.assertEqual(s.state, "UNKNOWN")
        self.assertTrue(any("older than an absence" in c for c in s.conflicts))

    def test_replica_seen_after_the_absence_still_counts(self):
        replica, _ = self.drift(replica_time="2026-09-12T00:00:00Z")
        earlier_primary = fact("evidence_70", state="NOT_FOUND", captured="2026-09-10T00:00:00Z")
        self.assertEqual(recon([replica, earlier_primary]).state, "FOUND")

    def test_both_stores_absent_is_absent(self):
        s = recon([fact("evidence_01", state="NOT_FOUND"),
                   fact("evidence_02", state="NOT_FOUND", store="customer_data_read_replica", attrs={"replica_role": "read_replica"})])
        self.assertEqual((s.state, s.conflicts), ("NOT_FOUND", ()))

    def test_most_exposed_state_wins_among_fresh_stores(self):
        flow_a = fact("evidence_01", "data_flow_1", "DATA_FLOW", "DISABLED")
        flow_b = fact("evidence_02", "data_flow_1", "DATA_FLOW", "ACTIVE", store="customer_data_read_replica", attrs={"replica_role": "read_replica"})
        s = recon([flow_a, flow_b], rid="data_flow_1")
        self.assertEqual(s.state, "ACTIVE")
        self.assertIn("primary shows DISABLED", s.conflicts)

    def test_exposure_order_by_record_type(self):
        def two(record_type, rid, first, second):
            a = fact("evidence_01", rid, record_type, first)
            b = fact("evidence_02", rid, record_type, second, store="customer_data_read_replica", attrs={"replica_role": "read_replica"})
            return recon([a, b], rid=rid).state
        self.assertEqual(two("BACKUP_ARCHIVE", "backup_1", "BLOCKED", "ACTIVE"), "ACTIVE")
        self.assertEqual(two("ANALYTICS_PROFILE", "analytics_1", "ANONYMIZED", "IDENTIFIABLE"), "IDENTIFIABLE")
        self.assertEqual(two("SUPPORT_MESSAGE", "support_1", "REDACTED", "NO_DIRECT_IDENTIFIER"), "NO_DIRECT_IDENTIFIER")
        self.assertEqual(two("ACCOUNT_TOMBSTONE", "tombstone_1", "REDACTED", "IDENTIFIABLE"), "IDENTIFIABLE")

    def test_same_store_same_time_disagreement_uses_the_most_exposed_and_says_so(self):
        a = fact("evidence_01", "data_flow_1", "DATA_FLOW", "DISABLED")
        b = fact("evidence_02", "data_flow_1", "DATA_FLOW", "ACTIVE")
        s = recon([a, b], rid="data_flow_1")
        self.assertEqual(s.state, "ACTIVE")
        self.assertTrue(any("disagree" in n for n in s.notes))


class AbsenceProofTests(unittest.TestCase):
    def test_a_table_dump_proves_absence_only_when_the_file_concerns_the_subject(self):
        dump = coverage("evidence_02", store="customer_profiles_primary", observed="2026-09-05T00:00:00Z")
        anchored = recon([], [dump], anchors={A02})
        self.assertEqual((anchored.state, anchored.absence), ("NOT_FOUND", "table_dump"))
        unanchored = recon([], [dump], anchors=set())
        self.assertEqual((unanchored.state, unanchored.absence), ("UNKNOWN", None))
        self.assertEqual(len(unanchored.absence_candidates), 1)
        self.assertIn("scope is unproven", unanchored.absence_candidates[0].reason)

    def test_an_anchor_for_a_different_record_type_in_the_same_file_does_not_prove_absence(self):
        dump = coverage("evidence_02", store="customer_profiles_primary")
        elsewhere = ("evidence_02", "security_service", "SECURITY_AUDIT_LOG")  # the subject appears in the file, but not as an account
        s = recon([], [dump], anchors={elsewhere})
        self.assertEqual((s.state, len(s.absence_candidates)), ("UNKNOWN", 1))

    def test_the_wrong_system_or_record_type_proves_nothing(self):
        for dump in (coverage(system="search_service"), coverage(record_type="SEARCH_INDEX_ENTRY")):
            s = recon([], [dump], anchors={A02})
            self.assertEqual(s.state, "UNKNOWN", dump)

    def test_only_sql_dumps_count_json_listings_and_other_sources_do_not(self):
        for parser in ("json", "log", "txt", "image", "pdf"):
            s = recon([], [coverage(parser=parser)], anchors={A02})
            self.assertEqual(s.state, "UNKNOWN", parser)

    def test_a_table_that_contains_the_resource_is_not_an_absence(self):
        present = fact("evidence_02", state="FOUND", store="customer_profiles_primary", parser="sql")
        s = recon([present], [coverage("evidence_02", store="customer_profiles_primary")], anchors={A02})
        self.assertEqual(s.state, "FOUND")
        self.assertEqual([o.kind for o in s.observations], ["fact"])

    def test_missing_coverage_leaves_the_state_unknown(self):
        self.assertEqual(recon([], []).state, "UNKNOWN")

    def test_a_dump_from_before_the_resource_was_seen_is_not_even_a_candidate(self):
        seen = fact("evidence_05", state="FOUND", captured="2026-09-10T00:00:00Z")
        old_dump = coverage("evidence_02", observed="2026-09-01T00:00:00Z")
        s = recon([seen], [old_dump], anchors=set())
        self.assertEqual((s.state, s.absence_candidates), ("FOUND", ()))
        newer = coverage("evidence_03", observed="2026-09-20T00:00:00Z")
        s = recon([seen], [newer], anchors=set())
        self.assertEqual(len(s.absence_candidates), 1)  # a newer silent dump might mean removal: unproven, so reported

    def test_explicit_lookup_absence_needs_no_anchor(self):
        s = recon([fact(state="NOT_FOUND")], [], anchors=set())
        self.assertEqual((s.state, s.absence), ("NOT_FOUND", "explicit"))

    def test_unanchored_dump_does_not_override_a_positive_sighting(self):
        seen = fact("evidence_05", state="FOUND", captured="2026-09-02T00:00:00Z")
        s = recon([seen], [coverage("evidence_03", observed="2026-09-20T00:00:00Z")], anchors=set())
        self.assertEqual(s.state, "FOUND")


class ProvisionalEvidenceTests(unittest.TestCase):
    def test_screenshot_only_state_is_provisional(self):
        s = recon([image_fact(state="FOUND")])
        self.assertEqual((s.state, s.provisional), ("FOUND", True))

    def test_a_listed_backup_does_not_establish_restore_risk(self):
        s = recon([image_fact(resource="backup_1", record_type="BACKUP_ARCHIVE", state="FOUND")], rid="backup_1")
        self.assertEqual((s.state, s.provisional), ("FOUND", True))  # exists; restore access unknown

    def test_a_screenshot_cannot_override_database_evidence(self):
        db = fact("evidence_01", "backup_1", "BACKUP_ARCHIVE", "BLOCKED", parser="sql", captured="2026-09-01T00:00:00Z")
        shot = image_fact(evidence_id="evidence_09", resource="backup_1", record_type="BACKUP_ARCHIVE", state="FOUND",
                          captured="2026-09-20T00:00:00Z")
        s = recon([db, shot], rid="backup_1")
        self.assertEqual((s.state, s.provisional), ("BLOCKED", False))
        self.assertTrue(any("screenshot" in n for n in s.notes))
        self.assertEqual(len(s.observations), 2)  # still listed for transparency

    def test_a_screenshot_alone_cannot_prove_a_block_or_a_disabled_flow_by_itself_being_trusted(self):
        s = recon([image_fact(resource="data_flow_1", record_type="DATA_FLOW", state="ACTIVE")], rid="data_flow_1")
        self.assertTrue(s.provisional)

    def test_a_later_screenshot_that_contradicts_firm_absence_makes_the_state_unknown(self):
        absent = fact("evidence_01", state="NOT_FOUND", captured="2026-09-15T00:00:00Z")
        shown = image_fact(evidence_id="evidence_04", state="FOUND", captured="2026-09-26T00:00:00Z")
        s = recon([absent, shown])
        self.assertEqual((s.state, s.provisional_conflict, s.exists), ("UNKNOWN", True, None))
        self.assertTrue(any("contradicting the firm evidence" in c for c in s.conflicts))
        self.assertEqual(len(s.observations), 2)

    def test_an_earlier_screenshot_is_simply_superseded(self):
        shown = image_fact(evidence_id="evidence_04", state="FOUND", captured="2026-09-01T00:00:00Z")
        absent = fact("evidence_01", state="NOT_FOUND", captured="2026-09-15T00:00:00Z")
        s = recon([shown, absent])
        self.assertEqual((s.state, s.provisional_conflict), ("NOT_FOUND", False))

    def test_a_later_screenshot_that_agrees_or_only_adds_detail_changes_nothing(self):
        db = fact("evidence_01", "backup_1", "BACKUP_ARCHIVE", "BLOCKED", parser="sql")
        listed = image_fact(evidence_id="evidence_09", resource="backup_1", record_type="BACKUP_ARCHIVE", state="FOUND",
                            captured="2026-09-20T00:00:00Z")
        s = recon([db, listed], rid="backup_1")
        self.assertEqual((s.state, s.provisional_conflict), ("BLOCKED", False))
        found = fact("evidence_01", state="FOUND")
        same = image_fact(evidence_id="evidence_09", state="FOUND", captured="2026-09-20T00:00:00Z")
        self.assertEqual(recon([found, same]).state, "FOUND")

    def test_a_later_screenshot_with_a_different_live_state_is_a_contradiction(self):
        off = fact("evidence_01", "data_flow_1", "DATA_FLOW", "DISABLED")
        on = image_fact(evidence_id="evidence_14", resource="data_flow_1", record_type="DATA_FLOW", state="ACTIVE",
                        captured="2026-09-20T00:00:00Z")
        s = recon([off, on], rid="data_flow_1")
        self.assertEqual((s.state, s.provisional_conflict), ("UNKNOWN", True))

    def test_image_facts_never_prove_absence_by_omission(self):
        s = recon([], [coverage(parser="image")], anchors={A02})
        self.assertEqual(s.state, "UNKNOWN")


class ClaimTests(unittest.TestCase):
    def test_claims_are_attached_but_never_change_state(self):
        s = recon([fact(state="FOUND")], claims=[claim("SUCCESS")])
        self.assertEqual((s.state, len(s.claims)), ("FOUND", 1))
        self.assertEqual(recon([], claims=[claim("SUCCESS")]).state, "UNKNOWN")  # "deleted" claim is not absence

    def test_provenance_survives_into_observations(self):
        s = recon([fact("evidence_07", state="FOUND", locator="records[3]")])
        self.assertEqual((s.observations[0].evidence_id, s.observations[0].locator, s.observations[0].parser),
                         ("evidence_07", "records[3]", "json"))


class AggregateEdgeTests(unittest.TestCase):
    def test_no_observations(self):
        self.assertEqual(aggregate("USER_PROFILE", [])[0], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
