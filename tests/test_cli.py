from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pdfdesk.cli import main
from pdfdesk.model import ModelError
from test_core import pdf_bytes


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.arguments = ["--data-dir", str(self.root / "store")]
        self.source = self.root / "report.pdf"
        self.source.write_bytes(pdf_bytes(["A local source document."]))

    def tearDown(self):
        self.directory.cleanup()

    def invoke(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = main(self.arguments + arguments)
        return status, output.getvalue(), error.getvalue()

    def test_cli_local_document_lifecycle_without_model_access(self):
        with patch("pdfdesk.cli.Ollama", side_effect=AssertionError("Model must not be needed")):
            status, output, error = self.invoke(["import", str(self.source)])
            self.assertEqual((status, error), (0, ""))
            document_id = json.loads(output)["id"]
            self.assertEqual(json.loads(self.invoke(["list"])[1])[0]["id"], document_id)
            self.assertEqual(json.loads(self.invoke(["show", document_id])[1])["pages"][0]["text"], "A local source document.")
            self.assertEqual(json.loads(self.invoke(["delete", document_id])[1])["deleted"], document_id)
            self.assertEqual(json.loads(self.invoke(["list"])[1]), [])

    def test_cli_model_commands_use_selected_configuration(self):
        document_id = json.loads(self.invoke(["import", str(self.source)])[1])["id"]
        with patch("pdfdesk.cli.Ollama") as model, patch("pdfdesk.cli.Service") as service:
            service.return_value.summarize.return_value = {"pages": [{"page": 1, "summary": "A summary"}]}
            service.return_value.answer.return_value = {"supported": False, "answer": "No evidence."}
            self.assertEqual(self.invoke(["--model", "local:test", "--ollama-url", "http://127.0.0.1:9999", "summarize", document_id])[0], 0)
            model.assert_called_with(base_url="http://127.0.0.1:9999", model="local:test")
            service.return_value.summarize.assert_called_with(document_id)
            status, output, error = self.invoke(["ask", document_id, "What evidence?"])
            self.assertEqual((status, error), (0, ""))
            self.assertFalse(json.loads(output)["supported"])
            service.return_value.answer.assert_called_with(document_id, "What evidence?")

    def test_cli_search_returns_exact_source_with_original_page_without_a_model(self):
        self.source.write_bytes(pdf_bytes(["Launch is June 2.", "Budget is 42 dollars."]))
        with patch("pdfdesk.cli.Ollama", side_effect=AssertionError("Search must not need a model")):
            document_id = json.loads(self.invoke(["import", str(self.source)])[1])["id"]
            status, output, error = self.invoke(["search", document_id, "  budget  "])
            self.assertEqual((status, error), (0, ""))
            result = json.loads(output)
            self.assertEqual(result["document_id"], document_id)
            self.assertEqual(result["query"], "budget")
            self.assertEqual(len(result["results"]), 1)
            passage = result["results"][0]
            self.assertEqual(passage["id"], "p2-s1")
            self.assertEqual(passage["page"], 2)
            self.assertEqual(passage["text"], "Budget is 42 dollars.")
            self.assertGreater(passage["score"], 0)
            self.assertEqual(passage["url"], f"/api/documents/{document_id}/file#page=2")
            self.assertEqual(json.loads(self.invoke(["search", document_id, "unicorn"])[1])["results"], [])

    def test_cli_search_rejects_invalid_queries_and_missing_documents(self):
        document_id = json.loads(self.invoke(["import", str(self.source)])[1])["id"]
        with patch("pdfdesk.cli.Ollama", side_effect=AssertionError("Search must not need a model")):
            for query in ["", " \t ", "x" * 2001]:
                with self.subTest(query_length=len(query)):
                    status, output, error = self.invoke(["search", document_id, query])
                    self.assertEqual((status, output), (1, ""))
                    self.assertIn("search query between", error)
            self.assertEqual(self.invoke(["search", "a" * 32, "budget"]),
                             (1, "", "Document not found.\n"))

    def test_cli_reports_missing_files_documents_and_model_failures(self):
        status, output, error = self.invoke(["import", str(self.root / "missing.pdf")])
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertIn("missing.pdf", error)
        self.assertEqual(self.invoke(["show", "a" * 32]), (1, "", "Document not found.\n"))
        document_id = json.loads(self.invoke(["import", str(self.source)])[1])["id"]
        with patch("pdfdesk.cli.Service") as service:
            service.return_value.answer.side_effect = ModelError("Model unavailable.")
            self.assertEqual(self.invoke(["ask", document_id, "Question?"]), (1, "", "Model unavailable.\n"))

    def test_module_entrypoint_is_runnable(self):
        result = subprocess.run([sys.executable, "-m", "pdfdesk", "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("summarize", result.stdout)
        self.assertIn("--data-dir", result.stdout)

    def test_invalid_server_port_is_a_usage_error(self):
        result = subprocess.run([sys.executable, "-m", "pdfdesk", "serve", "--port", "70000"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Port must be", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
