# Dashboard PoC Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web dashboard that shows live practice session state alongside Mesen2, polling a shared SQLite DB and orchestrator state file.

**Architecture:** FastAPI serves a JSON API and static HTML/CSS/JS. The orchestrator writes a `data/orchestrator_state.json` file on each split change. The dashboard reads this file + the DB to serve `/api/state` and `/api/splits`. The HTML page polls `/api/state` every ~1s to auto-update. No WebSocket, no JS framework.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, SQLite (existing `db.py`), vanilla HTML/CSS/JS, pytest, httpx (test client)

---

## File Structure

| File | Role |
|------|------|
| `python/spinlab/dashboard.py` | Create — FastAPI app, all API endpoints, server startup |
| `python/spinlab/static/index.html` | Create — Single-page HTML (Live view only for PoC) |
| `python/spinlab/static/style.css` | Create — Dark theme, 320px narrow column |
| `python/spinlab/static/app.js` | Create — Polling logic, DOM updates |
| `tests/test_dashboard.py` | Create — API endpoint tests using FastAPI TestClient |
| `tests/test_db_dashboard.py` | Create — Tests for new DB query methods |
| `tests/test_scheduler_peek.py` | Create — Tests for `peek_next_n` |
| `tests/test_orchestrator_state.py` | Create — Tests for state file writing |
| `python/spinlab/db.py` | Modify — Add 7 new query methods (lines 268+) |
| `python/spinlab/scheduler.py` | Modify — Add `peek_next_n(n)` method (line 39+) |
| `python/spinlab/orchestrator.py` | Modify — Write state file on each split change |
| `python/spinlab/cli.py` | Modify — Add `dashboard` subcommand |
| `pyproject.toml` | Modify — Add `fastapi`, `uvicorn`, `httpx` dependencies |
| `scripts/spinlab.ahk` | Modify — Ctrl+Alt+W also launches dashboard |

---

## Chunk 1: Data Layer — DB Queries + Scheduler Peek

### Task 1: Add dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add fastapi, uvicorn, httpx to pyproject.toml**

```toml
# In [project] section, change:
dependencies = ["pyyaml"]
# To:
dependencies = ["pyyaml", "fastapi", "uvicorn"]

# In [project.optional-dependencies], change:
dev = ["pytest"]
# To:
dev = ["pytest", "httpx"]
```

- [ ] **Step 2: Install updated dependencies**

Run: `pip install -e ".[dev]"`
Expected: Success, fastapi/uvicorn/httpx installed

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat(dashboard): add fastapi, uvicorn, httpx dependencies"
```

---

### Task 2: DB query methods — `get_current_session` and `get_split_attempt_count`

**Files:**
- Create: `tests/test_db_dashboard.py`
- Modify: `python/spinlab/db.py:268+`

- [ ] **Step 1: Write failing tests for `get_current_session` and `get_split_attempt_count`**

```python
# tests/test_db_dashboard.py
"""Tests for dashboard-specific DB queries."""
import pytest
from spinlab.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


def test_get_current_session_returns_active(db):
    db.create_session("sess1", "test_game")
    result = db.get_current_session("test_game")
    assert result is not None
    assert result["id"] == "sess1"
    assert result["ended_at"] is None


def test_get_current_session_ignores_ended(db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 5, 3)
    result = db.get_current_session("test_game")
    assert result is None


