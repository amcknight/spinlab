#!/usr/bin/env bash
# Push a branch and open a PR from the host machine, using PR metadata
# that a sandbox agent left behind in .pr/body.md.
#
# The sandbox has no GitHub credentials, so it cannot `git push` or
# `gh pr create` directly. Instead, the agent commits its work and writes
# the intended PR body to .pr/body.md (and optionally .pr/title.txt).
# The user then runs this script from their normal terminal, which uses
# their existing `gh auth` to push and open the PR.
#
# Usage:
#   scripts/finish-pr.sh                  # uses current branch
#   scripts/finish-pr.sh <branch-name>    # checks out + pushes that branch

set -euo pipefail

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
BODY_FILE=".pr/body.md"
TITLE_FILE=".pr/title.txt"

if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    echo "refusing to PR from $BRANCH" >&2
    exit 1
fi

if [ ! -f "$BODY_FILE" ]; then
    echo "no $BODY_FILE found — the sandbox agent should write the PR body there" >&2
    exit 1
fi

if [ -f "$TITLE_FILE" ]; then
    TITLE=$(head -n1 "$TITLE_FILE")
else
    # Fall back to the subject of the latest commit on the branch.
    TITLE=$(git log -1 --pretty=%s "$BRANCH")
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
    git checkout "$BRANCH"
fi

# Verify the branch doesn't have CRLF pollution before pushing.
BAD=$(git ls-files --eol | awk '$1 ~ /crlf|mixed/ {print $4}' | grep -v '\.\(bat\|ps1\|ahk\)$' || true)
if [ -n "$BAD" ]; then
    echo "refusing to push: these files have non-LF endings:" >&2
    echo "$BAD" >&2
    echo "run: git add --renormalize . && git commit --amend --no-edit" >&2
    exit 1
fi

git push -u origin "$BRANCH"

gh pr create --title "$TITLE" --body-file "$BODY_FILE"
