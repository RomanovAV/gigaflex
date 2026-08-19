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


if __name__ == "__main__":
    unittest.main()
