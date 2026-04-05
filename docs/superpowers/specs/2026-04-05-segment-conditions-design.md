# Segment Start/End Conditions — Design

**Date:** 2026-04-05
**Status:** Draft

## Motivation

Today a segment is identified purely by its geography: `(game, level, start_type, start_ord, end_type, end_ord)`. This is sufficient for linear kaizo hacks where every level starts small-powerup, every level ends the same way, and the game forcibly strips powerups at level boundaries.

It breaks down for:

- **Older / standard hacks** where powerup carries into the level and affects strat choice (small vs. big vs. cape vs. fire).
- **100% runs** where the same level is entered twice (normal exit, secret exit) or where dragon-coin collection splits across visits.
- **Niche hacks** where the blue-box "backup powerup" is route-critical.
- **Meme / custom categories** where arbitrary per-level conditions (colored keys, moon counts) matter.

The goal is to attach **conditions** — named, game-state-derived values — to segment identity, to save states, and to attempts, so that:

1. The same geographic transition captured under different conditions becomes distinct segments with their own save states, attempts, and estimates.
2. Attempts always record their full observed conditions so we can later re-partition data without losing history.
3. Custom per-level conditions can be added by editing YAML without DB migrations or code changes.
4. The manual escape hatches (cancel-in-emulator, delete-from-dashboard) let the user discard attempts that don't meet the conditions they care about.

## Glossary

These terms are used throughout and will also live in `docs/GLOSSARY.md`:

- **Geography** — the level-physical part of a segment: `(game, level, start_type, start_ord, end_type, end_ord)`. Independent of game state.
- **Waypoint** — a node in the segment graph: `(game, level, endpoint_type, ordinal, conditions)`. Save states attach to waypoints. Two waypoints at the same geographic endpoint but with different conditions are distinct waypoints.
- **Segment** — an edge between two waypoints (a start waypoint and an end waypoint). Attempts attach to segments.
- **Condition** — a named, game-state-derived value read from memory at a transition (e.g. `powerup=big`, `on_yoshi=true`). Defined in per-game YAML.
- **Observed conditions** — the full snapshot of in-scope conditions recorded at the moment of a transition. Always captured on attempts.
- **Required conditions** — the subset of conditions a waypoint carries as part of its identity. Two attempts are "the same segment" only if their observed conditions match the required conditions of the segment's start and end waypoints.
- **is_primary** — a per-segment flag selecting which segment is served by the practice loop when multiple segments share the same geography.
- **Hot/cold** — existing save-state-capture variant on checkpoint waypoints (hot = captured at checkpoint hit, cold = captured on first respawn). Orthogonal to conditions.

## Data Model

### Condition definitions (YAML)

Per-game file at `python/spinlab/games/<game_id>/conditions.yaml`:

```yaml
conditions:
  - name: powerup
    address: 0x0019
    size: 1
    type: enum
    values: { 0: small, 1: big, 2: cape, 3: fire }
    scope: game
  # Future additions (deferred, schema-ready):
  # - name: on_yoshi
  #   address: 0x187A
  #   size: 1
  #   type: bool
  #   scope: game
  # - name: yellow_key_held
  #   address: 0x7E1F2D
  #   size: 1
  #   type: bool
  #   scope: { levels: [42] }
```

**v1 ships with `powerup` only.** All other conditions are future additions that the schema and YAML loader must support without code changes.

Scope values:

- `game` — tracked on every attempt for every level in the game.
- `{ levels: [N, ...] }` — tracked only on attempts in the listed levels.

An attempt's observed-conditions snapshot is the union of all in-scope definitions at capture time. A condition outside scope is simply absent from the snapshot — not stored as null.

### SQLite tables

**`waypoints`** (new)

| column          | type    | notes                                                         |
| --------------- | ------- | ------------------------------------------------------------- |
| id              | TEXT PK | deterministic hash of identity fields                         |
| game_id         | TEXT    |                                                               |
| level_number    | INT     |                                                               |
| endpoint_type   | TEXT    | 'level_entrance', 'checkpoint', 'goal', 'orb', etc.           |
| ordinal         | INT     |                                                               |
| conditions_json | TEXT    | canonicalised JSON of required conditions (sorted keys)       |

Waypoint ID derivation: `sha256(f"{game_id}:{level}:{endpoint_type}.{ordinal}:{conditions_json}")[:16]` — stable across processes.

**`segments`** (modified)

Existing columns retained; adds:

| column             | type | notes                                            |
| ------------------ | ---- | ------------------------------------------------ |
| start_waypoint_id  | TEXT | FK → waypoints.id                                |
| end_waypoint_id    | TEXT | FK → waypoints.id                                |
| is_primary         | BOOL | default True when first segment in geography     |

Segment ID derivation changes to include both waypoint IDs so that two segments sharing geography but differing in conditions are distinct rows:
`game_id:level:start_type.start_ord:end_type.end_ord:start_wp_id[:8]:end_wp_id[:8]`

The existing `start_type / start_ordinal / end_type / end_ordinal` columns on `segments` are retained as denormalised convenience fields (they are derivable from the waypoints but heavily used in existing query paths).

**`waypoint_save_states`** (new table, replaces `segment_variants`)

