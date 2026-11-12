"""Screenshot parser tests: voting, layout, uncertainty, failures, provenance, and the real images."""

import unittest

from evidence import image_parser as ip
from evidence import ocr, store as store_mod
from evidence.ocr import OcrFailed, OcrPass, OcrUnavailable, OcrWord
from identity import IdentityIndex, email_hash, normalize_phone
from loaders import DATASET_DIR, EvidenceFile, Identity, load_dataset
from tests.helpers import CAPTURED, HAS_DATASET, TmpCase, digest
from tests.image_fixtures import (
    BACKUP_HEADER, HEIGHT, WIDTH, account_screen, audit_screen, backup_screen, passes_from, screen, search_screen, words,
)


def cell(text, x=0.0, y=0.0):
    return ip.Cell(text, x, y, x + 10, y + 10)


class VotingTests(unittest.TestCase):
    def test_two_of_three_agreeing_passes_confirm(self):
        r = ip.vote([cell("user_11"), cell("user_11"), cell("user_ll")], ip.USER)
        self.assertEqual((r.value, r.status), ("user_11", "confirmed"))
        self.assertIn("user_ll", r.candidates)

    def test_bad_shape_is_never_a_reading(self):
        r = ip.vote([cell("user_ll"), cell("user_ll"), cell("user_ll")], ip.USER)
        self.assertEqual((r.value, r.status, r.candidates), (None, "unread", ("user_ll",)))

    def test_single_valid_reading_is_not_confirmed(self):
        r = ip.vote([cell("user_11"), cell("user_ll"), None], ip.USER)
        self.assertEqual((r.value, r.status), ("user_11", "single"))

    def test_different_valid_readings_conflict_and_yield_no_value(self):
        r = ip.vote([cell("user_11"), cell("user_17"), cell("user_ll")], ip.USER)
        self.assertEqual((r.value, r.status), (None, "conflict"))
        self.assertEqual(set(r.candidates), {"user_11", "user_17", "user_ll"})

    def test_no_text_at_all_is_empty_not_unread(self):
        self.assertEqual(ip.vote([None, cell("  "), None], ip.USER).status, "empty")

    def test_free_text_without_pattern_confirms_by_agreement(self):
        self.assertEqual(ip.vote([cell("Rowan Parsons")] * 2 + [cell("Gabrie1 Silva")]).value, "Rowan Parsons")

    def test_transform_extracts_a_token_from_a_longer_cell(self):
        r = ip.vote([cell("user_11 · account_5762")] * 3, r"account_\d+", ip.grep(r"account_\d+"))
        self.assertEqual((r.value, r.status), ("account_5762", "confirmed"))

    def test_bbox_comes_from_an_agreeing_pass(self):
        r = ip.vote([cell("a@x.com", 5, 6), cell("a@x.com", 5, 6)], ip.EMAIL)
        self.assertEqual(r.bbox, (5, 6, 15, 16))


class PatternTests(unittest.TestCase):
    def test_email_shapes(self):
        for good in ("a.b@x.com", "A.B@X.COM", "lu-9274813@example.test", "a+tag@x.io"):
            self.assertRegex(good, f"^{ip.EMAIL}$")
        for bad in ("r.pI27@example.test", "Aa@x.com", "a@x", "no at sign"):
            self.assertNotRegex(bad, f"^{ip.EMAIL}$")

    def test_grep_never_returns_the_clean_tail_of_a_garbled_token(self):
        pick = ip.grep(ip.EMAIL)
        self.assertIsNone(pick("r.pI27@example.test"))
        self.assertIsNone(pick("X.y@z.com"))
        self.assertEqual(pick("contact a@x.com."), "a@x.com")
        self.assertEqual(pick("email:r.pl27@example.test"), "r.pl27@example.test")
        self.assertEqual(ip.grep(ip.USER)("Subject filter: user_03 · 18-26 Aug"), "user_03")
        self.assertIsNone(ip.grep(ip.USER)("superuser_03"))
        self.assertIsNone(ip.grep(ip.USER)("user_ll"))

    def test_datetime_parsing(self):
        self.assertEqual(ip.iso_from_text("07 Aug 2026 08:00 UTC"), "2026-08-07T08:00:00Z")
        self.assertEqual(ip.iso_from_text("01 sep 2026 06:00:14 UTC"), "2026-09-01T06:00:14Z")
        self.assertEqual(ip.iso_from_text("PRODUCTION · eu-west · 07 Aug 2026 08:00 UTC"), "2026-08-07T08:00:00Z")
        for bad in ("07 Foo 2026 08:00 UTC", "31 Feb 2026 08:00 UTC", "01 Sep 2026", "25 Aug 04:03"):
            self.assertIsNone(ip.iso_from_text(bad), bad)


