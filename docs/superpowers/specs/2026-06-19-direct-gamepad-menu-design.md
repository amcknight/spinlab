---
title: Direct-gamepad menu (replace the WRAM R-menu)
date: 2026-06-19
status: implemented
---

> **Implemented 2026-06-19** on branch `feat/gamepad-menu` via
> `docs/superpowers/plans/2026-06-19-direct-gamepad-menu.md` (9 tasks). New
> `spinlab.gamepad` package (detector / source / loop / probe), `gamepad:`
> config section + `[gamepad]` pygame extra, `spinlab gamepad-probe` CLI; WRAM
> menu fully retired from poller + snapshot. **Outstanding: the manual hardware
> smoke test** — no controller in CI; Andrew must run `spinlab gamepad-probe`
> with the 8bitdo, confirm X/Y and A/B give distinct indices, fill `config.yaml`,
> and verify end-to-end dispatch before relying on it live.

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
set. Reading the real pad immediately fixes the face-button **merge** (every
physical button is distinct). The gameplay **collision** is mitigated — not
magically eliminated — by mapping onto buttons with no/rare game function; how well
depends on the user's setup, which is exactly why the mapping is configurable.

### Button-availability reality (why config is mandatory, not a nicety)
There is **no universally-free button** to assume. Andrew is a "dirty remapper"
running SMM2-style controls: **A is remapped to R2 (spin), and L / L2 are
save-state / load-state.** So the buttons a naive design would grab for the menu
(L2/R2) are already in heavy use, and a menu press there would spin Mario or
save/load. The realistic free candidates are no-game-function buttons — **stick
clicks (L3/R3), Home/Star/Capture** — but the exact set varies per user and
controller. Consequences for the design:
- The config must accept **any** button id for the modifier and each verb.
- **Modifier choice matters most:** it's *held* to open the menu, so it should be a
  button whose held action is harmless (ideally no game function — a stick click).
  A modifier on save/load/spin would fire that action every time the menu opens.
- Verbs placed on game-used buttons will still trigger their game action while the
  modifier is held (the press reaches RetroArch too). That's an accepted,
  user-chosen tradeoff — the `gamepad-probe` exists so the user can find their free
  buttons and decide.

### Settled choices
- **Replace** the WRAM (`$15`/`$17`) menu input entirely; gamepad is the sole menu
  input. No gamepad connected → the menu is simply inactive (use the UI buttons).
- **Modifier-based** gesture (hold a modifier, press a verb). Prefer no-game-function
  buttons (stick clicks / Home) for the modifier especially; the config allows any
  button so the user maps around their own remaps (see Button-availability reality).
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
     modifier: 8              # button that OPENS the menu — pick a no-game-function
                              # button (e.g. a stick click) so holding it is harmless
     buttons:
       pause: 9               # each verb -> a button id (a free button if you have
       toggle_science: 10     # one; otherwise a game button you accept leaking)
       toggle_practice: 11
       prev_segment: 4
       next_segment: 5
   ```
   (IDs are placeholders — real indices vary per controller/mode and Andrew's
   remaps; discover them with the probe.) Parsed into a typed config object; the
   structure is chosen so a future `per_game` / `per_level` override map can wrap it
   without a rewrite.
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
