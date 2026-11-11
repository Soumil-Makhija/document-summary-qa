"""Service boundaries for source-only answers, independent of model accuracy."""
from pathlib import Path
import tempfile
import unittest

from pdfdesk.core import DocumentError, Store
from pdfdesk.model import ModelError
from pdfdesk.selection import sentence_catalog
from pdfdesk.service import ABSTENTION, Service
from test_core import pdf_bytes
from test_service import ScriptedModel


class AnswerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.document = self.store.import_pdf(pdf_bytes([
            "Annual memberships are non-refundable.",
            "Choose Download then SVG to export a design.",
            "Standard approvals usually take 2 to 4 business days.",
        ]), "policies.pdf")
        self.id = self.document["id"]

    def tearDown(self):
        self.temporary.cleanup()

    def selected(self, question, page):
        passages = self.store.search(self.id, question, limit=12)
        if not passages:
            passages = self.store.passages(self.id)
        catalog = sentence_catalog(passages)
        key = next(key for key, value in catalog.items() if value["page"] == page)
        return {"explanation": "The requested fact is stated.", "status": "answered",
                "sentence_ids": [key]}

    def test_compound_answer_combines_actual_pages_without_rewriting_question(self):
        question = "How do I export a design, and are annual memberships refundable?"
        model = ScriptedModel(self.selected(question, 2), self.selected(question, 1))
        answer = Service(self.store, model).answer(self.id, question)
        self.assertTrue(answer["supported"])
        self.assertEqual([item["page"] for item in answer["evidence"]], [2, 1])
        self.assertEqual([call["task"] for call in model.calls], ["select_answer"] * 2)
        self.assertEqual([call["payload"]["QUESTION"] for call in model.calls],
                         ["How do I export a design", "are annual memberships refundable?"])
        for item in answer["evidence"]:
            self.assertIn(item["quote"], self.document["pages"][item["page"] - 1]["text"])
            self.assertEqual(item["url"], f"/api/documents/{self.id}/file#page={item['page']}")
        self.assertEqual(answer["answer"], "\n\n".join(item["quote"] for item in answer["evidence"]))

    def test_negative_policy_can_be_a_supported_answer(self):
        question = "Are annual memberships refundable?"
        result = Service(self.store, ScriptedModel(self.selected(question, 1))).answer(self.id, question)
        self.assertTrue(result["supported"])
        self.assertIn("non-refundable", result["answer"])

    def test_missing_clause_returns_no_partial_supported_answer(self):
        question = "How do I export a design, and what is the badge fee?"
        model = ScriptedModel(self.selected(question, 2),
            {"explanation": "The fee is absent.", "status": "missing", "sentence_ids": []})
        result = Service(self.store, model).answer(self.id, question)
        self.assertFalse(result["supported"])
        self.assertEqual(result["answer"], ABSTENTION)
        self.assertEqual(result["evidence"], [])

    def test_guarantee_qualifier_reaches_model_unchanged(self):
        question = "What guaranteed deadline applies to standard approvals?"
        model = ScriptedModel({"explanation": "Only usual times appear.", "status": "missing", "sentence_ids": []})
        result = Service(self.store, model).answer(self.id, question)
        self.assertFalse(result["supported"])
        self.assertEqual(model.calls[0]["payload"]["QUESTION"], question)

    def test_unknown_ids_and_model_written_quote_fields_are_rejected(self):
        for result in [
            {"explanation": "A fact.", "status": "answered", "sentence_ids": ["E999"]},
            {"explanation": "A fact.", "status": "answered", "sentence_ids": ["E1"],
             "quote": "Annual memberships are refundable."},
        ]:
            with self.subTest(result=result), self.assertRaises(ModelError):
                Service(self.store, ScriptedModel(result)).answer(self.id, "What policy applies?")

    def test_invalid_question_and_empty_retrieval_never_call_model(self):
        model = ScriptedModel()
        for question in [None, 17, "", "  ", "x" * 2001]:
            with self.subTest(question=question), self.assertRaises(DocumentError):
                Service(self.store, model).answer(self.id, question)
        large = self.store.import_pdf(pdf_bytes(["A workshop policy."] * 13), "large.pdf")
        result = Service(self.store, model).answer(large["id"], "unicorns meteorites")
        self.assertFalse(result["supported"])
        self.assertEqual(result["retrieved"], [])
        self.assertEqual(model.calls, [])

    def test_transport_failure_has_no_fabricated_fallback(self):
        with self.assertRaisesRegex(ModelError, "unavailable"):
            Service(self.store, ScriptedModel(ModelError("Model unavailable"))).answer(
                self.id, "What policy applies?")


if __name__ == "__main__":
    unittest.main()