Save states attach to **waypoints**, not segments. One row per `(waypoint_id, variant_type)` with `variant_type ∈ {hot, cold}`. Non-checkpoint waypoints (level entrances, goals) only ever carry a single `hot` variant — `cold` applies only to checkpoint waypoints, matching existing semantics.

| column        | type    | notes                                |
| ------------- | ------- | ------------------------------------ |
| waypoint_id   | TEXT    | FK → waypoints.id                    |
| variant_type  | TEXT    | 'hot' or 'cold'                      |
| state_path    | TEXT    | filesystem path to `.mss`            |
| is_default    | BOOL    | which variant the practice loop uses |

Primary key: `(waypoint_id, variant_type)`.

> **No data migration.** Existing databases and save states are discarded as part of this change — drop `segment_variants`, `segments`, `attempts`, and related tables, recreate fresh with the new schema, and re-run reference captures to repopulate.

**`attempts`** (modified)

Adds:

| column                      | type | notes                                     |
| --------------------------- | ---- | ----------------------------------------- |
| observed_start_conditions   | TEXT | JSON blob                                 |
| observed_end_conditions     | TEXT | JSON blob (null for incomplete attempts)  |
| invalidated                 | BOOL | default False; set True by manual cancel  |

Invalidated attempts are preserved (not deleted) but excluded from estimators and stats.

## Runtime Flow

### Startup

1. Python loads `conditions.yaml` for the active game, building an in-memory `ConditionRegistry`.
2. On Lua TCP connect, Python sends a new command `set_conditions:<json>` carrying the list of `(name, address, size)` tuples. Lua caches them.
3. Lua reads the addresses on every transition event and includes `conditions: { name: raw_value, ... }` in the event payload. Decoding raw → logical value (`0` → `"small"`) happens on the Python side.

### Reference-run capture

When a transition event arrives with observed conditions:

1. Python decodes raw values via the `ConditionRegistry`.
2. Filters to in-scope conditions for the current level.
3. Looks up or creates the corresponding **waypoint** (based on geography + observed conditions as required conditions).
4. Save state is captured and attached to that waypoint.
5. When the *next* transition arrives, a segment is looked up or created between the previous waypoint and the new waypoint.
6. New segments get `is_primary=True` iff no other active segment exists for the same geography; otherwise `is_primary=False`.

### Practice loop

Query becomes `WHERE active AND is_primary`. The allocator sees only primary segments, unchanged otherwise.

When serving a segment, the save state loaded is the one attached to the segment's **start waypoint** (respecting hot/cold variant selection as today).

### Manual invalidation

Two paths:

1. **In-emulator hotkey** — a reserved button combo (e.g. `L + Select`) detected in Lua practice mode. Fires an `attempt_invalidated` event over TCP. Python marks the current attempt's `invalidated=True` and advances as if the attempt completed-without-counting. Practice loop treats invalidated attempts as skipped for scheduling purposes.
2. **Dashboard delete button** — on the recent-attempts view, each row has a button that toggles `invalidated`. Estimators recompute on toggle.

The specific hotkey is a config value in `config.yaml` under a new `practice.invalidate_combo` key (e.g. `["L", "Select"]`) with a comment explaining rationale. The final default is picked during implementation; the only hard constraint is it must not collide with any existing in-emulator control combos.

## Dashboard UI (v1)

A new **Segments** view grouped by `(game, level)`. For each level, one collapsible section listing every segment. Each row shows:

- Start waypoint summary: `level_entrance.0` + conditions
- End waypoint summary: `goal.0` + conditions (or `checkpoint.1`, etc.)
- Attempt count, estimate
- `is_primary` toggle
- `active` toggle

This view makes duplicate-level routes (normal exit + secret exit) legible: both appear as separate rows in level 42's section, each independently togglable.

No route-ordering UI in v1. A "route" is implicitly "the set of is_primary=True segments."

## Testing

Fast tests (no emulator) must cover:

- ConditionRegistry parses `conditions.yaml` correctly for all documented scopes.
- Waypoint ID derivation is stable and deterministic across processes.
- Segment creation during transition handling correctly assigns `is_primary` based on existing-segment lookup.
- Attempt invalidation excludes attempts from estimators but preserves rows.
- In-scope filtering: a level-42-scoped condition never appears on a level-5 attempt's observed snapshot.

Emulator tests must cover:

- Lua receives `set_conditions` command, reads the powerup address at transitions, includes it in payloads.
- Full capture flow: transition → waypoint creation → save state attached to waypoint.
- Manual-invalidate hotkey fires TCP event and does not collide with existing in-emulator controls.

## Out of Scope for v1

Explicitly deferred (schema should not preclude them):

- **Additional conditions beyond powerup.** Schema and loader support them; no extra YAML entries ship.
- **Pooling across conditions** — "this segment ignores backup powerup" as a declarative feature. Today's rule is strict: the required conditions on a waypoint define the segment.
- **Python-hook escape hatch** for computed/derived conditions.
- **Route modeling UI** — ordered sequences, same-level-visited-twice sequencing, route validity checking.
- **Dashboard YAML editor.**
- **Auto-promotion of is_primary** based on observed route utility.
- **Hot/cold as a condition dimension** — stays a save-state variant for now.

## Open Questions

None blocking v1. Items parked above will be re-visited as usage demands.
