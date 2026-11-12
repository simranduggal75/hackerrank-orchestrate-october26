"""Resource ownership: exact identifiers, link events, ambiguity, time policy, and what is NOT linked."""

import unittest

from evidence.facts import Identifier
from footprint import FootprintBuilder
from identity import email_hash
from tests.footprint_fixtures import (
    AUDIT, World, activity_row, coverage, fact, ident, image_fact, link_row,
)

SAM = "sam.lee@example.test"


def kinds(footprint):
    return [u.kind for u in footprint.unresolved]


class ExactIdentifierTests(unittest.TestCase):
    def test_exact_email_links_regardless_of_case(self):
        w = World([fact(resource="account_1", idents=[ident("email", "SAM.LEE@Example.test")])])
        fp = w.footprint()
        self.assertEqual(list(fp.resources), ["account_1"])
        owned = fp.resources["account_1"]
        self.assertEqual((owned.roles, {l.via for l in owned.links}, owned.shared), (frozenset({"owner"}), {"record"}, False))

    def test_hash_phone_and_user_id_link_too(self):
        facts = [fact("evidence_01", "account_1", idents=[ident("correlation_key", email_hash(SAM), via="record")]),
                 fact("evidence_02", "account_2", idents=[ident("owner_phone", "+44 7700 900001")]),
                 fact("evidence_03", "audit_record_3", "SECURITY_AUDIT_LOG", idents=[ident("subject_reference", "user_1")])]
        self.assertEqual(sorted(World(facts).footprint().resources), ["account_1", "account_2", "audit_record_3"])

    def test_historical_email_links_only_after_it_was_the_users(self):
        old = fact(resource="support_1", record_type="SUPPORT_MESSAGE", idents=[ident("lookup_key", "sam.old@example.test", via="trace")])
        w = World([old])
        self.assertEqual(list(w.footprint(SAM, "2026-09-30T00:00:00Z").resources), ["support_1"])
        # An address first adopted AFTER the audit is not yet the subject's, so it cannot link anything:
        new_only = fact(resource="account_2", idents=[ident("email", SAM)])
        self.assertEqual(World([new_only]).footprint("sam.old@example.test", "2026-05-01T00:00:00Z").resources, {})

    def test_a_lookalike_address_is_somebody_else(self):
        facts = [fact("evidence_01", "search_1", "SEARCH_INDEX_ENTRY", idents=[ident("email", "sam.lee.jr@example.test")]),
                 fact("evidence_02", "search_2", "SEARCH_INDEX_ENTRY", idents=[ident("email", "xsam.lee@example.test")])]
        w = World(facts)
        self.assertEqual(w.footprint(SAM).resources, {})
        self.assertEqual(list(w.footprint("sam.lee.jr@example.test").resources), ["search_1"])

    def test_names_never_link(self):
        w = World([fact(idents=[ident("full_name", "Sam Lee", "name")])])
        self.assertEqual(w.footprint().resources, {})

    def test_uncertain_identifiers_never_link_but_are_reported(self):
        w = World([image_fact(evidence_id="evidence_04", resource="account_5762", idents=[Identifier("customer_reference", "user_1", "uncertain", "ocr")])])
        fp = w.footprint()
        self.assertEqual(fp.resources, {})
        self.assertIn("uncertain_identifier", kinds(fp))

    def test_uncertain_identifier_note_mentions_independent_support(self):
        shot = image_fact(evidence_id="evidence_04", resource="account_5762", idents=[Identifier("customer_reference", "user_1", "uncertain", "ocr")])
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "account_5762", "account_service", None, SAM, "user_1")
        fp = World([shot], activity=[link]).footprint()
        note = next(u for u in fp.unresolved if u.kind == "uncertain_identifier")
        self.assertIn("independently supported by link event activity_1", note.detail)
        self.assertEqual(list(fp.resources), ["account_5762"])  # linked by the explicit event, not by the OCR read

    def test_confirmed_screenshot_identifiers_link_and_stay_provisional(self):
        shot = image_fact(resource="account_5762", idents=[ident("email", SAM, via="ocr")])
        fp = World([shot]).footprint()
        owned = fp.resources["account_5762"]
        self.assertEqual({l.via for l in owned.links}, {"ocr"})
        self.assertTrue(owned.state.provisional)
        self.assertIn("provisional_only", kinds(fp))


