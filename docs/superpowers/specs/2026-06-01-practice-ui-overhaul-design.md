# Practice UI Overhaul — Design Spec

**Date:** 2026-06-01
**Status:** **Plans A, B, C SHIPPED to main 2026-06-02** (improvement view · simulator simplify · alpha→memory-windows). **Plan D (responsive two-mode capstone) PARKED** pending a live smoke of A+B+C on real data; D also picks up the deferred §A polish (last-attempt callout, PB marker, death-rate arrow), wiring C's picker into A, picker persistence, and dropping the slope heatmaps. Plan files: `../plans/2026-06-02-*.md`.
**Scope:** Reorganize the practice-facing dashboard so the model is legible *while*
and *after* practicing. Frontend-led reorganization + relabeling + a responsive
two-mode layout; minimal new backend. No modeling changes.

## Motivation

The model and engine work (em_suite sampler, practice simulation engine) is sound,
but the surfaces that expose it are unusable in the moment:

- **Can't consume it live.** Eyes are on the game; the live graphs (Model tab) only
  update mid-play when you can't look, and you can't pause to study them without
  invalidating a segment. Stopping practice hides the graphs entirely.
- **Clunky / illegible.** A 6-column value table squishes into the 428px strip;
  numbers are raw ms or scientific notation (`Value/sec = 5.71e-2`); labels are
  jargon (`Δ`, `Value`, `Slack`, `Objective`, "gated"/"Ungated").
- **The alpha matrix is opaque.** A 10×10 grid of 0.0–1.0 decay rates with no way to
  tell which pair to use or which cells are trustworthy.
- **Wrong altitude.** The panels throw model internals and a Monte-Carlo planning
  tool at you when, during practice, you want one thing: *am I improving on this
  segment?*

## Decisions captured during brainstorming

1. **Consumption:** review-after-stopping (persistent) **and** between-attempts
   glances. (Not always-on peripheral; not freeze-a-moment.)
2. **Primary live signal:** "Am I improving on this segment?" is the headline — but
   the user wants *a lot* of info available, just organized so the priority leads.
3. **Display:** two modes via a **single responsive app** — a narrow live strip
   beside the game, and a wide review/planning layout when widened / on a second
   monitor.
4. **Alphas:** option **B** — a simple memory-window picker (auto by default,
   adjustable from a short plain-English list), greying out windows without enough
   data. The raw 10×10 matrix is dropped from the UI.

## Core reframe

Two consumers, two altitudes:

- **Live (during practice):** "Am I improving on *this* segment?" — a focused,
  glanceable performance tracker. No simulator, no objectives, no decay grid.
- **Review/planning (after stopping):** the full picture — per-segment depth, an
  all-segments overview, and the run-planning simulator (simplified). Persists so
  you can actually study it.

One responsive layout serves both: below a width breakpoint it collapses to the
live strip; above it, the full review layout.

---

## Design

### A. Live strip (narrow, beside the game)

Current segment only. Dense-but-glanceable (the user wants info, not a bare verdict):

- **Verdict line:** `↓ Getting faster` / `→ Holding` / `↑ Slower`, with a one-line
  basis: "recent ~5: 21.2s vs baseline 24.0s".
- **Last-attempt callout** (the between-attempts payload): "cleared 20.8s — 2nd
  best ✓" or "died at cp1".
- **Clear-time trend:** a compact sparkline of recent completion times, PB marked.
- **Two small stats:** death-rate (with trend arrow) and consistency/spread.

Nothing else on the strip — no simulator, objectives, or decay controls.

### B. Review / planning (wide, after stopping — persistent)

Three stacked sections:

1. **This segment (deep).** The full improvement view for the segment you were
   practicing (or any you pick):
   - Verdict + larger clear-time trend, death-rate trend, consistency, gap-to-best/gold.
   - **Now / Baseline window picker** (see §C): pick the two memory windows; greys
     out windows you lack data for.
   - **Plain-language model trends:** death-chance, clear-time, and death-time over
     attempts — so you can confirm the model is tracking reality. (These are the
     existing em_suite param histories, relabeled from `p_die` / `T_s` / `T_d`.)

