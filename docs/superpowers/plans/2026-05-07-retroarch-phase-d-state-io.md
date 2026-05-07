# RetroArch Migration — Phase D: Savestate I/O via NCI + Filesystem

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `lua/spinlab.lua`'s save/load logic — `save_state_to_file`, `load_state_from_file`, `pending_saves`, `pending_loads`, the cpuExec-deferred drain — to a Python module that drives RetroArch via NCI plus filesystem shuffles. Replace per-frame queueing with synchronous, immediate save/load operations. Surface a small `StateIO` API that can be plugged into the Phase C poller (via a `state_path_for` resolver) and consumed by Phase F's session_manager wiring.

**Architecture:** RetroArch's NCI `SAVE_STATE` writes a slot file that SpinLab does not own. We pick a high reserved slot (`9999`) for SpinLab's swap operations, fire `SAVE_STATE` after navigating to that slot, wait for the slot file's mtime to advance (filesystem confirmation), then move the file to a SpinLab-keyed path under `spinlab_state_dir/<segment_id>.state`. Loads reverse the move and issue `LOAD_STATE_SLOT 9999`. The user's manual save-state slot counter is left undisturbed because we navigate back after the operation. All filesystem operations are unit-testable against `tmp_path`; NCI is faked with a stub client.

**Tech Stack:** Python 3.11+, stdlib `pathlib`, `shutil`, `time`, pytest with `tmp_path` and `monkeypatch`. Builds on Phase B's `NCIClient` and Phase C's `Poller` / `events` module.

