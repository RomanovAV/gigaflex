# gigaflex

Python autonomous OpenSpec change and plan runner for GigaCode CLI.

For a compact LLM-oriented project reference, see
[`gigaflex-llms.txt`](gigaflex-llms.txt).

OpenSpec is the primary workflow: the change defines the contract, GigaFlex
executes one `tasks.md` group at a time, and the completed change passes an
isolated review and finalize gate before the owner archives it.

This is a small standalone rewrite of the useful ralphex core:

- execute local OpenSpec changes while keeping proposal, design, and delta
  specs read-only
- parse English and Russian markdown plans with level-two or level-three
  `Task N:` / `Iteration N:` / `Задача N:` / `Итерация N:` headings
- execute Superpowers `docs/superpowers/plans/*.md` implementation plans directly
- run one task section per agent iteration
- run each task iteration in a disposable snapshot worktree and promote only
  its validated linear commits; pre-existing dirty files stay outside task
  commits, and overlapping task/user changes stop before promotion
- keep detailed agent output in progress logs, provide agents only a bounded
  current-run snapshot, and generate a live local dashboard
- persist successful phase checkpoints and resume interrupted runs without
  repeating review or finalize when HEAD and the working tree are unchanged
- detect gigaflex completion signals
- run review and a default finalize pass
- run five specialist review agents in parallel in disposable detached
  worktrees, then synthesize/fix findings in the main worktree; follow-up
  passes always run quality and implementation plus every specialist that
  found an issue earlier in the review run
- compare the accumulated change against the immutable commit captured at the
  start of the run instead of auditing the whole repository from scratch
- create/switch a git branch from the plan filename
- optionally run a plan in an isolated git worktree
- guard against dirty working trees, with an explicit run-wide override
- move completed plans into `completed/`
- call `gigacode` through a configurable CLI boundary

Current assumption: GigaCode CLI is available in `PATH`. Task, review,
finalize, and quick-plan sessions use one-shot mode by default:

```bash
gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<generated prompt>'
```

The default argument template is
`--approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}`.
`gigaflex` replaces `{prompt}` with the generated prompt before invoking
GigaCode. If custom `gigacode_args` do not include `{prompt}`, GigaFlex adds
`-p <generated prompt>` instead of sending a non-interactive prompt through
stdin.
For the actual subprocess invocation GigaFlex also enforces
`--output-format stream-json`. Assistant text is decoded into the detailed
progress log, while lifecycle events update a concise local dashboard and final
`result` events provide exact token and timing statistics.
Custom non-interactive arguments cannot disable shell execution accidentally:
GigaFlex normalizes every plan, task, review, synthesis, and finalize invocation
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
.gigaflex/progress/status-my-feature.json
.gigaflex/progress/status-my-feature.html
```

The CLI prints the absolute dashboard path when the run starts. Open the HTML
file in any browser; it is self-contained, needs no HTTP server, and refreshes
while the run is active. It shows task checklist progress, the current phase,
the current review status and pass history, parallel review sessions, executor
retries, failures, elapsed time, and known token usage. The JSON file exposes
the same state for future integrations. The full
agent transcript and executor diagnostics remain in `progress-my-feature.txt`.

Prompts do not point agents at that append-only historical transcript. GigaFlex
refreshes `context-my-feature.txt` with at most the last 200 lines/50,000
characters written by the current process and labels it as a static prompt-time
snapshot. Earlier runs therefore cannot silently grow task and review context.

Successful phases are recorded in `checkpoint-my-feature.json`. After an
interruption, a normal plan run reuses a completed review and resumes at
finalize—or reuses finalize too—only when the plan identity, immutable base,
current `HEAD`, and a snapshot of the working tree still match exactly. Any
repository change invalidates the affected checkpoint. An explicit `--review`
request always performs a fresh review.

Run a standard local OpenSpec `spec-driven` change by passing its change
directory:

```bash
PYTHONPATH=python python3 -m gigaflex.cli \
  --openspec openspec/changes/add-dark-mode
```

GigaFlex uses the change's `tasks.md` as the writable checklist and provides
`proposal.md`, `design.md`, and all `specs/**/*.md` delta specs to each task
agent as read-only context. Each `## N. ...` group in `tasks.md` is one agent
iteration and one commit. Branch and progress names come from the change
directory rather than the generic `tasks.md` filename.

