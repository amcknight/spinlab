# Live Practice View (D-Live) — Design Spec

**Date:** 2026-06-02
**Status:** Design approved via visual brainstorm (mockups v1→v8, `.superpowers/brainstorm/`). Ready for implementation planning.
**Parent:** Plan D of the practice UI overhaul (`2026-06-01-practice-ui-overhaul-design.md`). "Plan D" decomposed into four sub-projects — **D-Live** (this spec), **D-Layout** (responsive strip↔review), **D-Sim** (Simulator fixes), **D-Viz** (histogram-over-time). This spec covers D-Live only.

## Goal

Replace the current Model-tab practice card with a focused, glanceable **live practice view** built around one question — *am I improving on this segment?* — framed by stable whole-run context. Delete the legacy "Expected: … ms/att" insight card. Frontend-led; needs a modest read-only backend aggregation. No modeling changes.

## Motivation (from the 2026-06-02 smoke test)

The shipped Plan A view had the right idea but: led with a qualitative verdict instead of a number, plotted a raw clear-time line that visibly disagreed with the smoothed verdict, had no axis, buried the window picker, showed a mislabeled `ms/att` legacy card, and gave no whole-run or session context. Andrew's redesign (this spec) fixes all of that.

## Architecture: three sections, swappable graph slot

Structurally the view is **three stacked sections**, each an independent unit:

1. **Route bar** — whole-run aggregates (stable across segment switches).
2. **Segment summary** — the current segment's header, stat cluster, and the big "last clear" number.
3. **Graph slot** — a **swappable** region. The default occupant is the clear-time trend graph specified below, but the slot is a pluggable interface so other per-segment visualizations can occupy it later (e.g. a death histogram, the hazard plot, or the D-Viz histogram-over-time) — chosen by a small graph picker, without touching sections 1–2. Build the slot with a clean interface (`render(host, segmentData)`) so adding a graph type is additive.

The **session** (below) is not a fourth section — it's a cross-cutting overlay: a marker on the graph plus colored diffs on the section-1/2 stats.

These map onto three *altitudes* (route / segment / session) so switching segments only changes section 2 + the graph:

### 1. Route bar (top, stable — does not change per segment)

