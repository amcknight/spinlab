# /improve Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a user-level `/improve` skill that runs a 9-phase multi-lens improvement scan (parallel lenses → merge → parallel critique → verify → rank → present → pick → handoff).

**Architecture:** Four prompt files in `~/.claude/skills/improve/`. SKILL.md drives the flow and points to the three references files (lenses, critiques, ranking). No code, no scripts — pure prompt-driven skill that orchestrates `superpowers:dispatching-parallel-agents`.

**Tech Stack:** Markdown + YAML frontmatter. Discovered by Claude Code from the user's `~/.claude/skills/` directory.

**Validation:** This is a prompt-driven skill. Per the spec, **no automated tests** — assertion tests would test the model, not the skill. Per-task validation is structural (file exists, frontmatter parses, expected sections present). End-to-end validation is one manual run of `/improve` on the SpinLab repo in a fresh session.

**Spec:** `docs/superpowers/specs/2026-05-13-improve-skill-design.md`

---

### Task 1: Create skill directory and verify location

**Files:**
- Create: `C:/Users/thedo/.claude/skills/improve/` (directory)
- Create: `C:/Users/thedo/.claude/skills/improve/references/` (directory)

- [ ] **Step 1: Verify the parent directory exists**

Use Glob:
```
pattern: C:/Users/thedo/.claude/*
```
Expected: list including `plugins`, `projects`. The `skills/` subdirectory may or may not exist yet.

- [ ] **Step 2: Create the skill directories**

Use PowerShell:
```powershell
New-Item -ItemType Directory -Force -Path C:/Users/thedo/.claude/skills/improve/references | Out-Null
```

The `-Force` flag creates intermediate `skills/` and `improve/` directories if missing. `-Force` on `-ItemType Directory` is safe — it does NOT truncate existing directories (unlike `-Force` on files).

- [ ] **Step 3: Verify creation**

Use Glob:
```
pattern: C:/Users/thedo/.claude/skills/improve/**
```
Expected: the `improve/` and `improve/references/` directories listed (no files yet).

- [ ] **Step 4: No commit yet**

The skill directory lives outside any git repo. Plan and spec are committed in SpinLab; the skill files themselves are personal user-level files. The user can back up `~/.claude/skills/` to their own repo separately if they want — out of scope for this plan.

---

### Task 2: Write references/lenses.md

**Files:**
- Create: `C:/Users/thedo/.claude/skills/improve/references/lenses.md`

- [ ] **Step 1: Write the file with full content**

Write to `C:/Users/thedo/.claude/skills/improve/references/lenses.md`:

````markdown
# Lens system prompts

Each lens below is used verbatim as the system prompt for one parallel Explore agent in Phase 2 of /improve. Append the focus arg (if any) and project context summary to each before dispatch.

Each lens MUST format findings as:
`<file:line> — <one-line claim> — <one-line why it matters>`

And must end with a free-text section: "## Patterns I noticed across the codebase"

## Lens 1: Principal architect

You are a principal architect reading this codebase for the first time with skeptical, experienced eyes. Your job is to surface latent structural issues.

Look for:
- Leaky encapsulation — modules whose internals leak into callers, or callers reaching into module guts
- Units doing too much — files that have grown to handle multiple unrelated responsibilities
- Fuzzy boundaries — places where two modules' responsibilities blur
- Asymmetries — one way of doing something here, a different way for the same thing elsewhere, with no good reason
- Things that should be one — duplicated logic, parallel hierarchies that should converge
- Things that should be two — single modules juggling responsibilities that pull in opposite directions

For every finding, also ask: would *removing* this be a win? Sometimes the right architectural move is deletion.

## Lens 2: Control inversion & coupling

You are looking at this codebase through the lens of control flow and coupling. Your question: who calls whom, and is that direction right?

Look for:
- Logic pulling when it should be pushing (or vice versa)
- Hard-to-test code because of how it's wired together (e.g., a function that constructs its own dependencies internally instead of accepting them)
- Missing dependency injection opportunities — places where threading a parameter would unlock testability
- Circular or near-circular call graphs
- Push/pull tangles — events fired AND values returned by the same operation

For every finding, also ask: would *removing* this coupling be a win?

## Lens 3: Test & mock skeptic

You are a test reviewer with very low patience for ceremony. Read the test suite with hostile eyes.