2. **All segments overview.** Cards or a compact list — *not* a 6-column table.
   Each segment shows its headline numbers; segments below the data gate are shown
   **inline**, marked e.g. "— need 2 clears + 2 deaths" (not exiled to a separate
   "Ungated" block).

3. **Run planning (the Simulator, simplified).**
   - "**Practice next**": a plain ranked list ("1. L6 start→cp1 — saves ~0.5s/run"),
     replacing the `Value` / `Value/sec` columns.
   - "**PB odds this session**": the full-run projection in plain words.
   - **Advanced** (collapsed by default): policy (`no_reset`/`target_paced`), the
     objective selector, slack, target/quantile/session inputs, and the cumulative-
     split table — each with a one-line explanation. Sensible defaults so the
     simple view works with zero input.

### C. Alphas as memory windows

Replace the 0.0–1.0 decay grid with memory windows (≈ 1/α attempts):

- **Now** = recent skill (default ~5 attempts, α 0.2). **Baseline** = longer-memory
  reference (default ~20 attempts, α 0.05). The gap between them *is* the "am I
  improving" signal.
- Auto by default; adjustable from a short list (Last 2 / 5 / 10 / 20 / 50 / All-time).
- **Grey out** windows longer than the segment's attempt count — those aren't wrong,
  they're not yet distinct from the all-time average. This is the answer to "which
  matrix values are working."

### D. Readability conventions (everywhere)

- Times in **seconds**; never scientific notation.
- Jargon → plain language: `Δ` → "time saved"; `Value`/`Value/sec` → the ranked
  "practice next" list; "gated/Ungated" → inline "ready" / "need N more"; `Slack`,
  `Objective`, policy names → Advanced-only, each with a one-liner.
- Insufficient-data states say so inline ("—" / "need 2 clears + 2 deaths"), never a
  fabricated value (consistent with the project's no-silent-fallback rule).

---

## Architecture / implementation notes

**Frontend-led.** The hard parts already exist server-side:

- Per-attempt history (clear times, outcomes) → events/attempts tables.
- Death-chance / clear-time / death-time trends → em_suite `param_history` (the
  2026-05-31 practice-visuals work) — reused and relabeled.
- "Practice next" ranking + PB odds → practice engine `per_segment_values` and the
  objective slate (already shipped).
- Memory windows → the sampler already maintains EMAs at every α; "window = 1/α" and
  the grey-out rule are presentation logic.

**Likely the one new backend piece:** a small per-segment "progress" aggregation
(verdict + recent-vs-baseline + death-rate + consistency + trend series) so the live
strip is one cheap fetch rather than several. To be decided at planning time.

**Relationship to prior specs:**
- Absorbs/relabels `2026-05-31-em-suite-practice-visuals` (param histories kept; the
  slope heatmaps and raw matrix are dropped — the Now-vs-Baseline trend replaces them).
- Simplifies the surface of `2026-06-01-practice-simulation-engine` (engine unchanged;
  its panel becomes the "Run planning" section with Advanced-gated knobs).

**Out of scope:** any modeling/sampler change; new estimators; the per-attempt push
protocol (the existing SSE app-state cadence is reused); always-on peripheral and
freeze-a-moment modes (deferred — not chosen).

## Testing

- Extend the Playwright frontend-smoke harness: live-strip renders verdict + trend
  for a gated segment; review layout renders all three sections; all-segments shows a
  not-ready segment inline; "Advanced" stays collapsed by default; numbers never show
  scientific notation or raw seg_ids.
- Vitest for pure helpers (window↔α mapping, verdict derivation, number formatting).

## Open questions (for planning)

- Exact width breakpoint for strip↔review, and whether the desktop app can open the
  review layout as its own window.
- Whether the new per-segment "progress" aggregation is one endpoint or assembled
  client-side from existing ones.
- Sparkline implementation: reuse the existing chart.js usage vs. a lightweight inline
  SVG for the strip.
