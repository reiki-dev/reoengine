from __future__ import annotations

import hashlib
from dataclasses import replace

from .._data import Json, atomic_write, loads, object_value, strings
from ..events import EventKind
from ..gates import GateRequest, run_gates
from ..runlinks import check_run_links
from ..policy import _digest
from ..state import Status
from ..usage import Outcome, _record
from ._base import StageContext

class GatesFixMixin(StageContext):
        def _gates(self) -> dict[str, Json]:
            check_run_links(self.directory, self.project.links)
            request = GateRequest(self.state.base_sha, strings(self._plan()["allowed_paths"], "allowed_paths"),
                                  self._checks(), self.profile.blocked_paths,
                                  self.inputs.task["tdd_gate"] is True,
                                  str(self.inputs.task.get("tdd_gate_reason", "")),
                                  target_checks=self._task_checks(),
                                  timeout=self.config.limits.timeout_seconds, offline_env=self.project.offline_env,
                                  isolation_mode=self.config.limits.isolation.mode,
                                  isolation_wrapper=self.config.limits.isolation.wrapper,
                                  path_exclude_dirs=(self.git.root, self.directory))
            report = run_gates(self.git, request, self.directory / "gates.json",
                               on_tdd_disabled=lambda reason: self._event(EventKind.TDD_DISABLED, input_hash=_digest(reason)),
                               diff_output=self.directory / "review" / "diff.patch")
            if not report.passed:
                self._event(EventKind.GATE_FAILED, exit_code=1)
            self._flush_coder_usage("accepted" if report.passed else "gate_failed")
            # Outcome attribution is separate metadata, never the model's own claims.
            atomic_write(self.directory / "feedback.json", list(report.feedback))
            return report.to_json()

        def _flush_coder_usage(self, outcome: Outcome) -> None:
            existing = {record.ts for record in self.usage.read_days() if record.run_id == self.state.run_id}
            for path in sorted((self.directory / "usage").glob(f"coder-{self.state.attempt}-*.json")):
                receipt = _record(loads(path.read_text(encoding="utf-8")))
                if receipt.ts not in existing:
                    self.usage.append(replace(receipt, outcome=outcome))
                    existing.add(receipt.ts)

        def _fix(self) -> dict[str, Json]:
            gates = object_value(loads((self.directory / "gates.json").read_text(encoding="utf-8")), "gates")
            checks = gates.get("checks")
            failures = sum(1 for check in checks if isinstance(check, dict) and check.get("passed") is not True) \
                if isinstance(checks, list) else 0
            diff_path = self.directory / "review" / "diff.patch"
            diff_digest = hashlib.sha256(diff_path.read_bytes() if diff_path.is_file() else b"").hexdigest()
            if self.config.limits.fix_loop_token_budget:
                records = [record for record in self.usage.read_days() if record.run_id == self.state.run_id
                           and record.role in {"coder"}]
                tokens = sum((record.usage.input_tokens or 0) + (record.usage.output_tokens or 0)
                             for record in records)
                if tokens > self.config.limits.fix_loop_token_budget:
                    return self._result(Status.STOPPED, stop_reason="fix_loop_budget")
            if (self.config.limits.fix_loop_no_progress_stop
                    and self.state.fix_previous_failures is not None
                    and self.state.fix_previous_diff_sha256 is not None
                    and failures >= self.state.fix_previous_failures
                    and diff_digest == self.state.fix_previous_diff_sha256):
                return self._result(Status.STOPPED, stop_reason="no_progress")
            self.state = replace(self.state, fix_previous_failures=failures,
                                 fix_previous_diff_sha256=diff_digest)
            if self.state.attempt >= self.config.limits.max_fix_rounds + 1:
                return self._result(Status.STOPPED, stop_reason="max_fix_rounds")
            return {"attempt": self.state.attempt + 1, "failures": self._feedback()}