A thin bar above the segment view. Surfaces whole-run aggregates (effectively the Simulator's headline output, always visible):

- **Title:** `<Game> · <category>` (e.g. "Cute Kaizo · any%").
- **Practice saved (this session):** `Saved 6.2s · 3.1s/hr · 2:01:14`, near the title. The saved value = how much **Exp. run** has dropped since session start; the rate = saved ÷ session-elapsed; the duration ticks live. (Practice gains == reduction in Exp. run, so they are one concept, not two.)
- **Predictions cluster (right):** `Exp. run` (total expected run time = sum of per-segment expected clear times) and `Deaths/run` (expected deaths in the next run = sum of per-segment death expectations). Each renders as the standard **label / value / diff** stack (see below). Both **recompute live** as clears/deaths land.

### 2. Segment view (middle — current segment)

- **Header row:** segment name (left); a stat cluster (right) of three standard stacks: **Deaths** (recent death %), **Expected** (model's typical-clear estimate, `e_sample_0`), **Practice** (expected shave from one more rep, `e_sample_0 − e_sample_1`). Deaths and Expected carry a session diff; **Practice has no diff slot but its value is colored** (it trends green when there's a gain).
- **Headline:** small label "last clear", then the big number = the **actual most-recent clear time** (concrete, not a smoothed estimate), with a rank chip ("2nd best ✓") = its rank among all clears of this segment.
- **Trend graph:**
  - **Y axis in seconds**, lower = faster (improving line trends down). 2–3 labeled gridlines.
  - **Clear-time line:** blue polyline over attempts (x = attempts in chronological order).
  - **Gold line:** one dashed reference at the **best-ever completed clear** (`compute_golds` `gold_ms`). This is the only reference line on the live view (per-segment "PB" and "gold" are the same thing → one line, one word: "gold"). Target-pace / model-floor / smoothed-average lines are **not** on the live view.
  - **Deaths:** red **ticks along the bottom** at the attempts where you died (death markers convey recency/density of deaths alongside the clear trend; death-time-height plotting was tried and rejected as too noisy).
  - **Last-clear marker:** the most recent clear is dot-marked on the line.

### 3. Session (temporal overlay — not a separate panel)

- A subtle **"session start" vertical line** on the segment graph.
- **Diffs since session start** on the stats (Deaths −8%, Expected −0.6s, route Exp.run −4s, Deaths/run −2), rendered as colored values (green = improvement, red = regression). These are important info and are **colored**, not muted.
- **Session is hardcoded** to the current practice session = since the dashboard started and a game was selected. No day/week/selector (dropped to keep the bar compact and avoid ambiguity). Session length ticks beside "Practice saved".

## Liveliness

Liveliness is a primary aesthetic goal — the view should feel alive, not snapshot-y.

- **In-progress attempt:** an **amber dot climbs the Y axis frame-by-frame** as the current attempt runs (starting low/off-axis, rising with elapsed segment time), then resolves — landing as a blue clear point or a red bottom tick (death). No moving text label on the dot. Driven by a lightweight **client-side timer** seeded by the current attempt's start time (interpolating between SSE pushes — no new push infra).
- The route Predictions and the per-segment stats **recompute as attempts land**, riding the existing SSE app-state cadence.
- **Flash-on-change (nice-to-have, may defer to a v2):** when any non-live value updates, briefly flash it (e.g. green/red tint on the direction of change) and fade back to its resting color. Applies to the stats and predictions. Defer only if it complicates the render meaningfully; the goal is everything visibly reacting.

## Standard stat stack (used everywhere)

One consistent format for every stat in both the route bar and the segment header:

```
LABEL        (8px uppercase, dim)
value        (15px)
diff         (10px, colored: green improvement / red regression)
```

Right-aligned, stacked top-to-bottom. The "Practice" stat omits the diff row (its value is the colored signal).

## What gets removed

- The legacy `renderPracticeInsight` card (`#insight`, `#current-goal`, `#current-attempts` in `index.html`) — the "Expected: 13.8s · 23830.5 ms/att" line. This kills the mislabeled `ms_per_attempt` and the stuck `attempt_count` confusion in one move (see `project_practice_ui_overhaul` findings #2/#3).
- The verdict chip and the recent/earlier window picker are **not** part of the live view (the picker moves to the wide review view in D-Layout).

## Data / backend needs (read-only)

The view needs a per-segment progress payload and a route-level aggregate. Exact endpoint shape is a planning detail, but the data required:

**Per segment:**
- `last_clear_ms` + `rank` (Nth-best among this segment's completed clears).
- A **chronological per-attempt series**: `[{outcome: clear|death, time_ms}]` covering enough history to reach session start — clears → line points (clear time), deaths → bottom ticks.
- `gold_ms` (best-ever clear, from `compute_golds`).
- `death_rate` (recent) + its session-start value (for the diff).
- `expected_clear_ms` (`e_sample_0`) + its session-start value.
- `practice_gain_ms` (`e_sample_0 − e_sample_1`).

**Route aggregate:**
- `exp_run_ms` (Σ expected clears) + session-start value.
- `deaths_per_run` (Σ expected deaths) + session-start value.
- `practice_saved_ms` (= session drop in `exp_run_ms`), `rate_per_hour`, `session_elapsed`.

**Session tracking:** a "practice session" boundary keyed to dashboard start + game selection, with a **snapshot of the relevant metrics at session start** so the diffs and "saved" are computed against it.

The Predictions and Practice numbers are the practice engine's outputs (`per_segment_values`, the objective slate, `sample(0)`/`sample(1)`). D-Live surfaces those aggregates as the always-visible route bar — overlapping with the Simulator, which reshapes what D-Sim needs to do.

## Testing

- **Vitest** for the pure render helpers: the stat-stack formatter (label/value/colored-diff), the sparkline point/axis mapping, the death-tick placement, rank-of-last-clear, and number/diff formatting (seconds, signs, colors; no scientific notation, no raw ms).
- **Vitest** for the reducer(s) that turn the per-attempt series into line points + death ticks + gold line + session-start index.
- **Playwright smoke:** the route bar renders aggregates; the segment view renders the headline + axis + gold line + death ticks; diffs are colored; the legacy `#insight` card is gone; numbers are humanized.
- **Python** route/reducer tests for the new aggregation (per-segment payload + route aggregate + session snapshot), mirroring the existing `segment_progress` test style.

## Decisions (resolved 2026-06-02)

- **Live climbing dot cadence:** frame-by-frame via a **client-side timer** seeded by the current attempt's start time, interpolating between SSE pushes. No new push infra.
- **Session-start snapshot storage:** **snapshot in memory** at session start (do not re-derive by replay) — unless a snapshot value turns out to be needed by something else, in which case derive it there.
- **Route aggregate source:** **reuse the practice engine** (its per-segment values + objective slate) so the route bar stays single-source with the Simulator.

## Out of scope (other Plan-D sub-projects)

- **D-Layout:** the responsive narrow-strip ↔ wide-review layout, where this view lives in both modes; the window picker's new home; the "all segments overview".
- **D-Sim:** Simulator tab fixes (SSE refresh, now→after header) — reshaped by D-Live surfacing the aggregates.
- **D-Viz:** the histogram-over-time visualization.
