# Live Practice View (D-Live) — Design Spec

**Date:** 2026-06-02
**Status:** Design approved via visual brainstorm (mockups v1→v10 in `.superpowers/brainstorm/`). Ready for implementation planning.
**Parent:** Plan D of the practice UI overhaul (`2026-06-01-practice-ui-overhaul-design.md`). Plan D decomposed into **D-Live** (this spec), **D-Layout** (responsive strip↔review), **D-Sim** (Simulator fixes), **D-Viz** (more graphs). This spec covers D-Live.

## Goal

Replace the Model-tab practice card with a focused, glanceable **live practice view** built around one question — *am I improving on this segment?* — framed by stable whole-run context. Delete the legacy "Expected: … ms/att" insight card. Frontend-led, plus a read-only backend aggregation. **No modeling changes; no fudged values** — every number traces to a principled source (see Computation Sources).

## Two time concepts (the spine of the whole design)

From `_roll_up_episode` (`db/attempts.py`):

- **Episode / total time** (`time_ms`) = the whole run-until-clear: every death attempt + a **3.2s/death reload penalty** + the final clear. "What this segment actually cost me this time."
- **Clean clear time** (`clean_tail_ms`) = just the final successful attempt — no deaths. Equals the sampler's `success_time`.

Because `episode = clean clear + death cost`, **episode time ≥ clean clear ≥ best-ever clean clear**. The live view leans into this:

- The **graph + headline plot EPISODE time** (the real per-run cost, deaths included).
- The **"floor" line is the best clean clear** — the unreachable-without-a-perfect-run target episode times sink toward. Deaths are the gap between the two.

## Architecture: three sections, swappable graph slot

1. **Route bar** — whole-run aggregates (stable across segment switches).
2. **Segment summary** — current segment's header, stat cluster, big "last completion" number + decomposition.
3. **Graph slot** — a **swappable** region with a clean `render(host, segmentData)` interface. **Graph #1 (default) = the episode-time trend** specified below. Other graphs (clean-clear trend, death histogram, hazard, D-Viz) can occupy the slot later via a small picker without touching sections 1–2.

**Session** is a cross-cutting overlay, not a section: a marker on the graph + colored "since session start" diffs on the stats.

## Aligned stat columns

Both the route bar and the segment header use one right-aligned stat format — **label / value / colored-diff** stacked — and share **column positions so the two rows line up by meaning**. Right-to-left:

| Column → | Floor | Time | Deaths |   (Practice is segment-only, leftmost) |
|---|---|---|---|---|
| **Run (route)** | Floors* | Exp. Run | Exp. Deaths | — |
| **Segment** | Floor* | Expected | Deaths | Practice (leftmost) |

\* Floor / Floors render **only when non-zero** (a clean best improved this session). Practice has no diff slot; its **value is colored by sign** (green = gain, red = regression) and may render in ms.

## 1. Route bar

- **Title:** `<Game> · <category>`.
- **Practice saved (this session):** `Saved 6.2s · 3.1s/hr · 2:01:14` near the title — saved = how much **Exp. Run** dropped since session start; rate = saved ÷ session-elapsed; duration **ticks live**.
- **Stat columns (right):** `Floors` (if non-zero) · `Exp. Run` · `Exp. Deaths`, each label/value/diff. Recompute live as attempts land.

## 2. Segment summary

