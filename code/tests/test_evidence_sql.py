"""SQL parser tests: every table family, replica drift, timestamps, provenance, damaged dumps."""

import unittest

from evidence import sql_parser
from loaders import DATASET_DIR, load_dataset
from tests.helpers import CAPTURED, GENERIC_DDL, NEEDS_DATASET, TmpCase, sql_insert

PROFILE = {"resource_id": "account_1", "customer_reference": "user_1", "full_name": "Ann Bee",
           "email": "ann@x.com", "phone": "+1 202 555 0101", "account_status": "ACTIVE", "signup_channel": "organic"}
SEARCH = {"resource_id": "search_9", "index_name": "customer_directory_v3",
          "document": {"display_name": "Ann Bee", "email": "ann@x.com", "customer_reference": "user_1"}}


def generic_row(table, rid, system, collection, record, updated=CAPTURED):
    return sql_insert(table, 5, rid, system, collection, record, updated)


def replica_sql(extra=""):
    return "\n".join([
        f"-- Captured at: {CAPTURED}", "-- Database topology: read_replica", "BEGIN TRANSACTION;",
        GENERIC_DDL.format(t="customer_data_primary"),
        generic_row("customer_data_primary", "account_1", "account_service", "customer_profiles", PROFILE),
        GENERIC_DDL.format(t="customer_data_read_replica"),
        generic_row("customer_data_read_replica", "account_1", "account_service", "customer_profiles", PROFILE),
        generic_row("customer_data_read_replica", "search_9", "search_service", "customer_search_documents", SEARCH),
        extra, "COMMIT;"])


class GenericReplicaTests(TmpCase):
    def parse(self, sql, captured=CAPTURED):
        return sql_parser.parse(self.write("r.sql", sql), self.ev("r.sql", captured))

    def test_replica_drift_is_visible_through_store_names(self):
        result = self.parse(replica_sql())
        self.assertEqual(self.codes(result), [])
        by_store = {}
        for f in result.facts:
            by_store.setdefault(f.store, set()).add(f.resource_id)
        self.assertEqual(by_store["customer_data_primary"], {"account_1"})
        self.assertEqual(by_store["customer_data_read_replica"], {"account_1", "search_9"})  # replica-only row
        roles = {f.store: f.attributes["replica_role"] for f in result.facts}
        self.assertEqual(roles, {"customer_data_primary": "primary", "customer_data_read_replica": "read_replica"})

    def test_record_types_systems_and_identifiers(self):
        result = self.parse(replica_sql())
        search = next(f for f in result.facts if f.resource_id == "search_9")
        self.assertEqual((search.system, search.record_type, search.state), ("search_service", "SEARCH_INDEX_ENTRY", "FOUND"))
        self.assertIn(("email", "ann@x.com", "email"), [(i.field, i.value, i.kind) for i in search.identifiers])
        self.assertEqual(search.attributes["topology"], "read_replica")

    def test_provenance_points_at_table_and_row(self):
        result = self.parse(replica_sql())
        for f in result.facts:
            self.assertEqual((f.provenance.evidence_id, f.provenance.path, f.provenance.parser),
                             ("evidence_01", "evidence/r.sql", "sql"))
            self.assertRegex(f.provenance.locator, r"^table customer_data_\w+ rowid \d+$")
        locators = {f.provenance.locator for f in result.facts}
        self.assertEqual(len(locators), len(result.facts))  # unique per row

    def test_coverage_includes_table_level_and_per_collection_rows(self):
        result = self.parse(replica_sql())
        table_level = {c.store: c.row_count for c in result.coverage if c.system == "*"}
        self.assertEqual(table_level, {"customer_data_primary": 1, "customer_data_read_replica": 2})
        self.assertIn(("search_service", "SEARCH_INDEX_ENTRY", "customer_data_read_replica"),
                      {(c.system, c.record_type, c.store) for c in result.coverage})

    def test_record_stamped_after_snapshot_is_flagged_and_pushed_later(self):
        late = generic_row("customer_data_primary", "account_2", "account_service", "customer_profiles",
                           dict(PROFILE, resource_id="account_2"), updated="2026-09-05T00:00:00Z")
        result = self.parse(replica_sql(late))
        self.assertIn("RECORD_AFTER_CAPTURE", self.codes(result))
        fact = next(f for f in result.facts if f.resource_id == "account_2")
        self.assertEqual(fact.observed_at, "2026-09-05T00:00:00Z")
        self.assertFalse(fact.available_by("2026-09-02T00:00:00Z"))

    def test_header_timestamp_disagreeing_with_manifest_is_reported(self):
        result = self.parse(replica_sql(), captured="2026-09-02T00:00:00Z")
        self.assertIn("TIMESTAMP_MISMATCH", self.codes(result))

    def test_unknown_collection_and_table_are_reported_not_dropped(self):
        extra = "\n".join([
            generic_row("customer_data_primary", "zzz_1", "mystery_service", "mystery", {"resource_id": "zzz_1"}),
            "CREATE TABLE audit_notes (id INTEGER, note TEXT);", sql_insert("audit_notes", 2, 1, "hi")])
        result = self.parse(replica_sql(extra))
        self.assertIn("UNKNOWN_COLLECTION", self.codes(result))
        self.assertIn("UNKNOWN_TABLE", self.codes(result))
        self.assertEqual(result.stats["rows_unmapped"], 2)
        self.assertEqual(len(result.facts), 3)

    def test_resource_id_mismatch_between_row_and_json_is_warned(self):
        bad = generic_row("customer_data_primary", "account_7", "account_service", "customer_profiles", PROFILE)
        self.assertIn("RESOURCE_ID_MISMATCH", self.codes(self.parse(replica_sql(bad))))


