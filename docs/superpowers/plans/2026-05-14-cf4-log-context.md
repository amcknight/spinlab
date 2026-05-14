# CF4: Structured Log Context + Silent-Except Sweep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land a tiny `python/spinlab/log.py` helper, give the 60 Hz Poller transition-based failure logging, and sweep ~18 silent-except / context-free-warning sites so 2 am triage gets structured breadcrumbs.

**Architecture:** New module `spinlab.log` exporting `info / warn / error` functions that append `key=value` repr fields to a message. Poller grows two boolean transition flags. All other changes are mechanical context-adds or replace `except: pass` with `log.warn(...)`. No new exception classes — existing `ActionError` and `RAClientError` hierarchies untouched.

**Tech Stack:** Python 3.11+ stdlib `logging`. Tests via `pytest -m "not emulator"`, type-check via `npx pyright`, lint via `ruff check`.

**Spec:** `docs/superpowers/specs/2026-05-14-cf4-log-context-design.md`.

---

## File Structure

**Create:**
- `python/spinlab/log.py` — the helper (~30 lines)
- `tests/unit/test_log.py` — unit tests for the helper

**Modify:**
- `python/spinlab/retroarch/poller.py` — transition-log + Bucket C try/except wrappers
- `tests/unit/retroarch/test_poller.py` — extend with transition-log + wrapper tests
- `python/spinlab/retroarch/raclient.py` — Bucket A (SAVE_STATE timeout, move-fallback, slot cleanup) + Bucket A (basename error message)
- `python/spinlab/retroarch/nci.py` — Bucket A (READ_CORE_RAM -1 reply) + Bucket B (socket drain)
- `python/spinlab/retroarch/movies.py` — Bucket A (ReplayError emission, movie stop)
- `python/spinlab/retroarch/movie_io.py` — Bucket A (file-stability poll exhaustion)
- `python/spinlab/session_manager.py` — Bucket A (SSE broadcast)
- `python/spinlab/state_builder.py` — Bucket B (silent JSON parse)
- `python/spinlab/practice.py` — Bucket A (segment load timeout) + Bucket B (teardown ConnectionError)
- `python/spinlab/retroarch/cold_fill_detector.py` — Bucket B (phantom-death suppression diagnostic)
- `python/spinlab/capture/cold_fill.py` — Bucket A (save_state failure context)

---

## Task 1: Create the `log` helper module and unit tests

**Files:**
- Create: `python/spinlab/log.py`
- Create: `tests/unit/test_log.py`

- [ ] **Step 1.1: Write the failing test for `log.warn`**

Create `tests/unit/test_log.py`:

```python
"""Tests for spinlab.log — tiny structured-log helper."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from spinlab import log


def test_warn_formats_fields_as_key_value(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.warn")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "save_state timed out", path="/foo/bar.state", slot=9999)
    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.levelno == logging.WARNING
    # repr-based: strings get quoted, ints stay bare.
    assert rec.getMessage() == "save_state timed out path='/foo/bar.state' slot=9999"


def test_info_formats_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.info")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log.info(logger, "poller read recovered")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert caplog.records[0].getMessage() == "poller read recovered"


def test_warn_with_no_fields_emits_clean_message(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.nofields")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "bare message")
    assert caplog.records[0].getMessage() == "bare message"  # no trailing space


def test_error_with_exc_attaches_traceback(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.exc")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR, logger=logger.name):
            log.error(logger, "movie playback failed", exc=exc, replay_path="/r.replay")
    rec = caplog.records[0]
    assert rec.levelno == logging.ERROR
    assert rec.getMessage() == "movie playback failed replay_path='/r.replay'"
    assert rec.exc_info is not None
    assert rec.exc_info[0] is ValueError


def test_path_repr_renders_correctly(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("spinlab.test_log.path")
    p = Path("/tmp/foo")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.warn(logger, "msg", path=p)
    msg = caplog.records[0].getMessage()
    # Path repr varies by OS (PosixPath vs WindowsPath); just verify the path is present.
    assert "path=" in msg
    assert "foo" in msg
```

