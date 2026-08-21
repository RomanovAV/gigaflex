from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
from typing import Optional

from .config import (
    init_global_config,
    init_global_prompt_templates,
    init_project_config,
    init_project_prompt_templates,
    load_config,
)
from .checkpoint import RunCheckpoint, checkpoint_path
from .dashboard import ProgressDashboard, dashboard_paths
from .executor import GigaCodeExecutor
from .git import (
    BranchBaseline,
    GitError,
    GitService,
    ReviewWorktreeManager,
    TaskWorktreeManager,
    branch_name_from_plan,
    jira_branch_name,
    move_plan_to_completed,
)
from .planner import clean_plan_output, next_plan_path
from .plan import (
    PlanSource,
    parse_plan_file,
    resolve_markdown_plan,
    resolve_openspec_change,
)
from .progress import ProgressLog
from .prompts import load_prompt_templates, render_make_plan, render_plan_skill
from .runner import RunOptions, Runner
from .skills import (
    install_planning_skill,
    install_superpowers_converter_skill,
    planning_skill_installed,
    planning_skill_path,
)
from .stats import RunStatistics, statistics_path


JIRA_TASK_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]*-\d+|\d+)$")
BRANCH_DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gigaflex")
    parser.add_argument("plan_file", nargs="?", help="path to markdown plan file")
    parser.add_argument(
        "--openspec",
        type=Path,
        metavar="CHANGE_DIR",
        help="execute a local OpenSpec change directory using its tasks.md",
    )
    parser.add_argument("--config", type=Path, help="config file path")
    parser.add_argument("--init", action="store_true", help="create local .gigaflex config")
    parser.add_argument(
        "--init-prompts",
        action="store_true",
        help="create local .gigaflex prompt templates that override global prompts",
    )
    parser.add_argument("--init-git", action="store_true", help="run git init first when current directory is not a git repository")
    parser.add_argument(
        "--install-planning-skill",
        action="store_true",
        help="install the bundled planning skill for GigaCode",
    )
    parser.add_argument(
        "--install-superpowers-converter-skill",
        action="store_true",
        help="install the bundled Superpowers-to-GigaFlex conversion skill for GigaCode",
    )
    parser.add_argument(
        "--force-skill-install",
        action="store_true",
        help="overwrite an existing modified bundled skill",
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        help="GigaCode skills directory, default: ~/.gigacode/skills",
    )
    parser.add_argument("--plan", help="create a markdown execution plan for this request")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="create a plan non-interactively with the one-shot plan prompt",
    )
    parser.add_argument("--gigacode-command", help="command to run, default: gigacode")
    parser.add_argument(
        "--gigacode-arg",
        action="append",
        default=[],
        help="extra arg for all gigacode invocations; repeatable",
    )
    parser.add_argument("--plan-model", help="GigaCode model for plan creation; falls back to task model")
    parser.add_argument("--task-model", help="GigaCode model for task execution")
    parser.add_argument("--review-model", help="GigaCode model for read-only review agents; falls back to task model")
    parser.add_argument("--finalize-model", help="GigaCode model for finalize; falls back to review/task model")
    parser.add_argument("--tasks-only", action="store_true", help="run task phase only")
    parser.add_argument("--review", action="store_true", help="skip tasks and run review phase")
    parser.add_argument("--max-iterations", type=int, help="maximum task iterations")
    parser.add_argument(
        "--review-iterations",
        type=int,
        help=(
            "maximum review repair cycles; one terminal verification pass runs "
            "after the last repair"
        ),
    )
    parser.add_argument("--session-timeout", type=int, help="seconds before killing one gigacode session")
    parser.add_argument("--idle-timeout", type=int, help="seconds of no output before killing one gigacode session")
    parser.add_argument(
        "--retry-count",
        type=int,
        help="retry failed gigacode sessions and incomplete task completions N times",
    )
    parser.add_argument("--retry-delay", type=float, help="seconds between gigacode retries")
    parser.add_argument("--retry-pattern", action="append", default=[], help="transient error text to treat as retryable")
    parser.add_argument("--rate-limit-pattern", action="append", default=[], help="rate-limit text to detect in failed sessions")
    parser.add_argument("--wait-on-rate-limit", type=float, help="seconds to wait before retrying a rate-limited session")
    parser.add_argument("--review-workers", type=int, help="maximum parallel review agents")
    parser.add_argument(
        "--default-branch",
        help="legacy alias for --base-ref",
    )
    parser.add_argument(
        "--base-ref",
        help="branch or git ref to capture as the immutable review base",
    )
    parser.add_argument("--branch", help="branch to create/switch to before running a plan")
    parser.add_argument("--no-branch", action="store_true", help="do not create/switch branches")
    parser.add_argument(
        "--jira-task",
        help=(
            "Jira task key or number; enforces feature/<task>-... branch names "
            "and prefixes new commit messages with the task"
        ),
    )
    parser.add_argument("--worktree", action="store_true", help="run the plan in an isolated git worktree")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow uncommitted changes at startup and between run phases",
    )
    parser.add_argument("--no-move-plan", action="store_true", help="do not move completed plan to completed/")
    parser.add_argument("--no-commit-plan", action="store_true", help="do not commit newly created plans")
    finalize_group = parser.add_mutually_exclusive_group()
    finalize_group.add_argument(
        "--finalize",
        action="store_true",
        dest="finalize",
        default=None,
        help="run finalize prompt after review (enabled by default)",
    )
    finalize_group.add_argument(
        "--no-finalize",
        action="store_false",
        dest="finalize",
        help="skip the finalize prompt after review",
    )
    parser.add_argument(
        "--no-parallel-review",
        action="store_true",
        help="use one read-only reviewer before synthesis instead of parallel agents",
    )
    parser.add_argument("--dry-run", action="store_true", help="print prompts instead of invoking gigacode")
    return parser


