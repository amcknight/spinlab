# `/improve` — multi-lens improvement scanner

**Date:** 2026-05-13
**Status:** Design approved
**Location:** User-level skill at `~/.claude/skills/improve/SKILL.md` (works in every project)

## Motivation

Andrew has been opening conversations with variants of an "architect goggles" prompt — *"find encapsulation, refactors, dead code, smells, missing tests, communication-scheme cruft, etc; produce a big list; pick top wins."* Six recent sessions used some variant of this pattern, and it's been productive. The pattern needs to be codified so it's reproducible, runs the same broad sweep every time, and chains into superpowers (brainstorming → writing-plans → execution) without manual stitching.

The skill must:

- Surface latent improvements across multiple axes in one invocation
- Push back on its own output before presenting (independent critique, not self-justification)
- Distinguish what to act on now from what to defer
- Hand off cleanly to the existing superpowers pipeline for execution

## Architecture

A user-level skill that dispatches **parallel architect-lens subagents** to scan the codebase, then dispatches **parallel critique subagents** to push back on the merged findings, then a **verification subagent** to reality-check critique claims against code, before ranking and presenting.

```
~/.claude/skills/improve/
├── SKILL.md            # Entry point + 9-phase flow
├── references/
│   ├── lenses.md       # The 6 lens system-prompts (editable phrasing bank)
│   ├── critiques.md    # Skeptic, dreamer, convergence-hunter prompts
│   └── ranking.md      # Qualitative tier definitions + handoff rules
```

Phrasings live in `references/` so they can be iterated on without touching the skill's flow logic.

## Flow (9 phases)

```
1. PREP        load CLAUDE.md, MEMORY.md (capped: profile + recent feedback files only),
                git log --oneline -30, and from last 3 docs/superpowers/scans/ files:
                ONLY the `## Top wins` + `## Picked` sections (not full critique reports).
                Interpret optional natural-language focus arg.

2. DISPATCH    6 parallel Explore agents — one per lens:
                  architect, control-inversion, test-skeptic,
                  dead-code, types, observability.
                Each lens prompt includes the YAGNI cross-cutting question
                ("...and would removing this be a win?").
                Each lens is instructed to format findings as:
                  `<file:line> — <one-line claim> — <one-line why it matters>`

