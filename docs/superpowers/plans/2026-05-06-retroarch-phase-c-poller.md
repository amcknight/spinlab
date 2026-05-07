# RetroArch Migration — Phase C: Memory Polling + Transition Detection

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `lua/spinlab.lua`'s memory polling and transition detection to Python, driving RetroArch via the existing `NCIClient`. Replaces the Lua frame-callback model with a Python asyncio polling loop that emits the same transition events SpinLab's session manager already consumes.

**Architecture:** A single source-of-truth address constants module. Pure transition-detection predicates that take memory snapshots and previous state, returning event objects. A polling loop module that owns timing, the `state_just_loaded` re-sync invariant, and the cold-fill state machine. All logic is unit-testable against synthetic memory snapshots — no live RetroArch needed for tests.

**Tech Stack:** Python 3.11+, stdlib `asyncio`, `dataclasses`, pytest. Builds on Phase B's `NCIClient`.

**Phase A audit reference:** [`docs/retroarch-migration/lua-audit.md`](../../retroarch-migration/lua-audit.md). Spec: [`docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`](../specs/2026-05-06-retroarch-migration-design.md). The Lua to port: `lua/spinlab.lua` (entry behaviour: `read_mem`, `detect_transitions`, `detect_finish`, `check_checkpoint_hit`, `is_death_frame`, `is_exit_frame`, `goal_type`, `handle_cold_fill`).

**What this phase does NOT do:** Save state I/O (Phase D). Replay (Phase E). Practice/speed-run state machines (those live in `python/spinlab/practice.py` and `speed_run.py` already and just need their event sources rewired). HUD overlay (deleted). The Lua TCP server (replaced by direct Python orchestration in Phase F).

---

## File Structure

| Path | Purpose |
|------|---------|
| `python/spinlab/retroarch/addresses.py` | Single source of truth for SMW WRAM addresses. Replaces `lua/addresses.lua` + `tests/integration/addresses.py` + `lua/poke_engine.lua` ADDR_MAP. |
| `python/spinlab/retroarch/snapshot.py` | `MemorySnapshot` dataclass. The "all the values we read each frame" container. Built from a single `read_ram` call so memory is consistent across fields. |
| `python/spinlab/retroarch/events.py` | Event dataclasses emitted by detection: `LevelEntrance`, `Death`, `LevelExit`, `Checkpoint`, `Spawn`. Mirrors the JSON shapes in `lua/spinlab.lua` `send_event` calls. |
| `python/spinlab/retroarch/predicates.py` | Pure functions: `is_death_frame(prev, curr)`, `is_exit_frame(prev, curr)`, `check_checkpoint_hit(prev, curr, transition_state)`, `detect_finish(prev, curr)`, `goal_type(curr)`. Stateless apart from the `prev` arg. |
| `python/spinlab/retroarch/transition_state.py` | `TransitionState` dataclass + `reset()`. Per-segment mutable state: `died_flag`, `cp_ordinal`, `first_cp_entrance`, `last_event_key`. |
| `python/spinlab/retroarch/conditions.py` | `ConditionRegistry` — replaces Lua's `condition_defs` + `read_conditions()`. Holds the dynamic `(name, address, size)` list set via Python (replacing the TCP `set_conditions` command). |
| `python/spinlab/retroarch/poller.py` | The polling loop. Owns: cadence, `state_just_loaded` re-sync, cold-fill sub-state, event emission to a callback or asyncio.Queue. Glue between `NCIClient` and the predicates. |
| `python/spinlab/retroarch/cold_fill.py` | Cold-fill state machine (`waiting_death` → `waiting_spawn`). Separated from the main poller because it's a distinct mode of operation. |
| `tests/unit/retroarch/test_addresses.py` | Address-constant tests: every constant matches `lua/addresses.lua` exactly. |
| `tests/unit/retroarch/test_snapshot.py` | Snapshot read tests against fake NCI server. |
| `tests/unit/retroarch/test_predicates.py` | Predicates tested with synthetic prev/curr snapshots — no NCI needed. |
| `tests/unit/retroarch/test_transition_state.py` | Reset behaviour, edge counters. |
| `tests/unit/retroarch/test_conditions.py` | Condition registry: register, replace, read. |
| `tests/unit/retroarch/test_poller.py` | Polling loop with a fake NCIClient: drives event sequences from scripted snapshots. |
| `tests/unit/retroarch/test_cold_fill.py` | Cold-fill state machine. |

---

## Task 1: Address constants

Port `lua/addresses.lua` to `python/spinlab/retroarch/addresses.py` as the single source of truth. Pin every constant to a unit test that documents its value (so future renames stay consistent).

**Files:**
- Create: `python/spinlab/retroarch/addresses.py`
- Create: `tests/unit/retroarch/test_addresses.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/retroarch/test_addresses.py
"""Pin SMW address constants — these are kaizosplits-derived and must not drift."""
from spinlab.retroarch import addresses as a


def test_smw_address_constants():
    # Memory map (must match lua/addresses.lua).
    assert a.ADDR_GAME_MODE == 0x0100
    assert a.ADDR_LEVEL_NUM == 0x13BF
    assert a.ADDR_ROOM_NUM == 0x010B
    assert a.ADDR_LEVEL_START == 0x1935
    assert a.ADDR_PLAYER_ANIM == 0x0071
    assert a.ADDR_EXIT_MODE == 0x0DD5
    assert a.ADDR_IO == 0x1DFB
    assert a.ADDR_FANFARE == 0x0906
    assert a.ADDR_BOSS_DEFEAT == 0x13C6
    assert a.ADDR_MIDWAY == 0x13CE
    assert a.ADDR_CP_ENTRANCE == 0x1B403


def test_smw_io_port_values():
    assert a.IO_ORB == 3
    assert a.IO_GOAL == 4
    assert a.IO_KEY == 7
    assert a.IO_FADEOUT == 8
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_addresses.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'spinlab.retroarch.addresses'`.

