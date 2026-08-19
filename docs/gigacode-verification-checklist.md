# GigaCode Verification Checklist

Use this checklist on a machine where `gigacode` is installed. Add notes,
outputs, and failures under each section as you test.

## Historical Baseline

These checks passed in prior verification runs with GigaCode `26.5.17`:

- `gigacode` is available at `/Users/19268765/.gigacode/bin/gigacode`.
- Unit tests passed from the repository root: `19/19`, then `20/20` after the
  prompt-placeholder fix.
- the first CLI launch created the global config and all global prompt templates.
- `--init` created the local project config without shadowing global prompts.
- `--init-prompts` created local prompt overrides on demand.
- `--plan --dry-run` printed the plan-generation prompt and did not invoke
  `gigacode`.
- Real `--plan` created markdown plans under `docs/plans/`.
- Generated plans were not wrapped in markdown code fences.
- Repeated `--plan` did not overwrite the first file; it created `-2.md`.
- Custom `.gigaflex/prompts/make_plan.txt` overrode the embedded prompt.
- The previous newline fix worked: `created plan:` and `progress log:` no
  longer stick to the last line of GigaCode output.
- Small task execution passed end-to-end on 2026-06-12 with the same explicit
  prompt and shell-policy flags (the current canonical ordering is
  `gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<prompt>'`).
  GigaCode created `SMOKE_TEST.md`, marked the task checkboxes,
  committed the changes, and emitted the completion signal. GigaCode marks
  `-p` as deprecated, but this remains the confirmed unambiguous one-shot form
  when approval and array-valued tool options are also present.

The formerly unresolved item was small task execution. When the prompt was sent
through stdin, GigaCode warned that `run_shell_command` needed approval and the
task failed before commit. Passing the prompt through `-p {prompt}` fixed the
invocation shape but did not allow shell commands by itself. GigaCode help for
26.5.17 says `--approval-mode=auto-edit` allows edit/write tools, while shell
commands require `--allowed-tools run_shell_command`. The current default uses
the confirmed explicit `-p` invocation with
`--allowed-tools run_shell_command`; the 2026-06-12 smoke run confirmed
autonomous commits without a manual follow-up.

## Current Retest Scope

Run these checks after updating to the latest code. The first two are quick
sanity checks; the third is the real regression test.

## 1. Check GigaCode Availability

```bash
command -v gigacode
gigacode --version
```

Expected:

- `gigacode` is found in `PATH`.
- Version command returns successfully.

Notes:

```text

```

## 2. Run Unit Tests From Repository Root

From the repository root, not from `/tmp`:

```bash
cd /path/to/gigaflex
PYTHONPATH=python python3 -m unittest discover -s tests
```

Expected:

- All tests pass.

Notes:

```text

```

## 3. Verify Small Task Execution With Shell Tool Allowed

Use a clean temporary git repository so the smoke test cannot disturb real work:

```bash
mkdir -p /tmp/gigaflex-task-check
cd /tmp/gigaflex-task-check
git init
git config user.email "test@example.com"
git config user.name "GigaFlex Test"
mkdir -p docs/plans
cat > README.md <<'EOF'
# Smoke Repo
EOF
git add README.md
git commit -m "initial commit"
```

Create `docs/plans/20260612-smoke.md`:

```md
# Plan: Smoke test

## Overview
Add a tiny smoke-test artifact.

## Context
This checks that gigaflex can run GigaCode non-interactively.

### Task 1: Add smoke file
- [ ] Create `SMOKE_TEST.md` with one short sentence.
- [ ] Mark this task complete.
- [ ] Commit the change.

## Validation
- git status --short
```

Run:

```bash
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.cli docs/plans/20260612-smoke.md --allow-dirty --tasks-only --no-move-plan
```

Expected:

- No warning like `Tool "run_shell_command" requires user approval`.
- The startup section logs
  `gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<prompt>'`,
  not the full prompt text.
- `SMOKE_TEST.md` is created and contains a non-empty sentence.
- The checkbox in the plan is marked `[x]`.
- A new commit is created after `initial commit`.
- The command exits successfully and prints `progress log: ...`.
- The command prints `dashboard: .../status-20260612-smoke.html`.
- `status-20260612-smoke.html` is self-contained and shows the completed run;
  `status-20260612-smoke.json` has `"status": "success"`.
- The progress log contains `<<<GIGAFLEX:ALL_TASKS_DONE>>>` or a clear success
  path, not `<<<GIGAFLEX:TASK_FAILED>>>`.

Collect:

```bash
git log --oneline --decorate -5
git status --short
cat docs/plans/20260612-smoke.md
cat SMOKE_TEST.md
cat .gigaflex/progress/progress-20260612-smoke.txt
cat .gigaflex/progress/status-20260612-smoke.json
```

Notes:

```text
Verified on 2026-06-12 in /tmp/gigaflex-task-check.

Command:
PYTHONPATH=/Users/19268765/IdeaProjects/gigaflex-new/python:$PYTHONPATH python3 -m gigaflex.cli docs/plans/20260612-smoke.md --allow-dirty --tasks-only --no-move-plan

Observed:
- No non-interactive shell approval warning.
- Startup logged: gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<prompt>'
- Created SMOKE_TEST.md with a non-empty sentence.
- Marked all three Task 1 checkboxes as [x].
- Created commits:
  81796c9 feat: mark smoke-task checkboxes complete
  e84517b feat: add smoke-test artifact
- Emitted the completion signal and exited successfully.
- Left only untracked .gigaflex/ progress files in the smoke repository.
```

## Optional Regression Checks

The following already passed historically. Re-run only if related code changed.

