from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.dashboard import ProgressDashboard, dashboard_paths


class ProgressDashboardTest(unittest.TestCase):
    def test_creates_json_and_self_contained_html_from_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.md"
            plan.write_text(
                """# Useful feature

## Task 1: First task
- [x] Finished item
- [ ] Remaining item

## Task 2: Second task
- [ ] Another item
""",
                encoding="utf-8",
            )
            progress = root / "progress-feature.txt"
            json_path, html_path = dashboard_paths(progress)
            dashboard = ProgressDashboard(
                json_path,
                html_path,
                name="feature",
                plan_file=plan,
                progress_file=progress,
                branch="feature/dashboard",
            )

            dashboard.start()

            state = json.loads(json_path.read_text(encoding="utf-8"))
            page = html_path.read_text(encoding="utf-8")
            self.assertEqual("Useful feature", state["title"])
            self.assertEqual(2, len(state["tasks"]))
            self.assertEqual(1, state["tasks"][0]["completed_items"])
            self.assertIn("Useful feature", page)
            self.assertIn('http-equiv="refresh" content="2"', page)
            self.assertIn("feature/dashboard", page)
            self.assertNotIn("fetch(", page)

    def test_tracks_task_session_usage_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.md"
            plan.write_text(
                """# Plan

## Task 1: Implement
- [ ] Do it
""",
                encoding="utf-8",
            )
            dashboard = ProgressDashboard(
                root / "status.json",
                root / "status.html",
                name="plan",
                plan_file=plan,
            )
            dashboard.start()
            dashboard.phase_started("tasks")
            dashboard.task_started(1, "Implement", 1)
            dashboard.executor_event("task", "attempt_started", {"attempt": 1, "attempts": 2})
            dashboard.executor_event("task", "started", {"pid": 42})
            dashboard.executor_event(
                "task",
                "finished",
                {
                    "returncode": 0,
                    "duration_ms": 1200,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            )
            plan.write_text(plan.read_text(encoding="utf-8").replace("[ ]", "[x]"), encoding="utf-8")
            dashboard.task_finished()
            dashboard.complete()

            state = dashboard.state
            self.assertEqual("success", state["status"])
            self.assertEqual("completed", state["sessions"]["task"]["status"])
            self.assertEqual(120, state["usage"]["total_tokens"])
            self.assertEqual("completed", state["tasks"][0]["status"])
            self.assertEqual("completed", state["phases"][0]["status"])
            self.assertNotIn(
                'http-equiv="refresh"',
                (root / "status.html").read_text(encoding="utf-8"),
            )

    def test_failure_is_visible_in_json_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard = ProgressDashboard(
                root / "status.json",
                root / "status.html",
                name="review",
                plan_file=None,
            )
            dashboard.start()
            dashboard.phase_started("review")
            dashboard.review_attempt_started(1, 3, parallel=True)
            dashboard.fail("review output was invalid")

            self.assertEqual("failed", dashboard.state["status"])
            self.assertEqual("failed", dashboard.state["phases"][1]["status"])
            self.assertEqual("failed", dashboard.state["review"]["status"])
            self.assertEqual(
                "failed",
                dashboard.state["review"]["attempts"][0]["status"],
            )
            self.assertIn(
                "review output was invalid",
                (root / "status.html").read_text(encoding="utf-8"),
            )

    def test_tracks_review_status_and_attempt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard = ProgressDashboard(
                root / "status.json",
                root / "status.html",
                name="review",
                plan_file=None,
                review_iterations=3,
            )
            dashboard.start()
            dashboard.phase_started("review")
            dashboard.review_attempt_started(1, 3, parallel=True)
            dashboard.review_synthesis_started(1, 2)
            dashboard.review_attempt_finished(
                1,
                "needs_another_pass",
                findings=2,
                message="Fixes applied; checking again",
            )
            dashboard.review_attempt_started(2, 3, parallel=True)
            dashboard.review_attempt_finished(
                2,
                "passed",
                findings=0,
                message="No findings remain",
            )

            review = dashboard.state["review"]
            page = (root / "status.html").read_text(encoding="utf-8")
            self.assertEqual("passed", review["status"])
            self.assertEqual(2, review["current_attempt"])
            self.assertEqual(3, review["max_attempts"])
            self.assertEqual(
                ["needs_another_pass", "passed"],
                [attempt["status"] for attempt in review["attempts"]],
            )
            self.assertIn("Review status", page)
            self.assertIn("Attempt 2 of 3", page)
            self.assertIn("Another pass required", page)
            self.assertIn("No findings remain", page)

    def test_dashboard_paths_follow_progress_name(self) -> None:
        self.assertEqual(
            (Path("status-demo.json"), Path("status-demo.html")),
            dashboard_paths(Path("progress-demo.txt")),
        )


if __name__ == "__main__":
    unittest.main()
