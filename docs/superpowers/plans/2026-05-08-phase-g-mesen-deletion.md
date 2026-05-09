# Phase G — Delete Mesen + Lua Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Mesen-Lua backend from SpinLab entirely. After Phase G, only the RetroArch backend exists; `lua/` is gone; `tcp_manager.py` and `spinrec.py` are gone; the dashboard, capture flow, tests, and docs all assume RA.

**Architecture:** A 7-commit deletion pass over the SpinLab codebase, each commit independently keeping the full pytest suite green. Order is "test references → unused code paths → production files → schema → docs" so that no commit ever leaves a dangling import or broken test.

**Tech Stack:** Python 3.11+ (codebase), SQLite (db schema), pytest (test suite). No external libs added or removed.

**Spec:** [`docs/superpowers/specs/2026-05-08-phase-g-mesen-deletion-design.md`](../specs/2026-05-08-phase-g-mesen-deletion-design.md)

**Definition of done:**
- All 7 tasks committed with passing test suite at each step
- `git grep -i "TcpManager\|spinrec\|mesen-lua"` returns no production-code matches
- `lua/` directory does not exist
- `EmulatorConfig` has no Mesen fields and no `backend` field
- `capture_sessions` table has no `spinrec_path` column
- README's "Migration in progress" warning is gone
- `python -m pytest` passes (xfails from Phase E remain xfailed)

**Breaking change for users:** Phase G drops the `spinrec_path` DB column. Existing user databases require `spinlab db reset` to upgrade. No migration script — Andrew confirmed accumulated data is test-only and discardable.

---

## File structure — what changes

**Files removed entirely (8):**
- `lua/addresses.lua`, `lua/json.lua`, `lua/overlay.lua`, `lua/poke_engine.lua`, `lua/spinlab.lua`, `lua/spinrec.lua` — the entire Lua scripts directory
- `python/spinlab/tcp_manager.py` — Mesen TCP backend
- `python/spinlab/spinrec.py` — `.spinrec` format reader/writer

**Test files removed (5):**
- `tests/unit/test_tcp_manager.py`
- `tests/unit/test_spinrec.py`
- `tests/unit/test_dashboard_backend_select.py`
- `tests/integration/test_lua_conditions.py`
- `tests/integration/test_smoke.py`

**Production files modified (significant changes):**
- `python/spinlab/config.py` — drop `path`, `lua_script`, `script_data_dir`, `backend` fields and parsing
- `python/spinlab/dashboard.py` — drop the `if backend == "retroarch"` branch + `else: TcpManager(...)`
- `python/spinlab/routes/system.py` — drop Mesen launch path from `launch_emulator`
- `python/spinlab/protocol.py` — remove `RecSavedEvent` and dispatch entry
- `python/spinlab/session_manager.py` — remove `_handle_rec_saved` + `RecSavedEvent` import
- `python/spinlab/capture/reference.py` — drop spinrec path construction (`_new_session_spinrec_path`, `_create_new_session` returns sess_id only), drop `handle_rec_saved`
- `python/spinlab/routes/reference.py` — drop `_resolve_spinrec_path`, replace with `.replay` path resolution; simplify `has_spinrec`/`has_replay` to just `has_replay`
- `python/spinlab/db/core.py` — drop `spinrec_path` column from `capture_sessions` schema
- `python/spinlab/db/capture_sessions.py` — drop column from queries + dataclass
- `python/spinlab/db/capture_runs.py` — drop spinrec-file unlinking from `hard_delete_capture_run`
- `tests/conftest.py` — drop `FakeTcpManager`
- `tests/integration/conftest.py` — drop residual Mesen fixtures (most already removed in Task 12 of Phase E)
- Various smaller tests with Mesen branches (audited per task)

**Documentation updates:**
- `README.md` — remove Mesen requirement + "migration in progress" header; document `spinlab db reset` as required upgrade step from pre-Phase-G
- `docs/ARCHITECTURE.md` — describe RA-only architecture
- `docs/retroarch-migration/status.md` — mark migration done
- `docs/retroarch-migration/path-to-parity.md` — close P2.2
- `frontend/src/types.ts` — drop `has_spinrec` field on Reference
- `frontend/src/manage.ts` — simplify Replay button condition (already uses `has_replay`; drop the `||` with `has_spinrec`)
- `CLAUDE.md` — remove Mesen-specific testing guidance

---

## Pre-flight

- [ ] **Read the spec.** Open [`docs/superpowers/specs/2026-05-08-phase-g-mesen-deletion-design.md`](../specs/2026-05-08-phase-g-mesen-deletion-design.md) and read the "Decisions" section. The plan below implements those decisions verbatim.

