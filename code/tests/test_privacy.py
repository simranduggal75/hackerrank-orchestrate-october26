"""The files that ship in code.zip must not contain personal data copied from the challenge dataset.

The dataset is read at test time to build the forbidden sets, so this file itself holds no personal data.
"""

import csv
import re
import unittest

import packaging
from loaders import DATASET_DIR
from tests.helpers import NEEDS_DATASET

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"\+\d[\d ()-]{7,}\d")
# Placeholder domains only ("exampie" is the deliberate l -> I OCR-confusable variant of "example").
ALLOWED_DOMAINS = {"example.test", "exampie.test", "example.com", "example.org", "x.com", "x.io", "z.com"}


def shipped_texts():
    for name, path in packaging.members(packaging.ROOT):
        try:
            yield name, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


class PlaceholderDomainTests(unittest.TestCase):
    def test_every_email_in_a_shipped_file_uses_a_placeholder_domain(self):
        seen = 0
        for name, text in shipped_texts():
            for email in EMAIL.findall(text):
                seen += 1
                self.assertIn(email.split("@", 1)[1].lower(), ALLOWED_DOMAINS, f"{name}: {email}")
        self.assertGreater(seen, 50)  # the scan really covered the fixtures


@NEEDS_DATASET
class NoDatasetPersonalDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def rows(name):
            with open(DATASET_DIR / name, encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))

        identities = rows("user_identities.csv")
        cls.emails = {r["email"].lower() for r in identities}
        for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            cls.emails |= {r["email"].lower() for r in rows(name)}
        for r in rows("user_audit_log.csv"):
            cls.emails |= {v.lower() for v in (r["old_value"], r["new_value"]) if "@" in v}
        for path in DATASET_DIR.rglob("*"):  # every email that appears anywhere in the evidence text
            if path.is_file() and path.suffix in (".sql", ".json", ".log", ".txt", ".csv"):
                cls.emails |= {e.lower() for e in EMAIL.findall(path.read_text(encoding="utf-8", errors="ignore"))}
        cls.names = {r["name"] for r in identities if len(r["name"]) > 5}
        cls.phones = {re.sub(r"\D", "", r["phone"]) for r in identities}

    def test_no_email_from_the_dataset(self):
        for name, text in shipped_texts():
            for email in EMAIL.findall(text):
                self.assertNotIn(email.lower(), self.emails, f"{name}: dataset email")

    def test_no_customer_name_from_the_dataset(self):
        for name, text in shipped_texts():
            for person in self.names:
                self.assertNotIn(person, text, f"{name}: dataset name")

    def test_no_phone_number_from_the_dataset(self):
        for name, text in shipped_texts():
            for phone in PHONE.findall(text):
                self.assertNotIn(re.sub(r"\D", "", phone), self.phones, f"{name}: dataset phone")

    def test_the_archive_that_would_be_built_contains_none_either(self):
        import tempfile
        import zipfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "code.zip"
            self.assertEqual(packaging.build(packaging.ROOT, archive), [])
            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    text = zf.read(member).decode("utf-8", errors="ignore")
                    self.assertFalse({e.lower() for e in EMAIL.findall(text)} & self.emails, member)


if __name__ == "__main__":
    unittest.main()