**Phase A audit reference:** [`docs/retroarch-migration/lua-audit.md`](../../retroarch-migration/lua-audit.md) (Save/load row + Phase C followups #1, #2). Spec: [`docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`](../specs/2026-05-06-retroarch-migration-design.md) (Phase D section + Open Question #1). The Lua to port: `lua/spinlab.lua` `save_state_to_file` (241–256), `load_state_from_file` (258–273), `pending_saves`/`pending_loads` drain (1242–1263), and the `state_path = STATE_DIR/.../<level>_cp<n>_<hot|cold>.mss` path-computation idioms (525, 573, 604, 671).

**What this phase does NOT do:**
- Wire `StateIO` into `session_manager` or `practice.py` — that's Phase F-live.
- Touch the existing Lua TCP path (`tcp_manager.py`, `lua/spinlab.lua`); they keep running until Phase G.
- Build BSV / replay — that's Phase E.
- Add live-RetroArch dependencies to unit tests. All tests use `tmp_path` and a `_FakeNCIClient` stub.
- Solve `ADDR_CP_ENTRANCE` re-verification (still tracked as a Phase C followup).

---

## File Structure

| Path | Purpose |
|------|---------|
| `python/spinlab/retroarch/state_io.py` | `StateIO` class. Owns the SAVE_STATE→copy→rename and copy→LOAD_STATE_SLOT shuffles. Pure sync. |
| `python/spinlab/retroarch/state_paths.py` | Pure helpers: `segment_state_filename(segment_id) -> str`, `ra_slot_filename(game_basename, slot) -> str`. Path math, no I/O. |
| `python/spinlab/retroarch/poller.py` | **Amended** — `PollerDeps` gains `state_path_for: Callable[[TransitionEvent], str] | None = None`; the poller calls it per emitted event before forwarding to `on_event`. |
| `python/spinlab/retroarch/events.py` | **Amended** — `Spawn` gains `segment_id: str = ""` (Phase C followup #1; required so the resolver can compute the cold-fill path). |
| `tests/unit/retroarch/test_state_paths.py` | Pure path helpers — no I/O. |
| `tests/unit/retroarch/test_state_io_save.py` | Save flow: NCI fired, mtime polled, file moved into SpinLab path. |
| `tests/unit/retroarch/test_state_io_load.py` | Load flow: SpinLab file copied back into RA's slot, NCI fired. Missing-file error path. |
| `tests/unit/retroarch/test_state_io_resolver.py` | `StateIO.state_path_for(event)` resolver behaviour for each event type. |
| `tests/unit/retroarch/test_poller_state_path.py` | Verifies the poller calls `state_path_for` and stamps the result onto each event before delegating to `on_event`. |

---

## Design Decisions

These are locked in for Phase D. Open Question #1 from the spec is closed by Decision 1; the others reflect choices that the spec left implicit.

### Decision 1: Slot management strategy — **Option C (NCI + filesystem shuffle)**

The spec's Open Question #1 listed three options. We adopt **Option C**: SpinLab calls `SAVE_STATE` and immediately copies the resulting RA slot file to a SpinLab-keyed path; loads reverse the copy and call `LOAD_STATE_SLOT N`.

**Why C, not A or B:**
- **A (reserved slot range, e.g. 9000+)** depends on `SAVE_STATE_SLOT N` existing as an NCI command. Phase 2 spike never confirmed this, and the libretro NCI docs are explicit only about `LOAD_STATE_SLOT`. Building on an unverified primitive would put us at risk of a mid-Phase-D rewrite.
- **B (navigate via `STATE_SLOT_PLUS/MINUS`)** disturbs the user's slot counter mid-session. Even if we navigate back, race conditions exist if the user manually saves between our navigate-out and navigate-back. Andrew explicitly wants his manual auto-index sequence preserved.
- **C** is the safest. We use exactly one reserved slot (`9999` — see Decision 6), which the user is unlikely to navigate to manually. RA's auto-index counter is left alone because we don't navigate. Worst case: one slot file (`<game>.state9999`) sits in the user's savestate dir; we always overwrite it on the next save.

**Tradeoff documented:** the user's `<game>.state9999` slot is reserved by SpinLab and will be overwritten without warning. This is a deliberate convention; surfaced in the README during Phase F.

### Decision 2: Sync vs async — **sync, like NCIClient**

`NCIClient` is sync (Phase B). The poller is async, but state_io is invoked from synchronous code paths (e.g., `recorder.py`, `practice.py`). Make `StateIO` sync; async callers wrap individual methods in `asyncio.to_thread` if they need to. This matches Phase B's pattern and keeps unit tests simple.

### Decision 3: Public API — small and focused

```python
class StateIO:
    def __init__(
        self,
        client: NCIClient,
        ra_savestate_dir: Path,
        spinlab_state_dir: Path,
        ra_game_basename: str,  # e.g. "Toothpaste World"; the basename RA uses for slot files
        reserved_slot: int = 9999,
        save_timeout_sec: float = 1.0,
    ) -> None: ...

    def save_segment_state(self, segment_id: str) -> Path: ...
    """Trigger NCI SAVE_STATE, wait for the slot file, move it to the SpinLab path. Returns the SpinLab path."""

    def load_segment_state(self, segment_id: str) -> None: ...
    """Copy the SpinLab file back into RA's slot path, fire LOAD_STATE_SLOT. Raises FileNotFoundError if no state."""

    def state_path_for(self, segment_id: str) -> Path: ...
    """Pure path resolution (no I/O). Returns where the file would live."""

    def has_state_for(self, segment_id: str) -> bool: ...
    """Filesystem check: does the SpinLab-keyed file exist?"""

    def resolve_event_path(self, event: TransitionEvent) -> str: ...
    """Resolver suitable for PollerDeps.state_path_for. Knows which events get paths and what segment_id key to use for each. Returns "" for events that don't need paths."""
```

The "RA slot used" (`reserved_slot`) is an implementation detail — exposed as a constructor parameter for testing and edge cases (e.g., user has actually used slot 9999), but defaults to `9999` and is documented but not part of the typical caller contract.

### Decision 4: Populating `state_path` on events — **resolver callback in PollerDeps**

The spec's Phase C closeout followup #2 deferred `LevelEntrance.state_path`, `Checkpoint.state_path`, `Spawn.state_path` population to Phase D. Three options were on the table:
- **A.** Resolver callback in `PollerDeps`.
- **B.** Detector emits `state_path=""`, poller post-processes via callback before forwarding (same callback, just different wording).
- **C.** Populate in session_manager when it ingests events.

We adopt **Option A**. It is structurally the same as B but plumbed through `PollerDeps` cleanly, which already exists as the dep-injection seam. Phase F still gets to decide what session_manager does with the path; the poller just makes sure events leaving the poller already have their `state_path` populated when one is appropriate.

**This requires a tiny Phase C amendment in Task 2 of this plan.** Extend `PollerDeps` with `state_path_for: Callable[[TransitionEvent], str] | None = None`; have the poller, before calling `on_event(ev)`, do:

```python
if self._deps.state_path_for is not None:
    new_path = self._deps.state_path_for(ev)
    if new_path:
        ev = dataclasses.replace(ev, state_path=new_path)
```

Frozen dataclasses don't support direct mutation, so we use `dataclasses.replace`. `LevelEntrance`, `Checkpoint`, `Spawn` all have `state_path` fields; for `Death`/`LevelExit` (no `state_path`) the resolver returns `""` and the poller skips the replace.

### Decision 5: SAVE_STATE confirmation — **filesystem mtime polling**

NCI's `SAVE_STATE` is fire-and-forget (per Phase B's `_send_no_reply`). state_io needs to know when RA has finished writing the slot file before moving it. Options:
- **A.** Filesystem polling: read the slot-file mtime before SAVE_STATE, fire, then poll until mtime advances or timeout.
- **B.** Sleep a small fixed amount (50–100ms).
- **C.** Use `is_core_running(tick_addr)` as a sanity gate.

We adopt **Option A (mtime polling)**. Sleep is fragile across Windows / WSL / different drive types. mtime is reliable, fast, and gives a real failure path (timeout → `StateSaveTimeout` exception) instead of a silent race. We do **not** use `is_core_running` here; it's the wrong primitive (it tells us the core is advancing, not that the save completed). We use a 1-second default timeout, which is generous (real save times on a healthy RA install are well under 100ms).

If the slot file doesn't exist before SAVE_STATE (first capture of the session), we treat any subsequent existence as a successful save.

### Decision 6: Reserved slot number — **9999**

We need a slot number that is:
- Unlikely to collide with the user's manual saves (auto-index typically counts up from 0).
- A single fixed value, not a range, because we only need one swap slot.
- Documented and surfaceable to users.

**`9999`** satisfies all three. RA's auto-index would have to wrap around or be deliberately set very high to collide. We document the reserved slot in the README at Phase F. Configurable via `StateIO`'s `reserved_slot=` kwarg if a user actually hits 9999.

---

## Task 1: Pure path helpers

The `state_paths` module exposes two pure functions for filename construction. Separating these from `StateIO` keeps the state_io module focused on side effects.

**Files:**
- Create: `python/spinlab/retroarch/state_paths.py`
- Create: `tests/unit/retroarch/test_state_paths.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_state_paths.py
"""Pure path-helper tests — no I/O, no fixtures needed."""
import pytest

from spinlab.retroarch.state_paths import (
    ra_slot_filename,
    segment_state_filename,
)


def test_segment_state_filename_basic():
    assert segment_state_filename("seg-abc123") == "seg-abc123.state"


def test_segment_state_filename_sanitizes_path_separators():
    """segment_id may contain colons / slashes (e.g. game:level:cp). Replace those."""
    assert segment_state_filename("game:5:cp1") == "game_5_cp1.state"
    assert segment_state_filename("foo/bar") == "foo_bar.state"


def test_segment_state_filename_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        segment_state_filename("")


def test_ra_slot_filename_basic():
    """Mirrors RA's <game_basename>.state<slot> convention."""
    assert ra_slot_filename("Toothpaste World", 9999) == "Toothpaste World.state9999"
    assert ra_slot_filename("game", 0) == "game.state0"


def test_ra_slot_filename_zero_slot_no_suffix_number():
    """RA's auto-index slot 0 still uses .state0 over NCI's LOAD_STATE_SLOT."""
    # (This documents what we send; cross-check with live RA during Phase F.)
    assert ra_slot_filename("g", 0) == "g.state0"
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_state_paths.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/state_paths.py
"""Pure path-helper functions for SpinLab + RA savestate filenames.

Kept separate from state_io so the path math is unit-testable without any
filesystem or NCI dependencies. state_io composes these to do its job.
"""
from __future__ import annotations

# Characters that segment_id may legitimately contain (game:level:cp idiom)
# but that we cannot put in a file name. Replaced with underscores.
_PATH_SEPARATOR_CHARS = (":", "/", "\\")

# Lua used .mss; we use .state to match the file extension RA already uses
# for slot files (RA: <game>.state<N>; SpinLab: <segment>.state). One byte
# per file, single-extension naming, easier to grep for.
_SPINLAB_STATE_EXT = ".state"


def segment_state_filename(segment_id: str) -> str:
    """Filename for a SpinLab-managed savestate keyed by segment id.

    segment_ids in SpinLab can include colons (e.g. "game:5:cp1") and other
    separators that aren't filesystem-safe; replace them with underscores.
    """
    if not segment_id:
        raise ValueError("segment_id is empty")
    sanitized = segment_id
    for ch in _PATH_SEPARATOR_CHARS:
        sanitized = sanitized.replace(ch, "_")
    return sanitized + _SPINLAB_STATE_EXT


def ra_slot_filename(game_basename: str, slot: int) -> str:
    """Filename RA writes for a given slot.

    Mirrors the convention RA uses for save-state files in `savestate_directory`.
    Example: for game "Toothpaste World" and slot 9999, the file is
    "Toothpaste World.state9999". Slot 0 still gets the suffix ".state0".
    """
    return f"{game_basename}.state{slot}"
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_state_paths.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_paths.py tests/unit/retroarch/test_state_paths.py
git commit -m "feat(retroarch): pure path helpers for SpinLab + RA slot filenames"
```

---

## Task 2: Poller amendment — `state_path_for` resolver and `Spawn.segment_id`

Two small Phase C amendments are needed before `StateIO` can be useful end-to-end:

1. `Spawn` event needs a `segment_id` field (Phase C followup #1; required so the resolver knows which segment a cold-fill spawn captures for).
2. `PollerDeps` needs a `state_path_for` callback; `Poller.run()` must call it (when set) and stamp the result onto the event before forwarding (Phase C followup #2; per Design Decision 4 above).

**Files:**
- Edit: `python/spinlab/retroarch/events.py`
- Edit: `python/spinlab/retroarch/poller.py`
- Edit: `python/spinlab/retroarch/cold_fill.py` (populate `segment_id` on the emitted Spawn)
- Create: `tests/unit/retroarch/test_poller_state_path.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_poller_state_path.py
"""The poller calls state_path_for(event) and stamps results onto events."""
import asyncio
from typing import Iterator

import pytest

from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    pass


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    def fn(_client) -> MemorySnapshot:
        return next(seq)
    return fn


@pytest.mark.asyncio
async def test_state_path_for_called_on_each_event():
    """The resolver runs for every emitted event and its return is stamped."""
    snapshots = iter([
        _snap(level_num=5),  # seed
        _snap(level_num=5, level_start=1),  # entrance
    ])
    received: list[TransitionEvent] = []
    resolver_calls: list[TransitionEvent] = []

    def resolver(ev: TransitionEvent) -> str:
        resolver_calls.append(ev)
        if isinstance(ev, LevelEntrance):
            return "/states/seg-1.state"
        return ""

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=resolver,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntrance)]
    assert len(entrances) == 1
    assert entrances[0].state_path == "/states/seg-1.state"
    assert resolver_calls, "resolver was never invoked"


@pytest.mark.asyncio
async def test_resolver_returning_empty_keeps_existing_state_path():
    """When the resolver returns '', the event's state_path stays as detector emitted it (default '')."""
    snapshots = iter([
        _snap(player_anim=0),
        _snap(player_anim=9),
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=lambda ev: "",
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    deaths = [e for e in received if isinstance(e, Death)]
    assert len(deaths) == 1
    # Death has no state_path field; this asserts we didn't crash trying to set one.


@pytest.mark.asyncio
async def test_resolver_optional_default_none():
    """If state_path_for is None, the poller skips resolution entirely."""
    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        # state_path_for omitted — defaults to None.
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntrance)]
    assert len(entrances) == 1
    assert entrances[0].state_path == ""  # detector default unchanged


def test_spawn_has_segment_id_field():
    """Spawn must carry segment_id so cold-fill resolution can compute its path."""
    s = Spawn(timestamp_ms=0, level_num=5, segment_id="seg-x")
    assert s.segment_id == "seg-x"
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_poller_state_path.py -v
```

Expected: FAIL — `Spawn` has no `segment_id`, `PollerDeps` has no `state_path_for`, poller doesn't call resolver.

- [ ] **Step 3: Implement amendments**

In `python/spinlab/retroarch/events.py`, extend `Spawn`:

```python
@dataclass(frozen=True)
class Spawn(TransitionEvent):
    level_num: int = 0
    is_cold_cp: bool = False
    cp_ordinal: int = 0
    state_captured: bool = False
    state_path: str = ""
    segment_id: str = ""  # populated by ColdFillTracker; resolver uses it as the path key
```

In `python/spinlab/retroarch/cold_fill.py`, populate it:

```python
emitted = Spawn(
    timestamp_ms=timestamp_ms,
    level_num=curr.level_num,
    is_cold_cp=True,
    cp_ordinal=0,
    state_captured=True,
    segment_id=self._segment_id or "",
)
```

In `python/spinlab/retroarch/poller.py`:

```python
import dataclasses
# ...

@dataclass
class PollerDeps:
    client: NCIClient
    read_snapshot: Callable[[NCIClient], MemorySnapshot]
    on_event: Callable[[TransitionEvent], None]
    state_path_for: Callable[[TransitionEvent], str] | None = None


class Poller:
    # ...

    def _stamp_state_path(self, ev: TransitionEvent) -> TransitionEvent:
        """Apply state_path_for resolver if configured. Returns event with stamped path."""
        if self._deps.state_path_for is None:
            return ev
        path = self._deps.state_path_for(ev)
        if not path:
            return ev
        # dataclasses.replace works only on classes that have the field; for events
        # without state_path the resolver should return "" (handled above).
        if not hasattr(ev, "state_path"):
            return ev
        return dataclasses.replace(ev, state_path=path)

    async def run(self) -> None:
        while not self._stopped:
            try:
                snap = self._deps.read_snapshot(self._deps.client)
            except Exception:
                await asyncio.sleep(self._period)
                continue

            ts = int(time.perf_counter() * 1000 - self._start_ms)

            if self._state_just_loaded:
                self._detector.resync_after_state_load(snap)
                self._state_just_loaded = False
                await asyncio.sleep(self._period)
                continue

            for event in self._detector.step(snap, timestamp_ms=ts):
                self._deps.on_event(self._stamp_state_path(event))

            cf_event = self._cold_fill.step(snap, timestamp_ms=ts)
            if cf_event is not None:
                self._deps.on_event(self._stamp_state_path(cf_event))

            await asyncio.sleep(self._period)
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_poller_state_path.py tests/unit/retroarch/test_poller.py tests/unit/retroarch/test_cold_fill.py tests/unit/retroarch/test_events.py -v
```

Expected: all green. The Phase C tests must continue to pass (`state_path_for=None` is the default).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/events.py python/spinlab/retroarch/poller.py python/spinlab/retroarch/cold_fill.py tests/unit/retroarch/test_poller_state_path.py
git commit -m "feat(retroarch): poller resolver hook + Spawn.segment_id (Phase D prep)"
```

---

## Task 3: `StateIO` constructor, path resolution, existence check

Pure-resolution methods first. No NCI calls, no save/load yet — just `state_path_for` and `has_state_for`. These are the methods most heavily exercised by the resolver path in Phase F, so getting them solid first pays off.

**Files:**
- Create: `python/spinlab/retroarch/state_io.py`
- Create: `tests/unit/retroarch/test_state_io_resolver.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_state_io_resolver.py
"""StateIO path-resolution and has_state_for tests. No NCI involvement here."""
from pathlib import Path

import pytest

from spinlab.retroarch.events import (
    Checkpoint,
    Death,
    LevelEntrance,
    LevelExit,
    Spawn,
)
from spinlab.retroarch.state_io import StateIO


class _FakeClient:
    """Stub NCIClient — none of these tests hit the wire."""


@pytest.fixture
def state_io(tmp_path):
    """Build StateIO against fresh tmp dirs. RA dir intentionally empty."""
    ra_dir = tmp_path / "ra_savestates"
    ra_dir.mkdir()
    sl_dir = tmp_path / "spinlab_states"
    sl_dir.mkdir()
    return StateIO(
        client=_FakeClient(),
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Test Game",
    )


def test_state_path_for_returns_keyed_path(state_io, tmp_path):
    p = state_io.state_path_for("seg-abc")
    assert p == tmp_path / "spinlab_states" / "seg-abc.state"


def test_state_path_for_sanitizes_segment_id(state_io, tmp_path):
    p = state_io.state_path_for("game:5:cp1")
    assert p == tmp_path / "spinlab_states" / "game_5_cp1.state"


def test_has_state_for_false_when_missing(state_io):
    assert state_io.has_state_for("seg-abc") is False


def test_has_state_for_true_after_file_created(state_io, tmp_path):
    f = tmp_path / "spinlab_states" / "seg-abc.state"
    f.write_bytes(b"x")
    assert state_io.has_state_for("seg-abc") is True


# resolve_event_path tests: per-event-type behaviour.

def test_resolve_event_path_level_entrance_uses_level_room(state_io, tmp_path):
    """LevelEntrance state path keyed by level+room (no segment_id known yet)."""
    ev = LevelEntrance(timestamp_ms=0, level=5, room=0)
    p = state_io.resolve_event_path(ev)
    # Convention: "entrance_<level>_<room>"
    assert p.endswith("entrance_5_0.state")


def test_resolve_event_path_checkpoint_uses_level_ordinal(state_io, tmp_path):
    ev = Checkpoint(timestamp_ms=0, level_num=5, cp_type="midway", cp_ordinal=2)
    p = state_io.resolve_event_path(ev)
    assert p.endswith("cp_5_2_hot.state")


def test_resolve_event_path_spawn_uses_segment_id(state_io, tmp_path):
    """Cold-fill spawn carries its own segment_id."""
    ev = Spawn(timestamp_ms=0, level_num=5, segment_id="seg-cold-1",
               state_captured=True, is_cold_cp=True)
    p = state_io.resolve_event_path(ev)
    assert p.endswith("seg-cold-1.state")


def test_resolve_event_path_spawn_without_segment_id_returns_empty(state_io):
    """Defensive: if for some reason segment_id is unset, return '' (no path)."""
    ev = Spawn(timestamp_ms=0, level_num=5, segment_id="")
    assert state_io.resolve_event_path(ev) == ""


def test_resolve_event_path_death_returns_empty(state_io):
    """Death has no state_path field — resolver returns ''."""
    assert state_io.resolve_event_path(Death(timestamp_ms=0)) == ""


def test_resolve_event_path_level_exit_returns_empty(state_io):
    """LevelExit isn't path-tagged — that's per the Lua audit."""
    ev = LevelExit(timestamp_ms=0, level=5, goal="normal")
    assert state_io.resolve_event_path(ev) == ""
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_state_io_resolver.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# python/spinlab/retroarch/state_io.py
"""StateIO — sync save/load + path resolution against RA + SpinLab filesystem.

Replaces lua/spinlab.lua's save_state_to_file/load_state_from_file plus the
pending_saves/pending_loads/cpuExec-deferred drain pattern. The cpuExec
deferral was a Mesen-specific requirement; NCI has no such constraint.

Phase D scope: this module owns the SAVE_STATE -> mtime-poll -> move flow,
and the reverse for load. Wiring into session_manager / practice.py / the
capture pipeline is Phase F-live.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from spinlab.retroarch.events import (
    Checkpoint,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.nci import NCIClient
from spinlab.retroarch.state_paths import (
    ra_slot_filename,
    segment_state_filename,
)

DEFAULT_RESERVED_SLOT = 9999  # see Decision 6 in Phase D plan
DEFAULT_SAVE_TIMEOUT_SEC = 1.0  # mtime-advance wait; healthy RA writes in <100ms

logger = logging.getLogger(__name__)


class StateSaveTimeout(RuntimeError):
    """SAVE_STATE was issued but the slot file mtime did not advance in time."""


class StateIO:
    """Sync owner of SpinLab's segment-keyed savestate files.

    Side-effecting; not thread-safe (don't share an instance across threads).
    Async callers wrap individual methods in `asyncio.to_thread` if they need
    to call from an event loop.
    """

    def __init__(
        self,
        client: NCIClient,
        ra_savestate_dir: Path,
        spinlab_state_dir: Path,
        ra_game_basename: str,
        reserved_slot: int = DEFAULT_RESERVED_SLOT,
        save_timeout_sec: float = DEFAULT_SAVE_TIMEOUT_SEC,
    ) -> None:
        self._client = client
        self._ra_dir = Path(ra_savestate_dir)
        self._sl_dir = Path(spinlab_state_dir)
        self._game_basename = ra_game_basename
        self._reserved_slot = reserved_slot
        self._save_timeout_sec = save_timeout_sec
        # Ensure SpinLab dir exists; the RA dir is RA's responsibility.
        self._sl_dir.mkdir(parents=True, exist_ok=True)

    # -- pure path resolution --------------------------------------------------

    def state_path_for(self, segment_id: str) -> Path:
        """Where SpinLab keeps the savestate for a given segment id."""
        return self._sl_dir / segment_state_filename(segment_id)

    def has_state_for(self, segment_id: str) -> bool:
        return self.state_path_for(segment_id).exists()

    def _ra_slot_path(self) -> Path:
        return self._ra_dir / ra_slot_filename(self._game_basename, self._reserved_slot)

    # -- event-shaped resolver -------------------------------------------------

    def resolve_event_path(self, event: TransitionEvent) -> str:
        """Resolver for `PollerDeps.state_path_for`.

        Returns the absolute path string to stamp onto the event, or "" when
        no path applies (Death, LevelExit, Spawn with no segment_id).

        Naming conventions chosen to match lua/spinlab.lua's filename layout
        but flattened (segment_id-keyed where possible):
        - LevelEntrance  -> "entrance_<level>_<room>"
        - Checkpoint     -> "cp_<level>_<ordinal>_hot"
        - Spawn(cold-fill) -> "<segment_id>"
        """
        if isinstance(event, LevelEntrance):
            return str(self.state_path_for(f"entrance_{event.level}_{event.room}"))
        if isinstance(event, Checkpoint):
            return str(self.state_path_for(f"cp_{event.level_num}_{event.cp_ordinal}_hot"))
        if isinstance(event, Spawn):
            if not event.segment_id:
                return ""
            return str(self.state_path_for(event.segment_id))
        return ""

    # -- save/load (Tasks 4 & 5; stubbed below until those tasks land) --------

    def save_segment_state(self, segment_id: str) -> Path:
        """Triggered by Task 4."""
        raise NotImplementedError("implemented in Task 4")

    def load_segment_state(self, segment_id: str) -> None:
        """Triggered by Task 5."""
        raise NotImplementedError("implemented in Task 5")
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_state_io_resolver.py -v
```

Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_io.py tests/unit/retroarch/test_state_io_resolver.py
git commit -m "feat(retroarch): StateIO resolver + path/existence helpers"
```

---

## Task 4: `save_segment_state` — SAVE_STATE + mtime poll + move

The save flow: read pre-save mtime of the reserved slot file (or note absence), call `client.save_state()`, poll the mtime until it advances or timeout, then `shutil.move` the slot file into the SpinLab-keyed path. Returns the SpinLab path.

**Files:**
- Edit: `python/spinlab/retroarch/state_io.py`
- Create: `tests/unit/retroarch/test_state_io_save.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_state_io_save.py
"""save_segment_state tests — fake NCI, real tmp_path filesystem."""
import time
from pathlib import Path

import pytest

from spinlab.retroarch.state_io import (
    DEFAULT_RESERVED_SLOT,
    StateIO,
    StateSaveTimeout,
)


class _FakeNCI:
    """NCIClient stub. save_state() optionally simulates RA writing the slot file."""

    def __init__(self) -> None:
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []
        self._on_save = None  # callable invoked when save_state fires

    def save_state(self) -> None:
        self.save_state_calls += 1
        if self._on_save:
            self._on_save()

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)


@pytest.fixture
def setup(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )
    return io, nci, ra_dir, sl_dir


def test_save_segment_state_first_capture(setup):
    """No pre-existing slot file. save_state() writes one. We move it to SpinLab path."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci._on_save = lambda: slot_path.write_bytes(b"FAKE_SAVE_DATA")

    result = io.save_segment_state("seg-1")

    assert nci.save_state_calls == 1
    assert result == sl_dir / "seg-1.state"
    assert result.read_bytes() == b"FAKE_SAVE_DATA"
    assert not slot_path.exists(), "slot file should have been moved out of RA dir"


def test_save_segment_state_overwrites_previous(setup):
    """Second capture for the same segment overwrites the SpinLab file."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"OLD")

    # Pre-existing slot file at *older* mtime than our new save.
    slot_path.write_bytes(b"PREEXISTING_SLOT")
    pre_mtime = slot_path.stat().st_mtime
    # Step time forward so the new mtime is detectable.

    def on_save():
        time.sleep(0.01)
        slot_path.write_bytes(b"NEW_SAVE_DATA")

    nci._on_save = on_save

    result = io.save_segment_state("seg-1")
    assert result.read_bytes() == b"NEW_SAVE_DATA"


def test_save_segment_state_times_out_when_save_doesnt_happen(setup):
    """If no slot file appears, raise StateSaveTimeout."""
    io, nci, ra_dir, sl_dir = setup
    nci._on_save = None  # save_state() doesn't write a file.

    with pytest.raises(StateSaveTimeout):
        io.save_segment_state("seg-2")

    assert nci.save_state_calls == 1


def test_save_segment_state_times_out_when_existing_file_unchanged(setup):
    """Pre-existing slot file with no mtime advance -> timeout."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    slot_path.write_bytes(b"STALE")
    nci._on_save = None  # don't touch the file at all

    with pytest.raises(StateSaveTimeout):
        io.save_segment_state("seg-3")


def test_save_segment_state_creates_spinlab_dir_if_missing(tmp_path):
    """Constructor creates spinlab_state_dir; verify by passing one that doesn't exist."""
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "deep" / "nested" / "states"  # doesn't exist
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )
    assert sl_dir.exists()

    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    nci._on_save = lambda: slot_path.write_bytes(b"D")
    result = io.save_segment_state("a")
    assert result.exists()
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_state_io_save.py -v
```

Expected: FAIL — `save_segment_state` is `NotImplementedError`.

- [ ] **Step 3: Implement**

Replace the `save_segment_state` stub in `state_io.py` with:

```python
def save_segment_state(self, segment_id: str) -> Path:
    """Trigger SAVE_STATE, wait for the slot file to appear/advance, move it.

    Returns the SpinLab path the file now lives at. Raises StateSaveTimeout
    if the slot file's mtime does not advance (or it does not appear) within
    `save_timeout_sec`.
    """
    slot_path = self._ra_slot_path()
    pre_mtime = slot_path.stat().st_mtime if slot_path.exists() else None

    self._client.save_state()

    deadline = time.monotonic() + self._save_timeout_sec
    poll_interval = 0.01  # 10ms — finer-grained than RA's typical save time
    while time.monotonic() < deadline:
        if slot_path.exists():
            cur_mtime = slot_path.stat().st_mtime
            if pre_mtime is None or cur_mtime > pre_mtime:
                break
        time.sleep(poll_interval)
    else:
        raise StateSaveTimeout(
            f"SAVE_STATE for segment {segment_id!r}: slot file "
            f"{slot_path} did not advance within {self._save_timeout_sec}s"
        )

    target = self.state_path_for(segment_id)
    # shutil.move handles cross-device moves; both dirs are typically on the
    # same filesystem (user data dir) so this is usually a rename.
    shutil.move(str(slot_path), str(target))
    logger.debug("StateIO: saved segment %s -> %s", segment_id, target)
    return target
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_state_io_save.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_io.py tests/unit/retroarch/test_state_io_save.py
git commit -m "feat(retroarch): StateIO.save_segment_state — NCI + mtime poll + move"
```

---

## Task 5: `load_segment_state` — copy + LOAD_STATE_SLOT

The load flow: copy the SpinLab-keyed file into RA's reserved slot path, then call `client.load_state_slot(reserved_slot)`. Raises `FileNotFoundError` if the SpinLab file doesn't exist (caller is expected to gate via `has_state_for` if it wants graceful handling).

**Files:**
- Edit: `python/spinlab/retroarch/state_io.py`
- Create: `tests/unit/retroarch/test_state_io_load.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/retroarch/test_state_io_load.py
"""load_segment_state tests."""
import pytest

from spinlab.retroarch.state_io import DEFAULT_RESERVED_SLOT, StateIO


class _FakeNCI:
    def __init__(self) -> None:
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []

    def save_state(self) -> None:
        self.save_state_calls += 1

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)


@pytest.fixture
def setup(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
    )
    return io, nci, ra_dir, sl_dir


def test_load_copies_file_into_slot_then_calls_nci(setup):
    """SpinLab file exists -> copy to slot path, fire LOAD_STATE_SLOT 9999."""
    io, nci, ra_dir, sl_dir = setup
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"STATEDATA")

    io.load_segment_state("seg-1")

    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    assert slot_path.read_bytes() == b"STATEDATA"
    # SpinLab file remains (copy, not move): we may load it again.
    assert sp_path.exists()
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]


