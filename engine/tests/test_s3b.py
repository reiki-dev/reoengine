from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))

from pipeline import FakeAdapter, git_command, make_inputs, make_pipeline, make_repository
from reona._data import Json, atomic_write, object_value, read_lines
from reona.config import Project, load_project
from reona.gc import collect_run_links
from reona.gitops import Git, SafetyError, ensure_safe_tree, secret_path, tree_hash
from reona.runlinks import ensure_run_links, is_link, remove_run_links
from reona.state import RunLock, Stage, Status, save_state


class S3bTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="s3b-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = make_repository(self.directory)
        self.git = Git(self.repo)

    def test_ignored_source_secrets_are_not_opened_or_walked(self) -> None:
        (self.repo / ".gitignore").write_text(".env\nstorage/\nnode_modules/\n", encoding="utf-8")
        git_command(self.repo, "add", ".gitignore")
        git_command(self.repo, "commit", "-qm", "synthetic ignores")
        (self.repo / ".env").touch()
        (self.repo / "storage").mkdir()
        (self.repo / "storage/x.db").touch()
        (self.repo / "node_modules").mkdir()
        pipeline = make_pipeline(self.directory, self.repo, FakeAdapter())
        pipeline._set_inputs(make_inputs())
        original_open, original_iterdir = Path.open, Path.iterdir

        def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
            if path.is_relative_to(self.repo) and secret_path(path.relative_to(self.repo).as_posix()):
                raise AssertionError("Source secret opened")
            return original_open(path, *args, **kwargs)

        def guarded_iterdir(path: Path) -> object:
            if path.is_relative_to(self.repo):
                raise AssertionError("Source worktree traversed")
            return original_iterdir(path)

        with patch.object(Path, "open", new=guarded_open), patch.object(Path, "iterdir", new=guarded_iterdir):
            pipeline._prepare()
        self.assertEqual(pipeline.state.base_sha, self.git.head())
        self.assertFalse((pipeline.git.root / ".env").exists())
        self.assertFalse((pipeline.git.root / "storage").exists())
        self.assertFalse((pipeline.git.root / "node_modules").exists())
        self.assertNotIn("source_dirty", [object_value(event, "event")["event"]
                                         for event in read_lines(pipeline.events.path)])

    def test_committed_secret_refused_before_checkout(self) -> None:
        blob = self.git.command(("hash-object", "-w", "--stdin"), stdin=b"").decode().strip()
        self.git.command(("update-index", "--add", "--cacheinfo", f"100644,{blob},.env"))
        git_command(self.repo, "commit", "-qm", "synthetic secret name only")
        pipeline = make_pipeline(self.directory, self.repo, FakeAdapter())
        pipeline._set_inputs(make_inputs())
        with self.assertRaises(SafetyError):
            pipeline._prepare()
        self.assertFalse(pipeline.git.root.exists())

    def test_status_counts_staged_unstaged_rename_delete_and_untracked_files(self) -> None:
        (self.repo / "deleted.txt").write_text("synthetic", encoding="utf-8")
        git_command(self.repo, "add", "deleted.txt")
        git_command(self.repo, "commit", "-qm", "synthetic deletion base")
        git_command(self.repo, "mv", "unchanged.txt", "private name.txt")
        (self.repo / "src/value.py").write_text("VALUE = 2\n", encoding="utf-8")
        git_command(self.repo, "add", "src/value.py")
        (self.repo / "src/value.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "untracked").mkdir()
        (self.repo / "untracked/one.txt").touch()
        (self.repo / "untracked/two.txt").touch()
        self.assertEqual(self.git.status_counts(), (3, 2))

    def test_dirty_preflight_records_only_counts_and_pins_head(self) -> None:
        base = self.git.head()
        (self.repo / "src/value.py").write_text("PRIVATE_UNCOMMITTED_EDIT", encoding="utf-8")
        (self.repo / "private untracked.txt").touch()
        (self.repo / "another untracked.txt").touch()
        pipeline = make_pipeline(self.directory, self.repo, FakeAdapter())
        execute = pipeline.execute_stage

        def interrupt(stage: Stage) -> dict[str, Json]:
            output = execute(stage)
            if stage == Stage.PREFLIGHT:
                raise KeyboardInterrupt()
            return output

        with patch.object(pipeline, "execute_stage", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            pipeline.start(make_inputs())
        self.assertEqual(pipeline.state.base_sha, base)
        self.assertEqual((pipeline.git.root / "src/value.py").read_text(), "VALUE = 0\n")
        events = [object_value(event, "event") for event in read_lines(pipeline.events.path)]
        warning = [event for event in events if event["event"] == "source_dirty"]
        self.assertEqual(len(warning), 1)
        self.assertEqual((warning[0]["changed_file_count"], warning[0]["untracked_file_count"]), (1, 2))
        text = pipeline.events.path.read_text()
        self.assertNotIn("private untracked", text)
        self.assertNotIn("another untracked", text)
        self.assertNotIn("PRIVATE_UNCOMMITTED_EDIT", text)
        git_command(self.repo, "add", "src/value.py")
        git_command(self.repo, "commit", "-qm", "synthetic later head")
        self.assertEqual(pipeline.resume().base_sha, base)

    def test_project_links_validation(self) -> None:
        target = self.directory / "target"
        target.mkdir()
        config = self.directory / "project.toml"
        prefix = f'repo={json.dumps(str(self.repo))}\nprofile="synthetic"\n'
        config.write_text(prefix + f'[links]\nnode_modules={json.dumps(str(target))}\n', encoding="utf-8")
        self.assertEqual(load_project(config).links, {"node_modules": target})
        for name in ("", ".", "..", "../x", "x/y", "x\\y", "x..y", "C:x", "worktree", "RUN.LOCK", "node_modules."):
            with self.subTest(name=name):
                config.write_text(prefix + f'[links]\n{json.dumps(name)}={json.dumps(str(target))}\n', encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_project(config)
                with self.assertRaises(ValueError):
                    Project(self.repo, "synthetic", links={name: target})
        (self.directory / "file.txt").touch()
        for value in ("relative", str(self.directory / "missing"), str(self.directory / "file.txt")):
            config.write_text(prefix + f'[links]\nnode_modules={json.dumps(value)}\n', encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_project(config)

    def test_links_outside_worktree_resume_gc_and_target_survival(self) -> None:
        target = self.directory / "target space & %literal% ! кириллица"
        target.mkdir()
        marker = target / "synthetic.txt"
        marker.write_text("synthetic", encoding="utf-8")
        fake = FakeAdapter()
        pipeline = make_pipeline(self.directory, self.repo, fake)
        pipeline.project = replace(pipeline.project, links={"node_modules": target})
        self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
        link = pipeline.directory / "node_modules"
        self.assertTrue(is_link(link))
        self.assertEqual(link.resolve(), target.resolve())
        self.assertFalse((pipeline.git.root / "node_modules").exists())
        ensure_safe_tree(pipeline.git.root)
        before = tree_hash(pipeline.git.root)
        diff = pipeline.git.diff(pipeline.state.base_sha)
        marker.write_text("changed synthetic target", encoding="utf-8")
        self.assertEqual(tree_hash(pipeline.git.root), before)
        self.assertEqual(pipeline.git.diff(pipeline.state.base_sha), diff)
        self.assertNotIn(b"node_modules", diff.patch)
        self.assertEqual(next(call.cwd for call in fake.calls if call.model == "coder"), pipeline.git.root)
        calls = len(fake.calls)
        self.assertEqual(pipeline.resume().status, Status.AWAITING_HUMAN)
        self.assertEqual(len(fake.calls), calls)
        with RunLock(pipeline.directory):
            self.assertEqual(collect_run_links(self.directory), 0)
        self.assertTrue(is_link(link))
        self.assertEqual(collect_run_links(self.directory), 1)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(marker.is_file())
        self.assertEqual(pipeline.resume().status, Status.AWAITING_HUMAN)
        self.assertTrue(is_link(link))
        self.assertEqual(len(fake.calls), calls)
        with RunLock(pipeline.directory):
            self.assertEqual(remove_run_links(pipeline.directory), 1)
            self.assertEqual(remove_run_links(pipeline.directory), 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "changed synthetic target")

    def test_cleanup_broken_link_and_gc_skips_running_run(self) -> None:
        target = self.directory / "target"
        target.mkdir()
        run = self.directory / "run"
        run.mkdir()
        ensure_run_links(run, {"node_modules": target})
        from reona.state import RunState
        save_state(run, RunState(run.name, stage=Stage.IMPLEMENT, status=Status.RUNNING))
        self.assertEqual(collect_run_links(self.directory), 0)
        target.rmdir()
        with RunLock(run):
            self.assertEqual(remove_run_links(run), 1)
        self.assertFalse(os.path.lexists(run / "node_modules"))

    def test_links_refuse_existing_directory_or_wrong_target(self) -> None:
        target = self.directory / "target"
        target.mkdir()
        run = self.directory / "run"
        run.mkdir()
        (run / "node_modules").mkdir()
        with self.assertRaises(ValueError):
            ensure_run_links(run, {"node_modules": target})
        (run / "node_modules").rmdir()
        ensure_run_links(run, {"node_modules": target})
        other = self.directory / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            ensure_run_links(run, {"node_modules": other})
        # Cleanup must also refuse to recurse if a link has been replaced.
        remove_run_links(run)
        (run / "node_modules").mkdir()
        (run / "node_modules/keep.txt").touch()
        with self.assertRaises(ValueError):
            remove_run_links(run)
        self.assertTrue((run / "node_modules/keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
