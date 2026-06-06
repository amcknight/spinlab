# Phase D iter-2 — Run-Level Graph + Persistent Info on Stop — Design Spec

**Date:** 2026-06-06
**Status:** Approved via brainstorm (visual companion mockups in `.superpowers/brainstorm/`). PoC-first: build, run on real RA data, then re-brainstorm the deferred composition items.
**Parent:** Iteration 2 of the Phase-D two-tab shell (`2026-06-05-phase-d-shell-design.md`). Composes onto the shipped D-Live live view (`2026-06-02-live-practice-view-design.md`).

## The problem this solves

Stopping Practice today hides the **entire** `#practice-card` (`model.ts:updatePracticeCard` sets `display:none` + `destroyLiveView()` when `mode !== "practice"`). The spine (route bar), the segment you just practiced, its graph — all vanish in one step, leaving only the bare Model State table. Andrew's iter-2 lead, "persistent info on stop," wants the opposite: **keep as much visible as makes sense when changing states.**

Alongside that, a new idea surfaced in the brainstorm: a **run-level "whole-session improvement" graph** — the run analog of the per-segment episode graph — that Andrew wants to prototype **in practice mode first**, then let it inform the stop/idle composition.

This spec delivers both: the run graph (practice mode), and persistence of the live view across the stop transition.

## Guiding principle

**Keep as much visible as makes sense when changing states.** The practice→idle transition should reveal/freeze, not tear down. Stay close to the existing D-Live view (the gold standard) — promote and surround, do not rewrite.

## Part 1 — Run-level session-improvement graph (backend)

The segment graph plots one segment's episode times over its completions, with a dashed floor = its best clean clear. The run analog plots the **projected full-run time (Exp.Run)**, recomputed after every attempt this session, declining as segments improve. The drop from session-start *is* "Saved 6.2s," drawn as a curve.

### Why this works in practice mode

It is the model's **projection** (closed-form `expected_episode_time_scalar` summed over segments), not actual full runs — you never run the whole route while practicing segments, so there are no full-run observations to plot. The projection is always defined where segments are estimable. This is the same exact closed form `route_summary` already uses; **no Monte-Carlo, no new constants** (honors `project_model_principles`).

### The reducer

Add to `python/spinlab/estimators/live_view.py` a closed-form reducer that produces the series. It replays **this session's** attempts in chronological order (`created_at`, tiebreak `id`); each attempt belongs to one segment, so it rebuilds that segment's `SamplerState` incrementally and re-sums Exp.Run across all segments after each attempt → one point per session attempt.

- **Session window:** the attempts since the session-start snapshot (Part 2). The baseline (state *before* this session) already reflects all prior history — so `series[0]`'s predecessor is the session-start Exp.Run.
- **Per point:** `{exp_run_ms: float}`, indexed by session attempt order.
- **None handling:** a point is emitted only when the route is estimable (`n_estimable > 0`), mirroring `route_summary`'s honest skip. If the session has no estimable route yet, `series` is empty (the FE shows nothing rather than a fabricated line).

### Exposure

The series rides on the existing `RouteSummary` / `GET /api/games/{id}/live-summary` (mirrors how the segment `series` rides on `LiveSegmentView` / `/segments/{id}/live`). New `RouteSummary` fields:

- `series: list[dict]` — per-session-attempt `exp_run_ms` points.
- `baseline_exp_run_ms: float | None` — the session-start reference line. (Savings = baseline − current is already `practice_saved_ms`.)
- `floor_ms: float | None` — Σ over segments of each segment's running-min-clean = the theoretical best run, rendered as a flat reference line. (Decision: a single horizontal reference at the current Σ-floors; if a clean PB lands mid-session it steps down. A fully per-point running floor is deferred — flat is enough to read "distance to floor.")

`RouteSummaryResponse` in `api_schemas.py` gains the same fields; the frontend types regenerate via `npm run gen-types`.

### X-axis

Session attempt order (chronological index), mirroring the segment graph's completion index. Wall-clock X is deferred.

## Part 2 — Freeze-and-persist the session snapshot (backend)