- [ ] **Run the full test suite to establish a green baseline.**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest --tb=no -q 2>&1 | tail -3
```

Expected: `944 passed, 13 skipped, 1 xfailed` (numbers may shift slightly with later commits but the suite must be all-green or all-passing-except-xfails). If anything fails before you start, fix it first per `feedback_fix_preexisting_failures.md`.

---

## Task 1: Test infrastructure cleanup

**Files:**
- Delete: `tests/unit/test_tcp_manager.py`
- Delete: `tests/unit/test_spinrec.py`
- Delete: `tests/unit/test_dashboard_backend_select.py`
- Delete: `tests/integration/test_lua_conditions.py`
- Delete: `tests/integration/test_smoke.py`
- Modify: `tests/conftest.py` (drop `FakeTcpManager`)
- Modify: `tests/integration/conftest.py` (audit for residual Mesen fixtures; some already removed in Task 12 of Phase E)

The Mesen-only test files block deletion of their dependencies (`tcp_manager.py`, `spinrec.py`, `lua/`). Removing them first lets later tasks delete the production files without orphaning any test.

- [ ] **Step 1.1: Confirm the 5 test files exist and are Mesen-only.**

```bash
ls -1 tests/unit/test_tcp_manager.py tests/unit/test_spinrec.py tests/unit/test_dashboard_backend_select.py tests/integration/test_lua_conditions.py tests/integration/test_smoke.py
```

Expected: all 5 paths print without errors.

- [ ] **Step 1.2: Delete the 5 test files.**

```bash
git rm tests/unit/test_tcp_manager.py tests/unit/test_spinrec.py tests/unit/test_dashboard_backend_select.py tests/integration/test_lua_conditions.py tests/integration/test_smoke.py
```

- [ ] **Step 1.3: Find and remove `FakeTcpManager`.**

```bash
grep -n "FakeTcpManager" tests/conftest.py
```

Expected: lines defining `FakeTcpManager` (probably as a class) and any uses. Edit `tests/conftest.py` to remove the class definition and any imports/fixtures that depend on it. The `fake_dashboard_server` fixture in `tests/integration/conftest.py` uses `FakeTcpManager` — if that fixture is still used by remaining tests, replace with a fake implementing the RA-shape `EmuBackend` Protocol; if not, delete the fixture.

Search for usage to decide:
```bash
grep -rn "FakeTcpManager\|fake_dashboard_server" tests --include="*.py"
```

- [ ] **Step 1.4: Audit `tests/integration/conftest.py` for residual Mesen-side fixtures.**

```bash
grep -n "mesen_process\|tcp_client\|smoke_mesen\|MESEN_PATH\|--testrunner" tests/integration/conftest.py
```

Most were removed in Phase E Task 12. Any remaining lines (fixtures, helpers, env-var probes) get deleted. If a deleted fixture is referenced by a test that is still present, that test will fail in Step 1.5; either delete the test or fix it to use the RA harness.

- [ ] **Step 1.5: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes minus the deleted tests. Test count drops by however many tests the 5 files contained (probably 30-50). If any failure references the deleted code, it's a missed reference — find with `grep`, fix, re-run.

- [ ] **Step 1.6: Commit.**

```bash
git add tests/
git commit -m "$(cat <<'EOF'
chore(tests): delete Mesen-only test files (Phase G step 1/7)

Drops test_tcp_manager.py, test_spinrec.py,
test_dashboard_backend_select.py, test_lua_conditions.py, test_smoke.py
plus FakeTcpManager + residual Mesen fixtures from conftest.

These tests' production dependencies (TcpManager, spinrec.py, lua/)
get deleted in subsequent commits. Removing the tests first means
each subsequent deletion can run a green test suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Delete `lua/` directory

**Files:**
- Delete: `lua/addresses.lua`, `lua/json.lua`, `lua/overlay.lua`, `lua/poke_engine.lua`, `lua/spinlab.lua`, `lua/spinrec.lua`

Trivial. Task 1 removed all test references to lua/ files.

- [ ] **Step 2.1: Confirm no production code references lua/ paths.**

```bash
grep -rn "lua/\|LUA_DIR\|spinlab\.lua\|poke_engine\.lua" python tests --include="*.py" 2>&1 | grep -v "Binary file" | head -20
```

Expected: 0 hits in production code; possibly some hits in test fixtures that were missed in Task 1. Investigate any hits before proceeding.

- [ ] **Step 2.2: Delete the directory.**

```bash
git rm -r lua/
```

- [ ] **Step 2.3: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes. Same test count as after Task 1.

- [ ] **Step 2.4: Commit.**

