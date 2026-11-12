import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.executor import ExecResult, GigaCodeExecutor
from gigaflex.git import GitError, GitService, TaskWorktreeManager
from gigaflex.progress import ProgressLog
from gigaflex.runner import RunOptions, Runner


class TaskRepositoryCase(unittest.TestCase):
    def setUp(self):
        notifications = patch('gigaflex.cli.notify_attention', return_value={})
        self.notification = notifications.start()
        self.addCleanup(notifications.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        os.chdir(self.repo)
        self.git = GitService(self.repo)
        self.git.run('init', '-q')
        self.git.run('config', 'user.name', 'Test')
        self.git.run('config', 'user.email', 'test@example.com')
        self.plan = self.repo / 'plan.md'
        self.plan.write_text('# Plan\n### Task 1: Build\n- [ ] Implement\n- [ ] Validate\n')
        (self.repo / '.gitignore').write_text('/.gigaflex/\nbuild/\n')
        (self.repo / 'tracked.txt').write_text('original\n')
        (self.repo / 'deleted.txt').write_text('delete me\n')
        self.git.run('add', '.')
        self.git.run('commit', '-qm', 'initial')
        self.initial_head = self.git.head_commit()
        self.messages = []
        self.manager = TaskWorktreeManager(self.git, diagnostic=self.messages.append, temp_parent=self.root)

    def runner(self, executor, retries=1):
        progress = self.repo / '.gigaflex/progress/progress-test.txt'
        return Runner(
            RunOptions(plan_file=self.plan, progress_file=progress, tasks_only=True,
                       finalize_enabled=False, task_completion_retries=retries, delay_seconds=0,
                       allow_dirty=True),
            executor, ProgressLog(progress), task_worktrees=self.manager,
        )

    def recover(self, directory):
        manifest = json.loads((directory / 'manifest.json').read_text())
        bundle = directory / manifest['bundle']
        self.git.run('bundle', 'verify', str(bundle))
        self.assertEqual('', self.git.run('for-each-ref', '--format=%(refname)',
                                        'refs/gigaflex/recovery/').stdout)
        prefix = manifest['refs']['head'].rsplit('/', 1)[0]
        self.git.run('fetch', '--no-tags', str(bundle), f'{prefix}/*:{prefix}/*')
        restored = self.root / 'restored'
        self.git.add_detached_worktree(restored, manifest['refs']['worktree'])
        self.addCleanup(self.git.remove_worktree, restored)
        restored_git = GitService(restored)
        restored_git.run('reset', '--mixed', manifest['commits']['head'])
        restored_git.run('read-tree', manifest['refs']['index'])
        return restored, restored_git, manifest


class TaskRecoveryTest(TaskRepositoryCase):
    def test_missing_commit_and_dirty_result_are_repaired_in_same_workspace(self):
        for mode in ('missing_commit', 'dirty_result'):
            with self.subTest(mode=mode):
                # Each mode starts from the previous committed state with a new task.
                self.plan.write_text('# Plan\n### Task 1: Build\n- [ ] Implement\n- [ ] Validate\n')
                self.git.run('add', 'plan.md')
                self.git.run('commit', '--allow-empty', '-qm', 'next input')
                head_before = self.git.head_commit()
                calls = []
                testcase = self

                class Executor:
                    def run(self, prompt, *, retry_guard=None, cwd=None):
                        calls.append((prompt, cwd))
                        task_git = GitService(cwd)
                        if len(calls) == 1:
                            (cwd / 'plan.md').write_text(
                                (cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                            (cwd / 'result.txt').write_text(mode + '\n')
                            if mode == 'dirty_result':
                                task_git.run('add', 'plan.md')
                                task_git.run('commit', '-qm', 'checklist only')
                        else:
                            testcase.assertEqual(head_before, testcase.git.head_commit())
                            testcase.assertIn('Completion requirements still unsatisfied:', prompt)
                            testcase.assertIn('without creating a commit' if mode == 'missing_commit'
                                              else 'result.txt', prompt)
                            task_git.run('add', 'plan.md', 'result.txt')
                            task_git.run('commit', '-qm', 'complete result')
                        return ExecResult(output='done', returncode=0)

                self.runner(Executor()).run_tasks()
                self.assertEqual(2, len(calls))
                self.assertEqual(calls[0][1], calls[1][1])
                self.assertFalse(calls[0][1].exists())
                self.assertEqual(mode + '\n', (self.repo / 'result.txt').read_text())
                self.assertEqual('', self.git.run('status', '--short').stdout)
                self.assertFalse((self.repo / '.git/gigaflex/recovery').exists())

    def test_exhausted_task_keeps_uncommitted_output_in_recovery(self):
        calls = []

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                if len(calls) == 1:
                    (cwd / 'plan.md').write_text((cwd / 'plan.md').read_text().replace('[ ]', '[x]'))
                    task_git = GitService(cwd)
                    task_git.run('add', 'plan.md')
                    task_git.run('commit', '-qm', 'checklist')
                    (cwd / 'valuable-result.txt').write_text('retain this\n')
                return ExecResult(output='done', returncode=0)

        with self.assertRaisesRegex(GitError, 'left new uncommitted changes.*task recovery saved to:'):
            self.runner(Executor()).run_tasks()
        self.assertEqual(2, len(calls))
        self.assertFalse(calls[0].exists())
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertIn('[ ] Validate', self.plan.read_text())
        directories = list((self.repo / '.git/gigaflex/recovery').iterdir())
        self.assertEqual(1, len(directories))
        restored, _, manifest = self.recover(directories[0])
        self.assertEqual('retain this\n', (restored / 'valuable-result.txt').read_text())
        self.assertIn('left new uncommitted changes', manifest['reason'])

    def test_prose_task_with_marker_but_no_commit_does_not_duplicate_marker(self):
        change = self.repo / 'openspec/changes/analyze'
        change.mkdir(parents=True)
        tasks = change / 'tasks.md'
        tasks.write_text('## Задача 1: Анализ\n\nПроверить источники.\n')
        self.git.run('add', str(tasks))
        self.git.run('commit', '-qm', 'prose plan')
        calls = []
        testcase = self

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                plan = cwd / tasks.relative_to(testcase.repo)
                if len(calls) == 1:
                    plan.write_text(plan.read_text().replace(
                        '## Задача 1: Анализ\n', '## Задача 1: Анализ\n- [x] 1. Анализ\n'))
                else:
                    correction = prompt.split('Required checklist correction:', 1)[1]
                    testcase.assertNotIn('<COMPLETION_MARKER>', correction)
                    task_git = GitService(cwd)
                    task_git.run('add', str(plan))
                    task_git.run('commit', '-qm', 'validated prose task')
                return ExecResult(output='done', returncode=0)

        runner = self.runner(Executor())
        runner.options.plan_file = tasks
        runner.options.plan_kind = 'openspec'
        runner.options.plan_source = change
        runner.run_tasks()
        self.assertEqual(2, len(calls))
        self.assertEqual(1, tasks.read_text().count('- [x] 1. Анализ'))

    def test_transport_retry_does_not_adopt_committed_requirement_changes(self):
        executor = GigaCodeExecutor(retry_count=2, retry_delay=0, output=lambda _line: None)

        def fail_after_changing_contract(_prompt, _output, _session, *, cwd=None):
            plan = cwd / 'plan.md'
            plan.write_text(plan.read_text().replace('- [ ] Validate\n', ''))
            task_git = GitService(cwd)
            task_git.run('add', 'plan.md')
            task_git.run('commit', '-qm', 'changed requirement')
            return ExecResult(output='timeout', returncode=-9, timed_out=True)

        with patch.object(executor, '_run_once', side_effect=fail_after_changing_contract) as run_once:
            with self.assertRaisesRegex(GitError, 'task recovery saved to:'):
                self.runner(executor, retries=0).run_tasks()
        run_once.assert_called_once()
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertIn('- [ ] Validate', self.plan.read_text())

    def test_deleted_requirement_is_rejected_when_correction_budget_is_zero(self):
        calls = []

        class Executor:
            def run(self, prompt, *, retry_guard=None, cwd=None):
                calls.append(cwd)
                plan = cwd / 'plan.md'
                plan.write_text(plan.read_text().replace('[ ] Implement', '[x] Implement')
                                .replace('- [ ] Validate\n', ''))
                task_git = GitService(cwd)
                task_git.run('add', 'plan.md')
                task_git.run('commit', '-qm', 'changed requirements')
                return ExecResult(output='done', returncode=0)

        with self.assertRaisesRegex(GitError, 'modified protected plan content.*task recovery saved to:'):
            self.runner(Executor(), retries=0).run_tasks()
        self.assertEqual(1, len(calls))
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertIn('- [ ] Validate', self.plan.read_text())
        self.assertEqual('', self.git.run('status', '--short').stdout)

    def test_bundle_restores_commits_staging_binary_symlink_and_deletion(self):
        (self.repo / 'tracked.txt').write_text('pre-existing user input\n')
        main_status = self.git.run('status', '--porcelain').stdout
        with self.assertRaisesRegex(GitError, 'injected failure.*task recovery saved to:'):
            with self.manager.create('task 1') as workspace:
                task_git = GitService(workspace.path)
                (workspace.path / 'feature.txt').write_text('committed result\n')
                task_git.run('add', 'feature.txt')
                task_git.run('commit', '-qm', 'feature')
                task_head = task_git.head_commit()
                (workspace.path / 'tracked.txt').write_text('staged\n')
                task_git.run('add', 'tracked.txt')
                (workspace.path / 'tracked.txt').write_text('unstaged\n')
                (workspace.path / 'build').mkdir()
                force_added = workspace.path / 'build/force.txt'
                force_added.write_text('ignored path staged\n')
                task_git.run('add', '--force', 'build/force.txt')
                force_added.write_text('ignored path unstaged\n')
                (workspace.path / 'deleted.txt').unlink()
                task_git.run('add', 'deleted.txt')
                (workspace.path / 'résult.bin').write_bytes(b'\x00\xff\xfe\x01')
                (workspace.path / 'link').symlink_to('feature.txt')
                executable = workspace.path / 'run.sh'
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o755)
                expected_status = task_git.run('status', '--porcelain').stdout
                raise RuntimeError('injected failure')
        self.assertFalse(workspace.path.exists())
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertEqual(main_status, self.git.run('status', '--porcelain').stdout)
        self.assertEqual('pre-existing user input\n', (self.repo / 'tracked.txt').read_text())
        restored, restored_git, manifest = self.recover(workspace.recovery_path)
        self.assertEqual(task_head, restored_git.head_commit())
        self.assertEqual('committed result\n', (restored / 'feature.txt').read_text())
        self.assertEqual('staged\n', restored_git.run('show', ':tracked.txt').stdout)
        self.assertEqual('unstaged\n', (restored / 'tracked.txt').read_text())
        self.assertEqual('ignored path staged\n', restored_git.run('show', ':build/force.txt').stdout)
        self.assertEqual('ignored path unstaged\n', (restored / 'build/force.txt').read_text())
        self.assertEqual(b'\x00\xff\xfe\x01', (restored / 'résult.bin').read_bytes())
        self.assertEqual('feature.txt', os.readlink(restored / 'link'))
        self.assertTrue((restored / 'run.sh').stat().st_mode & 0o111)
        self.assertFalse((restored / 'deleted.txt').exists())
        self.assertEqual(expected_status, restored_git.run('status', '--porcelain').stdout)
        self.assertEqual(self.initial_head, manifest['base_commit'])
        self.assertIn('event=recovery_saved', '\n'.join(self.messages))

    def test_interrupt_saves_work_before_cleanup(self):
        with self.assertRaisesRegex(KeyboardInterrupt, 'task recovery saved to:'):
            with self.manager.create('interrupted task') as workspace:
                (workspace.path / 'partial.txt').write_text('partial\n')
                raise KeyboardInterrupt()
        self.assertFalse(workspace.path.exists())
        restored, _, _ = self.recover(workspace.recovery_path)
        self.assertEqual('partial\n', (restored / 'partial.txt').read_text())

    def test_recovery_can_restore_a_task_head_reset_to_the_bundle_prerequisite(self):
        with self.assertRaisesRegex(GitError, 'task recovery saved to:'):
            with self.manager.create('reset task') as workspace:
                task_git = GitService(workspace.path)
                task_git.run('reset', '--hard', self.initial_head)
                (workspace.path / 'partial.txt').write_text('partial\n')
                raise RuntimeError('invalid task history')
        restored, restored_git, _ = self.recover(workspace.recovery_path)
        self.assertEqual(self.initial_head, restored_git.head_commit())
        self.assertEqual('partial\n', (restored / 'partial.txt').read_text())

    def test_recovery_failure_keeps_original_workspace(self):
        with patch('gigaflex.task_recovery.save_task_recovery', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(GitError, 'disk full; task worktree retained at:'):
                with self.manager.create('failed task') as workspace:
                    (workspace.path / 'only-copy.txt').write_text('valuable\n')
                    raise RuntimeError('agent failed')
        self.assertEqual('valuable\n', (workspace.path / 'only-copy.txt').read_text())
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertIn('event=recovery_failed', '\n'.join(self.messages))
        self.git.remove_worktree(workspace.path)
        self.git.prune_worktrees()

    def test_populated_nested_repository_keeps_original_workspace(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                with self.assertRaisesRegex(GitError, 'cannot bundle populated nested repository.*retained at:'):
                    with self.manager.create('nested repository') as workspace:
                        nested = workspace.path / 'nested'
                        nested.mkdir()
                        nested_git = GitService(nested)
                        nested_git.run('init', '-q')
                        nested_git.run('config', 'user.name', 'Test')
                        nested_git.run('config', 'user.email', 'test@example.com')
                        (nested / 'committed.txt').write_text('nested commit\n')
                        nested_git.run('add', '.')
                        nested_git.run('commit', '-qm', 'nested result')
                        (nested / 'partial.txt').write_text('nested partial work\n')
                        if staged:
                            GitService(workspace.path).run('add', 'nested')
                        raise RuntimeError('agent failed')
                self.assertEqual('nested commit\n', (nested / 'committed.txt').read_text())
                self.assertEqual('nested partial work\n', (nested / 'partial.txt').read_text())
                self.git.remove_worktree(workspace.path)
                self.git.prune_worktrees()

    def test_linked_checkout_saves_bundle_in_common_git_directory(self):
        linked = self.root / 'linked'
        self.git.add_detached_worktree(linked, self.initial_head)
        self.addCleanup(self.git.remove_worktree, linked)
        manager = TaskWorktreeManager(GitService(linked), temp_parent=self.root)
        with self.assertRaisesRegex(GitError, 'task recovery saved to:'):
            with manager.create('linked task') as workspace:
                (workspace.path / 'partial.txt').write_text('linked result\n')
                raise RuntimeError('agent failed')
        self.assertEqual((self.repo / '.git/gigaflex/recovery').resolve(),
                         workspace.recovery_path.parent)
        restored, _, manifest = self.recover(workspace.recovery_path)
        self.assertEqual('linked result\n', (restored / 'partial.txt').read_text())
        self.assertEqual(str(linked.resolve()), manifest['repository'])


if __name__ == '__main__':
    unittest.main()
