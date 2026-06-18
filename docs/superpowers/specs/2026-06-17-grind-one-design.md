# GrindOne — repeat-one-segment practice (PoC design)

Status: PoC built autonomously 2026-06-17 (Andrew away, said "kick it off, make the
button"). Design decisions below were made solo — **review and redirect freely.**
Needs a live smoke test before it can be trusted.

## Goal

Manufacture single-segment practice depth on demand. The normal allocator spreads
attempts across all segments; to grind one segment (for clean depth on a target, or
just to watch its graph move intuitively), the user pins ONE segment and repeats it.

## Core principle

Reuse the entire normal practice loop — load state → play → detect attempt end →
record attempt. The ONLY change is "which segment next": instead of the scheduler/
allocator + history cursor choosing, a pinned `grind_segment_id` is returned every
cycle. => zero new data-quality risk (per project_data_hardening_golden_session).

## Decisions (made solo — flagged for Andrew)

1. **Not a new Mode.** Stays `Mode.PRACTICE`; grind is a `grind_segment_id: str | None`
   field on `PracticeSession`. No new mode transition, no new stop path.
2. **Pin point = `_segment_at_cursor()`** (practice.py). When `grind_segment_id` is set
   it resolves+returns that segment every cycle, bypassing scheduler AND history/cursor.
   Completion records the attempt normally; the cursor advance becomes a harmless no-op.
3. **Attempts tagged `chosen_allocator="grind_one"`** so grind-manufactured depth is
   distinguishable from allocator picks in later analysis. Honest attribution.
4. **Re-resolve the segment each cycle** (SegmentWithModel.load_all + filter) so a
   fresh expected-time shows up and a vanished state file cleanly ends the loop.
5. **Start validation:** grind start checks the segment exists for the game and its
   state file exists on disk; else a typed `GrindSegmentNotPracticableError` (409).
6. **Stop = normal practice stop button.** To return to allocator-spread practice,
   stop and start normal practice.
7. **R-menu nav (R+left/right) in grind:** left harmless — it drops+reloads the SAME
   pinned segment (acts as "redo"). Pause (R+X) / toggle (R+Y) unchanged.
8. **UI:** a "Grind" button on the segment-detail header and per-row in the segments
   table → `POST /api/practice/grind {segment_id}`. The existing mode=practice UI shows
   it running + the current segment; the route bar gets a `grind_segment_id` field for
   a "Grinding" badge.

## Open questions for Andrew

- Button placement: detail header + table rows (PoC does both) — too much? too little?
- Keep R+left/right as a harmless "redo" in grind, or disable nav entirely?
- Want a one-click "switch the grind target" without stop/start, or is stop/start fine?
- Should the grind badge show attempt count this session for the intuitive
  "watch the graph move" feel?

## Files touched

Backend: `practice.py` (grind field + `_segment_at_cursor` pin + `_build_history_entry`
allocator override), `session_manager.py` (`start_practice(grind_segment_id=...)` +
validation), `routes/practice.py` (new route), `errors.py` (new error),
`api_schemas.py` (route-summary `grind_segment_id`), `routes/model.py` (populate it).
Frontend: `model-api.ts` (postGrind), `segment-detail.ts` + `segments-view.ts`
(buttons), route-bar badge. Types regen via `npm run gen-types`.
