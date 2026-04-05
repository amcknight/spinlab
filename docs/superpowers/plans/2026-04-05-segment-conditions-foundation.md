# Segment Conditions Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add memory-derived "conditions" (starting with powerup) to segment identity, introducing Waypoints as graph nodes that own save states. Reference-run capture produces distinct segments per condition combo.

**Architecture:** New `waypoints` table (nodes), new `waypoint_save_states` table (replaces `segment_variants`). Segments gain `start_waypoint_id`, `end_waypoint_id`, `is_primary` columns. A new `ConditionRegistry` loads per-game YAML; Python pushes condition addresses to Lua at startup via a new `set_conditions` TCP command. Lua reads addresses at every transition event and includes observed values in the payload. Reference capture decodes observed conditions and creates/reuses waypoints accordingly.

**Tech Stack:** Python 3.11+ (dataclasses, sqlite3, pyyaml), Lua (Mesen2), pytest.

**Design spec:** `docs/superpowers/specs/2026-04-05-segment-conditions-design.md`
**Glossary:** `docs/GLOSSARY.md`

**No data migration.** Existing databases and save states are discarded when schema version changes; re-capture reference runs to repopulate.

---

## File Structure

**New files:**
- `python/spinlab/condition_registry.py` — YAML loader, scope filtering, value decoding
- `python/spinlab/db/waypoints.py` — WaypointsMixin
- `python/spinlab/games/__init__.py` — empty package marker
- `python/spinlab/games/abcdef0123456789/conditions.yaml` — placeholder for SMW (use real game_id in practice)
- `tests/test_condition_registry.py`
- `tests/test_waypoints_db.py`
- `tests/test_capture_with_conditions.py`

**Modified files:**
- `python/spinlab/models.py` — add `Waypoint` dataclass; add waypoint FKs + `is_primary` to `Segment`; add observed-conditions + `invalidated` to `Attempt`; update `Segment.make_id`
- `python/spinlab/db/core.py` — add `waypoints` + `waypoint_save_states` tables, amend `segments` / `attempts` schemas, add stale-drop entries
- `python/spinlab/db/__init__.py` — mix in `WaypointsMixin`; rewire variant helpers to use `waypoint_save_states`
- `python/spinlab/db/segments.py` — update `upsert_segment`, `get_all_segments_with_model`, `segments_missing_cold`, variant helpers
- `python/spinlab/db/attempts.py` — persist observed conditions + `invalidated` on attempts
- `python/spinlab/reference_capture.py` — create/reuse waypoints, attach save states to waypoints, assign `is_primary`
- `python/spinlab/session_manager.py` (or equivalent startup path) — load ConditionRegistry, send `set_conditions` on TCP connect
- `python/spinlab/tcp.py` (wherever TCP commands are framed) — add `set_conditions` sender
- `lua/spinlab.lua` — add `set_conditions` handler; read condition addresses at transitions and include them in event payloads
- `python/spinlab/routes/segments.py` — include waypoints + conditions in `/api/segments` payload

---

## Task 1: Condition YAML schema + loader

**Files:**
- Create: `python/spinlab/condition_registry.py`
- Create: `tests/test_condition_registry.py`
- Create: `python/spinlab/games/__init__.py`
- Create: `python/spinlab/games/abcdef0123456789/conditions.yaml`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_condition_registry.py
from pathlib import Path
from spinlab.condition_registry import ConditionRegistry, ConditionDef, Scope

def test_loads_single_game_scoped_condition(tmp_path: Path):
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text(
        "conditions:\n"
        "  - name: powerup\n"
        "    address: 0x0019\n"
        "    size: 1\n"
        "    type: enum\n"
        "    values: { 0: small, 1: big, 2: cape, 3: fire }\n"
        "    scope: game\n"
    )
    reg = ConditionRegistry.from_yaml(yaml_path)
    assert len(reg.definitions) == 1
    d = reg.definitions[0]
    assert d.name == "powerup"
    assert d.address == 0x0019
    assert d.size == 1
    assert d.type == "enum"
    assert d.values == {0: "small", 1: "big", 2: "cape", 3: "fire"}
    assert d.scope == Scope.game()

def test_level_scoped_condition(tmp_path: Path):
    yaml_path = tmp_path / "conditions.yaml"
    yaml_path.write_text(
        "conditions:\n"
        "  - name: yellow_key\n"
        "    address: 0x7E1F2D\n"
        "    size: 1\n"
        "    type: bool\n"
        "    scope: { levels: [42, 17] }\n"
    )
    reg = ConditionRegistry.from_yaml(yaml_path)
    d = reg.definitions[0]
    assert d.scope.levels == [42, 17]
    assert d.scope.is_game_scope is False

def test_in_scope_filtering():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small"}, scope=Scope.game()),
        ConditionDef(name="yellow_key", address=0x7E1F2D, size=1, type="bool",
                     values=None, scope=Scope.levels([42])),
    ])
    assert [d.name for d in reg.in_scope(level=5)] == ["powerup"]
    assert [d.name for d in reg.in_scope(level=42)] == ["powerup", "yellow_key"]

def test_decode_enum():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small", 1: "big"}, scope=Scope.game()),
    ])
    assert reg.decode({"powerup": 1}, level=5) == {"powerup": "big"}

def test_decode_bool():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="on_yoshi", address=0x187A, size=1, type="bool",
                     values=None, scope=Scope.game()),
    ])
    assert reg.decode({"on_yoshi": 0}, level=5) == {"on_yoshi": False}
    assert reg.decode({"on_yoshi": 1}, level=5) == {"on_yoshi": True}