Look for:
- Tests that mock what they should integrate (mocking the database when an in-memory or real DB would catch real bugs)
- Trivial tests that assert what the language already guarantees
- Slow tests not earning their runtime (timing-sensitive tests, sleeps, real network calls)
- Missing coverage on real behavior — paths that are critical but only exercised through indirect tests
- Brittle tests — pinned to internal implementation details rather than observable behavior
- Flaky tests masked by retries

For every finding, also ask: would *removing* this test be a win? Some tests cost more to maintain than they earn.

## Lens 4: Dead code & gravestones

You are hunting for code that no longer earns its keep. Read commit history when needed to understand what's stale.

Look for:
- Orphaned helpers — functions/classes with no callers
- Gravestone comments — comments left behind after the code they described was removed
- Comments that lie — names or comments that haven't matched behavior in recent commits
- Dead config knobs — settings nobody changes that complicate the code path
- Dishonest names — function or variable names that don't reflect what the code actually does
- Unused branches — if/else arms that can no longer be reached

For every finding, also ask: is this just dead, or is its presence actively making the surrounding code worse?

## Lens 5: Types & contracts

You are auditing type surfaces and signatures.

Look for:
- Undertyped public APIs — functions taking `Any` / `dict[str, Any]` / `object` when they could take a precise type
- Missing dataclasses — places where ad-hoc dicts are passed around and would benefit from a named type
- Signatures that hide their real inputs — functions that read globals or mutate parameters in ways the signature doesn't reveal
- Overpromise / underpromise — return types that claim more or less than the function actually delivers
- Schema duplication — same data shape defined differently in multiple places
- Stringly-typed code — enum-shaped data passed as bare strings

For every finding, also ask: would *removing* the abstraction be a win, or does adding a type clarify it?

## Lens 6: Observability & operability

You are reading this codebase asking: when something goes wrong in production at 2am, how does future-me debug it?

Look for:
- Logging gaps — critical paths with no log line
- Unhelpful errors — exceptions whose messages don't help the reader understand what went wrong
- Missing diagnostics — places where state would need to be inspected but there's no way to see it
- Debuggability black holes — long-running operations with no progress signal
- Missing diagrams — architecture that's complex enough that a diagram would help newcomers
- Silent failures — code that swallows exceptions or returns sentinel values without surfacing why

For every finding, also ask: would *removing* the operation be a win, or does it need observability added?
````

- [ ] **Step 2: Verify the file is written and has all 6 lenses**

Read the file:
```
Read: C:/Users/thedo/.claude/skills/improve/references/lenses.md
```
Visually confirm: 6 `## Lens N:` headers (1 through 6), each with a "Look for:" block and a "For every finding, also ask:" line.

---

### Task 3: Write references/critiques.md

**Files:**
- Create: `C:/Users/thedo/.claude/skills/improve/references/critiques.md`

- [ ] **Step 1: Write the file with full content**

Write to `C:/Users/thedo/.claude/skills/improve/references/critiques.md`:

````markdown
# Critique agent system prompts

Used verbatim in Phase 4 of /improve. Each critique agent receives:
- The merged findings list from Phase 3
- The user's optional focus arg
- NO access to the codebase (verification happens in Phase 5)

Every claim a critique agent makes MUST come with a `(claim, supporting_file_or_function_anchor)` pair so Phase 5 can verify it. Claims with no anchor default to low confidence.

## Skeptic

You are a hostile reviewer of a list of proposed code improvements. The list was produced by a swarm of architect-lens agents. Your job is to push back.

For each item in the list:
- Is the claim actually correct, or does it misread the code?
- Is the severity overstated? "Critical encapsulation issue" might be "minor stylistic preference."
- Is it missing context — does the surrounding code already address what the agent flagged?

Also scan the list as a whole:
- What's missing on the small end? Concrete, narrow, obvious things the lenses overlooked because they were busy looking for big patterns.
- Any duplicate items framed differently?

Output:
- KILL list — items to drop, with reason and file:line anchor for the evidence
- OVERSTATED list — items to keep but downgrade in severity, with reason
- MISSED list — concrete new items the lenses missed, with file:line anchor

Be specific. Vague skepticism isn't useful.

## Dreamer

You are looking at a list of proposed code improvements and asking: is this list too timid?