class OtherFamiliesTests(TmpCase):
    def test_typed_inventory_uses_snapshot_scope_time_and_reports_empty_tables(self):
        sql = "\n".join([
            "BEGIN TRANSACTION;",
            "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);",
            sql_insert("snapshot_scope", 2, "account_service", "2026-08-31T08:00:00Z"),
            "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, customer_reference TEXT, full_name TEXT, "
            "email TEXT, phone TEXT, email_hash TEXT, account_status TEXT NOT NULL, updated_at TEXT NOT NULL);",
            sql_insert("customer_profiles", 8, "account_1", "user_1", "Ann Bee", "ann@x.com", "+1 202 555 0101", None,
                       "ACTIVE", "2026-08-30T00:00:00Z"),
            "CREATE TABLE connector_subscriptions (resource_id TEXT PRIMARY KEY, customer_reference TEXT, "
            "customer_email TEXT, connector_name TEXT NOT NULL, enabled INTEGER NOT NULL, "
            "last_delivery_status TEXT NOT NULL, updated_at TEXT NOT NULL);",
            "COMMIT;"])
        result = sql_parser.parse(self.write("i.sql", sql), self.ev("i.sql"))
        self.assertEqual(self.codes(result), [])
        fact = result.facts[0]
        self.assertEqual((fact.record_type, fact.observed_at, fact.captured_at),
                         ("USER_PROFILE", "2026-08-31T08:00:00Z", CAPTURED))
        self.assertEqual(fact.attributes["updated_at"], "2026-08-30T00:00:00Z")
        empty = [c for c in result.coverage if c.store == "connector_subscriptions"]
        self.assertEqual([(c.system, c.row_count) for c in empty], [("integration_service", 0)])

    def test_typed_data_flow_and_backup_states(self):
        sql = "\n".join([
            "CREATE TABLE connector_subscriptions (resource_id TEXT PRIMARY KEY, customer_reference TEXT, "
            "customer_email TEXT, connector_name TEXT NOT NULL, enabled INTEGER NOT NULL, "
            "last_delivery_status TEXT NOT NULL, updated_at TEXT NOT NULL);",
            sql_insert("connector_subscriptions", 7, "data_flow_1", "user_1", "ann@x.com", "sync", 1, "ok", CAPTURED),
            sql_insert("connector_subscriptions", 7, "data_flow_2", None, "bob@x.com", "sync", 0, "stopped", CAPTURED),
            "CREATE TABLE backup_restore_entries (resource_id TEXT PRIMARY KEY, customer_reference TEXT, "
            "customer_email TEXT, archive_key TEXT NOT NULL, restore_allowed INTEGER NOT NULL, updated_at TEXT NOT NULL);",
            sql_insert("backup_restore_entries", 6, "backup_1", "user_1", None, "k1", 0, CAPTURED)])
        result = sql_parser.parse(self.write("t.sql", sql), self.ev("t.sql"))
        states = {f.resource_id: f.state for f in result.facts}
        self.assertEqual(states, {"data_flow_1": "ACTIVE", "data_flow_2": "DISABLED", "backup_1": "BLOCKED"})
        self.assertIn("NO_SNAPSHOT_SCOPE", self.codes(result))

    def test_account_failover_tables(self):
        cols = ("resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, full_name TEXT NOT NULL, email TEXT NOT NULL, "
                "phone TEXT NOT NULL, account_status TEXT NOT NULL, updated_at TEXT NOT NULL")
        row = ("account_005", "user_05", "Leila H", "leila@x.com", "+212 6 48", "ACTIVE", CAPTURED)
        sql = "\n".join([
            f"CREATE TABLE customer_profiles_failover_replica ({cols});", sql_insert("customer_profiles_failover_replica", 7, *row),
            f"CREATE TABLE customer_profiles_primary ({cols});"])
        result = sql_parser.parse(self.write("a.sql", sql), self.ev("a.sql"))
        self.assertEqual([(f.store, f.resource_id, f.state) for f in result.facts],
                         [("customer_profiles_failover_replica", "account_005", "FOUND")])
        self.assertEqual(result.facts[0].attributes["replica_role"], "failover_replica")
        coverage = {c.store: c.row_count for c in result.coverage}
        self.assertEqual(coverage, {"customer_profiles_failover_replica": 1, "customer_profiles_primary": 0})

    def test_search_primary_keeps_source_resource(self):
        sql = "\n".join([
            "CREATE TABLE search_documents_primary (resource_id TEXT PRIMARY KEY, display_name TEXT, email TEXT, "
            "source_resource_id TEXT, indexed_at TEXT NOT NULL);",
            sql_insert("search_documents_primary", 5, "search_001", "Jules Bard", "jonas@x.com", "account_006", "2026-08-30T00:00:00Z")])
        result = sql_parser.parse(self.write("s.sql", sql), self.ev("s.sql"))
        fact = result.facts[0]
        self.assertEqual((fact.system, fact.record_type, fact.attributes["source_resource_id"]),
                         ("search_service", "SEARCH_INDEX_ENTRY", "account_006"))
        self.assertEqual(fact.attributes["indexed_at"], "2026-08-30T00:00:00Z")


