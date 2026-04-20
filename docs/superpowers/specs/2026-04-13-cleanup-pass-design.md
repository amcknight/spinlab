# Cleanup Pass — Capture Consolidation, Frontend Hygiene, Dead Code, Test Reorg

**Date:** 2026-04-13
**Status:** Complete — all sections implemented 2026-04-14 through 2026-04-20

## Goal

Pay down accumulated structural debt across capture, frontend testing/encapsulation, dead code, and test organisation, without touching the deeper domain model. The intent is a codebase that's measurably easier to navigate and refactor *before* the larger "find The Model" work begins (deferred to a future spec).

## Scope

In:

1. Consolidate the capture pipeline into a `capture/` package with sharper names.
2. Diagnose and fix the reference-seeding bug so reference run times become first-attempt practice data.
3. Add a Playwright smoke test that drives the real frontend against a fake-game-loaded dashboard.
4. Split `frontend/src/model.ts` (468 LOC) into layered modules following the existing `model-logic.ts` precedent.
5. Delete the Overlay timer.
6. Prune the `Status` enum down to states that are actually first-class (not just RPC error codes).
7. Audit `manifest.py` and any other quietly vestigial code; delete or document.
8. Reorganise the test suite: consolidate fragmented db tests, mirror source layout, prune large files for implementation-detail assertions.

Out (deferred to future specs):

- Re-modeling `Mode` / `AttemptSource` / generalised boundary events ("find The Model").
- Evaluation substrate / run fixture library / Bayesian estimators.
- TypeScript codegen from Python (existing `api-contract.test.ts` already covers contract drift adequately).
- End-to-end emulator-driven Playwright test (deserves its own design — out of scope here).
- Pre-start pause, room subsegments, merge/split, practice-time-includes-retry-time.

## 1. Capture pipeline consolidation

### Diagnosis

Five files own capture-adjacent concepts today:

- `reference_capture.py` (219 LOC) — misnamed; it's the segment-building engine, used by both reference *and* replay flows.
- `capture_controller.py` (260) — orchestrates reference + replay + fill_gap.
- `cold_fill_controller.py` (110) — separate orchestrator with its own queue; not a draft producer.
- `draft_manager.py` (85) — tiny state holder for pending reference drafts.
- `reference_seeding.py` (44) — one-call helper, only invoked from `DraftManager.save()`.

The shape isn't "five things doing the same job" — it's "two genuinely-different orchestrators (reference/replay vs cold-fill) sharing some primitives, with names that obscure that fact."

### Target layout

```
python/spinlab/capture/
  __init__.py          # re-exports public types
  recorder.py          # was reference_capture.py — segment-building engine
  draft.py             # was draft_manager.py + reference_seeding.py folded in
  reference.py         # was capture_controller.py — ref/replay/fill_gap orchestration
  cold_fill.py         # was cold_fill_controller.py — moved as-is
```

### Renames

| Old | New |
|-----|-----|
| `ReferenceCapture` | `SegmentRecorder` |
| `RefSegmentTime` | `RecordedSegmentTime` |
| `CaptureController` | `ReferenceController` |
| `ColdFillController` | `ColdFillController` (unchanged) |
| `DraftManager` | `DraftManager` (unchanged) |

`__init__.py` re-exports `SegmentRecorder`, `DraftManager`, `ReferenceController`, `ColdFillController`, and `RecordedSegmentTime` so callers import from `spinlab.capture` rather than submodules.

### What does NOT change

- `SessionManager` still talks to two orchestrators (`self.capture` and `self.cold_fill`). No false unification.
- Public method signatures on the orchestrators are preserved — only names and import paths change.
- TCP protocol and DB schema untouched.
- `protocol.py` capture-adjacent commands untouched.

### Rationale

The two-orchestrator topology matches the actual runtime semantics (reference flow produces a draft; cold-fill is a maintenance task with no draft, no segment building, just a queue). Forcing them through one pipeline would be Procrustean. Renaming + namespacing gives all the navigability benefit without the structural risk.

## 2. Reference seeding fix

### Symptom

Reference run segment times don't appear as first-attempt practice data after saving a draft. Estimators behave as if no data exists for a freshly captured segment.

### Two leading hypotheses

**H1 (likely):** Timestamps from Lua aren't propagating, so [reference_capture.py:117-118](python/spinlab/reference_capture.py#L117-L118) never appends to `segment_times`. [draft_manager.py:43](python/spinlab/draft_manager.py#L43) silently no-ops on empty `segment_times`. No error, no seeded attempts.