class LayoutTests(unittest.TestCase):
    def layout(self, base=None):
        base = base or backup_screen()
        app = next(a for a in ip.APPS if a.key == "backups")
        return ip.read_layout(passes_from(base)[0], app.headers)

    def test_segmentation_joins_a_cell_and_splits_columns(self):
        cells = ip.segment(words("Account lifecycle", 100, 50) + words("Valid", 400, 50))
        self.assertEqual([c.text for c in cells], ["Account lifecycle", "Valid"])

    def test_touching_words_join_without_a_space(self):
        a, b = OcrWord("user", 100, 10, 130, 22), OcrWord("_03", 130.5, 10, 160, 22)
        self.assertEqual(ip.segment([a, b])[0].text, "user_03")

    def test_regions_are_found(self):
        lay = self.layout()
        self.assertEqual((lay.title.text, lay.badge.text), ("Customer-platform archives", "Healthy"))
        self.assertEqual(lay.subtitle.text, "Northstar Production · weekly snapshots")
        self.assertEqual((lay.panel_title.text, lay.declared_rows), ("backup_9287", 3))
        self.assertIn("vaultrecovery", ip._norm(lay.app_text))

    def test_table_rows_exclude_the_row_number_badge(self):
        lay = self.layout()
        self.assertEqual(len(lay.rows), 3)
        self.assertEqual(lay.rows[0]["Archive ID"].text, "backup_9291")
        self.assertEqual(lay.rows[2]["Account"].text, "bm4273@example.test")
        self.assertEqual(lay.rows[1]["Region"].text, "us-west-2")

    def test_panel_pairs_and_soft_wrapped_path(self):
        lay = self.layout()
        self.assertEqual(lay.panel_value("Account").text, "bm4273@example.test")
        self.assertEqual(lay.panel_value("Path").text, "s3://northstar-backups/customer-platform/weekly/segment-42-20260426.tar.zst")
        self.assertEqual(lay.panel_value("Retention end").text, "26 Jul 2026")
        self.assertIsNone(lay.panel_value("Activity"))  # the timeline is not a key/value pair

    def test_kpi_labels_match_fuzzily(self):
        base = account_screen()
        lay = ip.read_layout(passes_from(base)[0])
        self.assertEqual(lay.kpi("email").text, "mk.48297@example.test")
        self.assertEqual(lay.kpi("Phone").text, "+81 90 5550 1234")


