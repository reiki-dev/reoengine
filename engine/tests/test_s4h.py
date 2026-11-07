from __future__ import annotations

import json
import hashlib
import os
import unittest
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from unittest.mock import patch

from test_s4c import SyntheticTest, WritingAdapter
from pipeline import FakeAdapter, git_command, make_inputs, make_pipeline
from reoengine._data import read_lines
from reoengine.gitops import Git
from reoengine.guard import OutsideGuard, Snapshot, SnapshotError, source_snapshot
from reoengine.state import Status


class GitMetadataTests(SyntheticTest):
    def bump(self, path: Path) -> None:
        info = path.lstat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

    def assert_change(self, guard: OutsideGuard, action: Callable[[], object]) -> None:
        before = guard.snapshot()
        action()
        violations = guard.compare(before, guard.snapshot())
        self.assertEqual([v.category for v in violations], ["source_repo"])
        self.assertGreater(violations[0].changed_path_count, 0)

    def test_service_changes_stop_pipeline_without_private_diagnostics(self) -> None:
        gitdir = self.repo / ".git"
        hook = gitdir / "hooks/synthetic-existing"
        hook.touch()
        git_command(self.repo, "branch", "synthetic-alias")
        actions: dict[str, Callable[[], object]] = {
            "config": lambda: git_command(self.repo, "config", "--local", "reoengine.synthetic", "changed"),
            "existing-hook": lambda: hook.write_text("synthetic replacement", encoding="utf-8"),
            "new-hook": (gitdir / "hooks/synthetic-new").touch,
            "exclude": lambda: (gitdir / "info/exclude").write_bytes(
                (gitdir / "info/exclude").read_bytes() + b"synthetic-change\n"),
            "tag": lambda: git_command(self.repo, "tag", "synthetic-tag"),
            "branch": lambda: git_command(self.repo, "branch", "synthetic-branch"),
            # Same commit, different symbolic HEAD: head() alone cannot see this.
            "HEAD": lambda: git_command(self.repo, "symbolic-ref", "HEAD", "refs/heads/synthetic-alias"),
        }
        for index, (case, action) in enumerate(actions.items()):
            with self.subTest(case=case):
                def mutate() -> None:
                    action()

                fake = WritingAdapter(mutate)
                pipeline = make_pipeline(self.directory / f"case-{index}", self.repo, fake,
                                         config_directory=self.config)
                self.assertEqual(pipeline.start(make_inputs(tdd=False)).status, Status.FAILED)
                self.assertEqual(pipeline.exit_code, 1)
                error = json.loads((pipeline.directory / "error.json").read_text(encoding="utf-8"))
                self.assertEqual(error["error"], "outside_write:source_repo")
                detections = [event for event in read_lines(pipeline.events.path)
                              if isinstance(event, dict) and event.get("event") == "outside_write_detected"]
                self.assertEqual(len(detections), 1)
                event = detections[0]
                self.assertEqual(event["category"], "source_repo")
                self.assertGreater(event["changed_file_count"], 0)
                self.assertEqual([call.model for call in fake.calls], ["coder"])
                diagnostic = json.dumps(error) + json.dumps(event)
                for private in (str(self.repo), ".git", "synthetic", "hooks", "exclude", "HEAD", "refs/"):
                    self.assertNotIn(private, diagnostic)

    def test_all_service_roots_and_recursive_entries_are_metadata_only(self) -> None:
        gitdir = self.repo / ".git"
        git_command(self.repo, "pack-refs", "--all")
        git_command(self.repo, "branch", "synthetic-head-target")
        names = ("config", "config.worktree", "HEAD", "packed-refs", "info/exclude",
                 "info/attributes", "info/nested/fixture", "hooks/nested/fixture",
                 "refs/heads/nested/fixture", "refs/tags/nested/fixture")
        for name in names:
            path = gitdir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch()
        source = self.pipeline().source
        before = source_snapshot(source)
        assert before is not None
        for name in names:
            with self.subTest(name=name):
                path = gitdir / name
                info = path.lstat()
                content_hashed = (name in {"config", "config.worktree", "HEAD", "packed-refs",
                                           "info/exclude", "info/attributes"}
                                  and info.st_size <= 1_048_576)
                if name.startswith("hooks/") and "/" not in name[len("hooks/"):]:
                    content_hashed = info.st_size <= 1_048_576
                if name.startswith("info/") and "/" not in name[len("info/"):]:
                    content_hashed = info.st_size <= 1_048_576
                expected = ("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if content_hashed
                            else f"metadata:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}")
                self.assertEqual(before.paths["gitdir:common/" + name], expected)
                if not content_hashed:
                    self.bump(path)
                    after = source_snapshot(source)
                    self.assertIsNotNone(after)
                    self.assertNotEqual(before, after)
                    assert after is not None
                    before = after

    def test_git_transient_entries_and_locks_are_excluded(self) -> None:
        guard = self.pipeline().guard
        gitdir = self.repo / ".git"
        before = guard.snapshot()
        self.bump(gitdir / "index")
        for name in ("objects/ab/synthetic", "logs/synthetic", "FETCH_HEAD", "ORIG_HEAD",
                     "COMMIT_EDITMSG", "gc.log", "worktrees/synthetic/HEAD", "modules/synthetic/config",
                     "config.lock", "refs/heads/synthetic.lock", "hooks/nested.lock/fixture",
                     "info/synthetic.lock"):
            path = gitdir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.assertEqual(guard.compare(before, guard.snapshot()), ())
        # Removing a lock also changes its parent's mtime, which is excluded.
        (gitdir / "refs/heads/synthetic.lock").unlink()
        self.assertEqual(guard.compare(before, guard.snapshot()), ())

    def test_normal_engine_git_operations_are_outside_snapshot_intervals(self) -> None:
        pipeline = self.pipeline()
        between = False
        command = Git.command
        snapshot = pipeline.guard.snapshot
        operations: list[tuple[str, ...]] = []

        def take_snapshot() -> Snapshot:
            nonlocal between
            value = snapshot()
            between = not between
            return value

        def check_command(git: Git, arguments: Sequence[str], *, environment: dict[str, str] | None = None,
                          stdin: str | bytes | None = None) -> bytes:
            if arguments[0] in ("worktree", "branch", "update-ref", "symbolic-ref", "config", "gc", "prune"):
                self.assertFalse(between)
                operations.append(tuple(arguments))
            return command(git, arguments, environment=environment, stdin=stdin)

        # The full pipeline includes detached run and TDD baseline worktrees.
        with patch.object(Git, "command", check_command), \
                patch.object(pipeline.guard, "snapshot", side_effect=take_snapshot):
            self.assertEqual(pipeline.start(make_inputs()).status, Status.AWAITING_HUMAN)
        self.assertFalse(between)
        self.assertEqual(sum(operation[:2] == ("worktree", "add") for operation in operations), 2)
        self.assertTrue(all("--detach" in operation for operation in operations if operation[1] == "add"))
        self.assertEqual(sum(operation[:2] == ("worktree", "remove") for operation in operations), 1)
        self.assertFalse(self.events("outside_write_detected"))
        # Ref packing/branch changes between intervals become the next baseline.
        git_command(self.repo, "branch", "synthetic-outside")
        git_command(self.repo, "pack-refs", "--all")
        before = pipeline.guard.snapshot()
        with pipeline.source.temporary_worktree(pipeline.source.head()):
            pass
        self.assertEqual(pipeline.guard.compare(before, pipeline.guard.snapshot()), ())

    def worktree_guard(self) -> tuple[OutsideGuard, Path]:
        source = self.pipeline().source
        worktree = source.create_worktree(self.directory / "linked", source.head())
        personal = Path(os.fsdecode(worktree.command(("rev-parse", "--absolute-git-dir"))).strip())
        return OutsideGuard(self.directory / "empty-run", self.config, self.directory / "state", worktree), personal

    def test_worktree_common_and_private_metadata_are_guarded(self) -> None:
        guard, personal = self.worktree_guard()
        snapshot = guard.snapshot()
        self.assertIn("gitdir:common/config", snapshot.source.paths)
        self.assertIn("gitdir:private/HEAD", snapshot.source.paths)
        self.assert_change(guard, lambda: guard.source.command(("config", "--local", "reoengine.synthetic", "changed")))
        self.assert_change(guard, lambda: (personal / "HEAD").write_bytes(b"ref: refs/heads/master\n"))
        for name in ("config.worktree", "info/nested/fixture", "hooks/nested/fixture"):
            path = personal / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assert_change(guard, path.touch)
            if name == "config.worktree":
                self.assert_change(guard, lambda: path.write_bytes(b"changed\n"))
            else:
                self.assert_change(guard, lambda: self.bump(path))
        before = guard.snapshot()
        self.bump(personal / "index")
        self.assertEqual(guard.compare(before, guard.snapshot()), ())

    def test_git_metadata_limit_is_shared_and_fails_before_and_after_agent(self) -> None:
        source = self.pipeline().source
        snapshot = source_snapshot(source)
        assert snapshot is not None
        limit = len(snapshot.paths)
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", limit):
            self.assertEqual(snapshot, source_snapshot(source))
        with patch("reoengine.guard.MAX_IGNORED_ENTRIES", limit - 1):
            self.assertIsNone(source_snapshot(source))
        for after in (False, True):
            with self.subTest(after=after):
                fake = (WritingAdapter((self.repo / ".git/hooks/synthetic-overflow").touch)
                        if after else FakeAdapter())
                pipeline = make_pipeline(self.directory / f"limit-{after}", self.repo, fake,
                                         config_directory=self.config)
                with patch("reoengine.guard.MAX_IGNORED_ENTRIES", limit if after else limit - 1):
                    pipeline.start(make_inputs(tdd=False))
                self.assert_source_snapshot_failure(pipeline, fake, int(after))

    def test_git_metadata_access_and_partial_walk_errors_fail_closed(self) -> None:
        guard = self.pipeline().guard
        gitdir = self.repo / ".git"
        lstat, scandir = Path.lstat, os.scandir
        for target in (gitdir / "config", gitdir / "hooks", gitdir / "refs/heads"):
            for error in (PermissionError, OSError):
                def fail_stat(path: Path) -> os.stat_result:
                    if path == target:
                        raise error("synthetic private path")
                    return lstat(path)

                with self.subTest(target=target.name, error=error), patch.object(Path, "lstat", fail_stat):
                    with self.assertRaisesRegex(SnapshotError, "^outside_snapshot_unavailable:source_repo$"):
                        guard.snapshot()

        class BrokenScan:
            def __enter__(self) -> BrokenScan:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def __iter__(self) -> Iterator[os.DirEntry[str]]:
                with scandir(gitdir / "hooks") as entries:
                    yield next(entries)
                raise OSError("synthetic private path")

        def incomplete(path: Path) -> object:
            return BrokenScan() if path == gitdir / "hooks" else scandir(path)

        with patch("reoengine.guard.os.scandir", side_effect=incomplete):
            with self.assertRaisesRegex(SnapshotError, "^outside_snapshot_unavailable:source_repo$"):
                guard.snapshot()

    def assert_link_not_followed(self, junction: bool) -> None:
        target = self.directory / "external"
        target.mkdir()
        (target / "fixture").touch()
        link = self.repo / ".git/hooks/synthetic-link"
        if junction:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symbolic links unavailable on this platform")
        scandir = os.scandir

        def no_link_scan(path: Path) -> object:
            self.assertNotIn(path, (link, target))
            return scandir(path)

        source = self.pipeline().source
        with patch("reoengine.guard.os.scandir", side_effect=no_link_scan), \
                patch.object(Path, "open", side_effect=AssertionError("source content read")):
            before = source_snapshot(source)
            assert before is not None
            info = link.lstat()
            self.assertEqual(before.paths["gitdir:common/hooks/synthetic-link"],
                             f"metadata:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}")
            self.bump(target / "fixture")
            self.assertEqual(before, source_snapshot(source))
            self.assertFalse(any(name.endswith("synthetic-link/fixture") for name in before.paths))

    def test_service_symlink_is_not_followed(self) -> None:
        self.assert_link_not_followed(False)

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_service_junction_is_not_followed(self) -> None:
        self.assert_link_not_followed(True)


if __name__ == "__main__":
    unittest.main()