def test_load_overwrites_existing_slot_file(setup):
    """Slot file already exists from a previous SpinLab load -> overwrite."""
    io, nci, ra_dir, sl_dir = setup
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"
    slot_path.write_bytes(b"OLD")
    sp_path = sl_dir / "seg-1.state"
    sp_path.write_bytes(b"NEW")

    io.load_segment_state("seg-1")

    assert slot_path.read_bytes() == b"NEW"


def test_load_missing_segment_state_raises(setup):
    """No SpinLab file for this segment -> FileNotFoundError, no NCI call."""
    io, nci, ra_dir, sl_dir = setup

    with pytest.raises(FileNotFoundError, match="seg-missing"):
        io.load_segment_state("seg-missing")

    assert nci.load_state_slot_calls == []


def test_load_uses_custom_reserved_slot(tmp_path):
    """If reserved_slot=42, file goes to <game>.state42 and load_state_slot(42)."""
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    nci = _FakeNCI()
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="G",
        reserved_slot=42,
    )
    (sl_dir / "x.state").write_bytes(b"D")

    io.load_segment_state("x")

    assert (ra_dir / "G.state42").read_bytes() == b"D"
    assert nci.load_state_slot_calls == [42]
```

- [ ] **Step 2: Run tests, expect failure**

```
python -m pytest tests/unit/retroarch/test_state_io_load.py -v
```

Expected: FAIL — `load_segment_state` is `NotImplementedError`.

- [ ] **Step 3: Implement**

Replace the `load_segment_state` stub:

```python
def load_segment_state(self, segment_id: str) -> None:
    """Copy SpinLab's segment file into RA's reserved slot, fire LOAD_STATE_SLOT.

    Raises FileNotFoundError if no SpinLab file exists for this segment.
    Caller can gate via has_state_for().
    """
    sp_path = self.state_path_for(segment_id)
    if not sp_path.exists():
        raise FileNotFoundError(
            f"No SpinLab savestate for segment {segment_id!r} at {sp_path}"
        )
    slot_path = self._ra_slot_path()
    # Ensure RA dir exists (it normally does, but defensive).
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(sp_path), str(slot_path))
    self._client.load_state_slot(self._reserved_slot)
    logger.debug(
        "StateIO: loaded segment %s (slot=%d)", segment_id, self._reserved_slot
    )
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/unit/retroarch/test_state_io_load.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_io.py tests/unit/retroarch/test_state_io_load.py
git commit -m "feat(retroarch): StateIO.load_segment_state — copy + LOAD_STATE_SLOT"
```

---

## Task 6: Roundtrip integration test (still fakes, no live RA)

Verify the save→load cycle with both halves working together. Tests that the SpinLab-keyed file produced by `save_segment_state` is what `load_segment_state` puts back into RA's slot path. Single test class, single happy path — the per-method tests already cover failure modes.

**Files:**
- Create: `tests/unit/retroarch/test_state_io_roundtrip.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/retroarch/test_state_io_roundtrip.py
"""save -> load roundtrip with the same fake NCI client."""
from spinlab.retroarch.state_io import DEFAULT_RESERVED_SLOT, StateIO


