"""Best-effort local attention signals, without extra flags or dependencies."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .terminal import _display


APPLE_NOTIFICATION_SCRIPT = '''on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
'''


def notify_attention(project: str) -> dict[str, object]:
    result: dict[str, object] = {"terminal_bell_sent": False, "desktop_requested": False, "backend": ""}
    if os.environ.get('CI', '').lower() not in {'', '0', 'false'}:
        result['skipped'] = 'ci'
        return result

    for stream in (sys.stderr, sys.stdout):
        try:
            if stream.isatty():
                stream.write('\a')
                stream.flush()
                result['terminal_bell_sent'] = True
                break
        except (AttributeError, OSError, ValueError):
            continue

    # On SSH the bell reaches the client; a desktop service would belong to
    # the remote host rather than the terminal user's machine.
    if any(os.environ.get(name) for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')):
        return result

    title = 'GigaFlex — нужно ваше действие'
    message = ('Проект: ' + _display(project, 100)
               + '. Подробности и варианты продолжения — в терминале и отчёте запуска.')
    command, script = None, None
    if sys.platform == 'darwin':
        executable = shutil.which('osascript')
        if executable:
            result['backend'] = 'osascript'
            command, script = [executable, '-', title, message], APPLE_NOTIFICATION_SCRIPT
    elif sys.platform.startswith('linux') and any(os.environ.get(name) for name in
                                                 ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')):
        executable = shutil.which('notify-send')
        if executable:
            result['backend'] = 'notify-send'
            command = [executable, '--app-name=GigaFlex', title, message]

    if command is not None:
        try:
            # User/project text is argv data, never interpolated into a script.
            process = subprocess.run(command, input=script if script is not None else '', text=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            result['desktop_requested'] = process.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result
