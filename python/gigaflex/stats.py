from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import threading
import time
from typing import Optional


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class InvocationStat:
    session: str
    attempt: int
    status: str
    returncode: int
    wall_duration_ms: int
    reported_duration_ms: Optional[int]
    api_duration_ms: Optional[int]
    session_id: str
    models: tuple[str, ...]
    usage: Optional[TokenUsage]


class RunStatistics:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self._finished: Optional[float] = None
        self._status = "running"
        self._invocations: list[InvocationStat] = []
        self._lock = threading.Lock()

    def add(self, invocation: InvocationStat) -> None:
        with self._lock:
            self._invocations.append(invocation)

    def finish(self, status: str = "success") -> None:
        with self._lock:
            if self._finished is None:
                self._finished = time.monotonic()
            self._status = status

    @property
    def wall_duration_ms(self) -> int:
        with self._lock:
            finished = self._finished if self._finished is not None else time.monotonic()
            return round((finished - self._started) * 1000)

    @property
    def invocations(self) -> list[InvocationStat]:
        with self._lock:
            return list(self._invocations)

    def to_dict(self) -> dict[str, object]:
        invocations = self.invocations
        known_usage = [item.usage for item in invocations if item.usage is not None]
        totals = _sum_usage(known_usage)
        return {
            "status": self.status,
            "wall_duration_ms": self.wall_duration_ms,
            "call_count": len(invocations),
            "summed_call_duration_ms": sum(item.wall_duration_ms for item in invocations),
            "usage_known_calls": len(known_usage),
            "usage": asdict(totals) if totals is not None else None,
            "invocations": [
                {
                    **asdict(item),
                    "models": list(item.models),
                }
                for item in invocations
            ],
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def render_text(self) -> str:
        data = self.to_dict()
        usage = data["usage"]
        lines = [
            f"status: {data['status']}",
            f"total wall time: {_format_duration(int(data['wall_duration_ms']))}",
            f"GigaCode calls: {data['call_count']}",
            "summed call time: "
            f"{_format_duration(int(data['summed_call_duration_ms']))} "
            "(parallel calls overlap)",
        ]
        if usage is None:
            lines.append("tokens: unknown (no completed GigaCode result events)")
        else:
            assert isinstance(usage, dict)
            lines.append(
                "tokens: "
                f"input={usage['input_tokens']} "
                f"output={usage['output_tokens']} "
                f"cache_read={usage['cache_read_input_tokens']} "
                f"total={usage['total_tokens']} "
                f"(known for {data['usage_known_calls']}/{data['call_count']} calls)"
            )

        lines.append("calls:")
        for item in self.invocations:
            models = ",".join(item.models) if item.models else "unknown-model"
            usage_text = (
                "tokens="
                f"{item.usage.total_tokens} "
                f"(in={item.usage.input_tokens} "
                f"out={item.usage.output_tokens} "
                f"cache={item.usage.cache_read_input_tokens})"
                if item.usage is not None
                else "tokens=unknown"
            )
            reported = (
                f" reported={_format_duration(item.reported_duration_ms)}"
                if item.reported_duration_ms is not None
                else ""
            )
            api = (
                f" api={_format_duration(item.api_duration_ms)}"
                if item.api_duration_ms is not None
                else ""
            )
            lines.append(
                f"- {item.session} attempt={item.attempt} status={item.status} "
                f"wall={_format_duration(item.wall_duration_ms)}{reported}{api} "
                f"{usage_text} model={models}"
            )
        return "\n".join(lines) + "\n"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status


def statistics_path(progress_file: Path) -> Path:
    name = progress_file.name
    if name.startswith("progress-"):
        name = "stats-" + name[len("progress-"):]
    else:
        name = "stats-" + name
    return progress_file.with_name(Path(name).with_suffix(".json").name)


def _sum_usage(items: list[TokenUsage]) -> Optional[TokenUsage]:
    if not items:
        return None
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=sum(item.output_tokens for item in items),
        cache_read_input_tokens=sum(item.cache_read_input_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
    )


def _format_duration(milliseconds: int) -> str:
    seconds, millis = divmod(max(0, milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    if seconds:
        return f"{seconds}.{millis:03d}s"
    return f"{millis}ms"
