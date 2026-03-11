# Step 3 — Reference Capture Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the passive recorder to save a state file on each level entrance, then post-process the JSONL log into a practice-ready YAML manifest.

**Architecture:** Two changes: (1) The Lua script queues a state save whenever a fresh level entrance is detected, and includes the state file path in the JSONL entry. (2) A new Python `capture.py` reads the JSONL, pairs each entrance with its exit, and writes a manifest YAML. The pairing logic and manifest builder are pure functions, making them straightforward to test without mocking.

**Tech Stack:** Lua (Mesen2), Python 3.11+, PyYAML, pytest

---

## File Map

| Action   | File | Purpose |
|----------|------|---------|
| Modify   | `lua/spinlab.lua` | Queue state save + add `state_path` to JSONL on `level_entrance` |
| Modify   | `config.yaml` | Add `emulator.script_data_dir` so Python knows where the JSONL lives |
| Create   | `python/spinlab/capture.py` | Parse JSONL → pair events → write manifest YAML |
| Create   | `tests/test_capture.py` | Tests for pairing logic and manifest building |
| Create   | `pyproject.toml` | Minimal project config so `pytest` and `python -m spinlab.capture` work |

---

## Chunk 1: Lua Changes

### Task 1: Queue state save on level_entrance

**Files:**
- Modify: `lua/spinlab.lua` (lines ~183–200, the `level_entrance` detection block)

In `detect_transitions`, the `level_entrance` block currently only logs to JSONL. Add two things:
1. Compute the state file path and set `pending_save`.
2. Include `state_path` in the JSONL entry.

- [ ] **Step 1: Open `lua/spinlab.lua` and locate the level_entrance block**

Find this block (around line 183):
```lua
  -- Level entrance: gameMode transitions to 18 (GmPrepareLevel)
  if curr.game_mode == 18 and prev.game_mode ~= 18 then
    if not died_flag then
      level_start_frame = frame_counter
      log_jsonl({
        event   = "level_entrance",
        level   = curr.level_num,
        room    = curr.room_num,
        frame   = frame_counter,
        ts_ms   = ts_ms(),
        session = "passive",
      })
      log("Level entrance: " .. curr.level_num)
```

- [ ] **Step 2: Replace the level_entrance block with the version that saves state**

Replace that block with:
```lua
  -- Level entrance: gameMode transitions to 18 (GmPrepareLevel)
  if curr.game_mode == 18 and prev.game_mode ~= 18 then
    if not died_flag then
      level_start_frame = frame_counter
      local state_fname = GAME_ID .. "_" .. curr.level_num .. "_" .. curr.room_num .. ".mss"
      local state_path  = STATE_DIR .. "/" .. state_fname
      pending_save = state_path
      log_jsonl({
        event      = "level_entrance",
        level      = curr.level_num,
        room       = curr.room_num,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
        state_path = state_path,
      })
      log("Level entrance: " .. curr.level_num .. " -> queued state save: " .. state_fname)
```

- [ ] **Step 3: Manually verify in Mesen2**

Load the ROM with `scripts/launch.bat`. Enter one level. In the Mesen2 script log, you should see:
```
[SpinLab] Level entrance: 105 -> queued state save: smw_cod_105_1.mss
[SpinLab] Saved state to: C:\...\states\smw_cod_105_1.mss (NNNNNN bytes)
```
Also confirm the `.mss` file exists on disk and the JSONL entry has a `state_path` field.

> **Windows note:** If you see `ERROR: Could not open file for writing`, the `states/` directory doesn't exist. `ensure_dir` uses `mkdir -p` which is a no-op on Windows. Create it manually: `mkdir "C:\Users\thedo\Documents\Mesen2\LuaScriptData\spinlab\states"` then restart the script.

- [ ] **Step 4: Commit**

```bash
git add lua/spinlab.lua
git commit -m "feat(lua): save state on level_entrance for reference capture"
```

---

## Chunk 2: Python capture.py

### Task 2: Project config and pytest setup

**Files:**
- Create: `pyproject.toml`
- Modify: `config.yaml`

- [ ] **Step 1: Add `emulator.script_data_dir` to `config.yaml`**

Add this line under `emulator:`:
```yaml
emulator:
  path: "C:/Apps/Mesen/Mesen 2.1.1/Mesen.exe"
  type: mesen2
  lua_script: "lua/spinlab.lua"
  script_data_dir: "C:/Users/thedo/Documents/Mesen2/LuaScriptData/spinlab"
```

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "spinlab"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pyyaml"]

[project.optional-dependencies]
dev = ["pytest"]

