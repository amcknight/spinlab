# Scenario-Runner Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the emulator test infrastructure emit a readable diagnostic trajectory when scenarios are slow or stall; stop swallowing `pause_toggle` exceptions; replace the unconditional 0.3s pause-settle sleep with an event-driven wait. Next flake gets actionable diagnostics instead of opaque `NCITimeout` cascades.

**Architecture:**
- Add structured `logging.Logger` calls inside `RAPokeEngine.run_scenario` for each phase (`fresh_boot_load`, `addr_map_zero`, `frame_advance_loop_start`, `loop_end`). Lines route through the existing `spinlab` ring buffer so the `pytest_runtest_makereport` diagnostic hook surfaces them on failure.
- Capture `harness.proc.poll()` and elapsed scenario phase info in the `run_scenario` fixture's timeout `pytest.fail` message.
- Replace `time.sleep(0.3)` after `pause_toggle()` in `replay_ra_dashboard` setup with a `wait_for` predicate that polls `harness.client.get_status()` for `PLAYING`.
- Replace the silent `except Exception: pass` swallow at `tests/integration/ra_harness.py:315-320` with a `logger.warning` that names the exception type, message, attempt, and port. The existing `get_status` retry already handles recovery; we only need to surface what's being recovered from.

**Tech Stack:** Python 3.11, pytest, stdlib `logging`, existing `tests/integration/_wait_for.py` (`wait_for` + `WaitOutcome`), existing `tests/integration/_diagnostics.py` ring buffer.

**Scope absorbed:** CF-B = M2 (pause-settle sleep) + M5 (run_scenario silence + run_scenario timeout missing proc.poll) + M10 (orchestrator_ready polling — covered as a side-effect: the new `wait_for` here uses the same shape, with a focused 0.05s interval) + OB5 (swallowed pause_toggle exception in RAHarness.launch).

---

### Task 1: Add per-phase logging to `RAPokeEngine.run_scenario`

**Files:**
- Modify: `tests/integration/ra_poke_engine.py:32-134`
- Test: `tests/unit/integration/test_ra_poke_engine.py` (add test)

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/unit/integration/test_ra_poke_engine.py`:

```python
def test_run_scenario_logs_phase_trajectory(caplog):
    """Each phase emits one log line so a slow/stalled scenario produces a
    readable trajectory in the pytest diagnostic block instead of silence."""
    import logging
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    scenario = parse_poke("settle: 1\n1: game_mode=20\n")

    with caplog.at_level(logging.INFO, logger="spinlab.test.scenario"):
        engine.run_scenario(scenario)

    messages = [r.message for r in caplog.records if r.name == "spinlab.test.scenario"]
    # Exact phase set (order matters): no fresh_boot when slot is None, so
    # the trajectory is just zero/loop_start/loop_end.
    assert any("addr_map_zero" in m for m in messages), f"no addr_map_zero line in {messages}"
    assert any("frame_advance_loop_start" in m for m in messages), f"no loop_start in {messages}"
    assert any("loop_end" in m for m in messages), f"no loop_end in {messages}"


def test_run_scenario_logs_fresh_boot_when_slot_set(caplog):
    """When `fresh_boot_slot` is configured, the load-state phase is also
    surfaced so a stuck LOAD_STATE_SLOT is visible in the trajectory."""
    import logging
    fake = FakeNCIClient()
    # FakeNCIClient needs a load_state_slot stub for this case.
    fake.load_state_slot = lambda slot: None  # type: ignore[attr-defined]
    engine = RAPokeEngine(fake, fresh_boot_slot=9998)
    scenario = parse_poke("settle: 1\n1: game_mode=20\n")

    with caplog.at_level(logging.INFO, logger="spinlab.test.scenario"):
        engine.run_scenario(scenario)

    messages = [r.message for r in caplog.records if r.name == "spinlab.test.scenario"]
    assert any("fresh_boot_load" in m for m in messages), f"no fresh_boot_load in {messages}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_poke_engine.py::test_run_scenario_logs_phase_trajectory tests/unit/integration/test_ra_poke_engine.py::test_run_scenario_logs_fresh_boot_when_slot_set -v`