- [ ] **Step 3: Implement the module**

```python
# python/spinlab/retroarch/addresses.py
"""SMW WRAM address constants — single source of truth.

Ported from `lua/addresses.lua`, which was ported from kaizosplits/Memory.cs.
Addresses are WRAM-flat offsets (suitable for NCI's `READ_CORE_RAM <addr>`).
For SMW these are equivalent to SNES bus addresses minus 0x7E0000 for the
$7E:0000-$7E:1FFF range; the one exception is ADDR_CP_ENTRANCE which is at
0x1B403 (within the $7F bank in WRAM-flat). Verify against live game during
poller integration; if Mesen's snesMemory mode reads this differently, we
may need a different read mechanism for that one address.
"""

# Game state.
ADDR_GAME_MODE = 0x0100  # 0x0E = standard in-level; other values vary.
ADDR_LEVEL_NUM = 0x13BF  # current level number
ADDR_ROOM_NUM = 0x010B  # current room/sublevel
ADDR_LEVEL_START = 0x1935  # 0->1 when player appears in level (entrance edge)
ADDR_PLAYER_ANIM = 0x0071  # player animation; 9 = death

# Exit / progression.
ADDR_EXIT_MODE = 0x0DD5  # 0 = not exiting; non-zero = exiting level
ADDR_IO = 0x1DFB  # SPC I/O port: see IO_* values below
ADDR_FANFARE = 0x0906  # steps to 1 when goal reached
ADDR_BOSS_DEFEAT = 0x13C6  # 0 = alive; non-zero = defeated
ADDR_MIDWAY = 0x13CE  # midway checkpoint tape: 0->1 on touch
ADDR_CP_ENTRANCE = 0x1B403  # ASM-style checkpoint entrance (kaizo hack patches)

# SPC I/O port values (read from ADDR_IO).
IO_ORB = 3
IO_GOAL = 4
IO_KEY = 7
IO_FADEOUT = 8
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/unit/retroarch/test_addresses.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/addresses.py tests/unit/retroarch/test_addresses.py
git commit -m "feat(retroarch): SMW address constants (single source of truth)"
```

---

## Task 2: MemorySnapshot dataclass + reader

A frozen dataclass capturing every value `lua/spinlab.lua`'s `read_mem()` returns, plus a single function that builds it from one batched NCI call.

**Files:**
- Create: `python/spinlab/retroarch/snapshot.py`
- Create: `tests/unit/retroarch/test_snapshot.py`

The Lua reads 11 fields each frame via individual `emu.read` calls. We could batch them via a single `READ_CORE_RAM` over a contiguous range, but the addresses span 0x0071 to 0x1B403 (way too wide to read in one block). Instead: 11 separate `read_ram` calls. The NCI client now holds a persistent socket so this is cheap.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_snapshot.py
"""Tests for MemorySnapshot — the per-frame view of SMW state."""
import pytest

from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot, read_snapshot