class MentionTests(unittest.TestCase):
    def test_an_email_inside_text_is_a_mention_not_ownership(self):
        w = World([fact(resource="support_1", record_type="SUPPORT_MESSAGE", state="IDENTIFIABLE", idents=[
            ident("subject_reference", "user_2"), ident("message_text", SAM, via="text")])])
        fp = w.footprint()
        owned = fp.resources["support_1"]
        self.assertEqual((owned.roles, owned.shared), (frozenset({"mention"}), False))
        self.assertIn("mention_only", kinds(fp))
        self.assertEqual(list(w.footprint("pat.roe@example.test").resources), ["support_1"])  # its owner is user_2

    def test_owner_evidence_outranks_a_mention(self):
        w = World([fact("evidence_01", "support_1", "SUPPORT_MESSAGE", idents=[ident("message_text", SAM, via="text")]),
                   fact("evidence_02", "support_1", "SUPPORT_MESSAGE", idents=[ident("subject_reference", "user_1")])])
        self.assertEqual(w.footprint().resources["support_1"].roles, frozenset({"owner", "mention"}))


class SearchQueryTests(unittest.TestCase):
    def test_a_search_query_is_not_propagated_to_every_hit(self):
        hit = fact("evidence_01", "search_428", "SEARCH_INDEX_ENTRY", idents=[ident("email", SAM)], attrs={"query": SAM})
        decoy = fact("evidence_01", "search_1874", "SEARCH_INDEX_ENTRY", idents=[ident("email", "sam.lee.jr@example.test")], attrs={"query": SAM})
        fp = World([hit, decoy]).footprint()
        self.assertEqual(list(fp.resources), ["search_428"])

    def test_a_trace_key_links_a_single_resource(self):
        f = fact(resource="search_1", record_type="SEARCH_INDEX_ENTRY", idents=[ident("match_key", SAM, via="trace")])
        owned = World([f]).footprint().resources["search_1"]
        self.assertEqual({l.via for l in owned.links}, {"trace"})


class ResourcePropagationTests(unittest.TestCase):
    def test_identifier_free_facts_about_an_owned_resource_supply_its_state(self):
        owner = fact("evidence_01", "account_1", idents=[ident("email", SAM)], captured="2026-09-01T00:00:00Z")
        later_404 = fact("evidence_02", "account_1", state="NOT_FOUND", idents=[], captured="2026-09-10T00:00:00Z")
        fp = World([owner, later_404]).footprint()
        state = fp.resources["account_1"].state
        self.assertEqual((state.state, state.absence), ("NOT_FOUND", "explicit"))

    def test_a_resource_without_any_identifier_link_is_not_guessed(self):
        orphan = fact("evidence_02", "account_77", state="NOT_FOUND", idents=[])
        self.assertEqual(World([orphan]).footprint().resources, {})

    def test_facts_that_name_the_subject_but_have_no_resource_are_listed_separately(self):
        doc = fact("evidence_05", None, "LEGAL_HOLD_RECORD", idents=[ident("subject_reference", "user_1")], attrs={"order_reference": "ORD-1"})
        fp = World([doc]).footprint()
        self.assertEqual(fp.resources, {})
        self.assertEqual([f.attributes["order_reference"] for f in fp.unlinked_facts], ["ORD-1"])


