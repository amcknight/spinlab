# Attempt Surgery Table

Date: 2026-06-07
Status: design (approved in brainstorm, pending written-spec review)

## Motivation

Modeling is gated on clean data: today's backtest showed that excluding 38 outlier
attempts cut clean-time prediction error 2.6x. Before a fresh "golden" data
collection session we need a way to **see every attempt and remove the bad ones
safely** — mistake outliers (a 41.7s "clean" clear on a ~13s segment), AFK times,
mis-bounded recordings. The `compute_golds` fix already merged (Best now respects
`invalidated`, Floor already did) means invalidation finally moves every model
number — so a table that drives invalidation is now meaningful.

This is the "Run Surgery" idea right-sized: not a new panel, a focused attempt
table dropped into the existing segment-detail view.

## Surface

A sortable attempt table added to the **segment-detail view** (clicked from the
Model tab). The existing Chart.js trend chart can stay above it; the broader
segment-page redesign stays parked. The table is the surgery surface.

## Columns

`Order · Clean Tail · Total · Deaths · Ago · action`

- **Order** — chronological attempt index (1 = oldest).
- **Clean Tail** — the success-leg time (the final clear with no deaths). `—` for
  incomplete episodes (died out, never cleared).
- **Total** — full episode time incl. death legs + per-death penalty.
- **Deaths** — death count in the episode.
- **Ago** — relative age, compact with a unit suffix: `now / 47s / 34m / 7h / 6d /
  8w / 7y`. **Months are skipped** (so `m` unambiguously means minutes); weeks roll
  to years around 52w.
- **action** — the invalidate/restore control (see below). Not sortable.

**Sorting:** every data header is **click-to-sort**; **double-click flips**
direction. Default sort: **Clean Tail, longest first** (mistakes on top). The
current floor (best clean clear) is marked with a **★** on its Clean Tail. No
outlier warnings or algorithmic flags — the sort plus the user's eye.

## Data source

The table lists **every episode the model pools for this segment** —
`get_segment_attempts(segment_id)`, which returns all rolled-up episodes
regardless of `is_hot` (the main estimator pools hot and cold uniformly today).
No hot/cold column or filter. Incomplete episodes are shown (Clean Tail `—`); a
truly empty episode (no success, no death) should not exist because an episode
only exists when an event was logged — we do not special-case or guard it.

## Action: invalidate (reversible only)

Toggling invalidate flips the **whole episode** — every death event's time AND
the success time — in and out of the model. Because `invalidated` is episode-level
(`set_attempt_invalidated` expands a row to its `episode_id` and flips all rows),
an invalidated episode drops out of `success_time_pool`, `death_time_pool`, the
EMAs, gold (`compute_golds`), and floor. Invalidated rows **stay** in the table,
struck-through/greyed, with a **⟲ restore** control. The action is fully
reversible.

**No hard-delete.** Invalidation already achieves "exclude the bad ones" safely
and reversibly; a destructive delete (and its confirm UX + endpoint) is not built.

This is episode-level surgery. Removing a single bad death-time from an otherwise-
good episode (event-level) is **out of scope** — the dominant problem is bad
clean-tails, which are episode-success outliers that episode-level invalidation
nails.

## Auto-recalc on toggle

Invalidating (or restoring) immediately rebuilds **that segment's** model_state
(per-segment, cheap) so Best / Floor / Room update in the table and the Model tab
without a separate "recalculate" step. This is the one new piece of backend
wiring: `PATCH /api/attempts/{id}` currently only sets the flag; it must also
trigger a per-segment model_state rebuild. The route resolves the segment from the
attempt and rebuilds just that segment (reusing the per-segment rebuild path the
episode-close flow already uses; extract a small `rebuild_segment(segment_id)`
helper if one isn't cleanly callable).

## Backend changes

1. **A surgery list route** — return every episode for a segment carrying the
   fields the table needs, including `id` (the per-episode event id for PATCH) and
   `invalidated`, which the current `/api/segments/{id}/history` route strips (it
   pre-filters invalidated and omits the id, because it feeds the trend chart). Add
   a dedicated route (e.g. `GET /api/segments/{id}/attempts`) rather than
   overloading `/history`, so the chart's filtered view is untouched. Fields per
   row: `id`, `order` (chronological index), `clean_tail_ms`, `time_ms` (total),
   `deaths`, `created_at`, `invalidated`, `completed`, and an `is_floor` flag (the
   row whose clean_tail equals the segment's current floor).
2. **Recalc-on-invalidate** — `PATCH /api/attempts/{id}` rebuilds the affected
   segment's model_state after flipping the flag (see above).

No delete endpoint, no recorder guard, no schema migration (the `invalidated`
column already exists).

## Frontend changes

A new attempt-table component rendered in the segment-detail view
(`frontend/src/segment-detail.ts` today is chart-only). The sort logic (column +
direction state, comparator, the `—`/null handling) lives in a pure,
unit-tested helper. The `Ago` formatter is a pure function (compact units,
skip-months) — likely an addition to `frontend/src/format.ts`. Invalidate/restore
calls `PATCH /api/attempts/{id}`, then re-fetches the list and the model so the
recalc'd numbers and the struck-through state render. Invalidated rows render
struck-through; the floor row shows the ★.

## Out of scope (explicitly deferred)

- Hard-delete; event-level (single death-time) surgery.
- Recorder guard / cleanup of empty episodes (expected not to occur).
- Hot/cold column or filter; start-condition ("on Yoshi") column — future, skipped.
- The broader segment-page redesign.
- Making the cold-only histogram respect invalidation (separate surface).

## Testing

- **Backend:** the surgery list route returns invalidated rows (with `id`,
  `invalidated`) — distinct from `/history` which omits them; `is_floor` flags the
  right row; `PATCH` flips the episode AND the segment's model_state reflects it
  (Best/Floor change after invalidating the gold attempt). Episode-level semantics:
  invalidating one event id flips the whole episode.
- **Frontend:** the `Ago` formatter (each unit boundary, skip-months, `now`); the
  sort helper (each column, double-click flip, `—`/null ordering, default = Clean
  Tail desc); the table renders struck-through invalidated rows + ★ on the floor.
- Full gate before merge: `python -m pytest` + `cd frontend && npm test` +
  `npm run typecheck` + `npm run build`.