class _FakeNCI:
    def __init__(self) -> None:
        self._next_save_payload: bytes = b""
        self.save_state_calls = 0
        self.load_state_slot_calls: list[int] = []
        self._slot_path = None

    def bind(self, slot_path) -> None:
        self._slot_path = slot_path

    def stage_save_payload(self, payload: bytes) -> None:
        self._next_save_payload = payload

    def save_state(self) -> None:
        self.save_state_calls += 1
        self._slot_path.write_bytes(self._next_save_payload)

    def load_state_slot(self, slot: int) -> None:
        self.load_state_slot_calls.append(slot)


def test_save_then_load_roundtrip(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci = _FakeNCI()
    nci.bind(slot_path)
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )

    nci.stage_save_payload(b"PAYLOAD_AT_T=0")
    sp_path = io.save_segment_state("seg-A")
    assert sp_path.read_bytes() == b"PAYLOAD_AT_T=0"
    # Slot file moved out of RA dir.
    assert not slot_path.exists()

    # Loading puts the bytes back into the slot path.
    io.load_segment_state("seg-A")
    assert slot_path.read_bytes() == b"PAYLOAD_AT_T=0"
    assert nci.load_state_slot_calls == [DEFAULT_RESERVED_SLOT]
    # SpinLab file persists for re-load.
    assert sp_path.exists()


