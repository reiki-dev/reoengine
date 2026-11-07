from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / 'tests/fakes'))

from pipeline import FakeAdapter, make_inputs, make_pipeline, make_repository
from reona import gates
from reona._data import Json, atomic_write, loads, object_value, read_lines
from reona.adapters.agy import AgyAdapter
from reona.adapters.base import AgentResult, ErrorKind
from reona.config import Engine, Limits, RoleConfig, Sandbox, load_project
from reona.fsm import Pipeline, RunInputs, StageError, _review_approved
from reona.gates import GateCheck, GateRequest, plant_canary, run_gates
from reona.gitops import Git
from reona.procs import ProcessResult
from reona.schemas import SchemaError, validate, validate_artifact
from reona.state import Stage, Status
from reona.usage import TokenUsage


class ConflictingReviewAdapter(FakeAdapter):
    def __init__(self, severity: str) -> None:
        super().__init__()
        self.severity = severity

    def invoke(self, role_cfg: RoleConfig, prompt_file: Path, cwd: Path, timeout: float,
               schema: Path | None = None) -> AgentResult:
        result = super().invoke(role_cfg, prompt_file, cwd, timeout, schema)
        if role_cfg.model == 'reviewer':
            assert result.final_message_path is not None
            atomic_write(result.final_message_path, {
                'approved': True, 'summary': 'Synthetic conflicting review',
                'findings': [{'path': 'src/value.py', 'line': 1,
                              'severity': self.severity, 'text': 'Synthetic security finding'}],
            })
        return result


