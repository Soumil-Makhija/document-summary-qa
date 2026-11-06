"""PDF extraction, persistent local storage, and page-grounded retrieval.

Extracted text and original files stay under the configured store directory.
Each operation opens its own SQLite connection so a Store can be shared by a
threaded web server. Passage text is always a substring of normalized page text.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from pypdf import PdfReader


MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 80
MAX_TEXT_CHARS = 2_000_000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
_ID = re.compile(r"^[a-f0-9]{32}$")
_SPACE = re.compile(r"\s+")
_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_STOP = frozenset(
    "a an and are as at be by can could did do does for from had has have how i "
    "in into is it its me of on or our please should that the their them there "
    "these they this to was were what when where which who why will with would "
    "you your document pdf tell about according summarize summary".split()
)


class DocumentError(ValueError):
    """A PDF could not be safely imported or does not contain usable text."""


def _normalize(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _tokens(text: str) -> list[str]:
    return [word.casefold() for word in _WORDS.findall(text)]


def _chunks(text: str):
    """Yield overlapping page-local chunks without rewriting their contents."""
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            # Prefer a word boundary, but keep progress for unbroken strings.
            boundary = text.rfind(" ", start + CHUNK_SIZE // 2, end + 1)
            if boundary != -1:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            yield piece
        if end == len(text):
            break
        next_start = max(start + 1, end - CHUNK_OVERLAP)
        boundary = text.find(" ", next_start, min(end, next_start + 80))
        start = boundary + 1 if boundary != -1 else next_start


class Store:
    """Persistent workspace for validated PDFs and extracted page passages."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.originals = self.root / "originals"
        self.originals.mkdir(exist_ok=True)
        self.db_path = self.root / "documents.sqlite3"
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    warnings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (document_id, page)
                );
                CREATE TABLE IF NOT EXISTS passages (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (document_id, id)
                );
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["warnings"] = json.loads(result["warnings"])
        return result

    @staticmethod
    def _require(connection, document_id):
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise KeyError(document_id)
        row = connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise KeyError(document_id)
        return row

    def import_pdf(self, data: bytes, filename: str) -> dict:
        """Validate and extract a PDF before committing its original and index."""
        if not isinstance(data, bytes) or not data:
            raise DocumentError("Choose a nonempty PDF file.")
        if len(data) > MAX_BYTES:
            raise DocumentError("PDF exceeds the 10 MiB upload limit.")
        if b"%PDF-" not in data[:1024]:
            raise DocumentError("This file is not a valid PDF.")
        pages = []
        warnings = []
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise DocumentError("Encrypted PDFs are not supported. Export an unencrypted copy.")
            count = len(reader.pages)
            if not count:
                raise DocumentError("PDF contains no pages.")
            if count > MAX_PAGES:
                raise DocumentError("PDF exceeds the 80-page limit.")
            total_chars = 0
            for number, page in enumerate(reader.pages, start=1):
                try:
                    text = _normalize(page.extract_text() or "")
                except Exception as error:
                    raise DocumentError(
                        f"Could not extract text from page {number}. Try exporting the PDF again."
                    ) from error
                total_chars += len(text)
                if total_chars > MAX_TEXT_CHARS:
                    raise DocumentError("PDF contains too much extracted text (2 million character limit).")
                if not text:
                    warnings.append(
                        f"Page {number} has no extractable text; scanned images require OCR."
                    )
                pages.append({"page": number, "text": text})
        except DocumentError:
            raise
        except Exception as error:
            raise DocumentError("This PDF is malformed or could not be read.") from error
        if not any(page["text"] for page in pages):
            raise DocumentError("PDF has no extractable text. Scanned or image-only PDFs require OCR.")

        document_id = uuid.uuid4().hex
        # Uploaded names are display metadata only; originals use opaque IDs.
        name = str(filename or "document.pdf").replace("\\", "/").split("/")[-1]
        name = "".join(char for char in name if char.isprintable()).strip()[:200]
        if name in {"", ".", ".."}:
            name = "document.pdf"
        metadata = {
            "id": document_id,
            "name": name,
            "page_count": len(pages),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": warnings,
        }
        destination = self.originals / f"{document_id}.pdf"
        pending = self.originals / f".{document_id}.tmp"
        try:
            with pending.open("xb") as stream:
                stream.write(data)
            pending.replace(destination)
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    tuple(metadata[key] for key in
                          ("id", "name", "page_count", "size_bytes", "sha256", "created_at"))
                    + (json.dumps(warnings),),
                )
                connection.executemany(
                    "INSERT INTO pages VALUES (?, ?, ?)",
                    [(document_id, page["page"], page["text"]) for page in pages],
                )
                connection.executemany(
                    "INSERT INTO passages VALUES (?, ?, ?, ?)",
                    [(document_id, f"p{page['page']}-s{number}", page["page"], text)
                     for page in pages
                     for number, text in enumerate(_chunks(page["text"]), start=1)],
                )
        except Exception:
            pending.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        return {**metadata, "pages": pages}

    def list_documents(self) -> list[dict]:
        with self._connection() as connection:
            return [self._metadata(row) for row in connection.execute(
                "SELECT * FROM documents ORDER BY created_at DESC, id"
            )]

    def get_document(self, document_id: str) -> dict:
        with self._connection() as connection:
            metadata = self._metadata(self._require(connection, document_id))
            metadata["pages"] = [dict(row) for row in connection.execute(
                "SELECT page, text FROM pages WHERE document_id = ? ORDER BY page", (document_id,)
            )]
            return metadata

    def get_pdf_path(self, document_id: str) -> Path:
        with self._connection() as connection:
            self._require(connection, document_id)
        return self.originals / f"{document_id}.pdf"

    def delete_document(self, document_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require(connection, document_id)
            # Failure to remove the original rolls back the database deletion.
            (self.originals / f"{document_id}.pdf").unlink(missing_ok=True)
            connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    def passages(self, document_id: str) -> list[dict]:
        with self._connection() as connection:
            self._require(connection, document_id)
            return [dict(row) for row in connection.execute(
                "SELECT id, page, text FROM passages WHERE document_id = ? ORDER BY page, rowid",
                (document_id,),
            )]

    def search(self, document_id: str, question: str, limit: int = 8) -> list[dict]:
        """Rank document-local passages using BM25 plus exact adjacent phrases."""
        passages = self.passages(document_id)
        question_tokens = _tokens(str(question))
        terms = list(dict.fromkeys(word for word in question_tokens if word not in _STOP))
        if not terms or limit <= 0:
            return []
        frequencies = [Counter(_tokens(passage["text"])) for passage in passages]
        lengths = [sum(frequency.values()) for frequency in frequencies]
        average_length = sum(lengths) / max(len(lengths), 1)
        document_frequency = {
            term: sum(term in frequency for frequency in frequencies) for term in terms
        }
        phrases = [" ".join(question_tokens[index:index + 2])
                   for index in range(len(question_tokens) - 1)
                   if question_tokens[index] not in _STOP
                   and question_tokens[index + 1] not in _STOP]
        ranked = []
        for passage, frequency, length in zip(passages, frequencies, lengths):
            score = 0.0
            matches = 0
            for term in terms:
                count = frequency[term]
                if not count:
                    continue
                matches += 1
                inverse_frequency = math.log(
                    1 + (len(passages) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                )
                score += inverse_frequency * count * 2.2 / (
                    count + 1.2 * (0.25 + 0.75 * length / max(average_length, 1))
                )
            if score:
                normalized_words = " ".join(_tokens(passage["text"]))
                score += 0.35 * sum(phrase in normalized_words for phrase in phrases)
                score *= 0.75 + 0.25 * matches / len(terms)
                ranked.append({**passage, "score": round(score, 6)})
        ranked.sort(key=lambda passage: (-passage["score"], passage["page"], passage["id"]))
        return ranked[:min(int(limit), 100)]
