"""The participant-written evaluation cases: expected results come from the rules, the engine must match."""

import unittest

from evaluation import custom_cases


class CustomCaseTests(unittest.TestCase):
    def test_there_are_at_least_three_cases_with_distinct_names(self):
        names = [c.name for c in custom_cases.CUSTOM_CASES]
        self.assertGreaterEqual(len(names), 3)
        self.assertEqual(len(names), len(set(names)))

    def test_each_case_matches_its_expected_outcome(self):
        for result in custom_cases.run_all():
            with self.subTest(result["name"]):
                self.assertEqual((result["status"], result["assessment"], result["steps"]),
                                 (result["expected_status"], result["expected_assessment"], result["expected_steps"]))

    def test_the_cases_cover_every_output_status_and_both_incomplete_assessments(self):
        results = custom_cases.run_all()
        self.assertEqual({r["status"] for r in results}, {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"})
        self.assertEqual({r["assessment"] for r in results if r["status"] == "INCOMPLETE"}, {"PROVEN_VIOLATION", "UNRESOLVED"})


if __name__ == "__main__":
    unittest.main()
