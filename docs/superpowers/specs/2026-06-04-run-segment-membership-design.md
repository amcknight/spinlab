# Run↔Segment Membership — Design

**Date:** 2026-06-04
**Status:** Approved (brainstorm)
**Topic:** Replace single-owner `capture_run_id` on segments with a many-to-many run↔segment membership; make replays independent and non-contributing to practice data.

## Problem

A segment's `id` is **geography-keyed** (`game_id:level:start.ord:end.ord:start_wp:end_wp` — see `Segment.make_id` in `python/spinlab/models.py`). It carries no run or category. So segment rows are shared across every run that traverses the same geography. The run↔segment relationship is, in reality, **many-to-many** (a run captures many geographies; a geography is captured by many runs).

Today that many-to-many is stored as a **one-to-many** via a single `segments.capture_run_id` "owner" column, set on first capture and deliberately never overwritten (`upsert_segment` ON CONFLICT keeps the first owner — the 2026-05-29 `48e47e3` replay-clobber fix). Consequences observed in the 2026-06-04 smoke (game `5d5f596431889601` "Cute Kaizo"):

- A new reference run that re-records existing levels owns **zero** segments (they stay owned by the first run). The Manage tab's Segments section is **run-scoped** (`get_segments_by_reference` → `WHERE capture_run_id=? AND active=1`), so it shows "No segments" even though the run's header counter showed `2` captured during the run. (DB confirmed: run `live_95f6be8c` owns 0; all 4 segments owned by the older `live_75a45cfd`.)
- The Model tab is **geography/game-scoped** (`/api/model` → `get_all_segments_with_model(game_id, primary_only=True)`); it shows the shared primaries and never reflects "which run," so switching the active reference run does nothing to it. This is correct pooling behavior, but reads as "stuck on one reference run."
- "Ownership" is conceptually confusing because one key (`segment_id` = geography) is doing two jobs: the **artifact key** ("what a run captured") and the **modeling/pooling key** (`attempts`, `model_state`, `segment_fits` all join on `segment_id`).

## Goals