- **Header row:** segment name (left); the four-column stat cluster (right): `Practice` · `Floor`(if non-zero) · `Expected` · `Deaths`.
- **Headline:** label "last completion", big value = the **actual most-recent episode time**, with a **rank** ("2nd best" = 2nd-fastest *episode*; no checkmark) and, **horizontally beside it**, a tight decomposition: `1 death · 13.6s clean` (that completion's death count + its clean-tail time).

## 3. Graph #1 — episode-time trend

- **Y axis in seconds**, lower = faster. Range spans the floor up through death-heavy spikes.
- **Episode-time line:** blue polyline, one point per completion (x = completions in order). Death-heavy episodes spike high; near-clean ones sit near the floor.
- **Floor line:** the **running-best clean clear so far**, drawn as a **diagonal polyline through the same x-points** (not a horizontal step — so it tracks point-to-point like the blue line and the blue line never visually crosses below it). Steps down where a new clean best was set. Labeled "floor 12.8".
- **Per-completion death count:** a small red number under each point (deaths in that episode). (The earlier per-attempt red ticks are deferred to a possible future graph.)
- **Live in-progress attempt:** an amber dot **climbs the Y axis frame-by-frame** (client-side timer seeded by the attempt's start, interpolating between SSE pushes), resolving into the next completion point.

## Session overlay

- Subtle **"session start" vertical line** on the graph.
- **Colored diffs since session start** on the stats (Deaths −8%, Expected −2.1s, Exp. Run −4s, Exp. Deaths −2). Important info → **colored** (green improvement / red regression), not muted.
- **Session is hardcoded** to the current practice session = since the dashboard started and a game was selected. No day/week selector. Session length ticks by "Practice saved".

## Liveliness

Primary aesthetic goal — the view should feel alive.
- Frame-by-frame climbing dot (above).
- **Flash-on-change (nice-to-have, may defer to v2):** when any value updates, briefly tint it in its change direction and fade back.

## Computation Sources (no fudge — the load-bearing table)

Every value, with its principled source. **All route + model numbers come from one Monte-Carlo rollout** (the practice engine's `RolloutMatrix`) — no MC/closed-form mixing.

| UI element | Concept | Source |
|---|---|---|
| Graph line · headline · rank | **Episode** time | survived-episode `time_ms` (episode total incl. deaths+reload); rank among episode totals |
| Decomposition "N deaths · Xs clean" | deaths + **clean** | episode's death count + `clean_tail_ms` |
| Floor line | running-min **clean clear** | running min of `clean_tail_ms` (= `clean_gold_ms` so far), diagonal |
| Floor / Floors stat | clean-best improvement this session | drop in min-clean since session start (segment); Σ over segments (run); shown only when non-zero |
| Expected (segment) | **Episode** time | `e_sample_0` — rollout column mean (MC), consistent with the graph |
| Deaths % (segment) | per-attempt death rate | `p_die` EMA (fast window); drives the MC death draws |
| Practice (segment) | value-of-practice | engine `value` = baseline−swap of the objective via common random numbers (model spec §4); colored by sign |
| Exp. Run (route) | **Episode** total run time | `expected_total_finished_time` under **no_reset** (MC; death-retry cost already included) |
| Exp. Deaths (route) | expected deaths per run | **counted in the rollouts** — `sample_episode` returns its death count, averaged over rollouts (consistent with Exp. Run; no closed-form) |
| Practice saved · rate (route) | session improvement | drop in Exp. Run since session start; rate = saved ÷ session-elapsed |

**Default objective = `expected_total_finished_time`, policy = `no_reset`.** For this additive objective the engine `value` equals `e_sample_0 − e_sample_1` per segment; `value` is used so it stays correct under a future non-additive objective.

**Backend additions needed:**
- `sample_episode` (or a sibling) returns `(time, death_count)` so the rollout can average deaths per run.
- A per-segment progress payload + a route aggregate payload (read-only), assembled from the practice engine + sampler.
- Session tracking: a "practice session" boundary (dashboard start + game select) with an **in-memory snapshot** of the metrics at session start (do not re-derive by replay), for the diffs and "saved".

## Removed

- The legacy `renderPracticeInsight` card (`#insight`, `#current-goal`, `#current-attempts`) — kills the mislabeled `ms_per_attempt` and stuck `attempt_count` (overhaul findings #2/#3).
- No verdict chip; no recent/earlier window picker on the live view (picker → review view in D-Layout).

## Testing

- **Vitest** for pure helpers: stat-stack formatter (label/value/colored-diff), the episode-line + diagonal-floor + Y-axis mapping, per-completion death-count placement, rank-of-last-episode, decomposition string, number/sign/color formatting (seconds, no sci-notation, no raw ms).
- **Vitest** for the reducer turning the per-completion series into line points + floor polyline + session-start index.
- **Playwright smoke:** route bar renders aligned columns; segment view renders headline + decomposition + axis + diagonal floor + death counts; diffs colored; legacy `#insight` gone; numbers humanized.
- **Python** tests for the new aggregation (per-segment payload, route aggregate, MC death-count, session snapshot), mirroring the `segment_progress` test style.

## Phasing

- **v1:** everything above except the flash-on-change animation. The **stepping/diagonal floor + Floor stat** are v1 **if cheap**, else an immediate fast-follow (they shine early when the floor moves often).
- **v2 / fast-follow:** flash-on-change; additional graphs in the slot.

## Out of scope (other Plan-D sub-projects)

- **D-Layout:** responsive narrow-strip ↔ wide-review layout; the window picker's new home; the all-segments overview.
- **D-Sim:** Simulator tab fixes — reshaped now that the route bar surfaces the aggregates.
- **D-Viz:** further graphs for the swappable slot (clean-clear trend, death histogram, hazard, histogram-over-time).
