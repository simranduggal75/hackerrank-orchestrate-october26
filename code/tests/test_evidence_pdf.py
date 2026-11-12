"""PDF parser tests. PDFs are generated on the fly with PyMuPDF; tests skip if it is missing."""

import unittest

from evidence import pdf_parser
from loaders import DATASET_DIR, load_dataset
from tests.helpers import CAPTURED, NEEDS_DATASET, TmpCase

try:
    import pymupdf  # type: ignore
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        pymupdf = None

NEEDS_PDF = unittest.skipUnless(pymupdf, "PyMuPDF not installed")

ATTACHMENT = [
    "DELIVERY ADDRESS CORRECTION REQUEST", "Support reference CS-4827 / support_001", "Detail Paula Rowan",
    "Detail pr.37294@example.test", "Detail +91 98765 55501", "Detail order ORD-20264827",
    "Record file delivery_address_correction.pdf", "Detail ticket CS-4827", "Detail uploader customer",
    "captured 12 Sep 2026 07 30 UTC"]
JOURNAL = [
    "COMPLIANCE JOURNAL - SIGNED RECORD EXTRACT", "Reference CJX-20260905-0630", "issued 05 Sep 2026 06 30 UTC",
    "Record Event reference audit_record_003", "Detail subject reference user_03",
    "Detail event Account closure recorded", "Detail actor automated account workflow",
    "recorded 25 Aug 2026 04 03 UTC", "retain until 25 Aug 2027 04 03 UTC"]
REGISTER = [
    "DISPUTED ORDERS AND TAX RECORDS REGISTER", "generated 29 May 2026 08", "00 UTC", "Disputed order entries", "1",
    "ORD-20264118 | user_08 | Card dispute | 04 May 2026 | USD 184.20 | Retain until 04 May 2033 | AR", "2",
    "ORD-20264207 | user_12 | Tax correction | 09 May 2026 | EUR 72.40 | Retain until 09 May 2033 | ML"]
STATEMENT = [
    "NORTHSTAR SHOP - CUSTOMER STATEMENT", "Statement NST-2026-0718-1842", "Customer Ivana Dolan", "user_18",
    "ik.dm48271@example.test", "order ORD-20261260"]


