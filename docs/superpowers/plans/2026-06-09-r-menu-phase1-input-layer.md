# R-menu Phase 1 — Input-Layer Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read SMW's second controller byte (`$15`, the B/Y/Select/Start + d-pad held byte) into the snapshot and generalize `ControllerMenuDetector`'s command registry from "bit → command" to "(byte, bit) → command," so future verbs can live on Y and the d-pad — without adding any new command yet.

**Architecture:** `$15` is read as part of a single 3-byte `$15`–`$17` NCI cluster (it sits 2 bytes below the already-read `$17`), so no extra round-trip and the same read-first write-barrier applies. `ControllerMenuDetector` becomes registry-driven: a `MODIFIER` and a `COMMANDS` map keyed by `(snapshot-field, bit)`, with a set-based rising-edge debounce that naturally spans both bytes. Both are constructor-injectable so tests can exercise a `$15` command before any real one ships. No poller / SessionManager changes — the detector still consumes the snapshot it's handed.

**Tech Stack:** Python 3.11 (frozen dataclass snapshot, pure detector), pytest (unit; `fake_nci_server` fixture for the mocked snapshot read).

**Spec:** `docs/superpowers/specs/2026-06-09-r-menu-vocabulary-expansion-design.md` (Phase 1).

## Decisions locked before writing

1. **`$15`+`$17` are one cluster.** `read_ram(0x15, 3)` returns `$15` (held1), `$16` (pressed1, ignored), `$17` (held2). This replaces the current lone `$17` read — read count stays at 7, and the cluster is still read FIRST (the write-barrier that fixed the `$17` input-poll race covers `$15` too, since they're the same read).
2. **`controller_held_1` is a trailing, default-`0` field** — same rationale as `controller_held`: transition detection never consults it, so the ~10 test `MemorySnapshot(...)` builders legitimately omit it.
3. **No new command in Phase 1.** `COMMANDS` stays `{pause}` (on `$17`). The `$15` path is exercised by unit tests that *inject* a `$15` registry into the detector. The first real `$15` command (toggle-practice on Y) and its on-real-RA confirmation land in Phase 2 — that's the real-RA `$15` read+dispatch check. `$15` is adjacent to the already-confirmed `$17` and shares the same cluster read, so deferring the live check one phase is low-risk. (Deliberate departure from the spec's "Phase 1 emulator scenario," noted.)
4. **Set-based debounce.** The old single-int `_prev_command_bits` can't span two bytes cleanly; track a `set` of pressed command-keys instead. Dispatch iterates `sorted(...)` for deterministic event order when multiple fire on one frame.
5. **Detector takes `modifier` + `commands` as constructor kwargs** (defaulting to the module constants). Pure DI for testability; the production poller still calls `ControllerMenuDetector()` with defaults.

## File map

| File | Change |
|---|---|
| `python/spinlab/retroarch/addresses.py` | + `ADDR_CONTROLLER_HELD_1 = 0x15` |
| `python/spinlab/retroarch/snapshot.py` | + `controller_held_1` field; cluster-read `$15`–`$17` |
| `tests/unit/retroarch/test_snapshot.py` | assert `controller_held_1` reads `$15` |
| `python/spinlab/retroarch/menu_detector.py` | (byte,bit) registry + set-based debounce + injectable modifier/commands |
| `tests/unit/retroarch/test_menu_detector.py` | update `_snap` for two bytes; add injected-`$15`-registry tests |

---

### Task 1: Read `$15` into the snapshot

**Files:**
- Modify: `python/spinlab/retroarch/addresses.py`
- Modify: `python/spinlab/retroarch/snapshot.py`
- Test: `tests/unit/retroarch/test_snapshot.py`

- [ ] **Step 1: Add the address constant**

In `python/spinlab/retroarch/addresses.py`, immediately **above** the existing `ADDR_CONTROLLER_HELD = 0x17` line, add:

```python
# Controller 1 held buttons, byte 1 (B Y Select Start Up Down Left Right).
# kaizosplits buttonsHeld1. Read alongside $17 for the R-menu command layer:
# Y (0x40) and the d-pad (Left 0x02 / Right 0x01) live here, not in $17.
ADDR_CONTROLLER_HELD_1 = 0x15
```

(Leave `ADDR_CONTROLLER_HELD = 0x17` as-is, directly below.)

- [ ] **Step 2: Extend the snapshot test (red)**

