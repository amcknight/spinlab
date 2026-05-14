# CF4: Structured Log Context + Silent-Except Sweep

**Date:** 2026-05-14
**Origin:** [/improve scan 2026-05-13](../scans/2026-05-13-improve.md), convergent fix CF4
**Tier:** high-leverage (medium)

## Problem

The codebase has ~22 silent-except / context-free-warning sites that hurt 2am debuggability. Examples:

- `poller.py:101-103` — 60Hz read failures silently sleep+continue. No log, no counter.
- `state_builder.py:173-174` — `except (json.JSONDecodeError, KeyError): pass` eats serialized model-state corruption with zero diagnostic.
- `raclient.py:313-321` — SAVE_STATE timeout warning logs the pattern but not file mtime, retry count, or attempt budget.
- `practice.py:251-254` — `except (ConnectionError, OSError): pass` on teardown swallows backend disconnect with no record.
- `poller.py:122` — `self._deps.on_event(event)` has no try-except; a buggy handler crashes the 60Hz tick.

The verification pass confirmed no structured logging convention exists today (`logger.info(..., extra={...})` calls return zero hits across `python/spinlab/`).

## Goal

Make every infrastructure failure leave a useful breadcrumb without flooding the log. Adopt a tight convention for context-carrying log calls, then sweep the silent-except / context-free sites.

## Non-goals

