from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading


class ProgressLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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