```bash
git add lua/
git commit -m "$(cat <<'EOF'
chore(lua): delete lua/ directory (Phase G step 2/7)

Removes addresses.lua, json.lua, overlay.lua, poke_engine.lua,
spinlab.lua, spinrec.lua. The Mesen-Lua backend is gone in Phase G;
none of these scripts are loaded by anything anymore.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Drop Mesen branches from dashboard, system route, and config

**Files:**
- Modify: `python/spinlab/dashboard.py` — drop the `if backend == "retroarch"` branch; orchestrator construction becomes unconditional
- Modify: `python/spinlab/routes/system.py` — drop the Mesen-launch path in `launch_emulator`; rename `_launch_retroarch` → `_launch_emulator`
- Modify: `python/spinlab/config.py` — drop `path`, `lua_script`, `script_data_dir`, `backend` fields from `EmulatorConfig` and `from_yaml`

This commit removes Mesen-aware code paths from the entry points (dashboard wiring + emulator launch + config parsing). After this commit, `tcp_manager.py` is no longer imported anywhere in production code.

- [ ] **Step 3.1: Read current state to make targeted edits.**

```bash
grep -n "backend\|TcpManager" python/spinlab/dashboard.py python/spinlab/routes/system.py python/spinlab/config.py
```

You should see:
- `dashboard.py`: line ~59 has `if config.emulator.backend == "retroarch":` branch with `else: TcpManager(...)` fallback
- `routes/system.py`: line ~156 `if config.emulator.backend == "retroarch":` calling `_launch_retroarch`; lines ~158-192 have the Mesen launch path
- `config.py`: lines ~20-30 have backend + Mesen fields; lines ~63-100 parse them

- [ ] **Step 3.2: Simplify `dashboard.py` to always use `build_orchestrator`.**

Replace the if/else block (lines ~59-63) with the always-RA path:

```python
# Before:
if config.emulator.backend == "retroarch":
    from spinlab.retroarch.orchestrator import build_orchestrator
    tcp: EmuBackend = build_orchestrator(config)
else:
    tcp = TcpManager(config.network.host, config.network.port)

# After:
from spinlab.retroarch.orchestrator import build_orchestrator
tcp: EmuBackend = build_orchestrator(config)
```

Also remove the `from .tcp_manager import TcpManager` import at the top of `dashboard.py` if present.

- [ ] **Step 3.3: Simplify `routes/system.py`'s `launch_emulator`.**

Inline `_launch_retroarch`'s body into `launch_emulator` (or rename `_launch_retroarch` → `_launch_emulator` and have `launch_emulator` always delegate to it). Drop the Mesen launch path entirely. After this step:
- `launch_emulator(body, config)` always launches RA via the existing RA path
- All references to `config.emulator.path`, `config.emulator.lua_script`, `config.emulator.script_data_dir`, and `breadcrumb`/`lua_dir.txt` are gone from this file.

Read the existing `_launch_retroarch` to confirm its signature works as-is. The simplification is mechanical: keep the RA half, delete the rest.

- [ ] **Step 3.4: Simplify `config.py`.**

In `EmulatorConfig` dataclass, drop these fields:
- `backend: str = "mesen-lua"`
- `path: Path | None = None`
- `lua_script: Path | None = None`
- `script_data_dir: Path | None = None`

Keep:
- `retroarch_path: Path | None = None`
- `ra_core_path: Path | None = None`
- `savestate_dir: Path | None = None`
- `spinlab_state_dir: Path | None = None`
- `ra_game_basename: str | None = None`
- `ra_movie_dir: Path | None = None`
- `ra_core_subdir: str | None = None`

In `from_yaml`, remove the `backend` parsing (keys `emu.get("backend")`, `emu.get("path")`, `emu.get("lua_script")`, `emu.get("script_data_dir")`) and the `if backend not in ("mesen-lua", "retroarch"): raise` validation. Drop those fields from the `EmulatorConfig(...)` constructor call.

User config files with `backend: "retroarch"` or `path: ...` etc. will now silently ignore those keys (Python's `.get()` doesn't reject unknown keys). That's the intended UX.

- [ ] **Step 3.5: Find any remaining references to the removed fields.**

```bash
grep -rn "config\.emulator\.backend\|config\.emulator\.path\|config\.emulator\.lua_script\|config\.emulator\.script_data_dir\|emulator\.backend == " python tests --include="*.py" 2>&1 | head -20
```

Expected: 0 hits, or only hits in tests that need updating. Fix each.

- [ ] **Step 3.6: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes. The dashboard module loads without TcpManager. Tests that previously asserted backend selection are gone (Task 1).

- [ ] **Step 3.7: Commit.**

```bash
git add python/spinlab/dashboard.py python/spinlab/routes/system.py python/spinlab/config.py
git commit -m "$(cat <<'EOF'
refactor(config): drop backend selection + Mesen-only fields (Phase G step 3/7)

