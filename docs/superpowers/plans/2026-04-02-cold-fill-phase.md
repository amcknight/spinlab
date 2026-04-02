# Cold-Fill Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a reference run, automatically capture cold (respawn) save states for all checkpoint segments that only have hot variants, so practice mode defaults to cold starts.

**Architecture:** New `Mode.COLD_FILL` with a queue-based state machine. Python's `CaptureController` manages the segment queue and stores cold variants. Lua gets a dedicated cold-fill mini state machine (~25 lines) that watches for death→spawn and captures the respawn state. Dashboard shows progress via SSE.

**Tech Stack:** Python 3.11+, SQLite, Mesen2 Lua, FastAPI SSE, vanilla JS

---

### Task 1: Add Mode.COLD_FILL and transition rules

**Files:**
- Modify: `python/spinlab/models.py:9-23`
- Test: `tests/test_session_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_session_manager.py`, add:

```python
class TestColdFillMode:
    def test_cold_fill_mode_exists(self):
        assert Mode.COLD_FILL.value == "cold_fill"

    def test_idle_to_cold_fill_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.IDLE, Mode.COLD_FILL)
        assert result == Mode.COLD_FILL

    def test_cold_fill_to_idle_legal(self):
        from spinlab.models import transition_mode
        result = transition_mode(Mode.COLD_FILL, Mode.IDLE)
        assert result == Mode.IDLE

    def test_cold_fill_to_practice_illegal(self):
        from spinlab.models import transition_mode
        with pytest.raises(ValueError):
            transition_mode(Mode.COLD_FILL, Mode.PRACTICE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_manager.py::TestColdFillMode -v`
