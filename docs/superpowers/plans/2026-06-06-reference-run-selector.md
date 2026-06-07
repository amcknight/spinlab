# Reference Run Selector + Run-Scoped Pages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Each task: read the exact current code first, then TDD (failing test → implement → pass → commit). Steps use checkbox (`- [ ]`).

**Goal:** Make the selected reference run scope visibility on every page, managed from a title-bar selector with 3-way deletion, retiring the Setup References section. Practice *data* stays geography-pooled.

**Spec:** `docs/superpowers/specs/2026-06-06-reference-run-selector-design.md` (read it first — it has the load-bearing model + all decisions). Supersedes `2026-06-04-run-segment-membership-design.md`.

**Architecture:** Backend reuses the existing per-game `active` capture run (`get/set_active_capture_run`) and traversal membership (`attempts` by `capture_run_id`). A run-scoped segment query replaces game-scoped `get_active_segments` in every model-view consumer; attempt pooling (`get_segment_attempts` by geography) is untouched. Frontend adds a title-bar run selector (mirrors the game selector) + a 3-way delete dialog, and removes the Setup References list (keeps recording controls).

**Tech Stack:** Python (FastAPI, pytest), TypeScript (Vite, Vitest), SQLite.

**Baseline:** run full `python -m pytest` green before starting (do NOT run with the dashboard open — its RA contends with `test_two_harnesses_use_distinct_nci_ports`; see `memory/project_test_reliability_known_issues.md`).

---

### Task 1: Run-scoped segment query (backend, no consumers yet)

**Files:** `python/spinlab/db/segments.py` (+ a new `get_segments_for_run` or a `run_id=` param on the active-segments path); test `tests/unit/db/test_db_segments.py`.

- [ ] **Step 1 — failing test:** mirror `test_count_segments_traversed_in_run_*`. Seed a segment owned by run "old", an `EventAttempt` stamped `capture_run_id="new"` (non-invalidated). Assert the run-scoped query for "new" returns that segment (Red), for a run with no attempts returns `[]`, and invalidated-only traversal is excluded.
- [ ] **Step 2 — implement:** add a run-scoped active-segments query using the membership predicate:
  ```sql
  WHERE s.active = 1
    AND s.id IN (SELECT DISTINCT a.segment_id FROM attempts a
                 WHERE a.capture_run_id = ? AND a.invalidated = 0)
  ORDER BY s.ordinal
  ```
  Keep the `Segment` row shape identical to `get_active_segments`. Decide: new function `get_segments_for_run(game_id, run_id)` vs `get_active_segments(game_id, run_id=None)`. Prefer a separate function to keep the game-scoped one for any legitimate whole-game caller; grep callers first.
- [ ] **Step 3 — pass + commit.** ruff/pyright clean on changed files.

### Task 2: Thread the active run into every model-view consumer (backend)

**Files:** `python/spinlab/session_manager.py` (the `_active_segments`/snapshot inputs path), `python/spinlab/routes/model.py` (`/api/model`, route/live-summary), the practice loop, the simulator (`practice-engine`) state, `python/spinlab/state_builder.py`. Tests: extend the relevant route/unit tests.

- [ ] **Step 1 — enumerate consumers:** grep `get_active_segments(` across `python/spinlab`. Each call that drives a *model view* (practice, model table, route/live-summary, simulator, segments list) must resolve the active run via `get_active_capture_run(game_id)` and use the Task-1 run-scoped query; `None` active → empty list. **Do NOT change `get_segment_attempts`** (pooling stays by geography).
- [ ] **Step 2 — failing tests:** with an active run that traversed only segment X (of two in the game), assert `/api/model`, live-summary, and the practice segment set return only X. With no active run, they return empty. A shared segment's `get_segment_attempts` still returns all geography attempts (pooling unchanged).
- [ ] **Step 3 — implement, pass, commit.** Watch for the snapshot path (`_snapshot_inputs`) — the session snapshot should also scope to the active run so route Exp.Run/floor reflect the run.

### Task 3: Deletion endpoint (backend)

**Files:** `python/spinlab/db/capture_runs.py` + `python/spinlab/routes/` (references routes); test `tests/unit/db/test_db_references.py` + the references route test.