EmulatorConfig: drop `backend`, `path`, `lua_script`, `script_data_dir`.
Only RA fields remain. Existing user config files with these keys
will silently ignore them (no breaking error).

dashboard.py: always builds the RA orchestrator. The `else: TcpManager`
fallback is gone.

routes/system.py: launch_emulator always runs the RA launch path.

After this commit, TcpManager is no longer imported anywhere in
production code.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Simplify reference flow + drop spinrec-related events

**Files:**
- Modify: `python/spinlab/protocol.py` — remove `RecSavedEvent` dataclass + dispatch entry
- Modify: `python/spinlab/session_manager.py` — remove `_handle_rec_saved` + `RecSavedEvent` import + dispatch entry
- Modify: `python/spinlab/capture/reference.py` — drop `_new_session_spinrec_path`, change `_create_new_session` to return only `sess_id`, drop `handle_rec_saved`, drop `path=spinrec_path` from `ReferenceStartCmd`/`ReplayCmd`, simplify `start_replay`
- Modify: `python/spinlab/routes/reference.py` — replace `_resolve_spinrec_path` with `_resolve_replay_path` returning `<data>/<game>/rec/<ref_id>.replay`; drop `has_spinrec` from `list_references` response
- Modify: `frontend/src/types.ts` — drop `has_spinrec: boolean` from `Reference`
- Modify: `frontend/src/manage.ts` — replace `selectedRef?.has_spinrec || selectedRef?.has_replay` with just `selectedRef?.has_replay`

This commit removes spinrec-format from the runtime data flow. The DB column still exists (dropped in Task 5); the `RecSavedEvent` and `_resolve_spinrec_path` infra both go.

- [ ] **Step 4.1: Drop `RecSavedEvent` from `protocol.py`.**

In `python/spinlab/protocol.py`:
- Delete the `@dataclass class RecSavedEvent: ...` block (around line 89-93)
- Delete the `"rec_saved": RecSavedEvent,` entry in the discriminator dict (around line 238)

```bash
grep -n "RecSavedEvent\|rec_saved" python/spinlab/protocol.py
```

Expected after the edit: 0 hits.

- [ ] **Step 4.2: Drop `RecSavedEvent` handler from `session_manager.py`.**

In `python/spinlab/session_manager.py`:
- Remove `RecSavedEvent` from the `from spinlab.protocol import (...)` block (around line 29)
- Remove the dispatch entry `RecSavedEvent: self._handle_rec_saved,` (around line 106)
- Delete the `async def _handle_rec_saved(self, event: RecSavedEvent) -> None:` method body (around line 319-320)

- [ ] **Step 4.3: Drop spinrec construction from `capture/reference.py`.**

In `python/spinlab/capture/reference.py`:
- Remove the `RecSavedEvent` import (around line 49)
- Delete the `_new_session_spinrec_path` method (around line 202-206)
- In `_create_new_session` (around line 249), drop the `spinrec_path` parameter and computation. Update the `db.create_capture_session(...)` call to not pass `spinrec_path` (Task 5 handles the DB side; for now leave the column passing as `""` empty string if needed to keep tests green; the Task 5 schema change makes it actually go away).

Actually since Task 5 drops the column, this commit must coordinate. Two options:
  (a) This commit passes `spinrec_path=""` to `db.create_capture_session` to keep the NOT NULL constraint satisfied; Task 5 drops both.
  (b) This commit + Task 5 are done in one combined commit.

Pick (a) to keep commits focused: change here is "stop computing spinrec_path; pass empty string to DB". Task 5 then drops the column entirely.

Concrete edit at `_create_new_session`:

```python
# Before:
def _create_new_session(self, run_id, data_dir, game_id):
    """Create a new capture_session row + spinrec path. Returns (session_id, spinrec_path)."""
    next_ord = self.db.next_session_ordinal(run_id)
    sess_id = f"sess_{uuid.uuid4().hex[:8]}"
    spinrec_path = self._new_session_spinrec_path(data_dir, game_id, run_id, next_ord)
    self.db.create_capture_session(
        session_id=sess_id, capture_run_id=run_id,
        ordinal=next_ord, spinrec_path=spinrec_path,
    )
    self.recorder.current_capture_session_id = sess_id
    self.recorder.current_session_ordinal = next_ord
    return sess_id, spinrec_path

# After:
def _create_new_session(self, run_id, data_dir, game_id):
    """Create a new capture_session row. Returns the session id."""
    next_ord = self.db.next_session_ordinal(run_id)
    sess_id = f"sess_{uuid.uuid4().hex[:8]}"
    self.db.create_capture_session(
        session_id=sess_id, capture_run_id=run_id,
        ordinal=next_ord, spinrec_path="",  # column drops in Task 5
    )
    self.recorder.current_capture_session_id = sess_id
    self.recorder.current_session_ordinal = next_ord
    return sess_id
```

