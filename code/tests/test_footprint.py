"""Footprints over the real dataset: properties that must hold for every case, plus known cases."""

import unittest

from activity import ActivityIndex
from evidence.store import extract_all
from footprint import POLICIES, FootprintBuilder
from identity import IdentityIndex
from loaders import load_dataset
from tests.helpers import NEEDS_DATASET

STATES = {"FOUND", "NOT_FOUND", "ACTIVE", "DISABLED", "BLOCKED", "IDENTIFIABLE", "ANONYMIZED", "REDACTED",
          "NO_DIRECT_IDENTIFIER", "UNKNOWN"}


@NEEDS_DATASET
class RealFootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.identity = IdentityIndex(cls.ds.identities, cls.ds.audit_log)
        cls.store = extract_all(cls.ds.evidence_files)
        cls.activity = ActivityIndex(cls.ds.activity)
        cls.builder = FootprintBuilder(cls.store, cls.identity, cls.activity)
        cls.cases = {c.case_id: c for c in cls.ds.requests + cls.ds.sample_requests}
        cls.fp = {p: {cid: cls.builder.build(cls.identity.resolve_request(c), p) for cid, c in cls.cases.items()} for p in POLICIES}
        cls.has_images = bool(cls.store.results["evidence_04"].facts)

    def by_user(self, user_id):
        """The case whose request email is this user's current email in the identity table."""
        email = next(i.email for i in self.ds.identities if i.user_id == user_id).lower()
        return next(c for c in self.cases.values() if c.email.lower() == email)

    # ---- properties over every case ----------------------------------------------------

    def test_every_case_builds_under_both_policies_and_is_deterministic(self):
        for policy in POLICIES:
            self.assertEqual(len(self.fp[policy]), 550)
            again = {cid: self.builder.build(self.identity.resolve_request(c), policy) for cid, c in self.cases.items()}
            for cid, fp in self.fp[policy].items():
                self.assertEqual(sorted(fp.resources), sorted(again[cid].resources), cid)
                self.assertEqual({r: o.state.state for r, o in fp.resources.items()},
                                 {r: o.state.state for r, o in again[cid].resources.items()}, cid)

    def test_no_observation_or_claim_postdates_the_audit(self):
        for policy in POLICIES:
            for cid, fp in self.fp[policy].items():
                for rid, owned in fp.resources.items():
                    for o in owned.state.observations:
                        self.assertLessEqual(o.captured_at, fp.as_of, (policy, cid, rid))
                        self.assertLessEqual(o.observed_at, fp.as_of, (policy, cid, rid))
                    for claim in owned.state.claims:
                        self.assertLessEqual(claim.event_time, fp.as_of, (policy, cid, rid))

    def test_strict_links_are_never_late_and_strict_is_a_subset_of_record_time(self):
        for cid, strict in self.fp["strict"].items():
            relaxed = self.fp["record_time"][cid]
            self.assertTrue(set(strict.resources) <= set(relaxed.resources), cid)
            for owned in strict.resources.values():
                self.assertFalse(any(l.late for l in owned.links), cid)

    def test_states_use_the_documented_vocabulary(self):
        for fp in self.fp["strict"].values():
            for owned in fp.resources.values():
                self.assertIn(owned.state.state, STATES)
                self.assertEqual(owned.state.exists is None, owned.state.state == "UNKNOWN")

    def test_activity_only_resources_are_never_owned(self):
        for fp in self.fp["record_time"].values():
            for rid in fp.resources:
                self.assertFalse(rid.startswith(("cache_", "email_delivery_")), rid)

    def test_a_shared_resource_always_names_at_least_two_owners(self):
        for fp in self.fp["strict"].values():
            for owned in fp.resources.values():
                if owned.shared:
                    self.assertGreaterEqual(len(owned.owners), 2)
                    self.assertIn(fp.user_id, owned.owners)

    def test_every_case_resolves_to_a_subject(self):
        self.assertTrue(all(fp.user_id for fp in self.fp["strict"].values()))

    def test_the_late_file_policy_adds_resources(self):
        strict = sum(len(fp.resources) for fp in self.fp["strict"].values())
        relaxed = sum(len(fp.resources) for fp in self.fp["record_time"].values())
        self.assertGreater(relaxed, strict)

    def test_temporal_proximity_is_never_used(self):
        # Cases 36 and 37 share a reported_at; their footprints must not borrow each other's resources.
        a, b = self.fp["strict"]["case_36"], self.fp["strict"]["case_37"]
        self.assertTrue(set(a.resources).isdisjoint(b.resources))

    # ---- known cases --------------------------------------------------------------------

    def test_replica_only_account_is_a_remaining_copy(self):
        fp = self.fp["strict"]["case_05"]
        state = fp.resources["account_005"].state
        self.assertEqual(state.state, "FOUND")
        self.assertIn("primary reports absent at 2026-09-02T23:00:00Z", state.conflicts)
        self.assertIn("evidence_54", {o.evidence_id for o in state.observations})
        self.assertEqual({o.kind for o in state.observations}, {"fact", "table_absence"})

    def test_a_late_file_link_is_reported_under_strict_and_used_under_record_time(self):
        strict, relaxed = self.fp["strict"]["case_26"], self.fp["record_time"]["case_26"]
        self.assertNotIn("account_5684", strict.resources)
        self.assertTrue(any(u.kind == "late_file_link" and u.resource_id == "account_5684" for u in strict.unresolved))
        state = relaxed.resources["account_5684"].state
        self.assertEqual((state.state, state.absence), ("NOT_FOUND", "explicit"))
        self.assertEqual({o.evidence_id for o in state.observations}, {"evidence_92"})  # the state comes from an allowed file

    def test_link_event_ownership_of_a_tombstone_the_screenshot_could_not_read(self):
        case = self.by_user("user_17")
        fp = self.fp["strict"][case.case_id]
        self.assertIn("tombstone_17", fp.resources)
        self.assertIn("link_event", {l.via for l in fp.resources["tombstone_17"].links})

    def test_screenshot_ownership_is_confirmed_by_link_event_and_stays_provisional(self):
        if not self.has_images:
            self.skipTest("screenshot facts unavailable (no OCR engine or cache)")
        case = self.by_user("user_11")
        fp = self.fp["strict"][case.case_id]
        owned = fp.resources["account_5762"]
        self.assertTrue({"ocr", "link_event"} <= {l.via for l in owned.links})
        # a 404 lookup (2026-09-15) is contradicted by a LATER screenshot (2026-09-26): neither is believed alone
        state = owned.state
        self.assertEqual((state.state, state.provisional_conflict), ("UNKNOWN", True))
        self.assertEqual({o.parser for o in state.observations}, {"log", "image"})
        self.assertTrue(any(u.kind == "provisional_contradiction" and u.resource_id == "account_5762" for u in fp.unresolved))
        note = next(u for u in fp.unresolved if u.kind == "uncertain_identifier")
        self.assertIn("independently supported by link event activity_4000", note.detail)

    def test_exclusive_object_link_attaches_the_screenshot_row_by_alias(self):
        if not self.has_images:
            self.skipTest("screenshot facts unavailable (no OCR engine or cache)")
        case = self.by_user("user_07")
        owned = self.fp["strict"][case.case_id].resources["object_19"]
        self.assertTrue(owned.exclusive)
        self.assertTrue(any(o.parser == "image" for o in owned.state.observations))
        self.assertTrue(owned.state.provisional)

    def test_register_rows_that_name_a_subject_but_no_resource_are_kept_separate(self):
        case = self.cases["case_08"]  # user_08 appears in the finance register PDF
        fp = self.fp["strict"][case.case_id]
        self.assertTrue(any(f.record_type == "LEGAL_HOLD_RECORD" and f.resource_id is None for f in fp.unlinked_facts))

    def test_a_deletion_claim_for_an_unlinked_account_is_only_a_candidate(self):
        fp = self.fp["strict"]["case_01"]
        self.assertNotIn("account_001", fp.resources)
        self.assertTrue(any(u.kind == "convention_candidate" and u.resource_id == "account_001" for u in fp.unresolved))


if __name__ == "__main__":
    unittest.main()
