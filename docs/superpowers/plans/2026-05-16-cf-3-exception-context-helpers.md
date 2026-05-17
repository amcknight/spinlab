# CF-3 Exception Context — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach diagnostic context (path, segment id, state, command, queue capacity) at 6 sites where exceptions are currently swallowed or logged without context.

**Architecture:** Reuse the existing `spinlab.log` structured-log helper (`log.info / log.warn / log.error`, supports `exc=...` plus arbitrary keyword fields). **No new module.** Each of the 6 sites becomes a small call to `log.<level>(logger, msg, exc=exc, **fields)`. M9 (condition_registry yaml) re-raises a `ValueError` that includes the yaml path; the others log without re-raising. This is the lower-friction option vs. typed exception subclasses (which would force callers to switch except clauses).

**Tech Stack:** Python 3.11. Existing helper: `python/spinlab/log.py` (already supports `exc=...` + `**fields`). `pytest`, `caplog`. No new deps.

**Decision (option a vs option b from the scan):**
- Picked **(a) helper function pattern** — actually, the helper already exists at `python/spinlab/log.py`. The plan reduces to 6 small site edits, each calling `log.warn` or `log.info` with the right kwargs. Net new helper code: **0 lines**.
- Rejected **(b) typed exception subclasses** — would require touching except clauses everywhere upstream and adds a hierarchy nobody needs for a 6-site fix. Reserve typed exceptions for cases where catchers must discriminate by type (none of these are).

**Anti-goal:** Don't ship a logging framework. Don't add new abstraction layers. If a task starts to grow past ~10 line edit, stop and re-scope.

**Branch:** `improve/exception-context-helpers` (already created).

**Site inventory (verbatim from the scan):**

| ID  | File:line                                  | Today                                           | After                                                                |
|-----|--------------------------------------------|-------------------------------------------------|----------------------------------------------------------------------|
| M10 | `speed_run.py:268-271`                     | `except (ConnectionError, OSError): pass`       | mirror practice.py:275-280 — `log.info(... exc=exc)`                 |
| M11 | `retroarch/orchestrator.py:299-306`        | `logger.exception("RetroArchOrchestrator: tick error")` | `log.error(... exc=exc, practice_armed=..., speed_run_armed=...)` |
| M12 | `retroarch/nci.py:76-91 _drain_socket`     | drains silently                                 | log warn iff > 0 datagrams drained, with `count=`                    |
| M14 | `sse.py:35-53 broadcast`                   | drops subscriber silently                       | `log.warn("SSE subscriber dropped (queue full)", subscribers_left=)` |
| M15 | `estimators/__init__.py:86-89 load_mature_states` | `except (json.JSONDecodeError, KeyError): continue` | `log.warn("skipped corrupt estimator state", exc=exc, segment_id=, estimator=, game_id=)` |
| M9  | `condition_registry.py:90-113 from_yaml`   | KeyError / ValueError without path              | wrap loop body, re-raise as `ValueError(f"... at {path}: {exc}")`    |

---

## File Structure

- **Modify** `python/spinlab/speed_run.py` (one site)
- **Modify** `python/spinlab/retroarch/orchestrator.py` (one site)
- **Modify** `python/spinlab/retroarch/nci.py` (one site, `log` already imported)
- **Modify** `python/spinlab/sse.py` (one site, add `logger` + `log` import)
- **Modify** `python/spinlab/estimators/__init__.py` (one site, add `logger` + `log` import)
- **Modify** `python/spinlab/condition_registry.py` (one site, add `logger` is NOT needed — re-raise only)
- **Create** `tests/unit/test_log_context.py` — the 6 tests, one per site, grouped in a single file (small + obvious + co-located)

No new production source files.

---

## Task 1: M10 — speed_run teardown log mirrors practice teardown

**Files:**
- Modify: `python/spinlab/speed_run.py:268-271`
- Test: `tests/unit/test_log_context.py` (new)