Expected: Both FAIL with `assert any("addr_map_zero" in m for m in messages)` — no log lines emitted yet.

- [ ] **Step 3: Add the logger + per-phase emit**

Replace `tests/integration/ra_poke_engine.py` lines 31-90 with:

```python
"""
... existing docstring unchanged ...
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from tests.integration.addresses import ADDR_MAP

from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.snapshot import read_snapshot

FRAME_PERIOD_MS = 16  # 60Hz approximation; only used for monotonic timestamps

# Frames of detector silence required to declare a scenario "quiescent"
# (i.e. no more events coming, safe to terminate). 12 frames = 200ms at 60Hz
# — comfortably larger than the longest observed event-firing delay (events
# fire on the same frame as the triggering poke) while still cutting most
# scenarios from ~75 frames to ~30 frames.
QUIESCENCE_FRAMES = 12

# Frames to free-run after LOAD_STATE_SLOT before zeroing ADDR_MAP. snes9x
# needs a few frames to re-initialise the SPC core and finish unpacking the
# state; without this settle, the first scenario poke can land before RA's
# normal frame loop has resumed and the snapshot reads stale data.
POST_LOAD_SETTLE_FRAMES = 30

# Per-phase trajectory logger. Routes through the spinlab logger so the ring
# buffer captures it for the pytest_runtest_makereport diagnostic dump.
logger = logging.getLogger("spinlab.test.scenario")


class _NCISurface(Protocol):
    def read_ram(self, addr: int, length: int) -> bytes: ...
    def write_ram(self, addr: int, data: bytes) -> None: ...
    def frame_advance(self) -> None: ...
    def load_state_slot(self, slot: int) -> None: ...


class RAPokeEngine:
    def __init__(
        self,
        client: _NCISurface,
        fresh_boot_slot: int | None = None,
    ) -> None:
        self._client = client
        self._fresh_boot_slot = fresh_boot_slot

    def run_scenario(self, scenario: dict) -> list:
        scenario_start = time.monotonic()

        # 0. If the harness staged a fresh-boot savestate, reset to it so
        #    SPC700 / 65816 / WRAM are all back to a known clean state. WRAM-
        #    only zeroing in step 1 can't reach the SPC chip, which is what
        #    let test_entrance_goal's fanfare music leak io_port writes into
        #    the next scenario before this hook landed.
        if self._fresh_boot_slot is not None:
            phase_start = time.monotonic()
            self._client.load_state_slot(self._fresh_boot_slot)
            # snes9x writes the state asynchronously; advance a handful of
            # frames so the load is fully applied before we start poking.
            for _ in range(POST_LOAD_SETTLE_FRAMES):
                self._client.frame_advance()
            # Tiny wall-clock sleep so the FRAMEADVANCE chain has actually
            # been processed by RA's NCI thread before we start bombing it
            # with WRITE_CORE_RAM. Without this, the first scenario can
            # see writes ignored.
            time.sleep(0.1)
            logger.info(
                "fresh_boot_load slot=%d settle_frames=%d elapsed=%.3fs",
                self._fresh_boot_slot, POST_LOAD_SETTLE_FRAMES,
                time.monotonic() - phase_start,
            )
```

Replace the `# 1. Zero ADDR_MAP` ... `# 2. Schedule + bookkeeping` ... loop section at lines 92-134 with:

```python
        # 1. Zero ADDR_MAP for per-scenario isolation
        phase_start = time.monotonic()
        for addr in ADDR_MAP.values():
            self._client.write_ram(addr, b"\x00")
        logger.info(
            "addr_map_zero count=%d elapsed=%.3fs",
            len(ADDR_MAP), time.monotonic() - phase_start,
        )

        # 2. Schedule + bookkeeping
        schedule: dict[int, list[dict]] = {}
        for poke in scenario["pokes"]:
            schedule.setdefault(poke["frame"], []).append(poke)
        last_poke_frame = max(schedule, default=0)
        end_frame_cap = last_poke_frame + scenario["settle_frames"]

        held: dict[int, int] = {}
        detector = TransitionDetector()
        events: list = []
        # Start the quiescence clock at last_poke_frame so we always run at
        # least QUIESCENCE_FRAMES past the last write before terminating —
        # gives the detector room to observe the final state changes.
        frame_of_last_event = last_poke_frame

        logger.info(
            "frame_advance_loop_start last_poke_frame=%d end_frame_cap=%d "
            "pokes=%d",
            last_poke_frame, end_frame_cap, len(scenario["pokes"]),
        )
        last_frame = 0
        for frame in range(1, end_frame_cap + 1):
            last_frame = frame
            for poke in schedule.get(frame, []):
                held[poke["addr"]] = poke["value"]
            self._client.frame_advance()
            # Re-assert held values AFTER the ROM frame ran. Mask to low byte:
            # .poke files use values like 0x105 (e.g., level_num) — only the
            # low byte lands at $13BF; the high byte is held separately on
            # $13BE in the actual ROM.
            for addr, value in held.items():
                self._client.write_ram(addr, bytes([value & 0xFF]))
            snap = read_snapshot(self._client)  # type: ignore[arg-type]
            new_events = list(detector.step(snap, frame * FRAME_PERIOD_MS))
            events.extend(new_events)
            if new_events:
                frame_of_last_event = frame

            # Quiescence-based early termination. Don't even check until we've
            # passed last_poke_frame so all scheduled pokes have a chance to
            # land before we declare the scenario done.
            if (frame > last_poke_frame
                    and frame - frame_of_last_event >= QUIESCENCE_FRAMES):
                break

        logger.info(
            "loop_end frame=%d events=%d elapsed_total=%.3fs",
            last_frame, len(events), time.monotonic() - scenario_start,
        )
        return events
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_poke_engine.py -v`

Expected: All tests in `test_ra_poke_engine.py` pass (existing 4 + new 2 = 6 or more).

- [ ] **Step 5: Run the full fast suite to confirm no regression**

Run: `python -m pytest -m "not emulator" -q`

Expected: 915 passed (or current baseline + 2 new tests; no failures).

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ra_poke_engine.py tests/unit/integration/test_ra_poke_engine.py
git commit -m "tests/integration: per-phase logger in RAPokeEngine.run_scenario

Emits trajectory lines (fresh_boot_load, addr_map_zero,
frame_advance_loop_start, loop_end) via the spinlab.test.scenario logger.
Lines route through the existing ring buffer so the pytest_runtest_makereport
diagnostic dump surfaces them when a scenario times out or fails."
```

---

### Task 2: Capture `proc.poll()` on `run_scenario` timeout in conftest

**Files:**
- Modify: `tests/integration/conftest.py:173-191`

- [ ] **Step 1: Read the current `run_scenario` factory**

Run: `Read tool on tests/integration/conftest.py lines 145-195`.

The current body lives inside the `run_scenario` fixture at roughly:

```python
async def _run(scenario_name: str, timeout: float = 30.0) -> list:
    scenario_path = SCENARIO_DIR / scenario_name
    if not scenario_path.exists():
        pytest.fail(f"Scenario file not found: {scenario_path}")
    scenario = parse_poke_file(str(scenario_path))
    start = time.monotonic()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                ra_harness_love_yourself.engine.run_scenario, scenario
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"run_scenario({scenario_name!r}) timed out after "
            f"{elapsed:.1f}s (limit {timeout:.1f}s)"
        )
