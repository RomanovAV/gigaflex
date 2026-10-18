from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import time
from typing import Callable, Iterator, Optional

from .checkpoint import ResumeError, RunCheckpoint
from .dashboard import ProgressDashboard
from .executor import ExecResult, GigaCodeExecutor
from .git import (
    GitError,
    GitService,
    ReviewWorktreeManager,
    TaskWorktree,
    TaskWorktreeManager,
)
from .plan import (
    Plan,
    Task,
    file_has_uncompleted_checkbox,
    parse_plan_file,
    task_plan_update_allowed,
)
from .progress import ProgressLog
from .prompts import (
    DEFAULT_PROMPTS,
    REVIEW_AGENTS,
    FollowupReviewScope,
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
    IdentifiedReviewFinding,
    ReviewDecisionRecord,
    ReviewOutputError,
    SynthesisDecision,
    build_review_decision_records,
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
from .validation import RepositoryState, ValidationCommand, repository_state, run_validation


CORE_REVIEW_AGENTS = ("quality", "implementation")
MAX_REVIEW_DECISION_MEMORY = 30


@dataclass
class RunOptions:
    plan_file: Optional[Path]
    progress_file: Path
    default_branch: str = "main"
    max_iterations: int = 50
    review_iterations: int = 10
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
    resume: bool = False
    resume_note: str = ""
    validation_commands: tuple[ValidationCommand, ...] = ()


@dataclass(frozen=True)
class TaskCompletionStatus:
    errors: tuple[str, ...] = ()
    repairable: bool = True

    @property
    def complete(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class TaskBaseline:
    plan: str
    context: dict[Path, bytes]
    head: str
    dirty: set[Path]


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
        task_worktrees: Optional[TaskWorktreeManager] = None,
        checkpoint: Optional[RunCheckpoint] = None,
    ) -> None:
        self.options = options
        self.executor = executor
        self.synthesis_executor = synthesis_executor or executor
        self.review_agent_executor = review_agent_executor or self.synthesis_executor
        self.finalize_executor = finalize_executor or self.synthesis_executor
        self._review_decision_memory: dict[str, ReviewDecisionRecord] = {}
        self.log = log
        self.dashboard = dashboard
        self.review_worktrees = review_worktrees
        self.task_worktrees = task_worktrees
        self.checkpoint = checkpoint
        self._active_cwd: Optional[Path] = None
        self._latest_review_verification_records: tuple[ReviewDecisionRecord, ...] = ()
        self._pending_task_recovery: Optional[dict[str, object]] = None
        self._pending_phase_recovery: Optional[dict[str, object]] = None
        self._reviewed_state: Optional[RepositoryState] = None

    def run(self) -> None:
        if self.options.dry_run:
            self.print_prompts()
            return
        blocked = self.checkpoint.blocked if self.checkpoint is not None else {}
        if self.options.resume and not blocked:
            raise ResumeError("no saved stopped run was found for this plan; run without --resume")
        if blocked:
            if not self.options.resume:
                raise ResumeError(
                    f"this run has saved unfinished work; continue with: {blocked.get('resume_command', '--resume')}"
                )
            recovery = blocked.get("task_recovery")
            if recovery:
                if recovery.get("phase", "task") != "task":
                    if self.options.tasks_only or self.task_worktrees is None:
                        raise ResumeError("saved review/finalize work requires a full or review run")
                    self._pending_phase_recovery = dict(recovery)
                elif self.options.review_only or self.task_worktrees is None:
                    raise ResumeError("saved task work must be resumed in task mode")
                else:
                    self._pending_task_recovery = dict(recovery)
            elif self.checkpoint is not None:
                self.checkpoint.clear_blocked()
        self.log.event("execution_started", resume=self.options.resume)
        if not self.options.review_only:
            had_uncompleted_work = self._has_uncompleted_work()
            if self.checkpoint is not None and had_uncompleted_work:
                self.checkpoint.invalidate_from("tasks")
                self.checkpoint.mark_started("tasks")
            self.run_tasks()
            if self.checkpoint is not None and had_uncompleted_work:
                self.checkpoint.mark_completed(
                    "tasks",
                    self.checkpoint.current_state(),
                )
        if self.options.tasks_only:
            self.log.section("done")
            self.log.write("task execution completed\n")
            return
        if self._pending_phase_recovery is not None:
            recovery = self._pending_phase_recovery
            if recovery.get("phase") == "synthesis":
                self._run_synthesis(recovery["phase_context"]["findings"], recovery=recovery)
            elif recovery.get("phase") == "finalize":
                self._finalize_candidate(recovery=recovery, may_repair=True)
            else:
                raise ResumeError("unknown saved execution phase; saved work has been retained")
            self._pending_phase_recovery = None
        review_reused = False
        if self.checkpoint is not None and not self.options.review_only:
            review_state = self.checkpoint.current_state()
            review_reused = self.checkpoint.can_reuse("review", review_state)
            if review_reused and self._run_checks("review"):
                review_reused = False
        if review_reused:
            self._log_checkpoint_reuse("review")
            self._reviewed_state = self._state()
        else:
            if self.checkpoint is not None:
                self.checkpoint.invalidate_from("review")
                self.checkpoint.mark_started("review")
            self.run_review()
            if self.checkpoint is not None:
                self.checkpoint.mark_completed(
                    "review",
                    self.checkpoint.current_state(),
                )
        if self.options.finalize_enabled:
            finalize_reused = False
            if self.checkpoint is not None and not self.options.review_only:
                finalize_state = self.checkpoint.current_state()
                finalize_reused = self.checkpoint.can_reuse(
                    "finalize",
                    finalize_state,
                )
                if finalize_reused and self._run_checks("finalize"):
                    finalize_reused = False
            if finalize_reused:
                self._log_checkpoint_reuse("finalize")
            else:
                if self.checkpoint is not None:
                    self.checkpoint.invalidate_from("finalize")
                    self.checkpoint.mark_started("finalize")
                self.run_finalize()
                if self.checkpoint is not None:
                    self.checkpoint.mark_completed(
                        "finalize",
                        self.checkpoint.current_state(),
                    )

        self._assert_state(self._reviewed_state, "completed result")
        report = {"repository": asdict(self._reviewed_state) if self._reviewed_state else None,
                  "review": "passed", "finalize": "passed" if self.options.finalize_enabled else "disabled"}
        self.log.event("verification", **report)
        self.log.diagnostic("session=verification report=" + json.dumps(report))
        if self.dashboard is not None:
            self.dashboard.result_verified(report)

    @property
    def verified_state(self) -> Optional[RepositoryState]:
        self._assert_state(self._reviewed_state, "completed result")
        return self._reviewed_state

    def _log_checkpoint_reuse(self, phase: str) -> None:
        self.log.event("phase_reused", phase=phase,
                       history=self.checkpoint.phase_history(phase) if self.checkpoint else None)
        self.log.section(f"{phase} checkpoint")
        self.log.write(
            f"reused successful {phase} result for unchanged HEAD and working tree\n"
        )
        self.log.diagnostic(
            f"session=checkpoint event=phase_skipped phase={phase} reason=unchanged_state"
        )
        if self.dashboard is not None:
            self.dashboard.phase_reused(
                phase,
                f"Reused successful {phase} result for unchanged repository state",
            )

    def run_tasks(self) -> None:
        if self.dashboard is not None:
            self.dashboard.phase_started("tasks", "Executing plan tasks")
        if self.options.plan_file is None:
            raise ValueError("plan file is required for task execution")
        self._validate_plan_has_tasks()
        if not self._has_uncompleted_work():
            if self._pending_task_recovery:
                raise ResumeError("the plan no longer contains the saved pending task; saved work has been retained")
            self.log.section("tasks")
            self.log.write("plan already has no uncompleted task sections\n")
            return

        for iteration in range(1, self.options.max_iterations + 1):
            selected_task = self._parse_plan_file().first_uncompleted_task()
            if selected_task is None:
                return
            task_label = self._task_label(selected_task)
            self.log.event("task_started", number=selected_task.number, title=selected_task.title,
                           iteration=iteration, head=self._git().head_commit())
            if self.dashboard is not None:
                self.dashboard.task_started(
                    selected_task.number,
                    selected_task.title,
                    iteration,
                )
            self.log.section(f"task iteration {iteration}: {task_label}")
            if self.task_worktrees is None:
                result = self._execute_task_iteration(selected_task)
            else:
                original_plan = self.options.plan_file.read_text(encoding="utf-8")
                original_context = self._plan_context_snapshot()
                task_context = (
                    self.task_worktrees.resume(task_label, self._pending_task_recovery)
                    if self._pending_task_recovery else self.task_worktrees.create(task_label)
                )
                with task_context as workspace:
                    baseline = TaskBaseline(
                        original_plan,
                        {workspace.path / path.resolve().relative_to(workspace.repo_root): content
                         for path, content in original_context.items()},
                        workspace.snapshot_commit, set(),
                    ) if workspace.resumed else None
                    with self._use_task_workspace(workspace):
                        result = self._execute_task_iteration(
                            selected_task, baseline, resumed=workspace.resumed,
                        )
                        task_head = self._git().head_commit()
                    workspace.promote(task_head)
                    self._pending_task_recovery = None
                    if self.checkpoint is not None:
                        self.checkpoint.clear_blocked()
            self.log.event("task_completed", number=selected_task.number, title=selected_task.title,
                           iteration=iteration, head=self._git().head_commit())
            if self.dashboard is not None:
                self.dashboard.task_finished()
            if result.signal == ALL_TASKS_DONE and not self._has_uncompleted_work():
                return
            if not self._has_uncompleted_work():
                return
            time.sleep(self.options.delay_seconds)
        raise RuntimeError(f"max task iterations reached: {self.options.max_iterations}")

    def _execute_task_iteration(
        self, selected_task: Task, baseline: Optional[TaskBaseline] = None, *, resumed: bool = False,
    ) -> ExecResult:
        assert self.options.plan_file is not None
        context = self._context()
        plan_before = baseline.plan if baseline else self.options.plan_file.read_text(encoding="utf-8")
        context_before = baseline.context if baseline else self._plan_context_snapshot()
        head_before = baseline.head if baseline else self._git().head_commit()
        dirty_before = baseline.dirty if baseline else self._uncommitted_paths()
        prompt = render_task_prompt(
            self.options.prompts.task,
            context,
            selected_task.number,
            selected_task.title,
            selected_task.section,
            selected_task.has_implicit_tracking,
        )
        task_label = self._task_label(selected_task)
        if resumed:
            status = self._task_completion_status(selected_task, plan_before, context_before, head_before, dirty_before)
            if status.complete and not self.options.resume_note and not self._run_checks("task"):
                self.log.write(f"saved task {task_label} already satisfies completion checks; retrying promotion\n")
                return ExecResult(output="saved task completion verified", returncode=0)
            if not status.repairable:
                self._restore_task_contract(plan_before, context_before)
            prompt += (
                "\nResuming saved task work after an interrupted or blocked run.\n"
                "Inspect existing commits and staged/unstaged files; preserve valid work.\n"
                "Verify the entire original selected task before marking it complete and committing.\n"
            )
        result = self._run_task_agent(
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
        self._prefix_new_commits(head_before, f"task {task_label}")
        if not self._can_restore_contract_for_retry(
            selected_task, plan_before, context_before, head_before, dirty_before,
            self.options.task_completion_retries,
        ):
            self._accept_task_result_or_raise(
                result, selected_task, plan_before, context_before, head_before, dirty_before,
            )
        completion_retries = 0
        while (
            completion_retries < max(0, self.options.task_completion_retries)
            and self._can_retry_incomplete_task(
                selected_task,
                plan_before,
                context_before,
                head_before,
                dirty_before,
            )
        ):
            completion_retries += 1
            self.log.event("completion_retry", phase="task", task=task_label, attempt=completion_retries)
            status = self._task_completion_status(
                selected_task, plan_before, context_before, head_before, dirty_before,
            )
            validation_errors = status.errors
            if not status.repairable:
                self._restore_task_contract(plan_before, context_before)
                validation_errors += (
                    "The original plan and read-only context were restored. Verify ALL selected "
                    "requirements again, restore completion tracking only after validation, and commit the correction.",
                )
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
                current_task.has_implicit_tracking,
                validation_errors=validation_errors,
            )
            result = self._run_task_agent(
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
            if not self._can_restore_contract_for_retry(
                selected_task, plan_before, context_before, head_before, dirty_before,
                self.options.task_completion_retries - completion_retries,
            ):
                self._accept_task_result_or_raise(
                    result, selected_task, plan_before, context_before, head_before, dirty_before,
                )
        self._validate_completed_task_iteration(
            selected_task,
            plan_before,
            context_before,
            head_before,
            dirty_before,
            completion_retries,
        )
        for attempt in range(max(0, self.options.task_completion_retries) + 1):
            errors = self._run_checks("task")
            if not errors:
                break
            if attempt == max(0, self.options.task_completion_retries):
                raise RuntimeError("task runner validation failed: " + "; ".join(errors))
            self.log.event("completion_retry", phase="task_validation", task=task_label,
                           attempt=attempt + 1, errors=errors)
            current = self._matching_task(self._parse_plan_file(), selected_task)
            result = self._run_task_agent(render_task_completion_retry_prompt(
                prompt, self.options.plan_file, selected_task.number, selected_task.title,
                current.section, current.has_implicit_tracking, validation_errors=tuple(errors),
            ), retry_guard=lambda _result: self._prepare_task_retry(
                selected_task, plan_before, context_before, head_before, dirty_before,
            ))
            self._prefix_new_commits(head_before, "task validation repair")
            self._accept_task_result_or_raise(result, selected_task, plan_before, context_before, head_before, dirty_before)
            self._validate_completed_task_iteration(selected_task, plan_before, context_before, head_before, dirty_before)
        return result

    def run_review(self) -> None:
        if self.dashboard is not None:
            self.dashboard.phase_started("review", "Reviewing the completed changes")
        if self.options.parallel_review:
            self.run_parallel_review()
            return

        context = self._context()
        repair_budget = max(0, self.options.review_iterations)
        max_attempts = repair_budget + 1
        repair_count = 0
        iteration = 0
        followup_scope: Optional[FollowupReviewScope] = None
        while True:
            iteration += 1
            terminal_verification = repair_count >= repair_budget
            active_scope = self._verification_scope(
                followup_scope,
                terminal_verification=terminal_verification,
            )
            if self.dashboard is not None:
                self.dashboard.review_attempt_started(
                    iteration,
                    max_attempts,
                    parallel=False,
                )
            section = (
                f"review terminal verification {iteration}"
                if terminal_verification
                else f"review iteration {iteration}"
            )
            self.log.section(section)
            self._log_review_attempt_scope(
                iteration,
                repair_count,
                active_scope,
                terminal_verification=terminal_verification,
            )
            reviewed_before = self._state()
            head_before = self._git().head_commit()
            result = self._run_single_review_agent(
                "review",
                lambda review_context: render_review_prompt(
                    self.options.prompts.review,
                    review_context,
                    decision_memory=self._review_decision_memory_snapshot(),
                    followup_scope=active_scope,
                ),
                base_ref=(
                    active_scope.base_commit
                    if active_scope is not None and active_scope.base_commit
                    else self.options.default_branch
                ),
            )
            self._prefix_new_commits(head_before, "review")
            self._assert_state(reviewed_before, "read-only review")
            if not result.ok:
                raise RuntimeError(describe_failure("gigacode review session", result))
            if result.signal == TASK_FAILED:
                raise RuntimeError("review failed")
            structured_output = self._structured_review_output(
                "review",
                result,
            )
            findings = {"review": structured_output}
            check_errors = self._run_checks("review")
            self._assert_state(reviewed_before, "review validation")
            if check_errors:
                findings["runner_validation"] = self._validation_finding(check_errors)
            identified = identify_review_findings(findings)
            self.log.event("review_findings", iteration=iteration, repository=asdict(reviewed_before) if reviewed_before else None,
                           findings=[asdict(item) for item in identified])
            if not identified:
                self._reviewed_state = reviewed_before
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

            if terminal_verification:
                self._raise_review_repair_budget_exhausted(
                    iteration,
                    repair_count,
                    len(identified),
                )

            self.log.section("review synthesis")
            if self.dashboard is not None:
                self.dashboard.review_synthesis_started(iteration, len(identified))
            head_before = self._git().head_commit()
            dirty_before = self._safe_uncommitted_paths()
            if self._run_synthesis(findings):
                self._reviewed_state = self._state()
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=len(identified),
                        message=f"Review passed on attempt {iteration}",
                    )
                return
            repair_count += 1
            followup_scope = self._build_followup_review_scope(
                repair_count,
                head_before,
                dirty_before,
            )
            if self.dashboard is not None:
                self.dashboard.review_attempt_finished(
                    iteration,
                    "needs_another_pass",
                    findings=len(identified),
                    message=(
                        f"Review attempt {iteration} repaired findings; "
                        "running focused verification"
                    ),
                )
            time.sleep(self.options.delay_seconds)

    def run_parallel_review(self) -> None:
        context = self._context()
        selected_agents = tuple(REVIEW_AGENTS)
        repair_budget = max(0, self.options.review_iterations)
        max_attempts = repair_budget + 1
        repair_count = 0
        iteration = 0
        followup_scope: Optional[FollowupReviewScope] = None
        while True:
            iteration += 1
            terminal_verification = repair_count >= repair_budget
            active_scope = self._verification_scope(
                followup_scope,
                terminal_verification=terminal_verification,
            )
            if self.dashboard is not None:
                self.dashboard.review_attempt_started(
                    iteration,
                    max_attempts,
                    parallel=True,
                )
            section = (
                f"parallel review terminal verification {iteration}"
                if terminal_verification
                else f"parallel review iteration {iteration}"
            )
            self.log.section(section)
            self.log.diagnostic(
                "session=review event=agents_selected "
                f"iteration={iteration} agents={','.join(selected_agents)!r} "
                f"repair_count={repair_count} "
                f"terminal_verification={terminal_verification}"
            )
            self._log_review_attempt_scope(
                iteration,
                repair_count,
                active_scope,
                terminal_verification=terminal_verification,
            )
            reviewed_before = self._state()
            head_before = self._git().head_commit()
            results = self._run_parallel_review_agents(
                selected_agents,
                followup_scope=active_scope,
            )
            self._prefix_new_commits(head_before, "parallel review")
            self._assert_state(reviewed_before, "read-only review")
            findings: dict[str, str] = {}
            for name in selected_agents:
                result = results[name]
                self.log.section(f"review agent: {name}")
                self.log.write(result.output)
                if result.error_output:
                    self.log.write(result.error_output)
                if not result.ok:
                    raise RuntimeError(describe_failure(f"gigacode review agent {name}", result))
                findings[name] = self._structured_review_output(name, result)

            check_errors = self._run_checks("review")
            self._assert_state(reviewed_before, "review validation")
            if check_errors:
                findings["runner_validation"] = self._validation_finding(check_errors)
            identified = identify_review_findings(findings)
            self.log.event("review_findings", iteration=iteration, repository=asdict(reviewed_before) if reviewed_before else None,
                           findings=[asdict(item) for item in identified])
            if not identified:
                self._reviewed_state = reviewed_before
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

            if terminal_verification:
                self._raise_review_repair_budget_exhausted(
                    iteration,
                    repair_count,
                    len(identified),
                )

            self.log.section("review synthesis")
            if self.dashboard is not None:
                self.dashboard.review_synthesis_started(iteration, len(identified))
            head_before = self._git().head_commit()
            dirty_before = self._safe_uncommitted_paths()
            if self._run_synthesis(findings):
                self._reviewed_state = self._state()
                if self.dashboard is not None:
                    self.dashboard.review_attempt_finished(
                        iteration,
                        "passed",
                        findings=len(identified),
                        message=f"Review passed on attempt {iteration}",
                    )
                return
            repair_count += 1
            followup_scope = self._build_followup_review_scope(
                repair_count,
                head_before,
                dirty_before,
            )
            if self.dashboard is not None:
                self.dashboard.review_attempt_finished(
                    iteration,
                    "needs_another_pass",
                    findings=len(identified),
                    message=(
                        f"Review attempt {iteration} repaired findings; "
                        "running focused verification"
                    ),
                )
            selected_agents = self._followup_review_agents()
            time.sleep(self.options.delay_seconds)

    @staticmethod
    def _verification_scope(
        scope: Optional[FollowupReviewScope],
        *,
        terminal_verification: bool,
    ) -> Optional[FollowupReviewScope]:
        if scope is None:
            return None
        return replace(scope, terminal_verification=terminal_verification)

    def _log_review_attempt_scope(
        self,
        iteration: int,
        repair_count: int,
        scope: Optional[FollowupReviewScope],
        *,
        terminal_verification: bool,
    ) -> None:
        mode = "initial" if scope is None else "followup"
        self.log.event("review_pass", iteration=iteration, repair_count=repair_count,
                       mode=mode, terminal_verification=terminal_verification)
        self.log.diagnostic(
            "session=review event=attempt_scope "
            f"iteration={iteration} mode={mode} repair_count={repair_count} "
            f"terminal_verification={terminal_verification} "
            f"files={len(scope.files) if scope is not None else 'full'}"
        )

    def _raise_review_repair_budget_exhausted(
        self,
        iteration: int,
        repair_count: int,
        findings: int,
    ) -> None:
        message = (
            "review repair budget exhausted after terminal verification: "
            f"{findings} findings remain after {repair_count} repair cycles"
        )
        self.log.diagnostic(
            "session=review event=repair_budget_exhausted "
            f"iteration={iteration} repairs={repair_count} findings={findings}"
        )
        if self.dashboard is not None:
            self.dashboard.review_attempt_finished(
                iteration,
                "needs_another_pass",
                findings=findings,
                message=message,
            )
        raise RuntimeError(message)

    def _safe_uncommitted_paths(self) -> set[Path]:
        git = self._git()
        if not git.is_repo():
            return set()
        return self._uncommitted_paths()

    def _build_followup_review_scope(
        self,
        repair_number: int,
        base_commit: str,
        dirty_before: set[Path],
    ) -> FollowupReviewScope:
        records = self._latest_review_verification_records
        files = {record.file for record in records if record.file}
        git = self._git()
        head_commit = git.head_commit()
        if git.is_repo():
            if base_commit and head_commit:
                try:
                    files.update(
                        str(path)
                        for path in git.changed_paths_between(
                            base_commit,
                            head_commit,
                        )
                    )
                except GitError as exc:
                    self.log.diagnostic(
                        "session=review event=followup_diff_failed "
                        f"base={base_commit!r} head={head_commit!r} "
                        f"error={str(exc)!r}"
                    )
            dirty_after = self._safe_uncommitted_paths()
            files.update(
                self._display_path(path)
                for path in dirty_after - dirty_before
            )
        scope = FollowupReviewScope(
            repair_number=repair_number,
            base_commit=base_commit,
            head_commit=head_commit,
            files=tuple(sorted(files)),
            decisions=records,
        )
        self.log.diagnostic(
            "session=review event=followup_scope_created "
            f"repair={repair_number} base={base_commit or 'none'} "
            f"head={head_commit or 'none'} files={len(scope.files)} "
            f"decisions={len(scope.decisions)}"
        )
        return scope

    def _followup_review_agents(self) -> tuple[str, ...]:
        relevant = {
            record.agent
            for record in self._latest_review_verification_records
        }
        return tuple(
            name
            for name in REVIEW_AGENTS
            if name in CORE_REVIEW_AGENTS or name in relevant
        )

    def _state(self) -> Optional[RepositoryState]:
        git = self._git()
        if not git.is_repo() or not git.head_commit():
            return None
        root = git.repo_root()
        excluded = [self.options.progress_file, self.log.prompt_context_file,
                    statistics_path(self.options.progress_file)]
        if self.checkpoint is not None:
            excluded.append(self.checkpoint.path)
        if self.dashboard is not None:
            excluded.extend((self.dashboard.json_path, self.dashboard.html_path))
        paths = tuple(path.resolve().relative_to(root) for path in excluded if path.resolve().is_relative_to(root))
        return repository_state(git, paths, self._artifact_paths())

    def _artifact_paths(self) -> tuple[Path, ...]:
        manager = self.task_worktrees or self.review_worktrees
        return manager.artifact_paths if manager is not None else ()

    def _assert_state(self, expected: Optional[RepositoryState], phase: str) -> None:
        if expected is not None and self._state() != expected:
            raise RuntimeError(f"{phase} changed HEAD, file contents, or staged state; the result was not verified")

    def _run_checks(self, phase: str) -> list[str]:
        commands = [command for command in self.options.validation_commands if phase in command.phases]
        if not commands:
            self.log.event("validation", phase=phase, status="not_configured", checks=[])
            self.log.diagnostic(f"session=validation phase={phase} status=not_configured evidence=agent_report_and_repository_checks")
            if self.dashboard is not None:
                self.dashboard.validation_finished({"phase": phase, "status": "not_configured", "checks": []})
            return []
        expected = self._state()
        if self._active_cwd is None and self.task_worktrees is not None:
            manager = ReviewWorktreeManager(
                self._git(), artifact_paths=self._artifact_paths(),
                ignored_paths=self.task_worktrees.ignored_paths,
            )
            with manager.create(["validation"], base_ref=self.options.default_branch) as worktrees:
                root = worktrees.paths["validation"]
                results = self._execute_checks(commands, root)
        else:
            results = self._execute_checks(commands, self._git().repo_root())
        self._assert_state(expected, "runner validation")
        report = {"phase": phase, "repository": asdict(expected) if expected else None, "checks": results,
                  "status": "passed" if all(item["status"] == "passed" for item in results) else "failed"}
        self.log.event("validation", **report)
        self.log.diagnostic("session=validation report=" + json.dumps(report, ensure_ascii=False))
        if self.dashboard is not None:
            self.dashboard.validation_finished(report)
        return [f"{item['name']}: {item['status']} (exit {item['returncode']})\n{str(item['output'])[-4000:]}"
                for item in results if item["status"] != "passed"]

    def _execute_checks(self, commands, root: Path) -> list[dict[str, object]]:
        git = GitService(root)
        results = []
        for command in commands:
            before = repository_state(git, artifact_paths=self._artifact_paths())
            result = run_validation(command, root)
            changed = repository_state(git, artifact_paths=self._artifact_paths()) != before
            if changed:
                result["status"] = "failed"
                result["output"] = str(result["output"]) + "\nValidation command modified repository content or index."
            results.append(result)
            if changed:
                break
        return results

    @staticmethod
    def _validation_finding(errors: list[str]) -> str:
        evidence = " ".join("; ".join(errors).split()).replace("<", "[").replace(">", "]")[:6000]
        return (
            "<FINDING>\nseverity: major\ncategory: correctness\nfile: .gigaflex/config\nline: 1\n"
            f"evidence: Runner-owned validation failed: {evidence}\n"
            "impact: The current result does not satisfy the configured project checks.\n"
            "suggested_fix: Fix the deliverable and pass the original configured checks without weakening them.\n</FINDING>"
        )

    def _phase_protected(self) -> dict[Path, bytes]:
        protected = self._plan_context_snapshot()
        if self.options.plan_file is not None:
            protected[self.options.plan_file] = self.options.plan_file.read_bytes()
        return protected

    def _phase_errors(self, before: Optional[RepositoryState], protected: dict[Path, bytes], phase: str) -> list[str]:
        errors = []
        try:
            same_contract = self._phase_protected() == protected
        except (OSError, UnicodeError):
            same_contract = False
        if not same_contract:
            errors.append("modified protected plan or OpenSpec context")
        current = self._state()
        if self._active_cwd is not None:
            dirty = self._uncommitted_paths()
            if dirty:
                errors.append("uncommitted phase output: " + ", ".join(self._display_path(path) for path in sorted(dirty)))
        elif current is not None and before is not None and current != before:
            # Legacy direct Runner callers still cannot accept newly dirty state,
            # including changes to a path that was already dirty at entry.
            if current.tree != self._git().tree_id("HEAD") or current.index != self._git().tree_id("HEAD"):
                errors.append("phase changed content or staged state without a clean committed result")
        if not errors:
            errors.extend(self._run_checks(phase))
        return errors

    def _restore_phase_contract(self, protected: dict[Path, bytes]) -> None:
        if self._active_cwd is None:
            return
        if self.options.plan_file is not None:
            self._restore_task_contract(
                protected[self.options.plan_file].decode("utf-8"),
                {path: content for path, content in protected.items() if path != self.options.plan_file},
            )

    def _run_synthesis_agent(self, prompt: str) -> ExecResult:
        if self._active_cwd is None:
            return self.synthesis_executor.run(prompt)
        return self.synthesis_executor.run(prompt, cwd=self._active_cwd)

    def _execute_synthesis(self, findings, before, protected) -> bool:
        prompt = self._render_review_synthesis_prompt(findings, self._context())
        errors = []
        for attempt in range(max(0, self.options.task_completion_retries) + 1):
            if errors:
                self.log.event("completion_retry", phase="synthesis", attempt=attempt, errors=errors)
                self._restore_phase_contract(protected)
            correction = "\n\nCorrect this same candidate and return a complete finding ledger. " \
                         "Validate and commit required deliverables; remove only verified disposable outputs. " \
                         "Preserve the original plan and context.\n" + "\n".join(errors) if errors else ""
            result = self._run_synthesis_agent(prompt + correction)
            if before is not None:
                self._prefix_new_commits(before.head, "review synthesis")
            if not result.ok or result.signal == TASK_FAILED:
                raise RuntimeError(describe_failure("gigacode review synthesis", result))
            completed = self._accept_review_synthesis_or_raise(result, findings)
            errors = self._phase_errors(before, protected, "review")
            if not errors:
                # Even an all-rejected ledger cannot approve code it just changed.
                return completed and self._state() == before
            self.log.diagnostic(f"session=review-synthesis event=completion_rejected attempt={attempt} errors={errors!r}")
        raise RuntimeError("review synthesis did not complete its candidate: " + "; ".join(errors))

    def _run_synthesis(self, findings, *, recovery=None) -> bool:
        if self.task_worktrees is None:
            return self._execute_synthesis(findings, self._state(), self._phase_protected())
        original_protected = self._phase_protected()
        context = (self.task_worktrees.resume("review synthesis", recovery) if recovery
                   else self.task_worktrees.create("review synthesis"))
        with context as workspace:
            workspace.phase = "synthesis"
            workspace.phase_context = {"findings": findings}
            with self._use_task_workspace(workspace):
                protected = ({workspace.path / path.resolve().relative_to(workspace.repo_root): content
                              for path, content in original_protected.items()} if recovery else self._phase_protected())
                tree = self._git().tree_id(workspace.snapshot_commit)
                before = RepositoryState(workspace.snapshot_commit, tree, tree,
                                         workspace.artifact_signature)
                if recovery:
                    self._restore_phase_contract(protected)
                completed = self._execute_synthesis(findings, before, protected)
                changed = self._state() != before
                head = self._git().head_commit()
            if changed:
                workspace.promote(head)
                self._reviewed_state = None
            else:
                workspace.discard_verified()
            if self.checkpoint is not None:
                self.checkpoint.clear_blocked()
            return completed and not changed

    def run_finalize(self) -> None:
        if self._reviewed_state is not None:
            self._assert_state(self._reviewed_state, "state after review")
        if self.task_worktrees is None:
            before = self._state()
            result = self.finalize_executor.run(render(self.options.prompts.finalize, self._context()))
            self._assert_state(before, "finalize")
            self._validate_finalize_signal(result)
            errors = self._run_checks("finalize")
            if errors:
                raise RuntimeError("finalize validation failed: " + "; ".join(errors))
            return
        retries = max(0, self.options.task_completion_retries)
        for attempt in range(retries + 1):
            if attempt:
                self.log.event("completion_retry", phase="finalize", attempt=attempt)
            if self.dashboard is not None:
                self.dashboard.phase_started("finalize", "Verifying the reviewed result")
            self.log.section("finalize")
            repaired = self._finalize_candidate(may_repair=attempt < retries)
            if not repaired:
                self._assert_state(self._reviewed_state, "finalize verification")
                return
            # Every finalize edit is a repair candidate, never final approval.
            if self.checkpoint is not None:
                self.checkpoint.invalidate_from("review")
                self.checkpoint.mark_started("review")
            self.run_review()
            if self.checkpoint is not None:
                self.checkpoint.mark_completed("review", self.checkpoint.current_state())
        raise RuntimeError("finalize repair budget exhausted")

    @staticmethod
    def _validate_finalize_signal(result: ExecResult) -> None:
        if not result.ok:
            raise RuntimeError(describe_failure("gigacode finalize session", result))
        if result.signal == FINALIZE_FAILED:
            raise RuntimeError("finalize failed: " + result.output[-4000:])
        if result.signal != FINALIZE_DONE:
            raise RuntimeError("finalize did not report successful verification")

    def _finalize_candidate(self, *, recovery=None, may_repair: bool) -> bool:
        original_protected = self._phase_protected()
        context = (self.task_worktrees.resume("finalize", recovery) if recovery
                   else self.task_worktrees.create("finalize"))
        with context as workspace:
            workspace.phase = "finalize"
            with self._use_task_workspace(workspace):
                tree = self._git().tree_id(workspace.snapshot_commit)
                before = RepositoryState(workspace.snapshot_commit, tree, tree,
                                         workspace.artifact_signature)
                protected = ({workspace.path / path.resolve().relative_to(workspace.repo_root): content
                              for path, content in original_protected.items()} if recovery else self._phase_protected())
                result = self.finalize_executor.run(
                    render(self.options.prompts.finalize, self._context())
                    + "\nRunner verification boundary: inspect this fixed candidate without changing HEAD, "
                    "the index, tracked files, plan, non-ignored untracked files, or configured task artifacts. "
                    "Report required repairs; do not implement them during final verification.\n",
                    cwd=workspace.path,
                )
                signal_error = ""
                try:
                    self._validate_finalize_signal(result)
                except RuntimeError as exc:
                    signal_error = str(exc)
                changed = self._state() != before
                errors = self._phase_errors(before, protected, "finalize")
                if not changed and not errors and not signal_error:
                    accepted = True
                else:
                    accepted = False
                    if not may_repair:
                        raise RuntimeError("finalize requires repair after its retry budget: "
                                           + "; ".join(errors + [signal_error or "finalize modified the verified candidate"]))
                    reason = "\n".join(errors + [signal_error or "finalize modified the verified candidate"])
                    workspace.phase_context = {"failure": reason}
                    # Clean committed edits still need review; incomplete edits or
                    # failed checks first go through the repair executor here.
                    for correction in range(max(0, self.options.task_completion_retries) + 1):
                        if not errors and not signal_error:
                            break
                        self._restore_phase_contract(protected)
                        repair = self._run_synthesis_agent(
                            "Phase: repair the final verification candidate.\n"
                            "Fix the underlying deliverable, preserve the original plan and context, "
                            "run its checks and commit the correction. Do not change orchestration files.\n"
                            "Preserve useful prior work; remove only verified disposable outputs.\n"
                            "The candidate will undergo fresh review and final verification.\n"
                            "Untrusted verification diagnostics:\n" + reason
                            + ("\nOperator context: " + self.options.resume_note if self.options.resume_note else "")
                        )
                        if not repair.ok or repair.signal == TASK_FAILED:
                            raise RuntimeError(describe_failure("final verification repair", repair))
                        self._prefix_new_commits(before.head, "final verification repair")
                        signal_error = ""
                        errors = self._phase_errors(before, protected, "finalize")
                        reason = "\n".join(errors)
                        if not errors:
                            break
                    if errors:
                        raise RuntimeError("final verification repair incomplete: " + "; ".join(errors))
                changed = self._state() != before
                head = self._git().head_commit()
            if changed:
                workspace.promote(head)
                self._reviewed_state = None
            else:
                workspace.discard_verified()
            if self.checkpoint is not None:
                self.checkpoint.clear_blocked()
            return not accepted

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
            progress_file=self.log.snapshot_for_prompt(),
            default_branch=self.options.default_branch,
            jira_task=self.options.jira_task,
            plan_kind=self.options.plan_kind,
            plan_source=self.options.plan_source,
            plan_context_files=self.options.plan_context_files,
            resume_note=self.options.resume_note,
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

    def _task_completion_status(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
    ) -> TaskCompletionStatus:
        assert self.options.plan_file is not None
        label = f"task {self._task_label(selected_task)}"
        try:
            plan_after = self.options.plan_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return TaskCompletionStatus((f"{label} removed or made its plan unreadable",), False)
        if not task_plan_update_allowed(
            plan_before, plan_after, selected_task, plan_format=self.options.plan_kind,
        ):
            return TaskCompletionStatus(
                (f"{label} modified protected plan content: requirements, headings, "
                 "or tracking outside the selected section",),
                False,
            )
        changed_context = self._changed_plan_context(context_before)
        if changed_context:
            paths = ", ".join(self._display_path(path) for path in changed_context)
            return TaskCompletionStatus(
                (f"{label} modified read-only plan context: {paths}",), False,
            )
        completed_task = self._matching_task(self._parse_plan_file(), selected_task)
        errors = []
        if completed_task is None or not completed_task.complete:
            errors.append(f"{label} did not complete its selected plan section")
        if self._git().head_commit() == head_before:
            errors.append(f"{label} completed without creating a commit")
        new_dirty = self._uncommitted_paths() - dirty_before
        if new_dirty and not self.options.allow_dirty:
            paths = ", ".join(self._display_path(path) for path in sorted(new_dirty))
            errors.append(f"{label} left new uncommitted changes in the working tree: {paths}")
        return TaskCompletionStatus(tuple(errors))

    def _validate_completed_task_iteration(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
        completion_retries: int = 0,
    ) -> None:
        status = self._task_completion_status(
            selected_task, plan_before, context_before, head_before, dirty_before,
        )
        if status.errors:
            errors = list(status.errors)
            if completion_retries:
                errors[0] += (
                    f" after {completion_retries} automatic completion "
                    f"{'retry' if completion_retries == 1 else 'retries'}"
                )
            raise RuntimeError("; ".join(errors))
        new_dirty = self._uncommitted_paths() - dirty_before
        if new_dirty:
            self._log_allowed_dirty("task", new_dirty, selected_task)

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
                self._restore_plan_snapshot_if_safe(
                    plan_before,
                    selected_task,
                    head_before,
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
            if self._task_iteration_completed_cleanly(
                selected_task,
                plan_before,
                context_before,
                head_before,
                dirty_before,
            ):
                self.log.diagnostic(
                    "session=task event=signal_conflict "
                    f"task={task_label!r} signal=TASK_FAILED "
                    "action=accept_committed_completion"
                )
            else:
                self._restore_plan_snapshot_if_safe(
                    plan_before,
                    selected_task,
                    head_before,
                    reason="task_failed",
                )
                raise RuntimeError("task failed")

    def _can_retry_incomplete_task(
        self,
        selected_task: Task,
        plan_before: str,
        context_before: dict[Path, bytes],
        head_before: str,
        dirty_before: set[Path],
    ) -> bool:
        status = self._task_completion_status(
            selected_task, plan_before, context_before, head_before, dirty_before,
        )
        if not status.repairable and self._active_cwd is None:
            self.log.diagnostic(
                "session=task event=completion_retry_rejected "
                f"task={self._task_label(selected_task)!r} reason={'; '.join(status.errors)!r}"
            )
        return not status.complete and (status.repairable or self._active_cwd is not None)

    def _can_restore_contract_for_retry(
        self, selected_task: Task, plan_before: str, context_before: dict[Path, bytes],
        head_before: str, dirty_before: set[Path], retries_left: int,
    ) -> bool:
        return (
            retries_left > 0 and self._active_cwd is not None
            and not self._task_completion_status(
                selected_task, plan_before, context_before, head_before, dirty_before,
            ).repairable
        )

    def _restore_task_contract(self, plan_before: str, context_before: dict[Path, bytes]) -> None:
        assert self._active_cwd is not None and self.options.plan_file is not None
        changes = {self.options.plan_file: plan_before.encode("utf-8")}
        for path in self._changed_plan_context(context_before):
            changes[path] = context_before.get(path)
        for path in changes:
            if path.is_symlink() or not path.resolve().is_relative_to(self._active_cwd.resolve()):
                raise GitError(f"cannot restore protected content through a symlink or external path: {path}")
            if path.exists() and not path.is_file():
                raise GitError(f"protected content was replaced with a directory: {path}")
        for path, content in changes.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.log.diagnostic("session=task event=contract_restored action=revalidate_original_requirements")

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

        if self._git().head_commit() != head_before:
            status = self._task_completion_status(
                selected_task, plan_before, context_before, head_before, dirty_before,
            )
            if not status.repairable:
                self.log.diagnostic(
                    "session=task event=retry_guard_rejected "
                    f"task={self._task_label(selected_task)!r} reason=protected_plan_modified"
                )
                return False
        self._restore_plan_snapshot_if_safe(
            plan_before,
            selected_task,
            head_before,
            reason="retry",
        )
        return True

    def _restore_plan_snapshot_if_safe(
        self,
        plan_before: str,
        selected_task: Task,
        head_before: str,
        *,
        reason: str,
    ) -> None:
        if self._git().head_commit() != head_before:
            self.log.diagnostic(
                "session=task event=plan_snapshot_restore_skipped "
                f"task={self._task_label(selected_task)!r} reason={reason!r} "
                "cause=head_changed"
            )
            return
        self._restore_plan_snapshot(plan_before, selected_task, reason=reason)

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
        return self._task_completion_status(
            selected_task, plan_before, context_before, head_before, dirty_before,
        ).complete

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

        if self._active_cwd is not None:
            return (
                f"{describe_failure('gigacode task session', result)}; automatic retries "
                f"were exhausted while task {self._task_label(selected_task)} still lacked "
                f"a clean committed completion ({'; '.join(state)}). "
                "The isolated task result will be preserved before cleanup."
            )
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
        return GitService(self._active_cwd or Path("."))

    def _run_task_agent(
        self,
        prompt: str,
        *,
        retry_guard: Callable[[ExecResult], bool],
    ) -> ExecResult:
        if self._active_cwd is None:
            return self.executor.run(prompt, retry_guard=retry_guard)
        return self.executor.run(
            prompt,
            retry_guard=retry_guard,
            cwd=self._active_cwd,
        )

    @contextmanager
    def _use_task_workspace(self, workspace: TaskWorktree) -> Iterator[None]:
        previous_options = self.options
        previous_cwd = self._active_cwd

        def remap(path: Optional[Path]) -> Optional[Path]:
            if path is None:
                return None
            try:
                relative = path.resolve().relative_to(workspace.repo_root.resolve())
            except ValueError:
                return path
            return workspace.path / relative

        self.options = replace(
            self.options,
            plan_file=remap(self.options.plan_file),
            plan_source=remap(self.options.plan_source),
            plan_context_files=tuple(
                remapped
                for path in self.options.plan_context_files
                if (remapped := remap(path)) is not None
            ),
            # The isolated workspace starts clean. Any leftover there is new task
            # state and cannot be promoted transactionally.
            allow_dirty=False,
        )
        self._active_cwd = workspace.path
        try:
            yield
        finally:
            self._active_cwd = previous_cwd
            self.options = previous_options

    def _run_single_review_agent(
        self,
        name: str,
        render_prompt: Callable[[PromptContext], str],
        *,
        base_ref: Optional[str] = None,
    ) -> ExecResult:
        if self.review_worktrees is None:
            return self.review_agent_executor.run(render_prompt(self._context()))

        with self.review_worktrees.create(
            [name],
            base_ref=base_ref or self.options.default_branch,
        ) as worktrees:
            worktree = worktrees.paths[name]
            context = self._context_for_review_worktree(
                worktree,
                worktrees.repo_root,
                worktrees.review_manifest,
            )
            before = repository_state(GitService(worktree), artifact_paths=self._artifact_paths())
            result = self.review_agent_executor.run(
                render_prompt(context),
                cwd=worktree,
            )
            if repository_state(GitService(worktree), artifact_paths=self._artifact_paths()) != before:
                raise RuntimeError("read-only review modified its isolated candidate")
            return result

    def _run_parallel_review_agents(
        self,
        agent_names: tuple[str, ...],
        *,
        followup_scope: Optional[FollowupReviewScope] = None,
    ) -> dict[str, ExecResult]:
        agents = {name: REVIEW_AGENTS[name] for name in agent_names}
        if self.review_worktrees is None:
            context = self._context()
            prompts = {
                name: render_review_agent_prompt(
                    self.options.prompts.review_agent,
                    name,
                    focus,
                    context,
                    decision_memory=self._review_decision_memory_snapshot(),
                    followup_scope=followup_scope,
                )
                for name, focus in agents.items()
            }
            results = self.review_agent_executor.run_batch(prompts)
            return self._retry_crashed_review_agents(prompts, results)

        packet_base_ref = (
            followup_scope.base_commit
            if followup_scope is not None and followup_scope.base_commit
            else self.options.default_branch
        )
        with self.review_worktrees.create(
            agents,
            base_ref=packet_base_ref,
        ) as worktrees:
            prompts = {
                name: render_review_agent_prompt(
                    self.options.prompts.review_agent,
                    name,
                    focus,
                    self._context_for_review_worktree(
                        worktrees.paths[name],
                        worktrees.repo_root,
                        worktrees.review_manifest,
                    ),
                    decision_memory=self._review_decision_memory_snapshot(),
                    followup_scope=followup_scope,
                )
                for name, focus in agents.items()
            }
            before = {name: repository_state(GitService(path), artifact_paths=self._artifact_paths())
                      for name, path in worktrees.paths.items()}
            results = self.review_agent_executor.run_batch(
                prompts,
                workdirs=worktrees.paths,
            )
            results = self._retry_crashed_review_agents(
                prompts,
                results,
                workdirs=worktrees.paths,
            )
            for name, path in worktrees.paths.items():
                if repository_state(GitService(path), artifact_paths=self._artifact_paths()) != before[name]:
                    raise RuntimeError(f"read-only reviewer {name} modified its isolated candidate")
            return results

    def _retry_crashed_review_agents(
        self,
        prompts: dict[str, str],
        results: dict[str, ExecResult],
        *,
        workdirs: Optional[dict[str, Path]] = None,
    ) -> dict[str, ExecResult]:
        recovered = dict(results)
        crashed = [
            name
            for name in prompts
            if name in recovered and recovered[name].dependency_crash
        ]
        for name in crashed:
            self.log.diagnostic(
                "session=review event=sequential_crash_retry "
                f"agent={name!r} reason=dependency_crash"
            )
            if workdirs is None:
                recovered[name] = self.review_agent_executor.run(prompts[name])
            else:
                recovered[name] = self.review_agent_executor.run(
                    prompts[name],
                    cwd=workdirs[name],
                )
        return recovered

    def _context_for_review_worktree(
        self,
        worktree: Path,
        repo_root: Path,
        review_manifest: Path,
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
            # The bounded prompt snapshot is runner-owned and intentionally
            # remains outside disposable review worktrees.
            progress_file=context.progress_file,
            default_branch=context.default_branch,
            jira_task=context.jira_task,
            plan_kind=context.plan_kind,
            plan_source=remap(context.plan_source),
            plan_context_files=tuple(
                remapped
                for path in context.plan_context_files
                if (remapped := remap(path)) is not None
            ),
            review_manifest=review_manifest,
            resume_note=context.resume_note,
        )

    def _uncommitted_paths(self) -> set[Path]:
        git = self._git()
        repo_root = git.repo_root()
        ignored = {
            self.options.progress_file.resolve(),
            statistics_path(self.options.progress_file).resolve(),
            self.log.prompt_context_file.resolve(),
        }
        if self.checkpoint is not None:
            ignored.add(self.checkpoint.path.resolve())
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
                decision_memory=self._review_decision_memory_snapshot(),
            )
        except ReviewOutputError as exc:
            raise RuntimeError(f"invalid structured review output: {exc}") from exc

    def _accept_review_synthesis_or_raise(
        self,
        result: ExecResult,
        findings: dict[str, str],
    ) -> bool:
        self._latest_review_verification_records = ()
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
            recovery = self._run_synthesis_agent(
                render_review_synthesis_recovery_prompt(
                    self.options.prompts.review_synthesis,
                    findings,
                    self._context(),
                    result.output,
                    validation_error,
                    decision_memory=self._review_decision_memory_snapshot(),
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
                    f"action=focused_verification reason={reason!r}"
                )
                self._latest_review_verification_records = tuple(
                    build_review_decision_records(
                        identified,
                        [
                            SynthesisDecision(
                                finding_id=item.finding_id,
                                decision="confirmed",
                                reason=(
                                    "Synthesis reconciliation failed; verify whether "
                                    f"the original finding remains: {reason}"
                                ),
                            )
                            for item in identified
                        ],
                    )
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
            audit = self._run_synthesis_agent(
                render_review_synthesis_blocked_audit_prompt(
                    self.options.prompts.review_synthesis,
                    findings,
                    self._context(),
                    result.output,
                    blocked_ids,
                    decision_memory=self._review_decision_memory_snapshot(),
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

        decision_records = build_review_decision_records(identified, decisions)
        self.log.event("review_decisions", source="agent_report",
                       findings=[asdict(item) for item in identified],
                       decisions=[asdict(item) for item in decisions],
                       records=[asdict(item) for item in decision_records],
                       head=self._git().head_commit())
        self._latest_review_verification_records = tuple(
            record
            for record in decision_records
            if record.decision in {"fixed", "confirmed"}
        )
        self._remember_review_decisions(identified, decisions)

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

    def _review_decision_memory_snapshot(self) -> tuple[ReviewDecisionRecord, ...]:
        return tuple(self._review_decision_memory.values())

    def _remember_review_decisions(
        self,
        findings: list[IdentifiedReviewFinding],
        decisions: list[SynthesisDecision],
    ) -> None:
        added = 0
        removed = 0
        for record in build_review_decision_records(findings, decisions):
            previous = self._review_decision_memory.pop(record.fingerprint, None)
            if record.decision in {"fixed", "rejected"}:
                self._review_decision_memory[record.fingerprint] = record
                added += 1
            elif previous is not None:
                removed += 1

        trimmed = 0
        while len(self._review_decision_memory) > MAX_REVIEW_DECISION_MEMORY:
            oldest = next(iter(self._review_decision_memory))
            del self._review_decision_memory[oldest]
            trimmed += 1

        self.log.diagnostic(
            "session=review event=decision_memory_updated "
            f"accepted={added} removed={removed} trimmed={trimmed} "
            f"retained={len(self._review_decision_memory)}"
        )

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
    elif result.dependency_crash:
        parts.append("crashed in an external dependency")
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
