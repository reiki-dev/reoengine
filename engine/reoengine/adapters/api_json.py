"""HTTP adapters for providers that return JSON answers."""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
import threading
from queue import Queue
from collections.abc import Mapping
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from typing import Callable

from .._data import Json, integer, loads, object_value
from ..config import Engine, Pricing, QuotaConfig, RoleConfig
from ..usage import TokenUsage, cost_usd
from .base import AgentResult, ErrorKind, validate_response

_DEFAULT_KEYS = {Engine.ANTHROPIC: "ANTHROPIC_API_KEY", Engine.OPENAI: "OPENAI_API_KEY",
                 Engine.GEMINI: "GEMINI_API_KEY"}
_RETRY_STATUS = {429, 500, 502, 503, 529}
_MAX_RESPONSE = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the API key header must not travel to another host."""

    def redirect_request(self, req: urllib.request.Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class ApiJsonAdapter:
    def __init__(self, engine: Engine, *, quota: QuotaConfig | None = None,
                 pricing: Mapping[str, Pricing] | None = None) -> None:
        if engine not in _DEFAULT_KEYS:
            raise ValueError("ApiJsonAdapter requires an API engine")
        self.engine = engine
        self.quota = quota or QuotaConfig()
        self.pricing = pricing or {}
        self._sleep: Callable[[float], None] = time.sleep

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        del cwd
        if role_cfg.engine != self.engine:
            raise ValueError(f"ApiJsonAdapter requires engine={self.engine.value}")
        env_name = role_cfg.api_key_env or _DEFAULT_KEYS[self.engine]
        key = os.environ.get(env_name)
        if not key:
            return AgentResult(-1, "", TokenUsage(), 0, None, f"missing {env_name}", ErrorKind.PROCESS,
                               role_cfg.note)
        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        output = prompt_file.resolve().parent / f"response-{uuid.uuid4().hex}.json"
        try:
            prompt = prompt_file.read_text(encoding="utf-8")
            definition = _schema_value(schema) if schema is not None else None
            payload = _request_payload(self.engine, role_cfg, prompt, definition)
            url, headers = _request_target(self.engine, role_cfg, key)
            if role_cfg.base_url is not None:
                parsed = urlsplit(role_cfg.base_url)
                host = (parsed.hostname or "").lower()
                loopback = host in {"127.0.0.1", "localhost", "::1"}
                if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or \
                        not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
                    raise ValueError("Unsafe API base URL")
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (OSError, ValueError, UnicodeError):
            return AgentResult(-1, "", TokenUsage(), time.monotonic() - started, None,
                               error=ErrorKind.PROCESS, note=role_cfg.note)

        delays = (2.0, 4.0, 8.0, 16.0)
        retries = 0
        status = 0
        response_body = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _result(-1, time.monotonic() - started, None, TokenUsage(),
                               ErrorKind.TIMEOUT, "", role_cfg.note, key)
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            result_queue: Queue[tuple[int, bytes] | BaseException] = Queue(maxsize=1)
            response_holder: list[object] = []
            def perform_request() -> None:
                try:
                    try:
                        response = _OPENER.open(request, timeout=max(0.001, remaining))
                        response_holder.append(response)
                        with response:
                            result_queue.put((response.status, response.read(_MAX_RESPONSE + 1)))
                    except urllib.error.HTTPError as exc:
                        response_holder.append(exc)
                        with exc:
                            result_queue.put((exc.code, exc.read(_MAX_RESPONSE + 1)))
                except BaseException as exc:
                    result_queue.put(exc)
            worker = threading.Thread(target=perform_request, daemon=True)
            worker.start()
            worker.join(max(0.0, remaining))
            if worker.is_alive():
                for response in response_holder:
                    def close_response(value: object = response) -> None:
                        try:
                            value.close()  # type: ignore[attr-defined]
                        except (AttributeError, OSError):
                            pass
                    threading.Thread(target=close_response, daemon=True).start()
                return _result(-1, time.monotonic() - started, None, TokenUsage(),
                               ErrorKind.TIMEOUT, "", role_cfg.note, key)
            response_result = result_queue.get()
            if isinstance(response_result, BaseException):
                exc = response_result
                if isinstance(exc, urllib.error.HTTPError):
                    status = exc.code
                    response_body = exc.read(_MAX_RESPONSE + 1)
                elif isinstance(exc, (TimeoutError, socket.timeout)):
                    return _result(-1, time.monotonic() - started, None, TokenUsage(), ErrorKind.TIMEOUT, "", role_cfg.note, key)
                elif isinstance(exc, urllib.error.URLError):
                    kind = ErrorKind.TIMEOUT if isinstance(exc.reason, (TimeoutError, socket.timeout)) else ErrorKind.PROCESS
                    return _result(-1, time.monotonic() - started, None, TokenUsage(), kind, "", role_cfg.note, key)
                else:
                    return _result(-1, time.monotonic() - started, None, TokenUsage(), ErrorKind.PROCESS, "", role_cfg.note, key)
            else:
                status, response_body = response_result
            if len(response_body) > _MAX_RESPONSE:
                return _result(status, time.monotonic() - started, None, TokenUsage(),
                               ErrorKind.SCHEMA, "response exceeded 8 MB", role_cfg.note, key)
            if status not in _RETRY_STATUS:
                break
            if retries >= len(delays):
                retry_error = ErrorKind.QUOTA if status == 429 else ErrorKind.PROCESS
                return _result(status, time.monotonic() - started, None, TokenUsage(), retry_error,
                               "HTTP request failed", role_cfg.note, key)
            delay = delays[retries]
            if time.monotonic() + delay > deadline:
                retry_error = ErrorKind.QUOTA if status == 429 else ErrorKind.TIMEOUT
                return _result(status, time.monotonic() - started, None, TokenUsage(), retry_error,
                               "HTTP request failed", role_cfg.note, key)
            self._sleep(delay)
            retries += 1

        if status < 200 or status >= 300:
            return _result(status, time.monotonic() - started, None, TokenUsage(), ErrorKind.PROCESS,
                           "HTTP request failed", role_cfg.note, key)
        try:
            decoded = loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeError):
            return _result(status, time.monotonic() - started, None, TokenUsage(), ErrorKind.SCHEMA,
                           "response was not valid JSON", role_cfg.note, key)
        try:
            provider = object_value(decoded, "provider response")
            message, usage = _extract(self.engine, provider)
        except (ValueError, TypeError, KeyError):
            return _result(status, time.monotonic() - started, None, TokenUsage(), ErrorKind.PROTOCOL,
                           "invalid provider response", role_cfg.note, key)
        pricing = self.pricing.get(role_cfg.model)
        if pricing is not None:
            usage = TokenUsage(usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
                               usage.reasoning_output_tokens, cost_usd(usage, pricing),
                               usage.duration_seconds, "computed")
        message = message.replace(key, "[redacted]")
        content_error: ErrorKind | None = None
        try:
            output.write_text(message, encoding="utf-8")
            # Invalid JSON and schema failures are response-content failures.
            loads(message)
            validate_response(output, schema)
            content_error = None
        except (ValueError, UnicodeError, OSError):
            content_error = ErrorKind.SCHEMA
        return _result(0, time.monotonic() - started, output, usage, content_error, "", role_cfg.note, key)


def _schema_value(path: Path) -> dict[str, Json]:
    definition = object_value(loads(path.read_text(encoding="utf-8")), "response schema")
    return {name: value for name, value in definition.items()
            if name not in {"$schema", "title", "description"}}


def _request_target(engine: Engine, role_cfg: RoleConfig, key: str) -> tuple[str, dict[str, str]]:
    base = role_cfg.base_url
    if engine == Engine.GEMINI:
        return f"{(base or 'https://generativelanguage.googleapis.com').rstrip('/')}/v1beta/models/{role_cfg.model}:generateContent", {
            "x-goog-api-key": key, "Content-Type": "application/json"}
    if engine == Engine.ANTHROPIC:
        return f"{(base or 'https://api.anthropic.com').rstrip('/')}/v1/messages", {
            "x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    return f"{(base or 'https://api.openai.com').rstrip('/')}/v1/responses", {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _request_payload(engine: Engine, role: RoleConfig, prompt: str,
                     schema: dict[str, Json] | None) -> dict[str, Json]:
    level = {"low": "LOW", "minimal": "LOW", "medium": "MEDIUM"}.get(role.effort.value, "HIGH")
    if engine == Engine.GEMINI:
        generation: dict[str, Json] = {"thinkingConfig": {"thinkingLevel": level}}
        if schema is not None:
            generation.update({"responseMimeType": "application/json", "responseJsonSchema": schema})
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation}
    if engine == Engine.ANTHROPIC:
        payload: dict[str, Json] = {"model": role.model, "max_tokens": 16000,
                                    "messages": [{"role": "user", "content": prompt}]}
        if schema is not None:
            payload["tools"] = [{"name": "respond", "description": "Return the answer",
                                 "input_schema": schema}]
            payload["tool_choice"] = {"type": "tool", "name": "respond"}
        return payload
    payload = {"model": role.model, "input": prompt}
    if schema is not None:
        payload["text"] = {"format": {"type": "json_schema", "name": "response",
                                      "schema": schema, "strict": True}}
    effort = role.effort.value
    if effort in {"low", "medium", "high"}:
        payload["reasoning"] = {"effort": effort}
    return payload


def _extract(engine: Engine, data: dict[str, Json]) -> tuple[str, TokenUsage]:
    if engine == Engine.GEMINI:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ValueError("Gemini prompt blocked")
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("Gemini returned no candidates")
        candidate = object_value(candidates[0], "candidate")
        if candidate.get("finishReason") not in (None, "STOP"):
            raise ValueError("Gemini response incomplete")
        content = object_value(candidate.get("content"), "candidate content")
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise ValueError("Gemini candidate has no parts")
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("thought") is True:
                continue
            text_value = part.get("text")
            if isinstance(text_value, str):
                texts.append(text_value)
        usage = object_value(data.get("usageMetadata", {}), "usageMetadata")
        return "".join(texts), TokenUsage(_int_or_none(usage, "promptTokenCount"),
                                          _int_or_none(usage, "cachedContentTokenCount"),
                                          _int_or_none(usage, "candidatesTokenCount"),
                                          _int_or_none(usage, "thoughtsTokenCount"))
    usage_data = object_value(data.get("usage", {}), "usage")
    if engine == Engine.ANTHROPIC:
        if data.get("stop_reason") == "max_tokens":
            raise ValueError("Anthropic response incomplete")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ValueError("Anthropic response has no content")
        tool = next((block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"), None)
        if tool is not None:
            answer = json.dumps(tool.get("input"), ensure_ascii=False, separators=(",", ":"))
        else:
            answer = "".join(str(block.get("text", "")) for block in blocks
                             if isinstance(block, dict) and block.get("type") == "text")
        return answer, TokenUsage(_int_or_none(usage_data, "input_tokens"),
                                  _int_or_none(usage_data, "cache_read_input_tokens"),
                                  _int_or_none(usage_data, "output_tokens"))
    if data.get("status") == "incomplete":
        raise ValueError("OpenAI response incomplete")
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        answer = output_text
    else:
        output = data.get("output")
        if not isinstance(output, list):
            raise ValueError("OpenAI response has no output")
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            contents = item.get("content")
            if not isinstance(contents, list):
                continue
            for output_content in contents:
                if isinstance(output_content, dict) and output_content.get("type") == "output_text":
                    text = output_content.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        answer = "".join(chunks)
    return answer, TokenUsage(_int_or_none(usage_data, "input_tokens"), None,
                              _int_or_none(usage_data, "output_tokens"))


def _int_or_none(data: dict[str, Json], key: str) -> int | None:
    value = data.get(key)
    return None if value is None else integer(value, key)


def _result(code: int, duration: float, path: Path | None, usage: TokenUsage,
            error: ErrorKind | None, message: str, note: str | None, key: str) -> AgentResult:
    safe_message = message.replace(key, "[redacted]") if key else message
    safe_note = note.replace(key, "[redacted]") if note else note
    return AgentResult(code, "", usage, duration, path, safe_message, error, safe_note)
