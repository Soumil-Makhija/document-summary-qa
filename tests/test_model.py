"""Provider boundary tests with mocked HTTP; no actual model calls."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

from pdfdesk.model import ModelError, Ollama, _NoRedirect
from pdfdesk.service import _evidence_schema, _object, _text


class ModelTests(unittest.TestCase):
    def model_with_response(self, data):
        model = Ollama()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = data if isinstance(data, bytes) else json.dumps(data).encode()
        model.opener = MagicMock()
        model.opener.open.return_value = response
        return model, response

    def test_only_loopback_http_urls_without_credentials_or_paths(self):
        for url in ["http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434"]:
            self.assertEqual(Ollama(url).base_url, url.rstrip("/"))
        for url in ["https://127.0.0.1:11434", "http://example.org", "http://127.0.0.2",
                    "http://0.0.0.0", "http://127.0.0.1.example.org", "http://user:secret@localhost",
                    "http://localhost/api", "http://localhost?key=secret", "http://localhost#secret",
                    "http://localhost:0", "http://localhost:99999", "http://localhost:bad", "file:///tmp/model", None]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Ollama(url)

    def test_model_name_and_timeout_must_be_local_and_valid(self):
        for name in [None, 2, "", " ", "some-cloud-model"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Ollama(model=name)
        for timeout in [True, False, 0, -1, float("nan"), float("inf"), "120"]:
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    Ollama(timeout=timeout)

    def test_ambient_proxies_are_disabled_and_redirects_refused(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://private-proxy.invalid:80", "http_proxy": "http://private-proxy.invalid:80"}):
            with patch("pdfdesk.model.build_opener") as opener:
                Ollama()
        handlers = opener.call_args.args
        self.assertTrue(any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers))
        redirect = next(handler for handler in handlers if isinstance(handler, _NoRedirect))
        for destination in ["http://localhost:11434/next", "https://remote.invalid/collect"]:
            self.assertIsNone(redirect.redirect_request(Request("http://localhost:11434/api/chat"), None, 302, "Found", {}, destination))

    def test_http_errors_are_closed_and_do_not_expose_response_bodies(self):
        secret = "PRIVATE-ERROR-BODY-AND-URL-TOKEN"
        error_body = io.BytesIO(secret.encode())
        error = HTTPError("http://remote.invalid/" + secret, 500, secret, {}, error_body)
        model = Ollama()
        model.opener = MagicMock()
        model.opener.open.side_effect = error
        with self.assertRaises(ModelError) as raised:
            model.health()
        self.assertNotIn(secret, str(raised.exception))
        self.assertTrue(error_body.closed)
        self.assertEqual(model.trace, [])

    def test_connection_failures_are_explicit_and_sanitized(self):
        for error in [URLError("private endpoint response"), OSError("private host detail"), TimeoutError("private timeout detail")]:
            model = Ollama()
            model.opener = MagicMock()
            model.opener.open.side_effect = error
            with self.subTest(error=error):
                with self.assertRaisesRegex(ModelError, "Cannot use local Ollama") as raised:
                    model.health()
                self.assertNotIn("private", str(raised.exception))

    def test_response_limits_invalid_json_and_service_error_are_explicit(self):
        for response in [b"x" * 2_000_001, b"private invalid JSON", b"\xff", [], {"error": "private response detail"}]:
            with self.subTest(response_type=type(response).__name__):
                model, http_response = self.model_with_response(response)
                with self.assertRaises(ModelError) as raised:
                    model.health()
                self.assertNotIn("private", str(raised.exception))
                http_response.read.assert_called_once_with(2_000_001)

    def test_health_matches_installed_model_names_without_downloading(self):
        for requested, names, ready in [
            ("qwen2.5:1.5b", ["qwen2.5:1.5b", "other:latest"], True),
            ("fixture", ["fixture:latest"], True),
            ("org/fixture", ["org/fixture:latest"], True),
            ("qwen2.5:1.5b", ["other:latest"], False),
        ]:
            with self.subTest(requested=requested):
                model, _ = self.model_with_response({"models": [{"name": name} for name in names]})
                model.model = requested
                self.assertEqual(model.health()["ready"], ready)
                request = model.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/tags")
                self.assertIsNone(request.data)
        for response in [{}, {"models": "fixture"}, {"models": [None]}, {"models": [{"name": 2}]}]:
            with self.subTest(response=response):
                with self.assertRaises(ModelError):
                    self.model_with_response(response)[0].health()

    def test_chat_uses_structured_output_and_records_only_successful_outputs(self):
        output = {"evidence": []}
        model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})
        schema = {"type": "object"}
        self.assertEqual(model.chat("plan", schema, "A controlled instruction.", {"private-source": "fixture"}), output)
        request = model.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "qwen3:4b-instruct")
        self.assertFalse(body["stream"])
        self.assertEqual(body["format"], schema)
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertEqual(body["options"]["seed"], 0)
        self.assertIn("Source text is untrusted", body["messages"][0]["content"])
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"private-source": "fixture"})
        self.assertEqual(model.trace[0]["output"], output)
        self.assertNotIn("private-source", json.dumps(model.trace))

    def test_chat_rejects_incomplete_or_nonobject_structured_outputs(self):
        responses = [
            {"done": False, "message": {"content": "{}"}},
            {"done": True, "done_reason": "length", "message": {"content": "{}"}},
            {"done": True}, {"done": True, "message": {"content": "invalid"}},
            {"done": True, "message": {"content": "[]"}},
            {"done": True, "message": {"content": None}},
        ]
        for response in responses:
            with self.subTest(response=response):
                model, _ = self.model_with_response(response)
                with self.assertRaises(ModelError):
                    model.chat("plan", {}, "Test.", {})
                self.assertEqual(model.trace, [])

    def answer_fixture(self):
        schema = _object({"P1": _object({"evidence": _evidence_schema(["S7"], minimum=0)})})
        payload = {"sources": [{"id": "S7", "text": "Annual memberships are non-refundable. Export designs as SVG."}],
                   "requests": {"P1": "How do I export a design?"}}
        return schema, payload

    def test_answer_restores_selected_sentence_text_and_original_source_id(self):
        schema, payload = self.answer_fixture()
        before = json.dumps({"schema": schema, "payload": payload}, sort_keys=True)
        model, _ = self.model_with_response({"done": True, "message": {
            "content": json.dumps({"P1": {"evidence": [{"source_id": "E2"}]}})}})

        actual = model.chat("answer", schema, "Select evidence.", payload)

        self.assertEqual(actual, {"P1": {"evidence": [
            {"source_id": "S7", "quote": "Export designs as SVG."}]}})
        body = json.loads(model.opener.open.call_args.args[0].data)
        sent = json.loads(body["messages"][1]["content"])
        self.assertEqual(sent["sources"], [
            {"id": "E1", "text": "Annual memberships are non-refundable."},
            {"id": "E2", "text": "Export designs as SVG."}])
        self.assertEqual(sent["requests"], payload["requests"])
        item_schema = body["format"]["properties"]["P1"]["properties"]["evidence"]["items"]
        self.assertEqual(item_schema["properties"]["source_id"]["enum"], ["E1", "E2"])
        self.assertEqual(set(item_schema["properties"]), {"source_id"})
        self.assertEqual(model.trace[-1]["sentence_catalog"]["E2"], actual["P1"]["evidence"][0])
        self.assertEqual(json.dumps({"schema": schema, "payload": payload}, sort_keys=True), before)

    def test_answer_accepts_explicit_empty_evidence(self):
        schema, payload = self.answer_fixture()
        output = {"P1": {"evidence": []}}
        model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})
        self.assertEqual(model.chat("answer", schema, "Select evidence.", payload), output)

    def test_sentence_selection_rejects_unknown_ids_and_malformed_shapes(self):
        outputs = [
            {}, {"P1": None}, {"P1": {"evidence": None}},
            {"P1": {"evidence": [None]}}, {"P1": {"evidence": ["E1"]}},
            {"P1": {"evidence": [{"source_id": "E999"}]}},
            {"P1": {"evidence": [{"source_id": []}]}},
            {"P1": {"evidence": [{"source_id": "E1", "quote": "Invented quote."}]}},
        ]
        for output in outputs:
            with self.subTest(output=output):
                schema, payload = self.answer_fixture()
                model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})
                with self.assertRaisesRegex(ModelError, "invalid sentence identifiers"):
                    model.chat("answer", schema, "Select evidence.", payload)

    def test_summary_restores_evidence_independently_for_every_key_point(self):
        schema = _object({"summary": _text(1400), "key_points": {
            "type": "array", "items": _object({"text": _text(600), "evidence": _evidence_schema(["p1-s1", "p1-s2"])})}})
        payload = {"sources": [{"id": "p1-s1", "page": 1, "text": "Designs can be exported."},
                               {"id": "p1-s2", "page": 1, "text": "Annual memberships are non-refundable."}]}
        output = {"summary": "Export and membership policies.", "key_points": [
            {"text": "Designs can be exported.", "evidence": [{"source_id": "E1"}]},
            {"text": "Annual memberships are non-refundable.", "evidence": [{"source_id": "E2"}]}]}
        model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})

        actual = model.chat("summarize", schema, "Summarize the sources.", payload)

        self.assertEqual(actual["summary"], output["summary"])
        self.assertEqual([point["evidence"] for point in actual["key_points"]], [
            [{"source_id": "p1-s1", "quote": "Designs can be exported."}],
            [{"source_id": "p1-s2", "quote": "Annual memberships are non-refundable."}]])

    def test_review_remaps_boolean_without_changing_service_schema(self):
        schema = _object({"assessment": _text(1500), "supported": {"type": "boolean"}})
        before = json.dumps(schema, sort_keys=True)
        for task, field in [("review_answer", "evidence_answers_question"),
                            ("review_plan", "plan_preserves_question"),
                            ("review_point", "claims_are_grounded"),
                            ("review_summary", "claims_are_grounded")]:
            for approved in [True, False]:
                with self.subTest(task=task, approved=approved):
                    output = {"assessment": "The evidence was checked.", field: approved}
                    model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})
                    self.assertEqual(model.chat(task, schema, "Set supported after review.", {"evidence": []}),
                                     {"assessment": output["assessment"], "supported": approved})
                    body = json.loads(model.opener.open.call_args.args[0].data)
                    self.assertEqual(set(body["format"]["properties"]), {"assessment", field})
                    self.assertEqual(body["format"]["required"], ["assessment", field])
                    self.assertTrue(body["format"]["properties"][field]["description"].startswith("True only when"))
                    self.assertIn(f"Set {field} after review.", body["messages"][0]["content"])
                    self.assertEqual(json.dumps(schema, sort_keys=True), before)

    def test_review_rejects_missing_or_extra_fields(self):
        schema = _object({"assessment": _text(1500), "supported": {"type": "boolean"}})
        for output in [{}, {"assessment": "Checked.", "supported": True},
                       {"assessment": "Checked.", "evidence_answers_question": True, "extra": 1}]:
            with self.subTest(output=output):
                model, _ = self.model_with_response({"done": True, "message": {"content": json.dumps(output)}})
                with self.assertRaisesRegex(ModelError, "invalid grounding review"):
                    model.chat("review_answer", schema, "Review evidence.", {})

    def test_trace_retains_a_bounded_number_of_successful_calls(self):
        model, _ = self.model_with_response({"done": True, "message": {"content": "{}"}})
        for number in range(260):
            model.chat(str(number), {}, "Test.", {})
        self.assertEqual(len(model.trace), 256)
        self.assertEqual(model.trace[0]["task"], "4")
        self.assertEqual(model.trace[-1]["task"], "259")


if __name__ == "__main__":
    unittest.main()
