import contextlib
import io
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.interaction import (Launch, StoppedRun, choose_path, choose_review_base,
                                 parse_continuation, recovery_choice, saved_runs, startup_choice)
from test_task_recovery import TaskRepositoryCase


class InteractionTest(TaskRepositoryCase):
    def stopped(self, *, note=False):
        return StoppedRun(1, Launch(self.repo, ('plan.md', '--base-ref', self.initial_head, '--resume')),
                          'The dependency needs attention', '/saved/task.bundle', '/progress.txt', note)

    def test_continuation_is_parsed_as_exact_argv_without_shell_evaluation(self):
        directory = self.repo / 'path with spaces; $literal'
        directory.mkdir()
        arguments = ['plan with spaces.md', '--resume-note', 'fixed; $(not-a-command)', '--resume']
        command = f'cd {shlex.quote(str(directory))} && {shlex.join(["gigaflex", *arguments])}'
        self.assertEqual(Launch(directory, tuple(arguments)), parse_continuation(command))
        for invalid in (None, '', 'gigaflex --resume', 'cd . && gigaflex --resume',
                        'cd /tmp ; gigaflex --resume', 'cd /tmp && gigaflex --restart'):
            self.assertIsNone(parse_continuation(invalid))

    def test_saved_run_discovery_ignores_bad_or_unrelated_files(self):
        progress = self.repo / '.gigaflex/progress'
        progress.mkdir(parents=True)
        command = f'cd {shlex.quote(str(self.repo))} && gigaflex plan.md --resume'
        (progress / 'checkpoint-demo.json').write_text(json.dumps({'blocked': {'resume_command': command}}))
        (progress / 'checkpoint-bad.json').write_text('broken JSON')
        (progress / 'checkpoint-null.json').write_text(json.dumps({'blocked': {'resume_command': None}}))
        (progress / 'checkpoint-complete.json').write_text('{}')
        self.assertEqual([('demo', Launch(self.repo, ('plan.md', '--resume')))], saved_runs(progress))

    def test_start_menu_selects_openspec_without_exposing_flags(self):
        source = self.repo / 'openspec/changes/demo'
        source.mkdir(parents=True)
        (source / 'tasks.md').write_text('## 1. Build\n- [ ] Implement\n')
        with patch('gigaflex.interaction.ask', side_effect=['openspec', str(source)]):
            launch = startup_choice(self.repo, self.repo / '.gigaflex/progress')
        self.assertEqual(Launch(self.repo, ('--openspec', str(source))), launch)

    def test_plan_picker_excludes_completed_plans_and_allows_manual_path(self):
        plans = self.repo / 'docs/plans'
        (plans / 'completed').mkdir(parents=True)
        (plans / 'active.md').write_text('active')
        (plans / 'completed/done.md').write_text('done')
        with patch('gigaflex.interaction.ask', return_value='manual') as question, \
                patch('gigaflex.interaction.ask_text', return_value=str(self.repo / 'other plan.md')):
            selected = choose_path(self.repo, openspec=False)
        self.assertEqual((self.repo / 'other plan.md').resolve(), selected)
        labels = [item.label for item in question.call_args.args[1]]
        self.assertIn('docs/plans/active.md', labels)
        self.assertFalse(any('done.md' in label for label in labels))

    def test_start_menu_supports_review_creation_and_cancel(self):
        for action, text, expected in [('review', None, ('--review',)),
                                       ('create', 'Build a report', ('--plan', 'Build a report')),
                                       ('exit', None, None)]:
            with self.subTest(action=action), patch('gigaflex.interaction.ask', return_value=action), \
                    patch('gigaflex.interaction.ask_text', return_value=text):
                launch = startup_choice(self.repo, self.repo / '.gigaflex/progress')
                self.assertEqual(expected, launch.arguments if launch else None)

    def test_resume_and_note_are_translated_into_argv(self):
        with patch('gigaflex.interaction.ask', return_value='resume'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.stopped().launch, recovery_choice(self.stopped()))
        with patch('gigaflex.interaction.ask', return_value='note'), \
                patch('gigaflex.interaction.ask_text', return_value='service restarted; literal text'), \
                contextlib.redirect_stdout(io.StringIO()):
            launch = recovery_choice(self.stopped())
        self.assertEqual(('--resume-note', 'service restarted; literal text'), launch.arguments[-2:])
        with patch('gigaflex.interaction.ask', return_value='resume'), contextlib.redirect_stdout(io.StringIO()):
            launch = recovery_choice(self.stopped(note=True))
        self.assertIn('--resume-note', launch.arguments)

    def test_restart_is_explicit_and_preserves_pinned_base(self):
        with patch('gigaflex.interaction.ask', return_value='restart'), contextlib.redirect_stdout(io.StringIO()):
            launch = recovery_choice(self.stopped())
        self.assertIn('--restart', launch.arguments)
        self.assertNotIn('--resume', launch.arguments)
        self.assertEqual(self.initial_head, launch.arguments[launch.arguments.index('--base-ref') + 1])

    def test_details_and_cancelled_note_do_not_launch_work(self):
        output = io.StringIO()
        with patch('gigaflex.interaction.ask', side_effect=['details', 'note', 'exit']), \
                patch('gigaflex.interaction.ask_text', return_value=None), contextlib.redirect_stdout(output):
            self.assertIsNone(recovery_choice(self.stopped()))
        self.assertIn('/saved/task.bundle', output.getvalue())
        self.assertIn('/progress.txt', output.getvalue())
        self.assertEqual(self.initial_head, self.git.head_commit())

    def test_review_base_named_manual_is_distinct_from_manual_input_action(self):
        self.git.run('branch', 'manual')
        with patch('gigaflex.interaction.ask', return_value='ref:manual'), \
                patch('gigaflex.interaction.ask_text') as text:
            self.assertEqual('manual', choose_review_base(self.git))
            text.assert_not_called()
