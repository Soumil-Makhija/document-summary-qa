"""Explicit scripted model outputs test provenance guards, not model quality."""

from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest

from reportlab.pdfgen import canvas

from pdfdesk.core import DocumentError, Store
from pdfdesk.model import ModelError
from pdfdesk.service import ABSTENTION, MAX_SUMMARY_PASSAGES, Service
from tools.evaluate_live import answer_checks, citation_checks, summary_checks, write_report


def pdf_bytes(pages):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, invariant=1)
    for text in pages:
        pdf.drawString(45, 740, text)
        pdf.showPage()
    pdf.save()
    return stream.getvalue()


class ScriptedModel:
    """An explicitly fake model; its records are never live evaluation evidence."""

    model = "scripted-unit-test-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, task, schema, instruction, payload):
        self.calls.append({"task": task, "schema": schema, "instruction": instruction,
                           "payload": deepcopy(payload)})
        if not self.responses:
            raise AssertionError(f"Unexpected fake model call: {task}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(task, payload)
        return deepcopy(response)


APPROVED = {"assessment": "The supplied fixture evidence supports the requested facts.", "supported": True}
REJECTED = {"assessment": "A requested fact or qualifier is not established.", "supported": False}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.document = self.store.import_pdf(pdf_bytes([
            "Annual memberships are non-refundable. Annual memberships include workroom access.",
            "To export a design, open Designs, choose Download, and select SVG.",
            "Standard approvals usually take 2 to 4 business days.",
        ]), "policies.pdf")
        self.document_id = self.document["id"]

    def tearDown(self):
        self.directory.cleanup()

    def evidence(self, page=1, quote=None):
        return {"source_id": f"p{page}-s1", "quote": quote or self.document["pages"][page - 1]["text"]}

    def plan(self, question):
        return {"requests": [question.strip()]}

    def alias_evidence(self, question, page=1, quote=None):
        retrieved = self.store.search(self.document_id, question, limit=12)
        if not retrieved:
            retrieved = self.store.passages(self.document_id)
        source = next((index, passage) for index, passage in enumerate(retrieved, 1) if passage["page"] == page)
        return {"source_id": f"S{source[0]}", "quote": quote or source[1]["text"]}

    def selected(self, question, *pages):
        return {"P1": {"evidence": [self.alias_evidence(question, page) for page in pages]}}

    def test_known_compound_answer_resolves_both_actual_pages(self):
        question = "How do I export a design, and are annual memberships refundable?"
        model = ScriptedModel(self.plan(question), self.selected(question, 2, 1, 1), APPROVED)
        answer = Service(self.store, model).answer(self.document_id, "  " + question + "  ")
        self.assertTrue(answer["supported"])
        self.assertEqual([source["page"] for source in answer["evidence"]], [2, 1])
        self.assertEqual(answer["answer"], self.document["pages"][1]["text"] + "\n\n" + self.document["pages"][0]["text"])
        self.assertEqual(answer["question"], "How do I export a design, and are annual memberships refundable?")
        self.assertEqual([call["task"] for call in model.calls], ["plan", "answer", "review_answer"])
        self.assertEqual(model.calls[0]["payload"], {"question": question})
        self.assertNotIn("sources", model.calls[-1]["payload"])
        for source in answer["evidence"]:
            self.assertEqual(source["url"], f"/api/documents/{self.document_id}/file#page={source['page']}")
            self.assertIn(source["quote"], self.document["pages"][source["page"] - 1]["text"])

    def test_explicit_negative_policy_is_a_supported_answer(self):
        question = "Are annual memberships refundable?"
        model = ScriptedModel(self.plan(question), self.selected(question, 1), APPROVED)
        answer = Service(self.store, model).answer(self.document_id, question)
        self.assertTrue(answer["supported"])
        self.assertIn("non-refundable", answer["answer"])

    def test_unknown_compound_part_abstains_without_a_review_call(self):
        question = "How do I export a design, and what is the badge fee?"
        model = ScriptedModel(self.plan(question), {"P1": {"evidence": []}})
        answer = Service(self.store, model).answer(self.document_id, question)
        self.assertFalse(answer["supported"])
        self.assertEqual(answer["answer"], ABSTENTION)
        self.assertEqual(answer["evidence"], [])
        self.assertNotIn("review_answer", [call["task"] for call in model.calls])

    def test_grounding_review_rejects_incomplete_compound_or_wrong_qualifier(self):
        for question, page in [("How do I export a design, and what is the badge fee?", 2),
                               ("What guaranteed deadline applies to standard approvals?", 3),
                               ("Are monthly memberships refundable?", 1)]:
            with self.subTest(question=question):
                model = ScriptedModel(self.plan(question), self.selected(question, page), REJECTED)
                answer = Service(self.store, model).answer(self.document_id, question)
                self.assertFalse(answer["supported"])
                self.assertEqual(answer["evidence"], [])
                self.assertEqual(answer["answer"], ABSTENTION)

    def test_forged_quote_wrong_passage_and_wrong_document_are_rejected(self):
        other = self.store.import_pdf(pdf_bytes(["Badge fee is 999 dollars."]), "other.pdf")
        question = "What is the annual membership policy and how do I export a design?"
        for evidence in [
            self.alias_evidence(question, 1, "Annual memberships are refundable."),
            self.alias_evidence(question, 2, self.document["pages"][0]["text"]),
            {"source_id": "S999", "quote": self.document["pages"][0]["text"]},
            self.alias_evidence(question, 1, self.store.get_document(other["id"])["pages"][0]["text"]),
        ]:
            with self.subTest(evidence=evidence):
                model = ScriptedModel(self.plan(question), {"P1": {"evidence": [evidence]}})
                with self.assertRaises(ModelError):
                    Service(self.store, model).answer(self.document_id, question)
                self.assertNotIn("review_answer", [call["task"] for call in model.calls])

    def test_wrong_answer_evidence_and_review_schemas_fail_explicitly(self):
        question = "Are annual memberships refundable?"
        outputs = [
            {}, {"P1": {}}, {"P1": {"evidence": [], "answer": "invented"}},
            {"P1": {"evidence": "S1"}}, {"P1": {"evidence": [None]}},
            {"P1": {"evidence": [{"source_id": "S1"}]}},
            {"P1": {"evidence": [{**self.alias_evidence(question), "page": 999}]}},
            {"P1": {"evidence": [self.alias_evidence(question)] * 9}},
        ]
        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaises(ModelError):
                    Service(self.store, ScriptedModel(self.plan(question), output)).answer(self.document_id, question)
        for review in [{}, {"assessment": "ok", "supported": "true"},
                       {"assessment": " ", "supported": True}, {**APPROVED, "extra": 1}]:
            with self.subTest(review=review):
                with self.assertRaises(ModelError):
                    Service(self.store, ScriptedModel(self.plan(question), self.selected(question, 1), review)).answer(self.document_id, question)

    def test_invalid_question_plans_are_rejected_before_source_selection(self):
        for plan in [{}, {"requests": []}, {"requests": "question"}, {"requests": [""]},
                     {"requests": ["x" * 2001]}, {"requests": ["same", "same"]},
                     {"requests": [str(number) for number in range(9)]},
                     {"requests": ["question"], "extra": True}]:
            with self.subTest(plan=plan):
                model = ScriptedModel(plan)
                with self.assertRaisesRegex(ModelError, "question plan"):
                    Service(self.store, model).answer(self.document_id, "Are annual memberships refundable?")
                self.assertEqual([call["task"] for call in model.calls], ["plan"])

    def test_changed_plan_must_pass_coverage_review_without_source_context(self):
        question = "How do I export a design, and are annual memberships refundable?"
        model = ScriptedModel({"requests": ["How do I export a design?"]}, REJECTED)
        answer = Service(self.store, model).answer(self.document_id, question)
        self.assertFalse(answer["supported"])
        self.assertEqual(answer["evidence"], [])
        self.assertEqual([call["task"] for call in model.calls], ["plan", "review_plan"])
        self.assertEqual(model.calls[1]["payload"], {"question": question, "requests": ["How do I export a design?"]})

    def test_split_plan_answers_all_parts_and_never_returns_a_partial_answer(self):
        question = "How do I export a design, and are annual memberships refundable?"
        plan = {"requests": ["How do I export a design?", "Are annual memberships refundable?"]}
        for final_selection, expected_supported in [(self.selected(question, 1), True), ({"P1": {"evidence": []}}, False)]:
            with self.subTest(expected_supported=expected_supported):
                responses = [plan, APPROVED, self.selected(question, 2), APPROVED, final_selection]
                if expected_supported:
                    responses.append(APPROVED)
                model = ScriptedModel(*responses)
                answer = Service(self.store, model).answer(self.document_id, question)
                self.assertIs(answer["supported"], expected_supported)
                if expected_supported:
                    self.assertEqual({source["page"] for source in answer["evidence"]}, {1, 2})
                else:
                    self.assertEqual(answer["answer"], ABSTENTION)
                    self.assertEqual(answer["evidence"], [])

    def test_invalid_questions_never_call_the_model(self):
        model = ScriptedModel()
        for question in [None, 17, "", " \n\t", "x" * 2001]:
            with self.subTest(question=question):
                with self.assertRaises(DocumentError):
                    Service(self.store, model).answer(self.document_id, question)
        self.assertEqual(model.calls, [])

    def test_empty_retrieval_abstains_without_model_call(self):
        # Thirteen real pages selects the larger-document lexical retrieval path.
        large = self.store.import_pdf(pdf_bytes(["A workshop policy."] * 13), "large.pdf")
        model = ScriptedModel()
        answer = Service(self.store, model).answer(large["id"], "unicorns meteorites")
        self.assertFalse(answer["supported"])
        self.assertEqual(answer["retrieved"], [])
        self.assertEqual(model.calls, [])

    def summary(self, page=1):
        text = self.document["pages"][page - 1]["text"]
        return {"summary": text, "key_points": [
            {"text": text, "evidence": [self.evidence(page)]}]}

    def test_summary_covers_every_page_and_resolves_every_key_point(self):
        responses = []
        for page in range(1, 4):
            responses.extend([self.summary(page), APPROVED, APPROVED])
        model = ScriptedModel(*responses)
        result = Service(self.store, model).summarize(self.document_id)
        self.assertEqual([page["page"] for page in result["pages"]], [1, 2, 3])
        self.assertEqual([call["task"] for call in model.calls], ["summarize", "review_point", "review_summary"] * 3)
        for call in model.calls:
            if call["task"] in {"review_point", "review_summary"}:
                self.assertNotIn("sources", call["payload"])
        for page in result["pages"]:
            self.assertEqual(page["key_points"][0]["evidence"][0]["page"], page["page"])
            self.assertEqual(page["key_points"][0]["evidence"][0]["quote"], self.document["pages"][page["page"] - 1]["text"])
        self.assertEqual(result["kind"], "generated_summary")
        self.assertIn("can be wrong", result["notice"])

    def test_summary_rejected_by_grounding_review_is_not_returned(self):
        model = ScriptedModel(self.summary(), APPROVED, REJECTED)
        with self.assertRaisesRegex(ModelError, "page 1.*grounding review"):
            Service(self.store, model).summarize(self.document_id)
        self.assertEqual([call["task"] for call in model.calls], ["summarize", "review_point", "review_summary"])

    def test_related_quote_does_not_establish_a_different_key_point(self):
        generated = {"summary": "Annual memberships are non-refundable.", "key_points": [
            {"text": "Annual memberships are non-refundable.", "evidence": [
                self.evidence(1, "Annual memberships include workroom access.")]}]}
        model = ScriptedModel(generated, REJECTED)
        with self.assertRaisesRegex(ModelError, "key point.*grounding review"):
            Service(self.store, model).summarize(self.document_id)
        review = model.calls[-1]
        self.assertEqual(review["task"], "review_point")
        self.assertEqual(review["payload"]["evidence"][0]["quote"], "Annual memberships include workroom access.")
        self.assertNotIn("sources", review["payload"])

    def test_numeric_guard_rejects_999_even_before_model_review(self):
        generated = self.summary()
        generated["key_points"][0]["text"] = "Annual memberships cost 999 dollars."
        model = ScriptedModel(generated)
        with self.assertRaisesRegex(ModelError, "key point.*grounding review"):
            Service(self.store, model).summarize(self.document_id)
        self.assertEqual([call["task"] for call in model.calls], ["summarize"])

    def test_summary_numeric_guard_uses_only_actual_key_point_quotes(self):
        generated = self.summary()
        generated["summary"] = "Annual memberships cost 999 dollars."
        model = ScriptedModel(generated, APPROVED)
        with self.assertRaisesRegex(ModelError, "summary.*grounding review"):
            Service(self.store, model).summarize(self.document_id)
        self.assertEqual([call["task"] for call in model.calls], ["summarize", "review_point"])

    def test_summary_wrong_quote_page_and_schema_are_rejected(self):
        forged = self.summary()
        forged["key_points"][0]["evidence"] = [self.evidence(2)]
        for output in [{}, {"summary": "ok", "key_points": []},
                       {"summary": "ok", "key_points": [{"text": "ok", "evidence": [], "extra": 1}]},
                       forged]:
            with self.subTest(output=output):
                with self.assertRaises(ModelError):
                    Service(self.store, ScriptedModel(output)).summarize(self.document_id)

    def test_summary_processes_all_page_local_batches(self):
        # One long page must be summarized in more than one five-passage batch.
        long = self.store.import_pdf(pdf_bytes([" ".join(f"fact{n}" for n in range(1200))]), "long.pdf")
        passages = self.store.passages(long["id"])
        self.assertGreater(len(passages), 5)

        def generated(task, payload):
            self.assertEqual(task, "summarize")
            self.assertLessEqual(len(payload["sources"]), 5)
            source = payload["sources"][0]
            return {"summary": "Synthetic batch summary.", "key_points": [
                {"text": "Synthetic batch point.", "evidence": [{"source_id": source["id"], "quote": source["text"][:30]}]}]}

        count = (len(passages) + 4) // 5
        model = ScriptedModel(*[response for _ in range(count) for response in (generated, APPROVED, APPROVED)])
        result = Service(self.store, model).summarize(long["id"])
        inputs = [source["id"] for call in model.calls if call["task"] == "summarize" for source in call["payload"]["sources"]]
        self.assertEqual(inputs, [passage["id"] for passage in passages])
        self.assertEqual(len(result["pages"][0]["key_points"]), count)

    def test_too_long_summary_rejects_before_model_call(self):
        # The store interface supplies a supported extracted document plus a
        # deliberately oversized passage list; no generated output is invented.
        class OversizedStore:
            def get_document(inner, document_id):
                return self.document

            def passages(inner, document_id):
                return [{}] * (MAX_SUMMARY_PASSAGES + 1)

        model = ScriptedModel()
        with self.assertRaisesRegex(DocumentError, "160 extracted passages"):
            Service(OversizedStore(), model).summarize(self.document_id)
        self.assertEqual(model.calls, [])

    def test_model_failures_propagate_without_fallback_or_partial_answer(self):
        question = "Are annual memberships refundable?"
        for operation, responses in [
            ("answer", [ModelError("Local model unavailable")]),
            ("answer", [self.plan(question), ModelError("Selection unavailable")]),
            ("answer", [self.plan(question), self.selected(question, 1), ModelError("Review unavailable")]),
            ("summarize", [ModelError("Local model unavailable")]),
            ("summarize", [self.summary(), ModelError("Review unavailable")]),
            ("summarize", [self.summary(), APPROVED, ModelError("Summary review unavailable")]),
        ]:
            with self.subTest(operation=operation, responses=responses):
                service = Service(self.store, ScriptedModel(*responses))
                with self.assertRaises(ModelError):
                    if operation == "answer":
                        service.answer(self.document_id, question)
                    else:
                        service.summarize(self.document_id)


class EvaluationChecksTests(unittest.TestCase):
    """Mechanical-check regressions only; these are not live-model results."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "store")
        self.document = self.store.import_pdf(pdf_bytes([
            "The access code is rotated every 23 days.", "Annual memberships are non-refundable.",
        ]), "fixture.pdf")
        self.document_id = self.document["id"]
        self.citation = {"passage_id": "p1-s1", "page": 1,
                         "quote": self.document["pages"][0]["text"],
                         "url": f"/api/documents/{self.document_id}/file#page=1"}

    def tearDown(self):
        self.directory.cleanup()

    def test_wrong_document_page_quote_or_original_link_fails_resolution(self):
        self.assertTrue(all(check["passed"] for check in citation_checks([self.citation], self.document_id, self.store)))
        for changed in [{"page": 2}, {"quote": "rotated every 999 days"},
                        {"passage_id": "p99-s1"}, {"url": "/api/documents/other/file#page=1"}]:
            with self.subTest(changed=changed):
                checks = citation_checks([{**self.citation, **changed}], self.document_id, self.store)
                self.assertFalse(all(check["passed"] for check in checks))

    def test_required_pages_and_forbidden_substrings_detect_wrong_answers(self):
        actual = {"document_id": self.document_id, "supported": True,
                  "answer": self.citation["quote"], "evidence": [self.citation]}
        expected = {"supported": True, "required_pages": [1], "required_terms": ["23", "days"], "forbidden_terms": ["999"]}
        self.assertTrue(all(check["passed"] for check in answer_checks(actual, expected, self.document_id, self.store)))
        for bad in [{**expected, "required_pages": [1, 2]}, {**expected, "forbidden_terms": ["23"]}]:
            self.assertFalse(all(check["passed"] for check in answer_checks(actual, bad, self.document_id, self.store)))

    def test_abstention_must_not_include_claimed_support(self):
        expected = {"supported": False, "required_pages": [], "required_terms": [], "forbidden_terms": []}
        actual = {"document_id": self.document_id, "supported": False, "answer": ABSTENTION, "evidence": [self.citation]}
        self.assertFalse(all(check["passed"] for check in answer_checks(actual, expected, self.document_id, self.store)))
        actual["evidence"] = []
        self.assertTrue(all(check["passed"] for check in answer_checks(actual, expected, self.document_id, self.store)))

    def test_summary_cannot_silently_drop_a_later_page(self):
        actual = {"pages": [{"page": 1, "summary": "Rotation policy.", "key_points": [
            {"text": "Codes rotate every 23 days.", "evidence": [self.citation]}]}]}
        checks = summary_checks(actual, self.document_id, self.store, {"required_pages": [1, 2]})
        self.assertFalse(next(check["passed"] for check in checks if check["name"] == "all-extractable-pages-covered"))

    def test_atomic_report_replaces_complete_json_without_temporary_leftovers(self):
        import json

        output = self.root / "reports" / "live.json"
        write_report(output, {"status": "running", "cases": []})
        write_report(output, {"status": "failed", "cases": [{"actual": "genuine output is retained"}]})
        self.assertEqual(json.loads(output.read_text())["cases"][0]["actual"], "genuine output is retained")
        self.assertEqual([path.name for path in output.parent.iterdir()], ["live.json"])


if __name__ == "__main__":
    unittest.main()
