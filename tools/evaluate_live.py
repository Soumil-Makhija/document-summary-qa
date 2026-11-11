#!/usr/bin/env python3
"""Run the public synthetic PDFs through the actual installed local model.

No mocks, model downloads, hosted providers, or paid calls are used. Every
completed operation is atomically checkpointed, including genuine failed model
outputs. Exit 2 means unavailable model access; 1 means failed execution or
mechanical checks; 0 means smoke checks passed and manual review is still needed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdfdesk.core import Store  # noqa: E402
from pdfdesk.model import ModelError, Ollama  # noqa: E402
from pdfdesk.service import ABSTENTION, Service  # noqa: E402


DISCLAIMER = (
    "Automated checks inspect support decisions, literal substring presence, physical-page "
    "coverage, and exact quote/citation resolution. They are smoke checks, not an accuracy "
    "score, proof of entailment, or independent factual grounding. The local model "
    "classifies and selects source sentences. Manually inspect every summary, answer, "
    "selected passage, and model decision. This small synthetic set is not an "
    "untouched holdout and says nothing about arbitrary contracts or research papers."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _check(name, passed, detail):
    return {"name": name, "passed": bool(passed), "detail": detail}


def write_report(path: Path, report: dict) -> None:
    """Publish a whole valid JSON snapshot without truncating an earlier one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def citation_checks(evidence, document_id, store, prefix="citation"):
    """Resolve each returned citation against the real selected PDF's index."""
    checks = []
    if not isinstance(evidence, list):
        return [_check(prefix + ":format", False, "Evidence must be a list.")]
    document = store.get_document(document_id)
    pages = {page["page"]: page["text"] for page in document["pages"]}
    passages = {passage["id"]: passage for passage in store.passages(document_id)}
    for index, item in enumerate(evidence):
        label = f"{prefix}:{index}"
        if not isinstance(item, dict):
            checks.append(_check(label + ":format", False, "Citation must be an object."))
            continue
        passage_id, page, quote = item.get("passage_id"), item.get("page"), item.get("quote")
        passage = passages.get(passage_id) if isinstance(passage_id, str) else None
        exact = (passage is not None and type(page) is int and passage["page"] == page
                 and isinstance(quote, str) and bool(quote.strip())
                 and quote in passage["text"] and quote in pages.get(page, ""))
        checks.append(_check(label + ":exact-page-quote", exact,
                             "Quote must be an exact substring of the indexed passage and physical PDF page in this document."))
        checks.append(_check(label + ":original-pdf-link",
                             item.get("url") == f"/api/documents/{document_id}/file#page={page}",
                             "Citation link must identify this original PDF and its physical page."))
    return checks


def answer_checks(actual, expected, document_id, store):
    checks = [
        _check("selected-document", actual.get("document_id") == document_id,
               "The response must belong to the PDF selected by the case."),
        _check("support-decision", actual.get("supported") is expected["supported"],
               f"Expected supported={expected['supported']} for every requested part."),
    ]
    answer, evidence = actual.get("answer"), actual.get("evidence")
    checks.append(_check("answer-present", isinstance(answer, str) and bool(answer.strip()),
                         "Return an answer or explicit abstention text."))
    lower = answer.lower() if isinstance(answer, str) else ""
    for term in expected["required_terms"]:
        checks.append(_check("required-substring:" + term, term.lower() in lower,
                             "Literal lowercase substring smoke check; not semantic entailment."))
    for term in expected["forbidden_terms"]:
        checks.append(_check("forbidden-substring:" + term, term.lower() not in lower,
                             "Conflicting or injected value must not appear in the returned answer."))
    evidence_pages = {item.get("page") for item in evidence if isinstance(item, dict)} if isinstance(evidence, list) else set()
    for page in expected["required_pages"]:
        checks.append(_check(f"required-physical-page:{page}", page in evidence_pages,
                             "Expected supporting physical page must be cited."))
    if expected["supported"]:
        checks.append(_check("supported-has-evidence", isinstance(evidence, list) and bool(evidence),
                             "A supported answer needs resolved quotes."))
    else:
        checks.append(_check("unsupported-has-no-evidence", evidence == [],
                             "An unsupported full-question answer must not claim supporting quotes."))
        checks.append(_check("explicit-abstention", answer == ABSTENTION,
                             "Unsupported questions use the application's explicit abstention."))
    checks.extend(citation_checks(evidence, document_id, store))
    if isinstance(evidence, list) and all(isinstance(item, dict) and isinstance(item.get("quote"), str) for item in evidence):
        if actual.get("supported"):
            checks.append(_check("answer-matches-source-extracts",
                                 answer == "\n\n".join(dict.fromkeys(item["quote"] for item in evidence)),
                                 "A source-extract answer must contain the resolved quotes verbatim."))
    return checks


