from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Iterable, Optional


DATE_PREFIX_RE = re.compile(r"^[\d-]+")
BRANCH_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
COMMIT_IDENTITY_RE = re.compile(
    r"^(.*) <([^<>]*)> (\d+) ([+-]\d{4})$"
)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class BranchBaseline:
    base_branch: str
    base_commit: str


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

    def resolve_commit(self, ref: str) -> str:
        if not ref:
            raise GitError("git ref is required")
        proc = self.run(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            check=False,
        )
        commit = proc.stdout.strip()
        if proc.returncode != 0 or not commit:
            raise GitError(f"git ref not found or does not name a commit: {ref}")
        return commit

    def branch_baseline(self, branch: str) -> Optional[BranchBaseline]:
        if not branch:
            return None
        commit_proc = self.run(
            "config",
            "--local",
            "--get",
            f"branch.{branch}.gigaflexBaseCommit",
            check=False,
        )
        commit = commit_proc.stdout.strip()
        if commit_proc.returncode != 0 or not commit:
            return None
        branch_proc = self.run(
            "config",
            "--local",
            "--get",
            f"branch.{branch}.gigaflexBaseBranch",
            check=False,
        )
        base_branch = branch_proc.stdout.strip() or commit
        try:
            resolved = self.resolve_commit(commit)
        except GitError as exc:
            raise GitError(
                f"stored GigaFlex base commit for {branch} no longer exists: {commit}"
            ) from exc
        return BranchBaseline(base_branch=base_branch, base_commit=resolved)

    def set_branch_baseline(
        self,
        branch: str,
        baseline: BranchBaseline,
    ) -> None:
        if not branch:
            raise GitError("execution branch is required to store its review base")
        commit = self.resolve_commit(baseline.base_commit)
        self.run(
            "config",
            "--local",
            f"branch.{branch}.gigaflexBaseBranch",
            baseline.base_branch or commit,
        )
        self.run(
            "config",
            "--local",
            f"branch.{branch}.gigaflexBaseCommit",
            commit,
        )

    def is_ancestor(self, ancestor: str, descendant: str = "HEAD") -> bool:
        proc = self.run(
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(
            f"could not compare git history {ancestor}..{descendant}: {detail}"
        )

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
            f"gigaflex: prefix new commits with {prefix}",
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

    def switch_or_create_branch(self, branch: str, start_point: str = "HEAD") -> None:
        if not branch:
            return
        if self.current_branch() == branch:
            return
        if self.branch_exists(branch):
            self.run("switch", branch)
            return
        if start_point == "HEAD" and not self.has_commits():
            self.run("switch", "-c", branch)
        else:
            self.run("switch", "-c", branch, start_point)

    def worktree_path(self, branch: str) -> Path:
        return self.repo_root() / ".gigaflex" / "worktrees" / worktree_dir_name(branch)

    def ensure_worktree(self, branch: str, start_point: str = "HEAD") -> Path:
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
            self.run("worktree", "add", "-b", branch, str(path), start_point)
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

    def create_review_snapshot(
        self,
        index_path: Path,
        excluded_paths: Iterable[Path] = (),
    ) -> str:
        """Create an unreachable commit for the current working-tree state."""
        head = self.head_commit()
        if not head:
            raise GitError("cannot create a review snapshot without a HEAD commit")

        index_path = index_path.resolve()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_env = {
            "GIT_INDEX_FILE": str(index_path),
            "GIT_AUTHOR_NAME": "GigaFlex",
            "GIT_AUTHOR_EMAIL": "gigaflex@localhost",
            "GIT_COMMITTER_NAME": "GigaFlex",
            "GIT_COMMITTER_EMAIL": "gigaflex@localhost",
        }
        try:
            self.run("read-tree", head, env=snapshot_env)
            self.run("add", "--all", env=snapshot_env)
            excluded = [str(_normalize_relative(path)) for path in excluded_paths]
            if excluded:
                self.run(
                    "reset",
                    "-q",
                    head,
                    "--",
                    *excluded,
                    env=snapshot_env,
                )
            tree = self.run("write-tree", env=snapshot_env).stdout.strip()
            snapshot = self.run(
                "commit-tree",
                tree,
                "-p",
                head,
                input_text="gigaflex: ephemeral review snapshot\n",
                env=snapshot_env,
            ).stdout.strip()
        finally:
            index_path.unlink(missing_ok=True)
        if not snapshot:
            raise GitError("git commit-tree did not return a review snapshot commit")
        return snapshot

    def add_detached_worktree(self, path: Path, commit: str) -> None:
        self.run("worktree", "add", "--detach", str(path), commit)

    def remove_worktree(self, path: Path) -> None:
        result = self.run(
            "worktree",
            "remove",
            "--force",
            str(path),
            check=False,
        )
        if result.returncode != 0 and path.exists():
            shutil.rmtree(path)

    def prune_worktrees(self) -> None:
        self.run("worktree", "prune", "--expire", "now", check=False)

    def tree_id(self, ref: str) -> str:
        return self.run("rev-parse", f"{ref}^{{tree}}").stdout.strip()

    def index_path(self) -> Path:
        raw = self.run("rev-parse", "--git-path", "index").stdout.strip()
        path = Path(raw)
        return path.resolve() if path.is_absolute() else (self.cwd / path).resolve()

    def replace_paths_from_ref(self, ref: str, paths: Iterable[Path]) -> None:
        """Replace selected index/worktree paths with their exact state at ref."""
        normalized = sorted({_normalize_relative(path) for path in paths})
        if not normalized:
            return

        present: list[str] = []
        absent: list[str] = []
        for path in normalized:
            result = self.run(
                "ls-tree",
                "-z",
                "--name-only",
                ref,
                "--",
                path,
            ).stdout.split("\0")
            if path in result:
                present.append(path)
            else:
                absent.append(path)

        if present:
            self.run("checkout", ref, "--", *present)
        if absent:
            self.run(
                "rm",
                "-q",
                "-f",
                "--cached",
                "--ignore-unmatch",
                "--",
                *absent,
            )
            root = self.repo_root()
            for relative in absent:
                target = root / relative
                if target.is_dir() and not target.is_symlink():
                    raise GitError(
                        "cannot replace task path because it is a directory: "
                        f"{relative}"
                    )
                target.unlink(missing_ok=True)

    def linear_commits_between(self, ancestor: str, descendant: str) -> list[str]:
        if not self.is_ancestor(ancestor, descendant):
            raise GitError(
                f"task result {descendant} does not descend from snapshot {ancestor}"
            )
        commits = self.run(
            "rev-list",
            "--reverse",
            "--topo-order",
            f"{ancestor}..{descendant}",
        ).stdout.splitlines()
        for commit in commits:
            parents = self.run("rev-list", "--parents", "-n", "1", commit).stdout.split()
            if len(parents) != 2:
                raise GitError(
                    "task worktree produced a merge commit; transactional promotion "
                    "supports linear task commits only"
                )
        return commits

    def changed_paths_between(self, ancestor: str, descendant: str) -> set[Path]:
        output = self.run(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            ancestor,
            descendant,
            "--",
        ).stdout
        return {Path(value) for value in output.split("\0") if value}

    def cherry_pick_transaction(self, commits: list[str]) -> None:
        if not commits:
            raise GitError("task worktree produced no commits to promote")
        try:
            self.run("cherry-pick", *commits)
        except GitError:
            self.run("cherry-pick", "--abort", check=False)
            raise

    def rewrite_commit_tree(
        self,
        source_commit: str,
        tree: str,
        parent: str,
    ) -> str:
        source = _parse_commit(self.run("cat-file", "commit", source_commit).stdout)
        result = self.run(
            "commit-tree",
            tree,
            "-p",
            parent,
            input_text=source.message,
            env=_commit_identity_env(source),
        )
        rewritten = result.stdout.strip()
        if not rewritten:
            raise GitError("git commit-tree did not return a rewritten task commit")
        return rewritten

    def update_head(self, new_commit: str, old_commit: str, reason: str) -> None:
        self.run("update-ref", "-m", reason, "HEAD", new_commit, old_commit)


@dataclass(frozen=True)
class ReviewWorktreeSet:
    snapshot_commit: str
    paths: dict[str, Path]
    repo_root: Path


@dataclass
class ReviewWorktreeManager:
    git: GitService
    diagnostic: Callable[[str], None] = lambda _line: None
    temp_parent: Optional[Path] = None

    @property
    def repo_root(self) -> Path:
        return self.git.repo_root()

    def create(self, names: Iterable[str]) -> "_ReviewWorktreeContext":
        unique_names = tuple(dict.fromkeys(names))
        if not unique_names:
            raise ValueError("at least one review workspace name is required")
        return _ReviewWorktreeContext(self, unique_names)

    def report(self, line: str) -> None:
        try:
            self.diagnostic(line)
        except Exception:
            # Diagnostics must never prevent disposal of a temporary worktree.
            pass


class _ReviewWorktreeContext:
    def __init__(self, manager: ReviewWorktreeManager, names: tuple[str, ...]) -> None:
        self.manager = manager
        self.names = names
        self.root: Optional[Path] = None
        self.worktrees: list[Path] = []

    def __enter__(self) -> ReviewWorktreeSet:
        parent = str(self.manager.temp_parent) if self.manager.temp_parent else None
        self.root = Path(tempfile.mkdtemp(prefix="gigaflex-review-", dir=parent))
        try:
            snapshot = self.manager.git.create_review_snapshot(
                self.root / "snapshot.index"
            )
            self.manager.report(
                "session=review-worktree event=snapshot_created "
                f"commit={snapshot} workspaces={len(self.names)}"
            )
            paths: dict[str, Path] = {}
            for index, name in enumerate(self.names, start=1):
                slug = worktree_dir_name(name)
                path = self.root / f"{index:02d}-{slug}"
                self.manager.git.add_detached_worktree(path, snapshot)
                self.worktrees.append(path)
                paths[name] = path
                self.manager.report(
                    "session=review-worktree event=created "
                    f"name={name!r} path={str(path)!r} commit={snapshot}"
                )
            return ReviewWorktreeSet(
                snapshot_commit=snapshot,
                paths=paths,
                repo_root=self.manager.repo_root,
            )
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._cleanup()
        except Exception as cleanup_error:
            if exc is None:
                raise
            self.manager.report(
                "session=review-worktree event=cleanup_failed "
                f"error={str(cleanup_error)!r}"
            )

    def _cleanup(self) -> None:
        root = self.root
        if root is None:
            return
        cleanup_errors: list[str] = []
        for path in reversed(self.worktrees):
            try:
                self.manager.git.remove_worktree(path)
                self.manager.report(
                    "session=review-worktree event=removed "
                    f"path={str(path)!r}"
                )
            except (OSError, GitError) as exc:
                cleanup_errors.append(f"{path}: {exc}")
        self.manager.git.prune_worktrees()
        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError as exc:
            cleanup_errors.append(f"{root}: {exc}")
        self.worktrees.clear()
        self.root = None
        if cleanup_errors:
            raise GitError(
                "could not remove disposable review worktrees: "
                + "; ".join(cleanup_errors)
            )


@dataclass(frozen=True)
class TaskWorktree:
    manager: "TaskWorktreeManager"
    path: Path
    repo_root: Path
    base_commit: str
    snapshot_commit: str
    original_dirty_paths: frozenset[Path]

    def promote(self, task_head: str) -> list[str]:
        return self.manager.promote(self, task_head)


@dataclass
class TaskWorktreeManager:
    """Run a task against a snapshot and promote only its committed delta."""

    git: GitService
    diagnostic: Callable[[str], None] = lambda _line: None
    temp_parent: Optional[Path] = None
    ignored_paths: tuple[Path, ...] = ()

    @property
    def repo_root(self) -> Path:
        return self.git.repo_root()

    def create(self, label: str) -> "_TaskWorktreeContext":
        return _TaskWorktreeContext(self, label)

    def promote(self, workspace: TaskWorktree, task_head: str) -> list[str]:
        task_git = GitService(workspace.path)
        commits = task_git.linear_commits_between(workspace.snapshot_commit, task_head)
        touched_paths: set[Path] = set()
        parent = workspace.snapshot_commit
        for commit in commits:
            touched_paths.update(task_git.changed_paths_between(parent, commit))
            parent = commit
        adopted_paths = sorted(touched_paths & set(workspace.original_dirty_paths))

        self._assert_main_unchanged(workspace, "promotion-before.index")
        promotion_path = workspace.path.parent / "promotion"
        try:
            self.git.add_detached_worktree(promotion_path, workspace.base_commit)
            promotion_git = GitService(promotion_path)
            if adopted_paths:
                self._promote_with_adopted_paths(
                    promotion_git,
                    workspace,
                    commits,
                    adopted_paths,
                )
            else:
                promotion_git.cherry_pick_transaction(commits)
            promoted_head = promotion_git.head_commit()
            self._assert_main_unchanged(workspace, "promotion-after.index")
            self._install_promoted_head(
                workspace,
                promoted_head,
                touched_paths,
            )
        finally:
            self.git.remove_worktree(promotion_path)
            self.git.prune_worktrees()
        if adopted_paths:
            shown = ", ".join(str(path) for path in adopted_paths[:10])
            if len(adopted_paths) > 10:
                shown += f", ... ({len(adopted_paths)} total)"
            self.report(
                "session=task-worktree event=adopted_preexisting_changes "
                f"count={len(adopted_paths)} paths={shown!r}"
            )
        self.report(
            "session=task-worktree event=promoted "
            f"commits={len(commits)} head={self.git.head_commit()}"
        )
        return commits

    def _promote_with_adopted_paths(
        self,
        promotion_git: GitService,
        workspace: TaskWorktree,
        commits: list[str],
        adopted_paths: list[Path],
    ) -> None:
        promotion_git.replace_paths_from_ref(
            workspace.snapshot_commit,
            adopted_paths,
        )
        bootstrap_tree = promotion_git.run("write-tree").stdout.strip()
        bootstrap = promotion_git.run(
            "commit-tree",
            bootstrap_tree,
            "-p",
            workspace.base_commit,
            input_text="gigaflex: ephemeral adopted task inputs\n",
            env={
                "GIT_AUTHOR_NAME": "GigaFlex",
                "GIT_AUTHOR_EMAIL": "gigaflex@localhost",
                "GIT_COMMITTER_NAME": "GigaFlex",
                "GIT_COMMITTER_EMAIL": "gigaflex@localhost",
            },
        ).stdout.strip()
        if not bootstrap:
            raise GitError("git commit-tree did not return an adopted-input commit")
        promotion_git.run("reset", "--hard", bootstrap)

        first, *remaining = commits
        promotion_git.cherry_pick_transaction([first])
        rewritten_first = promotion_git.rewrite_commit_tree(
            first,
            promotion_git.tree_id("HEAD"),
            workspace.base_commit,
        )
        promotion_git.run("reset", "--hard", rewritten_first)
        if remaining:
            promotion_git.cherry_pick_transaction(remaining)

    def _install_promoted_head(
        self,
        workspace: TaskWorktree,
        promoted_head: str,
        touched_paths: set[Path],
    ) -> None:
        index_path = self.git.index_path()
        index_backup = workspace.path.parent / "main.index.backup"
        had_index = index_path.exists()
        if had_index:
            shutil.copy2(index_path, index_backup)

        head_updated = False
        try:
            self.git.update_head(
                promoted_head,
                workspace.base_commit,
                "gigaflex: promote isolated task",
            )
            head_updated = True
            self.git.replace_paths_from_ref(promoted_head, touched_paths)
        except BaseException as install_error:
            rollback_errors: list[str] = []
            if head_updated:
                try:
                    self.git.update_head(
                        workspace.base_commit,
                        promoted_head,
                        "gigaflex: roll back failed task promotion",
                    )
                except (OSError, GitError) as exc:
                    rollback_errors.append(f"HEAD: {exc}")
            try:
                self.git.replace_paths_from_ref(
                    workspace.snapshot_commit,
                    touched_paths,
                )
            except (OSError, GitError) as exc:
                rollback_errors.append(f"working tree: {exc}")
            finally:
                try:
                    if had_index:
                        shutil.copy2(index_backup, index_path)
                    else:
                        index_path.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(f"index: {exc}")
            if rollback_errors:
                raise GitError(
                    f"task promotion failed ({install_error}); rollback also failed: "
                    + "; ".join(rollback_errors)
                ) from install_error
            raise
        finally:
            index_backup.unlink(missing_ok=True)

    def _assert_main_unchanged(
        self,
        workspace: TaskWorktree,
        index_name: str,
    ) -> None:
        if self.git.head_commit() != workspace.base_commit:
            raise GitError(
                "main HEAD changed while the isolated task was running; "
                "task commits were not promoted"
            )
        ignored = {_normalize_relative(path) for path in self.ignored_paths}
        current_dirty_paths = frozenset(
            path
            for path in self.git.dirty_paths()
            if _normalize_relative(path) not in ignored
        )
        current_snapshot = self.git.create_review_snapshot(
            workspace.path.parent / index_name,
            self.ignored_paths,
        )
        if self.git.tree_id(current_snapshot) != self.git.tree_id(workspace.snapshot_commit):
            raise GitError(
                "main working tree changed while the isolated task was running; "
                "task commits were not promoted"
            )
        if current_dirty_paths != workspace.original_dirty_paths:
            raise GitError(
                "main working-tree status changed while the isolated task was running; "
                "task commits were not promoted"
            )

    def report(self, line: str) -> None:
        try:
            self.diagnostic(line)
        except Exception:
            # Diagnostics must never prevent disposal of a temporary worktree.
            pass


class _TaskWorktreeContext:
    def __init__(self, manager: TaskWorktreeManager, label: str) -> None:
        self.manager = manager
        self.label = label
        self.root: Optional[Path] = None
        self.path: Optional[Path] = None

    def __enter__(self) -> TaskWorktree:
        parent = str(self.manager.temp_parent) if self.manager.temp_parent else None
        self.root = Path(tempfile.mkdtemp(prefix="gigaflex-task-", dir=parent))
        try:
            base_commit = self.manager.git.head_commit()
            if not base_commit:
                raise GitError("cannot create a task worktree without a HEAD commit")
            ignored = {
                _normalize_relative(path)
                for path in self.manager.ignored_paths
            }
            original_dirty_paths = frozenset(
                path
                for path in self.manager.git.dirty_paths()
                if _normalize_relative(path) not in ignored
            )
            snapshot = self.manager.git.create_review_snapshot(
                self.root / "snapshot.index",
                self.manager.ignored_paths,
            )
            path = self.root / worktree_dir_name(self.label)
            self.path = path
            self.manager.git.add_detached_worktree(path, snapshot)
            self.manager.report(
                "session=task-worktree event=created "
                f"path={str(path)!r} base={base_commit} snapshot={snapshot} "
                f"dirty_paths={len(original_dirty_paths)}"
            )
            return TaskWorktree(
                manager=self.manager,
                path=path,
                repo_root=self.manager.repo_root,
                base_commit=base_commit,
                snapshot_commit=snapshot,
                original_dirty_paths=original_dirty_paths,
            )
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._cleanup()
        except Exception as cleanup_error:
            if exc is None:
                raise
            self.manager.report(
                "session=task-worktree event=cleanup_failed "
                f"error={str(cleanup_error)!r}"
            )

    def _cleanup(self) -> None:
        root = self.root
        if root is None:
            return
        cleanup_errors: list[str] = []
        if self.path is not None:
            try:
                self.manager.git.remove_worktree(self.path)
                self.manager.report(
                    "session=task-worktree event=removed "
                    f"path={str(self.path)!r}"
                )
            except (OSError, GitError) as exc:
                cleanup_errors.append(f"{self.path}: {exc}")
        self.manager.git.prune_worktrees()
        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError as exc:
            cleanup_errors.append(f"{root}: {exc}")
        self.path = None
        self.root = None
        if cleanup_errors:
            raise GitError(
                "could not remove disposable task worktree: "
                + "; ".join(cleanup_errors)
            )


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