- New exception hierarchies. `ActionError` (controller-flow) and `RAClientError` / `MovieRecordError` (RA-layer) already do their jobs.
- Restructuring how logs are written / rotated / collected. `{data_dir}/spinlab.log` config in `dashboard.py` is fine.
- The transactional `finalizer.py:40-60` rollback-step log — adding per-step try/except changes commit/rollback semantics. Defer.
- DIAGRAMS.md additions (separate scope, CF8).
- "No Game" race fix (Andrew's separate ~10-line backlog item).

## Design

### 1. The helper — `python/spinlab/log.py`

A single new module exporting `info / warn / error`. Each takes a `logging.Logger`, a message string, optional `exc=` for traceback, and keyword fields appended as `key=value` pairs.

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

**Format rationale:** repr-based formatting handles paths, ints, exceptions, and dicts uniformly. Paths get quoted (`path='C:/foo/bar.state'`), ints stay readable (`slot=9999`), no ad-hoc f-string drift. The whole module is ~30 lines including docstring.

**Why a module named `log`:** lets call sites read `log.warn(logger, ...)` — short, namespaced, doesn't conflict with stdlib `logging` imports.

### 2. The Poller transition-log

Two new flags on `Poller`:

```python
self._read_failing: bool = False
self._conditions_failing: bool = False
```

Read loop becomes (replacing `poller.py:99-103`):

```python
try:
    snap = self._deps.read_snapshot(self._deps.client)
except Exception as e:
    if not self._read_failing:
        log.warn(logger, "poller read failed", exc=e)
        self._read_failing = True
    await asyncio.sleep(self._period)
    continue
if self._read_failing:
    log.info(logger, "poller read recovered")
    self._read_failing = False
```

Same shape in `_stamp_conditions` (`poller.py:72-79`) for `reg.read_all()` failures.

Net effect: one warning per outage, one info on recovery. No 60Hz flood. Closes F1 (silent read failure) AND F9 (silent recovery from the orchestrator's NCI suppression flag — pattern carries over).

### 3. The sweep — three buckets

#### Bucket A: add context to existing log calls (8 sites, mechanical)

For each site, replace the existing `logger.warning("...", arg1, arg2)` with `log.warn(logger, "...", **fields)` carrying the minimum useful context.

| File:line | What | Fields to add |
|---|---|---|
| `raclient.py:313-321` | SAVE_STATE timeout warning | `pattern=`, `mtime_baseline=`, `attempts=SAVE_RETRY_ATTEMPTS`, `game_basename=` |
| `raclient.py:435-438` | basename-not-set error | `attempted_op=`, `connected=` |
| `nci.py:154` | RA `-1` reply for READ_CORE_RAM | `address=`, `length=`, `command=` |
| `movies.py:117-122` | ReplayErrorEvent emission | `replay_path=`, `slot=` |
| `movies.py:146-150` | movie stop RAClientError warning | `replay_path=` |
| `movie_io.py:148-160` | file-stability poll exhaustion | `pattern=`, `polls_completed=`, `movie_dir=` |
| `practice.py:209-214` | segment load timeout silent continue | `segment_id=`, `timeout_s=SEGMENT_LOAD_TIMEOUT_S` |
| `session_manager.py:203-212` | SSE broadcast exception | `subscriber_count=` |

#### Bucket B: replace silent `except: pass` with `log.warn` (9 sites, judgment per site)

| File:line | Before | After |
|---|---|---|
| `poller.py:101-103` | silent retry loop | handled by §2 (transition-log) — listed here for completeness |
| `poller.py:72-79` | `except Exception: return ev` (unstamped) | handled by §2 (transition-log) — listed here for completeness |
| `state_builder.py:173-174` | `except (JSONDecodeError, KeyError): pass` | `log.warn(logger, "model output deserialization failed", exc=e, segment_id=..., estimator=sr["estimator"])` |
| `cold_fill_detector.py:49-63` | phantom-death suppression silent | `log.info(logger, "cold-fill phantom death suppressed", exit_mode=..., segment_id=...)` (when the suppression fires) |
| `raclient.py:228-230` | stale slot cleanup unlink failure best-effort | `log.warn(logger, "stale slot cleanup failed", exc=e, path=...)` |
| `raclient.py:354-362` | move-to-copy fallback | `log.warn(logger, "move fell back to copy, source not deleted", src=..., dst=..., attempts=MOVE_RETRY_ATTEMPTS)` |
| `nci.py:72-87` | socket drain fire-and-forget | `log.warn(logger, "socket drain after timeout failed", exc=e)` if drain itself raises |
| `cold_fill.py:98-101` | save_state failure during capture, continues silently | `log.warn(logger, "cold-fill save_state failed, skipping segment", exc=e, segment_id=...)` |
| `practice.py:251-254` | `except (ConnectionError, OSError): pass` on teardown | `log.info(logger, "practice teardown after backend disconnect", exc=e)` — downgrade to info, intentional silence becomes traceable |

#### Bucket C: add new try/except wrappers (2 sites)

| File:line | What | Wrapper |
|---|---|---|
| `poller.py:122` | unwrapped `self._deps.on_event(event)` | `try: self._deps.on_event(event) except Exception as e: log.error(logger, "poller event handler raised", exc=e, event_type=type(event).__name__)` — handler crashes no longer kill the 60Hz tick |
| `poller.py:119-128` | `detector.step()` exceptions caught at outer `read_snapshot` except | wrap `for event in self._detector.step(...):` in its own try; `log.error(logger, "detector.step raised", exc=e, snapshot=snap)` |

### 4. Testing

- **`tests/unit/test_log.py`** (new):
  - `log.warn(logger, "x", path="/foo", slot=9999)` produces record with `msg == "x path='/foo' slot=9999"`
  - `log.error(logger, "x", exc=exc)` produces record with `exc_info` populated
  - `log.warn(logger, "x")` (no fields) produces record with `msg == "x"` (no trailing space)
  - `log.warn(logger, "x", path=Path("/foo"))` produces `path=PosixPath('/foo')` or `WindowsPath('/foo')` — verify Path repr survives
- **`tests/unit/retroarch/test_poller.py`** (extend):
  - One new test: inject a `read_snapshot` that raises twice then succeeds; assert exactly one warning + one info via `caplog`.
- **No per-site tests for the sweep.** Existing test coverage of the silent-except sites stays. Behavioral changes (Bucket C wrappers) are covered by the existing poller / event-routing tests.

### 5. Migration order

The sweep is independent per site — order doesn't matter for correctness, but ordering for review clarity:

1. Land `python/spinlab/log.py` + its unit tests.
2. Land poller transition-log (§2) — its own logical change.
3. Bucket A — mechanical context-adds, can land as one commit (8 sites).
4. Bucket B — site-by-site judgment, but no ordering dependency. One commit acceptable.
5. Bucket C — two small wrappers, one commit.

Total ~5 commits if split logically, 1 commit if bundled.

## Risks

- **The `log` module name** could shadow a future `import log` in third-party code. Risk is low since the codebase doesn't currently import bare `log`. Mitigation: namespace it as `from spinlab import log`.
- **Bucket B for `practice.py:251-254` (downgrade silent → info)** is the one judgment call most likely to be wrong. If teardown-after-disconnect is the common case, INFO will get noisy. If it's rare, INFO is fine. Andrew can downgrade further to DEBUG in follow-up if it's noisy in practice.
- **Bucket C `poller.py:122` wrapper** changes behavior — handler exceptions used to crash the tick. The wrapper makes the system more robust but may mask handler bugs that previously surfaced loudly. Mitigation: the new log line is at ERROR level with full traceback, so the bug is still visible — just not fatal.

## Out of scope (revisited)

- `finalizer.py:40-60` per-step rollback log — touches transactional commit, defer.
- DIAGRAMS.md additions — that's CF8 in the same scan.
- "No Game" race fix — Andrew's separate ~10-line item.
- `errors.py` `ActionError` restructuring — works fine.
- Introducing structured-log libraries (structlog etc.) — overkill for PoC.

## Acceptance

- `python/spinlab/log.py` exists with `info`, `warn`, `error` exported.
- `tests/unit/test_log.py` exists and passes.
- Poller transition-log test passes.
- Grep `\bexcept[^:]*:\s*pass\s*$` across `python/spinlab/` returns ≤ 1 hit (the deferred finalizer site, if any).
- Full `python -m pytest` passes.
- `ruff check python/` clean.
- `npx pyright python/spinlab/log.py python/spinlab/retroarch/poller.py` clean.