def test_get_split_attempt_count(db):
    from spinlab.models import Split, Attempt, Rating
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for _ in range(3):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=True,
            time_ms=1000, rating=Rating.GOOD,
        ))
    db.log_attempt(Attempt(
        split_id="s1", session_id="other_sess", completed=True,
        time_ms=1000, rating=Rating.GOOD,
    ))
    assert db.get_split_attempt_count("s1", "sess1") == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db_dashboard.py -v`
Expected: FAIL — `get_current_session` and `get_split_attempt_count` not defined

- [ ] **Step 3: Implement `get_current_session` and `get_split_attempt_count` in db.py**

Add after `end_session` method (after line 266 in `db.py`):

```python
    def get_current_session(self, game_id: str) -> Optional[dict]:
        """Get active session (ended_at IS NULL)."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE game_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (game_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_split_attempt_count(self, split_id: str, session_id: str) -> int:
        """Count attempts on a split in a specific session."""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM attempts "
            "WHERE split_id = ? AND session_id = ?",
            (split_id, session_id),
        ).fetchone()
        return row["cnt"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_dashboard.py
git commit -m "feat(db): add get_current_session and get_split_attempt_count"
```

---

### Task 3: DB query methods — `get_recent_attempts` and `get_all_splits_with_schedule`

**Files:**
- Modify: `tests/test_db_dashboard.py`
- Modify: `python/spinlab/db.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_db_dashboard.py`:

```python
def test_get_recent_attempts(db):
    from spinlab.models import Split, Attempt, Rating
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal", description="Level 1")
    db.upsert_split(split)
    db.create_session("sess1", "test_game")
    for i in range(10):
        db.log_attempt(Attempt(
            split_id="s1", session_id="sess1", completed=(i % 2 == 0),
            time_ms=1000 + i * 100, rating=Rating.GOOD,
        ))
    results = db.get_recent_attempts("test_game", limit=5)
    assert len(results) == 5
    # Most recent first (last inserted has highest time_ms = 1900)
    assert results[0]["time_ms"] == 1900
    # Joined with split info
    assert results[0]["goal"] == "normal"


def test_get_all_splits_with_schedule(db):
    from spinlab.models import Split
    s1 = Split(id="s1", game_id="test_game", level_number=1,
               room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2,
               room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    db.ensure_schedule("s1")
    db.ensure_schedule("s2")
    results = db.get_all_splits_with_schedule("test_game")
    assert len(results) == 2
    # Has schedule fields
    assert "ease_factor" in results[0]
    # Grouped by level_number
    assert results[0]["level_number"] <= results[1]["level_number"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db_dashboard.py::test_get_recent_attempts tests/test_db_dashboard.py::test_get_all_splits_with_schedule -v`
Expected: FAIL

- [ ] **Step 3: Implement both methods in db.py**

Add after `get_split_attempt_count`:

```python
    def get_recent_attempts(self, game_id: str, limit: int = 8) -> list[dict]:
        """Last N attempts joined with split info, most recent first."""
        rows = self.conn.execute(
            """SELECT a.*, s.goal, s.description, s.level_number,
                      s.reference_time_ms
               FROM attempts a
               JOIN splits s ON a.split_id = s.id
               WHERE s.game_id = ?
               ORDER BY a.created_at DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_splits_with_schedule(self, game_id: str) -> list[dict]:
        """All splits joined with schedule, ordered by level_number."""
        rows = self.conn.execute(
            """SELECT s.*, sch.ease_factor, sch.interval_minutes,
                      sch.repetitions, sch.next_review
               FROM splits s
               LEFT JOIN schedule sch ON s.id = sch.split_id
               WHERE s.game_id = ?
               ORDER BY s.level_number, s.room_id""",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_dashboard.py
git commit -m "feat(db): add get_recent_attempts and get_all_splits_with_schedule"
```

---

### Task 4: DB query method — `get_session_history`

**Files:**
- Modify: `tests/test_db_dashboard.py`
- Modify: `python/spinlab/db.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_db_dashboard.py`:

```python
def test_get_session_history(db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    db.create_session("sess2", "test_game")
    db.end_session("sess2", 5, 4)
    db.create_session("sess3", "test_game")  # still active
    results = db.get_session_history("test_game", limit=5)
    assert len(results) == 3
    # Most recent first
    assert results[0]["id"] == "sess3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_dashboard.py::test_get_session_history -v`
Expected: FAIL

- [ ] **Step 3: Implement `get_session_history`**

Add after `get_all_splits_with_schedule`:

```python
    def get_session_history(self, game_id: str, limit: int = 10) -> list[dict]:
        """Recent sessions, most recent first."""
        rows = self.conn.execute(
            """SELECT * FROM sessions
               WHERE game_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py tests/test_db_dashboard.py
git commit -m "feat(db): add get_session_history"
```

---

### Task 5: `Scheduler.peek_next_n(n)` + DB helper

**Files:**
- Create: `tests/test_scheduler_peek.py`
- Modify: `python/spinlab/db.py` — add `get_all_scheduled_split_ids`
- Modify: `python/spinlab/scheduler.py:38+`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_scheduler_peek.py
"""Tests for Scheduler.peek_next_n()."""
import pytest
from spinlab.db import Database
from spinlab.models import Split
from spinlab.scheduler import Scheduler


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


def seed_splits(db, n):
    for i in range(n):
        s = Split(id=f"s{i}", game_id="test_game", level_number=i,
                  room_id=0, goal="normal", state_path=f"/state_{i}.mss")
        db.upsert_split(s)
        db.ensure_schedule(s.id)


def test_peek_returns_requested_count(db):
    seed_splits(db, 5)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert len(result) == 3


def test_peek_returns_less_if_fewer_available(db):
    seed_splits(db, 2)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(5)
    assert len(result) == 2


def test_peek_returns_empty_with_no_splits(db):
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert result == []


def test_peek_returns_split_ids(db):
    seed_splits(db, 3)
    sched = Scheduler(db, "test_game")
    result = sched.peek_next_n(3)
    assert all(isinstance(sid, str) for sid in result)
    assert all(sid.startswith("s") for sid in result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler_peek.py -v`
Expected: FAIL — `peek_next_n` not defined

- [ ] **Step 3a: Add `get_all_scheduled_split_ids` to db.py**

Add after `get_session_history` in `db.py`:

```python
    def get_all_scheduled_split_ids(self, game_id: str) -> list[str]:
        """All active split IDs ordered by next_review (soonest first)."""
        rows = self.conn.execute(
            """SELECT s.id FROM splits s
               JOIN schedule sch ON s.id = sch.split_id
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY sch.next_review ASC""",
            (game_id,),
        ).fetchall()
        return [row["id"] for row in rows]
```

- [ ] **Step 3b: Implement `peek_next_n` in scheduler.py**

Add after `pick_next` method (after line 38 in `scheduler.py`):

```python
    def peek_next_n(self, n: int) -> list[str]:
        """Return the next N split IDs in priority order, without side effects.

        Used by the dashboard to show the upcoming queue.
        """
        now = datetime.utcnow()

        # Overdue first, then upcoming
        due = self.db.get_due_splits(self.game_id, now)
        if len(due) >= n:
            return [d["id"] for d in due[:n]]

        ids = [d["id"] for d in due]

        # Fill remaining from upcoming (all splits ordered by next_review)
        if len(ids) < n:
            for sid in self.db.get_all_scheduled_split_ids(self.game_id):
                if sid not in ids:
                    ids.append(sid)
                    if len(ids) >= n:
                        break

        return ids[:n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler_peek.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py python/spinlab/scheduler.py tests/test_scheduler_peek.py
git commit -m "feat(scheduler): add peek_next_n for dashboard queue preview"
```

---

## Chunk 2: Orchestrator State File + Dashboard API

### Task 6: Orchestrator writes state file

**Files:**
- Create: `tests/test_orchestrator_state.py`
- Modify: `python/spinlab/orchestrator.py`

- [ ] **Step 1: Write failing tests for state file writing**

```python
# tests/test_orchestrator_state.py
"""Tests for orchestrator state file writing."""
import json
import pytest
from pathlib import Path
from spinlab.orchestrator import write_state_file, clear_state_file


def test_write_state_file_creates_json(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(
        state_path,
        session_id="abc123",
        started_at="2026-03-12T15:30:00Z",
        current_split_id="smw_cod:44:0:normal",
        queue=["smw_cod:56:1:normal", "smw_cod:58:0:key"],
    )
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["session_id"] == "abc123"
    assert data["started_at"] == "2026-03-12T15:30:00Z"
    assert data["current_split_id"] == "smw_cod:44:0:normal"
    assert len(data["queue"]) == 2
    assert "updated_at" in data


def test_write_state_file_atomic(tmp_path):
    """The .tmp file should not linger after a successful write."""
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(state_path, "s1", "2026-03-12T15:30:00Z", "split1", [])
    assert not (tmp_path / "orchestrator_state.json.tmp").exists()


def test_clear_state_file_removes(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    write_state_file(state_path, "s1", "2026-03-12T15:30:00Z", "split1", [])
    clear_state_file(state_path)
    assert not state_path.exists()


def test_clear_state_file_noop_if_missing(tmp_path):
    state_path = tmp_path / "orchestrator_state.json"
    clear_state_file(state_path)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator_state.py -v`
Expected: FAIL — `write_state_file` and `clear_state_file` not defined

- [ ] **Step 3: Implement `write_state_file` and `clear_state_file`**

Add to `python/spinlab/orchestrator.py` after the imports (after line 17), before `_parse_attempt_result_from_buffer`:

```python
from datetime import datetime


def write_state_file(
    path: Path,
    session_id: str,
    started_at: str,
    current_split_id: str,
    queue: list[str],
) -> None:
    """Atomically write orchestrator state for dashboard consumption."""
    state = {
        "session_id": session_id,
        "started_at": started_at,
        "current_split_id": current_split_id,
        "queue": queue,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def clear_state_file(path: Path) -> None:
    """Remove state file when session ends."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator_state.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/orchestrator.py tests/test_orchestrator_state.py
git commit -m "feat(orchestrator): add write_state_file and clear_state_file"
```

---

### Task 7: Wire state file into the orchestrator's main loop

**Files:**
- Modify: `python/spinlab/orchestrator.py:110-208`

No new test — this is integration wiring. The `write_state_file`/`clear_state_file` functions are already tested.

- [ ] **Step 1: Add state file writes to the `run()` function**

In `python/spinlab/orchestrator.py`, make these changes to the `run()` function:

**After line 120** (`data_dir = Path(...)`), add:
```python
    state_file = data_dir / "orchestrator_state.json"
```

**After line 150** (`db.create_session(session_id, game_id)`), capture started_at:
```python
    session_started_at = datetime.utcnow().isoformat() + "Z"
```

**Inside the while loop, after line 172** (`send_line(sock, "practice_load:" + ...)`), add:
```python
            queue = scheduler.peek_next_n(3)
            # Remove current split from queue if present
            queue = [q for q in queue if q != cmd.id][:2]
            write_state_file(state_file, session_id, session_started_at, cmd.id, queue)
```

**In the `finally` block, after line 206** (`db.end_session(...)`), add:
```python
        clear_state_file(state_file)
```

- [ ] **Step 2: Run existing orchestrator tests to verify nothing broke**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add python/spinlab/orchestrator.py
git commit -m "feat(orchestrator): write state file on each split change"
```

---

### Task 8: FastAPI dashboard app — skeleton + `/api/state`

**Files:**
- Create: `python/spinlab/dashboard.py`
- Create: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing tests for `/api/state`**

```python
# tests/test_dashboard.py
"""Tests for dashboard API endpoints."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from spinlab.db import Database
from spinlab.models import Split, Attempt, Rating


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.upsert_game("test_game", "Test Game", "any%")
    return d


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "orchestrator_state.json"


@pytest.fixture
def client(db, state_file, tmp_path):
    from spinlab.dashboard import create_app
    app = create_app(db=db, game_id="test_game", state_file=state_file)
    return TestClient(app)


def test_api_state_no_session(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "idle"
    assert data["current_split"] is None


def test_api_state_with_active_session(client, db, state_file):
    split = Split(id="s1", game_id="test_game", level_number=1,
                  room_id=0, goal="normal", description="Level 1",
                  reference_time_ms=5000)
    db.upsert_split(split)
    db.ensure_schedule("s1")
    db.create_session("sess1", "test_game")

    state_file.write_text(json.dumps({
        "session_id": "sess1",
        "current_split_id": "s1",
        "queue": [],
        "updated_at": "2026-03-12T15:30:00Z",
    }))

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "practice"
    assert data["current_split"]["id"] == "s1"
    assert data["session"]["id"] == "sess1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dashboard.py -v`
Expected: FAIL — `spinlab.dashboard` does not exist

- [ ] **Step 3a: Add `get_split_with_schedule` and `get_splits_with_schedule_by_ids` to db.py**

Add after `get_all_scheduled_split_ids` in `db.py`:

```python
    def get_split_with_schedule(self, split_id: str) -> Optional[dict]:
        """Single split joined with its schedule data."""
        row = self.conn.execute(
            """SELECT s.*, sch.ease_factor, sch.interval_minutes,
                      sch.repetitions, sch.next_review
               FROM splits s
               LEFT JOIN schedule sch ON s.id = sch.split_id
               WHERE s.id = ?""",
            (split_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_splits_summary_by_ids(self, split_ids: list[str]) -> list[dict]:
        """Get summary dicts for a list of split IDs, preserving order."""
        if not split_ids:
            return []
        placeholders = ",".join("?" for _ in split_ids)
        rows = self.conn.execute(
            f"""SELECT s.id, s.goal, s.description, s.level_number,
                       sch.ease_factor, sch.repetitions
                FROM splits s
                LEFT JOIN schedule sch ON s.id = sch.split_id
                WHERE s.id IN ({placeholders})""",
            split_ids,
        ).fetchall()
        by_id = {r["id"]: dict(r) for r in rows}
        return [by_id[sid] for sid in split_ids if sid in by_id]
```

- [ ] **Step 3b: Implement dashboard.py with `create_app` and `/api/state`**

```python
# python/spinlab/dashboard.py
"""SpinLab dashboard — FastAPI web app for live stats and management."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import Database


def _read_state_file(path: Path) -> Optional[dict]:
    """Read orchestrator state file, returning None if missing/invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def create_app(
    db: Database,
    game_id: str,
    state_file: Path,
) -> FastAPI:
    app = FastAPI(title="SpinLab Dashboard")

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/api/state")
    def api_state():
        orch_state = _read_state_file(state_file)
        session = db.get_current_session(game_id)

        if not session:
            return {
                "mode": "idle",
                "current_split": None,
                "queue": [],
                "recent": [],
                "session": None,
            }

        mode = "practice" if orch_state else "reference"

        current_split = None
        queue = []
        if orch_state:
            split_id = orch_state.get("current_split_id")
            if split_id:
                row = db.get_split_with_schedule(split_id)
                if row:
                    row["attempt_count"] = db.get_split_attempt_count(
                        split_id, session["id"]
                    )
                    current_split = row

            queue = db.get_splits_summary_by_ids(
                orch_state.get("queue", [])
            )

        recent = db.get_recent_attempts(game_id, limit=8)

        return {
            "mode": mode,
            "current_split": current_split,
            "queue": queue,
            "recent": recent,
            "session": dict(session),
        }

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db.py python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): create FastAPI app with /api/state endpoint"
```

---

### Task 9: `/api/splits` endpoint

**Files:**
- Modify: `tests/test_dashboard.py`
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Write failing test for `/api/splits`**

Append to `tests/test_dashboard.py`:

```python
def test_api_splits_returns_all_with_schedule(client, db):
    s1 = Split(id="s1", game_id="test_game", level_number=1,
               room_id=0, goal="normal")
    s2 = Split(id="s2", game_id="test_game", level_number=2,
               room_id=0, goal="key")
    db.upsert_split(s1)
    db.upsert_split(s2)
    db.ensure_schedule("s1")
    db.ensure_schedule("s2")
    resp = client.get("/api/splits")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["splits"]) == 2
    assert "ease_factor" in data["splits"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py::test_api_splits_returns_all_with_schedule -v`
Expected: FAIL — 404

- [ ] **Step 3: Add `/api/splits` endpoint to dashboard.py**

Add inside `create_app`, after the `api_state` function:

```python
    @app.get("/api/splits")
    def api_splits():
        splits = db.get_all_splits_with_schedule(game_id)
        return {"splits": splits}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add /api/splits endpoint"
```

---

### Task 10: `/api/sessions` endpoint

**Files:**
- Modify: `tests/test_dashboard.py`
- Modify: `python/spinlab/dashboard.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_dashboard.py`:

```python
def test_api_sessions_returns_history(client, db):
    db.create_session("sess1", "test_game")
    db.end_session("sess1", 10, 8)
    db.create_session("sess2", "test_game")
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sessions"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py::test_api_sessions_returns_history -v`
Expected: FAIL — 404

- [ ] **Step 3: Add `/api/sessions` endpoint**

Add inside `create_app`:

```python
    @app.get("/api/sessions")
    def api_sessions():
        sessions = db.get_session_history(game_id)
        return {"sessions": sessions}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add /api/sessions endpoint"
```

---

## Chunk 3: CLI + Static Files + AHK

### Task 11: `spinlab dashboard` CLI subcommand

**Files:**
- Modify: `python/spinlab/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_cli.py`:

```python
def test_dashboard_subcommand_imports():
    """Dashboard subcommand is registered and dashboard module is importable."""
    from spinlab import dashboard
    assert hasattr(dashboard, "create_app")
```

- [ ] **Step 2: Run test to verify it passes (create_app already exists)**

Run: `pytest tests/test_cli.py::test_dashboard_subcommand_imports -v`
Expected: PASS (this validates the module exists)

- [ ] **Step 3: Add dashboard subcommand to cli.py**

In `python/spinlab/cli.py`, add after the `lua-cmd` subparser (after line 30):

```python
    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    p_dash.add_argument(
        "--port", type=int, default=15483, help="Dashboard port"
    )
```

Add the handler after the `lua-cmd` elif block (after line 53):

```python
    elif parsed.command == "dashboard":
        import uvicorn
        import yaml
        from spinlab.dashboard import create_app
        from spinlab.db import Database

        with open(parsed.config, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        game_id = config["game"]["id"]
        data_dir = Path(config["data"]["dir"])
        db = Database(data_dir / "spinlab.db")
        state_file = data_dir / "orchestrator_state.json"
        app = create_app(db=db, game_id=game_id, state_file=state_file)
        print(f"SpinLab Dashboard: http://localhost:{parsed.port}")
        uvicorn.run(app, host="127.0.0.1", port=parsed.port, log_level="warning")
```

- [ ] **Step 4: Run existing CLI tests to verify nothing broke**

Run: `pytest tests/test_cli.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cli.py tests/test_cli.py
git commit -m "feat(cli): add dashboard subcommand"
```

---

### Task 12: HTML page — index.html served at `/`

**Files:**
- Modify: `python/spinlab/dashboard.py`
- Create: `python/spinlab/static/index.html`

- [ ] **Step 1: Write test for `GET /` returning HTML**

Append to `tests/test_dashboard.py`:

```python
def test_root_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SpinLab" in resp.text
```

- [ ] **Step 2: Add root route to serve index.html**

Add to `create_app` in `dashboard.py`, after the static mount:

```python
    from fastapi.responses import FileResponse

    @app.get("/")
    def root():
        return FileResponse(str(static_dir / "index.html"))
```

- [ ] **Step 3: Create the static directory**

Run: `mkdir -p python/spinlab/static`

- [ ] **Step 4: Create index.html**

```html
<!-- python/spinlab/static/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=320">
  <title>SpinLab</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <div id="app">
    <header>
      <h1>SpinLab</h1>
      <div id="session-timer" class="header-stat"></div>
    </header>

    <section id="mode-idle" class="mode-panel">
      <p class="status-msg">No active session</p>
    </section>

    <section id="mode-reference" class="mode-panel" hidden>
      <p class="status-msg">Reference Run</p>
      <p id="ref-count" class="sub-stat"></p>
    </section>

    <section id="mode-practice" class="mode-panel" hidden>
      <div id="current-split" class="card">
        <div class="card-label">Current</div>
        <div id="cs-goal" class="cs-goal"></div>
        <div id="cs-difficulty" class="cs-difficulty"></div>
        <div id="cs-attempts" class="cs-attempts"></div>
      </div>

      <div id="model-insight" class="card">
        <div class="card-label">Insight</div>
        <div id="mi-tier" class="mi-line"></div>
      </div>

      <div id="up-next" class="card">
        <div class="card-label">Up Next</div>
        <ul id="queue-list"></ul>
      </div>

      <div id="recent-results" class="card">
        <div class="card-label">Recent</div>
        <ul id="recent-list"></ul>
      </div>
    </section>

    <footer id="session-stats">
      <span id="stats-line"></span>
    </footer>
  </div>

  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 5: Run tests to verify root route works**

Run: `pytest tests/test_dashboard.py::test_root_serves_html -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/static/index.html
git commit -m "feat(dashboard): add index.html and root route"
```

---

### Task 13: Dark theme CSS

**Files:**
- Create: `python/spinlab/static/style.css`

- [ ] **Step 1: Create style.css**

```css
/* python/spinlab/static/style.css — SpinLab dark theme, 320px narrow */
:root {
  --bg: #1a1a2e;
  --surface: #16213e;
  --card: #0f3460;
  --text: #e0e0e0;
  --text-dim: #8888aa;
  --accent: #00d2ff;
  --green: #4caf50;
  --yellow: #ffc107;
  --red: #f44336;
  --orange: #ff9800;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: 'Consolas', 'Monaco', monospace;
  background: var(--bg);
  color: var(--text);
  width: 320px;
  min-height: 100vh;
  overflow-x: hidden;
}

header {
  background: var(--surface);
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--card);
}

header h1 {
  font-size: 16px;
  color: var(--accent);
  font-weight: 700;
}

.header-stat {
  font-size: 12px;
  color: var(--text-dim);
}

.status-msg {
  text-align: center;
  padding: 32px 12px;
  font-size: 14px;
  color: var(--text-dim);
}

.sub-stat {
  text-align: center;
  font-size: 12px;
  color: var(--text-dim);
}

.card {
  margin: 6px 8px;
  padding: 8px 10px;
  background: var(--card);
  border-radius: 6px;
}

.card-label {
  font-size: 10px;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}

.cs-goal {
  font-size: 14px;
  font-weight: 600;
}

.cs-difficulty {
  font-size: 11px;
  margin-top: 2px;
}

.cs-attempts {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
}

.mi-line {
  font-size: 12px;
}

#queue-list, #recent-list {
  list-style: none;
}

#queue-list li, #recent-list li {
  font-size: 11px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  display: flex;
  justify-content: space-between;
}

#queue-list li:last-child, #recent-list li:last-child {
  border-bottom: none;
}

footer {
  padding: 8px 12px;
  font-size: 11px;
  color: var(--text-dim);
  text-align: center;
  border-top: 1px solid var(--card);
}

/* Difficulty tier colors */
.tier-new { color: var(--text-dim); }
.tier-struggling { color: var(--red); }
.tier-normal { color: var(--yellow); }
.tier-strong { color: var(--green); }

/* Rating colors */
.rating-easy, .rating-good { color: var(--green); }
.rating-hard { color: var(--yellow); }
.rating-again { color: var(--red); }

.mode-panel[hidden] { display: none; }
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/static/style.css
git commit -m "feat(dashboard): add dark theme CSS"
```

---

### Task 14: Polling JavaScript — app.js

**Files:**
- Create: `python/spinlab/static/app.js`

- [ ] **Step 1: Create app.js**

```javascript
// python/spinlab/static/app.js — SpinLab dashboard polling logic
(function () {
  "use strict";

  const POLL_MS = 1000;

  // DOM refs
  const modeIdle = document.getElementById("mode-idle");
  const modeRef = document.getElementById("mode-reference");
  const modePractice = document.getElementById("mode-practice");
  const sessionTimer = document.getElementById("session-timer");
  const csGoal = document.getElementById("cs-goal");
  const csDifficulty = document.getElementById("cs-difficulty");
  const csAttempts = document.getElementById("cs-attempts");
  const miTier = document.getElementById("mi-tier");
  const queueList = document.getElementById("queue-list");
  const recentList = document.getElementById("recent-list");
  const statsLine = document.getElementById("stats-line");

  function showMode(mode) {
    modeIdle.hidden = mode !== "idle";
    modeRef.hidden = mode !== "reference";
    modePractice.hidden = mode !== "practice";
  }

  function tierClass(ef, reps) {
    if (!reps || reps === 0) return "tier-new";
    if (ef < 1.8) return "tier-struggling";
    if (ef < 2.5) return "tier-normal";
    return "tier-strong";
  }

  function tierLabel(ef, reps) {
    if (!reps || reps === 0) return "New";
    if (ef < 1.8) return "Struggling";
    if (ef < 2.5) return "Normal";
    return "Strong";
  }

  function ratingClass(rating) {
    if (!rating) return "";
    return "rating-" + rating;
  }

  function formatTime(ms) {
    if (!ms) return "—";
    return (ms / 1000).toFixed(1) + "s";
  }

  function elapsedStr(startedAt) {
    if (!startedAt) return "";
    var start = new Date(startedAt);
    var diff = Math.floor((Date.now() - start.getTime()) / 1000);
    var m = Math.floor(diff / 60);
    var s = diff % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function update(data) {
    showMode(data.mode);

    // Session timer
    if (data.session && data.session.started_at) {
      sessionTimer.textContent = elapsedStr(data.session.started_at);
    } else {
      sessionTimer.textContent = "";
    }

    if (data.mode !== "practice") return;

    // Current split
    var cs = data.current_split;
    if (cs) {
      var label = cs.description || cs.goal;
      csGoal.textContent = label + " — " + (cs.goal || "");
      var tc = tierClass(cs.ease_factor, cs.repetitions);
      csDifficulty.className = "cs-difficulty " + tc;
      csDifficulty.textContent = tierLabel(cs.ease_factor, cs.repetitions);
      csAttempts.textContent = "Attempts: " + (cs.attempt_count || 0);

      // Model insight placeholder: show tier from ease factor
      miTier.className = "mi-line " + tc;
      miTier.textContent = "EF: " + (cs.ease_factor || 2.5).toFixed(2) +
        " | Reps: " + (cs.repetitions || 0);
    }

    // Queue
    queueList.innerHTML = "";
    (data.queue || []).forEach(function (q) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = q.description || q.goal || q.id;
      var diff = document.createElement("span");
      diff.className = tierClass(q.ease_factor, q.repetitions);
      diff.textContent = tierLabel(q.ease_factor, q.repetitions);
      li.appendChild(name);
      li.appendChild(diff);
      queueList.appendChild(li);
    });

    // Recent results
    recentList.innerHTML = "";
    (data.recent || []).forEach(function (r) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = r.description || r.goal;
      var info = document.createElement("span");
      info.className = ratingClass(r.rating);
      info.textContent = formatTime(r.time_ms) + " " + (r.rating || "");
      li.appendChild(name);
      li.appendChild(info);
      recentList.appendChild(li);
    });

    // Session stats
    if (data.session) {
      var sa = data.session.splits_attempted || 0;
      var sc = data.session.splits_completed || 0;
      statsLine.textContent = sc + "/" + sa + " cleared | " +
        elapsedStr(data.session.started_at);
    }
  }

  function poll() {
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(update)
      .catch(function () { /* silently retry next tick */ });
  }

  poll();
  setInterval(poll, POLL_MS);
})();
```

- [ ] **Step 2: Commit**

```bash
git add python/spinlab/static/app.js
git commit -m "feat(dashboard): add polling JavaScript"
```

---

### Task 15: AHK — launch dashboard with Ctrl+Alt+W

**Files:**
- Modify: `scripts/spinlab.ahk`

- [ ] **Step 1: Add dashboard launch to Ctrl+Alt+W handler**

In `scripts/spinlab.ahk`, replace the `^!w::` block (lines 23-30) with:

```autohotkey
^!w:: {
    if ProcessExist("Mesen.exe") {
        Flash("Mesen already running — reference mode active")
    } else {
        Run('cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide')
        Flash("Launching Mesen2 — reference run mode", 3000)
    }
    ; Start dashboard if not already running
    try {
        whr := ComObject("WinHttp.WinHttpRequest.5.1")
        whr.Open("GET", "http://localhost:15483/api/state", false)
        whr.Send()
    } catch {
        Run('cmd /c spinlab dashboard', A_ScriptDir '\..',  'Min')
        Flash("Dashboard starting on :15483", 2000)
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add scripts/spinlab.ahk
git commit -m "feat(ahk): launch dashboard alongside Mesen on Ctrl+Alt+W"
```

---

### Task 16: Smoke test — run the full test suite

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Manual verification (optional)**

Run: `spinlab dashboard --help`
Expected: Shows help with `--config` and `--port` options