```

- [ ] **Step 2: Replace the `_run` body**

Edit `tests/integration/conftest.py` — find the `_run` function inside the `run_scenario` fixture and replace the body:

```python
async def _run(scenario_name: str, timeout: float = 30.0) -> list:
    scenario_path = SCENARIO_DIR / scenario_name
    if not scenario_path.exists():
        pytest.fail(f"Scenario file not found: {scenario_path}")
    scenario = parse_poke_file(str(scenario_path))
    start = time.monotonic()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                ra_harness_love_yourself.engine.run_scenario, scenario
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        # Capture RA process state at timeout so the next debugger can
        # distinguish "RA died mid-scenario" (proc.poll() != None) from
        # "scenario hung on a frame_advance roundtrip" (proc.poll() == None).
        try:
            proc_status = ra_harness_love_yourself.proc.poll()
        except Exception as exc:  # noqa: BLE001 — defensive: never let
            # diagnostic capture mask the real timeout.
            proc_status = f"<poll failed: {type(exc).__name__}: {exc}>"
        pytest.fail(
            f"run_scenario({scenario_name!r}) timed out after "
            f"{elapsed:.1f}s (limit {timeout:.1f}s); "
            f"RA proc.poll()={proc_status} pid={ra_harness_love_yourself.proc.pid}"
        )
```

- [ ] **Step 3: Run the full fast suite to confirm no regression**

Run: `python -m pytest -m "not emulator" -q`

Expected: same green baseline as before this task.

- [ ] **Step 4: Manually verify the new fail message shape**

If RA + Love Yourself ROM are available locally:

Run: `python -m pytest tests/integration/test_transitions.py -m emulator -v -x`

Expected: tests pass. (No manual timeout-induction needed — the diff is read-only on the happy path; the failure path renders only when a real timeout fires. The text appears in pytest output as `RA proc.poll()=<exit_code or None>`.)

- [ ] **Step 5: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests/integration: capture RA proc.poll() on run_scenario timeout

When a poke scenario times out, the pytest.fail message now includes the
harness pid and proc.poll() result so the next debugger can tell whether
RA died mid-scenario (Mode 3 ACCESS_VIOLATION style) or just stalled
waiting for a frame_advance roundtrip."
```

---

### Task 3: Replace 0.3s pause-settle sleep with `wait_for(PLAYING)`

**Files:**
- Modify: `tests/integration/conftest.py:287-295`

- [ ] **Step 1: Read the current pause-settle block**

The current block looks like:

```python
# PLAY_REPLAY requires RA in PLAYING state. The harness left it paused.
harness = ra_harness_love_yourself_no_reset
try:
    status = harness.client.get_status()
    if status.state == "PAUSED":
        harness.client.pause_toggle()
        time.sleep(0.3)  # let RA settle into PLAYING before tests POST replay/start
except Exception as exc:
    pytest.fail(format_pause_toggle_failure(harness, exc))
```

- [ ] **Step 2: Replace `time.sleep(0.3)` with `wait_for(PLAYING)`**

Replace with:

```python
# PLAY_REPLAY requires RA in PLAYING state. The harness left it paused.
harness = ra_harness_love_yourself_no_reset
try:
    status = harness.client.get_status()
    if status.state == "PAUSED":
        harness.client.pause_toggle()
        # Event-driven settle: poll status until PLAYING is observed.
        # Caps at 2s; on a healthy host the first poll typically wins
        # (so the previous unconditional 300ms is now ~50ms).
        play_outcome = wait_for(
            name="ra_playing_after_pause_toggle",
            fetch=lambda: harness.client.get_status(),
            predicate=lambda s: (
                (s.state == "PLAYING", f"state={s.state!r}")
            ),
            timeout_s=2.0,
            interval_s=0.05,
        )
        if not play_outcome.succeeded:
            pytest.fail(play_outcome.format_message())
except Exception as exc:
    pytest.fail(format_pause_toggle_failure(harness, exc))
```

`wait_for` is already imported at the top of conftest.py (see the orchestrator-ready block at lines 277-285).

- [ ] **Step 3: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`

Expected: green baseline preserved.

- [ ] **Step 4: Run the replay-fixture test 15 times in a row to validate**

Per `feedback_stress_test_flakes` memory: one green run is not enough for an emulator-test change.

```bash
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  echo "=== Run $i ===" >> /tmp/stress.log
  python -m pytest tests/integration/test_replay_fixture.py -m emulator -v >> /tmp/stress.log 2>&1
