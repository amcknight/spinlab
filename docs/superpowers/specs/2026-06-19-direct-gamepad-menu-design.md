---
title: Direct-gamepad menu (replace the WRAM R-menu)
date: 2026-06-19
status: approved
---

# Direct-gamepad menu

## Problem / why

The controller menu (pause / science / toggle-practice / prev-next-segment) is
currently driven by reading SMW WRAM held-button bytes (`$15`/`$17`) over NCI and
decoding SNES button bits — see `ControllerMenuDetector`. Two problems surfaced in
live testing (2026-06-19, logs + a controller-byte probe at `C:/tmp/read_controller.py`):

1. **Face-button merge.** On Andrew's 8bitdo, the four SNES face buttons collapse
   to two inputs at the SMW level: **X ≡ Y** (both set bit `0x40` on *both* `$15`
   and `$17`) and **A ≡ B** (both set `0x80` on both bytes). So `R+X` and `R+Y` are
   literally indistinguishable — `R+X` fires both `pause` and `toggle_practice`,
   and start/stop wins. R, L, Select, Start, and the d-pad read cleanly, proving
   the byte decoding is correct; it's specifically the face buttons that double.
   This is the controller→core reporting, not a RetroArch binding (bindings are
   correct) and not SpinLab's reading.
2. **Modifier collides with gameplay.** `R` is held during play to scroll the
   screen, so the menu is armed constantly and in-game presses leak as menu
   commands — the root of the "R+X and R+Y feel the same" report and the
   accidental session-stops (the death-to-Idle, prior "Bug C").

The WRAM approach is also limited to the SNES button set, so it can't use the
controller's extra buttons (L2, R2, stick-clicks, etc.).

## Decision

Read the **physical gamepad directly** (not the SMW-emulated input) and retire the
WRAM menu. The menu becomes emulator-agnostic and gains the full physical button
set, and a modifier on an **SMW-unused button** (e.g. L2/R2) eliminates both the
face-button merge and the gameplay collision in one move.

### Settled choices
- **Replace** the WRAM (`$15`/`$17`) menu input entirely; gamepad is the sole menu
  input. No gamepad connected → the menu is simply inactive (use the UI buttons).
- **Modifier-based** gesture (hold a modifier, press a verb), preferring SMW-unused
  buttons for the modifier and verbs so menu presses mostly don't leak into play.
- **Configurable** mapping (firm requirement), **one global mapping** in
  `config.yaml`. Per-game and **per-level overrides are deferred** — design the
  config so it's not painted into a corner, but do not build them now.
- **Library: `pygame`** (its `joystick` module). Mature, cross-platform, handles
  8bitdo in X-input and D-input modes, simple polling (`get_button(i)`), hot-plug
  aware, runs windowless (dummy SDL video driver on headless). Rejected: raw XInput
  via ctypes (Windows-only, X-input-mode only); `sdl2`/`inputs` (finicky/less
  maintained).
- **Verbs unchanged**: `pause`, `toggle_science`, `toggle_practice`,
  `prev_segment`, `next_segment`. With a free modifier the old collisions are gone,
  so `toggle_practice` (start/stop) is safe to keep on the controller.

## Architecture

The existing `ControllerMenuDetector` state machine is input-source-agnostic — only
its *input* was WRAM bits. Generalize it from `(byte, bit)` keys to opaque
**button IDs**, and feed it from a gamepad source. Everything downstream — the
`ControllerCommandEvent` / `ControllerMenuArmedEvent` types and the entire
`SessionManager` command dispatch (pause/science/toggle_practice/prev/next) — is
**reused untouched**.

### Components
1. **`ButtonSource` protocol** — `pressed() -> set[ButtonId]` (the set of buttons
   currently held this poll). Lets tests inject a fake source; the detector never
   knows whether input came from a real pad.
2. **`GamepadButtonSource`** (pygame-backed `ButtonSource`) — opens the configured
   joystick, pumps SDL events, returns the held-button set each poll. Handles "no
   pad connected" gracefully (empty set; log once on first miss and on reconnect).
   Button IDs are pygame button indices.
3. **Refactored `ControllerMenuDetector`** — same state machine (modifier-held →
   `MenuArmed(True)`; command fires on rising edge; seed currently-held buttons on
   arm so a held button doesn't fire; release → `MenuArmed(False)`), now keyed on
   button IDs with `modifier: ButtonId` and `commands: dict[ButtonId, verb]` from
   config. The WRAM-specific bit constants (`BUTTON_X`, `HELD1/HELD2`, etc.) are
   removed.
4. **Gamepad poll loop** — a daemon thread polling `GamepadButtonSource` at ~60Hz,
   stepping the detector, and forwarding emitted events into the asyncio loop via
   `loop.call_soon_threadsafe(...)` → `SessionManager.route_event`. (SDL's event
   pump prefers a single owning thread; a dedicated daemon thread keeps it off the
   async loop. Final thread-vs-task call belongs in the plan.)
5. **`gamepad` config section** in `config.yaml`:
   ```yaml
   gamepad:
     enabled: true
     device_index: 0          # which joystick (pygame index)
     modifier: 6              # button id that opens the menu (e.g. L2)
     buttons:
       pause: 7               # R2
       toggle_science: 8      # L3 (left stick click)
       toggle_practice: 9     # R3
       prev_segment: 4        # L1
       next_segment: 5        # R1
   ```
   (IDs above are placeholders; real indices vary by controller/mode and are
   discovered via the probe.) Parsed into a typed config object; the structure is
   chosen so a future `per_game` / `per_level` override map can wrap it without a
   rewrite.
6. **`spinlab gamepad-probe` CLI** — prints each button index as it's pressed (and
   the active device list), so Andrew fills in the config. Replaces the WRAM
   controller probe for this purpose.

### Removed
- The WRAM `$15`/`$17` menu reading in the poller and the WRAM-specific button
  bits in `ControllerMenuDetector`.
- `controller_held` / `controller_held_1` from `MemorySnapshot` and `read_snapshot`
  **iff** the menu detector was their only consumer (verify; if so, drop the
  cluster read too — a small snapshot speedup).
- The route-bar R-menu hint text updates to reflect the new (configured) buttons,
  or is generalized/removed.

## Testing

- **Detector**: full unit coverage via a fake `ButtonSource` (inject pressed-sets
  frame by frame) — same scenarios as the current WRAM tests (arm, rising-edge
  fire, seed-on-arm, release), now button-ID based. No gamepad needed.
- **Config parsing**: unit test the `gamepad` section → typed object.
- **`GamepadButtonSource`**: thin pygame wrapper; covered by a manual smoke test
  with the real controller (no gamepad in CI). Guard import/init so a missing
  pygame or no-controller environment degrades to "menu inactive", never a crash.

## Out of scope / deferred
- Per-game and per-level mapping overrides (config shape leaves room).
- Axis/hat-as-button, chords beyond modifier+1, rebinding from the UI.
- Configurable modifier choice between R (WRAM) and gamepad — WRAM path is being
  retired, so this collapses.
