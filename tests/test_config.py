from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.config import Config, init_global_config, init_global_prompt_templates, load_config
from gigaflex.executor import GigaCodeExecutor
from gigaflex.prompts import DEFAULT_PROMPTS


class ConfigTest(unittest.TestCase):
    def test_default_args_enable_noninteractive_auto_edit(self) -> None:
        self.assertEqual(
            [
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            Config().resolved_args,
        )

    def test_default_interactive_args_pass_initial_prompt_and_allow_plan_writes(self) -> None:
        self.assertEqual(
            [
                "--prompt-interactive",
                "{prompt}",
                "--approval-mode=auto-edit",
            ],
            Config().resolved_interactive_args,
        )

    def test_default_gigacode_skills_dir_uses_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(home / ".gigacode/skills", Config().gigacode_skills_dir)

    def test_created_plans_are_committed_by_default(self) -> None:
        self.assertTrue(Config().commit_plan_on_creation)

    def test_finalize_is_enabled_by_default(self) -> None:
        self.assertTrue(Config().finalize_enabled)

    def test_worktree_is_disabled_by_default(self) -> None:
        self.assertFalse(Config().worktree)

    def test_default_branch_is_auto_detected_by_default(self) -> None:
        self.assertEqual("", Config().default_branch)

    def test_idle_timeout_defaults_to_fifteen_minutes(self) -> None:
        self.assertEqual(900, Config().idle_timeout)

    def test_session_timeout_defaults_to_thirty_minutes(self) -> None:
        self.assertEqual(1800, Config().session_timeout)

    def test_retry_count_defaults_to_one_retry(self) -> None:
        self.assertEqual(1, Config().retry_count)

    def test_retry_delay_defaults_to_five_seconds(self) -> None:
        self.assertEqual(5.0, Config().retry_delay)

    def test_retry_patterns_include_transient_http_errors(self) -> None:
        self.assertIn("API Error: 503", Config().retry_patterns)

    def test_rate_limit_patterns_include_429(self) -> None:
        self.assertIn("429 Too Many Requests", Config().rate_limit_patterns)

    def test_load_config_reads_retry_pattern_lists_and_rate_limit_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text(
                """[gigaflex]
retry_patterns = temporary one, temporary two
rate_limit_patterns = limit one, limit two
wait_on_rate_limit = 12.5
""",
                encoding="utf-8",
            )

            cfg = load_config(config)

            self.assertEqual(["temporary one", "temporary two"], cfg.retry_patterns)
            self.assertEqual(["limit one", "limit two"], cfg.rate_limit_patterns)
            self.assertEqual(12.5, cfg.wait_on_rate_limit)

    def test_loaded_custom_args_cannot_drop_noninteractive_shell_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text(
                "[gigaflex]\ngigacode_args = --debug\n",
                encoding="utf-8",
            )

            args = load_config(config).resolved_args
            self.assertEqual(
                [
                    "--debug",
                    "--approval-mode=auto-edit",
                    "--allowed-tools",
                    "run_shell_command",
                ],
                args,
            )
            self.assertIn("-p '<prompt>'", GigaCodeExecutor(args=args).command_line())

    def test_environment_custom_args_cannot_drop_noninteractive_shell_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "GIGAFLEX_GIGACODE_ARGS": "-p {prompt}",
                },
                clear=True,
            ):
                cfg = load_config()

            self.assertEqual(
                [
                    "--approval-mode=auto-edit",
                    "--allowed-tools",
                    "run_shell_command",
                    "-p",
                    "{prompt}",
                ],
                cfg.resolved_args,
            )

    def test_load_config_reads_interactive_gigacode_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text(
                "[gigaflex]\ngigacode_interactive_args = -i {prompt}\n",
                encoding="utf-8",
            )

            self.assertEqual(
                ["-i", "{prompt}"],
                load_config(config).resolved_interactive_args,
            )

    def test_load_config_reads_gigacode_skills_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config"
            skills_dir = tmp_path / "custom-skills"
            config.write_text(
                f"[gigaflex]\ngigacode_skills_dir = {skills_dir}\n",
                encoding="utf-8",
            )

            self.assertEqual(skills_dir, load_config(config).gigacode_skills_dir)

    def test_config_can_disable_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text("[gigaflex]\nfinalize_enabled = false\n", encoding="utf-8")

            self.assertFalse(load_config(config).finalize_enabled)

    def test_local_config_overrides_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            project = tmp_path / "project"
            global_dir = home / ".config/gigaflex"
            local_dir = project / ".gigaflex"
            global_dir.mkdir(parents=True)
            local_dir.mkdir(parents=True)
            (global_dir / "config").write_text(
                "[gigaflex]\ntask_model = global-task\nreview_workers = 2\n",
                encoding="utf-8",
            )
            (local_dir / "config").write_text(
                "[gigaflex]\ntask_model = local-task\n",
                encoding="utf-8",
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}):
                    cfg = load_config()
            finally:
                os.chdir(original_cwd)

            self.assertEqual("local-task", cfg.task_model)
            self.assertEqual(2, cfg.review_workers)

    def test_explicit_config_overrides_local_and_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            project = tmp_path / "project"
            explicit = tmp_path / "explicit-config"
            global_dir = home / ".config/gigaflex"
            local_dir = project / ".gigaflex"
            global_dir.mkdir(parents=True)
            local_dir.mkdir(parents=True)
            (global_dir / "config").write_text(
                "[gigaflex]\ntask_model = global-task\n",
                encoding="utf-8",
            )
            (local_dir / "config").write_text(
                "[gigaflex]\ntask_model = local-task\n",
                encoding="utf-8",
            )
            explicit.write_text(
                "[gigaflex]\ntask_model = explicit-task\n",
                encoding="utf-8",
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(project)
                with patch.dict(os.environ, {"HOME": str(home)}):
                    cfg = load_config(explicit)
            finally:
                os.chdir(original_cwd)

            self.assertEqual("explicit-task", cfg.task_model)

    def test_init_global_config_creates_commented_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"HOME": str(home)}):
                written = init_global_config()

            config = home / ".config/gigaflex/config"
            self.assertEqual([config], written)
            self.assertTrue(config.is_file())
            text = config.read_text(encoding="utf-8")
            self.assertIn("[gigaflex]", text)
            self.assertIn("# task_model =", text)
            self.assertIn("# review_workers = 5", text)
            self.assertIn("# finalize_enabled = true", text)

    def test_init_global_config_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = home / ".config/gigaflex/config"
            config.parent.mkdir(parents=True)
            config.write_text("[gigaflex]\ntask_model = keep-me\n", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": str(home)}):
                written = init_global_config()

            self.assertEqual([], written)
            self.assertEqual(
                "[gigaflex]\ntask_model = keep-me\n",
                config.read_text(encoding="utf-8"),
            )

    def test_init_global_prompts_creates_templates_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            prompt_dir = home / ".config/gigaflex/prompts"
            prompt_dir.mkdir(parents=True)
            existing = prompt_dir / "task.txt"
            existing.write_text("keep global task", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": str(home)}):
                written = init_global_prompt_templates()

            self.assertEqual("keep global task", existing.read_text(encoding="utf-8"))
            self.assertTrue((prompt_dir / "make_plan.txt").is_file())
            self.assertTrue((prompt_dir / "plan_skill.txt").is_file())
            self.assertTrue((prompt_dir / "review_agent.txt").is_file())
            self.assertNotIn(existing, written)

    def test_init_global_prompts_updates_an_unchanged_previous_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            prompt_dir = home / ".config/gigaflex/prompts"
            prompt_dir.mkdir(parents=True)
            finalize = prompt_dir / "finalize.txt"
            finalize.write_text(
                """Finalize the branch for {goal}.

Check git status, run the validation commands from the plan if available, and leave the branch in a clean state.
Do not rewrite history unless the plan explicitly asks for it.

Progress log: {progress_file}
Plain text output only.
""",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"HOME": str(home)}):
                written = init_global_prompt_templates()

            self.assertEqual(DEFAULT_PROMPTS.finalize, finalize.read_text(encoding="utf-8"))
            self.assertIn(finalize, written)
            self.assertTrue((prompt_dir / ".defaults.json").is_file())

    def test_init_global_prompts_updates_legacy_review_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            prompt_dir = home / ".config/gigaflex/prompts"
            prompt_dir.mkdir(parents=True)
            review = prompt_dir / "review.txt"
            review.write_text(
                """You are the review agent.

Review {goal}.

Run:
- git log {base_ref}..HEAD --oneline
- git diff {base_ref}...HEAD --stat
- git diff {base_ref}...HEAD

Read changed files in full context.
Report confirmed issues only: bugs, broken requirements, missing tests, regressions, security problems, and unnecessary complexity.
Do not modify files, run mutating commands, or make commits.

Output format:
- file:line - severity - issue - why it matters - suggested fix

If there are no findings, output exactly:
NO FINDINGS

Progress log: {progress_file}
Plain text output only.
""",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"HOME": str(home)}):
                written = init_global_prompt_templates()

            self.assertEqual(DEFAULT_PROMPTS.review, review.read_text(encoding="utf-8"))
            self.assertIn(review, written)

    def test_resolved_default_args_are_copied(self) -> None:
        first = Config().resolved_args
        first.append("--include-directories")

        self.assertEqual(
            [
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            Config().resolved_args,
        )

    def test_phase_args_add_model_flag_before_prompt_args(self) -> None:
        cfg = Config(task_model="fast-task", review_model="strong-review")

        self.assertEqual(
            [
                "--model",
                "strong-review",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.args_for_phase("review"),
        )

    def test_custom_args_cannot_drop_noninteractive_shell_access(self) -> None:
        cfg = Config(gigacode_args=["-p", "{prompt}", "--debug"])

        self.assertEqual(
            [
                "--debug",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.resolved_args,
        )

    def test_custom_args_replace_incompatible_approval_mode_and_keep_other_tools(self) -> None:
        cfg = Config(
            gigacode_args=[
                "-p",
                "{prompt}",
                "--approval-mode",
                "default",
                "--allowed-tools",
                "read_file",
            ]
        )

        self.assertEqual(
            [
                "--approval-mode",
                "auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "read_file",
                "-p",
                "{prompt}",
            ],
            cfg.resolved_args,
        )

    def test_equals_allowed_tools_form_is_normalized_for_shell_access(self) -> None:
        cfg = Config(gigacode_args=["-p", "{prompt}", "--allowed-tools=read_file"])

        self.assertEqual(
            [
                "--allowed-tools",
                "run_shell_command",
                "read_file",
                "--approval-mode=auto-edit",
                "-p",
                "{prompt}",
            ],
            cfg.resolved_args,
        )

    def test_equals_allowed_tools_shell_form_is_converted_to_supported_syntax(self) -> None:
        cfg = Config(
            gigacode_args=[
                "-p",
                "{prompt}",
                "--approval-mode=auto-edit",
                "--allowed-tools=run_shell_command",
            ]
        )

        self.assertEqual(
            [
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.resolved_args,
        )

    def test_embedded_prompt_form_is_moved_after_array_options(self) -> None:
        cfg = Config(
            gigacode_args=[
                "--prompt={prompt}",
                "--allowed-tools",
                "read_file",
            ]
        )

        self.assertEqual(
            [
                "--allowed-tools",
                "run_shell_command",
                "read_file",
                "--approval-mode=auto-edit",
                "-p",
                "{prompt}",
            ],
            cfg.resolved_args,
        )

    def test_all_noninteractive_phases_keep_shell_access_after_override(self) -> None:
        cfg = Config(
            gigacode_args=["--debug"],
            plan_model="plan",
            task_model="task",
            review_model="review",
            finalize_model="finalize",
        )

        phase_args = [
            cfg.args_for_phase("plan"),
            cfg.args_for_phase("task"),
            cfg.args_for_review_agent(),
            cfg.args_for_phase("synthesis"),
            cfg.args_for_phase("finalize"),
        ]
        for args in phase_args:
            self.assertIn("--approval-mode=auto-edit", args)
            allowed_tools = args.index("--allowed-tools")
            self.assertEqual("run_shell_command", args[allowed_tools + 1])
            self.assertIn("-p '<prompt>'", GigaCodeExecutor(args=args).command_line())

    def test_interactive_plan_args_include_plan_model(self) -> None:
        cfg = Config(plan_model="planning-model")

        self.assertEqual(
            [
                "--model",
                "planning-model",
                "--prompt-interactive",
                "{prompt}",
                "--approval-mode=auto-edit",
            ],
            cfg.args_for_interactive_plan(),
        )

    def test_review_model_falls_back_to_task_model(self) -> None:
        cfg = Config(task_model="shared-model")

        self.assertEqual(
            [
                "--model",
                "shared-model",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.args_for_phase("review"),
        )

    def test_synthesis_uses_task_model_instead_of_review_model(self) -> None:
        cfg = Config(task_model="code-model", review_model="review-model")

        self.assertEqual(
            [
                "--model",
                "code-model",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.args_for_phase("synthesis"),
        )

    def test_review_agent_args_keep_shell_approval_flags(self) -> None:
        cfg = Config(review_model="review-model")

        self.assertEqual(
            [
                "--model",
                "review-model",
                "--approval-mode=auto-edit",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.args_for_review_agent(),
        )

    def test_review_agent_args_preserve_custom_split_approval_mode(self) -> None:
        cfg = Config(
            gigacode_args=[
                "--approval-mode",
                "auto-edit",
                "--debug",
                "{prompt}",
            ]
        )

        self.assertEqual(
            [
                "--approval-mode",
                "auto-edit",
                "--debug",
                "--allowed-tools",
                "run_shell_command",
                "-p",
                "{prompt}",
            ],
            cfg.args_for_review_agent(),
        )


if __name__ == "__main__":
    unittest.main()
