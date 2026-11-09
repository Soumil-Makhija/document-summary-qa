import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pdfdesk.core import MAX_BYTES, Store
from pdfdesk.model import ModelError
from pdfdesk.server import make_server
from test_core import pdf_bytes


class FakeModel:
    def health(self):
        return {"available": True, "model": "test-model"}


class FakeService:
    def __init__(self, store, model):
        self.store = store

    def summarize(self, document_id):
        return {"document_id": document_id, "kind": "generated_summary", "pages": [{"page": 1, "summary": "A test summary.", "key_points": []}]}

    def answer(self, document_id, question):
        return {"document_id": document_id, "supported": True, "kind": "source_extract", "question": question, "answer": "Launch is June 2.", "evidence": [{"page": 1, "quote": "Launch is June 2.", "passage_id": "p1-s1"}]}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.service_patch = patch("pdfdesk.server.Service", FakeService)
        self.service_patch.start()
        self.server = make_server(self.store, FakeModel())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service_patch.stop()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def upload(self):
        source = pdf_bytes(["Launch is June 2.", "Budget is 42 dollars."])
        status, headers, body = self.request("POST", "/api/documents", source, {"X-Filename": "test%20report.pdf", "Content-Type": "application/pdf"})
        self.assertEqual(status, 201)
        document = json.loads(body)
        self.assertEqual(document["name"], "test report.pdf")
        return document, source

    def test_full_http_document_lifecycle_and_service_routes(self):
        document, source = self.upload()
        base = "/api/documents/" + document["id"]
        self.assertEqual(json.loads(self.request("GET", "/api/documents")[2])[0]["id"], document["id"])
        self.assertEqual(json.loads(self.request("GET", base)[2])["pages"][1]["page"], 2)
        status, headers, original = self.request("GET", base + "/file")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(original, source)
        self.assertEqual(json.loads(self.request("GET", base + "/passages")[2])[0]["id"], "p1-s1")
        status, _, body = self.request("POST", base + "/summary", b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["kind"], "generated_summary")
        status, _, body = self.request("POST", base + "/answer", json.dumps({"question": "When is launch?"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["evidence"][0]["page"], 1)
        self.assertEqual(self.request("DELETE", base)[0], 200)
        self.assertEqual(self.request("GET", base)[0], 404)
        self.assertEqual(self.request("GET", base + "/file")[0], 404)

    def test_health_and_static_security_headers(self):
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["model"]["available"])
        for resource, content_type in [("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")]:
            status, headers, body = self.request("GET", resource)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(content_type))
            self.assertGreater(len(body), 20)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_wrong_host_and_foreign_origins_are_rejected(self):
        for host in ["evil.example", "127.0.0.1:1", "localhost", f"evil.example:{self.port}"]:
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/documents", headers={"Host": host})[0], 403)
        for origin in ["https://evil.example", "null", "http://127.0.0.1:1"]:
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/documents", b"", {"Origin": origin})[0], 403)
        self.assertEqual(self.request("POST", "/api/documents", b"not pdf", {"Origin": f"http://localhost:{self.port}"})[0], 403)
        self.assertEqual(self.request("POST", "/api/documents", b"not pdf", {"Origin": f"http://127.0.0.1:{self.port}"})[0], 400)
        self.assertEqual(self.request("POST", "/api/documents", b"not pdf", {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})[0], 400)

    def test_invalid_bodies_limits_and_missing_routes_return_json_errors(self):
        document, _ = self.upload()
        answer = f"/api/documents/{document['id']}/answer"
        for body in [b"not json", b"[]", b"{}", b'{"question": ""}', json.dumps({"question": "x" * 2001}).encode()]:
            with self.subTest(body=body[:20]):
                status, _, result = self.request("POST", answer, body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(result))
        self.assertEqual(self.request("POST", "/api/documents", b"", {"Content-Length": str(MAX_BYTES + 1)})[0], 413)
        self.assertEqual(self.request("POST", answer, b"", {"Content-Length": "32769"})[0], 413)
        self.assertEqual(self.request("POST", answer, b"", {"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("POST", answer, b"", {"Transfer-Encoding": "chunked"})[0], 400)
        for path in ["/unknown", "/../core.py", "/api/documents/" + "a" * 32]:
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_model_failure_is_a_502_and_does_not_leak_unexpected_errors(self):
        document, _ = self.upload()
        with patch.object(self.server.service, "summarize", side_effect=ModelError("Model unavailable.")):
            status, _, body = self.request("POST", f"/api/documents/{document['id']}/summary", b"{}")
            self.assertEqual(status, 502)
            self.assertEqual(json.loads(body)["error"], "Model unavailable.")
        with patch.object(self.server.service, "summarize", side_effect=RuntimeError("PRIVATE CONTENT")):
            status, _, body = self.request("POST", f"/api/documents/{document['id']}/summary", b"{}")
            self.assertEqual(status, 500)
            self.assertNotIn(b"PRIVATE CONTENT", body)

    def test_busy_model_has_no_waiting_inference_queue(self):
        document, _ = self.upload()
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def slow_summary(document_id):
            entered.set()
            release.wait(timeout=4)
            return {"pages": []}

        route = f"/api/documents/{document['id']}/summary"
        with patch.object(self.server.service, "summarize", side_effect=slow_summary):
            worker = threading.Thread(target=lambda: outcomes.append(self.request("POST", route, b"{}")[0]))
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            try:
                status, headers, body = self.request("POST", route, b"{}")
                self.assertEqual(status, 429)
                self.assertEqual(headers["Retry-After"], "2")
                self.assertIn("busy", json.loads(body)["error"])
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertEqual(outcomes, [200])
        self.assertEqual(self.request("POST", route, b"{}")[0], 200)

    def test_server_rejects_non_loopback_bind(self):
        for host in ["0.0.0.0", "192.168.1.2", "example.com"]:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    make_server(self.store, FakeModel(), host=host)


if __name__ == "__main__":
    unittest.main()