- [ ] **Step 4.4: Update callers of `_create_new_session` in `capture/reference.py`.**

Find the two `start_reference` and `resume_reference` methods (around lines 277, 298) that call `_create_new_session` expecting `(sess_id, spinrec_path)`. Update both to expect only `sess_id`. The `await self.tcp.send_command(ReferenceStartCmd(path=spinrec_path))` calls become `await self.tcp.send_command(ReferenceStartCmd(path=""))` — the orchestrator's `_on_reference_start` handler only uses `cmd.path` to derive the `.replay` filename via `with_suffix(".replay")`, so passing empty string would break that.

Replace the path the orchestrator uses. In `_on_reference_start`, the current code does:
```python
movie_path = Path(cmd.path).with_suffix(".replay")
```

The orchestrator could instead derive the path from the recorder's known data_dir + game_id + ref_id. Refactor: pass the `.replay` path explicitly in `ReferenceStartCmd.path`. In `start_reference`/`resume_reference`:

```python
# Compute the replay path directly:
replay_path = self._game_rec_dir(data_dir, game_id) / f"{run_id}__sess{ordinal:03d}.replay"
await self.tcp.send_command(ReferenceStartCmd(path=str(replay_path)))
```

And in `_on_reference_start`:
```python
movie_path = Path(cmd.path)  # already a .replay path; no suffix translation
```

Same simplification in `start_replay` for `ReplayCmd`.

- [ ] **Step 4.5: Drop `handle_rec_saved` from `capture/reference.py`.**

Delete the `def handle_rec_saved(self, event: RecSavedEvent) -> None:` method (around line 632-633).

- [ ] **Step 4.6: Update `routes/reference.py`.**

Replace `_resolve_spinrec_path` with `_resolve_replay_path`:

```python
def _resolve_replay_path(
    ref_id: str,
    session: SessionManager,
    db: Database,
    *,
    session_id: str | None = None,
) -> str | None:
    """Return the .replay path for a reference run. Returns None if no
    replay file exists for the requested ref/session."""
    gid = session.game_id or "unknown"
    sessions = db.list_capture_sessions_for_run(ref_id)
    if sessions:
        if session_id is not None:
            target = next((s for s in sessions if s["id"] == session_id), None)
            if target is None:
                return None
            ordinal = target["ordinal"]
        else:
            ordinal = sessions[0]["ordinal"]
        path = session.data_dir / gid / "rec" / f"{ref_id}__sess{ordinal:03d}.replay"
    else:
        path = session.data_dir / gid / "rec" / f"{ref_id}.replay"
    return str(path) if path.is_file() else None
```

In `replay_start`, replace `spinrec_path = _resolve_spinrec_path(...)` with `replay_path = _resolve_replay_path(...)` and update the error detail string from `"spinrec_not_found"` to `"replay_not_found"`. Pass `replay_path` to `session.start_replay(...)`.

In `list_references`, drop the `has_spinrec` field calculation and the `legacy_spinrec` path computation. Keep only `has_replay`. Result:

```python
out: list[dict] = []
for ref in refs:
    d: dict = dict(ref)
    ref_id = d["id"]
    sessions = db.list_capture_sessions_for_run(ref_id)
    if sessions:
        d["has_replay"] = any(
            (session.data_dir / gid / "rec" / f"{ref_id}__sess{s['ordinal']:03d}.replay").is_file()
            for s in sessions
        )
    else:
        legacy_replay = session.data_dir / gid / "rec" / f"{ref_id}.replay"
        d["has_replay"] = legacy_replay.is_file()
    out.append(d)
```

- [ ] **Step 4.7: Update frontend types and manage.ts.**

In `frontend/src/types.ts`, drop the `has_spinrec: boolean;` line from the `Reference` interface (around line 214).

In `frontend/src/manage.ts`, simplify the `hasReplayable` calculation:

```typescript
// Before:
const hasReplayable =
  !!(selectedRef?.has_spinrec || selectedRef?.has_replay);

// After:
const hasReplayable = !!selectedRef?.has_replay;
```

Drop the comment line above about "Either a Mesen-era .spinrec or an RA-era .replay" since it's now stale.

- [ ] **Step 4.8: Build the frontend.**

```bash
cd frontend && npm run build && cd ..
```

Expected: builds clean.

- [ ] **Step 4.9: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes. Capture/reference tests may need adjustment if they assert on `spinrec_path` in returned tuples. Fix each.

- [ ] **Step 4.10: Commit.**