After every task group is complete, the first review pass uses five specialist
reviewers to inspect the accumulated change from the immutable base commit
captured at launch through the current result. They start from the full
`base...HEAD` diff, read changed files in context, and may inspect directly
related code or tests, but they do not perform an unrelated repository-wide
audit. Confirmed findings go through the scoped synthesis stage. Follow-up
passes always run `quality` and `implementation`, plus every specialist that
reported a finding earlier in the review run, before the default finalize pass.

GigaFlex invokes the configured GigaCode CLI in the target workspace for every
phase. Existing team skills, project rules, allowed tools, and GigaCode settings
therefore remain available instead of being replaced by the runner.

Localized prose-only task sections such as `## Задача 1: ...` are also
supported when an OpenSpec generator omits checkboxes. They start as pending;
after completing a section, the task agent adds a durable
`- [x] N. <title>` marker below its heading so later runs can resume safely.

Completing an OpenSpec run does not move `tasks.md` or archive the change.
GigaFlex prints the corresponding `openspec archive <change-name>` command so
the spec merge and archive remain an explicit OpenSpec lifecycle action.
Archived changes and changes without `tasks.md` are rejected.

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
PYTHONPATH=python python3 -m gigaflex.cli --install-planning-skill
```

Superpowers implementation plans with `## Task N:` or `### Task N:` headings
and step checkboxes can also be executed directly:

```bash
PYTHONPATH=python python3 -m gigaflex.cli \
  docs/superpowers/plans/2026-07-01-demo.md
```

Install the bundled Superpowers conversion skill when you want to turn a
Superpowers design spec into a plan, or normalize a plan by removing
Superpowers-specific execution mechanics:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --install-superpowers-converter-skill
```

The default destination is `~/.gigacode/skills/planning/SKILL.md`. Existing
customized content is preserved; use `--force-skill-install` to replace it
with the bundled version. For a GigaCode version using another skills
directory, pass `--skill-dir PATH` or configure `gigacode_skills_dir`.
The converter skill is installed as
`~/.gigacode/skills/superpowers-to-gigaflex/SKILL.md` and follows the same
overwrite rules.

Specialist and single-review sessions use `review_model`, keep the configured
shell approval arguments, and receive an explicit inspect-only prompt. Keeping
the normal invocation lets reviewers run inspection commands without an
interactive approval failure. The synthesis session uses `task_model` and is
the only review stage instructed to fix files or create commits.

Every specialist or single-review pass receives an ephemeral snapshot commit
containing the current tracked and non-ignored untracked working-tree state.
Each reviewer runs from its own detached temporary worktree, so ordinary file,
index, and detached-HEAD changes cannot collide with another reviewer or the
main working tree. The runner removes all review worktrees in a `finally` path,
prunes their git metadata, and deletes the temporary directory after every
review pass, including failed and interrupted passes. Review worktrees are an
isolation boundary for normal reviewer activity, not a security sandbox against
arbitrary commands that deliberately target the shared git repository.

Observed GigaCode constraints:

- GigaCode edits only inside its configured workspace. To let it work across
  sibling project directories, pass the appropriate GigaCode flag as, for
  example, `--gigacode-arg=--include-directories=/path/to/shared`. Extra
  GigaCode arguments are applied to both one-shot and interactive planning
  invocations. They can also be placed directly in `gigacode_args` and
  `gigacode_interactive_args`.
- GigaCode exposes administrative subcommands such as `mcp`, `extensions`,
  `auth`, `sandbox`, and `hooks`, but no task-execution subcommand.
  GigaFlex therefore uses the default `gigacode [query..]` command and does
  not assume a `gigacode task` command, JSON/REST API, or Python SDK.
- Non-interactive runs fail if GigaCode asks for shell approval without the
  shell tool being explicitly allowed. Real logs and GigaCode help indicate that
  `--approval-mode=auto-edit` must be paired with
  `--allowed-tools run_shell_command`; `gigaflex` includes both by default and
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
  for example `.gigaflex/progress/stats-my-feature.json`. It includes each
  GigaCode attempt, measured wall time, GigaCode/API durations when reported,
  model names, per-call tokens, aggregate tokens, total run wall time, and the
  sum of call durations. The CLI prints the absolute statistics path and records
  run status such as `success`, `failed`, or `interrupted`, plus the failing
  phase and reason when applicable. Summed call time may exceed wall time
  because review agents run in parallel.
- GigaCode runs on Node.js, so Node warnings such as
  `MaxListenersExceededWarning` may appear in combined output.

Run from this directory:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --dry-run ../e2e/testdata/test-plan.md
PYTHONPATH=python python3 -m gigaflex.cli docs/plans/my-feature.md
```

