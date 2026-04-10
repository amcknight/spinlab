---
name: execute-plan
description: Execute an implementation plan end-to-end in a container, finishing with a PR. Reads project-specific instructions from CLAUDE.md.
---

# Execute Plan

Implements a pre-written implementation plan inside a container. The plan was authored locally (e.g., via brainstorming/planning workflows). This skill handles pure execution: branch, implement, verify, PR.

All project-specific conventions (test commands, build steps, coding standards) come from CLAUDE.md in the repo root. Read and follow it.

## Container Guard

Check for container indicators before proceeding:
- `/.dockerenv`

If not in a container, stop and tell the user:
> This skill is container-only. Run via: `sbx run claude -- "/execute-plan <name>"`

## Step 1: Read the Plan

Read the plan file identified by the caller. Extract:
- Goal and architecture
- File structure (creates/modifies)
- Numbered tasks with acceptance criteria

Create a TodoWrite checklist from the plan's tasks before writing any code.

## Step 2: Branch

```bash
git checkout -b plan/<plan-name-without-date>
```

Example: `2026-04-07-vite-dev-server-integration.md` becomes `plan/vite-dev-server-integration`.

## Step 3: Execute Tasks

For each task in order:

1. Read the task requirements and acceptance criteria
2. Write a failing test that covers the acceptance criteria
3. Implement the minimum code to pass
4. Run the project's test commands (per CLAUDE.md) to confirm green
5. Commit: `task N: <description>`
6. Mark done in TodoWrite

Fix failures before moving to the next task.

## Step 4: Verify

Run the project's full verification suite as specified in CLAUDE.md (all test commands, type checks, linting, etc.). All must pass before proceeding.

## Step 5: PR

```bash
git push -u origin HEAD
gh pr create --title "<concise title from plan goal>" --body "$(cat <<'EOF'
## Summary
Implements plan: `docs/superpowers/plans/<plan-file>`

<what was done, one bullet per task>

## Verification
- [x] Full test suite passes
- [x] Type checks pass (if applicable)

Executed by `/execute-plan` in container
EOF
)"
```

Report the PR URL as final output.