Expected: FAIL — `Mode` has no `COLD_FILL` attribute

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/models.py`, add `COLD_FILL` to the Mode enum and update transitions:

```python
class Mode(Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PRACTICE = "practice"
    REPLAY = "replay"
    FILL_GAP = "fill_gap"
    COLD_FILL = "cold_fill"


_LEGAL_TRANSITIONS: dict[Mode, set[Mode]] = {
    Mode.IDLE: {Mode.REFERENCE, Mode.PRACTICE, Mode.FILL_GAP, Mode.COLD_FILL},
    Mode.REFERENCE: {Mode.IDLE, Mode.REPLAY},
    Mode.PRACTICE: {Mode.IDLE},
    Mode.REPLAY: {Mode.IDLE},
    Mode.FILL_GAP: {Mode.IDLE},
    Mode.COLD_FILL: {Mode.IDLE},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_manager.py::TestColdFillMode -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_session_manager.py
git commit -m "feat: add Mode.COLD_FILL with transition rules"
```

---

### Task 2: Add DB query for segments missing cold variants

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Test: `tests/test_segment_variants.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_segment_variants.py`, add:

```python
def test_segments_missing_cold(db):
    """Get segments that have a hot variant but no cold variant."""
    # Create two segments
    s1 = Segment(
        id="g1:105:entrance.0:checkpoint.1", game_id="g1",
        level_number=105, start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, reference_id="run1",
    )
    s2 = Segment(
        id="g1:105:checkpoint.1:goal.0", game_id="g1",
        level_number=105, start_type="checkpoint", start_ordinal=1,
        end_type="goal", end_ordinal=0, reference_id="run1",
    )
    db.upsert_segment(s1)
    db.upsert_segment(s2)

    # s1 has both hot and cold; s2 has only hot
    db.add_variant(SegmentVariant(s1.id, "hot", "/hot1.mss", False))
    db.add_variant(SegmentVariant(s1.id, "cold", "/cold1.mss", True))
    db.add_variant(SegmentVariant(s2.id, "hot", "/hot2.mss", False))

    missing = db.segments_missing_cold("g1")
    assert len(missing) == 1
    assert missing[0]["segment_id"] == s2.id
    assert missing[0]["hot_state_path"] == "/hot2.mss"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_segment_variants.py::test_segments_missing_cold -v`
Expected: FAIL — `segments_missing_cold` doesn't exist

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/db/segments.py`, add to `SegmentsMixin`:

```python
def segments_missing_cold(self, game_id: str) -> list[dict]:
    """Return segments that have a hot variant but no cold variant."""
    rows = self.conn.execute(
        """SELECT s.id AS segment_id, hot.state_path AS hot_state_path,
                  s.level_number, s.start_type, s.start_ordinal,
                  s.end_type, s.end_ordinal, s.description
           FROM segments s
           JOIN segment_variants hot
             ON hot.segment_id = s.id AND hot.variant_type = 'hot'
           LEFT JOIN segment_variants cold
             ON cold.segment_id = s.id AND cold.variant_type = 'cold'
           WHERE s.game_id = ? AND s.active = 1 AND cold.segment_id IS NULL
           ORDER BY s.ordinal, s.level_number, s.start_ordinal""",
        (game_id,),
    ).fetchall()
    cols = ["segment_id", "hot_state_path", "level_number",
            "start_type", "start_ordinal", "end_type", "end_ordinal", "description"]
    return [dict(zip(cols, r)) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_segment_variants.py::test_segments_missing_cold -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/segments.py tests/test_segment_variants.py
git commit -m "feat: add segments_missing_cold DB query"
```

---

### Task 3: Add cold-fill queue to CaptureController

**Files:**
- Modify: `python/spinlab/capture_controller.py`
- Test: `tests/test_cold_fill.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cold_fill.py`:

```python
"""Tests for CaptureController cold-fill flow."""
import json
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from spinlab.capture_controller import CaptureController
from spinlab.models import Mode, SegmentVariant


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    return tcp


@pytest.fixture
def db():
    db = MagicMock()
    db.segments_missing_cold = MagicMock(return_value=[
        {"segment_id": "g1:105:cp.1:cp.2", "hot_state_path": "/hot1.mss",
         "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
         "end_type": "checkpoint", "end_ordinal": 2, "description": ""},
        {"segment_id": "g1:105:cp.2:goal.0", "hot_state_path": "/hot2.mss",
         "level_number": 105, "start_type": "checkpoint", "start_ordinal": 2,
         "end_type": "goal", "end_ordinal": 0, "description": ""},
    ])
    db.get_variant = MagicMock(return_value=None)
    db.add_variant = MagicMock()
    return db


class TestStartColdFill:
    async def test_start_cold_fill_sends_first_segment(self, tcp, db):
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)

        assert result["status"] == "started"
        assert result["new_mode"] == Mode.COLD_FILL
        assert result["total"] == 2
        assert result["current"] == 1

        # Verify Lua command sent for first segment
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["state_path"] == "/hot1.mss"
        assert sent["segment_id"] == "g1:105:cp.1:cp.2"

    async def test_start_cold_fill_no_gaps(self, tcp, db):
        db.segments_missing_cold.return_value = []
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)
        assert result["status"] == "no_gaps"

    async def test_start_cold_fill_not_connected(self, tcp, db):
        tcp.is_connected = False
        cc = CaptureController()
        result = await cc.start_cold_fill("g1", tcp, db)
        assert result["status"] == "not_connected"


class TestHandleColdFillSpawn:
    async def test_stores_cold_variant_and_advances(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        # Simulate spawn event for first segment
        done = await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"},
            tcp, db,
        )
        assert done is False  # still have one more

        # Verify cold variant stored with is_default=True
        v = db.add_variant.call_args[0][0]
        assert v.segment_id == "g1:105:cp.1:cp.2"
        assert v.variant_type == "cold"
        assert v.state_path == "/cold1.mss"
        assert v.is_default is True

        # Verify second segment loaded
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["segment_id"] == "g1:105:cp.2:goal.0"

    async def test_returns_true_when_queue_empty(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        # Process both segments
        await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold1.mss"}, tcp, db,
        )
        done = await cc.handle_cold_fill_spawn(
            {"state_captured": True, "state_path": "/cold2.mss"}, tcp, db,
        )
        assert done is True

    async def test_ignores_spawn_without_state(self, tcp, db):
        cc = CaptureController()
        await cc.start_cold_fill("g1", tcp, db)

        done = await cc.handle_cold_fill_spawn(
            {"state_captured": False}, tcp, db,
        )
        assert done is False
        # Queue unchanged — still on first segment
        assert cc.cold_fill_current == "g1:105:cp.1:cp.2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cold_fill.py -v`
Expected: FAIL — `start_cold_fill` and `handle_cold_fill_spawn` don't exist

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/capture_controller.py`, add to `CaptureController.__init__`:

```python
# Cold-fill state
self.cold_fill_queue: list[dict] = []
self.cold_fill_current: str | None = None
self.cold_fill_total: int = 0
```

Add methods after the fill-gap section:

```python
# --- Cold-fill ---

async def start_cold_fill(self, game_id: str, tcp: "TcpManager", db: "Database") -> dict:
    if not tcp.is_connected:
        return {"status": "not_connected"}
    gaps = db.segments_missing_cold(game_id)
    if not gaps:
        return {"status": "no_gaps"}
    self.cold_fill_queue = gaps
    self.cold_fill_total = len(gaps)
    self.cold_fill_current = None
    return await self._load_next_cold_fill(tcp)

async def _load_next_cold_fill(self, tcp: "TcpManager") -> dict:
    if not self.cold_fill_queue:
        self.cold_fill_current = None
        return {"status": "complete", "new_mode": Mode.COLD_FILL}
    seg = self.cold_fill_queue[0]
    self.cold_fill_current = seg["segment_id"]
    await tcp.send(json.dumps({
        "event": "cold_fill_load",
        "state_path": seg["hot_state_path"],
        "segment_id": seg["segment_id"],
    }))
    current_num = self.cold_fill_total - len(self.cold_fill_queue) + 1
    return {
        "status": "started",
        "new_mode": Mode.COLD_FILL,
        "current": current_num,
        "total": self.cold_fill_total,
    }

async def handle_cold_fill_spawn(self, event: dict, tcp: "TcpManager", db: "Database") -> bool:
    """Store cold variant, advance queue. Returns True when all done."""
    if not event.get("state_captured") or not self.cold_fill_current:
        return False
    variant = SegmentVariant(
        segment_id=self.cold_fill_current,
        variant_type="cold",
        state_path=event["state_path"],
        is_default=True,
    )
    db.add_variant(variant)
    self.cold_fill_queue.pop(0)
    if not self.cold_fill_queue:
        self.cold_fill_current = None
        return True
    await self._load_next_cold_fill(tcp)
    return False

def get_cold_fill_state(self) -> dict | None:
    if not self.cold_fill_current:
        return None
    current_num = self.cold_fill_total - len(self.cold_fill_queue)
    seg = self.cold_fill_queue[0] if self.cold_fill_queue else None
    label = ""
    if seg:
        s = seg
        start = "start" if s["start_type"] == "entrance" else f"cp{s['start_ordinal']}"
        end = "goal" if s["end_type"] == "goal" else f"cp{s['end_ordinal']}"
        label = s.get("description") or f"L{s['level_number']} {start} > {end}"
    return {
        "current": current_num,
        "total": self.cold_fill_total,
        "segment_label": label,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cold_fill.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture_controller.py tests/test_cold_fill.py
git commit -m "feat: cold-fill queue in CaptureController"
```

---

### Task 4: Wire cold-fill into SessionManager

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_session_manager.py`, add:

```python
class TestColdFill:
    async def test_save_draft_triggers_cold_fill(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"

        # Set up draft
        sm.capture.draft.enter_draft("run1", 3)

        # Mock: 2 segments missing cold
        mock_db.segments_missing_cold = MagicMock(return_value=[
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "checkpoint", "end_ordinal": 2, "description": ""},
            {"segment_id": "seg2", "hot_state_path": "/hot2.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 2,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ])

        result = await sm.save_draft("Test")
        assert result["status"] == "ok"
        assert sm.mode == Mode.COLD_FILL

    async def test_save_draft_no_gaps_stays_idle(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.capture.draft.enter_draft("run1", 3)
        mock_db.segments_missing_cold = MagicMock(return_value=[])

        result = await sm.save_draft("Test")
        assert result["status"] == "ok"
        assert sm.mode == Mode.IDLE

    async def test_cold_fill_spawn_routes_correctly(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.capture.cold_fill_current = "seg1"
        sm.capture.cold_fill_queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.capture.cold_fill_total = 1

        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold1.mss",
        })
        assert sm.mode == Mode.IDLE

    async def test_cold_fill_state_in_get_state(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.capture.cold_fill_current = "seg1"
        sm.capture.cold_fill_queue = [
            {"segment_id": "seg1", "hot_state_path": "/hot1.mss",
             "level_number": 105, "start_type": "checkpoint", "start_ordinal": 1,
             "end_type": "goal", "end_ordinal": 0, "description": ""},
        ]
        sm.capture.cold_fill_total = 2

        state = sm.get_state()
        assert state["mode"] == "cold_fill"
        assert "cold_fill" in state
        assert state["cold_fill"]["total"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_manager.py::TestColdFill -v`
Expected: FAIL — `save_draft` doesn't trigger cold-fill, spawn routing doesn't handle COLD_FILL

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/session_manager.py`:

**Update `save_draft`** (around line 350):

```python
async def save_draft(self, name: str) -> dict:
    result = await self.capture.save_draft(self.db, name)
    if result.get("status") == "ok" and self.game_id and self.tcp.is_connected:
        cf_result = await self.capture.start_cold_fill(
            self.game_id, self.tcp, self.db,
        )
        if cf_result.get("new_mode") == Mode.COLD_FILL:
            self.mode = Mode.COLD_FILL
    await self._notify_sse()
    return result
```

**Update `_handle_spawn`** (around line 265):

```python
async def _handle_spawn(self, event: dict) -> None:
    if self.mode == Mode.COLD_FILL:
        done = await self.capture.handle_cold_fill_spawn(event, self.tcp, self.db)
        if done:
            self.mode = Mode.IDLE
        await self._notify_sse()
        return
    if self.mode == Mode.FILL_GAP:
        if self.capture.handle_fill_gap_spawn(event, self.db):
            self.mode = Mode.IDLE
            await self._notify_sse()
        return
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_spawn(event, self._require_game(), self.db)
```

**Update `_handle_death`** (around line 260):

```python
async def _handle_death(self, event: dict) -> None:
    if self.mode not in (Mode.REFERENCE, Mode.REPLAY, Mode.COLD_FILL):
        return
    if self.mode in (Mode.REFERENCE, Mode.REPLAY):
        self.capture.handle_death()
```

**Update `get_state`** (around line 92), add after the draft block:

```python
if self.mode == Mode.COLD_FILL:
    cf_state = self.capture.get_cold_fill_state()
    if cf_state:
        base["cold_fill"] = cf_state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_manager.py::TestColdFill -v`
Expected: PASS

- [ ] **Step 5: Run all session manager tests**

Run: `pytest tests/test_session_manager.py -v`
Expected: All PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: wire cold-fill into SessionManager"
```

---

### Task 5: Add cold_fill_load handler in Lua

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Add cold-fill state to the STATE section**

After the existing state declarations (around line 72), add:

```lua
-- Cold-fill state (captures cold starts after reference run)
local cold_fill = {
  active = false,
  state = nil,            -- "waiting_death" or "waiting_spawn"
  segment_id = nil,
  prev_anim = 0,
  prev_level_start = 0,
}
```

- [ ] **Step 2: Add the cold-fill per-frame handler**

Before the `handle_practice` function (around line 678), add:

```lua
-----------------------------------------------------------------------
-- COLD-FILL MODE (captures cold save states after reference run)
-----------------------------------------------------------------------
local CFSTATE_WAITING_DEATH = "waiting_death"
local CFSTATE_WAITING_SPAWN = "waiting_spawn"

local function handle_cold_fill()
  local anim = emu.read(ADDR_PLAYER_ANIM, SNES, false)
  local level_start = emu.read(ADDR_LEVEL_START, SNES, false)

  if cold_fill.state == CFSTATE_WAITING_DEATH then
    -- Detect death: player_anim transitions to 9
    if anim == 9 and cold_fill.prev_anim ~= 9 then
      cold_fill.state = CFSTATE_WAITING_SPAWN
      log("Cold-fill: death detected, waiting for spawn")
    end

  elseif cold_fill.state == CFSTATE_WAITING_SPAWN then
    -- Detect spawn: level_start transitions 0→1
    if level_start == 1 and cold_fill.prev_level_start == 0 then
      -- Capture cold save state
      local game_dir = STATE_DIR .. "/" .. (game_id or "unknown")
      ensure_dir(game_dir)
      local path = game_dir .. "/cold_" .. cold_fill.segment_id:gsub("[:/]", "_") .. ".mss"
      table.insert(pending_saves, path)
      send_event({
        event = "spawn",
        is_cold_cp = true,
        state_captured = true,
        state_path = path,
        segment_id = cold_fill.segment_id,
      })
      log("Cold-fill: spawn captured for " .. cold_fill.segment_id)
      cold_fill.active = false
      cold_fill.state = nil
      cold_fill.segment_id = nil
    end
  end

  cold_fill.prev_anim = anim
  cold_fill.prev_level_start = level_start
end
```

- [ ] **Step 3: Add cold_fill_load command handler**

In `handle_json_message` (around line 826), add a new elseif before the closing `end`:

```lua
  elseif decoded_event == "cold_fill_load" then
    local path = json_get_str(line, "state_path")
    local seg_id = json_get_str(line, "segment_id")
    if not path or not seg_id then
      client:send(to_json({event = "error", message = "cold_fill_load requires state_path and segment_id"}) .. "\n")
    else
      table.insert(pending_loads, path)
      cold_fill.active = true
      cold_fill.state = CFSTATE_WAITING_DEATH
      cold_fill.segment_id = seg_id
      cold_fill.prev_anim = 0
      cold_fill.prev_level_start = 0
      client:send("ok:cold_fill\n")
      log("Cold-fill: loaded " .. seg_id .. " — die to capture cold start")
    end
```

- [ ] **Step 4: Wire handle_cold_fill into the main frame callback**

In `on_start_frame` (around line 1120), update the practice/transition dispatch:

```lua
  if cold_fill.active then
    handle_cold_fill()
  elseif practice.active then
    handle_practice(curr)
  else
    detect_transitions(curr)
  end
```

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat: cold_fill_load handler and state machine in Lua"
```

---

### Task 6: Display cold-fill progress in the dashboard

**Files:**
- Modify: `python/spinlab/static/manage.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Add cold-fill status display to manage.js**

In `manage.js`, in the `renderManage` function (around line 77), add before the segments table rendering:

```javascript
  // Cold-fill progress banner
  const cfBanner = document.getElementById('cold-fill-banner');
  if (cfBanner) {
    if (lastState?.mode === 'cold_fill' && lastState?.cold_fill) {
      const cf = lastState.cold_fill;
      cfBanner.innerHTML =
        '<div class="cold-fill-status">' +
        '<strong>Capturing cold starts</strong> — ' +
        'Die to continue (' + cf.current + '/' + cf.total + ')' +
        (cf.segment_label ? ' — ' + cf.segment_label : '') +
        '</div>';
      cfBanner.style.display = 'block';
    } else {
      cfBanner.style.display = 'none';
    }
  }
```

- [ ] **Step 2: Add the banner element to the HTML**

In `python/spinlab/static/index.html`, add the banner div just before the segment table (before line 120, before the `<table>` that contains `segment-body`):

```html
<div id="cold-fill-banner" style="display:none"></div>
```

- [ ] **Step 3: Add CSS styling**

In `python/spinlab/static/style.css`, add:

```css
.cold-fill-status {
  background: #1a3a2a;
  border: 1px solid #2d5a3d;
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 12px;
  color: #8fd;
  font-size: 0.95em;
}
```

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/manage.js python/spinlab/static/style.css python/spinlab/static/index.html
git commit -m "feat: cold-fill progress banner in dashboard"
```

---

### Task 7: Integration test — full cold-fill cycle

**Files:**
- Create: `tests/test_cold_fill_integration.py`

- [ ] **Step 1: Write the integration test**

This test uses a real SQLite DB (not mocks) to verify the full cycle:

```python
"""Integration test: full cold-fill cycle with real DB."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from spinlab.db import Database
from spinlab.models import Mode, Segment, SegmentVariant
from spinlab.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    d.upsert_game("g1", "Test Game", "any%")
    return d


@pytest.fixture
def tcp():
    tcp = MagicMock()
    tcp.is_connected = True
    tcp.send = AsyncMock()
    return tcp


@pytest.fixture
def sm(db, tcp):
    return SessionManager(db=db, tcp=tcp, rom_dir=None)


def _create_segments_with_hot_only(db):
    """Create 3 segments: entrance→cp1, cp1→cp2, cp2→goal. All hot only."""
    segs = [
        Segment(id="g1:105:entrance.0:checkpoint.1", game_id="g1",
                level_number=105, start_type="entrance", start_ordinal=0,
                end_type="checkpoint", end_ordinal=1, reference_id="run1"),
        Segment(id="g1:105:checkpoint.1:checkpoint.2", game_id="g1",
                level_number=105, start_type="checkpoint", start_ordinal=1,
                end_type="checkpoint", end_ordinal=2, reference_id="run1"),
        Segment(id="g1:105:checkpoint.2:goal.0", game_id="g1",
                level_number=105, start_type="checkpoint", start_ordinal=2,
                end_type="goal", end_ordinal=0, reference_id="run1"),
    ]
    for s in segs:
        db.upsert_segment(s)
    # Entrance segment gets cold by default (entrance state IS the cold state)
    db.add_variant(SegmentVariant(segs[0].id, "hot", "/hot0.mss", False))
    db.add_variant(SegmentVariant(segs[0].id, "cold", "/cold0.mss", True))
    # cp1 and cp2 segments only have hot
    db.add_variant(SegmentVariant(segs[1].id, "hot", "/hot1.mss", False))
    db.add_variant(SegmentVariant(segs[2].id, "hot", "/hot2.mss", False))
    return segs


class TestColdFillIntegration:
    async def test_full_cycle(self, sm, db, tcp):
        sm.game_id = "g1"
        segs = _create_segments_with_hot_only(db)

        # Set up and save draft
        db.create_capture_run("run1", "g1", "Test Run", draft=True)
        sm.capture.draft.enter_draft("run1", 3)
        result = await sm.save_draft("Test Run")

        assert result["status"] == "ok"
        assert sm.mode == Mode.COLD_FILL

        # Verify first segment loaded
        sent = json.loads(tcp.send.call_args[0][0])
        assert sent["event"] == "cold_fill_load"
        assert sent["segment_id"] == segs[1].id

        # Simulate spawn for first segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold1.mss",
        })
        assert sm.mode == Mode.COLD_FILL  # still filling

        # Verify cold variant stored
        v = db.get_variant(segs[1].id, "cold")
        assert v is not None
        assert v.state_path == "/cold1.mss"
        assert v.is_default is True

        # Simulate spawn for second segment
        await sm.route_event({
            "event": "spawn",
            "state_captured": True,
            "state_path": "/cold2.mss",
        })
        assert sm.mode == Mode.IDLE  # done

        # Verify both cold variants exist
        v2 = db.get_variant(segs[2].id, "cold")
        assert v2 is not None
        assert v2.state_path == "/cold2.mss"

        # Verify no more gaps
        assert db.segments_missing_cold("g1") == []
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_cold_fill_integration.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/integration`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_cold_fill_integration.py
git commit -m "test: cold-fill integration test with real DB"
```

---

### Task 8: Handle disconnect during cold-fill

**Files:**
- Modify: `python/spinlab/session_manager.py`
- Modify: `tests/test_session_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_session_manager.py`, add to `TestColdFill`:

```python
    async def test_disconnect_during_cold_fill_returns_idle(self, mock_db, mock_tcp):
        sm = make_sm(mock_db, mock_tcp)
        sm.game_id = "game1"
        sm.mode = Mode.COLD_FILL
        sm.capture.cold_fill_current = "seg1"
        sm.capture.cold_fill_queue = [{"segment_id": "seg1"}]
        sm.capture.cold_fill_total = 1

        sm.on_disconnect()
        assert sm.mode == Mode.IDLE
        assert sm.capture.cold_fill_current is None
        assert sm.capture.cold_fill_queue == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_manager.py::TestColdFill::test_disconnect_during_cold_fill_returns_idle -v`
Expected: FAIL — disconnect doesn't clear cold-fill state

- [ ] **Step 3: Write minimal implementation**

In `python/spinlab/capture_controller.py`, add a method:

```python
def clear_cold_fill(self) -> None:
    """Reset cold-fill state (e.g., on disconnect)."""
    self.cold_fill_queue = []
    self.cold_fill_current = None
    self.cold_fill_total = 0
```

In `python/spinlab/session_manager.py`, update `on_disconnect` (around line 405):

```python
def on_disconnect(self) -> None:
    if self.practice_session and self.practice_session.is_running:
        self.practice_session.is_running = False
    self.capture.clear_cold_fill()
    self.capture.handle_disconnect(self.db)
    self._clear_ref_and_idle()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_manager.py::TestColdFill -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/capture_controller.py python/spinlab/session_manager.py tests/test_session_manager.py
git commit -m "feat: clear cold-fill state on disconnect"
```

---

### Task 9: Clean up spike script

**Files:**
- Delete: `lua/death_spike.lua`

- [ ] **Step 1: Remove the spike script**

The death spike experiment is complete and results are documented in memory. Remove the script:

```bash
git rm lua/death_spike.lua
```

- [ ] **Step 2: Also remove the spike test state file if it exists**

```bash
rm -f lua/spike_test.mss
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove death spike experiment script"
```