The bug: `speed_run.run_loop()` `finally` block catches `(ConnectionError, OSError)` and `pass`-es, while the identical block in `practice.py:273-281` calls `log.info(logger, "practice teardown after backend disconnect", exc=exc)`. Either both should be silent or both should log; we pick "both log" (consistent with the practice path).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_log_context.py`:

```python
"""Tests that verify each CF-3 site logs the right context on failure."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest


# ---------------------------------------------------------------------------
# M10 — speed_run teardown
# ---------------------------------------------------------------------------

class _DisconnectedEmu:
    """Minimal EmuBackend stub: connected=False after the loop body runs once."""

    def __init__(self) -> None:
        self.is_connected = True
        self._sent: list[object] = []
        self._first_send = True

    async def send_command(self, cmd: object) -> None:
        self._sent.append(cmd)
        if self._first_send:
            # First send = the practice/speed-run start command — succeed.
            self._first_send = False
            return
        # Subsequent sends (e.g. the stop command in finally) — fail as if RA died.
        raise ConnectionError("backend gone")


def test_speed_run_teardown_logs_when_backend_disconnects(caplog):
    """speed_run.run_loop's finally must log, not silently pass, on backend-gone."""
    from spinlab.speed_run import SpeedRunSession  # late import — avoids fixture coupling

    emu = _DisconnectedEmu()
    session = SpeedRunSession(emu=emu, segments=[], scheduler=None, db=None, game_id="g", session_id="s")  # type: ignore[arg-type]

    caplog.set_level(logging.INFO, logger="spinlab.speed_run")
    asyncio.run(session.run_loop())

    messages = [r.getMessage() for r in caplog.records if r.name == "spinlab.speed_run"]
    assert any("speed_run teardown" in m for m in messages), (
        f"expected a 'speed_run teardown' log line; got: {messages!r}"
    )
```

Note: SpeedRunSession's actual `__init__` signature may differ — read `python/spinlab/speed_run.py` top-of-file before writing the test, and copy the signature exactly. If construction is heavier than this stub allows (e.g. requires a real DB), simplify the test to call the smaller failure path directly: instantiate the minimum to enter `run_loop()`'s `finally`, then assert the log.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_speed_run_teardown_logs_when_backend_disconnects -v`

Expected: FAIL — message "no 'speed_run teardown' log line" (the current code passes silently).

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/speed_run.py`. Find the block at lines 267-272:

```python
        finally:
            try:
                await self.emu.send_command(SpeedRunStopCmd())
            except (ConnectionError, OSError):
                pass
            self.stop()
```

Replace the `pass` line with a `log.info` call (matching `practice.py:275-280`):

```python
        finally:
            try:
                await self.emu.send_command(SpeedRunStopCmd())
            except (ConnectionError, OSError) as exc:
                log.info(
                    logger, "speed_run teardown after backend disconnect",
                    exc=exc,
                )
            self.stop()
```

Verify `from spinlab import log` is already present near the top of `speed_run.py`. Imports at top of file (already there per `grep`): `import logging` at line 5; `logger = logging.getLogger(__name__)` at line 28. If `from spinlab import log` is not imported, add it next to `import logging`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_log_context.py::test_speed_run_teardown_logs_when_backend_disconnects -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/speed_run.py tests/unit/test_log_context.py
git commit -m "speed_run: log teardown disconnect (mirror practice.py)"
```

---

## Task 2: M11 — orchestrator tick error logs state context

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py:299-306`
- Test: `tests/unit/test_log_context.py` (append)

The bug: `_tick_loop` catches `Exception` and calls `logger.exception("RetroArchOrchestrator: tick error")` without state info. When this fires at 50ms intervals, the log fills with identical lines and the operator can't tell whether practice was armed, whether speed-run was running, or what.

We add two fields that already exist on the timing objects: `practice_armed=self._practice_timing.is_armed` and `speed_run_armed=self._speed_run_timing.is_armed`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_log_context.py`:

```python
# ---------------------------------------------------------------------------
# M11 — orchestrator tick error includes timing state
# ---------------------------------------------------------------------------

def test_tick_loop_logs_state_on_error(caplog):
    """When a tick raises, the log must include practice_armed / speed_run_armed."""
    from spinlab.retroarch.orchestrator import RetroArchOrchestrator  # noqa: F401

    # Build the smallest object whose _tick_loop can be invoked once.
    # The exact construction depends on the orchestrator's __init__; read it
    # before writing the test. The minimum needed:
    #   - a _practice_timing whose .tick() raises and whose .is_armed is True
    #   - a _speed_run_timing whose .is_armed is False
    #   - self._running = True for one iteration, then False
    class _Boom:
        is_armed = True
        def tick(self) -> None:
            raise RuntimeError("kaboom")

    class _Quiet:
        is_armed = False
        def tick(self) -> None:
            pass

    orch = RetroArchOrchestrator.__new__(RetroArchOrchestrator)  # bypass __init__
    orch._practice_timing = _Boom()
    orch._speed_run_timing = _Quiet()
    orch._running = True

    # Run one iteration: monkeypatch the sleep to flip _running off so the loop
    # exits without us having to manage real time.
    import spinlab.retroarch.orchestrator as orch_mod

    async def _stop_after(_delay: float) -> None:
        orch._running = False

    real_sleep = orch_mod.asyncio.sleep
    orch_mod.asyncio.sleep = _stop_after  # type: ignore[assignment]
    try:
        caplog.set_level(logging.ERROR, logger="spinlab.retroarch.orchestrator")
        asyncio.run(orch._tick_loop())
    finally:
        orch_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]

    records = [
        r for r in caplog.records
        if r.name == "spinlab.retroarch.orchestrator" and "tick error" in r.getMessage()
    ]
    assert records, "expected a tick error log line"
    msg = records[0].getMessage()
    assert "practice_armed=True" in msg, msg
    assert "speed_run_armed=False" in msg, msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_tick_loop_logs_state_on_error -v`

Expected: FAIL — the existing `logger.exception(...)` call has no `practice_armed=` / `speed_run_armed=` fields.

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/retroarch/orchestrator.py`. The current block (lines 299-306):

```python
    async def _tick_loop(self) -> None:
        while self._running:
            try:
                self._practice_timing.tick()
                self._speed_run_timing.tick()
            except Exception:
                logger.exception("RetroArchOrchestrator: tick error")
            await asyncio.sleep(TICK_INTERVAL_SEC)
```

Replace with:

```python
    async def _tick_loop(self) -> None:
        while self._running:
            try:
                self._practice_timing.tick()
                self._speed_run_timing.tick()
            except Exception as exc:
                log.error(
                    logger, "RetroArchOrchestrator: tick error",
                    exc=exc,
                    practice_armed=self._practice_timing.is_armed,
                    speed_run_armed=self._speed_run_timing.is_armed,
                )
            await asyncio.sleep(TICK_INTERVAL_SEC)
```

Add `from spinlab import log` near `import logging` at line 14 if not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_log_context.py::test_tick_loop_logs_state_on_error -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/retroarch/orchestrator.py tests/unit/test_log_context.py
git commit -m "orchestrator: include timing state in tick_loop error log"
```

---

## Task 3: M12 — NCI `_drain_socket` logs when it actually drains

**Files:**
- Modify: `python/spinlab/retroarch/nci.py:76-91`
- Test: `tests/unit/test_log_context.py` (append)

The bug: `_drain_socket` is called after every NCI timeout to discard any late-arriving datagram. If a datagram is actually drained, that's diagnostic information ("a reply arrived after we gave up") and currently no record exists. We log a single warn line if `count > 0`. No log when nothing is drained — that's the happy path.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_log_context.py`:

```python
# ---------------------------------------------------------------------------
# M12 — NCI _drain_socket logs late datagram count
# ---------------------------------------------------------------------------

def test_drain_socket_logs_drained_datagram_count(caplog):
    """When a stale datagram is drained, log warn with count=1."""
    import socket

    from spinlab.retroarch.nci import NCIClient

    # Build two UDP sockets bound to localhost — one acts as "RA" (sends a late
    # datagram), the other acts as NCIClient's receive socket.
    fake_ra = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake_ra.bind(("127.0.0.1", 0))
    fake_ra_port = fake_ra.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_sock.bind(("127.0.0.1", 0))
    client_port = client_sock.getsockname()[1]
    client_sock.settimeout(0.5)

    # Send a "late reply" to client_sock so there's something to drain.
    fake_ra.sendto(b"late reply", ("127.0.0.1", client_port))

    client = NCIClient(host="127.0.0.1", port=fake_ra_port, timeout=0.5)

    caplog.set_level(logging.WARNING, logger="spinlab.retroarch.nci")
    client._drain_socket(client_sock)

    client_sock.close()
    fake_ra.close()

    records = [r for r in caplog.records if "drained late" in r.getMessage().lower()]
    assert records, "expected drained-late-datagram log line"
    assert "count=1" in records[0].getMessage(), records[0].getMessage()


