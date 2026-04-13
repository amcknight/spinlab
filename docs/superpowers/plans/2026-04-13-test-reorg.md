# Test Suite Reorganisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganise the flat `tests/` directory to mirror the `python/spinlab/` source layout so a test's home is obvious from the module it covers. No test content changes, no pruning — this is Phase 1 of the 2026-04-13 cleanup pass ([spec](../specs/2026-04-13-cleanup-pass-design.md), section 8, sequencing step 1).

**Architecture:** Mechanical `git mv` of unit-style tests into topic subdirectories (`capture/`, `db/`, `estimators/`, `routes/`, `allocators/`, `cli/`) under a new `tests/unit/` root. Remaining single-module tests live flat at `tests/unit/`. `tests/integration/` is already in target shape — untouched. Factory imports via `from tests.factories import ...` continue to work because `tests/` remains a package.

**Tech Stack:** pytest, Python 3.11, git mv.

---

## Constraints

- **No test content edits.** If a test breaks after a move, the move is wrong — revert and investigate. Content-level pruning is a later plan.
- **Use `git mv`.** Preserves blame. Windows is case-sensitive on path but case-insensitive on filesystem — `git mv` handles this correctly; plain `mv` does not.
- **One commit per subdirectory.** Keeps reverts surgical if one group breaks collection.
- **Run the full fast suite after every move.** Collection errors from missing `__init__.py` or stale `.pyc` are the most likely failure mode.
- **Do not add `__init__.py` files.** `tests/` is already a package for the factories import to work; pytest does rootdir-relative collection and does not need per-subdir `__init__.py`. If collection fails, add `__init__.py` then — don't pre-emptively add.

## File mapping

Source module → target directory:

| Source module(s) | Target dir | Test files |
|---|---|---|
| `reference_capture.py`, `capture_controller.py`, `cold_fill_controller.py`, `draft_manager.py`, `reference_seeding.py` | `tests/unit/capture/` | `test_reference_capture.py`, `test_capture_controller.py`, `test_capture_with_conditions.py`, `test_cold_fill.py`, `test_cold_fill_integration.py`, `test_draft_lifecycle.py`, `test_reference_seeding.py` |
| `db/` | `tests/unit/db/` | `test_db_attempts.py`, `test_db_dashboard.py`, `test_db_references.py`, `test_waypoints_db.py` |
| `estimators/` | `tests/unit/estimators/` | `test_kalman.py`, `test_exp_decay.py`, `test_rolling_mean.py`, `test_estimator_params.py`, `test_estimator_sanity.py` |
| `routes/` | `tests/unit/routes/` | `test_attempts_route.py`, `test_segments_route.py`, `test_dashboard_references.py` |
| `allocators/` | `tests/unit/allocators/` | `test_allocators.py`, `test_mix_allocator.py` |
| `cli.py` | `tests/unit/cli/` | `test_cli.py`, `test_cli_db_reset.py`, `test_cli_logging.py` |

Remaining files stay flat at `tests/unit/` (single-module sources, no subpackage to mirror):

```
test_attempts_conditions.py        test_practice.py
test_attempts_invalidation.py      test_practice_coverage.py
test_condition_registry.py         test_protocol.py
test_condition_registry_startup.py test_replay.py
test_config.py                     test_reset_logging.py
test_dashboard_integration.py      test_romid.py
test_estimator_params.py           test_scheduler_fallback.py
test_fake_tcp.py                   test_scheduler_kalman.py
test_invalidate_flow.py            test_segment_variants.py
test_model_output.py               test_segment_with_model.py
test_models.py                     test_segments_is_primary.py
test_models_enums.py               test_session_manager.py
test_multi_game.py                 test_session_manager_conditions.py
                                   test_speed_run_mode.py
                                   test_spinrec.py
                                   test_sse.py
                                   test_state_builder.py
                                   test_system_state.py
                                   test_tcp_manager.py
                                   test_vite.py
```

`tests/conftest.py`, `tests/factories.py`, `tests/fixtures/`, `tests/integration/`, `tests/playwright/` stay where they are.