done
grep -E "^=+ |passed|failed|error" /tmp/stress.log | tail -50
```

Expected: 15/15 pass, no new flake mode. If a new flake appears, raise `timeout_s` to 3.0 (the harness's `PAUSE_VERIFY_RETRIES × PAUSE_VERIFY_INTERVAL_S` is 60 × 0.3 = 18s for the initial launch pause; 2s here is for the second toggle which is uncontended).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "tests/integration: event-driven pause-settle in replay_ra_dashboard

Replace unconditional time.sleep(0.3) after pause_toggle with
wait_for(name='ra_playing_after_pause_toggle', timeout_s=2.0,
interval_s=0.05). On a healthy host the first poll wins, so the
fixture-setup wait shrinks from 300ms to ~50ms. Slow hosts get up to
2s before failing loud with a named WaitOutcome message."
```

---

### Task 4: Surface swallowed `pause_toggle` exceptions in `RAHarness.launch`

**Files:**
- Modify: `tests/integration/ra_harness.py:313-321` (the inner `try: client.pause_toggle() except Exception: pass` inside the retry loop)
- Test: `tests/unit/integration/test_ra_harness.py` (add test)

- [ ] **Step 1: Read the current swallow site**

Run: `Read tool on tests/integration/ra_harness.py lines 310-325`. The relevant block is:

```python
for attempt in range(PAUSE_VERIFY_RETRIES):
    if after_state == "PLAYING":
        try:
            client.pause_toggle()
        except Exception:
            # Best-effort — keep trying. The next get_status
            # call surfaces a persistent error.
            pass
    time.sleep(PAUSE_VERIFY_INTERVAL_S)
    ...
```

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/integration/test_ra_harness.py` (the file already exists; append):

```python
def test_pause_toggle_exception_during_retry_is_logged(caplog, monkeypatch):
    """When client.pause_toggle() raises during the PAUSE_VERIFY retry loop,
    the harness should log a warning naming the exception type, message, and
    attempt number — not swallow it silently. The next get_status call still
    handles recovery; the log gives future debuggers a starting point when
    a persistent NCI fault is the root cause of a slow launch."""
    import logging
    from tests.integration.ra_harness import RAHarness

    # NOTE: This test exercises only the log line shape. The full retry loop
    # has many other dependencies (subprocess.Popen, NCIClient, etc.) so we
    # extract the swallow handler into a small helper that takes (exc, attempt,
    # port) and emit the warning from there. The helper is tested directly.
    with caplog.at_level(logging.WARNING, logger="tests.integration.ra_harness"):
        RAHarness._log_pause_toggle_recoverable(
            exc=ConnectionError("boom"),
            attempt=3,
            port=55321,
        )
    msgs = [r.message for r in caplog.records]
    assert any(
        "pause_toggle raised" in m and "ConnectionError" in m
        and "boom" in m and "attempt=3" in m and "port=55321" in m
        for m in msgs
    ), f"expected warning with type/message/attempt/port, got {msgs}"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_pause_toggle_exception_during_retry_is_logged -v`

Expected: FAIL with `AttributeError: type object 'RAHarness' has no attribute '_log_pause_toggle_recoverable'`.

- [ ] **Step 4: Add the helper + wire it into the retry loop**

In `tests/integration/ra_harness.py`, find the `class RAHarness:` definition and add this classmethod near the other private helpers (e.g. near `_cleanup_launch`):

```python
@classmethod
def _log_pause_toggle_recoverable(
    cls, *, exc: Exception, attempt: int, port: int,
) -> None:
    """Surface a recoverable pause_toggle failure inside the PAUSE_VERIFY
    retry loop. The retry loop itself handles recovery; this log line gives
    future debuggers a starting point when a persistent NCI fault is the
    root cause of a slow or failed launch."""
    logger.warning(
        "ra_harness: pause_toggle raised %s: %s on attempt=%d port=%d",
        type(exc).__name__, exc, attempt, port,
    )
