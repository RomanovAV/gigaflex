from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
import os
import shlex
from typing import Optional

from .defaults import DEFAULT_GIGACODE_ARGS, DEFAULT_GIGACODE_INTERACTIVE_ARGS
from .executor import DEFAULT_RATE_LIMIT_PATTERNS, DEFAULT_TRANSIENT_RETRY_PATTERNS
from .prompts import init_prompt_templates, sync_global_prompt_templates


GLOBAL_CONFIG_RELATIVE_DIR = Path(".config/gigaflex")


def global_config_dir() -> Path:
    return Path.home() / GLOBAL_CONFIG_RELATIVE_DIR


@dataclass
class Config:
    gigacode_command: str = "gigacode"
    gigacode_args: Optional[list[str]] = None
    gigacode_interactive_args: Optional[list[str]] = None
    gigacode_skills_dir: Path = field(default_factory=lambda: Path.home() / ".gigacode/skills")
    plan_model: Optional[str] = None
    task_model: Optional[str] = None
    review_model: Optional[str] = None
    finalize_model: Optional[str] = None
    plans_dir: Path = Path("docs/plans")
    progress_dir: Path = Path(".gigaflex/progress")
    prompts_dir: Path = Path(".gigaflex/prompts")
    default_branch: str = ""
    max_iterations: int = 50
    review_iterations: int = 10
    finalize_enabled: bool = True
    session_timeout: Optional[int] = 1800
    idle_timeout: Optional[int] = 900
    retry_count: int = 1
    retry_delay: float = 5.0
    retry_patterns: list[str] = field(default_factory=lambda: DEFAULT_TRANSIENT_RETRY_PATTERNS.copy())
    rate_limit_patterns: list[str] = field(default_factory=lambda: DEFAULT_RATE_LIMIT_PATTERNS.copy())
    wait_on_rate_limit: Optional[float] = None
    review_workers: int = 5
    create_branch: bool = True
    worktree: bool = False
    move_plan_on_completion: bool = True
    commit_plan_on_creation: bool = True
    allow_dirty: bool = False

    @property
    def resolved_args(self) -> list[str]:
        args = self.gigacode_args if self.gigacode_args is not None else DEFAULT_GIGACODE_ARGS
        return _with_prompt_after_noninteractive_options(
            _with_noninteractive_shell_access(args)
        )

    @property
    def resolved_interactive_args(self) -> list[str]:
        if self.gigacode_interactive_args is not None:
            return self.gigacode_interactive_args
        return DEFAULT_GIGACODE_INTERACTIVE_ARGS.copy()

    def args_for_phase(self, phase: str) -> list[str]:
        model = self.model_for_phase(phase)
        args = self.resolved_args
        if model:
            return ["--model", model, *args]
        return args

    def args_for_review_agent(self) -> list[str]:
        return self.args_for_phase("review")

    def args_for_interactive_plan(self) -> list[str]:
        model = self.model_for_phase("plan")
        args = self.resolved_interactive_args
        if model:
            return ["--model", model, *args]
        return args

    def model_for_phase(self, phase: str) -> Optional[str]:
        if phase == "plan":
            return self.plan_model or self.task_model
        if phase == "task":
            return self.task_model
        if phase == "review":
            return self.review_model or self.task_model
        if phase == "synthesis":
            return self.task_model
        if phase == "finalize":
            return self.finalize_model or self.review_model or self.task_model
        raise ValueError(f"unknown phase: {phase}")

    @property
    def prompt_dirs(self) -> list[Path]:
        return [self.prompts_dir, global_config_dir() / "prompts"]


