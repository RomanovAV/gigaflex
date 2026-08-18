from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigalphex.git import (
    BranchBaseline,
    GitService,
    ReviewWorktreeManager,
    jira_branch_name,
)


class GitServiceTest(unittest.TestCase):
    def test_branch_baseline_round_trips_branch_label_and_exact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaLphex Test")
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
            git.run("config", "user.name", "GigaLphex Test")
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
            git.run("config", "user.name", "GigaLphex Test")

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

            self.assertTrue(all(not path.exists() for path in created_paths))
            self.assertEqual(head_before, git.head_commit())
            self.assertEqual(status_before, git.run("status", "--short").stdout)
            worktree_list = git.run("worktree", "list", "--porcelain").stdout
            self.assertNotIn("gigalphex-review-", worktree_list)
            self.assertIn("event=snapshot_created", "\n".join(diagnostics))
            self.assertIn("event=removed", "\n".join(diagnostics))

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

            with self.assertRaisesRegex(RuntimeError, "review failed"):
                with manager.create(["quality"]) as worktrees:
                    created_path = worktrees.paths["quality"]
                    raise RuntimeError("review failed")

            assert created_path is not None
            self.assertFalse(created_path.exists())
            self.assertNotIn(
                "gigalphex-review-",
                git.run("worktree", "list", "--porcelain").stdout,
            )

    def test_dirty_paths_preserves_spaces_and_both_sides_of_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = GitService(repo)
            git.run("init")
            git.run("config", "user.email", "test@example.com")
            git.run("config", "user.name", "GigaLphex Test")

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
            git.run("config", "user.name", "GigaLphex Test")

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
            git.run("config", "user.name", "GigaLphex Test")

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
            git.run("config", "user.name", "GigaLphex Test")

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
