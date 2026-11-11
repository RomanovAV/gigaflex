import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.executor import ExecResult
from gigaflex.git import GitError, GitService
from gigaflex.checkpoint import ResumeError, RunCheckpoint
from gigaflex.review import build_review_decision_records, identify_review_findings, parse_synthesis_output
import test_phase_verification as phases
from test_phase_verification import Callback, FINDING, decision
from test_task_recovery import TaskRepositoryCase


class ReviewReliabilityTest(TaskRepositoryCase):
    make_runner = phases.PhaseVerificationTest.make_runner
    finding_then_clean = phases.PhaseVerificationTest.finding_then_clean

    def test_equivalent_claims_merge_with_strongest_severity_and_all_agents(self):
        minor = FINDING.replace('severity: major', 'severity: minor')
        findings = identify_review_findings({'documentation': minor, 'quality': FINDING})
        self.assertEqual(1, len(findings))
        self.assertEqual(('documentation', 'quality'), findings[0].agents)
        self.assertEqual('major', findings[0].finding.severity)
        ledger = parse_synthesis_output(decision().output, ['F001'])
        records = build_review_decision_records(findings, ledger)
        self.assertEqual(['documentation', 'quality'], [item.agent for item in records])

    def test_different_issues_on_same_line_are_never_merged(self):
        other = FINDING.replace('original instead of fixed', 'empty instead of original')
        self.assertEqual(2, len(identify_review_findings({'quality': FINDING, 'testing': other})))

    def test_case_sensitive_paths_and_evidence_literals_are_not_merged(self):
        for other in (FINDING.replace('tracked.txt', 'TRACKED.txt'),
                      FINDING.replace('original instead', 'ORIGINAL instead')):
            with self.subTest(finding=other):
                self.assertEqual(2, len(identify_review_findings({'quality': FINDING, 'testing': other})))

    def context_finding(self, severity='minor', category='documentation', file='plan.md'):
        return (FINDING.replace('severity: major', f'severity: {severity}')
                .replace('category: correctness', f'category: {category}')
                .replace('file: tracked.txt', f'file: {file}'))

    def test_minor_protected_context_advisory_passes_with_durable_report(self):
        reviewer = Callback(lambda *args: ExecResult(output=self.context_finding()))
        synthesis = Callback(lambda *args: decision('advisory'))
        runner = self.make_runner(synthesis, reviewer)
        events = []
        runner.log.event_callback = lambda kind, **data: events.append((kind, data))
        runner.run()
        self.assertEqual(self.initial_head, self.git.head_commit())
        self.assertEqual(1, len(synthesis.calls))
        self.assertEqual(1, len(reviewer.calls))
        report = json.loads(runner.advisory_report.read_text())
        self.assertEqual('plan.md', report[0]['file'])
        self.assertTrue(any(kind == 'review_advisories' for kind, _ in events))
        self.assertFalse(self.git.is_dirty())

    def test_advisory_cannot_hide_executable_deliverable_major_or_requirements_defect(self):
        for severity, category, file in [('minor', 'documentation', 'tracked.txt'),
                                         ('major', 'documentation', 'plan.md'),
                                         ('minor', 'requirements', 'plan.md'),
                                         ('blocker', 'traceability', 'plan.md')]:
            with self.subTest(severity=severity, category=category, file=file):
                reviewer = Callback(lambda *args: ExecResult(output=self.context_finding(severity, category, file)))
                with self.assertRaisesRegex(GitError, 'invalid advisory'):
                    self.make_runner(Callback(lambda *args: decision('advisory')), reviewer).run()
                self.assertEqual(self.initial_head, self.git.head_commit())

    def test_real_contract_ambiguity_remains_blocked_and_preserves_recovery(self):
        reviewer = Callback(lambda *args: ExecResult(output=self.context_finding('major', 'requirements')))
        synthesis = Callback(lambda *args: decision('blocked'))
        with self.assertRaisesRegex(GitError, 'review synthesis blocked') as raised:
            self.make_runner(synthesis, reviewer).run()
        self.assertTrue((Path(raised.exception.task_recovery['directory']) / 'task.bundle').exists())

    def test_unresolved_unchanged_candidate_stops_before_another_review(self):
        reviewer = self.finding_then_clean()
        synthesis = Callback(lambda *args: decision('confirmed'))
        with self.assertRaisesRegex(GitError, 'review made no progress') as raised:
            self.make_runner(synthesis, reviewer).run()
        self.assertEqual(1, len(reviewer.calls))
        self.assertEqual(1, len(synthesis.calls))
        packet = raised.exception.task_recovery
        self.assertTrue(packet['phase_context']['no_progress'])
        checkpoint = RunCheckpoint(self.repo / '.gigaflex/checkpoint.json', self.git,
                                   identity='review', base_commit=self.initial_head)
        checkpoint.mark_blocked('no progress', 'review', 'gigaflex --resume', packet)
        resumed = self.make_runner()
        resumed.options.resume = True
        resumed.checkpoint = checkpoint
        with self.assertRaisesRegex(ResumeError, 'supply --resume-note'):
            resumed.run()
        self.assertFalse(resumed.review_agent_executor.calls)
        self.assertTrue((Path(packet['directory']) / 'task.bundle').exists())

    def test_empty_commit_cannot_disguise_no_progress(self):
        def synth(prompt, cwd, n):
            GitService(cwd).run('commit', '--allow-empty', '-qm', 'no actual fix')
            return decision()
        with self.assertRaisesRegex(GitError, 'review made no progress'):
            self.make_runner(Callback(synth), self.finding_then_clean()).run()
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_parallel_no_progress_deduplicates_and_does_not_start_second_batch(self):
        class Reviewers:
            calls = 0
            def run_batch(inner, prompts, *, workdirs=None):
                inner.calls += 1
                return {name: ExecResult(output=FINDING) for name in prompts}
        reviewer = Reviewers()
        synthesis = Callback(lambda *args: decision('confirmed'))
        runner = self.make_runner(synthesis, reviewer)
        runner.options.parallel_review = True
        with self.assertRaisesRegex(GitError, 'review made no progress'):
            runner.run()
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(1, len(synthesis.calls))
        self.assertIn('id="F001"', synthesis.calls[0][0])
        self.assertNotIn('id="F002"', synthesis.calls[0][0])

    def test_synthesis_resume_uses_updated_context_then_fresh_review_and_finalize(self):
        source = self.repo / 'openspec/changes/demo'
        source.mkdir(parents=True)
        context = source / 'design.md'
        context.write_text('old context\n')
        self.git.run('add', '.')
        self.git.run('commit', '-qm', 'context')
        self.initial_head = self.git.head_commit()
        def configure(runner):
            runner.options.plan_kind = 'openspec'
            runner.options.plan_source = source
            runner.options.plan_context_files = (context,)
            return runner
        def fail(prompt, cwd, n):
            (cwd / 'valuable.txt').write_text('saved work\n')
            return decision()
        first = configure(self.make_runner(Callback(fail), self.finding_then_clean(), retries=0))
        with self.assertRaises(GitError) as raised:
            first.run()
        packet = raised.exception.task_recovery
        checkpoint = RunCheckpoint(self.repo / '.gigaflex/checkpoint.json', self.git,
                                   identity='phases', base_commit=self.initial_head)
        checkpoint.mark_blocked('unfinished synthesis', 'review', 'gigaflex --resume', packet)
        context.write_text('operator context\n')
        def resume(prompt, cwd, n):
            self.assertEqual('saved work\n', (cwd / 'valuable.txt').read_text())
            self.assertEqual('operator context\n', (cwd / context.relative_to(self.repo)).read_text())
            git = GitService(cwd)
            git.run('add', 'valuable.txt')
            git.run('commit', '-qm', 'validated saved work')
            return decision()
        runner = configure(self.make_runner(Callback(resume)))
        runner.checkpoint = checkpoint
        runner.options.resume = True
        runner.run()
        self.assertEqual(1, len(runner.synthesis_executor.calls))
        self.assertEqual(1, len(runner.review_agent_executor.calls))
        self.assertEqual(1, len(runner.finalize_executor.calls))
        self.assertEqual('saved work\n', (self.repo / 'valuable.txt').read_text())
        self.assertEqual('operator context\n', context.read_text())
        self.assertFalse(checkpoint.blocked)
        self.assertTrue((Path(packet['directory']) / 'task.bundle').exists())

    def test_readonly_mutation_names_changed_file_head_and_index_in_event(self):
        def review(prompt, cwd, n):
            target = cwd / 'tracked.txt'
            target.write_text('unexpected\n')
            GitService(cwd).run('add', 'tracked.txt')
            return ExecResult(output='NO FINDINGS')
        runner = self.make_runner(reviewer=Callback(review))
        events = []
        runner.log.event_callback = lambda kind, **data: events.append((kind, data))
        with self.assertRaisesRegex(RuntimeError, "staged_files: 'tracked.txt'"):
            runner.run()
        report = next(data for kind, data in events if kind == 'repository_change')
        self.assertEqual(['tracked.txt'], report['files'])
        self.assertNotEqual(report['before']['index'], report['after']['index'])
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_main_index_only_mutation_is_named_after_review(self):
        (self.repo / 'tracked.txt').write_text('user input\n')
        runner = self.make_runner()
        runner.run_review()
        self.git.run('add', 'tracked.txt')
        with self.assertRaisesRegex(RuntimeError, "staged_files: 'tracked.txt'"):
            runner.run_finalize()
