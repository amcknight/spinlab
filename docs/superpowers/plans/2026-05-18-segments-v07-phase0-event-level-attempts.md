---
date: 2026-05-18
status: shipped
shipped_at: 2026-05-19
shipped_commit: 36c5536
focus: "Phase 0 of segments-v07 integration — refactor `attempts` from episode-shaped to event-level (one row per death-or-clear)."
spec: docs/superpowers/specs/2026-05-18-segments-v07-integration-design.md
scope: phase-0 only; no model wiring (Phase 1), no UI (Phase 3)
---

## Shipped 2026-05-19 in merge `36c5536`

Phase 0 was executed in a prior session via worktree `worktree-segments-v07-phase0`
and merged. Delivered:

- `python/spinlab/db/migrations/0002_event_level_attempts.sql`
- Event-level `attempts` table; `_roll_up_episode` adapter in
  `python/spinlab/db/attempts.py`; `EventAttempt` model
- `PracticeTiming` per-event emission via `EventAttemptEmission`
- `Scheduler.update_state_after_episode` post-Phase-0 ordering
- Tests: `test_event_level_attempts.py` (DB + integration),
  `test_estimator_parity_phase0.py`, `test_timing.py` additions,
  `tests/fixtures/segments_v07/capture_golden.py`,
  pinned golden estimator outputs JSON
- 930 tests green on baseline 2026-05-19.

Phase 1 unblocked. The body below is preserved as the design that landed.

---


## What this plan covers

Phase 0 of the spec. End state after this plan lands:

- `attempts` table holds **one row per died-or-survived event**, not one per episode.
- The capture pipeline (practice and speed-run) writes per-event rows as they happen.
- Existing Kalman / ExpDecay / RollingMean estimators keep working because a thin **episode adapter** rolls the event rows back up into the `EpisodeView` they expect.
- `model_states` rows are wiped on migration; `rebuild_all_states` repopulates from event-level attempts via the adapter.
- All existing attempts data is dropped (Andrew confirmed in spec).
- Frontend semantics unchanged: "Recent Attempts" etc. still display episodes, rebuilt from event rows.

Out of scope (deferred to Phase 1):

- Vendoring `segments_experiment/` into the repo.
- Calling `fit_segment` / `refit_segment`.
- `segment_fits` table.
- Any UI change.

## Anchor questions resolved

- **OQ1 (`s_at_death`)** — closed; not needed. Prototype consumes only
  `(outcome, time_ms)`. See spec Resolution log.
- **OQ5 (`time_ms` semantics)** — closed here: `time_ms` per event is
  the wall-clock from the preceding `LevelEntranceEvent` /
  `SpawnEvent` to this `DeathEvent` / `LevelExitEvent`. This matches
  the prototype's convention where it subtracts a uniform
  `config.RESPAWN_MS = 3500ms` to recover `tau` (in-attempt
  play-time). We do **not** try to special-case entrance vs respawn —
  the SMW entrance fade is roughly the same duration as a respawn, and
  the prototype is already designed for that level of uniformity.

## Schema (migration `0002_event_level_attempts.sql`)

```sql
-- Drop and recreate. Andrew confirmed existing attempts data is expendable.
DROP TABLE IF EXISTS attempts;

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  session_id TEXT REFERENCES sessions(id),
  capture_run_id TEXT REFERENCES capture_runs(id),
  episode_id TEXT NOT NULL,        -- groups consecutive attempts in one player run
  outcome TEXT NOT NULL CHECK (outcome IN ('died','survived')),
  time_ms INTEGER NOT NULL,        -- wall-clock for this single attempt
  source TEXT NOT NULL DEFAULT 'practice',
  chosen_allocator TEXT,
  invalidated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  CHECK ((session_id IS NOT NULL) <> (capture_run_id IS NOT NULL))
);

CREATE INDEX idx_attempts_segment_episode
  ON attempts(segment_id, episode_id, id);

-- Wipe model state so rebuild_all_states regenerates from the new attempts shape.
DELETE FROM model_state;
```