Today `SessionManager.practice_session_snapshot` is captured at practice/hyper-play **start** and **cleared at stop** (`_on_practice_done` / `_on_hyper_play_done`). Change the lifecycle so the snapshot survives the stop:

- **Do not clear on stop.** Mark it frozen by recording an `ended_at` timestamp on the snapshot.
- **Elapsed freezes:** elapsed = `(ended_at or now) − started_at`. While practicing, `ended_at` is unset and elapsed ticks; once stopped, it is fixed.
- **Clear/replace only at the next practice/hyper-play start**, and on game switch (avoids the known `switch_game` stale-window).
- Idle `live-summary` / `live` calls still pass the persisted baseline, so the frozen diffs and the frozen run-series render exactly as they were at stop.

AppState needs one new signal so the frontend can tell "idle with a frozen session" from "idle, never practiced" — e.g. a boolean indicating a persisted session snapshot exists (final field name chosen during planning by reading `api_schemas.py` / the `session` payload).

## Part 3 — Frontend: run-graph component + persistence

### Run-graph component

New `frontend/src/run-graph.ts`, mirroring `episode-graph.ts`: renders the Exp.Run curve, the session-start baseline line, the Σ-floors floor line, and the shaded savings band. New host `#live-run-graph` placed **above** the segment summary (Option A — chosen): spine → **run graph** → segment summary → segment graph. `live-view.ts` reads the series off the already-fetched `live-summary` payload and renders it (no extra request).

### Persistence across stop

`model.ts:updatePracticeCard` gains a third state:

1. **Practicing / hyper-play** — live, 1s tick running (today's behavior).
2. **Idle with frozen session** — the card **stays visible**, rendered from the persisted snapshot: spine frozen with a `(frozen)` label, run graph + segment summary + segment graph all retained. No 1s tick.
3. **Idle, no session** (cold open / post game-switch / after a new session starts) — hidden + `destroyLiveView()`, as today.

`live-view.ts` gets a `frozen` flag: when set, it skips the `setInterval` tick and renders the frozen elapsed instead of `Date.now()`.

### Idle layout

On idle-with-frozen-session the Play page reads, top to bottom: persisted spine (frozen) → run graph → segment summary + segment graph (the segment you just practiced) → practice controls → Model State table → Simulator. The table and Simulator already render idle; this change simply stops the live view above them from disappearing.

## Scope boundaries

**In scope:** Part 1 (run-graph reducer + exposure), Part 2 (snapshot freeze/persist), Part 3 (run-graph component + practice-card persistence, Option A layout).

**Explicitly deferred to the next live look** (decide *after* seeing the run graph on real RA data, per PoC-first):

- Segment-graph demotion (move it below the All Segments table, or to a click-popup) — Andrew's idea, contingent on whether the run graph crowds the view.
- Click-to-focus master-detail (clicking a Model State row re-focuses the segment view above).
- The practicing-view cull (gated separately on the alpha/matrix strategy decision Andrew is not ready to make).
- Run-selector promotion and prior-like fill (tied to the `run_segment_membership` refactor).

**Out of scope:** any modeling change beyond the new closed-form series reducer; Monte-Carlo; CRN.

## Testing

- **Vitest:** `run-graph.ts` pure render (a series → expected points/SVG); `live-view.ts` frozen flag skips the tick and renders frozen elapsed.
- **pytest (fast):** route-series reducer — closed-form decline, `floor_ms` = Σ segment floors, empty-series / None handling, baseline alignment; snapshot freeze/persist lifecycle — not cleared on stop, `ended_at` freezes elapsed, cleared on next start and on game switch.
- **Playwright smoke:** after a practice stop, the Play idle view keeps the spine + run graph + segment graph visible and shows `(frozen)`; a fresh start clears it.
- **Full `pytest` (incl. emulator)** green before merge, per project policy.

## Iteration loop

Build → run on real RA data in practice mode → re-brainstorm the deferred composition (segment-graph demotion, click-to-focus, crowding) from what we see. This spec is the structural anchor for iter-2, not the final word on the stop/idle layout.