3. MERGE       dedupe across lens reports (cluster by file:line proximity + topic).
                Items flagged by multiple lenses keep the strongest framing.
                Assign initial tier (see Ranking) based on cross-lens coverage and
                scope inferred from the merge view (NOT from individual lenses —
                they don't know enough).

4. CRITIQUE    3 parallel subagents reading only the merged list (cheap, ~2-5k token input each):
                  • skeptic         — what's wrong / overstated / missing on the small end
                  • dreamer         — is the ambitious version less work? Is this the trailing
                                       edge of a bigger refactor?
                  • convergence     — what upstream fix resolves 3+ items? Shared root causes?

5. VERIFY      one subagent re-reads the SPECIFIC FILES named in critique reports.
                Job: "are these claims actually true in the code?"
                Outputs:
                  - per-item verified_confidence (high / medium / low / debunked)
                  - evidence for each
                  - any new issues spotted along the way (free bonus pass)
                  - tier adjustments (some "trivial" finds become "medium" once
                    the verifier sees N call sites)

6. RANK        qualitative tier-based grouping (NO numeric score).
                Tiers:
                  • must-fix         — broken or about to break; correctness/safety
                  • high-leverage    — unlocks new work or removes recurring pain
                  • convergent win   — one fix subsuming 3+ findings
                  • nearby cleanup   — worth doing when working in the area anyway
                Within each tier, order by verified_confidence (high first).
                If overall findings are light: produce a "codebase looks fine"
                output instead of padding weak items into top wins.

7. PRESENT     show top wins grouped by tier + the synergies the convergence-hunter found.
                Show what was dropped during critique and why.
                Show what the verify pass *added* (the free bonus finds).

8. PICK        user chooses 1-3 items, asks to regenerate, or scraps the scan.

9. HANDOFF     per pick, dispatch by handoff tier (which equals effort, not a separate axis):
                  trivial  →  do it inline now
                  medium   →  invoke brainstorming → writing-plans
                  big      →  invoke brainstorming → writing-plans → subagent-driven-development
                Save scan to docs/superpowers/scans/YYYY-MM-DD-improve.md
                (or HH-MM suffix if multiple scans same day).
```

## Lenses (`references/lenses.md`)

Six lens system-prompts, each used as the system prompt for one parallel Explore agent.

1. **Principal architect** — encapsulation, boundaries, asymmetries (one way here / another way there for no good reason), units doing too much
2. **Control inversion & coupling** — DI opportunities, who-calls-who, push-vs-pull tangles, things that are hard to test because of how they're wired
3. **Test & mock skeptic** — over-mocking, trivial tests, slow tests not earning their runtime, missing real-behavior coverage
4. **Dead code & gravestones** — orphans, gravestone comments left from removed code, comments that lie, names that haven't matched behavior in recent commits
5. **Types & contracts** — undertyped public APIs, `Any` / `dict[str, Any]` abuse, missing dataclasses, signatures that hide their real inputs
6. **Observability & operability** — logging gaps, missing diagnostics, debuggability black holes, unhelpful errors, diagrams that ought to exist

Every lens prompt also asks the **YAGNI cross-cutting question:** *"...and for any of these, would removing this be a win?"*

Each lens prompt ends with the **output format instruction:** `<file:line> — <one-line claim> — <one-line why it matters>` plus a free-text "patterns I noticed across the codebase" section at the bottom.

## Critique agents (`references/critiques.md`)

Three parallel subagents run after MERGE. They read only the merged list — they do NOT re-read the codebase. That's VERIFY's job.

- **Skeptic** — *"Pick this list apart. What's wrong, what's overstated, what's missing on the small end? Be specific: name file:line and say what's off."*
- **Dreamer** — *"Is this list timid? Which items are the trailing edge of a bigger refactor that's actually less total work? Which would a principal engineer push further on?"*
- **Convergence hunter** — *"What's the smallest set of upstream fixes that resolves 3+ items? Find shared root causes. Output: each convergent fix + which list items it absorbs."*

Each critique must produce its claims with `(claim, supporting_file_or_function_anchor)` pairs so VERIFY has somewhere to look. Claims without anchors get default-low confidence and bypass VERIFY.

## Ranking (`references/ranking.md`)

**No numeric score.** Quantitative scoring is false precision when every input is a guess; it invites bikeshedding rather than reading each item. Replaced with qualitative tiers, each with a defined meaning:

- **must-fix** — broken now, or correctness/safety risk; do this first
- **high-leverage** — unlocks new work, removes recurring pain, or significantly improves a daily-use path
- **convergent win** — one fix that subsumes 3+ other listed findings; named explicitly by the convergence-hunter
- **nearby cleanup** — worth doing when working in the area; not worth a dedicated session

Within each tier, items ordered by verified_confidence (high → low).

**Handoff size (= effort dimension; not a separate axis):**

To avoid confusion with the priority tiers above, this dimension is called "size" — every item carries both a priority tier AND a handoff size.

- **trivial** — single file, no design decisions → inline edit
- **medium** — one component, real design choices → brainstorming → writing-plans
- **big** — multi-phase, cross-cutting → brainstorming → writing-plans → subagent-driven-development

Handoff size is set during MERGE/VERIFY based on scope inferred from cross-lens coverage and call-site evidence. Lenses do NOT set size themselves — they don't see enough to judge.

## Args & invocation

```
/improve [optional natural-language focus]
```

Examples:

- `/improve` — full sweep
- `/improve ignore the replay layer, it's mid-rewrite` — excludes that area from all lenses
- `/improve focus on the test suite` — narrows lens emphasis (still runs all 6 to keep coverage)
- `/improve we're about to ship v2, only must-fix tier` — filters output, keeps full scan

Focus arg may also trigger inline lightweight research if items reference external libs/patterns (no dedicated research agent — that was dropped during critique).

## Auto-loaded context at PREP

- `CLAUDE.md` (project conventions, gotchas)
- `MEMORY.md` index + linked memory files matching: `user_*.md`, `feedback_*.md`, in-flight `project_*.md` (cap: 10 memory files max; if cap is hit, drop by oldest mtime first)
- `git log --oneline -30` (recent context)
- From last 3 `docs/superpowers/scans/*.md` files: ONLY the `## Top wins` and `## Picked this session` sections (not the verbatim critique reports — those would blow context)
- `ARCHITECTURE.md` and `DIAGRAMS.md` if present

The "last 3 scans" load enables cross-session dedup: if an item surfaces that was triaged or rejected in a recent scan, the skill flags it explicitly rather than re-pitching it as new.

## Scan file format (`docs/superpowers/scans/YYYY-MM-DD-improve.md`)

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
- <item> — <one-line rationale> — size: medium

### high-leverage
- ...

### convergent win
- <item> — absorbs: [item-A, item-B, item-C] — size: big

### nearby cleanup
- ...

## Picked this session
- <item> → handed off to: <brainstorming|writing-plans|inline> → branch: <name if applicable>

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

## "Codebase looks fine" output

When merged findings are sparse and critique confirms nothing rises above `nearby cleanup`:

```
Scanned <N> files across 6 lenses. Codebase is in good shape.
Minor finds (not worth a dedicated session):
- ...
Most recent significant scan: <date>, where we picked <items>.
```

Don't pad weak items into a fake top-wins list.

## Out of scope (v1)

- **Research agent.** Cut during design critique. Focus arg covers the cases that would have triggered it.
- **Lens auto-selection from a pre-scan.** Considered, deferred. Lenses are parallel and cheap; saving 2 dispatches isn't worth the added phase. Revisit if scan cost becomes a real complaint.
- **Eval harness for pruning the lens bank.** The skill-creator skill supports this. Future work — set up after the lens bank has been used for ~10 invocations and we can see which lenses consistently produce keepers.
- **In-flight A/B logging of phrasings.** Same reasoning — defer until there's signal to act on.

## Testing & validation

This skill is hard to test mechanically — its output is a fuzzy list of suggestions. Validation strategy for v1:

1. **Self-test by running `/improve` on the SpinLab repo** immediately after the skill is built. The 6 prior architect-prompt sessions produced known-good findings (memory files reference them) — the skill's output should subsume those plus surface new ones.
2. **Apply the skill's own critique pass to the skill design** (already done in the brainstorming session — see the 6 verified changes folded above).
3. **No automated tests for v1.** The skill is prompt-driven; assertion-based tests would test the model, not the skill.

## Open questions

None at design time. Spec is implementation-ready.
