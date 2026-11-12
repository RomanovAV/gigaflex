import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.cli import main
from gigaflex.executor import ExecResult
from gigaflex.git import GitService
from gigaflex.history import history_root
from gigaflex.interaction import Launch
from test_task_recovery import TaskRepositoryCase


class CliInteractionTest(TaskRepositoryCase):
    def setUp(self):
        super().setUp()
        with (self.repo / '.gitignore').open('a') as stream:
            stream.write('.DS_Store\n')
        self.git.run('add', '.gitignore')
        self.git.run('commit', '-qm', 'runtime ignores')
        self.initial_head = self.git.head_commit()
        self.args = ['plan.md', '--no-branch', '--base-ref', self.initial_head,
                     '--tasks-only', '--retry-count', '0', '--no-open-dashboard', '--gigacode-command', 'unused']

    def environment(self, execute, *, interactive=True):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ, {'HOME': str(self.root / 'home')}))
        stack.enter_context(patch('gigaflex.cli.interactive_available', return_value=interactive))
        stack.enter_context(patch('gigaflex.cli.GigaCodeExecutor.run', side_effect=execute))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack

    def complete(self, prompt, *, cwd=None, retry_guard=None):
        plan = cwd / 'plan.md'
        plan.write_text(plan.read_text().replace('[ ]', '[x]'))
        git = GitService(cwd)
        git.run('add', '.')
        git.run('commit', '-qm', 'completed')
        return ExecResult(output='done')

    def results(self):
        return [json.loads(path.read_text()) for path in history_root(self.git).glob('*/*/result.json')]

    def test_terminal_resume_finalizes_failure_then_continues_without_command_copy(self):
        calls = []
        self.notification.return_value = {'terminal_bell_sent': True, 'desktop_requested': False, 'backend': ''}
        def execute(prompt, *, cwd=None, retry_guard=None):
            calls.append(cwd)
            partial = cwd / 'valuable.txt'
            if len(calls) == 1:
                partial.write_text('saved result\n')
                return ExecResult(output='dependency unavailable', returncode=1)
            self.assertEqual('saved result\n', partial.read_text())
            return self.complete(prompt, cwd=cwd)
        def choose(stopped):
            self.notification.assert_called_once_with(self.repo.name)
            records = self.results()
            self.assertEqual(1, len(records))
            self.assertEqual('blocked', records[0]['status'])
            self.assertTrue((Path(stopped.recovery) / 'task.bundle').exists())
            self.assertIn('--resume', stopped.launch.arguments)
            return stopped.launch
        with self.environment(execute), patch('gigaflex.cli.recovery_choice', side_effect=choose) as menu:
            self.assertEqual(0, main(self.args))
        self.assertEqual(2, len(calls))
        self.assertEqual(1, menu.call_count)
        self.notification.assert_called_once()
        records = self.results()
        first = next(item for item in records if item['status'] == 'blocked')
        second = next(item for item in records if item['status'] == 'success')
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(first['attempt_id'], second['parent_attempt_id'])
        self.assertTrue(first['notifications'][0]['data']['terminal_bell_sent'])
        self.assertEqual([], second['notifications'])
        self.assertEqual('saved result\n', (self.repo / 'valuable.txt').read_text())

    def test_menu_exit_and_ctrl_c_keep_saved_work(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                # A stopped run is intentionally reused; no model call is needed to
                # show its menu again on the second ordinary launch.
                def fail(prompt, *, cwd=None, retry_guard=None):
                    (cwd / 'valuable.txt').write_text('keep this\n')
                    return ExecResult(output='blocked dependency', returncode=1)
                with self.environment(fail), patch('gigaflex.cli.recovery_choice',
                        side_effect=KeyboardInterrupt if interrupt else lambda stopped: None):
                    self.assertEqual(130 if interrupt else 1, main(self.args))
                state = json.loads((self.repo / '.gigaflex/progress/checkpoint-plan.json').read_text())
                recovery = state['blocked']['task_recovery']['directory']
                self.assertTrue((Path(recovery) / 'task.bundle').exists())
                self.assertEqual(self.initial_head, self.git.head_commit())

    def test_non_tty_failure_never_opens_menu(self):
        with self.environment(lambda *args, **kwargs: ExecResult(output='failure', returncode=1), interactive=False), \
                patch('gigaflex.cli.recovery_choice') as menu:
            self.assertEqual(1, main(self.args))
            menu.assert_not_called()

    def test_bare_launch_runs_selected_task_then_returns_to_start_menu(self):
        launch = Launch(Path.cwd(), tuple(self.args))
        with self.environment(self.complete), \
                patch('gigaflex.cli.startup_choice', side_effect=[launch, None]) as menu:
            self.assertEqual(0, main([]))
        self.assertEqual(2, menu.call_count)
        self.assertIn('[x]', self.plan.read_text())

    def test_dirty_checkout_cancel_does_not_launch_agent_or_change_files(self):
        target = self.repo / 'tracked.txt'
        target.write_text('user work\n')
        with self.environment(self.complete), patch('gigaflex.cli.ask', return_value='cancel'), \
                patch('gigaflex.cli.GigaCodeExecutor.run') as execute:
            self.assertEqual(130, main(self.args))
            execute.assert_not_called()
        self.assertEqual('user work\n', target.read_text())
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_dirty_checkout_choice_preserves_untouched_user_changes(self):
        target = self.repo / 'tracked.txt'
        target.write_text('user work\n')
        with self.environment(self.complete), patch('gigaflex.cli.ask', return_value='continue') as question:
            self.assertEqual(0, main(self.args))
        self.assertEqual(1, question.call_count)
        self.assertEqual('user work\n', target.read_text())
        self.assertIn('tracked.txt', self.git.run('status', '--short').stdout)

    def test_missing_review_base_is_selected_once_and_stored(self):
        arguments = ['--review', '--no-parallel-review', '--no-finalize', '--no-open-dashboard',
                     '--retry-count', '0', '--gigacode-command', 'unused']
        with self.environment(lambda *args, **kwargs: ExecResult(output='NO FINDINGS')), \
                patch('gigaflex.cli.choose_review_base', return_value=self.initial_head) as choose:
            self.assertEqual(0, main(arguments))
            self.assertEqual(0, main(arguments))
        self.assertEqual(1, choose.call_count)
        self.assertEqual(self.initial_head, self.git.branch_baseline(self.git.current_branch()).base_commit)

    def test_missing_planning_skill_can_use_standard_plan_creation(self):
        def draft(prompt, *, cwd=None, retry_guard=None):
            return ExecResult(output='# Plan\n\n### Task 1: Build\n- [ ] Implement\n')
        arguments = ['--plan', 'build the report', '--no-branch', '--retry-count', '0',
                     '--gigacode-command', 'unused']
        with self.environment(draft), \
                patch('gigaflex.cli.should_use_interactive_plan', side_effect=lambda args: bool(args.plan and not args.quick)), \
                patch('gigaflex.cli.planning_skill_installed', return_value=False), \
                patch('gigaflex.cli.ask', return_value='quick') as question:
            self.assertEqual(0, main(arguments))
        self.assertEqual(1, question.call_count)
        self.assertTrue(list((self.repo / 'docs/plans').glob('*.md')))

    def test_recovered_timeout_and_success_do_not_notify(self):
        def complete_then_timeout(prompt, *, cwd=None, retry_guard=None):
            result = self.complete(prompt, cwd=cwd)
            result.returncode = -9
            result.timed_out = True
            return result
        with self.environment(complete_then_timeout):
            self.assertEqual(0, main(self.args))
        self.notification.assert_not_called()

    def test_deliberate_interrupt_preserves_work_without_notification(self):
        def interrupt(prompt, *, cwd=None, retry_guard=None):
            (cwd / 'valuable.txt').write_text('keep after interruption\n')
            raise KeyboardInterrupt
        with self.environment(interrupt):
            self.assertEqual(130, main(self.args))
        self.notification.assert_not_called()
        state = json.loads((self.repo / '.gigaflex/progress/checkpoint-plan.json').read_text())
        self.assertTrue((Path(state['blocked']['task_recovery']['directory']) / 'task.bundle').exists())

    def test_notification_failure_does_not_hide_original_blocker_or_recovery(self):
        self.notification.side_effect = RuntimeError('notifier unavailable')
        with self.environment(lambda *args, **kwargs: ExecResult(output='original failure', returncode=1), interactive=False):
            self.assertEqual(1, main(self.args))
        state = json.loads((self.repo / '.gigaflex/progress/checkpoint-plan.json').read_text())
        self.assertTrue((Path(state['blocked']['task_recovery']['directory']) / 'task.bundle').exists())
        self.assertEqual('blocked', self.results()[0]['status'])

    def test_notifications_can_be_disabled_without_disabling_recovery(self):
        config = self.root / 'silent-config'
        config.write_text('[gigaflex]\nnotifications = false\n')
        with self.environment(lambda *args, **kwargs: ExecResult(output='original failure', returncode=1), interactive=False):
            self.assertEqual(1, main([*self.args, '--config', str(config)]))
        self.notification.assert_not_called()
        result = self.results()[0]
        self.assertEqual('blocked', result['status'])
        self.assertEqual('disabled', result['notifications'][0]['data']['skipped'])
        state = json.loads((self.repo / '.gigaflex/progress/checkpoint-plan.json').read_text())
        self.assertTrue((Path(state['blocked']['task_recovery']['directory']) / 'task.bundle').exists())
