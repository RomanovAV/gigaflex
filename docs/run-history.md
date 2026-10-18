# Run history and structured results

History is created automatically for plan execution (including OpenSpec and
tasks-only mode) and review. It starts after repository/plan setup. Plan
generation (`--plan`), setup commands, invalid arguments, and `--dry-run` do not
create execution history. Early errors before execution setup keep their
existing CLI diagnostics.

## Location and identity

The CLI prints `run history: ...` at startup and `run result: .../result.json`
after finishing. Records live in:

```text
<git-common-dir>/gigaflex/runs/<run_id>/<attempt_id>/
```

For a normal checkout, `<git-common-dir>` is `.git`. Linked worktrees share the
same history root. History stays outside tracked/untracked deliverables and is
retained until manually removed.

- `run_id` identifies the logical execution. A normal launch or `--restart`
  creates a new one, even for the same plan.
- `attempt_id` uniquely identifies one CLI invocation. It matches the current
  invocation's progress-snapshot `run_id` (the older progress field names the
  invocation, not the logical execution).
- `--resume` retains `run_id`, creates a new `attempt_id`, and references the
  stopped attempt through `parent_attempt_id`. The link is saved with the
  stopped checkpoint and checked against the original identity and base commit.
- Old checkpoints or removed history may lack a usable parent. Such a resume
  records `mode: resume`, `lineage: unavailable`, and a null parent. Do not count
  it as an independent new task merely because a new ID had to be allocated.
- Rejected launches are recorded when they reach history setup. For example,
  launching without `--resume` while stopped work exists leaves
  `execution_started: false` and does not replace the stopped attempt's link.

Group attempts by `run_id` when counting tasks. Sum invocation usage/time across
the group's attempts when measuring cumulative cost; preserve per-attempt
outcomes. A plain new run may reuse completed phase checkpoints: those
dependencies appear in `reused_phases` with the original history reference when
available. It is not a new execution of those phases.

## Files (schema version 1)

| File | Contents |
|---|---|
| `started.json` | Immutable identity, mode, lineage, base, and start time |
| `inputs.json` | Input plan/OpenSpec context, effective settings and prompt snapshots, runtime identity, initial repository state, continuation checkpoint |
| `events.jsonl` | Ordered structured observations with timestamps and sequence numbers |
| `progress.txt` | Transcript and diagnostics from this invocation only, mirrored during execution |
| `stats.json` | Final statistics for this attempt only |
| `status.json` | Final dashboard snapshot, including phase and task states |
| `checkpoint.json` | Checkpoint state when the attempt finished |
| `result.json` | Terminal outcome, evidence, statistics, and artifact references |

JSON documents use atomic publication within a unique attempt directory and
completed documents are not overwritten. Events are appended while executing.
These are local operational records, not tamper-proof storage.

`started.json` always retains its original `status: running`. Use `result.json`
for the terminal status. If it is absent (for example, after process termination
or a failed history write), completion is unknown. Preserve that attempt in
analysis instead of dropping it or treating it as successful. The journal and
partial files can still explain how far execution got.

## Input versions

`inputs.json` retains plan and OpenSpec context contents with SHA-256 hashes,
effective GigaFlex configuration and phase arguments, and the exact loaded
GigaFlex prompt templates with their hashes. Configuration includes resolved
CLI/environment overrides; arbitrary environment variables are not exported.

Repository state records `head`, working-tree `tree`, staged `index`, and a
signature of configured ignored artifacts. Orchestration files are excluded.
The runtime records the package version, a digest of Python package sources,
Python version, and platform. These identify observations; they do not constitute
a full repository backup, retained Git refs, dependency image, or replay bundle.
Task recovery bundles remain a separate mechanism.

GigaCode version and loaded native instructions (`GIGACODE.md`, rules, skills)
are not observed by this implementation. They are explicitly unknown rather
than inferred from configuration or filesystem presence. Requested model
settings are in the configuration; reported models and known token usage are
in invocation statistics and executor events, including fallback events.

Snapshots contain local plan, prompt, and configuration contents. They stay in
the repository's Git metadata and are not sent to another service by history
recording.

## Reading the result

- `status`, `exit_code`, and `failure` describe the CLI attempt. Statuses follow
  the existing statistics contract: `success`, `blocked`, `failed`, or
  `interrupted`.
- `validation` preserves every runner-owned check report, including failed
  attempts preceding a successful repair. Each report is bound to the observed
  repository state where available. `not_configured` means no explicit checks
  were configured for that phase; it is not a passed test.
- `review.passes`, `review.findings`, and `review.decisions` preserve parsed
  review observations, including rejected/blocked decisions and empty final
  passes. Agent claims are not promoted to independently proven root causes.
  Findings from configured check failures identify `runner_validation` as
  their agent/source.
- `tasks` records task starts and accepted, promoted completions in event
  sequence. `retries` includes executor retries and runner completion/repair
  retries. Executor invocation events have unique `call_id` values.
- `verification` describes the reviewed result before plan archival.
  `repository` describes the final checkout, including archival when performed.
- `statistics` includes wall time, invocations, reported models, and known token
  usage. Missing usage is unknown, including when an executor did not emit a
  completed result. Currency cost is `null` with `status: not_reported`.
- `artifacts` contains attempt-relative references plus the live progress path
  and saved recovery location when available. Live files may change on later
  invocations; archived files remain the evidence for this attempt.
- `evidence_complete` becomes false on a recorded journal error, missing input
  snapshot, or unavailable final repository state. Unknown external instruction
  loading and unavailable currency cost are separately declared limitations.

The module records observations only. It does not generate diagnoses, rewrite
instructions, run benchmarks, or aggregate a performance score.
