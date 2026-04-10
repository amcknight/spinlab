#!/usr/bin/env bash
# Preflight check before executing a plan. Verifies the repo is in a clean
# state to run the plan from scratch, then creates a fresh branch.
#
# Usage:
#   scripts/plan-preflight.sh <branch-name>
#
# Exits 0 and leaves you on a freshly-created branch if preconditions hold.
# Exits non-zero (with a specific code per failure mode) if anything is
# wrong. Intended to be called from the /execute-plan skill so the agent
# cannot silently reuse a stale branch.
#
# Why this exists:
#   Previous headless runs of /execute-plan landed on an existing branch
#   (from a prior aborted run) and committed on top of it. Because the
#   stale branch was off a pre-fix commit, the resulting PR contained
#   "reverts" of unrelated main-branch fixes. There is no way for the
#   agent to ask the user in headless mode, so the only safe action is
#   to fail loud and let the operator prepare a clean state.
#
# Exit codes (use these to distinguish failures in callers / CI):
#   0  ok, branch created, checked out
#   10 missing branch-name argument
#   11 not in a git repository / git error
#   12 not currently on the main branch
#   13 working tree not clean (staged, unstaged, or untracked changes)
#   14 branch already exists locally
#   15 branch already exists on origin
#   16 main is not in sync with origin/main
#
# Destructive operations are NEVER taken. If the branch exists, the
# operator must delete it explicitly:
#
#     git branch -D <branch>                 # local
#     git push origin --delete <branch>      # remote
#     gh pr close <pr-number>                # any open PR

set -euo pipefail

BRANCH="${1:-}"
MAIN_BRANCH="main"

if [ -z "$BRANCH" ]; then
    echo "ERROR: plan-preflight.sh requires a branch name argument." >&2
    echo "Usage: scripts/plan-preflight.sh <branch-name>" >&2
    exit 10
fi

# Must be inside a git repo.
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: not inside a git repository." >&2
    exit 11
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "$MAIN_BRANCH" ]; then
    echo "ERROR: plan-preflight.sh must be run from '$MAIN_BRANCH'." >&2
    echo "       Currently on: $CURRENT_BRANCH" >&2
    echo "       Fix: git checkout $MAIN_BRANCH" >&2
    exit 12
fi

# Working tree must be clean (no staged, unstaged, or untracked changes).
# Untracked files are included on purpose: the plan should execute
# against a known state, not pick up random files.
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean. Plan execution requires a clean state." >&2
    echo "" >&2
    git status --short >&2
    echo "" >&2
    echo "       Fix: commit, stash, or discard the changes above before running the plan." >&2
    exit 13
fi

# Target branch must not exist locally.
if git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
    echo "ERROR: branch '$BRANCH' already exists locally." >&2
    echo "" >&2
    echo "       This usually means a prior /execute-plan run aborted or was rerun without cleanup." >&2
    echo "       The agent refuses to reuse an existing branch because a stale branch may" >&2
    echo "       be off a pre-fix base and will produce a PR with unrelated 'reverts'." >&2
    echo "" >&2
    echo "       Fix (from the host, not the sandbox):" >&2
    echo "           git branch -D $BRANCH" >&2
    echo "           git push origin --delete $BRANCH   # if it was pushed" >&2
    echo "           gh pr close <pr-number>            # if a PR was opened" >&2
    exit 14
fi

# Target branch must not exist on origin either. If it does, a prior push
# will silently succeed later and open a confusing PR.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "ERROR: branch '$BRANCH' already exists on origin." >&2
    echo "" >&2
    echo "       Fix (from the host):" >&2
    echo "           git push origin --delete $BRANCH" >&2
    echo "           gh pr close <pr-number>            # if a PR was opened" >&2
    exit 15
fi

# Main should be in sync with origin/main. If not, we're executing the
# plan against a stale base and the resulting PR will look confusing.
# This is a soft check: if the remote is unreachable we skip it rather
# than fail (e.g. offline dev mode).
if git rev-parse --verify --quiet "refs/remotes/origin/$MAIN_BRANCH" >/dev/null; then
    LOCAL=$(git rev-parse "$MAIN_BRANCH")
    REMOTE=$(git rev-parse "origin/$MAIN_BRANCH")
    BASE=$(git merge-base "$MAIN_BRANCH" "origin/$MAIN_BRANCH")
    if [ "$LOCAL" != "$REMOTE" ]; then
        if [ "$LOCAL" = "$BASE" ]; then
            echo "ERROR: local $MAIN_BRANCH is behind origin/$MAIN_BRANCH." >&2
            echo "       Fix: git pull --ff-only" >&2
            exit 16
        elif [ "$REMOTE" = "$BASE" ]; then
            # Local is ahead of remote. Unusual but not an error for plan
            # execution — the agent will push the new branch, not main.
            echo "note: local $MAIN_BRANCH is ahead of origin/$MAIN_BRANCH (unpushed commits)." >&2
        else
            echo "ERROR: local $MAIN_BRANCH and origin/$MAIN_BRANCH have diverged." >&2
            echo "       Fix: rebase or merge manually before running the plan." >&2
            exit 16
        fi
    fi
fi

# All checks passed — create and check out the branch.
git checkout -b "$BRANCH"
echo "[plan-preflight] ok — on fresh branch '$BRANCH' off $MAIN_BRANCH"
