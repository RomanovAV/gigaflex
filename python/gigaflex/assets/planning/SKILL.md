---
name: planning
description: Create gigaflex-compatible implementation plans with interactive context gathering.
---

# Interactive Implementation Planning

Create a structured implementation plan for the user's request. Inspect the
repository first, ask only useful questions, and stop after writing the plan.
Do not begin implementation.

If the invoking prompt supplies an exact output path, create exactly that file.
Otherwise create a non-colliding file under
`docs/plans/YYYYMMDD-<short-task-name>.md`.

## 1. Discover Context

Before asking questions:

1. Classify the request as feature work, bug fixing, refactoring, migration, or
   unclear/general work.
2. Inspect the relevant repository context using available read-only tools:
   - project structure, README, build and test configuration
   - related code and existing implementation patterns
   - tests covering the affected area
   - dependencies and integration points
   - git status and recent changes when relevant
3. Summarize what appears to be involved, including concrete files or modules.

Use an exploration subagent when available and useful. Do not edit source files
during discovery.

## 2. Ask Focused Questions

Present the discovered context, then ask questions one at a time with the
interactive user-question tool.

Prioritize:

1. the exact outcome or acceptance criteria
2. scope boundaries and affected components
3. important constraints or compatibility requirements
4. testing preference when it materially changes the plan

Offer a recommended answer when the repository context supports one. Stop
asking as soon as enough information exists to produce a concrete plan. Do not
ask ceremonial questions whose answers can be inferred from the codebase.

If a material question remains unanswered, list it in an `Unanswered Questions`
section without checkboxes.

## 3. Choose an Approach

When more than one realistic implementation direction exists:

1. present two or three approaches conversationally
2. lead with the recommended approach
3. explain the important trade-offs
4. ask the user to choose one

Skip this step when the path is obvious, the user already selected an
approach, or the request is a narrow bug fix.

## 4. Create the Plan

Ensure the destination directory exists and avoid overwriting another plan.
Write the plan in the same language as the user's request.

Use this structure:

```markdown
# Plan: <short title>

## Overview
Describe the goal, expected outcome, and integration with the existing system.

## Context
- Relevant files and components
- Existing patterns to preserve
- Dependencies, constraints, assumptions, and risks

## Development Approach
- Selected implementation approach and why
- Testing approach
- Compatibility or migration requirements

### Task 1: <one independently committable unit>
- [ ] Concrete implementation step with file or module reference
- [ ] Handle relevant errors and edge cases
- [ ] Add or update focused tests
- [ ] Run the relevant validation command

### Task 2: <next independently committable unit>
- [ ] Concrete implementation step with file or module reference
- [ ] Add or update focused tests
- [ ] Run the relevant validation command

## Validation
- Exact automated commands and any necessary manual checks

## Technical Details
- Important interfaces, data structures, formats, or processing flow

## Unanswered Questions
List only material unresolved questions, without checkboxes. Remove this
section when nothing remains unresolved.

## Post-Completion
List external deployment, consuming-project, or manual follow-up work without
checkboxes. Remove this section when it is not needed.
```

Plan rules:

- Executable headings must consistently use `### Task N:`,
  `### Iteration N:`, `### Задача N:`, or `### Итерация N:`.
- Put actionable checkboxes only inside executable task sections.
- Keep each task independently committable and small enough for one agent run.
- Keep task ownership mutually exclusive. A later task must not repeat
  implementation, tests, or validation already included in an earlier task.
- Put tests and validation in the same task as the behavior they verify. Use a
  separate integration-test task only for a genuinely distinct integration
  boundary that earlier tasks cannot validate.
- Include tests for code behavior that is added or changed.
- Use exact validation commands discovered from the repository.
- Do not invent mandatory coverage percentages or irrelevant test work.
- Keep external/manual actions outside task checkboxes.
- Prefer the smallest design that satisfies the request.

## 5. Stop

After writing the plan:

1. report the created path
2. briefly mention any unresolved questions
3. stop and return control to the user

Do not implement tasks, modify source files, create a branch, or start the
gigaflex execution loop.