```bash
git add python/spinlab/protocol.py python/spinlab/session_manager.py python/spinlab/capture/reference.py python/spinlab/routes/reference.py python/spinlab/retroarch/orchestrator.py frontend/src/types.ts frontend/src/manage.ts python/spinlab/static/
git commit -m "$(cat <<'EOF'
refactor(reference): drop spinrec from runtime data flow (Phase G step 4/7)

Removes the spinrec format from active code paths:

- protocol.py: drop RecSavedEvent dataclass + dispatch
- session_manager.py: drop _handle_rec_saved
- capture/reference.py: drop _new_session_spinrec_path,
  ReferenceStartCmd/ReplayCmd now carry .replay paths directly
- retroarch/orchestrator.py: _on_reference_start no longer translates
  .spinrec → .replay (path is already .replay)
- routes/reference.py: _resolve_spinrec_path → _resolve_replay_path
- has_spinrec field dropped from Reference type / API response

DB still has the spinrec_path column (NOT NULL); empty string passed
through until Task 5 drops it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Drop `spinrec_path` column from DB schema

**Files:**
- Modify: `python/spinlab/db/core.py` — drop `spinrec_path TEXT NOT NULL` from `capture_sessions` CREATE TABLE
- Modify: `python/spinlab/db/capture_sessions.py` — drop column from queries + `CaptureSessionRow` dataclass
- Modify: `python/spinlab/db/capture_runs.py` — drop the spinrec-file unlinking from `hard_delete_capture_run`
- Modify: `python/spinlab/capture/reference.py` — drop the `spinrec_path=""` placeholder from `_create_new_session`
- Update: `README.md` — note that pre-Phase-G databases need `spinlab db reset` to upgrade

Breaking change for users with existing databases. Per Decision 2, no migration script.

- [ ] **Step 5.1: Drop the column from the schema.**

In `python/spinlab/db/core.py` (around line 117-126), the `capture_sessions` CREATE TABLE currently reads:

```sql
CREATE TABLE IF NOT EXISTS capture_sessions (
  id TEXT PRIMARY KEY,
  capture_run_id TEXT NOT NULL REFERENCES capture_runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  spinrec_path TEXT NOT NULL,
  end_reason TEXT,
  UNIQUE (capture_run_id, ordinal)
);
```

Remove the `spinrec_path TEXT NOT NULL,` line.

- [ ] **Step 5.2: Update `db/capture_sessions.py`.**

In the `CaptureSessionRow` (or similar) dataclass, drop `spinrec_path: str`. In the `create_capture_session` function:
- Drop the `spinrec_path` parameter
- Drop `spinrec_path` from the INSERT column list and VALUES tuple

In any SELECT functions, drop the `s.spinrec_path` from the column list and from the row-to-dict mapping.

```bash
grep -n "spinrec_path" python/spinlab/db/capture_sessions.py
```

Expected after edits: 0 hits.

**Plus update tests that pass `spinrec_path` to `create_capture_session` or `make_capture_session`.** Run:

```bash
grep -rn "spinrec_path" tests --include="*.py"
```

Drop `spinrec_path` arguments from the following call sites:
- `tests/factories.py:121-125` — drop the `spinrec_path` parameter from `make_capture_session`'s signature; drop the `path = spinrec_path or f"/tmp/{sid}.spinrec"` computation; pass only the 3 remaining args to `db.create_capture_session`
- `tests/integration/test_crash_recovery.py:88, 101` — drop the 4th positional arg
- `tests/unit/capture/test_multi_session.py:270, 302, 303, 323, 324, 355, 414` — drop the 4th positional arg
- `tests/unit/db/test_db_capture_sessions.py:19` — drop the `spinrec_path=...` kwarg
- `tests/unit/db/test_db_references.py:85` — drop the `spinrec_path=...` kwarg
- `tests/unit/routes/test_dashboard_references.py:206, 252` — drop the `spinrec_path=...` kwarg
- `tests/integration/test_replay_fixture.py:162, 167` — these are comments referring to `_resolve_spinrec_path`; update text to mention `_resolve_replay_path`

Any test that asserts on a returned row's `spinrec_path` value also needs the assertion dropped. Quick scan:

```bash
grep -rn '\["spinrec_path"\]\|\.spinrec_path' tests --include="*.py"
```

- [ ] **Step 5.3: Update `db/capture_runs.py` to drop file-unlinking.**

`hard_delete_capture_run` (around line 114) currently:
1. Selects `spinrec_path`s for all sessions
2. Deletes DB rows
3. Unlinks the spinrec files from disk

Drop steps 1 and 3 entirely. The function becomes a pure DB delete.

```bash
grep -n "spinrec" python/spinlab/db/capture_runs.py
```

Expected after edits: 0 hits.

- [ ] **Step 5.4: Drop the `spinrec_path=""` placeholder in `capture/reference.py`.**

In `_create_new_session` (added in Task 4 step 4.3), drop the `spinrec_path=""` argument from the `db.create_capture_session(...)` call.

- [ ] **Step 5.5: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes. Tests that built test DBs will work because `Database.__init__` runs `CREATE TABLE IF NOT EXISTS` against fresh tmp paths — they get the new schema. **Existing user databases will fail** with `no such column: spinrec_path` until reset; that's the intended breaking change.

- [ ] **Step 5.6: Update README to note the breaking change.**

In `README.md`, near the top of the setup section, add a brief note:

```markdown
### Upgrading from pre-Phase-G