def test_decode_drops_out_of_scope():
    reg = ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small"}, scope=Scope.game()),
        ConditionDef(name="yellow_key", address=0x7E1F2D, size=1, type="bool",
                     values=None, scope=Scope.levels([42])),
    ])
    result = reg.decode({"powerup": 0, "yellow_key": 1}, level=5)
    assert result == {"powerup": "small"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_condition_registry.py -v`
Expected: FAIL with ModuleNotFoundError or ImportError.

- [ ] **Step 3: Implement ConditionRegistry**

```python
# python/spinlab/condition_registry.py
"""Loads per-game condition definitions from YAML; decodes raw values."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


# Scope types ----------------------------------------------------------
@dataclass(frozen=True)
class Scope:
    """Scope of a condition: entire game, or specific levels only."""
    is_game_scope: bool
    levels: tuple[int, ...] = ()

    @classmethod
    def game(cls) -> "Scope":
        return cls(is_game_scope=True)

    @classmethod
    def levels_of(cls, levels: Iterable[int]) -> "Scope":
        return cls(is_game_scope=False, levels=tuple(levels))

    # Alias used by tests for readability.
    @classmethod
    def levels(cls, levels_: Iterable[int]) -> "Scope":
        return cls.levels_of(levels_)

    def covers(self, level: int) -> bool:
        return self.is_game_scope or level in self.levels


@dataclass(frozen=True)
class ConditionDef:
    name: str
    address: int
    size: int
    type: str                              # 'enum' or 'bool'
    values: dict[int, str] | None
    scope: Scope


@dataclass
class ConditionRegistry:
    definitions: list[ConditionDef] = field(default_factory=list)

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
        return cls(definitions=defs)

    def in_scope(self, level: int) -> list[ConditionDef]:
        return [d for d in self.definitions if d.scope.covers(level)]

    def decode(self, raw: dict[str, int], level: int) -> dict[str, Any]:
        """Decode raw memory values into logical conditions, filtering to in-scope."""
        result: dict[str, Any] = {}
        for d in self.in_scope(level):
            if d.name not in raw:
                continue
            v = raw[d.name]
            if d.type == "enum":
                assert d.values is not None
                result[d.name] = d.values.get(v, f"unknown_{v}")
            elif d.type == "bool":
                result[d.name] = bool(v)
            else:
                raise ValueError(f"unknown condition type: {d.type}")
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_condition_registry.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Create packaging + placeholder YAML**

```python
# python/spinlab/games/__init__.py
"""Per-game configuration: conditions.yaml, addresses, etc."""
```

```yaml
# python/spinlab/games/abcdef0123456789/conditions.yaml
# Replace the directory name with your ROM's 16-char game_id (truncated SHA-256).
conditions:
  - name: powerup
    address: 0x0019           # SMW RAM: Mario's current powerup. 0=small, 1=big, 2=cape, 3=fire.
    size: 1
    type: enum
    values: { 0: small, 1: big, 2: cape, 3: fire }
    scope: game
```

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/condition_registry.py python/spinlab/games tests/test_condition_registry.py
git commit -m "feat: add ConditionRegistry YAML loader + SMW powerup definition"
```

---

## Task 2: Waypoint model

**Files:**
- Modify: `python/spinlab/models.py`
- Create: tests go in existing `tests/test_models.py` (or create if missing)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py (append)
from spinlab.models import Waypoint

def test_waypoint_id_is_deterministic():
    a = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    b = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    assert a.id == b.id

def test_waypoint_id_differs_by_conditions():
    a = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "big"})
    b = Waypoint.make("game1", 5, "checkpoint", 1, {"powerup": "small"})
    assert a.id != b.id

def test_waypoint_conditions_are_canonical_json():
    # key order in input must not affect id
    a = Waypoint.make("g", 1, "goal", 0, {"a": 1, "b": 2})
    b = Waypoint.make("g", 1, "goal", 0, {"b": 2, "a": 1})
    assert a.id == b.id
    assert a.conditions_json == '{"a": 1, "b": 2}'

def test_empty_conditions():
    w = Waypoint.make("g", 1, "entrance", 0, {})
    assert w.conditions_json == "{}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v -k waypoint`
Expected: FAIL with ImportError.

- [ ] **Step 3: Add Waypoint to models.py**

Add after the `Segment` dataclass (around current line 116 in `python/spinlab/models.py`):

```python
import hashlib
import json

@dataclass
class Waypoint:
    id: str
    game_id: str
    level_number: int
    endpoint_type: EndpointType
    ordinal: int
    conditions_json: str     # canonical JSON (sorted keys)

    @staticmethod
    def make(game_id: str, level_number: int, endpoint_type: str,
             ordinal: int, conditions: dict) -> "Waypoint":
        canonical = json.dumps(conditions, sort_keys=True, separators=(", ", ": "))
        h = hashlib.sha256(
            f"{game_id}:{level_number}:{endpoint_type}.{ordinal}:{canonical}".encode()
        ).hexdigest()[:16]
        return Waypoint(
            id=h,
            game_id=game_id,
            level_number=level_number,
            endpoint_type=endpoint_type,
            ordinal=ordinal,
            conditions_json=canonical,
        )
```

If `hashlib` / `json` not already imported at top of `models.py`, add them.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v -k waypoint`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_models.py
git commit -m "feat: add Waypoint model with deterministic id"
```

---

## Task 3: Segment model — add waypoint FKs and is_primary

**Files:**
- Modify: `python/spinlab/models.py` (Segment dataclass + make_id)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py (append)
from spinlab.models import Segment

def test_segment_id_includes_waypoint_ids():
    wp_a = Waypoint.make("g", 5, "entrance", 0, {"powerup": "small"})
    wp_b = Waypoint.make("g", 5, "goal", 0, {"powerup": "small"})
    wp_c = Waypoint.make("g", 5, "entrance", 0, {"powerup": "big"})
    id_small = Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_a.id, wp_b.id)
    id_big   = Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_c.id, wp_b.id)
    assert id_small != id_big
    # Same waypoints → same segment id
    assert id_small == Segment.make_id("g", 5, "entrance", 0, "goal", 0, wp_a.id, wp_b.id)

def test_segment_is_primary_default_true():
    wp_a = Waypoint.make("g", 1, "entrance", 0, {})
    wp_b = Waypoint.make("g", 1, "goal", 0, {})
    seg = Segment(
        id=Segment.make_id("g", 1, "entrance", 0, "goal", 0, wp_a.id, wp_b.id),
        game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="goal", end_ordinal=0,
        start_waypoint_id=wp_a.id, end_waypoint_id=wp_b.id,
    )
    assert seg.is_primary is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v -k "segment_id_includes or is_primary"`
Expected: FAIL — `make_id` signature mismatch, `start_waypoint_id` not a Segment field.

- [ ] **Step 3: Update Segment dataclass and make_id**

In `python/spinlab/models.py`, update the `Segment` dataclass:

```python
@dataclass
class Segment:
    id: str
    game_id: str
    level_number: int
    start_type: EndpointType
    start_ordinal: int
    end_type: EndpointType
    end_ordinal: int
    description: str = ""
    strat_version: int = 1
    active: bool = True
    ordinal: Optional[int] = None
    reference_id: Optional[str] = None
    start_waypoint_id: Optional[str] = None
    end_waypoint_id: Optional[str] = None
    is_primary: bool = True

    @staticmethod
    def make_id(game_id: str, level: int, start_type: str, start_ord: int,
                end_type: str, end_ord: int,
                start_waypoint_id: str, end_waypoint_id: str) -> str:
        return (f"{game_id}:{level}:{start_type}.{start_ord}:{end_type}.{end_ord}"
                f":{start_waypoint_id[:8]}:{end_waypoint_id[:8]}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v -k "segment_id_includes or is_primary"`
Expected: 2 tests PASS.

- [ ] **Step 5: Fix existing call sites of Segment.make_id**

Run: `grep -rn "Segment.make_id" python/ tests/ --include="*.py"`

For every existing call site that currently passes 6 args, pass two placeholder waypoint IDs derived from empty-conditions waypoints (the following task will replace these). For now, quick fix: in each call site, insert two `""` string args and add a type: ignore if needed — OR, better, delegate to Task 4's capture rewrite. If any call sites are in test helpers or non-capture code, update them to pass empty-conditions waypoints explicitly.

(Expect 2-4 call sites: `reference_capture.py` twice, possibly `tests/` fixtures.)

- [ ] **Step 6: Run fast tests to verify nothing else is broken that this task owns**

Run: `pytest -m "not (emulator or slow)" -x`
Expected: anything failing should now trace back to Task 4's capture rewrite (not this task). If a non-capture test fails with a `make_id` TypeError, fix that call site here.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/models.py tests/test_models.py python/spinlab/reference_capture.py
git commit -m "feat: add waypoint FKs and is_primary to Segment; update make_id signature"
```

---

## Task 4: Attempt model — add observed conditions + invalidated

**Files:**
- Modify: `python/spinlab/models.py` (Attempt dataclass)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py (append)
from spinlab.models import Attempt, AttemptSource

def test_attempt_has_observed_conditions_and_invalidated():
    a = Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE, deaths=0,
        observed_start_conditions='{"powerup": "big"}',
        observed_end_conditions='{"powerup": "small"}',
    )
    assert a.observed_start_conditions == '{"powerup": "big"}'
    assert a.observed_end_conditions == '{"powerup": "small"}'
    assert a.invalidated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v -k "observed_conditions"`
Expected: FAIL — unknown kwargs.

- [ ] **Step 3: Add fields to Attempt**

In `python/spinlab/models.py`, on the `Attempt` dataclass:

```python
    observed_start_conditions: str | None = None
    observed_end_conditions: str | None = None
    invalidated: bool = False
```

Keep them at the end so existing positional-arg usages continue to work.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v -k "observed_conditions"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/models.py tests/test_models.py
git commit -m "feat: add observed conditions + invalidated to Attempt"
```

---

## Task 5: Add waypoints + waypoint_save_states tables; amend schema

**Files:**
- Modify: `python/spinlab/db/core.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waypoints_db.py
from spinlab.db import Database

def test_waypoints_table_exists():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(waypoints)").fetchall()}
    assert cols == {"id", "game_id", "level_number", "endpoint_type",
                    "ordinal", "conditions_json"}

def test_waypoint_save_states_table_exists():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute(
        "PRAGMA table_info(waypoint_save_states)").fetchall()}
    assert cols == {"waypoint_id", "variant_type", "state_path", "is_default"}

def test_segments_table_has_waypoint_columns():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(segments)").fetchall()}
    assert "start_waypoint_id" in cols
    assert "end_waypoint_id" in cols
    assert "is_primary" in cols

def test_attempts_table_has_condition_columns():
    db = Database(":memory:")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(attempts)").fetchall()}
    assert "observed_start_conditions" in cols
    assert "observed_end_conditions" in cols
    assert "invalidated" in cols

def test_segment_variants_table_dropped():
    db = Database(":memory:")
    row = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_variants'"
    ).fetchone()
    assert row is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waypoints_db.py -v`
Expected: FAIL — tables missing or old columns missing.

- [ ] **Step 3: Update SCHEMA in db/core.py**

In `python/spinlab/db/core.py`:

1. Remove the `segment_variants` CREATE TABLE entirely (lines 35-41).
2. Add waypoints + waypoint_save_states tables.
3. Amend `segments` with new columns.
4. Amend `attempts` with new columns.
5. Update `_init_schema` to drop `segment_variants`, `segments`, `attempts`, and the new tables if their columns don't match expectations.

Replacement SCHEMA snippets:

```python
# Add after the games table, before segments:
CREATE TABLE IF NOT EXISTS waypoints (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  endpoint_type TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  conditions_json TEXT NOT NULL DEFAULT '{}'
);

# Amend segments: add start_waypoint_id, end_waypoint_id, is_primary columns:
CREATE TABLE IF NOT EXISTS segments (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games(id),
  level_number INTEGER NOT NULL,
  start_type TEXT NOT NULL,
  start_ordinal INTEGER NOT NULL DEFAULT 0,
  end_type TEXT NOT NULL,
  end_ordinal INTEGER NOT NULL DEFAULT 0,
  start_waypoint_id TEXT REFERENCES waypoints(id),
  end_waypoint_id TEXT REFERENCES waypoints(id),
  is_primary INTEGER DEFAULT 1,
  description TEXT DEFAULT '',
  strat_version INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,
  ordinal INTEGER,
  reference_id TEXT REFERENCES capture_runs(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

# Replace segment_variants:
CREATE TABLE IF NOT EXISTS waypoint_save_states (
  waypoint_id TEXT NOT NULL REFERENCES waypoints(id),
  variant_type TEXT NOT NULL,
  state_path TEXT NOT NULL,
  is_default INTEGER DEFAULT 0,
  PRIMARY KEY (waypoint_id, variant_type)
);

# Amend attempts: add three columns:
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT NOT NULL,
  completed INTEGER NOT NULL,
  time_ms INTEGER,
  strat_version INTEGER NOT NULL,
  source TEXT DEFAULT 'practice',
  deaths INTEGER DEFAULT 0,
  clean_tail_ms INTEGER,
  observed_start_conditions TEXT,
  observed_end_conditions TEXT,
  invalidated INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
```

Update `_init_schema` stale-drop list and `_expected_columns` to include `segments`, `waypoints`, and `waypoint_save_states`:

```python
def _init_schema(self) -> None:
    stale_tables = ["splits", "segment_variants"]  # drop legacy table unconditionally
    for table in ["model_state", "attempts", "segments", "waypoints", "waypoint_save_states"]:
        cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and cols != self._expected_columns(table):
            stale_tables.append(table)
    if stale_tables:
        drops = "; ".join(f"DROP TABLE IF EXISTS {t}" for t in stale_tables)
        self.conn.executescript(drops + ";")
    self.conn.executescript(SCHEMA)
    self.conn.commit()
    # ... retain existing ALTER TABLE migration attempts below ...
```

And extend `_expected_columns`:

```python
@staticmethod
def _expected_columns(table: str) -> set[str]:
    return {
        "model_state": {"segment_id", "estimator", "state_json", "output_json", "updated_at"},
        "attempts": {"id", "segment_id", "session_id", "completed", "time_ms",
                     "strat_version", "source", "deaths", "clean_tail_ms",
                     "observed_start_conditions", "observed_end_conditions",
                     "invalidated", "created_at"},
        "segments": {"id", "game_id", "level_number", "start_type", "start_ordinal",
                     "end_type", "end_ordinal", "start_waypoint_id", "end_waypoint_id",
                     "is_primary", "description", "strat_version", "active", "ordinal",
                     "reference_id", "created_at", "updated_at"},
        "waypoints": {"id", "game_id", "level_number", "endpoint_type",
                      "ordinal", "conditions_json"},
        "waypoint_save_states": {"waypoint_id", "variant_type", "state_path", "is_default"},
    }.get(table, set())
```

Also delete the post-schema `UPDATE segments SET reference_id = ...` backfill (lines 138-145) — there is no data to backfill now.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_waypoints_db.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/db/core.py tests/test_waypoints_db.py
git commit -m "feat(db): add waypoints + waypoint_save_states tables; amend segments/attempts schema"
```

---

## Task 6: WaypointsMixin — CRUD

**Files:**
- Create: `python/spinlab/db/waypoints.py`
- Modify: `python/spinlab/db/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waypoints_db.py (append)
from spinlab.models import Waypoint

def test_upsert_and_get_waypoint():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 5, "checkpoint", 1, {"powerup": "big"})
    db.upsert_waypoint(w)
    got = db.get_waypoint(w.id)
    assert got is not None
    assert got.id == w.id
    assert got.conditions_json == w.conditions_json
    assert got.endpoint_type == "checkpoint"

def test_upsert_waypoint_idempotent():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 5, "goal", 0, {"powerup": "small"})
    db.upsert_waypoint(w)
    db.upsert_waypoint(w)
    rows = db.conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()
    assert rows[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waypoints_db.py -v -k upsert_and_get`
Expected: FAIL — `upsert_waypoint` undefined.

- [ ] **Step 3: Create WaypointsMixin**

```python
# python/spinlab/db/waypoints.py
"""Waypoint CRUD."""

from ..models import Waypoint


class WaypointsMixin:
    def upsert_waypoint(self, w: Waypoint) -> None:
        self.conn.execute(
            """INSERT INTO waypoints
               (id, game_id, level_number, endpoint_type, ordinal, conditions_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (w.id, w.game_id, w.level_number, w.endpoint_type,
             w.ordinal, w.conditions_json),
        )
        self.conn.commit()

    def get_waypoint(self, waypoint_id: str) -> Waypoint | None:
        row = self.conn.execute(
            """SELECT id, game_id, level_number, endpoint_type, ordinal, conditions_json
               FROM waypoints WHERE id = ?""",
            (waypoint_id,),
        ).fetchone()
        if row is None:
            return None
        return Waypoint(
            id=row["id"],
            game_id=row["game_id"],
            level_number=row["level_number"],
            endpoint_type=row["endpoint_type"],
            ordinal=row["ordinal"],
            conditions_json=row["conditions_json"],
        )
```

- [ ] **Step 4: Wire mixin into Database**

In `python/spinlab/db/__init__.py`, add:

```python
from .waypoints import WaypointsMixin
```

And add `WaypointsMixin` to the `Database` class's base list.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_waypoints_db.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/db/waypoints.py python/spinlab/db/__init__.py tests/test_waypoints_db.py
git commit -m "feat(db): add WaypointsMixin with upsert/get"
```

---

## Task 7: Replace add_variant/get_variants with waypoint-save-state helpers

**Files:**
- Modify: `python/spinlab/db/segments.py`
- Modify: `python/spinlab/models.py` (SegmentVariant → rename to WaypointSaveState)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waypoints_db.py (append)
from spinlab.models import Waypoint, WaypointSaveState

def test_save_state_attaches_to_waypoint():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 1, "checkpoint", 1, {})
    db.upsert_waypoint(w)
    db.add_save_state(WaypointSaveState(
        waypoint_id=w.id, variant_type="hot",
        state_path="/tmp/hot.mss", is_default=True))
    got = db.get_save_state(w.id, "hot")
    assert got is not None
    assert got.state_path == "/tmp/hot.mss"

