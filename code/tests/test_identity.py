"""Identity tests. Run from the repo root:  python -m unittest discover -s code/tests -t code"""

import unittest

from identity import (
    IdentityIndex, email_hash, normalize_email, normalize_hash, normalize_phone,
)
from loaders import DATASET_DIR, AuditEvent, Identity, Request, load_dataset


def ident(uid, name, email, phone, updated="2026-09-01T00:00:00Z"):
    return Identity(uid, name, email, phone, email_hash(email), updated)


def audit(aid, uid, time, field, old, new):
    return AuditEvent(aid, uid, time, field, old, new)


def email_change(n, uid, time, old, new):
    return [audit(f"audit_{n}a", uid, time, "email", old, new),
            audit(f"audit_{n}b", uid, time, "email_hash", email_hash(old), email_hash(new))]


def request(email, audit_at="2026-09-30T00:00:00Z", case="c1"):
    return Request(case, email, "2026-09-01T00:00:00Z", "COMPLETE", "2026-09-02T00:00:00Z", audit_at, "delete me")


class NormalisationTests(unittest.TestCase):
    def test_email(self):
        self.assertEqual(normalize_email("  Cj.Qr4729135@Example.TEST "), "cj.qr4729135@example.test")
        self.assertEqual(normalize_email("ＡＢＣ@Example.com"), "abc@example.com")  # full-width forms

    def test_email_keeps_plus_and_dots_distinct(self):
        self.assertNotEqual(normalize_email("a.b@x.com"), normalize_email("ab@x.com"))
        self.assertNotEqual(normalize_email("a+tag@x.com"), normalize_email("a@x.com"))

    def test_phone(self):
        self.assertEqual(normalize_phone("+1 (202) 555-0166"), "+12025550166")
        self.assertEqual(normalize_phone("+971 50 555 0147"), "+971505550147")
        self.assertNotEqual(normalize_phone("+44 7700 900880"), normalize_phone("44 7700 900880"))

    def test_hash_matches_known_dataset_value_format(self):
        digest = email_hash("Am.84527391@Example.test")
        self.assertEqual(digest, email_hash("am.84527391@example.test"))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(normalize_hash(" ABC "), "abc")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        users = [
            ident("user_1", "Nadia Ionescu", "nia.current@example.test", "+40 721 000 111"),
            ident("user_2", "Sam Lee", "sam.lee@example.test", "+44 7700 900002"),
            ident("user_3", "Sam Lee Jr", "sam.lee.jr@example.test", "+44 7700 900003"),
        ]
        # user_1 changed email twice; user_2 changed phone only.
        events = (
            email_change(1, "user_1", "2026-02-01T00:00:00Z", "nia.old@example.test", "nia.mid@example.test")
            + email_change(2, "user_1", "2026-06-01T00:00:00Z", "nia.mid@example.test", "nia.current@example.test")
            + [audit("audit_3", "user_2", "2026-03-01T00:00:00Z", "phone", "+44 7700 900001", "+44 7700 900002")]
        )
        self.index = IdentityIndex(users, events)

    def test_consistent_fixture_has_no_issues(self):
        self.assertEqual(self.index.issues, [])

    def test_current_email_is_case_insensitive(self):
        r = self.index.resolve_email("NIA.CURRENT@EXAMPLE.TEST", "2026-09-30T00:00:00Z")
        self.assertEqual((r.status, r.user_id), ("CURRENT", "user_1"))
        self.assertIn("normalised", r.notes[0])

    def test_historical_email_resolves_to_same_user(self):
        for old in ("nia.old@example.test", "nia.mid@example.test"):
            r = self.index.resolve_email(old, "2026-09-30T00:00:00Z")
            self.assertEqual((r.status, r.user_id), ("HISTORICAL", "user_1"), old)
        self.assertIn("retired", r.notes[0])

    def test_status_depends_on_audit_time(self):
        # In March the middle address was still live.
        r = self.index.resolve_email("nia.mid@example.test", "2026-03-15T00:00:00Z")
        self.assertEqual(r.status, "CURRENT")
        self.assertEqual(r.matched.introduced_by, "audit_1a")

    def test_email_unused_until_after_audit_is_future_only(self):
        r = self.index.resolve_email("nia.current@example.test", "2026-04-01T00:00:00Z")
        self.assertEqual((r.status, r.user_id), ("FUTURE_ONLY", None))

    def test_later_changes_are_ignored_for_identifiers(self):
        ids = self.index.identifiers("user_1", "2026-04-01T00:00:00Z")
        self.assertEqual({v.norm for v in ids.emails}, {"nia.old@example.test", "nia.mid@example.test"})
        self.assertEqual(ids.current["email"], "nia.mid@example.test")
        self.assertEqual(ids.current["email_hash"], email_hash("nia.mid@example.test"))

    def test_full_identifier_history_after_all_changes(self):
        ids = self.index.identifiers("user_1", "2026-09-30T00:00:00Z")
        self.assertEqual(len(ids.emails), 3)
        self.assertEqual(len(ids.email_hashes), 3)
        self.assertEqual(ids.current["email"], "nia.current@example.test")

    def test_phone_history(self):
        ids = self.index.identifiers("user_2", "2026-09-30T00:00:00Z")
        self.assertEqual({v.norm for v in ids.phones}, {"+447700900001", "+447700900002"})
        self.assertEqual(ids.match("+44 7700 900001"), "phone")
        before = self.index.identifiers("user_2", "2026-01-01T00:00:00Z")
        self.assertEqual({v.norm for v in before.phones}, {"+447700900001"})
        self.assertIsNone(before.match("+44 7700 900002"))

    def test_unknown_email(self):
        r = self.index.resolve_email("nobody@example.com", "2026-09-30T00:00:00Z")
        self.assertEqual((r.status, r.user_id, r.identifiers), ("UNRESOLVED", None, None))

    def test_lookalike_email_does_not_resolve_to_the_wrong_user(self):
        self.assertEqual(self.index.resolve_email("sam.lee@example.test", "2026-09-30T00:00:00Z").user_id, "user_2")
        self.assertEqual(self.index.resolve_email("sam.lee.jr@example.test", "2026-09-30T00:00:00Z").user_id, "user_3")

    def test_match_is_exact_not_substring(self):
        ids = self.index.identifiers("user_2", "2026-09-30T00:00:00Z")
        self.assertEqual(ids.match("SAM.LEE@example.test"), "email")
        self.assertIsNone(ids.match("sam.lee.jr@example.test"))
        self.assertIsNone(ids.match("xsam.lee@example.test"))
        self.assertIsNone(ids.match("Sam Lee"))  # names never match
        self.assertEqual(ids.match("user_2"), "user_id")
        self.assertIsNone(ids.match("user_20"))
        self.assertEqual(ids.match(email_hash("sam.lee@example.test").upper()), "email_hash")
        self.assertIsNone(ids.match(email_hash("sam.lee.jr@example.test")))

    def test_find_user_by_each_field(self):
        self.assertEqual(self.index.find_user("email", "NIA.OLD@example.test"), ["user_1"])
        self.assertEqual(self.index.find_user("email_hash", email_hash("sam.lee@example.test")), ["user_2"])
        self.assertEqual(self.index.find_user("phone", "+44 7700 900001"), ["user_2"])
        self.assertEqual(self.index.find_user("email", "nia.current@example.test", as_of="2026-04-01T00:00:00Z"), [])

    def test_resolve_request_uses_audit_at(self):
        r = self.index.resolve_request(request("nia.mid@example.test", audit_at="2026-03-15T00:00:00Z"))
        self.assertEqual((r.case_id, r.status), ("c1", "CURRENT"))
        self.assertEqual(self.index.resolve_request(request("nia.mid@example.test")).status, "HISTORICAL")

    def test_later_change_note(self):
        r = self.index.resolve_email("nia.mid@example.test", "2026-03-15T00:00:00Z")
        self.assertTrue(any("ignored" in n for n in r.notes))