def test_snapshot_dataclass_shape():
    snap = MemorySnapshot(
        game_mode=0x0E, level_num=0x05, room_num=0, level_start=1, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    assert snap.game_mode == 0x0E
    assert snap.level_num == 0x05


def test_read_snapshot_against_fake_server(fake_nci_server):
    """All 11 addresses are read; values returned in the dataclass."""
    # Map each address to the byte we want it to return.
    expected = {
        0x0100: 0x0E, 0x13BF: 0x05, 0x010B: 0x00, 0x1935: 0x01,
        0x0071: 0x00, 0x0DD5: 0x00, 0x1DFB: 0x00, 0x0906: 0x00,
        0x13C6: 0x00, 0x13CE: 0x00, 0x1B403: 0x00,
    }
    for addr, val in expected.items():
        fake_nci_server.handle(
            f"READ_CORE_RAM {addr:x} 1",
            f"READ_CORE_RAM {addr:x} {val:02x}\n",
        )

    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    snap = read_snapshot(client)

    assert snap.game_mode == 0x0E
    assert snap.level_num == 0x05
    assert snap.level_start == 0x01
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_snapshot.py -v
```

Expected: FAIL with import error.

- [ ] **Step 3: Implement the module**

```python
# python/spinlab/retroarch/snapshot.py
"""MemorySnapshot — per-frame view of SMW state, built from NCI reads."""
from __future__ import annotations

from dataclasses import dataclass

from spinlab.retroarch import addresses as a
from spinlab.retroarch.nci import NCIClient


@dataclass(frozen=True)
class MemorySnapshot:
    """Frozen snapshot of every SMW byte that transition detection consults.

    All fields are single bytes; the names match `lua/spinlab.lua`'s `read_mem`
    keys verbatim so the port is one-to-one.
    """

    game_mode: int
    level_num: int
    room_num: int
    level_start: int
    player_anim: int
    exit_mode: int
    io_port: int
    fanfare: int
    boss_defeat: int
    midway: int
    cp_entrance: int


def read_snapshot(client: NCIClient) -> MemorySnapshot:
    """Read all 11 SMW state bytes via NCI and return a snapshot.

    Issues 11 separate READ_CORE_RAM calls. With the NCIClient's persistent
    socket this is ~11 * mean_rtt ≈ 110ms in the worst case from spike numbers,
    but typical p50 is ~10ms each so ~110ms is the worst case; in practice well
    under one frame's budget on a healthy localhost. If 60Hz polling hits
    measurable latency in production, batch into a contiguous range of low
    addresses (most fields cluster in $0000-$13FF) and read those in one call.
    """
    return MemorySnapshot(
        game_mode=client.read_ram(a.ADDR_GAME_MODE, 1)[0],
        level_num=client.read_ram(a.ADDR_LEVEL_NUM, 1)[0],
        room_num=client.read_ram(a.ADDR_ROOM_NUM, 1)[0],
        level_start=client.read_ram(a.ADDR_LEVEL_START, 1)[0],
        player_anim=client.read_ram(a.ADDR_PLAYER_ANIM, 1)[0],
        exit_mode=client.read_ram(a.ADDR_EXIT_MODE, 1)[0],
        io_port=client.read_ram(a.ADDR_IO, 1)[0],
        fanfare=client.read_ram(a.ADDR_FANFARE, 1)[0],
        boss_defeat=client.read_ram(a.ADDR_BOSS_DEFEAT, 1)[0],
        midway=client.read_ram(a.ADDR_MIDWAY, 1)[0],
        cp_entrance=client.read_ram(a.ADDR_CP_ENTRANCE, 1)[0],
    )
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_snapshot.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/snapshot.py tests/unit/retroarch/test_snapshot.py
git commit -m "feat(retroarch): MemorySnapshot dataclass + read_snapshot"
```

---

## Task 3: TransitionState

Port the `transition_state` table and `reset_transition_state()` from `lua/spinlab.lua`.

**Files:**
- Create: `python/spinlab/retroarch/transition_state.py`
- Create: `tests/unit/retroarch/test_transition_state.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_transition_state.py
from spinlab.retroarch.transition_state import TransitionState


def test_initial_state():
    s = TransitionState()
    assert s.died_flag is False
    assert s.cp_ordinal == 0
    assert s.first_cp_entrance == 0
    assert s.last_event_key is None


def test_reset_clears_all_fields():
    s = TransitionState()
    s.died_flag = True
    s.cp_ordinal = 3
    s.first_cp_entrance = 0x42
    s.last_event_key = "some_key"

    s.reset()

    assert s.died_flag is False
    assert s.cp_ordinal == 0
    assert s.first_cp_entrance == 0
    assert s.last_event_key is None
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/unit/retroarch/test_transition_state.py -v
```

Expected: FAIL with import error.

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/transition_state.py
"""TransitionState — per-segment mutable state for detection.

Cleared at the start of a new segment / mode change. Mirrors the
`transition_state` table in lua/spinlab.lua.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransitionState:
    died_flag: bool = False
    cp_ordinal: int = 0
    first_cp_entrance: int = 0
    last_event_key: str | None = None

    def reset(self) -> None:
        self.died_flag = False
        self.cp_ordinal = 0
        self.first_cp_entrance = 0
        self.last_event_key = None
```

- [ ] **Step 4: Run test**

```
python -m pytest tests/unit/retroarch/test_transition_state.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/transition_state.py tests/unit/retroarch/test_transition_state.py
git commit -m "feat(retroarch): TransitionState dataclass with reset()"
```

---

## Task 4: Pure detection predicates

Port `is_death_frame`, `is_exit_frame`, `check_checkpoint_hit`, `detect_finish`, `goal_type` from `lua/spinlab.lua`. All pure functions over `(prev, curr, transition_state?)`.

**Files:**
- Create: `python/spinlab/retroarch/predicates.py`
- Create: `tests/unit/retroarch/test_predicates.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_predicates.py
"""Pure predicate tests using synthetic snapshots."""
from spinlab.retroarch import addresses as a
from spinlab.retroarch.predicates import (
    check_checkpoint_hit,
    detect_finish,
    goal_type,
    is_death_frame,
    is_exit_frame,
)
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState


def _snap(**overrides) -> MemorySnapshot:
    """Build a snapshot with all-zero defaults plus per-test overrides."""
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(overrides)
    return MemorySnapshot(**base)


def test_death_frame_fires_on_anim_zero_to_nine():
    prev = _snap(player_anim=0)
    curr = _snap(player_anim=9)
    assert is_death_frame(prev, curr) is True


def test_death_frame_does_not_fire_when_already_dying():
    prev = _snap(player_anim=9)
    curr = _snap(player_anim=9)
    assert is_death_frame(prev, curr) is False


def test_exit_frame_fires_on_exit_mode_edge():
    prev = _snap(exit_mode=0)
    curr = _snap(exit_mode=1)
    assert is_exit_frame(prev, curr) is True


def test_exit_frame_does_not_fire_when_exit_mode_unchanged():
    prev = _snap(exit_mode=1)
    curr = _snap(exit_mode=1)
    assert is_exit_frame(prev, curr) is False


def test_goal_type_key():
    assert goal_type(_snap(io_port=a.IO_KEY)) == "key"


def test_goal_type_orb():
    assert goal_type(_snap(io_port=a.IO_ORB)) == "orb"


def test_goal_type_boss():
    assert goal_type(_snap(boss_defeat=1, fanfare=1)) == "boss"


def test_goal_type_normal():
    assert goal_type(_snap(fanfare=1)) == "normal"


def test_goal_type_abort():
    """Default — no fanfare, no goal flag."""
    assert goal_type(_snap()) == "abort"


def test_check_checkpoint_hit_midway():
    """Midway tape: midway 0 -> 1, no goal/orb/key/fadeout."""
    state = TransitionState(first_cp_entrance=0)
    prev = _snap(midway=0)
    curr = _snap(midway=1)
    assert check_checkpoint_hit(prev, curr, state) == "midway"


def test_check_checkpoint_hit_cp_entrance():
    """ASM-style cp_entrance change while in level, distinct from first."""
    state = TransitionState(first_cp_entrance=0x10)  # known starting room
    prev = _snap(level_num=1, cp_entrance=0x10)
    curr = _snap(level_num=1, cp_entrance=0x20)
    assert check_checkpoint_hit(prev, curr, state) == "cp_entrance"


def test_check_checkpoint_hit_suppressed_during_goal():
    """midway hit is ignored if the goal also fired this frame."""
    state = TransitionState(first_cp_entrance=0)
    prev = _snap(midway=0)
    curr = _snap(midway=1, fanfare=1)  # fanfare 1 = goal
    assert check_checkpoint_hit(prev, curr, state) is None


def test_detect_finish_normal_goal():
    prev = _snap(fanfare=0)
    curr = _snap(fanfare=1)
    assert detect_finish(prev, curr) == "normal"


def test_detect_finish_boss():
    prev = _snap(fanfare=0, boss_defeat=0)
    curr = _snap(fanfare=1, boss_defeat=1)
    assert detect_finish(prev, curr) == "boss"


def test_detect_finish_orb():
    prev = _snap(io_port=0)
    curr = _snap(io_port=a.IO_ORB)
    assert detect_finish(prev, curr) == "orb"


def test_detect_finish_key():
    prev = _snap(io_port=0)
    curr = _snap(io_port=a.IO_KEY)
    assert detect_finish(prev, curr) == "key"


def test_detect_finish_none_when_static():
    """No transitions → no finish event."""
    snap = _snap(fanfare=1)
    assert detect_finish(snap, snap) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/unit/retroarch/test_predicates.py -v
```

Expected: all FAIL with import error.

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/predicates.py
"""Pure detection predicates — port of lua/spinlab.lua transition functions.

Every function here takes a previous snapshot, a current snapshot, and
optionally a TransitionState. None mutate any state. Their return values are
the source of truth for what the polling loop turns into events.
"""
from __future__ import annotations

from spinlab.retroarch import addresses as a
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState

PLAYER_ANIM_DEAD = 9


def is_death_frame(prev: MemorySnapshot, curr: MemorySnapshot) -> bool:
    """Player animation transitioned from non-9 to 9 (death animation start)."""
    return curr.player_anim == PLAYER_ANIM_DEAD and prev.player_anim != PLAYER_ANIM_DEAD


def is_exit_frame(prev: MemorySnapshot, curr: MemorySnapshot) -> bool:
    """exit_mode 0 -> non-zero edge."""
    return curr.exit_mode != 0 and prev.exit_mode == 0


def goal_type(curr: MemorySnapshot) -> str:
    """Classify the current goal state of a level exit.

    Mirrors lua/spinlab.lua `goal_type`: precedence is key > orb > boss > normal,
    with anything else treated as 'abort' (e.g. start+select reset, death exit).
    """
    if curr.io_port == a.IO_KEY:
        return "key"
    if curr.io_port == a.IO_ORB:
        return "orb"
    if curr.boss_defeat != 0 and curr.fanfare == 1:
        return "boss"
    if curr.fanfare == 1 or curr.io_port == a.IO_GOAL:
        return "normal"
    return "abort"


def check_checkpoint_hit(
    prev: MemorySnapshot, curr: MemorySnapshot, state: TransitionState
) -> str | None:
    """Returns "midway" or "cp_entrance" if a checkpoint fired this frame, else None.

    Suppressed if any goal-type signal also fired this frame (orb/goal/key/fadeout)
    — those events take precedence and the checkpoint detection would be a
    spurious side effect.
    """
    got_orb = curr.io_port == a.IO_ORB
    got_goal = curr.fanfare == 1 or curr.io_port == a.IO_GOAL
    got_key = curr.io_port == a.IO_KEY
    got_fadeout = curr.io_port == a.IO_FADEOUT
    blocked = got_orb or got_goal or got_key or got_fadeout

    midway_hit = (prev.midway == 0 and curr.midway == 1) and not blocked
    cp_entrance_hit = (
        curr.level_num != 0
        and curr.cp_entrance != prev.cp_entrance
        and curr.cp_entrance != state.first_cp_entrance
        and not blocked
    )

    if midway_hit:
        return "midway"
    if cp_entrance_hit:
        return "cp_entrance"
    return None


def detect_finish(prev: MemorySnapshot, curr: MemorySnapshot) -> str | None:
    """Early finish detection (kaizosplits LevelFinish).

    Returns "normal" / "boss" / "orb" / "key" if one fired this frame, else None.
    Edge-triggered on the relevant transitions.
    """
    # Goal tape: fanfare 0 -> 1, boss alive, no orb.
    if curr.fanfare == 1 and prev.fanfare == 0 and curr.boss_defeat == 0 and curr.io_port != a.IO_ORB:
        return "normal"
    # Boss: fanfare 0 -> 1, boss defeated.
    if curr.fanfare == 1 and prev.fanfare == 0 and curr.boss_defeat != 0:
        return "boss"
    # Orb: io shifts to 3, boss alive.
    if curr.io_port == a.IO_ORB and prev.io_port != a.IO_ORB and curr.boss_defeat == 0:
        return "orb"
    # Key: io shifts to 7.
    if curr.io_port == a.IO_KEY and prev.io_port != a.IO_KEY:
        return "key"
    return None
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_predicates.py -v
```

Expected: all PASS (~14 tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/predicates.py tests/unit/retroarch/test_predicates.py
git commit -m "feat(retroarch): pure detection predicates ported from spinlab.lua"
```

---

## Task 5: ConditionRegistry

Port the dynamic conditions API from `lua/spinlab.lua` (`set_conditions` / `read_conditions`).

**Files:**
- Create: `python/spinlab/retroarch/conditions.py`
- Create: `tests/unit/retroarch/test_conditions.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_conditions.py
import pytest

from spinlab.retroarch.conditions import ConditionRegistry, ConditionSpec
from spinlab.retroarch.nci import NCIClient


def test_register_and_read(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 100 1", "READ_CORE_RAM 100 0e\n")
    fake_nci_server.handle("READ_CORE_RAM 200 2", "READ_CORE_RAM 200 fe ca\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    reg = ConditionRegistry()
    reg.set([
        ConditionSpec(name="game_mode", address=0x100, size=1),
        ConditionSpec(name="counter", address=0x200, size=2),
    ])

    values = reg.read_all(client)
    assert values == {"game_mode": 0x0E, "counter": 0xCAFE}


def test_replacing_set_overrides_previous(fake_nci_server):
    fake_nci_server.handle("READ_CORE_RAM 100 1", "READ_CORE_RAM 100 01\n")
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])

    reg = ConditionRegistry()
    reg.set([ConditionSpec(name="a", address=0x999, size=1)])
    reg.set([ConditionSpec(name="b", address=0x100, size=1)])
    assert reg.read_all(client) == {"b": 0x01}


def test_unsupported_size_raises():
    reg = ConditionRegistry()
    with pytest.raises(ValueError, match="size"):
        reg.set([ConditionSpec(name="bad", address=0x100, size=4)])


def test_empty_registry_returns_empty(fake_nci_server):
    client = NCIClient(host=fake_nci_server.address[0], port=fake_nci_server.address[1])
    reg = ConditionRegistry()
    assert reg.read_all(client) == {}
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_conditions.py -v
```

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/conditions.py
"""ConditionRegistry — dynamic per-event memory probes.

Replaces lua/spinlab.lua's `condition_defs` + `read_conditions()` + the TCP
`set_conditions` command. The registry holds a list of (name, address, size)
tuples; `read_all(client)` returns {name: int_value} via NCI reads.

Sizes 1 and 2 are supported (matching what kaizosplits uses). Larger sizes
are rejected because the value-construction logic is byte-by-byte and would
need a clearer endianness contract before extending.
"""
from __future__ import annotations

from dataclasses import dataclass

from spinlab.retroarch.nci import NCIClient

SUPPORTED_SIZES = (1, 2)


@dataclass(frozen=True)
class ConditionSpec:
    name: str
    address: int
    size: int  # bytes; must be in SUPPORTED_SIZES


class ConditionRegistry:
    """Holds the active set of condition probes. Replace via set()."""

    def __init__(self) -> None:
        self._specs: list[ConditionSpec] = []

    def set(self, specs: list[ConditionSpec]) -> None:
        for s in specs:
            if s.size not in SUPPORTED_SIZES:
                raise ValueError(
                    f"unsupported condition size {s.size} for {s.name!r}; "
                    f"only {SUPPORTED_SIZES} supported"
                )
        self._specs = list(specs)

    def read_all(self, client: NCIClient) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self._specs:
            data = client.read_ram(s.address, s.size)
            if s.size == 1:
                out[s.name] = data[0]
            else:  # size == 2, little-endian per emu.readWord convention
                out[s.name] = data[0] | (data[1] << 8)
        return out
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_conditions.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/conditions.py tests/unit/retroarch/test_conditions.py
git commit -m "feat(retroarch): ConditionRegistry for dynamic per-event probes"
```

---

## Task 6: Event dataclasses

Define the structured events that detection emits. Each mirrors a JSON shape from `lua/spinlab.lua`'s `send_event` calls. These flow into `session_manager`'s existing event pipeline (which currently consumes JSON dicts from the Lua TCP socket).

**Files:**
- Create: `python/spinlab/retroarch/events.py`
- Create: `tests/unit/retroarch/test_events.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_events.py
from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
    TransitionEvent,
)