- [ ] **Step 1.2: Run the test, confirm it fails**

```bash
python -m pytest tests/unit/test_log.py -v
```

Expected: ImportError on `from spinlab import log` (module doesn't exist).

- [ ] **Step 1.3: Implement the `log` module**

Create `python/spinlab/log.py`:

```python
"""Tiny structured-log helper. Appends key=value context to log messages.

Usage:
    from spinlab import log
    import logging
    logger = logging.getLogger(__name__)

    log.warn(logger, "save_state timed out", path=p, slot=s, attempts=3)
    log.error(logger, "movie playback failed", exc=exc, replay_path=path)
    log.info(logger, "poller read recovered")
"""
from __future__ import annotations

import logging


def _emit(level_fn, msg: str, exc: BaseException | None, fields: dict) -> None:
    if fields:
        msg = f"{msg} " + " ".join(f"{k}={v!r}" for k, v in fields.items())
    if exc is not None:
        level_fn(msg, exc_info=exc)
    else:
        level_fn(msg)


def info(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.info, msg, exc, fields)


def warn(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.warning, msg, exc, fields)


def error(logger: logging.Logger, msg: str, *, exc: BaseException | None = None, **fields: object) -> None:
    _emit(logger.error, msg, exc, fields)
```

- [ ] **Step 1.4: Run the test, confirm it passes**

```bash
python -m pytest tests/unit/test_log.py -v
```

Expected: 5 passed.

- [ ] **Step 1.5: Type-check the new module**

```bash
npx pyright python/spinlab/log.py
```

Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 1.6: Commit**

```bash
git add python/spinlab/log.py tests/unit/test_log.py
git commit -m "log: add tiny structured-log helper (info/warn/error with **fields)"
```

---

## Task 2: Poller transition-log for read failures + Bucket C wrappers

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`
- Modify: `tests/unit/retroarch/test_poller.py`

- [ ] **Step 2.1: Write the failing test for read transition-log**

Open `tests/unit/retroarch/test_poller.py` and look at how existing tests construct `Poller` + `PollerDeps` (around the file's existing tests; reuse the same fixture pattern). Add this test at the end of the file:

```python
async def test_poller_logs_read_failure_then_recovery(caplog):
    """Poller should log exactly one warning on read failure and one info on recovery."""
    import logging

    fail_then_recover = iter([RuntimeError("nci dead")] + [None] * 5)

    def read_snapshot(_client):
        nxt = next(fail_then_recover)
        if isinstance(nxt, Exception):
            raise nxt
        return _make_fake_snapshot()  # use the existing helper in the file

    deps = _make_test_deps(read_snapshot=read_snapshot)  # use existing fixture pattern
    poller = Poller(deps, period=0.001)

    with caplog.at_level(logging.INFO, logger="spinlab.retroarch.poller"):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await task

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warns) == 1, f"expected 1 warn, got {[r.getMessage() for r in warns]}"
    assert "poller read failed" in warns[0].getMessage()
    assert any("poller read recovered" in r.getMessage() for r in infos)
```

Notes:
- `_make_fake_snapshot()` and `_make_test_deps(...)` are placeholders — use whatever the existing tests in `test_poller.py` use to construct a working snapshot/deps. If no factory exists, copy the construction pattern from an existing test like `test_poller_emits_level_entrance` (or similar).
- The exact logger name will match `__name__` of the poller module: `spinlab.retroarch.poller`.

- [ ] **Step 2.2: Run the test, confirm it fails**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_logs_read_failure_then_recovery -v
```

Expected: assertion failure (warn count is 0; no logging today).

- [ ] **Step 2.3: Add the `_read_failing` flag and transition-log in Poller**

In `python/spinlab/retroarch/poller.py`, find the `Poller.__init__` block (around line 50-60). Add this field at the end of `__init__` (next to `self.poll_count: int = 0`):

```python
        # Transition-log state: log once on entering failure, once on recovery.
        self._read_failing: bool = False
```

At the top of the file, add the import:

```python
from spinlab import log
```

