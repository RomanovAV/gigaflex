from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.git import (
    BranchBaseline,
    GitError,
    GitService,
    ReviewWorktreeManager,
    TaskWorktreeManager,
    jira_branch_name,
)


class GitServiceTest(unittest.TestCase):
    def test_branch_baseline_round_trips_branch_label_and_exact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            commit = git.head_commit()

            git.set_branch_baseline(
                "feature/ABC-123-demo",
                BranchBaseline(base_branch="release/1.0", base_commit=commit),
            )

            self.assertEqual(
                BranchBaseline(base_branch="release/1.0", base_commit=commit),
                git.branch_baseline("feature/ABC-123-demo"),
            )

    def test_new_branch_can_start_from_explicit_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "base")
            base = git.head_commit()
            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "later")

            git.switch_or_create_branch("feature/demo", base)

            self.assertEqual(base, git.head_commit())
            self.assertFalse((repo / "two.txt").exists())

    def test_review_worktrees_snapshot_dirty_state_and_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")

            (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            tracked = repo / "tracked.txt"
            tracked.write_text("original\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            head_before = git.head_commit()

            tracked.write_text("modified\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
            status_before = git.run("status", "--short").stdout
            diagnostics: list[str] = []
            manager = ReviewWorktreeManager(
                git,
                diagnostic=diagnostics.append,
                temp_parent=tmp_path,
            )

            with manager.create(["quality", "testing"]) as worktrees:
                created_paths = list(worktrees.paths.values())
                manifest = worktrees.review_manifest
                manifest_text = manifest.read_text(encoding="utf-8")
                self.assertEqual(head_before, git.head_commit())
                self.assertEqual(
                    "modified\n",
                    (created_paths[0] / "tracked.txt").read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    "untracked\n",
                    (created_paths[0] / "untracked.txt").read_text(encoding="utf-8"),
                )
                self.assertFalse((created_paths[0] / "ignored.txt").exists())
                self.assertEqual("", GitService(created_paths[0]).current_branch())
                self.assertFalse(GitService(created_paths[0]).is_dirty())
                self.assertEqual("review-context.txt", manifest.name)
                self.assertIn("changed_paths: 2", manifest_text)
                self.assertIn("diff --git a/tracked.txt b/tracked.txt", manifest_text)
                self.assertIn("diff --git a/untracked.txt b/untracked.txt", manifest_text)
                self.assertFalse((manifest.parent / "review-context").exists())

            self.assertTrue(all(not path.exists() for path in created_paths))
            self.assertFalse(manifest.exists())
            self.assertEqual(head_before, git.head_commit())
            self.assertEqual(status_before, git.run("status", "--short").stdout)
            worktree_list = git.run("worktree", "list", "--porcelain").stdout
            self.assertNotIn("gigaflex-review-", worktree_list)
            self.assertIn("event=snapshot_created", "\n".join(diagnostics))
            self.assertIn("event=packet_created", "\n".join(diagnostics))
            self.assertIn("event=packet_removed", "\n".join(diagnostics))
            self.assertIn("event=removed", "\n".join(diagnostics))

    def test_large_review_context_is_written_to_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            for index in range(120):
                (repo / f"component-{index:03d}-with-a-descriptive-name.txt").write_text(
                    f"new content {index}\n",
                    encoding="utf-8",
                )

            manager = ReviewWorktreeManager(git, temp_parent=tmp_path)
            review_context = None
            with manager.create(["quality"]) as worktrees:
                review_context = worktrees.review_manifest
                manifest_text = worktrees.review_manifest.read_text(encoding="utf-8")
                self.assertEqual("review-context.txt", review_context.name)
                self.assertIn("changed_paths: 120", manifest_text)
                self.assertEqual(120, manifest_text.count("diff --git "))
                self.assertFalse((review_context.parent / "indexes").exists())
                self.assertFalse((review_context.parent / "patches").exists())
                self.assertFalse((review_context.parent / "summaries").exists())

            assert review_context is not None
            self.assertFalse(review_context.exists())

    def test_review_worktrees_are_removed_when_review_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            git.run("add", ".")
            snapshot_env = {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
            git.run("commit", "-m", "initial", env=snapshot_env)

            def broken_diagnostic(_line: str) -> None:
                raise OSError("log unavailable")

            manager = ReviewWorktreeManager(
                git,
                diagnostic=broken_diagnostic,
                temp_parent=tmp_path,
            )
            created_path = None
            packet = None

            with self.assertRaisesRegex(RuntimeError, "review failed"):
                with manager.create(["quality"]) as worktrees:
                    created_path = worktrees.paths["quality"]
                    packet = worktrees.review_manifest
                    raise RuntimeError("review failed")

            assert created_path is not None
            assert packet is not None
            self.assertFalse(created_path.exists())
            self.assertFalse(packet.exists())
            self.assertNotIn(
                "gigaflex-review-",
                git.run("worktree", "list", "--porcelain").stdout,
            )

    def test_review_packet_is_removed_when_worktree_prune_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            git.run("add", ".")
            git.run(
                "commit",
                "-m",
                "initial",
                env={
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
            )
            diagnostics: list[str] = []
            manager = ReviewWorktreeManager(
                git,
                diagnostic=diagnostics.append,
                temp_parent=tmp_path,
            )
            packet = None

            def failed_prune() -> None:
                raise GitError("simulated prune failure")

            git.prune_worktrees = failed_prune  # type: ignore[method-assign]
            with self.assertRaisesRegex(GitError, "simulated prune failure"):
                with manager.create(["quality"]) as worktrees:
                    packet = worktrees.review_manifest

            assert packet is not None
            self.assertFalse(packet.exists())
            self.assertIn("event=packet_removed", "\n".join(diagnostics))

    def test_task_worktree_promotes_only_committed_delta_and_preserves_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            (repo / "user.txt").write_text("original\n", encoding="utf-8")
            (repo / "plan.md").write_text("pending\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            (repo / "user.txt").write_text("user change\n", encoding="utf-8")
            progress = repo / "progress.txt"
            progress.write_text("before\n", encoding="utf-8")
            status_before = git.run("status", "--short").stdout
            head_before = git.head_commit()
            diagnostics: list[str] = []
            manager = TaskWorktreeManager(
                git,
                diagnostic=diagnostics.append,
                temp_parent=tmp_path,
                ignored_paths=(Path("progress.txt"),),
            )

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                self.assertEqual(
                    "user change\n",
                    (workspace.path / "user.txt").read_text(encoding="utf-8"),
                )
                (workspace.path / "plan.md").write_text("complete\n", encoding="utf-8")
                (workspace.path / "result.txt").write_text("done\n", encoding="utf-8")
                task_git.run("add", "plan.md", "result.txt")
                task_git.run("commit", "-m", "feat: complete task")
                progress.write_text("after\n", encoding="utf-8")
                workspace.promote(task_git.head_commit())

            self.assertNotEqual(head_before, git.head_commit())
            self.assertEqual("complete\n", (repo / "plan.md").read_text(encoding="utf-8"))
            self.assertEqual("done\n", (repo / "result.txt").read_text(encoding="utf-8"))
            self.assertEqual("user change\n", (repo / "user.txt").read_text(encoding="utf-8"))
            self.assertEqual("after\n", progress.read_text(encoding="utf-8"))
            self.assertEqual(status_before, git.run("status", "--short").stdout)
            self.assertIn("event=promoted", "\n".join(diagnostics))

    def test_task_worktree_adopts_touched_dirty_file_into_task_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            tracked = repo / "tracked.txt"
            tracked.write_text("original\n", encoding="utf-8")
            staged = repo / "staged.txt"
            staged.write_text("original\n", encoding="utf-8")
            untouched = repo / "untouched.txt"
            untouched.write_text("original\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            tracked.write_text("user change\n", encoding="utf-8")
            staged.write_text("staged user change\n", encoding="utf-8")
            untouched.write_text("staged user change\n", encoding="utf-8")
            git.run("add", "staged.txt", "untouched.txt")
            head_before = git.head_commit()
            diagnostics: list[str] = []
            manager = TaskWorktreeManager(
                git,
                diagnostic=diagnostics.append,
                temp_parent=tmp_path,
            )

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                (workspace.path / "tracked.txt").write_text(
                    "user change\nagent change\n",
                    encoding="utf-8",
                )
                (workspace.path / "staged.txt").write_text(
                    "staged user change\nagent change\n",
                    encoding="utf-8",
                )
                task_git.run("add", "tracked.txt", "staged.txt")
                task_git.run("commit", "-m", "feat: overlap")
                workspace.promote(task_git.head_commit())

            self.assertNotEqual(head_before, git.head_commit())
            self.assertEqual(
                "user change\nagent change\n",
                tracked.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "user change\nagent change\n",
                git.run("show", "HEAD:tracked.txt").stdout,
            )
            self.assertEqual(
                "staged user change\nagent change\n",
                git.run("show", "HEAD:staged.txt").stdout,
            )
            self.assertEqual(
                "staged user change\n",
                untouched.read_text(encoding="utf-8"),
            )
            self.assertEqual("M  untouched.txt\n", git.run("status", "--short").stdout)
            self.assertEqual("feat: overlap", git.run("log", "-1", "--format=%s").stdout.strip())
            self.assertIn(
                "event=adopted_preexisting_changes count=2 "
                "paths='staged.txt, tracked.txt'",
                "\n".join(diagnostics),
            )

    def test_task_worktree_adopts_touched_untracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            new_file = repo / "new.txt"
            new_file.write_text("user content\n", encoding="utf-8")
            manager = TaskWorktreeManager(git, temp_parent=tmp_path)

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                (workspace.path / "result.txt").write_text("ready\n", encoding="utf-8")
                task_git.run("add", "result.txt")
                task_git.run("commit", "-m", "feat: prepare task")
                (workspace.path / "new.txt").write_text(
                    "user content\nagent content\n",
                    encoding="utf-8",
                )
                task_git.run("add", "new.txt")
                task_git.run("commit", "-m", "feat: complete new file")
                workspace.promote(task_git.head_commit())

            self.assertEqual(
                "user content\nagent content\n",
                new_file.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "user content\nagent content\n",
                git.run("show", "HEAD:new.txt").stdout,
            )
            self.assertEqual(
                ["feat: prepare task", "feat: complete new file"],
                git.run(
                    "log",
                    "--reverse",
                    "--format=%s",
                    "HEAD~2..HEAD",
                ).stdout.splitlines(),
            )
            self.assertEqual("", git.run("status", "--short").stdout)

    def test_task_worktree_adopts_task_deletion_of_dirty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            removed = repo / "removed.txt"
            removed.write_text("original\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            removed.write_text("user change\n", encoding="utf-8")
            manager = TaskWorktreeManager(git, temp_parent=tmp_path)

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                task_git.run("rm", "removed.txt")
                task_git.run("commit", "-m", "feat: remove obsolete file")
                workspace.promote(task_git.head_commit())

            self.assertFalse(removed.exists())
            self.assertNotEqual(
                0,
                git.run("cat-file", "-e", "HEAD:removed.txt", check=False).returncode,
            )
            self.assertEqual("", git.run("status", "--short").stdout)

    def test_task_worktree_rejects_concurrent_main_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            tracked = repo / "tracked.txt"
            tracked.write_text("original\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            tracked.write_text("user change\n", encoding="utf-8")
            head_before = git.head_commit()
            manager = TaskWorktreeManager(git, temp_parent=tmp_path)

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                (workspace.path / "tracked.txt").write_text(
                    "user change\nagent change\n",
                    encoding="utf-8",
                )
                task_git.run("add", "tracked.txt")
                task_git.run("commit", "-m", "feat: overlap")
                tracked.write_text("concurrent user change\n", encoding="utf-8")
                with self.assertRaisesRegex(GitError, "main working tree changed"):
                    workspace.promote(task_git.head_commit())

            self.assertEqual(head_before, git.head_commit())
            self.assertEqual(
                "concurrent user change\n",
                tracked.read_text(encoding="utf-8"),
            )

    def test_task_worktree_rolls_back_head_and_dirty_state_when_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")
            tracked = repo / "tracked.txt"
            tracked.write_text("original\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            tracked.write_text("user change\n", encoding="utf-8")
            head_before = git.head_commit()
            status_before = git.run("status", "--short").stdout
            manager = TaskWorktreeManager(git, temp_parent=tmp_path)

            with manager.create("task 1") as workspace:
                task_git = GitService(workspace.path)
                (workspace.path / "tracked.txt").write_text(
                    "user change\nagent change\n",
                    encoding="utf-8",
                )
                task_git.run("add", "tracked.txt")
                task_git.run("commit", "-m", "feat: overlap")

                replace_paths = git.replace_paths_from_ref
                calls = 0

                def fail_first_install(ref, paths):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise GitError("injected install failure")
                    replace_paths(ref, paths)

                git.replace_paths_from_ref = fail_first_install  # type: ignore[method-assign]
                with self.assertRaisesRegex(GitError, "injected install failure"):
                    workspace.promote(task_git.head_commit())

            self.assertEqual(head_before, git.head_commit())
            self.assertEqual(status_before, git.run("status", "--short").stdout)
            self.assertEqual("user change\n", tracked.read_text(encoding="utf-8"))

    def test_dirty_paths_preserves_spaces_and_both_sides_of_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")

            original = repo / "old name.txt"
            original.write_text("tracked\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")

            git.run("mv", "old name.txt", "new name.txt")
            (repo / "untracked file.txt").write_text("new\n", encoding="utf-8")

            self.assertEqual(
                {
                    Path("old name.txt"),
                    Path("new name.txt"),
                    Path("untracked file.txt"),
                },
                set(git.dirty_paths()),
            )

    def test_jira_branch_name_uses_task_and_plan_description(self) -> None:
        self.assertEqual(
            "feature/PROJ-123-add-demo-feature",
            jira_branch_name(Path("docs/plans/20260625-add-demo-feature.md"), "PROJ-123"),
        )

    def test_ensure_clean_reports_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(Exception, "dirty.txt"):
                git.ensure_clean(False)

    def test_commit_subjects_since_returns_new_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")

            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            head = git.head_commit()

            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "PROJ-123 feat: add two")

            self.assertEqual(["PROJ-123 feat: add two"], git.commit_subjects_since(head))

    def test_prefix_commit_messages_since_rewrites_all_missing_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")

            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            base = git.head_commit()

            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "feat: add two", "-m", "Detailed body.")
            (repo / "three.txt").write_text("three\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "PROJ-123 test: add three")
            old_head = git.head_commit()
            old_metadata = git.run(
                "log",
                "--reverse",
                "--format=%an%x00%ae%x00%at%x00%cn%x00%ce%x00%ct",
                f"{base}..HEAD",
            ).stdout

            changed = git.prefix_commit_messages_since(base, "PROJ-123")

            new_commits = git.run(
                "rev-list",
                "--reverse",
                f"{base}..HEAD",
            ).stdout.splitlines()
            messages = [
                git.run("show", "-s", "--format=%B", commit).stdout
                for commit in new_commits
            ]
            new_metadata = git.run(
                "log",
                "--reverse",
                "--format=%an%x00%ae%x00%at%x00%cn%x00%ce%x00%ct",
                f"{base}..HEAD",
            ).stdout

            self.assertEqual(["feat: add two"], changed)
            self.assertNotEqual(old_head, git.head_commit())
            self.assertEqual(
                [
                    "PROJ-123 feat: add two\n\nDetailed body.\n\n",
                    "PROJ-123 test: add three\n\n",
                ],
                messages,
            )
            self.assertEqual(old_metadata, new_metadata)
            self.assertFalse(git.is_dirty())
            self.assertEqual("two\n", (repo / "two.txt").read_text(encoding="utf-8"))
            self.assertEqual("three\n", (repo / "three.txt").read_text(encoding="utf-8"))

    def test_prefix_commit_messages_since_keeps_already_prefixed_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaFlex Test")

            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "initial")
            base = git.head_commit()
            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            git.run("add", ".")
            git.run("commit", "-m", "PROJ-123 feat: add two")
            original_head = git.head_commit()

            changed = git.prefix_commit_messages_since(base, "PROJ-123")

            self.assertEqual([], changed)
            self.assertEqual(original_head, git.head_commit())


if __name__ == "__main__":
    unittest.main()