In `tests/unit/retroarch/test_snapshot.py`, add a sentinel for `$15` to `addr_to_value` (after the `0x0017` line):

```python
        0x0015: 0xDD,  # controller_held_1
```

Change the controller cluster entry in `clusters` from `(0x0017, 1)` to the 3-byte `$15`–`$17` range:

```python
        (0x0015, 3),                    # controller_held_1, (pressed1 $16 ignored), controller_held
```

Add an assertion at the end of the test:

```python
    assert snap.controller_held_1 == 0xDD
```

- [ ] **Step 3: Run it — verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_snapshot.py -q`
Expected: FAIL — `MemorySnapshot` has no `controller_held_1` (TypeError/AttributeError).

- [ ] **Step 4: Add the field + cluster read**

In `python/spinlab/retroarch/snapshot.py`, add the field at the END of the `MemorySnapshot` dataclass (after `controller_held`):

```python
    controller_held: int = 0
    # Controller 1 held buttons, byte 1 ($15: B Y Select Start + d-pad). Read
    # for the R-menu layer (Y / d-pad commands live here). Defaulted for the
    # same reason as controller_held — transition detection never consults it.
    controller_held_1: int = 0
```

In `read_snapshot`, replace the lone `$17` read:

```python
    controller_held = client.read_ram(a.ADDR_CONTROLLER_HELD, 1)[0]
```

with the 3-byte `$15`–`$17` cluster (still the first read, preserving the write-barrier):

```python
    # $15..$17: held byte1 ($15, B Y Select Start + d-pad), pressed1 ($16,
    # ignored), held byte2 ($17, A X L R). Read FIRST as the write-barrier that
    # collapses the FRAMEADVANCE/NMI input-poll race (see the long note below) —
    # one cluster covers both held bytes the menu layer needs.
    c_ctrl = client.read_ram(
        a.ADDR_CONTROLLER_HELD_1,
        a.ADDR_CONTROLLER_HELD - a.ADDR_CONTROLLER_HELD_1 + 1,
    )
    controller_held_1 = c_ctrl[0]
    controller_held = c_ctrl[a.ADDR_CONTROLLER_HELD - a.ADDR_CONTROLLER_HELD_1]
```

Move the existing "read FIRST so it acts as the synchronous barrier…" comment block to sit above this cluster read (it still applies — keep its wording, it explains the race). Update the docstring: "Read all 12 … into 7 …" → "Read all 13 SMW state bytes via NCI … into 7 …" and adjust the `$17`-lone-read note to read: "Note: `$15`–`$17` are read as one 3-byte cluster (the `$16` pressed byte is read but unused); it's a lone cluster far below the `$0071+` low cluster."

Add `controller_held_1=controller_held_1,` to the `MemorySnapshot(...)` return (right after `controller_held=controller_held,`).

- [ ] **Step 5: Run it — verify it passes**

Run: `python -m pytest tests/unit/retroarch/test_snapshot.py -q`
Expected: PASS.

- [ ] **Step 6: Confirm no snapshot consumers broke**

Run: `python -m pytest tests/unit/retroarch/ -q`
Expected: PASS (existing `_snap` builders omit `controller_held_1` → default 0).

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/retroarch/addresses.py python/spinlab/retroarch/snapshot.py tests/unit/retroarch/test_snapshot.py
git commit -m "feat(snapshot): read controller-1 held byte 1 (\$15) for the R-menu"
```

---

### Task 2: Generalize ControllerMenuDetector to a (byte, bit) registry

**Files:**
- Modify: `python/spinlab/retroarch/menu_detector.py`
- Test: `tests/unit/retroarch/test_menu_detector.py`

- [ ] **Step 1: Update the test file's snapshot builder + add two-byte tests (red)**

In `tests/unit/retroarch/test_menu_detector.py`, replace the `_snap` helper so it can set both held bytes, and update the imports to pull the byte-field constants:

```python
from spinlab.retroarch.menu_detector import (
    BUTTON_R,
    BUTTON_X,
    HELD1,
    HELD2,
    ControllerMenuDetector,
)


def _snap(controller_held: int = 0, controller_held_1: int = 0) -> MemorySnapshot:
    return MemorySnapshot(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0,
        cp_entrance=0, controller_held=controller_held,
        controller_held_1=controller_held_1,
    )
```

All existing tests keep passing as-is (they only set `controller_held`). Append two new tests that exercise the `$15` path via an **injected** registry — a fake command on the Y bit (`0x40` in `$15`):