---

## Task 0: Baseline

**Files:** none (reconnaissance).

- [ ] **Step 1: Confirm clean working tree**

Run: `git status`
Expected: clean. If not, stop and ask the user.

- [ ] **Step 2: Capture baseline test counts**

Run: `python -m pytest --collect-only -q 2>&1 | tail -5`
Record the collected count. Every subsequent task must produce the same count.

- [ ] **Step 3: Run full fast suite as baseline**

Run: `pytest -m "not (emulator or slow or frontend)"`
Expected: all green. If anything fails, stop and fix per project rule ("fix all failures, even pre-existing ones") before reorganising.

---

## Task 1: Create `tests/unit/` and move capture tests

**Files:**
- Create: `tests/unit/` (directory)
- Move: 7 files from `tests/` to `tests/unit/capture/`

- [ ] **Step 1: Create the unit directory**

```bash
mkdir -p tests/unit/capture
```

- [ ] **Step 2: Move capture test files with git mv**

```bash
git mv tests/test_reference_capture.py       tests/unit/capture/test_reference_capture.py
git mv tests/test_capture_controller.py      tests/unit/capture/test_capture_controller.py
git mv tests/test_capture_with_conditions.py tests/unit/capture/test_capture_with_conditions.py
git mv tests/test_cold_fill.py               tests/unit/capture/test_cold_fill.py
git mv tests/test_cold_fill_integration.py   tests/unit/capture/test_cold_fill_integration.py
git mv tests/test_draft_lifecycle.py         tests/unit/capture/test_draft_lifecycle.py
git mv tests/test_reference_seeding.py       tests/unit/capture/test_reference_seeding.py
```

- [ ] **Step 3: Purge stale bytecode**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
```

- [ ] **Step 4: Run collection and targeted suite**

```bash
python -m pytest --collect-only -q tests/unit/capture 2>&1 | tail -5
pytest tests/unit/capture -q
```
Expected: all capture tests collect and pass. If collection fails with `ModuleNotFoundError` or duplicate basename errors, create empty `tests/unit/__init__.py` and `tests/unit/capture/__init__.py`, then retry.

- [ ] **Step 5: Run full fast suite**

```bash
pytest -m "not (emulator or slow or frontend)"
```
Expected: same green as baseline, same collected count.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: move capture tests into tests/unit/capture/"
```

---

## Task 2: Move db tests

**Files:**
- Move: 4 files from `tests/` to `tests/unit/db/`

- [ ] **Step 1: Create and move**

```bash
mkdir -p tests/unit/db
git mv tests/test_db_attempts.py   tests/unit/db/test_db_attempts.py
git mv tests/test_db_dashboard.py  tests/unit/db/test_db_dashboard.py
git mv tests/test_db_references.py tests/unit/db/test_db_references.py
git mv tests/test_waypoints_db.py  tests/unit/db/test_waypoints_db.py
```

- [ ] **Step 2: Purge bytecode and verify**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest tests/unit/db -q
pytest -m "not (emulator or slow or frontend)"
```
Expected: all green, baseline count unchanged.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: move db tests into tests/unit/db/"
```

---

## Task 3: Move estimator tests

**Files:**
- Move: 5 files from `tests/` to `tests/unit/estimators/`

- [ ] **Step 1: Create and move**

```bash
mkdir -p tests/unit/estimators
git mv tests/test_kalman.py             tests/unit/estimators/test_kalman.py
git mv tests/test_exp_decay.py          tests/unit/estimators/test_exp_decay.py
git mv tests/test_rolling_mean.py       tests/unit/estimators/test_rolling_mean.py
git mv tests/test_estimator_params.py   tests/unit/estimators/test_estimator_params.py
git mv tests/test_estimator_sanity.py   tests/unit/estimators/test_estimator_sanity.py
```

- [ ] **Step 2: Purge bytecode and verify**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest tests/unit/estimators -q
pytest -m "not (emulator or slow or frontend)"
```
Expected: all green. These files use `from tests.factories import ...` — confirm those imports still resolve (they will, because `tests/` remains a package).

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: move estimator tests into tests/unit/estimators/"
```

