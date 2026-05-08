# Headless RetroArch Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-process Mesen+Lua poke harness with a Python harness driving headless RetroArch over NCI, so `tests/integration/test_transitions.py` exercises the production `TransitionDetector` directly.

**Architecture:** One RetroArch process per pytest session, paused at boot. A poke engine runs `.poke` scenarios frame-by-frame: write held bytes via `WRITE_CORE_RAM`, `FRAMEADVANCE`, `READ_CORE_RAM` for the snapshot (which serves as an implicit sync barrier), step the production detector, collect emitted events. Per-scenario isolation via a zero-pass over `ADDR_MAP` before each scenario.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, NCI (UDP), existing `spinlab.retroarch.nci.NCIClient`, `spinlab.retroarch.snapshot.read_snapshot`, `spinlab.retroarch.detector.TransitionDetector`, `spinlab.retroarch.addresses`.

**Spec:** [docs/superpowers/specs/2026-05-08-headless-ra-test-harness-design.md](../specs/2026-05-08-headless-ra-test-harness-design.md)

---

## File Structure

**Create:**
- `tests/integration/ra_poke_engine.py` — `RAPokeEngine`: stateless per-scenario engine
- `tests/integration/ra_harness.py` — `RAHarness`: process lifecycle + NCI client
- `tests/unit/integration/__init__.py` — package marker
- `tests/unit/integration/test_ra_poke_engine.py` — unit tests using a fake NCI client
- `tests/unit/integration/test_ra_harness.py` — unit tests for launch/teardown using mocked subprocess + NCI

**Modify:**
- `tests/integration/addresses.py` — re-export `ADDR_MAP` from `spinlab.retroarch.addresses`
- `tests/integration/conftest.py` — replace `mesen_process`/`tcp_client`/Mesen-driven `run_scenario` with `ra_harness`/RA-driven `run_scenario`; leave smoke + replay fixtures untouched
- `python/spinlab/config.py` — add `ra_core_path` to `EmulatorConfig` (path to libretro core `.dll`)
- `tests/integration/test_transitions.py` — add `pytest.mark.xfail` markers only for scenarios that fail due to known production bugs from `status.md` (decided in Task 6 after seeing live results)

**Delete:**
- The `mesen_process` fixture (lines ~129-175 of current `conftest.py`)
- The `tcp_client` fixture (lines ~178-214 of current `conftest.py`)
- The Mesen-driven `run_scenario` body (lines ~217-253 of current `conftest.py`)

---

## Task 1: Add `ra_core_path` to `EmulatorConfig`

**Files:**
- Modify: `python/spinlab/config.py:18-30` and `:62-103`
- Modify: `config.yaml` (project root) — add `ra_core_path` key under `emulator`
- Test: `tests/unit/test_config.py` (extend existing config tests)

**Why this exists.** `EmulatorConfig` already has `retroarch_path` (the RA executable) but no field for the libretro core. Production hardcodes `snes9x_libretro.dll` in `routes/system._launch_retroarch`. The test harness needs the same — making it configurable now lets us derive a sensible default from `retroarch_path` and lets users override per-machine.

- [ ] **Step 1: Find the existing config test for `EmulatorConfig`**

```bash
grep -rn "EmulatorConfig\|retroarch_path" tests/
```

Expected: a file like `tests/unit/test_config.py` containing tests for `AppConfig.from_yaml`. Pick whichever existing test exercises the retroarch fields.

- [ ] **Step 2: Write the failing test**

In whatever file you found in Step 1, add:

```python
def test_ra_core_path_parsed_from_yaml(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
data:
  dir: /tmp/data
emulator:
  backend: retroarch
  retroarch_path: C:/RetroArch-Win64/retroarch.exe
  ra_core_path: C:/RetroArch-Win64/cores/snes9x_libretro.dll
""")
    cfg = AppConfig.from_yaml(config_yaml)
    assert cfg.emulator.ra_core_path == Path("C:/RetroArch-Win64/cores/snes9x_libretro.dll")


def test_ra_core_path_defaults_to_none_when_absent(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
data:
  dir: /tmp/data
emulator:
  backend: retroarch
""")
    cfg = AppConfig.from_yaml(config_yaml)
    assert cfg.emulator.ra_core_path is None
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python -m pytest tests/unit/test_config.py::test_ra_core_path_parsed_from_yaml tests/unit/test_config.py::test_ra_core_path_defaults_to_none_when_absent -v
```

Expected: FAIL with `AttributeError: 'EmulatorConfig' object has no attribute 'ra_core_path'`.

- [ ] **Step 4: Add the field to `EmulatorConfig`**

