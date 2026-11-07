from __future__ import annotations

from pathlib import Path

from .._data import Json, atomic_write, loads, object_value, read_lines
from ..events import EventKind, sha256_file
from ..gates import sanitize_output
from ..schemas import validate_artifact
from ..state import RunLock, Stage, Status, load_state
from ..usage import TaskOutcome
from ..review_policy import _effective_review_approved, _review_approved
from ..fsm import RunInputs, StageError
from ._base import StageContext

class AcceptMixin(StageContext):
        def _record_acceptance(self) -> None:
            records = [record for record in self.usage.read_days() if record.run_id == self.state.run_id]
            seen: set[tuple[str, str, str, str]] = set()
            model_set: list[dict[str, str]] = []
            for record in records:
                key = (record.role, record.engine.value, record.model, record.effort.value)
                if key not in seen:
                    seen.add(key)
                    model_set.append({"role": record.role, "engine": record.engine.value,
                                      "model": record.model, "effort": record.effort.value})
            outcome = TaskOutcome(self.state.run_id, self.state.attempt, model_set=model_set)
            self.outcomes.append(outcome)
            already_emitted = False
            if self.events.path.exists():
                already_emitted = any(
                    isinstance(ev, dict) and ev.get("event") == EventKind.TASK_ACCEPTED.value
                    for ev in read_lines(self.events.path)
                )
            if not already_emitted:
                self._event(EventKind.TASK_ACCEPTED)

        def _result(self, status: Status, *, trust_artifacts: bool = True,
                    stop_reason: str | None = None) -> dict[str, Json]:
            gates_path = self.directory / "gates.json"
            isolation = self.config.limits.isolation.mode
            gate_isolation = "wrapper" if isolation == "wrapper" else "env-only"
            gates: dict[str, Json] = (object_value(loads(gates_path.read_text(encoding="utf-8")), "gates")
                                      if trust_artifacts and gates_path.exists() else {
                                          "passed": False, "checks": [], "isolation": gate_isolation})
            review_path = self.directory / "review.json"
            review = loads(review_path.read_text(encoding="utf-8")) if trust_artifacts and review_path.exists() else None
            records = [record for record in self.usage.read_days() if record.run_id == self.state.run_id]
            summary: dict[str, Json] = {}
            for metric in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "cost_usd"):
                values = [record.usage.to_json()[metric] for record in records]
                numbers = [value for value in values if type(value) in (int, float) and isinstance(value, (int, float))]
                summary[metric] = sum(numbers) if len(numbers) == len(values) and numbers else None
            try:
                changed: list[Json] = list(self.git.diff(self.state.base_sha).paths) if self.state.base_sha else []
            except (OSError, ValueError):
                changed = []
            try:
                head = self.git.head() if self.git.root.exists() else ""
            except (OSError, ValueError):
                head = self.state.head_sha
            result_warnings: list[Json] = ([] if isolation == "wrapper" else [
                "checks ran with environment filtering only (no network/filesystem isolation)",
                *(f"link {name} gives the agent write access to a host directory"
                  for name in self.project.links)])
            result: dict[str, Json] = {"run_id": self.state.run_id, "status": status.value,
                                       "base_sha": self.state.base_sha, "head_sha": head,
                                       "changed_files": changed,
                                       "warnings": result_warnings, "gates": gates, "review": review,
                                       "attempts": self.state.attempt,
                                       "usage_summary": summary,
                                       "artifacts": [name for name in ("review/diff.patch", "gates.json", "review.json")
                                                     if (self.directory / name).is_file()]}
            if stop_reason is not None:
                result["stop_reason"] = stop_reason
            validate_artifact("result", result)
            atomic_write(self.directory / "result.json", result)
            return result

        def approve(self) -> dict[str, Json]:
            with RunLock(self.directory):
                self.state = load_state(self.directory)
                if self.state.stage == Stage.DONE or self.state.status == Status.COMPLETED:
                    raise StageError(f"approve refused: run already completed ({self.state.status.value})")
                if not (self.state.stage == Stage.ACCEPT and self.state.status == Status.AWAITING_HUMAN):
                    raise StageError(f"approve refused: run is in {self.state.stage.value}/{self.state.status.value}, expected accept/awaiting_human")
                data = self._verify_run_integrity(action="approve")
                if not self.inputs.task:
                    plan = None if data["plan"] is None else object_value(data["plan"], "plan")
                    self._set_inputs(RunInputs(object_value(data["task"], "task"), plan, data["research"] is True))
                if not self.inputs.task:
                    raise StageError("approve refused: run inputs missing")
                self._verify_worktree(action="approve")
                gates_path = self.directory / "gates.json"
                if not gates_path.is_file():
                    raise StageError("approve refused: gates.json missing")
                try:
                    gates = object_value(loads(gates_path.read_text(encoding="utf-8")), "gates")
                except (OSError, ValueError) as exc:
                    raise StageError("approve refused: invalid gates.json") from exc
                if gates.get("passed") is not True:
                    raise StageError("approve refused: gates did not pass")
                gates_artifact = self._artifact(Stage.GATES)
                if not gates_artifact.is_file() or sha256_file(gates_artifact) != sha256_file(gates_path):
                    raise StageError("approve refused: gates artifact mismatch")
                review_path = self.directory / "review.json"
                if not review_path.is_file():
                    raise StageError("approve refused: review.json missing")
                try:
                    review = object_value(loads(review_path.read_text(encoding="utf-8")), "review")
                except (OSError, ValueError) as exc:
                    raise StageError("approve refused: invalid review.json") from exc
                if not _review_approved(review):
                    raise StageError("approve refused: review is not approved")
                review_artifact = self._artifact(Stage.REVIEW)
                if not review_artifact.is_file() or sha256_file(review_artifact) != sha256_file(review_path):
                    raise StageError("approve refused: review artifact mismatch")
                digest, reason = self._review_verification()
                if reason is not None or digest is None:
                    raise StageError(f"approve refused: {reason or 'missing review digest'}")
                if not _effective_review_approved(review, digest):
                    raise StageError("approve refused: review not effectively approved")
                self._save(stage=Stage.DONE, status=Status.COMPLETED)
                self._event(EventKind.HUMAN_APPROVED)
                self._record_acceptance()
                return self._result(Status.COMPLETED)

        def reject(self, reason: str) -> dict[str, Json]:
            with RunLock(self.directory):
                self.state = load_state(self.directory)
                if self.state.status == Status.STOPPED:
                    raise StageError("reject refused: run already stopped")
                if self.state.stage == Stage.DONE or self.state.status == Status.COMPLETED:
                    raise StageError(f"reject refused: run already completed ({self.state.status.value})")
                if not (self.state.stage == Stage.ACCEPT and self.state.status == Status.AWAITING_HUMAN):
                    raise StageError(f"reject refused: run is in {self.state.stage.value}/{self.state.status.value}, expected accept/awaiting_human")
                if not isinstance(reason, str) or not reason.strip():
                    raise StageError("reject refused: nonempty reason required")
                if "\x00" in reason:
                    raise StageError("reject refused: reason contains NUL")
                cleaned = sanitize_output(reason[:5000])[:500]
                note = cleaned if cleaned.strip() else "[redacted]"
                self._save(status=Status.STOPPED)
                self._event(EventKind.HUMAN_REJECTED, note=note)
                return self._result(Status.STOPPED)

