from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.progress import ProgressLog


class ProgressLogTest(unittest.TestCase):
    def test_diagnostic_writes_timestamped_executor_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.txt"
            log = ProgressLog(path)

            log.diagnostic("session=task event=prepared prompt_chars=42")

            text = path.read_text(encoding="utf-8")
            self.assertRegex(
                text,
                r"^\[executor \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ",
            )
            self.assertIn("session=task event=prepared prompt_chars=42", text)

    def test_prompt_snapshot_contains_only_bounded_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress-demo.txt"
            path.write_text("old run secret\n", encoding="utf-8")
            log = ProgressLog(path)
            for index in range(8):
                log.write(f"current line {index}\n")

            snapshot = log.snapshot_for_prompt(max_lines=3, max_chars=1_000)
            text = snapshot.read_text(encoding="utf-8")

            self.assertEqual("context-demo.txt", snapshot.name)
            self.assertNotIn("old run secret", text)
            self.assertNotIn("current line 4", text)
            self.assertIn("current line 5", text)
            self.assertIn("current line 7", text)
            self.assertIn(log.run_id, text)

    def test_prompt_snapshot_is_refreshed_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.txt"
            log = ProgressLog(path)
            log.write("before render\n")
            snapshot = log.snapshot_for_prompt()
            log.write("after render\n")

            self.assertNotIn(
                "after render",
                snapshot.read_text(encoding="utf-8"),
            )
            log.snapshot_for_prompt()
            self.assertIn(
                "after render",
                snapshot.read_text(encoding="utf-8"),
            )

    def test_prompt_snapshot_keeps_a_bounded_tail_of_one_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = ProgressLog(Path(tmp) / "progress.txt")
            log.write("abcdefghij\n")

            text = log.snapshot_for_prompt(max_chars=5).read_text(encoding="utf-8")

            self.assertTrue(text.endswith("ghij\n"))


if __name__ == "__main__":
    unittest.main()
