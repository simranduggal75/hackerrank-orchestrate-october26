"""code.zip packaging rules, on a synthetic tree and on the real repository."""

import tempfile
import unittest
import zipfile
from pathlib import Path

import packaging
from tests.helpers import NEEDS_DATASET


def make_tree(root: Path, extra: dict[str, str] | None = None) -> None:
    files = {"code/main.py": "print('hi')\n", "code/README.md": "# readme\n", "code/requirements.txt": "pymupdf==1.28.2\n",
             "code/evaluation/evaluation_report.md": "# report\n", "code/evaluation/usage_report.md": "# usage\n",
             "code/engine.py": "x = 1\n", "code/__pycache__/engine.cpython-311.pyc": "junk", "code/evidence/.ocr_cache/a.json": "{}",
             "code/.env": "KEY=1", "code/data.csv": "a,b", "code/nested.zip": "zip", "dataset/deletion_requests.csv": "a",
             "output.csv": "a", "log.txt": "log"}
    files.update(extra or {})
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SyntheticTreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.archive = self.root / "out.zip"

    def names(self):
        with zipfile.ZipFile(self.archive) as zf:
            return zf.namelist()

    def test_only_the_solution_is_packaged(self):
        make_tree(self.root)
        self.assertEqual(packaging.build(self.root, self.archive), [])
        self.assertEqual(self.names(), sorted(["README.md", "requirements.txt", "code/README.md", "code/engine.py", "code/main.py",
                                                "code/requirements.txt", "code/evaluation/evaluation_report.md",
                                                "code/evaluation/usage_report.md"]))

    def test_a_missing_report_blocks_the_build_and_writes_nothing(self):
        make_tree(self.root)
        (self.root / "code/evaluation/usage_report.md").unlink()
        self.assertTrue(any("usage_report" in p for p in packaging.build(self.root, self.archive)))
        self.assertFalse(self.archive.exists())

    def test_a_secret_blocks_the_build(self):
        fake = "api" + "_key = " + chr(39) + "abcd1234" + "efgh5678" + chr(39) + "\n"  # built at runtime: no secret-like literal in source
        make_tree(self.root, {"code/config.py": fake})
        self.assertTrue(any("config.py" in p for p in packaging.build(self.root, self.archive)))
        self.assertFalse(self.archive.exists())
        make_tree(self.root, {"code/config.py": "x = 'sk-" + "a" * 30 + "'\n"})
        self.assertTrue(packaging.build(self.root, self.archive))

    def test_the_archive_is_reproducible(self):
        make_tree(self.root)
        packaging.build(self.root, self.archive)
        first = self.archive.read_bytes()
        packaging.build(self.root, self.archive)
        self.assertEqual(first, self.archive.read_bytes())

    def test_inspect_rejects_forbidden_members(self):
        for bad in ("dataset/deletion_requests.csv", "code/__pycache__/x.pyc", "code/nested.zip", "output.csv", "code/.env", "../escape.py"):
            with zipfile.ZipFile(self.archive, "w") as zf:
                for name in packaging.REQUIRED:
                    zf.writestr(name, "ok\n")
                zf.writestr(bad, "x")
            self.assertTrue(packaging.inspect(self.archive), bad)


@NEEDS_DATASET
class RealArchiveTests(unittest.TestCase):
    def test_the_real_repository_packages_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "code.zip"
            self.assertEqual(packaging.build(packaging.ROOT, archive), [])
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
            self.assertFalse([n for n in names if n.startswith("dataset") or "__pycache__" in n or n.endswith((".pyc", ".csv", ".zip"))])
            self.assertIn("code/evaluation/evaluation_report.md", names)
            self.assertIn("code/evaluation/usage_report.md", names)
            self.assertTrue(any(n.startswith("code/tests/") for n in names))


if __name__ == "__main__":
    unittest.main()
