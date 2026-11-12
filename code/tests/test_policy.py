"""The policy table: contract vocabulary, and agreement with the supplied deletion_rules.csv."""

import unittest

import policy
from loaders import load_dataset
from tests.helpers import NEEDS_DATASET

CONTRACT_PAIRS = {
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"), ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"), ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"), ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"), ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"), ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"), ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"), ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
}


class VocabularyTests(unittest.TestCase):
    def test_the_allowed_pairs_are_exactly_the_contract_tables(self):
        self.assertEqual(set(policy.ALLOWED_PAIRS), CONTRACT_PAIRS)
        self.assertEqual(len(policy.ALLOWED_PAIRS), 15)

    def test_each_operation_has_one_phase_and_one_state(self):
        self.assertEqual(len(policy.OP_PHASE), 15)
        for phase, op, state in CONTRACT_PAIRS:
            self.assertEqual((policy.OP_PHASE[op], policy.OP_STATE[op]), (phase, state))

    def test_wrong_state_or_phase_for_an_operation_is_not_allowed(self):
        for pair in (("FINDING", "DELETE_RECORD", "ABSENT"), ("REMEDIATION", "DELETE_RECORD", "PRESENT"),
                     ("VERIFICATION", "VERIFY_RECORD_ABSENT", "PRESENT"), ("FINDING", "REPORT_RESTORE_RISK", "PRESENT"),
                     ("REMEDIATION", "VERIFY_RECORD_ABSENT", "ABSENT"), ("VERIFICATION", "FIX_EVERYTHING", "DONE")):
            self.assertNotIn(pair, policy.ALLOWED_PAIRS)

    def test_phases_and_operations_have_a_total_order(self):
        self.assertEqual(policy.PHASES, ("FINDING", "REMEDIATION", "VERIFICATION"))
        self.assertEqual(sorted(policy.PHASE_RANK, key=policy.PHASE_RANK.get), list(policy.PHASES))
        self.assertEqual(len(set(policy.OP_RANK.values())) , 6)  # ranks restart per phase

    def test_step_fields_and_statuses(self):
        self.assertEqual(policy.STEP_FIELDS, ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"))
        self.assertEqual(policy.STATUSES, ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"))


class CategoryTests(unittest.TestCase):
    def test_every_category_uses_only_contract_operations_of_the_right_phase(self):
        for name, entry in policy.CATEGORIES.items():
            for op, phase in ((entry.finding_op, "FINDING"), (entry.remediation_op, "REMEDIATION"), (entry.verify_op, "VERIFICATION")):
                if op:
                    self.assertEqual(policy.OP_PHASE[op], phase, (name, op))

    def test_a_category_with_a_violation_state_has_a_finding_and_a_fix(self):
        for name, entry in policy.CATEGORIES.items():
            if entry.violating:
                self.assertTrue(entry.finding_op and entry.remediation_op, name)
            if entry.missing:  # a retained record that must exist is verified, never removed or reported as remaining
                self.assertIsNone(entry.remediation_op, name)
                self.assertEqual(entry.verify_op, "VERIFY_RECORD_PRESENT", name)

    def test_compliant_and_violating_states_never_overlap(self):
        for name, entry in policy.CATEGORIES.items():
            self.assertFalse(entry.compliant & entry.violating, name)
            self.assertFalse(entry.compliant & entry.missing, name)

    def test_unsupported_types_are_not_also_mapped(self):
        self.assertFalse(set(policy.UNSUPPORTED) & set(policy.CATEGORIES))
        self.assertTrue(all(reason for reason in policy.UNSUPPORTED.values()))

    def test_the_action_decides_the_remediation_operation(self):
        expected = {"DELETE": "DELETE_RECORD", "REDACT": "REDACT_RECORD", "ANONYMIZE": "ANONYMIZE_RECORD",
                    "DISABLE": "DISABLE_DATA_FLOW"}
        for name, entry in policy.CATEGORIES.items():
            if entry.action in expected:
                self.assertEqual(entry.remediation_op, expected[entry.action], name)


@NEEDS_DATASET
class RulesTableAgreementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = {r.rule_id: r for r in load_dataset().rules}

    def test_each_mapped_category_matches_its_rule_row(self):
        for name, entry in policy.CATEGORIES.items():
            if entry.rule_id is None:
                continue
            rule = self.rules[entry.rule_id]
            self.assertEqual(rule.data_category, name)
            self.assertEqual(rule.required_action, entry.action, name)

    def test_every_rule_is_either_mapped_or_recorded_as_unsupported(self):
        for rule in self.rules.values():
            mapped = rule.data_category in policy.CATEGORIES
            self.assertTrue(mapped or rule.data_category in policy.UNSUPPORTED, rule.data_category)

    def test_unconditional_rules_are_the_only_ones_marked_always(self):
        for name, entry in policy.CATEGORIES.items():
            if entry.rule_id is None:
                continue
            always = self.rules[entry.rule_id].condition.lower() == "always"
            self.assertEqual(always, entry.condition == "always", name)


if __name__ == "__main__":
    unittest.main()
