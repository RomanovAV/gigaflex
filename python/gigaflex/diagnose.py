from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import os
import shlex
import subprocess
import sys

from .config import load_config
from .executor import GigaCodeExecutor, is_dependency_crash
from .prompts import PromptContext, load_prompt_templates, render_task_prompt


DEFAULT_PROMPT = "выполни pwd через run_shell_command"
APPROVAL_WARNING = "requires user approval but cannot execute in non-interactive mode"
DEPENDENCY_CRASH_ADVICE = (
    "Detected a GigaCode/libsecret dependency crash. Re-authenticate GigaCode "
    "and recreate its credential-store entry. Until the dependency is repaired, "
    "run GigaFlex with --review-workers 1 or --no-parallel-review."
)


def _argv(prompt: str) -> list[str]:
    return [
        "gigacode",
        "--approval-mode=auto-edit",
        "--allowed-tools",
        "run_shell_command",
        "-p",
        prompt,
    ]


def _run_inherited(argv: list[str]) -> int:
    return subprocess.run(argv).returncode


def _run_captured(argv: list[str], log_path: Path) -> tuple[int, str]:
    proc = subprocess.Popen(
        argv,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    chunks: list[str] = []
    for line in proc.stdout:
        chunks.append(line)
        print(line, end="")
    returncode = proc.wait()
    output = "".join(chunks)
    log_path.write_text(output, encoding="utf-8")
    return returncode, output


def _run_parallel(
    argv: list[str],
    log_dir: Path,
    workers: int,
) -> list[tuple[int, str]]:
    """Run identical captured probes concurrently and persist isolated logs."""
    if workers <= 0:
        return []

    def probe(number: int) -> tuple[int, int, str]:
        completed = subprocess.run(
            argv,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = completed.stdout or ""
        (log_dir / f"parallel-{number}.log").write_text(output, encoding="utf-8")
        return number, completed.returncode, output

    completed_probes: list[tuple[int, int, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe, number) for number in range(1, workers + 1)]
        for future in as_completed(futures):
            completed_probes.append(future.result())
    completed_probes.sort(key=lambda item: item[0])
    return [(returncode, output) for _, returncode, output in completed_probes]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare direct GigaCode, captured subprocess, and GigaFlex executor behavior.",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="also run the exact GigaFlex task prompt for this plan once",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=3,
        metavar="N",
        help="run N simultaneous captured probes (0 disables; default: 3)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.parallel_workers < 0:
        print("ERROR: --parallel-workers must be zero or greater", file=sys.stderr)
        return 2
    prompt = os.getenv("GIGAFLEX_DIAGNOSTIC_PROMPT", DEFAULT_PROMPT)
    log_dir = Path(".gigaflex/diagnostics") / datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    argv = _argv(prompt)
    cfg = load_config()

    print("GigaCode/GigaFlex diagnostic")
    print(f"working directory: {Path.cwd()}")
    print(f"python: {sys.executable}")
    print(f"executor command: {cfg.gigacode_command}")
    print(f"configured args: {cfg.resolved_args!r}")
    print(f"stdin is a terminal: {sys.stdin.isatty()}")
    print(f"stdout is a terminal: {sys.stdout.isatty()}")
    print(f"test command: {shlex.join(argv[:2] + ['<prompt>'] + argv[3:])}")
    print(f"log directory: {log_dir}")

    print("\n=== 1. subprocess with inherited terminal ===")
    inherited_status = _run_inherited(argv)
    print(f"exit status: {inherited_status}")

    print("\n=== 2. subprocess with captured stdout ===")
    captured_status, captured_output = _run_captured(argv, log_dir / "captured.log")
    print(f"\nexit status: {captured_status}")

    print("\n=== 3. GigaCodeExecutor ===")
    executor_chunks: list[str] = []
    result = GigaCodeExecutor(
        command=cfg.gigacode_command,
        args=cfg.args_for_phase("task"),
        retry_count=0,
        output=lambda text: (executor_chunks.append(text), print(text, end=""))[-1],
    ).run(prompt)
    executor_output = "".join(executor_chunks)
    (log_dir / "executor.log").write_text(executor_output, encoding="utf-8")
    print(f"\nexit status: {result.returncode}")

    print("\n=== 4. parallel captured subprocesses ===")
    parallel_results = _run_parallel(argv, log_dir, args.parallel_workers)
    if not parallel_results:
        print("skipped (--parallel-workers=0)")
    else:
        for number, (returncode, output) in enumerate(parallel_results, start=1):
            crash = is_dependency_crash(returncode, output)
            print(
                f"worker {number}: exit status {returncode}, "
                f"dependency crash: {'yes' if crash else 'no'}"
            )

    print("\n=== diagnosis ===")
    dependency_crash = (
        is_dependency_crash(inherited_status, "")
        or is_dependency_crash(captured_status, captured_output)
        or result.dependency_crash
        or any(
            is_dependency_crash(returncode, output)
            for returncode, output in parallel_results
        )
    )
    if dependency_crash:
        print(DEPENDENCY_CRASH_ADVICE)
    elif inherited_status != 0:
        print("Inherited Python subprocess failed: inspect GigaCode/project policy.")
    elif captured_status != 0 or APPROVAL_WARNING in captured_output:
        print("Capturing stdout triggers the failure; GigaCode requires a terminal/PTY.")
    elif not result.ok or APPROVAL_WARNING in executor_output:
        print("Minimal captured subprocess works, but GigaCodeExecutor/configuration fails.")
    else:
        print("All minimal checks pass; the failure depends on the full task prompt or project operations.")

    if args.plan is not None:
        plan_file = args.plan.resolve()
        if not plan_file.is_file():
            print(f"\nERROR: plan file not found: {plan_file}")
            return 2

        print("\n=== 5. exact task prompt ===")
        prompts = load_prompt_templates(cfg.prompt_dirs)
        task_prompt = render_task_prompt(
            prompts.task,
            PromptContext(
                plan_file=plan_file,
                progress_file=log_dir / "task-progress.log",
                default_branch=cfg.default_branch or "master",
            ),
        )
        (log_dir / "task-prompt.txt").write_text(task_prompt, encoding="utf-8")
        task_chunks: list[str] = []
        task_result = GigaCodeExecutor(
            command=cfg.gigacode_command,
            args=cfg.args_for_phase("task"),
            retry_count=0,
            output=lambda text: (task_chunks.append(text), print(text, end=""))[-1],
        ).run(task_prompt)
        task_output = "".join(task_chunks)
        (log_dir / "task-executor.log").write_text(task_output, encoding="utf-8")
        print(f"\nexit status: {task_result.returncode}")
        if task_result.dependency_crash:
            print(f"Result: {DEPENDENCY_CRASH_ADVICE}")
        elif APPROVAL_WARNING in task_output:
            print("Result: the approval failure is triggered by the exact task prompt/tool sequence.")
        elif task_result.ok:
            print("Result: the exact task prompt completed without an approval warning.")
        else:
            print("Result: the exact task prompt failed for another reason; inspect task-executor.log.")

    print(f"logs: {log_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