def test_level_entrance_fields():
    e = LevelEntrance(
        level=5, room=0, frame=120, timestamp_ms=2000,
        state_path="states/foo.state", conditions={"game_mode": 14},
    )
    assert isinstance(e, TransitionEvent)
    assert e.level == 5


def test_death_minimal():
    e = Death(level_num=5, timestamp_ms=3000, conditions={})
    assert isinstance(e, TransitionEvent)


def test_level_exit_full():
    e = LevelExit(
        level=5, room=0, goal="normal", elapsed_ms=10500, frame=600,
        timestamp_ms=4000, conditions={},
    )
    assert e.goal == "normal"


def test_checkpoint_full():
    e = Checkpoint(
        level_num=5, cp_type="midway", cp_ordinal=1, timestamp_ms=5000,
        state_path="states/cp.state", conditions={},
    )
    assert e.cp_type == "midway"


def test_spawn_full():
    e = Spawn(
        level_num=5, is_cold_cp=True, cp_ordinal=1, timestamp_ms=6000,
        state_captured=True, state_path="states/cold.state", conditions={},
    )
    assert e.is_cold_cp is True
```

- [ ] **Step 2: Run tests, expect import failure**

```
python -m pytest tests/unit/retroarch/test_events.py -v
```

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/events.py
"""Transition events emitted by the polling loop.

Each event mirrors a JSON shape from lua/spinlab.lua's send_event calls.
session_manager and the dashboard already consume those JSON dicts; the
adapter that converts these dataclasses to the existing dict shape lives
in Phase F. For Phase C, dataclasses are the produced type.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TransitionEvent:
    """Marker base — every concrete event inherits this for typing."""

    timestamp_ms: int
    conditions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LevelEntrance(TransitionEvent):
    level: int = 0
    room: int = 0
    frame: int = 0
    state_path: str = ""


@dataclass(frozen=True)
class Death(TransitionEvent):
    level_num: int = 0


@dataclass(frozen=True)
class LevelExit(TransitionEvent):
    level: int = 0
    room: int = 0
    goal: str = ""
    elapsed_ms: int = 0
    frame: int = 0


@dataclass(frozen=True)
class Checkpoint(TransitionEvent):
    level_num: int = 0
    cp_type: str = ""
    cp_ordinal: int = 0
    state_path: str = ""


@dataclass(frozen=True)
class Spawn(TransitionEvent):
    level_num: int = 0
    is_cold_cp: bool = False
    cp_ordinal: int = 0
    state_captured: bool = False
    state_path: str = ""
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_events.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/events.py tests/unit/retroarch/test_events.py
git commit -m "feat(retroarch): TransitionEvent dataclasses (LevelEntrance, Death, etc.)"
```