To enforce corporate Jira naming for a plan run, pass `--jira-task`. For
example, this switches or creates a branch like
`feature/PROJ-123-my-feature` and automatically adds `PROJ-123 ` to every new
commit created during the run:

```bash
PYTHONPATH=python python3 -m gigaflex.cli docs/plans/my-feature.md \
  --jira-task PROJ-123 \
  --allow-dirty
```

Diagnose differences between direct GigaCode and GigaFlex execution by running
this Python module from the affected project directory:

```bash
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.diagnose
```

The diagnostic also launches three simultaneous captured probes and stores
their outputs separately. This makes an `exit 139`/`SIGSEGV` or `libsecret`
failure that appears only under parallel load visible in the diagnosis. Change
the fan-out with `--parallel-workers N`, or pass `--parallel-workers 0` to skip
that probe. If the dependency crash is confirmed, refresh GigaCode credentials
and use `--review-workers 1` or `--no-parallel-review` as a temporary fallback.

To reproduce one exact task prompt once:

```bash
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.diagnose \
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
PYTHONPATH=python python3 -m gigaflex.cli --worktree docs/plans/my-feature.md
PYTHONPATH=python python3 -m gigaflex.cli --worktree --branch=my-feature docs/plans/tasks.md
```

Initialize local project config:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --init
```

If you skip `--init`, the first real plan creation or plan execution initializes
the local `.gigaflex/config` automatically. Dry runs and review-only runs do
not auto-create it. Local prompt templates are not created automatically,
because their presence overrides the global prompt with the same filename.
Initialization also creates or updates `.gitignore` with `.DS_Store` and
`.gigaflex/progress/` and `.gigaflex/worktrees/`, so local runtime artifacts
stay out of normal commits.

Create editable project-specific prompt overrides only when needed:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --init-prompts
```

Initialize git automatically when creating or running a plan in a fresh folder:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --plan "add user authentication" --init-git
```

Without `--init-git`, `gigaflex` does not create a git repository for you. Plan
creation still works, but the created plan is left uncommitted outside git.
When `--init-git` creates a new repository, it commits the current files first
as `chore: initialize repository`, then continues with plan creation or
execution.

Create a new executable plan:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --plan "add user authentication"
```

The default interactive mode requires the GigaCode `planning` skill. The skill
creates the requested file under `docs/plans/`; after the GigaCode session
exits, `gigaflex` verifies the file and commits it when configured to do so.
If the installed GigaCode version needs different interactive CLI arguments,
set `gigacode_interactive_args` while keeping a `{prompt}` placeholder.

Create a plan without the skill or from automation:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --plan "add user authentication" --quick
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
With `--jira-task PROJ-123`, these GigaFlex-created commits become
`PROJ-123 docs: ...`, and plan creation happens on
`feature/PROJ-123-<plan-description>`.

Run review with a different GigaCode model:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --review --review-model <model-name>
PYTHONPATH=python python3 -m gigaflex.cli docs/plans/my-feature.md --review-model <model-name>
```

Review the current `HEAD` against an explicit branch or other Git ref:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --review --base-ref develop
```

For a plan run, GigaFlex normally captures the branch and exact `HEAD` commit
that are checked out before it creates or switches to the execution branch.
That immutable commit becomes the review base. The branch label and commit are
stored in local Git config for the execution branch, so a later `--review`
restores the same base even if the original branch has advanced. `--base-ref`
overrides this selection for both plan runs and standalone review runs; the
resolved commit is then stored for future reviews of that execution branch.
If an execution branch predates this metadata and is already checked out, pass
`--base-ref` once; GigaFlex refuses to guess and use the feature branch itself.

Run tests:

```bash
PYTHONPATH=python python3 -m unittest discover -s tests
```

Review behavior:

- default: parallel review with `quality`, `implementation`, `testing`,
  `simplification`, and `documentation` agents on the first pass
