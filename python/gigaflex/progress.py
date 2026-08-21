from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import threading
import uuid


PROMPT_CONTEXT_MAX_LINES = 200
PROMPT_CONTEXT_MAX_CHARS = 50_000


class ProgressLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._run_start_offset = path.stat().st_size if path.exists() else 0
        self.run_id = (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + f"-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )

    def write(self, text: str) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(text)

    def section(self, title: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"\n\n=== {title} ({stamp}) ===\n")

    def stream(self, text: str) -> None:
        print(text, end="")
        self.write(text)

    def diagnostic(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"[executor {stamp}] {text}\n")

    @property
    def prompt_context_file(self) -> Path:
        name = self.path.name
        if name.startswith("progress-"):
            name = "context-" + name[len("progress-"):]
        else:
            name = "context-" + name
        return self.path.with_name(Path(name).with_suffix(".txt").name)

    def snapshot_for_prompt(
        self,
        *,
        max_lines: int = PROMPT_CONTEXT_MAX_LINES,
        max_chars: int = PROMPT_CONTEXT_MAX_CHARS,
    ) -> Path:
        """Write a bounded, immutable-at-render-time view of this run's log."""
        max_lines = max(1, max_lines)
        max_chars = max(1, max_chars)
        with self._lock:
            current = self._read_current_run_tail(max_chars * 4)
            lines = current.splitlines(keepends=True)[-max_lines:]
            body = "".join(lines)
            if len(body) > max_chars:
                body = body[-max_chars:]
                newline = body.find("\n")
                if 0 <= newline < len(body) - 1:
                    body = body[newline + 1:]
            header = (
                "GigaFlex bounded progress snapshot\n"
                f"run_id: {self.run_id}\n"
                "scope: current run only\n"
                "note: this file is a static prompt-time snapshot; do not search for "
                "or read older progress logs\n\n"
            )
            target = self.prompt_context_file
            target.write_text(header + body, encoding="utf-8")
            return target

    def _read_current_run_tail(self, max_bytes: int) -> str:
        if not self.path.exists():
            return ""
        end = self.path.stat().st_size
        start = max(self._run_start_offset, end - max_bytes)
        with self.path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if start > self._run_start_offset:
            newline = text.find("\n")
            if 0 <= newline < len(text) - 1:
                text = text[newline + 1:]
        return text