```python
# A registry with one command on the $15 (HELD1) byte, to prove the (byte,bit)
# mechanism reads the second byte. Mirrors how Phase 2 will register Y commands.
_HELD1_REGISTRY = {(HELD1, 0x40): "toggle_test"}


def test_command_on_held1_byte_dispatches():
    """A command bound to a $15 bit fires when that bit is pressed after R."""
    d = ControllerMenuDetector(commands=_HELD1_REGISTRY)
    events = _run(d, [
        _snap(controller_held=BUTTON_R),                       # R down -> menu open
        _snap(controller_held=BUTTON_R, controller_held_1=0x40),  # Y pressed after
    ])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "toggle_test"


def test_held1_command_already_held_at_open_is_seeded():
    """Y already held when R goes down does NOT fire (seed), same as X."""
    d = ControllerMenuDetector(commands=_HELD1_REGISTRY)
    events = _run(d, [
        _snap(controller_held_1=0x40),                          # Y held, no R
        _snap(controller_held=BUTTON_R, controller_held_1=0x40),  # R down, Y already held
    ])
    assert _cmds(events) == []


def test_pause_still_dispatches_with_default_registry():
    """The default registry (pause on $17) is unchanged by the generalization."""
    d = ControllerMenuDetector()
    events = _run(d, [_snap(controller_held=BUTTON_R),
                      _snap(controller_held=BUTTON_R | BUTTON_X)])
    cmds = _cmds(events)
    assert len(cmds) == 1 and cmds[0].command == "pause"
```

(The `_run`, `_cmds`, `_armed` helpers already exist in the file from the modifier rewrite.)

