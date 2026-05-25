# Death-Aware Rolling Calculations — Design

Design spec from the 2026-05-24 brainstorming session. A new estimator that fits the
existing `Estimator` ABC, consumes event-level data, and emits death-aware outputs.
PoC-scoped — explicitly defers learning curves, asymptote projections, screen-aware
extensions, allocator scoring changes, and frontend visualization.

## Motivation

The current estimator trio (`kalman`, `rolling_mean`, `exp_decay`) is doing more than
it can defend. The Kalman model's practice-loop allocator picks badly in real
sessions; debugging it has become its own project. The richer V07 segment model
(`python/spinlab/_segments_v07/`, integrating in parallel) is a substantially more
honest Bayesian model but adds a JAX dependency, a separate fit channel, and
modeling complexity (10 latents per segment, learning curves with empirical-Bayes
pooled halflives, Laplace/NUTS fallback, PPC honesty gates).

This spec proposes a third path: a deliberately simple estimator that tracks the
data the player actually cares about — death rate, where deaths happen, where
completions land — using exponentially decayed rolling statistics. No learning
curve. No Bayesian apparatus. No asymptote projection. If it works well enough,
it becomes the default and the others retire. If it doesn't, it costs us a few
files and a minor ABC extension to delete.

The goal isn't "smarter than intuition" — it's **zero-decision practice**. The
practice-loop allocator hands the player the next segment, the player runs it,
and never has to think about which segment to load. Matching gut picks without
breaking flow is success.

## Principles

Carried forward from [`2026-04-01-model-output-v2-design.md`](2026-04-01-model-output-v2-design.md):

1. **No silent fallbacks.** Missing-data fields return `None`.
2. **No unexplained constants.** Every number is data-derived, a named declared
   param, or marked as a placeholder.
3. **Show honest data.** Wild values from few data points are preferable to
   hidden problems.
4. **Predict forward.** `expected_ms` answers "what will my next attempt look
   like?"

Plus one new principle specific to this model:

5. **Honest about deaths.** Don't collapse died and survived attempts into a
   single mean. Track them separately, expose the death rate, and let downstream
   consumers reason about expected time as `p_die × E[death] + (1-p_die) × E[completion]`
   when they need a single number.

## In scope (V1)

- New estimator `death_aware_rolling` registered alongside the existing three.
- Event-level input (`list[EventAttempt]`) accepted via an optional kwarg
  extension to the `Estimator` ABC.
- Exponentially-decayed rolling statistics over the most recent episodes.
- Death-time and completion-time distributions as full weighted sample arrays
  (capped at ~5×halflife per outcome).
- Populates the legacy `ModelOutput.total` and `ModelOutput.clean` fields so
  the model is a complete drop-in for the existing allocator and UI.
- New `DeathExtras` payload carried as an optional field on `ModelOutput`.
  Propagates to frontend via OpenAPI codegen.

## Out of scope (deferrals)

| Deferral | Why deferred | Where it lives if/when picked up |
|---|---|---|
| Population priors (`get_priors`) | V1 segments are independent; cross-segment pooling adds modeling complexity without proving the basic shape works first. | Override `get_priors` later if needed. |
| Death-curve frontend plot | The data is shipped in `DeathExtras` but rendering is its own design question. | Frontend feature, separate spec. |
| Death-aware allocator (scoring with `p_die`) | The PoC ships with the existing greedy allocator unchanged — it consumes `total.expected_ms` from the new model the same way it does from `rolling_mean`. Folding in `p_die` is a follow-up once we see real outputs. | New allocator class (`death_aware_greedy`) once a scoring formula is chosen. |
| Screen-awareness (per-screen breakdown) | Structural change (per-screen vs per-segment) and a different question (where vs when). Probably wants its own output type. | Separate model or a new `extras` variant when we have data warranting it. |
| Learning curve / asymptote / "expected gold after infinite practice" | Requires modeling a learning curve. We're deliberately not doing that — see motivation. | Future model or extension; not this one. |
| Plateau detection | Same as above. | Same as above. |
| Time-based decay (wall-clock days) | V1 uses attempt-count decay. Time-decay is more sophisticated but might over-discount data the player still retains in muscle memory. | Add a second declared param if needed; same math structure. |

## Architecture

Three additive changes:

1. **New estimator file: `python/spinlab/estimators/death_aware_rolling.py`.**
   Implements the `Estimator` ABC. Registered as `death_aware_rolling`.
