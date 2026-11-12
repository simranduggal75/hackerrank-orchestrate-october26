"""Fact model, identifier classification, state normalisation, and the timestamp rule."""

import unittest

from evidence.facts import (
    Fact, Identifier, Provenance, classify_value, extract_identifiers, is_null, lookup_fact,
    make_identifier, record_fact, state_for, text_identifiers, to_bool, valid_timestamp,
)

PROV = Provenance("evidence_01", "evidence/x.json", "records[0]", "test")
HASH = "a" * 64


def fact(captured="2026-09-01T12:00:00Z", observed="2026-09-01T12:00:00Z"):
    return Fact("evidence_01", captured, observed, "account_service", "account_1", "USER_PROFILE", "FOUND",
                (), {}, None, PROV)


class ClassificationTests(unittest.TestCase):
    def test_kinds_are_decided_by_value_shape(self):
        self.assertEqual(classify_value("x", "user_26"), "user_id")
        self.assertEqual(classify_value("x", "a.b@example.test"), "email")
        self.assertEqual(classify_value("x", "A.B@EXAMPLE.TEST"), "email")
        self.assertEqual(classify_value("x", HASH), "email_hash")
        self.assertEqual(classify_value("x", "+81 90 5550 2345"), "phone")
        self.assertEqual(classify_value("full_name", "Kendra Whitman"), "name")
        self.assertEqual(classify_value("x", "Kendra Whitman"), "other")
        self.assertEqual(classify_value("x", "user_26x"), "other")

    def test_null_spellings(self):
        for value in (None, "", "  ", "null", "NULL", "None"):
            self.assertTrue(is_null(value), value)
        for value in ("nulll", "x", 0, False):
            self.assertFalse(is_null(value), value)

    def test_make_identifier_skips_nulls(self):
        self.assertIsNone(make_identifier("email", "null"))
        self.assertIsNone(make_identifier("email", None))
        self.assertEqual(make_identifier("email", "a@x.com").kind, "email")

    def test_booleans(self):
        self.assertIs(to_bool(True), True)
        self.assertIs(to_bool("false"), False)
        self.assertIs(to_bool(1), True)
        self.assertIs(to_bool(0), False)
        self.assertIsNone(to_bool("maybe"))
        self.assertIsNone(to_bool(None))

    def test_timestamp_validity(self):
        self.assertTrue(valid_timestamp("2026-09-01T12:00:00Z"))
        for bad in ("2026-9-1T12:00:00Z", "2026-09-01", None, 5, "2026-09-01T12:00:00+00:00",
                    "2026-13-45T99:00:00Z", "2026-02-30T00:00:00Z"):
            self.assertFalse(valid_timestamp(bad), bad)


class TextIdentifierTests(unittest.TestCase):
    def test_emails_are_extracted_as_exact_tokens(self):
        text = "Please send the delivery update to my.tr58294731@example.test. The order includes: Bag."
        ids = text_identifiers("message_text", text)
        self.assertEqual([i.value for i in ids], ["my.tr58294731@example.test"])
        self.assertEqual(ids[0].via, "text")

    def test_lookalike_addresses_stay_separate_tokens(self):
        ids = text_identifiers("message_text", "cc sm.9482731.jr@example.test and sm.9482731@example.test")
        self.assertEqual([i.value for i in ids], ["sm.9482731.jr@example.test", "sm.9482731@example.test"])

    def test_no_email_no_identifier(self):
        self.assertEqual(text_identifiers("message_text", "Customer contacted support."), [])
        self.assertEqual(text_identifiers("message_text", None), [])

    def test_uppercase_and_plus_addresses(self):
        ids = text_identifiers("message_text", "write to AK.92573@EXAMPLE.TEST or a+tag@x.io")
        self.assertEqual([i.value for i in ids], ["AK.92573@EXAMPLE.TEST", "a+tag@x.io"])