- [ ] **Step 2: Run them — verify the new ones fail**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: FAIL on import (`HELD1`/`HELD2` don't exist) and on the injected-`commands` kwarg.

- [ ] **Step 3: Rewrite the detector as a (byte, bit) registry**

Replace the whole body of `python/spinlab/retroarch/menu_detector.py` with:

```python
"""ControllerMenuDetector — the R-menu command layer.

A poller-level state machine over the per-frame held-button bytes. R
(controller-1 $17, bit 0x10) is a held MODIFIER: while R is down the command
menu is open, and pressing a command button dispatches the mapped command.
Releasing R closes it.

R is a pure modifier with no hold-time threshold — the menu opens the instant R
is held. The only subtlety is "press the command *after* R": a command button
already held when R goes down does NOT fire (it's seeded as already-seen). This
keeps the gesture precise and prevents an accidental command when a gameplay
button is held and R is tapped — you must press the command fresh while R is
held.

Buttons span two WRAM held bytes (kaizosplits naming): $17 (HELD2: A X L R) and
$15 (HELD1: B Y Select Start + d-pad). Each button — modifier or command — is
addressed by (snapshot-field, bit), so the registry can mix both bytes. The
modifier and the COMMANDS registry are constructor-injectable for testing; the
production poller uses the module defaults.

Single responsibility: it knows nothing about practice/pause — it only turns
controller input into ControllerMenuArmedEvent / ControllerCommandEvent, which
SessionManager routes.
"""
from __future__ import annotations

from spinlab.protocol import ControllerCommandEvent, ControllerMenuArmedEvent
from spinlab.retroarch.snapshot import MemorySnapshot

# Which MemorySnapshot field a button's byte lives in.
HELD2 = "controller_held"    # $17: A X L R - - - -
HELD1 = "controller_held_1"  # $15: B Y Select Start Up Down Left Right

# Button bits within their byte.
BUTTON_R = 0x10  # $17 — held modifier
BUTTON_X = 0x40  # $17 — pause

# A button is a (snapshot-field, bit) pair.
ButtonKey = tuple[str, int]

# The modifier that opens the menu.
MODIFIER: ButtonKey = (HELD2, BUTTON_R)

# (snapshot-field, bit) -> command name. Spans both held bytes; extend by adding
# a key. X = pause is the only command today.
COMMANDS: dict[ButtonKey, str] = {
    (HELD2, BUTTON_X): "pause",
}

_MenuEvent = ControllerCommandEvent | ControllerMenuArmedEvent


def _down(snap: MemorySnapshot, key: ButtonKey) -> bool:
    field, bit = key
    return bool(getattr(snap, field) & bit)


class ControllerMenuDetector:
    """Per-frame R-menu emitter. Stateful but pure (no IO)."""

    def __init__(
        self,
        *,
        modifier: ButtonKey = MODIFIER,
        commands: dict[ButtonKey, str] = COMMANDS,
    ) -> None:
        self._modifier = modifier
        self._commands = commands
        self._menu_open = False
        # Command keys held last frame (only meaningful while open) — used to
        # fire on the rising edge instead of every frame.
        self._prev_pressed: set[ButtonKey] = set()

    def reset(self) -> None:
        self._menu_open = False
        self._prev_pressed = set()

    def step(self, snap: MemorySnapshot) -> list[_MenuEvent]:
        events: list[_MenuEvent] = []
        r_down = _down(snap, self._modifier)
        pressed_now = {k for k in self._commands if _down(snap, k)}

        if r_down and not self._menu_open:
            # R just went down — open the menu. Seed with the commands ALREADY
            # held so they don't count as a press; only a fresh press fires.
            self._menu_open = True
            self._prev_pressed = pressed_now
            events.append(ControllerMenuArmedEvent(armed=True))
        elif not r_down and self._menu_open:
            self._menu_open = False
            self._prev_pressed = set()
            events.append(ControllerMenuArmedEvent(armed=False))

        # DISPATCH: while open, each command fires on its rising edge. Sorted for
        # deterministic event order if several rise on the same frame.
        if self._menu_open:
            for key in sorted(pressed_now - self._prev_pressed):
                events.append(ControllerCommandEvent(command=self._commands[key]))
            self._prev_pressed = pressed_now

        return events
```

- [ ] **Step 4: Run them — verify they pass**

Run: `python -m pytest tests/unit/retroarch/test_menu_detector.py -q`
Expected: PASS (all existing modifier tests + the 3 new ones).

- [ ] **Step 5: Confirm the poller still drives it**

The poller calls `self._menu.step(snap)` with no kwargs — unchanged. Confirm:

Run: `python -m pytest tests/unit/retroarch/test_poller.py -q`
Expected: PASS.

- [ ] **Step 6: Statics**

Run: `ruff check python/spinlab/retroarch/menu_detector.py` and `npx pyright python/spinlab/retroarch/menu_detector.py`
Expected: clean (0 errors).

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/retroarch/menu_detector.py tests/unit/retroarch/test_menu_detector.py
git commit -m "feat(menu): (byte,bit) command registry spanning \$15 + \$17"
```

---

### Task 3: Verification gate

- [ ] **Step 1: Fast suite + statics**

```bash
python -m pytest -m "not emulator" -q
npx pyright python/spinlab/retroarch/menu_detector.py python/spinlab/retroarch/snapshot.py
ruff check python/spinlab/retroarch/
```
Expected: all green; no new pyright/ruff errors in the touched files.

- [ ] **Step 2: Full suite (incl. emulator)**

```bash
python -m pytest
```
Expected: all pass, **zero skips**. Phase 1 adds no emulator test (per decision 3); the existing emulator menu test (`test_r_menu_pause_command`) must still pass — it confirms the generalized detector + the `$15`-inclusive cluster read didn't regress `$17`/pause on real RA. A `SKIPPED` emulator block is a failure — surface it.

- [ ] **Step 3: Note the Phase 2 handoff**

The real-RA confirmation that `$15` reads + dispatches arrives in Phase 2 (toggle-practice on Y is a `$15` command with its own emulator poke scenario). Phase 1 is unit-complete; do not add a `$15` emulator test here.

---

## Self-review notes (checked against the spec, Phase 1 section)

- **Read `$15` into the snapshot** (`ADDR_CONTROLLER_HELD_1`, `controller_held_1`, trailing default) → Task 1. Read as one `$15`–`$17` cluster, first, preserving the race barrier (decision 1).
- **Generalize `COMMANDS` to `(byte, bit) → name`; modifier stays R = (`$17`, `0x10`); seed unchanged** → Task 2. Set-based debounce replaces the single-int mask to span two bytes (decision 4). Injectable for tests (decision 5).
- **`$18`/`$16` "pressed" twins stay unread** — the `$16` byte is read in the cluster but unused; we still edge-detect the held bytes.
- **Spec's "Phase 1 emulator scenario"** — deferred to Phase 2's toggle-practice live test (decision 3); explicitly flagged in Task 3 Step 3. The full emulator suite still runs as the gate.
- No poller / SessionManager / integration-ADDR_MAP changes — those arrive with the first `$15` command (Phase 2). YAGNI.
