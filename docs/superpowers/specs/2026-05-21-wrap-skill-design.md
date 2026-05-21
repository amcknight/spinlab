# `/wrap` — Wind-Down Skill Design

**Date:** 2026-05-21
**Status:** design

## Purpose

A user-invoked skill that handles the wind-down phase right after implementation is done on a branch. It sits between "code is written and seems to work" and "merge it back to main."

Goals:

- Verify the branch is in a stable, mergeable state (without re-running expensive work needlessly).
- Sweep the diff for hygiene issues (debug code, dead imports, secrets, missing tests, leftover TODOs).
- Reconcile docs that the diff makes lie (README, ARCHITECTURE, CHANGELOG, plans/specs, CLAUDE.md).
- Surface small refactors *justified by* what just landed — apply the safe ones, propose the rest.
- Capture durable lessons ("would have been good to know earlier") into memory, CLAUDE.md, or docs.
- Tidy handoff state (stranded worktrees, scratch files, WIP commits).
- Hand off cleanly to `finishing-a-development-branch` for the actual merge/PR decision.

## Non-goals

- **No codebase-wide architectural review.** That's `/improve`. `/wrap` is strictly diff-anchored.
- **No merging.** The terminal step delegates to `finishing-a-development-branch`.
- **No harness/settings tuning.** /wrap may *suggest* `update-config` or `fewer-permission-prompts`, but never edits `settings.json` itself.
- **No auto-writing new tests.** Missing-test findings are surfaced as ask items, not generated.
- **No retro file artifact.** All durable writes land in existing files (memory, CLAUDE.md, README, docs).

## Core principle

> Gate the state. Scan in parallel. Apply the low-risk, ask the rest. Capture durable lessons. Hand off.

## Trigger

User-invoked only — `/wrap`, "wrap this up", "tidy this branch", "wind down before merging." Never auto-fires.

## Announce at start

> "I'm using the wrap skill to wind down this branch."

---

## The five phases

### Phase 1 — Gate

Verify the branch is wrappable before doing any expensive scanning.

**Steps:**

1. Detect environment (normal repo / named-branch worktree / detached HEAD) and base branch via `git merge-base HEAD main || master`.
2. **Test-skip cache lookup:**
   - Fingerprint = `git rev-parse HEAD` + `git stash create` (captures uncommitted changes without stashing).
   - Cache file: `.claude/wrap/last-green.json` storing `{ commit, working_tree_hash, timestamp, test_command }`.
   - If fingerprint matches cached green: skip the test run. Print `"tests last verified green at <timestamp>, no changes since."`
3. Otherwise: detect project test command (probe `package.json` scripts, `pytest.ini` / `pyproject.toml`, `Cargo.toml`, `go.mod`, or ask). Run the full suite. On success, write the cache.
4. Check working tree cleanliness via `git status --porcelain`.

**Failure handling:**

- Tests fail → surface failures, offer to invoke `systematic-debugging`. Do not proceed.
- Working tree dirty → offer to commit, stash, or discard. Re-gate after.
- No test command configured → skip the test gate with a one-line note. Continue.

**Cache details:**

- `.claude/wrap/` is git-ignored.
- Cache is invalidated by any commit (HEAD change) or any working-tree-content change.
- /wrap never trusts a cache older than 7 days, even on a clean fingerprint — a stale green from last week is suspicious.

### Phase 2 — Scan

Fan out parallel subagents, one per lens (except Lesson Scout, which runs in the main agent).

**Tiny-diff fast path:** if the diff against base is `< 50 lines` AND `< 3 files`, skip the fan-out entirely. Run an inline checklist in the main agent covering hygiene + lesson + handoff. The four-lens machinery is overhead when there's barely anything to scan.

**Model selection for parallel subagents:**

| Lens | Default model | Bump rule |
|---|---|---|
| Diff Inspector | Haiku | Bump to Sonnet if diff touches security-adjacent paths (`auth/`, `crypto/`, `secrets`, `*.env*`) or if diff > 1000 lines. |
| Drift Detector | Sonnet | Bump to Opus if diff touches public API / exported surfaces or if diff > 1000 lines. |
| Refactor Surfacer | Opus | Already Opus; not bumped further. (Stays Opus even on tiny diffs — but tiny-diff fast path skips Scan anyway.) |
| Lesson Scout | inherits main session | n/a — runs in main agent. |

**Floor rule:** models never drop below their default. The defaults are floors. (Avoids Haiku missing things in a 5000-line diff because someone guessed wrong.)

**Transparency:** /wrap prints a one-line model plan when Scan begins:

```
Scan models: inspector=haiku  drift=sonnet  refactor=opus
```

The user can call out a mistake and ask for a rerun with a stronger model.

#### Lens 1 — Diff Inspector (parallel subagent)

