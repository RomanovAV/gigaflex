from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import os
import shlex
import subprocess
import sys

from .config import load_config
from .executor import GigaCodeExecutor
from .prompts import PromptContext, load_prompt_templates, render_task_prompt


DEFAULT_PROMPT = "выполни pwd через run_shell_command"
APPROVAL_WARNING = "requires user approval but cannot execute in non-interactive mode"


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare direct GigaCode, captured subprocess, and GigaFlex executor behavior.",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="also run the exact GigaFlex task prompt for this plan once",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
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

    print("\n=== diagnosis ===")
    if inherited_status != 0:
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

        print("\n=== 4. exact task prompt ===")
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
        if APPROVAL_WARNING in task_output:
            print("Result: the approval failure is triggered by the exact task prompt/tool sequence.")
        elif task_result.ok:
            print("Result: the exact task prompt completed without an approval warning.")
        else:
            print("Result: the exact task prompt failed for another reason; inspect task-executor.log.")

    print(f"logs: {log_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
