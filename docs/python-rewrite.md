# Python rewrite notes

## Current core functions

- Parse English and Russian markdown plans with `Task`, `Iteration`, `Задача`,
  and `Итерация` sections and actionable checkboxes.
- Execute Superpowers implementation plans directly.
- Execute local OpenSpec changes through `--openspec`: use `tasks.md` as the
  writable checklist and pass proposal, design, and delta specs as read-only
  context. Localized prose-only task groups are tracked with durable completion
  markers.
- Create gigaflex-compatible plans from a free-form request.
- Commit newly created plan files by default when running inside a git
  repository.
- Commit completed plan moves after a successful full run.
- Install `.gitignore` entries for `.DS_Store`, `.gigaflex/progress/`, and
  `.gigaflex/worktrees/` during project initialization.
- Optionally initialize a missing git repository with `--init-git` and commit
  the initial working tree before execution.
- Run one task section per agent iteration.
- Stream agent output to terminal and a progress log.
- Maintain a self-contained live HTML dashboard and a machine-readable JSON
  status file beside each progress log.
- Record per-attempt timing, model, token usage, retry decisions, and final run
  status in a statistics JSON file.
- Detect gigaflex completion signals.
- Run a review loop after tasks.
- Run five specialist review agents in parallel from disposable detached
  worktrees built from one ephemeral working-tree snapshot, remove those
  worktrees after every pass, then synthesize/fix findings in the main worktree.
- Run a finalize prompt by default, with `--no-finalize` to disable it.
- Configure the agent command as `gigacode` plus arbitrary CLI args.
- Select GigaCode models per phase with `plan_model`, `task_model`,
  `review_model`, and `finalize_model`, mapped to GigaCode's `--model` flag.
  Read-only reviewers use `review_model`; review synthesis and fixes use
  `task_model`.
- Initialize the local `.gigaflex/` config automatically on first real plan
  creation or execution.
- Initialize editable global config and prompt templates automatically, with
  local project files as a fallback when global storage is not writable.
- Create local project prompt overrides only with `--init-prompts`.
- Bound executor runs with session timeout, idle timeout, retry count, retry
  delay, and review worker limit.
- Classify transient and rate-limit executor failures with configurable
  patterns, including optional longer waits before rate-limit retries.
- Retry logically incomplete task completions with a corrective prompt after
  restoring protected plan/OpenSpec state.
- Retry `Model not found` failures without the configured model and use the
  GigaCode default model for later calls.
- Validate git repository state, capture the launch branch and exact base commit,
  persist that base for the execution branch, create/switch the plan branch, and
  move completed plans.
- Enforce Jira branch names and automatically prefix new local commit subjects
  created during a run.
- Run full and tasks-only plan execution in an isolated git worktree with
  `--worktree`.

## Intentionally deferred

- Notifications.
- External second-model review.
- Docker wrapper.

The Python version is intentionally small first. It launches `gigacode` in
one-shot mode with
`--approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}` by
default. GigaCode 26.5.17 needs `--allowed-tools run_shell_command` for tests
and git commands; `--approval-mode=auto-edit` only covers edit/write tools.
Although GigaCode marks `-p/--prompt` as deprecated, its positional `query..`
form is ambiguous when combined with array-valued options. The runtime itself
recommends `-p` on non-interactive approval failures. If custom args omit
`{prompt}`, the executor adds `-p <generated prompt>` rather than using stdin.
Configured non-interactive arguments are normalized so every phase retains
`--approval-mode=auto-edit` and permission for `run_shell_command`.
If the CLI later needs a subcommand or different flags, the executor boundary
is `GigaCodeExecutor`, so adapting the invocation should be one local change.

GigaCode model selection is a CLI concern, not a prompt concern. The CLI exposes
`-m/--model`, so gigaflex adds `--model <name>` to the phase invocation instead
of embedding model names in the prompt text.

## Observed GigaCode behavior

- Prompts are plain text instructions. The task prompt tells GigaCode to read a
  markdown plan, find the first unchecked `### Task N:` or `### Iteration N:`
  section, complete it, test it, commit it, mark checkboxes as `[x]`, and emit a
  completion signal.
- One task section is expected per GigaCode launch. The prompt explicitly says:
  `Do not continue to the next task section.`
- Workspace guard is enforced by GigaCode. Files outside its workspace cannot be
  edited unless the GigaCode invocation includes the needed
  `--include-directories` values.
- Approval mode must be explicit in non-interactive runs. Real logs show
  `Warning: Tool "run_shell_command" requires user approval but cannot execute
  in non-interactive mode`; GigaCode help shows that shell execution additionally
  requires `--allowed-tools run_shell_command`.
- GigaCode appears to run on Node.js; `MaxListenersExceededWarning` can surface
  in its output.
- GigaCode has administrative CLI subcommands (`mcp`, `extensions`, `auth`,
  `sandbox`, and `hooks`) but no task-execution subcommand. There is also no
  observed JSON/REST API, official Python SDK, or `IN_PROGRESS` signal.
  `gigaflex` therefore uses the default `gigacode [query..]` command and
  treats the CLI process and output stream as the integration boundary.

## Usage

Run without installing:

```bash
PYTHONPATH=python python3 -m gigaflex.cli docs/plans/my-feature.md
```

Inspect prompts without invoking GigaCode:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --dry-run docs/plans/my-feature.md
```

Configure command shape:

```ini
[gigaflex]
gigacode_command = gigacode
gigacode_args = --approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}
gigacode_interactive_args = --prompt-interactive {prompt} --approval-mode=auto-edit
gigacode_skills_dir = ~/.gigacode/skills
default_branch =
```

An empty `default_branch` captures the launch branch and commit. A configured
value is retained as a legacy explicit base override, equivalent to supplying
`--base-ref` on each run.

Create local config:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --init
```

Global config and prompt templates are created automatically under
`~/.config/gigaflex/`. When that location is not writable, the CLI creates
`.gigaflex/config` and `.gigaflex/prompts/` in the current project instead.
Create local project overrides explicitly when needed:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --init-prompts
```

Create a new plan:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --install-planning-skill
PYTHONPATH=python python3 -m gigaflex.cli --plan "add user authentication"
```

With a terminal attached, plan creation invokes the installed GigaCode
`planning` skill interactively. Use `--quick`, or run without a TTY, to use the
one-shot `make_plan.txt` prompt.
