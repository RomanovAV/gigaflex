from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.executor import ExecResult
from gigaflex.git import GitError, GitService
from gigaflex.signals import FINALIZE_DONE
import test_phase_verification as phases
from test_phase_verification import Callback, decision, commit
from test_task_recovery import TaskRepositoryCase


class ProcessRecoveryTest(TaskRepositoryCase):
    make_runner = phases.PhaseVerificationTest.make_runner
    finding_then_clean = phases.PhaseVerificationTest.finding_then_clean

    def test_synthesis_complete_committed_result_survives_process_timeout(self):
        def synth(prompt, cwd, n):
            commit(cwd)
            return replace(decision(), returncode=-9, timed_out=True)
        synthesis = Callback(synth)
        reviewer = self.finding_then_clean()
        runner = self.make_runner(synthesis, reviewer)
        runner.run()
        self.assertEqual(1, len(synthesis.calls))
        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())
        self.assertIn('use complete validated result', runner.log.path.read_text())

    def test_partial_synthesis_restarts_in_same_workspace_and_preserves_work(self):
        def synth(prompt, cwd, n):
            if n == 1:
                (cwd / 'valuable.txt').write_text('partial result\n')
                return ExecResult(output='partial response', returncode=-11, dependency_crash=True)
            self.assertIn('finish the pending work', prompt)
            self.assertEqual('partial result\n', (cwd / 'valuable.txt').read_text())
            git = GitService(cwd)
            git.run('add', 'valuable.txt')
            git.run('commit', '-qm', 'preserved partial result')
            commit(cwd)
            return decision()
        synthesis = Callback(synth)
        runner = self.make_runner(synthesis, self.finding_then_clean())
        runner.run()
        self.assertEqual(2, len(synthesis.calls))
        self.assertEqual(synthesis.calls[0][1], synthesis.calls[1][1])
        self.assertEqual('partial result\n', (self.repo / 'valuable.txt').read_text())

    def test_exhausted_synthesis_process_recovery_is_bounded_and_saves_work(self):
        def synth(prompt, cwd, n):
            (cwd / 'valuable.txt').write_text('save after exhaustion\n')
            return ExecResult(output='incomplete', returncode=-9, timed_out=True)
        synthesis = Callback(synth)
        with self.assertRaisesRegex(GitError, 'process recovery exhausted') as raised:
            self.make_runner(synthesis, self.finding_then_clean()).run()
        self.assertEqual(2, len(synthesis.calls))
        restored, _, _ = self.recover(Path(raised.exception.task_recovery['directory']))
        self.assertEqual('save after exhaustion\n', (restored / 'valuable.txt').read_text())
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_permission_and_rate_limit_exhaustion_do_not_trigger_extra_process_restarts(self):
        for failure in (ExecResult(output='', returncode=1, approval_denied=True),
                        ExecResult(output='', returncode=1, rate_limited=True)):
            with self.subTest(failure=failure):
                synthesis = Callback(lambda *args: failure)
                with self.assertRaises(GitError):
                    self.make_runner(synthesis, self.finding_then_clean()).run()
                self.assertEqual(1, len(synthesis.calls))

    def test_single_readonly_reviewer_restarts_after_timeout(self):
        reviewer = Callback(lambda p, cwd, n: ExecResult(output='', returncode=-9, timed_out=True) if n == 1
                            else ExecResult(output='NO FINDINGS'))
        runner = self.make_runner(reviewer=reviewer)
        runner.run()
        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual(reviewer.calls[0][1], reviewer.calls[1][1])
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_only_failed_parallel_reviewer_is_restarted(self):
        class Reviewers:
            batches = 0
            replacements = []
            def run_batch(inner, prompts, *, workdirs=None):
                inner.batches += 1
                inner.failed_path = workdirs['quality']
                return {name: ExecResult(output='', returncode=-9, timed_out=True) if name == 'quality'
                        else ExecResult(output='NO FINDINGS') for name in prompts}
            def run(inner, prompt, *, cwd=None, retry_guard=None):
                inner.replacements.append(cwd)
                return ExecResult(output='NO FINDINGS')
        reviewer = Reviewers()
        runner = self.make_runner(reviewer=reviewer)
        runner.options.parallel_review = True
        runner.run()
        self.assertEqual(1, reviewer.batches)
        self.assertEqual([reviewer.failed_path], reviewer.replacements)

    def test_mutated_readonly_reviewer_is_detected_before_replacement(self):
        class Reviewers:
            replacements = 0
            def run_batch(inner, prompts, *, workdirs=None):
                (workdirs['quality'] / 'tracked.txt').write_text('unauthorized mutation\n')
                return {name: ExecResult(output='', returncode=-11, dependency_crash=True) if name == 'quality'
                        else ExecResult(output='NO FINDINGS') for name in prompts}
            def run(inner, *args, **kwargs):
                inner.replacements += 1
                return ExecResult(output='NO FINDINGS')
        reviewer = Reviewers()
        runner = self.make_runner(reviewer=reviewer)
        runner.options.parallel_review = True
        with self.assertRaisesRegex(RuntimeError, 'read-only reviewer quality modified'):
            runner.run()
        self.assertEqual(0, reviewer.replacements)

    def test_parallel_crash_label_never_overrides_permission_failure(self):
        class Reviewers:
            replacements = 0
            def run_batch(inner, prompts, *, workdirs=None):
                return {name: ExecResult(output='', returncode=-11, dependency_crash=True, approval_denied=True)
                        if name == 'quality' else ExecResult(output='NO FINDINGS') for name in prompts}
            def run(inner, *args, **kwargs):
                inner.replacements += 1
                return ExecResult(output='NO FINDINGS')
        reviewer = Reviewers()
        runner = self.make_runner(reviewer=reviewer)
        runner.options.parallel_review = True
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            runner.run()
        self.assertEqual(0, reviewer.replacements)

    def test_finalize_timeout_restarts_verification_without_requesting_code_repairs(self):
        finalizer = Callback(lambda p, cwd, n: ExecResult(output='', returncode=-9, timed_out=True) if n == 1
                             else ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE))
        synthesis = Callback(lambda *args: self.fail('process failure must not request code changes'))
        runner = self.make_runner(synthesis=synthesis, finalizer=finalizer)
        runner.run()
        self.assertEqual(2, len(finalizer.calls))
        self.assertEqual(finalizer.calls[0][1], finalizer.calls[1][1])
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_complete_readonly_finalize_result_survives_transport_exit(self):
        finalizer = Callback(lambda *args: ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE,
                                                     returncode=-9, timed_out=True))
        runner = self.make_runner(finalizer=finalizer)
        runner.run()
        self.assertEqual(1, len(finalizer.calls))
        self.assertIn('use complete read-only verification result', runner.log.path.read_text())

    def test_complete_committed_finalize_result_needs_review_not_another_code_repair(self):
        def finalize(prompt, cwd, n):
            if n == 1:
                commit(cwd)
                return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE, returncode=-9, timed_out=True)
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE)
        finalizer = Callback(finalize)
        synthesis = Callback(lambda *args: self.fail('the committed result already passed its checks'))
        runner = self.make_runner(synthesis=synthesis, finalizer=finalizer)
        runner.run()
        self.assertEqual(2, len(finalizer.calls))
        self.assertEqual(2, len(runner.review_agent_executor.calls))
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())

    def test_exhausted_finalize_restart_budget_stops_without_code_repair_calls(self):
        finalizer = Callback(lambda *args: ExecResult(output='', returncode=-9, timed_out=True))
        synthesis = Callback(lambda *args: self.fail('no code defect was established'))
        runner = self.make_runner(synthesis=synthesis, finalizer=finalizer)
        with self.assertRaisesRegex(GitError, 'automatic process recovery exhausted'):
            runner.run()
        self.assertEqual(2, len(finalizer.calls))
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_committed_finalize_repair_survives_crash_and_receives_fresh_review(self):
        def finalize(prompt, cwd, n):
            if n == 1:
                (cwd / 'tracked.txt').write_text('needs committed repair\n')
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE)
        def repair(prompt, cwd, n):
            commit(cwd)
            return ExecResult(output='', returncode=-11, dependency_crash=True)
        synthesis, finalizer = Callback(repair), Callback(finalize)
        runner = self.make_runner(synthesis=synthesis, finalizer=finalizer)
        runner.run()
        self.assertEqual(1, len(synthesis.calls))
        self.assertEqual(2, len(runner.review_agent_executor.calls))
        self.assertEqual(2, len(finalizer.calls))
        self.assertEqual('fixed\n', (self.repo / 'tracked.txt').read_text())