[tool.setuptools.packages.find]
where = ["python"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Install in editable mode**

```bash
cd c:/Users/thedo/git/spinlib
pip install -e ".[dev]"
```

- [ ] **Step 4: Verify pytest runs (no tests yet)**

```bash
pytest
```
Expected: `no tests ran` or `collected 0 items`

---

### Task 3: JSONL parsing and event pairing

**Files:**
- Create: `python/spinlab/capture.py` (skeleton + pairing logic)
- Create: `tests/test_capture.py`

The core of `capture.py` is: given a list of JSONL events, pair each `level_entrance` with the next `level_exit` for the same `(level, room)`. Pure function, easy to test.

- [ ] **Step 1: Create the `tests/` directory**

```bash
mkdir tests
```

- [ ] **Step 2: Create `tests/test_capture.py` with failing tests for the pairing logic**

```python
"""Tests for capture.py JSONL parsing and event pairing."""
import pytest
from spinlab.capture import build_manifest, pair_events, parse_log


def entrance(level, room, frame=0, state_path="states/x.mss"):
    return {
        "event": "level_entrance",
        "level": level,
        "room": room,
        "frame": frame,
        "ts_ms": 0,
        "session": "passive",
        "state_path": state_path,
    }


def exit_event(level, room, goal="normal", elapsed_ms=5000):
    return {
        "event": "level_exit",
        "level": level,
        "room": room,
        "goal": goal,
        "elapsed_ms": elapsed_ms,
        "frame": 0,
        "ts_ms": 0,
        "session": "passive",
    }


def test_parse_log_returns_list_of_dicts():
    lines = ['{"event": "death", "level": 105}', "", '{"event": "level_exit", "level": 106}']
    result = parse_log(lines)
    assert len(result) == 2
    assert result[0]["event"] == "death"


def test_pair_events_basic():
    events = [entrance(105, 1), exit_event(105, 1, "normal", 5000)]
    pairs = pair_events(events)
    assert len(pairs) == 1
    e, x = pairs[0]
    assert e["level"] == 105
    assert x["goal"] == "normal"
    assert x["elapsed_ms"] == 5000


def test_pair_events_two_levels():
    events = [
        entrance(105, 1),
        exit_event(105, 1, "normal", 3000),
        entrance(106, 1),
        exit_event(106, 1, "key", 8000),
    ]
    pairs = pair_events(events)
    assert len(pairs) == 2
    assert pairs[0][1]["elapsed_ms"] == 3000
    assert pairs[1][1]["goal"] == "key"


def test_pair_events_entrance_with_no_exit_is_dropped():
    events = [entrance(105, 1)]
    pairs = pair_events(events)
    assert len(pairs) == 0


def test_pair_events_death_between_entrance_and_exit_ignored():
    """Deaths are not entrance/exit events, so pairing ignores them."""
    events = [
        entrance(105, 1),
        {"event": "death", "level": 105, "room": 1},
        exit_event(105, 1, "normal", 4000),
    ]
    pairs = pair_events(events)
    assert len(pairs) == 1
```

- [ ] **Step 3: Run to confirm failure**

```bash
pytest tests/test_capture.py -v
```
Expected: `ImportError` or `ModuleNotFoundError` — `capture.py` doesn't exist yet.

- [ ] **Step 4: Create `python/spinlab/capture.py` with parse_log and pair_events**

```python
"""Reference capture: parse passive JSONL log into a practice manifest."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import yaml

from spinlab.models import Split


def parse_log(lines: list[str]) -> list[dict[str, Any]]:
    """Parse JSONL lines into a list of event dicts, skipping blank lines."""
    events = []
    for line in lines:
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def pair_events(
    events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair each level_entrance with the next level_exit for the same (level, room).

    Unpaired entrances (run abandoned) are silently dropped.
    Deaths and other events are ignored.
    """
    pairs: list[tuple[dict, dict]] = []
    pending: dict[tuple[int, int], dict] = {}  # (level, room) -> entrance event

    for event in events:
        evt = event.get("event")
        if evt == "level_entrance":
            key = (event["level"], event["room"])
            pending[key] = event
        elif evt == "level_exit":
            key = (event["level"], event["room"])
            if key in pending:
                pairs.append((pending.pop(key), event))

    return pairs
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_capture.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture.py tests/test_capture.py pyproject.toml config.yaml
git commit -m "feat(capture): JSONL parsing and event pairing with tests"
```

---

### Task 4: Manifest building and CLI

**Files:**
- Modify: `python/spinlab/capture.py` (add `build_manifest` and `main`)
- Modify: `tests/test_capture.py` (add manifest tests)

- [ ] **Step 1: Add failing manifest tests to `tests/test_capture.py`**

`build_manifest` is already imported at the top of the file (added in Task 3 Step 2). Just append the two test functions:
```python
def test_build_manifest_structure():
    pairs = [
        (entrance(105, 1, state_path="C:/states/smw_cod_105_1.mss"),
         exit_event(105, 1, "normal", 5000)),
    ]
    manifest = build_manifest(pairs, game_id="smw_cod", category="any%")
    assert manifest["game_id"] == "smw_cod"
    assert manifest["category"] == "any%"
    assert "captured_at" in manifest
    assert len(manifest["splits"]) == 1


def test_build_manifest_split_fields():
    pairs = [
        (entrance(105, 1, state_path="C:/states/smw_cod_105_1.mss"),
         exit_event(105, 1, "key", 8100)),
    ]
    manifest = build_manifest(pairs, game_id="smw_cod", category="any%")
    split = manifest["splits"][0]
    assert split["id"] == "smw_cod:105:1:key"
    assert split["level_number"] == 105
    assert split["room_id"] == 1
    assert split["goal"] == "key"
    assert split["state_path"] == "C:/states/smw_cod_105_1.mss"
    assert split["reference_time_ms"] == 8100
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_capture.py::test_build_manifest_structure tests/test_capture.py::test_build_manifest_split_fields -v
```
Expected: `ImportError` — `build_manifest` not yet defined.

- [ ] **Step 3: Add `build_manifest` and `main` to `python/spinlab/capture.py`**

Append to the file (after `pair_events`):
```python

def build_manifest(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    game_id: str,
    category: str,
) -> dict[str, Any]:
    """Build manifest dict from paired (entrance, exit) events."""
    splits = []
    for entr, ex in pairs:
        split_id = Split.make_id(
            game_id, entr["level"], entr["room"], ex["goal"]
        )
        splits.append(
            {
                "id": split_id,
                "level_number": entr["level"],
                "room_id": entr["room"],
                "goal": ex["goal"],
                "state_path": entr.get("state_path"),
                "reference_time_ms": ex["elapsed_ms"],
            }
        )
    return {
        "game_id": game_id,
        "category": category,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "splits": splits,
    }


def main() -> None:
    """CLI entry point: read config, parse log, write manifest."""
    config_path = Path("config.yaml")
    if not config_path.exists():
        raise SystemExit("config.yaml not found — run from repo root")

    with config_path.open() as f:
        config = yaml.safe_load(f)

    game_id: str = config["game"]["id"]
    category: str = config["game"]["category"]
    script_data_dir = Path(config["emulator"]["script_data_dir"])
    data_dir = Path(config["data"]["dir"])

    log_path = script_data_dir / "passive_log.jsonl"
    if not log_path.exists():
        raise SystemExit(f"Log not found: {log_path}")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    events = parse_log(lines)
    pairs = pair_events(events)
    manifest = build_manifest(pairs, game_id=game_id, category=category)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = data_dir / "captures" / f"{date_str}_{game_id}_manifest.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"Wrote {len(manifest['splits'])} splits → {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add `__main__.py` so `python -m spinlab.capture` works**

Create `python/spinlab/capture_main.py`... actually, just add to the bottom of `capture.py`:

The `if __name__ == "__main__": main()` block is already there. To support `python -m spinlab.capture`, that's sufficient — Python executes the module directly. Verify:

```bash
cd c:/Users/thedo/git/spinlib
python -m spinlab.capture --help 2>&1 || python -m spinlab.capture
```
Expected: either runs (if log exists) or prints `Log not found: ...`

- [ ] **Step 5: Run all tests**

```bash
pytest tests/test_capture.py -v
```
Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/capture.py tests/test_capture.py
git commit -m "feat(capture): manifest builder and CLI entry point"
```

---

## Chunk 3: End-to-End Verification

### Task 5: Run a reference capture and inspect output

This task is manual. No code changes.

- [ ] **Step 1: Launch Mesen2 and load the ROM**

```bat
scripts\launch.bat
```

- [ ] **Step 2: Play through 2–3 levels**

Complete each level normally. Watch the script log for:
```
[SpinLab] Level entrance: 105 -> queued state save: smw_cod_105_1.mss
[SpinLab] Saved state to: C:\...\states\smw_cod_105_1.mss (NNNNNN bytes)
[SpinLab] Level exit: 105 goal=normal elapsed=NNNNms
```

- [ ] **Step 3: Confirm state files exist on disk**

```bash
ls "C:/Users/thedo/Documents/Mesen2/LuaScriptData/spinlab/states/"
```
Expected: `.mss` files named `smw_cod_<level>_<room>.mss`

- [ ] **Step 4: Run capture.py from repo root**

```bash
cd c:/Users/thedo/git/spinlib
python -m spinlab.capture
```
Expected output:
```
Wrote N splits → data/captures/2026-MM-DD_smw_cod_manifest.yaml
```

- [ ] **Step 5: Inspect the manifest**

```bash
cat data/captures/*_manifest.yaml
```
Confirm:
- `game_id: smw_cod`
- One entry per completed level
- `state_path` points to an existing `.mss` file
- `reference_time_ms` is a plausible number (thousands of ms, not 0)
- `goal` matches what you actually did (normal/key/etc.)

- [ ] **Step 6: Commit**

```bash
git add data/captures/
git commit -m "chore: first reference capture manifest"
```

> **Optional:** If you don't want future captures checked in, add `data/captures/` to `.gitignore`. For now, keeping the first capture is useful as a reference.
