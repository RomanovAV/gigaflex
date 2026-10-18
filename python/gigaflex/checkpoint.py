from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Callable

from .git import GitService
from .artifacts import ArtifactSnapshot


CHECKPOINT_VERSION = 1
PHASE_ORDER = ("tasks", "review", "finalize")


class ResumeError(RuntimeError):
    """A saved run cannot be resumed without resolving its state mismatch."""


@dataclass(frozen=True)
class RepositoryState:
    head: str
    tree: str
    artifacts: str = ""


class RunCheckpoint:
    def __init__(
        self,
        path: Path,
        git: GitService,
        *,
        identity: str,
        base_commit: str,
        ignored_paths: tuple[Path, ...] = (),
        artifact_paths: tuple[Path, ...] = (),
        diagnostic: Callable[[str], None] = lambda _line: None,
        restart: bool = False,
    ) -> None:
        self.path = path
        self.git = git
        self.identity = identity
        self.base_commit = base_commit
        self.ignored_paths = ignored_paths
        self.artifact_paths = artifact_paths
        self.diagnostic = diagnostic
        self.history_reference: dict[str, str] | None = None
        self._data = self._load()
        if restart:
            stopped_phase = self.blocked.get("phase")
            self._data.pop("blocked", None)
            if stopped_phase in PHASE_ORDER:
                self.invalidate_from(stopped_phase)
            else:
                self._write()
        if not self._matches_run():
            if self.blocked:
                raise ResumeError(f"saved stopped run belongs to another plan or base: {self.path}")
            self._data = self._empty_data()
            self._write()

    @property
    def snapshot(self) -> dict[str, object]:
        return json.loads(json.dumps(self._data))

    @property
    def blocked(self) -> dict[str, object]:
        value = self._data.get("blocked")
        return dict(value) if isinstance(value, dict) else {}

    def mark_blocked(
        self, reason: str, phase: str, resume_command: str,
        task_recovery: dict[str, object] | None = None,
        *, history: dict[str, str] | None = None,
    ) -> None:
        recovery = task_recovery or self.blocked.get("task_recovery")
        self._data["blocked"] = {
            "reason": reason, "phase": phase, "resume_command": resume_command,
            "task_recovery": recovery, "updated_at": _timestamp(),
        }
        if history is not None:
            self._data["blocked"]["history"] = history
        self._write()

    def clear_blocked(self) -> None:
        self._data.pop("blocked", None)
        self._write()

    def current_state(self) -> RepositoryState:
        with tempfile.TemporaryDirectory(prefix="gigaflex-checkpoint-") as tmp:
            snapshot = self.git.create_review_snapshot(
                Path(tmp) / "snapshot.index",
                self.ignored_paths,
            )
        return RepositoryState(
            head=self.git.head_commit(),
            tree=self.git.tree_id(snapshot),
            artifacts=(ArtifactSnapshot.capture(self.git, self.artifact_paths, excluded_paths=self.ignored_paths).signature()
                       if self.artifact_paths else ""),
        )

    def can_reuse(self, phase: str, state: RepositoryState) -> bool:
        value = self._phases().get(phase)
        if not isinstance(value, dict):
            return False
        reusable = (value.get("head") == state.head and value.get("tree") == state.tree
                    and value.get("artifacts", "") == state.artifacts)
        if reusable:
            self._report(
                f"session=checkpoint event=reused phase={phase} head={state.head}"
            )
        return reusable

    def mark_started(self, phase: str) -> None:
        self._validate_phase(phase)
        self._data["last_started_phase"] = phase
        self._data["updated_at"] = _timestamp()
        self._write()
        self._report(f"session=checkpoint event=phase_started phase={phase}")

    def mark_completed(self, phase: str, state: RepositoryState) -> None:
        self._validate_phase(phase)
        phases = self._phases()
        phases[phase] = {
            **asdict(state),
            "completed_at": _timestamp(),
            "history": self.history_reference,
        }
        for later in PHASE_ORDER[PHASE_ORDER.index(phase) + 1:]:
            phases.pop(later, None)
        self._data["last_started_phase"] = ""
        self._data["updated_at"] = _timestamp()
        self._write()
        self._report(
            f"session=checkpoint event=phase_completed phase={phase} head={state.head}"
        )

    def invalidate_from(self, phase: str) -> None:
        self._validate_phase(phase)
        phases = self._phases()
        for item in PHASE_ORDER[PHASE_ORDER.index(phase):]:
            phases.pop(item, None)
        self._data["updated_at"] = _timestamp()
        self._write()
        self._report(f"session=checkpoint event=invalidated_from phase={phase}")

    def phase_history(self, phase: str) -> object:
        record = self._phases().get(phase, {})
        return record.get("history") if isinstance(record, dict) else None

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return self._empty_data()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._report(
                "session=checkpoint event=load_failed "
                f"error={str(exc)!r} action=reset"
            )
            return self._empty_data()
        return value if isinstance(value, dict) else self._empty_data()

    def _matches_run(self) -> bool:
        return (
            self._data.get("version") == CHECKPOINT_VERSION
            and self._data.get("identity") == self.identity
            and self._data.get("base_commit") == self.base_commit
        )

    def _empty_data(self) -> dict[str, object]:
        return {
            "version": CHECKPOINT_VERSION,
            "identity": self.identity,
            "base_commit": self.base_commit,
            "last_started_phase": "",
            "updated_at": _timestamp(),
            "phases": {},
        }

    def _phases(self) -> dict[str, object]:
        value = self._data.setdefault("phases", {})
        if not isinstance(value, dict):
            value = {}
            self._data["phases"] = value
        return value

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _report(self, line: str) -> None:
        try:
            self.diagnostic(line)
        except Exception:
            pass

    @staticmethod
    def _validate_phase(phase: str) -> None:
        if phase not in PHASE_ORDER:
            raise ValueError(f"unknown checkpoint phase: {phase}")


def checkpoint_path(progress_file: Path) -> Path:
    name = progress_file.name
    if name.startswith("progress-"):
        name = "checkpoint-" + name[len("progress-"):]
    else:
        name = "checkpoint-" + name
    return progress_file.with_name(Path(name).with_suffix(".json").name)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
