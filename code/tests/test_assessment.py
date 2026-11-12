"""Assessments, the evidence-coverage gate G0-G8, and condition-aware verdicts, on synthetic worlds."""

import unittest

from assessment import evaluate_gates
from footprint import Footprint, Unresolved
from intent import classify_intent
from tests.decision_fixtures import DELETE_TEXT, LATER, SAM, Scenario, compliant_world, owned
from tests.footprint_fixtures import AUDIT, EARLY, activity_row, image_fact, link_row


def failed(assessment):
    return {g.gate: [r.code for r in g.reasons] for g in assessment.failed_gates}


def verdicts(scenario):
    return {v.resource_id: v for v in scenario.assess()[0].verdicts}


class AssessmentKindTests(unittest.TestCase):
    def test_the_four_assessments_are_distinct_and_map_to_the_right_status(self):
        complete = Scenario(compliant_world()).assess()[0]
        violation = Scenario([owned(state="FOUND")]).assess()[0]
        unresolved = Scenario([]).assess()[0]
        refused = Scenario(compliant_world(), text="Please send me a copy of my data.").assess()[0]
        self.assertEqual([a.assessment for a in (complete, violation, unresolved, refused)],
                         ["PROVEN_COMPLETE", "PROVEN_VIOLATION", "UNRESOLVED", "NOT_AUTHORIZED"])
        self.assertEqual([a.status for a in (complete, violation, unresolved, refused)],
                         ["COMPLETE", "INCOMPLETE", "INCOMPLETE", "NOT_APPLICABLE"])
        self.assertNotEqual(violation.assessment, unresolved.assessment)  # same output status, different meaning

    def test_unresolved_is_never_reported_as_a_violation(self):
        decision = Scenario([]).decide()
        self.assertEqual(decision.status, "INCOMPLETE")
        self.assertFalse([s for s in decision.steps if s["phase"] == "FINDING"])