@NEEDS_PDF
class PdfTemplateTests(TmpCase):
    def make_pdf(self, name, *pages):
        doc = pymupdf.open()
        for lines in pages:
            page = doc.new_page()
            y = 72
            for line in lines:
                page.insert_text((60, y), line, fontsize=9)
                y += 14
        path = self.dir / name
        doc.save(str(path))
        doc.close()
        return path

    def parse(self, name, *pages, captured=CAPTURED):
        return pdf_parser.parse(self.make_pdf(name, *pages), self.ev(name, captured))

    def test_support_attachment(self):
        result = self.parse("a.pdf", ATTACHMENT[:6], ATTACHMENT[6:])  # fields split across pages
        self.assertEqual(self.codes(result), [])
        fact = result.facts[0]
        self.assertEqual((fact.system, fact.resource_id, fact.record_type, fact.state),
                         ("support_platform", "support_001", "SUPPORT_ATTACHMENT", "FOUND"))
        self.assertEqual({(i.kind, i.value) for i in fact.identifiers},
                         {("email", "pr.37294@example.test"), ("phone", "+91 98765 55501")})
        self.assertEqual(fact.observed_at, "2026-09-12T07:30:00Z")
        self.assertEqual(fact.attributes, {"ticket": "CS-4827", "file": "delivery_address_correction.pdf",
                                           "uploader": "customer", "order": "ORD-20264827"})
        self.assertEqual((fact.provenance.evidence_id, fact.provenance.path, fact.provenance.parser),
                         ("evidence_01", "evidence/a.pdf", "pdf"))
        self.assertEqual(result.stats["pages"], 2)

    def test_security_journal(self):
        fact = self.parse("j.pdf", JOURNAL).facts[0]
        self.assertEqual((fact.system, fact.resource_id, fact.record_type, fact.observed_at),
                         ("security_service", "audit_record_003", "SECURITY_AUDIT_LOG", "2026-09-05T06:30:00Z"))
        self.assertEqual([(i.field, i.value) for i in fact.identifiers], [("subject_reference", "user_03")])
        self.assertEqual((fact.attributes["event"], fact.attributes["recorded_at"], fact.attributes["retain_until"]),
                         ("Account closure recorded", "2026-08-25T04:03:00Z", "2027-08-25T04:03:00Z"))

    def test_register_yields_one_fact_per_row_without_resource_ids(self):
        result = self.parse("r.pdf", REGISTER)
        self.assertEqual(self.codes(result), [])
        self.assertEqual([(f.record_type, f.resource_id, f.identifiers[0].value) for f in result.facts],
                         [("LEGAL_HOLD_RECORD", None, "user_08"), ("LEGAL_HOLD_RECORD", None, "user_12")])
        first = result.facts[0]
        self.assertEqual((first.observed_at, first.attributes["order_reference"], first.attributes["retain_until"]),
                         ("2026-05-29T08:00:00Z", "ORD-20264118", "2033-05-04T00:00:00Z"))
        self.assertEqual([f.provenance.locator for f in result.facts], ["register row 1", "register row 2"])

    def test_customer_statement(self):
        fact = self.parse("s.pdf", STATEMENT).facts[0]
        self.assertEqual((fact.record_type, fact.resource_id, fact.observed_at), ("CUSTOMER_STATEMENT", None, CAPTURED))
        self.assertEqual({(i.kind, i.value) for i in fact.identifiers}, {("user_id", "user_18"), ("email", "ik.dm48271@example.test")})
        self.assertEqual(fact.attributes["statement"], "NST-2026-0718-1842")

    def test_lookalike_email_in_document_is_an_exact_token(self):
        lines = STATEMENT[:4] + ["cc ik.dm48271.jr@example.test and ik.dm48271@example.test"]
        values = [i.value for i in self.parse("s.pdf", lines).facts[0].identifiers if i.kind == "email"]
        self.assertEqual(values, ["ik.dm48271.jr@example.test", "ik.dm48271@example.test"])

    def test_unknown_template_falls_back_with_a_warning(self):
        result = self.parse("u.pdf", ["QUARTERLY NEWSLETTER", "Write to news@x.com about account_77 and user_5"])
        self.assertEqual(self.codes(result), ["UNRECOGNIZED_TEMPLATE"])
        fact = result.facts[0]
        self.assertEqual((fact.record_type, fact.state, fact.resource_id), ("DOCUMENT", "UNKNOWN", "account_77"))

    def test_recognised_template_missing_its_key_field(self):
        result = self.parse("m.pdf", ["DELIVERY ADDRESS CORRECTION REQUEST", "no reference here"])
        self.assertEqual(sorted(self.codes(result)), ["MISSING_FIELD", "NO_FACTS"])
        self.assertEqual(result.facts, [])

    def test_pdf_without_text_layer(self):
        doc = pymupdf.open()
        doc.new_page()
        path = self.dir / "blank.pdf"
        doc.save(str(path))
        result = pdf_parser.parse(path, self.ev("blank.pdf"))
        self.assertEqual(self.codes(result), ["NO_TEXT"])

    def test_corrupt_pdf_is_reported(self):
        path = self.write("bad.pdf", b"%PDF-1.4 this is not really a pdf")
        result = pdf_parser.parse(path, self.ev("bad.pdf"))
        self.assertEqual((self.codes(result), result.facts), (["UNREADABLE_PDF"], []))

    def test_missing_file_is_reported(self):
        result = pdf_parser.parse(self.dir / "absent.pdf", self.ev("absent.pdf"))
        self.assertEqual(self.codes(result), ["UNREADABLE_PDF"])


class PdfBackendTests(TmpCase):
    def test_missing_backend_is_an_explicit_error(self):
        original = pdf_parser._backend
        pdf_parser._backend = lambda: None
        try:
            result = pdf_parser.parse(self.write("x.pdf", b"%PDF"), self.ev("x.pdf"))
        finally:
            pdf_parser._backend = original
        self.assertEqual(self.codes(result), ["PDF_BACKEND_MISSING"])


@NEEDS_PDF
@NEEDS_DATASET
class RealPdfTests(unittest.TestCase):
    def test_four_real_pdfs(self):
        ds = load_dataset()
        got = {}
        for e in ds.evidence_files:
            if e.file_path.endswith(".pdf"):
                result = pdf_parser.parse(DATASET_DIR / e.file_path, e)
                self.assertEqual(result.issues, [], e.file_id)
                got[e.file_path.split("/")[-1]] = result.facts
        self.assertEqual({k: len(v) for k, v in got.items()},
                         {"account_history_report_20260718.pdf": 1, "legal_retention_register_20260529.pdf": 6,
                          "security_retention_extract.pdf": 1, "delivery_correction_attachment.pdf": 1})
        self.assertEqual(got["security_retention_extract.pdf"][0].attributes["event"], "Account closure recorded")
        self.assertEqual(got["delivery_correction_attachment.pdf"][0].resource_id, "support_001")


if __name__ == "__main__":
    unittest.main()
