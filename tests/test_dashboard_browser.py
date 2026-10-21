import contextlib
import io
import os
import webbrowser
from unittest.mock import patch

from test_task_recovery import TaskRepositoryCase
from gigaflex.cli import main


class DashboardBrowserTest(TaskRepositoryCase):
    def test_review_opens_existing_dashboard_and_survives_unavailable_browser(self):
        for outcome in (True, False, webbrowser.Error("no browser"), OSError("cannot launch")):
            with self.subTest(outcome=outcome):
                self.run_review(browser_outcome=outcome)

    def test_review_can_disable_browser_opening(self):
        self.run_review(disabled=True)

    def run_review(self, *, browser_outcome=True, disabled=False):
        # Exercise URI escaping as well as opening after HTML creation.
        progress = self.repo / ".gigaflex" / "progress space #тест"
        config = self.root / "browser-config"
        config.write_text(f"[gigaflex]\nprogress_dir = {progress}\n")
        dashboard = (progress / "status-review.html").resolve()

        def open_browser(uri):
            self.assertEqual(dashboard.as_uri(), uri)
            self.assertTrue(dashboard.is_file())
            self.assertIn("<html", dashboard.read_text())
            if isinstance(browser_outcome, Exception):
                raise browser_outcome
            return browser_outcome

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"HOME": str(self.root / "home")}),
            patch("gigaflex.cli.webbrowser.open_new_tab", side_effect=open_browser) as browser,
            patch("gigaflex.cli.Runner.run") as run,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main([
                "--review", "--base-ref", "HEAD", "--config", str(config),
                *(["--no-open-dashboard"] if disabled else []),
            ])
        self.assertEqual(0, code, errors.getvalue())
        run.assert_called_once()
        self.assertIn(f"dashboard: {dashboard}", output.getvalue())
        if disabled:
            browser.assert_not_called()
        else:
            browser.assert_called_once()
        if not disabled and browser_outcome is not True:
            self.assertIn("could not open dashboard in browser", errors.getvalue())
        else:
            self.assertNotIn("could not open dashboard", errors.getvalue())