class GateTests(unittest.TestCase):
    def test_g0_not_authorized(self):
        a = Scenario(compliant_world(), text="Could you export my data?").assess()[0]
        self.assertIn("G0", failed(a))
        self.assertEqual(a.verdicts, ())

    def test_g0_unresolved_subject(self):
        scenario = Scenario(compliant_world())
        resolution = scenario.world.identity.resolve_email("nobody@example.org", AUDIT, "case_x")
        footprint = scenario.world.builder.build(resolution, "strict")
        gates = evaluate_gates(classify_intent(DELETE_TEXT), footprint, ())
        self.assertIn("subject_unresolved", [r.code for g in gates for r in g.reasons])

    def test_g1_needs_an_account_that_is_proven_absent(self):
        self.assertIn("G1", failed(Scenario([]).assess()[0]))
        self.assertEqual(failed(Scenario(compliant_world()).assess()[0]), {})
        # An account seen only in a screenshot is not proven absent.
        shot = Scenario([image_fact(resource="account_1", state="NOT_FOUND", idents=(SAM,))]).assess()[0]
        self.assertIn("G1", failed(shot))
        self.assertIn("G3", failed(shot))

    def test_g2_a_proven_violation_fails_the_gate(self):
        a = Scenario([owned(state="FOUND")]).assess()[0]
        self.assertEqual(list(failed(a)), ["G2"])

    def test_g3_a_state_that_cannot_be_determined_is_not_compliant(self):
        a = Scenario([owned(state="NOT_FOUND"), owned("account_2", "USER_PROFILE", "UNKNOWN", "evidence_02")]).assess()[0]
        self.assertIn("G3", failed(a))
        self.assertEqual(a.assessment, "UNRESOLVED")

    def test_g4_an_active_flow_is_a_violation_and_an_unreadable_backup_is_unresolved(self):
        flow = Scenario(compliant_world() + [owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_03")]).assess()[0]
        self.assertEqual(flow.assessment, "PROVEN_VIOLATION")
        backup = Scenario(compliant_world() + [owned("backup_1", "BACKUP_ARCHIVE", "FOUND", "evidence_03")]).assess()[0]
        self.assertEqual((backup.assessment, list(failed(backup))), ("UNRESOLVED", ["G4"]))

    def test_g4_a_disabled_flow_and_blocked_backup_pass(self):
        a = Scenario(compliant_world() + [owned("sync_1", "DATA_FLOW", "DISABLED", "evidence_03"),
                                          owned("backup_1", "BACKUP_ARCHIVE", "BLOCKED", "evidence_04")]).assess()[0]
        self.assertEqual(a.assessment, "PROVEN_COMPLETE")

    def test_g5_a_retained_record_that_is_missing_is_a_violation_only_when_its_period_is_shown(self):
        base = compliant_world()
        kept = owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", "evidence_03", attrs={"retention_until": "2027-01-01T00:00:00Z"})
        self.assertEqual(Scenario(base + [kept]).assess()[0].assessment, "PROVEN_COMPLETE")
        gone = owned("audit_record_1", "SECURITY_AUDIT_LOG", "NOT_FOUND", "evidence_03", attrs={"retention_until": "2027-01-01T00:00:00Z"})
        self.assertEqual(Scenario(base + [gone]).assess()[0].assessment, "PROVEN_VIOLATION")
        undated = owned("audit_record_1", "SECURITY_AUDIT_LOG", "NOT_FOUND", "evidence_03")
        a = Scenario(base + [undated]).assess()[0]
        self.assertEqual((a.assessment, list(failed(a))), ("UNRESOLVED", ["G5"]))  # a retained record, not shown to be required

    def test_g5_an_expired_retention_period_does_not_require_the_record(self):
        expired = owned("audit_record_1", "SECURITY_AUDIT_LOG", "NOT_FOUND", "evidence_03", attrs={"retention_until": "2026-01-01T00:00:00Z"})
        v = verdicts(Scenario(compliant_world() + [expired]))["audit_record_1"]
        self.assertEqual((v.condition, v.kind), ("refuted", "UNRESOLVED"))

    def test_g6_an_unmapped_record_type_blocks_complete_and_is_recorded_as_a_gap(self):
        a = Scenario(compliant_world() + [owned("lookup_1", "UNMAPPED_LOOKUP", "UNKNOWN", "evidence_03")]).assess()[0]
        self.assertEqual((a.assessment, list(failed(a))), ("UNRESOLVED", ["G6"]))
        self.assertIn("unmapped_type", [r.code for r in a.gaps])

    def test_g7_a_rule_condition_that_is_not_established_leaves_the_case_unresolved(self):
        a = Scenario(compliant_world() + [owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03")]).assess()[0]
        self.assertEqual((a.assessment, list(failed(a))), ("UNRESOLVED", ["G7"]))

    def test_g8_a_hiding_risk_signal_blocks_complete(self):
        resolution, footprint = Scenario(compliant_world()).footprint()
        footprint.unresolved.append(Unresolved("late_file_link", "account_9", "linked only by a file captured after audit_at"))
        gates = evaluate_gates(classify_intent(DELETE_TEXT), footprint, ())
        self.assertIn("G8", [g.gate for g in gates if not g.passed])

    def test_a_note_that_is_not_a_hiding_risk_does_not_block_complete(self):
        resolution, footprint = Scenario(compliant_world()).footprint()
        footprint.unresolved.append(Unresolved("convention_candidate", "account_9", "a naming-convention candidate"))
        gates = evaluate_gates(classify_intent(DELETE_TEXT), footprint, ())
        self.assertNotIn("G8", [g.gate for g in gates if not g.passed])

    def test_every_gate_is_reported_for_every_case(self):
        self.assertEqual([g.gate for g in Scenario(compliant_world()).assess()[0].gates],
                         ["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"])


class TemporalTests(unittest.TestCase):
    def test_a_file_captured_after_the_audit_never_establishes_state(self):
        late = owned(state="FOUND", captured=LATER)
        a = Scenario(compliant_world() + [late], audit_at=AUDIT).assess()[0]
        self.assertEqual(a.assessment, "PROVEN_COMPLETE")

    def test_a_later_record_inside_an_early_file_is_not_used(self):
        later_record = owned("account_1", state="FOUND", evidence_id="evidence_02", captured=EARLY, observed=LATER)
        a = Scenario(compliant_world() + [later_record]).assess()[0]
        self.assertEqual(a.assessment, "PROVEN_COMPLETE")

    def test_the_same_world_gives_a_different_answer_at_a_later_audit(self):
        facts = [owned(state="NOT_FOUND", captured=EARLY), owned(state="FOUND", evidence_id="evidence_02", captured=LATER)]
        before = Scenario(facts, audit_at=AUDIT).assess()[0]
        after = Scenario(facts, audit_at="2026-10-30T00:00:00Z").assess()[0]
        self.assertEqual((before.assessment, after.assessment), ("PROVEN_COMPLETE", "PROVEN_VIOLATION"))

    def test_activity_claims_never_establish_state(self):
        claim = activity_row("activity_1", EARLY, "DELETE_RECORD", "deletion_worker", "account_service", "account_1")
        a = Scenario([owned(state="FOUND")], activity=[claim]).assess()[0]
        self.assertEqual(a.assessment, "PROVEN_VIOLATION")  # "SUCCESS" is a claim; the record is still there
        absent_only_by_claim = Scenario([owned("account_1", state="UNKNOWN")], activity=[claim]).assess()[0]
        self.assertNotEqual(absent_only_by_claim.assessment, "PROVEN_COMPLETE")


class ConditionTests(unittest.TestCase):
    def test_shared_context_in_the_message_text_makes_redaction_required(self):
        ordered = owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03", attrs={"message_text": "Where is order ORD-4411?"})
        v = verdicts(Scenario(compliant_world() + [ordered]))["support_1"]
        self.assertEqual((v.kind, v.condition), ("VIOLATION", "established"))
        plain = owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03", attrs={"message_text": "Thanks, bye."})
        self.assertEqual(verdicts(Scenario(compliant_world() + [plain]))["support_1"].kind, "UNRESOLVED")

    def test_a_redacted_message_is_compliant_and_verified_either_way(self):
        done = owned("support_1", "SUPPORT_MESSAGE", "REDACTED", "evidence_03")
        v = verdicts(Scenario(compliant_world() + [done]))["support_1"]
        self.assertEqual((v.kind, v.verify), ("COMPLIANT", True))

    def test_a_message_with_no_direct_identifier_is_verified_as_redacted_without_establishing_the_condition(self):
        clean = owned("support_1", "SUPPORT_MESSAGE", "NO_DIRECT_IDENTIFIER", "evidence_03", attrs={"message_text": "Thanks, bye."})
        scenario = Scenario(compliant_world() + [clean])
        v = verdicts(scenario)["support_1"]
        self.assertEqual((v.kind, v.condition, v.verify), ("COMPLIANT", "unestablished", True))
        steps = [s for s in scenario.decide().steps if s["resource_id"] == "support_1"]
        self.assertEqual([(s["operation"], s["evidence_id"]) for s in steps], [("VERIFY_RECORD_REDACTED", "evidence_03")])

    def test_an_identifiable_message_is_still_never_verified_as_redacted(self):
        exposed = owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03", attrs={"message_text": "Thanks, bye."})
        self.assertFalse([s for s in Scenario(compliant_world() + [exposed]).decide().steps if s["resource_id"] == "support_1"])

    def test_an_attachment_is_deleted_only_when_exclusive_ownership_is_stated(self):
        attachment = owned("object_1", "SUPPORT_ATTACHMENT", "FOUND", "evidence_03")
        plain = verdicts(Scenario(compliant_world() + [attachment]))["object_1"]
        self.assertEqual((plain.kind, plain.condition), ("UNRESOLVED", "unestablished"))
        link = link_row("activity_9", EARLY, "object_1", "support_platform", "att_1", "sam.lee@example.test", "user_1", exclusive=True)
        exclusive = verdicts(Scenario(compliant_world() + [attachment], activity=[link]))["object_1"]
        self.assertEqual((exclusive.kind, exclusive.condition), ("VIOLATION", "established"))

    def test_an_attributable_analytics_profile_must_be_anonymized(self):
        v = verdicts(Scenario(compliant_world() + [owned("analytics_1", "ANALYTICS_PROFILE", "IDENTIFIABLE", "evidence_03")]))["analytics_1"]
        self.assertEqual((v.kind, v.condition), ("VIOLATION", "established"))

    def test_a_tombstone_exposing_identifiers_is_a_violation_only_with_deletion_and_a_retained_record(self):
        tomb = owned("tombstone_1", "ACCOUNT_TOMBSTONE", "IDENTIFIABLE", "evidence_03")
        alone = verdicts(Scenario(compliant_world() + [tomb]))["tombstone_1"]
        self.assertEqual((alone.kind, alone.condition), ("UNRESOLVED", "unestablished"))
        kept = owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", "evidence_04", attrs={"retention_until": "2027-01-01T00:00:00Z"})
        with_retained = verdicts(Scenario(compliant_world() + [tomb, kept]))["tombstone_1"]
        self.assertEqual((with_retained.kind, with_retained.condition), ("VIOLATION", "established"))


class ReplicaTests(unittest.TestCase):
    def replica_world(self, activity=()):
        primary = owned(state="NOT_FOUND", store="customer_data_primary", captured=EARLY)
        replica = owned(state="FOUND", evidence_id="evidence_02", store="customer_data_failover_replica", captured=EARLY)
        return Scenario([primary, replica], activity=activity)

    def test_a_copy_that_survives_in_a_replica_is_a_violation(self):
        a = self.replica_world().assess()[0]
        self.assertEqual(a.assessment, "PROVEN_VIOLATION")
        self.assertEqual([c.evidence_id for v in a.verdicts for c in v.copies], ["evidence_02"])

    def test_a_replica_copy_uses_the_system_recorded_on_its_evidence_row_not_an_activity_claim(self):
        named = activity_row("activity_2", EARLY, "SYNC_REPLICA", "replication_service", "account_failover_replica", "account_1")
        for activity in ([], [named]):
            copies = [c for v in self.replica_world(activity).assess()[0].verdicts for c in v.copies]
            self.assertEqual([(c.system_id, c.role) for c in copies], [("account_service", "failover_replica")])

    def test_a_replica_older_than_a_primary_absence_is_unresolved_not_a_violation(self):
        primary = owned(state="NOT_FOUND", store="customer_data_primary", evidence_id="evidence_01", captured="2026-09-05T00:00:00Z")
        replica = owned(state="FOUND", evidence_id="evidence_02", store="customer_data_replica_ap_south", captured=EARLY)
        a, footprint = Scenario([primary, replica]).assess()
        # The replica may simply lag the primary: neither a remaining copy nor proof of absence.
        self.assertEqual(a.assessment, "UNRESOLVED")
        self.assertIn("stale_conflict", [u.kind for u in footprint.unresolved])


if __name__ == "__main__":
    unittest.main()
