"""Command-line entry point for PDF Desk."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .core import DocumentError, Store
from .model import ModelError, Ollama
from .search import search_document
from .service import Service


def _port(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Port must be an integer between 0 and 65535.") from error
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 0 and 65535.")
    return number


def parser():
    result = argparse.ArgumentParser(description="Local PDF summaries and evidence-grounded answers.")
    result.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PDFDESK_DATA_DIR", ".pdfdesk")))
    result.add_argument("--model", default=os.environ.get("PDFDESK_MODEL", "qwen3:4b-instruct"))
    result.add_argument("--ollama-url", default=os.environ.get("PDFDESK_OLLAMA_URL", "http://127.0.0.1:11434"))
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local browser reading desk.")
    serve.add_argument("--port", type=_port, default=8765)
    upload = commands.add_parser("import", help="Copy and index a text PDF.")
    upload.add_argument("path", type=Path)
    commands.add_parser("list", help="List imported document metadata.")
    for name in ("show", "summarize", "delete"):
        command = commands.add_parser(name)
        command.add_argument("id")
    ask = commands.add_parser("ask")
    ask.add_argument("id")
    ask.add_argument("question")
    search = commands.add_parser("search", help="Search source text without a model.")
    search.add_argument("id")
    search.add_argument("query")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        store = Store(args.data_dir)
        if args.command == "import":
            # Reject excessive inputs before reading all bytes into memory.
            from .core import MAX_BYTES
            if args.path.stat().st_size > MAX_BYTES:
                raise DocumentError("PDF exceeds the 10 MiB upload limit.")
            result = store.import_pdf(args.path.read_bytes(), args.path.name)
        elif args.command == "list":
            result = store.list_documents()
        elif args.command == "show":
            result = store.get_document(args.id)
        elif args.command == "search":
            result = search_document(store, args.id, args.query)
        elif args.command == "delete":
            store.delete_document(args.id)
            result = {"deleted": args.id}
        else:
            model = Ollama(base_url=args.ollama_url, model=args.model)
            if args.command == "serve":
                from .server import make_server
                server = make_server(store, model, port=args.port)
                print(f"PDF Desk: http://127.0.0.1:{server.server_address[1]}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                return 0
            service = Service(store, model)
            result = (service.summarize(args.id) if args.command == "summarize"
                      else service.answer(args.id, args.question))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyError:
        print("Document not found.", file=sys.stderr)
    except (DocumentError, ModelError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
    return 1