class ParseHappyPathTests(TmpCase):
    def run_parse(self, base, *overrides, captured=CAPTURED, name="shot.png"):
        path = self.write(name, b"not-a-real-png")
        passes = passes_from(base, *overrides)
        return ip.parse(path, self.ev(name, captured), ocr=lambda p: passes)

    def test_backup_screen_facts_and_provenance(self):
        result = self.run_parse(backup_screen(), {}, {}, {})
        self.assertEqual([i for i in result.issues if i.level != "INFO"], [])
        self.assertEqual([f.resource_id for f in result.facts], ["backup_9291", "backup_9288", "backup_9287"])
        first = result.facts[0]
        self.assertEqual((first.system, first.record_type, first.state, first.evidence_id, first.captured_at),
                         ("backup_service", "BACKUP_ARCHIVE", "FOUND", "evidence_01", CAPTURED))
        self.assertEqual(first.observed_at, CAPTURED)
        ident = first.identifiers[0]
        self.assertEqual((ident.field, ident.value, ident.kind, ident.via), ("customer_email", "vendor847@example.test", "email", "ocr"))
        self.assertEqual((first.provenance.path, first.provenance.parser), ("evidence/shot.png", "image"))
        self.assertRegex(first.provenance.locator, r"^archives table row 1 @ \(\d+,\d+\)-\(\d+,\d+\)$")
        self.assertEqual(first.attributes["ocr"], {"resource_id": "confirmed", "customer_email": "confirmed"})
        self.assertEqual(first.attributes["restore_access"], "not shown")  # not invented
        self.assertIn("does not show whether restore", first.attributes["state_basis"])
        self.assertEqual(first.attributes["source"], "screenshot_ocr")
        self.assertEqual(result.stats["facts"], 3)

    def test_every_screenshot_fact_is_provisional_and_says_why(self):
        result = self.run_parse(backup_screen(), {}, {}, {})
        for fact in result.facts:
            self.assertIs(fact.attributes["provisional"], True)
            self.assertTrue(fact.attributes["state_basis"])
        # a listed backup shows existence only; it must not claim restore is allowed or blocked
        self.assertEqual({f.state for f in result.facts}, {"FOUND"})

    def test_selected_row_gains_panel_details(self):
        result = self.run_parse(backup_screen(), {}, {}, {})
        by_id = {f.resource_id: f for f in result.facts}
        self.assertEqual(by_id["backup_9287"].attributes["retention_end"], "26 Jul 2026")
        self.assertTrue(by_id["backup_9287"].attributes["archive_path"].endswith("segment-42-20260426.tar.zst"))
        self.assertNotIn("archive_path", by_id["backup_9291"].attributes)

    def test_a_pass_specific_ocr_error_is_outvoted(self):
        result = self.run_parse(backup_screen(), {"bm4273@example.test": "bm4273@exampIe.test"}, {}, {})
        fact = next(f for f in result.facts if f.resource_id == "backup_9287")
        self.assertEqual(fact.identifiers[0].value, "bm4273@example.test")
        self.assertEqual(fact.attributes["ocr"]["customer_email"], "confirmed")

    def test_exact_values_only_so_a_systematic_confusable_misread_is_rejected_not_repaired(self):
        bad = {"ap-southeast-1": "ap-southeast-l"}
        result = self.run_parse(backup_screen(), bad, bad, bad)
        first = result.facts[0]
        self.assertNotIn("region", first.attributes)
        self.assertEqual(first.attributes["uncertain_text"]["region"], ["ap-southeast-l"])