For each item:
- Is this the trailing edge of a bigger refactor? Sometimes the "small fix here" is the symptom of a structural issue that's actually less work to fix at the root.
- Is the ambitious version actually less work? Sometimes removing an abstraction entirely is less work than tweaking it.
- Would a principal engineer push this further? What's the version of this refactor that a senior engineer would advocate for?

Output:
- REFRAME list — items to upgrade into something more ambitious, with the new framing + file:line anchors
- NEW BIGGER list — entirely new items that emerge when you zoom out from the existing list, with anchors

Reality check yourself: bigger ≠ better. The dreamer's job is to surface ambitious options; the verifier will decide if they survive contact with the code.

## Convergence hunter

You are looking at a list of proposed code improvements and asking one question: what's the smallest set of upstream fixes that resolves the most items?

For each potential convergent fix:
- Name the fix concretely
- List which items in the original list it absorbs (subsumes)
- Give a file:line anchor for the root cause

A good convergent fix:
- Absorbs 3+ items
- Touches one or a few places that are upstream of the surface symptoms
- Is itself a real, namable thing (not "refactor everything")

Output:
- CONVERGENT FIXES list — each with name, absorbed-items, root-cause anchor, and estimated effort (small / medium / large)
- Optionally: SHARED ROOT CAUSES — patterns you noticed across multiple items even if there's no single fix
````

- [ ] **Step 2: Verify the file has all 3 critique sections**

Read the file:
```
Read: C:/Users/thedo/.claude/skills/improve/references/critiques.md
```
Visually confirm: `## Skeptic`, `## Dreamer`, `## Convergence hunter` headers, each with an Output block.

---

### Task 4: Write references/ranking.md

**Files:**
- Create: `C:/Users/thedo/.claude/skills/improve/references/ranking.md`

- [ ] **Step 1: Write the file with full content**

Write to `C:/Users/thedo/.claude/skills/improve/references/ranking.md`:

````markdown
# Ranking and output

## Priority tiers

Items are grouped into these tiers in Phase 6 of /improve. NO numeric scoring — each tier has a defined meaning.

- **must-fix** — Broken now, or correctness / safety risk. Do this first regardless of effort.
- **high-leverage** — Unlocks new work, removes recurring pain, or significantly improves a daily-use path. The bread and butter of /improve.
- **convergent win** — One fix that subsumes 3+ other listed findings. Named explicitly by the convergence-hunter and confirmed by VERIFY. Often higher ROI than any individual high-leverage item.
- **nearby cleanup** — Worth doing when working in the area; not worth a dedicated session.

Within each tier, order items by `verified_confidence` (high → medium → low).

## Handoff size

Every item also carries a handoff size (set during MERGE, possibly adjusted by VERIFY):

- **trivial** — Single file, no design decisions needed. Inline edit in /improve itself.
- **medium** — One component, real design choices to make. → brainstorming → writing-plans → single execution session.
- **big** — Multi-phase, cross-cutting work. → brainstorming → writing-plans → subagent-driven-development.

Priority tier is "how important". Handoff size is "how much work". They're orthogonal — a must-fix item can be trivial, a nearby cleanup can technically be big (rare).

## "Codebase looks fine" output

When fewer than 3 items rise above `nearby cleanup` tier and the critique pass doesn't surface anything substantial, /improve emits this output instead of fake top wins:

```
Scanned <N> files across 6 lenses. Codebase is in good shape.

Minor finds (not worth a dedicated session):
- <item> (file:line) — <one-line note>
- ...

Most recent significant scan: <date>, where we picked <items>.
```

This is the right answer when it's true. Don't pad.

## Scan file format

Save to `docs/superpowers/scans/YYYY-MM-DD-improve.md` (or `-HHMM` suffix if multiple scans same day).

```markdown
---
date: 2026-05-13
focus: "<arg passed, or 'full sweep'>"
git_head: <SHA>
lenses_run: [architect, control-inversion, test-skeptic, dead-code, types, observability]
critiques_run: [skeptic, dreamer, convergence]
findings_count: <N>
status: <full_scan | codebase_looks_fine>
---

## Top wins

### must-fix
- <item> — <one-line rationale> — size: <trivial|medium|big>

### high-leverage
- ...

### convergent win
- <item> — absorbs: [item-A, item-B, item-C] — size: <trivial|medium|big>

### nearby cleanup
- ...

## Picked this session
- <item> → handed off to: <inline|brainstorming|writing-plans|subagent-driven-development> → branch: <name if applicable>

## Dropped during critique
- <item> — killed by skeptic: <reason>
- <item> — reframed by dreamer into: <new item>

## New finds from VERIFY pass
- <item discovered during verification re-read>

## Full merged list (collapsed)
<all findings, grouped by lens, with file:line refs>

## Critique reports
<skeptic, dreamer, convergence, verify — verbatim>
```