Note what dropped: `completed`, `deaths`, `clean_tail_ms`. These were
episode-level fields. They become **derived** from grouping by
`episode_id` (see Episode adapter below).

## Capture-pipeline changes

`timing.PracticeTimer` is the right place to emit per-event rows. It
already sees `DeathEvent` (line 130) and the completion path
(`_enter_result`). The change is to fire a callback per event, not
just at episode end.

Three knobs added to `PracticeTimer.arm`:

```python
on_attempt_result:   Callable[[AttemptResultEvent], None]      # existing — episode end
on_event_attempt:    Callable[[EventAttempt], None] | None     # NEW — per died/cleared event
```

`EventAttempt` is a new tiny protocol dataclass:

```python
@dataclass(frozen=True)
class EventAttempt:
    segment_id: str
    episode_id: str
    outcome: Literal['died', 'survived']
    time_ms: int
    timestamp_ms: int  # wall-clock at the event (for ordering / debug)
```

Where it fires inside `PracticeTimer`:

- On `DeathEvent` (after `self._deaths += 1`): compute
  `time_ms_for_event = now - self._last_event_ms`, fire `outcome='died'`.
  New internal field `self._last_event_ms` initialized in `arm()` to
  `self._start_ms` and updated on each death/clear.
- On `LevelExitEvent` (completion) and `CheckpointEvent`
  (checkpoint-end completion): compute `time_ms_for_event = now -
  self._last_event_ms`, fire `outcome='survived'`.
- Aborts (`event.goal == 'abort'`): no row.

`episode_id` is minted by `PracticeTimer.arm` (UUID4) and passed
through to every event in that armed attempt. Episode boundaries
match `arm()` boundaries — that's a clean definition since `arm()` is
already called per attempt.

(Resolves OQ4: episode_id lifetime = `arm()` to `_reset()`.)

Speed-run mode (`speed_run.py`) follows the same pattern. The
SpeedRunTimer has analogous arm / reset logic.

The capture pipeline (reference recording in `capture/reference.py`)
also logs an `Attempt` per segment for reference runs (line 76-86).
That path is **episode-equivalent** by construction — a reference run
captures the "ideal" clear, no deaths. It emits a single
`outcome='survived'` row at segment close. (Reference runs with
deaths in them aren't a current feature; if they appear, the same
event-level emission applies.)

## Episode adapter (`db/attempts.py`)

The estimators consume `Attempt` (episode shape) today. We introduce
an `EpisodeView` derived from event rows. The shape matches what the
scheduler / Kalman / RollingMean currently expect:

```python
@dataclass(frozen=True)
class EpisodeView:
    segment_id: str
    session_id: str | None
    capture_run_id: str | None
    completed: bool          # last event in episode is 'survived'
    time_ms: int             # sum of event time_ms across episode
    deaths: int              # count of 'died' events in episode
    clean_tail_ms: int | None # time_ms of the final ('survived') event;
                              # None if episode aborted (no 'survived' end)
    source: str
    chosen_allocator: str | None
    created_at: str          # created_at of the closing event
    invalidated: bool        # OR of any invalidated flag in the episode
```

New helpers on `AttemptsMixin`:

- `get_episode_views(segment_id, session_id=None, since_id=None) -> list[EpisodeView]`
  — used by the existing estimators / rebuild path.
- `get_recent_episodes(game_id, limit=...) -> list[RecentEpisodeRow]`
  — replaces the existing `get_recent_attempts`; same TypedDict shape
  so frontend stays unchanged.

The aggregation is `SELECT ... FROM attempts WHERE segment_id=? AND
session_id=? GROUP BY episode_id ORDER BY MIN(id)` with appropriate
SUM / COUNT / last-row tricks.

`Database.log_attempt` is replaced by `Database.log_event_attempt`.
Callers update accordingly (4 sites: `practice.py`, `speed_run.py`,
`capture/reference.py`, `capture/finalizer.py`, `scheduler.py`).

## Estimator rebuild

`scheduler.record_attempt(attempt: Attempt)` currently feeds estimators
their episode tuple. After the refactor:

