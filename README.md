# gigalphex

Python autonomous plan runner for GigaCode CLI.

This is a small standalone rewrite of the useful ralphex core:

- parse English and Russian markdown plans with level-two or level-three
  `Task N:` / `Iteration N:` / `Задача N:` / `Итерация N:` headings
- execute Superpowers `docs/superpowers/plans/*.md` implementation plans directly
- run one task section per agent iteration
- keep detailed agent output in progress logs and generate a live local dashboard
- detect gigalphex completion signals
- run review and a default finalize pass
- run five specialist review agents in parallel, then synthesize/fix findings
- create/switch a git branch from the plan filename
- optionally run a plan in an isolated git worktree
- guard against dirty working trees
- move completed plans into `completed/`
- call `gigacode` through a configurable CLI boundary

Current assumption: GigaCode CLI is available in `PATH`. Task, review,
finalize, and quick-plan sessions use one-shot mode by default:

```bash
gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<generated prompt>'
```

The default argument template is
`--approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}`.
`gigalphex` replaces `{prompt}` with the generated prompt before invoking
GigaCode. If custom `gigacode_args` do not include `{prompt}`, GigaLphex adds
`-p <generated prompt>` instead of sending a non-interactive prompt through
stdin.
For the actual subprocess invocation GigaLphex also enforces
`--output-format stream-json`. Assistant text is decoded into the detailed
progress log, while lifecycle events update a concise local dashboard and final
`result` events provide exact token and timing statistics.
Custom non-interactive arguments cannot disable shell execution accidentally:
GigaLphex normalizes every plan, task, review, synthesis, and finalize invocation
to include `--approval-mode=auto-edit` and allow `run_shell_command`.
GigaCode marks `-p/--prompt` as deprecated in favor of
the positional query, but its variadic `query..` parser consumes options placed
after the query, while array-valued options can consume a query placed after
them. The explicit `-p` form is therefore the reliable non-interactive contract
for GigaCode 26.5.17 and is also the form recommended by its runtime approval
error. `--approval-mode=auto-edit` allows edit/write tools, while shell commands
such as tests and `git commit` also require
`--allowed-tools run_shell_command`. Combined stdout/stderr is streamed into
the detailed progress log without flooding the normal terminal output.

Every plan execution and review run creates two live status files next to its
progress log:

```text
.gigalphex/progress/status-my-feature.json
.gigalphex/progress/status-my-feature.html
```

The CLI prints the absolute dashboard path when the run starts. Open the HTML
file in any browser; it is self-contained, needs no HTTP server, and refreshes
while the run is active. It shows task checklist progress, the current phase,
parallel review sessions, retries, failures, elapsed time, and known token
usage. The JSON file exposes the same state for future integrations. The full
agent transcript and executor diagnostics remain in `progress-my-feature.txt`.

Interactive plan creation is different. When stdin and stdout are attached to
a terminal, `--plan` launches GigaCode with
`--prompt-interactive '<generated prompt>' --approval-mode=auto-edit` and
inherits the current terminal. Unlike a positional prompt or `-p`, the
`--prompt-interactive` flag executes the planning request and keeps the TUI
open so the user can answer the planning skill's questions. Auto-edit lets the
skill create the requested plan file. Use `--quick` to force the one-shot plan
prompt. Non-TTY sessions, including CI, automatically use quick mode.

Install the bundled planning skill once:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --install-planning-skill
```

Superpowers implementation plans with `## Task N:` or `### Task N:` headings
and step checkboxes can be executed directly:

```bash
PYTHONPATH=python python3 -m gigalphex.cli \
  docs/superpowers/plans/2026-07-01-demo.md
```

Standard local OpenSpec `spec-driven` changes can be executed with an explicit
change-directory flag:

```bash
PYTHONPATH=python python3 -m gigalphex.cli \
  --openspec openspec/changes/add-dark-mode
```

GigaLphex uses the change's `tasks.md` as the writable checklist and provides
`proposal.md`, `design.md`, and all `specs/**/*.md` delta specs to each task
agent as read-only context. Each `## N. ...` group in `tasks.md` is one agent
iteration and one commit. Branch and progress names come from the change
directory rather than the generic `tasks.md` filename.

Localized prose-only task sections such as `## Задача 1: ...` are also
supported when an OpenSpec generator omits checkboxes. They start as pending;
after completing a section, the task agent adds a durable
`- [x] N. <title>` marker below its heading so later runs can resume safely.