class LinkEventTests(unittest.TestCase):
    def test_a_link_event_owns_a_resource_even_without_any_record(self):
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "tombstone_17", "identity_service", "user_1", SAM, "user_1")
        fp = World(activity=[link]).footprint()
        owned = fp.resources["tombstone_17"]
        self.assertEqual({l.via for l in owned.links}, {"link_event"})
        self.assertEqual((owned.state.state, owned.state.record_type), ("UNKNOWN", "ACCOUNT_TOMBSTONE"))
        self.assertIn("no_observation", kinds(fp))

    def test_exclusive_link_is_flagged(self):
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "object_19", "object_storage", "a.pdf", SAM, "user_1", exclusive=True)
        self.assertTrue(World(activity=[link]).footprint().resources["object_19"].exclusive)

    def test_links_for_other_people_or_recorded_later_are_not_used(self):
        rows = [link_row("activity_1", "2026-09-01T00:00:00Z", "account_5", "account_service", None, "pat.roe@example.test", "user_2"),
                link_row("activity_2", "2026-12-01T00:00:00Z", "search_5", "search_service", None, SAM, "user_1")]
        fp = World(activity=rows).footprint()
        self.assertEqual(fp.resources, {})
        self.assertIn("late_link_event", kinds(fp))

    def test_alias_attaches_a_screenshot_row_that_has_no_resource_id(self):
        shot = image_fact(resource=None, record_type="SUPPORT_ATTACHMENT", system="object_storage", evidence_id="evidence_46",
                          attrs={"file_name": "delivery_confirmation_19.pdf"})
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "object_19", "object_storage", "delivery_confirmation_19.pdf", SAM, "user_1", exclusive=True)
        state = World([shot], activity=[link]).footprint().resources["object_19"].state
        self.assertEqual((state.state, state.provisional, len(state.observations)), ("FOUND", True, 1))
        self.assertEqual(state.observations[0].evidence_id, "evidence_46")

    def test_alias_in_another_system_is_not_merged(self):
        pdf = fact(resource=None, record_type="CUSTOMER_STATEMENT", system="account_history_service", evidence_id="evidence_18",
                   attrs={"statement": "NST-1"}, idents=[ident("document_text", SAM, via="text")])
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "export_1842", "export_service", "NST-1", SAM, "user_1")
        fp = World([pdf], activity=[link]).footprint()
        self.assertIn("alias_other_system", kinds(fp))
        self.assertEqual(fp.resources["export_1842"].state.state, "UNKNOWN")


class AmbiguityTests(unittest.TestCase):
    def test_a_resource_owned_by_two_users_is_flagged_not_silently_assigned(self):
        shared = fact(resource="account_9", idents=[ident("email", SAM), ident("customer_reference", "user_2")])
        fp = World([shared]).footprint()
        owned = fp.resources["account_9"]
        self.assertTrue(owned.shared)
        self.assertEqual(owned.owners, frozenset({"user_1", "user_2"}))
        self.assertIn("ambiguous_owner", kinds(fp))

    def test_a_link_event_for_someone_else_makes_the_resource_ambiguous(self):
        f = fact(resource="account_9", idents=[ident("email", SAM)])
        link = link_row("activity_1", "2026-09-01T00:00:00Z", "account_9", "account_service", None, "pat.roe@example.test", "user_2")
        self.assertTrue(World([f], activity=[link]).footprint().resources["account_9"].shared)

    def test_source_resource_links_a_search_document_to_an_owned_account(self):
        account = fact("evidence_01", "account_6", idents=[ident("email", SAM)])
        doc = fact("evidence_02", "search_1", "SEARCH_INDEX_ENTRY", idents=[ident("display_name", "S Lee", "name")],
                   attrs={"source_resource_id": "account_6"})
        owned = World([account, doc]).footprint().resources["search_1"]
        self.assertEqual({l.via for l in owned.links}, {"source_resource"})

    def test_source_resource_does_not_hide_a_conflicting_owner(self):
        account = fact("evidence_01", "account_6", idents=[ident("email", SAM)])
        doc = fact("evidence_02", "search_1", "SEARCH_INDEX_ENTRY", idents=[ident("email", "pat.roe@example.test")],
                   attrs={"source_resource_id": "account_6"})
        self.assertTrue(World([account, doc]).footprint().resources["search_1"].shared)

    def test_unresolved_request_email_gives_an_empty_footprint(self):
        w = World([fact(idents=[ident("email", SAM)])])
        fp = w.builder.build(w.identity.resolve_email("nobody@example.com", AUDIT, "c"))
        self.assertEqual((fp.user_id, fp.resources, kinds(fp)), (None, {}, ["subject_unresolved"]))