2. **`Estimator` ABC extension.** Add `events: list[EventAttempt] | None = None`
   kwarg to `process_attempt`, `model_output`, and `rebuild_state`. Existing
   estimators ignore the new kwarg; default `None` keeps backward compatibility.
3. **`models.py` extension.** Add `DeathExtras` pydantic dataclass and
   `extras: DeathExtras | None = None` field on `ModelOutput`.

No DB schema changes. The new estimator reuses the existing `model_state` table
for its (minimal) JSON state.

## Data flow

```
Player completes an episode
  ↓
PracticeTiming / SpeedRunTiming / reference recorder writes EventAttempt rows
  ↓
Scheduler.process_attempt(estimator, segment_id, ...) :
  - Loads episode-shaped attempts (existing path) → all_attempts
  - Loads event rows via db.get_segment_event_rows(segment_id) → events
  - Calls estimator.process_attempt(state, new_attempt, all_attempts, events=events, params=params)
  ↓
DeathAwareRollingEstimator:
  - Groups events by episode_id, orders by closing-event id
  - Filters invalidated episodes
  - Assigns each episode a decay weight from its index
  - Computes weighted stats (death/completion sample arrays, p_die, means)
  - Returns ModelOutput(total, clean, extras=DeathExtras(...))
  ↓
Persisted as model_state.state_json (n_episodes only; stats recompute from events)
  ↓
API surfaces ModelOutput via state_builder; OpenAPI codegen propagates DeathExtras
to frontend/src/api-types.ts on next `npm run gen-types`
```

The scheduler's event-loading step is a one-line addition. The estimator never
talks to the DB directly — it receives the event list.

## Math

The model has two granularities and is explicit about which question is being
answered at which level:

- **Life-level** — each `EventAttempt` row is one life. A died event is a
  life that ended in death; a survived event is a life that completed the
  segment. Used for `death_samples`, `completion_samples`, and their means.
- **Episode-level** — each `episode_id` is one player attempt. May contain
  multiple lives. Used for `p_die`, `n_attempts_effective`, and
  `total.expected_ms`.

### Inputs

- `events: list[EventAttempt]` — chronologically ordered (by event id).
- `halflife: int` — declared param. Default = 20 episodes. Range [1, 200].

### Episode grouping

Group events by `episode_id`. Within each episode, events are already
chronologically ordered. Filter out episodes where any constituent event has
`invalidated=True` — the event table's `invalidated` flag is episode-level by
design (see [`Database.set_attempt_invalidated`](../../../python/spinlab/db/attempts.py));
any one event being marked is sufficient grounds to drop the episode.

Order surviving episodes by their closing-event `id` (chronological). Let
`N_episodes` = length of this list.

For each episode, derive:
- `episode_outcome` — `"completed"` if any event is `survived`; else `"died"`.
- `episode_total_time_ms` — only defined when `episode_outcome == "completed"`.
  Equal to `sum(event.time_ms for event in episode) + DEFAULT_DEATH_PENALTY_MS × deaths_in_episode`,
  which matches the production roll-up in [`_roll_up_episode`](../../../python/spinlab/db/attempts.py).
- `had_any_death` — `True` if any event in the episode is `died`.

### Weights

```
episode_index_i ∈ [0, N_episodes - 1]   # 0 = oldest, N-1 = most recent
weight_i = 2 ** (-(N_episodes - 1 - episode_index_i) / halflife)
```

The most-recent episode gets weight 1.0; an episode `halflife` episodes ago
gets weight 0.5; one `5 × halflife` episodes ago gets weight ≈ 0.031. Episodes
beyond ~5×halflife contribute weight < 0.001 — drop them from the working set
before computing any stats (the truncation is a perf optimization; outputs are
unchanged within float precision).