---

## Task 4: Move route tests

**Files:**
- Move: 3 files from `tests/` to `tests/unit/routes/`

- [ ] **Step 1: Create and move**

```bash
mkdir -p tests/unit/routes
git mv tests/test_attempts_route.py       tests/unit/routes/test_attempts_route.py
git mv tests/test_segments_route.py       tests/unit/routes/test_segments_route.py
git mv tests/test_dashboard_references.py tests/unit/routes/test_dashboard_references.py
```

- [ ] **Step 2: Purge bytecode and verify**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest tests/unit/routes -q
pytest -m "not (emulator or slow or frontend)"
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: move route tests into tests/unit/routes/"
```

---

## Task 5: Move allocator tests

**Files:**
- Move: 2 files from `tests/` to `tests/unit/allocators/`

- [ ] **Step 1: Create and move**

```bash
mkdir -p tests/unit/allocators
git mv tests/test_allocators.py    tests/unit/allocators/test_allocators.py
git mv tests/test_mix_allocator.py tests/unit/allocators/test_mix_allocator.py
```

- [ ] **Step 2: Purge bytecode and verify**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest tests/unit/allocators -q
pytest -m "not (emulator or slow or frontend)"
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: move allocator tests into tests/unit/allocators/"
```

---

## Task 6: Move cli tests

**Files:**
- Move: 3 files from `tests/` to `tests/unit/cli/`

- [ ] **Step 1: Create and move**

```bash
mkdir -p tests/unit/cli
git mv tests/test_cli.py          tests/unit/cli/test_cli.py
git mv tests/test_cli_db_reset.py tests/unit/cli/test_cli_db_reset.py
git mv tests/test_cli_logging.py  tests/unit/cli/test_cli_logging.py
```

- [ ] **Step 2: Purge bytecode and verify**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest tests/unit/cli -q
pytest -m "not (emulator or slow or frontend)"
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: move cli tests into tests/unit/cli/"
```

---

## Task 7: Move remaining flat files to `tests/unit/`

**Files:**
- Move: all remaining `tests/test_*.py` files to `tests/unit/test_*.py`

These are single-module-source tests with no subpackage to mirror.

- [ ] **Step 1: List what's left at `tests/` root**

```bash
ls tests/test_*.py
```
Expected: the ~32 files listed in the "Remaining files" table at the top of this plan.

- [ ] **Step 2: Move them all**

```bash
git mv tests/test_attempts_conditions.py         tests/unit/test_attempts_conditions.py
git mv tests/test_attempts_invalidation.py       tests/unit/test_attempts_invalidation.py
git mv tests/test_condition_registry.py          tests/unit/test_condition_registry.py
git mv tests/test_condition_registry_startup.py  tests/unit/test_condition_registry_startup.py
git mv tests/test_config.py                      tests/unit/test_config.py
git mv tests/test_dashboard_integration.py       tests/unit/test_dashboard_integration.py
git mv tests/test_fake_tcp.py                    tests/unit/test_fake_tcp.py
git mv tests/test_invalidate_flow.py             tests/unit/test_invalidate_flow.py
git mv tests/test_model_output.py                tests/unit/test_model_output.py
git mv tests/test_models.py                      tests/unit/test_models.py
git mv tests/test_models_enums.py                tests/unit/test_models_enums.py
git mv tests/test_multi_game.py                  tests/unit/test_multi_game.py
git mv tests/test_practice.py                    tests/unit/test_practice.py
git mv tests/test_practice_coverage.py           tests/unit/test_practice_coverage.py
git mv tests/test_protocol.py                    tests/unit/test_protocol.py
git mv tests/test_replay.py                      tests/unit/test_replay.py
git mv tests/test_reset_logging.py               tests/unit/test_reset_logging.py
git mv tests/test_romid.py                       tests/unit/test_romid.py
git mv tests/test_scheduler_fallback.py          tests/unit/test_scheduler_fallback.py
git mv tests/test_scheduler_kalman.py            tests/unit/test_scheduler_kalman.py
git mv tests/test_segment_variants.py            tests/unit/test_segment_variants.py
git mv tests/test_segment_with_model.py          tests/unit/test_segment_with_model.py
git mv tests/test_segments_is_primary.py         tests/unit/test_segments_is_primary.py
git mv tests/test_session_manager.py             tests/unit/test_session_manager.py
git mv tests/test_session_manager_conditions.py  tests/unit/test_session_manager_conditions.py
git mv tests/test_speed_run_mode.py              tests/unit/test_speed_run_mode.py
git mv tests/test_spinrec.py                     tests/unit/test_spinrec.py
git mv tests/test_sse.py                         tests/unit/test_sse.py
git mv tests/test_state_builder.py               tests/unit/test_state_builder.py
git mv tests/test_system_state.py                tests/unit/test_system_state.py
git mv tests/test_tcp_manager.py                 tests/unit/test_tcp_manager.py
git mv tests/test_vite.py                        tests/unit/test_vite.py
```

- [ ] **Step 3: Verify `tests/` root has only the expected survivors**

```bash
ls tests/
```
Expected entries: `__init__.py` (if present originally), `conftest.py`, `factories.py`, `fixtures/`, `integration/`, `playwright/`, `unit/`. No `test_*.py` files remaining.

- [ ] **Step 4: Purge bytecode and run the full fast suite**

```bash
find tests -name __pycache__ -type d -exec rm -rf {} +
pytest -m "not (emulator or slow or frontend)"
```
Expected: all green, same collected count as Task 0 baseline.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: move remaining unit tests into tests/unit/"
```