Phase G dropped the Mesen backend and the `spinrec_path` column from
the `capture_sessions` table. If you have an existing SpinLab database
from before this change, run:

```bash
spinlab db reset --config config.yaml
```

This deletes and recreates the database with the new schema. Saved
practice attempts will be lost; recapture as needed.
```

This is provisional — the broader README update happens in Task 7. The note here is essential because Task 5 IS the breaking commit; users running this commit's code against an old DB will see a hard error.

- [ ] **Step 5.7: Commit.**

```bash
git add python/spinlab/db/core.py python/spinlab/db/capture_sessions.py python/spinlab/db/capture_runs.py python/spinlab/capture/reference.py README.md
git commit -m "$(cat <<'EOF'
refactor(db): drop spinrec_path column from capture_sessions (Phase G step 5/7)

Per spec Decision 2, no migration. Existing databases require
spinlab db reset to upgrade.

Drops:
- spinrec_path column from capture_sessions CREATE TABLE
- spinrec_path from CaptureSessionRow dataclass
- spinrec_path from create_capture_session/list_capture_sessions queries
- spinrec-file unlinking from hard_delete_capture_run
- spinrec_path placeholder in _create_new_session

README documents the breaking change.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Delete `tcp_manager.py` and `spinrec.py`

**Files:**
- Delete: `python/spinlab/tcp_manager.py`
- Delete: `python/spinlab/spinrec.py`

After Tasks 1-5, no production code or test imports either file. This commit is the actual file removal.

- [ ] **Step 6.1: Confirm no remaining references.**

```bash
grep -rn "from spinlab.tcp_manager\|import.*tcp_manager\|from spinlab.spinrec\|import spinrec\|TcpManager\|SpinrecHeader\|read_spinrec\|write_spinrec" python tests --include="*.py" 2>&1 | grep -v "^Binary file"
```