def load_config(path: Optional[Path] = None) -> Config:
    cfg = Config()
    candidates = [
        global_config_dir() / "config",
        Path(".gigaflex/config"),
    ]
    if path is not None:
        candidates.append(path)

    parser = ConfigParser()
    parser.optionxform = str
    read_files = parser.read([str(p) for p in candidates if p.exists()], encoding="utf-8")
    if not read_files:
        return _apply_env(cfg)

    section = parser["gigaflex"] if parser.has_section("gigaflex") else parser["DEFAULT"]
    cfg.gigacode_command = section.get("gigacode_command", cfg.gigacode_command)
    if "gigacode_args" in section:
        cfg.gigacode_args = shlex.split(section.get("gigacode_args", ""))
    if "gigacode_interactive_args" in section:
        cfg.gigacode_interactive_args = shlex.split(section.get("gigacode_interactive_args", ""))
    cfg.gigacode_skills_dir = Path(
        section.get("gigacode_skills_dir", str(cfg.gigacode_skills_dir))
    ).expanduser()
    cfg.plan_model = _optional_str(section.get("plan_model", cfg.plan_model))
    cfg.task_model = _optional_str(section.get("task_model", cfg.task_model))
    cfg.review_model = _optional_str(section.get("review_model", cfg.review_model))
    cfg.finalize_model = _optional_str(section.get("finalize_model", cfg.finalize_model))
    cfg.plans_dir = Path(section.get("plans_dir", str(cfg.plans_dir)))
    cfg.progress_dir = Path(section.get("progress_dir", str(cfg.progress_dir)))
    cfg.prompts_dir = Path(section.get("prompts_dir", str(cfg.prompts_dir)))
    cfg.default_branch = section.get("default_branch", cfg.default_branch)
    cfg.max_iterations = section.getint("max_iterations", cfg.max_iterations)
    cfg.review_iterations = section.getint("review_iterations", cfg.review_iterations)
    cfg.finalize_enabled = section.getboolean("finalize_enabled", cfg.finalize_enabled)
    if "session_timeout" in section:
        cfg.session_timeout = section.getint("session_timeout")
    if "idle_timeout" in section:
        cfg.idle_timeout = section.getint("idle_timeout")
    cfg.retry_count = section.getint("retry_count", cfg.retry_count)
    cfg.retry_delay = section.getfloat("retry_delay", cfg.retry_delay)
    if "retry_patterns" in section:
        cfg.retry_patterns = _csv_list(section.get("retry_patterns", ""))
    if "rate_limit_patterns" in section:
        cfg.rate_limit_patterns = _csv_list(section.get("rate_limit_patterns", ""))
    if "wait_on_rate_limit" in section:
        cfg.wait_on_rate_limit = section.getfloat("wait_on_rate_limit")
    cfg.review_workers = section.getint("review_workers", cfg.review_workers)
    cfg.create_branch = section.getboolean("create_branch", cfg.create_branch)
    cfg.worktree = section.getboolean("worktree", cfg.worktree)
    cfg.move_plan_on_completion = section.getboolean("move_plan_on_completion", cfg.move_plan_on_completion)
    cfg.commit_plan_on_creation = section.getboolean("commit_plan_on_creation", cfg.commit_plan_on_creation)
    cfg.allow_dirty = section.getboolean("allow_dirty", cfg.allow_dirty)
    return _apply_env(cfg)


def _apply_env(cfg: Config) -> Config:
    if command := os.getenv("GIGAFLEX_GIGACODE_COMMAND"):
        cfg.gigacode_command = command
    if args := os.getenv("GIGAFLEX_GIGACODE_ARGS"):
        cfg.gigacode_args = shlex.split(args)
    if args := os.getenv("GIGAFLEX_GIGACODE_INTERACTIVE_ARGS"):
        cfg.gigacode_interactive_args = shlex.split(args)
    if skills_dir := os.getenv("GIGAFLEX_GIGACODE_SKILLS_DIR"):
        cfg.gigacode_skills_dir = Path(skills_dir).expanduser()
    if model := os.getenv("GIGAFLEX_TASK_MODEL"):
        cfg.task_model = model
    if model := os.getenv("GIGAFLEX_REVIEW_MODEL"):
        cfg.review_model = model
    return cfg