def test_get_default_save_state_falls_back_to_any():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    w = Waypoint.make("g1", 1, "checkpoint", 1, {})
    db.upsert_waypoint(w)
    db.add_save_state(WaypointSaveState(
        waypoint_id=w.id, variant_type="cold",
        state_path="/tmp/cold.mss", is_default=False))
    got = db.get_default_save_state(w.id)
    assert got is not None
    assert got.variant_type == "cold"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waypoints_db.py -v -k save_state`
Expected: FAIL — `WaypointSaveState` / `add_save_state` / `get_save_state` / `get_default_save_state` undefined.

- [ ] **Step 3: Rename SegmentVariant to WaypointSaveState**

In `python/spinlab/models.py`, replace the existing `SegmentVariant` dataclass with:

```python
@dataclass
class WaypointSaveState:
    waypoint_id: str
    variant_type: str        # 'cold', 'hot'
    state_path: str
    is_default: bool = False
```

- [ ] **Step 4: Replace variant helpers in SegmentsMixin**

In `python/spinlab/db/segments.py`, remove `add_variant`, `get_variants`, `get_variant`, `get_default_variant` and replace with:

```python
from ..models import WaypointSaveState

    def add_save_state(self, s: WaypointSaveState) -> None:
        self.conn.execute(
            """INSERT INTO waypoint_save_states
               (waypoint_id, variant_type, state_path, is_default)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(waypoint_id, variant_type) DO UPDATE SET
                 state_path=excluded.state_path,
                 is_default=excluded.is_default""",
            (s.waypoint_id, s.variant_type, s.state_path, int(s.is_default)),
        )
        self.conn.commit()

    def get_save_state(self, waypoint_id: str,
                       variant_type: str) -> WaypointSaveState | None:
        row = self.conn.execute(
            """SELECT waypoint_id, variant_type, state_path, is_default
               FROM waypoint_save_states
               WHERE waypoint_id = ? AND variant_type = ?""",
            (waypoint_id, variant_type),
        ).fetchone()
        if row is None:
            return None
        return WaypointSaveState(
            waypoint_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )

    def get_default_save_state(self, waypoint_id: str) -> WaypointSaveState | None:
        row = self.conn.execute(
            """SELECT waypoint_id, variant_type, state_path, is_default
               FROM waypoint_save_states WHERE waypoint_id = ?
               ORDER BY is_default DESC LIMIT 1""",
            (waypoint_id,),
        ).fetchone()
        if row is None:
            return None
        return WaypointSaveState(
            waypoint_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )
```

- [ ] **Step 5: Run new test to verify it passes**

Run: `pytest tests/test_waypoints_db.py -v -k save_state`
Expected: PASS.

- [ ] **Step 6: Fix callers**

Run: `grep -rn "SegmentVariant\|add_variant\|get_variants\|get_variant\|get_default_variant" python/ tests/ --include="*.py"`

Update every caller. Most will be in `reference_capture.py` (handled in Task 10) and `tests/`. For the time being:

- In `reference_capture.py`: change `SegmentVariant` → `WaypointSaveState`, `db.add_variant(...)` → `db.add_save_state(...)`, construct with `waypoint_id` instead of `segment_id`. Stub the waypoint_id with the segment's start_waypoint_id field if it exists on the Segment, else leave the call site as a `# TODO Task 10` — Task 10 replaces these thoroughly.
- In tests: replace the symbol names, pick a concrete waypoint id like `"wp_test_1"` to satisfy arguments.

- [ ] **Step 7: Run fast tests**

Run: `pytest -m "not (emulator or slow)" -x`
Expected: remaining failures concentrate in reference capture / session flow tests (resolved in Task 10). Anything else (DB-level tests, unit tests) should pass.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/models.py python/spinlab/db/segments.py tests/
git commit -m "refactor: replace SegmentVariant with WaypointSaveState; rework DB helpers"
```

---

## Task 8: Update get_all_segments_with_model to join waypoints

**Files:**
- Modify: `python/spinlab/db/segments.py` (`get_all_segments_with_model`, `segments_missing_cold`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waypoints_db.py (append)
from spinlab.models import Segment

def _make_seg_with_waypoints(db, game_id, level, start_type, start_ord,
                             end_type, end_ord, start_conds, end_conds,
                             hot_path):
    wp_start = Waypoint.make(game_id, level, start_type, start_ord, start_conds)
    wp_end = Waypoint.make(game_id, level, end_type, end_ord, end_conds)
    db.upsert_waypoint(wp_start)
    db.upsert_waypoint(wp_end)
    seg = Segment(
        id=Segment.make_id(game_id, level, start_type, start_ord,
                           end_type, end_ord, wp_start.id, wp_end.id),
        game_id=game_id, level_number=level,
        start_type=start_type, start_ordinal=start_ord,
        end_type=end_type, end_ordinal=end_ord,
        start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
        is_primary=True,
    )
    db.upsert_segment(seg)
    db.add_save_state(WaypointSaveState(
        waypoint_id=wp_start.id, variant_type="hot",
        state_path=hot_path, is_default=True))
    return seg

def test_all_segments_with_model_returns_save_state_path():
    db = Database(":memory:")
    db.upsert_game("g", "Game", "any%")
    seg = _make_seg_with_waypoints(
        db, "g", 1, "entrance", 0, "goal", 0, {}, {}, "/tmp/s.mss")
    rows = db.get_all_segments_with_model("g")
    assert len(rows) == 1
    assert rows[0]["id"] == seg.id
    assert rows[0]["state_path"] == "/tmp/s.mss"
    assert rows[0]["is_primary"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waypoints_db.py::test_all_segments_with_model_returns_save_state_path -v`