class ParseUncertaintyTests(TmpCase):
    def run_parse(self, base, *overrides, captured=CAPTURED):
        path = self.write("shot.png", b"x")
        passes = passes_from(base, *overrides)
        return ip.parse(path, self.ev("shot.png", captured), ocr=lambda p: passes)

    def test_conflicting_identifier_is_dropped_and_reported(self):
        result = self.run_parse(backup_screen(), {"vendor847@example.test": "a@x.com"}, {"vendor847@example.test": "b@x.com"}, {"vendor847@example.test": "zzzz"})
        first = result.facts[0]
        self.assertEqual(first.identifiers, ())
        self.assertEqual(first.attributes["ocr"]["customer_email"], "conflict")
        self.assertEqual(set(first.attributes["uncertain_text"]["customer_email"]), {"a@x.com", "b@x.com", "zzzz"})
        issue = next(i for i in result.issues if i.code == "UNCERTAIN_TEXT")
        self.assertIn("conflict", issue.message)
        self.assertEqual(first.resource_id, "backup_9291")  # the other fields are unaffected

    def test_single_reading_becomes_an_uncertain_identifier_that_never_matches(self):
        result = self.run_parse(account_screen(), {}, {"user_11": "user_ll"}, {"user_11": "user_ll"})
        fact = result.facts[0]
        user = next(i for i in fact.identifiers if i.field == "customer_reference")
        self.assertEqual((user.value, user.kind, user.via), ("user_11", "uncertain", "ocr"))
        self.assertEqual(fact.attributes["ocr"]["customer_reference"], "single")
        # ... and exact matching ignores it:
        users = [Identity("user_11", "Hazel Stone", "other@x.com", "+81 1", email_hash("other@x.com"), "2026-01-01T00:00:00Z")]
        subject = IdentityIndex(users, []).identifiers("user_11", "2026-12-31T00:00:00Z")
        store = store_mod.EvidenceStore([result], [EvidenceFile("evidence_01", "evidence/shot.png", "o", CAPTURED)])
        self.assertEqual(store.facts_for_subject(subject, "2026-12-31T00:00:00Z"), [])
        # the confirmed email still matches its own subject
        users = [Identity("user_11", "Hazel Stone", "mk.48297@example.test", "+81 90 5550 1234", email_hash("mk.48297@example.test"), "2026-01-01T00:00:00Z")]
        subject = IdentityIndex(users, []).identifiers("user_11", "2026-12-31T00:00:00Z")
        hits = store.facts_for_subject(subject, "2026-12-31T00:00:00Z")
        self.assertEqual({kind for m in hits for _, kind in m.matched}, {"email", "phone"})

    def test_unreadable_resource_id_is_not_invented(self):
        garbled = {"backup_9291": "backup_9z91"}
        result = self.run_parse(backup_screen(), garbled, garbled, garbled)
        first = result.facts[0]
        self.assertIsNone(first.resource_id)
        self.assertEqual(first.attributes["uncertain_text"]["resource_id"], ["backup_9z91"])
        self.assertTrue(any(i.code == "UNCERTAIN_TEXT" and "resource_id" in i.locator for i in result.issues))
        self.assertEqual(first.identifiers[0].value, "vendor847@example.test")

    def test_mixed_case_email_is_malformed(self):
        mixed = {"bm4273@example.test": "bm4273@exampIe.test"}
        result = self.run_parse(backup_screen(), mixed, mixed, mixed)
        fact = next(f for f in result.facts if f.resource_id == "backup_9287")
        self.assertEqual(fact.identifiers, ())
        self.assertEqual(fact.attributes["ocr"]["customer_email"], "unread")

    def test_blank_cells_are_not_reported_as_uncertain(self):
        result = self.run_parse(search_screen(), {}, {}, {})
        second = result.facts[1]
        self.assertEqual(second.resource_id, "search_1874")
        self.assertEqual(second.attributes["ocr"]["display_name"], "empty")
        self.assertEqual([i.field for i in second.identifiers], ["email"])
        self.assertFalse(any("display_name" in i.locator or "customer_reference" in i.locator for i in result.issues))

    def test_a_pass_that_loses_a_row_is_ignored_and_reported(self):
        full = passes_from(backup_screen(), {}, {})  # scales 2 and 3 see all three rows
        short = screen("Vault Recovery Manager", title="Customer-platform archives", badge="Healthy", declared=3,
                       subtitle="Northstar Production · weekly snapshots", header=BACKUP_HEADER,
                       rows=[["backup_9291", "vendor847@example.test", "ap-southeast-1", "26 Apr 02:00", "102.3 GB", "AES-256", "Archive"],
                             ["backup_9287", "bm4273@example.test", "eu-west-1", "26 Apr 02:00", "84.7 GB", "AES-256", "Archive"]],
                       panel_title="backup_9287", panel=[("Account", "bm4273@example.test")])
        passes = full + [OcrPass(4, WIDTH, HEIGHT, tuple(short))]  # scale 4 lost the middle row
        path = self.write("shot.png", b"x")
        result = ip.parse(path, self.ev("shot.png"), ocr=lambda p: passes)
        self.assertIn("ROW_COUNT_MISMATCH", [i.code for i in result.issues])
        self.assertEqual([f.resource_id for f in result.facts], ["backup_9291", "backup_9288", "backup_9287"])
        self.assertEqual(result.facts[1].attributes["ocr"]["resource_id"], "confirmed")  # two passes remain

    def test_a_single_pass_can_confirm_nothing(self):
        result = self.run_parse(backup_screen(), {})
        self.assertIn("TOO_FEW_PASSES", [i.code for i in result.issues])
        first = result.facts[0]
        self.assertIsNone(first.resource_id)
        self.assertEqual(first.identifiers[0].kind, "uncertain")

    def test_unreadable_subject_cell_falls_back_to_the_page_filter_and_says_so(self):
        # OCR lost the underscore in the table cells ("user 03"); the page header still reads
        # "Subject filter: user_03" clearly.
        result = self.run_parse(audit_screen(subject_cell="user 03"), {}, {}, {})
        fact = result.facts[0]
        self.assertEqual(fact.resource_id, "audit_record_003")
        self.assertEqual((fact.identifiers[0].value, fact.identifiers[0].kind), ("user_03", "user_id"))
        self.assertIn("page subject filter", fact.attributes["subject_source"])
        confirmed = self.run_parse(audit_screen(), {}, {}, {}).facts[0]
        self.assertEqual(confirmed.attributes["subject_source"], "table cell")


