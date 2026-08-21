from pathlib import Path
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.cli import (
    add_gigacode_args,
    branch_for_plan,
    build_parser,
    completed_plan_commit_message,
    find_interactively_created_plan,
    main,
    normalize_jira_task,
    plan_commit_message,
    should_auto_init,
    should_use_interactive_plan,
)


def write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class CliTest(unittest.TestCase):
    def test_extra_gigacode_args_are_inserted_before_prompt_option(self) -> None:
        self.assertEqual(
            [
                "--include-directories=/workspace/shared",
                "-p",
                "{prompt}",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
            ],
            add_gigacode_args(
                [
                    "-p",
                    "{prompt}",
                    "--approval-mode=auto-edit",
                    "--allowed-tools",
                    "run_shell_command",
                ],
                ["--include-directories=/workspace/shared"],
            ),
        )

    def test_extra_gigacode_args_do_not_split_legacy_prompt_flag_and_value(self) -> None:
        self.assertEqual(
            [
                "--include-directories=/workspace/shared",
                "-p",
                "{prompt}",
            ],
            add_gigacode_args(
                ["-p", "{prompt}"],
                ["--include-directories=/workspace/shared"],
            ),
        )

    def test_main_creates_global_config_and_prompt_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            project = tmp_path / "project"
            project.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--init"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            config = home / ".config/gigaflex/config"
            self.assertTrue(config.is_file())
            self.assertIn("# task_model =", config.read_text(encoding="utf-8"))
            self.assertTrue((home / ".config/gigaflex/prompts/task.txt").is_file())
            self.assertTrue((home / ".config/gigaflex/prompts/plan_skill.txt").is_file())
            self.assertTrue((home / ".config/gigaflex/prompts/review_synthesis.txt").is_file())
            self.assertFalse((project / ".gigaflex/prompts").exists())

    def test_regular_command_falls_back_to_local_files_when_global_config_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.write_text("not a writable directory", encoding="utf-8")
            project = tmp_path / "project"
            project.mkdir()
            skills_dir = tmp_path / "skills"
            stdout = io.StringIO()
            stderr = io.StringIO()
            original_cwd = Path.cwd()

            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(
                    stdout
                ), contextlib.redirect_stderr(stderr):
                    code = main(["--install-planning-skill", "--skill-dir", str(skills_dir)])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue((skills_dir / "planning/SKILL.md").is_file())
            self.assertTrue((project / ".gigaflex/config").is_file())
            self.assertTrue((project / ".gigaflex/prompts/task.txt").is_file())
            self.assertTrue((project / ".gigaflex/prompts/plan_skill.txt").is_file())
            self.assertIn("installed planning skill", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())

    def test_init_prompts_creates_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            project = tmp_path / "project"
            project.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--init-prompts"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue((project / ".gigaflex/prompts/task.txt").is_file())
            self.assertTrue((project / ".gigaflex/prompts/review_synthesis.txt").is_file())
            self.assertFalse((project / ".gigaflex/config").exists())

    def test_auto_init_requires_existing_plan_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                args = build_parser().parse_args(["docs/plans/missing.md"])

                self.assertFalse(should_auto_init(args))
            finally:
                os.chdir(original_cwd)

    def test_openspec_flag_is_distinct_from_plan_creation_and_plan_file(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(["plan.md", "--openspec", "openspec/changes/demo"])

        self.assertEqual(2, code)
        self.assertIn("cannot be combined with a markdown plan file", stderr.getvalue())

    def test_openspec_dry_run_uses_tasks_and_lists_change_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            change = tmp_path / "openspec/changes/add-search"
            spec = change / "specs/search/spec.md"
            spec.parent.mkdir(parents=True)
            (change / "tasks.md").write_text(
                "## 1. Build search\n- [ ] 1.1 Implement search\n",
                encoding="utf-8",
            )
            (change / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
            spec.write_text("## ADDED Requirements\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = main(["--openspec", str(change), "--dry-run"])

            output = stdout.getvalue()
            self.assertEqual(0, code)
            self.assertIn("Selected task identity: 1: Build search", output)
            self.assertIn(str(change / "proposal.md"), output)
            self.assertIn(str(spec), output)
            self.assertIn("progress-add-search.txt", output)

    def test_openspec_dry_run_accepts_localized_prose_tasks_without_checkboxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            change = Path(tmp) / "openspec/changes/rko-check"
            change.mkdir(parents=True)
            (change / "tasks.md").write_text(
                """# Задачи: изменение проверки

## Задача 1: Добавить метод

Изменить интерфейс адаптера.
""",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = main(["--openspec", str(change), "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("Selected task identity: 1: Добавить метод", stdout.getvalue())
            self.assertIn(
                "<COMPLETION_MARKER>\n- [x] 1. Добавить метод\n</COMPLETION_MARKER>",
                stdout.getvalue(),
            )

    def test_completed_openspec_change_is_not_moved_and_prints_archive_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            home = tmp_path / "home"
            change = repo / "openspec/changes/add-search"
            change.mkdir(parents=True)
            tasks = change / "tasks.md"
            tasks.write_text(
                "## 1. Build search\n- [x] 1.1 Implement search\n",
                encoding="utf-8",
            )
            original_cwd = Path.cwd()
            stdout = io.StringIO()
            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                with patch.dict(os.environ, {"HOME": str(home)}), patch(
                    "gigaflex.cli.Runner.run"
                ), contextlib.redirect_stdout(stdout):
                    code = main(["--openspec", str(change), "--no-branch"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue(tasks.is_file())
            self.assertFalse((change / "completed/tasks.md").exists())
            self.assertIn("OpenSpec change complete: add-search", stdout.getvalue())
            self.assertIn("openspec archive add-search", stdout.getvalue())

    def test_finalize_cli_defaults_to_config_and_can_be_disabled(self) -> None:
        parser = build_parser()

        self.assertIsNone(parser.parse_args([]).finalize)
        self.assertTrue(parser.parse_args(["--finalize"]).finalize)
        self.assertFalse(parser.parse_args(["--no-finalize"]).finalize)

    def test_jira_task_normalizes_branch_and_commit_messages(self) -> None:
        self.assertEqual("PROJ-123", normalize_jira_task("proj-123"))
        self.assertEqual("123", normalize_jira_task("123"))
        self.assertEqual(
            "feature/PROJ-123-add-demo-feature",
            branch_for_plan(Path("docs/plans/20260625-add-demo-feature.md"), None, "PROJ-123"),
        )
        self.assertEqual(
            "PROJ-123 docs: add plan 20260625-add-demo-feature",
            plan_commit_message(Path("docs/plans/20260625-add-demo-feature.md"), "PROJ-123"),
        )
        self.assertEqual(
            "PROJ-123 docs: complete plan 20260625-add-demo-feature",
            completed_plan_commit_message(
                Path("docs/plans/20260625-add-demo-feature.md"),
                "PROJ-123",
            ),
        )

    def test_invalid_jira_task_is_rejected(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(["--review", "--jira-task", "PROJ/123"])

        self.assertEqual(2, code)
        self.assertIn("Jira task must be a number or key like PROJ-123", stderr.getvalue())

    def test_jira_task_rejects_no_branch_for_plan_runs(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(["docs/plans/my-plan.md", "--jira-task", "PROJ-123", "--no-branch"])

        self.assertEqual(2, code)
        self.assertIn("--jira-task requires branch creation", stderr.getvalue())

    def test_jira_task_rejects_non_corporate_explicit_branch(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "docs/plans/my-plan.md",
                    "--jira-task",
                    "PROJ-123",
                    "--branch",
                    "my-plan",
                ]
            )

        self.assertEqual(2, code)
        self.assertIn("feature/PROJ-123-", stderr.getvalue())

    def test_quick_requires_plan(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(["--quick"])

        self.assertEqual(2, code)
        self.assertIn("--quick requires --plan", stderr.getvalue())

    def test_force_skill_install_requires_install_command(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = main(["--force-skill-install"])

        self.assertEqual(2, code)
        self.assertIn("--force-skill-install requires", stderr.getvalue())

    def test_interactive_plan_requires_tty_and_can_be_forced_quick(self) -> None:
        interactive_args = build_parser().parse_args(["--plan", "add demo"])
        quick_args = build_parser().parse_args(["--plan", "add demo", "--quick"])

        with patch("sys.stdin.isatty", return_value=True), patch(
            "sys.stdout.isatty",
            return_value=True,
        ):
            self.assertTrue(should_use_interactive_plan(interactive_args))
            self.assertFalse(should_use_interactive_plan(quick_args))

    def test_auto_init_includes_real_plan_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                args = build_parser().parse_args(["--plan", "add demo"])

                self.assertTrue(should_auto_init(args))
            finally:
                os.chdir(original_cwd)

    def test_auto_init_skips_plan_creation_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                args = build_parser().parse_args(["--plan", "add demo", "--dry-run"])

                self.assertFalse(should_auto_init(args))
            finally:
                os.chdir(original_cwd)

    def test_plan_execution_auto_initializes_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )
            plan = tmp_path / "docs/plans/20260612-smoke.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                encoding="utf-8",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            str(plan),
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--allow-dirty",
                            "--tasks-only",
                            "--no-move-plan",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue((tmp_path / ".gigaflex/config").exists())
            self.assertTrue((tmp_path / ".gigaflex/prompts/task.txt").is_file())
            progress = (
                tmp_path / ".gigaflex/progress/progress-20260612-smoke.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("plan already has no uncompleted task sections", progress)
            self.assertNotIn("session=task event=prepared", progress)

    def test_init_git_without_plan_only_initializes_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            project = tmp_path / "project"
            project.mkdir()
            original_cwd = Path.cwd()
            stdout = io.StringIO()
            stderr = io.StringIO()

            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(
                    stdout
                ), contextlib.redirect_stderr(stderr):
                    code = main(["--init-git"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue((project / ".git").is_dir())
            self.assertIn("initialized git repository", stdout.getvalue())
            self.assertNotIn("plan file is required", stderr.getvalue())

    def test_plan_execution_outside_git_repo_returns_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            plan = tmp_path / "docs/plans/20260612-smoke.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                encoding="utf-8",
            )

            try:
                os.chdir(tmp_path)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main([str(plan)])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(1, code)
            self.assertIn("not inside a git repository", stderr.getvalue())
            self.assertIn("--init-git", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_auto_init_does_not_make_clean_plan_execution_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            home = tmp_path / "home"
            home.write_text("not a writable directory", encoding="utf-8")
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )
            plan = repo / "docs/plans/20260612-smoke.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                encoding="utf-8",
            )

            code = -1
            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(
                    stdout
                ), contextlib.redirect_stderr(stderr):
                    code = main(
                        [
                            str(plan),
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--tasks-only",
                            "--no-move-plan",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertEqual("", stderr.getvalue())
            self.assertTrue((repo / ".gigaflex/config").exists())

    def test_plan_execution_init_git_commits_initial_state_before_dirty_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )
            plan = tmp_path / "docs/plans/20260612-smoke.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                encoding="utf-8",
            )

            try:
                os.chdir(tmp_path)
                stdout = io.StringIO()
                git_env = {
                    "GIT_AUTHOR_NAME": "GigaFlex Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "GigaFlex Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                }
                with patch.dict(os.environ, git_env), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            str(plan),
                            "--init-git",
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--tasks-only",
                            "--no-move-plan",
                        ]
                    )

                log = subprocess.run(
                    ["git", "log", "--oneline", "--decorate"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertIn("initialized git repository", stdout.getvalue())
            self.assertIn("committed initial repository state", stdout.getvalue())
            self.assertIn("chore: initialize repository", log)

    def test_plan_creation_commits_created_plan_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("# Plan: Demo")
print()
print("## Overview")
print("Demo plan.")
print()
print("### Task 1: Build")
print("- [ ] Do it")
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(["--plan", "add demo feature", "--gigacode-command", str(fake_gigacode)])

                committed = subprocess.run(
                    ["git", "log", "--name-only", "--format=%s", "-1"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertIn("docs: add plan 202", committed)
            self.assertIn(".gigaflex/config", committed)
            self.assertIn(".gitignore", committed)
            self.assertIn("docs/plans/", committed)
            self.assertIn("add-demo-feature.md", committed)
            self.assertTrue((tmp_path / ".gigaflex/config").exists())
            self.assertTrue((tmp_path / ".gigaflex/prompts/task.txt").is_file())

    def test_plan_creation_with_jira_task_uses_corporate_branch_and_commit_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("# Plan: Demo")
print()
print("## Overview")
print("Demo plan.")
print()
print("### Task 1: Build")
print("- [ ] Do it")
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)
                base_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()

                stdout = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "--plan",
                            "add demo feature",
                            "--jira-task",
                            "proj-123",
                            "--gigacode-command",
                            str(fake_gigacode),
                        ]
                    )

                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                subject = subprocess.run(
                    ["git", "log", "--format=%s", "-1"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                stored_commit = subprocess.run(
                    [
                        "git",
                        "config",
                        "--local",
                        "--get",
                        "branch.feature/PROJ-123-add-demo-feature.gigaflexBaseCommit",
                    ],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertEqual("feature/PROJ-123-add-demo-feature", branch)
            self.assertTrue(subject.startswith("PROJ-123 docs: add plan "))
            self.assertEqual(base_commit, stored_commit)
            self.assertIn("created plan:", stdout.getvalue())

    def test_interactive_plan_uses_planning_skill_and_existing_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            capture = tmp_path / "prompt.txt"
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                f"""#!/usr/bin/env python3
from pathlib import Path
import sys
prompt = "\\n".join(sys.argv[1:])
Path({str(capture)!r}).write_text(prompt)
marker = "Create exactly this plan file:\\n"
target = Path(prompt.split(marker, 1)[1].splitlines()[0])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("# Plan: Demo\\n\\n### Task 1: Build\\n- [ ] Do it\\n")
""",
            )
            installed_skill = home / ".gigacode/skills/planning/SKILL.md"
            installed_skill.parent.mkdir(parents=True)
            installed_skill.write_text("---\nname: planning\n---\n", encoding="utf-8")

            try:
                os.chdir(tmp_path)
                stdout = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), patch(
                    "gigaflex.cli.should_use_interactive_plan",
                    return_value=True,
                ), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "--plan",
                            "add demo feature",
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--gigacode-arg=--include-directories=/workspace/shared",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            plans = list((tmp_path / "docs/plans").glob("*.md"))
            prompt = capture.read_text(encoding="utf-8")
            self.assertEqual(0, code)
            self.assertEqual(1, len(plans))
            self.assertIn("--prompt-interactive", prompt)
            self.assertIn("--approval-mode=auto-edit", prompt)
            self.assertIn("--include-directories=/workspace/shared", prompt)
            self.assertIn("installed `planning` skill", prompt)
            self.assertIn("add demo feature", prompt)
            self.assertIn(str(plans[0].relative_to(tmp_path)), prompt)
            self.assertIn(f"created plan: {plans[0].relative_to(tmp_path)}", stdout.getvalue())

    def test_interactive_plan_reports_missing_planning_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                stderr = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), patch(
                    "gigaflex.cli.should_use_interactive_plan",
                    return_value=True,
                ), contextlib.redirect_stderr(stderr):
                    code = main(["--plan", "add demo feature"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(2, code)
            self.assertIn("planning skill not found", stderr.getvalue())
            self.assertIn("--install-planning-skill", stderr.getvalue())
            self.assertIn("--quick", stderr.getvalue())
            self.assertFalse((tmp_path / ".gigaflex").exists())

    def test_install_planning_skill_to_explicit_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            skills_dir = tmp_path / "skills"
            stdout = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(stdout):
                code = main(["--install-planning-skill", "--skill-dir", str(skills_dir)])

            skill = skills_dir / "planning/SKILL.md"
            self.assertEqual(0, code)
            self.assertTrue(skill.is_file())
            self.assertIn("name: planning", skill.read_text(encoding="utf-8"))
            self.assertIn(f"installed planning skill: {skill}", stdout.getvalue())

    def test_install_superpowers_converter_skill_to_explicit_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            skills_dir = tmp_path / "skills"
            stdout = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(stdout):
                code = main([
                    "--install-superpowers-converter-skill",
                    "--skill-dir",
                    str(skills_dir),
                ])

            skill = skills_dir / "superpowers-to-gigaflex/SKILL.md"
            self.assertEqual(0, code)
            self.assertTrue(skill.is_file())
            self.assertIn("name: superpowers-to-gigaflex", skill.read_text(encoding="utf-8"))
            self.assertIn(
                f"installed superpowers-to-gigaflex skill: {skill}",
                stdout.getvalue(),
            )

    def test_install_planning_skill_preserves_modified_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            skills_dir = tmp_path / "skills"
            skill = skills_dir / "planning/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("custom skill\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stderr(stderr):
                code = main(["--install-planning-skill", "--skill-dir", str(skills_dir)])

            self.assertEqual(1, code)
            self.assertEqual("custom skill\n", skill.read_text(encoding="utf-8"))
            self.assertIn("--force-skill-install", stderr.getvalue())

    def test_interactive_plan_accepts_one_new_file_when_skill_changes_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp) / "docs/plans"
            plans_dir.mkdir(parents=True)
            existing = plans_dir / "existing.md"
            existing.write_text("# Existing\n", encoding="utf-8")
            expected = plans_dir / "expected.md"
            actual = plans_dir / "actual.md"
            actual.write_text("# Actual\n", encoding="utf-8")

            found = find_interactively_created_plan(expected, {existing})

            self.assertEqual(actual, found)

    def test_plan_creation_can_initialize_git_repository_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("# Plan: Demo")
print()
print("### Task 1: Build")
print("- [ ] Do it")
""",
            )

            try:
                os.chdir(tmp_path)
                stdout = io.StringIO()
                git_env = {
                    "GIT_AUTHOR_NAME": "GigaFlex Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "GigaFlex Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                }
                with patch.dict(os.environ, git_env), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "--plan",
                            "add demo feature",
                            "--init-git",
                            "--gigacode-command",
                            str(fake_gigacode),
                        ]
                    )

                committed = subprocess.run(
                    ["git", "log", "--name-only", "--format=%s", "-1"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertTrue((tmp_path / ".git").exists())
            self.assertIn("docs: add plan 202", committed)
            self.assertIn("docs/plans/", committed)

    def test_review_requires_explicit_or_stored_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            original_cwd = Path.cwd()

            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "branch", "-m", "master"], check=True)

                stderr = io.StringIO()
                with patch.dict(os.environ, {"HOME": home_tmp}), contextlib.redirect_stderr(stderr):
                    code = main(["--review"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(1, code)
            self.assertIn("no GigaFlex review base is stored for branch master", stderr.getvalue())
            self.assertIn("--base-ref REF", stderr.getvalue())

    def test_plan_resume_on_existing_execution_branch_requires_stored_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            repo = Path(tmp)
            plan = repo / "docs/plans/20260818-demo.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                "# Plan: Demo\n\n### Task 1: Done\n- [x] Already complete\n",
                encoding="utf-8",
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "feature state"], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "branch", "-m", "demo"], check=True)

                stderr = io.StringIO()
                with (
                    patch.dict(os.environ, {"HOME": home_tmp}),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main([str(plan), "--tasks-only"])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(1, code)
            self.assertIn(
                "no GigaFlex review base is stored for branch demo",
                stderr.getvalue(),
            )
            self.assertIn("--base-ref REF", stderr.getvalue())

    def test_review_uses_explicit_base_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            capture = tmp_path / "prompt.txt"
            home = tmp_path / "home"
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                f"""#!/usr/bin/env python3
from pathlib import Path
import sys
prompt = "\\n".join(sys.argv[1:]) + "\\nSTDIN\\n" + sys.stdin.read()
with Path({str(capture)!r}).open("a") as fh:
    fh.write(prompt)
if "specialist review agents have returned" in prompt:
    print("<<<GIGAFLEX:REVIEW_DONE>>>")
elif "Phase: final verification" in prompt:
    print("<<<GIGAFLEX:FINALIZE_DONE>>>")
else:
    print("NO FINDINGS")
""",
            )

            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "branch", "release"], check=True)
                base_commit = subprocess.run(
                    ["git", "rev-parse", "release"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()

                stdout = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "--review",
                            "--base-ref",
                            "release",
                            "--no-parallel-review",
                            "--gigacode-command",
                            str(fake_gigacode),
                        ]
                    )
                stored_commit = subprocess.run(
                    ["git", "config", "--local", "--get", "branch.master.gigaflexBaseCommit"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                stored_branch = subprocess.run(
                    ["git", "config", "--local", "--get", "branch.master.gigaflexBaseBranch"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
            finally:
                os.chdir(original_cwd)

            captured_prompt = capture.read_text(encoding="utf-8")
            self.assertEqual(0, code)
            self.assertIn(f"git diff {base_commit}...HEAD", captured_prompt)
            self.assertIn(f"current branch vs {base_commit}", captured_prompt)
            self.assertEqual(base_commit, stored_commit)
            self.assertEqual("release", stored_branch)

    def test_plan_run_captures_starting_branch_as_immutable_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            capture = tmp_path / "prompt.txt"
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                f"""#!/usr/bin/env python3
from pathlib import Path
import sys
prompt = "\\n".join(sys.argv[1:]) + "\\nSTDIN\\n" + sys.stdin.read()
with Path({str(capture)!r}).open("a") as fh:
    fh.write(prompt)
print("NO FINDINGS")
""",
            )

            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                plan = repo / "docs/plans/20260818-demo.md"
                plan.parent.mkdir(parents=True)
                plan.write_text(
                    "# Plan: Demo\n\n### Task 1: Done\n- [x] Already complete\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "release state"], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "branch", "-m", "release/2.0"], check=True)
                base_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()

                with patch.dict(os.environ, {"HOME": home_tmp}), contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            str(plan),
                            "--branch",
                            "feature/demo",
                            "--no-parallel-review",
                            "--no-finalize",
                            "--no-move-plan",
                            "--gigacode-command",
                            str(fake_gigacode),
                        ]
                    )
                current_branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                stored_commit = subprocess.run(
                    ["git", "config", "--local", "--get", "branch.feature/demo.gigaflexBaseCommit"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                stored_branch = subprocess.run(
                    ["git", "config", "--local", "--get", "branch.feature/demo.gigaflexBaseBranch"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
            finally:
                os.chdir(original_cwd)

            captured_prompt = capture.read_text(encoding="utf-8")
            self.assertEqual(0, code)
            self.assertEqual("feature/demo", current_branch)
            self.assertEqual(base_commit, stored_commit)
            self.assertEqual("release/2.0", stored_branch)
            self.assertIn(f"git diff {base_commit}...HEAD", captured_prompt)

    def test_review_restores_stored_base_for_execution_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            capture = tmp_path / "prompt.txt"
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                f"""#!/usr/bin/env python3
from pathlib import Path
import sys
prompt = "\\n".join(sys.argv[1:]) + "\\nSTDIN\\n" + sys.stdin.read()
with Path({str(capture)!r}).open("a") as fh:
    fh.write(prompt)
print("NO FINDINGS")
""",
            )

            try:
                os.chdir(repo)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("base\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "base"], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "branch", "-m", "release"], check=True)
                base_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                subprocess.run(["git", "switch", "-c", "feature/demo"], check=True, stdout=subprocess.PIPE)
                Path("README.md").write_text("feature\n", encoding="utf-8")
                subprocess.run(["git", "commit", "-am", "feature"], check=True, stdout=subprocess.PIPE)
                subprocess.run(
                    ["git", "config", "--local", "branch.feature/demo.gigaflexBaseBranch", "release"],
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "--local", "branch.feature/demo.gigaflexBaseCommit", base_commit],
                    check=True,
                )

                with patch.dict(os.environ, {"HOME": home_tmp}), contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "--review",
                            "--no-parallel-review",
                            "--no-finalize",
                            "--gigacode-command",
                            str(fake_gigacode),
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertIn(
                f"git diff {base_commit}...HEAD",
                capture.read_text(encoding="utf-8"),
            )

    def test_successful_plan_run_commits_completed_plan_move_and_ignores_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
prompt = " ".join(sys.argv[1:]) + sys.stdin.read()
if "specialist review agents" in prompt:
    print("<<<GIGAFLEX:REVIEW_DONE>>>")
elif "Phase: final verification" in prompt:
    print("<<<GIGAFLEX:FINALIZE_DONE>>>")
elif "You are the" in prompt:
    print("NO FINDINGS")
else:
    print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(["--init"]))

                plan = tmp_path / "docs/plans/20260612-smoke.md"
                plan.parent.mkdir(parents=True)
                plan.write_text(
                    """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main([str(plan), "--gigacode-command", str(fake_gigacode), "--no-branch"])

                latest = subprocess.run(
                    ["git", "log", "--name-only", "--format=%s", "-1"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
                status = subprocess.run(
                    ["git", "status", "--short"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout
            finally:
                os.chdir(original_cwd)

            self.assertEqual(0, code)
            self.assertIn("docs: complete plan 20260612-smoke", latest)
            self.assertIn("docs/plans/completed/20260612-smoke.md", latest)
            self.assertEqual("", status)

    def test_plan_run_writes_token_and_timing_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            home = Path(home_tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys

prompt = "\\n".join(sys.argv[1:]) + sys.stdin.read()
marker = "Phase: implement exactly one task section from "
plan_line = next(line for line in prompt.splitlines() if line.startswith(marker))
plan = Path(plan_line[len(marker):].rstrip("."))
plan.write_text(plan.read_text().replace("- [ ]", "- [x]"))
subprocess.run(["git", "add", str(plan)], check=True)
subprocess.run(["git", "commit", "-m", "feat: complete task"], check=True)

print(json.dumps({
    "type": "assistant",
    "session_id": "session-1",
    "message": {
        "model": "vllm/Test",
        "content": [{"type": "text", "text": "implemented"}]
    }
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "session-1",
    "duration_ms": 1250,
    "duration_api_ms": 900,
    "result": "implemented",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_input_tokens": 20,
        "total_tokens": 105
    },
    "stats": {"models": {"vllm/Test": {}}}
}))
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                plan = tmp_path / "docs/plans/20260612-smoke.md"
                plan.parent.mkdir(parents=True)
                plan.write_text(
                    """# Plan: Smoke

### Task 1: Implement
- [ ] Complete the task
""",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            str(plan),
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--tasks-only",
                            "--no-branch",
                            "--no-move-plan",
                            "--allow-dirty",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            stats_file = (tmp_path / ".gigaflex/progress/stats-20260612-smoke.json").resolve()
            dashboard_file = (tmp_path / ".gigaflex/progress/status-20260612-smoke.html").resolve()
            dashboard_json = (tmp_path / ".gigaflex/progress/status-20260612-smoke.json").resolve()
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            dashboard_state = json.loads(dashboard_json.read_text(encoding="utf-8"))
            self.assertEqual(0, code)
            self.assertEqual(1, stats["call_count"])
            self.assertEqual(105, stats["usage"]["total_tokens"])
            self.assertEqual("task", stats["invocations"][0]["session"])
            self.assertEqual(1250, stats["invocations"][0]["reported_duration_ms"])
            self.assertEqual("success", stats["status"])
            self.assertIn("status: success", stdout.getvalue())
            self.assertIn("tokens: input=100 output=5", stdout.getvalue())
            self.assertIn(f"statistics: {stats_file}", stdout.getvalue())
            self.assertIn(f"dashboard: {dashboard_file}", stdout.getvalue())
            self.assertNotIn("implemented", stdout.getvalue())
            self.assertEqual("success", dashboard_state["status"])
            self.assertEqual(105, dashboard_state["usage"]["total_tokens"])
            self.assertTrue(dashboard_file.is_file())

    def test_interrupted_run_writes_statistics_file_with_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            home = Path(home_tmp)
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.dict(os.environ, {"HOME": str(home)}),
                    patch("gigaflex.cli.Runner.run", side_effect=KeyboardInterrupt),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main(
                        [
                            "--review",
                            "--base-ref",
                            "HEAD",
                            "--gigacode-command",
                            "unused-gigacode",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            stats_file = (tmp_path / ".gigaflex/progress/stats-review.json").resolve()
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            self.assertEqual(130, code)
            self.assertEqual("interrupted", stats["status"])
            self.assertEqual(0, stats["call_count"])
            self.assertIn("status: interrupted", stdout.getvalue())
            self.assertIn(f"statistics: {stats_file}", stdout.getvalue())
            self.assertIn("interrupted", stderr.getvalue())

    def test_failed_run_writes_reason_to_progress_and_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            tmp_path = Path(tmp)
            home = Path(home_tmp)
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)
                Path("README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                with (
                    patch.dict(os.environ, {"HOME": str(home)}),
                    patch("gigaflex.cli.Runner.run", side_effect=RuntimeError("review crashed")),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    code = main(
                        [
                            "--review",
                            "--base-ref",
                            "HEAD",
                            "--gigacode-command",
                            "unused-gigacode",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            progress = tmp_path / ".gigaflex/progress/progress-review.txt"
            stats_file = tmp_path / ".gigaflex/progress/stats-review.json"
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            progress_text = progress.read_text(encoding="utf-8")
            self.assertEqual(1, code)
            self.assertEqual("failed", stats["status"])
            self.assertEqual("review crashed", stats["failure_reason"])
            self.assertEqual("startup", stats["failure_phase"])
            self.assertIn("=== failure", progress_text)
            self.assertIn("error: RuntimeError: review crashed", progress_text)
            self.assertIn("session=runner event=failed", progress_text)

    def test_plan_run_can_use_isolated_worktree_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(["--init"]))

                plan = tmp_path / "docs/plans/20260612-smoke.md"
                plan.parent.mkdir(parents=True)
                plan.write_text(
                    """# Plan: Smoke

### Task 1: Already done
- [x] Nothing left to do
""",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            str(plan),
                            "--worktree",
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--tasks-only",
                            "--no-move-plan",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            worktree = tmp_path / ".gigaflex/worktrees/smoke"
            branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            main_branch = subprocess.run(
                ["git", "-C", str(tmp_path), "branch", "--show-current"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            self.assertEqual(0, code)
            self.assertEqual("smoke", branch)
            self.assertNotEqual("smoke", main_branch)
            self.assertTrue((worktree / "docs/plans/20260612-smoke.md").exists())
            self.assertIn("progress log:", stdout.getvalue())

    def test_openspec_run_remaps_change_context_into_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_cwd = Path.cwd()
            fake_gigacode = write_script(
                tmp_path / "fake_gigacode.py",
                """#!/usr/bin/env python3
print("<<<GIGAFLEX:ALL_TASKS_DONE>>>")
""",
            )

            try:
                os.chdir(tmp_path)
                subprocess.run(["git", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "GigaFlex Test"], check=True)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(["--init"]))

                change = tmp_path / "openspec/changes/add-search"
                change.mkdir(parents=True)
                (change / "tasks.md").write_text(
                    "## 1. Build search\n- [x] 1.1 Implement search\n",
                    encoding="utf-8",
                )
                (change / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "--openspec",
                            str(change),
                            "--worktree",
                            "--gigacode-command",
                            str(fake_gigacode),
                            "--tasks-only",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            worktree = tmp_path / ".gigaflex/worktrees/add-search"
            branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            self.assertEqual(0, code)
            self.assertEqual("add-search", branch)
            self.assertTrue((worktree / "openspec/changes/add-search/tasks.md").is_file())
            self.assertTrue((worktree / "openspec/changes/add-search/proposal.md").is_file())
            self.assertIn("OpenSpec change complete: add-search", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
