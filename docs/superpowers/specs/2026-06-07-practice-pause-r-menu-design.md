# Practice Pause + the R-menu command layer

Date: 2026-06-07
Status: design (approved in brainstorm, pending written-spec review)

## Motivation

A chunk of the outlier attempts polluting the model (the "38 suspects" from the
alpha backtest — e.g. a 41.7s "clean" clear on a ~13s segment) come from the
player sitting on a segment reading graphs/chat while the attempt clock runs.
**Practice Pause** lets the player stop the timer for a break without that idle
time entering the data. It is part of the data-hardening push toward a clean
golden collection session (see the data-hardening project).

The trigger is generalized: **holding R is a command "menu"** — an extensible
input layer for future controller commands. Practice Pause (R+X) is the first and,
for now, only command.

## The R-menu input framework (the reusable layer)

SpinLab reads **no controller input today** — the frame snapshot is all
game-state bytes. This feature adds the input-reading layer:

- **Snapshot:** read SMW's controller mirror into the per-frame snapshot —
  `$17` (controller-1 **held**, byte `A X L R · · · ·`) and `$18` (controller-1
  **newly-pressed**, same layout). R = bit 4 (`0x10`), X = bit 6 (`0x40`). These
  are vanilla SMW's SNES joypad copy, stable across hacks. (`$15`/`$16` — the
  B/Y/Select/Start/d-pad byte — are intentionally NOT read yet; adding them later
  extends the command-button set.)
- **Menu detector** (poller-level, alongside the transition detector): a small
  state machine over the per-frame held/pressed bytes:
  - **ARM:** R held (`$17 & 0x10`) for ≥ a threshold of consecutive frames
    (~0.5s ≈ 30 frames at 60Hz) arms the menu. A quick look-ahead tap of R never
    reaches the threshold, so it can't arm mid-play.
  - **DISPATCH:** while armed, a newly-pressed command button (`$18`) fires the
    mapped command via a registry `COMMANDS = {X: "pause"}`. Future commands add
    a key (e.g. `Y: "grind-one"`). Emits a `ControllerCommandEvent(command)`.
  - **DISARM:** releasing R disarms the menu.
- The command threshold + bit constants are named file-level constants with
  rationale (no magic numbers), per project modeling guidelines.

## Pause behavior (discard + restart same)

The `ControllerCommandEvent("pause")` is handled in the practice session as a
**toggle**:

- **While PLAYING → PAUSE:** disarm the current attempt's timing so the partial
  episode is **dropped entirely** (nothing recorded — uses the existing
  `PracticeTiming.disarm()` / IDLE path, where events are ignored). Freeze the
  session/savings clock: record the pause-start and, on resume, add the paused
  span to a **session pause-offset** that elapsed + savings-per-hour subtract.
  While paused, detector events (death/finish) are ignored, so the game may run
  freely and the player can step away — anything in-game is discarded.
- **While PAUSED → RESUME:** reload the **same** segment fresh (re-arm a new
  attempt via the normal practice-loop load path) and unfreeze the clock.

**Locked decisions:**
- **The emulator is NOT paused.** Recording is frozen and reload-on-resume makes
  the in-game state during the pause irrelevant, so no RA pause mechanism is
  needed.
- **Practice mode only** for now. HyperPlay can adopt the same R-menu later (the
  input layer is mode-agnostic; only the command handler is practice-scoped).

## UI feedback (dashboard)

- **Menu armed (R held past threshold):** a small hint shows the available
  commands — for now just `X — Pause`. Surfaced via the existing SSE/AppState
  push so the dashboard reflects it.
- **Paused:** the live practice card shows a **PAUSED** state with the elapsed
  timer and savings/hr **frozen** (not ticking). Resuming clears it.

## Architecture / where it lives

- `python/spinlab/retroarch/addresses.py` + `snapshot.py` — the `$17`/`$18`
  controller bytes (held/pressed) as new snapshot fields + address-map entries.
- A new **menu/command detector** (poller-level), emitting `ControllerCommandEvent`
  — kept separate from the transition detector (single responsibility).
- `session_manager` / `practice` — handle the pause command: disarm + freeze the
  session clock + reload-on-resume; expose `is_paused` / menu-armed state on
  `AppState` for the UI.
- Frontend — the menu hint + the PAUSED live-card state (timer/savings frozen).

## Out of scope (explicitly deferred)

- Any command other than Pause (the registry is built to extend, but only X=pause
  ships).
- Reading `$15`/`$16` (other buttons) — added when a command needs them.
- HyperPlay pausing.
- Pausing the actual emulator.
- A freeze-and-continue pause that preserves a mid-run attempt (we chose
  discard-and-restart-same).

## Testing

- **Menu detector (unit):** R-held-below-threshold does NOT arm; R-held-past-
  threshold arms; armed + X-pressed emits `pause`; R release disarms; a lone X
  press (no R) does nothing. Pure over synthetic held/pressed byte sequences.
- **Pause handling (unit):** pause while PLAYING disarms timing (no episode
  emitted) and starts the session pause-offset; resume reloads the segment and
  clears paused state; elapsed/savings exclude the paused span.
- **Emulator (RA poke harness):** poke `$17`/`$18` to confirm the snapshot reads
  the held/pressed bytes and the detector arms + dispatches on a real frame
  stream. This is the live confirmation of the addresses/bits.
- Full gate before merge: `python -m pytest` (incl. emulator) + `cd frontend &&
  npm test` + `npm run typecheck` + `npm run build`.