Then replace the existing read-failure block (`poller.py:99-103`):

```python
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception:
                await asyncio.sleep(self._period)
                continue
```

with:

```python
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception as exc:
                if not self._read_failing:
                    log.warn(logger, "poller read failed", exc=exc)
                    self._read_failing = True
                await asyncio.sleep(self._period)
                continue
            if self._read_failing:
                log.info(logger, "poller read recovered")
                self._read_failing = False
```

- [ ] **Step 2.4: Run the test, confirm it passes**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_logs_read_failure_then_recovery -v
```

Expected: PASS.

- [ ] **Step 2.5: Run the full poller test file**

```bash
python -m pytest tests/unit/retroarch/test_poller.py -v
```

Expected: all passing (no regressions in existing tests).

- [ ] **Step 2.6: Commit**

```bash
git add python/spinlab/retroarch/poller.py tests/unit/retroarch/test_poller.py
git commit -m "poller: transition-log on read failure / recovery"
```

---

## Task 3: Poller transition-log for `_stamp_conditions` failures

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`

- [ ] **Step 3.1: Add the `_conditions_failing` flag**

In `Poller.__init__`, next to `self._read_failing`, add:

```python
        self._conditions_failing: bool = False
```

- [ ] **Step 3.2: Replace `_stamp_conditions` body**

Current (`poller.py:72-82`):

```python
    def _stamp_conditions(self, ev: Any) -> Any:
        reg = self._deps.conditions_registry
        if reg is None:
            return ev
        try:
            values = reg.read_all(self._deps.client)
        except Exception:
            return ev
        if not values:
            return ev
        return dataclasses.replace(ev, conditions=values)
```

Replace with:

```python
    def _stamp_conditions(self, ev: Any) -> Any:
        reg = self._deps.conditions_registry
        if reg is None:
            return ev
        try:
            values = reg.read_all(self._deps.client)
        except Exception as exc:
            if not self._conditions_failing:
                log.warn(logger, "poller condition read failed", exc=exc)
                self._conditions_failing = True
            return ev
        if self._conditions_failing:
            log.info(logger, "poller condition read recovered")
            self._conditions_failing = False
        if not values:
            return ev
        return dataclasses.replace(ev, conditions=values)
```

- [ ] **Step 3.3: Run the existing test file, confirm no regressions**

```bash
python -m pytest tests/unit/retroarch/test_poller.py -v
```

Expected: all passing.

- [ ] **Step 3.4: Commit**

```bash
git add python/spinlab/retroarch/poller.py
git commit -m "poller: transition-log on condition-read failure / recovery"
```

---

## Task 4: Bucket C — wrap `on_event` and `detector.step` in Poller

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`
- Modify: `tests/unit/retroarch/test_poller.py`

- [ ] **Step 4.1: Write the failing test for handler-exception isolation**

Add to `tests/unit/retroarch/test_poller.py`:

```python
async def test_poller_event_handler_exception_does_not_crash_tick(caplog):
    """A handler that raises should be logged at ERROR and not stop the poller."""
    import logging

    snaps = [_make_fake_snapshot()] * 5  # always yield a snapshot
    snap_iter = iter(snaps)

    def read_snapshot(_client):
        return next(snap_iter)

    def crashy_handler(_event):
        raise ValueError("handler exploded")

    deps = _make_test_deps(read_snapshot=read_snapshot, on_event=crashy_handler)
    # Pick a detector that emits at least one event per snapshot for the test.
    # Reuse whatever helper the file's existing event-emission tests use.
    poller = Poller(deps, period=0.001)

    with caplog.at_level(logging.ERROR, logger="spinlab.retroarch.poller"):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.02)
        poller.stop()
        await task

    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("event handler raised" in r.getMessage() for r in errs)
    # Crucially, the poller must have continued to tick (poll_count > 1).
    assert poller.poll_count > 1
```

If the existing test fixtures don't make it easy to emit events deterministically, you can simulate it by hand-injecting an event into the queue or by using a stub detector. Match whatever pattern the existing event-emission tests use.

- [ ] **Step 4.2: Run the test, confirm it fails**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_event_handler_exception_does_not_crash_tick -v
```