Completing an OpenSpec run does not move `tasks.md` or archive the change.
GigaLphex prints the corresponding `openspec archive <change-name>` command so
the spec merge and archive remain an explicit OpenSpec lifecycle action.
Archived changes and changes without `tasks.md` are rejected.

Install the bundled Superpowers conversion skill when you want to turn a
Superpowers design spec into a plan, or normalize a plan by removing
Superpowers-specific execution mechanics:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --install-superpowers-converter-skill
```

The default destination is `~/.gigacode/skills/planning/SKILL.md`. Existing
customized content is preserved; use `--force-skill-install` to replace it
with the bundled version. For a GigaCode version using another skills
directory, pass `--skill-dir PATH` or configure `gigacode_skills_dir`.
The converter skill is installed as
`~/.gigacode/skills/superpowers-to-gigalphex/SKILL.md` and follows the same
overwrite rules.

Specialist and single-review sessions use `review_model`, keep the configured
shell approval arguments, and receive an explicit inspect-only prompt. Keeping
the normal invocation lets reviewers run inspection commands without an
interactive approval failure. The synthesis session uses `task_model` and is
the only review stage instructed to fix files or create commits.

Observed GigaCode constraints:

- GigaCode edits only inside its configured workspace. To let it work across
  sibling project directories, pass the appropriate GigaCode flag as, for
  example, `--gigacode-arg=--include-directories=/path/to/shared`. Extra
  GigaCode arguments are applied to both one-shot and interactive planning
  invocations. They can also be placed directly in `gigacode_args` and
  `gigacode_interactive_args`.
- GigaCode exposes administrative subcommands such as `mcp`, `extensions`,
  `auth`, `sandbox`, and `hooks`, but no task-execution subcommand.
  GigaLphex therefore uses the default `gigacode [query..]` command and does
  not assume a `gigacode task` command, JSON/REST API, or Python SDK.
- Non-interactive runs fail if GigaCode asks for shell approval without the
  shell tool being explicitly allowed. Real logs and GigaCode help indicate that
  `--approval-mode=auto-edit` must be paired with
  `--allowed-tools run_shell_command`; `gigalphex` includes both by default and
  still detects the warning if it appears.
- There is no `IN_PROGRESS` signal. The dashboard therefore reports factual
  outer progress—plan checkboxes, phases, active processes, output activity,
  and retries—instead of inventing a percentage for an active LLM session.
- Progress logs include executor lifecycle events for every phase: sanitized
  command, prompt transport and size, process start/PID, first output, timeout
  or approval detection, exit status, token usage, and retry decisions. Prompt
  contents are never included in these diagnostic lines. Parallel reviewers
  are identified as `review-agent:<name>`.
- Every plan/review run writes a statistics report next to the progress log,
  for example `.gigalphex/progress/stats-my-feature.json`. It includes each
  GigaCode attempt, measured wall time, GigaCode/API durations when reported,
  model names, per-call tokens, aggregate tokens, total run wall time, and the
  sum of call durations. The CLI prints the absolute statistics path and records
  run status such as `success`, `failed`, or `interrupted`. Summed call time may
  exceed wall time because review agents run in parallel.
- GigaCode runs on Node.js, so Node warnings such as
  `MaxListenersExceededWarning` may appear in combined output.

Run from this directory:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --dry-run ../e2e/testdata/test-plan.md
PYTHONPATH=python python3 -m gigalphex.cli docs/plans/my-feature.md
```

To enforce corporate Jira naming for a plan run, pass `--jira-task`. For
example, this switches or creates a branch like
`feature/PROJ-123-my-feature` and automatically adds `PROJ-123 ` to every new
commit created during the run:

```bash
PYTHONPATH=python python3 -m gigalphex.cli docs/plans/my-feature.md \
  --jira-task PROJ-123 \
  --allow-dirty
```

Diagnose differences between direct GigaCode and GigaLphex execution by running
this Python module from the affected project directory:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.diagnose
```

To reproduce one exact task prompt once:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.diagnose \
  --plan docs/plans/my-feature.md
```

For a concise Russian introduction covering OpenSpec, Superpowers, and a
regular markdown plan, see
[`docs/how-to-try.md`](docs/how-to-try.md), or open the
[`interactive setup page`](docs/how-to-try.html). For a more detailed real-task
testing guide, see
[`docs/real-task-testing-guide.md`](docs/real-task-testing-guide.md).

Run a plan in a separate git worktree, close to ralphex `--worktree`
behavior:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --worktree docs/plans/my-feature.md
PYTHONPATH=python python3 -m gigalphex.cli --worktree --branch=my-feature docs/plans/tasks.md
```

Initialize local project config:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --init
```

