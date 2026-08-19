from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.signals import (
    ALL_TASKS_DONE,
    FINALIZE_DONE,
    FINALIZE_FAILED,
    REVIEW_DONE,
    TASK_FAILED,
    detect_signal,
)


class SignalsTest(unittest.TestCase):
    def test_detects_gigaflex_signals(self) -> None:
        self.assertEqual(ALL_TASKS_DONE, detect_signal("done\n<<<GIGAFLEX:ALL_TASKS_DONE>>>\n"))
        self.assertEqual(TASK_FAILED, detect_signal("failed\n<<<GIGAFLEX:TASK_FAILED>>>\n"))
        self.assertEqual(REVIEW_DONE, detect_signal("review\n<<<GIGAFLEX:REVIEW_DONE>>>\n"))
        self.assertEqual(FINALIZE_DONE, detect_signal("finalized\n<<<GIGAFLEX:FINALIZE_DONE>>>\n"))
        self.assertEqual(FINALIZE_FAILED, detect_signal("failed\n<<<GIGAFLEX:FINALIZE_FAILED>>>\n"))

    def test_ignores_signal_that_is_not_the_final_non_empty_line(self) -> None:
        self.assertEqual("", detect_signal("<<<GIGAFLEX:ALL_TASKS_DONE>>>\nextra commentary\n"))


if __name__ == "__main__":
    unittest.main()