The next /improve run reads ONLY the `## Top wins` and `## Picked this session` sections from this file. Keep critique reports verbatim for human review, not for cross-session loading.
````

- [ ] **Step 2: Verify the file has all required sections**

Read the file:
```
Read: C:/Users/thedo/.claude/skills/improve/references/ranking.md
```
Visually confirm: `## Priority tiers`, `## Handoff size`, `## "Codebase looks fine" output`, `## Scan file format` headers.

---

### Task 5: Write SKILL.md

**Files:**
- Create: `C:/Users/thedo/.claude/skills/improve/SKILL.md`

- [ ] **Step 1: Write the file with full content**

Write to `C:/Users/thedo/.claude/skills/improve/SKILL.md`:

````markdown
---
name: improve
description: Multi-lens improvement scanner that dispatches parallel architect-lens subagents (encapsulation, control-inversion, test/mock skeptic, dead code, types, observability) plus critique subagents (skeptic, dreamer, convergence) and a code verification pass. Produces qualitatively-ranked top wins and hands off picked items to superpowers (brainstorming → writing-plans → execution). Use when the user asks for a broad sweep of improvements, architectural review, refactoring opportunities, or with phrases like "put on your architect hat", "find improvements", "what can we clean up", "step back and look at this".
---

# /improve — Multi-lens improvement scanner

**Announce at start:** "Running /improve. Dispatching 6 parallel lens subagents, then critique, then verify."

## Phase 1: PREP

Gather context before dispatching lenses. Load in parallel:

- `CLAUDE.md` (if present, project conventions)
- The user's `~/.claude/projects/<project>/memory/MEMORY.md` index. Then load up to 10 linked memory files matching `user_*.md`, `feedback_*.md`, and `project_*.md` (in-flight work). If more than 10 match, keep the 10 most recently modified.
- `git log --oneline -30` (recent context — "we just did a big refactor" detection)
- From the last 3 files in `docs/superpowers/scans/` (if the directory exists), load ONLY the `## Top wins` and `## Picked this session` sections. Do NOT load the full critique reports — they would consume too much context.
- `ARCHITECTURE.md` and `DIAGRAMS.md` if present

Interpret the optional natural-language focus arg from `$ARGUMENTS`. Examples:
- "ignore the replay layer, it's mid-rewrite" → instruct each lens to skip that area
- "focus on the test suite" → instruct each lens to weight tests more heavily (but still run all 6)
- "we're about to ship, only must-fix tier" → keep full scan, filter PRESENT phase output

Build a one-paragraph **project context summary** to pass to every lens: stack, recent commits, in-flight work, areas to ignore.

## Phase 2: DISPATCH (lenses)

Read `references/lenses.md`. Use `superpowers:dispatching-parallel-agents` to dispatch 6 Explore agents in parallel — one per lens.

Each agent's prompt:
1. Its lens system prompt from `references/lenses.md` (verbatim)
2. The project context summary from Phase 1
3. The focus arg (if any), with explicit instructions for any areas to skip
4. Output format reminder: `<file:line> — <one-line claim> — <one-line why it matters>` plus a free-text "## Patterns I noticed across the codebase" section

## Phase 3: MERGE

Combine all 6 lens reports into a single deduped list. Dedup heuristic:
- Cluster by `file:line` proximity (same file, within ~20 lines)
- Cluster by topic similarity (same identifier or same concept named differently)
- When items merge, keep the strongest framing; record which lenses surfaced it

Assign initial **handoff size** (trivial / medium / big) to each item based on:
- Cross-lens coverage (3+ lenses flagging the same area suggests bigger scope)
- Inferred scope from file:line refs (multiple files = bigger)

Do NOT assign size based on what individual lenses said — they don't see enough context to judge scope.

## Phase 4: CRITIQUE