```

Then replace the silent swallow inside the retry loop:

```python
for attempt in range(PAUSE_VERIFY_RETRIES):
    if after_state == "PLAYING":
        try:
            client.pause_toggle()
        except Exception as exc:
            # Best-effort — keep trying. The next get_status call
            # surfaces a persistent error. Log a warning so debuggers
            # can see what was being recovered from instead of silence.
            cls._log_pause_toggle_recoverable(
                exc=exc, attempt=attempt, port=port,
            )
    time.sleep(PAUSE_VERIFY_INTERVAL_S)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/integration/test_ra_harness.py::test_pause_toggle_exception_during_retry_is_logged -v`

Expected: PASS.

- [ ] **Step 6: Run the fast suite**

Run: `python -m pytest -m "not emulator" -q`

Expected: green baseline + 1 new test.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "tests/integration: surface swallowed pause_toggle exceptions

The PAUSE_VERIFY retry loop in RAHarness.launch caught pause_toggle()
exceptions and silently continued. Add _log_pause_toggle_recoverable
classmethod that emits a warning with exception type, message, attempt,
and port. Recovery still happens via the next get_status call; the log
just makes the recovery visible to future debuggers."
```

---

### Task 5: 15-iteration full-suite stress validation

**Files:** None — this is validation, not implementation.

- [ ] **Step 1: Run the full suite 15 times sequentially**

```bash
rm -f /tmp/scenario-runner-stress.log
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  echo "=== Run $i ===" >> /tmp/scenario-runner-stress.log
  python -m pytest >> /tmp/scenario-runner-stress.log 2>&1
done
```

- [ ] **Step 2: Tally pass/fail rate**

```bash
grep -E "passed|failed|error" /tmp/scenario-runner-stress.log | tail -45
```

Expected: 15/15 pass, OR a flake rate ≤ the pre-CF-B baseline (~7% per `project_test_reliability_known_issues`). If the new code introduced a regression, the rate will spike.

- [ ] **Step 3: Spot-check diagnostic output on at least one failed run (if any)**

For any run that failed, confirm the diagnostic block now contains:
- `fresh_boot_load slot=X ...` / `addr_map_zero count=X ...` / `frame_advance_loop_start ...` / `loop_end ...` lines from the scenario logger
- `RA proc.poll()=<exit_code or None>` in the timeout message (if a timeout was the trigger)
- `pause_toggle raised <ExceptionType>: <message> on attempt=N port=P` (if pause_toggle ever raised during launch retries)

- [ ] **Step 4: If everything is green, mark CF-B done in the scan file**

Edit `docs/superpowers/scans/2026-05-19-improve-*.md` — under the "Picked this session" line for CF-B, append: ` — SHIPPED at <commit-SHA>`.

- [ ] **Step 5: Commit the scan-file update (if no commit yet for it)**

```bash
git add docs/superpowers/scans/2026-05-19-improve-*.md
git commit -m "docs: mark CF-B shipped in 2026-05-19 improve scan"
```

---

## Notes for the executing agent

- **The `spinlab.test.scenario` logger inherits from `spinlab`.** `_diagnostics.install_log_handler()` already sets the `spinlab` logger to INFO and attaches the ring handler, so `spinlab.test.scenario` lines land in the ring automatically — no extra wiring needed.
- **Don't add a `logger.exception` in the swallow site.** The retry loop is expected to encounter transient `ConnectionResetError`-style failures during RA's NCI thread starvation; a full traceback per attempt would flood the ring. `logger.warning` with type/message/attempt/port is exactly the right signal density.
- **Don't lower `PAUSE_VERIFY_INTERVAL_S` to chase speed.** It's tuned to RA's NCI thread quanta; lowering it caused regressions in the 2026-05-18 flake hunt (see `project_test_reliability_known_issues`).
- **The 0.05s `interval_s` in Task 3 is safe** because the wait is on a single uncontended `pause_toggle()` follow-up (not on the initial launch toggle); the harness has already completed its PAUSE_VERIFY loop before this point.