- `scheduler.record_event_attempt(event: EventAttempt)` — called per
  event row. Buffers events by `episode_id`; when the episode closes
  (next `EventAttempt` with a different episode_id, or explicit
  flush), builds an `EpisodeView` and runs the existing estimators on
  it.
- `rebuild_all_states()` — replays the new `get_episode_views()` for
  each segment through the existing estimator math. Numerical output
  must match today's `rebuild_all_states` for the same equivalent
  inputs.

## Test plan

Six new tests, all in `tests/`:

1. **Migration test** (`tests/db/test_migrations.py`) —
   apply 0002, assert old `attempts` cols (deaths, completed,
   clean_tail_ms) are gone, new cols (outcome, episode_id) are
   present, `model_state` is empty.

2. **PracticeTimer per-event emission** (`tests/test_timing.py`) —
   feed a synthetic event stream (2 deaths + 1 clear), assert 3
   `EventAttempt`s fire with correct outcomes, `time_ms` values, and a
   shared `episode_id`.

3. **EpisodeView round-trip** (`tests/db/test_attempts.py`) —
   insert event rows, call `get_episode_views`, assert
   `time_ms_total == sum`, `deaths == count(died)`, `clean_tail_ms ==
   final survived event time_ms`, `completed == True`.

4. **Aborted episode** — sequence with deaths but no surviving event;
   `EpisodeView.completed=False`, `clean_tail_ms=None`.

5. **Estimator parity** (`tests/test_scheduler.py`) — feed an
   episode-equivalent attempt sequence and assert Kalman /
   ExpDecay / RollingMean produce **bit-identical**
   `expected_ms` / `ms_per_attempt` / `floor_ms` to the pre-refactor
   golden values (captured once on `main` immediately before the
   refactor merges, pinned in the test).

6. **Capture-pipeline integration** (`tests/integration/test_event_level_attempts.py`)
   — drive a multi-death practice run via the RA harness, assert the
   right number of event rows land with the right outcomes and a
   shared `episode_id`. New emulator test; gated on the standard
   `pytest -m emulator`.

## Risks and mitigations

- **Estimator output drifts after rebuild.** The episode adapter sums
  per-event `time_ms`. If the previous shape included penalty math
  inside `time_ms` (it does — `practice.py` adds
  `death_penalty_ms * deaths`), the new sum will differ. Mitigation:
  the `EpisodeView.time_ms` rebuilds the same penalty-inclusive total
  in the adapter, *not* in the captured event rows. Event-level
  `time_ms` is the raw wall-clock — penalties live in the adapter so
  the prototype (Phase 1) gets the clean number.

- **Reference recording emits one survived row per segment but the
  segment may span checkpoints.** Already handled by the existing
  capture pipeline — each segment is its own
  `arm`/`recorded_segment_time` pair, so per-segment emission
  naturally produces one `outcome='survived'` event per segment.

- **Existing tests fail wholesale because they construct `Attempt`
  directly.** Mitigation: keep the `Attempt` dataclass as an alias
  for `EpisodeView` during the refactor; remove only after the test
  suite is green.

## Verification (the don't-skip list)

Before merging Phase 0:

- `python -m pytest` green from the start of the session (baseline)
  and at the end. No skips. (See [[feedback_run_all_tests]],
  [[feedback_fix_preexisting_failures]].)
- `npx pyright python/` no new errors over the baseline.
- `cd frontend && npm run typecheck` green.
- Run the suite 15+ times to catch flakes
  ([[feedback_stress_test_flakes]]).
- Manually exercise the practice loop in `spinlab dashboard`, watch
  attempts table grow per-event in `sqlite3 spinlab.db "SELECT * FROM
  attempts ORDER BY id DESC LIMIT 10"`, confirm Recent Attempts UI
  still shows episode-shaped rows.

## After Phase 0

Phase 1 (vendor `segments_experiment` and call `fit_segment` per
attempt) is unblocked once Phase 0 ships and the event-level rows are
flowing. The data shape the prototype expects is exactly what Phase 0
produces — `(outcome, time_ms)` tuples in order.
