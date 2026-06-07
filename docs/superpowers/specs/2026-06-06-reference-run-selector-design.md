# Reference Run as a First-Class Selectable Scope — Design

**Date:** 2026-06-06
**Status:** Approved (brainstorm)
**Supersedes:** `2026-06-04-run-segment-membership-design.md` (the minimal "Manage-tab only" version). This generalizes run-scoping to **every** page and adds a title-bar selector + deletion.

## Problem

A segment id is geography-keyed (`game:level:start.ord:end.ord:start_wp:end_wp`), so segment rows + save-states + pooled attempts are shared across every run over the same geography. Today a "run" is essentially a Manage-tab listing; Practice/Model/Simulator are **game-scoped** (`get_active_segments(game_id)`), so recording a new reference run doesn't change what those pages show. Live-observed 2026-06-06 (Cute Kaizo): a new 1-level reference run still showed segments/data from a prior multi-level run ("stuff not from that run"), because nothing scopes the model views to a run. The user wants the **selected reference run to scope visibility everywhere**, managed from a **title-bar selector** (not a Setup section).

## Core model (the load-bearing decision)

- **The active run scopes *visibility* on every page.** Segments, Setup, Play (Practice / Model table / Simulator / route+segment live views) show only the active run's **traversed** segments — membership = `segments.id ∈ (SELECT DISTINCT segment_id FROM attempts WHERE capture_run_id = <active> AND invalidated = 0)`, the same membership query the 2026-06-04 spec defined.
- **Practice *data* stays geography-pooled per segment.** `get_segment_attempts(segment_id)` is unchanged — a segment's attempts/graphs accumulate by geography and surface under any run that includes it. Switching runs changes *which* segments show, not how each segment's stats are computed. (So a level shared by two runs carries its practice history into both — a feature, cross-run learning.)
- **Save-states stay geography-keyed** (`waypoint_save_states`), unchanged. Same level = same geography = same state; a genuinely different layout changes the waypoint id and thus gets a distinct state. The overwrite observed during a re-record of the same level is correct.
- **"Run" = which segments you see/capture; "geography" = where practice stats live.** That split is the whole design.

## Existing infrastructure to reuse (don't rebuild)

- `capture_runs` has per-game `active` (at most one active saved run per game), `name`, `created_at`, `status` ('draft'|'saved'), `kind` ('live'|'replay'). `get_active_capture_run(game_id)`, `set_active_capture_run(run_id)`, `get_saved_capture_runs(game_id)` exist.
- `list_references` + `rename_reference` (PATCH) endpoints exist; the Setup References section already lists/selects/renames runs.
- `count_segments_traversed_in_run` already derives traversal membership from `attempts`.

So "current run" is already a persisted per-game concept. The work is (a) use it to scope all read paths, (b) move its management to a title-bar selector, (c) add deletion.

## Design

### A. Backend — thread the active run through read paths
1. Add a run-scoped segment query (active run → traversal membership; empty when no active run). Reuse the membership predicate from the 2026-06-04 spec.
2. Route it into every consumer that's currently game-scoped via `get_active_segments(game_id)`: the practice loop, `/api/model`, route-summary/live-summary, the simulator (`practice-engine`), and the segments list. **Attempt pooling stays game-wide / by geography** (`get_segment_attempts` unchanged).
3. **No active run → pages return an empty set**, and the frontend shows an empty state ("No reference run selected — pick or record one").

### B. Backend — deletion endpoint (new)
`DELETE /api/references/{run_id}` with a mode param (`run_only` | `run_and_data`):
- **run_only:** delete the `capture_runs` row + its `attempts` membership rows (the run's `capture_run_id`-stamped attempt rows). Segments/save-states/other-run attempts persist; geography-pooled history survives and re-surfaces under any future run over the same geography.
- **run_and_data:** the above, plus purge segments + save-states + attempts that are **exclusive** to this run (i.e. no other run's attempts reference that segment_id). Shared-with-another-run data is protected.
- Deleting the active run reassigns active → most-recent remaining saved run (or none).

### C. Frontend — title-bar run selector (beside the game name)
- Dropdown mirroring the game selector, populated from `list_references` for the current game.
- **Gated:** disabled until a game is selected.
- **Sort:** ≥4 runs → recent group on top (by `created_at` desc), then alphabetical; <4 → all alphabetical. (MVP "recent" = creation order; true last-used ordering would need a `last_active_at` touch — deferred, not MVP.)
- Selecting → `set_active_capture_run` → re-render all pages off the new active run.
- **Rename** available inline (pencil; rename endpoint exists).
- Each row has a red ✕ → deletion dialog (D).

### D. Frontend — 3-way delete dialog
Confirm **"Delete this Reference Run?"** with **[Delete Run] [Delete Run + Data] [Cancel]**; `+ Data` styled danger. Calls the endpoint (B) with the chosen mode. On success, refresh the selector; if the deleted run was active, fall back per (B).

### E. Lifecycle (auto-select, sensible defaults)
- Pick a game → auto-select its last-active run; if runs exist but none active, select most-recent.
- Finish a **new** reference run (finalize → status='saved') → `set_active_capture_run` to it.
- Game with no runs → selector empty; pages show the empty state.

### F. Setup page — retire the References section
- Remove the References **list** (select/rename/delete now live in the title-bar selector).
- **Keep recording controls** ("Start Reference Run", the live recording indicator, save-and-name) — recording needs a home. Net: the References *section* is gone, as requested.

## Testing (Red-Green)
- **Backend:** run-scoped segment query returns only the active run's traversed segments (incl. a re-record that owns 0); empty when no active run; attempt pooling unchanged (a shared segment's `get_segment_attempts` still returns all geography attempts). Deletion: `run_only` keeps shared data + removes membership; `run_and_data` purges only exclusive segments/attempts/states and protects shared; delete-active reassigns active. Lifecycle: finalize sets active; game-switch auto-selects last-active.
- **Frontend (vitest):** selector render/sort (≥4 vs <4)/gating/rename; 3-way delete dialog wiring; empty-state when no active run.
- **Playwright smoke:** selector present beside game name; Setup References list gone.
- Full `python -m pytest` (incl. emulator + frontend smoke) green before merge.

## Out of scope (separate threads)
- Kaizo HyperPlay RA-core/NCI corruption crash (own investigation).
- Poller-never-gives-up after RA death.
- Per-run *distinct* save states (not needed under the geography model).
- Run categories/metadata; `last_active_at` recency ordering (deferred).
- Priors-so-all-segments-estimable (separate modeling spec).

## Notes / risks
- Making `get_active_segments`-equivalent run-scoped touches every model-view consumer — enumerate them so none keep the game-scoped path. Attempt pooling must stay by geography (don't accidentally scope `get_segment_attempts`).
- `run_and_data` exclusivity check must be correct (a segment is exclusive iff no *other* run's non-invalidated attempts reference it) — test both directions.
- Empty-state (no active run) must be handled on every page, not just Practice.