If you skip `--init`, the first real plan creation or plan execution initializes
the local `.gigalphex/config` automatically. Dry runs and review-only runs do
not auto-create it. Local prompt templates are not created automatically,
because their presence overrides the global prompt with the same filename.
Initialization also creates or updates `.gitignore` with `.DS_Store` and
`.gigalphex/progress/` and `.gigalphex/worktrees/`, so local runtime artifacts
stay out of normal commits.

Create editable project-specific prompt overrides only when needed:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --init-prompts
```

Initialize git automatically when creating or running a plan in a fresh folder:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --plan "add user authentication" --init-git
```

Without `--init-git`, `gigalphex` does not create a git repository for you. Plan
creation still works, but the created plan is left uncommitted outside git.
When `--init-git` creates a new repository, it commits the current files first
as `chore: initialize repository`, then continues with plan creation or
execution.

Create a new executable plan:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --plan "add user authentication"
```

The default interactive mode requires the GigaCode `planning` skill. The skill
creates the requested file under `docs/plans/`; after the GigaCode session
exits, `gigalphex` verifies the file and commits it when configured to do so.
If the installed GigaCode version needs different interactive CLI arguments,
set `gigacode_interactive_args` while keeping a `{prompt}` placeholder.

Create a plan without the skill or from automation:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --plan "add user authentication" --quick
```

Generated plans are requested entirely in the same language as the `--plan`
text, including structural headings. Russian plans may use `# План`,
`## Обзор`, `## Контекст`, `### Задача N:`, and `## Проверка`; they are parsed
and executed exactly like their English equivalents.
By default, a newly created plan is committed as `docs: add plan <name>` when
the current directory is inside a git repository. Use `--no-commit-plan` or
`commit_plan_on_creation = false` to leave the plan uncommitted.
When a full run finishes and moves the plan into `docs/plans/completed/`, that
move is committed as `docs: complete plan <name>`.
With `--jira-task PROJ-123`, these GigaLphex-created commits become
`PROJ-123 docs: ...`, and plan creation happens on
`feature/PROJ-123-<plan-description>`.

Run review with a different GigaCode model:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --review --review-model <model-name>
PYTHONPATH=python python3 -m gigalphex.cli docs/plans/my-feature.md --review-model <model-name>
```

Review the current `HEAD` against an explicit branch or other Git ref:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --review --base-ref develop
```

Run tests:

```bash
PYTHONPATH=python python3 -m unittest discover -s tests
```

Review behavior:

- default: parallel review with `quality`, `implementation`, `testing`,
  `simplification`, and `documentation` agents
- reviewers return machine-validated `<FINDING>` blocks with severity,
  category, file, evidence, impact, and a minimal suggested fix
- malformed review output is rejected instead of being forwarded to synthesis
- synthesis receives normalized findings inside an explicit untrusted-data
  boundary and independently verifies every claim against the repository
- pass `--base-ref REF` to compare `REF...HEAD`; without it, the default branch
  is auto-detected
- reviewers only inspect and report findings; they do not edit or commit
- synthesis uses `task_model`, verifies reported findings, and is the only
  stage that may fix, test, and commit changes
- finalize runs after a successful review by default; pass `--no-finalize` to
  skip the final validation/cleanup pass
- fallback: pass `--no-parallel-review` to use one read-only reviewer followed
  by the same synthesis stage
- limit fan-out with `--review-workers N`
- kill stuck sessions with `--session-timeout SECONDS`
- kill silent sessions with `--idle-timeout SECONDS`; any stdout bytes reset
  the timer even when the process has not emitted a complete line
- retry failed sessions with `--retry-count N --retry-delay SECONDS`
- when a successful task session leaves its selected checkbox or prose-task
  marker pending, automatically recheck the unchanged task boundary and retry
  it up to `retry_count` times with an explicit corrective prompt; unsafe
  changes to later tasks or read-only OpenSpec context are never retried
- before retrying a failed task process, GigaLphex restores the plan snapshot
  from the start of that iteration so the retry selects the same task; partial
  code changes remain available for the next attempt
- if a task session times out after a clean task commit, the completed
  iteration is accepted without rerunning the task agent
- only exhausted task retries stop for operator inspection; the plan is restored
  to the current task before the run exits
- if a configured model returns `API Error: 404 Model not found`, retry the same
  prompt without `--model` and use GigaCode's default model for later calls
- classify transient failures with `retry_patterns`
- classify rate limits with `rate_limit_patterns`; pass
  `--wait-on-rate-limit SECONDS` to wait longer before retrying those failures

Configure GigaCode:

