# PDF Desk

A local PDF reading desk: keep the original PDF and extracted page text together, retrieve passages within one selected document, and inspect the supporting page for each result.

## Core milestone

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
```

The extraction/storage core uses pypdf and SQLite. Import a PDF with `Store(Path('.pdfdesk')).import_pdf(data, filename)`, then use `get_document`, `passages`, and `search` on its returned opaque ID. Each passage retains its 1-based PDF page number. Search stays within the selected document and works after restart. Original files are copied into the data directory; filenames never become storage paths.

Supported input: digitally authored, unencrypted text PDFs up to 10 MiB and 80 pages. Image-only scans require OCR and are rejected. Mixed documents retain empty page numbers and report warnings. Complex tables, reading order, and mathematical notation can extract imperfectly; compare extracted text with the original.

The next checkpoint adds a local model, browser UI and CLI. Model readiness alone is not live validation; final evidence will include actual generated summaries, page-cited answers and unsupported-question behavior.
