# Spec: Fail-Loud Hardening

## Philosophy

Single-user app. Silent defaults hide bugs. The principle:

- **Raise when the invariant is broken** — corrupt data, logic violations, missing required state. Crash, see the traceback, fix the root cause.
- **Log at ERROR (don't raise) when the environment is transiently unavailable** — TCP not connected yet, Lua not running, SSE subscriber slow. Restarting won't fix it; crashing won't help.
- **Litmus test:** "If I restart the process, will this fix itself?" Yes → log ERROR. No → raise.

## Changes

### Fix 1: CLI lua-cmd silent OSError

**File:** `python/spinlab/cli.py`, lines 111-116

**Problem:** `except OSError: pass` — user runs `spinlab lua-cmd`, socket fails, zero feedback.

**Fix:** Print the error and `sys.exit(1)`.

```python
except OSError as e:
    print(f"Failed to connect to Lua TCP server: {e}", file=sys.stderr)
    sys.exit(1)
```

**Category:** Raise — this is a user-facing command that should report failure.

---

### Fix 2: Dashboard event loop bare Exception catch

**File:** `python/spinlab/dashboard.py`, lines 51-57

**Problem:** `except Exception: logger.exception(...)` catches everything — DB corruption, state machine bugs, KeyErrors — and silently retries after 1s sleep.

**Fix:** Narrow the catch to known transient errors. Let everything else propagate.

```python
try:
    event = await tcp.recv_event(timeout=1.0)
    if event:
        await session.route_event(event)
except (ConnectionError, OSError, asyncio.TimeoutError):
    await asyncio.sleep(1)
```

Unknown exceptions crash the event loop, which crashes the process. Good — you see the traceback.

**Category:** Raise (by not catching) — unknown exceptions are bugs.

---

### Fix 3: TCP Manager silent JSON parse drop

**File:** `python/spinlab/tcp_manager.py`, lines 100-104

**Problem:** `except json.JSONDecodeError: pass` — malformed JSON silently vanishes.

**Fix:** Non-JSON lines are legitimate (heartbeat, `ok:queued`, `pong`). But lines that *look* like JSON (start with `{`) and fail to parse are bugs. Split the logic:

```python
if text.startswith("{"):
    event = json.loads(text)  # let JSONDecodeError propagate — it's a bug
    await self.events.put(event)
# else: non-JSON control line (heartbeat, ok:queued, pong) — skip silently
```

**Category:** Raise — if Lua sends `{broken json}`, that's a bug in the sender.

---

### Fix 4: TCP Manager silent connection error in read loop

**File:** `python/spinlab/tcp_manager.py`, lines 105-112

**Problem:** `except (ConnectionError, OSError, asyncio.CancelledError): pass` — connection errors are swallowed without any logging.

**Fix:** Log at ERROR for connection errors. CancelledError is expected during shutdown — keep that silent.

```python
except asyncio.CancelledError:
    pass  # expected during shutdown
except (ConnectionError, OSError) as e:
    logger.error("TCP read loop error: %s", e)
```

**Category:** Log ERROR — transient, but should be visible.

---

### Fix 5: Practice session silent practice_stop failure

**File:** `python/spinlab/practice.py`, lines 148-151

**Problem:** `except (ConnectionError, OSError): pass` when sending `practice_stop`.

**Fix:** Log at ERROR. Lua will timeout on its own, but the user should know the clean shutdown didn't happen.

```python
except (ConnectionError, OSError) as e:
    logger.error("Failed to send practice_stop to Lua: %s", e)
```

**Category:** Log ERROR — Lua will self-recover, but visibility matters.

---

### Fix 6: SSE broadcast silent subscriber drop

**File:** `python/spinlab/sse.py`, lines 29-47

**Problem:** Subscribers dropped for being full with no logging.

**Fix:** Log when a subscriber is dropped.

```python
dead.append(q)
logger.warning("Dropping stale SSE subscriber (queue full)")
```

**Category:** Log WARNING — this is back-pressure, not a bug, but should be visible.

---

### Fix 7: Scheduler model state deserialization failures

**File:** `python/spinlab/scheduler.py`, lines 96-112

**Problem:** `except (json.JSONDecodeError, KeyError): logger.warning(...)` — corrupt model state is silently skipped. Allocator gets incomplete data.

**Fix:** Raise. Corrupt model state means the DB has bad data. The right fix is to investigate, not silently degrade.

```python
# Remove try/except entirely. Let JSONDecodeError/KeyError propagate.
out = ModelOutput.from_dict(json.loads(sr["output_json"]))
model_outputs[sr["estimator"]] = out
```

If this crashes during practice, good — you'll see which segment has corrupt state and can fix or reset it.

**Category:** Raise — data integrity violation.

---

### Fix 8: Scheduler silent state rebuild on missing prior

**File:** `python/spinlab/scheduler.py`, lines 178-189

**Problem:** Missing prior state for an incomplete attempt silently falls through to `rebuild_state([new_attempt])`, which creates a fresh state from one data point. If prior state was supposed to exist but got corrupted/deleted, this silently produces garbage.

**Fix:** Log at ERROR when falling through to rebuild. The rebuild itself is acceptable (it's self-healing), but visibility is needed.

```python
else:
    logger.error(
        "No prior model state for segment=%s estimator=%s, rebuilding from scratch",
        segment_id, est.name,
    )
    state = est.rebuild_state([new_attempt])
```

**Category:** Log ERROR — self-healing but should be visible so the root cause (why was state missing?) can be investigated.

---

### Fix 9: Session Manager ROM fallback checksum

**File:** `python/spinlab/session_manager.py`, lines 219-232

**Problem:** ROM not found in rom_dir → silently uses `file_{name}` as fake checksum. Later, if the ROM appears, it gets a different (real) checksum — now you have two "games" for the same ROM.

**Fix:** Raise. If `rom_dir` is configured, the ROM should be there. A missing ROM is a config error.

```python
if not rom_path.exists():
    raise FileNotFoundError(
        f"ROM not found in rom_dir: {rom_path}. "
        f"Check config.yaml rom.dir or place the ROM file there."
    )
```

**Category:** Raise — config error, won't fix itself on restart.

---

### Fix 10: Session Manager model output deserialization in practice state

**File:** `python/spinlab/session_manager.py`, lines 149-155

**Problem:** `except (json.JSONDecodeError, KeyError)` when building practice state view — silently skips corrupt model outputs in the dashboard display.

**Fix:** Same as Fix 7 — raise. Corrupt data in the DB should be visible.

**Category:** Raise — data integrity.

---

### Fix 11: Draft Manager silent orphan cleanup

**File:** `python/spinlab/draft_manager.py`, lines 46-60

**Problem:** Multiple orphaned drafts found → older ones hard-deleted with no logging.

**Fix:** Log each deletion.

```python
for row in rows[1:]:
    logger.warning("Deleting orphaned draft capture run: %s", row[0])
    db.hard_delete_capture_run(row[0])
```

**Category:** Log WARNING — self-healing cleanup, but should be visible.

---

### Fix 12: Draft Manager direct db.conn access

**File:** `python/spinlab/draft_manager.py`, lines 48-56

**Problem:** `db.conn.execute(...)` bypasses the DB abstraction layer.

**Fix:** Add two methods to Database and use them:

```python
# In db/capture_runs.py:
def get_draft_runs(self, game_id: str) -> list[str]:
    """Return IDs of draft capture runs for a game, newest first."""
    rows = self.conn.execute(
        "SELECT id FROM capture_runs WHERE game_id = ? AND draft = 1 ORDER BY created_at DESC",
        (game_id,),
    ).fetchall()
    return [r[0] for r in rows]

def count_active_segments(self, reference_id: str) -> int:
    """Count active segments for a capture run."""
    row = self.conn.execute(
        "SELECT COUNT(*) FROM segments WHERE reference_id = ? AND active = 1",
        (reference_id,),
    ).fetchone()
    return row[0]
```

**Category:** Encapsulation fix, not error handling.

---

### Fix 13: Manifest import empty fallback

**File:** `python/spinlab/manifest.py`, line 46

**Problem:** `manifest.get("segments", manifest.get("splits", []))` — if neither key exists, silently imports zero segments.

**Fix:** Raise if neither key found.

```python
entries = manifest.get("segments") or manifest.get("splits")
if entries is None:
    raise KeyError("Manifest missing 'segments' (or legacy 'splits') key")
```

**Category:** Raise — malformed input should fail visibly.

---

### Fix 14: Practice session no segment_id validation

**File:** `python/spinlab/practice.py`, around line 110

**Problem:** `attempt_result` from Lua is processed without checking that the segment_id matches what was sent via `practice_load`.

**Fix:** Validate before processing.

```python
result_seg_id = self._result_data.get("segment_id")
if result_seg_id != cmd.id:
    raise ValueError(
        f"Segment ID mismatch: sent {cmd.id}, got {result_seg_id!r}"
    )
```

Note: This requires Lua to echo back the `segment_id` in `attempt_result`. If Lua doesn't currently send it, add it to the Lua side as part of this fix.

**Category:** Raise — logic desync between Lua and Python is a bug.

---

### Fix 15: Cascading delete not wrapped in transaction

**File:** `python/spinlab/db/capture_runs.py`, `hard_delete_capture_run()`

**Problem:** Five DELETE statements with a single `conn.commit()` at the end. Partial failure leaves DB inconsistent.

**Fix:** Wrap in `transaction()` context manager.

```python
def hard_delete_capture_run(self, run_id: str) -> None:
    with self.transaction():
        seg_ids = [...]
        self.conn.execute("DELETE FROM segment_variants ...")
        self.conn.execute("DELETE FROM model_state ...")
        self.conn.execute("DELETE FROM attempts ...")
        self.conn.execute("DELETE FROM segments ...")
        self.conn.execute("DELETE FROM capture_runs ...")
```

**Category:** Data integrity — partial deletes corrupt the DB.

---

### Fix 16: set_active_capture_run silent no-op

**File:** `python/spinlab/db/capture_runs.py`, `set_active_capture_run()`

**Problem:** If `run_id` doesn't exist, silently returns without doing anything.

**Fix:** Raise.

```python
if not row:
    raise ValueError(f"Capture run not found: {run_id}")
```

**Category:** Raise — caller expects this to succeed.

---

## Summary

| Fix | File | Category | Risk |
|-----|------|----------|------|
| 1 | cli.py | Print + exit | None |
| 2 | dashboard.py | Narrow catch | Low — might surface latent bugs |
| 3 | tcp_manager.py | Raise on bad JSON | Low |
| 4 | tcp_manager.py | Log ERROR | None |
| 5 | practice.py | Log ERROR | None |
| 6 | sse.py | Log WARNING | None |
| 7 | scheduler.py | Raise on corrupt data | Medium — will crash if DB has bad data |
| 8 | scheduler.py | Log ERROR | None |
| 9 | session_manager.py | Raise on missing ROM | Low |
| 10 | session_manager.py | Raise on corrupt data | Medium |
| 11 | draft_manager.py | Log WARNING | None |
| 12 | draft_manager.py | Add DB methods | None |
| 13 | manifest.py | Raise on bad input | Low |
| 14 | practice.py | Raise on ID mismatch | Low |
| 15 | capture_runs.py | Wrap in transaction | None |
| 16 | capture_runs.py | Raise on missing run | Low |

## Testing

- Fixes 1-6, 8, 11: No new tests needed — these add logging/errors to paths that were silent.
- Fix 7, 10: Add a test that corrupt model state raises instead of being skipped.
- Fix 9: Add a test that missing ROM raises FileNotFoundError.
- Fix 12: Add tests for new DB methods `get_draft_runs()`, `count_active_segments()`.
- Fix 13: Add a test that missing segments key raises KeyError.
- Fix 14: Add a test that segment_id mismatch raises ValueError.
- Fix 15: Verify existing tests pass with transaction wrapper.
- Fix 16: Add a test that missing run_id raises ValueError.