```ini
[gigalphex]
gigacode_command = gigacode
gigacode_args = --approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}
gigacode_interactive_args = --prompt-interactive {prompt} --approval-mode=auto-edit
gigacode_skills_dir = ~/.gigacode/skills
task_model =
review_model =
default_branch =
prompts_dir = .gigalphex/prompts
session_timeout = 1800
idle_timeout = 900
retry_count = 1
retry_delay = 5
retry_patterns = FYA_TRANSIENT_TIMEOUT,API Error: 529,API Error: 502,API Error: 503,API Error: 504,502 Bad Gateway,503 Service Unavailable,504 Gateway Timeout
rate_limit_patterns = Rate limit exceeded,rate limit reached,429 Too Many Requests,quota exceeded,insufficient_quota,You've hit your usage limit
wait_on_rate_limit =
review_workers = 5
finalize_enabled = true
create_branch = true
worktree = false
move_plan_on_completion = true
commit_plan_on_creation = true
allow_dirty = false
```

Configuration loading priority, from lowest to highest:

1. embedded defaults
2. global config at `~/.config/gigalphex/config`
3. project config at `.gigalphex/config`
4. a file passed with `--config`
5. supported `GIGALPHEX_*` environment variables
6. CLI arguments

The CLI creates the global directory `~/.config/gigalphex/`, a commented
`~/.config/gigalphex/config` template, and all seven prompt templates under
`~/.config/gigalphex/prompts/` automatically. If global files cannot be
created, it creates `.gigalphex/config` and `.gigalphex/prompts/` in the
current project instead.
Existing global config files and customized prompts are never overwritten.
Global prompts that still match an earlier installed default are upgraded to
the current embedded default automatically.

Git behavior:

- plan runs create/switch to a branch derived from the plan filename
- `--jira-task TASK` enforces `feature/TASK-<description>` branch names for
  plan creation and execution, and automatically prefixes new commit messages
  with `TASK `; adding a missing prefix rewrites the new local commit objects,
  so their SHAs change before the run continues
- `--worktree` runs full and tasks-only plan execution in
  `.gigalphex/worktrees/<branch>` instead of switching the current checkout
- `--branch` overrides the branch name for normal branch switching and
  worktree runs
- review-only mode does not switch branches
- `--review --base-ref REF` validates the ref and compares it with the current
  `HEAD`
- dirty working trees are rejected unless `--allow-dirty` is passed; with
  `--allow-dirty`, review prompts include committed, staged, unstaged, and
  untracked changes via `git status --short`, `git diff --cached`, and `git diff`
- completed full runs move the plan file to `completed/`
- use `--no-branch` or `--no-move-plan` to disable those steps

Prompt customization:

- global editable defaults are created automatically in
  `~/.config/gigalphex/prompts/`, with local files used as a fallback
- `--init-prompts` creates project-specific overrides in
  `.gigalphex/prompts/`
- both directories use `make_plan.txt`, `plan_skill.txt`, `task.txt`, `review.txt`,
  `review_agent.txt`, `review_synthesis.txt`, and `finalize.txt`
- loading priority is local prompts directory, then `~/.config/gigalphex/prompts`, then embedded defaults

Bundled skills:

- `--install-planning-skill` installs the bundled skill globally
- `--install-superpowers-converter-skill` installs the bundled
  `superpowers-to-gigalphex` skill for turning `docs/superpowers/specs/` into
  executable plans or normalizing `docs/superpowers/plans/` into `docs/plans/`;
  implementation plans can also be executed directly without conversion
- `--skill-dir PATH` overrides the configured GigaCode skills directory
- `--force-skill-install` replaces an existing modified bundled skill
- interactive `--plan` checks for `<skills-dir>/planning/SKILL.md` before
  launching GigaCode and suggests `--quick` when the skill is unavailable

After installing the converter skill, use it from an interactive GigaCode
session, for example:

```text
Use the superpowers-to-gigalphex skill to convert docs/superpowers/specs/2026-07-01--demo.md into docs/plans/demo.md.
```

Model selection:

- GigaCode exposes model selection as `-m/--model`, separate from the one-shot
  `-p/--prompt` option and `--prompt-interactive`.
- `plan_model`, `task_model`, `review_model`, and `finalize_model` add
  `--model <name>` to the GigaCode invocation for their phases.
- review agents use `review_model`, falling back to `task_model`.
- synthesis/fixes always use `task_model`, the same model as task execution.
- `plan_model` falls back to `task_model`, and `finalize_model` falls back to
  `review_model`/`task_model`.
# gigalphex