def test_drain_socket_silent_when_nothing_drained(caplog):
    """No log emitted when the buffer was already empty."""
    import socket

    from spinlab.retroarch.nci import NCIClient

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.5)

    client = NCIClient(host="127.0.0.1", port=1, timeout=0.5)

    caplog.set_level(logging.WARNING, logger="spinlab.retroarch.nci")
    client._drain_socket(sock)
    sock.close()

    assert not [r for r in caplog.records if "drained late" in r.getMessage().lower()], (
        "drain should be silent when nothing was drained"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_drain_socket_logs_drained_datagram_count -v`

Expected: FAIL on the first test — drain is currently silent in all cases.

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/retroarch/nci.py`. Current `_drain_socket` (lines 76-91):

```python
    def _drain_socket(self, sock: socket.socket) -> None:
        """Discard any datagrams sitting in the receive buffer.

        Called after a timeout so that a reply that arrived during the timeout
        interval cannot be misattributed to the next command issued on the same socket.
        """
        sock.setblocking(False)
        try:
            while True:
                try:
                    sock.recvfrom(RECV_BUFFER_BYTES)
                except (BlockingIOError, OSError):
                    break
        finally:
            sock.setblocking(True)
            sock.settimeout(self.timeout)
```

Replace with:

```python
    def _drain_socket(self, sock: socket.socket) -> None:
        """Discard any datagrams sitting in the receive buffer.

        Called after a timeout so that a reply that arrived during the timeout
        interval cannot be misattributed to the next command issued on the same socket.
        """
        sock.setblocking(False)
        drained = 0
        try:
            while True:
                try:
                    sock.recvfrom(RECV_BUFFER_BYTES)
                    drained += 1
                except (BlockingIOError, OSError):
                    break
        finally:
            sock.setblocking(True)
            sock.settimeout(self.timeout)
        if drained:
            log.warn(logger, "NCI: drained late datagrams", count=drained)
```

`from spinlab import log` is already imported in this file (line 14).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_log_context.py::test_drain_socket_logs_drained_datagram_count tests/unit/test_log_context.py::test_drain_socket_silent_when_nothing_drained -v`

Expected: both PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/retroarch/nci.py tests/unit/test_log_context.py
git commit -m "nci: log when _drain_socket discards late datagrams"
```

---

## Task 4: M14 — SSE broadcaster logs dropped subscriber

**Files:**
- Modify: `python/spinlab/sse.py`
- Test: `tests/unit/test_log_context.py` (append)

The bug: when a subscriber's queue is full *and* the recovery (get one, put again) also fails, the queue is removed from the subscriber list silently. We log a warn line with the remaining subscriber count so operators can see if there's a pattern of drops.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_log_context.py`:

```python
# ---------------------------------------------------------------------------
# M14 — SSE broadcaster logs dropped subscriber
# ---------------------------------------------------------------------------

def test_sse_broadcaster_logs_dropped_subscriber(caplog):
    """When recovery fails and a subscriber is unsubscribed, log warn."""
    from spinlab.sse import SSEBroadcaster

    broadcaster = SSEBroadcaster()
    q = broadcaster.subscribe(maxsize=1)
    # Fill the queue twice so the recovery path also fails: put once, then
    # broadcast tries put_nowait (fails QueueFull), get_nowait (succeeds, removes
    # the one item), put_nowait again (succeeds). To force the second put to
    # also fail, we need the queue to refuse — easiest path is to monkeypatch
    # put_nowait to always raise QueueFull.
    import asyncio as _asyncio

    def _always_full(_item: object) -> None:
        raise _asyncio.QueueFull()

    # Pre-fill so the initial put_nowait fails; then make the recovery put fail too.
    q.put_nowait("first")
    q.put_nowait = _always_full  # type: ignore[assignment]

    caplog.set_level(logging.WARNING, logger="spinlab.sse")
    asyncio.run(broadcaster.broadcast({"hello": "world"}))

    records = [r for r in caplog.records if "subscriber dropped" in r.getMessage().lower()]
    assert records, "expected SSE subscriber-dropped log line"
    # The remaining-subscriber count should be 0 (the only one was just dropped).
    assert "subscribers_left=0" in records[0].getMessage(), records[0].getMessage()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_sse_broadcaster_logs_dropped_subscriber -v`

Expected: FAIL — no log emitted on drop today.

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/sse.py`. Add imports near the top (after `import asyncio`):

```python
import logging

from spinlab import log

logger = logging.getLogger(__name__)
```

Modify the `broadcast` method's dead-queue cleanup (lines 52-53):

Before:
```python
        for q in dead:
            self.unsubscribe(q)
```

After:
```python
        for q in dead:
            self.unsubscribe(q)
            log.warn(
                logger, "SSE subscriber dropped (queue full)",
                subscribers_left=len(self._subscribers),
            )
```

The order matters: `unsubscribe` first so `len(self._subscribers)` reflects post-drop state in the log.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_log_context.py::test_sse_broadcaster_logs_dropped_subscriber -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/sse.py tests/unit/test_log_context.py
git commit -m "sse: log when subscriber dropped after queue-full recovery"
```

---

## Task 5: M15 — estimators load_mature_states logs corrupt state_json

**Files:**
- Modify: `python/spinlab/estimators/__init__.py:71-90`
- Test: `tests/unit/test_log_context.py` (append)

The bug: `load_mature_states` silently skips DB rows whose `state_json` blob fails to decode. If a schema change made every estimator state suddenly unparseable, the function would silently return `[]` and the priors would degrade to defaults with no signal. Log a warn with `segment_id`, `estimator`, `game_id` so the row in question is identifiable.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_log_context.py`:

```python
# ---------------------------------------------------------------------------
# M15 — estimators.load_mature_states logs corrupt state_json
# ---------------------------------------------------------------------------

def test_load_mature_states_logs_corrupt_json(caplog):
    """Corrupt state_json row must produce a warn with seg/estimator/game context."""
    from dataclasses import dataclass

    from spinlab.estimators import EstimatorState, load_mature_states

    @dataclass
    class _DummyState(EstimatorState):
        n_completed: int = 0

        def to_dict(self) -> dict:
            return {"n_completed": self.n_completed}

        @classmethod
        def from_dict(cls, d: dict) -> "_DummyState":
            return cls(n_completed=d["n_completed"])

    class _StubDB:
        def load_all_model_states(self, game_id: str) -> list[dict]:
            return [
                {"segment_id": "seg-corrupt", "estimator": "dummy", "state_json": "{not json"},
                {"segment_id": "seg-ok",      "estimator": "dummy", "state_json": '{"n_completed": 10}'},
            ]

    caplog.set_level(logging.WARNING, logger="spinlab.estimators")
    results = load_mature_states(
        db=_StubDB(),  # type: ignore[arg-type]
        game_id="game-x",
        estimator_name="dummy",
        state_cls=_DummyState,
        maturity_threshold=5,
    )

    # The good row should survive
    assert len(results) == 1
    assert results[0].n_completed == 10

    # The bad row should have produced a log line with context
    records = [r for r in caplog.records if "corrupt estimator state" in r.getMessage().lower()]
    assert records, "expected corrupt-state-json log line"
    msg = records[0].getMessage()
    assert "segment_id='seg-corrupt'" in msg, msg
    assert "estimator='dummy'" in msg, msg
    assert "game_id='game-x'" in msg, msg
```

Note: `EstimatorState` is an abstract dataclass — the test defines a tiny concrete one inline. Read `python/spinlab/estimators/__init__.py:30-69` to confirm `EstimatorState`'s base shape (its abstract methods are `to_dict` and `from_dict`, plus a `n_completed: int` field on the base class). If `EstimatorState` doesn't define `n_completed` itself, the dummy may need to declare it differently.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_load_mature_states_logs_corrupt_json -v`

Expected: FAIL — no log emitted today.

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/estimators/__init__.py`. Add imports near the top (after the existing `import json` at line 4):

```python
import logging

from spinlab import log

logger = logging.getLogger(__name__)
```

Modify the `load_mature_states` function (lines 71-90):

Before:
```python
    rows = db.load_all_model_states(game_id)
    states: list[S] = []
    for r in rows:
        if r["estimator"] != estimator_name or not r["state_json"]:
            continue
        try:
            states.append(state_cls.from_dict(json.loads(r["state_json"])))
        except (json.JSONDecodeError, KeyError):
            continue
    return [s for s in states if s.n_completed >= maturity_threshold]
```

After:
```python
    rows = db.load_all_model_states(game_id)
    states: list[S] = []
    for r in rows:
        if r["estimator"] != estimator_name or not r["state_json"]:
            continue
        try:
            states.append(state_cls.from_dict(json.loads(r["state_json"])))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warn(
                logger, "skipped corrupt estimator state",
                exc=exc,
                segment_id=r.get("segment_id"),
                estimator=estimator_name,
                game_id=game_id,
            )
            continue
    return [s for s in states if s.n_completed >= maturity_threshold]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_log_context.py::test_load_mature_states_logs_corrupt_json -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/estimators/__init__.py tests/unit/test_log_context.py
git commit -m "estimators: log corrupt state_json with segment+estimator+game context"
```

---

## Task 6: M9 — `condition_registry.from_yaml` re-raises with path context

**Files:**
- Modify: `python/spinlab/condition_registry.py:89-113`
- Test: `tests/unit/test_log_context.py` (append)

The bug: `from_yaml` raises `KeyError("name")` or `ValueError("unknown scope: ...")` or a bare `yaml.YAMLError` with zero indication of which yaml file is broken. At 2am with multiple games loaded, the operator needs the path to find the file. We wrap the entire parse body in `try/except (KeyError, ValueError, yaml.YAMLError)` and re-raise a `ValueError` whose message includes `path`, preserving the original via `from exc`.

This is a re-raise, not a log — `from_yaml` is called once per game at startup and any failure should halt loading, not be silently logged.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_log_context.py`:

```python
# ---------------------------------------------------------------------------
# M9 — condition_registry.from_yaml error includes path
# ---------------------------------------------------------------------------

def test_from_yaml_error_includes_path(tmp_path):
    """A malformed conditions.yaml must raise with the yaml path in the message."""
    from spinlab.condition_registry import ConditionRegistry

    bad_yaml = tmp_path / "conditions.yaml"
    bad_yaml.write_text(
        # Missing 'name' key — will trigger KeyError("name") in the parse loop.
        "conditions:\n"
        "  - scope: game\n"
        "    address: 0x1000\n"
        "    size: 1\n"
        "    type: u8\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        ConditionRegistry.from_yaml(bad_yaml)

    msg = str(exc_info.value)
    assert str(bad_yaml) in msg, f"path not in error message: {msg!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_log_context.py::test_from_yaml_error_includes_path -v`

Expected: FAIL — currently raises `KeyError("name")` with no path.

- [ ] **Step 3: Implement the fix**

Edit `python/spinlab/condition_registry.py`. Confirm `import yaml` is already at the top (it must be, since `yaml.safe_load` is used). Modify `from_yaml` (lines 89-113):

Before:
```python
    @classmethod
    def from_yaml(cls, path: Path) -> "ConditionRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defs: list[ConditionDef] = []
        for c in raw.get("conditions", []):
            scope_raw = c["scope"]
            if scope_raw == "game":
                scope = Scope.game()
            elif isinstance(scope_raw, dict) and "levels" in scope_raw:
                scope = Scope.levels_of(scope_raw["levels"])
            else:
                raise ValueError(f"unknown scope: {scope_raw!r}")
            defs.append(ConditionDef(
                name=c["name"],
                address=int(c["address"]),
                size=int(c["size"]),
                type=c["type"],
                values=({int(k): str(v) for k, v in c["values"].items()}
                        if c.get("values") else None),
                scope=scope,
            ))
        return cls(
            definitions=defs,
            death_penalty_ms=raw.get("death_penalty_ms", DEFAULT_DEATH_PENALTY_MS),
        )
```

After:
```python
    @classmethod
    def from_yaml(cls, path: Path) -> "ConditionRegistry":
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            defs: list[ConditionDef] = []
            for c in raw.get("conditions", []):
                scope_raw = c["scope"]
                if scope_raw == "game":
                    scope = Scope.game()
                elif isinstance(scope_raw, dict) and "levels" in scope_raw:
                    scope = Scope.levels_of(scope_raw["levels"])
                else:
                    raise ValueError(f"unknown scope: {scope_raw!r}")
                defs.append(ConditionDef(
                    name=c["name"],
                    address=int(c["address"]),
                    size=int(c["size"]),
                    type=c["type"],
                    values=({int(k): str(v) for k, v in c["values"].items()}
                            if c.get("values") else None),
                    scope=scope,
                ))
            return cls(
                definitions=defs,
                death_penalty_ms=raw.get("death_penalty_ms", DEFAULT_DEATH_PENALTY_MS),
            )
        except (KeyError, ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"failed to parse conditions yaml at {path}: {exc}") from exc
```

Indent the existing body four spaces; nothing else changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_log_context.py::test_from_yaml_error_includes_path -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add python/spinlab/condition_registry.py tests/unit/test_log_context.py
git commit -m "condition_registry: include yaml path in from_yaml parse errors"
```

---

## Task 7: Full-suite verification + branch wrap-up

CLAUDE.md is explicit: full `python -m pytest` (including emulator and frontend tests) before declaring work done, and `SKIPPED` emulator tests count as failures.

- [ ] **Step 1: Run fast suite**

```powershell
python -m pytest -m "not emulator"
```

Expected: all green. Add the 6 new tests; total ~796 passed (was ~790 + 6 new tests). No failures, no errors.

- [ ] **Step 2: Run emulator suite (requires RetroArch + SPINLAB_TEST_ROM)**

```powershell
python -m pytest -m emulator
```

Expected: ~12 passed. `SKIPPED` is NOT acceptable per CLAUDE.md — if every emulator test skips, that's a fixture failure, not a green light. Surface to Andrew before continuing.

- [ ] **Step 3: Frontend build + tests** (only if a frontend file changed — for CF-3 none did, so this is a sanity check only)

```powershell
cd frontend; npm run build; npm test
```

Expected: build + Vitest both pass. If neither changed, skip this step.

- [ ] **Step 4: Static analysis sanity-pass on changed files**

```powershell
npx pyright python/spinlab/speed_run.py python/spinlab/retroarch/orchestrator.py python/spinlab/retroarch/nci.py python/spinlab/sse.py python/spinlab/estimators/__init__.py python/spinlab/condition_registry.py
ruff check python/spinlab/speed_run.py python/spinlab/retroarch/orchestrator.py python/spinlab/retroarch/nci.py python/spinlab/sse.py python/spinlab/estimators/__init__.py python/spinlab/condition_registry.py
```

Expected: no new errors. Pre-existing errors in these files are OK to leave; per CLAUDE.md "Don't introduce new errors. Existing errors are tracked and will be cleaned up over time."

- [ ] **Step 5: Hand off to finishing-a-development-branch**

Invoke `superpowers:finishing-a-development-branch` to decide between merge / PR. Branch is `improve/exception-context-helpers`; six commits; small surface; no schema change; no frontend change. The expected default is a merge-to-main fast-forward.

---

## Self-Review Notes

- **Spec coverage:** all 6 sites from the scan have a dedicated task (Tasks 1-6). M13 is correctly excluded (debunked by VERIFY). The "no new helper module" decision is in the Architecture header.
- **No placeholders:** every step has the actual code to write or command to run. Test imports are concrete. The two places where dynamic discovery is needed (SpeedRunSession's __init__ signature in Task 1, EstimatorState's base fields in Task 5) have an explicit "read the file first, copy the signature" note rather than `TBD`.
- **Type consistency:** `log.info / log.warn / log.error` signature matches `python/spinlab/log.py:26-35` (`logger`, `msg`, `*, exc=None, **fields`). Logger names match `__name__` per module (e.g. `spinlab.speed_run`, `spinlab.retroarch.orchestrator`).
- **Anti-pattern check:** no task creates a new helper module; no task introduces a new exception class. Each fix is ≤10 lines of edit + a focused test.