- **Input:** unified diff vs base branch, list of changed files.
- **Looks for:**
  - Debug prints / `console.log` / `dbg!` / `print(...)` added in new lines.
  - Commented-out code blocks added by this branch.
  - TODO / FIXME / XXX / HACK markers added by this branch.
  - `.skip` / `xfail` / `xtest` / `it.skip` added by this branch.
  - New dependencies added (in `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`) but not referenced anywhere.
  - Secrets / `.env` / credentials / API keys in the diff.
  - New functions/classes added in source paths with no matching name in test paths.
- **Skips itself if:** diff < 20 lines.
- **Returns:** findings tagged `auto-safe` (e.g. obvious dead import, debug print in new code) or `ask` (e.g. "this new function has no test").

#### Lens 2 — Drift Detector (parallel subagent)

- **Input:** diff + paths to all `*.md` docs in repo + most recent plan/spec under `docs/superpowers/plans/` or `specs/` if one exists.
- **Looks for:**
  - Docs that now lie because of the diff (renamed function still in README, removed flag still in CHANGELOG, plan promised X but Y shipped).
  - CLAUDE.md sections that contradict new code.
  - Spec/plan drift — original plan vs what shipped.
- **Skips itself if:** diff touches no `.md` files AND no public symbols (export / public / `__all__`) AND no entrypoint files (CLI, main, index).
- **Returns:** doc updates with proposed content. Almost always `ask` (doc rewrites are judgment calls).

#### Lens 3 — Refactor Surfacer (parallel subagent)

- **Input:** diff + the *full content* of every file the diff touches.
- **Looks for:** small refactors *justified by what just landed* —
  - Duplication introduced within the diff (two near-identical functions added).
  - A helper that's obvious now that the second use exists.
  - A name that became misleading once the function grew.
  - A parameter that's now always passed the same value.
- **Hard constraint in prompt:** *"Each suggestion must cite the diff line(s) that justify it. If a refactor can't be tied to the diff, it does not belong in /wrap — it belongs in /improve. Do not surface it."*
- **Skips itself if:** diff < 50 lines OR diff only touches docs/config.
- **Returns:** refactor proposals. All `ask` — no auto-apply for structural changes.

#### Lens 4 — Lesson Scout (sequential, main agent)

- **Input:** the *current conversation* — what tripped us up, what we course-corrected, what would have saved time if known upfront.
- **Why sequential, in main agent:** subagents don't have conversation history. Only the main agent can reflect.
- **Looks for:** 0–3 durable items of the form "would have been good to know earlier" —
  - Non-obvious project constraints.
  - Recurring pitfalls.
  - Tool / library / API gotchas.
- **Routing per lesson:**
  - Memory file (if cross-conversation, user/feedback/project/reference flavor).
  - CLAUDE.md (if project-wide convention).
  - README / ARCHITECTURE / a specific doc (if user-facing).
- **Output:** each lesson with proposed home and 2–4 lines of content. User picks which to save.
- **Hard rule in prompt:** *"Never invent lessons. Better to surface zero than to fabricate filler. If nothing notable came up, skip this phase entirely."*

#### Inline handoff-state checks (no subagent)

Cheap bash, runs in main agent during synthesis:

- Stranded worktrees: `git worktree list` → any not on the wrapped branch?
- Background processes started this session that are still running (best-effort — only what the assistant can see via its own task list / running background commands).
- Commit history: count of WIP / fixup / `!!!` commits → propose squash if any.

### Phase 3 — Apply

After the four lenses report, the main agent deduplicates findings into a single Action set and runs two sub-passes.

**Auto-pass — apply without asking:**

- Delete dead imports added by this branch.
- Remove debug prints / `console.log` added in new lines.
- Remove commented-out code blocks added by this branch (only if confidently dead — not pinned TODOs).
- Format-only fixes on touched files (`ruff --fix` / `eslint --fix` / language equivalent), **scoped to the diff** — never on the whole repo.

After auto-pass: show a single summary diff. No per-item confirmation.

**Ask-pass — one batched menu:**

- Each `ask`-flagged finding becomes a numbered item with: file:line, what, why.
- User picks individually (`1,3,5`), in bulk (`all`, `none`), or qualifier (`only docs`, `only refactors`).
- Skipped items return at the end as a "Deferred follow-ups" list (not written anywhere — just shown).
- Declined refactors cache their content hash so they're not re-surfaced on the same branch.

**Two firm rules:**

- **No new tests written during Apply.** Missing-test findings become ask items proposing "add a test for X" — never auto-written.
- **No cross-cutting refactors.** Anything touching files outside the diff's blast radius gets refused and pointed to `/improve`.

**Post-apply test run:** re-run the test suite. Cache hit if Apply touched nothing; fresh run if it did. Update the green cache.

### Phase 4 — Reflect

Lesson Scout's findings land here.

For each candidate lesson:

