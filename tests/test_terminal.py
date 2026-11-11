import contextlib
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from gigaflex.terminal import Choice, ask, interactive_available, _display


class TerminalTest(unittest.TestCase):
    def test_scripts_and_ci_never_read_input(self):
        for terminal, ci in ((False, ''), (True, 'true')):
            with self.subTest(terminal=terminal, ci=ci), \
                    patch('sys.stdin', Mock(isatty=lambda: terminal)), \
                    patch('sys.stdout', Mock(isatty=lambda: terminal)), \
                    patch.dict(os.environ, {'CI': ci}), patch('builtins.input') as read:
                self.assertFalse(interactive_available())
                self.assertIsNone(ask('Continue?', [Choice('yes', 'Yes')]))
                read.assert_not_called()

    def test_numbered_fallback_checks_invalid_input_and_honors_default(self):
        output = io.StringIO()
        with patch('gigaflex.terminal.interactive_available', return_value=True), \
                patch.dict(os.environ, {'TERM': 'dumb'}), contextlib.redirect_stdout(output), \
                patch('builtins.input', side_effect=['99', '2']):
            result = ask('Action?', [Choice('a', 'First'), Choice('b', 'Second')])
        self.assertEqual('b', result)
        self.assertIn('Введите номер', output.getvalue())
        with patch('gigaflex.terminal.interactive_available', return_value=True), \
                patch.dict(os.environ, {'TERM': 'dumb'}), contextlib.redirect_stdout(io.StringIO()), \
                patch('builtins.input', return_value=''):
            self.assertEqual('b', ask('Action?', [Choice('a', 'First'), Choice('b', 'Second')], default='b'))

    def test_eof_cancels_numbered_prompt(self):
        with patch('gigaflex.terminal.interactive_available', return_value=True), \
                patch.dict(os.environ, {'TERM': 'dumb'}), contextlib.redirect_stdout(io.StringIO()), \
                patch('builtins.input', side_effect=EOFError):
            self.assertIsNone(ask('Action?', [Choice('a', 'First')]))

    def test_display_sanitizes_controls_and_bounds_wide_text(self):
        value = _display('file\x1b[31m\nname\r\x00', 80)
        for control in ('\x1b', '\n', '\r', '\x00'):
            self.assertNotIn(control, value)
        self.assertEqual('界界…', _display('界' * 10, 5))


@unittest.skipUnless(os.name != 'nt' and hasattr(os, 'openpty'), 'POSIX PTY required')
class TerminalPtyTest(unittest.TestCase):
    def prompt(self, keys=None, *, interrupt=False, code=None, directory=None):
        import termios
        master, slave = os.openpty()
        attributes = termios.tcgetattr(slave)
        code = code or """
from gigaflex.terminal import ask, Choice
try:
    result = ask('Выберите действие', [Choice('a', 'First'), Choice('b', 'Second')])
    print('RESULT=' + str(result), flush=True)
except KeyboardInterrupt:
    print('INTERRUPTED', flush=True)
"""
        environment = {**os.environ, 'CI': 'false', 'TERM': 'xterm',
                       'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'python')}
        if directory is not None:
            environment['HOME'] = str(directory / 'home')
        process = subprocess.Popen([sys.executable, '-B', '-u', '-c', code], stdin=slave, stdout=slave,
                                   stderr=slave, env=environment, cwd=directory)
        output = bytearray()
        def until(marker):
            deadline = time.monotonic() + 8
            while marker not in output:
                if time.monotonic() >= deadline:
                    self.fail('PTY prompt did not reach expected state: ' + output.decode(errors='replace'))
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
                elif process.poll() is not None:
                    self.fail('PTY process exited early: ' + output.decode(errors='replace'))
        try:
            until('Enter'.encode())
            if interrupt:
                process.send_signal(signal.SIGINT)
                until(b'INTERRUPTED')
            else:
                os.write(master, keys)
                until(b'RESULT=')
            process.wait(timeout=5)
            while select.select([master], [], [], 0)[0]:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                output.extend(chunk)
            self.assertEqual(0, process.returncode, output.decode(errors='replace'))
            restored = termios.tcgetattr(slave)
            # Darwin sets the kernel's transient PENDIN bit when canonical
            # mode is restored, even for a bare setcbreak/tcsetattr round trip.
            attributes[3] &= ~getattr(termios, 'PENDIN', 0)
            restored[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(attributes, restored, 'terminal mode was not restored')
            return output.decode(errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)

    def test_real_arrow_selection_and_enter(self):
        output = self.prompt(b'\x1b[B\r')
        self.assertIn('RESULT=b', output)
        self.assertIn('\x1b[?25h', output)

    def test_escape_preserves_terminal_and_cancels(self):
        self.assertIn('RESULT=None', self.prompt(b'\x1b'))

    def test_unrelated_utf8_keys_do_not_cancel_selection(self):
        self.assertIn('RESULT=b', self.prompt('я'.encode() + b'\x1b[B\r'))

    def test_sigint_restores_terminal_mode(self):
        self.assertIn('INTERRUPTED', self.prompt(interrupt=True))

    def test_real_bare_cli_opens_menu_and_quits_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            code = "from gigaflex.cli import main; print('RESULT=' + str(main([])), flush=True)"
            output = self.prompt(b'q', code=code, directory=directory)
            self.assertIn('GigaFlex', output)
            self.assertIn('RESULT=0', output)
            self.assertEqual([], list(directory.iterdir()))