All events within an episode inherit that episode's weight. Halflife counts
*episodes* (the player's mental unit of practice), not events.

### Episode-level aggregates

```
n_attempts_effective    = Σ weight_i  over all episodes
n_died_effective        = Σ weight_i  over episodes where had_any_death == True
n_completed_effective   = Σ weight_i  over episodes where episode_outcome == "completed"
p_die                   = n_died_effective / n_attempts_effective
```

**`p_die` semantics:** "fraction of recent attempts that contained any death."
An episode that died twice and then completed counts as a "death attempt" —
that matches how a player thinks about it ("I died on that segment two runs
ago, even though I made it eventually"). `n_died_effective` and
`n_completed_effective` are **not** complementary (an episode can have both
deaths and a completion); their sum can exceed `n_attempts_effective`.

```
E[episode_total]    = Σ (weight_i × episode_total_time_ms_i) / Σ weight_i
                      over episodes where episode_outcome == "completed"
```

`E[episode_total]` is the weighted mean episode-total time, computed only over
completed episodes (incomplete episodes have no meaningful total). This is the
direct successor to `rolling_mean`'s mean — same quantity, decay-weighted.

### Life-level aggregates

For per-event samples (each life is one sample):

```
death_samples      = [(event.time_ms, episode_weight) for each died event in the window]
completion_samples = [(event.time_ms, episode_weight) for each survived event in the window]
```

**Multi-death episodes contribute multiple death samples** — each died event
is one sample with its own `time_ms` (time since last respawn or arm) and the
episode's weight. A single episode with 3 deaths and then a survival contributes
3 death samples and 1 completion sample, all sharing the same episode weight.

```
E[death_time]      = Σ (w × t) / Σ w   over death_samples       # None if empty
E[completion_time] = Σ (w × t) / Σ w   over completion_samples  # None if empty
```

### Legacy ModelOutput fields

The new model populates `ModelOutput.total` and `ModelOutput.clean` so it can
serve as a drop-in for the existing greedy allocator and UI.

```
total.expected_ms    = E[episode_total]
                       (decayed weighted mean of completed-episode totals;
                        the simplest defensible "what will my next attempt
                        take" answer — episode-level granularity matches the
                        question)
total.ms_per_attempt = (weighted_mean_first_half − weighted_mean_second_half) / half_n
                       over completed-episode totals in chronological order,
                       with episode weights applied to each half's mean.
                       Same crude-trend shape as rolling_mean's slope, with
                       weights threaded through.
total.floor_ms       = min episode_total_time_ms across ALL completed episodes,
                       not just the window (best-ever is sticky info)

clean.expected_ms    = E[completion_time]
                       (same value as DeathExtras.expected_completion_time_ms;
                        kept on both surfaces so each is self-contained)
clean.ms_per_attempt = same weighted slope formula, applied to completion_samples
clean.floor_ms       = min survived event time_ms across ALL survived events,
                       not just the window
```

`DEFAULT_DEATH_PENALTY_MS` is the existing constant in `models.py` (= 3200ms),
folded into `episode_total_time_ms` exactly as the production roll-up does.

**Why not the formula `p_die × (E[death]+penalty) + (1-p_die) × E[completion]`?**
That formula assumes one outcome per attempt (die OR complete, not both). For
multi-life episodes that's wrong. The expected-attempt-time involves a geometric
sum over lives until completion, with each life's outcome independent — too much
modeling for V1, and unnecessary because `E[episode_total]` is the direct
empirical answer to the same question.

### Edge cases

| Case | Behavior |
|---|---|
| `events` empty or all invalidated | Return `ModelOutput(total=all-None, clean=all-None, extras=None)`. |
| Zero completed episodes | `total.expected_ms = None`. `total.floor_ms = None`. `clean.expected_ms = None` (no survived events ⇒ no completion samples). `p_die ≈ 1`. `death_samples` populated; `completion_samples = []`. |
| Zero died episodes | `p_die = 0`. `death_samples = []`. `total.expected_ms = E[episode_total]`. `clean.expected_ms = E[completion_time]`. |
| Single episode, completed, no deaths | `n_attempts = 1`, `p_die = 0`, single-sample completion. `ms_per_attempt = None` (no slope across a single point). |
| Halflife outside [1, 200] | `declared_params` bounds enforce this; out-of-bounds raises on apply, per the `rolling_mean` pattern. |
| Sample arrays exceed 5×halflife per outcome | Truncate to most-recent 5×halflife on each side before serialization. |

## State management

`state_json` is intentionally minimal:

```python
@dataclass
class DeathAwareRollingState(EstimatorState):
    n_completed: int   # inherited bookkeeping
    n_attempts: int    # inherited bookkeeping
    # No per-segment stats cached — recomputed from events every call.
```

`process_attempt`, `model_output`, and `rebuild_state` all share one
recompute-from-events code path. With N bounded by 5×halflife, recompute cost
is O(N) per call where N ≤ 1000 — microseconds. Mirrors the `rolling_mean`
pattern; avoids the incremental-update complexity that bit Kalman.

`halflife` lives in `declared_params` and persists in the existing estimator
config storage. Not stored in `state_json` — it's a knob, not state.

```python
def declared_params(self) -> list[ParamDef]:
    return [
        ParamDef(
            "halflife", "Halflife (episodes)",
            default=20.0, min_val=1.0, max_val=200.0, step=1.0,
            description=(
                "Number of episodes for the rolling weight to halve. "
                "20 ≈ recent month of casual practice; lower = more "
                "responsive to recent changes, higher = more stable."
            ),
        ),
    ]
```

## Integration touch points

| File | Change | Size |
|---|---|---|
| [`python/spinlab/models.py`](../../../python/spinlab/models.py) | Add `DeathExtras` pydantic dataclass; add `extras: DeathExtras \| None = None` to `ModelOutput`; update `ModelOutput.to_dict()` / `from_dict()`. | ~30 lines |
| [`python/spinlab/estimators/__init__.py`](../../../python/spinlab/estimators/__init__.py) | Add `events: list[EventAttempt] \| None = None` kwarg to `Estimator.process_attempt` / `model_output` / `rebuild_state` signatures; import `death_aware_rolling` in `_register_all`. | ~10 lines |
| [`python/spinlab/estimators/death_aware_rolling.py`](../../../python/spinlab/estimators/death_aware_rolling.py) | NEW. Full implementation. | ~200 lines |
| [`python/spinlab/scheduler.py`](../../../python/spinlab/scheduler.py) | When estimator processes an attempt, fetch events via `db.get_segment_event_rows(segment_id)` and pass via kwarg. Wrap in `events=events if estimator can consume them else None` — check via `inspect` or simpler approach (estimators ignoring the kwarg costs nothing). | ~10 lines |
| [`python/spinlab/api_schemas.py`](../../../python/spinlab/api_schemas.py) | Re-export `DeathExtras` from `models.py` for OpenAPI. | ~3 lines |
| [`frontend/src/api-types.ts`](../../../frontend/src/api-types.ts) | Auto-regenerated by `npm run gen-types`. No manual edits. | (generated) |
| [`tests/unit/test_death_aware_rolling.py`](../../../tests/unit/test_death_aware_rolling.py) | NEW. Mirrors `test_rolling_mean.py` shape. | ~250 lines |
| [`tests/integration/`](../../../tests/integration/) | One integration test: multi-death practice flow → estimator outputs sensible (`p_die ∈ [0,1]`, `death_samples` populated, totals positive). | ~80 lines |

Allocator: **PoC ships with existing greedy allocator unchanged.** Death-aware
scoring is a follow-up.

UI: **PoC ships with no UI changes.** `DeathExtras` flows through the API but
isn't rendered. Inspect via the existing Model tab's raw JSON view.

## DeathExtras shape

```python
@pydantic_dataclass(config=ConfigDict(extra="allow"))
class DeathExtras:
    """Death-aware fields published by death_aware_rolling.

    Carried on ModelOutput.extras when the active estimator is death-aware.
    Legacy estimators leave ModelOutput.extras = None.

    n_died_effective and n_completed_effective are NOT complementary — an
    episode can both contain deaths and end in a completion. Their sum can
    exceed n_attempts_effective.
    """
    halflife_attempts: int
    n_attempts_effective: float       # decayed sum of episode weights (denominator for p_die)
    n_died_effective: float           # decayed weight over episodes with ANY died event
    n_completed_effective: float      # decayed weight over episodes whose last event is survived
    p_die: float                      # n_died_effective / n_attempts_effective; "fraction of attempts with any death"
    death_samples: list[tuple[int, float]]      # life-level (time_ms, weight) per died event; capped at ~5*halflife
    completion_samples: list[tuple[int, float]] # life-level (time_ms, weight) per survived event; capped at ~5*halflife
    expected_death_time_ms: float | None        # weighted mean of death_samples; None when empty
    expected_completion_time_ms: float | None   # weighted mean of completion_samples; None when empty
```

`death_samples` and `completion_samples` ship the raw weighted data so the
frontend can render any shape (KDE, histogram, multimodal cluster) without
making assumptions about distribution form. SMW death-time distributions are
likely multimodal (deaths cluster at hard parts), so a Gaussian summary would
lie about where deaths actually happen.

Storage cost: ~100–300 samples per distribution, ~10–20 bytes per JSON sample
= 1–3 KB per segment. SMW's ~60 segments = under 200 KB total. Trivial.

## Testing

### Unit tests (`tests/unit/test_death_aware_rolling.py`)

Mirror `tests/unit/test_rolling_mean.py` shape:

- **Empty events** → all-None output, `extras=None`.
- **Single died event** → `p_die=1.0`, `death_samples=[(t, 1.0)]`,
  `clean.expected_ms=None`.
- **Single survived event** → `p_die=0.0`, `completion_samples=[(t, 1.0)]`.
- **Mixed deaths and completions** → hand-computed weighted means match outputs
  to float tolerance.
- **Decay weighting** — synthesize 100 episodes with known times. Compare
  weighted mean to a NumPy reference implementation. Verify halflife=10 gives
  more weight to recent samples than halflife=100.
- **Multi-death episode** — one episode with 3 deaths + 1 survival contributes
  3 weighted death samples and 1 weighted completion sample at the same weight.
  Also: this episode counts as ONE attempt with `had_any_death=True`, so it
  contributes to both `n_died_effective` AND `n_completed_effective` (an
  episode can be both — the counts are not complementary).
- **`n_died + n_completed > n_attempts` is allowed** — assert this holds in
  the multi-death-then-survive case.
- **Invalidated episode filtering** — invalidated episodes do not appear in
  samples or counts.
- **Sample truncation** — synthesize 1000 episodes with halflife=10; assert
  samples capped at ~50 (5×halflife) per outcome.
- **Halflife knob** — declared_params surfaces halflife with the documented
  bounds. Out-of-bounds raises (mirrors rolling_mean).
- **`total.expected_ms` = `E[episode_total]`** — hand-construct events where
  episode totals are known; verify `total.expected_ms` matches the weighted
  mean of completed-episode totals. Verify `total.expected_ms = None` when
  there are zero completed episodes.
- **`total.floor_ms` is min episode_total across ALL completed**, not just the
  window — synthesize a great-but-old completed episode plus mediocre recent
  ones; verify floor reflects the old best.
- **`clean.floor_ms` is min survived event time_ms across ALL survived events**,
  not just the window.
- **State serialization** round-trips through `to_dict` / `from_dict`.
- **Registry** — `get_estimator("death_aware_rolling")` returns an instance.
- **Drop-in compat** — output shape passes the same shape assertions as
  rolling_mean's tests (both Estimates present; legacy fields populated).

### Integration test (`tests/integration/test_death_aware_rolling_e2e.py`)

One test that exercises the full pipeline:

1. Set the active estimator to `death_aware_rolling` for a freshly-created game.
2. Drive a practice session with multiple deaths per episode (via the RA
   poke harness, mirroring existing patterns).
3. After several episodes, assert `ModelOutput.extras` is non-None,
   `p_die ∈ [0, 1]`, `total.expected_ms > 0`, and `death_samples` has at least
   one entry.
4. Verify the value persists across a process restart (state survives via the
   `model_state` table).

No emulator-specific assertions about precise times — this is a pipeline
plumbing test, not a model correctness test.

### Existing estimator tests

No changes needed. The ABC kwarg is opt-in; legacy estimators continue to
ignore `events`. Re-run the full unit suite as a smoke check after the ABC
extension lands.

## Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| Halflife=20 default is wrong for SMW | It's a declared param surfaced in the estimator config UI. Andrew can tune per session. If a clear "right" value emerges from data, lock it in then. |
| Multi-modal death-time distributions don't render usefully without UI work | Out of scope for V1. Data is shipped in `DeathExtras`; rendering is its own design. |
| Death-aware allocator never gets built and the new model coexists indefinitely with greedy-on-rolling-mean | Acceptable. If the new model's `total.expected_ms` is no worse than rolling_mean's, the greedy allocator still picks reasonably. The allocator improvement is a separate decision. |
| ABC change ripples through every existing estimator's tests | The new kwarg is optional with a default of `None`. Existing tests are unaffected; new tests assert the new behavior. |
| Frontend gets a new optional field it doesn't render | Acceptable. The OpenAPI codegen handles the type. Nothing visually changes until a renderer ships. |
| If death-aware turns out wrong, the cleanup is invasive | False — the cleanup is: delete one estimator file, delete one optional kwarg from the ABC, delete one optional field on ModelOutput. ~5 minutes of work. |

## Success criteria

This PoC succeeds if, after a few practice sessions with the model active:

1. `total.expected_ms` from `death_aware_rolling` is qualitatively comparable
   to `rolling_mean`'s — the practice loop doesn't get worse.
2. The `DeathExtras` payload is populated with sensible numbers (death times
   look like times you actually died at; `p_die` matches your eyeball estimate).
3. The greedy allocator's segment picks are good enough to enable "zero-decision
   practice" — Andrew loads what it picks without overriding.

If all three hold, the next step is the death-aware allocator (separate spec)
and eventual retirement of the legacy estimators. If any fails, the model
goes back to the drawing board with concrete data on what went wrong.