---

## Task 7: TransitionDetector — stateful event emitter

Combines `MemorySnapshot`, `TransitionState`, and the predicates into a single class that the poller drives one frame at a time. Emits a list of events per frame. Pure logic — no IO. This is the porting heart of Phase C.

**Files:**
- Create: `python/spinlab/retroarch/detector.py`
- Create: `tests/unit/retroarch/test_detector.py`

The Lua `detect_transitions` function calls `detect_death`, `detect_checkpoint`, `detect_exit`, `detect_entrance` in order. The order matters — exit must come before entrance, and the `exit_this_frame` flag is read by entrance detection. Port that ordering carefully.

The `state_path` field in events depends on the game id and segment context. For Phase C, the detector doesn't compute state paths — it sets them to None / empty and the poller (which knows the game id) fills them in. Keeps the detector pure.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_detector.py
"""TransitionDetector tests — drive sequences of synthetic snapshots through it."""
from spinlab.retroarch import addresses as a
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
)
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**overrides) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(overrides)
    return MemorySnapshot(**base)


def test_initial_step_emits_no_events():
    d = TransitionDetector()
    events = d.step(_snap(), timestamp_ms=0)
    assert events == []


def test_level_entrance_on_level_start_edge():
    d = TransitionDetector()
    d.step(_snap(level_num=5), timestamp_ms=0)  # prev seeded
    events = d.step(_snap(level_num=5, level_start=1), timestamp_ms=16)

    assert len(events) == 1
    assert isinstance(events[0], LevelEntrance)
    assert events[0].level == 5


