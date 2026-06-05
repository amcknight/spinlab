# Run↔Segment Membership — Design

**Date:** 2026-06-04
**Status:** Approved (brainstorm) — **Design X (minimal)**
**Topic:** Make the Manage tab's run-scoped segment views use **traversal membership** (already recorded in `attempts`) instead of single-owner `capture_run_id`, so a run that re-records existing levels shows the segments it captured.

## Problem

A segment's `id` is **geography-keyed** (`game_id:level:start.ord:end.ord:start_wp:end_wp` — `Segment.make_id` in `python/spinlab/models.py`); it carries no run or category. So segment rows are shared across every run that traverses the same geography. `segments.capture_run_id` records the **first** run to create the row and is never overwritten (`upsert_segment` ON CONFLICT keeps the first owner — the 2026-05-29 `48e47e3` replay-clobber fix).

Observed 2026-06-04 (game `5d5f596431889601` "Cute Kaizo"): a new reference run that re-records existing levels **owns zero** segments (they stay owned by the first run). The Manage tab's Segments section is run-scoped via `get_segments_by_reference` → `WHERE s.capture_run_id = ?`, so it shows "No segments" even though the run's header counter showed `2` captured. (DB confirmed: run `live_95f6be8c` owns 0; all 4 segments owned by older `live_75a45cfd`.)

## Key finding — membership already exists

The mechanism to fix this is **already in the codebase**; only two read queries use the wrong key.

- The recorder writes a per-event row stamped with `capture_run_id` + `segment_id` for **every** segment any run closes — live (`source=REFERENCE`) and replay (`source=REPLAY`). So `attempts` already records run↔segment **traversal** for every run.
- `count_segments_traversed_in_run` (`python/spinlab/db/segments.py`) already derives the honest "segments this run captured" count from that: `SELECT COUNT(DISTINCT segment_id) FROM attempts WHERE capture_run_id=? AND invalidated=0`. Its docstring describes this exact bug. This is why the header counter correctly showed `2`.
- **Replays already don't pollute the model.** `events_from_rows` (`python/spinlab/scheduler.py:53-70`) is the single sampler-ingestion seam and skips `source==REPLAY`; the fit gate does too (`scheduler.py:350-351`). So replay event rows are recorded (for provenance/membership) but excluded from every model view.
- The model **already pools across runs by geography**, category-agnostic (keyed on `segment_id`). Save-states are **already geography-attached** (`waypoint_save_states`, keyed by waypoint).

So everything the user wants (pool across runs/categories; replays independent and non-contributing; geography-attached save-states) already holds. The only defect is that **Manage queries ownership where it means traversal**.

"Ownership" stays meaningful and is **not** removed: `count_segments_for_run` (ownership) answers a different question — "did this run create any *new* geography rows?" — used for the recorder ordinal and replay cleanup. Membership ("which segments did this run go through") is the `attempts`-derived view. The bug was using the former where the latter was meant.

## Goals

1. Manage's run-scoped Segments view shows the segments a run **traversed** (captured), including re-records of existing levels.
2. Run-scoped cold-fill (`segments_missing_cold(game_id, run_id=)`) likewise scopes by traversal, not ownership.
3. No new schema, no migration, no recorder/replay changes — reuse the existing `attempts`-derived membership.

## Non-goals (and why)

- **A `run_segments` membership table** — considered and rejected: it would duplicate membership that already lives in `attempts`. Its only edge over Design X is robust per-session grouping for re-recorded *multi-session* runs (see caveat); not worth a new table + migration.
- Dropping `segments.capture_run_id`/`capture_session_id` — they still serve ownership semantics (`count_segments_for_run`, recorder ordinal, replay cleanup). Leave them.
- Any model/replay/save-state change — already correct.

## Design

Two query rewrites in the DB layer; nothing else.

**1. `get_segments_by_reference(capture_run_id)` (`python/spinlab/db/capture_runs.py`)** — replace the ownership predicate with traversal membership:

```sql
-- was:  WHERE s.capture_run_id = ? AND s.active = 1
-- now:
WHERE s.active = 1
  AND s.id IN (
    SELECT DISTINCT a.segment_id FROM attempts a
    WHERE a.capture_run_id = ? AND a.invalidated = 0
  )
ORDER BY s.ordinal
```

The SELECT list (incl. `s.capture_run_id`, `s.capture_session_id`, `cs.ordinal AS session_ordinal`) is unchanged so the response shape (`ReferenceSegmentRow`) is unchanged. **Caveat:** `session_ordinal` comes from the segment's owner session (`s.capture_session_id`); for a segment re-recorded by a *different* run that's the original owner's session, so per-session grouping degrades for re-records. Acceptable (display-only; Andrew accepted).

**2. Run-scoped `segments_missing_cold(game_id, run_id)` (`python/spinlab/db/segments.py`)** — the `run_id`-scoped branch uses traversal instead of ownership:

```sql
-- was:  run_clause = "AND s.capture_run_id = ?"
-- now:
run_clause = "AND s.id IN (SELECT DISTINCT segment_id FROM attempts WHERE capture_run_id = ? AND invalidated = 0)"
```

`run_id=None` (whole-game) branch is unchanged.

Frontend (`frontend/src/manage.ts`) and the `/api/references/{id}/segments` route are unchanged — the endpoint now returns the run's traversed segments.

## Testing (Red-Green)

Mirror `test_count_segments_traversed_in_run_counts_segments_owned_by_other_runs` (`tests/unit/db/test_db_segments.py`): a segment owned by run "old", an `EventAttempt` stamped `capture_run_id="new"`.

- **`get_segments_by_reference`** (`tests/unit/db/test_db_references.py`): a segment owned by "old" but traversed by "new" is returned by `get_segments_by_reference("new")` (Red: returns `[]`; Green: returns it). Invalidated-only traversal is excluded. Existing `test_get_segments_by_reference` (owner == traverser) still passes.
- **`segments_missing_cold`** (`tests/unit/db/test_db_segments.py`): a segment with hot-but-no-cold, owned by "old", traversed by "new", appears in `segments_missing_cold("g", run_id="new")` (Red: excluded; Green: included). Whole-game (`run_id=None`) behavior unchanged.
- Full `python -m pytest` (incl. emulator + frontend smoke) green before merge.

## Notes / risks

- Relies on every captured segment having ≥1 non-invalidated event row — the same assumption `count_segments_traversed_in_run` already makes and that the live counter already trusts.
- Confirm no other caller of `get_segments_by_reference` depends on ownership semantics (only the `/references/{id}/segments` route → Manage).
- "Model stuck on one reference run" needs no code change: the model is geography-pooled by design; switching the active run intentionally changes only the Manage listing, not the model. Communicate in UI if confusing (out of scope here).