def test_save_two_segments_then_load_each(tmp_path):
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir = tmp_path / "sl"
    sl_dir.mkdir()
    slot_path = ra_dir / f"Game.state{DEFAULT_RESERVED_SLOT}"

    nci = _FakeNCI()
    nci.bind(slot_path)
    io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="Game",
        save_timeout_sec=0.5,
    )

    nci.stage_save_payload(b"DATA-A")
    io.save_segment_state("seg-A")
    nci.stage_save_payload(b"DATA-B")
    io.save_segment_state("seg-B")

    io.load_segment_state("seg-A")
    assert slot_path.read_bytes() == b"DATA-A"
    io.load_segment_state("seg-B")
    assert slot_path.read_bytes() == b"DATA-B"
```

- [ ] **Step 2: Run test, expect green (built on Tasks 4 + 5)**

```
python -m pytest tests/unit/retroarch/test_state_io_roundtrip.py -v
```

Expected: 2 PASS without further code changes.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/retroarch/test_state_io_roundtrip.py
git commit -m "test(retroarch): StateIO save/load roundtrip with fake NCI"
```

---

## Task 7: `state_path` resolver attached to a poller end-to-end

Wire `StateIO.resolve_event_path` into a real `Poller` and verify a complete event flow stamps state paths correctly. Still entirely fakes (no live RA), but cross-module — proves Phase D's resolver actually plugs into Phase C's poller and that the events arrive downstream with paths.