class TimeRuleTests(unittest.TestCase):
    def late_world(self):
        # a file captured after the audit whose record is itself stamped before it
        late = fact("evidence_02", "account_5", idents=[ident("email", SAM)], captured="2026-10-17T00:00:00Z", observed="2026-09-09T00:00:00Z")
        explicit_404 = fact("evidence_01", "account_5", state="NOT_FOUND", idents=[], captured="2026-09-15T00:00:00Z")
        return World([late, explicit_404])

    def test_strict_policy_ignores_a_late_file_and_reports_what_it_would_have_linked(self):
        fp = self.late_world().footprint()
        self.assertEqual(fp.resources, {})
        item = next(u for u in fp.unresolved if u.kind == "late_file_link")
        self.assertEqual((item.resource_id, item.reference), ("account_5", "evidence_02"))

    def test_record_time_policy_learns_ownership_but_never_state_from_the_late_file(self):
        fp = self.late_world().footprint(policy="record_time")
        owned = fp.resources["account_5"]
        self.assertTrue(all(l.late for l in owned.links))
        self.assertEqual({o.evidence_id for o in owned.state.observations}, {"evidence_01"})  # state: allowed file only
        self.assertEqual((owned.state.state, owned.state.absence), ("NOT_FOUND", "explicit"))

    def test_late_record_with_no_other_evidence_leaves_the_state_unknown(self):
        late = fact("evidence_02", "account_5", idents=[ident("email", SAM)], captured="2026-10-17T00:00:00Z", observed="2026-09-09T00:00:00Z")
        fp = World([late]).footprint(policy="record_time")
        self.assertEqual(fp.resources["account_5"].state.state, "UNKNOWN")
        self.assertIn("no_observation", kinds(fp))

    def test_a_record_stamped_after_the_audit_is_never_usable(self):
        f = fact("evidence_02", "account_5", idents=[ident("email", SAM)], captured="2026-10-17T00:00:00Z", observed="2026-10-16T00:00:00Z")
        for policy in ("strict", "record_time"):
            self.assertEqual(World([f]).footprint(policy=policy).resources, {}, policy)

    def test_late_notes_are_not_repeated_for_resources_linked_another_way(self):
        ok = fact("evidence_01", "account_5", idents=[ident("email", SAM)])
        late = fact("evidence_02", "account_5", idents=[ident("email", SAM)], captured="2026-10-17T00:00:00Z", observed="2026-09-09T00:00:00Z")
        self.assertNotIn("late_file_link", kinds(World([ok, late]).footprint()))

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            World().footprint(policy="whenever")


class UnlinkableResourceTests(unittest.TestCase):
    def test_cache_and_email_delivery_resources_exist_only_as_claims_and_never_become_owned(self):
        rows = [activity_row("activity_1", "2026-09-01T00:00:00Z", "REFRESH_CACHE", "account_service", "cache_service", "cache_9558"),
                activity_row("activity_2", "2026-09-01T00:00:00Z", "SEND_EMAIL", "notification_service", "email_delivery_service", "email_delivery_354")]
        fp = World(activity=rows).footprint()
        self.assertEqual(fp.resources, {})

    def test_naming_convention_is_reported_but_never_applied(self):
        rows = [activity_row("activity_1", "2026-09-01T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_001")]
        fp = World(activity=rows).footprint()
        self.assertEqual(fp.resources, {})  # user_1 vs account_001 is only a pattern
        item = next(u for u in fp.unresolved if u.kind == "convention_candidate")
        self.assertEqual((item.resource_id, item.reference), ("account_001", "activity_1"))

    def test_a_deletion_claim_alone_never_creates_ownership_or_state(self):
        rows = [activity_row("activity_1", "2026-09-01T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_5")]
        w = World([fact("evidence_01", "account_5", idents=[ident("email", SAM)])], activity=rows)
        state = w.footprint().resources["account_5"].state
        self.assertEqual((state.state, len(state.claims)), ("FOUND", 1))  # the claim says SUCCESS; the record is still there


class PrecisionTests(unittest.TestCase):
    def test_two_subjects_with_interleaved_activity_stay_separate(self):
        facts = [fact("evidence_01", "account_1", idents=[ident("email", SAM)]),
                 fact("evidence_01", "account_2", idents=[ident("email", "pat.roe@example.test")])]
        rows = [activity_row("activity_1", "2026-09-02T11:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_1"),
                activity_row("activity_2", "2026-09-02T11:01:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_2")]
        w = World(facts, activity=rows)
        self.assertEqual(list(w.footprint(SAM).resources), ["account_1"])
        self.assertEqual(list(w.footprint("pat.roe@example.test").resources), ["account_2"])


if __name__ == "__main__":
    unittest.main()