class ParseFailureTests(TmpCase):
    def parse(self, ocr_fn, name="shot.png"):
        return ip.parse(self.write(name, b"x"), self.ev(name), ocr=ocr_fn)

    def codes(self, result):
        return [i.code for i in result.issues]

    def test_ocr_unavailable_is_an_explicit_error(self):
        def boom(path):
            raise OcrUnavailable("the Windows OCR engine is only available on Windows")
        result = self.parse(boom)
        self.assertEqual((self.codes(result), result.facts), (["OCR_UNAVAILABLE"], []))
        self.assertEqual(result.issues[0].level, "ERROR")

    def test_ocr_failure_is_an_explicit_error(self):
        def boom(path):
            raise OcrFailed("timed out")
        self.assertEqual(self.codes(self.parse(boom)), ["OCR_FAILED"])

    def test_screenshot_with_no_text(self):
        empty = [OcrPass(2, WIDTH, HEIGHT, ()), OcrPass(3, WIDTH, HEIGHT, ())]
        result = self.parse(lambda p: empty)
        self.assertEqual(sorted(set(self.codes(result))), ["EMPTY_OCR_PASS", "NO_TEXT"])
        self.assertEqual(result.facts, [])

    def test_unknown_application(self):
        base = screen("Mystery Dashboard", title="Hello", subtitle="a long subtitle with plenty of ordinary words here to pass the minimum")
        result = self.parse(lambda p: passes_from(base, {}, {}, {}))
        self.assertEqual((self.codes(result), result.facts), (["UNRECOGNIZED_SCREEN"], []))

    def test_known_screen_without_records_is_an_error(self):
        base = screen("Vault Recovery Manager", title="Customer-platform archives", subtitle="x",
                      header=[("Archive ID", 258), ("Account", 454), ("Region", 752)], rows=[], declared=0)
        result = self.parse(lambda p: passes_from(base, {}, {}, {}))
        self.assertIn("NO_FACTS", self.codes(result))

    def test_a_mostly_empty_pass_is_flagged_but_others_still_work(self):
        passes = passes_from(backup_screen(), {}, {}) + [OcrPass(4, WIDTH, HEIGHT, tuple(words("Vault", 244, 23)))]
        result = self.parse(lambda p: passes)
        self.assertIn("EMPTY_OCR_PASS", self.codes(result))
        self.assertEqual(len(result.facts), 3)
        self.assertEqual(result.facts[0].attributes["ocr"]["resource_id"], "confirmed")

    def test_unreadable_file_through_the_store_dispatch(self):
        self.write("evidence/bad.png", b"\x89PNG not really")
        original = store_mod.PARSERS[".png"]
        store_mod.PARSERS[".png"] = lambda path, ev: ip.parse(path, ev, ocr=lambda p: (_ for _ in ()).throw(OcrFailed("cannot decode image")))
        try:
            result = store_mod.extract_file(EvidenceFile("e9", "evidence/bad.png", "o", CAPTURED), self.dir)
        finally:
            store_mod.PARSERS[".png"] = original
        self.assertEqual(self.codes(result), ["OCR_FAILED"])