Read `references/critiques.md`. Use `superpowers:dispatching-parallel-agents` to dispatch 3 critique agents in parallel. Each agent gets only the merged list — NOT the codebase. (Verification of their claims happens in Phase 5.)

- Skeptic
- Dreamer
- Convergence hunter

Their system prompts are in `references/critiques.md`. Each critique agent must produce its claims with `(claim, supporting_file_or_function_anchor)` pairs so VERIFY can check them.

## Phase 5: VERIFY

Dispatch one Explore agent (the verifier). Its prompt:

> You are verifying claims made by critique agents about a codebase. Here is the merged findings list and the 3 critique reports. For each critique claim that has a file:line anchor, re-read that location and judge: is the claim true? Output for each item:
> - `verified_confidence`: high | medium | low | debunked
> - one-line evidence
>
> Additionally, flag any new issues you spot while re-reading the named files (free bonus pass). Adjust handoff size where the call-site evidence changes the picture (e.g., a "trivial" item that actually touches 14 call sites becomes "medium").

Claims without file:line anchors get default-low confidence and skip verification.

## Phase 6: RANK

Read `references/ranking.md`. Group items by priority tier (must-fix, high-leverage, convergent win, nearby cleanup). Order within each tier by `verified_confidence` (high → medium → low).

If fewer than 3 items rise above `nearby cleanup` tier, emit the **"codebase looks fine"** output from `references/ranking.md` instead of padding weak items into top wins.

## Phase 7: PRESENT

Show the user:
1. **Top wins** grouped by priority tier, each with file:line refs, one-line rationale, handoff size tag
2. **Synergies** explicitly called out (which convergent fixes absorb which items)
3. **Dropped during critique** — items the skeptic killed, with reason
4. **New finds from VERIFY** — bonus items spotted during the second look

Keep the presentation tight. Don't restate every individual lens report — that's saved in the scan file.

## Phase 8: PICK

Ask the user to pick 1–3 items, regenerate top wins, or scrap the scan. If the top wins list has 4 or fewer items, use the `AskUserQuestion` tool with the items as options. Otherwise list them and ask in plain text.

## Phase 9: HANDOFF

For each picked item, dispatch by handoff size:

- **trivial** → read affected files, make the edit inline, commit (if in a git repo)
- **medium** → invoke `superpowers:brainstorming` then `superpowers:writing-plans`
- **big** → invoke `superpowers:brainstorming` then `superpowers:writing-plans` then `superpowers:subagent-driven-development`

Before exiting, save the scan to `docs/superpowers/scans/YYYY-MM-DD-improve.md` in the current working directory. If a scan already exists for today, append `-HHMM`. If `docs/superpowers/scans/` doesn't exist, create it. The scan file format is in `references/ranking.md`.

If the current working directory is not a git repo (no `.git/`), still save the scan but warn the user.
````

- [ ] **Step 2: Verify the YAML frontmatter parses**

Use PowerShell to confirm the frontmatter is valid YAML:

```powershell
$content = Get-Content C:/Users/thedo/.claude/skills/improve/SKILL.md -Raw
if ($content -match '(?s)^---\r?\n(.*?)\r?\n---') {
  $fm = $Matches[1]
  Write-Output "Frontmatter found:"
  Write-Output $fm
  if ($fm -match 'name:\s*improve' -and $fm -match 'description:') {
    Write-Output "OK: name and description fields present"
  } else {
    Write-Output "FAIL: missing required fields"
  }
} else {
  Write-Output "FAIL: no frontmatter delimiters found"
}
```

Expected output ends with "OK: name and description fields present".

- [ ] **Step 3: Verify all 9 phases are present in SKILL.md**

Use Grep:
```
pattern: "^## Phase [1-9]:"
path: C:/Users/thedo/.claude/skills/improve/SKILL.md
output_mode: count
```
Expected: 9 matches.

---

### Task 6: Structural sanity check

**Files:**
- Read only — no changes.

- [ ] **Step 1: List all skill files**

Use Glob:
```
pattern: C:/Users/thedo/.claude/skills/improve/**/*.md
```
Expected output (exactly 4 files):
- `C:/Users/thedo/.claude/skills/improve/SKILL.md`
- `C:/Users/thedo/.claude/skills/improve/references/lenses.md`
- `C:/Users/thedo/.claude/skills/improve/references/critiques.md`
- `C:/Users/thedo/.claude/skills/improve/references/ranking.md`

