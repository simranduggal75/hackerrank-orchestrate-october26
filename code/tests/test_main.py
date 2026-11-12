"""Smoke test for the read-only ``check`` command."""

import contextlib
import io
import unittest

import main
from loaders import DATASET_DIR


@unittest.skipUnless((DATASET_DIR / "deletion_requests.csv").exists(), "dataset/ not present")
class CheckCommandTests(unittest.TestCase):
    def test_check_runs_and_resolves_every_request(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main(["check"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("'CURRENT': 418, 'HISTORICAL': 100", text)
        self.assertNotIn("UNRESOLVED", text)

    def test_no_command_and_run_both_produce_the_submission_file_and_nothing_else_does(self):
        # The runner is replaced: the suite must never write the real output.csv.
        calls = []
        original = main.run_submission
        main.run_submission = lambda output: calls.append(output) or 0
        try:
            self.assertEqual(main.main([]), 0)
            self.assertEqual(main.main(["run", "--output", "somewhere.csv"]), 0)
        finally:
            main.run_submission = original
        self.assertEqual(calls, [None, "somewhere.csv"])

    def test_read_only_commands_never_dispatch_to_the_submission_writer(self):
        original = main.run_submission
        main.run_submission = lambda output: self.fail("a read-only command must not write output.csv")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["check"]), 0)
        finally:
            main.run_submission = original


if __name__ == "__main__":
    unittest.main()
