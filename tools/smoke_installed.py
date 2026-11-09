#!/usr/bin/env python3
"""Check a wheel installation, its CLI, persistence, and packaged web assets.

Run with a fresh environment's Python, using -I to exclude the source checkout:
    /tmp/pdfdesk-install/bin/python -I tools/smoke_installed.py
No model inference or model downloads are needed for this check.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.request import ProxyHandler, build_opener

import pdfdesk
from pdfdesk.core import Store
from pdfdesk.model import Ollama
from pdfdesk.server import make_server


def main():
    checkout = Path(__file__).resolve().parents[1]
    installed = Path(pdfdesk.__file__).resolve()
    if installed.is_relative_to(checkout / "pdfdesk"):
        raise RuntimeError("Use a fresh wheel installation, not the source checkout.")
    executable = Path(sys.executable).with_name("pdfdesk")
    checks = []
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    source = checkout / "examples" / "research-note.pdf"

    with tempfile.TemporaryDirectory(prefix="pdfdesk-installed-") as temporary:
        root = Path(temporary)
        data = root / "library"

        def cli(*arguments):
            result = subprocess.run(
                [str(executable), "--data-dir", str(data), *arguments],
                cwd=root, env=environment, capture_output=True, text=True,
                check=True, timeout=30,
            )
            return json.loads(result.stdout)

        document = cli("import", str(source))
        document_id = document["id"]
        assert document["page_count"] == 3
        assert cli("list")[0]["id"] == document_id
        assert cli("show", document_id)["pages"] == document["pages"]
        checks.append("installed CLI imports, lists, and reopens all three physical pages")
        search = cli("search", document_id, "14")
        assert search["results"] and all(p["page"] == 2 for p in search["results"])
        assert all(p["url"] == f"/api/documents/{document_id}/file#page=2" for p in search["results"])
        checks.append("installed CLI searches exact source passages with physical-page links")

        server = make_server(Store(data), Ollama())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = build_opener(ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path):
            with opener.open(base + path, timeout=10) as response:
                assert response.status == 200
                return response.read()

        try:
            assert b"PDF Desk" in get("/")
            assert b"selectDocument" in get("/app.js")
            assert b".workspace" in get("/style.css")
            checks.append("wheel includes HTML, JavaScript, and CSS served by the installed app")
            assert json.loads(get("/api/documents"))[0]["id"] == document_id
            assert get(f"/api/documents/{document_id}/file") == source.read_bytes()
            checks.append("installed HTTP app reuses the persisted library and exact original PDF")
            assert json.loads(get(f"/api/documents/{document_id}/search?q=14")) == search
            assert json.loads(get(f"/api/documents/{document_id}/search?q=unicorn"))["results"] == []
            checks.append("installed HTTP search agrees with the CLI and handles no matches")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

        assert cli("delete", document_id)["deleted"] == document_id
        assert cli("list") == []
        assert not list((data / "originals").glob("*.pdf"))
        checks.append("installed CLI deletes the indexed document and its original copy")

    print(json.dumps({"passed": True, "scope": "Fresh wheel installation; no model inference", "checks": checks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
