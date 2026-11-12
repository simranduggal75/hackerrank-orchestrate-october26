"""Planned steps: citation policy, ordering, dependency exceptions, replica copies, no fabrication."""

import json
import unittest

import policy
from planner import PlannedStep, order_steps, sort_key
from tests.decision_fixtures import Scenario, compliant_world, owned
from tests.footprint_fixtures import EARLY, activity_row


def step(phase, op, system="svc", resource="r1", evidence=""):
    return PlannedStep(phase, system, resource, op, policy.OP_STATE[op], evidence)


def by_phase(decision):
    out = {}
    for s in decision.steps:
        out.setdefault(s["phase"], []).append(s)
    return out


class CitationPolicyTests(unittest.TestCase):
    def test_a_complete_case_has_only_cited_verification_steps(self):
        steps = Scenario(compliant_world()).decide().steps
        self.assertTrue(steps)
        self.assertTrue(all(s["phase"] == "VERIFICATION" and s["evidence_id"] for s in steps))

    def test_an_incomplete_case_cites_findings_and_leaves_every_later_step_empty(self):
        phases = by_phase(Scenario([owned(state="FOUND")]).decide())
        self.assertTrue(all(s["evidence_id"] for s in phases["FINDING"]))
        self.assertTrue(all(s["evidence_id"] == "" for s in phases["REMEDIATION"] + phases["VERIFICATION"]))

    def test_the_current_state_evidence_of_a_verification_is_kept_internally(self):
        extra = [owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", "evidence_03", attrs={"retention_until": "2027-01-01T00:00:00Z"})]
        decision = Scenario([owned(state="FOUND")] + extra).decide()
        checks = [p for p in decision.planned if p.phase == "VERIFICATION"]
        self.assertTrue(all(p.evidence_id == "" for p in checks))
        retained = next(p for p in checks if p.resource_id == "audit_record_1")
        self.assertEqual((retained.check_class, retained.supporting_evidence), ("existing_compliant", "evidence_03"))

    def test_not_authorized_has_no_steps(self):
        decision = Scenario(compliant_world(), text="Please correct my surname.").decide()
        self.assertEqual((decision.status, decision.steps, decision.steps_json), ("NOT_APPLICABLE", [], "[]"))


class OrderingTests(unittest.TestCase):
    def test_phase_then_contract_operation_then_system_then_resource(self):
        shuffled = [step("VERIFICATION", "VERIFY_RECORD_ABSENT", "b", "r2"), step("FINDING", "REPORT_EXPOSED_PII", "a", "r1"),
                    step("REMEDIATION", "DELETE_RECORD", "b", "r1"), step("FINDING", "REPORT_REMAINING_RECORD", "z", "r9"),
                    step("REMEDIATION", "DISABLE_DATA_FLOW", "z", "r9"), step("VERIFICATION", "VERIFY_RECORD_ABSENT", "a", "r3")]
        ordered = [(s.phase, s.operation, s.system_id, s.resource_id) for s in order_steps(shuffled)]
        self.assertEqual(ordered, sorted(ordered, key=lambda t: (policy.PHASE_RANK[t[0]], policy.OP_RANK[t[1]], t[2], t[3])))
        self.assertEqual(ordered[0][1], "REPORT_REMAINING_RECORD")
        self.assertEqual(ordered[2][1], "DISABLE_DATA_FLOW")  # a flow is stopped before records change

    def test_resource_ids_sort_as_plain_strings(self):
        steps = [step("FINDING", "REPORT_REMAINING_RECORD", "svc", rid) for rid in ("account_10", "account_9", "account_100")]
        self.assertEqual([s.resource_id for s in order_steps(steps)], ["account_10", "account_100", "account_9"])

    def test_ordering_is_independent_of_input_order(self):
        steps = [step("REMEDIATION", "DELETE_RECORD", "svc", f"r{i}") for i in range(5)]
        self.assertEqual(order_steps(steps), order_steps(list(reversed(steps))))

    def test_an_evidence_backed_edge_moves_a_downstream_copy_before_its_source(self):
        replica = step("REMEDIATION", "DELETE_RECORD", "zz_replica", "r1")
        primary = step("REMEDIATION", "DELETE_RECORD", "aa_primary", "r1")
        self.assertEqual(order_steps([replica, primary])[0], primary)  # plain sort
        edges = {(("zz_replica", "r1"), ("aa_primary", "r1"))}
        self.assertEqual(order_steps([replica, primary], edges)[0], replica)

    def test_a_dependency_cycle_cannot_loop_forever(self):
        a, b = step("REMEDIATION", "DELETE_RECORD", "s1", "r1"), step("REMEDIATION", "DELETE_RECORD", "s2", "r2")
        edges = {(("s1", "r1"), ("s2", "r2")), (("s2", "r2"), ("s1", "r1"))}
        self.assertEqual(len(order_steps([a, b], edges)), 2)

    def test_sort_key_uses_only_contract_fields(self):
        self.assertEqual(sort_key(step("FINDING", "REPORT_REMAINING_RECORD", "s", "r")), (0, 0, "s", "r"))

    def test_a_flow_is_disabled_before_the_account_record_is_deleted(self):
        world = [owned(state="FOUND"), owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_03")]
        ops = [s["operation"] for s in Scenario(world).decide().steps if s["phase"] == "REMEDIATION"]
        self.assertLess(ops.index("DISABLE_DATA_FLOW"), ops.index("DELETE_RECORD"))


class StepContentTests(unittest.TestCase):
    def test_each_violation_yields_finding_remediation_and_verification_of_the_contract_kind(self):
        cases = {("USER_PROFILE", "FOUND"): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
                 ("ANALYTICS_PROFILE", "IDENTIFIABLE"): ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
                 ("DATA_FLOW", "ACTIVE"): ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
                 ("USER_EXPORT", "FOUND"): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")}
        for (rtype, state), expected in cases.items():
            decision = Scenario(compliant_world() + [owned("thing_1", rtype, state, "evidence_03")]).decide()
            ops = [s["operation"] for s in decision.steps if s["resource_id"] == "thing_1"]
            self.assertEqual(ops, list(expected), rtype)

    def test_every_planned_step_is_in_the_contract_vocabulary(self):
        world = [owned(state="FOUND"), owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_03"),
                 owned("analytics_1", "ANALYTICS_PROFILE", "IDENTIFIABLE", "evidence_04")]
        for s in Scenario(world).decide().steps:
            self.assertIn((s["phase"], s["operation"], s["state"]), policy.ALLOWED_PAIRS)

    def test_step_numbers_are_consecutive_and_json_is_compact_and_valid(self):
        decision = Scenario([owned(state="FOUND")]).decide()
        self.assertEqual([s["step_no"] for s in decision.steps], list(range(1, len(decision.steps) + 1)))
        self.assertEqual(json.loads(decision.steps_json), decision.steps)
        self.assertNotIn(": ", decision.steps_json)

    def test_no_step_is_invented_for_an_unmapped_or_unlinked_record(self):
        decision = Scenario(compliant_world() + [owned("lookup_1", "UNMAPPED_LOOKUP", "UNKNOWN", "evidence_03")]).decide()
        self.assertNotIn("lookup_1", {s["resource_id"] for s in decision.steps})
        stray = owned("account_9", "USER_PROFILE", "FOUND", "evidence_05", idents=())  # no identifier: not the subject's
        self.assertNotIn("account_9", {s["resource_id"] for s in Scenario(compliant_world() + [stray]).decide().steps})

    def test_unresolved_resources_get_no_finding_or_remediation(self):
        decision = Scenario(compliant_world() + [owned("support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", "evidence_03")]).decide()
        self.assertEqual(decision.assessment.assessment, "UNRESOLVED")
        self.assertFalse([s for s in decision.steps if s["resource_id"] == "support_1"])

    def test_steps_are_deterministic(self):
        world = [owned(state="FOUND"), owned("sync_1", "DATA_FLOW", "ACTIVE", "evidence_03")]
        self.assertEqual(Scenario(world).decide().steps_json, Scenario(list(reversed(world))).decide().steps_json)


class ReplicaStepTests(unittest.TestCase):
    def test_a_surviving_replica_copy_shares_the_resources_single_set_of_steps(self):
        primary = owned(state="NOT_FOUND", store="customer_data_primary")
        replica = owned(state="FOUND", evidence_id="evidence_02", store="customer_data_failover_replica")
        named = activity_row("activity_2", EARLY, "SYNC_REPLICA", "replication_service", "account_failover_replica", "account_1")
        decision = Scenario([primary, replica], activity=[named]).decide()
        self.assertEqual([(s["phase"], s["operation"], s["system_id"], s["evidence_id"]) for s in decision.steps],
                         [("FINDING", "REPORT_REMAINING_RECORD", "account_service", "evidence_02"),
                          ("REMEDIATION", "DELETE_RECORD", "account_service", ""),
                          ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "")])


if __name__ == "__main__":
    unittest.main()