class TimeCutoffTests(TmpCase):
    def test_screen_time_after_file_capture_is_flagged_and_excluded_for_earlier_audits(self):
        self.write("evidence/s.png", b"x")
        passes = passes_from(search_screen(executed="01 Sep 2026 06:00:14 UTC"), {}, {}, {})
        original = store_mod.PARSERS[".png"]
        store_mod.PARSERS[".png"] = lambda path, ev: ip.parse(path, ev, ocr=lambda p: passes)
        try:
            result = store_mod.extract_file(EvidenceFile("evidence_09", "evidence/s.png", "o", "2026-09-01T06:00:00Z"), self.dir)
        finally:
            store_mod.PARSERS[".png"] = original
        fact = result.facts[0]
        self.assertEqual((fact.observed_at, fact.captured_at), ("2026-09-01T06:00:14Z", "2026-09-01T06:00:00Z"))
        self.assertIn("RECORD_AFTER_CAPTURE", [i.code for i in result.issues])
        self.assertFalse(fact.available_by("2026-09-01T06:00:10Z"))
        self.assertTrue(fact.available_by("2026-09-01T06:00:14Z"))

    def test_unconfirmed_screen_time_falls_back_to_the_manifest_time(self):
        base = search_screen(executed="01 Sep 2026 06:00:14 UTC")
        # Only one pass reads a well-formed time (the others are garbled), so it is "single", not confirmed.
        result = ip.parse(self.write("s.png", b"x"), self.ev("s.png"), ocr=lambda p: passes_from(
            base, {}, {"06:00:14": "06:0O:14"}, {"06:00:14": "O6:00:14"}))
        fact = result.facts[0]
        self.assertEqual(fact.attributes["ocr"]["observed_at"], "single")
        self.assertEqual(fact.observed_at, CAPTURED)

    def test_image_facts_obey_the_manifest_capture_time(self):
        result = ip.parse(self.write("b.png", b"x"), self.ev("b.png", "2026-09-10T00:00:00Z"),
                          ocr=lambda p: passes_from(backup_screen(), {}, {}, {}))
        fact = result.facts[0]
        self.assertFalse(fact.available_by("2026-09-09T23:59:59Z"))
        self.assertTrue(fact.available_by("2026-09-10T00:00:00Z"))


# --------------------------------------------------------------------------- real images

REAL = HAS_DATASET and ocr.available()