class RecordNormalisationTests(unittest.TestCase):
    def test_nested_document_identifiers_are_found(self):
        raw = {"resource_id": "search_1", "index_name": "idx",
               "document": {"display_name": "A B", "email": "a@x.com", "customer_reference": None}}
        ids = extract_identifiers(raw)
        self.assertEqual({(i.field, i.kind) for i in ids}, {("display_name", "name"), ("email", "email")})

    def test_analytics_state(self):
        anon = {"resource_id": "analytics_1", "user_id": None, "email": None, "phone": None, "email_hash": None}
        self.assertEqual(state_for("ANALYTICS_PROFILE", anon, extract_identifiers(anon)), "ANONYMIZED")
        ident = dict(anon, email="a@x.com")
        self.assertEqual(state_for("ANALYTICS_PROFILE", ident, extract_identifiers(ident)), "IDENTIFIABLE")
        only_user = dict(anon, user_id="user_3")
        self.assertEqual(state_for("ANALYTICS_PROFILE", only_user, extract_identifiers(only_user)), "IDENTIFIABLE")

    def test_tombstone_state(self):
        clean = {"resource_id": "tombstone_1", "subject_reference": "user_9", "name": None, "email": None,
                 "phone": None, "email_hash": None, "account_status": "DELETED"}
        self.assertEqual(state_for("ACCOUNT_TOMBSTONE", clean, extract_identifiers(clean)), "REDACTED")
        dirty = dict(clean, name="Addo Kofi", email="addo@x.com")
        self.assertEqual(state_for("ACCOUNT_TOMBSTONE", dirty, extract_identifiers(dirty)), "IDENTIFIABLE")

    def test_support_message_states(self):
        redacted = {"resource_id": "support_1", "subject_reference": "user_1", "message_text": "[REDACTED]"}
        exposed = {"resource_id": "support_2", "subject_reference": "user_1", "message_text": "mail a@x.com now"}
        generic = {"resource_id": "support_3", "subject_reference": "user_1", "message_text": "Customer followed up."}
        for raw, expected in ((redacted, "REDACTED"), (exposed, "IDENTIFIABLE"), (generic, "NO_DIRECT_IDENTIFIER")):
            self.assertEqual(state_for("SUPPORT_MESSAGE", raw, extract_identifiers(raw)), expected)

    def test_flow_and_backup_states(self):
        self.assertEqual(state_for("DATA_FLOW", {"enabled": True}, ()), "ACTIVE")
        self.assertEqual(state_for("DATA_FLOW", {"enabled": 0}, ()), "DISABLED")
        self.assertEqual(state_for("DATA_FLOW", {}, ()), "UNKNOWN")
        self.assertEqual(state_for("BACKUP_ARCHIVE", {"restore_allowed": True}, ()), "ACTIVE")
        self.assertEqual(state_for("BACKUP_ARCHIVE", {"restore_allowed": "false"}, ()), "BLOCKED")

    def test_record_fact_keeps_provenance_and_does_not_duplicate_resource_id(self):
        raw = {"resource_id": "account_1", "customer_reference": "user_1", "email": "a@x.com", "account_status": "ACTIVE"}
        f = record_fact(evidence_id="evidence_01", captured_at="2026-09-01T12:00:00Z", observed_at=None,
                        system="account_service", record_type="USER_PROFILE", raw=raw, store="t", provenance=PROV)
        self.assertEqual(f.resource_id, "account_1")
        self.assertEqual(f.observed_at, f.captured_at)  # no record time -> capture time
        self.assertEqual(f.attributes, {"account_status": "ACTIVE"})
        self.assertIs(f.provenance, PROV)

    def test_lookup_fact_unknown_state_and_note_subject(self):
        common = dict(evidence_id="evidence_01", captured_at="2026-09-01T12:00:00Z", observed_at=None,
                      system="s", resource_id="r_1", record_type="USER_PROFILE", provenance=PROV)
        odd = lookup_fact(result="WEIRD", subject="a@x.com", **common)
        self.assertEqual((odd.state, odd.attributes["raw_state"]), ("UNKNOWN", "WEIRD"))
        note = lookup_fact(result="ANONYMIZED", subject="no direct identifier", **common)
        self.assertEqual(note.identifiers, ())
        self.assertEqual(note.attributes["subject_note"], "no direct identifier")
        upper = lookup_fact(result="FOUND", subject="CJ.QR4729135@EXAMPLE.TEST", **common)
        self.assertEqual(upper.identifiers[0], Identifier("subject_reference", "CJ.QR4729135@EXAMPLE.TEST", "email"))


class TimestampRuleTests(unittest.TestCase):
    AUDIT = "2026-09-10T00:00:00Z"

    def test_file_and_record_must_both_be_at_or_before_audit(self):
        self.assertTrue(fact("2026-09-09T00:00:00Z", "2026-09-09T00:00:00Z").available_by(self.AUDIT))
        self.assertTrue(fact(self.AUDIT, self.AUDIT).available_by(self.AUDIT))  # equality is allowed
        self.assertFalse(fact("2026-09-11T00:00:00Z", "2026-09-09T00:00:00Z").available_by(self.AUDIT))  # late file
        self.assertFalse(fact("2026-09-09T00:00:00Z", "2026-09-11T00:00:00Z").available_by(self.AUDIT))  # late record
        self.assertFalse(fact("2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z").available_by(self.AUDIT))


if __name__ == "__main__":
    unittest.main()