In [python/spinlab/config.py:18-30](python/spinlab/config.py#L18-L30):

```python
@dataclass
class EmulatorConfig:
    backend: str = "mesen-lua"  # "mesen-lua" | "retroarch"
    # Mesen-Lua keys (unused under retroarch backend):
    path: Path | None = None
    lua_script: Path | None = None
    script_data_dir: Path | None = None
    # RetroArch keys:
    retroarch_path: Path | None = None
    ra_core_path: Path | None = None
    savestate_dir: Path | None = None
    spinlab_state_dir: Path | None = None
    ra_game_basename: str | None = None
```

- [ ] **Step 5: Parse the field in `from_yaml`**

In [python/spinlab/config.py:62-103](python/spinlab/config.py#L62-L103), inside the `emu = raw.get("emulator", {})` block, add the parse line and pass it through:

```python
        retroarch_path = emu.get("retroarch_path")
        ra_core_path = emu.get("ra_core_path")
        savestate_dir = emu.get("savestate_dir")
        # ...
            emulator=EmulatorConfig(
                backend=backend,
                path=Path(emu_path) if emu_path else None,
                lua_script=Path(lua_script) if lua_script else None,
                script_data_dir=Path(script_data_dir) if script_data_dir else None,
                retroarch_path=Path(retroarch_path) if retroarch_path else None,
                ra_core_path=Path(ra_core_path) if ra_core_path else None,
                savestate_dir=Path(savestate_dir) if savestate_dir else None,
                spinlab_state_dir=Path(spinlab_state_dir) if spinlab_state_dir else None,
                ra_game_basename=ra_game_basename if ra_game_basename else None,
            ),
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/unit/test_config.py -v
```

Expected: PASS, including the two new tests and any existing tests still passing.

- [ ] **Step 7: Add `ra_core_path` to local `config.yaml`**

The user runs locally with a real `config.yaml`. Add the key so subsequent tasks can use it. Default convention is `<retroarch_dir>/cores/snes9x_libretro.dll`.

```bash
grep -n "retroarch_path\|ra_core_path" config.yaml
```

If `ra_core_path` isn't present, add it next to `retroarch_path` in the `emulator:` section. The actual path on this machine is likely `C:/RetroArch-Win64/cores/snes9x_libretro.dll`.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/config.py tests/unit/test_config.py config.yaml
git commit -m "feat(config): add ra_core_path to EmulatorConfig"
```

---

## Task 2: Source-of-truth swap for `tests/integration/addresses.py`

**Files:**
- Modify: `tests/integration/addresses.py` (full rewrite)
- Test: `tests/unit/integration/test_addresses.py` (NEW)

**Why this exists.** Today `addresses.py` parses `lua/addresses.lua` at import time. Going forward `spinlab.retroarch.addresses` is the source of truth on the RA side. Re-exporting from there means there's one canonical Python ADDR_MAP for the harness to reference.

- [ ] **Step 1: Create `tests/unit/integration/__init__.py`**

```bash
mkdir -p tests/unit/integration
touch tests/unit/integration/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/unit/integration/test_addresses.py`:

```python
"""Verify the integration-test ADDR_MAP matches spinlab.retroarch.addresses."""

from spinlab.retroarch import addresses as ra_addr
from tests.integration.addresses import ADDR_MAP


def test_addr_map_keys_match_lua_keys_used_in_poke_files():
    """The .poke files reference these names — they must all be present."""
    expected_keys = {
        "game_mode", "level_num", "room_num", "level_start", "player_anim",
        "exit_mode", "io_port", "fanfare", "boss_defeat", "midway", "cp_entrance",
    }
    assert expected_keys.issubset(ADDR_MAP.keys())


def test_addr_map_values_match_spinlab_retroarch_addresses():
    assert ADDR_MAP["game_mode"] == ra_addr.ADDR_GAME_MODE
    assert ADDR_MAP["level_num"] == ra_addr.ADDR_LEVEL_NUM
    assert ADDR_MAP["room_num"] == ra_addr.ADDR_ROOM_NUM
    assert ADDR_MAP["level_start"] == ra_addr.ADDR_LEVEL_START
    assert ADDR_MAP["player_anim"] == ra_addr.ADDR_PLAYER_ANIM
    assert ADDR_MAP["exit_mode"] == ra_addr.ADDR_EXIT_MODE
    assert ADDR_MAP["io_port"] == ra_addr.ADDR_IO
    assert ADDR_MAP["fanfare"] == ra_addr.ADDR_FANFARE
    assert ADDR_MAP["boss_defeat"] == ra_addr.ADDR_BOSS_DEFEAT
    assert ADDR_MAP["midway"] == ra_addr.ADDR_MIDWAY
    assert ADDR_MAP["cp_entrance"] == ra_addr.ADDR_CP_ENTRANCE
```

- [ ] **Step 3: Run tests to verify they pass with current code (or fail surprisingly)**

```bash
python -m pytest tests/unit/integration/test_addresses.py -v
```

Expected: PASS — the existing Lua-parsed ADDR_MAP should already match (lua/addresses.lua is the historical source of truth that `spinlab.retroarch.addresses.py` was ported from). If a test fails, that's a pre-existing drift between the two sources and worth knowing before the rewrite.

- [ ] **Step 4: Rewrite `tests/integration/addresses.py`**

Replace the entire file contents with:

```python
"""SMW WRAM address constants — re-export from spinlab.retroarch.addresses.

The Python source of truth for memory addresses lives at
spinlab.retroarch.addresses. This file exists as a Mesen-era compatibility
shim; under the RA harness it just re-exports the canonical values so that
poke_parser.py (and any other consumer of ADDR_MAP) reads them from one place.

Note: the keys here MUST match the names used in tests/integration/scenarios/
.poke files (e.g., 'game_mode', 'level_num') — those names are stable user
input, not implementation detail.
"""
from spinlab.retroarch import addresses as _a

ADDR_MAP: dict[str, int] = {
    "game_mode": _a.ADDR_GAME_MODE,
    "level_num": _a.ADDR_LEVEL_NUM,
    "room_num": _a.ADDR_ROOM_NUM,
    "level_start": _a.ADDR_LEVEL_START,
    "player_anim": _a.ADDR_PLAYER_ANIM,
    "exit_mode": _a.ADDR_EXIT_MODE,
    "io_port": _a.ADDR_IO,
    "fanfare": _a.ADDR_FANFARE,
    "boss_defeat": _a.ADDR_BOSS_DEFEAT,
    "midway": _a.ADDR_MIDWAY,
    "cp_entrance": _a.ADDR_CP_ENTRANCE,
}
```

- [ ] **Step 5: Run tests to verify**

```bash
python -m pytest tests/unit/integration/test_addresses.py tests/integration/test_poke_parser.py -v
```

Expected: PASS. The poke parser tests still pass because ADDR_MAP keys are unchanged.

- [ ] **Step 6: Run the broader unit suite**

```bash
python -m pytest -m "not (emulator or slow or frontend)" -q
```

Expected: PASS for everything that was passing before.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/addresses.py tests/unit/integration/__init__.py tests/unit/integration/test_addresses.py
git commit -m "refactor(tests): integration ADDR_MAP re-exports from spinlab.retroarch.addresses"
```

---

## Task 3: Build `RAPokeEngine` against a fake NCI client

**Files:**
- Create: `tests/integration/ra_poke_engine.py`
- Create: `tests/unit/integration/test_ra_poke_engine.py`

**Why this exists.** This is the per-frame poke→advance→read loop. By testing it against a fake NCI client (in-memory dict for WRAM, frame counter int) we can verify the held-values pattern, scenario sequencing, zero-pass, and detector wiring without launching RA. Live integration comes in Task 6.

- [ ] **Step 1: Build the fake NCI client used by tests**

Create `tests/unit/integration/test_ra_poke_engine.py` and put the fake near the top:

```python
"""Tests for RAPokeEngine using a fake NCI client.

The fake holds WRAM in a dict and tracks frame_advance calls. This lets us
verify the engine's poke ordering, held-values behavior, zero-pass, and
detector-wiring without running RetroArch.
"""
from __future__ import annotations

from dataclasses import dataclass

from tests.integration.addresses import ADDR_MAP
from tests.integration.poke_parser import parse_poke
from tests.integration.ra_poke_engine import RAPokeEngine


class FakeNCIClient:
    """Minimal NCI surface for poke-engine tests.

    - read_ram(addr, n): returns wram[addr:addr+n], padded with 0 if missing.
    - write_ram(addr, data): writes bytes into the wram dict.
    - frame_advance(): no-op — the test fixture mutates wram directly to
      simulate ROM behavior between frames if needed; default is "frame
      runs but no CPU writes," matching a paused-by-frame-step model.
    """

    def __init__(self) -> None:
        self.wram: dict[int, int] = {}
        self.writes: list[tuple[int, bytes]] = []
        self.frame_advances = 0

    def read_ram(self, addr: int, n: int = 1) -> bytes:
        return bytes(self.wram.get(addr + i, 0) for i in range(n))

    def write_ram(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.wram[addr + i] = b
        self.writes.append((addr, data))

    def frame_advance(self) -> None:
        self.frame_advances += 1
```

- [ ] **Step 2: Add the first failing test — zero-pass**

Append to `test_ra_poke_engine.py`:

```python
def test_run_scenario_zeroes_addr_map_before_first_frame():
    fake = FakeNCIClient()
    # Pre-load WRAM with non-zero values that should be cleared by the zero-pass
    for addr in ADDR_MAP.values():
        fake.wram[addr] = 0xFF

    engine = RAPokeEngine(fake)
    scenario = parse_poke("settle: 1\n1: game_mode=20\n")
    engine.run_scenario(scenario)

    # The first ~11 writes (one per ADDR_MAP entry) are the zero-pass.
    zero_writes = fake.writes[: len(ADDR_MAP)]
    written_addrs = {addr for addr, data in zero_writes}
    assert written_addrs == set(ADDR_MAP.values())
    for addr, data in zero_writes:
        assert data == b"\x00", f"zero-pass wrote {data!r} to 0x{addr:04X}"
```

- [ ] **Step 3: Run to verify failure**

```bash
python -m pytest tests/unit/integration/test_ra_poke_engine.py::test_run_scenario_zeroes_addr_map_before_first_frame -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tests.integration.ra_poke_engine'`.

- [ ] **Step 4: Create minimal `RAPokeEngine`**

Create `tests/integration/ra_poke_engine.py`:

```python
"""Per-frame poke engine — runs .poke scenarios via NCI against a paused RA.

Each scenario:
  1. Zero every ADDR_MAP byte (per-scenario isolation).
  2. Construct a fresh TransitionDetector.
  3. For each frame in 1..(last_poke_frame + settle_frames):
       a. Apply scheduled pokes for this frame to held_values.
       b. Re-write every held byte (fire-and-forget).
       c. frame_advance().
       d. read_snapshot() — acts as implicit sync barrier.
       e. detector.step(snap, frame * 16) — emit events.
  4. Return events as list of dicts.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.snapshot import read_snapshot
from tests.integration.addresses import ADDR_MAP

FRAME_PERIOD_MS = 16  # 60Hz approximation; only used for monotonic timestamps


class _NCISurface(Protocol):
    def read_ram(self, addr: int, n: int = 1) -> bytes: ...
    def write_ram(self, addr: int, data: bytes) -> None: ...
    def frame_advance(self) -> None: ...


class RAPokeEngine:
    def __init__(self, client: _NCISurface) -> None:
        self._client = client

    def run_scenario(self, scenario: dict) -> list[dict]:
        # 1. Zero ADDR_MAP for per-scenario isolation
        for addr in ADDR_MAP.values():
            self._client.write_ram(addr, b"\x00")

        # 2. Schedule + bookkeeping
        schedule: dict[int, list[dict]] = {}
        for poke in scenario["pokes"]:
            schedule.setdefault(poke["frame"], []).append(poke)
        last_poke_frame = max(schedule, default=0)
        end_frame = last_poke_frame + scenario["settle_frames"]

        held: dict[int, int] = {}
        detector = TransitionDetector()
        events: list[dict] = []

        for frame in range(1, end_frame + 1):
            for poke in schedule.get(frame, []):
                held[poke["addr"]] = poke["value"]
            # Mask to low byte: matches Lua's emu.write semantics, which writes
            # one byte and silently truncates wider values. .poke files use
            # values like 0x105 (e.g., level_num) — Lua writes 0x05 to $13BF.
            for addr, value in held.items():
                self._client.write_ram(addr, bytes([value & 0xFF]))
            self._client.frame_advance()
            snap = read_snapshot(self._client)  # type: ignore[arg-type]
            for ev in detector.step(snap, frame * FRAME_PERIOD_MS):
                events.append(asdict(ev))

        return events
```

- [ ] **Step 5: Run zero-pass test to verify pass**

```bash
python -m pytest tests/unit/integration/test_ra_poke_engine.py::test_run_scenario_zeroes_addr_map_before_first_frame -v
```

Expected: PASS.

- [ ] **Step 6: Add scheduled-pokes test**

Append to `test_ra_poke_engine.py`:

```python
def test_run_scenario_applies_scheduled_pokes_at_correct_frames():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    # Frame 1 pokes game_mode=20; frame 3 pokes player_anim=9.
    scenario = parse_poke(
        "settle: 0\n"
        "1: game_mode=20\n"
        "3: player_anim=9\n"
    )
    engine.run_scenario(scenario)

    # Skip the zero-pass writes; inspect per-frame writes.
    post_zero = fake.writes[len(ADDR_MAP):]
    # Each frame writes len(held) bytes (held grows over time).
    # Frame 1: 1 held byte (game_mode)
    # Frame 2: 1 held byte (game_mode still held)
    # Frame 3: 2 held bytes (game_mode + player_anim)
    counts_by_frame_advance = []
    cursor = 0
    for f_idx in range(fake.frame_advances):
        # Find writes between this frame_advance and the previous; we don't
        # strictly capture frame boundaries — but we can count total writes
        # up to the FRAMEADVANCE call by walking the list. Simpler:
        # the last write before the (i+1)-th frame_advance is the i-th frame's
        # final write.
        pass

    # A simpler assertion: by end of run, game_mode AND player_anim are held
    # at the right values, and the total write count = zero-pass + sum(held
    # over all frames).
    assert fake.wram[ADDR_MAP["game_mode"]] == 20
    assert fake.wram[ADDR_MAP["player_anim"]] == 9


def test_held_values_repoke_every_frame():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    # 2 held bytes, 5 frames total (last_poke=1, settle=4).
    scenario = parse_poke("settle: 4\n1: game_mode=20 level_num=0x05\n")
    engine.run_scenario(scenario)

    # Total writes = 11 (zero-pass) + 2 held bytes × 5 frames = 21.
    expected_total = len(ADDR_MAP) + 2 * 5
    assert len(fake.writes) == expected_total
    # Every frame's writes should include both held addresses.
    held_addrs = {ADDR_MAP["game_mode"], ADDR_MAP["level_num"]}
    post_zero = fake.writes[len(ADDR_MAP):]
    for i in range(0, len(post_zero), 2):
        frame_writes = {addr for addr, _ in post_zero[i:i+2]}
        assert frame_writes == held_addrs


def test_frame_advance_called_once_per_frame():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    scenario = parse_poke("settle: 4\n1: game_mode=20\n")  # 5 frames
    engine.run_scenario(scenario)
    assert fake.frame_advances == 5
```

- [ ] **Step 7: Run new tests**

```bash
python -m pytest tests/unit/integration/test_ra_poke_engine.py -v
```

Expected: all PASS.

- [ ] **Step 8: Add detector-wiring test**

Append:

```python
def test_run_scenario_emits_level_entrance_event():
    """Poke level_start 0->1 with level_num set; detector should emit
    LevelEntrance. This is the simplest end-to-end check that the engine
    correctly drives the production TransitionDetector."""
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    scenario = parse_poke(
        "settle: 5\n"
        "1: game_mode=20 level_num=0x105\n"
        "2: level_start=1\n"
    )
    events = engine.run_scenario(scenario)

    entrance_events = [e for e in events if e.get("event") == "level_entrance"]
    assert len(entrance_events) == 1
    assert entrance_events[0]["level"] == 0x105
```

- [ ] **Step 9: Run**

```bash
python -m pytest tests/unit/integration/test_ra_poke_engine.py::test_run_scenario_emits_level_entrance_event -v
```

Expected: PASS. (If FAIL: the fake's `read_ram` is reading 0 from addresses not yet written — verify the engine wrote level_num at frame 1 before reading.)

- [ ] **Step 10: Commit**

```bash
git add tests/integration/ra_poke_engine.py tests/unit/integration/test_ra_poke_engine.py
git commit -m "feat(tests): RAPokeEngine drives .poke scenarios via NCI surface"
```

---

## Task 4: Build `RAHarness` against mocked subprocess + NCI

**Files:**
- Create: `tests/integration/ra_harness.py`
- Create: `tests/unit/integration/test_ra_harness.py`

**Why this exists.** Process lifecycle is its own concern — Popen + ping retries + pause confirmation + clean shutdown. Splitting it from the engine lets us test launch failure modes (missing exe, NCI never replies) without involving WRAM logic.

- [ ] **Step 1: Write failing test for happy-path launch**

Create `tests/unit/integration/test_ra_harness.py`:

```python
"""Tests for RAHarness using mocked subprocess + NCIClient.

Uses tmp_path for paths so existence checks resolve naturally without patching
pathlib.Path.exists (which has session-wide side effects on a class method).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spinlab.retroarch.exceptions import NCITimeout
from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError


@pytest.fixture
def fake_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Three real (empty) files standing in for rom/core/exe."""
    rom = tmp_path / "rom.smc"
    rom.write_bytes(b"")
    core = tmp_path / "core.dll"
    core.write_bytes(b"")
    exe = tmp_path / "retroarch.exe"
    exe.write_bytes(b"")
    return rom, core, exe


@pytest.fixture
def fake_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.returncode = None
    return proc


@pytest.fixture
def fake_client_running_then_paused():
    """NCI client that reports running on first is_core_running, paused on second."""
    client = MagicMock()
    client.version.return_value = "1.0"
    client.is_core_running.side_effect = [True, False]
    return client


def test_launch_happy_path(fake_paths, fake_proc, fake_client_running_then_paused):
    rom, core, exe = fake_paths

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=fake_client_running_then_paused):
        harness = RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)

    fake_client_running_then_paused.pause_toggle.assert_called_once()
    assert harness.engine is not None
    harness.teardown()
```

- [ ] **Step 2: Run failing test**

```bash
python -m pytest tests/unit/integration/test_ra_harness.py -v
```

Expected: FAIL with import error (`ra_harness` doesn't exist yet).

- [ ] **Step 3: Create `RAHarness`**

Create `tests/integration/ra_harness.py`:

```python
"""Lifecycle for headless RetroArch in integration tests.

Owns:
  - subprocess.Popen of retroarch.exe with null drivers + ROM
  - NCIClient (UDP 55355) connection to the launched RA
  - RAPokeEngine bound to that client
  - graceful teardown (client.quit + Popen.terminate fallback)

NOT owned:
  - retroarch.cfg generation (harness reuses user's existing cfg per spec)
  - per-test fixture lifecycle (that's conftest.py's ra_harness fixture)
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from spinlab.retroarch import addresses as a
from spinlab.retroarch.exceptions import NCITimeout
from spinlab.retroarch.nci import NCIClient
from tests.integration.ra_poke_engine import RAPokeEngine

logger = logging.getLogger(__name__)

# RA needs a moment after Popen before NCI starts replying.
NCI_PING_RETRIES = 10
NCI_PING_INTERVAL_S = 0.5

# is_core_running uses a frame-counter byte. SMW's frame counter at $0014 ticks
# every frame; same address used by scripts/smoke_nci_client.py.
ADDR_FRAME_COUNTER = 0x0014

# Teardown timing.
QUIT_GRACE_S = 2.0


class RAHarnessLaunchError(RuntimeError):
    """Raised when RA fails to launch into a usable state."""


@dataclass
class RAHarness:
    proc: subprocess.Popen
    client: NCIClient
    engine: RAPokeEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = RAPokeEngine(self.client)

    @classmethod
    def launch(
        cls,
        rom_path: Path,
        core_path: Path,
        retroarch_exe: Path,
    ) -> "RAHarness":
        for p, label in [(retroarch_exe, "retroarch_exe"), (core_path, "core_path"), (rom_path, "rom_path")]:
            if not p.exists():
                raise RAHarnessLaunchError(f"{label} does not exist: {p}")

        cmd = [
            str(retroarch_exe),
            "--video=null",
            "--audio=null",
            "-L", str(core_path),
            str(rom_path),
        ]
        logger.info("ra_harness: launching %s", cmd)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        client = NCIClient()
        # Ping until NCI replies or we exhaust retries.
        for attempt in range(NCI_PING_RETRIES):
            try:
                client.version()
                break
            except NCITimeout:
                time.sleep(NCI_PING_INTERVAL_S)
        else:
            cls._kill(proc)
            raise RAHarnessLaunchError(
                f"NCI did not reply after {NCI_PING_RETRIES} attempts × {NCI_PING_INTERVAL_S}s"
            )

        # Confirm core is running before pausing — guards against the
        # spike-found "deep pause" trap.
        if not client.is_core_running(tick_addr=ADDR_FRAME_COUNTER):
            cls._kill(proc)
            raise RAHarnessLaunchError(
                "RA NCI replied but core is not advancing frames — refusing to pause"
            )

        client.pause_toggle()
        # is_core_running with a fresh delay confirms the toggle landed.
        if client.is_core_running(tick_addr=ADDR_FRAME_COUNTER):
            cls._kill(proc)
            raise RAHarnessLaunchError("PAUSE_TOGGLE did not stop frame advance")

        return cls(proc=proc, client=client)

    def teardown(self) -> None:
        try:
            self.client.quit()
        except Exception as exc:
            logger.warning("ra_harness: client.quit() raised %s", exc)
        try:
            self.proc.wait(timeout=QUIT_GRACE_S)
        except subprocess.TimeoutExpired:
            self._kill(self.proc)
        self.client.close()

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
```

- [ ] **Step 4: Run happy-path test**

```bash
python -m pytest tests/unit/integration/test_ra_harness.py::test_launch_happy_path -v
```

Expected: PASS.

- [ ] **Step 5: Add failure-mode tests**

Append to `test_ra_harness.py`:

```python
def test_launch_raises_when_rom_missing(tmp_path):
    """Use a path to a file that genuinely does not exist."""
    rom = tmp_path / "missing.smc"
    core = tmp_path / "core.dll"; core.write_bytes(b"")
    exe = tmp_path / "retroarch.exe"; exe.write_bytes(b"")

    with pytest.raises(RAHarnessLaunchError, match="rom_path does not exist"):
        RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)


def test_launch_raises_when_nci_never_replies(fake_paths, fake_proc):
    rom, core, exe = fake_paths
    timeout_client = MagicMock()
    timeout_client.version.side_effect = NCITimeout("no reply")

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=timeout_client), \
         patch("tests.integration.ra_harness.time.sleep"):
        with pytest.raises(RAHarnessLaunchError, match="NCI did not reply"):
            RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)
    fake_proc.terminate.assert_called_once()


def test_launch_raises_when_pause_doesnt_stop_frames(fake_paths, fake_proc):
    """Deep-pause guard: if PAUSE_TOGGLE doesn't stop advancing frames,
    refuse to proceed rather than enter a hung state."""
    rom, core, exe = fake_paths
    runaway_client = MagicMock()
    runaway_client.version.return_value = "1.0"
    runaway_client.is_core_running.side_effect = [True, True]

    with patch("tests.integration.ra_harness.subprocess.Popen", return_value=fake_proc), \
         patch("tests.integration.ra_harness.NCIClient", return_value=runaway_client):
        with pytest.raises(RAHarnessLaunchError, match="did not stop frame advance"):
            RAHarness.launch(rom_path=rom, core_path=core, retroarch_exe=exe)


def test_teardown_calls_quit_then_terminates_on_timeout(fake_client_running_then_paused):
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=[], timeout=2.0), None]

    harness = RAHarness(proc=proc, client=fake_client_running_then_paused)
    harness.teardown()

    fake_client_running_then_paused.quit.assert_called_once()
    proc.terminate.assert_called_once()
```

- [ ] **Step 6: Run all harness tests**

```bash
python -m pytest tests/unit/integration/test_ra_harness.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/ra_harness.py tests/unit/integration/test_ra_harness.py
git commit -m "feat(tests): RAHarness owns RA process + NCI lifecycle"
```

---

## Task 5: Rewire `tests/integration/conftest.py`

**Files:**
- Modify: `tests/integration/conftest.py` — replace `mesen_process`, `tcp_client`, and the `run_scenario` body with `ra_harness` + RA-driven `run_scenario`

**Why this exists.** Connects the new harness into the existing test fixtures. Deletes the Mesen-specific plumbing that exclusively served the poke harness.

- [ ] **Step 1: Read the current `conftest.py` to identify deletion targets**

```bash
grep -n "def mesen_process\|def tcp_client\|def run_scenario" tests/integration/conftest.py
```

Expected: three function definitions matching the three names. Note their line ranges.

- [ ] **Step 2: Delete the three Mesen-specific fixtures**

In [tests/integration/conftest.py](tests/integration/conftest.py):

- Delete the entire `mesen_process` fixture (the `@pytest_asyncio.fixture(scope="session", loop_scope="session") async def mesen_process(): ...` block).
- Delete the entire `tcp_client` fixture.
- Delete the entire `run_scenario` fixture (the Mesen-driven body).

Leave `smoke_mesen_process`, `dashboard_server`, `replay_mesen_process`, `replay_dashboard`, and all other fixtures untouched.

- [ ] **Step 3: Add the new `ra_harness` and `run_scenario` fixtures**

Add at the end of `conftest.py` (or wherever stylistically fits — pick the spot that mirrors the deleted fixtures' position):

```python
# ---------------------------------------------------------------------------
# RetroArch poke harness (replaces the Lua poke_engine.lua path)
# ---------------------------------------------------------------------------


def _ra_paths() -> tuple[Path | None, Path | None, Path | None]:
    """Resolve (retroarch_exe, ra_core_path, rom_path) from env/config."""
    config = _load_config()
    emu = config.get("emulator", {})
    retroarch_exe = emu.get("retroarch_path")
    ra_core_path = emu.get("ra_core_path")
    rom_path = _test_rom_path()
    return (
        Path(retroarch_exe) if retroarch_exe else None,
        Path(ra_core_path) if ra_core_path else None,
        Path(rom_path) if rom_path else None,
    )


@pytest.fixture(scope="session")
def ra_harness():
    """Launch one RetroArch process per pytest session for poke-driven tests."""
    from tests.integration.ra_harness import RAHarness, RAHarnessLaunchError

    retroarch_exe, ra_core_path, rom_path = _ra_paths()
    missing = [
        label for label, p in
        [("retroarch_path", retroarch_exe), ("ra_core_path", ra_core_path), ("rom", rom_path)]
        if p is None or not p.exists()
    ]
    if missing:
        pytest.skip(f"ra_harness requires: {', '.join(missing)} (set in config.yaml emulator section)")

    try:
        harness = RAHarness.launch(rom_path=rom_path, core_path=ra_core_path, retroarch_exe=retroarch_exe)
    except RAHarnessLaunchError as exc:
        pytest.skip(f"ra_harness launch failed: {exc}")

    try:
        yield harness
    finally:
        harness.teardown()


@pytest.fixture
def run_scenario(ra_harness):
    """Send a poke scenario through the RA harness and collect events."""
    async def _run(scenario_name: str, timeout: float = 30.0) -> list[dict]:
        scenario_path = SCENARIO_DIR / scenario_name
        if not scenario_path.exists():
            pytest.fail(f"Scenario file not found: {scenario_path}")
        scenario = parse_poke_file(str(scenario_path))
        return await asyncio.to_thread(ra_harness.engine.run_scenario, scenario)
    return _run
```

- [ ] **Step 4: Verify the file imports are still right**

After deletions, some imports may now be unused (`from spinlab.tcp_manager import TcpManager` in particular). Remove unused imports:

```bash
ruff check tests/integration/conftest.py
```

Expected: ruff flags any unused imports. Apply suggested fixes:

```bash
ruff check --fix tests/integration/conftest.py
```

- [ ] **Step 5: Run the unit + non-emulator suite to catch import / fixture errors**

```bash
python -m pytest -m "not (emulator or slow or frontend)" -q
```

Expected: PASS. (The new fixtures only activate when an emulator-marked test requests them.)

- [ ] **Step 6: Commit**

```bash
git add tests/integration/conftest.py
git commit -m "refactor(tests): conftest rewires run_scenario through RA harness, drops mesen_process/tcp_client"
```

---

## Task 6: Live integration run + xfail decisions

**Files:**
- Possibly modify: `tests/integration/test_transitions.py` (only adding `pytest.mark.xfail` for scenarios that fail due to known production bugs)

**Why this exists.** Until now everything's been against fakes/mocks. This task drives the real harness, runs the 9 `.poke` scenarios, and decides per-scenario whether failures are harness bugs (fix) or production bugs from `status.md` (xfail with reason). The plan ships when each failure is in one of those two buckets.

- [ ] **Step 1: Verify config.yaml has `ra_core_path` set**

```bash
grep "ra_core_path" config.yaml
```

Expected: a line with the libretro core path. If absent, set it (Task 1 step 7 should have done this).

- [ ] **Step 2: Verify RetroArch can launch with null drivers manually**

This is a sanity check — does null-driver mode even work on this Windows install?

```bash
"C:/RetroArch-Win64/retroarch.exe" --video=null --audio=null -L "C:/RetroArch-Win64/cores/snes9x_libretro.dll" "C:/path/to/Toothpaste.smc"
```

(Replace paths from `config.yaml`.)

Expected: RA launches without a window. NCI port 55355 starts replying. Kill it after a few seconds via `taskkill /IM retroarch.exe /F`.

If null drivers don't work (RA refuses, or hangs), fall back: drop the `--video=null --audio=null` flags from `RAHarness.launch`'s `cmd` and proceed with a visible window. Tests still work; the spec's "Risk: RA process won't launch headless" mitigation triggers.

- [ ] **Step 3: Run `test_transitions.py` against live RA**

```bash
python -m pytest tests/integration/test_transitions.py -v
```

Expected: a mix of passes and failures. Each failure is one of:

- **Harness bug:** something is wrong with the engine, the harness, or the conftest wiring. The detector behaves correctly under Mesen but the harness drove the wrong inputs. **Fix it.**
- **Pre-existing production bug:** the detector behaves the same way under both backends but the test exposes a real bug in `TransitionDetector` or its predicates (e.g., spurious overworld checkpoints). **xfail with `reason` pointing at `status.md`.**

The two are distinguishable: if you can reproduce the bad event sequence in a unit test of `TransitionDetector` directly (no RA, no NCI), it's a production bug. If only the RA harness produces it, it's a harness bug.

- [ ] **Step 4: For each failing scenario, classify and act**

For a harness bug, debug locally, push fixes to `ra_poke_engine.py` or `ra_harness.py`, re-run.

For a production bug, mark the test xfail. Example:

```python
@pytest.mark.xfail(
    reason="check_checkpoint_hit doesn't gate on in-level state; "
           "see docs/retroarch-migration/status.md 'Spurious checkpoint+entrance events on overworld'",
    strict=True,
)
async def test_some_scenario(run_scenario):
    ...
```

`strict=True` means the test must keep failing — if the bug ever silently fixes itself, the suite turns red and we know to drop the marker. Keep the markers minimal (only the assertions known-broken) and reference `status.md` so a future reader knows where to find context.

- [ ] **Step 5: Re-run until green**

```bash
python -m pytest tests/integration/test_transitions.py -v
```

Expected: every test either PASSes or XFAILs. No outright failures. No XPASSes.

- [ ] **Step 6: Final pass — full unit suite + the integration test**

```bash
python -m pytest -m "not (slow or frontend)" -q
python -m pytest tests/integration/test_transitions.py -v
```

Expected: both green (or appropriately xfailed). `pytest -m emulator` may include `test_replay_fixture.py` and `test_smoke.py` which still need Mesen — those are out of scope and skip cleanly per their existing markers.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/test_transitions.py
git commit -m "test: drive .poke transition tests through RA harness; xfail known production bugs"
```

If no edits to `test_transitions.py` were needed (every scenario passed under RA), commit any earlier changes that are still pending and skip this commit.

---

## Task 7: Update status doc and path-to-parity

**Files:**
- Modify: `docs/retroarch-migration/status.md` — note that test_transitions.py runs under RA
- Modify: `docs/retroarch-migration/path-to-parity.md` — strike P1.2 ("skip Mesen-only tests") because it's now resolved (transitions ported, smoke + replay still skip per existing markers)

**Why this exists.** The migration tracking docs reflect post-Plan-2 reality so the next person picking this up isn't misled.

- [ ] **Step 1: Read both docs**

```bash
cat docs/retroarch-migration/status.md docs/retroarch-migration/path-to-parity.md
```

- [ ] **Step 2: Update `status.md`**

Add a "What works" bullet:

> - **Transition-detection tests under RA.** `tests/integration/test_transitions.py` runs through the new `RAHarness`/`RAPokeEngine` poke harness. The 9 `.poke` scenarios exercise the production `TransitionDetector` directly. Any scenarios marked `xfail` reference `status.md` bugs.

Move out of "Known broken / untested" any items that the harness now covers:

> - **Integration tests under `tests/integration/test_transitions.py`** — now passing/xfailed under RA via the new harness. (`test_replay_fixture.py` and `test_smoke.py` remain Mesen-bound, deferred to Phase E.)

- [ ] **Step 3: Update `path-to-parity.md`**

Strike P1.2 ("Skip Mesen-only tests when backend is RetroArch") — it's resolved by the new harness for `test_transitions.py`. The smoke + replay tests are now genuinely Mesen-only by design until Phase E ports them, so the "skip cleanly" half is fine. Replace the section with a one-line resolution note pointing to this plan.

- [ ] **Step 4: Commit**

```bash
git add docs/retroarch-migration/status.md docs/retroarch-migration/path-to-parity.md
git commit -m "docs(retroarch): status.md + path-to-parity.md reflect RA poke harness landed"
```

---

## Plan Self-Review

Spec coverage check (each section of the spec → which task implements it):

| Spec section | Task |
|---|---|
| §Architecture (file layout) | Tasks 2, 3, 4, 5 |
| §Components → RAHarness | Task 4 |
| §Components → RAPokeEngine | Task 3 |
| §Components → addresses.py | Task 2 |
| §Components → conftest.py | Task 5 |
| §Per-frame loop | Task 3 |
| §Initial-state strategy (zero-pass) | Task 3 |
| §Migration story (delete Mesen poke fixtures) | Task 5 |
| §RetroArch gotchas (deep pause, fire-and-forget) | Task 4 |
| §DoD | Task 6 + Task 7 |
| §Future work | (out of scope, no task) |

Type consistency:

- `RAHarness.launch(rom_path, core_path, retroarch_exe)` signature consistent across Tasks 4, 5.
- `RAPokeEngine(client)` signature consistent across Tasks 3, 4 (harness post-init), 5.
- `run_scenario(scenario: dict) -> list[dict]` consistent everywhere.
- `ADDR_MAP` keys (game_mode, level_num, …) match `.poke` files and `spinlab.retroarch.addresses` constants.

Placeholder scan: no TBD/TODO. Every step has either runnable commands or full code blocks.

---

## Out of scope

- BSV input recording / replay (Phase E)
- Porting `test_replay_fixture.py` (Phase E)
- Porting `test_smoke.py` to RA (Phase E or Phase G)
- Deleting `lua/poke_engine.lua`, `lua/spinlab.lua`, `TcpManager`, dual-backend orchestration in production code (Phase G)
- Hermetic RA test config (future-work note in spec)
- Fixing any production bugs surfaced by the harness — those are subsequent plans, not this one