class ConsistencyAndAmbiguityTests(unittest.TestCase):
    def test_shared_historical_email_is_ambiguous(self):
        users = [ident("user_1", "A", "a@x.com", "+1 1"), ident("user_2", "B", "b@x.com", "+1 2")]
        events = (email_change(1, "user_1", "2026-01-01T00:00:00Z", "shared@x.com", "a@x.com")
                  + email_change(2, "user_2", "2026-01-01T00:00:00Z", "shared@x.com", "b@x.com"))
        index = IdentityIndex(users, events)
        self.assertEqual(index.resolve_email("shared@x.com", "2026-09-30T00:00:00Z").status, "AMBIGUOUS")
        self.assertTrue(any("more than one user" in i for i in index.issues))

    def test_inconsistent_audit_chain_and_snapshot_are_reported(self):
        users = [ident("user_1", "A", "a@x.com", "+1 1")]
        events = [audit("audit_1", "user_1", "2026-01-01T00:00:00Z", "email", "old@x.com", "mid@x.com"),
                  audit("audit_2", "user_1", "2026-02-01T00:00:00Z", "email", "other@x.com", "z@x.com")]
        issues = IdentityIndex(users, events).issues
        self.assertTrue(any("does not follow" in i for i in issues))
        self.assertTrue(any("differs from the identity snapshot" in i for i in issues))

    def test_bad_snapshot_hash_is_reported(self):
        bad = Identity("user_1", "A", "a@x.com", "+1 1", "0" * 64, "2026-09-01T00:00:00Z")
        self.assertTrue(any("not sha256" in i for i in IdentityIndex([bad], []).issues))

    def test_events_are_ordered_by_time_not_file_order(self):
        users = [ident("user_1", "A", "c@x.com", "+1 1")]
        events = email_change(2, "user_1", "2026-05-01T00:00:00Z", "b@x.com", "c@x.com") + \
                 email_change(1, "user_1", "2026-01-01T00:00:00Z", "a@x.com", "b@x.com")
        index = IdentityIndex(users, events)
        self.assertEqual(index.issues, [])
        self.assertEqual([v.norm for v in index.timeline("user_1", "email")], ["a@x.com", "b@x.com", "c@x.com"])


