# SpinLab Glossary

Quick reference for domain terms used across specs, architecture docs, and code. Keep entries short and link out to specs/architecture for depth.

## Routing & Segment Model

- **Geography** — the level-physical part of a segment: `(game, level, start_type, start_ord, end_type, end_ord)`. Independent of in-level game state.
- **Waypoint** — a node in the segment graph: `(game, level, endpoint_type, ordinal, conditions)`. Save states attach to waypoints. Two waypoints at the same geographic endpoint but with different conditions are distinct waypoints. See `docs/superpowers/specs/2026-04-05-segment-conditions-design.md`.
- **Segment** — an edge between two waypoints (start → end). Attempts attach here. Identified by both waypoint IDs plus geography.
- **Route** — a path through the waypoint graph. In v1 implicit as "the set of `is_primary=True` segments"; explicit route modeling is future work.

## Conditions

- **Condition** — a named, memory-derived value read at a transition (e.g. `powerup=big`). Defined in per-game YAML.
- **Observed conditions** — full snapshot of in-scope conditions recorded at a transition. Always captured on attempts.
- **Required conditions** — subset of conditions a waypoint carries as part of its identity.
- **Condition scope** — `game` (every level) or `{ levels: [...] }` (specific levels only).

## Save States

- **Hot variant** — save state captured at the exact frame a checkpoint is hit.
- **Cold variant** — save state captured on first respawn from a checkpoint (post-death-animation).

## Reference Capture

- **Capture run** (`capture_runs` row) — one reference recording (live or replay). `draft=1` until finalized; `active=1` for the currently selected reference per game. Spans 1..N capture sessions.
- **Capture session** (`capture_sessions` row) — one continuous recording window inside a capture run. Has its own `.spinrec` file and an `ordinal` within the run. Stopping a reference ends the session and leaves the run paused; resuming opens a new session.
- **Paused run** — a `draft=1` capture run with no active session. State lives in memory as `ReferenceController.paused_run_id`. Survives restart via `recover_paused_capture_run`.
- **Reference attempt** — an `attempts` row with `source='reference'` written by `SegmentRecorder` at segment close. One row per died/survived event; all rows for a segment share an `episode_id` and have `capture_run_id` as their parent. Written atomically with the segment upsert — no separate drain step at finalize.

## Time Series

- **Total time** (`time_ms`) — wall-clock time for a completed attempt, including any deaths and respawns. The "total" series in estimator outputs and the history chart.
- **Clean tail** (`clean_tail_ms`) — time from the last death (or segment start if deathless) to completion. Isolates execution quality from death count. The "clean" series in estimator outputs and the history chart.

## Flags

- **is_primary** — per-segment flag. Practice loop serves only primary segments. Auto-True for the first segment in a geography.
- **active** — per-segment flag. Inactive segments are excluded everywhere, including capture matching.
- **invalidated** — per-attempt flag. Invalidated attempts are preserved but excluded from estimators. Set by in-emulator hotkey or dashboard delete.

## Polymorphic Identifiers

- **`attempts.parent_id`** — references a practice session id when `source='practice'`, and a capture_run id when `source='reference'`. The single column expresses "the parent context this attempt belongs to."

## AHK Shortcuts (see `scripts/spinlab.ahk`)

- **CAW** (Ctrl+Alt+W) — Start the dashboard (`spinlab dashboard`). If already running, re-uses existing process.
- **CAX** (Ctrl+Alt+X) — Stop the dashboard (graceful HTTP shutdown, kill RetroArch, fallback taskkill).

## Modes (see `docs/ARCHITECTURE.md`)

- **Idle / Reference / Replay / Practice / Cold-fill / Speed-run / Fill-gap** — dashboard mode-machine states (see `spinlab.models.Mode`).
- **Reference run** — a recorded run that captures waypoints, save states, and attempts as transitions fire.
- **Practice loop** — the serve-save-state, collect-rating, update-estimator cycle.