class S6AgySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ENGINE / 'tests', prefix='s6-agy-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.prompt = self.directory / 'prompt.txt'
        self.prompt.write_text('Synthetic security probe', encoding='utf-8')

    def assert_safe_command_or_refusal(self, role: str, sandbox: Sandbox) -> None:
        config = RoleConfig(Engine.AGY, role, sandbox=sandbox)
        result = AgentResult(0, '', TokenUsage(), 0, None)
        with patch('reona.adapters.agy.invoke_json', return_value=result) as invoke:
            AgyAdapter(command=('synthetic-agy',), capabilities=('--sandbox', '--mode')).invoke(
                config, self.prompt, self.directory, 1)
        invoke.assert_called_once()
        command = invoke.call_args.args[0]
        with self.subTest(policy='permissions'):
            self.assertNotIn('--dangerously-skip-permissions', command)
        with self.subTest(policy='noninteractive'):
            self.assertNotIn('--print', command)
            self.assertNotIn('-p', command)
        self.assertIn('--sandbox', command)
        mode = 'plan' if sandbox == Sandbox.READ_ONLY else 'accept-edits'
        self.assertEqual(command[command.index('--mode') + 1], mode)

    def test_agy_reader_safe_command_or_refusal(self) -> None:
        self.assert_safe_command_or_refusal('reader', Sandbox.READ_ONLY)

    def test_agy_researcher_safe_command_or_refusal(self) -> None:
        self.assert_safe_command_or_refusal('researcher', Sandbox.READ_ONLY)

    def test_agy_reviewer_safe_command_or_refusal(self) -> None:
        self.assert_safe_command_or_refusal('reviewer', Sandbox.READ_ONLY)

    def test_agy_coder_safe_command_or_refusal(self) -> None:
        self.assert_safe_command_or_refusal('coder', Sandbox.WORKSPACE_WRITE)

    def test_agy_denied_actions_rejected(self) -> None:
        schema = self.directory / 'schema.json'
        atomic_write(schema, {'type': 'object'})
        for response_schema in (None, schema):
            with self.subTest(schema=response_schema):
                envelope = {'status': 'SUCCESS', 'response': 'Synthetic result',
                            'structured_output': {}, 'denied_actions': [
                                {'action': 'command', 'display_name': 'Synthetic sensitive action'}]}
                command = (sys.executable, '-B', '-c', 'print(' + repr(json.dumps(envelope)) + ')')
                result = AgyAdapter(command, capabilities=('--sandbox', '--mode')).invoke(
                    RoleConfig(Engine.AGY, 'synthetic'), self.prompt, self.directory, 5, response_schema)
                self.assertFalse(result.success)
                self.assertEqual(result.error, ErrorKind.PROTOCOL)
                self.assertIsNone(result.final_message_path)
                self.assertIn('denied', result.stderr_tail)
                self.assertNotIn('Synthetic sensitive action', result.stdout_tail + result.stderr_tail)
        self.assertFalse(list(self.directory.glob('response-*.json')))

    def test_agy_empty_or_missing_denied_actions_accepted(self) -> None:
        for extra in ({}, {'denied_actions': []}):
            with self.subTest(extra=extra):
                envelope = {'status': 'SUCCESS', 'response': 'Synthetic result', **extra}
                command = (sys.executable, '-B', '-c', 'print(' + repr(json.dumps(envelope)) + ')')
                result = AgyAdapter(command, capabilities=('--sandbox', '--mode')).invoke(
                    RoleConfig(Engine.AGY, 'synthetic'), self.prompt, self.directory, 5)
                self.assertTrue(result.success, result)

    def probe_command(self, help_text: str, exit_code: int = 0) -> tuple[str, ...]:
        script = self.directory / 'synthetic_agy.py'
        script.write_text(
            'import json, sys\nfrom pathlib import Path\n'
            'if sys.argv[1:] == ["--help"]:\n'
            '    with Path("probes.txt").open("a") as trace:\n        trace.write("probe\\n")\n'
            f'    print({help_text!r})\n    sys.exit({exit_code})\n'
            'Path("prompt-started.txt").write_text(sys.stdin.read())\n'
            'print(json.dumps({"status": "SUCCESS", "response": "Synthetic result"}))\n',
            encoding='utf-8')
        return (sys.executable, '-B', str(script))

    def test_agy_missing_capabilities_refuses_before_prompt_process(self) -> None:
        for help_text in ('--mode plan', '--sandbox', '--sandbox-extra --mode-extra'):
            with self.subTest(help=help_text):
                adapter = AgyAdapter(self.probe_command(help_text))
                with self.assertRaisesRegex(RuntimeError, '--sandbox and --mode'):
                    adapter.invoke(RoleConfig(Engine.AGY, 'synthetic'), self.prompt, self.directory, 5)
                self.assertFalse((self.directory / 'prompt-started.txt').exists())
        self.assertEqual((self.directory / 'probes.txt').read_text().splitlines(), ['probe'] * 3)

    def test_agy_failed_probe_refuses_before_prompt_process(self) -> None:
        adapter = AgyAdapter(self.probe_command('--sandbox --mode', exit_code=2))
        with self.assertRaisesRegex(RuntimeError, 'Could not verify'):
            adapter.invoke(RoleConfig(Engine.AGY, 'synthetic'), self.prompt, self.directory, 5)
        self.assertFalse((self.directory / 'prompt-started.txt').exists())

    def test_agy_capability_probe_cached_per_command(self) -> None:
        adapter = AgyAdapter(self.probe_command('--sandbox --mode'))
        role = RoleConfig(Engine.AGY, 'synthetic')
        for _ in range(2):
            self.assertTrue(adapter.invoke(role, self.prompt, self.directory, 5).success)
        self.assertEqual((self.directory / 'probes.txt').read_text().splitlines(), ['probe'])
        self.assertEqual((self.directory / 'prompt-started.txt').read_text(), self.prompt.read_text())
        adapter.command = (*adapter.command[:1], '-u', *adapter.command[1:])
        self.assertTrue(adapter.invoke(role, self.prompt, self.directory, 5).success)
        self.assertEqual((self.directory / 'probes.txt').read_text().splitlines(), ['probe', 'probe'])

    def test_agy_probe_timeout_refuses_before_prompt_process(self) -> None:
        with patch('reona.adapters.agy.run_process', return_value=ProcessResult(-1, '', '', 0, True)) as probe, \
                patch('reona.adapters.agy.invoke_json') as invoke:
            with self.assertRaisesRegex(RuntimeError, 'Could not verify'):
                AgyAdapter(('synthetic-agy',)).invoke(
                    RoleConfig(Engine.AGY, 'synthetic'), self.prompt, self.directory, 30)
        invoke.assert_not_called()
        self.assertEqual(probe.call_args.args[0], ['synthetic-agy', '--help'])
        self.assertLessEqual(probe.call_args.kwargs['timeout'], 5)
        self.assertNotIn('stdin_file', probe.call_args.kwargs)

    def test_agy_unknown_sandbox_refuses_before_any_process(self) -> None:
        role = replace(RoleConfig(Engine.AGY, 'synthetic'), sandbox='unknown')
        with patch('reona.adapters.agy.run_process') as probe, \
                patch('reona.adapters.agy.invoke_json') as invoke:
            with self.assertRaisesRegex(ValueError, 'sandbox'):
                AgyAdapter(('synthetic-agy',)).invoke(role, self.prompt, self.directory, 5)
        probe.assert_not_called()
        invoke.assert_not_called()


