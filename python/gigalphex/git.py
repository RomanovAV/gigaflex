from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable, Optional


DATE_PREFIX_RE = re.compile(r"^[\d-]+")
BRANCH_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
COMMIT_IDENTITY_RE = re.compile(
    r"^(.*) <([^<>]*)> (\d+) ([+-]\d{4})$"
)


class GitError(RuntimeError):
    pass


@dataclass
class GitService:
    cwd: Path = Path(".")

    def run(
        self,
        *args: str,
        check: bool = True,
        input_text: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.cwd),
            text=True,
            input=input_text,
            env={**os.environ, **env} if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {detail}")
        return proc

    def ensure_repo(self) -> None:
        proc = self.run("rev-parse", "--is-inside-work-tree", check=False)
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            raise GitError("not inside a git repository")

    def is_repo(self) -> bool:
        proc = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def init_repo_if_missing(self) -> bool:
        if self.is_repo():
            return False
        self.run("init")
        return True

    def default_branch(self, configured: str = "") -> str:
        if configured:
            return configured

        origin_head = self.run("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        if origin_head.returncode == 0:
            value = origin_head.stdout.strip()
            if value.startswith("origin/"):
                return value.split("/", 1)[1]
            if value:
                return value

        for branch in ("main", "master", "trunk"):
            exists = self.run("rev-parse", "--verify", "--quiet", branch, check=False)
            if exists.returncode == 0:
                return branch

        current = self.current_branch()
        if current:
            return current
        raise GitError("could not detect default branch; pass --default-branch")

    def current_branch(self) -> str:
        proc = self.run("branch", "--show-current", check=False)
        return proc.stdout.strip()

    def repo_root(self) -> Path:
        proc = self.run("rev-parse", "--show-toplevel")
        return Path(proc.stdout.strip()).resolve()

    def is_dirty(self) -> bool:
        return bool(self.dirty_paths())

    def dirty_paths(self) -> list[Path]:
        proc = self.run(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            check=True,
        )
        paths: list[Path] = []
        records = proc.stdout.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            if not record:
                index += 1
                continue
            if len(record) < 4 or record[2] != " ":
                index += 1
                continue

            status = record[:2]
            paths.append(Path(record[3:]))
            if "R" in status or "C" in status:
                index += 1
                if index < len(records) and records[index]:
                    paths.append(Path(records[index]))
            index += 1
        return paths

    def has_commits(self) -> bool:
        proc = self.run("rev-parse", "--verify", "HEAD", check=False)
        return proc.returncode == 0

    def head_commit(self) -> str:
        proc = self.run("rev-parse", "--verify", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def commit_subjects_since(self, commit: str) -> list[str]:
        proc = self.run(
            "log",
            "--format=%s",
            f"{commit}..HEAD" if commit else "HEAD",
            check=True,
        )
        return [line for line in proc.stdout.splitlines() if line]

    def prefix_commit_messages_since(self, commit: str, prefix: str) -> list[str]:
        prefix = prefix.strip()
        head = self.head_commit()
        if not prefix or not head or head == commit:
            return []
        if commit:
            ancestor = self.run(
                "merge-base",
                "--is-ancestor",
                commit,
                head,
                check=False,
            )
            if ancestor.returncode != 0:
                raise GitError(
                    f"cannot prefix commits because {commit} is not an ancestor of HEAD"
                )

        revision_range = f"{commit}..{head}" if commit else head
        commits = self.run(
            "rev-list",
            "--reverse",
            "--topo-order",
            revision_range,
        ).stdout.splitlines()
        parsed = {
            object_id: _parse_commit(self.run("cat-file", "commit", object_id).stdout)
            for object_id in commits
        }
        changed_subjects = [
            item.subject
            for item in parsed.values()
            if not _message_has_prefix(item.message, prefix)
        ]
        if not changed_subjects:
            return []

        commit_set = set(commits)
        rewritten: dict[str, str] = {}
        for object_id in commits:
            item = parsed[object_id]
            parents = []
            for parent in item.parents:
                if parent in commit_set and parent not in rewritten:
                    raise GitError(
                        "cannot prefix commits because their topological order is invalid"
                    )
                parents.append(rewritten.get(parent, parent))

            message = (
                item.message
                if _message_has_prefix(item.message, prefix)
                else _prefix_message(item.message, prefix)
            )
            parent_args = [
                value
                for parent in parents
                for value in ("-p", parent)
            ]
            result = self.run(
                "commit-tree",
                item.tree,
                *parent_args,
                input_text=message,
                env=_commit_identity_env(item),
            )
            rewritten[object_id] = result.stdout.strip()

        new_head = rewritten.get(head)
        if not new_head:
            raise GitError("cannot prefix commits because HEAD was not rewritten")
        self.run(
            "update-ref",
            "-m",
            f"gigalphex: prefix new commits with {prefix}",
            "HEAD",
            new_head,
            head,
        )
        return changed_subjects

    def ensure_clean(self, allow_dirty: bool, ignored_paths: Iterable[Path] = ()) -> None:
        if allow_dirty:
            return

        dirty = self.dirty_paths()
        ignored = {_normalize_relative(path) for path in ignored_paths}
        remaining = [path for path in dirty if _normalize_relative(path) not in ignored]
        if remaining:
            shown = ", ".join(str(path) for path in remaining[:5])
            if len(remaining) > 5:
                shown += f", ... ({len(remaining)} total)"
            raise GitError(
                "working tree has uncommitted changes "
                f"({shown}); commit/stash them or pass --allow-dirty"
            )

    def branch_exists(self, branch: str) -> bool:
        proc = self.run("rev-parse", "--verify", "--quiet", branch, check=False)
        return proc.returncode == 0

    def ensure_ref_exists(self, ref: str) -> None:
        if not self.branch_exists(ref):
            raise GitError(f"git ref not found: {ref}")

    def switch_or_create_branch(self, branch: str) -> None:
        if not branch:
            return
        if self.current_branch() == branch:
            return
        if self.branch_exists(branch):
            self.run("switch", branch)
            return
        self.run("switch", "-c", branch)

    def worktree_path(self, branch: str) -> Path:
        return self.repo_root() / ".gigalphex" / "worktrees" / worktree_dir_name(branch)

    def ensure_worktree(self, branch: str) -> Path:
        if not branch:
            raise GitError("branch name is required for worktree")
        path = self.worktree_path(branch)
        if path.exists():
            probe = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "true":
                return path
            raise GitError(f"worktree path exists but is not a git worktree: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(branch):
            self.run("worktree", "add", str(path), branch)
        else:
            self.run("worktree", "add", "-b", branch, str(path), "HEAD")
        return path

    def commit_file(self, path: Path, message: str) -> None:
        self.run("add", "--", str(path))
        self.run("commit", "--only", "-m", message, "--", str(path))

    def commit_paths(self, paths: list[Path], message: str) -> None:
        if not paths:
            return
        args = [str(path) for path in paths]
        self.run("add", "--all", "--", *args)
        self.run("commit", "-m", message, "--", *args)

    def commit_all_if_dirty(self, message: str) -> bool:
        if not self.is_dirty():
            return False
        self.run("add", "--all")
        self.run("commit", "-m", message)
        return True


@dataclass(frozen=True)
class _ParsedCommit:
    tree: str
    parents: tuple[str, ...]
    author: str
    committer: str
    message: str

    @property
    def subject(self) -> str:
        lines = self.message.splitlines()
        return lines[0] if lines else "(empty commit message)"


def _parse_commit(raw: str) -> _ParsedCommit:
    try:
        headers, message = raw.split("\n\n", 1)
    except ValueError as exc:
        raise GitError("cannot parse commit object without a message separator") from exc

    tree = ""
    parents: list[str] = []
    author = ""
    committer = ""
    for line in headers.splitlines():
        if line.startswith("tree "):
            tree = line.removeprefix("tree ")
        elif line.startswith("parent "):
            parents.append(line.removeprefix("parent "))
        elif line.startswith("author "):
            author = line.removeprefix("author ")
        elif line.startswith("committer "):
            committer = line.removeprefix("committer ")
    if not tree or not author or not committer:
        raise GitError("cannot parse required commit metadata")
    return _ParsedCommit(
        tree=tree,
        parents=tuple(parents),
        author=author,
        committer=committer,
        message=message,
    )


def _message_has_prefix(message: str, prefix: str) -> bool:
    subject = message.split("\n", 1)[0]
    return subject == prefix or subject.startswith(f"{prefix} ")


def _prefix_message(message: str, prefix: str) -> str:
    return f"{prefix} {message}" if message else f"{prefix}\n"


def _commit_identity_env(commit: _ParsedCommit) -> dict[str, str]:
    author_name, author_email, author_date = _parse_commit_identity(commit.author)
    committer_name, committer_email, committer_date = _parse_commit_identity(
        commit.committer
    )
    return {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_DATE": committer_date,
    }


def _parse_commit_identity(value: str) -> tuple[str, str, str]:
    match = COMMIT_IDENTITY_RE.fullmatch(value)
    if not match:
        raise GitError("cannot parse commit author or committer metadata")
    name, email, timestamp, timezone = match.groups()
    return name, email, f"@{timestamp} {timezone}"


def branch_name_from_plan(plan_file: Path) -> str:
    name = plan_file.name
    if name.endswith(".md"):
        name = name[:-3]
    branch = DATE_PREFIX_RE.sub("", name).strip("-")
    return branch or name


def jira_branch_name(plan_file: Path, jira_task: str) -> str:
    description = BRANCH_SLUG_RE.sub("-", branch_name_from_plan(plan_file).lower()).strip("-")
    return f"feature/{jira_task}-{description or 'plan'}"


def _normalize_relative(path: Path) -> str:
    return Path(str(path)).as_posix().removeprefix("./")


def worktree_dir_name(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "plan"


def move_plan_to_completed(plan_file: Path) -> Path:
    completed_dir = plan_file.parent / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    target = completed_dir / plan_file.name
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        index = 2
        while target.exists():
            target = completed_dir / f"{stem}-{index}{suffix}"
            index += 1
    shutil.move(str(plan_file), str(target))
    return target