- [ ] **Step 1 — failing tests:** `run_only` removes the `capture_runs` row + that run's `attempts` rows, keeps segments/states/other-run attempts; a segment shared with another run survives. `run_and_data` additionally purges segments/save-states/attempts **exclusive** to the run (no other run's non-invalidated attempts reference the segment_id), and PROTECTS shared ones. Deleting the active run reassigns active → most-recent remaining (or none).
- [ ] **Step 2 — implement** `delete_capture_run(run_id, *, purge_data: bool)` in the DB layer (exclusivity check: `segment_id NOT IN (SELECT segment_id FROM attempts WHERE capture_run_id != ? AND invalidated = 0)`), and `DELETE /api/references/{run_id}?mode=run_only|run_and_data` calling it. Reassign active via existing `set_active_capture_run`.
- [ ] **Step 3 — pass + commit.** gen-types so the FE sees the new endpoint.

### Task 4: Title-bar run selector (frontend)

**Files:** `frontend/index.html` (selector beside `#game-name`), `frontend/src/app.ts` (wire active-run state + render, mirror game-selector), a new `frontend/src/run-selector.ts` (pure render: list→sorted options, gating, rename pencil, red ✕), `frontend/style.css`; tests `frontend/src/run-selector.test.ts`.

- [ ] **Step 1 — failing test (pure sort/gating):** `runSelectorOptions(runs)` → ≥4 runs: recent (created_at desc) group on top then alphabetical; <4: all alphabetical. Gating: disabled when no game.
- [ ] **Step 2 — implement** the pure helper + render; selecting POSTs `set_active` (reuse/confirm the existing select endpoint the Setup References section used), then triggers a full re-fetch/re-render of pages. Rename via existing PATCH. Lifecycle: on game change auto-select last-active (or most-recent); after a new run finalizes, select it.
- [ ] **Step 3 — pass, typecheck, build, commit.**

### Task 5: 3-way delete dialog (frontend)

**Files:** `frontend/src/run-selector.ts` (or a small dialog module), `frontend/style.css`; test alongside.

- [ ] **Step 1 — failing test:** clicking ✕ opens "Delete this Reference Run?" with [Delete Run] [Delete Run + Data] [Cancel]; each button calls the endpoint with the right mode; `+ Data` has the danger class; Cancel closes with no call.
- [ ] **Step 2 — implement + wire** to the Task-3 endpoint; on success refresh selector + re-render; if deleted run was active, reflect the backend's active fallback.
- [ ] **Step 3 — pass, typecheck, build, commit.**

### Task 6: Retire the Setup References section (frontend)

**Files:** `frontend/index.html` (remove the References list block, keep `#btn-ref-start` + live indicator + save-and-name), `frontend/src/` (remove the references-list render/handlers now living in the selector; keep recording flow), `frontend/style.css`; update `frontend/src/manage.test.ts` / any test asserting the old References list.

- [ ] **Step 1 — failing/adjusted tests:** the Setup page no longer renders the References list; recording controls still present and functional; a vitest/smoke asserts the selector is the run-management surface.
- [ ] **Step 2 — implement, pass, typecheck, build, commit.**

### Task 7: Smoke + full gate

- [ ] Playwright smoke: run selector present beside the game name on load; Setup References list gone; selecting a run re-renders. (Mirror existing smoke patterns; presence-level where live data isn't available.)
- [ ] `cd frontend && npm run build` then full `python -m pytest` — all pass, 0 skipped (dashboard CLOSED). `ruff check python/` + `npx pyright` on changed files: no new errors. Commit.

---

## Self-Review
- Spec coverage: core scope → Tasks 1-2; deletion → Tasks 3,5; selector + lifecycle → Task 4; Setup retire → Task 6; tests/gate → all + Task 7. ✓
- Pooling-stays-by-geography invariant: explicitly called out in Task 2 (don't touch `get_segment_attempts`). ✓
- Empty-state (no active run) on every page: Task 2 Step 2 + Task 4. ✓
- `run_and_data` exclusivity correctness: Task 3 tests both directions. ✓

## Execution note
Phased; each task is a clean commit. Backend (1-3) before frontend (4-6). Create the implementation branch at start (`feat/reference-run-selector`). Do NOT run the full suite with the dashboard open.
