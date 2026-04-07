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
- **StartPoint** — (deprecated term, replaced by Waypoint) — a waypoint that a save state loads you into.

## Flags

- **is_primary** — per-segment flag. Practice loop serves only primary segments. Auto-True for the first segment in a geography.
- **active** — per-segment flag. Inactive segments are excluded everywhere, including capture matching.
- **invalidated** — per-attempt flag. Invalidated attempts are preserved but excluded from estimators. Set by in-emulator hotkey or dashboard delete.

## AHK Shortcuts (see `scripts/spinlab.ahk`)

- **CAW** (Ctrl+Alt+W) — Start the dashboard (`spinlab dashboard`). If already running, re-uses existing process.
- **CAX** (Ctrl+Alt+X) — Stop the dashboard (graceful HTTP shutdown, kill Mesen, fallback taskkill).

## Modes (see `docs/ARCHITECTURE.md`)

- **Passive / Reference / Replay / Practice** — Lua script modes.
- **Reference run** — a recorded run that captures waypoints, save states, and attempts as transitions fire.
- **Practice loop** — the serve-save-state, collect-rating, update-estimator cycle.