**H2:** Seeding runs but `save_draft(name, scheduler=None)` is called from a route that doesn't pass the scheduler. `db.log_attempt` writes the rows, but `scheduler.rebuild_all_states()` doesn't fire — estimators only pick up the data when the next attempt triggers a rebuild.

### Disambiguation

After a reference run + draft save, query `SELECT COUNT(*) FROM attempts WHERE source = 'reference'`:

- 0 rows → H1 (the seed call is short-circuiting on empty input)
- N rows → H2 (the seed call ran but the model wasn't rebuilt)

### Fix

Diagnose during the consolidation pass. Both hypotheses have surgical fixes:

- **H1:** Trace timestamp flow from Lua → protocol parser → `SegmentRecorder.handle_*` and repair the missing link. Likely a missed field in event parsing.
- **H2:** Wire scheduler through the save path so `scheduler` is never `None` at the seed call site.

### Regression test

Add a unit test in `tests/unit/capture/test_draft.py` (post-rename) that:

1. Constructs a `SegmentRecorder` with synthetic `RecordedSegmentTime` entries.
2. Saves the draft via `DraftManager.save()` with a mock scheduler.
3. Asserts `attempts` table has rows with `source='reference'` matching the recorded times.
4. Asserts `scheduler.rebuild_all_states()` was called.

This test would fail under both H1 (if extended to also drive the recorder via fake events) and H2 today.

## 3. Frontend Playwright smoke

### Goal

Catch FE/BE contract drift bugs that current tests miss — the kind that bite after refactors despite the unit suite passing. Concretely: render code reading a field that the API stopped sending, or expecting a shape that the API restructured.

### Approach

Add `tests/integration/test_frontend_smoke.py` (no `@pytest.mark.emulator` — runs with the fast suite). The test:

1. Spawns the real dashboard (FastAPI + Uvicorn) against an in-memory or temp DB, reusing the harness from `test_smoke.py`.
2. Pre-seeds the DB via `tests/factories.py` with: a game, a few segments, attempts across estimators, an active reference, a recent practice session.
3. Calls a new test-only helper that forcibly sets `SystemState` to `tcp_connected=True, game_id=<seeded>, game_context populated` — bypasses the TCP/Lua boundary so the dashboard renders as if a ROM is loaded. The helper lives in `tests/integration/conftest.py` (test fixture only — no production code path).
4. Drives Playwright through the real frontend bytes (built static assets via `frontend/npm run build` — already a precondition for `pytest -m frontend`).

### What the test asserts

- Every tab renders without console errors.
- SSE connects and a state update tick arrives.
- Practice card shows current segment data matching seeded fixtures.
- Segments tab lists seeded segments with correct counts.
- Manage tab shows the seeded reference.
- Model tab renders model outputs in the expected shape.

Roughly 150 LOC test + 50 LOC fixture helper.

### Why D, not B/C

- **B (no fake game):** Dashboard genuinely can't render most state without `game_id`. Test would either degrade to a tabs-only smoke (which is A) or require a full emulator (which is C).
- **C (with emulator):** The bugs we're catching are FE/BE contract drift, not emulator integration. C wouldn't catch them better; it'd catch them slower with more flake.
- **D (fake game):** Real frontend, real backend, real DB — only the ROM/Lua boundary is faked. That boundary is already covered by `test_smoke.py`.

### Future work

A separate emulator-driven Playwright test (the C option) is worth doing later but earns its own design. Out of scope here.

## 4. `model.ts` split

[model.ts](frontend/src/model.ts) is 468 LOC — 20% of the frontend. A precedent already exists: `model-logic.ts` was extracted previously. Extend the same pattern.

### Target layout

```
frontend/src/
  model.ts             # orchestration + DOM event binding
  model-logic.ts       # pure logic (already exists)
  model-render.ts      # DOM building / template functions
  model-api.ts         # fetch helpers + response coercion
```

### Rationale for layered (B) over feature-split (C)

- Matches the existing `model-logic.ts` precedent — consistency over novelty.
- Layers stay meaningful regardless of how the model tab's *sections* evolve. Phase 3 ("find The Model") may shake up what the tab shows; B's filenames don't go stale, C's would.
- The model tab's sections (segments table, allocator weights, tuning) share little rendering, so C's "one feature, one file" benefit is real but smaller than usual.

### Out of scope

`segment-detail.ts` (203) and `manage.ts` (224) follow the same pattern only if they grow further. Don't split them now.

## 5. Overlay timer removal

The Overlay timer was floated for removal in `future.txt:10`. Doubt is the right signal; delete it. Keep the Goal display (still load-bearing for hot-start orientation). Removal touches the Lua overlay rendering and any Python-side state that drives it.

## 6. `Status` enum pruning

Today's [models.py:63-81](python/spinlab/models.py#L63-L81) `Status` has ~15 values, mostly negative ("NOT_CONNECTED", "NOT_IN_REFERENCE", "NO_HOT_VARIANT", etc). Most are RPC error codes, not domain states.

### Approach

For each `Status` value:

- **First-class state** (e.g. `OK`, `STARTED`, `STOPPED`, `DRAFT_PENDING`, `PRACTICE_ACTIVE`): keep.
- **Negative/error code** (e.g. `NOT_CONNECTED`, `NOT_IN_REFERENCE`, `NO_HOT_VARIANT`, `ALREADY_RUNNING`, `NO_DRAFT`, etc): replace with raised typed exceptions caught at the route boundary and translated to HTTP responses.

### Practical limits

Pruning is bounded to the cleanup pass — don't redesign the error model. If a value's call sites are scattered or its replacement creates more churn than clarity, leave it and add a short docstring on the value explaining why it stays. The goal is "fewer states, clearer semantics" not "zero states left."

### Out of scope

Replacing `ActionResult` itself, redesigning route error mapping, or touching the FE's error handling beyond what's mechanically required.

## 7. Dead-code audit

A short bounded sweep, not a refactor. Look at each candidate and either keep with documentation or delete:

- `manifest.py` (64 LOC) — claims to "find, load, and seed DB from reference manifests." Verify whether anything still imports it; check git log for recent meaningful use.
- `reference_seeding.py` — folded into `capture/draft.py` as part of section 1 (so it disappears as a standalone file regardless).
- Any other modules surfaced during the audit.

Time-box: half a day. If audit reveals something genuinely worth keeping but undocumented, add a docstring explaining why it earns its place.

## 8. Test suite cleanup

### Reorganisation

Mirror the source layout:

```
tests/
  unit/
    capture/           # was test_reference_capture, test_draft_lifecycle, test_cold_fill, etc
    db/                # was test_db_dashboard, test_db_references, test_db_attempts, test_waypoints_db
    estimators/        # was test_kalman, test_exp_decay, test_rolling_mean
    routes/            # was test_*_route
    ...
  integration/         # already in this shape
```

### Pruning — conservative

Two oversized files get a *light* behavior-vs-implementation pass:

- `tests/test_session_manager.py` (543 LOC)
- `tests/test_dashboard_integration.py` (440 LOC)

Criterion for deletion is intentionally narrow: a test only goes if it asserts something *no consumer relies on* (e.g. "this private attribute equals X after this call") AND it would need rewriting anyway because of section 1 or 6. Anything ambiguous stays.

Default is to keep. The bias is "leave it alone unless removing makes the suite measurably clearer." A test that's verbose but documents real behavior earns its keep — verbosity is not a deletion criterion.

If a test fails after the rename/refactor and the underlying behavior still holds, *update* the test rather than deleting it.

### Update

`CLAUDE.md` test commands updated if the path changes (e.g. `pytest tests/unit/capture/`).

## Sequencing

Suggested order:

1. **Test reorg first** (section 8 reorganisation only, no pruning yet). Mechanical move; gives a clean foundation for everything else.
2. **Capture consolidation + seeding fix** (sections 1, 2). Together because the rename and the bug fix touch the same files.
3. **Status pruning** (section 6). Touches the routes layer; benefits from the consolidated capture being already done.
4. **Frontend Playwright** (section 3). Independent.
5. **model.ts split** (section 4). Independent.
6. **Overlay timer + dead code audit** (sections 5, 7). Small, opportunistic.
7. **Test pruning** (section 8 second pass). Done last because earlier sections naturally invalidate some implementation-detail tests, making them obvious deletions.

Each section can be its own implementation plan; one PR per section is reasonable.

## Verification

For each section:

- Full `pytest` passes (per project rule: no red suite is acceptable).
- `cd frontend && npm run build && npm test && npm run typecheck` passes.
- Manual smoke: `spinlab dashboard` boots, all tabs render with seeded data.
- Section-specific:
  - Section 1: `from spinlab.capture import ...` works; smoke + replay fixture tests pass.
  - Section 2: New regression test passes; manual reference run produces seeded attempts.
  - Section 3: New Playwright test runs in fast suite (no emulator needed).
  - Section 6: `Status` value count shrinks; no `KeyError` on undefined status anywhere.

## Risk

Low across the board. No DB schema changes, no protocol changes, no model changes. Largest blast radius is section 1 (touches every importer of the capture modules) but the changes are mechanical renames.
