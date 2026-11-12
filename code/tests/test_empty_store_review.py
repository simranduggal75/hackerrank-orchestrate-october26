"""Facts behind the rejection of the `empty_store` alternative, pinned so the decision stays evidence-based.

These are properties of the supplied evidence and of the experimental output, not of any sample answer. If the dataset
changed so that one stopped holding, the review would have to be redone.
"""

import glob
import sqlite3
import unittest
from pathlib import Path

from evaluation import experiments
from evaluation.report import VARIANT_NOTES
from evidence.store import extract_all
from loaders import DATASET_DIR, load_dataset
from tests.helpers import NEEDS_DATASET


@NEEDS_DATASET
class EmptyStoreReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.review = experiments.empty_store_review(cls.ds, extract_all(cls.ds.evidence_files))

    def test_snapshot_entries_carry_no_completeness_or_scope_field(self):
        self.assertEqual(self.review["snapshot_key_sets"], [("captured_at", "collection", "data_store")])

    def test_one_store_reports_different_counts_at_different_times_inside_a_bundle(self):
        self.assertGreater(self.review["stores_with_varying_counts_in_one_bundle"], 0)

    def test_no_account_collection_is_a_platform_wide_inventory(self):
        self.assertGreater(self.review["identities"], 100 * self.review["max_rows_in_a_collection"])

    def test_the_sql_scope_table_names_systems_and_a_time_only(self):
        paths = sorted(glob.glob(str(DATASET_DIR / "evidence" / "databases" / "customer_data_inventory_*.sql")))
        self.assertTrue(paths)
        for path in paths:
            con = sqlite3.connect(":memory:")
            con.executescript(Path(path).read_text(encoding="utf-8"))
            columns = [c[1] for c in con.execute("pragma table_info(snapshot_scope)")]
            self.assertEqual(columns, ["origin_system", "captured_at"], path)

    def test_no_subject_has_independent_evidence_that_could_confirm_an_empty_collection(self):
        self.assertGreater(self.review["subject_file_pairs"], 100)
        self.assertEqual(self.review["pairs_with_independent_account_evidence"], 0)

    def test_the_variant_would_complete_cases_without_any_deletion_verification(self):
        self.assertGreater(self.review["flipped"], 0)
        self.assertEqual(self.review["flipped_with_account_step"], 0)
        self.assertNotIn("VERIFY_RECORD_ABSENT", self.review["flipped_ops"])

    def test_the_variant_is_recorded_as_rejected_and_stays_out_of_production(self):
        self.assertIn("Rejected after review", VARIANT_NOTES["empty_store"])
        self.assertIn("REJECTED", experiments.__doc__)


if __name__ == "__main__":
    unittest.main()
