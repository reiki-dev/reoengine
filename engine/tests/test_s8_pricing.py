from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reoengine.adapters.api_json import ApiJsonAdapter
from reoengine.config import (Effort, Engine, Pricing, RoleConfig, load_pricing,
                          load_roles)
from reoengine.doctor import Probe
from reoengine.events import LimitRemaining
from reoengine.procs import ProcessResult
from reoengine.state import Stage
from reoengine.usage import TokenUsage, UsageLog, UsageRecord, cost_usd
from reoengine.cli import main
from tests.fakes.http_server import ScriptedHttpServer


class PricingTests(unittest.TestCase):
    def test_formula_defaults_optional_prices_and_missing_tokens(self) -> None:
        pricing = Pricing(input=2, output=4, cached_input=0.5, reasoning_output=8)
        usage = TokenUsage(1_000_000, 200_000, 300_000, 50_000)
        self.assertEqual(cost_usd(usage, pricing), 3.3)
        self.assertEqual(cost_usd(TokenUsage(), Pricing(input=2, output=4)), 0)

    def test_load_pricing_absent_valid_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pricing.toml"
            self.assertEqual(load_pricing(path), {})
            path.write_text('["gemini-3.8-flash"]\ninput=1.5\noutput=3\n', encoding="utf-8")
            self.assertEqual(load_pricing(path)["gemini-3.8-flash"], Pricing(1.5, 3, 1.5, 3))
            for content in ('["model"]\ninput=1\noutput=1\nextra=1\n',
                            '["model"]\ninput=-1\noutput=1\n',
                            '["model"]\ninput="bad"\noutput=1\n',
                            '["model"]\ninput=1\n'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_pricing(path)

    def test_usage_cost_source_round_trip_and_legacy_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = UsageRecord("run", Stage.IMPLEMENT, "coder", Engine.CODEX, "model", Effort.HIGH,
                                 TokenUsage(10, cost_usd=0.25, cost_source="reported"), 100, 0,
                                 "accepted", 1)
            log = UsageLog(directory)
            path = log.append(record)
            self.assertEqual(log.read_days()[0].usage.cost_source, "reported")
            data = json.loads(path.read_text(encoding="utf-8").strip())
            data.pop("cost_source")
            path.write_text(json.dumps(data) + "\n", encoding="utf-8")
            self.assertIsNone(log.read_days()[0].usage.cost_source)

    def test_api_adapter_computes_cost_only_when_priced(self) -> None:
        body = {"candidates": [{"content": {"parts": [{"text": '{"answer":"ok"}'}]}}],
                "usageMetadata": {"promptTokenCount": 1_000_000, "cachedContentTokenCount": 200_000,
                                  "candidatesTokenCount": 300_000, "thoughtsTokenCount": 50_000}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.txt"
            prompt.write_text("return JSON", encoding="utf-8")
            pricing = {"fake-model": Pricing(2, 4, 0.5, 8)}
            role = RoleConfig(Engine.GEMINI, "fake-model", api_key_env="GEMINI_API_KEY")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                    ScriptedHttpServer([(200, json.dumps(body).encode())]) as server:
                from dataclasses import replace
                role = replace(role, base_url=server.base_url)
                result = ApiJsonAdapter(Engine.GEMINI, pricing=pricing).invoke(role, prompt, root, 5)
            self.assertEqual(result.usage.cost_usd, 3.3)
            self.assertEqual(result.usage.cost_source, "computed")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                    ScriptedHttpServer([(200, json.dumps(body).encode())]) as server:
                role = replace(role, base_url=server.base_url)
                result = ApiJsonAdapter(Engine.GEMINI).invoke(role, prompt, root, 5)
            self.assertIsNone(result.usage.cost_usd)
            self.assertIsNone(result.usage.cost_source)


class RoleProfileTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def test_profiles_load_and_doctor_uses_selected_roles_file(self) -> None:
        profiles = self.ROOT / "config" / "profiles-roles"
        subscription = load_roles(profiles / "subscription-only.toml")
        hybrid_path = profiles / "hybrid-gemini-api.toml"
        hybrid = load_roles(hybrid_path)
        self.assertEqual(subscription["coder"].model, "gpt-6-luna")
        self.assertEqual(hybrid["reviewer"].engine, Engine.GEMINI)
        self.assertEqual(hybrid["reader"].api_key_env, "GEMINI_API_KEY")
        self.assertEqual(hybrid["coder"].engine, Engine.CODEX)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            state = root / "state"
            output = io.StringIO()
            def probe(argv: tuple[str, ...] | list[str]) -> ProcessResult:
                return ProcessResult(0, "ok", "", 0.0, False)
            code = main(["--config-dir", str(config), "--state-dir", str(state), "doctor",
                         "--roles", str(hybrid_path)], doctor_probe=probe, home=root,
                        neutral_directory=root / "neutral", out=output, err=io.StringIO())
            self.assertEqual(code, 0)
            self.assertIn("gemini: GEMINI_API_KEY not set", output.getvalue())


if __name__ == "__main__":
    unittest.main()
