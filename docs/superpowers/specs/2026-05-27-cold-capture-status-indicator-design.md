# Cold-Capture Status Indicator — Design

**Date:** 2026-05-27
**Status:** approved (design)

## Motivation

During reference runs, every checkpoint segment needs a **cold** save state (the
deterministic post-respawn snapshot) before it can be practiced from a clean
start. Today there is no way to see which segments have their cold state and
which don't without running a practice/cold-fill pass. This blocks
data-collection planning: you can't tell at a glance where the gaps are.

Worse, the DB can drift out of sync with disk — a `waypoint_save_states` row can
point at a `.state` file that was later deleted (this has blocked HyperPlay
before, see `project_hyperplay_death_load_drops` / testing-feedback 2026-05-24).
The indicator must reflect what's actually on disk, not just what the DB claims.

Backlog origin: cold-capture-debt #3 (`docs/BACKLOG.md` / memory
`project_cold_capture_design_debt`).

## Goal

Per-segment, show in the Segments tab (Manage page) whether the segment's cold
state exists, so completeness is visible without playing.

## Semantics (decided)

`has_cold_state = True` **iff** the segment's start waypoint has a `cold`-variant
`waypoint_save_states` row **and that row's `state_path` file exists on disk.**

- Hot-only (cold variant absent) → `False`.
- Cold row present but file deleted → `False` (the orphaned-row case — the whole
  point of checking disk).
- No save states at all → `False`.

Rationale for disk-check over DB-row-only: it's the honest "can I cold-start this
segment right now?" signal and surfaces the orphaned-row inconsistency that a
row-presence check would hide.

## Scope

**In:** read-only per-segment cold-state column in the Segments tab.

**Out (deferred follow-up):** per-segment manual cold-capture *trigger* button.
Exercising it requires a live emulator/practice run, so it's a separate task.

## Architecture & data flow

The cold-status concern lives at the **route layer**, mirroring the route's
existing per-segment `get_waypoint(...)` loop. No new DB method and no change to
the shared `get_all_segments_with_model` query (it returns a cold-*preferred*
`state_path` that can't distinguish cold from hot-fallback).

1. **DB** (`python/spinlab/db/segments.py`): reuse the existing
   `Database.get_save_state(waypoint_id, variant_type) -> WaypointSaveState | None`.
   Called with `variant_type="cold"` it returns the cold variant's
   `state_path`, or `None` when there's no cold row.

2. **Route** (`python/spinlab/routes/segments.py`): inside the existing loop in
   `api_segments`, for each segment with a `start_waypoint_id`:
   `cold = db.get_save_state(swid, "cold")`,
   `has_cold_state = bool(cold and cold.state_path and os.path.exists(cold.state_path))`,
   add to the segment dict. (`False` when `swid` is `None`.) Adds one query +
   one `os.path.exists` per segment on Manage-page load — acceptable
   (tens–hundreds of segments, not a hot path).

3. **Schema** (`python/spinlab/api_schemas.py`): add `has_cold_state: bool` (required;
   the route always sets it) to `ApiSegment` (the item model in
   `SegmentsResponse`). Flows to the codegen'd frontend `ApiSegment` type via
   `npm run gen-types`.

4. **Frontend** (`frontend/src/segments-view.ts`): add a **"Cold"** column to the
   per-level table header and a `✓`/`✗` cell per row (`✗` gets a dim/warning
   class). No change to the existing Segment / Conditions / Primary columns.

## Testing (all verifiable without a live run)

- **DB/route unit tests:** seed segments and assert `has_cold_state`:
  - cold row + file present on disk → `True`
  - cold row + file path that does NOT exist → `False` (orphaned-row case)
  - hot-only (no cold variant) → `False`
  - no save states → `False`
- **Frontend unit test** (`frontend/src/segments-view.test.ts`): `renderSegmentsView`
  emits the Cold column with `✓`/`✗` driven by `has_cold_state`.
- **Playwright smoke:** Segments tab renders the Cold column (existing frontend
  smoke harness; no emulator).

## Edge cases

- No game loaded: `/api/segments` returns `{segments: []}` — unchanged.
- Segment with no start waypoint or no cold row: `cold_path` is `None` → `False`.
- File present in DB but deleted on disk: `False` (intended).

## Out of scope / follow-ups

- Per-segment manual cold-capture trigger button (needs a live run).
- Three-state display (cold / hot-only / none) — deferred; binary is enough for
  the capture-gap question. A hot-only segment is practicable via fallback but
  still needs cold capture, so it correctly reads `✗` here.
