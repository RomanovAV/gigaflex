import contextlib
import io
import json
import os
import shlex
from pathlib import Path
from unittest.mock import Mock, patch

from test_task_recovery import TaskRepositoryCase
from gigaflex.checkpoint import ResumeError, RunCheckpoint
from gigaflex.cli import main
from gigaflex.executor import ExecResult
from gigaflex.git import GitError, GitService


class TaskResumeTest(TaskRepositoryCase):
    def checkpoint(self):
        return RunCheckpoint(self.repo / '.gigaflex/checkpoint.json', self.git,
                             identity='plan:test', base_commit=self.initial_head)

    def save_partial(self, *, retained=False, complete=False):
        checkpoint = self.checkpoint()
        try:
            with contextlib.ExitStack() as stack:
                if retained:
                    stack.enter_context(patch('gigaflex.task_recovery.save_task_recovery',
                                              side_effect=OSError('disk full')))
                with self.manager.create('1: Build') as workspace:
                    task_git = GitService(workspace.path)
                    (workspace.path / 'feature.txt').write_text('committed work\n')
                    if complete:
                        (workspace.path / 'plan.md').write_text(self.plan.read_text().replace('[ ]', '[x]'))
                    task_git.run('add', '.')
                    task_git.run('commit', '-qm', 'feature')
                    if not complete:
                        (workspace.path / 'partial.txt').write_text('staged work\n')
                        task_git.run('add', 'partial.txt')
                        (workspace.path / 'partial.txt').write_text('unstaged work\n')
                    raise RuntimeError('dependency unavailable')
        except GitError as error:
            checkpoint.mark_blocked(str(error), 'tasks', 'gigaflex plan.md --resume', error.task_recovery)
        return checkpoint, workspace

    def test_contract_changes_are_repaired_and_revalidated_in_the_same_workspace(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.plan.write_text('# Plan\n### Task 1: Build\n- [ ] Implement\n- [ ] Validate\n')
                self.git.run('add', 'plan.md')
                self.git.run('commit', '--allow-empty', '-qm', 'input')
                calls = []
                testcase = self

                class Executor:
                    def run(self, prompt, *, retry_guard=None, cwd=None):
                        calls.append(cwd)
                        task_git = GitService(cwd)
                        plan = cwd / 'plan.md'
                        if len(calls) == 1:
                            plan.write_text(plan.read_text().replace('- [ ] Validate\n', ''))
                            (cwd / 'feature.txt').write_text(f'work {committed}\n')
                            if committed:
                                task_git.run('add', '.')
                                task_git.run('commit', '-qm', 'bad plan but useful work')
                        else:
                            testcase.assertEqual(testcase.plan.read_text(), plan.read_text())
                            testcase.assertIn('Verify ALL selected requirements again', prompt)
                            testcase.assertEqual(f'work {committed}\n', (cwd / 'feature.txt').read_text())
                            plan.write_text(plan.read_text().replace('[ ]', '[x]'))
                            task_git.run('add', '.')
                            task_git.run('commit', '-qm', 'validated original requirements')
                        return ExecResult(output='done', returncode=0)

                self.runner(Executor()).run_tasks()
                self.assertEqual(2, len(calls))
                self.assertEqual(calls[0], calls[1])
                self.assertIn('- [x] Validate', self.plan.read_text())

    def test_contract_restoration_without_revalidation_cannot_complete_a_task(self):
        calls = []

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                if len(calls) == 1:
                    (cwd / 'plan.md').write_text('# Easier plan\n')
                    task_git = GitService(cwd)
                    task_git.run('add', '.')
                    task_git.run('commit', '-qm', 'removed requirements')
                return ExecResult(output='done', returncode=0)

        with self.assertRaisesRegex(GitError, 'did not complete its selected plan section'):
            self.runner(Executor()).run_tasks()
        self.assertEqual(2, len(calls))
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_repeated_contract_changes_exhaust_a_bounded_retry_budget(self):
        calls = []

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                plan = cwd / 'plan.md'
                plan.write_text(plan.read_text().replace('- [ ] Validate\n', ''))
                return ExecResult(output='done', returncode=0)

        with self.assertRaisesRegex(GitError, 'modified protected plan content.*after 2 automatic completion retries'):
            self.runner(Executor(), retries=2).run_tasks()
        self.assertEqual(3, len(calls))
        self.assertEqual(1, len(set(calls)))

    def test_openspec_context_is_restored_before_corrective_retry(self):
        source = self.repo / 'openspec/changes/demo'
        source.mkdir(parents=True)
        tasks = source / 'tasks.md'
        tasks.write_text('## 1. Build\n- [ ] 1.1 Implement\n')
        proposal = source / 'proposal.md'
        proposal.write_text('Original requirement\n')
        self.git.run('add', 'openspec')
        self.git.run('commit', '-qm', 'OpenSpec input')
        calls = []
        testcase = self

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                change = cwd / source.relative_to(testcase.repo)
                task_git = GitService(cwd)
                if len(calls) == 1:
                    (change / 'proposal.md').unlink()
                    (change / 'extra.md').write_text('Replacement requirements\n')
                    (cwd / 'feature.txt').write_text('useful work\n')
                    task_git.run('add', '.')
                    task_git.run('commit', '-qm', 'modified context')
                else:
                    testcase.assertEqual('Original requirement\n', (change / 'proposal.md').read_text())
                    testcase.assertFalse((change / 'extra.md').exists())
                    testcase.assertEqual('useful work\n', (cwd / 'feature.txt').read_text())
                    (change / 'tasks.md').write_text((change / 'tasks.md').read_text().replace('[ ]', '[x]'))
                    task_git.run('add', '.')
                    task_git.run('commit', '-qm', 'validated original context')
                return ExecResult(output='done', returncode=0)

        runner = self.runner(Executor())
        runner.options.plan_file = tasks
        runner.options.plan_source = source
        runner.options.plan_kind = 'openspec'
        runner.options.plan_context_files = (proposal,)
        runner.run_tasks()
        self.assertEqual(2, len(calls))
        self.assertEqual('Original requirement\n', proposal.read_text())

    def test_resume_restores_saved_commits_and_both_index_and_working_versions(self):
        checkpoint, workspace = self.save_partial()
        calls = []
        testcase = self

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                task_git = GitService(cwd)
                testcase.assertEqual('committed work\n', (cwd / 'feature.txt').read_text())
                testcase.assertEqual('staged work\n', task_git.run('show', ':partial.txt').stdout)
                testcase.assertEqual('unstaged work\n', (cwd / 'partial.txt').read_text())
                testcase.assertIn('Resuming saved task work', prompt)
                testcase.assertIn('The dependency is available {now}', prompt)
                (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                task_git.run('add', '.')
                task_git.run('commit', '-qm', 'completed saved work')
                return ExecResult(output='done', returncode=0)

        runner = self.runner(Executor())
        runner.checkpoint = checkpoint
        runner.options.resume = True
        runner.options.resume_note = 'The dependency is available {now}'
        runner.run()
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0].exists())
        self.assertNotEqual(workspace.path, calls[0])
        self.assertTrue(workspace.recovery_path.exists())
        self.assertFalse(checkpoint.blocked)
        self.assertEqual('unstaged work\n', (self.repo / 'partial.txt').read_text())

    def test_resume_uses_the_retained_workspace_when_bundle_save_failed(self):
        checkpoint, workspace = self.save_partial(retained=True)
        calls = []

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                task_git = GitService(cwd)
                task_git.run('add', '.')
                task_git.run('commit', '-qm', 'complete retained work')
                return ExecResult(output='done', returncode=0)

        runner = self.runner(Executor())
        runner.checkpoint = checkpoint
        runner.options.resume = True
        runner.run()
        self.assertEqual([workspace.path], calls)
        self.assertFalse(workspace.path.exists())
        self.assertFalse(checkpoint.blocked)

    def test_resume_can_promote_an_already_complete_task_without_another_agent_call(self):
        checkpoint, _ = self.save_partial(complete=True)
        executor = Mock()
        runner = self.runner(executor)
        runner.checkpoint = checkpoint
        runner.options.resume = True
        runner.run()
        executor.run.assert_not_called()
        self.assertIn('- [x] Validate', self.plan.read_text())

    def test_resume_refuses_changed_main_state_and_keeps_saved_work(self):
        checkpoint, workspace = self.save_partial()
        (self.repo / 'tracked.txt').write_text('new user edit\n')
        executor = Mock()
        runner = self.runner(executor)
        runner.checkpoint = checkpoint
        runner.options.resume = True
        with self.assertRaisesRegex(ResumeError, 'main working tree changed'):
            runner.run()
        executor.run.assert_not_called()
        self.assertEqual('new user edit\n', (self.repo / 'tracked.txt').read_text())
        self.assertTrue(workspace.recovery_path.exists())
        self.assertTrue(checkpoint.blocked)

    def test_resume_refuses_staging_changes_even_when_file_contents_match(self):
        (self.repo / 'tracked.txt').write_text('original dirty input\n')
        checkpoint, workspace = self.save_partial()
        self.git.run('add', 'tracked.txt')
        runner = self.runner(Mock())
        runner.checkpoint = checkpoint
        runner.options.resume = True
        with self.assertRaisesRegex(ResumeError, 'main index changed'):
            runner.run()
        self.assertTrue(workspace.recovery_path.exists())

    def test_starting_without_resume_cannot_silently_discard_saved_work(self):
        checkpoint, workspace = self.save_partial()
        executor = Mock()
        runner = self.runner(executor)
        runner.checkpoint = checkpoint
        with self.assertRaisesRegex(ResumeError, 'continue with:.*--resume'):
            runner.run()
        executor.run.assert_not_called()
        self.assertTrue(workspace.recovery_path.exists())

    def test_saved_work_cannot_be_resumed_concurrently(self):
        checkpoint, _ = self.save_partial(complete=True)
        recovery = checkpoint.blocked['task_recovery']
        with self.manager.resume('1: Build', recovery) as active:
            with self.assertRaisesRegex(ResumeError, 'already being resumed'):
                with self.manager.resume('1: Build', recovery):
                    self.fail('a second runner entered the same recovery')
            self.assertTrue(active.path.exists())
            active.promote(GitService(active.path).head_commit())

    def test_checkpoint_identity_change_does_not_erase_saved_work(self):
        checkpoint, workspace = self.save_partial()
        original = checkpoint.path.read_bytes()
        with self.assertRaisesRegex(ResumeError, 'another plan or base'):
            RunCheckpoint(checkpoint.path, self.git, identity='another plan', base_commit=self.initial_head)
        self.assertEqual(original, checkpoint.path.read_bytes())
        self.assertTrue(workspace.recovery_path.exists())

    def test_explicit_restart_uses_current_checkout_and_retains_old_bundle(self):
        checkpoint, workspace = self.save_partial()
        (self.repo / 'tracked.txt').write_text('operator correction\n')
        self.git.run('add', 'tracked.txt')
        self.git.run('commit', '-qm', 'operator correction')
        restarted = RunCheckpoint(checkpoint.path, self.git, identity='plan:test',
                                  base_commit=self.git.head_commit(), restart=True)
        self.assertFalse(restarted.blocked)
        self.assertTrue(workspace.recovery_path.exists())
        testcase = self

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                testcase.assertEqual('operator correction\n', (cwd / 'tracked.txt').read_text())
                testcase.assertFalse((cwd / 'feature.txt').exists())
                (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                task_git = GitService(cwd)
                task_git.run('add', '.')
                task_git.run('commit', '-qm', 'complete from corrected checkout')
                return ExecResult(output='done', returncode=0)

        runner = self.runner(Executor())
        runner.checkpoint = restarted
        runner.run()
        self.assertTrue(workspace.recovery_path.exists())

    def test_cli_reports_blocked_state_then_resumes_with_saved_work(self):
        self.plan.write_text(self.plan.read_text() + '### Task 2: Finish\n- [ ] Finish feature\n')
        self.git.run('add', 'plan.md')
        self.git.run('commit', '-qm', 'two task plan')
        calls = []
        testcase = self

        def execute(prompt, *, retry_guard=None, cwd=None):
            calls.append(cwd)
            task_git = GitService(cwd)
            if len(calls) == 1:
                plan = cwd / 'plan.md'
                plan.write_text(plan.read_text().replace('[ ]', '[x]', 2))
                task_git.run('add', 'plan.md')
                task_git.run('commit', '-qm', 'first task complete')
                return ExecResult(output='done', returncode=0)
            if len(calls) == 2:
                (cwd / 'partial.txt').write_text('valuable partial result\n')
                return ExecResult(output='service unavailable', returncode=1)
            testcase.assertEqual('valuable partial result\n', (cwd / 'partial.txt').read_text())
            testcase.assertIn('service restarted', prompt)
            (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
            task_git.run('add', '.')
            task_git.run('commit', '-qm', 'finish recovered task')
            return ExecResult(output='done', returncode=0)

        args = ['plan.md', '--no-branch', '--base-ref', 'HEAD', '--tasks-only', '--retry-count', '0',
                '--gigacode-command', 'unused-gigacode']
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, {'HOME': str(self.root / 'home')}),
              patch('gigaflex.cli.webbrowser.open_new_tab', return_value=True),
              patch('gigaflex.cli.GigaCodeExecutor.run', side_effect=execute),
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            self.assertEqual(1, main(args))
            state_path = self.repo / '.gigaflex/progress/status-plan.json'
            state = json.loads(state_path.read_text())
            self.assertEqual('blocked', state['status'])
            self.assertIn('--resume', state['resume_command'])
            self.assertIn('--allow-dirty', state['resume_command'])
            self.assertTrue(Path(state['recovery_path']).exists())
            self.assertIn('Needs attention:', stderr.getvalue())
            self.assertEqual(1, main(args))
            self.assertEqual(2, len(calls))
            command = shlex.split(state['resume_command'])
            resume_args = command[command.index('gigaflex') + 1:]
            result = main([*resume_args, '--resume-note', 'service restarted'])
            self.assertEqual(0, result, stderr.getvalue())
        self.assertEqual(3, len(calls))
        self.assertEqual('success', json.loads(state_path.read_text())['status'])
        checkpoint = json.loads((self.repo / '.gigaflex/progress/checkpoint-plan.json').read_text())
        self.assertNotIn('blocked', checkpoint)
        self.assertEqual('valuable partial result\n', (self.repo / 'partial.txt').read_text())
