# Full-Stack Smoke Tests + Hardening

**Date**: 2026-04-06
**Status**: Draft
**Context**: Manual testing session uncovered 12 bugs that the existing 400+ test suite didn't catch. Root cause: tests exercise components in isolation (mocked TCP, TestClient, seeded DBs) but never the assembled system. The poke test infrastructure already launches headless Mesen with real TCP — we extend it to include the dashboard + DB.

## Goals

1. Full-stack integration tests that catch the class of bugs found today
2. File-based logging so dashboard errors are always inspectable
3. A `spinlab db reset` command for clean slate during development

## Non-Goals

- Reference run or practice loop testing (future spec, using `.spinrec` replay as gold standard)
- Frontend/Playwright tests (separate effort)
- CI support (requires Mesen installed; local-only for now)

---

## 1. Full-Stack Smoke Test Suite

### Infrastructure

Extends the existing `tests/integration/` poke test infrastructure. Same Mesen session-scoped launch, same TCP connection.

**New fixtures** in `tests/integration/conftest.py` (added to existing file):

- `dashboard_server` (session-scoped) — starts a real FastAPI app with:
  - Real `Database` in a temp directory
  - Real `AppConfig` pointing at the live TCP port
  - Uvicorn running in a background thread on a free port
  - Waits for the event loop to connect to Mesen's TCP server
  - Yields the base URL (e.g. `http://127.0.0.1:{port}`)
  - Tears down uvicorn + DB on session end

- `dashboard_url` (session-scoped) — convenience alias, just the URL string

- `api` (function-scoped) — a `requests.Session` pre-configured with the base URL, for clean test code:
  ```python
  def test_example(api):
      resp = api.get("/api/state")
      assert resp.json()["tcp_connected"] is True
  ```

**Dependency chain**:
```
mesen_process (launches Mesen headless with spinlab.lua)
  -> tcp_client (validates Lua TCP server is up, sends game_context)
  -> dashboard_server (starts FastAPI + DB, event loop connects to same TCP)
```

Note: The `tcp_client` fixture currently consumes the `rom_info` event and sends `game_context` back. For the smoke tests, the dashboard's event loop needs to handle `rom_info` instead. The fixture ordering needs adjustment — `dashboard_server` should start before consuming `rom_info`, so the dashboard's event loop handles it naturally. `tcp_client` remains available for tests that need to send raw TCP messages (like poke scenarios).

**New test file**: `tests/integration/test_smoke.py`

**Marker**: `@pytest.mark.emulator` (reuses existing marker, skips when Mesen not available)

### Test Scenarios

#### 1. `test_no_game_endpoints_return_200`
Start dashboard before Mesen connects. Hit all GET endpoints. Assert 200 with empty data, no 409s.

Endpoints: `/api/state`, `/api/segments`, `/api/references`, `/api/sessions`, `/api/estimator-params`, `/api/model`

#### 2. `test_game_loads_after_mesen_connects`
After Mesen starts and `rom_info` flows through the event loop:
- `GET /api/state` returns `tcp_connected: true`, `game_id` is not null, `game_name` is populated
- `GET /api/segments` returns 200
- `GET /api/references` returns 200
- `GET /api/model` returns estimator name and estimator list

#### 3. `test_stale_estimator_fallback` (unit test, not full-stack)
This is a fast unit test in `tests/test_scheduler.py`, not a smoke test — the dashboard fixture is session-scoped so we can't vary DB state per test.

- Create a temp DB, seed `allocator_config(key='estimator', value='bogus_name')`
- Instantiate `Scheduler(db, game_id)` — assert it doesn't crash
- Assert `scheduler.estimator.name` is the default (`kalman`), not `bogus_name`

#### 4. `test_no_tcp_errors_after_connect`
After game loads, check that no `err:` prefixed messages were received from Lua. This catches forward-reference bugs like `parse_string_array` and malformed command payloads.

Implementation: the `tcp_client` or a test helper collects all non-JSON lines from the TCP stream; assert none start with `err:`.

#### 5. `test_reference_start_works_after_connect`
After game loads:
- `POST /api/reference/start` returns 200 with `status: ok` (or a sensible status, not 409 "No game loaded")

Note: We can't fully test reference capture without poke scenarios for level transitions, but we can verify the endpoint accepts the request and the mode switches to `reference`.

---

## 2. File-Based Logging

**Problem**: Dashboard runs via AHK hotkey. Console output is lost. Errors like the `model_b` crash and TCP exceptions are invisible unless you happen to have a terminal open.

**Solution**: Configure Python `logging` to write to a rotating log file at startup.

- **Location**: `{data_dir}/spinlab.log`
- **Format**: `%(asctime)s %(levelname)s %(name)s — %(message)s`
- **Rotation**: `RotatingFileHandler`, 1 MB max, 3 backups
- **Level**: `INFO` to file, `WARNING` to console (same as current)
- **Scope**: Added in `cli.py` `main()` before any other initialization

This means every `logger.warning()`, `logger.exception()`, and `logger.info()` call already in the codebase (dashboard event loop, TCP manager, session manager) automatically goes to the file.

---

## 3. `spinlab db reset` CLI Command

**Problem**: Schema drift between laptop and desktop. Need a way to nuke the DB without manually deleting files.

**Solution**: New CLI subcommand.

```
spinlab db reset [--config config.yaml]
```

Behavior:
1. Resolve `data_dir` from config
2. Delete `{data_dir}/spinlab.db` if it exists
3. Create a fresh `Database` (triggers `_init_schema()` which creates all tables)
4. Print confirmation: `Database reset: {path}`

No confirmation prompt — this is a dev tool, and the data isn't precious. If we want a prompt later, we can add one.

---

## 4. TcpManager: Expand Known Non-JSON

Add all known non-JSON responses from Lua to `_KNOWN_NON_JSON` to suppress spurious warnings:

Current: `{"pong", "ok:queued", "heartbeat"}`

Add: any `ok:*` and `err:*` prefixed responses. Rather than enumerating them all, change the logic:

```python
if text.startswith("ok:") or text.startswith("err:"):
    logger.debug("TCP response: %s", text)
elif text in _KNOWN_NON_JSON:
    logger.debug("TCP non-JSON (expected): %s", text)
else:
    logger.warning("Unexpected non-JSON from Lua: %r", text)
```

This is forward-compatible — new Lua `ok:`/`err:` responses won't trigger warnings.

---

## 5. Update Documentation

After implementation, update:

- **`CLAUDE.md`** — add smoke test run instructions (e.g. `pytest -m emulator` now also runs smoke tests), document `spinlab db reset`, document log file location
- **`docs/ARCHITECTURE.md`** — add section on full-stack test infrastructure if it describes the test layers
- **`config.example.yaml`** — ensure `script_data_dir` field is documented (added to `EmulatorConfig` during this session)
