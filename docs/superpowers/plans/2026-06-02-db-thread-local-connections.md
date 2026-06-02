# DB Thread-Local Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the intermittent "Segment not found" 404s (and the ~3s UI lag) by giving each thread its own SQLite connection, so concurrent reads/writes never share one connection.

**Architecture:** `DatabaseCore` currently holds a single `sqlite3.Connection` (`check_same_thread=False`, autocommit, WAL) shared by every thread. Sync FastAPI routes run in Starlette's threadpool concurrently with the practice/recording loop, and concurrent statement execution on one shared connection intermittently returns an empty result for a committed row. Fix: make `conn` a property backed by `threading.local()` so each thread lazily gets its own connection (WAL already supports N readers + 1 writer across connections); add `PRAGMA busy_timeout` so a contended writer waits instead of erroring. `:memory:` databases are per-connection, so they keep a single shared connection (tests are single-threaded and never hit the bug).

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `threading.local`; pytest.

**Spec/context:** Root cause confirmed in this session (isolation repro: 3 spurious misses + `sqlite3.InterfaceError` on a permanently-present row). Memory: `project_db_shared_connection_concurrency`. No separate spec doc — this plan is the design record (brainstormed + approved 2026-06-02, Approach A).

---

## Key facts about existing code (verified)

- `python/spinlab/db/core.py` `DatabaseCore.__init__` (lines 37–50) sets `self.conn = sqlite3.connect(..., check_same_thread=False)`, then `row_factory = sqlite3.Row`, `isolation_level = None`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, then `run_migrations(self.conn)`. `close()` (52–53) calls `self.conn.close()`.
- `transaction()` (55–83) reads `self.conn.in_transaction` and issues `BEGIN IMMEDIATE`/`SAVEPOINT`/`COMMIT`/`ROLLBACK` on `self.conn`. Making `conn` a property keeps this working **per-thread** (each thread's transaction is scoped to its own connection).
- All domain mixins (`SegmentsMixin`, `AttemptsMixin`, …) and `Database` (in `db/__init__.py`, mixins + `DatabaseCore` last in MRO) reference `self.conn`. A property on `DatabaseCore` serves all of them via the MRO.
- `db.get_segment_by_id(id)` = plain `SELECT * FROM segments WHERE id = ?` (the read that 404'd). Used as the test's probe.
- Tests construct `Database(tmp_path/"x.db")` (file) or `Database(":memory:")`. Existing db tests live in `tests/unit/db/`.

## File Structure

**New files:**
- `tests/unit/db/test_db_concurrency.py` — concurrency regression test + `:memory:` sharing + transaction-still-works tests.

**Modified files:**
- `python/spinlab/db/core.py` — `__init__`, new `_configure`/`_new_conn` helpers, `conn` property, `close()`; add `import threading` and a `_BUSY_TIMEOUT_MS` constant.

---

## Task 1: Concurrency regression test (Red)

**Files:**
- Create: `tests/unit/db/test_db_concurrency.py`

- [ ] **Step 1: Write the failing test.** Create `tests/unit/db/test_db_concurrency.py`:

```python
"""Concurrency regression: one Database used from many threads must never
return an empty result for a row that is permanently present.

Pre-fix (single shared sqlite connection across threads) this fails with
spurious None results and/or sqlite3.InterfaceError. Post-fix (thread-local
connections) reads are isolated per thread and it passes deterministically.
"""
from __future__ import annotations

import threading

from spinlab.db import Database
from spinlab.models import Segment


def _seg(seg_id: str) -> Segment:
    return Segment(
        id=seg_id, game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True,
    )


def test_concurrent_reads_never_miss_a_present_row(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(_seg(seg_id))
    assert db.get_segment_by_id(seg_id) is not None  # present before threads start

    stop = threading.Event()
    misses: list[str] = []
    errors: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                reads[0] += 1
                if db.get_segment_by_id(seg_id) is None:
                    misses.append("None")
            except Exception as e:  # InterfaceError etc.
                errors.append(repr(e))

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                db.update_segment(seg_id, description=f"d{i}")
                db.upsert_segment(_seg(seg_id))
            except Exception as e:
                errors.append(repr(e))

    threads = [threading.Thread(target=reader) for _ in range(4)] + \
              [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    # Spin enough iterations to exercise interleaving without a wall-clock sleep
    # dependency: stop once readers have done a lot of work.
    while reads[0] < 40000 and not errors:
        pass
    stop.set()
    for t in threads:
        t.join()

    assert db.get_segment_by_id(seg_id) is not None  # still present at the end
    assert errors == [], f"DB raised under concurrency: {errors[:3]}"
    assert misses == [], f"{len(misses)} spurious empty reads of a present row"
    db.close()
```

- [ ] **Step 2: Run it against current code — expect FAIL.**

Run: `python -m pytest tests/unit/db/test_db_concurrency.py -q`
Expected: FAIL — either `spurious empty reads` or a `sqlite3.InterfaceError` in `errors`. (Probabilistic: if it passes once, re-run 2–3×; it fails reliably under this load on the shared-connection code.)

- [ ] **Step 3: Commit the red test.**

```bash
git add tests/unit/db/test_db_concurrency.py
git commit -m "test(db): concurrency regression — present row must never read empty"
```

---

## Task 2: Thread-local connections (Green)

**Files:**
- Modify: `python/spinlab/db/core.py`

- [ ] **Step 1: Add imports + the busy-timeout constant.** At the top of `python/spinlab/db/core.py`, add `import threading` next to the existing `import sqlite3`, and add this constant after the `_savepoint_counter` line (line ~33):

```python
# A contended writer waits this long for the WAL write lock before raising
# SQLITE_BUSY. Sized well above a normal write batch (a few statements,
# << 100ms) so routine contention just waits, yet short enough that a genuine
# deadlock still surfaces as an error rather than hanging forever.
_BUSY_TIMEOUT_MS = 5000
```

- [ ] **Step 2: Replace `__init__` and `close`, add `_configure`/`_new_conn`/`conn`.** Replace the current `__init__` (lines 37–50) and `close` (52–53) with:

```python
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # File DBs: one connection per thread (no cross-thread sharing → no
        # concurrent-use corruption). :memory: DBs are per-connection, so they
        # MUST share one connection or each thread would see an empty database;
        # tests are single-threaded and never hit the concurrency bug.
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._shared_conn: sqlite3.Connection | None = None
        conn = self._new_conn()
        run_migrations(conn)  # runs once; file is shared, so all conns see the schema
        if self._is_memory:
            self._shared_conn = conn
        else:
            self._local.conn = conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        # Autocommit: lets BEGIN / COMMIT / ROLLBACK / SAVEPOINT do what they
        # say in the SQL we issue (see transaction() and run_migrations()).
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._configure(conn)
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._is_memory:
            assert self._shared_conn is not None
            return self._shared_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._shared_conn = None
        if hasattr(self._local, "conn"):
            del self._local.conn
```

Leave `transaction()` and everything below it unchanged — they use `self.conn`, which now resolves per-thread.

- [ ] **Step 3: Run the regression test — expect PASS.**

Run: `python -m pytest tests/unit/db/test_db_concurrency.py -q`
Expected: PASS (0 misses, 0 errors), deterministically.

- [ ] **Step 4: Run the full db unit suite — no regressions.**

Run: `python -m pytest tests/unit/db/ -q`
Expected: all pass (file-backed and `:memory:` tests).

- [ ] **Step 5: Commit.**

```bash
git add python/spinlab/db/core.py
git commit -m "fix(db): thread-local connections + busy_timeout (no shared-conn concurrency)"
```

---

## Task 3: Guard the carve-out + transaction semantics

**Files:**
- Modify: `tests/unit/db/test_db_concurrency.py`

- [ ] **Step 1: Add a `:memory:` sharing test and a transaction-rollback test.** Append to `tests/unit/db/test_db_concurrency.py`:

```python
def test_memory_db_shares_state_across_operations():
    # :memory: keeps ONE shared connection; writes are visible to later reads
    # on the same Database (would be invisible if each call got a fresh
    # per-connection in-memory db).
    db = Database(":memory:")
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(_seg(seg_id))
    assert db.get_segment_by_id(seg_id) is not None
    db.close()


def test_transaction_rollback_still_works(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    try:
        with db.transaction():
            db.upsert_segment(_seg(seg_id))
            raise RuntimeError("boom")  # force rollback
    except RuntimeError:
        pass
    assert db.get_segment_by_id(seg_id) is None  # rolled back
    db.close()
```

- [ ] **Step 2: Run them — expect PASS.**

Run: `python -m pytest tests/unit/db/test_db_concurrency.py -q`
Expected: 3 passed.

- [ ] **Step 3: Commit.**

```bash
git add tests/unit/db/test_db_concurrency.py
git commit -m "test(db): :memory: sharing + transaction rollback under thread-local conns"
```

---

## Task 4: Full gate + stress verification

**Files:** none (verification only).

- [ ] **Step 1: Fast suite.**

Run: `python -m pytest -m "not emulator" -q`
Expected: green (≈790 tests + the 3 new). Requires `cd frontend && npm run build` first if the frontend smoke bundle is stale.

- [ ] **Step 2: Static analysis on the changed module.**

Run: `npx pyright python/spinlab/db/core.py`
Expected: no new errors.

- [ ] **Step 3: Stress the new concurrency test (flake bar).** Per the project rule that one green run is noise, run it 15× sequentially:

Run (PowerShell): `1..15 | ForEach-Object { python -m pytest tests/unit/db/test_db_concurrency.py -q }`
Expected: 15/15 green. Any failure → stop and investigate (do not dismiss as flake without a written entry).

- [ ] **Step 4: Full unfiltered gate (merge rule).** REQUIRES the live dashboard stopped (it binds NCI 55355 + holds the DB) — coordinate with Andrew before running.

Run: `python -m pytest`
Expected: green, count up by 3.

- [ ] **Step 5: Final commit (if anything outstanding).**

```bash
git add -A
git commit -m "test(db): verify thread-local connection fix under full gate"
```

---

## Self-review notes

- **Root-cause coverage:** the shared connection is the only thing that changes; Task 1's test fails pre-fix (the exact symptom) and passes post-fix — direct evidence the fix addresses the cause, not a symptom.
- **`transaction()` unchanged & correct:** it operates on `self.conn` (now per-thread), so a transaction is scoped to the thread that opened it; the global savepoint counter still yields unique names (uniqueness only matters within a connection). Task 3 guards rollback.
- **`:memory:` carve-out justified, not a fudge:** memory DBs are per-connection by SQLite design; sharing one connection is the only correct choice, and tests never run concurrent DB access so the bug can't recur there. Guarded by `test_memory_db_shares_state_across_operations`.
- **busy_timeout:** named constant with rationale; handles the new possibility of two threads writing at once (WAL = 1 writer) by waiting rather than raising.
- **No magic numbers, no silent fallbacks:** the only swallowed exceptions are in `close()` (best-effort teardown), consistent with the prior `close()`.
- **Snappiness:** removing shared-connection contention should also relieve the ~3s lag; if lag persists after this lands, it points at the per-SSE-push double-fetch / synchronous MC evaluate (tracked separately in the overhaul memory), not the connection model.
```