- [ ] **Step 2: Verify cross-references are consistent**

Check that SKILL.md's references match the files we wrote. Use Grep:
```
pattern: "references/(lenses|critiques|ranking)\.md"
path: C:/Users/thedo/.claude/skills/improve/SKILL.md
output_mode: content
```
Expected: at least one reference to each of `references/lenses.md`, `references/critiques.md`, `references/ranking.md`.

- [ ] **Step 3: Verify lens count consistency**

Check the SKILL.md says 6 lenses and lenses.md defines 6 lenses.

Use Grep on SKILL.md:
```
pattern: "6 (Explore )?agents"
path: C:/Users/thedo/.claude/skills/improve/SKILL.md
output_mode: count
```
Expected: at least 1 match.

Use Grep on lenses.md:
```
pattern: "^## Lens [1-6]:"
path: C:/Users/thedo/.claude/skills/improve/references/lenses.md
output_mode: count
```
Expected: 6 matches.

---

### Task 7: Manual integration test (run /improve on SpinLab)

This is the end-to-end validation. It must be run in a **fresh Claude Code session** so the skill is discovered.

**Files:**
- No files changed. This is a runtime test.

- [ ] **Step 1: Restart Claude Code session**

Tell the user to either:
- Open a new Claude Code session, OR
- Run `/clear` in the current session if that re-scans the skills directory (uncertain — fresh session is safer)

The skill needs to be picked up by Claude Code's skill discovery. New session is the safe path.

- [ ] **Step 2: Verify /improve appears in the skill list**

In the new session, the user types `/` and looks for `improve` in the user-invocable skills list. Or types `/improve` directly; Claude Code should auto-complete.

If `/improve` does NOT appear, troubleshoot:
1. Verify file path: `C:/Users/thedo/.claude/skills/improve/SKILL.md` exists.
2. Verify YAML frontmatter syntax (no tabs, proper `---` delimiters, valid `name:` and `description:` fields).
3. Check if user-level skills require any registry update (currently they should be auto-discovered).

- [ ] **Step 3: Run /improve on the SpinLab repo**

User runs: `/improve`

Expected behavior:
1. Announces "Running /improve. Dispatching 6 parallel lens subagents, then critique, then verify."
2. Loads context (CLAUDE.md, MEMORY.md, recent commits)
3. Dispatches 6 parallel lens agents
4. After all 6 return, dispatches 3 critique agents in parallel
5. Dispatches 1 verifier
6. Presents top wins grouped by tier
7. Asks user to pick

The scan file should be saved to `c:/Users/thedo/git/spinlab/docs/superpowers/scans/2026-05-13-improve.md` (or similar).

- [ ] **Step 4: Sanity-check the output**

Look at the scan file. It should have:
- All 4 priority tier sections (even if some are empty)
- File:line references on findings
- A "Critique reports" section with verbatim subagent reports
- A "Picked this session" section if the user picked anything

Compare against the 6 prior architect-prompt sessions referenced in MEMORY.md (e.g., `project_polish_followups.md`, `project_hardening_round.md`, `project_testing_feedback_*.md`). The scan should surface at least one item that overlaps with those historical lists, indicating the lenses are finding real things.

- [ ] **Step 5: Report results back**

Tell the user:
- Did `/improve` run end-to-end without errors?
- Were the findings reasonable?
- Was the critique pass adding value or noise?
- Was the verify pass catching false-positives from the dreamer?
- Any phases that felt off (too slow, too verbose, missing context)?

These notes seed the v2 refinement — same memory-file approach as past project iterations.

---

## Notes for the executor

- **No TDD-style failing-test-first cycles.** This skill is a set of prompt files, not code. Per-task validation is structural (file exists, parses, has expected sections). End-to-end validation is Task 8.
- **No commits inside `~/.claude/skills/`.** That directory is not in a git repo by design. Plan and spec are already committed to SpinLab.
- **The `Write` tool will refuse to overwrite a file that hasn't been Read first** — if you need to re-write any of these files (e.g., to fix a typo), `Read` it first.
- **Tasks 2-5 can technically be done in parallel** (they touch independent files). Up to the executor to decide; sequential is also fine and easier to debug.
