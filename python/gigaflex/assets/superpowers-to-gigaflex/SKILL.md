---
name: superpowers-to-gigaflex
description: Convert a Superpowers design spec or implementation plan into a gigaflex-compatible markdown plan. Use when the user asks to migrate or convert docs/superpowers/specs, docs/superpowers/plans, superpowers:brainstorming output, or superpowers:writing-plans output for execution by gigaflex.
---

# Convert Superpowers Work Into GigaFlex Plans

Create an executable markdown plan for `gigaflex`. Do not implement the plan,
edit product code, create branches, or start an execution loop.

## Inputs

- Use the source path provided by the user.
- If no path is provided, look for likely sources in `docs/superpowers/specs/`
  and `docs/superpowers/plans/`. If more than one plausible source exists, ask
  which one to convert.
- Treat Superpowers design specs and Superpowers implementation plans as valid
  inputs.

## Workflow

1. Read the complete source artifact.
2. Inspect the repository context needed to make the plan executable: README,
   build/test configuration, files named by the source, and nearby existing
   patterns.
3. Determine the source type:
   - Superpowers spec/design: synthesize a fresh gigaflex implementation plan.
   - Superpowers implementation plan: normalize it into gigaflex format,
     preserving requirements while removing Superpowers execution mechanics.
4. Write the converted plan to the user-specified destination, or to a
   non-colliding file under `docs/plans/YYYYMMDD-<short-title>.md`.
5. Report the created path and any material unresolved questions. Stop.

## Conversion Rules

- Preserve user-approved requirements, constraints, success criteria, interface
  decisions, and validation expectations.
- Remove Superpowers-only process directives, including `REQUIRED SUB-SKILL`,
  `superpowers:subagent-driven-development`, `superpowers:executing-plans`,
  fresh-subagent dispatch, task-reviewer gates, `.superpowers/sdd/progress.md`,
  finishing-branch workflow, and explicit Superpowers ledger updates.
- Do not include a commit step as a task checkbox. GigaFlex requires each task
  session to commit completed implementation and plan updates itself.
- Remove process-only test-first sequencing such as separate RED/GREEN
  micro-steps. Preserve the required automated test coverage and validation
  outcomes alongside the behavior they verify.
- Keep tasks independently committable and mutually exclusive. A later task must
  not repeat implementation, tests, or validation owned by an earlier task.
- Put tests and validation in the same task as the behavior they verify.
- Use exact file paths and exact validation commands whenever the repository
  makes them knowable.
- Put actionable checkboxes only inside executable task sections.
- If a non-blocking detail is unknown, record it in `## Unanswered Questions`
  without checkboxes. If the missing detail blocks an executable plan, ask the
  user before writing the plan.
- Write the plan in the same language as the source artifact unless the user
  asks otherwise.

## Required Output Shape

Use this structure unless the user gives a stricter destination format:

```markdown
# Plan: <short title>

## Overview
Describe the goal and expected outcome.

## Context
- Relevant source spec or Superpowers plan path
- Important files, modules, constraints, assumptions, and risks
- Existing patterns to preserve

## Development Approach
- Selected implementation approach
- Testing approach
- Compatibility or migration notes

### Task 1: <independently committable unit>
- [ ] Implement the concrete behavior in `path/to/file`
- [ ] Handle relevant errors and edge cases
- [ ] Add or update focused tests in `path/to/test_file`
- [ ] Run `exact validation command`

### Task 2: <next independently committable unit>
- [ ] Implement the next concrete behavior
- [ ] Add or update focused tests
- [ ] Run the relevant validation command

## Validation
- Exact automated commands and any necessary manual checks

## Technical Details
- Important interfaces, data structures, formats, or processing flow

## Unanswered Questions
List only material unresolved questions without checkboxes. Remove this section
when nothing remains unresolved.

## Post-Completion
List external deployment, consuming-project, or manual follow-up work without
checkboxes. Remove this section when it is not needed.
```

Executable headings must use one supported form consistently:
`### Task N:`, `### Iteration N:`, `### Задача N:`, or `### Итерация N:`.