def should_auto_init(args: argparse.Namespace) -> bool:
    if args.dry_run or args.review or args.config is not None or Path(".gigaflex/config").exists():
        return False
    if args.plan:
        return True
    if args.openspec:
        return args.openspec.exists()
    return bool(args.plan_file and Path(args.plan_file).exists())


def normalize_jira_task(value: str) -> str:
    normalized = value.strip().upper()
    if not JIRA_TASK_RE.fullmatch(normalized):
        raise ValueError("Jira task must be a number or key like PROJ-123")
    return normalized


def jira_commit_message(jira_task: str, message: str) -> str:
    return f"{jira_task} {message}" if jira_task else message


def plan_commit_message(plan_path: Path, jira_task: str = "") -> str:
    return jira_commit_message(jira_task, f"docs: add plan {plan_path.stem}")


def completed_plan_commit_message(plan_path: Path, jira_task: str = "") -> str:
    return jira_commit_message(jira_task, f"docs: complete plan {plan_path.stem}")


def branch_for_plan(
    plan_path: Path,
    explicit_branch: Optional[str],
    jira_task: str,
) -> str:
    if not jira_task:
        return explicit_branch or branch_name_from_plan(plan_path)
    if explicit_branch:
        validate_jira_branch_name(explicit_branch, jira_task)
        return explicit_branch
    return jira_branch_name(plan_path, jira_task)


def validate_jira_branch_name(branch: str, jira_task: str) -> None:
    required_prefix = f"feature/{jira_task}-"
    description = branch.removeprefix(required_prefix)
    if (
        not branch.startswith(required_prefix)
        or not description
        or not BRANCH_DESCRIPTION_RE.fullmatch(description)
    ):
        raise ValueError(
            "with --jira-task, --branch must start with "
            f"{required_prefix} and include a safe description"
        )


def select_run_baseline(
    git: GitService,
    requested_ref: str,
    execution_branch: str,
    *,
    review_only: bool,
    allow_unborn: bool = False,
    require_stored_for_existing: bool = False,
) -> BranchBaseline:
    if requested_ref:
        return BranchBaseline(
            base_branch=requested_ref,
            base_commit=git.resolve_commit(requested_ref),
        )

    execution_branch_exists = bool(
        execution_branch and git.branch_exists(execution_branch)
    )
    if execution_branch_exists:
        stored = git.branch_baseline(execution_branch)
        if stored is not None:
            return stored

    if review_only or (
        require_stored_for_existing
        and execution_branch_exists
        and git.current_branch() == execution_branch
    ):
        branch_detail = f" for branch {execution_branch}" if execution_branch else ""
        raise GitError(
            "no GigaFlex review base is stored"
            f"{branch_detail}; pass --base-ref REF once to select it"
        )

    commit = git.head_commit()
    if not commit:
        if allow_unborn:
            return BranchBaseline(
                base_branch=git.current_branch() or "unborn HEAD",
                base_commit="HEAD",
            )
        raise GitError("cannot capture a run base before the repository has a commit")
    return BranchBaseline(
        base_branch=git.current_branch() or "detached HEAD",
        base_commit=commit,
    )