# What each screenshot shows, read by eye from the image itself (never from sample answers). The screenshots show
# personal data (fictional customers), so this table keeps only plain non-personal ids and SHA-256 digests of the
# exact text of emails, names and normalised phone numbers: ``digest(value)`` is compared, never the value.
TRUTH = {
    "evidence_04": {"resources": {"account_5762"}, "ids": set(), "emails": {"db83a42f1f41508b4c44fc526100f9663ea8f72b347f16769ef3ac6975015fa2"}, "other": {"661d1342fb06d0b9ad32b4ecd234a161f995d1dd3249ca8db7b10cf789dfc540"},
                    "phones": {"06d75c17b9005d4a49637b3cfb4767e7c5baf9c52d36d40e85c34c409f1b725c"}},
    "evidence_09": {"resources": {"search_428", "search_1874"}, "ids": {"user_13"},
                    "emails": {"7afbbca4e0e4d5228a422ca39f5453505d365849ba5e20a288a203f0ba505c3d", "f244251fefdc64b69254a9a865e1517f5be0000743ea7d2fdc0f5b3adb0fda5d"}, "other": {"86f76204fb037f697915ae22cf6db5b65bd49ee1f23df5280a98d09ed779b1b5"}},
    "evidence_14": {"resources": {"data_flow_1121", "data_flow_9846"}, "ids": set(),
                    "emails": {"ea61073600454d4cb320cd63802b6e63ceb0a1398a7b4dd10049a71729471d00", "3a998570469df6b3cf40bb58e9b2a55e4890cded9f801df993ed6584f990826c"}, "other": set()},
    "evidence_19": {"resources": {"analytics_7438"}, "ids": {"user_20"}, "emails": {"09f05b9fb59a2493e9349bbfeac4ab5288a534689c8ecbe44bcc106f5ede50a7"},
                    "other": {"785047bb16bd8e13672103b82215c53ea0b649df71774f27f8f05fcb8223a766"}},
    "evidence_23": {"resources": {"export_1261", "export_1260", "export_1259", "export_1258"}, "ids": set(),
                    "emails": {"f6ef14336a9bfe2a63e445f2642e9145efc07b6840189a83f5caa0c2fa548469", "0649eddaba37af918d82b64da379e6623d34cf290b2a26ecbe53bafc1484f8af", "61b09761f981abf5391d437e4b8c0316e4e030ce983b97bd6723a3402c777f0a", "12810566ec94895965476862c1486912c1e2a90dced75f5adeed8e675e9759a4"},
                    "other": set()},
    "evidence_28": {"resources": {"support_1034"}, "ids": set(), "emails": {"2223309bb52ac82b56ee55504acf27956503f83a61a81703bd9f24a5f5a292c6"}, "other": {"9ed5fd68d6a3672f233bb9fecab18a82c36573f02962455341844c28b82f9c4f"}},
    "evidence_32": {"resources": {"backup_9291", "backup_9288", "backup_9287", "backup_9286"}, "ids": set(),
                    "emails": {"272cd42cfe5629f0a60fc6d88462f16d930dcf90680ce0a17bb0690a97df3a57", "368e0776d757d5fa352269b247642f161fd9a0d15d59ea34259b79f61f5ad1d4", "7001f88650909c41c862cc4394921012deaa96f9d5ef925f4b052ad6f30aeb62", "a7135c40f536383ed4a30885d386a40d2a8eb2f37ca22dc1a44a72ab0d8bd18a"},
                    "other": set()},
    "evidence_37": {"resources": {f"audit_record_00{i}" for i in range(1, 7)}, "ids": {"user_03"}, "emails": set(), "other": set()},
    "evidence_42": {"resources": set(), "ids": set(), "emails": set(), "other": set()},
    "evidence_46": {"resources": set(), "ids": set(), "emails": set(), "other": set()},
    "evidence_60": {"resources": {"marketing_001"}, "ids": set(), "emails": {"8e7b1729854eb17c37d2f8a5d8a731d11fcb62d02467d7f9fea71520227230b4"}, "other": {"31b0043d97693c72ef8d04b8968fc8707d3f8341d0b289e4ac66d5c190ea4316"},
                    "phones": {"e2921c9781049c6e63758a90c60a3931aaf543784a3e0833eddbd6a580f85cf1"}},
}
FACT_COUNTS = {"evidence_04": 1, "evidence_09": 2, "evidence_14": 2, "evidence_19": 1, "evidence_23": 4, "evidence_28": 1,
               "evidence_32": 4, "evidence_37": 6, "evidence_42": 1, "evidence_46": 8, "evidence_60": 1}