- follow-up passes always run `quality` and `implementation`, plus every
  specialist that reported a finding in any earlier pass of the current review
  run
- implementation review always verifies that the requested result was actually
  produced, whether the deliverable is code, data, analysis, documentation, or
  a mixture
- any executable source, script, migration, SQL, build logic, code-bearing
  notebook, or runtime configuration keeps the full code-review and focused
  automated-test requirements; non-code context never weakens those checks
- non-executable deliverables use appropriate validation such as source and
  calculation verification, reproducibility, schema/link integrity, and
  requirement coverage without inventing a requirement to write code
- reviewers return machine-validated `<FINDING>` blocks with severity,
  category, file, evidence, impact, and a minimal suggested fix
- explanatory text around an otherwise unambiguous `NO FINDINGS` result or
  complete `<FINDING>` blocks is removed deterministically; field values remain
  strictly validated, and ambiguous or incomplete output gets one focused
  format retry instead of a fresh repository review
- when every reviewer reports no findings, the runner skips synthesis entirely
- synthesis receives normalized findings with stable `F001`-style identifiers
  inside an explicit untrusted-data boundary, plus a compact scope containing
  only the files named by those findings
- synthesis starts from path-limited diffs and scoped file reads; it may inspect
  a directly necessary dependency, source, or focused test, but is instructed
  not to repeat a repository-wide review
- synthesis must return one machine-validated `fixed`, `rejected`, `confirmed`,
  or `blocked` decision for every input identifier; missing, duplicate, or
  invented identifiers and free-form summaries trigger one scoped automatic
  ledger-reconciliation pass
- the runner checks that the number and exact set of processed findings match
  the input; the structured decisions, rather than the completion signal, are
  authoritative. All-rejected ledgers complete review, fixed or confirmed
  findings require another specialist pass. A `blocked` decision gets one
  focused audit against the repository, plan, and named external sources;
  only a verified blocker that remains after that audit stops the run
- accepted `fixed` and `rejected` decisions are retained in a compact in-run
  memory and supplied to later review and synthesis passes, preventing agents
  from reopening settled claims merely to prefer a different valid convention;
  current repository evidence can still override stale memory
- explanatory synthesis text is removed when the complete expected decision
  ledger can be recovered deterministically; if scoped ledger reconciliation is
  still malformed, the runner stops with a review-protocol error instead of
  repeating the full specialist review
- plan runs compare against the exact commit checked out when the run started,
  or the base already stored for an existing execution branch
- standalone `--review` restores that stored commit; when none is stored, pass
  `--base-ref REF` once instead of silently reviewing against `main` or `master`
- reviewers only inspect and report findings; they do not edit or commit
- synthesis uses `task_model`, verifies reported findings, and is the only
  stage that may fix, test, and commit deliverable changes; runner-owned plan,
  progress, prompt-context, checkpoint, status, and statistics files remain
  read-only during synthesis
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
- before retrying a failed task process, GigaFlex restores the plan snapshot
  only while the isolated task HEAD is unchanged; it never rewrites a checklist
  behind an already-created commit
- if a task session times out after a clean task commit, the completed
  iteration is accepted without rerunning the task agent
- a contradictory `TASK_FAILED` marker is treated as a protocol warning when
  repository validation proves a clean committed completion
- only exhausted task retries stop for operator inspection; the plan is restored
  to the current task before the run exits
- if a configured model returns `API Error: 404 Model not found`, retry the same
  prompt without `--model` and use GigaCode's default model for later calls
- classify transient failures with `retry_patterns`
- classify `exit 139`/`SIGSEGV` and libsecret crash signatures as external
  dependency crashes; failed parallel reviewers receive one additional
  sequential retry without rerunning successful reviewers
- classify rate limits with `rate_limit_patterns`; pass
  `--wait-on-rate-limit SECONDS` to wait longer before retrying those failures

Configure GigaCode:

