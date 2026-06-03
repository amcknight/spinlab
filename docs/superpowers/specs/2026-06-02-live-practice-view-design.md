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

**The entire live view is computed from EXACT CLOSED FORMS — no Monte-Carlo, no CRN.** This is valid because the live view uses only the *additive* total-run-time objective under `no_reset`: a no-reset run never aborts, so expected run time = the sum of per-segment expected episode times, and every number below has an exact closed form already present in the sampler. Zero randomness → nothing to be swamped by, and no MC/closed-form mixing (it's all closed-form). The Monte-Carlo engine (`RolloutMatrix`) and real CRN stay where they belong — the **Simulator**, for non-additive objectives (chance-under-target, PB-odds, quantile) and the distribution view — and are a separate concern from this UI.

| UI element | Concept | Source (closed-form) |
|---|---|---|
| Graph line · headline · rank | **Episode** time (observed) | survived-episode `time_ms` (episode total incl. deaths+reload); rank among episode totals |
| Decomposition "N deaths · Xs clean" | deaths + **clean** (observed) | episode's death count + `clean_tail_ms` |
| Floor line | running-min **clean clear** (observed) | running min of `clean_tail_ms` (= `clean_gold_ms` so far), diagonal |
| Floor / Floors stat | clean-best improvement this session | drop in min-clean since session start (segment); Σ over segments (run); only when non-zero |
| Expected (segment) | **Episode** time (predicted) | `expected_episode_time_scalar(state)` — closed-form geometric mean, no slide |
| Deaths % (segment) | per-attempt death rate | `p_die` EMA at `DEFAULT_FAST_IDX` |
| Practice (segment) | value-of-practice | `expected_episode_time(no slide) − expected_episode_time(one slope step)` — exact closed-form delta; colored by sign. Exact for the additive objective the live view uses. |
| Exp. Run (route) | **Episode** total run time | Σ over estimable segments of `expected_episode_time_scalar` (exact, additive/no_reset) |
| Exp. Deaths (route) | expected deaths per run | Σ over estimable segments of `p/(1−p)` (closed-form geometric expected death count) |
| Practice saved · rate (route) | session improvement | drop in Exp. Run since session start; rate = saved ÷ session-elapsed |

**Default objective = total run time, policy = `no_reset`** (the only objective the live view uses; implicit, no selector).

**Honest incompleteness:** segments below the prediction gate (or with `p → 1`, where the geometric mean diverges → `None`) have no estimate. Exp. Run / Exp. Deaths sum only the estimable segments and the payload flags how many were skipped — never treat a missing segment as zero.

**Why this isn't throwaway:** the closed forms already exist in the sampler; the frontend consumes a payload contract, not a method, so a future swap to MC for any number is invisible to the UI; and the MC engine remains intact for the Simulator. "Both" already coexist — closed-form for the live view, MC for the Simulator.

**Backend additions needed:**
- A read-only **closed-form live-view reducer** (per-segment payload) + a **route aggregate** reducer, over sampler states + observed attempts. No rollout/MC.
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
