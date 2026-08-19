from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Callable, Optional

from .dashboard import ProgressDashboard
from .executor import ExecResult, GigaCodeExecutor
from .git import GitError, GitService, ReviewWorktreeManager
from .plan import Plan, Task, file_has_uncompleted_checkbox, parse_plan, parse_plan_file
from .progress import ProgressLog
from .prompts import (
    DEFAULT_PROMPTS,
    REVIEW_AGENTS,
    PromptContext,
    PromptTemplates,
    render,
    render_review_agent_prompt,
    render_review_format_retry_prompt,
    render_review_prompt,
    render_review_synthesis_blocked_audit_prompt,
    render_review_synthesis_prompt,
    render_review_synthesis_recovery_prompt,
    render_task_completion_retry_prompt,
    render_task_prompt,
)
from .review import (
    ReviewOutputError,
    identify_review_findings,
    normalize_review_output,
    parse_synthesis_output,
    recover_review_output,
    recover_synthesis_output,
)
from .signals import (
    ALL_TASKS_DONE,
    FINALIZE_DONE,
    FINALIZE_FAILED,
    REVIEW_DONE,
    TASK_FAILED,
)
from .stats import statistics_path


@dataclass
class RunOptions:
    plan_file: Optional[Path]
    progress_file: Path
    default_branch: str = "main"
    max_iterations: int = 50
    review_iterations: int = 5
    tasks_only: bool = False
    review_only: bool = False
    finalize_enabled: bool = True
    dry_run: bool = False
    parallel_review: bool = True
    delay_seconds: float = 1.0
    prompts: PromptTemplates = field(default_factory=lambda: DEFAULT_PROMPTS)
    jira_task: str = ""
    plan_kind: str = "gigaflex"
    plan_source: Optional[Path] = None
    plan_context_files: tuple[Path, ...] = ()
    task_completion_retries: int = 1
    allow_dirty: bool = False