def test_death_emits_once_per_anim_transition():
    d = TransitionDetector()
    d.step(_snap(player_anim=0), timestamp_ms=0)
    e1 = d.step(_snap(player_anim=9), timestamp_ms=16)
    e2 = d.step(_snap(player_anim=9), timestamp_ms=32)

    assert any(isinstance(e, Death) for e in e1)
    assert not any(isinstance(e, Death) for e in e2), "death must not refire while still dying"


def test_exit_emits_on_exit_mode_edge():
    d = TransitionDetector()
    d.step(_snap(exit_mode=0, fanfare=1, level_num=5), timestamp_ms=0)
    events = d.step(_snap(exit_mode=1, fanfare=1, level_num=5), timestamp_ms=16)

    assert any(isinstance(e, LevelExit) and e.goal == "normal" for e in events)


def test_checkpoint_then_spawn_after_death():
    """Real sequence: hit midway -> die -> respawn."""
    d = TransitionDetector()
    # Frame 1: clean snapshot.
    d.step(_snap(level_num=5, midway=0, level_start=1), timestamp_ms=0)
    # Frame 2: midway tape.
    cp_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=16)
    assert any(isinstance(e, Checkpoint) and e.cp_type == "midway" for e in cp_events)
    # Frame 3: death.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=1), timestamp_ms=32)
    # Frame 4: still dying.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=0), timestamp_ms=48)
    # Frame 5: respawn — level_start 0 -> 1 with died_flag still set.
    spawn_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=64)
    assert any(isinstance(e, Spawn) and e.is_cold_cp for e in spawn_events)
```

- [ ] **Step 2: Run tests, expect import failure**

```
python -m pytest tests/unit/retroarch/test_detector.py -v
```

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/detector.py
"""TransitionDetector — stateful, pure-logic event emitter.

Drives one frame at a time via .step(snapshot, timestamp_ms). Maintains
prev-snapshot, transition state, cp_acquired, level_start_frame internally.
Returns a list of events emitted on this frame (often empty).

Caller (poller) is responsible for: fetching snapshots, supplying timestamps,
filling state_path on events that need them (since that depends on game_id
which the detector doesn't know about), and forwarding events downstream.
"""
from __future__ import annotations

from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.predicates import (
    check_checkpoint_hit,
    detect_finish,
    goal_type,
    is_death_frame,
    is_exit_frame,
)
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.transition_state import TransitionState

FPS = 60.0  # SMW NTSC; close enough for elapsed-ms math


class TransitionDetector:
    """Per-frame transition emitter. Stateful but pure (no IO)."""

    def __init__(self) -> None:
        self._prev: MemorySnapshot | None = None
        self._state = TransitionState()
        self._cp_acquired = False
        self._level_start_frame = 0
        self._frame_counter = 0
        self._exit_this_frame = False

    def reset(self) -> None:
        """Clear all state (for new segment / mode change / state-load)."""
        self._prev = None
        self._state.reset()
        self._cp_acquired = False
        self._level_start_frame = 0
        self._exit_this_frame = False

    def resync_after_state_load(self, snapshot: MemorySnapshot) -> None:
        """Replace prev wholesale after a save state load.

        Mirrors lua/spinlab.lua's `state_just_loaded` re-sync: avoid phantom
        edge transitions on the first frame after load by treating the loaded
        state as if it were the previous frame's reading too.
        """
        self._prev = snapshot

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> list[TransitionEvent]:
        self._frame_counter += 1
        events: list[TransitionEvent] = []
        prev = self._prev
        if prev is None:
            self._prev = curr
            return events

        # 1. Death.
        if is_death_frame(prev, curr) and not self._state.died_flag:
            events.append(Death(timestamp_ms=timestamp_ms, level_num=curr.level_num))
            self._state.died_flag = True

        # 2. Checkpoint.
        cp_type = check_checkpoint_hit(prev, curr, self._state)
        if cp_type is not None:
            self._state.cp_ordinal += 1
            self._cp_acquired = True
            self._state.first_cp_entrance = 0  # opens cp_entrance shifts after first hit
            events.append(
                Checkpoint(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    cp_type=cp_type,
                    cp_ordinal=self._state.cp_ordinal,
                )
            )

        # 3. Exit (must come before entrance — see lua/spinlab.lua comment).
        self._exit_this_frame = is_exit_frame(prev, curr)
        if self._exit_this_frame:
            elapsed = int((self._frame_counter - self._level_start_frame) / FPS * 1000)
            events.append(
                LevelExit(
                    timestamp_ms=timestamp_ms,
                    level=curr.level_num,
                    room=curr.room_num,
                    goal=goal_type(curr),
                    elapsed_ms=elapsed,
                    frame=self._frame_counter,
                )
            )

        # 4. Entrance: level_start 0->1 OR fast retry.
        edge_spawn = curr.level_start == 1 and prev.level_start == 0
        fast_retry = (
            self._state.died_flag
            and curr.level_start == 1
            and curr.player_anim != 9
            and prev.player_anim == 9
        )
        if (edge_spawn or fast_retry) and not self._exit_this_frame:
            if self._state.died_flag:
                # Respawn after death.
                was_cp = self._cp_acquired
                if was_cp:
                    self._cp_acquired = False
                events.append(
                    Spawn(
                        timestamp_ms=timestamp_ms,
                        level_num=curr.level_num,
                        is_cold_cp=was_cp,
                        cp_ordinal=self._state.cp_ordinal,
                        state_captured=was_cp,
                    )
                )
                self._state.died_flag = False
            else:
                # Fresh level entry.
                self._state.cp_ordinal = 0
                self._cp_acquired = False
                self._state.first_cp_entrance = curr.cp_entrance
                self._level_start_frame = self._frame_counter
                events.append(
                    LevelEntrance(
                        timestamp_ms=timestamp_ms,
                        level=curr.level_num,
                        room=curr.room_num,
                        frame=self._frame_counter,
                    )
                )

        self._prev = curr
        return events
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_detector.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/detector.py tests/unit/retroarch/test_detector.py
git commit -m "feat(retroarch): TransitionDetector — stateful event emitter"
```

