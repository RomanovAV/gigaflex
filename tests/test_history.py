import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.cli import main
from gigaflex.executor import ExecResult
from gigaflex.git import GitService
from gigaflex.history import RunHistory, capture_repository, history_root
from gigaflex.progress import ProgressLog
from gigaflex.stats import InvocationStat, RunStatistics, TokenUsage
from gigaflex.signals import FINALIZE_DONE
from test_phase_verification import FINDING, decision
from test_task_recovery import TaskRepositoryCase


class HistoryTest(TaskRepositoryCase):
    def setUp(self):
        super().setUp()
        with (self.repo / ".gitignore").open("a") as stream:
            stream.write(".DS_Store\n")
        self.git.run("add", ".gitignore")
        self.git.run("commit", "-qm", "configured runtime ignores")
        self.initial_head = self.git.head_commit()

    def cli(self, execute, *extra, full=False):
        output, errors = io.StringIO(), io.StringIO()
        args = ["plan.md", "--no-branch", "--base-ref", self.initial_head,
                "--retry-count", "0", "--gigacode-command", "unused"]
        if not full:
            args.append("--tasks-only")
        args.extend(extra)
        with (patch.dict(os.environ, {"HOME": str(self.root / "home")}),
              patch("gigaflex.cli.webbrowser.open_new_tab", return_value=True),
              patch("gigaflex.cli.GigaCodeExecutor.run", side_effect=execute),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            code = main(args)
        return code, output.getvalue(), errors.getvalue()

    def complete(self, prompt, *, retry_guard=None, cwd=None):
        (cwd / "plan.md").write_text((cwd / "plan.md").read_text().replace("[ ]", "[x]"))
        git = GitService(cwd)
        git.run("add", ".")
        git.run("commit", "-qm", "complete")
        return ExecResult(output="done", returncode=0)

    def records(self):
        return [(p, json.loads(p.read_text())) for p in history_root(self.git).glob("*/*/result.json")]

    def test_new_invocation_preserves_history_inputs_and_own_log(self):
        code, output, errors = self.cli(self.complete)
        self.assertEqual(0, code, errors)
        first_path, first = self.records()[0]
        frozen = {p.name: p.read_bytes() for p in first_path.parent.iterdir()}
        self.assertEqual("success", first["status"])
        self.assertTrue(first["execution_started"])
        self.assertEqual("not_configured", first["validation"][0]["data"]["status"])
        self.assertIsNone(first["cost"]["amount"])
        inputs = json.loads((first_path.parent / "inputs.json").read_text())
        self.assertIn("[ ] Implement", inputs["plan"]["files"][0]["content"])
        self.assertEqual(self.initial_head, inputs["repository"]["head"])
        self.assertNotEqual(inputs["repository"]["head"], first["repository"]["head"])
        self.assertIn("task", inputs["prompts"])
        self.assertEqual("not_observed", inputs["external_instructions"]["status"])
        self.assertIn(str(first_path), output)
        self.assertFalse(self.git.is_dirty())

        # A repeat of the completed plan is its own invocation, even if it does no work.
        code, _, errors = self.cli(self.complete)
        self.assertEqual(0, code, errors)
        records = self.records()
        self.assertEqual(2, len(records))
        second_path, second = next(pair for pair in records if pair[0] != first_path)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(frozen, {p.name: p.read_bytes() for p in first_path.parent.iterdir()})
        self.assertNotIn("task iteration 1", (second_path.parent / "progress.txt").read_text())
        self.assertIn("task iteration 1", (first_path.parent / "progress.txt").read_text())

    def test_resume_links_to_stopped_attempt_despite_rejected_plain_launch(self):
        def fail(prompt, *, retry_guard=None, cwd=None):
            (cwd / "partial.txt").write_text("saved partial work")
            return ExecResult(output="service unavailable", returncode=1)

        code, _, errors = self.cli(fail)
        self.assertEqual(1, code, errors)
        first_path, first = self.records()[0]
        frozen = first_path.read_bytes()
        checkpoint_path = self.repo / ".gigaflex/progress/checkpoint-plan.json"
        stopped = json.loads(checkpoint_path.read_text())["blocked"]
        self.assertEqual(first["run_id"], stopped["history"]["run_id"])
        code, _, _ = self.cli(lambda *a, **kw: self.fail("executor must not run"))
        self.assertEqual(1, code)
        rejected = next(value for path, value in self.records() if path != first_path)
        self.assertFalse(rejected["execution_started"])
        self.assertEqual(stopped, json.loads(checkpoint_path.read_text())["blocked"])

        code, _, errors = self.cli(self.complete, "--resume", "--allow-dirty")
        self.assertEqual(0, code, errors)
        resumed = next(value for _, value in self.records() if value["mode"] == "resume")
        self.assertEqual(first["run_id"], resumed["run_id"])
        self.assertEqual(first["attempt_id"], resumed["parent_attempt_id"])
        self.assertNotEqual(first["attempt_id"], resumed["attempt_id"])
        self.assertEqual("linked", resumed["lineage"])
        self.assertEqual("success", resumed["status"])
        self.assertEqual(frozen, first_path.read_bytes())
        self.assertEqual("saved partial work", (self.repo / "partial.txt").read_text())

    def test_interrupt_is_recorded_and_restart_starts_an_independent_run(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt("stop")

        code, _, errors = self.cli(interrupt)
        self.assertEqual(130, code, errors)
        _, interrupted = self.records()[0]
        self.assertEqual("interrupted", interrupted["status"])
        code, _, errors = self.cli(self.complete, "--restart")
        self.assertEqual(0, code, errors)
        restarted = next(value for _, value in self.records() if value["mode"] == "restart")
        self.assertNotEqual(interrupted["run_id"], restarted["run_id"])
        self.assertIsNone(restarted["parent_attempt_id"])

    def test_input_snapshots_follow_effective_override_and_changed_resume_settings(self):
        prompts = self.repo / ".gigaflex/prompts"
        prompts.mkdir(parents=True)
        (prompts / "task.txt").write_text("ORIGINAL {task_number}")
        code, _, _ = self.cli(lambda *a, **kw: ExecResult(output="failed", returncode=1))
        self.assertEqual(1, code)
        first_path, _ = self.records()[0]
        first = json.loads((first_path.parent / "inputs.json").read_text())
        (prompts / "task.txt").write_text("REVISED {task_number}")
        code, _, errors = self.cli(self.complete, "--resume", "--task-model", "other-model")
        self.assertEqual(0, code, errors)
        second_path = next(path for path, value in self.records() if value["mode"] == "resume")
        second = json.loads((second_path.parent / "inputs.json").read_text())
        self.assertEqual("ORIGINAL {task_number}", first["prompts"]["task"]["content"])
        self.assertNotEqual(first["prompts"]["task"]["sha256"], second["prompts"]["task"]["sha256"])
        self.assertNotEqual(first["configuration"]["sha256"], second["configuration"]["sha256"])

    def test_events_keep_failed_checks_retries_agent_claims_and_usage(self):
        history = RunHistory(history_root(self.git), attempt_id="attempt", identity="test",
                             base_commit=self.initial_head, mode="new")
        history.record("validation", phase="task", status="failed", checks=[{"name": "test", "returncode": 1}])
        history.record("validation", phase="task", status="passed", checks=[{"name": "test", "returncode": 0}])
        history.record("review_decisions", source="agent_report", decisions=[{"decision": "rejected"}])
        history.executor_event("task", "retry_scheduled", {"next_attempt": 2})
        stats = RunStatistics()
        stats.on_invocation = history.invocation
        stats.add(InvocationStat("task", 1, "success", 0, 10, None, None, "session", ("actual-model",),
                                 TokenUsage(10, 5, 0, 15)))
        stats.finish()
        history.finish(statistics=stats, dashboard={}, repository={}, checkpoint={}, exit_code=0, artifact_paths={})
        result = json.loads((history.path / "result.json").read_text())
        self.assertEqual(["failed", "passed"], [item["data"]["status"] for item in result["validation"]])
        self.assertEqual(1, len(result["retries"]["executor"]))
        self.assertEqual("agent_report", result["review"]["decisions"][0]["data"]["source"])
        self.assertEqual(15, result["statistics"]["usage"]["total_tokens"])
        self.assertEqual(["actual-model"], result["statistics"]["invocations"][0]["models"])
        with self.assertRaises(RuntimeError):
            history.record("late")

    def test_actual_validation_failure_and_repair_survive_in_report(self):
        checks = [{"name": "value", "argv": [sys.executable, "-c",
                   "from pathlib import Path; assert Path('tracked.txt').read_text() == 'fixed'"],
                   "phases": ["task"]}]
        config = self.repo / ".gigaflex/config"
        config.parent.mkdir()
        config.write_text("[gigaflex]\nvalidation_commands = " + json.dumps(checks) + "\n")
        calls = []

        def execute(prompt, *, retry_guard=None, cwd=None):
            calls.append(prompt)
            if len(calls) == 2:
                (cwd / "tracked.txt").write_text("fixed")
            return self.complete(prompt, cwd=cwd)

        code, _, errors = self.cli(execute, "--retry-count", "1")
        self.assertEqual(0, code, errors)
        _, result = self.records()[0]
        self.assertEqual(["failed", "passed"], [item["data"]["status"] for item in result["validation"]])
        self.assertEqual("task_validation", result["retries"]["completion"][0]["data"]["phase"])
        self.assertEqual(2, len(calls))

    def test_review_findings_decisions_and_reused_phase_have_provenance(self):
        calls = []

        def execute(prompt, *, retry_guard=None, cwd=None):
            calls.append(prompt)
            if len(calls) == 1:
                return self.complete(prompt, cwd=cwd)
            if len(calls) == 2:
                return ExecResult(output=FINDING, returncode=0)
            if len(calls) == 3:
                (cwd / "tracked.txt").write_text("fixed\n")
                task_git = GitService(cwd)
                task_git.run("add", ".")
                task_git.run("commit", "-qm", "repair")
                return decision("fixed")
            if len(calls) == 4:
                return ExecResult(output="NO FINDINGS", returncode=0)
            return ExecResult(output=FINALIZE_DONE, signal=FINALIZE_DONE, returncode=0)

        code, _, errors = self.cli(execute, "--no-parallel-review", "--no-move-plan", full=True)
        self.assertEqual(0, code, errors)
        first_path, first = self.records()[0]
        self.assertEqual(5, len(calls))
        self.assertEqual("fixed", first["review"]["decisions"][0]["data"]["decisions"][0]["decision"])
        self.assertEqual(1, len(first["review"]["findings"][0]["data"]["findings"]))
        self.assertEqual([], first["review"]["findings"][-1]["data"]["findings"])
        self.assertEqual("passed", first["verification"][0]["data"]["review"])

        code, _, errors = self.cli(lambda *a, **kw: self.fail("checkpoint should be reused"),
                                  "--no-parallel-review", "--no-move-plan", full=True)
        self.assertEqual(0, code, errors)
        second = next(value for path, value in self.records() if path != first_path)
        self.assertEqual(["review", "finalize"], [item["data"]["phase"] for item in second["reused_phases"]])
        self.assertEqual(first["attempt_id"], second["reused_phases"][0]["data"]["history"]["attempt_id"])

    def test_unfinished_attempt_remains_visible_and_legacy_resume_is_explicit(self):
        history = RunHistory(history_root(self.git), attempt_id="unfinished", identity="test",
                             base_commit=self.initial_head, mode="resume")
        self.assertEqual("unavailable", history.started["lineage"])
        self.assertFalse((history.path / "result.json").exists())
        history.record("execution_started")
        self.assertEqual("running", json.loads((history.path / "started.json").read_text())["status"])
        self.assertTrue((history.path / "events.jsonl").exists())

    def test_linked_worktree_uses_shared_history_without_dirtying_repository(self):
        path = self.root / "linked"
        self.git.add_detached_worktree(path, self.initial_head)
        self.addCleanup(self.git.remove_worktree, path)
        self.assertEqual(history_root(self.git), history_root(GitService(path)))
        RunHistory(history_root(GitService(path)), attempt_id="linked", identity="test",
                   base_commit=self.initial_head, mode="new")
        self.assertFalse(GitService(path).is_dirty())

    def test_unborn_snapshot_preserves_index_and_versions_uncommitted_inputs(self):
        root = self.root / "unborn"
        root.mkdir()
        git = GitService(root)
        git.run("init", "-q")
        (root / "input.txt").write_text("staged")
        git.run("add", "input.txt")
        index_before = git.run("write-tree").stdout.strip()
        (root / "input.txt").write_text("working")
        (root / "untracked.txt").write_text("new")
        (root / "live.txt").write_text("runtime")
        state = capture_repository(git, (Path("live.txt"),), ())
        self.assertEqual("unborn", state["status"])
        self.assertEqual("", state["head"])
        self.assertEqual(index_before, state["index"])
        self.assertEqual("working", git.run("show", state["tree"] + ":input.txt").stdout)
        self.assertEqual("new", git.run("show", state["tree"] + ":untracked.txt").stdout)
        self.assertNotIn("live.txt", git.run("ls-tree", "--name-only", state["tree"]).stdout)
        self.assertEqual(index_before, git.run("write-tree").stdout.strip())
        self.assertEqual("", git.head_commit())

    def test_dry_run_does_not_create_history(self):
        code, _, errors = self.cli(lambda *a, **kw: self.fail("executor must not run"), "--dry-run")
        self.assertEqual(0, code, errors)
        self.assertFalse(history_root(self.git).exists())