**Files:**
- Create: `tests/unit/retroarch/test_state_io_with_poller.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/retroarch/test_state_io_with_poller.py
"""StateIO.resolve_event_path wired into a real Poller via PollerDeps."""
import asyncio
from typing import Iterator

import pytest

from spinlab.retroarch.events import (
    Checkpoint,
    LevelEntrance,
    Spawn,
    TransitionEvent,
)
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot
from spinlab.retroarch.state_io import StateIO


class _FakeNCI:
    def save_state(self) -> None: ...
    def load_state_slot(self, slot: int) -> None: ...


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    def fn(_client) -> MemorySnapshot:
        return next(seq)
    return fn


@pytest.mark.asyncio
async def test_poller_uses_state_io_resolver_for_level_entrance(tmp_path):
    sl_dir = tmp_path / "sl"
    ra_dir = tmp_path / "ra"
    ra_dir.mkdir()
    sl_dir.mkdir()

    nci = _FakeNCI()
    state_io = StateIO(
        client=nci,
        ra_savestate_dir=ra_dir,
        spinlab_state_dir=sl_dir,
        ra_game_basename="G",
    )

    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),  # entrance
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=nci,
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=state_io.resolve_event_path,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntrance)]
    assert len(entrances) == 1
    # The path is the SpinLab-keyed file for entrance_5_0.
    assert entrances[0].state_path.endswith("entrance_5_0.state")
```

