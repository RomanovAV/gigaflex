from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.checkpoint import RunCheckpoint, checkpoint_path
from gigaflex.git import GitService


class RunCheckpointTest(unittest.TestCase):
    def test_reuses_completed_phase_only_for_identical_head_and_tree(self) -> None:
        with temporary_git_repo() as (repo, git):
            path = repo / ".gigaflex/checkpoint.json"
            checkpoint = RunCheckpoint(
                path,
                git,
                identity="plan:demo",
                base_commit=git.head_commit(),
                ignored_paths=(Path(".gigaflex/checkpoint.json"),),
            )
            state = checkpoint.current_state()
            checkpoint.mark_completed("review", state)

            reloaded = RunCheckpoint(
                path,
                git,
                identity="plan:demo",
                base_commit=git.head_commit(),
                ignored_paths=(Path(".gigaflex/checkpoint.json"),),
            )
            self.assertTrue(reloaded.can_reuse("review", reloaded.current_state()))

            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            self.assertFalse(reloaded.can_reuse("review", reloaded.current_state()))

    def test_changed_base_resets_previous_phase_results(self) -> None:
        with temporary_git_repo() as (repo, git):
            path = repo / "checkpoint.json"
            first = RunCheckpoint(
                path,
                git,
                identity="plan:demo",
                base_commit=git.head_commit(),
                ignored_paths=(Path("checkpoint.json"),),
            )
            first.mark_completed("finalize", first.current_state())
            (repo / "next.txt").write_text("next\n", encoding="utf-8")
            git.run("add", "next.txt")
            git.run("commit", "-m", "next")

            second = RunCheckpoint(
                path,
                git,
                identity="plan:demo",
                base_commit=git.head_commit(),
                ignored_paths=(Path("checkpoint.json"),),
            )

            self.assertFalse(second.can_reuse("finalize", second.current_state()))

    def test_checkpoint_path_follows_progress_name(self) -> None:
        self.assertEqual(
            Path(".gigaflex/progress/checkpoint-demo.json"),
            checkpoint_path(Path(".gigaflex/progress/progress-demo.txt")),
        )


class temporary_git_repo:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        repo = Path(self.tmp.name)
        git = GitService(repo)
        git.run("init")
        git.run("config", "user.email", "test@example.com")
        git.run("config", "user.name", "GigaFlex Test")
        (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        git.run("add", "tracked.txt")
        git.run("commit", "-m", "initial")
        return repo, git

    def __exit__(self, exc_type, exc, traceback):
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
