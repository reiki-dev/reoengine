from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tests/fakes"))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_repository
from reona._data import Json, atomic_write, loads, object_value
from reona.adapters.base import AgentResult
from reona.config import Limits, RoleConfig, load_project
from reona.fsm import InvalidDigest, Pipeline, _effective_review_approved, review_input_digest
from reona.gates import HIGH_RISK_PATHS, risk_tier
from reona.gitops import Change, Diff
from reona.schemas import SchemaError, validate_artifact
from reona.state import Stage, Status


def make_digest(paths: tuple[str, ...]) -> dict[str, Json]:
    return {"checklist": [{"id": "R1", "satisfied": True, "evidence": "synthetic"}],
            "checks": [], "suspicious": [], "unknowns": [],
            "coverage": [{"path": path, "reviewed": True, "note": "synthetic"} for path in paths]}


class PolicyAdapter(FakeAdapter):
    def __init__(self, *, unknowns: bool = False, reviewed: bool = True,
                 bad_coverage: bool = False) -> None:
        super().__init__()
        self.unknowns = unknowns
        self.reviewed = reviewed
        self.bad_coverage = bad_coverage
        self.high_path = False

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model == "coder" and self.high_path:
            (cwd / "src/config.py").write_text("SETTING = 1\n", encoding="utf-8")
        if role_cfg.model == "reader":
            response = result.final_message_path
            if response is None:
                raise AssertionError("Missing synthetic response")
            digest = object_value(loads(response.read_text(encoding="utf-8")), "digest")
            digest.update({"risk_tier": "normal", "changed_files": ["invented"], "input_digest": "invented"})
            digest["unknowns"] = ["synthetic unknown"] if self.unknowns else []
            coverage = digest["coverage"]
            if not isinstance(coverage, list):
                raise AssertionError("Missing synthetic coverage")
            for value in coverage:
                object_value(value, "coverage")["reviewed"] = self.reviewed
            if self.bad_coverage:
                digest["coverage"] = []
            atomic_write(response, digest)
        return result


