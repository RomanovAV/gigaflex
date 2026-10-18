"""Local, per-invocation evidence. No model-generated diagnosis or evaluation."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import tempfile
import threading
import uuid

from . import __version__
from .artifacts import ArtifactSnapshot
from .git import GitService
from .validation import repository_state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_once(path: Path, value: object) -> None:
    # Attempt directories are unique. A completed record must never be replaced.
    if path.exists():
        raise FileExistsError(str(path))
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(_json(value))
    temporary.replace(path)


def history_root(git: GitService) -> Path:
    common = Path(git.run("rev-parse", "--git-common-dir").stdout.strip())
    return (git.cwd / common).resolve() / "gigaflex" / "runs"


def _file_snapshot(path: Path) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"path": str(path.resolve()), "status": "unavailable", "reason": str(exc)}
    return {"path": str(path.resolve()), "status": "captured", "sha256": _hash(data),
            "content": data.decode("utf-8", errors="replace")}


def capture_repository(git: GitService, excluded: tuple[Path, ...],
                       artifact_paths: tuple[Path, ...]) -> dict[str, object]:
    if git.head_commit():
        return asdict(repository_state(git, excluded, artifact_paths))
    # Tasks-only execution also supports a repository before its first commit.
    # Use a separate index to fingerprint inputs without inventing a HEAD commit.
    index = git.run("write-tree").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="gigaflex-history-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git.run("read-tree", index, env=env)
        git.run("add", "--all", env=env)
        if excluded:
            git.run("reset", "-q", index, "--", *(str(path) for path in excluded), env=env)
        tree = git.run("write-tree", env=env).stdout.strip()
    artifacts = ArtifactSnapshot.capture(git, artifact_paths, excluded_paths=excluded).signature() if artifact_paths else ""
    return {"head": "", "tree": tree, "index": index, "artifacts": artifacts, "status": "unborn"}


def capture_inputs(cfg, options, git: GitService, excluded: tuple[Path, ...]) -> dict[str, object]:
    prompts = asdict(options.prompts)
    settings = asdict(cfg)
    # Preserve effective invocation arguments, including normalization and phase model overrides.
    settings["phase_args"] = {phase: cfg.args_for_phase(phase)
                              for phase in ("task", "review", "synthesis", "finalize")}
    settings["run_options"] = {
        key: value for key, value in asdict(options).items() if key != "prompts"
    }
    package = Path(__file__).parent
    sources = {path.name: _hash(path.read_bytes()) for path in sorted(package.glob("*.py"))}
    paths = ([options.plan_file] if options.plan_file else []) + list(options.plan_context_files)
    return {
        "schema_version": 1,
        "repository": {"root": str(git.repo_root()), "branch": git.current_branch(),
                       "base_commit": options.default_branch,
                       **capture_repository(git, excluded, cfg.task_artifact_paths)},
        "plan": {"kind": options.plan_kind,
                 "source": str(options.plan_source.resolve()) if options.plan_source else None,
                 "files": [_file_snapshot(path) for path in dict.fromkeys(paths)]},
        "configuration": {"sha256": _hash(_json(settings).encode()), "effective": settings},
        "prompts": {name: {"sha256": _hash(content.encode()), "content": content}
                    for name, content in prompts.items()},
        "runtime": {"gigaflex_version": __version__, "source_sha256": _hash(_json(sources).encode()),
                    "python": platform.python_version(), "platform": platform.platform(),
                    "gigacode_version": None},
        "external_instructions": {
            "status": "not_observed",
            "reason": "GigaCode does not report effective GIGACODE.md, rules or skills to this runner.",
        },
    }


class RunHistory:
    """A logical run contains unique CLI attempts; resume links to a stopped attempt."""

    def __init__(self, root: Path, *, attempt_id: str, identity: str,
                 base_commit: str, mode: str, parent: object = None) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, object]] = []
        self._finished = False
        self._recording_errors: list[str] = []
        self.run_id = uuid.uuid4().hex
        parent_id = None
        lineage = "new"
        # Never trust a checkpoint reference as a path without checking IDs and its manifest.
        if mode == "resume":
            lineage = "unavailable"
            if isinstance(parent, dict) and all(
                isinstance(parent.get(key), str) and re.fullmatch(r"[A-Za-z0-9-]+", parent[key])
                for key in ("run_id", "attempt_id")
            ):
                try:
                    record = json.loads((root / parent["run_id"] / parent["attempt_id"] / "started.json").read_text())
                except (OSError, ValueError):
                    record = {}
                if not isinstance(record, dict):
                    record = {}
                if (record.get("identity") == identity and record.get("base_commit") == base_commit
                        and record.get("run_id") == parent["run_id"]
                        and record.get("attempt_id") == parent["attempt_id"]):
                    self.run_id, parent_id = parent["run_id"], parent["attempt_id"]
                    lineage = "linked"
        self.attempt_id = attempt_id
        self.path = root / self.run_id / attempt_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.started = {
            "schema_version": 1, "run_id": self.run_id, "attempt_id": attempt_id,
            "parent_attempt_id": parent_id, "lineage": lineage, "mode": mode,
            "identity": identity, "base_commit": base_commit, "started_at": _now(),
            "status": "running",
        }
        _write_once(self.path / "started.json", self.started)
        (self.path / "events.jsonl").touch(exist_ok=False)

    @property
    def reference(self) -> dict[str, str]:
        return {"run_id": self.run_id, "attempt_id": self.attempt_id}

    def save_inputs(self, inputs: dict[str, object]) -> None:
        _write_once(self.path / "inputs.json", inputs)

    def record(self, kind: str, **data: object) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("cannot append to a finished run history")
            item = {"sequence": len(self._events) + 1, "at": _now(), "kind": kind, "data": data}
            try:
                with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                # Executor callbacks may swallow exceptions. Surface missing evidence in result.json.
                self._recording_errors.append(str(exc))
                raise
            self._events.append(item)

    def executor_event(self, session: str, event: str, fields: dict[str, object]) -> None:
        if event not in {"activity", "first_output", "stdin_sent"}:
            self.record("executor", session=session, event=event, fields=fields)

    def invocation(self, invocation) -> None:
        self.record("invocation", call_id=uuid.uuid4().hex, **asdict(invocation))

    def finish(self, *, statistics, dashboard, repository: object,
               checkpoint: object, exit_code: int, artifact_paths: dict[str, str]) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("run history is already finished")
            stats = statistics.to_dict()
            _write_once(self.path / "stats.json", stats)
            _write_once(self.path / "status.json", dashboard)
            _write_once(self.path / "checkpoint.json", checkpoint)
            groups = {}
            for event in self._events:
                groups.setdefault(event["kind"], []).append(event)
            executor = groups.get("executor", [])
            retries = [item for item in executor if item["data"]["event"] == "retry_scheduled"]
            result = {
                **self.started, "status": stats["status"], "finished_at": _now(), "exit_code": exit_code,
                "failure": {"phase": stats["failure_phase"], "reason": stats["failure_reason"]},
                "execution_started": bool(groups.get("execution_started")),
                "evidence_complete": (not self._recording_errors and (self.path / "inputs.json").exists()
                                      and not (isinstance(repository, dict) and repository.get("status") == "unavailable")),
                "recording_errors": self._recording_errors,
                "repository": repository,
                "validation": groups.get("validation", []),
                "verification": groups.get("verification", []),
                "tasks": [item for item in self._events if item["kind"] in {"task_started", "task_completed"}],
                "review": {"passes": groups.get("review_pass", []),
                           "findings": groups.get("review_findings", []),
                           "decisions": groups.get("review_decisions", [])},
                "retries": {"executor": retries, "completion": groups.get("completion_retry", [])},
                "reused_phases": groups.get("phase_reused", []),
                "statistics": stats,
                "cost": {"amount": None, "currency": None, "status": "not_reported"},
                "artifacts": {"inputs": "inputs.json", "events": "events.jsonl", "progress": "progress.txt",
                              "statistics": "stats.json", "dashboard": "status.json", "checkpoint": "checkpoint.json",
                              **artifact_paths},
                "interpretation": {
                    "validation": "Runner-executed checks; not_configured is not a passed check.",
                    "review": "Parsed agent reports and decisions, not independently established defects or causes.",
                    "statistics": "This CLI attempt only; resumes share run_id. Unknown usage/cost is not zero.",
                },
            }
            _write_once(self.path / "result.json", result)
            self._finished = True
