import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.notifications import APPLE_NOTIFICATION_SCRIPT, notify_attention


class NotificationsTest(unittest.TestCase):
    def environment(self, *, platform='linux', variables=None, terminal=True):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ, variables or {}, clear=True))
        stack.enter_context(patch('gigaflex.notifications.sys.platform', platform))
        self.stderr = Mock(isatty=lambda: terminal)
        self.stdout = Mock(isatty=lambda: terminal)
        stack.enter_context(patch('gigaflex.notifications.sys.stderr', self.stderr))
        stack.enter_context(patch('gigaflex.notifications.sys.stdout', self.stdout))
        return stack

    def test_ci_never_rings_or_requests_desktop_notification(self):
        with self.environment(platform='darwin', variables={'CI': 'true'}), \
                patch('gigaflex.notifications.subprocess.run') as run:
            result = notify_attention('project')
            self.stderr.write.assert_not_called()
            self.stdout.write.assert_not_called()
            run.assert_not_called()
        self.assertEqual('ci', result['skipped'])

    def test_ssh_rings_once_on_terminal_without_notifying_remote_desktop(self):
        with self.environment(platform='darwin', variables={'SSH_TTY': '/dev/pts/1'}), \
                patch('gigaflex.notifications.subprocess.run') as run:
            result = notify_attention('project')
            self.stderr.write.assert_called_once_with('\a')
            self.stderr.flush.assert_called_once()
            self.stdout.write.assert_not_called()
            run.assert_not_called()
        self.assertTrue(result['terminal_bell_sent'])
        self.assertFalse(result['desktop_requested'])

    def test_redirected_stderr_can_ring_stdout_and_broken_stream_does_not_block(self):
        with self.environment(variables={'SSH_CONNECTION': 'remote'}):
            self.stderr.write.side_effect = OSError('closed stream')
            result = notify_attention('project')
            self.stdout.write.assert_called_once_with('\a')
        self.assertTrue(result['terminal_bell_sent'])

    def test_macos_text_is_argv_data_not_applescript_source(self):
        project = 'quoted "; do shell script "bad"\n\x1b[31m'
        with self.environment(platform='darwin', terminal=False), \
                patch('gigaflex.notifications.shutil.which', return_value='/usr/bin/osascript'), \
                patch('gigaflex.notifications.subprocess.run', return_value=Mock(returncode=0)) as run:
            result = notify_attention(project)
        command = run.call_args.args[0]
        self.assertEqual(['/usr/bin/osascript', '-'], command[:2])
        self.assertIn('do shell script', command[3])
        self.assertNotIn('\x1b', command[3])
        self.assertEqual(APPLE_NOTIFICATION_SCRIPT, run.call_args.kwargs['input'])
        self.assertNotIn('do shell script', run.call_args.kwargs['input'])
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(2, run.call_args.kwargs['timeout'])
        self.assertEqual('osascript', result['backend'])
        self.assertTrue(result['desktop_requested'])

    def test_linux_desktop_uses_optional_notify_send(self):
        with self.environment(variables={'WAYLAND_DISPLAY': 'wayland-0'}, terminal=False), \
                patch('gigaflex.notifications.shutil.which', return_value='/usr/bin/notify-send'), \
                patch('gigaflex.notifications.subprocess.run', return_value=Mock(returncode=0)) as run:
            result = notify_attention('project')
        self.assertEqual(['/usr/bin/notify-send', '--app-name=GigaFlex'], run.call_args.args[0][:2])
        self.assertTrue(result['desktop_requested'])
        self.assertEqual('notify-send', result['backend'])

    def test_headless_or_missing_notifier_never_spawns_a_process(self):
        for variables in ({}, {'DISPLAY': ':0'}):
            with self.subTest(variables=variables), self.environment(variables=variables, terminal=False), \
                    patch('gigaflex.notifications.shutil.which', return_value=None), \
                    patch('gigaflex.notifications.subprocess.run') as run:
                result = notify_attention('project')
                run.assert_not_called()
                self.assertFalse(result['desktop_requested'])
                self.assertFalse(result['terminal_bell_sent'])

    def test_native_errors_and_timeout_are_best_effort(self):
        for outcome in (PermissionError('not allowed'), subprocess.TimeoutExpired('notifier', 2), Mock(returncode=1)):
            with self.subTest(outcome=outcome), self.environment(platform='darwin'), \
                    patch('gigaflex.notifications.shutil.which', return_value='/usr/bin/osascript'), \
                    patch('gigaflex.notifications.subprocess.run') as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                result = notify_attention('project')
                self.assertTrue(result['terminal_bell_sent'])
                self.assertFalse(result['desktop_requested'])
