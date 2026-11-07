from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))

from pipeline import FakeAdapter, git_command, make_inputs, make_pipeline, make_repository
from reona._data import loads, object_value, read_lines
from reona.config import Limits, Project, load_project
from reona.fsm import Pipeline, StageError
from reona.gates import GateRequest, plant_canary, run_gates
from reona.gitops import Git, SafetyError, ensure_safe_tree, secret_path, tree_hash
from reona.guard import OutsideGuard
from reona.state import Status


class SecretPatternsTests(unittest.TestCase):
    def test_sampledata_documents_are_allowed(self) -> None:
        for name in ("sampledata-hub.html", "SAMPLEDATA_SPECIFICATION.md",
                     "sampledata_mail_client_argon.md", "sampledata.csv", "sampledata.csv.old"):
            with self.subTest(name=name):
                self.assertFalse(secret_path(name))
                self.assertFalse(secret_path("public/" + name))

    def test_sampledata_state_names_and_backups_remain_secret(self) -> None:
        for name in ("sampledata-state.json", "storage/sampledata-state.json.bak",
                     "sampledata_state.json", "sampledata.db", "sampledata.db-wal",
                     "sampledata_credentials.json", "sampledata_token.secret", "sampledata_export.csv",
                     "sampledata_secret.json", "sampledata_dump.csv", "SampleData.DB"):
            for suffix in ("", ".bak", ".old", ".orig", "~"):
                with self.subTest(name=name, suffix=suffix):
                    self.assertTrue(secret_path(name + suffix))
                    self.assertTrue(secret_path((name + suffix).upper()))


class WorktreeExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="s5a-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = make_repository(self.directory)
        self.git = Git(self.repo)
        self.config_directory = self.directory / "configuration"
        self.config_directory.mkdir()

    def commit_names(self, *names: str) -> str:
        # Synthetic empty blobs added directly to the index: no secret file is
        # opened, hashed, or materialized in the source worktree by this fixture.
        blob = self.git.command(("hash-object", "-w", "--stdin"), stdin=b"").decode().strip()
        for name in names:
            self.git.command(("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"))
        git_command(self.repo, "commit", "-qm", "synthetic names only")
        return self.git.head()

    @contextmanager
    def no_excluded_reads(self, names: tuple[str, ...] = (".env.example",)) -> Iterator[None]:
        original_open = Path.open

        def checked_open(path: Path, *args: object, **kwargs: object) -> object:
            if secret_path(path.name) or any(path.as_posix().endswith("/" + name) for name in names):
                raise AssertionError("Prohibited file opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=checked_open):
            yield

    def pipeline(self, fake: FakeAdapter, *, exclusions: tuple[str, ...] = (".env.example",)) -> Pipeline:
        return make_pipeline(self.directory, self.repo, fake,
                             config_directory=self.config_directory,
                             exclude_from_worktree=exclusions, limits=Limits(max_fix_rounds=0))

    def test_base_secret_without_exclusion_refused_before_checkout(self) -> None:
        base = self.commit_names(".env.example")
        with self.no_excluded_reads(), self.assertRaisesRegex(SafetyError, "Forbidden secret/data path exists in base"):
            self.git.create_worktree(self.directory / "work", base)
        self.assertFalse((self.directory / "work").exists())

    def test_exact_exclusion_does_not_allow_other_secret_names(self) -> None:
        self.commit_names(".env.example")
        for name in (".env", "server.pem", "data.db", "id_rsa", "x/secret.key",
                     "nested/.env.example", ".env.example.bak", ".env/notes.txt"):
            with self.subTest(name=name):
                base = self.commit_names(name)
                source = Git(self.repo, exclude_from_worktree=(".env.example",))
                with self.no_excluded_reads(), self.assertRaises(SafetyError) as caught:
                    source.create_worktree(self.directory / "work", base)
                self.assertNotIn(name, str(caught.exception))
                self.assertFalse((self.directory / "work").exists())
                self.git.command(("update-index", "--force-remove", name))

    def test_sparse_checkout_never_materializes_exclusion_and_diff_is_empty(self) -> None:
        base = self.commit_names(".env.example")
        source = Git(self.repo, exclude_from_worktree=(".env.example",))
        work = self.directory / "work"
        original_command = Git.command
        commands: list[tuple[str, ...]] = []

        def checked_command(git: Git, arguments: Sequence[str], *,
                            environment: dict[str, str] | None = None,
                            stdin: str | bytes | None = None, allowed_exit_codes: tuple[int, ...] = (0,)) -> bytes:
            self.assertFalse(os.path.lexists(work / ".env.example"))
            output = original_command(git, arguments, environment=environment, stdin=stdin,
                                      allowed_exit_codes=allowed_exit_codes)
            self.assertFalse(os.path.lexists(work / ".env.example"))
            commands.append(tuple(arguments))
            return output

        with self.no_excluded_reads(), patch.object(Git, "command", new=checked_command):
            other = source.create_worktree(work, base)
            source.validate_base(base)
            self.assertEqual(other.command(("diff", base)), b"")
            self.assertEqual(other.command(("status", "--porcelain")), b"")
            self.assertEqual(other.diff(base).patch, b"")
            self.assertEqual(other.diff(base).paths, ())
            tree_hash(other.root, other.exclude_from_worktree)
        self.assertTrue((work / "src/value.py").is_file())
        self.assertTrue(any(args[:2] == ("worktree", "add") and "--no-checkout" in args for args in commands))
        self.assertEqual(other.command(("config", "--worktree", "--get", "core.sparseCheckout")).strip(), b"true")

    def test_literal_nested_exclusions_and_no_directory_exemptions(self) -> None:
        names = (".env.example", "storage/sampledata-state.json.template", "nested/private note.txt",
                 "nested/!note.txt", "nested/#note.txt")
        base = self.commit_names(*names, "nested/private note.txt.copy", "nested/deeper/private note.txt")
        source = Git(self.repo, exclude_from_worktree=names)
        with self.no_excluded_reads(names):
            other = source.create_worktree(self.directory / "work", base)
            self.assertEqual(other.diff(base).patch, b"")
            with other.temporary_worktree(base) as temporary:
                self.assertEqual(temporary.exclude_from_worktree, names)
                self.assertFalse(any(os.path.lexists(temporary.root / name) for name in names))
        self.assertFalse(any(os.path.lexists(other.root / name) for name in names))
        self.assertTrue((other.root / "nested/private note.txt.copy").is_file())
        self.assertTrue((other.root / "nested/deeper/private note.txt").is_file())
        with self.assertRaisesRegex(SafetyError, "must name files"):
            Git(self.repo, exclude_from_worktree=(*names, "nested")).validate_base(base)

    def test_absence_is_not_a_deletion_or_allowed_blocked_gate_violation(self) -> None:
        base = self.commit_names(".env.example", "excluded.txt")
        other = Git(self.repo, exclude_from_worktree=(".env.example", "excluded.txt")).create_worktree(
            self.directory / "work", base)
        plant_canary(other)
        (other.root / "src/value.py").write_text("VALUE = 1\n", encoding="utf-8")
        request = GateRequest(base, ("src/**",), ((sys.executable, "-c", "pass"),),
                              blocked_paths=(".env*", "excluded.txt"), tdd_gate=False,
                              tdd_gate_reason="synthetic")
        with self.no_excluded_reads((".env.example", "excluded.txt")):
            report = run_gates(other, request, self.directory / "gates.json")
            diff = other.diff(base)
            self.assertEqual(other.diff(base, ("src/value.py",)), diff)
        self.assertTrue(report.passed, report)
        self.assertEqual(diff.paths, ("src/value.py",))
        self.assertNotIn(b".env.example", diff.patch)
        self.assertNotIn(b"excluded.txt", diff.patch)

    def test_sparse_exclusions_match_casefold_even_with_source_ignorecase(self) -> None:
        self.git.command(("config", "core.ignoreCase", "true"))
        other = Git(self.repo, exclude_from_worktree=("UNCHANGED.TXT",)).create_worktree(
            self.directory / "work", self.git.head())
        self.assertFalse((other.root / "unchanged.txt").exists())
        self.assertEqual(other.diff(self.git.head()).patch, b"")
        with patch("reona.gitops.tree_entries", return_value=iter((("unchanged.txt", other.root / "unchanged.txt"),))):
            with self.assertRaisesRegex(SafetyError, "Excluded path"):
                ensure_safe_tree(other.root, other.exclude_from_worktree)
        self.assertEqual(self.git.command(("config", "--get", "core.ignoreCase")).strip(), b"true")

    def test_case_and_unicode_variants_rejected_without_filesystem_dependency(self) -> None:
        for configured, appeared in ((".env.example", ".ENV.Example"),
                                     ("excluded.txt", "EXCLUDED.TXT"),
                                     ("docs/Шаблон письма.example", "DOCS/ШАБЛОН ПИСЬМА.EXAMPLE"),
                                     ("docs/caf\u00e9.txt", "docs/CAFE\u0301.TXT")):
            with self.subTest(configured=configured), self.no_excluded_reads((configured,)), \
                    patch("reona.gitops.tree_entries", return_value=iter(((appeared, self.repo / appeared),))):
                with self.assertRaises(SafetyError):
                    ensure_safe_tree(self.repo, (configured,))

    def test_cyrillic_space_and_unicode_normalization_in_sparse_checkout_and_preflight(self) -> None:
        names = ("docs/Шаблон письма.example", "docs/cafe\u0301.txt", ".ENV.Example")
        self.commit_names(*names)
        pipeline = self.pipeline(FakeAdapter(), exclusions=("DOCS/шаблон письма.EXAMPLE",
                                                          "docs/CAF\u00c9.txt", ".env.example"))
        with self.no_excluded_reads(names):
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
            self.assertFalse(any(os.path.lexists(pipeline.git.root / name) for name in names))
            self.assertEqual(pipeline.git.diff(pipeline.state.base_sha).paths,
                             ("src/value.py", "tests/test_value.py"))
        self.assertNotIn("outside_write_detected", pipeline.events.path.read_text())

    def test_recreated_exclusion_is_refused_before_hash_or_diff(self) -> None:
        base = self.commit_names(".env.example", "excluded.txt")
        names = (".env.example", "excluded.txt")
        other = Git(self.repo, exclude_from_worktree=names).create_worktree(self.directory / "work", base)
        for name in names:
            with self.subTest(name=name):
                sentinel = other.root / name
                sentinel.touch()
                with self.no_excluded_reads(names):
                    for check in (lambda: ensure_safe_tree(other.root, names),
                                  lambda: tree_hash(other.root, names), lambda: other.diff(base)):
                        with self.assertRaises(SafetyError) as caught:
                            check()
                        self.assertNotIn(name, str(caught.exception))
                sentinel.unlink()

    def test_project_config_defaults_and_validation(self) -> None:
        path = self.config_directory / "synthetic.toml"
        prefix = f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
        path.write_text(prefix, encoding="utf-8")
        self.assertEqual(load_project(path).exclude_from_worktree, ())
        self.assertEqual(Project(self.repo, "synthetic").exclude_from_worktree, ())
        for value in ((".env.example", "nested/safe file.txt"), (),):
            path.write_text(prefix + "exclude_from_worktree=" + json.dumps(value), encoding="utf-8")
            self.assertEqual(load_project(path).exclude_from_worktree, value)
        for value in ("../x", "/abs", "a/*", "a/?", "a/[xy]", "a//b", "a/./b", "..", "",
                      "C:/abs", "C:abs", "a\\b", "a/", "a\nb", "a\rb", "a\x00b"):
            path.write_text(prefix + "exclude_from_worktree=" + json.dumps([value]), encoding="utf-8")
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "exclude_from_worktree"):
                    load_project(path)
                with self.assertRaisesRegex(ValueError, "exclude_from_worktree"):
                    Project(self.repo, "synthetic", exclude_from_worktree=(value,))
        for value in ("literal.txt", [1], None, {}):
            # TOML has no null; use a scalar bool for that boundary case.
            literal = "false" if value is None else json.dumps(value)
            path.write_text(prefix + "exclude_from_worktree=" + literal, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_project(path)

    def test_secret_exclusions_require_template_suffix_at_config_load(self) -> None:
        path = self.config_directory / "synthetic.toml"
        prefix = f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
        for suffix in (".example", ".sample", ".template", ".dist", ".tpl", ".defaults"):
            names = tuple(name + suffix.upper() for name in (".env", "server.pem", "data.db", "id_rsa"))
            path.write_text(prefix + "exclude_from_worktree=" + json.dumps(names), encoding="utf-8")
            with self.subTest(suffix=suffix):
                self.assertEqual(load_project(path).exclude_from_worktree, names)
        for name in (".env", "server.pem", "data.db", "id_rsa", "id_ed25519", "credentials",
                     "sampledata_credentials.json", "server.pem.bak", ".env.example.bak"):
            path.write_text(prefix + "exclude_from_worktree=" + json.dumps(["safe.txt", name]), encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                load_project(path)
            self.assertEqual(str(caught.exception),
                             "exclude_from_worktree[1]: secret_name_requires_template_suffix")

    def test_duplicate_case_and_nfc_collisions_are_configuration_errors(self) -> None:
        path = self.config_directory / "synthetic.toml"
        prefix = f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
        for names in ((".env.example", ".env.example"), (".env.example", ".ENV.Example"),
                      ("docs/caf\u00e9.txt", "DOCS/CAFE\u0301.TXT")):
            path.write_text(prefix + "exclude_from_worktree=" + json.dumps(names), encoding="utf-8")
            with self.subTest(names=names), self.assertRaises(ValueError) as caught:
                load_project(path)
            self.assertEqual(str(caught.exception), "exclude_from_worktree[1]: duplicate_or_colliding_path")

    def test_preflight_refuses_incompatible_local_config_before_worktree_or_agent(self) -> None:
        for key, value in (("core.worktree", "synthetic-private-value"), ("core.worktree", ""),
                           ("core.bare", "true")):
            self.git.command(("config", "--local", key, value))
            try:
                fake = FakeAdapter()
                pipeline = make_pipeline(self.directory / (key + str(len(value))), self.repo, fake,
                                         config_directory=self.config_directory,
                                         exclude_from_worktree=(".env.example",))
                self.assertEqual(pipeline.start(make_inputs()).status, Status.FAILED)
                self.assertEqual(pipeline.exit_code, 1)
                error = (pipeline.directory / "error.json").read_text()
                self.assertIn(key, error)
                self.assertNotIn("synthetic-private-value", error)
                self.assertEqual(fake.calls, [])
                self.assertFalse(pipeline.git.root.exists())
            finally:
                self.git.command(("--work-tree=" + str(self.repo), "config", "--local", "--unset", key))

    def test_preflight_requires_git_225_only_with_exclusions(self) -> None:
        original_command = Git.command

        def old_version(git: Git, arguments: Sequence[str], **kwargs: object) -> bytes:
            if tuple(arguments) == ("--version",):
                return b"git version 2.24.9.windows.1\n"
            return original_command(git, arguments, **kwargs)

        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        with patch.object(Git, "command", new=old_version):
            self.assertEqual(pipeline.start(make_inputs()).status, Status.FAILED)
            self.assertIn("Git >= 2.25", (pipeline.directory / "error.json").read_text())
            Git(self.repo).check_exclusion_support()
        self.assertFalse(pipeline.git.root.exists())
        self.assertEqual(fake.calls, [])

    def test_shared_config_change_event_and_next_agent_guard_baseline(self) -> None:
        self.commit_names(".env.example")
        before = self.git.source_config_state()
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        with self.no_excluded_reads():
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
        after = self.git.source_config_state()
        self.assertFalse(before[1])
        self.assertTrue(after[1])
        events = [object_value(event, "event") for event in read_lines(pipeline.events.path)]
        changes = [event for event in events if event["event"] == "source_repo_config_changed"]
        self.assertEqual(len(changes), 1)
        event = changes[0]
        self.assertEqual(event["category"], "source_repo_config")
        self.assertEqual(event["old_repositoryformatversion"], before[0])
        self.assertEqual(event["new_repositoryformatversion"], after[0])
        self.assertIs(event["worktreeConfig_enabled"], True)
        self.assertNotIn(str(self.repo), json.dumps(event))
        self.assertLess(events.index(event), next(index for index, item in enumerate(events)
                                                 if item["event"] == "agent_end"))
        self.assertGreaterEqual(len(fake.calls), 3)
        self.assertFalse(any(item["event"] == "outside_write_detected" for item in events))
        second = make_pipeline(self.directory / "second", self.repo, FakeAdapter(),
                               config_directory=self.config_directory, exclude_from_worktree=(".env.example",))
        with self.no_excluded_reads():
            self.assertEqual(second.start(make_inputs()).status, Status.AWAITING_HUMAN)
        self.assertNotIn("source_repo_config_changed", second.events.path.read_text())

    def test_empty_exclusions_do_not_change_shared_config_or_emit_event(self) -> None:
        before = self.git.source_config_state()
        pipeline = self.pipeline(FakeAdapter(), exclusions=())
        self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
        self.assertEqual(self.git.source_config_state(), before)
        self.assertNotIn("source_repo_config_changed", pipeline.events.path.read_text())

    def test_resume_refuses_changed_exclusions_before_agent_or_tree_access(self) -> None:
        self.commit_names(".env.example", "excluded.txt")
        pipeline = self.pipeline(FakeAdapter(), exclusions=("excluded.txt", ".ENV.Example"))
        with self.no_excluded_reads((".env.example", "excluded.txt")):
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
        saved = object_value(loads((pipeline.directory / "inputs.json").read_text()), "inputs")
        self.assertEqual(saved["exclude_from_worktree"], [".env.example", "excluded.txt"])
        for changed in ((), (".env.example",), (".env.example", "other.txt")):
            fake = FakeAdapter()
            resumed = self.pipeline(fake, exclusions=changed)
            with self.subTest(changed=changed), \
                    patch("reona.fsm.ensure_safe_tree", side_effect=AssertionError("Must refuse before tree scan")):
                with self.assertRaisesRegex(StageError, "exclude_from_worktree configuration changed"):
                    resumed.resume()
            self.assertEqual(fake.calls, [])
        resumed = self.pipeline(FakeAdapter(), exclusions=(".env.example", "EXCLUDED.TXT"))
        with self.no_excluded_reads((".env.example", "excluded.txt")):
            self.assertEqual(resumed.resume().status, Status.AWAITING_HUMAN)

    def test_source_status_does_not_hash_exclusion_but_guard_tracks_metadata(self) -> None:
        # A required clean filter fails and records any attempt by Git to hash
        # this synthetic tracked file. The script/marker live outside the repo.
        script = self.directory / "filter_probe.py"
        marker = script.with_suffix(".called")
        script.write_text("from pathlib import Path\nPath(__file__).with_suffix('.called').touch()\n"
                          "raise SystemExit(1)\n", encoding="utf-8")
        command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
        self.git.command(("config", "filter.s5a.clean", command))
        self.git.command(("config", "filter.s5a.required", "true"))
        (self.repo / ".gitattributes").write_text(".env.example filter=s5a\n", encoding="utf-8")
        git_command(self.repo, "add", ".gitattributes")
        git_command(self.repo, "commit", "-qm", "synthetic filter probe")
        self.commit_names(".env.example")
        sentinel = self.repo / ".env.example"
        sentinel.touch()
        with self.assertRaises(ValueError):
            self.git.status_counts()
        self.assertTrue(marker.is_file(), "Control status must trigger the filter")
        marker.unlink()
        pipeline = self.pipeline(FakeAdapter(), exclusions=(".ENV.Example",))
        with self.no_excluded_reads():
            self.assertEqual(pipeline.source.status_counts(), (0, 0))
            self.assertTrue(pipeline.source.clean())
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
            before = pipeline.guard.snapshot()
            info = sentinel.lstat()
            os.utime(sentinel, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            violations = pipeline.guard.compare(before, pipeline.guard.snapshot())
        self.assertFalse(marker.exists(), "Excluded source file must never enter Git's clean filter")
        self.assertEqual([(v.category, v.changed_path_count) for v in violations], [("source_repo", 1)])

    def test_fake_pipeline_with_tdd_guard_and_fresh_resume(self) -> None:
        self.commit_names(".env.example")
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        with self.no_excluded_reads():
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
            self.assertTrue(object_value(loads((pipeline.directory / "gates.json").read_text()), "gates")["passed"])
            guard = OutsideGuard(pipeline.directory, self.config_directory, pipeline.state_directory, pipeline.source)
            before = guard.snapshot()
            pipeline.git.diff(pipeline.state.base_sha)
            self.assertEqual(guard.compare(before, guard.snapshot()), ())
            resumed_fake = FakeAdapter()
            resumed = self.pipeline(resumed_fake)
            self.assertEqual(resumed.resume().status, Status.AWAITING_HUMAN)
        self.assertEqual(resumed_fake.calls, [])
        self.assertFalse(os.path.lexists(resumed.git.root / ".env.example"))
        events = [object_value(event, "event") for event in read_lines(pipeline.events.path)]
        exclusions = [event for event in events if event["event"] == "worktree_exclusions"]
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["excluded_file_count"], 1)
        self.assertNotIn(".env.example", pipeline.events.path.read_text())
        self.assertNotIn(b".env.example", (pipeline.directory / "review/diff.patch").read_bytes())

    def test_resume_interrupted_fake_pipeline_keeps_exclusion(self) -> None:
        self.commit_names(".env.example")
        interrupted = self.pipeline(FakeAdapter("interrupt"))
        with self.no_excluded_reads(), self.assertRaises(KeyboardInterrupt):
            interrupted.start(make_inputs())
        resumed = self.pipeline(FakeAdapter())
        with self.no_excluded_reads():
            self.assertEqual(resumed.resume().status, Status.AWAITING_HUMAN)
        self.assertFalse(os.path.lexists(resumed.git.root / ".env.example"))

    def test_fake_agent_recreation_fails_paths_gate_without_private_diagnostics(self) -> None:
        self.commit_names(".env.example")
        fake = FakeAdapter("excluded_secret")
        pipeline = self.pipeline(fake)
        with self.no_excluded_reads():
            self.assertEqual(pipeline.start(make_inputs()).status, Status.STOPPED)
            with self.assertRaises(SafetyError):
                self.pipeline(FakeAdapter()).resume()
        self.assertEqual([call.model for call in fake.calls], ["coder"])
        gates = object_value(loads((pipeline.directory / "gates.json").read_text()), "gates")
        self.assertFalse(gates["passed"])
        self.assertIn("Forbidden secret/data path exists in worktree", (pipeline.directory / "gates.json").read_text())
        for path in (pipeline.events.path, pipeline.directory / "gates.json", pipeline.directory / "result.json"):
            self.assertNotIn(".env.example", path.read_text())


if __name__ == "__main__":
    unittest.main()