1. Model run↔segment as a real **many-to-many membership**; remove the "owner" concept.
2. Keep **geography as the universal pooling unit**: modeling data stays keyed on `segment_id`, pooling across all runs of a game regardless of category or route. Category is **not** a first-class structural key.
3. Keep **save-states attached to geography** (canonical cold/hot per geography — the existing `waypoint_save_states` model). Re-recording (any run, including replay) **updates** the canonical save-state.
4. Make **replays independent**: a replay gets membership and may refresh save-states, but writes **no** practice/modeling attempts (which also stops it double-counting the original run's data into the pooled model).
5. Fix the user-visible symptoms: Manage shows each run's captured segments; the Model's geography-pooling behavior is preserved (and intentional).

## Non-goals

- Per-run or per-category save-states (explicitly rejected — geography-attached canonical is the chosen model).
- Cross-category pooling as new work: it falls out for free because the geography key is already category-agnostic.
- The "some loads feel different" capture-quality issue (likely cold-state captured mid-respawn-animation). Filed separately, not part of this refactor.
- Any change to how the Model tab scopes data (it stays geography/game-pooled by design).

## Data model

**Unchanged:**
- `segments` rows stay geography-keyed; `attempts`, `model_state`, `segment_fits` keep joining on `segment_id`.
- `waypoints` and `waypoint_save_states` (canonical cold/hot per geography) — unchanged.
- `capture_runs` (`id, game_id, name, status, active, kind`) — `kind` already distinguishes `live`/`replay`. Category remains an optional label only; no structural use.

**New — `run_segments` membership join:**

```
run_segments(
  capture_run_id     TEXT NOT NULL,   -- FK capture_runs.id
  segment_id         TEXT NOT NULL,   -- FK segments.id (geography)
  capture_session_id TEXT,            -- session within the run that (last) captured it
  ordinal            INTEGER,         -- capture order within the run (for display)
  captured_at        TEXT NOT NULL,
  PRIMARY KEY (capture_run_id, segment_id)
)
```

A run's geographies = `SELECT ... FROM segments s JOIN run_segments rs ON rs.segment_id = s.id WHERE rs.capture_run_id = ? AND s.active = 1`. `capture_session_id` on the join preserves Manage's per-session grouping (`session_ordinal`).

**Removed (after backfill):** `segments.capture_run_id`, `segments.capture_session_id`. Membership now lives only in `run_segments`. `Segment.capture_run_id` / `capture_session_id` (dataclass in `models.py`) become inputs the recorder uses to write the membership row, not persisted columns on the segment.

## Behavior

**Capture (recorder, `python/spinlab/capture/recorder.py`):** when a geography is captured/closed, in addition to `upsert_segment` (geography def) and the save-state write, insert/replace a `run_segments` membership row for the active `(capture_run_id, capture_session_id, ordinal)`. `upsert_segment` no longer needs the keep-first-owner ON CONFLICT carve-out for `capture_run_id` (the column is gone); re-recording naturally adds a membership row for the new run.

**Re-record updates canonical save-state:** any run (live or replay) that re-captures a geography updates its canonical cold/hot save-state. No ownership gate.

**Replays (`kind=replay`):** get `run_segments` membership and may refresh save-states, but the recorder must **not** write event-attempts when the run is a replay. Today `_close_segment` writes event-attempts unconditionally via `self._source` (recorder.py:251-262); gate that write on the run not being a replay. [Plan: confirm how `kind`/`_source` is threaded into the recorder and pick the cleanest gate — likely skip `log_event_attempt` when `kind == "replay"`.]

## Queries / API

- `get_segments_by_reference` (`db/capture_runs.py`), run-scoped `segments_missing_cold` (`db/segments.py`), and any `sections_captured`/run-scoped count → rewrite to JOIN `run_segments` instead of `WHERE capture_run_id=`.
- `/api/segments` (`get_all_segments_with_model`) and `/api/model` — unchanged (geography/game-scoped; their SELECTs don't reference the dropped columns). The dropped columns appear only in `get_segments_by_reference`'s SELECT, which the JOIN rewrite above replaces (`rs.capture_run_id`, `rs.capture_session_id`).
- Manage frontend (`frontend/src/manage.ts`) keeps calling `/api/references/{id}/segments`; it now returns the run's membership-joined segments. No frontend logic change required (fixes "segments vanish").

## Migration (`python/spinlab/db/migrations/0008_run_segments.sql`)

1. `CREATE TABLE run_segments (...)`.
2. Backfill: one membership row per existing segment from its current `capture_run_id` / `capture_session_id` (`INSERT INTO run_segments SELECT capture_run_id, id, capture_session_id, ordinal, COALESCE(updated_at, created_at) FROM segments WHERE capture_run_id IS NOT NULL`).
3. Drop `segments.capture_run_id` and `segments.capture_session_id` (SQLite: table rebuild per the existing migration-runner conventions).

Migrations are immutable once shipped; any correction is a later migration.

## Testing (Red-Green)

- **The core bug:** a second run that re-records the same geographies yields its own `run_segments` membership; `get_segments_by_reference(new_run)` returns those segments (currently returns `[]`). Red before, green after.
- **Replays don't contribute:** a replay run produces membership + (optionally) refreshed save-states but writes **zero** new event-attempts; the geography's attempt/model data is unchanged after a replay (no double-count).
- **Pooling preserved:** attempts from two different runs over the same geography both feed `get_segment_attempts(segment_id)` / the model — unchanged across the refactor.
- **Migration backfill:** an existing DB's segments each get exactly one membership row matching their pre-migration `capture_run_id`; run-scoped queries return the same sets they did pre-migration for single-run geographies.
- Full `python -m pytest` (incl. emulator + frontend smoke) green before merge.

## Risks / notes

- `segments_missing_cold` run-scoping and cold-fill depend on the membership join — verify cold-fill's run-scoped path (`session_manager._launch... / capture/cold_fill.py`) reads via the join.
- Confirm nothing else reads `segments.capture_run_id` directly before dropping the column (grep at plan time).
- `attempts.capture_run_id` stays (provenance of practice attempts); only the `segments` columns move to the join.