def ensure_baseline_is_ancestor(
    git: GitService,
    baseline: BranchBaseline,
    descendant: str,
) -> None:
    if git.is_ancestor(baseline.base_commit, descendant):
        return
    raise GitError(
        "GigaFlex review base "
        f"{baseline.base_branch} ({baseline.base_commit}) is not an ancestor of "
        f"{descendant}; pass --base-ref REF to replace it"
    )


def add_gigacode_args(base_args: list[str], extra_args: list[str]) -> list[str]:
    for index, arg in enumerate(base_args):
        if "{prompt}" in arg:
            insertion_index = index
            if (
                arg == "{prompt}"
                and index > 0
                and base_args[index - 1]
                in {"-p", "--prompt", "-i", "--prompt-interactive"}
            ):
                insertion_index = index - 1
            return [
                *base_args[:insertion_index],
                *extra_args,
                *base_args[insertion_index:],
            ]
    return [*base_args, *extra_args]


def should_use_interactive_plan(args: argparse.Namespace) -> bool:
    return bool(
        args.plan
        and not args.quick
        and not args.dry_run
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def find_interactively_created_plan(
    expected_path: Path,
    existing_paths: set[Path],
) -> Path:
    if expected_path.is_file():
        return expected_path
    created = sorted(
        path
        for path in expected_path.parent.glob("*.md")
        if path not in existing_paths
    )
    if len(created) == 1:
        return created[0]
    if not created:
        raise RuntimeError(
            f"interactive planning finished without creating the expected plan: {expected_path}"
        )
    joined = ", ".join(str(path) for path in created)
    raise RuntimeError(f"interactive planning created multiple plan files: {joined}")


def main(argv: Optional[list[str]] = None) -> int:
    local_fallback_written: list[Path] = []
    local_fallback_started_clean = False
    try:
        init_global_config()
        init_global_prompt_templates()
    except OSError:
        try:
            fallback_git = GitService(Path("."))
            local_fallback_started_clean = (
                fallback_git.is_repo() and not fallback_git.is_dirty()
            )
            local_fallback_written.extend(init_project_config())
            local_fallback_written.extend(init_project_prompt_templates())
        except OSError as exc:
            print(
                f"warning: could not initialize global or local gigaflex files: {exc}",
                file=sys.stderr,
            )

    args = build_parser().parse_args(argv)
    if args.jira_task:
        try:
            args.jira_task = normalize_jira_task(args.jira_task)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        args.jira_task = ""
    if args.openspec and args.plan_file:
        print("error: --openspec cannot be combined with a markdown plan file", file=sys.stderr)
        return 2
    if args.openspec and args.plan:
        print("error: --openspec cannot be combined with --plan", file=sys.stderr)
        return 2
    if args.openspec and args.review:
        print("error: --openspec cannot be combined with --review", file=sys.stderr)
        return 2
    install_skill_requested = (
        args.install_planning_skill or args.install_superpowers_converter_skill
    )
    if args.force_skill_install and not install_skill_requested:
        print(
            "error: --force-skill-install requires a bundled skill install command",
            file=sys.stderr,
        )
        return 2
    if args.quick and not args.plan:
        print("error: --quick requires --plan", file=sys.stderr)
        return 2
    if args.base_ref and args.default_branch:
        print("error: --base-ref and --default-branch cannot be used together", file=sys.stderr)
        return 2
    if args.jira_task and not (args.plan or args.plan_file or args.openspec or args.review):
        print("error: --jira-task requires a plan file, --openspec, --plan, or --review", file=sys.stderr)
        return 2
    if args.jira_task and args.no_branch and not args.review:
        print("error: --jira-task requires branch creation; remove --no-branch", file=sys.stderr)
        return 2
    if args.jira_task and args.branch and (args.plan or args.plan_file or args.openspec):
        try:
            validate_jira_branch_name(args.branch, args.jira_task)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.init or args.init_prompts:
        written = local_fallback_written.copy()
        if args.init:
            written.extend(init_project_config())
        if args.init_prompts:
            written.extend(init_project_prompt_templates())
        written = list(dict.fromkeys(written))
        if written:
            print("initialized gigaflex files:")
            for path in written:
                print(f"- {path}")
        else:
            print("requested gigaflex files already initialized")
        return 0

    if should_use_interactive_plan(args):
        planning_cfg = load_config(args.config)
        if args.skill_dir:
            planning_cfg.gigacode_skills_dir = args.skill_dir.expanduser()
        if not planning_skill_installed(planning_cfg.gigacode_skills_dir):
            expected_skill = planning_skill_path(planning_cfg.gigacode_skills_dir)
            print(f"error: GigaCode planning skill not found: {expected_skill}", file=sys.stderr)
            install_command = "gigaflex --install-planning-skill"
            if args.skill_dir:
                install_command += f" --skill-dir {planning_cfg.gigacode_skills_dir}"
            print(f"install it with: {install_command}", file=sys.stderr)
            print("or use --quick for non-interactive plan creation", file=sys.stderr)
            return 2

    auto_init_written = local_fallback_written.copy()
    auto_init_started_clean = local_fallback_started_clean
    if should_auto_init(args):
        if not auto_init_written:
            auto_init_git = GitService(Path("."))
            auto_init_started_clean = auto_init_git.is_repo() and not auto_init_git.is_dirty()
        project_config_written = init_project_config()
        auto_init_written.extend(
            path for path in project_config_written if path not in auto_init_written
        )
        if project_config_written:
            print(f"initialized local gigaflex config: {Path('.gigaflex/config')}")

    if args.init_git and not args.dry_run:
        git = GitService(Path("."))
        if git.init_repo_if_missing():
            print("initialized git repository")
        if not git.has_commits() and git.commit_all_if_dirty(
            jira_commit_message(args.jira_task, "chore: initialize repository")
        ):
            print("committed initial repository state")
    if (
        args.init_git
        and not args.plan
        and not args.plan_file
        and not args.openspec
        and not args.review
        and not args.install_planning_skill
        and not args.install_superpowers_converter_skill
    ):
        return 0

    cfg = load_config(args.config)
    prompts = load_prompt_templates(cfg.prompt_dirs)

    if args.gigacode_command:
        cfg.gigacode_command = args.gigacode_command
    if args.skill_dir:
        cfg.gigacode_skills_dir = args.skill_dir.expanduser()
    if args.gigacode_arg:
        cfg.gigacode_args = add_gigacode_args(
            cfg.resolved_args,
            args.gigacode_arg,
        )
        cfg.gigacode_interactive_args = add_gigacode_args(
            cfg.resolved_interactive_args,
            args.gigacode_arg,
        )
    if args.plan_model:
        cfg.plan_model = args.plan_model
    if args.task_model:
        cfg.task_model = args.task_model
    if args.review_model:
        cfg.review_model = args.review_model
    if args.finalize_model:
        cfg.finalize_model = args.finalize_model
    if args.max_iterations is not None:
        cfg.max_iterations = args.max_iterations
    if args.review_iterations is not None:
        cfg.review_iterations = args.review_iterations
    if args.session_timeout is not None:
        cfg.session_timeout = args.session_timeout
    if args.idle_timeout is not None:
        cfg.idle_timeout = args.idle_timeout
    if args.retry_count is not None:
        cfg.retry_count = args.retry_count
    if args.retry_delay is not None:
        cfg.retry_delay = args.retry_delay
    if args.retry_pattern:
        cfg.retry_patterns = [*cfg.retry_patterns, *args.retry_pattern]
    if args.rate_limit_pattern:
        cfg.rate_limit_patterns = [*cfg.rate_limit_patterns, *args.rate_limit_pattern]
    if args.wait_on_rate_limit is not None:
        cfg.wait_on_rate_limit = args.wait_on_rate_limit
    if args.review_workers is not None:
        cfg.review_workers = args.review_workers
    if args.default_branch:
        cfg.default_branch = args.default_branch
    if args.base_ref:
        cfg.default_branch = args.base_ref
    if args.no_branch:
        cfg.create_branch = False
    if args.jira_task and not args.review:
        cfg.create_branch = True
    if args.worktree:
        cfg.worktree = True
    if args.allow_dirty:
        cfg.allow_dirty = True
    if args.no_move_plan:
        cfg.move_plan_on_completion = False
    if args.no_commit_plan:
        cfg.commit_plan_on_creation = False
    if args.finalize is not None:
        cfg.finalize_enabled = args.finalize

    requested_base_ref = cfg.default_branch

    if install_skill_requested:
        installers = []
        if args.install_planning_skill:
            installers.append(("planning", install_planning_skill))
        if args.install_superpowers_converter_skill:
            installers.append(("superpowers-to-gigaflex", install_superpowers_converter_skill))
        for name, installer in installers:
            try:
                skill_path, written = installer(
                    cfg.gigacode_skills_dir,
                    force=args.force_skill_install,
                )
            except FileExistsError as exc:
                print(f"error: {exc}", file=sys.stderr)
                print("re-run with --force-skill-install to overwrite it", file=sys.stderr)
                return 1
            except OSError as exc:
                print(f"error: could not install {name} skill: {exc}", file=sys.stderr)
                return 1
            if written:
                print(f"installed {name} skill: {skill_path}")
            else:
                print(f"{name} skill already installed: {skill_path}")
        return 0

    if args.plan:
        progress_file = cfg.progress_dir / "progress-plan.txt"
        log = ProgressLog(progress_file)
        interactive = should_use_interactive_plan(args)
        plan_path = next_plan_path(cfg.plans_dir, args.plan)
        existing_plan_paths = set(cfg.plans_dir.glob("*.md"))
        executor = GigaCodeExecutor(
            command=cfg.gigacode_command,
            args=(
                cfg.args_for_interactive_plan()
                if interactive
                else cfg.args_for_phase("plan")
            ),
            timeout=cfg.session_timeout,
            idle_timeout=cfg.idle_timeout,
            retry_count=cfg.retry_count,
            retry_delay=cfg.retry_delay,
            retry_patterns=cfg.retry_patterns,
            rate_limit_patterns=cfg.rate_limit_patterns,
            wait_on_rate_limit=cfg.wait_on_rate_limit,
            max_workers=cfg.review_workers,
            output=log.stream,
            diagnostic=log.diagnostic,
            name="plan",
        )
        prompt = (
            render_plan_skill(prompts.plan_skill, args.plan, plan_path)
            if interactive
            else render_make_plan(prompts.make_plan, args.plan)
        )
        if args.dry_run:
            log.section("make plan prompt")
            log.stream(prompt)
            log.stream("\n")
            print(f"progress log: {progress_file}")
            return 0
        try:
            log.section("make plan")
            if cfg.create_branch and args.jira_task:
                git = GitService(Path("."))
                if git.is_repo():
                    branch = branch_for_plan(plan_path, args.branch, args.jira_task)
                    baseline = select_run_baseline(
                        git,
                        requested_base_ref,
                        branch,
                        review_only=False,
                        require_stored_for_existing=True,
                    )
                    if git.branch_exists(branch):
                        ensure_baseline_is_ancestor(git, baseline, branch)
                    git.switch_or_create_branch(branch, baseline.base_commit)
                    git.set_branch_baseline(branch, baseline)
                    log.write(f"branch: {branch}\n")
                    log.write(
                        "review base: "
                        f"{baseline.base_branch} ({baseline.base_commit})\n"
                    )
            log.write(f"gigacode command: {executor.command_line()}\n")
            if interactive:
                log.write(f"interactive plan target: {plan_path}\n")
                print(f"starting interactive GigaCode planning session for: {plan_path}")
                print("exit GigaCode after the planning skill creates the plan file")
                result = executor.run_interactive(prompt)
            else:
                result = executor.run(prompt)
            if not result.ok:
                raise RuntimeError(f"gigacode plan session exited with status {result.returncode}")
            if interactive:
                created_path = find_interactively_created_plan(plan_path, existing_plan_paths)
                if created_path != plan_path:
                    log.write(
                        f"interactive skill used a different plan path: {created_path}\n"
                    )
                plan_path = created_path
            else:
                plan_path.write_text(clean_plan_output(result.output), encoding="utf-8")
            log.write(f"created plan: {plan_path}\n")
            if cfg.commit_plan_on_creation:
                git = GitService(Path("."))
                if git.is_repo():
                    message = plan_commit_message(plan_path, args.jira_task)
                    if auto_init_written:
                        git.commit_paths([*auto_init_written, plan_path], message)
                    else:
                        git.commit_file(plan_path, message)
                    log.write(f"committed plan: {message}\n")
                else:
                    log.write("skipped plan commit: not inside a git repository\n")
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"created plan: {plan_path}")
        print(f"progress log: {progress_file}")
        return 0

    plan_source: Optional[PlanSource] = None
    try:
        if args.openspec:
            plan_source = resolve_openspec_change(args.openspec)
        elif args.plan_file:
            plan_source = resolve_markdown_plan(Path(args.plan_file))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if plan_source is None and not args.review:
        print("error: a plan file or --openspec is required unless --review is used", file=sys.stderr)
        return 2

    plan_file = plan_source.checklist_path if plan_source else None
    plan_identity_path = plan_source.source_path if plan_source else None

    git = GitService(Path("."))
    worktree_path: Optional[Path] = None
    run_baseline: Optional[BranchBaseline] = None
    if not args.dry_run:
        try:
            git.ensure_repo()
            starting_branch = git.current_branch()
            execution_branch = starting_branch
            if plan_identity_path is not None and not args.review and (
                cfg.worktree or cfg.create_branch
            ):
                execution_branch = branch_for_plan(
                    plan_identity_path,
                    args.branch,
                    args.jira_task,
                )
            run_baseline = select_run_baseline(
                git,
                requested_base_ref,
                execution_branch,
                review_only=args.review,
                allow_unborn=args.tasks_only,
                require_stored_for_existing=bool(
                    plan_identity_path is not None
                    and not args.review
                    and (cfg.worktree or cfg.create_branch)
                ),
            )
            if args.review:
                ensure_baseline_is_ancestor(git, run_baseline, "HEAD")
                if args.jira_task:
                    try:
                        validate_jira_branch_name(starting_branch, args.jira_task)
                    except ValueError as exc:
                        raise GitError(
                            "current branch must follow Jira naming policy for --jira-task: "
                            f"{exc}"
                        ) from exc
                if requested_base_ref and starting_branch:
                    git.set_branch_baseline(starting_branch, run_baseline)
            elif execution_branch and git.branch_exists(execution_branch):
                ensure_baseline_is_ancestor(git, run_baseline, execution_branch)
            ignored_dirty_paths = auto_init_written if auto_init_started_clean else []
            git.ensure_clean(cfg.allow_dirty, ignored_dirty_paths)
            if cfg.worktree and plan_source is not None and not args.review:
                assert plan_identity_path is not None
                branch = execution_branch
                if not git.has_commits():
                    raise GitError(
                        "--worktree requires an initial commit; commit the repository state first"
                    )
                repo_root = git.repo_root()
                try:
                    source_relative = plan_identity_path.relative_to(repo_root)
                except ValueError as exc:
                    raise GitError(
                        f"plan source must be inside the git repository for --worktree: {plan_identity_path}"
                    ) from exc
                worktree_path = git.ensure_worktree(
                    branch,
                    run_baseline.base_commit,
                )
                git.set_branch_baseline(branch, run_baseline)
                worktree_source = worktree_path / source_relative
                if not worktree_source.exists():
                    raise GitError(
                        f"plan source is not available in worktree; commit it first: {source_relative}"
                    )
                try:
                    plan_source = (
                        resolve_openspec_change(worktree_source)
                        if plan_source.is_openspec
                        else resolve_markdown_plan(worktree_source)
                    )
                except ValueError as exc:
                    raise GitError(str(exc)) from exc
                plan_file = plan_source.checklist_path
                plan_identity_path = plan_source.source_path
                os.chdir(worktree_path)
                git = GitService(Path("."))
            elif cfg.create_branch and plan_identity_path is not None and not args.review:
                branch = execution_branch
                git.switch_or_create_branch(branch, run_baseline.base_commit)
                if git.has_commits():
                    git.set_branch_baseline(branch, run_baseline)
            cfg.default_branch = run_baseline.base_commit
        except GitError as exc:
            hint = "; pass --init-git to initialize this directory first" if str(exc) == "not inside a git repository" else ""
            print(f"error: {exc}{hint}", file=sys.stderr)
            return 1

    progress_base = plan_source.name if plan_source else "review"
    progress_file = cfg.progress_dir / f"progress-{progress_base}.txt"
    log = ProgressLog(progress_file)
    dashboard_json, dashboard_html = dashboard_paths(progress_file)
    dashboard = ProgressDashboard(
        dashboard_json,
        dashboard_html,
        name=progress_base,
        plan_file=plan_file,
        plan_kind=plan_source.kind if plan_source else "gigaflex",
        progress_file=progress_file,
        branch=git.current_branch() if not args.dry_run and git.is_repo() else "",
        tasks_enabled=not args.review,
        review_enabled=not args.tasks_only,
        review_iterations=cfg.review_iterations,
        parallel_review=not args.no_parallel_review,
        finalize_enabled=cfg.finalize_enabled and not args.tasks_only,
    )
    if not args.dry_run:
        dashboard.start()
        print(f"dashboard: {dashboard_html.resolve()}")
    statistics = RunStatistics()
    stats_file = statistics_path(progress_file).resolve()
    checkpoint_file = checkpoint_path(progress_file).resolve()
    if not args.dry_run:
        statistics.write_json(stats_file)
    task_executor = GigaCodeExecutor(
        command=cfg.gigacode_command,
        args=cfg.args_for_phase("task"),
        timeout=cfg.session_timeout,
        idle_timeout=cfg.idle_timeout,
        retry_count=cfg.retry_count,
        retry_delay=cfg.retry_delay,
        retry_patterns=cfg.retry_patterns,
        rate_limit_patterns=cfg.rate_limit_patterns,
        wait_on_rate_limit=cfg.wait_on_rate_limit,
        max_workers=cfg.review_workers,
        output=log.write,
        diagnostic=log.diagnostic,
        event_callback=dashboard.executor_event,
        name="task",
        statistics=statistics,
    )
    synthesis_executor = GigaCodeExecutor(
        command=cfg.gigacode_command,
        args=cfg.args_for_phase("synthesis"),
        timeout=cfg.session_timeout,
        idle_timeout=cfg.idle_timeout,
        retry_count=cfg.retry_count,
        retry_delay=cfg.retry_delay,
        retry_patterns=cfg.retry_patterns,
        rate_limit_patterns=cfg.rate_limit_patterns,
        wait_on_rate_limit=cfg.wait_on_rate_limit,
        max_workers=cfg.review_workers,
        output=log.write,
        diagnostic=log.diagnostic,
        event_callback=dashboard.executor_event,
        name="review-synthesis",
        statistics=statistics,
    )
    review_agent_executor = GigaCodeExecutor(
        command=cfg.gigacode_command,
        args=cfg.args_for_review_agent(),
        timeout=cfg.session_timeout,
        idle_timeout=cfg.idle_timeout,
        retry_count=cfg.retry_count,
        retry_delay=cfg.retry_delay,
        retry_patterns=cfg.retry_patterns,
        rate_limit_patterns=cfg.rate_limit_patterns,
        wait_on_rate_limit=cfg.wait_on_rate_limit,
        max_workers=cfg.review_workers,
        output=log.write,
        diagnostic=log.diagnostic,
        event_callback=dashboard.executor_event,
        name="review-agent",
        statistics=statistics,
    )
    finalize_executor = GigaCodeExecutor(
        command=cfg.gigacode_command,
        args=cfg.args_for_phase("finalize"),
        timeout=cfg.session_timeout,
        idle_timeout=cfg.idle_timeout,
        retry_count=cfg.retry_count,
        retry_delay=cfg.retry_delay,
        retry_patterns=cfg.retry_patterns,
        rate_limit_patterns=cfg.rate_limit_patterns,
        wait_on_rate_limit=cfg.wait_on_rate_limit,
        max_workers=cfg.review_workers,
        output=log.write,
        diagnostic=log.diagnostic,
        event_callback=dashboard.executor_event,
        name="finalize",
        statistics=statistics,
    )
    if not args.dry_run:
        log.section("startup")
        log.write(f"gigacode command: {task_executor.command_line()}\n")
        if review_agent_executor.command_line() != task_executor.command_line():
            log.write(f"review agent gigacode command: {review_agent_executor.command_line()}\n")
        if synthesis_executor.command_line() != task_executor.command_line():
            log.write(f"review synthesis gigacode command: {synthesis_executor.command_line()}\n")
        if cfg.finalize_enabled and finalize_executor.command_line() != synthesis_executor.command_line():
            log.write(f"finalize gigacode command: {finalize_executor.command_line()}\n")
        if cfg.session_timeout:
            log.write(f"session timeout: {cfg.session_timeout}s\n")
        if cfg.idle_timeout:
            log.write(f"idle timeout: {cfg.idle_timeout}s\n")
        if cfg.retry_count:
            log.write(f"retry count: {cfg.retry_count}, retry delay: {cfg.retry_delay}s\n")
        if cfg.wait_on_rate_limit is not None:
            log.write(f"rate limit wait: {cfg.wait_on_rate_limit}s\n")
        if cfg.allow_dirty:
            log.write("allow dirty: enabled for startup and phase transitions\n")
        log.write(f"review workers: {cfg.review_workers}\n")
        assert run_baseline is not None
        log.write(
            "review base: "
            f"{run_baseline.base_branch} ({run_baseline.base_commit})\n"
        )
        if args.jira_task:
            log.write(f"jira task: {args.jira_task}\n")
        if worktree_path is not None:
            log.write(f"worktree: {worktree_path}\n")
            assert plan_identity_path is not None
            log.write(f"branch: {branch_for_plan(plan_identity_path, args.branch, args.jira_task)}\n")
        elif cfg.create_branch and plan_identity_path is not None and not args.review:
            log.write(f"branch: {branch_for_plan(plan_identity_path, args.branch, args.jira_task)}\n")
        if plan_source and plan_source.is_openspec:
            log.write(f"OpenSpec change: {plan_source.source_path}\n")
            log.write(f"OpenSpec checklist: {plan_source.checklist_path}\n")
    options = RunOptions(
        plan_file=plan_file,
        progress_file=progress_file,
        default_branch=cfg.default_branch,
        max_iterations=cfg.max_iterations,
        review_iterations=cfg.review_iterations,
        tasks_only=args.tasks_only,
        review_only=args.review,
        finalize_enabled=cfg.finalize_enabled,
        dry_run=args.dry_run,
        parallel_review=not args.no_parallel_review,
        prompts=prompts,
        jira_task=args.jira_task,
        plan_kind=plan_source.kind if plan_source else "gigaflex",
        plan_source=plan_source.source_path if plan_source else None,
        plan_context_files=plan_source.context_paths if plan_source else (),
        task_completion_retries=cfg.retry_count,
        allow_dirty=cfg.allow_dirty,
    )
    review_worktrees = (
        None
        if args.dry_run
        else ReviewWorktreeManager(git, diagnostic=log.diagnostic)
    )
    repo_root = git.repo_root() if not args.dry_run else None
    orchestration_paths = (
        tuple(
            path.resolve().relative_to(repo_root)
            for path in (
                progress_file,
                stats_file,
                checkpoint_file,
                dashboard_json,
                dashboard_html,
                log.prompt_context_file,
            )
            if path.resolve().is_relative_to(repo_root)
        )
        if repo_root is not None
        else ()
    )
    task_worktrees = (
        None
        if args.dry_run or args.review
        else TaskWorktreeManager(
            git,
            diagnostic=log.diagnostic,
            ignored_paths=orchestration_paths,
        )
    )
    checkpoint = (
        None
        if args.dry_run
        else RunCheckpoint(
            checkpoint_file,
            git,
            identity=(
                f"{plan_source.kind}:{plan_source.source_path.resolve()}"
                if plan_source is not None
                else f"review:{git.current_branch()}"
            ),
            base_commit=run_baseline.base_commit if run_baseline is not None else "",
            ignored_paths=orchestration_paths,
            diagnostic=log.diagnostic,
        )
    )

    exit_code = 0
    run_status = "success"
    failure_reason = ""
    failure_phase = ""
    try:
        Runner(
            options,
            task_executor,
            log,
            synthesis_executor=synthesis_executor,
            review_agent_executor=review_agent_executor,
            finalize_executor=finalize_executor,
            dashboard=dashboard,
            review_worktrees=review_worktrees,
            task_worktrees=task_worktrees,
            checkpoint=checkpoint,
        ).run()
        if (
            not args.dry_run
            and cfg.move_plan_on_completion
            and plan_file is not None
            and not (plan_source and plan_source.is_openspec)
            and not args.review
            and not args.tasks_only
        ):
            moved_to = move_plan_to_completed(plan_file)
            log.section("plan")
            log.write(f"moved completed plan to {moved_to}\n")
            if git.is_repo():
                message = completed_plan_commit_message(plan_file, args.jira_task)
                git.commit_paths([plan_file, moved_to], message)
                log.write(f"committed completed plan move: {message}\n")
        if (
            not args.dry_run
            and plan_source is not None
            and plan_source.is_openspec
            and not parse_plan_file(plan_file, plan_format="openspec").has_uncompleted_tasks()
        ):
            log.section("OpenSpec")
            log.write(f"change complete: {plan_source.name}\n")
            log.write(f"ready to archive with: openspec archive {plan_source.name}\n")
            print(f"OpenSpec change complete: {plan_source.name}")
            print(f"ready to archive with: openspec archive {plan_source.name}")
        if not args.dry_run:
            dashboard.complete()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        exit_code = 130
        run_status = "interrupted"
        failure_reason = "run interrupted"
        failure_phase = str(dashboard.state.get("phase", "unknown"))
        if not args.dry_run:
            log.section("failure")
            log.write(
                f"phase: {failure_phase}\nreason: {failure_reason}\n"
            )
            dashboard.interrupt()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
        run_status = "failed"
        failure_reason = str(exc)
        failure_phase = str(dashboard.state.get("phase", "unknown"))
        if not args.dry_run:
            log.section("failure")
            log.write(
                f"phase: {failure_phase}\n"
                f"error: {type(exc).__name__}: {failure_reason}\n"
            )
            log.diagnostic(
                "session=runner event=failed "
                f"phase={failure_phase!r} error={failure_reason!r}"
            )
            dashboard.fail(str(exc))
    finally:
        if not args.dry_run:
            statistics.finish(
                run_status,
                failure_reason=failure_reason,
                failure_phase=failure_phase,
            )
            report = statistics.render_text()
            statistics.write_json(stats_file)
            log.section("run statistics")
            log.write(report)
            print(report, end="")
            print(f"statistics: {stats_file}")

    if exit_code:
        return exit_code
    print(f"progress log: {progress_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
