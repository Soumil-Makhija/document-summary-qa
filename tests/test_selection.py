"""Question wording and exact-source boundary checks; not model-quality tests."""
from copy import deepcopy
import unittest

from pdfdesk.core import DocumentError
from pdfdesk.model import ModelError
from pdfdesk.selection import question_clauses, select_answer, sentence_catalog


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, task, schema, instruction, payload):
        self.calls.append(deepcopy(payload))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def answered(*identifiers):
    return {"explanation": "The exact requested facts are stated.", "status": "answered", "sentence_ids": list(identifiers)}


MISSING = {"explanation": "A requested fact is absent.", "status": "missing", "sentence_ids": []}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.passages = [
            {"id": "p1-s1", "page": 1, "text": "Annual memberships are non-refundable."},
            {"id": "p2-s1", "page": 2, "text": "Choose Download then SVG to export a design."},
        ]

    def test_explicit_compound_keeps_literal_requested_facts_and_combines_pages(self):
        model = FakeModel(answered("E2"), answered("E1"))
        question = "How do I export a design, and are annual memberships refundable?"
        result = select_answer(model, question, self.passages)
        self.assertEqual([call["QUESTION"] for call in model.calls],
                         ["How do I export a design", "are annual memberships refundable?"])
        self.assertEqual([item["page"] for item in result], [2, 1])
        for item in result:
            passage = next(p for p in self.passages if p["id"] == item["passage_id"])
            self.assertIn(item["quote"], passage["text"])

    def test_no_planner_can_weaken_time_or_certainty_qualifiers(self):
        for question in ["What is the guaranteed repair deadline?", "What was the mean after six months?",
                         "How many checklist-group and control-group participants finished?"]:
            with self.subTest(question=question):
                model = FakeModel(MISSING)
                self.assertIsNone(select_answer(model, question, self.passages))
                self.assertEqual(model.calls[0]["QUESTION"], question)

    def test_one_missing_clause_returns_no_partial_supported_answer(self):
        model = FakeModel(answered("E2"), MISSING)
        self.assertIsNone(select_answer(model, "How do I export, and what is the fee?", self.passages))

    def test_malformed_or_inconsistent_selections_fail_closed(self):
        for output in [None, [], {}, answered(), answered("invented"), answered("E1", "E1"),
                       {**MISSING, "sentence_ids": ["E1"]}, {**answered("E1"), "status": []},
                       {**answered("E1"), "sentence_ids": [["E1"]]},
                       {**answered("E1"), "explanation": ""}, {**answered("E1"), "extra": True}]:
            with self.subTest(output=output):
                with self.assertRaises(ModelError):
                    select_answer(FakeModel(output), "Are annual memberships refundable?", self.passages)

    def test_transport_failure_does_not_fabricate_an_answer(self):
        with self.assertRaisesRegex(ModelError, "unavailable"):
            select_answer(FakeModel(ModelError("Model unavailable")), "What policy?", self.passages)

    def test_overlap_deduplication_preserves_a_real_page_and_passage(self):
        repeated = self.passages + [{**self.passages[0], "id": "p1-s2"}]
        catalog = sentence_catalog(repeated)
        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog["E1"]["passage_id"], "p1-s1")

    def test_empty_sources_make_no_model_call_and_too_many_clauses_fail(self):
        model = FakeModel()
        self.assertIsNone(select_answer(model, "What policy?", []))
        self.assertEqual(model.calls, [])
        with self.assertRaises(DocumentError):
            question_clauses(" and ".join(["what is the policy"] * 9))


if __name__ == "__main__":
    unittest.main()