class S6PipelineSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ENGINE / 'tests', prefix='s6-pipeline-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.repo = make_repository(self.directory)
        self.config_directory = self.directory / 'synthetic-config'
        self.config_directory.mkdir()

    def pipeline(self, fake: FakeAdapter, *, name: str = '', allow_legacy_argv_checks: bool = False) -> Pipeline:
        pipeline = make_pipeline(self.directory / name, self.repo, fake,
                                 limits=Limits(max_fix_rounds=0),
                                 allow_legacy_argv_checks=allow_legacy_argv_checks,
                                 config_directory=self.config_directory)
        pipeline.project = replace(pipeline.project, allowed_paths=('src/**', 'tests/**'))
        pipeline.config = replace(pipeline.config, projects={'test': pipeline.project})
        return pipeline

    def assert_rejected_before_process(self, checks: list[Json]) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        inputs = replace(inputs, plan={**inputs.plan, 'acceptance_checks': checks})
        validate_artifact('plan', inputs.plan)
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        # A successful mock lets an unsafe implementation reach its normal result
        # without executing either trusted checks or the injected argv.
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
            try:
                state = pipeline.start(inputs)
            except ValueError:
                state = None
        process.assert_not_called()
        self.assertEqual(fake.calls, [])
        self.assertFalse(pipeline.git.root.exists())
        if state is not None:
            self.assertEqual(state.status, Status.FAILED)
            self.assertIn(state.stage, (Stage.PREFLIGHT, Stage.BRIEF, Stage.PLAN))

    def test_plan_python_argv_rejected_before_run_process(self) -> None:
        self.assert_rejected_before_process([['python', '-c', 'print(1)']])

    def test_plan_powershell_argv_rejected_before_run_process(self) -> None:
        self.assert_rejected_before_process([['powershell', '-c', 'Write-Output 1']])

    def test_plan_unknown_check_id_rejected_before_run_process(self) -> None:
        self.assert_rejected_before_process(['unknown-security-check'])

    def test_registered_check_id_reaches_process(self) -> None:
        pipeline = self.pipeline(FakeAdapter())
        command = ('synthetic-trusted-runner', '--target', 'synthetic')
        pipeline.project = replace(pipeline.project, acceptance={'unit-tests': command})
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
            state = pipeline.start(make_inputs(tdd=False))
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        self.assertEqual([tuple(call.args[0]) for call in process.call_args_list], [command])
        saved = json.loads((pipeline.directory / 'inputs.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['task']['acceptance_checks'], ['unit-tests'])
        self.assertEqual(saved['plan']['acceptance_checks'], ['unit-tests'])

    def test_legacy_argv_requires_explicit_opt_in(self) -> None:
        command = ('synthetic-legacy-runner', '--synthetic')
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        inputs = replace(inputs, task={**inputs.task, 'acceptance_checks': [list(command)]},
                         plan={**inputs.plan, 'acceptance_checks': [list(command)]})
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                fake = FakeAdapter()
                pipeline = self.pipeline(fake, name=str(enabled), allow_legacy_argv_checks=enabled)
                with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
                    state = pipeline.start(inputs)
                if enabled:
                    self.assertEqual(state.status, Status.AWAITING_HUMAN)
                    self.assertEqual([tuple(call.args[0]) for call in process.call_args_list], [command])
                    events = read_lines(pipeline.events.path)
                    self.assertTrue(any(isinstance(event, dict) and event.get('note') == 'legacy_argv_checks'
                                        for event in events))
                else:
                    self.assertEqual((state.stage, state.status), (Stage.PREFLIGHT, Status.FAILED))
                    self.assertIn('--allow-legacy-argv-checks',
                                  (pipeline.directory / 'error.json').read_text(encoding='utf-8'))
                    process.assert_not_called()
                    self.assertEqual(fake.calls, [])
                    self.assertFalse(pipeline.git.root.exists())

    def test_invalid_check_lists_rejected_before_agents_or_worktree(self) -> None:
        invalid: tuple[list[Json], ...] = (['unit-tests', 'unit-tests'], [42], [],
                                          ['unit-tests', ['synthetic-runner']], ['unknown-security-check'])
        for field in ('task', 'plan'):
            for index, checks in enumerate(invalid):
                with self.subTest(field=field, checks=checks):
                    inputs = make_inputs(tdd=False)
                    value = inputs.task if field == 'task' else inputs.plan
                    assert value is not None
                    changed = {**value, 'acceptance_checks': checks}
                    inputs = replace(inputs, task=changed) if field == 'task' else replace(inputs, plan=changed)
                    fake = FakeAdapter()
                    pipeline = self.pipeline(fake, name=f'{field}-{index}')
                    with patch('reona.gates.run_process') as process:
                        try:
                            state = pipeline.start(inputs)
                        except ValueError:
                            state = None
                    if state is not None:
                        self.assertEqual((state.stage, state.status), (Stage.PREFLIGHT, Status.FAILED))
                    process.assert_not_called()
                    self.assertEqual(fake.calls, [])
                    self.assertFalse(pipeline.git.root.exists())

    def test_generated_plan_checks_rejected_before_plan_written(self) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        for index, checks in enumerate(([['python', '-c', 'print(1)']], ['unknown-security-check'])):
            with self.subTest(checks=checks):
                pipeline = self.pipeline(FakeAdapter(), name=f'generated-{index}')
                plan = {**inputs.plan, 'acceptance_checks': checks}
                with patch.object(pipeline, '_invoke', return_value=plan) as invoke, \
                        patch('reona.gates.run_process') as process:
                    state = pipeline.start(replace(inputs, plan=None))
                self.assertEqual((state.stage, state.status), (Stage.PLAN, Status.FAILED))
                self.assertEqual([call.args[0] for call in invoke.call_args_list], ['architect'])
                process.assert_not_called()
                self.assertFalse((pipeline.directory / 'plan.json').exists())

    def pause_after_plan(self, pipeline: Pipeline, inputs: RunInputs) -> None:
        execute = pipeline.execute_stage

        def interrupt(stage: Stage) -> dict[str, Json]:
            output = execute(stage)
            if stage == Stage.PLAN:
                raise KeyboardInterrupt()
            return output

        with patch.object(pipeline, 'execute_stage', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            pipeline.start(inputs)

    def test_resume_legacy_requires_opt_in_again(self) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        checks: list[Json] = [['synthetic-legacy-runner']]
        inputs = replace(inputs, task={**inputs.task, 'acceptance_checks': checks},
                         plan={**inputs.plan, 'acceptance_checks': checks})
        self.pause_after_plan(self.pipeline(FakeAdapter(), allow_legacy_argv_checks=True), inputs)
        fake = FakeAdapter()
        refused = self.pipeline(fake)
        with patch('reona.gates.run_process') as process, \
                self.assertRaisesRegex(StageError, 'legacy argv checks disabled.*--allow-legacy-argv-checks'):
            refused.resume()
        process.assert_not_called()
        self.assertEqual(fake.calls, [])
        resumed = self.pipeline(fake, allow_legacy_argv_checks=True)
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
            state = resumed.resume()
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        self.assertEqual(tuple(process.call_args.args[0]), ('synthetic-legacy-runner',))

    def test_legacy_argv_event_once_per_run_and_where_across_resume(self) -> None:
        inputs = make_inputs(tdd=False)
        if inputs.plan is None:
            raise ValueError('Synthetic plan required')
        checks: list[Json] = [['synthetic-legacy-runner']]
        inputs = replace(inputs, task={**inputs.task, 'acceptance_checks': checks},
                         plan={**inputs.plan, 'acceptance_checks': checks})
        pipeline = self.pipeline(FakeAdapter(), allow_legacy_argv_checks=True)
        self.pause_after_plan(pipeline, inputs)
        resumed = self.pipeline(FakeAdapter(), allow_legacy_argv_checks=True)
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)):
            state = resumed.resume()
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        events = [event for event in read_lines(resumed.events.path)
                  if isinstance(event, dict) and event.get('note') == 'legacy_argv_checks']
        self.assertEqual(len(events), 2)
        categories: list[str] = []
        for event in events:
            category = event.get('category')
            if not isinstance(category, str):
                raise ValueError('Legacy event category must be a string')
            categories.append(category)
            self.assertEqual(event.get('run_id'), state.run_id)
        self.assertCountEqual(categories, ['task.acceptance_checks', 'plan.acceptance_checks'])

    def test_resume_resolves_ids_from_current_configuration(self) -> None:
        self.pause_after_plan(self.pipeline(FakeAdapter()), make_inputs(tdd=False))
        resumed = self.pipeline(FakeAdapter())
        command = ('synthetic-current-runner',)
        resumed.project = replace(resumed.project, acceptance={'unit-tests': command})
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
            state = resumed.resume()
        self.assertEqual(state.status, Status.AWAITING_HUMAN)
        self.assertEqual([tuple(call.args[0]) for call in process.call_args_list], [command])

    def test_resume_generated_legacy_plan_refused_before_agents(self) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        plan = {**inputs.plan, 'acceptance_checks': [['synthetic-legacy-runner']]}
        pipeline = self.pipeline(FakeAdapter(), allow_legacy_argv_checks=True)
        with patch.object(pipeline, '_invoke', return_value=plan):
            self.pause_after_plan(pipeline, replace(inputs, plan=None))
        fake = FakeAdapter()
        refused = self.pipeline(fake)
        with patch('reona.gates.run_process') as process, \
                self.assertRaisesRegex(StageError, 'plan.acceptance_checks: legacy argv checks disabled'):
            refused.resume()
        process.assert_not_called()
        self.assertEqual(fake.calls, [])

    def test_tdd_requires_task_check_ids_even_with_project_checks(self) -> None:
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        pipeline.project = replace(pipeline.project, checks=(('synthetic-project-runner',),))
        inputs = make_inputs()
        with patch('reona.gates.run_process') as process:
            state = pipeline.start(replace(inputs, task={**inputs.task, 'acceptance_checks': []}))
        self.assertEqual((state.stage, state.status), (Stage.PREFLIGHT, Status.FAILED))
        self.assertEqual(fake.calls, [])
        process.assert_not_called()
        self.assertFalse(pipeline.git.root.exists())

    def test_task_paths_inside_project_glob_accepted(self) -> None:
        pipeline = self.pipeline(FakeAdapter())
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        pipeline._set_inputs(replace(inputs, task={**inputs.task, 'allowed_paths': ['src/api/**']},
                                     plan={**inputs.plan, 'allowed_paths': ['src/api/**']}))
        pipeline.execute_stage(Stage.PREFLIGHT)
        self.assertTrue(pipeline.git.root.is_dir())

    def test_invalid_task_paths_and_empty_project_policy_rejected(self) -> None:
        cases = (('../src/**', ('src/**',)), ('src/../tests/**', ('src/**',)),
                 ('/src/**', ('src/**',)), ('C:/src/**', ('src/**',)),
                 ('src\\api/**', ('src/**',)), ('src//api/**', ('src/**',)),
                 ('src/./api/**', ('src/**',)), ('src/**', ()),
                 ('src-other/**', ('src/**',)), ('src', ('src/**',)),
                 ('src/api/**', ('src/*/**',)), ('src/api/**', ('src/api',)))
        for index, (path, parents) in enumerate(cases):
            with self.subTest(path=path, parents=parents):
                fake = FakeAdapter()
                pipeline = self.pipeline(fake, name=f'paths-{index}')
                pipeline.project = replace(pipeline.project, allowed_paths=parents)
                inputs = make_inputs(tdd=False)
                inputs = replace(inputs, task={**inputs.task, 'allowed_paths': [path]})
                with patch('reona.gates.run_process') as process:
                    state = pipeline.start(inputs)
                self.assertEqual((state.stage, state.status), (Stage.PREFLIGHT, Status.FAILED))
                self.assertEqual(fake.calls, [])
                process.assert_not_called()
                self.assertFalse(pipeline.git.root.exists())

    def test_task_paths_outside_project_rejected_before_any_agent(self) -> None:
        fake = FakeAdapter()
        pipeline = self.pipeline(fake)
        inputs = make_inputs(tdd=False)
        inputs = replace(inputs, task={**inputs.task,
                                      'allowed_paths': ['src/**', 'tests/**', 'outside/**']})
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
            try:
                state = pipeline.start(inputs)
            except ValueError:
                state = None
        self.assertEqual(len(fake.calls), 0, 'Task scope must be checked before invoking any agent')
        process.assert_not_called()
        if state is not None:
            self.assertEqual(state.status, Status.FAILED)

    def test_plan_paths_outside_task_rejected_before_any_agent(self) -> None:
        for index, paths in enumerate((['outside/**'], ['tests/**'], ['src/api/**'], [])):
            with self.subTest(paths=paths):
                fake = FakeAdapter()
                pipeline = self.pipeline(fake, name=f'plan-paths-{index}')
                inputs = make_inputs(tdd=False)
                assert inputs.plan is not None
                inputs = replace(inputs, task={**inputs.task, 'allowed_paths': ['src/**']},
                                 plan={**inputs.plan, 'allowed_paths': paths}, research=True)
                with patch('reona.gates.run_process') as process:
                    state = pipeline.start(inputs)
                self.assertEqual(fake.calls, [])
                process.assert_not_called()
                self.assertEqual((state.stage, state.status), (Stage.PREFLIGHT, Status.FAILED))
                error = json.loads((pipeline.directory / 'error.json').read_text(encoding='utf-8'))
                self.assertEqual(error['error'], 'Plan must use a nonempty subset of task allowed_paths')
                self.assertFalse(pipeline.git.root.exists())

    def test_resume_saved_plan_paths_outside_project_rejected_before_agents(self) -> None:
        inputs = replace(make_inputs(tdd=False), plan=None, research=True)
        self.pause_after_plan(self.pipeline(FakeAdapter()), inputs)
        fake = FakeAdapter()
        resumed = self.pipeline(fake)
        path = resumed.directory / 'plan.json'
        plan = json.loads(path.read_text(encoding='utf-8'))
        plan['allowed_paths'] = ['outside/**']
        atomic_write(path, plan)
        with patch('reona.gates.run_process') as process, \
                self.assertRaisesRegex(StageError, 'Plan must use a nonempty subset of task allowed_paths'):
            resumed.resume()
        self.assertEqual(fake.calls, [])
        process.assert_not_called()

    def test_valid_plan_paths_pass_start_and_resume(self) -> None:
        for supplied in (True, False):
            with self.subTest(supplied=supplied):
                name = f'valid-plan-{supplied}'
                inputs = make_inputs(tdd=False)
                inputs = replace(inputs, plan=inputs.plan if supplied else None, research=True)
                fake = FakeAdapter()
                pipeline = self.pipeline(fake, name=name)
                self.pause_after_plan(pipeline, inputs)
                self.assertIn('researcher', [call.model for call in fake.calls])
                resumed = self.pipeline(FakeAdapter(), name=name)
                with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)) as process:
                    state = resumed.resume()
                self.assertEqual(state.status, Status.AWAITING_HUMAN)
                process.assert_called_once()

    def assert_conflicting_review_rejected(self, severity: str) -> None:
        fake = ConflictingReviewAdapter(severity)
        pipeline = self.pipeline(fake)
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)):
            state = pipeline.start(make_inputs(tdd=False))
        self.assertIn('reviewer', [call.model for call in fake.calls])
        review = json.loads((pipeline.directory / 'prompts' / 'fake-3.json').read_text(encoding='utf-8'))
        self.assertTrue(review['approved'])
        self.assertEqual(review['findings'][0]['severity'], severity)
        self.assertNotEqual(state.stage, Stage.ACCEPT)
        self.assertIn(state.status, (Status.FAILED, Status.STOPPED))
        self.assertFalse(list((pipeline.directory / 'checkpoints').glob('accept-*.json')))
        saved_review = object_value(loads((pipeline.directory / 'review.json').read_text(encoding='utf-8')), 'review')
        self.assertEqual(pipeline._feedback(), saved_review['findings'])
        validate_artifact('review', saved_review)

    def test_review_approved_with_high_finding_never_reaches_accept(self) -> None:
        self.assert_conflicting_review_rejected('high')

    def test_review_approved_with_critical_finding_never_reaches_accept(self) -> None:
        self.assert_conflicting_review_rejected('critical')

    def test_review_approved_with_medium_finding_can_reach_accept(self) -> None:
        pipeline = self.pipeline(ConflictingReviewAdapter('medium'))
        with patch('reona.gates.run_process', return_value=ProcessResult(0, '', '', 0)):
            state = pipeline.start(make_inputs(tdd=False))
        self.assertEqual((state.stage, state.status), (Stage.ACCEPT, Status.AWAITING_HUMAN))

    def test_saved_conflicting_review_routes_to_fix_loop(self) -> None:
        for severity in ('high', 'critical'):
            with self.subTest(severity=severity):
                pipeline = self.pipeline(FakeAdapter(), name=severity)
                pipeline._set_inputs(make_inputs(tdd=False))
                review: dict[str, Json] = {'approved': True, 'summary': 'Synthetic conflicting review',
                                          'findings': [{'path': 'src/value.py', 'line': None,
                                                        'severity': severity, 'text': 'Synthetic finding'}]}
                with patch.object(pipeline, '_load', return_value=review):
                    self.assertEqual(pipeline._next(Stage.REVIEW), Stage.FIX_LOOP)


