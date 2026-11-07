from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import cast

from .. import fsm
from .._data import Json, atomic_write, encode, loads, object_value, string
from ..events import EventKind, sha256_file
from ..gates import diff_changed_lines, risk_tier
from ..gitops import Diff
from ..review_policy import (_diff_sections, _effective_review_approved, _excerpt_in_section,
                             _incomplete_review, _line_in_section, review_input_digest)
from ..schemas import validate_artifact
from ..skills import select_skills
from ..state import Stage, Status
from ..fsm import InvalidDigest, StageError
from ..prompt import Prompt
from ._base import StageContext

class ReviewMixin(StageContext):
        def _review(self) -> dict[str, Json]:
            review_directory = self.directory / "review"
            review_directory.mkdir(parents=True, exist_ok=True)
            diff = self.git.diff(self.state.base_sha)
            if diff_changed_lines(diff.patch) > self.config.limits.review_max_diff_lines:
                return self._result(Status.STOPPED, stop_reason="diff_too_large")
            binding = self._review_binding(diff)
            (review_directory / "diff.patch").write_bytes(diff.patch)
            data: dict[str, Json] = {"task": self.inputs.task, "allowed_paths": list(self._allowed()),
                                     "gates": self._load(Stage.GATES), "diff": diff.patch.decode("utf-8", errors="replace"),
                                     "changed_files": list(diff.paths),
                                     "instruction": "Return review-digest JSON: requirement checklist, suspicious excerpts, unknowns. "
                                                    "Return checks as an empty array; the engine supplies authoritative gate results. "
                                                    "Include coverage {path, reviewed, note} for every changed_files path exactly once. "
                                                    "Report unknowns honestly, including unanswered suspicions and review limitations. "
                                                    "Excerpts must be nonblank literal fragments within the named file's diff section; "
                                                    "leading +, - or space diff markers are optional. Renames may use either path. "
                                                    "Invalid UTF-8 bytes are represented by replacement characters."}
            try:
                # Check the complete envelope before spending a reader invocation.
                cast(Callable[..., Prompt], fsm.__dict__["build_prompt"])(data, base_sha=self.state.base_sha, allowed_paths=self._allowed(),
                             profile=self.profile, limits=self.config.limits,
                             skills=select_skills(self.profile, self.catalog), json_contract=True)
            except ValueError as exc:
                raise StageError("review_input_too_large") from exc
            digest = self._invoke("reader", data, "review-digest",
                                  validator=lambda value: self._validate_digest(value, diff, binding=binding))
            atomic_write(review_directory / "review-digest.json", digest)
            atomic_write(review_directory / "binding.json", binding)
            if binding["review_mode"] == "full_diff":
                reviewer_data: Json = {"digest": digest, "diff": diff.patch.decode("utf-8", errors="replace"),
                                       "instruction": "Review the digest against the full diff; the diff is authoritative."}
                self._event(EventKind.REVIEW_MODE,
                            note=f"full_diff:{binding['review_reason']}")
                try:
                    review = self._invoke("reviewer", reviewer_data, "review", neutral=True,
                                          read_only_full_diff=True)
                except ValueError as exc:
                    if "max_prompt_bytes" in str(exc):
                        raise StageError("review_input_too_large") from exc
                    raise
            else:
                reviewer_data = digest
                self._event(EventKind.REVIEW_MODE, note="digest:normal")
                review = self._invoke("reviewer", reviewer_data, "review", neutral=True)
            atomic_write(self.directory / "review.json", review)
            if not _effective_review_approved(review, digest):
                self._review_feedback(review, _incomplete_review(digest))
            return review

        def _review_binding(self, diff: Diff) -> dict[str, Json]:
            diff_sha256 = hashlib.sha256(diff.patch).hexdigest()
            gates_sha256 = sha256_file(self._artifact(Stage.GATES))
            path_tier = risk_tier(diff.paths, high_risk_paths=self.project.high_risk_paths,
                                  tdd_gate=self.inputs.task["tdd_gate"] is True)
            diff_lines = diff_changed_lines(diff.patch)
            reason = ("high_risk_path" if path_tier == "high" else
                      "diff_size" if diff_lines > self.config.limits.review_full_diff_lines else "normal")
            tier = risk_tier(diff.paths, high_risk_paths=self.project.high_risk_paths,
                             tdd_gate=self.inputs.task["tdd_gate"] is True, diff_patch=diff.patch,
                             review_full_diff_lines=self.config.limits.review_full_diff_lines)
            return {"input_digest": review_input_digest(diff_sha256, gates_sha256, diff.paths, self.inputs.task),
                    "risk_tier": tier, "review_mode": "full_diff" if tier == "high" else "digest",
                    "review_reason": reason,
                    "base_sha": self.state.base_sha, "diff_sha256": diff_sha256, "gates_sha256": gates_sha256}

        def _review_verification(self) -> tuple[dict[str, Json] | None, str | None]:
            try:
                digest = object_value(loads((self.directory / "review/review-digest.json").read_text(
                    encoding="utf-8")), "digest")
                binding = object_value(loads((self.directory / "review/binding.json").read_text(
                    encoding="utf-8")), "binding")
                diff = self.git.diff(self.state.base_sha)
                current = self._review_binding(diff)
                reason = ("review binding does not match current input" if binding != current
                          or any(digest.get(key) != current[key] for key in ("input_digest", "risk_tier"))
                          or digest.get("changed_files") != sorted(diff.paths)
                          else _incomplete_review(digest))
                return (digest, reason)
            except (OSError, ValueError):
                return (None, "review binding or digest unavailable")

        def _review_feedback(self, review: Mapping[str, Json], reason: str | None) -> None:
            findings = review.get("findings")
            feedback = list(findings) if isinstance(findings, list) else []
            if reason is not None:
                feedback.append({"path": "", "line": None, "severity": "high",
                                 "text": "incomplete high-risk review: " + reason})
            atomic_write(self.directory / "feedback.json", feedback)

        def _supply_digest_checks(self, digest: dict[str, Json]) -> None:
            # Deep copy: digest compaction must never mutate the gate checkpoint.
            digest["checks"] = loads(encode(self._load(Stage.GATES)["checks"]).decode("utf-8"))
            if len(encode(digest)) / 4 > 12000 and isinstance(digest["checks"], list):
                for value in digest["checks"]:
                    check = object_value(value, "gate check")
                    for field in ("stdout_tail", "stderr_tail"):
                        tail = check.get(field)
                        if isinstance(tail, str):
                            check[field] = tail[-400:]
            if len(encode(digest)) / 4 > 12000:
                raise InvalidDigest("review_digest_too_large")

        def _validate_digest(self, digest: dict[str, Json], diff: Diff, *,
                             binding: Mapping[str, Json] | None = None) -> None:
            """Validate semantic evidence in the same retry as schema/extraction."""
            binding = binding if binding is not None else self._review_binding(diff)
            digest["risk_tier"] = binding["risk_tier"]
            digest["input_digest"] = binding["input_digest"]
            digest["changed_files"] = [path for path in sorted(diff.paths)]
            self._supply_digest_checks(digest)
            validate_artifact("review-digest", digest)
            coverage = digest.get("coverage")
            paths = [string(object_value(item, "coverage")["path"], "path")
                     for item in coverage] if isinstance(coverage, list) else []
            if (not isinstance(coverage, list) or len(paths) != len(set(paths))
                    or len(paths) != len(diff.paths) or set(paths) != set(diff.paths)):
                raise InvalidDigest("Digest coverage must contain every changed path exactly once")
            expected = [string(object_value(item, "requirement")["id"], "requirement.id")
                        for item in self.inputs.task["requirements"]] if isinstance(self.inputs.task["requirements"], list) else []
            checklist = digest["checklist"]
            actual = [string(object_value(item, "checklist")["id"], "id") for item in checklist] if isinstance(checklist, list) else []
            if (not isinstance(checklist, list) or len(actual) != len(set(actual))
                    or len(actual) != len(expected) or set(actual) != set(expected)):
                raise InvalidDigest("Digest does not cover task/check results")
            suspicious = digest["suspicious"]
            sections = _diff_sections(diff.patch)
            if isinstance(suspicious, list):
                for value in suspicious:
                    finding = object_value(value, "suspicious")
                    excerpt, path, line = finding["excerpt"], finding["path"], finding["line"]
                    if (not isinstance(excerpt, str) or not isinstance(path, str) or path not in diff.paths
                            or not any(_excerpt_in_section(excerpt, section) for section in sections.get(path, ()))):
                        raise InvalidDigest("Digest excerpt/path is not in the named file's diff section")
                    if (not isinstance(line, int) or isinstance(line, bool)
                            or not any(_excerpt_in_section(excerpt, section) and _line_in_section(line, excerpt, section)
                                       for section in sections.get(path, ()))):
                        raise InvalidDigest("Digest line does not match the first excerpt line inside a hunk of the named file's diff section")