- [ ] **Step 2: Run test, expect green (Tasks 2, 3 are sufficient)**

```
python -m pytest tests/unit/retroarch/test_state_io_with_poller.py -v
```

Expected: 1 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/retroarch/test_state_io_with_poller.py
git commit -m "test(retroarch): StateIO resolver wired through real Poller"
```

---

## Task 8: Full fast-suite sanity + documentation pass

Confirm the new tests don't regress anything else, and lock in the module-level docstring with the design decisions inline. Cheap insurance.

**Files:**
- Edit: `python/spinlab/retroarch/state_io.py` (docstring polish)

- [ ] **Step 1: Run full fast suite**

```
python -m pytest -m "not (emulator or slow or frontend)" -q | tail -5
```

Expected: all green. About 18–22 new tests added across Phase D tasks.

- [ ] **Step 2: Run pyright on the new module**

```
npx pyright python/spinlab/retroarch/state_io.py python/spinlab/retroarch/state_paths.py python/spinlab/retroarch/poller.py python/spinlab/retroarch/events.py python/spinlab/retroarch/cold_fill.py
```

Expected: no new errors. Existing tracked errors are out of scope.

- [ ] **Step 3: Run ruff**

```
ruff check python/spinlab/retroarch/state_io.py python/spinlab/retroarch/state_paths.py
```

Expected: clean.

- [ ] **Step 4: Polish module docstring**

Confirm `state_io.py` opens with a docstring that:
- States it replaces `lua/spinlab.lua`'s save/load logic.
- Mentions Decision 1 (filesystem shuffle, reserved slot 9999).
- Mentions Decision 5 (mtime polling for SAVE_STATE confirmation).
- Notes "Phase D scope; F-live wires this into session_manager."

The docstring written in Task 3 already covers these; touch up wording if needed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/state_io.py
git commit -m "docs(retroarch): polish StateIO module docstring"
```

