from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reoengine.adapters.api_json import ApiJsonAdapter
from reoengine.adapters.base import ErrorKind
from reoengine.config import Engine, Effort, RoleConfig, load_roles
from reoengine.usage import TokenUsage
from tests.fakes.http_server import ScriptedHttpServer


class Response302:
    def __init__(self, base: str) -> None:
        self.location = base + "/stolen"


def encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


class ApiJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Return an object", encoding="utf-8")
        self.schema = self.root / "schema.json"
        self.schema.write_text(json.dumps({"$schema": "ignored", "title": "ignored", "description": "ignored",
                                           "type": "object", "required": ["answer"],
                                           "properties": {"answer": {"type": "string"}},
                                           "additionalProperties": False}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def role(self, engine: Engine, base: str, *, effort: Effort = Effort.HIGH,
             key_env: str | None = None) -> RoleConfig:
        return RoleConfig(engine, "fake-model", effort=effort, api_key_env=key_env, base_url=base)

    def invoke(self, engine: Engine, server: ScriptedHttpServer, response_schema: Path | None = None) -> tuple[ApiJsonAdapter, object]:
        adapter = ApiJsonAdapter(engine)
        result = adapter.invoke(self.role(engine, server.base_url), self.prompt, self.root, 5, response_schema)
        return adapter, result

    def test_gemini_success_schema_usage_and_thought_filtering(self) -> None:
        body = {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True},
                                                              {"text": '{"answer":"ok"}'}]}}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5,
                                  "thoughtsTokenCount": 3, "cachedContentTokenCount": 2}}
        with ScriptedHttpServer([(200, encoded(body))]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            adapter = ApiJsonAdapter(Engine.GEMINI)
            result = adapter.invoke(self.role(Engine.GEMINI, server.base_url, effort=Effort.MEDIUM),
                                     self.prompt, self.root, 5, self.schema)
        self.assertTrue(result.success)
        self.assertEqual(server.requests[0]["headers"]["X-Goog-Api-Key"], "secret")
        payload = server.requests[0]["body"]
        self.assertEqual(payload["generationConfig"]["responseJsonSchema"]["type"], "object")
        self.assertNotIn("$schema", payload["generationConfig"]["responseJsonSchema"])
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MEDIUM")
        self.assertEqual(result.usage, TokenUsage(11, 2, 5, 3))
        self.assertEqual(result.final_message_path.read_text(encoding="utf-8"), '{"answer":"ok"}')

    def test_gemini_retries_quota_and_transient_server_error(self) -> None:
        with ScriptedHttpServer([(429, b'{}')] * 5) as server, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            adapter = ApiJsonAdapter(Engine.GEMINI)
            delays: list[float] = []
            adapter._sleep = delays.append
            result = adapter.invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 100)
        self.assertEqual(result.error, ErrorKind.QUOTA)
        self.assertEqual(len(server.requests), 5)
        self.assertEqual(delays, [2, 4, 8, 16])
        success = {"candidates": [{"content": {"parts": [{"text": '{"answer":"yes"}'}]}}]}
        with ScriptedHttpServer([(500, b'{}'), (200, encoded(success))]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            adapter = ApiJsonAdapter(Engine.GEMINI)
            adapter._sleep = lambda _: None
            result = adapter.invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        self.assertTrue(result.success)
        self.assertEqual(len(server.requests), 2)

    def test_gemini_bad_request_malformed_answer_and_empty_candidates(self) -> None:
        with ScriptedHttpServer([(400, b'{"error":"bad"}')]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            result = ApiJsonAdapter(Engine.GEMINI).invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        self.assertEqual(result.error, ErrorKind.PROCESS)
        for body, expected in (({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}, ErrorKind.SCHEMA),
                               ({"candidates": []}, ErrorKind.PROTOCOL),
                               ({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}, ErrorKind.PROTOCOL)):
            with self.subTest(expected=expected), ScriptedHttpServer([(200, encoded(body))]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
                result = ApiJsonAdapter(Engine.GEMINI).invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
            self.assertEqual(result.error, expected)

    def test_redirect_is_not_followed_and_key_is_not_forwarded(self) -> None:
        with ScriptedHttpServer([]) as other, patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            redirect = Response302(other.base_url)
            with ScriptedHttpServer([(302, b"{}")]) as server:
                server.redirect_to = redirect.location
                adapter = ApiJsonAdapter(Engine.GEMINI)
                result = adapter.invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5,
                                        self.schema)
            self.assertEqual(result.error, ErrorKind.PROCESS)
            self.assertEqual(other.requests, [])

    def test_missing_key_does_not_contact_server(self) -> None:
        with ScriptedHttpServer([]) as server, patch.dict(os.environ, {}, clear=True):
            result = ApiJsonAdapter(Engine.GEMINI).invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        self.assertEqual(result.error, ErrorKind.PROCESS)
        self.assertEqual(result.stderr_tail, "missing GEMINI_API_KEY")
        self.assertEqual(server.requests, [])

    def test_secret_is_redacted_from_result_and_provider_error(self) -> None:
        secret = "secret-value"
        with ScriptedHttpServer([(401, encoded({"error": secret}))]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": secret}):
            result = ApiJsonAdapter(Engine.GEMINI).invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        rendered = repr(result)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(secret, result.stderr_tail)
        with ScriptedHttpServer([(200, encoded({"candidates": [{"content": {"parts": [{"text": '{"answer":"secret-value"}'}]}}]}))]) as server, patch.dict(os.environ, {"GEMINI_API_KEY": secret}):
            result = ApiJsonAdapter(Engine.GEMINI).invoke(self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        self.assertNotIn(secret, result.final_message_path.read_text(encoding="utf-8"))

    def test_gemini_key_is_absent_from_all_artifacts_after_error_and_success(self) -> None:
        secret = "gemini-key-from-environment"
        error_body = encoded({"error": f"provider rejected key {secret}"})
        success_body = encoded({"candidates": [{"content": {"parts": [
            {"text": f'{{"answer":"{secret}"}}'}]}}]})
        with patch.dict(os.environ, {"GEMINI_API_KEY": secret}):
            with ScriptedHttpServer([(400, error_body)]) as server:
                failed = ApiJsonAdapter(Engine.GEMINI).invoke(
                    self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
            self.assertEqual(failed.error, ErrorKind.PROCESS)
            with ScriptedHttpServer([(200, success_body)]) as server:
                succeeded = ApiJsonAdapter(Engine.GEMINI).invoke(
                    self.role(Engine.GEMINI, server.base_url), self.prompt, self.root, 5)
        self.assertTrue(succeeded.success)
        self.assertNotIn(secret, repr(failed))
        self.assertNotIn(secret, repr(succeeded))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes().decode("utf-8", errors="replace"), str(path))

    def test_anthropic_and_openai_success_and_exhausted_quota(self) -> None:
        responses = {
            Engine.ANTHROPIC: {"content": [{"type": "tool_use", "input": {"answer": "ok"}}],
                               "usage": {"input_tokens": 4, "output_tokens": 3, "cache_read_input_tokens": 1}},
            Engine.OPENAI: {"output_text": '{"answer":"ok"}',
                            "usage": {"input_tokens": 6, "output_tokens": 2}},
        }
        env = {"ANTHROPIC_API_KEY": "a-secret", "OPENAI_API_KEY": "o-secret"}
        with patch.dict(os.environ, env):
            for engine, body in responses.items():
                with self.subTest(engine=engine), ScriptedHttpServer([(200, encoded(body))]) as server:
                    result = ApiJsonAdapter(engine).invoke(self.role(engine, server.base_url), self.prompt, self.root, 5, self.schema)
                self.assertTrue(result.success)
                payload = server.requests[0]["body"]
                if engine == Engine.ANTHROPIC:
                    self.assertEqual(payload["tool_choice"]["name"], "respond")
                    self.assertEqual(server.requests[0]["headers"]["X-Api-Key"], "a-secret")
                else:
                    self.assertTrue(payload["text"]["format"]["strict"])
                    self.assertEqual(server.requests[0]["headers"]["Authorization"], "Bearer o-secret")
        for engine, key in ((Engine.ANTHROPIC, "ANTHROPIC_API_KEY"), (Engine.OPENAI, "OPENAI_API_KEY")):
            with self.subTest(engine=engine), ScriptedHttpServer([(429, b'{}')] * 5) as server, patch.dict(os.environ, {key: "secret"}):
                adapter = ApiJsonAdapter(engine)
                adapter._sleep = lambda _: None
                result = adapter.invoke(self.role(engine, server.base_url), self.prompt, self.root, 100)
            self.assertEqual(result.error, ErrorKind.QUOTA)
            self.assertEqual(len(server.requests), 5)


class ApiConfigurationTests(unittest.TestCase):
    def test_api_fields_key_name_and_localhost_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            roles = Path(temporary) / "roles.toml"
            roles.write_text('[roles.reader]\nengine="gemini"\nmodel="fake"\napi_key_env="CUSTOM_KEY"\nbase_url="http://localhost:1234"\n', encoding="utf-8")
            config = load_roles(roles)["reader"]
            self.assertEqual((config.api_key_env, config.base_url), ("CUSTOM_KEY", "http://localhost:1234"))
            roles.write_text('[roles.reader]\nengine="gemini"\nmodel="fake"\napi_key_env="bad-name"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "api_key_env"):
                load_roles(roles)
            roles.write_text('[roles.reader]\nengine="gemini"\nmodel="fake"\nbase_url="http://example.com"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "base_url"):
                load_roles(roles)

    def test_api_engine_rejected_for_coder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            roles = Path(temporary) / "roles.toml"
            roles.write_text('[roles.coder]\nengine="openai"\nmodel="fake"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supports only JSON-answering roles"):
                load_roles(roles)


if __name__ == "__main__":
    unittest.main()
