"""G1 diagnostics (classification only) and the replica system_id evidence property."""

import glob
import sqlite3
import unittest
from pathlib import Path

from evaluation import diagnostics as d
from loaders import DATASET_DIR
from tests.decision_fixtures import LATER, Scenario, compliant_world, owned
from tests.footprint_fixtures import coverage
from tests.helpers import NEEDS_DATASET


def audit_record(evidence_id="evidence_03"):
    return owned("audit_record_1", "SECURITY_AUDIT_LOG", "FOUND", evidence_id, attrs={"retention_until": "2027-01-01T00:00:00Z"})


def cause(scenario):
    assessment, footprint = scenario.assess()
    return d.g1_cause(assessment, footprint, scenario.world.store)


class G1CauseTests(unittest.TestCase):
    def test_no_cause_when_g1_passes(self):
        self.assertIsNone(cause(Scenario(compliant_world())))

    def test_an_account_owned_but_not_proven_absent(self):
        self.assertEqual(cause(Scenario([owned("account_1", state="UNKNOWN")])), d.ACCOUNT_OWNED)

    def test_the_only_account_evidence_is_in_a_file_captured_after_the_audit(self):
        # A record observed before the audit sits in a file captured after it: a hiding-risk signal, not usable state.
        late = owned(state="FOUND", evidence_id="evidence_09", captured=LATER, observed="2026-09-09T00:00:00Z")
        self.assertEqual(cause(Scenario([late])), d.LATE_FILE)

    def test_a_file_with_the_subjects_records_and_an_empty_account_store(self):
        scenario = Scenario([audit_record()], coverages=[coverage("evidence_03", store="account_service/customer_profiles", rows=0)])
        self.assertEqual(cause(scenario), d.EMPTY_STORE)

    def test_a_file_with_the_subjects_records_and_an_account_collection_that_lacks_the_subject(self):
        scenario = Scenario([audit_record()], coverages=[coverage("evidence_03", store="customer_profiles_primary", rows=3)])
        self.assertEqual(cause(scenario), d.STORE_WITHOUT_SUBJECT)

    def test_per_resource_lookups_prove_nothing_about_the_subject_even_when_empty_or_non_empty(self):
        for rows in (0, 6):
            scenario = Scenario([audit_record()], coverages=[coverage("evidence_03", store="account_service/profile_lookups", rows=rows)])
            self.assertEqual(cause(scenario), d.LOOKUP_ONLY, rows)

    def test_a_collection_wins_over_lookups_in_the_same_file(self):
        scenario = Scenario([audit_record()], coverages=[coverage("evidence_03", store="account_service/profile_lookups", rows=6),
                                                        coverage("evidence_03", store="account_service/customer_profiles", rows=0)])
        self.assertEqual(cause(scenario), d.EMPTY_STORE)

    def test_an_account_store_in_an_unrelated_file_does_not_count(self):
        scenario = Scenario([audit_record("evidence_03")], coverages=[coverage("evidence_77", store="customer_profiles_primary", rows=0)])
        self.assertEqual(cause(scenario), d.NO_EVIDENCE)

    def test_an_account_store_captured_after_the_audit_does_not_count(self):
        scenario = Scenario([audit_record()], coverages=[coverage("evidence_03", store="customer_profiles_primary", rows=0, observed=LATER)])
        self.assertEqual(cause(scenario), d.NO_EVIDENCE)

    def test_every_cause_is_listed(self):
        self.assertEqual(len(set(d.CAUSES)), 6)


@NEEDS_DATASET
class ReplicaSystemEvidenceTests(unittest.TestCase):
    """Why a surviving replica copy is reported at the resource's home system. Property of the supplied snapshots,
    independent of any sample answer: where a row records origin_system, a replica row always records the same
    system as the primary row of that resource, and never a replica or replication system."""

    @classmethod
    def setUpClass(cls):
        cls.replica, cls.primary = {}, {}
        for path in sorted(glob.glob(str(DATASET_DIR / "evidence" / "databases" / "*.sql"))):
            con = sqlite3.connect(":memory:")
            con.executescript(Path(path).read_text(encoding="utf-8"))
            for (table,) in con.execute("select name from sqlite_master where type='table'").fetchall():
                columns = [c[1] for c in con.execute(f"pragma table_info({table})")]
                if "origin_system" not in columns or "resource_id" not in columns:
                    continue
                side = cls.replica if ("replica" in table or "replication" in table) else cls.primary
                for rid, system in con.execute(f"select resource_id, origin_system from {table}"):
                    side.setdefault(Path(path).name, {})[(table, rid)] = (rid, system)

    def rows(self, side):
        return [row for per_file in side.values() for row in per_file.values()]

    def test_the_supplied_snapshots_contain_replica_rows_to_compare(self):
        self.assertGreater(len(self.rows(self.replica)), 100)

    def test_no_replica_row_names_a_replica_or_replication_system(self):
        for rid, system in self.rows(self.replica):
            self.assertFalse("replica" in system or "replication" in system or "failover" in system, (rid, system))

    def test_a_replica_row_reports_the_same_system_as_the_primary_row_of_the_same_resource(self):
        primary = {}
        for rid, system in self.rows(self.primary):
            primary.setdefault(rid, set()).add(system)
        compared = 0
        for rid, system in self.rows(self.replica):
            if rid in primary:
                compared += 1
                self.assertEqual(primary[rid], {system}, rid)
        self.assertGreater(compared, 100)


@NEEDS_DATASET
class AccountEvidenceSchemaTests(unittest.TestCase):
    """The report's claim behind gate G1: an account that still exists is always identifier-bearing in the evidence."""

    def test_every_found_profile_fact_carries_an_identifier_and_a_404_lookup_carries_none(self):
        from evidence.store import extract_all
        from loaders import load_dataset
        facts = [f for f in extract_all(load_dataset().evidence_files).facts if f.record_type == "USER_PROFILE"]
        keyed = {"email", "email_hash", "phone", "user_id", "name"}
        found = [f for f in facts if f.state == "FOUND"]
        self.assertGreater(len(found), 300)
        self.assertTrue(all(any(i.kind in keyed for i in f.identifiers) for f in found))
        bare_404 = [f for f in facts if f.state == "NOT_FOUND" and f.provenance.parser == "json"
                    and not any(i.kind in keyed for i in f.identifiers)]
        self.assertGreater(len(bare_404), 10)  # the identifier-free lookups strict ownership cannot attribute


if __name__ == "__main__":
    unittest.main()
