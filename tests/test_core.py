"""Real PDF extraction and local storage tests; no model/network dependency."""

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from pdfdesk.core import CHUNK_SIZE, DocumentError, MAX_BYTES, Store


def pdf_bytes(pages):
    output = io.BytesIO()
    document = canvas.Canvas(output)
    for text in pages:
        if text:
            obj = document.beginText(45, 760)
            for line in text.splitlines():
                obj.textLine(line)
            document.drawText(obj)
        document.showPage()
    document.save()
    return output.getvalue()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_real_multipage_extraction_and_persistence(self):
        source = pdf_bytes(["Project Aurora\nThe launch date is March 14.", "Budget is 42 million dollars."])
        document = self.store.import_pdf(source, "report.pdf")
        self.assertEqual(document["page_count"], 2)
        self.assertEqual(document["pages"][0], {"page": 1, "text": "Project Aurora The launch date is March 14."})
        self.assertEqual(document["warnings"], [])
        restarted = Store(self.root)
        self.assertEqual(restarted.get_document(document["id"]), document)
        self.assertEqual(restarted.get_pdf_path(document["id"]).read_bytes(), source)
        self.assertEqual(restarted.list_documents()[0]["id"], document["id"])
        self.assertNotIn("pages", restarted.list_documents()[0])

    def test_mixed_blank_pages_preserve_page_provenance(self):
        document = self.store.import_pdf(pdf_bytes(["First page", "", "The launch date is June 2."]), "mixed.pdf")
        self.assertEqual([p["page"] for p in document["pages"]], [1, 2, 3])
        self.assertEqual(document["pages"][1]["text"], "")
        self.assertEqual(len(document["warnings"]), 1)
        self.assertIn("Page 2", document["warnings"][0])
        found = self.store.search(document["id"], "What is the launch date?")
        self.assertEqual(found[0]["page"], 3)
        self.assertEqual(found[0]["id"], "p3-s1")

    def test_retrieval_is_document_local_and_handles_multipart(self):
        one = self.store.import_pdf(pdf_bytes(["Launch date is April 1.", "Shipping budget is 500 credits."]), "one.pdf")
        other = self.store.import_pdf(pdf_bytes(["Secret launch date is December 30."]), "other.pdf")
        found = self.store.search(one["id"], "What are the launch date and shipping budget?")
        self.assertEqual({p["page"] for p in found}, {1, 2})
        self.assertTrue(all("Secret" not in p["text"] for p in found))
        self.assertEqual(self.store.search(other["id"], "shipping budget"), [])
        self.assertEqual(self.store.search(one["id"], "unicorns"), [])
        self.assertEqual(self.store.search(one["id"], "what is the"), [])
        self.assertEqual(len(self.store.search(one["id"], "launch budget", limit=1)), 1)

    def test_chunks_are_bounded_and_exact_substrings_of_their_page(self):
        source = " ".join(f"word{number}" for number in range(650))
        document = self.store.import_pdf(pdf_bytes([source, "Second page text."]), "long.pdf")
        chunks = self.store.passages(document["id"])
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), CHUNK_SIZE)
            page = document["pages"][chunk["page"] - 1]
            self.assertIn(chunk["text"], page["text"])
        first, second = chunks[:2]
        self.assertTrue(set(first["text"].split()) & set(second["text"].split()))

    def test_reject_malformed_empty_oversize_and_image_only(self):
        for data in [b"", b"not a PDF", b"%PDF-1.4\nmalformed", b"%PDF-" + b"x" * MAX_BYTES, pdf_bytes([""])]:
            with self.subTest(size=len(data)):
                with self.assertRaises(DocumentError):
                    self.store.import_pdf(data, "bad.pdf")
        self.assertEqual(self.store.list_documents(), [])
        self.assertEqual(list(self.store.originals.iterdir()), [])

    def test_reject_encrypted_and_too_many_pages(self):
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(pdf_bytes(["Private page"])) ))
        writer.encrypt("password")
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaisesRegex(DocumentError, "Encrypted"):
            self.store.import_pdf(stream.getvalue(), "encrypted.pdf")
        with self.assertRaisesRegex(DocumentError, "80-page"):
            self.store.import_pdf(pdf_bytes(["Page text"] * 81), "many.pdf")

    def test_extraction_failure_does_not_leave_partial_import(self):
        with patch("pypdf._page.PageObject.extract_text", side_effect=RuntimeError("internal error")):
            with self.assertRaisesRegex(DocumentError, "page 1"):
                self.store.import_pdf(pdf_bytes(["Some text"]), "broken.pdf")
        self.assertEqual(self.store.list_documents(), [])
        self.assertEqual(list(self.store.originals.iterdir()), [])

    def test_database_failure_rolls_back_the_original_file(self):
        source = pdf_bytes(["Do not leave this original behind"])
        with patch("pdfdesk.core.sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.import_pdf(source, "failed.pdf")
        self.assertEqual(self.store.list_documents(), [])
        self.assertEqual(list(self.store.originals.iterdir()), [])

    def test_shared_store_supports_concurrent_import_and_reads(self):
        source = pdf_bytes(["Concurrent reader evidence"])

        def import_and_read(number):
            document = self.store.import_pdf(source, f"document-{number}.pdf")
            result = self.store.search(document["id"], "reader evidence")
            self.assertEqual(result[0]["page"], 1)
            return document["id"]

        with ThreadPoolExecutor(max_workers=4) as workers:
            identifiers = list(workers.map(import_and_read, range(8)))
        self.assertEqual(len(set(identifiers)), 8)
        self.assertEqual(len(self.store.list_documents()), 8)

    def test_filename_traversal_is_only_sanitized_metadata(self):
        document = self.store.import_pdf(pdf_bytes(["Hello world"]), "../../outside\\report.pdf")
        self.assertEqual(document["name"], "report.pdf")
        path = self.store.get_pdf_path(document["id"])
        self.assertEqual(path.parent, self.store.originals)
        self.assertEqual(path.name, document["id"] + ".pdf")
        with self.assertRaises(KeyError):
            self.store.get_pdf_path("../../outside")

    def test_delete_removes_original_pages_and_passages(self):
        document = self.store.import_pdf(pdf_bytes(["Delete this document"]), "delete.pdf")
        path = self.store.get_pdf_path(document["id"])
        self.store.delete_document(document["id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.store.list_documents(), [])
        for method in [self.store.get_document, self.store.get_pdf_path, self.store.passages, self.store.delete_document]:
            with self.subTest(method=method.__name__):
                with self.assertRaises(KeyError):
                    method(document["id"])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM passages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