```
Lesson: <one-line>
Why durable: <one-line>
Proposed home: <memory file | CLAUDE.md section | README | docs/X.md>
Proposed content: <2-4 lines>
```

User picks per item. Writes happen in place:

- Append to existing memory file if related (and update `MEMORY.md` index if needed).
- Create new memory file if standalone.
- Edit CLAUDE.md section in place.
- Update README / ARCHITECTURE / docs in place.

**Harness-tuning suggestions** (separate sub-section, *suggestions only*):

- "You hit N permission prompts for `gh` — consider `/fewer-permission-prompts`."
- "You re-set `$env:X` three times — consider `update-config` to persist it."

/wrap never edits `settings.json` itself.

### Phase 5 — Handoff

Invoke `superpowers:finishing-a-development-branch`. That skill owns the 4-option menu (merge / PR / keep / discard). /wrap's job ends here.

---

## Relationship to other skills

| Skill | /wrap's relationship |
|---|---|
| `/improve` | Strictly disjoint scope. /wrap = diff-anchored, /improve = codebase-wide. If /wrap notices deep issues outside the diff, it says *"consider /improve on `foo.py`"* and moves on. |
| `superpowers:verification-before-completion` | /wrap's Gate applies its principle. /wrap cites it rather than reimplementing the philosophy. |
| `superpowers:finishing-a-development-branch` | Terminal handoff. /wrap never merges. |
| `superpowers:requesting-code-review` / `/ultrareview` | Not auto-invoked. /wrap *may* suggest in Apply phase ("consider /ultrareview before merging") on large or risky diffs. |
| `superpowers:receiving-code-review` | Orthogonal. Never invoked by /wrap. |
| `superpowers:systematic-debugging` | Invoked from Gate when tests fail. |
| `superpowers:writing-skills` | Used to *build* /wrap; not used *by* /wrap. |
| `update-config` / `fewer-permission-prompts` | /wrap suggests these in Reflect's harness-tuning section. Never invoked directly. |

---

## Edge cases & refusals

| Situation | Behavior |
|---|---|
| Already on main / master | Refuse: "/wrap is for non-main branches. Did you mean /improve?" |
| Detached HEAD, no commits since detach | Refuse: "nothing to wrap." |
| Tests fail at Gate | Surface failures, offer `systematic-debugging`. Do not proceed. |
| Tests pass but working tree dirty | Offer commit / stash / discard. Re-gate after. |
| Diff is empty (branch == base) | "Nothing wrapped — branch has no changes vs `<base>`." Exit cleanly. |
| Cache says green, but no test command configured | Skip test gate with one-line note. Continue. |
| Same branch wrapped twice with no changes between | Cache hit, lenses skip-by-input, exits quickly: "already wrapped." |
| Subagent times out or errors | Lens marked failed in synthesis; /wrap continues with remaining lenses' findings. Reports the failure at end. |
| Diff > 1000 lines | Model bumps apply; consider suggesting `/ultrareview` in Apply. |
| Stale cache (> 7 days) on clean fingerprint | Re-run tests anyway. |

---

## What /wrap never does

- Force-push, rebase published branches, delete other branches, modify remote state.
- Edit `settings.json` or other harness config.
- Write new tests.
- Apply structural refactors without asking.
- Surface findings unrelated to the current branch's diff.
- Fabricate lessons to fill the Reflect phase.

---

## Open follow-ups / future work

- **General test-result cache.** Currently /wrap maintains its own `.claude/wrap/last-green.json`. A separate, more general tool — say `wrap-test pytest`, or a pytest plugin — could cache results across all test invocations (not just /wrap's). Would benefit any test-running flow. Out of scope for v1.
- **CLI `--model` override.** Not in v1; the transparency line lets users notice and re-invoke by hand. Add if friction emerges.
- **Adaptive model selection by main-agent diff scan.** Currently uses dumb thresholds. A "main agent peeks at the diff and picks models" step is more flexible but costs tokens and is brittle. Defer to v2 if dumb thresholds prove wrong too often.
- **Cross-branch lesson aggregation.** If a lesson recurs across multiple wraps, surface that pattern. Requires a longitudinal store.

---

## Implementation plan (high level)

To be turned into a real implementation plan via `writing-plans`. Rough sketch:

1. Skill scaffold: `SKILL.md` with frontmatter, name, description.
2. Gate phase: bash detection of environment / base, test-skip cache, working-tree check.
3. Tiny-diff fast path: inline checklist.
4. Lens subagent prompts: one per lens, each with diff-anchor constraint baked in.
5. Synthesis + Apply pass: auto-pass first, then batched ask-menu.
6. Reflect phase: lesson menu writing to memory / CLAUDE.md / docs.
7. Handoff: invoke `finishing-a-development-branch`.
8. Edge cases & refusals.
9. Self-test by running /wrap on a recent feature branch and tuning prompts.