Expected: FAIL — exception bubbles, poll_count may be 1 or task raises.

- [ ] **Step 4.3: Wrap `detector.step` and `on_event` in the run loop**

In `poller.py`, find the inner event-dispatch loop (`poller.py:119-128`). Current code:

```python
            for event in self._detector.step(snap, timestamp_ms=ts):
                event = self._stamp_state_path(event)
                event = self._stamp_conditions(event)
                self._deps.on_event(event)

            cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            if cf_event is not None:
                cf_event = self._stamp_state_path(cf_event)
                cf_event = self._stamp_conditions(cf_event)
                self._deps.on_event(cf_event)
```

Replace with:

```python
            try:
                events = list(self._detector.step(snap, timestamp_ms=ts))
            except Exception as exc:
                log.error(logger, "detector.step raised", exc=exc)
                events = []

            for event in events:
                event = self._stamp_state_path(event)
                event = self._stamp_conditions(event)
                try:
                    self._deps.on_event(event)
                except Exception as exc:
                    log.error(
                        logger, "poller event handler raised",
                        exc=exc, event_type=type(event).__name__,
                    )

            try:
                cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            except Exception as exc:
                log.error(logger, "cold_fill.step raised", exc=exc)
                cf_event = None
            if cf_event is not None:
                cf_event = self._stamp_state_path(cf_event)
                cf_event = self._stamp_conditions(cf_event)
                try:
                    self._deps.on_event(cf_event)
                except Exception as exc:
                    log.error(
                        logger, "poller event handler raised",
                        exc=exc, event_type=type(cf_event).__name__,
                    )
```

Rationale for the `list(...)` conversion: `_detector.step` is a generator; if it raises mid-iteration, callers can't easily isolate the exception. Materializing eagerly makes the failure visible at the try boundary.

- [ ] **Step 4.4: Run the test, confirm it passes**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_event_handler_exception_does_not_crash_tick -v
```

Expected: PASS.

- [ ] **Step 4.5: Run the full poller test file**

```bash
python -m pytest tests/unit/retroarch/test_poller.py -v
```

Expected: all passing.

- [ ] **Step 4.6: Commit**

```bash
git add python/spinlab/retroarch/poller.py tests/unit/retroarch/test_poller.py
git commit -m "poller: isolate handler/detector exceptions with logged try/except"
```

---

## Task 5: Bucket A — add structured context to existing log calls

These are mechanical edits at sites that already have a `logger.warning/error/info/exception` call. Each substep edits one site. All changes go in one commit at the end.

**Files:**
- Modify: `python/spinlab/retroarch/raclient.py`
- Modify: `python/spinlab/retroarch/nci.py`
- Modify: `python/spinlab/retroarch/movies.py`
- Modify: `python/spinlab/retroarch/movie_io.py`
- Modify: `python/spinlab/session_manager.py`
- Modify: `python/spinlab/practice.py`
- Modify: `python/spinlab/capture/cold_fill.py`

- [ ] **Step 5.1: Add the `log` import to every file you'll edit in this task**

In each of the files listed above, add `from spinlab import log` near the existing `import logging` lines. Skip files that already have it from earlier tasks (poller.py).

- [ ] **Step 5.2: `raclient.py:313-321` — SAVE_STATE timeout**

Find the current block:

```python
        logger.warning(
            'save_state timeout dest="%s" pattern=%s ra_game="%s" %s',
            dest_path, pattern, cur_game, last_err,
        )
```

Replace with:

```python
        log.warn(
            logger, "save_state timed out",
            dest=str(dest_path), pattern=pattern, ra_game=cur_game,
            attempts=SAVE_RETRY_ATTEMPTS, detail=last_err,
        )
```

- [ ] **Step 5.3: `raclient.py:354-362` — move-to-copy fallback**

Find:

```python
            logger.warning(
                'save_state move_fallback src="%s" dst="%s" — copied but '
                "couldn't unlink source after %d retries (RA still holds handle)",
                src, dst, MOVE_RETRY_ATTEMPTS,
            )
