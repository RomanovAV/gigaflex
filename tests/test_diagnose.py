from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.diagnose import _argv, _run_parallel


class DiagnoseTest(unittest.TestCase):
    def test_uses_confirmed_gigacode_arguments(self) -> None:
        self.assertEqual(
            [
                "gigacode",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "check shell",
            ],
            _argv("check shell"),
        )

    def test_parallel_probe_captures_each_worker_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            results = _run_parallel(
                [sys.executable, "-c", "print('probe ok')"],
                log_dir,
                3,
            )

            self.assertEqual([(0, "probe ok\n")] * 3, results)
            self.assertEqual(
                "probe ok\n",
                (log_dir / "parallel-1.log").read_text(encoding="utf-8"),
            )

    def test_parallel_probe_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], _run_parallel(["unused"], Path(tmp), 0))


if __name__ == "__main__":
    unittest.main()