@unittest.skipUnless(REAL, "needs dataset/ and the Windows OCR engine (or a cache)")
class RealScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_dataset()
        cls.results = {}
        for e in ds.evidence_files:
            if e.file_path.endswith(".png"):
                cls.results[e.file_id] = ip.parse(DATASET_DIR / e.file_path, e)
        cls.manifest = {e.file_id: e for e in ds.evidence_files}

    def test_all_eleven_screenshots_parse_without_errors(self):
        self.assertEqual(set(self.results), set(TRUTH))
        for file_id, result in self.results.items():
            self.assertEqual([i for i in result.issues if i.level == "ERROR"], [], file_id)
            self.assertEqual(len(result.facts), FACT_COUNTS[file_id], file_id)

    def test_every_confirmed_value_matches_what_the_screenshot_shows(self):
        for file_id, result in self.results.items():
            truth = TRUTH[file_id]
            for fact in result.facts:
                if fact.resource_id is not None:
                    self.assertIn(fact.resource_id, truth["resources"], f"{file_id}: {fact.resource_id}")
                for ident in fact.identifiers:
                    if ident.kind == "uncertain":
                        continue
                    if ident.kind == "phone":
                        self.assertIn(digest(normalize_phone(ident.value)), truth.get("phones", set()), file_id)
                    elif ident.kind == "email":
                        self.assertIn(digest(ident.value), truth["emails"], f"{file_id}: an email")
                    else:
                        self.assertTrue(ident.value in truth["ids"] or digest(ident.value) in truth["other"], f"{file_id}: {ident.kind}")

    def test_the_readable_identifiers_are_in_fact_confirmed(self):
        for file_id in ("evidence_09", "evidence_14", "evidence_19", "evidence_23", "evidence_28", "evidence_32", "evidence_60"):
            seen = {f.resource_id for f in self.results[file_id].facts}
            self.assertEqual(seen, TRUTH[file_id]["resources"], file_id)
            emails = {digest(i.value) for f in self.results[file_id].facts for i in f.identifiers if i.kind == "email"}
            self.assertTrue(TRUTH[file_id]["emails"] <= emails, file_id)

    def test_known_limit_digit_one_versus_letter_l_stays_uncertain(self):
        # OCR reads "user_11" and "user_17" as "user_ll"/"user 17"; they must never be presented as confirmed.
        f04 = self.results["evidence_04"].facts[0]
        user = next(i for i in f04.identifiers if i.field == "customer_reference")
        self.assertEqual(user.kind, "uncertain")
        f42 = self.results["evidence_42"].facts[0]
        self.assertEqual(f42.identifiers, ())
        self.assertIn("subject_reference", f42.attributes["uncertain_text"])
        self.assertTrue(any(i.code == "UNCERTAIN_TEXT" for i in self.results["evidence_42"].issues))

    def test_provenance_of_every_image_fact(self):
        for file_id, result in self.results.items():
            e = self.manifest[file_id]
            for fact in result.facts:
                self.assertEqual((fact.evidence_id, fact.captured_at, fact.provenance.path, fact.provenance.parser),
                                 (file_id, e.captured_at, e.file_path, "image"))
                self.assertTrue(fact.provenance.locator)
                self.assertEqual(fact.attributes["source"], "screenshot_ocr")
                self.assertTrue(fact.attributes["state_basis"])

    def test_audit_screen_uses_the_documented_fallback(self):
        facts = self.results["evidence_37"].facts
        self.assertTrue(all(f.identifiers and f.identifiers[0].value == "user_03" for f in facts))
        self.assertTrue(all("page subject filter" in f.attributes["subject_source"] for f in facts))

    def test_decoy_search_hit_does_not_inherit_the_searched_address(self):
        decoy = next(f for f in self.results["evidence_09"].facts if f.resource_id == "search_1874")
        self.assertEqual([digest(i.value) for i in decoy.identifiers if i.kind == "email"], ["f244251fefdc64b69254a9a865e1517f5be0000743ea7d2fdc0f5b3adb0fda5d"])


if __name__ == "__main__":
    unittest.main()