```

Replace with:

```python
            log.warn(
                logger, "move fell back to copy, source not deleted",
                src=str(src), dst=str(dst), attempts=MOVE_RETRY_ATTEMPTS,
            )
```

- [ ] **Step 5.4: `raclient.py:428-431` — stale slot cleanup**

Find:

```python
            logger.warning(
                'startup_sweep could not remove slot_file="%s" err=%s',
                slot_path, exc,
            )
```

Replace with:

```python
            log.warn(
                logger, "startup_sweep could not remove slot file",
                exc=exc, slot_path=str(slot_path),
            )
```

(`exc` is the local variable name caught in the `except OSError as exc:` above.)

- [ ] **Step 5.5: `nci.py:154` — READ_CORE_RAM -1 reply**

Find:

```python
        if data_tokens[0] == "-1":
            raise NCIProtocolError(f"RetroArch returned error for read at {addr:#x}: {reply!r}")
```

Replace with:

```python
        if data_tokens[0] == "-1":
            log.warn(
                logger, "RA read_ram returned -1",
                addr=hex(addr), length=length, reply=reply,
            )
            raise NCIProtocolError(f"RetroArch returned error for read at {addr:#x}: {reply!r}")
```

Make sure `logger` is defined at module top (it should be: check for `logger = logging.getLogger(__name__)`). If not, add it.

- [ ] **Step 5.6: `movies.py:117-122` — ReplayError emission (two log calls)**

Find:

```python
        except MoviePlaybackError as exc:
            logger.error("Movie replay verification failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            logger.error("Movie replay failed: %s", exc)
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
```

Replace with:

```python
        except MoviePlaybackError as exc:
            log.error(
                logger, "movie replay verification failed",
                exc=exc, replay_path=str(path),
            )
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
        except RAClientError as exc:
            log.error(
                logger, "movie replay failed",
                exc=exc, replay_path=str(path),
            )
            self._on_event(ReplayErrorEvent(message=str(exc)))
            return
```

- [ ] **Step 5.7: `movies.py:146-150` — movie stop RAClientError**

Find:

```python
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            logger.warning("Movie replay failed to stop: %s", exc)
```

Replace with:

```python
        try:
            await self._active_playback.stop()
        except RAClientError as exc:
            log.warn(
                logger, "movie replay failed to stop",
                exc=exc, replay_path=str(self._active_playback.path),
            )
```

Note: this assumes `self._active_playback` has a `.path` attribute. If the attribute name differs (e.g., `_path` or accessible via a different property), check the playback type and adjust. If no path is reachable, drop the `replay_path=` field.

- [ ] **Step 5.8: `movie_io.py:209-212` — record_movie file poll exhaustion**

Find:

```python
            logger.warning(
                'record_movie no_new_file dir="%s" attempts=%d%s',
                movie_dir, MOVIE_POLL_ATTEMPTS, hint,
            )
```

Replace with:

```python
            log.warn(
                logger, "record_movie: no new file appeared",
                movie_dir=str(movie_dir), attempts=MOVIE_POLL_ATTEMPTS,
                existing_replays=existing_replays, hint=hint.strip() or None,
            )
```

(The `hint` string is a human-readable note about `replay_max_keep`. Keeping it as a field preserves the diagnostic; the `.strip() or None` collapses to `None` when no hint applies, suppressing the field.)

- [ ] **Step 5.9: `session_manager.py:209-212` — SSE broadcast**

Find:

```python
        try:
            await self.sse.broadcast(self.get_state())
        except Exception:
            logger.exception("SSE broadcast failed; subscribers will sync on next event")
```

Replace with:

```python
        try:
            await self.sse.broadcast(self.get_state())
        except Exception as exc:
            log.warn(
                logger, "SSE broadcast failed; subscribers will sync on next event",
                exc=exc, subscriber_count=self.sse.subscriber_count,
            )
```

Note: if `self.sse` doesn't expose `subscriber_count` as a property/attribute, look at the SSE class to find the right accessor (`len(self.sse._queues)` or similar). If no public accessor exists, add a simple `@property` to the SSE class that returns the count, or drop the field.

- [ ] **Step 5.10: `practice.py:209-214` — segment load timeout (info on first occurrence)**

Find:

```python
        while self.is_running and self.emu.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                continue
```

Replace with:

```python
        load_timeouts = 0
        while self.is_running and self.emu.is_connected:
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
                break
            except asyncio.TimeoutError:
                load_timeouts += 1
                if load_timeouts == 1:
                    log.info(
                        logger, "practice: waiting for attempt result",
                        segment_id=cmd.id, timeout_s=SEGMENT_LOAD_TIMEOUT_S,
                    )
                continue
```

Note: `cmd.id` is the segment ID in scope at the call site. Adjust if the local variable name differs.

- [ ] **Step 5.11: `raclient.py:433-438` — basename-not-set raise: add diagnostic log before raise**

Find:

```python
    def _require_basename(self) -> None:
        if not self._game_basename:
            raise RAClientError(
                "RAClient: game basename not set yet — call connect() first "
                "(or check that RetroArch is running and has a ROM loaded)."
            )
```

Replace with:

```python
    def _require_basename(self) -> None:
        if not self._game_basename:
            log.warn(
                logger, "RAClient: basename not set at op time",
                connected=self._connected,
            )
            raise RAClientError(
                "RAClient: game basename not set yet — call connect() first "
                "(or check that RetroArch is running and has a ROM loaded)."
            )
```

(Smallest useful diagnostic — surfaces whether `_connected` was True (race: GET_STATUS replied without rom_info) vs False (haven't connected yet). The spec mentioned `attempted_op=` but threading the op name through every caller is out of PoC scope.)

- [ ] **Step 5.12: `cold_fill.py:96-101` — cold-fill save_state failure**

Find:

```python
        seg_id = event.segment_id or self.current
        try:
            await self.emu.save_state(seg_id)
        except Exception:
            logger.exception("cold_fill: save_state failed for seg_id=%r — "
                             "continuing without storing this cold state", seg_id)
            return False
```

Replace with:

```python
        seg_id = event.segment_id or self.current
        try:
            await self.emu.save_state(seg_id)
        except Exception as exc:
            log.warn(
                logger, "cold_fill: save_state failed, skipping segment",
                exc=exc, segment_id=seg_id,
            )
            return False
```

- [ ] **Step 5.13: Run the fast test suite, confirm no regressions**

```bash
python -m pytest -m "not emulator" -q
```

Expected: all passing (no test depends on the exact text of these log lines; if any does, update it to match the new structured form).

- [ ] **Step 5.14: Type-check the touched files**

```bash
npx pyright python/spinlab/retroarch/raclient.py python/spinlab/retroarch/nci.py python/spinlab/retroarch/movies.py python/spinlab/retroarch/movie_io.py python/spinlab/session_manager.py python/spinlab/practice.py python/spinlab/capture/cold_fill.py
```

Expected: 0 errors, 0 warnings.

- [ ] **Step 5.15: Lint the touched files**

```bash
ruff check python/spinlab/retroarch/ python/spinlab/session_manager.py python/spinlab/practice.py python/spinlab/capture/cold_fill.py
```

Expected: All checks passed.

- [ ] **Step 5.16: Commit**

```bash
git add python/spinlab/retroarch/raclient.py python/spinlab/retroarch/nci.py python/spinlab/retroarch/movies.py python/spinlab/retroarch/movie_io.py python/spinlab/session_manager.py python/spinlab/practice.py python/spinlab/capture/cold_fill.py
git commit -m "log: add structured context to existing warn/error sites (Bucket A)"
```

---

## Task 6: Bucket B — replace silent `except: pass` with structured logging

**Files:**
- Modify: `python/spinlab/state_builder.py`
- Modify: `python/spinlab/practice.py`
- Modify: `python/spinlab/retroarch/cold_fill_detector.py`

- [ ] **Step 6.1: Add `log` import to each file that needs it (skip if already added in Task 5)**

```python
from spinlab import log
```

- [ ] **Step 6.2: `state_builder.py:169-174` — silent JSON parse**

Find:

```python
                for sr in state_rows:
                    output_json = sr.get("output_json")
                    if output_json:
                        try:
                            model_outputs[sr["estimator"]] = ModelOutput.from_dict(
                                json.loads(output_json)
                            ).to_dict()
                        except (json.JSONDecodeError, KeyError):
                            pass
```

Replace with:

```python
                for sr in state_rows:
                    output_json = sr.get("output_json")
                    if output_json:
                        try:
                            model_outputs[sr["estimator"]] = ModelOutput.from_dict(
                                json.loads(output_json)
                            ).to_dict()
                        except (json.JSONDecodeError, KeyError) as exc:
                            log.warn(
                                logger, "model output deserialization failed",
                                exc=exc,
                                segment_id=ps.current_segment_id,
                                estimator=sr["estimator"],
                            )
```

Make sure `logger` is defined at module top (`logger = logging.getLogger(__name__)`). If not, add it.

- [ ] **Step 6.3: `practice.py:251-254` — silent teardown ConnectionError**

Find:

```python
        finally:
            try:
                await self.emu.send_command(PracticeStopCmd())
            except (ConnectionError, OSError):
                pass
            self.stop()
```

Replace with:

```python
        finally:
            try:
                await self.emu.send_command(PracticeStopCmd())
            except (ConnectionError, OSError) as exc:
                log.info(
                    logger, "practice teardown after backend disconnect",
                    exc=exc,
                )
            self.stop()
```

(Note: `log.info` not `log.warn` — this is the expected outcome when the dashboard tears down after the emu vanishes. The log line just makes the silence traceable.)

- [ ] **Step 6.4: `cold_fill_detector.py` — add suppression log to `resync_after_state_load`**

The phantom-death suppression at lines 49-63 is silent. Add a log line at the END of the resync method so a future debugger sees that the suppression activated (with the stale exit_mode that would have caused the phantom):

Find:

```python
    def resync_after_state_load(self, snapshot: MemorySnapshot) -> None:
        """Sync prev_* to the just-loaded snapshot to suppress phantom edges.
        ...
        """
        self._waiting_spawn = False
        self._prev_anim = snapshot.player_anim
        self._prev_level_start = snapshot.level_start
        self._prev_exit_mode = snapshot.exit_mode
```

Replace with:

```python
    def resync_after_state_load(self, snapshot: MemorySnapshot) -> None:
        """Sync prev_* to the just-loaded snapshot to suppress phantom edges.
        ...
        """
        self._waiting_spawn = False
        self._prev_anim = snapshot.player_anim
        self._prev_level_start = snapshot.level_start
        self._prev_exit_mode = snapshot.exit_mode
        if snapshot.exit_mode != 0:
            log.info(
                logger, "cold_fill_detector: resync suppressed phantom death",
                exit_mode=snapshot.exit_mode,
                segment_id=self._segment_id,
            )
```

(Keep the rest of the existing docstring intact — the `...` above is shorthand for the existing comment block; do NOT actually replace it with literal `...`.)

Make sure `logger` is defined at module top. If not, add `logger = logging.getLogger(__name__)` near the imports.

- [ ] **Step 6.5: Run the fast test suite**

```bash
python -m pytest -m "not emulator" -q
```

Expected: all passing. If a test asserts on the silent-pass behavior (very unlikely), update it.

- [ ] **Step 6.6: Type-check + lint**

```bash
npx pyright python/spinlab/state_builder.py python/spinlab/practice.py python/spinlab/retroarch/cold_fill_detector.py
ruff check python/spinlab/state_builder.py python/spinlab/practice.py python/spinlab/retroarch/cold_fill_detector.py
```

Expected: 0 errors / All checks passed.

- [ ] **Step 6.7: Commit**

```bash
git add python/spinlab/state_builder.py python/spinlab/practice.py python/spinlab/retroarch/cold_fill_detector.py
git commit -m "log: replace silent except sites with structured warnings (Bucket B)"
```

---

## Notes on spec deviations

While drafting this plan, two minor reclassifications surfaced after reading the actual call sites:

- **`raclient.py:228-230`, `raclient.py:354-362`, `cold_fill.py:96-101`** — the spec listed these as Bucket B (silent except), but the code already contains a `logger.warning` / `logger.exception` call. They're really Bucket A (context-add), so they live in Task 5 instead of Task 6.
- **`nci.py:72-87` (socket drain)** — the spec's "log.warn if drain itself raises" doesn't have a clear hook. The drain's inner `except (BlockingIOError, OSError): break` is the loop-termination condition, not a failure path. No silent-failure surface to address. Skipped intentionally.

If the executor finds otherwise during implementation (e.g., the drain DOES have a silent path the lens flagged correctly), add a substep mid-task and proceed.

---

## Task 7: Final verification — acceptance criteria

**Files:** none modified. This task is verification only.

- [ ] **Step 7.1: Confirm grep shows ≤ 1 silent-pass site remaining**

```bash
grep -rn -E '^\s*except[^:]*:\s*pass\s*$' python/spinlab/
```

Expected: 0 or 1 hit. Allowed leftover: any `except: pass` inside a transactional `with self.transaction()` block (the spec deferred `finalizer.py:40-60`). If you see hits in other files, decide case-by-case whether the spec missed them.

If new sites surface, do NOT silently skip them — either add a follow-up task or document them in the commit message.

- [ ] **Step 7.2: Run the full test suite**

```bash
python -m pytest
```

Expected: all passing. The 11 emulator tests will skip if RA isn't running; that's fine for the unit-test-only acceptance gate. If RA is available, run them too:

```bash
python -m pytest -m emulator
```

Expected: all 12 emulator tests pass.

- [ ] **Step 7.3: Type-check the whole package**

```bash
npx pyright python/
```

Expected: no new errors introduced (project may have existing errors that are tracked per CLAUDE.md; the test is "no NEW errors from this change").

- [ ] **Step 7.4: Lint the whole package**

```bash
ruff check python/
```

Expected: All checks passed.

- [ ] **Step 7.5: Confirm `log` module is imported in the expected files**

```bash
grep -rln "from spinlab import log" python/spinlab/
```

Expected: at least these files appear:
- `python/spinlab/retroarch/poller.py`
- `python/spinlab/retroarch/raclient.py`
- `python/spinlab/retroarch/nci.py`
- `python/spinlab/retroarch/movies.py`
- `python/spinlab/retroarch/movie_io.py`
- `python/spinlab/retroarch/cold_fill_detector.py`
- `python/spinlab/session_manager.py`
- `python/spinlab/practice.py`
- `python/spinlab/state_builder.py`
- `python/spinlab/capture/cold_fill.py`

- [ ] **Step 7.6: Push branch and open PR (optional)**

If working on a feature branch:

```bash
git push -u origin <branch-name>
gh pr create --title "log: structured-log helper + silent-except sweep (CF4)" --body "$(cat <<'EOF'
## Summary
- Add `spinlab.log` helper (`info / warn / error` with key=value field appending)
- Poller transition-log: one warn on read failure, one info on recovery (no 60Hz flood)
- Wrap poller event-handler + detector.step exceptions so a buggy handler can't crash the tick
- Sweep ~13 silent-except / context-free-warning sites across raclient/nci/movies/movie_io/session_manager/practice/state_builder/cold_fill/cold_fill_detector

## Spec
docs/superpowers/specs/2026-05-14-cf4-log-context-design.md

## Test plan
- [x] `pytest -m "not emulator"` passes
- [ ] `pytest -m emulator` passes locally (RA running)
- [x] `npx pyright python/` clean (no new errors)
- [x] `ruff check python/` clean
- [x] `grep -rn 'except[^:]*:\s*pass\s*$' python/spinlab/` shows ≤1 hit

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

(Only run this step if Andrew explicitly asks for a PR.)