---

## Phase D exit criteria

- `python/spinlab/retroarch/state_io.py` exposes `StateIO` with `save_segment_state`, `load_segment_state`, `state_path_for`, `has_state_for`, `resolve_event_path`.
- `python/spinlab/retroarch/state_paths.py` provides pure path helpers, fully unit-tested.
- `Poller` (Phase C) now stamps `state_path` on events via an optional `PollerDeps.state_path_for` resolver.
- `Spawn` event carries `segment_id`; `ColdFillTracker` populates it.
- All new tests run against `tmp_path` + a fake NCI stub. No live RetroArch dependency.
- `lua/spinlab.lua` is unchanged. The Lua TCP path keeps working through Phase F-live.
- Full fast suite green (`pytest -m "not (emulator or slow or frontend)"`).
- pyright + ruff clean on the new files.

## What's deliberately not in Phase D

- **Wiring into `practice.py`, `session_manager.py`, the capture pipeline.** Those modules currently consume `state_path` strings from the Lua TCP protocol; replacing the source is Phase F-live's job. `StateIO.save_segment_state` is callable but no production code calls it yet.
- **`emu.reset()` replacement.** Already covered by Phase B's `client.reset()`. Not part of state I/O.
- **BSV record/playback.** Phase E.
- **Dashboard "Invalidate" button** (the L+Select replacement). Not state I/O.
- **Live RA integration test.** Possible but expensive; defer to Phase F-live, where wire-up to session_manager naturally exercises this.
- **`emu.setSpeed()` for replay speed control.** Phase E concern.
- **Removing `lua/spinlab.lua`.** Phase G.

## Phase D plan self-review

- **File structure:** 2 new implementation files (`state_io.py`, `state_paths.py`), small amendments to 3 existing files (`events.py`, `poller.py`, `cold_fill.py`). 6 new test files. Each file has one clear responsibility.
- **Coverage:** every Lua function from the audit's "Phase D drivers" row has a port. The cpuExec-deferred drain is intentionally dropped (NCI has no such constraint, per audit Tricky Pattern #3).
- **Open question #1 (slot management) is closed** — Decision 1 commits to Option C with rationale documented.
- **Phase C followups #1 + #2 close** in Task 2 (Spawn.segment_id and the resolver hook).
- **Phase C followup #5** (no integration test for cold-fill activation through poller) is partially addressed by Task 7's poller-resolver test; full live-RA exercise still defers to Phase F-live.
- **No placeholders.** Every method has a body or a `NotImplementedError` that's filled in by a later task in this same plan.
- **Type consistency:** `Path` for filesystem objects, `str` for the event-stamped `state_path` field (matches how downstream consumers — `recorder.py`, `practice.py`, `speed_run.py` — already handle it).
- **Failure mode discipline:** `StateSaveTimeout` for save-side timeouts (named exception, not generic `RuntimeError`); `FileNotFoundError` for load-side missing files (stdlib exception, expected by callers using `has_state_for` to gate).
- **Reserved slot 9999** is the single magic number, but it is configurable via constructor kwarg, named (`DEFAULT_RESERVED_SLOT`), and rationale is documented in Decision 6 — satisfies the project's "no magic numbers" guideline.
- **mtime polling** has a defined timeout (`save_timeout_sec=1.0`) and explicit failure mode rather than a fragile `time.sleep`.

## Next phase after D

Phase F-live — the integration step. Wire `StateIO` into `practice.py`'s segment-load path, `recorder.py`'s segment-save path, the cold-fill flow in `capture/cold_fill.py`. Replace the Lua TCP protocol's `practice_load`/`fill_gap_load`/`cold_fill_load` commands with direct Python calls. End state: dashboard practice loop runs against live RetroArch with runahead enabled.

After F-live and the live-practice smoke pass, Phase E (BSV) and Phase G (Lua/Mesen removal) follow.

---

### Critical Files for Implementation

- c:\Users\thedo\git\spinlab\.worktrees\retroarch-port\python\spinlab\retroarch\state_io.py
- c:\Users\thedo\git\spinlab\.worktrees\retroarch-port\python\spinlab\retroarch\state_paths.py
- c:\Users\thedo\git\spinlab\.worktrees\retroarch-port\python\spinlab\retroarch\poller.py
- c:\Users\thedo\git\spinlab\.worktrees\retroarch-port\python\spinlab\retroarch\events.py
- c:\Users\thedo\git\spinlab\.worktrees\retroarch-port\python\spinlab\retroarch\cold_fill.py