class Runner:
    def __init__(
        self,
        options: RunOptions,
        executor: GigaCodeExecutor,
        log: ProgressLog,
        synthesis_executor: Optional[GigaCodeExecutor] = None,
        review_agent_executor: Optional[GigaCodeExecutor] = None,
        finalize_executor: Optional[GigaCodeExecutor] = None,
        dashboard: Optional[ProgressDashboard] = None,
        review_worktrees: Optional[ReviewWorktreeManager] = None,
    ) -> None:
        self.options = options
        self.executor = executor
        self.synthesis_executor = synthesis_executor or executor
        self.review_agent_executor = review_agent_executor or self.synthesis_executor
        self.finalize_executor = finalize_executor or self.synthesis_executor
        self.log = log
        self.dashboard = dashboard
        self.review_worktrees = review_worktrees

    def run(self) -> None:
        if self.options.dry_run:
            self.print_prompts()
            return
        if not self.options.review_only:
            self.run_tasks()
        if self.options.tasks_only:
            self.log.section("done")
            self.log.write("task execution completed\n")
            return
        self.run_review()
        if self.options.finalize_enabled:
            self.run_finalize()

    def run_tasks(self) -> None:
        if self.dashboard is not None:
            self.dashboard.phase_started("tasks", "Executing plan tasks")
        if self.options.plan_file is None:
            raise ValueError("plan file is required for task execution")
        self._validate_plan_has_tasks()
        context = self._context()
        if not self._has_uncompleted_work():
            self.log.section("tasks")
            self.log.write("plan already has no uncompleted task sections\n")
            return

        for iteration in range(1, self.options.max_iterations + 1):
            selected_task = self._parse_plan_file().first_uncompleted_task()
            if selected_task is None:
                return
            plan_before = self.options.plan_file.read_text(encoding="utf-8")
            context_before = self._plan_context_snapshot()
            head_before = self._git().head_commit()
            dirty_before = self._uncommitted_paths()
            prompt = render_task_prompt(
                self.options.prompts.task,
                context,
                selected_task.number,
                selected_task.title,
                selected_task.section,
                selected_task.has_implicit_tracking,
            )
            task_label = self._task_label(selected_task)
            if self.dashboard is not None:
                self.dashboard.task_started(
                    selected_task.number,
                    selected_task.title,
                    iteration,
                )
            self.log.section(f"task iteration {iteration}: {task_label}")
            result = self.executor.run(
                prompt,
                retry_guard=(
                    lambda _result: self._prepare_task_retry(
                        selected_task,
                        plan_before,
                        context_before,
                        head_before,
                        dirty_before,
                    )
                ),
            )
            self._prefix_new_commits(
                head_before,
                f"task {task_label}",
            )
            self._accept_task_result_or_raise(
                result,
                selected_task,
                plan_before,
                context_before,
                head_before,
                dirty_before,
            )
            completion_retries = 0
            while (
                completion_retries < max(0, self.options.task_completion_retries)
                and self._can_retry_incomplete_task(
                    selected_task,
                    plan_before,
                    context_before,
                )
            ):
                completion_retries += 1
                current_task = self._matching_task(self._parse_plan_file(), selected_task)
                assert current_task is not None
                self.log.section(
                    f"task completion retry {completion_retries}: {task_label}"
                )
                self.log.diagnostic(
                    "session=task event=completion_retry_scheduled "
                    f"task={task_label!r} attempt={completion_retries} "
                    f"attempts={max(0, self.options.task_completion_retries)}"
                )
                retry_plan_before = self.options.plan_file.read_text(encoding="utf-8")
                retry_context_before = self._plan_context_snapshot()
                retry_head_before = self._git().head_commit()
                retry_dirty_before = self._uncommitted_paths()
                retry_prompt = render_task_completion_retry_prompt(
                    prompt,
                    self.options.plan_file,
                    selected_task.number,
                    selected_task.title,
                    current_task.section,
                    selected_task.has_implicit_tracking,
                )
                result = self.executor.run(
                    retry_prompt,
                    retry_guard=(
                        lambda _result: self._prepare_task_retry(
                            selected_task,
                            retry_plan_before,
                            retry_context_before,
                            retry_head_before,
                            retry_dirty_before,
                        )
                    ),
                )
                self._prefix_new_commits(
                    retry_head_before,
                    f"task completion retry {completion_retries}: {task_label}",
                )
                self._accept_task_result_or_raise(
                    result,
                    selected_task,
                    plan_before,
                    context_before,
                    head_before,
                    dirty_before,
                )
            self._validate_completed_task_iteration(
                selected_task,
                plan_before,
                context_before,
                head_before,
                dirty_before,
                completion_retries,
            )
            if self.dashboard is not None:
                self.dashboard.task_finished()
            if result.signal == ALL_TASKS_DONE and not self._has_uncompleted_work():
                return
            if not self._has_uncompleted_work():
                return
            time.sleep(self.options.delay_seconds)
        raise RuntimeError(f"max task iterations reached: {self.options.max_iterations}")

    def run_review(self) -> None:
        if self.dashboard is not None:
            self.dashboard.phase_started("review", "Reviewing the completed changes")
        if self.options.parallel_review:
            self.run_parallel_review()
            return

        context = self._context()
        for iteration in range(1, self.options.review_iterations + 1):
            if self.dashboard is not None:
                self.dashboard.review_attempt_started(
                    iteration,
                    self.options.review_iterations,
                    parallel=False,
                )
            self.log.section(f"review iteration {iteration}")
            head_before = self._git().head_commit()
            result = self._run_single_review_agent(
                "review",
                lambda review_context: render_review_prompt(
                    self.options.prompts.review,
                    review_context,
                ),
            )
            self._prefix_new_commits(head_before, "review")
            if not result.ok:
                raise RuntimeError(describe_failure("gigacode review session", result))
            if result.signal == TASK_FAILED:
                raise RuntimeError("review failed")
            structured_output = self._structured_review_output(
                "review",
                result,
            )
            identified = identify_review_findings({"review": structured_output})
            if not identified:
                self.log.diagnostic(
                    "session=review event=no_findings action=skip_synthesis"
                )
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=0,
                        message=f"Review passed on attempt {iteration}: no findings",
                    )
                return

            self.log.section("review synthesis")
            if self.dashboard is not None:
                self.dashboard.review_synthesis_started(iteration, len(identified))
            head_before = self._git().head_commit()
            synthesis = self.synthesis_executor.run(
                self._render_review_synthesis_prompt({"review": structured_output}, context)
            )
            self._prefix_new_commits(head_before, "review synthesis")
            if not synthesis.ok:
                raise RuntimeError(describe_failure("gigacode review synthesis", synthesis))
            if synthesis.signal == TASK_FAILED:
                raise RuntimeError("review failed")
            if self._accept_review_synthesis_or_raise(synthesis, {"review": structured_output}):
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=len(identified),
                        message=f"Review passed on attempt {iteration}",
                    )
                return
            if self.dashboard is not None:
                self.dashboard.review_attempt_finished(
                    iteration,
                    "needs_another_pass",
                    findings=len(identified),
                    message=f"Review attempt {iteration} requires another pass",
                )
            time.sleep(self.options.delay_seconds)
        raise RuntimeError(f"max review iterations reached: {self.options.review_iterations}")

    def run_parallel_review(self) -> None:
        context = self._context()
        for iteration in range(1, self.options.review_iterations + 1):
            if self.dashboard is not None:
                self.dashboard.review_attempt_started(
                    iteration,
                    self.options.review_iterations,
                    parallel=True,
                )
            self.log.section(f"parallel review iteration {iteration}")
            head_before = self._git().head_commit()
            results = self._run_parallel_review_agents()
            self._prefix_new_commits(head_before, "parallel review")
            findings: dict[str, str] = {}
            for name in REVIEW_AGENTS:
                result = results[name]
                self.log.section(f"review agent: {name}")
                self.log.write(result.output)
                if result.error_output:
                    self.log.write(result.error_output)
                if not result.ok:
                    raise RuntimeError(describe_failure(f"gigacode review agent {name}", result))
                findings[name] = self._structured_review_output(name, result)

            identified = identify_review_findings(findings)
            if not identified:
                self.log.diagnostic(
                    "session=review event=no_findings action=skip_synthesis"
                )
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=0,
                        message=f"Review passed on attempt {iteration}: no findings",
                    )
                return

            self.log.section("review synthesis")
            if self.dashboard is not None:
                self.dashboard.review_synthesis_started(iteration, len(identified))
            head_before = self._git().head_commit()
            synthesis = self.synthesis_executor.run(
                self._render_review_synthesis_prompt(findings, context)
            )
            self._prefix_new_commits(head_before, "review synthesis")
            if not synthesis.ok:
                raise RuntimeError(describe_failure("gigacode review synthesis", synthesis))
            if synthesis.signal == TASK_FAILED:
                raise RuntimeError("review failed")
            if self._accept_review_synthesis_or_raise(synthesis, findings):
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=len(identified),
                        message=f"Review passed on attempt {iteration}",
                    )
                return
            if self.dashboard is not None:
                self.dashboard.review_attempt_finished(
                    iteration,
                    "needs_another_pass",
                    findings=len(identified),
                    message=f"Review attempt {iteration} requires another pass",
                )
            time.sleep(self.options.delay_seconds)
        raise RuntimeError(f"max review iterations reached: {self.options.review_iterations}")

    def run_finalize(self) -> None:
        if self.dashboard is not None:
            self.dashboard.phase_started("finalize", "Running final verification")
        self.log.section("finalize")
        head_before = self._git().head_commit()
        dirty_before = self._uncommitted_paths()
        result = self.finalize_executor.run(render(self.options.prompts.finalize, self._context()))
        self._prefix_new_commits(head_before, "finalize")
        if not result.ok:
            raise RuntimeError(describe_failure("gigacode finalize session", result))
        if result.signal == FINALIZE_FAILED:
            raise RuntimeError("finalize failed")
        if result.signal != FINALIZE_DONE:
            raise RuntimeError("finalize did not report successful verification")
        new_dirty = self._uncommitted_paths() - dirty_before
        if new_dirty:
            if self.options.allow_dirty:
                self._log_allowed_dirty("finalize", new_dirty)
            else:
                raise RuntimeError("finalize left new uncommitted changes in the working tree")

    def print_prompts(self) -> None:
        context = self._context()
        if not self.options.review_only:
            self.log.section("task prompt")
            selected_task = (
                self._parse_plan_file().first_uncompleted_task()
                if self.options.plan_file is not None
                else None
            )
            if selected_task is None:
                self.log.stream("plan has no uncompleted task sections\n")
            else:
                self.log.stream(
                    render_task_prompt(
                        self.options.prompts.task,
                        context,
                        selected_task.number,
                        selected_task.title,
                        selected_task.section,
                        selected_task.has_implicit_tracking,
                    )
                )
                self.log.stream("\n")
        if not self.options.tasks_only:
            self.log.section("review prompt")
            if self.options.parallel_review:
                for name, focus in REVIEW_AGENTS.items():
                    self.log.stream(f"\n--- review agent: {name} ---\n")
                    self.log.stream(
                        render_review_agent_prompt(self.options.prompts.review_agent, name, focus, context)
                    )
                self.log.stream("\n--- review synthesis prompt uses collected agent findings ---\n")
            else:
                self.log.stream(render_review_prompt(self.options.prompts.review, context))
                self.log.stream("\n--- review synthesis prompt uses reviewer findings ---\n")
        if self.options.finalize_enabled:
            self.log.section("finalize prompt")
            self.log.stream(render(self.options.prompts.finalize, context))
            self.log.stream("\n")

    def _context(self) -> PromptContext:
        return PromptContext(
            plan_file=self.options.plan_file,
            progress_file=self.options.progress_file,
            default_branch=self.options.default_branch,
            jira_task=self.options.jira_task,
            plan_kind=self.options.plan_kind,
            plan_source=self.options.plan_source,
            plan_context_files=self.options.plan_context_files,
        )

    def _parse_plan_file(self) -> Plan:
        assert self.options.plan_file is not None
        return parse_plan_file(
            self.options.plan_file,
            plan_format=self.options.plan_kind,
        )

    def _validate_plan_has_tasks(self) -> None:
        assert self.options.plan_file is not None
        plan = self._parse_plan_file()
        if not plan.tasks:
            raise ValueError(f"plan file has no executable task sections: {self.options.plan_file}")

    def _has_uncompleted_work(self) -> bool:
        assert self.options.plan_file is not None
        plan = self._parse_plan_file()
        if plan.tasks:
            return plan.has_uncompleted_tasks()
        return file_has_uncompleted_checkbox(self.options.plan_file)

    def _validate_completed_task_iteration(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
        completion_retries: int = 0,
    ) -> None:
        assert self.options.plan_file is not None
        plan = self._parse_plan_file()
        completed_task = self._matching_task(plan, selected_task)
        self._validate_later_tasks_unchanged(selected_task, plan_before, plan)
        changed_context = self._changed_plan_context(context_before)
        if changed_context:
            paths = ", ".join(self._display_path(path) for path in changed_context)
            raise RuntimeError(
                f"task {self._task_label(selected_task)} modified read-only plan context: {paths}"
            )
        if completed_task is None or not completed_task.complete:
            retry_suffix = (
                f" after {completion_retries} automatic completion "
                f"{'retry' if completion_retries == 1 else 'retries'}"
                if completion_retries
                else ""
            )
            raise RuntimeError(
                f"task {self._task_label(selected_task)} did not complete its selected plan section"
                f"{retry_suffix}"
            )

        git = self._git()
        if git.head_commit() == head_before:
            raise RuntimeError(
                f"task {self._task_label(selected_task)} completed without creating a commit"
            )
        new_dirty = self._uncommitted_paths() - dirty_before
        if new_dirty:
            if self.options.allow_dirty:
                self._log_allowed_dirty("task", new_dirty, selected_task)
                return
            paths = ", ".join(self._display_path(path) for path in sorted(new_dirty))
            raise RuntimeError(
                f"task {self._task_label(selected_task)} left new uncommitted changes "
                f"in the working tree: {paths}"
            )

    def _accept_task_result_or_raise(
        self,
        result: ExecResult,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
    ) -> None:
        task_label = self._task_label(selected_task)
        if not result.ok:
            if not self._task_iteration_completed_cleanly(
                selected_task,
                plan_before,
                context_before,
                head_before,
                dirty_before,
            ):
                self._restore_plan_snapshot(
                    plan_before,
                    selected_task,
                    reason="attempts_exhausted",
                )
                if (
                    self._git().head_commit() != head_before
                    or self._uncommitted_paths() - dirty_before
                ):
                    raise RuntimeError(
                        self._describe_task_failure_with_repository_changes(
                            result,
                            selected_task,
                            head_before,
                            dirty_before,
                        )
                    )
                raise RuntimeError(describe_failure("gigacode task session", result))
            self.log.diagnostic(
                "session=task event=failure_recovered "
                f"task={task_label!r} reason=committed_task_completion"
            )
        if result.signal == TASK_FAILED:
            self._restore_plan_snapshot(
                plan_before,
                selected_task,
                reason="task_failed",
            )
            raise RuntimeError("task failed")

    def _can_retry_incomplete_task(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
    ) -> bool:
        plan = self._parse_plan_file()
        current_task = self._matching_task(plan, selected_task)
        if current_task is None or current_task.complete:
            return False
        if not self._later_tasks_unchanged(selected_task, plan_before, plan):
            self.log.diagnostic(
                "session=task event=completion_retry_rejected "
                f"task={self._task_label(selected_task)!r} reason=later_tasks_modified"
            )
            return False
        if self._changed_plan_context(context_before):
            self.log.diagnostic(
                "session=task event=completion_retry_rejected "
                f"task={self._task_label(selected_task)!r} reason=read_only_context_modified"
            )
            return False
        return True

    def _prepare_task_retry(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
    ) -> bool:
        if self._task_iteration_completed_cleanly(
            selected_task,
            plan_before,
            context_before,
            head_before,
            dirty_before,
        ):
            self.log.diagnostic(
                "session=task event=retry_guard_rejected "
                f"task={self._task_label(selected_task)!r} reason=committed_task_completion"
            )
            return False

        changed_context = self._changed_plan_context(context_before)
        if changed_context:
            self.log.diagnostic(
                "session=task event=retry_guard_rejected "
                f"task={self._task_label(selected_task)!r} reason=read_only_context_modified"
            )
            return False

        self._restore_plan_snapshot(
            plan_before,
            selected_task,
            reason="retry",
        )
        return True

    def _restore_plan_snapshot(
        self,
        plan_before: str,
        selected_task: Task,
        *,
        reason: str,
    ) -> None:
        assert self.options.plan_file is not None
        current = (
            self.options.plan_file.read_text(encoding="utf-8")
            if self.options.plan_file.exists()
            else None
        )
        if current == plan_before:
            return
        self.options.plan_file.parent.mkdir(parents=True, exist_ok=True)
        self.options.plan_file.write_text(plan_before, encoding="utf-8")
        self.log.diagnostic(
            "session=task event=plan_snapshot_restored "
            f"task={self._task_label(selected_task)!r} reason={reason}"
        )

    def _task_iteration_completed_cleanly(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
    ) -> bool:
        assert self.options.plan_file is not None
        plan = self._parse_plan_file()
        completed_task = self._matching_task(plan, selected_task)
        task_complete = completed_task is not None and completed_task.complete
        later_tasks_unchanged = self._later_tasks_unchanged(
            selected_task,
            plan_before,
            plan,
        )
        return (
            task_complete
            and later_tasks_unchanged
            and not self._changed_plan_context(context_before)
            and self._git().head_commit() != head_before
            and (
                self.options.allow_dirty
                or not (self._uncommitted_paths() - dirty_before)
            )
        )

    def _log_allowed_dirty(
        self,
        session: str,
        paths: set[Path],
        selected_task: Optional[Task] = None,
    ) -> None:
        displayed = [self._display_path(path) for path in sorted(paths)]
        shown = ", ".join(displayed[:10])
        if len(displayed) > 10:
            shown += f", ... ({len(displayed)} total)"
        task = (
            f" task={self._task_label(selected_task)!r}"
            if selected_task is not None
            else ""
        )
        self.log.diagnostic(
            f"session={session} event=new_uncommitted_changes_allowed{task} "
            f"count={len(displayed)} paths={shown!r}"
        )

    def _describe_task_failure_with_repository_changes(
        self,
        result: ExecResult,
        selected_task: Task,
        head_before: str,
        dirty_before: set[Path],
    ) -> str:
        new_dirty = sorted(
            str(path)
            for path in self._uncommitted_paths() - dirty_before
        )
        head_changed = self._git().head_commit() != head_before
        state = []
        if head_changed:
            state.append("HEAD changed")
        if new_dirty:
            state.append(f"new uncommitted paths: {', '.join(new_dirty)}")
        if not state:
            state.append("the selected task checklist changed without a clean committed completion")

        continuation = (
            "If the partial work is valid, inspect it and rerun the same plan"
            + (" with --allow-dirty" if new_dirty else "")
            + "; otherwise correct or remove only the unintended changes before rerunning."
        )
        return (
            f"{describe_failure('gigacode task session', result)}; automatic retries were "
            f"exhausted while task {self._task_label(selected_task)} still lacked a clean committed completion "
            f"({'; '.join(state)}). Inspect `git status --short`, `git diff`, "
            f"`git diff --cached`, and `git log -1 --oneline`. {continuation}"
        )

    def _matching_task(self, plan: Plan, selected_task: Task) -> Optional[Task]:
        matches = plan.tasks_matching(selected_task.number, selected_task.title)
        return matches[0] if len(matches) == 1 else None

    def _plan_context_snapshot(self) -> dict[Path, bytes]:
        if self.options.plan_kind != "openspec" or self.options.plan_source is None:
            return {}
        assert self.options.plan_file is not None
        return {
            path: path.read_bytes()
            for path in self.options.plan_source.rglob("*")
            if path.is_file() and path != self.options.plan_file
        }

    def _changed_plan_context(self, before: dict[Path, bytes]) -> list[Path]:
        if not before and self.options.plan_kind != "openspec":
            return []
        assert self.options.plan_file is not None
        assert self.options.plan_source is not None
        current_paths = {
            path
            for path in self.options.plan_source.rglob("*")
            if path.is_file() and path != self.options.plan_file
        }
        changed = set(before) ^ current_paths
        changed.update(
            path
            for path in set(before) & current_paths
            if path.read_bytes() != before[path]
        )
        return sorted(changed)

    def _validate_later_tasks_unchanged(
        self,
        selected_task: Task,
        plan_before: str,
        plan_after: Plan,
    ) -> None:
        if not self._later_tasks_unchanged(selected_task, plan_before, plan_after):
            raise RuntimeError(
                f"task {self._task_label(selected_task)} modified or marked a later plan section"
            )

    def _later_tasks_unchanged(
        self,
        selected_task: Task,
        plan_before: str,
        plan_after: Plan,
    ) -> bool:
        before = parse_plan(plan_before, plan_format=self.options.plan_kind)
        selected_matches = [
            index
            for index, task in enumerate(before.tasks)
            if task.number == selected_task.number and task.title == selected_task.title
        ]
        if len(selected_matches) != 1:
            return False

        for later_task in before.tasks[selected_matches[0] + 1:]:
            after_task = self._matching_task(plan_after, later_task)
            if after_task is None:
                return False
            before_checkboxes = [
                (checkbox.text, checkbox.checked)
                for checkbox in later_task.checkboxes
            ]
            after_checkboxes = [
                (checkbox.text, checkbox.checked)
                for checkbox in after_task.checkboxes
            ]
            if after_checkboxes != before_checkboxes:
                return False
        return True

    @staticmethod
    def _task_label(task: Task) -> str:
        return f"{task.number}: {task.title}"

    def _structured_review_output(
        self,
        name: str,
        result: ExecResult,
    ) -> str:
        try:
            return normalize_review_output(result.output)
        except ReviewOutputError as first_error:
            try:
                recovered = recover_review_output(result.output)
            except ReviewOutputError as recovery_error:
                validation_error = str(recovery_error)
            else:
                self.log.diagnostic(
                    "session=review event=output_recovered "
                    f"agent={name} method=deterministic"
                )
                return recovered
            self.log.diagnostic(
                "session=review event=invalid_output "
                f"agent={name} action=format_retry error={str(first_error)!r} "
                f"recovery_error={validation_error!r}"
            )

        self.log.section(f"review format retry: {name}")
        head_before = self._git().head_commit()
        retry_prompt = render_review_format_retry_prompt(
            result.output,
            validation_error,
        )
        retry = self._run_single_review_agent(
            f"format-{name}",
            lambda _context: retry_prompt,
        )
        self._prefix_new_commits(head_before, f"review format retry: {name}")
        if not retry.ok:
            raise RuntimeError(describe_failure("gigacode review format retry", retry))
        try:
            return normalize_review_output(retry.output)
        except ReviewOutputError as retry_error:
            try:
                recovered = recover_review_output(retry.output)
            except ReviewOutputError as recovery_error:
                raise RuntimeError(
                    "review protocol invalid after format retry: "
                    f"{recovery_error}"
                ) from recovery_error
            self.log.diagnostic(
                "session=review event=output_recovered "
                f"agent={name} method=format_retry_deterministic "
                f"strict_error={str(retry_error)!r}"
            )
            return recovered

    def _git(self) -> GitService:
        return GitService(Path("."))

    def _run_single_review_agent(
        self,
        name: str,
        render_prompt: Callable[[PromptContext], str],
    ) -> ExecResult:
        if self.review_worktrees is None:
            return self.review_agent_executor.run(render_prompt(self._context()))

        with self.review_worktrees.create([name]) as worktrees:
            worktree = worktrees.paths[name]
            context = self._context_for_review_worktree(
                worktree,
                worktrees.repo_root,
            )
            return self.review_agent_executor.run(
                render_prompt(context),
                cwd=worktree,
            )

    def _run_parallel_review_agents(self) -> dict[str, ExecResult]:
        if self.review_worktrees is None:
            context = self._context()
            prompts = {
                name: render_review_agent_prompt(
                    self.options.prompts.review_agent,
                    name,
                    focus,
                    context,
                )
                for name, focus in REVIEW_AGENTS.items()
            }
            return self.review_agent_executor.run_batch(prompts)

        with self.review_worktrees.create(REVIEW_AGENTS) as worktrees:
            prompts = {
                name: render_review_agent_prompt(
                    self.options.prompts.review_agent,
                    name,
                    focus,
                    self._context_for_review_worktree(
                        worktrees.paths[name],
                        worktrees.repo_root,
                    ),
                )
                for name, focus in REVIEW_AGENTS.items()
            }
            return self.review_agent_executor.run_batch(
                prompts,
                workdirs=worktrees.paths,
            )

    def _context_for_review_worktree(
        self,
        worktree: Path,
        repo_root: Path,
    ) -> PromptContext:
        context = self._context()

        def remap(path: Optional[Path]) -> Optional[Path]:
            if path is None:
                return None
            try:
                relative = path.resolve().relative_to(repo_root.resolve())
            except ValueError:
                return path
            return worktree / relative

        return PromptContext(
            plan_file=remap(context.plan_file),
            progress_file=remap(context.progress_file) or context.progress_file,
            default_branch=context.default_branch,
            jira_task=context.jira_task,
            plan_kind=context.plan_kind,
            plan_source=remap(context.plan_source),
            plan_context_files=tuple(
                remapped
                for path in context.plan_context_files
                if (remapped := remap(path)) is not None
            ),
        )

    def _uncommitted_paths(self) -> set[Path]:
        git = self._git()
        repo_root = git.repo_root()
        ignored = {
            self.options.progress_file.resolve(),
            statistics_path(self.options.progress_file).resolve(),
        }
        return {
            (repo_root / path).resolve()
            for path in git.dirty_paths()
            if (repo_root / path).resolve() not in ignored
        }

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._git().repo_root()))
        except ValueError:
            return str(path)

    def _render_review_synthesis_prompt(
        self,
        findings: dict[str, str],
        context: PromptContext,
    ) -> str:
        try:
            return render_review_synthesis_prompt(
                self.options.prompts.review_synthesis,
                findings,
                context,
            )
        except ReviewOutputError as exc:
            raise RuntimeError(f"invalid structured review output: {exc}") from exc

    def _accept_review_synthesis_or_raise(
        self,
        result: ExecResult,
        findings: dict[str, str],
    ) -> bool:
        identified = identify_review_findings(findings)
        expected_ids = [item.finding_id for item in identified]
        decisions = None
        first_validation_error = ""
        try:
            decisions = parse_synthesis_output(result.output, expected_ids)
        except ReviewOutputError as first_error:
            first_validation_error = str(first_error)
            try:
                decisions = recover_synthesis_output(result.output, expected_ids)
            except ReviewOutputError as recovery_error:
                validation_error = str(recovery_error)
            else:
                self.log.diagnostic(
                    "session=review-synthesis event=output_recovered "
                    "method=deterministic"
                )
                validation_error = ""

        if decisions is None:
            self.log.diagnostic(
                "session=review-synthesis event=invalid_output "
                f"action=reconcile error={first_validation_error!r} "
                f"recovery_error={validation_error!r}"
            )
            self.log.section("review synthesis reconciliation")
            head_before = self._git().head_commit()
            recovery = self.synthesis_executor.run(
                render_review_synthesis_recovery_prompt(
                    self.options.prompts.review_synthesis,
                    findings,
                    self._context(),
                    result.output,
                    validation_error,
                )
            )
            self._prefix_new_commits(head_before, "review synthesis reconciliation")
            if not recovery.ok or recovery.signal == TASK_FAILED:
                reason = (
                    "reported task failure"
                    if recovery.signal == TASK_FAILED
                    else describe_failure("gigacode review synthesis reconciliation", recovery)
                )
                self.log.diagnostic(
                    "session=review-synthesis event=reconciliation_failed "
                    f"action=next_review_iteration reason={reason!r}"
                )
                return False
            try:
                decisions = parse_synthesis_output(recovery.output, expected_ids)
            except ReviewOutputError as recovery_error:
                try:
                    decisions = recover_synthesis_output(
                        recovery.output,
                        expected_ids,
                    )
                except ReviewOutputError as final_error:
                    raise RuntimeError(
                        "review synthesis protocol invalid after reconciliation: "
                        f"{final_error}"
                    ) from final_error
                self.log.diagnostic(
                    "session=review-synthesis event=output_recovered "
                    "method=reconciliation_deterministic "
                    f"strict_error={str(recovery_error)!r}"
                )
            result = recovery

        blocked_before_audit = [
            decision for decision in decisions if decision.decision == "blocked"
        ]
        if blocked_before_audit:
            blocked_ids = [decision.finding_id for decision in blocked_before_audit]
            self.log.diagnostic(
                "session=review-synthesis event=blocked_audit_started "
                f"findings={','.join(blocked_ids)!r}"
            )
            self.log.section("review synthesis blocked audit")
            head_before = self._git().head_commit()
            audit = self.synthesis_executor.run(
                render_review_synthesis_blocked_audit_prompt(
                    self.options.prompts.review_synthesis,
                    findings,
                    self._context(),
                    result.output,
                    blocked_ids,
                )
            )
            self._prefix_new_commits(head_before, "review synthesis blocked audit")
            if not audit.ok or audit.signal == TASK_FAILED:
                reason = (
                    "reported task failure"
                    if audit.signal == TASK_FAILED
                    else describe_failure("gigacode review synthesis blocked audit", audit)
                )
                raise RuntimeError(f"review synthesis blocked audit failed: {reason}")
            try:
                decisions = parse_synthesis_output(audit.output, expected_ids)
            except ReviewOutputError as audit_error:
                try:
                    decisions = recover_synthesis_output(audit.output, expected_ids)
                except ReviewOutputError as final_error:
                    raise RuntimeError(
                        "review synthesis blocked audit protocol invalid: "
                        f"{final_error}"
                    ) from final_error
                self.log.diagnostic(
                    "session=review-synthesis event=output_recovered "
                    "method=blocked_audit_deterministic "
                    f"strict_error={str(audit_error)!r}"
                )
            result = audit
            remaining_blocked = [
                decision.finding_id
                for decision in decisions
                if decision.decision == "blocked"
            ]
            self.log.diagnostic(
                "session=review-synthesis event=blocked_audit_completed "
                f"remaining={','.join(remaining_blocked)!r}"
            )

        counts = {
            decision: sum(item.decision == decision for item in decisions)
            for decision in ("fixed", "rejected", "confirmed", "blocked")
        }
        self.log.diagnostic(
            "session=review-synthesis event=decisions_validated "
            f"input_findings={len(identified)} processed_findings={len(decisions)} "
            + " ".join(f"{name}={count}" for name, count in counts.items())
        )

        blocked = [decision for decision in decisions if decision.decision == "blocked"]
        if blocked:
            details = "; ".join(
                f"{decision.finding_id}: {decision.reason}" for decision in blocked
            )
            raise RuntimeError(f"review synthesis blocked: {details}")

        requires_another_pass = [
            decision
            for decision in decisions
            if decision.decision in {"fixed", "confirmed"}
        ]
        completed = not decisions or all(
            decision.decision == "rejected" for decision in decisions
        )
        if result.signal == REVIEW_DONE and requires_another_pass:
            ids = ",".join(
                decision.finding_id for decision in requires_another_pass
            )
            self.log.diagnostic(
                "session=review-synthesis event=premature_completion_signal_ignored "
                f"findings={ids!r}"
            )
        elif completed and result.signal != REVIEW_DONE:
            self.log.diagnostic(
                "session=review-synthesis event=completion_inferred_from_decisions"
            )
        return completed

    def _prefix_new_commits(self, head_before: str, label: str) -> None:
        if not self.options.jira_task:
            return
        try:
            changed = self._git().prefix_commit_messages_since(
                head_before,
                self.options.jira_task,
            )
        except GitError as exc:
            raise RuntimeError(
                f"{label} could not add Jira prefix {self.options.jira_task}: {exc}"
            ) from exc
        if changed:
            self.log.diagnostic(
                "session=git event=commit_messages_prefixed "
                f"label={label!r} jira_task={self.options.jira_task} "
                f"count={len(changed)}"
            )


def describe_failure(label: str, result: ExecResult) -> str:
    parts = [label]
    if result.rate_limited:
        parts.append("rate limited")
    elif result.transient_error:
        parts.append("hit a transient error")
    elif result.idle_timed_out:
        parts.append("idle timed out")
    elif result.timed_out:
        parts.append("timed out")
    elif result.api_error:
        parts.append(f"failed with {result.api_error}")
    else:
        parts.append(f"exited with status {result.returncode}")
    if result.approval_unavailable:
        parts.append("(GigaCode requested tool approval in non-interactive mode)")
    if result.attempts > 1:
        parts.append(f"after {result.attempts} attempts")
    return " ".join(parts)
