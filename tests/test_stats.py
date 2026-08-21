from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.stats import InvocationStat, RunStatistics, TokenUsage, statistics_path


class RunStatisticsTest(unittest.TestCase):
    def test_aggregates_tokens_and_call_durations(self) -> None:
        stats = RunStatistics()
        stats.add(
            InvocationStat(
                session="task",
                attempt=1,
                status="success",
                returncode=0,
                wall_duration_ms=1500,
                reported_duration_ms=1400,
                api_duration_ms=1000,
                session_id="session-1",
                models=("model-a",),
                usage=TokenUsage(100, 20, 5, 120),
            )
        )
        stats.add(
            InvocationStat(
                session="review-agent:quality",
                attempt=1,
                status="success",
                returncode=0,
                wall_duration_ms=2500,
                reported_duration_ms=2400,
                api_duration_ms=2000,
                session_id="session-2",
                models=("model-b",),
                usage=TokenUsage(200, 30, 10, 230),
            )
        )
        stats.finish()

        data = stats.to_dict()

        self.assertEqual("success", data["status"])
        self.assertEqual(2, data["call_count"])
        self.assertEqual(4000, data["summed_call_duration_ms"])
        self.assertEqual(
            {
                "input_tokens": 300,
                "output_tokens": 50,
                "cache_read_input_tokens": 15,
                "total_tokens": 350,
            },
            data["usage"],
        )
        self.assertIn("review-agent:quality", stats.render_text())

    def test_writes_machine_readable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            stats = RunStatistics()
            stats.finish()

            stats.write_json(path)
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(0, data["call_count"])
            self.assertEqual("success", data["status"])
            self.assertIsNone(data["usage"])

    def test_marks_interrupted_status(self) -> None:
        stats = RunStatistics()

        stats.finish("interrupted")

        self.assertEqual("interrupted", stats.to_dict()["status"])
        self.assertIn("status: interrupted", stats.render_text())

    def test_records_failure_phase_and_reason(self) -> None:
        stats = RunStatistics()

        stats.finish(
            "failed",
            failure_phase="tasks",
            failure_reason="task failed",
        )

        data = stats.to_dict()
        self.assertEqual("tasks", data["failure_phase"])
        self.assertEqual("task failed", data["failure_reason"])
        self.assertIn("failure phase: tasks", stats.render_text())
        self.assertIn("failure reason: task failed", stats.render_text())

    def test_statistics_path_matches_progress_file(self) -> None:
        self.assertEqual(
            Path(".gigaflex/progress/stats-demo.json"),
            statistics_path(Path(".gigaflex/progress/progress-demo.txt")),
        )


if __name__ == "__main__":
    unittest.main()