```ini
[gigaflex]
gigacode_command = gigacode
gigacode_args = --approval-mode=auto-edit --allowed-tools run_shell_command -p {prompt}
gigacode_interactive_args = --prompt-interactive {prompt} --approval-mode=auto-edit
gigacode_skills_dir = ~/.gigacode/skills
plan_model =
task_model =
review_model =
finalize_model =
default_branch =
prompts_dir = .gigaflex/prompts
session_timeout = 1800
idle_timeout = 900
retry_count = 1
retry_delay = 5
retry_patterns = FYA_TRANSIENT_TIMEOUT,API Error: 529,API Error: 502,API Error: 503,API Error: 504,502 Bad Gateway,503 Service Unavailable,504 Gateway Timeout
rate_limit_patterns = Rate limit exceeded,rate limit reached,429 Too Many Requests,quota exceeded,insufficient_quota,You've hit your usage limit
wait_on_rate_limit =
review_workers = 5
review_iterations = 10
finalize_enabled = true
create_branch = true
worktree = false
move_plan_on_completion = true
commit_plan_on_creation = true
allow_dirty = false
```

Leave `default_branch` empty to capture the branch checked out at launch. The
setting remains as a legacy persistent equivalent of `--base-ref` when an
explicit review base is required for every run.

Configuration loading priority, from lowest to highest:

1. embedded defaults
2. global config at `~/.config/gigaflex/config`
3. project config at `.gigaflex/config`
4. a file passed with `--config`
5. supported `GIGAFLEX_*` environment variables
6. CLI arguments

The CLI creates the global directory `~/.config/gigaflex/`, a commented
`~/.config/gigaflex/config` template, and all seven prompt templates under
`~/.config/gigaflex/prompts/` automatically. If global files cannot be
created, it creates `.gigaflex/config` and `.gigaflex/prompts/` in the
current project instead.
Existing global config files and customized prompts are never overwritten.
Global prompts that still match an earlier installed default are upgraded to
the current embedded default automatically.

Git behavior:

- plan runs capture the starting branch and immutable `HEAD` commit before they
  create/switch to a branch derived from the plan filename
- the captured base is stored as branch-local metadata in `.git/config`; resume
  and standalone review reuse it, while `--base-ref REF` explicitly replaces it
- `--jira-task TASK` enforces `feature/TASK-<description>` branch names for
  plan creation and execution, and automatically prefixes new commit messages
  with `TASK `; adding a missing prefix rewrites the new local commit objects,
  so their SHAs change before the run continues
- `--worktree` runs full and tasks-only plan execution in
  `.gigaflex/worktrees/<branch>` instead of switching the current checkout
- `--branch` overrides the branch name for normal branch switching and
  worktree runs
- review-only mode does not switch branches
- `--base-ref REF` resolves the ref to a commit, validates that it is an ancestor
  of the execution `HEAD`, and uses that immutable commit for plan or review runs
- dirty working trees are rejected unless `--allow-dirty` is passed
- `--allow-dirty` applies throughout the run: newly uncommitted paths left by a
  completed task or finalize pass are logged and carried into the next phase
  instead of stopping execution; task checklist completion and a task commit
  are still required
- with `--allow-dirty`, review prompts include committed, staged, unstaged, and
  untracked changes via `git status --short`, `git diff --cached`, and `git diff`
- completed full runs move the plan file to `completed/`
- use `--no-branch` or `--no-move-plan` to disable those steps

Prompt customization:

- global editable defaults are created automatically in
  `~/.config/gigaflex/prompts/`, with local files used as a fallback
- `--init-prompts` creates project-specific overrides in
  `.gigaflex/prompts/`
- both directories use `make_plan.txt`, `plan_skill.txt`, `task.txt`, `review.txt`,
  `review_agent.txt`, `review_synthesis.txt`, and `finalize.txt`
- loading priority is local prompts directory, then `~/.config/gigaflex/prompts`, then embedded defaults

Bundled skills:

- `--install-planning-skill` installs the bundled skill globally
- `--install-superpowers-converter-skill` installs the bundled
  `superpowers-to-gigaflex` skill for turning `docs/superpowers/specs/` into
  executable plans or normalizing `docs/superpowers/plans/` into `docs/plans/`;
  implementation plans can also be executed directly without conversion
- `--skill-dir PATH` overrides the configured GigaCode skills directory
- `--force-skill-install` replaces an existing modified bundled skill
- interactive `--plan` checks for `<skills-dir>/planning/SKILL.md` before
  launching GigaCode and suggests `--quick` when the skill is unavailable

After installing the converter skill, use it from an interactive GigaCode
session, for example:

```text
Use the superpowers-to-gigaflex skill to convert docs/superpowers/specs/2026-07-01--demo.md into docs/plans/demo.md.
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