def _optional_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _with_noninteractive_shell_access(args: list[str]) -> list[str]:
    normalized: list[str] = []
    approval_mode_added = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--approval-mode":
            if not approval_mode_added:
                normalized.extend(["--approval-mode", "auto-edit"])
                approval_mode_added = True
            index += 2 if index + 1 < len(args) else 1
            continue
        if arg.startswith("--approval-mode="):
            if not approval_mode_added:
                normalized.append("--approval-mode=auto-edit")
                approval_mode_added = True
            index += 1
            continue
        normalized.append(arg)
        index += 1

    if not approval_mode_added:
        normalized.append("--approval-mode=auto-edit")

    for index, arg in enumerate(normalized):
        if arg == "--allowed-tools":
            tool_index = index + 1
            while tool_index < len(normalized) and not normalized[tool_index].startswith("-"):
                if normalized[tool_index] == "run_shell_command":
                    return normalized
                tool_index += 1
            normalized.insert(index + 1, "run_shell_command")
            return normalized
        if arg.startswith("--allowed-tools="):
            tools = [tool for tool in arg.split("=", 1)[1].split(",") if tool]
            if "run_shell_command" not in tools:
                tools.insert(0, "run_shell_command")
            normalized[index:index + 1] = [
                "--allowed-tools",
                *tools,
            ]
            return normalized

    normalized.extend(["--allowed-tools", "run_shell_command"])
    return normalized


def _with_prompt_after_noninteractive_options(args: list[str]) -> list[str]:
    remaining: list[str] = []
    prompt_args: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if (
            arg in {"-p", "--prompt"}
            and index + 1 < len(args)
            and "{prompt}" in args[index + 1]
        ):
            prompt_args = ["-p", args[index + 1]]
            index += 2
            continue
        if arg.startswith("--prompt=") and "{prompt}" in arg:
            prompt_args = ["-p", arg.split("=", 1)[1]]
            index += 1
            continue
        if arg == "{prompt}":
            prompt_args = ["-p", arg]
            index += 1
            continue
        remaining.append(arg)
        index += 1

    return [*remaining, *prompt_args]


DEFAULT_CONFIG_TEXT = """[gigaflex]
# gigacode_command = gigacode
# gigacode_args = --approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}
# gigacode_interactive_args = --prompt-interactive {prompt} --approval-mode=auto-edit
# gigacode_skills_dir = ~/.gigacode/skills
# plan_model =
# task_model =
# review_model =
# finalize_model =
# plans_dir = docs/plans
# progress_dir = .gigaflex/progress
# prompts_dir = .gigaflex/prompts
# Legacy explicit review base; leave empty to capture the launch branch and commit.
# default_branch =
# max_iterations = 50
# Maximum synthesis/repair cycles; one terminal verification runs afterward.
# review_iterations = 10
# finalize_enabled = true
# session_timeout = 1800
# idle_timeout = 900
# retry_count = 1
# retry_delay = 5
# retry_patterns = FYA_TRANSIENT_TIMEOUT,API Error: 529,API Error: 502,API Error: 503,API Error: 504,502 Bad Gateway,503 Service Unavailable,504 Gateway Timeout
# rate_limit_patterns = Rate limit exceeded,rate limit reached,429 Too Many Requests,quota exceeded,insufficient_quota,You've hit your usage limit
# wait_on_rate_limit =
# review_workers = 5
# create_branch = true
# worktree = false
# move_plan_on_completion = true
# commit_plan_on_creation = true
# allow_dirty = false
"""


def init_global_config() -> list[Path]:
    config_dir = global_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config"
    if config_path.exists():
        return []
    config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return [config_path]


def init_global_prompt_templates() -> list[Path]:
    return sync_global_prompt_templates(global_config_dir() / "prompts")


DEFAULT_GITIGNORE_LINES = [
    ".DS_Store",
    ".gigaflex/progress/",
    ".gigaflex/worktrees/",
]


def init_project_config(base_dir: Path = Path(".gigaflex")) -> list[Path]:
    base_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    config_path = base_dir / "config"
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        written.append(config_path)

    gitignore_path = base_dir.parent / ".gitignore"
    if _ensure_gitignore_lines(gitignore_path, DEFAULT_GITIGNORE_LINES):
        written.append(gitignore_path)

    return written


def init_project_prompt_templates(base_dir: Path = Path(".gigaflex")) -> list[Path]:
    return init_prompt_templates(base_dir / "prompts")


def _ensure_gitignore_lines(path: Path, lines: list[str]) -> bool:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in lines if line not in existing]
    if not missing:
        return False

    content = "\n".join(existing)
    if content:
        content += "\n"
    content += "\n".join(missing) + "\n"
    path.write_text(content, encoding="utf-8")
    return True
