"""OCR backend tests: JSON decoding, caching, and explicit unavailability."""

import json
import os
import unittest
from unittest import mock

from evidence import ocr
from evidence.ocr import OcrFailed, OcrUnavailable
from tests.helpers import HAS_DATASET, TmpCase
from loaders import DATASET_DIR

GOOD = {"language": "en-US", "width": 1800, "height": 1150, "passes": [
    {"scale": 2, "words": [["Hello", 10.5, 20.0, 30.0, 12.0], ["a@x.com", 50, 20, 70, 12]]},
    {"scale": 3, "words": []}]}


class DecodingTests(unittest.TestCase):
    def test_boxes_become_original_pixel_rectangles(self):
        passes = ocr.passes_from_json(GOOD)
        self.assertEqual([(p.scale, p.width, p.height, len(p.words)) for p in passes], [(2, 1800, 1150, 2), (3, 1800, 1150, 0)])
        word = passes[0].words[0]
        self.assertEqual((word.text, word.x0, word.y0, word.x1, word.y1), ("Hello", 10.5, 20.0, 40.5, 32.0))
        self.assertEqual(word.yc, 26.0)

    def test_bad_shapes_are_reported(self):
        for bad in ({}, {"width": 1, "height": 1}, {"width": 1, "height": 1, "passes": [{"scale": 2, "words": [["x"]]}]},
                    {"width": "w", "height": 1, "passes": []}):
            with self.subTest(bad=bad), self.assertRaises(OcrFailed):
                ocr.passes_from_json(bad)


class BackendTests(TmpCase):
    def test_non_windows_hosts_report_unavailable(self):
        image = self.write("a.png", b"x")
        with mock.patch.object(ocr.os, "name", "posix"):
            with self.assertRaises(OcrUnavailable):
                ocr.run_windows_ocr(image)
            self.assertFalse(ocr.available())

    def test_missing_powershell_reports_unavailable(self):
        image = self.write("a.png", b"x")
        with mock.patch.object(ocr, "_powershell", return_value=None), mock.patch.object(ocr.os, "name", "nt"):
            with self.assertRaises(OcrUnavailable):
                ocr.run_windows_ocr(image)

    def test_helper_script_ships_with_the_module(self):
        self.assertTrue(ocr.SCRIPT.is_file())
        self.assertIn("Windows.Media.Ocr", ocr.SCRIPT.read_text(encoding="utf-8"))


class CacheTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.cache = self.dir / "cache"
        patcher = mock.patch.dict(os.environ, {ocr.CACHE_ENV: str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.image = self.write("a.png", b"pixels")
        self.calls = 0

    def fake_engine(self, path, scales=ocr.SCALES, timeout=0):
        self.calls += 1
        return ocr.passes_from_json(GOOD)

    def test_second_call_is_served_from_the_cache(self):
        with mock.patch.object(ocr, "run_windows_ocr", self.fake_engine):
            first = ocr.run_ocr(self.image)
            second = ocr.run_ocr(self.image)
        self.assertEqual(self.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)

    def test_cache_is_keyed_by_image_content(self):
        other = self.write("b.png", b"different pixels")
        with mock.patch.object(ocr, "run_windows_ocr", self.fake_engine):
            ocr.run_ocr(self.image)
            ocr.run_ocr(other)
        self.assertEqual(self.calls, 2)

    def test_corrupt_cache_entry_is_ignored_and_rewritten(self):
        with mock.patch.object(ocr, "run_windows_ocr", self.fake_engine):
            ocr.run_ocr(self.image)
            entry = next(self.cache.glob("*.json"))
            entry.write_text("{not json", encoding="utf-8")
            again = ocr.run_ocr(self.image)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(again[0].words), 2)
        json.loads(entry.read_text(encoding="utf-8"))  # healed

    def test_cache_hit_works_where_the_engine_does_not(self):
        with mock.patch.object(ocr, "run_windows_ocr", self.fake_engine):
            ocr.run_ocr(self.image)

        def unavailable(path, scales=ocr.SCALES, timeout=0):
            raise OcrUnavailable("not windows")
        with mock.patch.object(ocr, "run_windows_ocr", unavailable):
            self.assertEqual(len(ocr.run_ocr(self.image)), 2)

    def test_without_a_cache_directory_the_engine_runs_every_time(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ocr.CACHE_ENV, None)
            with mock.patch.object(ocr, "run_windows_ocr", self.fake_engine):
                ocr.run_ocr(self.image)
                ocr.run_ocr(self.image)
        self.assertEqual(self.calls, 2)


@unittest.skipUnless(HAS_DATASET and ocr.available(), "needs dataset/ and the Windows OCR engine")
class RealEngineTests(unittest.TestCase):
    def test_real_engine_returns_three_scaled_passes_in_original_pixels(self):
        path = next((DATASET_DIR / "evidence" / "images").glob("customer_export_console*.png"))
        passes = ocr.run_ocr(path)
        self.assertEqual([p.scale for p in passes], [2, 3, 4])
        for p in passes:
            self.assertEqual((p.width, p.height), (1800, 1100))
            self.assertGreater(len(p.words), 100)
            self.assertTrue(all(0 <= w.x0 < w.x1 <= 1800 and 0 <= w.y0 < w.y1 <= 1100 for w in p.words))
        texts = {w.text for w in passes[0].words}
        self.assertIn("export_1260", texts)


if __name__ == "__main__":
    unittest.main()