## 4. Verify Global Initialization and Local `--init`

```bash
mkdir -p /tmp/gigaflex-init-check
cd /tmp/gigaflex-init-check
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.cli --init
find .gigaflex -type f | sort
find ~/.config/gigaflex -type f | sort
```

Expected files:

```text
.gigaflex/config
~/.config/gigaflex/config
~/.config/gigaflex/prompts/finalize.txt
~/.config/gigaflex/prompts/make_plan.txt
~/.config/gigaflex/prompts/plan_skill.txt
~/.config/gigaflex/prompts/review.txt
~/.config/gigaflex/prompts/review_agent.txt
~/.config/gigaflex/prompts/review_synthesis.txt
~/.config/gigaflex/prompts/task.txt
```

The local `.gigaflex/prompts/` directory should not exist yet. Create local
overrides explicitly:

```bash
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.cli --init-prompts
```

## 5. Verify `--plan --dry-run`

```bash
cd /path/to/gigaflex
PYTHONPATH=python python3 -m gigaflex.cli --plan "add health check endpoint" --dry-run
```

Expected:

- Prints the plan-generation prompt.
- Does not invoke `gigacode`.
- Prints `progress log: .gigaflex/progress/progress-plan.txt`.

## 6. Install the Planning Skill

```bash
PYTHONPATH=python python3 -m gigaflex.cli --install-planning-skill
```

Expected:

- Reports `installed planning skill` or `planning skill already installed`.
- Creates `~/.gigacode/skills/planning/SKILL.md`.
- Running the command again does not overwrite an identical installed copy.
- A locally modified copy is preserved unless `--force-skill-install` is used.

If this GigaCode version uses another directory:

```bash
PYTHONPATH=python python3 -m gigaflex.cli \
  --install-planning-skill \
  --skill-dir /actual/gigacode/skills/path
```

Persist the discovered path if necessary:

```ini
[gigaflex]
gigacode_skills_dir = /actual/gigacode/skills/path
```

## 7. Verify Interactive Plan Generation

This check requires the `planning` skill installed in GigaCode and a real
terminal.

From the repository root:

```bash
cd /path/to/gigaflex
PYTHONPATH=python python3 -m gigaflex.cli --plan "add health check endpoint"
```

Expected:

- GigaCode opens interactively instead of returning one one-shot response.
- GigaFlex passes the planning request through `--prompt-interactive`.
- The `planning` skill inspects the repository and asks focused questions.
- Creates a file like `docs/plans/YYYYMMDD-add-health-check-endpoint.md`.
- The file contains a markdown plan.
- The plan includes `# Plan:`, `## Overview`, `## Context`, and at least one `### Task 1:` section.
- Task items use checkbox format: `- [ ] ...`.
- The saved file does not wrap the whole plan in markdown code fences.
- After exiting GigaCode, GigaFlex reports the created path and commits it
  when plan commits are enabled.

If this GigaCode version needs extra flags to launch its TUI, inspect
`gigacode --help` and configure them:

```ini
[gigaflex]
gigacode_interactive_args = <interactive flags> {prompt} --approval-mode=auto-edit
```

Notes:

```text

```

## 8. Verify Quick Plan Generation

```bash
PYTHONPATH=python python3 -m gigaflex.cli --plan "add quick health check plan" --quick
```

Expected:

- Uses the one-shot `make_plan.txt` prompt.
- Does not require the interactive `planning` skill.
- Creates a valid executable plan.

## 9. Verify Repeated Plan Generation Does Not Overwrite

Run the same command again:

```bash
PYTHONPATH=python python3 -m gigaflex.cli --plan "add health check endpoint"
```

Expected:

- Creates a second file with a numeric suffix, such as
  `docs/plans/YYYYMMDD-add-health-check-endpoint-2.md`.
- The first generated plan remains unchanged.

Notes:

```text

```

## 10. Verify Custom `make_plan.txt`

From the repository root:

```bash
cd /path/to/gigaflex
PYTHONPATH=python python3 -m gigaflex.cli --init-prompts
printf 'CUSTOM PLAN PROMPT: {plan_request}\n' > .gigaflex/prompts/make_plan.txt
PYTHONPATH=python python3 -m gigaflex.cli --plan "demo request" --quick --dry-run
```

Expected:

- Output contains `CUSTOM PLAN PROMPT: demo request`.
- This confirms local prompt templates override embedded defaults.

Notes:

```text

```

## 11. Verify OpenSpec Change Execution

Create a minimal local change containing `proposal.md`, `tasks.md`, and one
delta spec, then run:

```bash
PYTHONPATH=/path/to/gigaflex/python python3 -m gigaflex.cli \
  --openspec openspec/changes/approval-smoke \
  --tasks-only
```

Expected:

- `tasks.md` is used as the writable checklist.
- `proposal.md`, `design.md` when present, and `specs/**/*.md` are treated as
  read-only task context.
- Each numbered `## N. ...` task group is one iteration.
- The completed change is not moved or archived automatically.
- The CLI prints `ready to archive with: openspec archive approval-smoke`.
- Dashboard and statistics files use the change name.

## Things to Watch Closely

- Does `gigacode` receive the generated prompt through `-p`?
- Does GigaFlex pass the request through `--prompt-interactive` and keep the TUI open?
- Does the installed `planning` skill create the exact requested plan path?
- Does `--approval-mode=auto-edit --allowed-tools run_shell_command` avoid
  non-interactive approval failures?
- Does any run hang without output?
- Does generated markdown contain extra commentary or code fences?
- Does task execution emit one of the expected signals when done?
- Does the progress log contain enough detail to debug failures?

Notes:

```text

```