---

## Task 8: Cold-fill state machine

Port `handle_cold_fill` from `lua/spinlab.lua`. Distinct mode: caller activates it with a segment id, it watches for death-then-spawn, emits a single Spawn event with `is_cold_cp=True` and `state_captured=True`, and deactivates.

**Files:**
- Create: `python/spinlab/retroarch/cold_fill.py`
- Create: `tests/unit/retroarch/test_cold_fill.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_cold_fill.py
from spinlab.retroarch.cold_fill import ColdFillTracker
from spinlab.retroarch.events import Spawn
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def test_inactive_emits_nothing():
    cf = ColdFillTracker()
    assert cf.step(_snap(player_anim=9), timestamp_ms=0) is None


def test_active_waits_for_death_then_spawn():
    cf = ColdFillTracker()
    cf.activate(segment_id="boss-1")

    # Pre-death: nothing.
    assert cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0) is None
    # Death detected: still nothing emitted yet.
    assert cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16) is None
    # Still dying.
    assert cf.step(_snap(player_anim=9, level_start=0), timestamp_ms=32) is None
    # Spawn: level_start 0 -> 1 -> emits Spawn, deactivates.
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=48)
    assert isinstance(e, Spawn)
    assert e.is_cold_cp is True
    assert e.state_captured is True
    assert cf.is_active() is False


def test_fast_retry_path():
    """level_start stays at 1; spawn detected via player_anim 9 -> not-9."""
    cf = ColdFillTracker()
    cf.activate(segment_id="x")

    cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0)
    cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16)
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=32)
    assert isinstance(e, Spawn)
```

- [ ] **Step 2: Run tests, expect import failure**

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/cold_fill.py
"""ColdFillTracker — captures cold spawns after a reference run.

Activated externally with a segment id. Observes death-then-spawn sequence,
emits a single Spawn event with is_cold_cp=True, deactivates. Mirrors
lua/spinlab.lua's handle_cold_fill.
"""
from __future__ import annotations

from spinlab.retroarch.events import Spawn
from spinlab.retroarch.snapshot import MemorySnapshot

PLAYER_ANIM_DEAD = 9


class ColdFillTracker:
    def __init__(self) -> None:
        self._active = False
        self._waiting_spawn = False  # False = waiting for death; True = waiting for spawn
        self._segment_id: str | None = None
        self._prev_anim = 0
        self._prev_level_start = 0

    def is_active(self) -> bool:
        return self._active

    def activate(self, segment_id: str) -> None:
        self._active = True
        self._waiting_spawn = False
        self._segment_id = segment_id
        self._prev_anim = 0
        self._prev_level_start = 0

    def step(self, curr: MemorySnapshot, timestamp_ms: int) -> Spawn | None:
        if not self._active:
            return None

        emitted: Spawn | None = None

        if not self._waiting_spawn:
            # Look for death.
            if curr.player_anim == PLAYER_ANIM_DEAD and self._prev_anim != PLAYER_ANIM_DEAD:
                self._waiting_spawn = True
        else:
            edge_spawn = curr.level_start == 1 and self._prev_level_start == 0
            fast_retry = (
                curr.level_start == 1
                and curr.player_anim != PLAYER_ANIM_DEAD
                and self._prev_anim == PLAYER_ANIM_DEAD
            )
            if edge_spawn or fast_retry:
                emitted = Spawn(
                    timestamp_ms=timestamp_ms,
                    level_num=curr.level_num,
                    is_cold_cp=True,
                    cp_ordinal=0,
                    state_captured=True,
                )
                self._active = False
                self._waiting_spawn = False
                self._segment_id = None

        self._prev_anim = curr.player_anim
        self._prev_level_start = curr.level_start
        return emitted
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_cold_fill.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/cold_fill.py tests/unit/retroarch/test_cold_fill.py
git commit -m "feat(retroarch): ColdFillTracker state machine"
```

---

## Task 9: Poller — the asyncio loop

Glue: ties NCIClient + read_snapshot + TransitionDetector + ColdFillTracker + ConditionRegistry into a single asyncio task that runs at ~60Hz, handles `state_just_loaded` re-sync, and emits events to a callback (the dashboard's session_manager will subscribe in Phase F).

**Files:**
- Create: `python/spinlab/retroarch/poller.py`
- Create: `tests/unit/retroarch/test_poller.py`

The poller is small but coordinative. The bulk of the logic is in TransitionDetector (already tested). Here we just verify cadence, callback delivery, and the re-sync hook.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_poller.py
import asyncio
from typing import Iterator

import pytest

from spinlab.retroarch.events import Death, TransitionEvent
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    """Minimal NCIClient stand-in for poller tests."""

    def __init__(self) -> None:
        self.read_calls = 0


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    """Wrap a snapshot iterator into a callable matching the deps signature."""

    def fn(_client) -> MemorySnapshot:
        return next(seq)

    return fn


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


@pytest.mark.asyncio
async def test_poller_emits_death_event():
    """Poller fed a death sequence emits a Death event to the callback."""
    snapshots = iter([
        _snap(player_anim=0),  # frame 1
        _snap(player_anim=9),  # frame 2 -> death
        _snap(player_anim=9),  # frame 3 -> still dying, no event
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001)  # fast for test
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    assert any(isinstance(e, Death) for e in received), f"got: {received}"


@pytest.mark.asyncio
async def test_poller_resync_clears_phantom_edges():
    """After mark_state_loaded(), the next two snapshots don't generate phantoms."""
    # Without re-sync: snapshot 1 says alive, snapshot 2 says dead -> Death event.
    # With mark_state_loaded() before frame 2, the loaded state replaces prev.
    snapshots = iter([
        _snap(player_anim=0),  # seed
        _snap(player_anim=9),  # would normally fire Death
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    # Tell the poller we just loaded a state where Mario is dead -> next read should not fire Death.
    poller.mark_state_loaded()
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    # No Death events because re-sync was called before the dead snapshot was processed.
    assert not any(isinstance(e, Death) for e in received), f"got: {received}"
```