---

## Task 8: Update docs & final verification

**Files:**
- Modify: `CLAUDE.md` (Testing section)

- [ ] **Step 1: Check CLAUDE.md for stale test paths**

Only these references exist (verified via grep):
- Line 27: `tests/integration/test_smoke.py` — unchanged, integration stays put.
- Line 45: `tests/integration/addresses.py` — unchanged.

No edits required unless the Testing section mentions a moved path. Re-grep to confirm:

```bash
grep -n "tests/test_" CLAUDE.md
```
Expected: no matches. If any appear, update to `tests/unit/...` or `tests/unit/<subdir>/...` per the mapping table above.

- [ ] **Step 2: Check for other stale references in docs/scripts**

```bash
grep -rn "tests/test_" scripts/ docs/superpowers/plans/ 2>/dev/null || true
```
Plans and specs are historical — do NOT rewrite them. `scripts/dedicated_coverage.py` is the only live file that matched earlier; read it and update only if it passes a moved path to pytest.

- [ ] **Step 3: If `scripts/dedicated_coverage.py` needs updating**

Inspect the file and update any literal `tests/test_*.py` paths to the new location per the mapping table. If it only iterates with globs (e.g. `tests/**/test_*.py`) it needs no change.

- [ ] **Step 4: Run the full test suite (not just fast)**

```bash
python -m pytest
```
Expected: unit + slow + emulator + frontend all green, total count matches Task 0 baseline. Per the "Merging Branches" rule in CLAUDE.md, this must pass before the plan is considered complete.

- [ ] **Step 5: Frontend sanity (unchanged by this plan, but required by the verification contract)**

```bash
cd frontend && npm run build && npm test && npm run typecheck && cd ..
```
Expected: all green.

- [ ] **Step 6: Commit any doc/script edits**

If step 1 or 3 produced edits:

```bash
git add CLAUDE.md scripts/dedicated_coverage.py
git commit -m "docs: update test paths for tests/unit/ reorg"
```

If neither was touched, skip this step — no empty commit.

---

## Rollback

Each task is a single commit. If a move breaks collection or a test, `git reset --hard HEAD~1` reverts that task cleanly. Investigate, fix the mapping, retry. Don't bandage with edits to test content — this plan is move-only.

## Out of scope (future plans)

- Pruning oversized test files (section 8 second pass — Phase 7 of the cleanup pass).
- Capture package rename (Phase 2 — will further slot tests under `tests/unit/capture/` once source renames land).
- Integration test reshaping (already mirrors target).