Expected: FAIL — `upsert_segment` TypeError (doesn't know about waypoint/is_primary columns), or state_path NULL.

- [ ] **Step 3: Update upsert_segment to write waypoint/is_primary columns**

In `python/spinlab/db/segments.py`, update `upsert_segment`:

```python
def upsert_segment(self, seg: Segment) -> None:
    now = datetime.now(UTC).isoformat()
    self.conn.execute(
        """INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,
           end_type, end_ordinal, start_waypoint_id, end_waypoint_id, is_primary,
           description, strat_version, active, ordinal,
           reference_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             description=excluded.description,
             ordinal=excluded.ordinal,
             reference_id=excluded.reference_id,
             active=excluded.active,
             is_primary=excluded.is_primary,
             updated_at=excluded.updated_at""",
        (seg.id, seg.game_id, seg.level_number, seg.start_type,
         seg.start_ordinal, seg.end_type, seg.end_ordinal,
         seg.start_waypoint_id, seg.end_waypoint_id, int(seg.is_primary),
         seg.description, seg.strat_version, int(seg.active),
         seg.ordinal, seg.reference_id, now, now),
    )
    self.conn.commit()
```

- [ ] **Step 4: Update get_all_segments_with_model to JOIN waypoint_save_states**

```python
def get_all_segments_with_model(self, game_id: str) -> list[SegmentRow]:
    """Get all active segments with their start-waypoint save state path."""
    cur = self.conn.execute(
        """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                  s.end_type, s.end_ordinal, s.description, s.strat_version,
                  s.active, s.ordinal, s.is_primary,
                  s.start_waypoint_id, s.end_waypoint_id,
                  (SELECT wss.state_path FROM waypoint_save_states wss
                   WHERE wss.waypoint_id = s.start_waypoint_id
                   ORDER BY wss.is_default DESC LIMIT 1) AS state_path
           FROM segments s
           WHERE s.game_id = ? AND s.active = 1
           ORDER BY s.ordinal, s.level_number""",
        (game_id,),
    )
    actual_cols = [desc[0] for desc in cur.description]
    return [dict(zip(actual_cols, row)) for row in cur.fetchall()]
```

Also extend `SegmentRow` TypedDict with `is_primary: int`, `start_waypoint_id: str | None`, `end_waypoint_id: str | None`.

- [ ] **Step 5: Update _row_to_segment in db/core.py**

Append waypoint/is_primary fields when constructing the Segment:

```python
@staticmethod
def _row_to_segment(row: sqlite3.Row) -> Segment:
    keys = row.keys()
    return Segment(
        id=row["id"],
        game_id=row["game_id"],
        level_number=row["level_number"],
        start_type=row["start_type"],
        start_ordinal=row["start_ordinal"],
        end_type=row["end_type"],
        end_ordinal=row["end_ordinal"],
        description=row["description"] or "",
        strat_version=row["strat_version"],
        active=bool(row["active"]),
        ordinal=row["ordinal"] if "ordinal" in keys else None,
        reference_id=row["reference_id"] if "reference_id" in keys else None,
        start_waypoint_id=row["start_waypoint_id"] if "start_waypoint_id" in keys else None,
        end_waypoint_id=row["end_waypoint_id"] if "end_waypoint_id" in keys else None,
        is_primary=bool(row["is_primary"]) if "is_primary" in keys else True,
    )
```

- [ ] **Step 6: Update segments_missing_cold**

```python
def segments_missing_cold(self, game_id: str) -> list[MissingColdRow]:
    """Return segments whose start waypoint has hot but not cold save state."""
    rows = self.conn.execute(
        """SELECT s.id AS segment_id, hot.state_path AS hot_state_path,
                  s.level_number, s.start_type, s.start_ordinal,
                  s.end_type, s.end_ordinal, s.description
           FROM segments s
           JOIN waypoint_save_states hot
             ON hot.waypoint_id = s.start_waypoint_id AND hot.variant_type = 'hot'
           LEFT JOIN waypoint_save_states cold
             ON cold.waypoint_id = s.start_waypoint_id AND cold.variant_type = 'cold'
           WHERE s.game_id = ? AND s.active = 1 AND cold.waypoint_id IS NULL
           ORDER BY s.ordinal, s.level_number, s.start_ordinal""",
        (game_id,),
    ).fetchall()
    cols = ["segment_id", "hot_state_path", "level_number",
            "start_type", "start_ordinal", "end_type", "end_ordinal", "description"]
    return [dict(zip(cols, r)) for r in rows]
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_waypoints_db.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/db/segments.py python/spinlab/db/core.py
git commit -m "feat(db): join waypoint_save_states in segment queries"
```

---

## Task 9: Lua — set_conditions command and reading conditions at transitions

**Files:**
- Modify: `lua/spinlab.lua`
- Modify: `tests/integration/test_lua_basic.py` or similar emulator test file

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_lua_conditions.py (new)
import pytest
import json
from tests.integration.helpers import run_lua_test  # existing test runner helper

pytestmark = pytest.mark.emulator

def test_lua_set_conditions_and_reports_at_transition():
    """Python sends set_conditions with powerup address; Lua includes it in events."""
    # Use existing test harness pattern. Expected: after calling
    # set_conditions with [{"name": "powerup", "address": 0x19, "size": 1}],
    # a level_entrance event should include a "conditions" key with powerup's raw value.
    events = run_lua_test(
        rom="smw_test",
        setup_commands=[
            'set_conditions:' + json.dumps([{"name": "powerup", "address": 0x19, "size": 1}])
        ],
        expected_events=["level_entrance"],
    )
    entrance = [e for e in events if e["event"] == "level_entrance"][0]
    assert "conditions" in entrance
    assert "powerup" in entrance["conditions"]
    assert isinstance(entrance["conditions"]["powerup"], int)
```

(Adapt to the existing integration test harness — consult `tests/integration/` for actual helper names and patterns.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_lua_conditions.py -v -m emulator`
Expected: FAIL — `set_conditions` unknown command, or `conditions` key missing.

- [ ] **Step 3: Add condition-reading to Lua**

In `lua/spinlab.lua`:

(a) Add a module-level cache near the top (below other globals):

```lua
-- Condition definitions, populated via TCP set_conditions command.
-- Each entry: { name=string, address=int, size=int }
local condition_defs = {}
```

(b) Add a helper function:

```lua
local function read_conditions()
  local out = {}
  for _, d in ipairs(condition_defs) do
    if d.size == 1 then
      out[d.name] = emu.read(d.address, emu.memType.cpu, false)
    elseif d.size == 2 then
      out[d.name] = emu.readWord(d.address, emu.memType.cpu, false)
    else
      -- Deliberately crash loud: unexpected size
      error("unsupported condition size: " .. tostring(d.size))
    end
  end
  return out
end
```

(c) Add a `set_conditions` handler to the `prefixed_commands` table (currently at ~lines 1010-1060 of spinlab.lua):

```lua
["set_conditions"] = function(arg)
  -- arg is JSON: [{"name": "...", "address": N, "size": N}, ...]
  local ok, decoded = pcall(function() return json.decode(arg) end)
  if not ok or type(decoded) ~= "table" then
    log("set_conditions: invalid JSON")
    return
  end
  condition_defs = decoded
  log("set_conditions: loaded " .. #condition_defs .. " conditions")
end,
```

(d) In every transition event builder (search for `event =` or `send_event` and find `level_entrance`, `checkpoint`, `level_exit`, `death`, `spawn` event builders, approximately lines 472-591), add `conditions = read_conditions()` to the event table before sending.

Example edit pattern:

```lua
-- BEFORE
local evt = { event = "level_entrance", level = level_num, ... }
send_event(evt)

-- AFTER
local evt = { event = "level_entrance", level = level_num, ...,
              conditions = read_conditions() }
send_event(evt)
```

(e) If `json` library is not already required/in scope at the top of spinlab.lua, add `local json = require("json")` or confirm the existing JSON encode/decode helper name and reuse it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_lua_conditions.py -v -m emulator`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lua/spinlab.lua tests/integration/test_lua_conditions.py
git commit -m "feat(lua): add set_conditions command; read conditions at transitions"
```

---

## Task 10: Rewrite reference_capture to create waypoints

**Files:**
- Modify: `python/spinlab/reference_capture.py`
- Modify: `python/spinlab/session_manager.py` (or whichever owns ReferenceCapture) to pass `ConditionRegistry`
- Create: `tests/test_capture_with_conditions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture_with_conditions.py
from spinlab.db import Database
from spinlab.reference_capture import ReferenceCapture
from spinlab.condition_registry import ConditionRegistry, ConditionDef, Scope

def _registry():
    return ConditionRegistry(definitions=[
        ConditionDef(name="powerup", address=0x19, size=1, type="enum",
                     values={0: "small", 1: "big"}, scope=Scope.game()),
    ])

def _bootstrap_db():
    db = Database(":memory:")
    db.upsert_game("g1", "Game", "any%")
    return db

def test_entrance_then_goal_creates_segment_with_waypoints():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    cap.handle_entrance({
        "level": 5, "state_path": "/tmp/start.mss",
        "conditions": {"powerup": 0},  # raw: small
    })
    cap.handle_exit(
        {"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
        "g1", db, reg)
    segs = db.get_active_segments("g1")
    assert len(segs) == 1
    assert segs[0].start_waypoint_id is not None
    assert segs[0].end_waypoint_id is not None
    assert segs[0].is_primary is True
    wp = db.get_waypoint(segs[0].start_waypoint_id)
    assert wp is not None
    assert '"powerup": "small"' in wp.conditions_json

def test_same_geography_different_powerup_creates_two_segments():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    # Run 1: entered small, exited
    cap.handle_entrance({"level": 5, "state_path": "/tmp/s1.mss",
                         "conditions": {"powerup": 0}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
                    "g1", db, reg)
    # Run 2: entered big, exited
    cap.pending_start = None  # reset
    cap.handle_entrance({"level": 5, "state_path": "/tmp/s2.mss",
                         "conditions": {"powerup": 1}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 1}},
                    "g1", db, reg)
    segs = db.get_active_segments("g1")
    assert len(segs) == 2
    primary_count = sum(1 for s in segs if s.is_primary)
    assert primary_count == 1     # second segment is NOT primary

def test_save_state_attaches_to_start_waypoint():
    db = _bootstrap_db()
    cap = ReferenceCapture()
    cap.capture_run_id = "run1"
    reg = _registry()
    cap.handle_entrance({"level": 5, "state_path": "/tmp/start.mss",
                         "conditions": {"powerup": 0}})
    cap.handle_exit({"level": 5, "goal": "goal", "conditions": {"powerup": 0}},
                    "g1", db, reg)
    segs = db.get_active_segments("g1")
    ss = db.get_default_save_state(segs[0].start_waypoint_id)
    assert ss is not None
    assert ss.state_path == "/tmp/start.mss"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capture_with_conditions.py -v`
Expected: FAIL — `handle_exit` signature mismatch (no `registry` arg) and/or segments missing waypoints.

- [ ] **Step 3: Rewrite handle_entrance / handle_checkpoint / handle_exit / handle_spawn**

Full rewrite of `python/spinlab/reference_capture.py`. Key changes:

- Add `registry: ConditionRegistry` parameter to `handle_checkpoint`, `handle_exit`, `handle_spawn`.
- Store observed raw conditions on `pending_start`.
- On each segment creation, decode via registry, create start + end waypoints, attach save state to start waypoint, compute `is_primary` by checking if any other segment exists with the same geography.

```python
# python/spinlab/reference_capture.py (rewritten)
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database
    from .condition_registry import ConditionRegistry

logger = logging.getLogger(__name__)


class ReferenceCapture:
    def __init__(self) -> None:
        self.segments_count: int = 0
        self.capture_run_id: str | None = None
        self.pending_start: dict | None = None
        self.died: bool = False
        self.rec_path: str | None = None

    def clear(self) -> None:
        self.segments_count = 0
        self.capture_run_id = None
        self.pending_start = None
        self.died = False
        self.rec_path = None

    def enter_draft(self) -> tuple[str | None, int]:
        return self.capture_run_id, self.segments_count

    def handle_entrance(self, event: dict) -> None:
        if self.pending_start and self.pending_start["type"] != "entrance":
            logger.info("Ignoring level_entrance — pending start exists: %s", self.pending_start)
            return
        self.pending_start = {
            "type": "entrance",
            "ordinal": 0,
            "state_path": event.get("state_path"),
            "timestamp_ms": 0,
            "level_num": event["level"],
            "raw_conditions": event.get("conditions", {}),
        }
        self.died = False

    def _close_segment(self, db, game_id, start, end_type, end_ordinal,
                       level, end_raw_conditions, registry) -> None:
        from .models import Segment, Waypoint, WaypointSaveState

        start_conds = registry.decode(start["raw_conditions"], level=level)
        end_conds = registry.decode(end_raw_conditions, level=level)

        wp_start = Waypoint.make(game_id, level, start["type"], start["ordinal"], start_conds)
        wp_end = Waypoint.make(game_id, level, end_type, end_ordinal, end_conds)
        db.upsert_waypoint(wp_start)
        db.upsert_waypoint(wp_end)

        seg_id = Segment.make_id(
            game_id, level, start["type"], start["ordinal"],
            end_type, end_ordinal, wp_start.id, wp_end.id,
        )
        is_primary = self._compute_is_primary(
            db, game_id, level, start["type"], start["ordinal"],
            end_type, end_ordinal, seg_id)
        self.segments_count += 1
        seg = Segment(
            id=seg_id, game_id=game_id, level_number=level,
            start_type=start["type"], start_ordinal=start["ordinal"],
            end_type=end_type, end_ordinal=end_ordinal,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=is_primary,
            ordinal=self.segments_count,
            reference_id=self.capture_run_id,
        )
        db.upsert_segment(seg)

        state_path = start.get("state_path")
        if state_path:
            variant = "cold" if start["type"] == "entrance" else "hot"
            db.add_save_state(WaypointSaveState(
                waypoint_id=wp_start.id,
                variant_type=variant,
                state_path=state_path,
                is_default=True,
            ))

    @staticmethod
    def _compute_is_primary(db, game_id, level, start_type, start_ord,
                            end_type, end_ord, new_seg_id) -> bool:
        """Return True iff no other active segment exists for this geography."""
        row = db.conn.execute(
            """SELECT id FROM segments
               WHERE game_id = ? AND level_number = ?
               AND start_type = ? AND start_ordinal = ?
               AND end_type = ? AND end_ordinal = ?
               AND active = 1 AND id != ?""",
            (game_id, level, start_type, start_ord, end_type, end_ord, new_seg_id),
        ).fetchone()
        return row is None

    def handle_checkpoint(self, event: dict, game_id: str,
                          db: "Database", registry: "ConditionRegistry") -> None:
        if not self.pending_start:
            return
        cp_ordinal = event.get("cp_ordinal", 1)
        level = event.get("level_num", self.pending_start["level_num"])
        self._close_segment(
            db, game_id, self.pending_start, "checkpoint", cp_ordinal,
            level, event.get("conditions", {}), registry)
        self.pending_start = {
            "type": "checkpoint",
            "ordinal": cp_ordinal,
            "state_path": event.get("state_path"),
            "timestamp_ms": event.get("timestamp_ms", 0),
            "level_num": level,
            "raw_conditions": event.get("conditions", {}),
        }

    def handle_exit(self, event: dict, game_id: str,
                    db: "Database", registry: "ConditionRegistry") -> None:
        goal = event.get("goal", "abort")
        if goal == "abort":
            self.pending_start = None
            return
        if not self.pending_start:
            return
        level = event["level"]
        self._close_segment(
            db, game_id, self.pending_start, "goal", 0,
            level, event.get("conditions", {}), registry)
        self.pending_start = None

    def handle_spawn(self, event: dict, game_id: str,
                     db: "Database", registry: "ConditionRegistry") -> None:
        """Store cold save state on checkpoint waypoint (powerup typically 'small' after death)."""
        if not event.get("is_cold_cp") or not event.get("state_captured"):
            return
        cold_path = event.get("state_path")
        level = event.get("level_num")
        cp_ord = event.get("cp_ordinal")
        if cold_path is None or level is None or cp_ord is None:
            return
        from .models import Waypoint, WaypointSaveState
        conds = registry.decode(event.get("conditions", {}), level=level)
        wp = Waypoint.make(game_id, level, "checkpoint", cp_ord, conds)
        db.upsert_waypoint(wp)
        db.add_save_state(WaypointSaveState(
            waypoint_id=wp.id, variant_type="cold",
            state_path=cold_path, is_default=True))
        logger.debug("Stored cold save state for waypoint %s: %s", wp.id, cold_path)
```

- [ ] **Step 4: Update ReferenceCapture callers**

Run: `grep -rn "handle_checkpoint\|handle_exit\|handle_spawn" python/ --include="*.py"`

In every caller (likely `session_manager.py` or `capture_controller.py`), thread the registry through. The session manager should hold a `ConditionRegistry` instance loaded at startup via the next task.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_capture_with_conditions.py -v`
Expected: 3 tests PASS.

- [ ] **Step 6: Run all fast tests**

Run: `pytest -m "not (emulator or slow)" -x`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/reference_capture.py python/spinlab/session_manager.py tests/test_capture_with_conditions.py
git commit -m "feat: create waypoints during reference capture, assign is_primary"
```

---

## Task 11: Load ConditionRegistry at startup + send set_conditions on TCP connect

**Files:**
- Modify: `python/spinlab/session_manager.py` (or whichever owns the TCP connect lifecycle)
- Modify: the module that sends TCP commands to Lua (grep for `practice_load` to find it)

- [ ] **Step 1: Locate the TCP-connect and command-send sites**

Run: `grep -rn "practice_load\|reference_start" python/ --include="*.py"`
Run: `grep -rn "rom_info\|game_id" python/ --include="*.py" | head -n 20`

Identify:
- Where `rom_info` is first received (i.e. game_id known).
- Where Python sends newline-delimited JSON commands to Lua.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_condition_registry_startup.py
from pathlib import Path
from spinlab.condition_registry import ConditionRegistry, load_registry_for_game

def test_loads_registry_from_games_directory(tmp_path: Path, monkeypatch):
    # Create a fake games dir
    games_dir = tmp_path / "games" / "g1"
    games_dir.mkdir(parents=True)
    (games_dir / "conditions.yaml").write_text(
        "conditions:\n"
        "  - name: powerup\n"
        "    address: 0x19\n"
        "    size: 1\n"
        "    type: enum\n"
        "    values: { 0: small, 1: big }\n"
        "    scope: game\n"
    )
    reg = load_registry_for_game("g1", games_root=tmp_path / "games")
    assert len(reg.definitions) == 1
    assert reg.definitions[0].name == "powerup"

def test_missing_registry_returns_empty():
    reg = load_registry_for_game("nonexistent", games_root=Path("/tmp/nope"))
    assert reg.definitions == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_condition_registry_startup.py -v`
Expected: FAIL — `load_registry_for_game` undefined.

- [ ] **Step 4: Add helper**

In `python/spinlab/condition_registry.py`, append:

```python
def load_registry_for_game(game_id: str, games_root: Path | None = None) -> ConditionRegistry:
    """Load per-game conditions.yaml; return empty registry if file missing."""
    if games_root is None:
        games_root = Path(__file__).parent / "games"
    yaml_path = games_root / game_id / "conditions.yaml"
    if not yaml_path.exists():
        return ConditionRegistry(definitions=[])
    return ConditionRegistry.from_yaml(yaml_path)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_condition_registry_startup.py -v`
Expected: PASS.

- [ ] **Step 6: Wire registry into SessionManager**

In `python/spinlab/session_manager.py` (or wherever the game_id becomes known):

1. Import `from .condition_registry import ConditionRegistry, load_registry_for_game`.
2. Add attribute `self.condition_registry: ConditionRegistry = ConditionRegistry()` (empty default).
3. When `rom_info`/game_id is first received, set `self.condition_registry = load_registry_for_game(game_id)`.
4. Thread `self.condition_registry` into all `handle_checkpoint/handle_exit/handle_spawn` call sites.

- [ ] **Step 7: Send set_conditions to Lua after TCP connect**

Wherever the TCP client writes outgoing commands, add a `send_set_conditions(defs)` method that serialises to JSON and writes `f"set_conditions:{json_str}\n"`.

Call it immediately after `rom_info` is received and the registry is loaded:

```python
import json
defs_payload = [
    {"name": d.name, "address": d.address, "size": d.size}
    for d in self.condition_registry.definitions
]
self.tcp_client.send(f"set_conditions:{json.dumps(defs_payload)}\n")
```

- [ ] **Step 8: Run fast tests**

Run: `pytest -m "not (emulator or slow)" -x`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add python/spinlab/condition_registry.py python/spinlab/session_manager.py tests/test_condition_registry_startup.py
git commit -m "feat: load ConditionRegistry at startup, push to Lua via set_conditions"
```

---

## Task 12: Persist observed conditions on Attempts

**Files:**
- Modify: `python/spinlab/db/attempts.py`
- Modify: caller in `session_manager.py` / practice session that logs attempts

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attempts_conditions.py
from spinlab.db import Database
from spinlab.models import Attempt, AttemptSource

def test_log_attempt_persists_observed_conditions():
    db = Database(":memory:")
    db.upsert_game("g", "Game", "any%")
    # Minimal segment to satisfy FK
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal, "
        "end_type, end_ordinal, created_at, updated_at) "
        "VALUES ('s1', 'g', 1, 'entrance', 0, 'goal', 0, '2026-01-01', '2026-01-01')"
    )
    db.conn.commit()
    db.log_attempt(Attempt(
        segment_id="s1", session_id="sess1", completed=True,
        time_ms=1000, source=AttemptSource.PRACTICE,
        observed_start_conditions='{"powerup": "big"}',
        observed_end_conditions='{"powerup": "small"}',
    ))
    row = db.conn.execute(
        "SELECT observed_start_conditions, observed_end_conditions, invalidated "
        "FROM attempts WHERE segment_id = 's1'").fetchone()
    assert row[0] == '{"powerup": "big"}'
    assert row[1] == '{"powerup": "small"}'
    assert row[2] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attempts_conditions.py -v`
Expected: FAIL — `log_attempt` doesn't write the new columns.

- [ ] **Step 3: Update log_attempt**

In `python/spinlab/db/attempts.py`, find `log_attempt` and update the INSERT to include the three new columns, reading them from the `Attempt` dataclass.

```python
def log_attempt(self, a: Attempt) -> int:
    now = datetime.now(UTC).isoformat()
    cur = self.conn.execute(
        """INSERT INTO attempts
           (segment_id, session_id, completed, time_ms, strat_version, source,
            deaths, clean_tail_ms,
            observed_start_conditions, observed_end_conditions, invalidated,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (a.segment_id, a.session_id, int(a.completed), a.time_ms,
         a.strat_version, a.source.value, a.deaths, a.clean_tail_ms,
         a.observed_start_conditions, a.observed_end_conditions,
         int(a.invalidated), now),
    )
    self.conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attempts_conditions.py -v`
Expected: PASS.

- [ ] **Step 5: Pipe observed conditions through practice attempts**

Run: `grep -rn "log_attempt\|Attempt(" python/ --include="*.py"`

In the practice session path that constructs `Attempt` from `attempt_result` events, set `observed_start_conditions` / `observed_end_conditions` from the event payload (JSON-encoded) using `json.dumps(registry.decode(raw, level=...), sort_keys=True)`.

If this is intricate, write one targeted integration test that verifies attempts saved during practice carry the expected JSON.

- [ ] **Step 6: Run fast tests**

Run: `pytest -m "not (emulator or slow)" -x`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/db/attempts.py python/spinlab/session_manager.py tests/test_attempts_conditions.py
git commit -m "feat: persist observed conditions on attempts"
```

---

## Task 13: Expose waypoints + is_primary on /api/segments

**Files:**
- Modify: `python/spinlab/routes/segments.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_segments_route.py
from fastapi.testclient import TestClient
# Use whatever app-construction helper existing segments tests use.
# Pseudo-code — adapt to actual test harness:

def test_segments_endpoint_includes_waypoints_and_is_primary(client, seeded_db):
    # seeded_db fixture inserts one segment with known waypoints
    resp = client.get("/api/segments")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    r = rows[0]
    assert "start_waypoint_id" in r
    assert "end_waypoint_id" in r
    assert "is_primary" in r
    assert "start_conditions" in r    # decoded-from-waypoint
    assert "end_conditions" in r
```

(Adapt to project's FastAPI test pattern — look at existing `tests/test_routes_*.py` or similar.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_segments_route.py -v`
Expected: FAIL — keys missing.

- [ ] **Step 3: Update segments route**

In `python/spinlab/routes/segments.py`:

```python
import json
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/segments")
def list_segments(db=..., game_id=...):  # use existing DI pattern
    rows = db.get_all_segments_with_model(game_id)
    out = []
    for r in rows:
        start_wp = db.get_waypoint(r["start_waypoint_id"]) if r.get("start_waypoint_id") else None
        end_wp = db.get_waypoint(r["end_waypoint_id"]) if r.get("end_waypoint_id") else None
        r["start_conditions"] = json.loads(start_wp.conditions_json) if start_wp else {}
        r["end_conditions"] = json.loads(end_wp.conditions_json) if end_wp else {}
        r["is_primary"] = bool(r.get("is_primary", 1))
        out.append(r)
    return out
```

(Adapt to the actual DI / signature pattern in the existing file.)

- [ ] **Step 4: Update frontend types**

In `frontend/src/types.ts`, extend the Segment API response type with:

```ts
start_waypoint_id: string | null;
end_waypoint_id: string | null;
is_primary: boolean;
start_conditions: Record<string, string | boolean>;
end_conditions: Record<string, string | boolean>;
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_segments_route.py -v`
Expected: PASS.

- [ ] **Step 6: Type-check frontend**

Run: `cd frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/routes/segments.py frontend/src/types.ts tests/test_segments_route.py
git commit -m "feat(api): include waypoints, is_primary, decoded conditions on /api/segments"
```

---

## Task 14: Restrict practice loop to is_primary segments

**Files:**
- Modify: `python/spinlab/db/segments.py` (`get_all_segments_with_model`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waypoints_db.py (append)
def test_get_all_segments_filters_non_primary_when_flagged(db=None):
    db = Database(":memory:")
    db.upsert_game("g", "Game", "any%")
    # Seed two segments at same geography, one primary, one not
    wp1 = Waypoint.make("g", 1, "entrance", 0, {"powerup": "small"})
    wp2 = Waypoint.make("g", 1, "entrance", 0, {"powerup": "big"})
    wp_end = Waypoint.make("g", 1, "goal", 0, {})
    for w in (wp1, wp2, wp_end):
        db.upsert_waypoint(w)
    for wp_start, primary in ((wp1, True), (wp2, False)):
        seg = Segment(
            id=Segment.make_id("g", 1, "entrance", 0, "goal", 0, wp_start.id, wp_end.id),
            game_id="g", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="goal", end_ordinal=0,
            start_waypoint_id=wp_start.id, end_waypoint_id=wp_end.id,
            is_primary=primary,
        )
        db.upsert_segment(seg)

    rows_primary = db.get_all_segments_with_model("g", primary_only=True)
    assert len(rows_primary) == 1
    assert rows_primary[0]["is_primary"] == 1
    rows_all = db.get_all_segments_with_model("g", primary_only=False)
    assert len(rows_all) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waypoints_db.py -v -k primary`
Expected: FAIL — `primary_only` kwarg unknown.

- [ ] **Step 3: Add primary_only filter to get_all_segments_with_model**

```python
def get_all_segments_with_model(self, game_id: str, *,
                                primary_only: bool = True) -> list[SegmentRow]:
    primary_clause = "AND s.is_primary = 1" if primary_only else ""
    cur = self.conn.execute(
        f"""SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                   s.end_type, s.end_ordinal, s.description, s.strat_version,
                   s.active, s.ordinal, s.is_primary,
                   s.start_waypoint_id, s.end_waypoint_id,
                   (SELECT wss.state_path FROM waypoint_save_states wss
                    WHERE wss.waypoint_id = s.start_waypoint_id
                    ORDER BY wss.is_default DESC LIMIT 1) AS state_path
            FROM segments s
            WHERE s.game_id = ? AND s.active = 1 {primary_clause}
            ORDER BY s.ordinal, s.level_number""",
        (game_id,),
    )
    actual_cols = [desc[0] for desc in cur.description]
    return [dict(zip(actual_cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 4: Update call sites**

Run: `grep -rn "get_all_segments_with_model" python/ --include="*.py"`

- Practice loop call site → keep default (`primary_only=True`).
- Dashboard `/api/segments` route → pass `primary_only=False` (dashboard shows everything).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_waypoints_db.py -v -k primary`
Expected: PASS.

- [ ] **Step 6: Run full fast test suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/db/segments.py python/spinlab/ tests/
git commit -m "feat: practice loop serves only is_primary segments"
```

---

## Task 15: End-to-end verification

- [ ] **Step 1: Run the entire fast suite**

Run: `pytest -m "not (emulator or slow)"`
Expected: all pass.

- [ ] **Step 2: Run slow tests**

Run: `pytest -m slow`
Expected: all pass.

- [ ] **Step 3: Run emulator tests**

Run: `pytest -m emulator`
Expected: all pass (including new `test_lua_conditions`).

- [ ] **Step 4: Smoke-test manually (optional)**

1. Delete `~/.spinlab/*.db` or the configured data dir's DB to force fresh schema.
2. Start dashboard: `spinlab dashboard`.
3. Start Mesen2 with the Lua script.
4. Load SMW. Do a reference run through one level entering small, exit goal.
5. Check `/api/segments`: should show one segment with `start_conditions={"powerup": "small"}`, `is_primary=true`.
6. Do a second reference run entering the same level big.
7. Check `/api/segments`: should show two segments for that level, one primary, one not.

- [ ] **Step 5: Merge-ready commit**

```bash
git log --oneline -20
# Review the series.
```

No separate commit — all tasks already committed.

---

## Self-Review Checklist

- [x] Spec requirement: condition YAML schema → Task 1
- [x] Per-game / per-level scope → Task 1 (`test_in_scope_filtering`)
- [x] Enum + bool decoding → Task 1
- [x] Waypoint model with deterministic id → Task 2
- [x] Segment gains start/end waypoint FKs + is_primary → Task 3
- [x] Attempt gains observed conditions + invalidated → Task 4 (schema) + Task 12 (persistence); invalidation logic in Plan 2
- [x] Waypoints + waypoint_save_states tables, amended segments/attempts schema → Task 5
- [x] No data migration → Task 5 drops stale tables
- [x] WaypointsMixin CRUD → Task 6
- [x] Save states attach to waypoints → Task 7
- [x] Segment queries use waypoint joins → Task 8
- [x] Lua set_conditions command → Task 9
- [x] Lua reads conditions at every transition → Task 9
- [x] ReferenceCapture creates waypoints + assigns is_primary → Task 10
- [x] ConditionRegistry loaded at startup + pushed to Lua → Task 11
- [x] Observed conditions persisted on attempts → Task 12
- [x] /api/segments exposes waypoints + conditions → Task 13
- [x] Practice loop restricted to is_primary → Task 14