class DamagedDumpTests(TmpCase):
    def parse(self, sql):
        return sql_parser.parse(self.write("d.sql", sql), self.ev("d.sql"))

    def test_bad_statement_in_the_middle_is_reported_and_the_rest_is_kept(self):
        broken = replica_sql("INSERT INTO customer_data_primary VALUES ('only_one_column');")
        result = self.parse(broken)
        levels = {i.code: i.level for i in result.issues}
        self.assertEqual(levels, {"SQL_EXEC_ERROR": "WARN", "SQL_STATEMENT_FAILED": "ERROR"})
        self.assertEqual(result.stats["statements_failed"], 1)
        self.assertEqual({f.resource_id for f in result.facts}, {"account_1", "search_9"})

    def test_truncated_file_keeps_complete_statements(self):
        sql = replica_sql()
        cut = sql[: sql.index("search_9") + 20]  # ends inside the last INSERT
        result = self.parse(cut)
        self.assertEqual({i.code: i.level for i in result.issues}.get("SQL_TRUNCATED"), "ERROR")
        self.assertTrue(any(f.resource_id == "account_1" for f in result.facts))
        self.assertFalse(any(f.resource_id == "search_9" for f in result.facts))

    def test_invalid_record_json_is_skipped_with_an_issue(self):
        sql = "\n".join([
            "CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, "
            "collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);",
            sql_insert("customer_data_primary", 5, "account_1", "account_service", "customer_profiles", "{not json", CAPTURED),
            sql_insert("customer_data_primary", 5, "account_2", "account_service", "customer_profiles", "[1,2]", CAPTURED),
            generic_row("customer_data_primary", "account_3", "account_service", "customer_profiles", dict(PROFILE, resource_id="account_3"))])
        result = self.parse(sql)
        self.assertEqual(self.codes(result).count("BAD_RECORD_JSON"), 2)
        self.assertEqual([f.resource_id for f in result.facts], ["account_3"])
        self.assertEqual(result.stats["rows_skipped"], 2)

    def test_file_with_no_tables_is_an_error(self):
        self.assertIn("NO_TABLES", self.codes(self.parse("-- nothing here\n")))

    def test_non_utf8_bytes_are_reported(self):
        path = self.write("d.sql", replica_sql().encode("utf-8") + b"\n-- caf\xe9\n")
        result = sql_parser.parse(path, self.ev("d.sql"))
        self.assertIn("DECODE_ERROR", self.codes(result))
        self.assertTrue(result.facts)  # still usable

    def test_missing_file_is_an_error_not_a_crash(self):
        result = sql_parser.parse(self.dir / "absent.sql", self.ev("absent.sql"))
        self.assertEqual(self.codes(result), ["UNREADABLE_FILE"])
        self.assertEqual(result.facts, [])


@NEEDS_DATASET
class RealSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_dataset()
        cls.ev = {e.file_id: e for e in ds.evidence_files}

    def parse(self, file_id):
        e = self.ev[file_id]
        return sql_parser.parse(DATASET_DIR / e.file_path, e)

    def test_failover_replica_still_holds_account_005(self):
        result = self.parse("evidence_54")
        stores = {(f.store, f.resource_id) for f in result.facts}
        self.assertIn(("customer_profiles_failover_replica", "account_005", ), stores)
        self.assertNotIn(("customer_profiles_primary", "account_005"), stores)  # drift: gone from the primary

    def test_every_sql_file_parses_without_errors(self):
        for file_id, e in self.ev.items():
            if e.file_path.endswith(".sql"):
                result = sql_parser.parse(DATASET_DIR / e.file_path, e)
                self.assertEqual([i for i in result.issues if i.level == "ERROR"], [], file_id)


if __name__ == "__main__":
    unittest.main()
