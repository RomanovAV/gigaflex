"""Small ask-style terminal prompts; scripts never wait for input."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import select
import shutil
import sys
import unicodedata


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""


def interactive_available() -> bool:
    return (sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("CI", "").lower() in {"", "0", "false"})


def _display(text: str, width: int) -> str:
    """Keep filenames and diagnostics from injecting terminal control sequences."""
    clean = ''.join(' ' if unicodedata.category(char).startswith('C') else char for char in str(text))
    output, used = [], 0
    for char in clean:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1
        if used + size > max(1, width - 1):
            return ''.join(output) + '…'
        output.append(char)
        used += size
    return ''.join(output)


@contextmanager
def _keyboard():
    if os.name == 'nt':
        import msvcrt
        def read_key():
            key = msvcrt.getwch()
            if key in {'\x00', '\xe0'}:
                return {'H': 'up', 'P': 'down'}.get(msvcrt.getwch(), '')
            return key
        yield read_key
        return
    import termios
    import tty
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        def read_key():
            raw = os.read(descriptor, 1)
            if not raw:
                return None
            key = raw.decode('ascii', errors='ignore')
            if key == '\x1b' and select.select([descriptor], [], [], 0.08)[0]:
                prefix = os.read(descriptor, 1)
                if prefix in {b'[', b'O'} and select.select([descriptor], [], [], 0.08)[0]:
                    return {b'A': 'up', b'B': 'down'}.get(os.read(descriptor, 1), '')
                return ''
            return key
        yield read_key
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def ask(question: str, choices: list[Choice], *, default: str = ""):
    if not interactive_available() or not choices:
        return None
    selected = next((i for i, item in enumerate(choices) if item.value == default), 0)
    try:
        has_descriptor = os.isatty(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        has_descriptor = False
    basic_windows_console = (os.name == 'nt' and not any(os.environ.get(name) for name in
                             ('WT_SESSION', 'ANSICON', 'TERM')))
    if os.environ.get('TERM') == 'dumb' or not has_descriptor or basic_windows_console:
        print('\n' + _display(question, 160))
        for index, choice in enumerate(choices, 1):
            print(f'  {index}. {_display(choice.label, 120)}')
            if choice.description:
                print('     ' + _display(choice.description, 120))
        while True:
            try:
                value = input(f'Выберите вариант [1–{len(choices)}], Enter — {selected + 1}, q — выход: ').strip()
            except EOFError:
                return None
            if value.lower() in {'q', 'quit', 'exit'}:
                return None
            if not value:
                return choices[selected].value
            if value.isdigit() and 1 <= int(value) <= len(choices):
                return choices[int(value) - 1].value
            print('Введите номер варианта или q.')

    drawn = 0
    def render():
        nonlocal drawn
        columns, rows = shutil.get_terminal_size((80, 24))
        columns, rows = columns or 80, rows or 24
        window = min(len(choices), max(1, min(7, (rows - 5) // 2)))
        start = min(max(0, selected - window + 1), len(choices) - window)
        lines = [_display(question, columns)]
        for index in range(start, start + window):
            choice = choices[index]
            lines.append(_display(f"{'›' if index == selected else ' '} {index + 1}. {choice.label}", columns))
            lines.append(_display('     ' + choice.description, columns))
        lines.append(_display('↑/↓ — выбор · Enter — подтвердить · Esc — отмена', columns))
        if drawn:
            sys.stdout.write(f'\x1b[{drawn}A')
        sys.stdout.write('\x1b[J' + '\n'.join(lines) + '\n')
        sys.stdout.flush()
        drawn = len(lines)

    print()
    with _keyboard() as read_key:
        sys.stdout.write('\x1b[?25l')
        try:
            render()
            while True:
                key = read_key()
                if key in {'up', 'k'}:
                    selected = (selected - 1) % len(choices)
                elif key in {'down', 'j'}:
                    selected = (selected + 1) % len(choices)
                elif key in {'\r', '\n'}:
                    return choices[selected].value
                elif key is None or key in {'\x1b', '\x04', 'q'}:
                    return None
                elif key == '\x03':
                    raise KeyboardInterrupt
                elif key and key.isdigit() and 1 <= int(key) <= len(choices):
                    selected = int(key) - 1
                render()
        finally:
            sys.stdout.write('\x1b[?25h')
            sys.stdout.flush()


def ask_text(question: str):
    if not interactive_available():
        return None
    try:
        value = input(_display(question, 160) + '\n> ').strip()
    except EOFError:
        return None
    return value or None
