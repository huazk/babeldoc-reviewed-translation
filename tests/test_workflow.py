"""Lightweight checks without BabelDOC models, OCR downloads, or translation APIs."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from file_bridge import check_mapping, read_json
from pdf_review import dual, qa, reject_overwrite, verify


class MappingTests(unittest.TestCase):
    def test_valid_translation_and_explicit_keep(self):
        self.assertEqual([], check_mapping(
            ["Cost <b0>", "TCP"],
            {"Cost <b0>": "代价 <b0>", "TCP": "TCP"},
            {"TCP": "协议缩写"},
        ))

    def test_missing_empty_and_unexplained_identity_are_rejected(self):
        errors = check_mapping(["missing", "empty", "TCP"], {"empty": "", "TCP": "TCP"}, {})
        self.assertEqual(3, len(errors))

    def test_placeholder_loss_or_duplication_is_rejected(self):
        for target in ["代价", "代价 <b0><b0>", "代价 <b1>"]:
            with self.subTest(target=target):
                self.assertTrue(check_mapping(["Cost <b0>"], {"Cost <b0>": target}, {}))

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mapping.json"
            path.write_text('{"A":"one","A":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                read_json(path)


class PDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.translated = self.root / "translated.pdf"
        self.output = self.root / "dual.pdf"
        self.make_pdf(self.source, ["Original one", "Original two"])
        self.make_pdf(self.translated, ["Translation one", "Translation two"])

    @staticmethod
    def make_pdf(path, lines, width=300):
        with fitz.open() as doc:
            for line in lines:
                page = doc.new_page(width=width, height=200)
                page.insert_text((20, 40), line)
            doc.save(path)

    def args(self):
        return SimpleNamespace(input=self.source, translated=self.translated,
                               out=self.output, dual=self.output, all_pages=True)

    def test_dual_round_trip_preserves_pages(self):
        dual(self.args(), fitz)
        verify(self.args(), fitz)
        with fitz.open(self.output) as doc:
            self.assertEqual(["Original one", "Translation one", "Original two", "Translation two"],
                             [p.get_text().strip() for p in doc])

    def test_incorrect_pairing_is_detected(self):
        dual(self.args(), fitz)
        changed = self.root / "changed.pdf"
        self.make_pdf(changed, ["Wrong page", "Translation two"])
        args = self.args()
        args.translated = changed
        with self.assertRaisesRegex(ValueError, "Pixel mismatch"):
            verify(args, fitz)

    def test_page_dimensions_must_match(self):
        changed = self.root / "wide.pdf"
        self.make_pdf(changed, ["one", "two"], width=400)
        args = self.args()
        args.translated = changed
        with self.assertRaisesRegex(ValueError, "dimensions"):
            dual(args, fitz)
        self.assertFalse(self.output.exists())

    def test_page_counts_must_match(self):
        changed = self.root / "short.pdf"
        self.make_pdf(changed, ["one"])
        args = self.args()
        args.translated = changed
        with self.assertRaisesRegex(ValueError, "Page counts"):
            dual(args, fitz)

    def test_overwrite_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Output must differ"):
            reject_overwrite(self.source, self.source)

    def test_qa_renders_every_page_and_statistics(self):
        output = self.root / "qa"
        qa(SimpleNamespace(input=self.source, out=output, scale=1), fitz)
        stats = json.loads((output / "stats.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len(stats))
        self.assertTrue((output / "page-001.png").exists())
        self.assertTrue((output / "page-002.png").exists())
        self.assertTrue((output / "contact-001.png").exists())


if __name__ == "__main__":
    unittest.main()