@unittest.skipUnless((DATASET_DIR / "deletion_requests.csv").exists(), "dataset/ not present")
class RealDatasetIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.index = IdentityIndex(cls.ds.identities, cls.ds.audit_log)

    def test_index_is_internally_consistent(self):
        self.assertEqual(self.index.issues, [])

    def test_every_request_resolves_to_exactly_one_user(self):
        for r in self.ds.requests + self.ds.sample_requests:
            res = self.index.resolve_request(r)
            self.assertIn(res.status, ("CURRENT", "HISTORICAL"), (r.case_id, res.status, res.notes))
            self.assertIsNotNone(res.user_id)

    def test_known_sample_cases(self):
        by_id = {r.case_id: r for r in self.ds.sample_requests}
        self.assertEqual(self.index.resolve_request(by_id["case_05"]).user_id, "user_05")
        self.assertEqual(self.index.resolve_request(by_id["case_10"]).status, "HISTORICAL")
        self.assertEqual(self.index.resolve_request(by_id["case_10"]).user_id, "user_10")

    def test_mixed_case_request_email_resolves(self):
        upper = [r for r in self.ds.requests if r.email != r.email.lower()]
        self.assertTrue(upper)
        for r in upper:
            self.assertEqual(self.index.resolve_request(r).request_email, r.email)
            self.assertIn(self.index.resolve_request(r).status, ("CURRENT", "HISTORICAL"))

    def test_repeated_email_is_resolved_per_case(self):
        by_id = {r.case_id: r for r in self.ds.requests}
        a, b = (self.index.resolve_request(by_id[c]) for c in ("case_33", "case_52"))
        self.assertEqual(a.user_id, b.user_id)
        self.assertEqual((a.identifiers.as_of, b.identifiers.as_of), (by_id["case_33"].audit_at, by_id["case_52"].audit_at))

    def test_hash_of_every_current_email(self):
        for ident_ in self.ds.identities:
            self.assertEqual(email_hash(ident_.email), ident_.email_hash)


if __name__ == "__main__":
    unittest.main()
