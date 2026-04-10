# Simplify Sandbox PR Workflow — Design

**Date:** 2026-04-10
**Status:** Approved, ready for implementation plan

## Background

Commit 45600df ("sandbox bootstrap + PR handoff scripts") introduced a two-step PR workflow for work done inside a Docker Sandboxes (`sbx`) container:

1. Sandbox agent commits its work on a branch and writes PR metadata to `.pr/body.md`.
2. Agent stops and instructs the user to run `scripts/finish-pr.sh <branch>` from the Windows host, which pushes the branch and opens the PR using the host's `gh auth`.

The rationale in the commit message: "Sandboxes typically have no GitHub credentials, so git push and gh pr create both fail."

That rationale is wrong for this project's sandbox. Docker Sandboxes has a first-class credential-injection feature that makes the handoff unnecessary.

## The discovery that changes the design

Docker Sandboxes v0.24.2 (installed via `winget install Docker.sbx`) stores service credentials on the host and injects auth into the sandbox's outbound HTTP traffic via an in-container proxy. From `sbx secret set --help`:

> "When a sandbox starts, the proxy uses stored secrets to authenticate API requests on behalf of the agent. The secret is never exposed directly to the agent."

Two properties matter here:

1. **The agent never sees the token.** This is strictly more secure than `GH_TOKEN` env-var passthrough, because nothing running inside the container can exfiltrate the credential.
2. **Both `gh` and `git push` work without any in-container configuration.** The proxy intercepts `api.github.com` (used by `gh`) and `github.com` git-over-https (used by `git push` / `git fetch` / `git ls-remote`). No `gh auth login`, no `gh auth setup-git`, no credential helper, no env var.

End-to-end verification (2026-04-10, before writing this spec):

```
# Host:
gh auth token | sbx secret set -g github
sbx secret ls                               # shows: github (global)

# Sandbox:
sbx run shell -- -c "gh api user | head -5 && git ls-remote https://github.com/amcknight/spinlab.git HEAD"
```

Both calls succeeded: `gh api user` returned the authenticated user's profile JSON; `git ls-remote` returned the current `origin/main` SHA. Proxy is confirmed to cover both paths.

## The design

Delete the handoff machinery entirely. No replacement script, no CLAUDE.md guidance about credentials, no environment-variable check. If the proxy is configured (one-time host setup), `git push` and `gh pr create` Just Work from inside the sandbox. If it isn't, those commands will fail loudly with a normal auth error — which is self-diagnosing and doesn't need a custom error path.

### What gets deleted

1. **`scripts/finish-pr.sh`** — the entire file (~53 lines). Its reason for existing is gone.
2. **`.gitignore` entry for `.pr/`** — the handoff directory convention dies with the script.
3. **The `.pr/` directory itself**, if it exists in the working tree (it does not, but double-check).
4. **The "Pushing from a sandbox" section of `CLAUDE.md`** — currently ~10 lines instructing the agent to write `.pr/body.md` and stop. Replaced with nothing.

### What stays

1. **`scripts/bootstrap-sandbox.sh`** — unchanged. Its purpose (Python 3.11 + venv + editable install) is orthogonal to credentials, and the pain it documents (PEP 668, missing `requests` in `[dev]` extras, uv sync edge cases on container mounts) is real and recurring.
2. **`.gitattributes` commits (43b6a98, 251a14e)** — unchanged. Orthogonal to credentials; defensible on their own.
3. **The "Running in a Linux sandbox / container" section of `CLAUDE.md`** — trimmed. The bootstrap paragraph and the line-endings paragraph stay. The pushing paragraph goes.

### Non-changes (deliberately out of scope)

- **No new CLAUDE.md line explaining the proxy.** The user explicitly does not want one. The sandbox either has credentials or it doesn't; the failure mode of a missing credential is already loud and obvious (an auth error from `git push`). Adding a conditional ("when `GH_TOKEN` is set, you can push") is misleading anyway, because the proxy model doesn't use `GH_TOKEN`.
- **No step-0 credential check.** Same reason: the failure is self-diagnosing, and a check would either succeed silently (no value) or add a speed bump for a situation that shouldn't happen.
- **No pre-built sandbox template yet.** `sbx save` would let us snapshot a Python-ready sandbox and skip the bootstrap cold-start, but that's an orthogonal optimization (Option 4 from the brainstorming session). Out of scope for this change.

## Host-side setup (one-time, not owned by the repo)

This is a user action, documented only in this spec for reference. It is not part of the repo and not added to CLAUDE.md:

```
gh auth token | sbx secret set -g github
sbx secret ls                               # verify: github (global)
```

Done once, persists globally across all sandboxes on that host.

## Risk assessment

**What could break:** a future sandbox invocation where the user forgot to set the secret, or the secret was removed, or Docker Sandboxes changed its proxy behavior in a version bump.

**How it would manifest:** `git push` fails with an HTTP 401/403 or `fatal: Authentication failed`. `gh pr create` fails with a `gh` auth error. Both are unambiguous, and the fix is a one-liner on the host (`sbx secret set -g github`). There is no silent corruption, no partial state, no data loss.

**Why no fallback:** the old fallback (`finish-pr.sh` + `.pr/body.md` handoff) added ~70 lines of host-side script, a CLAUDE.md section that Claude has to read and follow correctly, a gitignored directory convention, and a two-step human-in-the-loop workflow. That's a lot of surface area to carry as insurance against a failure mode that's trivially recoverable with one host command.

## Implementation order

One commit, in this order (so history is atomic):

1. Delete `scripts/finish-pr.sh`.
2. Remove the `.pr/` line from `.gitignore`.
3. Delete the "Pushing from a sandbox" section of `CLAUDE.md`.
4. Trim the remaining "Running in a Linux sandbox / container" section to just bootstrap + line-endings.
5. Commit with a message that explains *why* (the discovery about the proxy), not just *what*.

No code changes, no test changes, no frontend changes. Pure removal + one doc trim.

## Follow-ups (not in this change)

- **`sbx save` template.** Once `bootstrap-sandbox.sh` runs cleanly in a fresh sandbox, `sbx save` can snapshot that state as a template. Future `sbx run -t spinlab-ready ...` invocations skip the Python install cold-start. This addresses the other pain point the original commit mentioned (~10 failed tool calls rediscovering Python setup), using a primitive Docker Sandboxes already provides. Worth doing as a separate follow-up; not a prerequisite for this change.
- **GitHub Actions.** User expressed interest ("flirting with"). Genuinely different architecture (CI-hosted Claude runs instead of desk-hosted), not a substitute for this fix. Revisit separately if and when it becomes a concrete direction.