Expected: 0 hits in code (the files themselves are about to be deleted, so don't count those).

If any hits remain, fix them before deleting (probably an import in a stray test or doc). Do NOT delete the files until this command returns clean.

- [ ] **Step 6.2: Delete the files.**

```bash
git rm python/spinlab/tcp_manager.py python/spinlab/spinrec.py
```

- [ ] **Step 6.3: Run the full pytest suite.**

```bash
python -m pytest --tb=short -q
```

Expected: passes.

- [ ] **Step 6.4: Run a final sanity grep on common Mesen markers.**

```bash
grep -rln "TcpManager\|spinrec\|--testrunner\|spinlab\.lua\|MESEN_PATH\|mesen-lua" python --include="*.py"
```

Expected: 0 hits in production code (allow comment-only references in `slot-management.md` and historical spec/plan docs).

- [ ] **Step 6.5: Commit.**

```bash
git add python/spinlab/
git commit -m "$(cat <<'EOF'
chore(retroarch): delete tcp_manager.py + spinrec.py (Phase G step 6/7)

The Mesen TCP backend and .spinrec format reader/writer have no
remaining callers after Tasks 1-5. Fully gone now.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Documentation update

**Files:**
- Modify: `README.md` — drop "Migration in progress" header, drop Mesen requirement, simplify config example
- Modify: `docs/ARCHITECTURE.md` — describe RA-only architecture
- Modify: `docs/retroarch-migration/status.md` — mark migration done
- Modify: `docs/retroarch-migration/path-to-parity.md` — close P2.2
- Modify: `CLAUDE.md` — remove Mesen-specific testing guidance

Final pass over user-facing docs. Doc-only commit.

- [ ] **Step 7.1: Update `README.md`.**

Remove the "Migration in progress" callout block at the top (around line 7). Remove Mesen from the Requirements list (the `[Mesen2](...)` line). Update the config.yaml example to drop Mesen fields.

In particular, drop these lines from README:
```
> **Migration in progress.** SpinLab is being ported from Mesen + Lua to RetroArch + snes9x_libretro on the `worktree/retroarch-port` branch. Most content below describes the current Mesen-based system; the RetroArch setup notes are at the end of this section. ...
```

and:
```
- [Mesen2](https://www.mesen.ca/) (has LuaSocket built in) — current
- [RetroArch](https://retroarch.com) + snes9x_libretro core (installed via RA's Online Updater) — post-migration
```

Replace with:
```
- [RetroArch](https://retroarch.com) + snes9x_libretro core (installed via RA's Online Updater)
```

The "RetroArch Setup" section becomes the only setup section. Drop "(post-migration)" from its heading.

- [ ] **Step 7.2: Update `docs/ARCHITECTURE.md`.**

If the doc still has Mesen-related architectural diagrams or per-backend explanations, replace with the RA-only architecture: NCI client, poller, transition detector, state I/O, orchestrator. Keep the doc focused on what is, not what was.

If `ARCHITECTURE.md` doesn't currently exist or is sparse, this step is a no-op or a short rewrite.

```bash
ls docs/ARCHITECTURE.md && head -50 docs/ARCHITECTURE.md
```

- [ ] **Step 7.3: Update `docs/retroarch-migration/status.md`.**

Add a "Migration complete" header at the top. Move the "What works" list to past tense ("the migration achieved..."). Mark the "Known broken / untested" items as either resolved, or moved to a separate post-migration follow-ups doc. Phase E xfails (poller starvation, replay-fixture test) stay listed as known follow-ups.

A possible structure:

```markdown
# RetroArch Migration — Complete (Phase G shipped 2026-05-XX)

The migration is done. RA is the only backend. SpinLab no longer ships
or supports Mesen-Lua. This doc is now historical.

## What worked
[summarize the major migration phases]

## Known follow-ups (not blockers)
- Poller starvation under uncapped movie playback (P0.3 — `test_poller_runs_during_playback` xfails)
- Replay → segment capture integration (depends on poller-throttle)
- 2nd-death practice reload bug (P0.2)
- ...
```

- [ ] **Step 7.4: Update `docs/retroarch-migration/path-to-parity.md`.**

Close P2.2 with a "Resolved <date>" annotation. Move all completed items into a "Done" section.

- [ ] **Step 7.5: Update `CLAUDE.md`.**

Remove any Mesen-specific testing markers, paths, or requirements. Simplify the `pytest -m emulator` description if relevant. Make sure CLAUDE.md describes the post-Phase-G state.

Specifically:
- Drop any references to Mesen2 binary path or MESEN_PATH env var
- Simplify "Smoke tests" entry (it referenced Mesen-headless smoke; that test is gone)
- Update "Replay fixture tests" entry — point at the new RA-recorded `.replay` fixture path

- [ ] **Step 7.6: Run the full pytest suite (no test changes here, but sanity).**

```bash
python -m pytest --tb=no -q
```

Expected: passes.

- [ ] **Step 7.7: Commit.**

```bash
git add README.md docs/ CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: post-Phase-G updates (Phase G step 7/7)

README: drop "migration in progress" header, drop Mesen from
requirements, drop Mesen-specific config example. RA setup section
becomes the only setup section.

ARCHITECTURE.md: describe RA-only architecture.

retroarch-migration/status.md: mark migration complete; remaining
items are post-migration follow-ups.

retroarch-migration/path-to-parity.md: close P2.2.

CLAUDE.md: drop Mesen-specific testing guidance.

Phase G is complete: SpinLab is RA-only.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Run the entire test suite.**

```bash
python -m pytest --tb=no -q
```

Expected: all green except the two known xfails from Phase E (`test_poller_runs_during_playback`, `test_replay_fixture::test_replay_produces_segments`).

- [ ] **Verify Phase G acceptance criteria.**

```bash
# No Mesen references in production code (history fine; doc references in
# slot-management.md / retroarch-migration/* are intentional historical context):
grep -rn "TcpManager\|spinrec\|mesen-lua\|--testrunner" python tests frontend --include="*.py" --include="*.ts"

# Lua dir gone:
ls lua/  # Expected: cannot access

# Mesen fields gone from config:
grep -n "backend\|lua_script\|script_data_dir" python/spinlab/config.py  # Expected: no matches

# spinrec_path column gone:
grep -n "spinrec_path" python/spinlab/db/

# README is clean:
grep "Migration in progress\|Mesen2" README.md  # Expected: no matches
```

- [ ] **Stage check: confirm 7 commits committed.**

```bash
git log main..HEAD --oneline | head -10
```

Expected: at least 7 new commits matching the 7 task commit messages, plus the prior Phase E commits.

- [ ] **Hand off.**

Phase G is complete. SpinLab is RA-only. The two Phase E xfails remain as documented follow-ups. Andrew can start fresh work (replay throttle, dashboard polish, whatever's next) on a clean foundation.
