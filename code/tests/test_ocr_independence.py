"""The submission must not depend on OCR being available: screenshots are provisional evidence."""

import unittest

import evidence.image_parser as image_parser
from engine import DecisionEngine
from evidence.ocr import OcrUnavailable
from evidence.store import extract_all
from loaders import load_dataset
from tests.helpers import NEEDS_DATASET


@NEEDS_DATASET
class OcrIndependenceTests(unittest.TestCase):
    def test_no_status_or_step_changes_when_the_ocr_engine_is_unavailable(self):
        ds = load_dataset()
        with_ocr = DecisionEngine(ds, extract_all(ds.evidence_files))
        original = image_parser.parse.__defaults__

        def unavailable(*args, **kwargs):
            raise OcrUnavailable("simulated: no OCR engine")

        image_parser.parse.__defaults__ = (unavailable,)
        try:
            store = extract_all(ds.evidence_files)
            self.assertFalse([f for f in store.facts if f.provenance.parser == "image"])
            without_ocr = DecisionEngine(ds, store)
            for case in ds.requests + ds.sample_requests:
                a, b = with_ocr.decide(case), without_ocr.decide(case)
                self.assertEqual((a.status, a.steps_json), (b.status, b.steps_json), case.case_id)
        finally:
            image_parser.parse.__defaults__ = original


if __name__ == "__main__":
    unittest.main()