def summary_checks(actual, document_id, store, expected):
    document = store.get_document(document_id)
    expected_pages = {page["page"] for page in document["pages"] if page["text"]}
    output_pages = actual.get("pages", [])
    valid_format = isinstance(output_pages, list) and all(isinstance(page, dict) for page in output_pages)
    checks = [_check("summary-pages-format", valid_format, "Summary must return page-scoped sections.")]
    if not valid_format:
        return checks
    found_pages = [page.get("page") for page in output_pages]
    checks.append(_check("all-extractable-pages-covered",
                         set(found_pages) == expected_pages and len(found_pages) == len(expected_pages),
                         "Every extractable physical page appears once, including late-page limitations."))
    for page in expected.get("required_pages", []):
        checks.append(_check(f"expected-summary-page:{page}", page in found_pages,
                             "The summary's expected substantive page must be represented."))
    for index, page in enumerate(output_pages):
        label = f"summary-page:{page.get('page')}:{index}"
        checks.append(_check(label + ":text", isinstance(page.get("summary"), str) and bool(page["summary"].strip()),
                             "The page must have generated summary text."))
        points = page.get("key_points")
        checks.append(_check(label + ":key-points", isinstance(points, list) and bool(points),
                             "The page must have cited key points."))
        if not isinstance(points, list):
            continue
        for point_number, point in enumerate(points):
            point_label = f"{label}:point:{point_number}"
            if not isinstance(point, dict):
                checks.append(_check(point_label + ":format", False, "Key point must be an object."))
                continue
            evidence = point.get("evidence")
            checks.append(_check(point_label + ":text", isinstance(point.get("text"), str) and bool(point["text"].strip()),
                                 "Generated key-point wording must be nonempty."))
            checks.append(_check(point_label + ":evidence", isinstance(evidence, list) and bool(evidence),
                                 "Every key point needs evidence."))
            checks.extend(citation_checks(evidence, document_id, store, point_label))
            checks.append(_check(point_label + ":same-physical-page",
                                 isinstance(evidence, list) and all(isinstance(item, dict) and item.get("page") == page.get("page") for item in evidence),
                                 "Page-scoped summary evidence must resolve on that same physical page."))
    return checks


def _unavailable(error):
    """Distinguish transport access failures from malformed generated content."""
    return isinstance(error, ModelError) and isinstance(error.__cause__, (URLError, HTTPException, OSError, TimeoutError))


def _record_failure(entry, error):
    entry["status"] = "failed"
    entry["error"] = {"type": type(error).__name__, "message": str(error)}
    entry["checks"] = [_check("execution", False, str(error))]


