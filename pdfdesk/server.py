"""Loopback-only HTTP interface for the local PDF reading desk."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import re
import socket
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from .core import DocumentError, MAX_BYTES
from .model import ModelError
from .search import search_document
from .service import Service


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, store, model):
        self.store = store
        self.model = model
        self.service = Service(store, model)
        self.inference_slot = threading.BoundedSemaphore(1)
        super().__init__(address, DeskHandler)
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


def make_server(store, model, host="127.0.0.1", port=0):
    """Create a server restricted to IPv4 loopback; caller owns its lifecycle."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("PDF Desk may bind only to 127.0.0.1 or localhost.")
    return DeskServer(("127.0.0.1", port), store, model)


class DeskHandler(BaseHTTPRequestHandler):
    server_version = "PDFDesk/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # URLs may contain private questions or document names; stay quiet.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def _guard(self, write=False):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._error(403, "Use the local PDF Desk address with its exact port.")
            return False
        if write:
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] != f"http://{hosts[0]}"):
                self._error(403, "Requests from another origin are not allowed.")
                return False
        return True

    def _body(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported; provide Content-Length.")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise ValueError("A valid Content-Length is required.")
        length = int(values[0])
        if length > maximum:
            self.close_connection = True
            self._error(413, "Request exceeds the size limit.")
            return None
        try:
            body = self.rfile.read(length)
        except (socket.timeout, TimeoutError) as error:
            raise ValueError("Request body timed out.") from error
        if len(body) != length:
            raise ValueError("Request body was incomplete.")
        return body

    def _json_body(self):
        body = self._body(32 * 1024)
        if body is None:
            return None
        try:
            value = json.loads(body)
        except (UnicodeError, ValueError) as error:
            raise ValueError("Request body must be valid JSON.") from error
        if not isinstance(value, dict):
            raise ValueError("Request JSON must be an object.")
        return value

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        if not self._guard(write=method != "GET"):
            return
        try:
            self._route(method)
        except KeyError:
            self._error(404, "Document not found.")
        except (DocumentError, ValueError) as error:
            self._error(400, str(error))
        except ModelError as error:
            self._error(502, str(error))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            # Never serialize arbitrary exceptions containing local paths/data.
            self._error(500, "The local request failed. Check storage access and try again.")

    def _route(self, method):
        path = urlsplit(self.path).path
        if method == "GET" and path in {"/", "/index.html", "/app.js", "/style.css"}:
            name = "index.html" if path == "/" else path[1:]
            mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
            self._send(200, files("pdfdesk").joinpath("web", name).read_bytes(), mime)
            return
        if method == "GET" and path == "/api/health":
            self._send(200, {"status": "ok", "model": self.server.model.health()})
            return
        if path == "/api/documents":
            if method == "GET":
                self._send(200, self.server.store.list_documents())
                return
            if method == "POST":
                body = self._body(MAX_BYTES)
                if body is None:
                    return
                filename = unquote(self.headers.get("X-Filename", "document.pdf"))
                self._send(201, self.server.store.import_pdf(body, filename))
                return
        match = re.fullmatch(r"/api/documents/([a-f0-9]{32})(?:/(file|passages|search|summary|answer))?", path)
        if match:
            document_id, action = match.groups()
            if method == "GET" and action is None:
                self._send(200, self.server.store.get_document(document_id))
                return
            if method == "DELETE" and action is None:
                self.server.store.delete_document(document_id)
                self._send(200, {"deleted": document_id})
                return
            if method == "GET" and action == "file":
                data = self.server.store.get_pdf_path(document_id).read_bytes()
                self._send(200, data, "application/pdf", {"Content-Disposition": 'inline; filename="document.pdf"'})
                return
            if method == "GET" and action == "passages":
                self._send(200, self.server.store.passages(document_id))
                return
            if method == "GET" and action == "search":
                queries = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=10).get("q", [])
                if len(queries) != 1:
                    raise ValueError("Provide exactly one search query in q.")
                self._send(200, search_document(self.server.store, document_id, queries[0]))
                return
            if method == "POST" and action in {"summary", "answer"}:
                value = self._json_body()
                if value is None:
                    return
                self.server.store.get_document(document_id)
                question = value.get("question")
                if action == "answer" and (not isinstance(question, str) or not question.strip() or len(question) > 2000):
                    raise ValueError("Provide a question between 1 and 2000 characters.")
                if not self.server.inference_slot.acquire(blocking=False):
                    self._send(429, {"error": "The model is busy. Wait for the current request to finish."}, extra={"Retry-After": "2"})
                    return
                try:
                    result = (self.server.service.summarize(document_id) if action == "summary"
                              else self.server.service.answer(document_id, question.strip()))
                    self._send(200, result)
                finally:
                    self.server.inference_slot.release()
                return
        self._error(404, "Route not found.")