class RiskTierTests(unittest.TestCase):
    def test_every_builtin_pattern(self) -> None:
        examples = ("adapters/reader.py", "engine/gates.py", "guard.py", "engine/gitops.py", "procs.py",
                    "engine/fsm.py", "config.py", "engine/schemas/review.json", "config/project.json",
                    "pyproject.toml", ".github/workflows/test.yml", "src/authentication.py", "src/auth/login.ts",
                    "security.py", "src/security/policy.ts", "src/mysecret.txt", "src/lib/secrets/x.ts",
                    "src/tokens.py", "src/db/token/store.ts", "package.json", "package-lock.json", "pnpm-lock.yaml",
                    "yarn.lock", "poetry.lock", "uv.lock", "db/migrations/001.py", "query.sql",
                    "Dockerfile", "docker-compose.yml", "compose.yaml", ".gitlab-ci.yml", "Jenkinsfile",
                    ".circleci/config.yml", "bitbucket-pipelines.yml", "Cargo.lock", "go.mod", "go.sum",
                    "requirements.txt", "Pipfile", "setup.py", "setup.cfg", "deploy.sh", "deploy.ps1",
                    "Makefile", ".env", "id.pem", "id.key", ".gitmodules", ".gitattributes")
        self.assertEqual(len(examples), len(HIGH_RISK_PATHS))
        for path in examples:
            with self.subTest(path=path):
                self.assertEqual(risk_tier((path,)), "high")

    def test_nested_high_risk_paths(self) -> None:
        paths = ("engine/reona/adapters/agy.py", "src/api/package.json", "sub/package-lock.json",
                 "sub/pnpm-lock.yaml", "sub/yarn.lock", "sub/poetry.lock", "sub/uv.lock",
                 "src/auth/login.ts", "src/security/policy.ts", "src/lib/secrets/x.ts",
                 "src/db/token/store.ts", "a/b/config/x.json", "a/.github/workflows/ci.yml")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(risk_tier((path,)), "high")

    def test_normal_paths_remain_normal(self) -> None:
        for path in ("README.md", "docs/a.md", "src/app.ts"):
            with self.subTest(path=path):
                self.assertEqual(risk_tier((path,)), "normal")

    def test_auth_prefix_conservatively_includes_authors(self) -> None:
        self.assertEqual(risk_tier(("src/authors.md",)), "high")

    def test_normal_project_additions_tdd_and_normalization(self) -> None:
        self.assertEqual(risk_tier(("docs/README.md",)), "normal")
        self.assertEqual(risk_tier(("docs/README.md",), high_risk_paths=("docs/**",)), "high")
        self.assertEqual(risk_tier(("fsm.py",), high_risk_paths=("docs/**",)), "high")
        self.assertEqual(risk_tier(("docs/README.md",)), "normal")
        self.assertEqual(risk_tier(("docs/README.md",), tdd_gate=False), "high")
        self.assertEqual(risk_tier(("ENGINE\\FSM.PY",)), "high")
        self.assertEqual(risk_tier(("src/cafe\u0301.py",), high_risk_paths=("src/café.py",)), "high")
        self.assertEqual(risk_tier(("src/plain.py",)), "normal")

    def test_deletions_and_both_rename_sides(self) -> None:
        for change in (Change("D", ("fsm.py",)), Change("R100", ("fsm.py", "docs/plain.md")),
                       Change("R100", ("docs/plain.md", "fsm.py"))):
            with self.subTest(change=change):
                self.assertEqual(risk_tier(Diff(b"", (change,)).paths), "high")

    def test_input_digest_is_canonical_stable_and_input_sensitive(self) -> None:
        task: dict[str, Json] = {"id": "SYN-1", "title": "Synthetic"}
        diff = hashlib.sha256(b"synthetic diff").hexdigest()
        original = review_input_digest(diff, "gates", ("b", "a"), task)
        self.assertEqual(original, review_input_digest(diff, "gates", ("a", "b"), dict(reversed(list(task.items())))))
        self.assertEqual(len(original), 64)
        self.assertNotEqual(original, review_input_digest(hashlib.sha256(b"changed").hexdigest(), "gates", ("a", "b"), task))
        self.assertNotEqual(original, review_input_digest(diff, "other gates", ("a", "b"), task))
        self.assertNotEqual(original, review_input_digest(diff, "gates", ("a",), task))
        self.assertNotEqual(original, review_input_digest(diff, "gates", ("a", "b"), {**task, "id": "SYN-2"}))


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="s6-review-", dir=ENGINE / "tests")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = make_repository(self.directory)
        self.fake = PolicyAdapter()
        self.pipeline = make_pipeline(self.directory, self.repo, self.fake, limits=Limits(max_fix_rounds=0))

    def start(self, *, high: bool = False, tdd: bool = True) -> None:
        if high:
            self.pipeline.project = replace(self.pipeline.project, high_risk_paths=("src/**",))
        self.pipeline.start(make_inputs(tdd=tdd))

    def digest(self) -> dict[str, Json]:
        return object_value(loads((self.pipeline.directory / "review/review-digest.json").read_text(
            encoding="utf-8")), "digest")

    def assert_rejected(self, reason: str) -> None:
        self.assertFalse(list((self.pipeline.directory / "checkpoints").glob("accept-*.json")))
        self.assertEqual(self.pipeline.state.stage, Stage.FIX_LOOP)
        findings = self.pipeline._feedback()
        self.assertTrue(findings)
        finding = object_value(findings[-1], "finding")
        self.assertEqual(finding["severity"], "high")
        self.assertIsNone(finding["line"])
        self.assertIn("incomplete high-risk review: " + reason, str(finding["text"]))

    def test_model_cannot_lower_risk_or_forge_binding_fields(self) -> None:
        self.fake.high_path = True
        self.start()
        self.assertEqual(self.pipeline.state.stage, Stage.ACCEPT)
        digest = self.digest()
        self.assertEqual(digest["risk_tier"], "high")
        diff = self.pipeline.git.diff(self.pipeline.state.base_sha)
        self.assertEqual(digest["changed_files"], sorted(diff.paths))
        binding = self.pipeline._review_binding(diff)
        self.assertEqual(digest["input_digest"], binding["input_digest"])
        saved = object_value(loads((self.pipeline.directory / "review/binding.json").read_text()), "binding")
        self.assertEqual(saved, binding)
        self.assertEqual(binding["diff_sha256"], hashlib.sha256(diff.patch).hexdigest())
        self.assertEqual(binding["gates_sha256"], hashlib.sha256(self.pipeline._artifact(Stage.GATES).read_bytes()).hexdigest())
        reviewer = next(call for call in self.fake.calls if call.model == "reviewer")
        self.assertIn('"risk_tier":"high"', reviewer.prompt)
        self.assertIn('"input_digest":', reviewer.prompt)
        self.assertIn('"coverage":', reviewer.prompt)
        self.assertIn('"diff":', reviewer.prompt)

    def test_high_unknowns_rejected_despite_approval(self) -> None:
        self.fake.unknowns = True
        self.start(high=True)
        self.assert_rejected("unknowns remain")

    def test_high_unreviewed_rejected_despite_approval(self) -> None:
        self.fake.reviewed = False
        self.start(high=True)
        self.assert_rejected("files remain unreviewed")

    def test_tdd_disabled_unknowns_rejected(self) -> None:
        self.fake.unknowns = True
        self.start(tdd=False)
        self.assert_rejected("unknowns remain")

    def test_normal_unknowns_can_reach_accept(self) -> None:
        self.fake.unknowns = True
        self.start()
        self.assertEqual(self.digest()["risk_tier"], "normal")
        self.assertEqual(self.pipeline.state.status, Status.AWAITING_HUMAN)
        self.assertTrue(list((self.pipeline.directory / "checkpoints").glob("accept-*.json")))

    def test_invalid_coverage_uses_existing_retry(self) -> None:
        self.fake.bad_coverage = True
        self.start()
        self.assertEqual(self.pipeline.state.status, Status.FAILED)
        self.assertEqual([call.model for call in self.fake.calls], ["coder", "reader", "reader"])

    def test_missing_extra_duplicate_coverage_raise_invalid_digest(self) -> None:
        self.pipeline._set_inputs(make_inputs())
        atomic_write(self.pipeline._artifact(Stage.GATES), {"checks": []})
        paths = ("src/value.py", "tests/test_value.py")
        diff = Diff(b"synthetic", (Change("M", paths),))
        for supplied in (paths[:1], (*paths, "extra.py"), (*paths, paths[0])):
            with self.subTest(paths=supplied), self.assertRaisesRegex(InvalidDigest, "coverage"):
                self.pipeline._validate_digest(make_digest(supplied), diff)

    def test_schema_requires_coverage_and_forbids_extra_properties(self) -> None:
        digest = make_digest(())
        digest.update({"risk_tier": "normal", "input_digest": "synthetic", "changed_files": []})
        validate_artifact("review-digest", digest)
        del digest["coverage"]
        with self.assertRaisesRegex(SchemaError, "coverage"):
            validate_artifact("review-digest", digest)
        with self.assertRaises(SchemaError):
            validate_artifact("review-digest", {**digest, "coverage": [], "extra": True})

    def test_binding_changes_before_review_decision_route_to_fix_loop(self) -> None:
        perform = self.pipeline._perform

        def change_after_review(stage: Stage) -> dict[str, Json]:
            output = perform(stage)
            if stage == Stage.REVIEW:
                (self.pipeline.git.root / "src/value.py").write_text("VALUE = 2\n", encoding="utf-8")
            return output

        with patch.object(self.pipeline, "_perform", side_effect=change_after_review):
            self.start()
        self.assert_rejected("review binding does not match current input")

    def test_gates_changes_before_review_decision_route_to_fix_loop(self) -> None:
        perform = self.pipeline._perform

        def change_after_review(stage: Stage) -> dict[str, Json]:
            output = perform(stage)
            if stage == Stage.REVIEW:
                gates = self.pipeline._load(Stage.GATES)
                gates["passed"] = False
                atomic_write(self.pipeline._artifact(Stage.GATES), gates)
            return output

        with patch.object(self.pipeline, "_perform", side_effect=change_after_review):
            self.start()
        self.assert_rejected("review binding does not match current input")

    def test_next_rechecks_high_risk_completeness(self) -> None:
        self.start(high=True)
        digest = self.digest()
        digest["unknowns"] = ["unanswered suspicion"]
        atomic_write(self.pipeline.directory / "review/review-digest.json", digest)
        self.assertEqual(self.pipeline._next(Stage.REVIEW), Stage.FIX_LOOP)
        self.assertIn("unknowns remain", str(self.pipeline._feedback()))

    def test_project_config_loads_optional_additive_patterns(self) -> None:
        path = self.directory / "project.toml"
        contents = 'repo = "' + self.repo.as_posix() + '"\nprofile = "synthetic"\n'
        path.write_text(contents, encoding="utf-8")
        self.assertEqual(load_project(path).high_risk_paths, ())
        path.write_text(contents + 'high_risk_paths = ["docs/**"]\n', encoding="utf-8")
        project = load_project(path)
        self.assertEqual(project.high_risk_paths, ("docs/**",))
        self.assertEqual(risk_tier(("docs/README.md",)), "normal")
        self.assertEqual(risk_tier(("docs/README.md",), high_risk_paths=project.high_risk_paths), "high")

    def test_infrastructure_and_supply_chain_paths_are_high(self) -> None:
        high = ("Dockerfile", "deploy/Dockerfile", "docker-compose.yml", "compose.yaml", ".gitlab-ci.yml",
                "Jenkinsfile", ".circleci/config.yml", "bitbucket-pipelines.yml", "Cargo.lock", "sub/go.sum",
                "go.mod", "requirements-dev.txt", "Pipfile", "Gemfile.lock", "scripts/deploy.ps1",
                "deploy.sh", "Makefile", "setup.py", "setup.cfg", "a/.env", "x/id.key", "k.pem",
                ".gitmodules", ".gitattributes")
        normal = ("README.md", "docs/a.md", "src/app.ts", "tests/test_foo.py", ".env.example", "ci.yml")
        for path in high:
            with self.subTest(path=path):
                self.assertEqual(risk_tier((path,)), "high")
        for path in normal:
            with self.subTest(path=path):
                self.assertEqual(risk_tier((path,)), "normal")

    def test_pure_approval_still_requires_complete_paths(self) -> None:
        review: dict[str, Json] = {"approved": True, "summary": "synthetic", "findings": []}
        digest: dict[str, Json] = {**make_digest(("src/value.py",)), "changed_files": ["src/value.py"],
                                   "risk_tier": "normal"}
        self.assertTrue(_effective_review_approved(review, digest))
        digest["coverage"] = []
        self.assertFalse(_effective_review_approved(review, digest))


if __name__ == "__main__":
    unittest.main()