def run_evaluation(base_url, model_name, output):
    started = time.monotonic()
    report = {
        "schema_version": 1, "started_at": _now(), "updated_at": _now(), "status": "running",
        "manual_review_required": True, "manual_review_caveat": DISCLAIMER,
        "scope": "Real local model sentence selection against two public synthetic PDFs.",
        "model": {"provider": "local Ollama", "base_url": base_url, "name": model_name},
        "application_code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                    for name in ["pdfdesk/core.py", "pdfdesk/model.py", "pdfdesk/service.py", "pdfdesk/selection.py", "pdfdesk/summary.py", "tools/evaluate_live.py"]},
        "documents": [], "lifecycle_checks": [], "summaries": [], "cases": [],
    }

    def checkpoint():
        report["updated_at"] = _now()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_report(output, report)

    checkpoint()
    try:
        manifest_bytes = (ROOT / "examples" / "evaluation.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        report["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        report["manifest"] = manifest
        report["planned_case_ids"] = [case["id"] for case in manifest["cases"]]
        model = Ollama(base_url=base_url, model=model_name)
        try:
            report["health"] = model.health()
        except ModelError as error:
            report["status"] = "blocked"
            report["error"] = {"type": type(error).__name__, "message": str(error)}
            checkpoint()
            return report
        if not report["health"]["ready"]:
            report["status"] = "blocked"
            report["error"] = {"type": "ModelUnavailable", "message": f"Configured local model {model_name} is not installed; no download was attempted."}
            checkpoint()
            return report
        checkpoint()
        with tempfile.TemporaryDirectory(prefix="pdfdesk-live-") as temporary:
            root = Path(temporary) / "store"
            initial_store = Store(root)
            documents = {}
            for specification in manifest["documents"]:
                name = specification["file"]
                imported = initial_store.import_pdf((ROOT / "examples" / name).read_bytes(), name)
                documents[name] = imported
                report["documents"].append({"fixture": name, "description": specification["description"],
                                             "metadata": {key: value for key, value in imported.items() if key != "pages"},
                                             "extracted_pages": imported["pages"],
                                             "passages": initial_store.passages(imported["id"])})
                report["lifecycle_checks"].append(_check("physical-page-count:" + name,
                    imported["page_count"] == specification["pages"], "Real extraction must retain the expected number of physical PDF pages."))
                checkpoint()
            # Store opens per-operation connections. Constructing a new Store
            # exercises restart/reopen against the persisted originals and index.
            store = Store(root)
            report["lifecycle_checks"].append(_check("restart-persists-documents-and-originals",
                all(store.get_document(document["id"]) == document
                    and store.get_pdf_path(document["id"]).read_bytes() == (ROOT / "examples" / name).read_bytes()
                    for name, document in documents.items()),
                "Reopened store preserves extracted pages, metadata, and exact original PDF bytes."))
            service = Service(store, model)
            for name, document in documents.items():
                entry = {"document": name, "document_id": document["id"], "status": "running",
                         "expected": manifest["summary_expectations"][name],
                         "manual_review": {"status": "pending", "topics_and_factuality_require_human_inspection": True},
                         "checks": []}
                report["summaries"].append(entry)
                checkpoint()
                entry_started, trace_start = time.monotonic(), len(model.trace)
                try:
                    actual = service.summarize(document["id"])
                    entry["actual"] = actual
                    entry["checks"] = summary_checks(actual, document["id"], store, entry["expected"])
                    entry["status"] = "smoke_checks_passed" if all(check["passed"] for check in entry["checks"]) else "failed"
                except Exception as error:
                    _record_failure(entry, error)
                    if _unavailable(error):
                        report["status"] = "blocked"
                finally:
                    entry["model_trace"] = model.trace[trace_start:]
                    entry["elapsed_seconds"] = round(time.monotonic() - entry_started, 3)
                    checkpoint()
                    print(f"summary {name}: {entry['status']}", flush=True)
                if report["status"] == "blocked":
                    return report
            for case in manifest["cases"]:
                entry = {"id": case["id"], "document": case["document"], "question": case["question"],
                         "expected": case, "status": "running", "checks": []}
                report["cases"].append(entry)
                checkpoint()
                entry_started, trace_start = time.monotonic(), len(model.trace)
                try:
                    document_id = documents[case["document"]]["id"]
                    actual = service.answer(document_id, case["question"])
                    entry["actual"] = actual
                    entry["checks"] = answer_checks(actual, case, document_id, store)
                    entry["status"] = "smoke_checks_passed" if all(check["passed"] for check in entry["checks"]) else "failed"
                except Exception as error:
                    _record_failure(entry, error)
                    if _unavailable(error):
                        report["status"] = "blocked"
                finally:
                    entry["model_trace"] = model.trace[trace_start:]
                    entry["elapsed_seconds"] = round(time.monotonic() - entry_started, 3)
                    checkpoint()
                    print(f"case {case['id']}: {entry['status']}", flush=True)
                if report["status"] == "blocked":
                    return report
            all_checks = report["lifecycle_checks"] + [check for entry in report["summaries"] + report["cases"] for check in entry["checks"]]
            report["checks_passed"] = sum(check["passed"] for check in all_checks)
            report["checks_total"] = len(all_checks)
            report["cases_completed"] = len(report["cases"])
            report["summaries_completed"] = len(report["summaries"])
            complete = len(report["cases"]) == len(manifest["cases"]) and len(report["summaries"]) == len(manifest["documents"])
            report["status"] = "smoke_checks_passed_manual_review_required" if complete and all(check["passed"] for check in all_checks) else "failed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = {"type": "KeyboardInterrupt", "message": "Interrupted; prior checkpoints and completed genuine outputs are preserved."}
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        report["finished_at"] = _now()
        checkpoint()
    return report


def exit_code(report):
    return {"blocked": 2, "smoke_checks_passed_manual_review_required": 0}.get(report["status"], 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--output", type=Path, default=Path("evidence/live.json"))
    args = parser.parse_args(argv)
    report = run_evaluation(args.base_url, args.model, args.output)
    print(f"{report['status']}: {args.output}")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