- [ ] **Step 2: Run tests, expect import failure**

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/poller.py
"""Poller — async loop that drives transition detection at ~60Hz.

Architecture:
  - NCIClient (Phase B) — owns the UDP socket.
  - read_snapshot (Phase C task 2) — builds a MemorySnapshot.
  - TransitionDetector (task 7) — converts (prev, curr) -> events.
  - ColdFillTracker (task 8) — separate mode, optional.
  - on_event callback — receives every emitted TransitionEvent.

Caller responsibilities:
  - Build PollerDeps with the right client/snapshot fn/event callback.
  - await poller.run() in an asyncio task.
  - poller.stop() to clean shutdown.
  - poller.mark_state_loaded() before the next poll if a save state was just
    loaded — replaces prev with the post-load snapshot to suppress phantom
    edge events.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from spinlab.retroarch.cold_fill import ColdFillTracker
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.events import TransitionEvent
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.snapshot import MemorySnapshot, read_snapshot

DEFAULT_PERIOD_SEC = 1.0 / 60.0  # one frame at 60 Hz


@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[TransitionEvent], None]


class Poller:
    def __init__(self, deps: PollerDeps, period_sec: float = DEFAULT_PERIOD_SEC) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._state_just_loaded = False
        self._detector = TransitionDetector()
        self._cold_fill = ColdFillTracker()
        self._start_ms = time.perf_counter() * 1000

    def mark_state_loaded(self) -> None:
        """Tell the poller the next snapshot replaces prev (suppress phantom edges)."""
        self._state_just_loaded = True

    def stop(self) -> None:
        self._stopped = True

    def activate_cold_fill(self, segment_id: str) -> None:
        self._cold_fill.activate(segment_id)

    async def run(self) -> None:
        while not self._stopped:
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception:  # NCIError or others — log and continue, don't kill the loop
                await asyncio.sleep(self._period)
                continue

            ts = int(time.perf_counter() * 1000 - self._start_ms)

            if self._state_just_loaded:
                self._detector.resync_after_state_load(snap)
                self._state_just_loaded = False
                await asyncio.sleep(self._period)
                continue

            for event in self._detector.step(snap, timestamp_ms=ts):
                self._deps.on_event(event)

            cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            if cf_event is not None:
                self._deps.on_event(cf_event)

            await asyncio.sleep(self._period)
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_poller.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Run the full unit suite for sanity**

```
python -m pytest -m "not (emulator or slow or frontend)" -q | tail -3
```

Should show all-green with the new tests added (~30 new tests across Phase C tasks).

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/poller.py tests/unit/retroarch/test_poller.py
git commit -m "feat(retroarch): Poller — async 60Hz transition-detection loop"
```

---

## Phase C exit criteria

- All transition detection logic from `lua/spinlab.lua` is in `python/spinlab/retroarch/` with 1:1 unit test coverage against synthetic snapshots.
- Cold-fill state machine ported.
- Polling loop runs at 60Hz, handles state-just-loaded re-sync, emits events via a callback.
- `address_map` consolidation is in place — only one Python source for SMW addresses.
- No live RetroArch dependency for any unit test in this phase.
- Full fast suite green.

## What's deliberately not in Phase C

- **Wiring into `session_manager`** (Phase F-live). Existing session_manager consumes JSON dicts from a TCP socket; we'll add an adapter to convert TransitionEvent → dict and feed events directly. That's a Phase F job.
- **State path computation** for events (depends on `game_id` + segment context). Phase D's state_io module owns this; the poller leaves event.state_path empty for now.
- **Speed-run / practice state machines** — they live in `python/spinlab/practice.py` / `speed_run.py` and just need their event source rewired in Phase F.
- **Integration tests against live RA.** Worthwhile but expensive (need RA + ROM + a poke harness in Python). Defer to Phase G.

## Phase C plan self-review

- File structure: 8 implementation files + 8 test files. Each implementation file has one clear responsibility.
- Coverage: every Lua function from the audit's "Phase C drivers" row has a port. Cold-fill state machine has its own task.
- No placeholders.
- Type consistency: `MemorySnapshot` used everywhere; `TransitionEvent` is the common output type; `step(snapshot, timestamp_ms)` is the consistent per-frame entry signature.
- Address constant `ADDR_CP_ENTRANCE = 0x1B403` carried over with the open question from Phase A's audit — will need verification against live game during Phase F integration.

## Next phase after C

Phase D — Savestate I/O. Builds on NCIClient (save_state / load_state_slot, working as of Phase B's end-of-day validation) plus filesystem juggling to manage SpinLab's segment-keyed states without disturbing Andrew's manual sequential auto-index saves.