class S6ReviewApprovalTests(unittest.TestCase):
    def test_review_approval_rejects_malformed_findings_and_missing_keys(self) -> None:
        reviews: tuple[dict[str, Json], ...] = (
            {}, {'findings': []}, {'approved': True}, {'approved': 1, 'findings': []},
            {'approved': False, 'findings': []}, {'approved': True, 'findings': None},
            {'approved': True, 'findings': {}}, {'approved': True, 'findings': [None]},
            {'approved': True, 'findings': [{}]},
        )
        for review in reviews:
            with self.subTest(review=review):
                self.assertFalse(_review_approved(review))
        severities: tuple[Json, ...] = ('high', 'critical', 'HIGH', None, 5, 'unknown', [], {})
        for severity in severities:
            with self.subTest(severity=severity):
                self.assertFalse(_review_approved({'approved': True, 'findings': [{'severity': severity}]}))

    def test_review_approval_accepts_only_allowed_findings(self) -> None:
        findings: list[Json] = []
        self.assertTrue(_review_approved({'approved': True, 'findings': findings}))
        for severity in ('info', 'low', 'medium'):
            findings.append({'severity': severity})
            with self.subTest(severity=severity):
                self.assertTrue(_review_approved({'approved': True, 'findings': findings}))


class S6AcceptanceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ENGINE / 'tests', prefix='s6-config-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def test_acceptance_registry_validates_argv_and_ids(self) -> None:
        path = self.directory / 'project.toml'
        prefix = f'repo={json.dumps(self.directory.as_posix())}\nprofile="synthetic"\n[acceptance]\n'
        for value in ('"runner"', '42', '[]', '["", "arg"]', '["runner", 42]', '["runner", "\\u0000"]'):
            with self.subTest(value=value):
                path.write_text(prefix + f'unit-tests={value}\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'acceptance.unit-tests'):
                    load_project(path)
        for identifier in ('UPPER', 'bad/id', 'a' * 65):
            with self.subTest(identifier=identifier):
                path.write_text(prefix + f'"{identifier}"=["runner"]\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'invalid check ID'):
                    load_project(path)
        path.write_text(prefix + 'unit-tests=["runner", "--synthetic"]\n', encoding='utf-8')
        project = load_project(path)
        self.assertEqual(project.acceptance, {'unit-tests': ('runner', '--synthetic')})
        self.assertIsNone(getattr(project.acceptance, '__setitem__', None))

    def test_acceptance_schema_ids_and_legacy_shape(self) -> None:
        inputs = make_inputs(tdd=False)
        assert inputs.plan is not None
        for name, artifact in (('task', inputs.task), ('plan', inputs.plan)):
            for checks in (['unit-tests', 'a.b_0-1'], [['synthetic-runner', '--synthetic']]):
                with self.subTest(name=name, checks=checks):
                    validate_artifact(name, {**artifact, 'acceptance_checks': checks})
            for checks in ([42], ['UPPER'], ['a' * 65], ['bad/id'], ['']):
                with self.subTest(name=name, checks=checks), self.assertRaises(SchemaError):
                    validate_artifact(name, {**artifact, 'acceptance_checks': checks})

    def test_schema_extensions_fail_closed(self) -> None:
        for schema in ({'anyOf': []}, {'anyOf': [42]}, {'type': 'string', 'pattern': 42},
                       {'type': 'string', 'pattern': '['},
                       {'anyOf': [{'type': 'string'}, {'type': 'string', 'unsupported': True}]}):
            with self.subTest(schema=schema), self.assertRaises(SchemaError):
                validate('synthetic', schema)

    def test_acceptance_schema_rejects_id_with_trailing_newline(self) -> None:
        inputs = make_inputs(tdd=False)
        if inputs.plan is None:
            raise ValueError('Synthetic plan required')
        for name, artifact in (('task', inputs.task), ('plan', inputs.plan)):
            with self.subTest(name=name), self.assertRaises(SchemaError):
                validate_artifact(name, {**artifact, 'acceptance_checks': ['unit-tests\n']})

    def test_acceptance_schema_rejects_mixed_ids_and_argv(self) -> None:
        inputs = make_inputs(tdd=False)
        if inputs.plan is None:
            raise ValueError('Synthetic plan required')
        cases: tuple[list[Json], ...] = (['unit-tests', ['synthetic-runner']],
                                         [['synthetic-runner'], 'unit-tests'])
        for name, artifact in (('task', inputs.task), ('plan', inputs.plan)):
            for checks in cases:
                with self.subTest(name=name, checks=checks), self.assertRaises(SchemaError):
                    validate_artifact(name, {**artifact, 'acceptance_checks': checks})


class S6EnvironmentSecurityTests(unittest.TestCase):
    def test_build_restricted_env_exists(self) -> None:
        builder = getattr(gates, 'build_restricted_env', None)
        self.assertTrue(callable(builder), 'Expose environment filtering without promising network isolation')
        source = {'PATH': 'synthetic-bin', 'TOKEN': 'synthetic-secret', 'HOME': 'synthetic-home',
                  'SERVICE_HOST': 'synthetic-host', 'https_proxy': 'synthetic-proxy'}
        env = builder({'CUSTOM': 'synthetic-value', 'API_BASE_URL': 'synthetic-url'}, source)
        self.assertEqual(env['PATH'], 'synthetic-bin')
        self.assertEqual(env['CUSTOM'], 'synthetic-value')
        self.assertNotIn('TOKEN', env)
        self.assertNotIn('HOME', env)
        self.assertEqual(env['SERVICE_HOST'], '127.0.0.1:9')
        self.assertEqual(env['API_BASE_URL'], 'http://127.0.0.1:9')
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'https_proxy'):
            self.assertEqual(env[name], 'http://127.0.0.1:9')
        self.assertEqual(env['NO_PROXY'], '')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(source['SERVICE_HOST'], 'synthetic-host')
        with self.assertRaises(ValueError):
            builder({'PYTHONPATH': 'synthetic-unsafe'}, source)

    def test_offline_environment_emits_deprecation_warning(self) -> None:
        source = {'PATH': 'synthetic-bin'}
        with self.assertWarns(DeprecationWarning) as warning:
            env = gates.offline_environment(source=source)
        self.assertEqual(Path(warning.filename).resolve(), Path(__file__).resolve())
        self.assertEqual(env, gates.build_restricted_env(source=source))


class S6TddSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=ENGINE / 'tests', prefix='s6-tdd-')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.repo = make_repository(self.directory)
        self.git = Git(self.repo)
        self.base = self.git.head()
        plant_canary(self.git)
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_value.py').write_text(
            'import unittest\nfrom src.value import VALUE\n'
            'class ValueTest(unittest.TestCase):\n'
            '    def test_value(self):\n        self.assertEqual(VALUE, 1)\n', encoding='utf-8')
        (self.repo / 'src/value.py').write_text('VALUE = 1\n', encoding='utf-8')

    def red_check(self, exit_code: int, stderr: str, stdout: str = '') -> GateCheck:
        command = (sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests')
        request = GateRequest(self.base, ('src/**', 'tests/**'), (command,), target_checks=(command,))

        def execute(name: str, argv: object, cwd: Path, request: GateRequest) -> GateCheck:
            if name == 'tdd_red:0':
                return GateCheck(name, False, exit_code, stdout, stderr)
            return GateCheck(name, True)

        with patch('reona.gates._execute', side_effect=execute) as execution, \
                patch('reona.gates.run_process') as process:
            report = run_gates(self.git, request, self.directory / 'gates.json')
        process.assert_not_called()
        self.assertEqual([call.args[0] for call in execution.call_args_list],
                         ['project:0', 'tdd_red:0', 'tdd_green:0'])
        return next(check for check in report.checks if check.name == 'tdd_red:0')

    def test_tdd_red_import_error_is_not_passed(self) -> None:
        self.assertFalse(self.red_check(1, 'ImportError: cannot import name synthetic').passed)

    def test_tdd_red_module_not_found_is_not_passed(self) -> None:
        self.assertFalse(self.red_check(1, "ModuleNotFoundError: No module named 'synthetic'").passed)

    def test_tdd_red_timeout_is_not_passed(self) -> None:
        # _execute normalizes ProcessResult.timed_out to GateCheck.exit_code=-1.
        self.assertFalse(self.red_check(-1, 'Synthetic test process timed out').passed)

    def test_tdd_red_process_error_minus_one_is_not_passed(self) -> None:
        self.assertFalse(self.red_check(-1, 'OSError').passed)

    def test_tdd_red_assertion_failure_can_pass(self) -> None:
        self.assertTrue(self.red_check(1, 'AssertionError: 0 != 1\nFAILED (failures=1)').passed)

    def test_tdd_red_pytest_summary_without_assertion_is_not_passed(self) -> None:
        for output in ('=== 1 failed in 0.1s ===',
                       'ValueError: bad\n=== 1 failed in 0.1s ===',
                       'AttributeError: bad\n=== 1 failed in 0.1s ==='):
            for stdout, stderr in ((output, ''), ('', output)):
                with self.subTest(output=output, stdout=bool(stdout)):
                    self.assertEqual(gates._check_kind(1, stdout, stderr), 'unknown')
                    self.assertFalse(self.red_check(1, stderr, stdout).passed)

    def test_tdd_red_pytest_assertion_with_summary_can_pass(self) -> None:
        output = 'E       assert 0 == 1\n=== 1 failed ==='
        for stdout, stderr in ((output, ''), ('', output)):
            with self.subTest(stdout=bool(stdout)):
                self.assertEqual(gates._check_kind(1, stdout, stderr), 'assertion_failed')
                self.assertTrue(self.red_check(1, stderr, stdout).passed)

    def test_tdd_red_infra_overrides_assertion_failure(self) -> None:
        self.assertFalse(self.red_check(1, 'AssertionError: 0 != 1',
                                        'ModuleNotFoundError: synthetic dependency').passed)

    def test_tdd_red_unknown_and_zero_exit_are_not_passed(self) -> None:
        for exit_code, output in ((1, 'synthetic failure'), (0, 'AssertionError: synthetic')):
            with self.subTest(exit_code=exit_code):
                self.assertFalse(self.red_check(exit_code, output).passed)

    def test_check_kind_runner_assertion_markers(self) -> None:
        outputs = ('AssertionError: 0 != 1', 'FAILED (failures=1)', 'FAIL: test_value (ValueTest)',
                   'E       assert 0 == 1', 'FAILED tests/test_value.py::test_value - assert 0 == 1',
                   'E       assert 0 == 1\n==================== 1 failed, 2 passed in 0.1s ====================',
                   'AssertionError [ERR_ASSERTION]: Expected values to be strictly equal',
                   "not ok 1 - synthetic\n  error: 'Expected values to be strictly equal'\n  code: 'ERR_ASSERTION'",
                   "✖ synthetic\n  code: 'ERR_ASSERTION'")
        for output in outputs:
            for stdout, stderr in ((output, ''), ('', output)):
                with self.subTest(output=output, stdout=bool(stdout)):
                    self.assertEqual(gates._check_kind(1, stdout, stderr), 'assertion_failed')

    def test_check_kind_strips_ansi_from_runner_output(self) -> None:
        cases = (('\x1b[31mFAIL: test_x ... AssertionError\x1b[0m', 'assertion_failed'),
                 ('\x1b[31mFAIL: test_x (SyntheticTest)\x1b[0m', 'assertion_failed'),
                 ('\x1b[31mE       assert 0 == 1\x1b[0m', 'assertion_failed'),
                 ('\x1b[31mImportError: synthetic dependency\x1b[0m', 'infra_error'),
                 ('\x1b[31mERROR: test_x (SyntheticTest)\x1b[0m', 'infra_error'))
        for output, expected in cases:
            for stdout, stderr in ((output, ''), ('', output)):
                with self.subTest(output=output, stdout=bool(stdout)):
                    self.assertEqual(gates._check_kind(1, stdout, stderr), expected)

    def test_check_kind_infra_markers_override_runner_failures(self) -> None:
        markers = ('ImportError', 'ModuleNotFoundError', 'SyntaxError', 'IndentationError', 'TabError',
                   'Cannot find module', 'Cannot find package', 'ERR_MODULE_NOT_FOUND',
                   'not recognized', 'command not found', 'No such file', 'PermissionError',
                   'FileNotFoundError', 'OSError', 'TypeError', 'NameError', 'ReferenceError',
                   'FAILED (failures=1, errors=1)', 'ERROR: test_synthetic',
                   'ERROR collecting tests/test_synthetic.py', '1 error in 0.1s')
        for marker in markers:
            for stdout, stderr in ((marker, 'AssertionError'), ('AssertionError', marker)):
                with self.subTest(marker=marker, stdout=stdout):
                    self.assertEqual(gates._check_kind(1, stdout, stderr), 'infra_error')

    def test_check_kind_fail_closed_and_timeout(self) -> None:
        for exit_code, output, expected in ((0, '', 'pass'), (1, '', 'unknown'),
                                            (1, 'not ok 1 - synthetic\nERR_TEST_FAILURE', 'unknown'),
                                            (2, 'error TS2307: synthetic', 'unknown'),
                                            (-1, 'AssertionError', 'infra_error'),
                                            (-9, 'AssertionError', 'infra_error')):
            with self.subTest(exit_code=exit_code, output=output):
                self.assertEqual(gates._check_kind(exit_code, output, ''), expected)
        self.assertEqual(gates._check_kind(0, '', '', timed_out=True), 'timeout')

    def test_execute_green_only_passes_zero_without_timeout(self) -> None:
        request = GateRequest(self.base, ('src/**', 'tests/**'), ())
        for result, passed in ((ProcessResult(0, '', '', 0), True),
                               (ProcessResult(1, '', 'AssertionError', 0), False),
                               (ProcessResult(2, '', '', 0), False),
                               (ProcessResult(-1, '', '', 0), False),
                               (ProcessResult(0, '', '', 0, True), False)):
            with self.subTest(result=result), patch('reona.gates.run_process', return_value=result):
                self.assertEqual(gates._execute('tdd_green:0', ('synthetic',), self.repo, request).passed, passed)


if __name__ == '__main__':
    unittest.main()
