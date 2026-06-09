# R-menu Vocabulary Expansion

Date: 2026-06-09
Status: design (approved in brainstorm, pending written-spec review)

## Motivation

Practice Pause shipped the R-menu command layer — holding R opens a command
"menu," and a button dispatches an action (R+X = pause). It is built to extend.
This spec defines the next batch of verbs so a whole practice session can be
driven from the controller without touching the keyboard, and lays the reusable
groundwork (reading a second controller byte) for any future command.

See the shipped layer: `python/spinlab/retroarch/menu_detector.py`,
`docs/superpowers/specs/2026-06-07-practice-pause-r-menu-design.md`.

## Model: flat shortcuts

Each `modifier + button` is one fixed, stateless action — no cursor, no modes
within the menu. A new verb is just one more entry in the command registry. R
stays the held modifier; the command button, pressed while R is held,
dispatches.

**Vocabulary** (R is the modifier throughout):

| Gesture | Command | Acts in |
|---|---|---|
| R + X | Pause (shipped) | Practice only |
| R + Y | Toggle Practice — Idle starts, Practice stops | Idle / Practice |
| R + → | Next segment | Practice only |
| R + ← | Abort current + go to previous segment | Practice only |

## Hard constraint: only the 12 SNES buttons, across two WRAM bytes

The detector reads SMW's joypad mirror in WRAM, which contains exactly the 12
SNES buttons — nothing else. Non-SNES inputs (the 8bitdo Star/menu key, L3/R3
stick presses) never reach the ROM and are invisible. The modifier and every
command button must be one of the 12.

Those 12 span **two** bytes (kaizosplits naming):

- **`$17` (buttonsHeld2):** `A X L R - - - -`. R = `0x10` (modifier), X = `0x40`.
  This is the only byte read today.
- **`$15` (buttonsHeld1):** `B Y Select Start Up Down Left Right`. Y = `0x40`,
  Left = `0x02`, Right = `0x01`.

So Toggle-Practice (Y) and the d-pad verbs live in `$15`, which we do not yet
read. Extending to them is the reusable-layer work in Phase 1.

## Modifier choice: R for now (deferred tradeoff)

R stays the hardcoded modifier for this spec. Recorded for a later revisit:

- The collision metric that matters is **"is the button ever *held* in
  gameplay?"**, not "is it rare." The menu opens on a *held* modifier. R is
  held in play (screen-scroll / look-ahead), so the menu can open mid-play and a
  command button pressed while scrolling can fire by accident — and this gets
  worse with d-pad verbs (hold R to scroll + press → to move = accidental "next
  segment").
- **Select** is the categorically safer modifier: although it has a gameplay
  function (drop reserve item), it is only ever *tapped*, never *held*. A *held*
  Select is therefore an unambiguous "open the menu" gesture that never happens
  by accident in play; the at-most-one item drop is harmless (our commands
  reload anyway).
- Decision: keep R hardcoded now (no config knob — explicitly out of scope per
  YAGNI). If the d-pad collisions bite in practice, migrate the modifier to
  Select (it is a one-line change of the modifier bit/byte once Phase 1 reads
  `$15`, since Select is `$15` bit `0x20`).

## Input leakage (inherent, mostly harmless)

The detector only *observes* WRAM; it cannot *block* input from the game. So
every `R+button` gesture also reaches SMW (R scrolls, → moves Mario, etc.). This
is harmless for our verbs because each either reloads a state (pause, next, prev
— whatever Mario did is discarded) or only matters between attempts
(toggle-practice). No new verb should be added that needs the input suppressed.

---

## Phase 1 — Input-layer extension (foundation)

**Goal:** read `$15` and generalize the command registry to span both bytes.

- `python/spinlab/retroarch/addresses.py`: add `ADDR_CONTROLLER_HELD_1 = 0x15`
  (B Y Select Start + d-pad held byte).
- `python/spinlab/retroarch/snapshot.py`: add a `controller_held_1` field
  (trailing, default 0, same rationale as `controller_held`) and read `$15`.
  Note: `$15` is adjacent to nothing we cluster; it is a lone read like `$17`.
  Apply the same read-order care that fixed the `$17` input-poll race
  (read the controller bytes before RA can re-poll input) if the emulator
  poke test shows flicker.
- `python/spinlab/retroarch/menu_detector.py`: generalize so the modifier and
  each command are addressed by `(byte, bit)`:
  - Modifier stays R = (`$17`, `0x10`).
  - `COMMANDS` becomes a registry of `(byte, bit) -> name`, e.g.
    `{(HELD2, 0x40): "pause", (HELD1, 0x40): "toggle_practice",
      (HELD1, 0x01): "next_segment", (HELD1, 0x02): "prev_segment"}`.
  - The held-modifier semantics and the "command must be pressed *after* R is
    held" seed (rising-edge per command bit, seeded at menu-open) are unchanged
    — just applied per `(byte, bit)`.
- The `$18`/`$16` "pressed" twins stay unread (we edge-detect the held bytes).

**Testing:** unit tests over synthetic two-byte snapshots; one emulator poke
scenario that holds R (`$17`) and taps a `$15` button to confirm the second byte
reads and dispatches on real RA.

## Phase 2 — Command dispatch + Toggle Practice

**Goal:** route commands by name with per-command mode rules; ship Toggle
Practice.

- `python/spinlab/session_manager.py`: `_handle_controller_command` grows from a
  single `pause` branch into a small per-command dispatch:
  - `pause` → if `mode == PRACTICE`: `practice_session.toggle_pause()` (shipped).
  - `toggle_practice` → if `mode == IDLE`: `start_practice()`; elif
    `mode == PRACTICE`: `stop_practice()`; else ignore. (Mode-spanning — the
    first command that acts outside PRACTICE.)
  - Unknown command names log a warning (shipped behavior).
- Each command's mode rule lives with its handler, not in the detector — the
  detector stays a dumb input→name translator.

**Testing:** unit tests that route a `ControllerCommandEvent("toggle_practice")`
in IDLE (starts) and in PRACTICE (stops), and that it's ignored elsewhere.

## Phase 3 — Segment history navigation (the heavy piece)

**Goal:** browser-style back/forward through the segments practiced this
session, driven by R+← / R+→.

`PracticeSession` gains an ordered visit history and a cursor:

- `history: list[str]` — segment_ids in the order they were loaded this session.
- `cursor: int` — index of the segment currently loaded.

**Behavior:**

- **Normal completion advances forward.** When an attempt finishes, the loop
  moves to `cursor + 1`: if that index exists in `history`, reload it; if the
  cursor is at the end, ask the scheduler for a fresh pick and append it. So
  ordinary practice (no navigation) always sits at the end of history, appending
  new picks — today's behavior, now recorded.
- **R+← (prev):** if `cursor > 0`, abort the current attempt (reusing the pause
  disarm path — the partial attempt is dropped, nothing recorded), decrement the
  cursor, and load `history[cursor]`. At `cursor == 0` it is a no-op.
- **R+→ (next):** abort the current attempt, increment the cursor; if that runs
  past the end of `history`, scheduler-pick + append; load `history[cursor]`.
- Navigation aborts record **nothing** (same drop-the-attempt semantics as
  pause), keeping the data clean.

**Reuse:** next/prev share the "disarm current attempt + load a chosen segment"
machinery with pause (pause = disarm + reload-*same*; nav = disarm + load-
*chosen*). Factor the common "abort and load segment X" path so pause, next, and
prev are thin callers.

**Open mechanics for the plan** (not decided here): exact cursor/append index
bookkeeping; whether `history` stores full `SegmentCommand`s (so a revisit
reloads without re-consulting the scheduler) or just ids; how a mid-history
completion that reaches a stale/invalidated segment is handled.

**Testing:** unit tests over a fake scheduler asserting the history/cursor
transitions for: linear play (append-forward), prev/prev/next, next-past-end
(fresh pick), prev at index 0 (no-op), and that nav drops the in-flight attempt.

## Phasing & dependencies

1 → 2 are small and independent (2 needs only the generalized registry from 1
for the Y bit, plus the dispatch change). 3 is the substantial practice-loop
subsystem and depends on 1 (d-pad bits). Recommended order: 1, 2, then 3. Phase
3 may be split into its own plan if it grows.

## Out of scope

- **Page/tab navigation** (R+d-pad to flip dashboard views): cool but low value;
  the buttons are better spent on segment nav. Dropped.
- **A modifier config knob / Select migration:** explicitly not now (R
  hardcoded). The held-vs-tapped analysis above is the record for when it's
  revisited.
- **GrindOne** (repeat-one-segment): related — it would reuse Phase 3's
  abort-and-load machinery — but it is its own backlog feature with its own
  brainstorm, not part of this spec.
- Reading the `$16`/`$18` "pressed" bytes; HyperPlay menu commands; suppressing
  input from the game.

## Testing gate (per phase, before merge)

Full `python -m pytest` (incl. emulator) + `cd frontend && npm test` +
`npm run typecheck` + `npm run build`. Each phase ships green independently.
