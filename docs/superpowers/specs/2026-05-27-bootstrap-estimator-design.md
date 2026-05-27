# Bootstrap-Resample Estimator — Design Note

**Status:** Spec — for the implementation plan to follow.
**Context:** Second of three branches in the death-distribution / cold-hot model-tab arc. Branch 1 (`is_hot` column + per-attempt cold/hot tagging) landed 2026-05-27. Branch 3 (hazard-rate visualization with cold-only filter) follows this one.

## Goal

Add a new `bootstrap_resample` estimator that produces `ModelOutput` for the same per-segment scheduling decisions as `death_aware_rolling`, but computed by resampling whole episodes from recent history instead of plugging weighted means into the geometric expected-time formula. The two estimators sit side-by-side in the existing estimator dropdown so they can be A/B-compared on real data.

## Why bootstrap

`death_aware_rolling` computes `E[attempt] = (p/(1-p)) · (E[death] + penalty) + E[completion]` from weighted aggregates. That formula assumes lives within an episode are i.i.d. Bernoulli trials. **The data violates this assumption.** Observed `p(die|life)` is meaningfully larger than `p(die|attempt)` on most segments, which means deaths cluster within episodes ("when you start dying, you keep dying"). The geometric formula is therefore systematically biased — by an unknown amount that depends on how clustered the deaths are.

Bootstrap sidesteps the assumption entirely. To estimate the expected attempt time, sample whole episodes from recent history (with the existing exponential decay as the sampling weight), sum each episode's per-life times, average across N draws. No i.i.d. claim, no parametric model, no hidden bias — just "what would your next attempt look like if it were drawn from the same distribution as your recent attempts."

When the bootstrap mean and the analytic mean agree, the geometric model is well-calibrated and either is fine. When they disagree, the divergence is itself the diagnostic — it tells you how much the clustering is costing you. Side-by-side in the UI makes this visible.

## Scope (this branch only)

**In scope:**

- New estimator class `BootstrapResampleEstimator` registered via `@register_estimator`, name `bootstrap_resample`, display name something like "Bootstrap (Monte Carlo)".
- Populates `ModelOutput.total.expected_ms` with the mean of N bootstrap-resampled episode times.
- Populates `ModelOutput.clean.expected_ms` with the mean of bootstrap-sampled completion times (last life's `time_ms` from completed episodes).
- Populates `floor_ms` for both `total` and `clean` the same way `death_aware_rolling` does (min over completed-episode totals; min over survived-event times). Reuse logic if it's already factored, else mirror it.
- Populates `ms_per_attempt` for both with the slope estimator used by `death_aware_rolling` (`_weighted_half_split_slope` over `completion_samples`). Don't reinvent.
- **Filters to cold attempts only** (`is_hot=0`) when constructing the resampling pool. Cold dominates today and is the right default for the scheduler's "next practice load" question. Hot data exists but is rare and arguably a different population (see deferred section).
- One declared param: `n_samples: int` (default ~1000; needs a sanity floor like 100 and a ceiling like 10000 — pick reasonable bounds). This is the bootstrap draw count.
- Reuse the existing `_group_into_episodes` and decay-weight machinery from `death_aware_rolling` — extract to a shared helper if it isn't one already, or import directly. **Don't duplicate the episode-grouping logic.**
- Same TDD discipline: red tests first, smallest implementation, full suite stays green.

**Out of scope (deferred — capture as backlog entries during the plan):**

- **Percentiles / distribution exposure.** Bootstrap naturally produces a distribution (the N sampled totals), not just a mean. The user wants this eventually — specifically, "run the bootstrap with different reweighting biases and pick the one that best predicts future runs" as a way to estimate learning. For this branch, expose only the mean. The percentile machinery comes later when the learning-estimation use case is wired up.
- **Hot data inclusion / hot↔cold transfer.** Cold-only is the right starting point. The future story (pooling hot+cold with a learnable transfer weight) is in `docs/BACKLOG.md` under "Cold/hot follow-ups."
- **Bias-as-learning meta-loop.** Running bootstrap with multiple reweighting schemes and selecting by held-out prediction — interesting but premature. Park.
- **Replacing `death_aware_rolling`.** Not the goal. Bootstrap is additive; the user picks which estimator to use via the existing dropdown.
- **Visualization of the bootstrap distribution.** That's branch 3's territory (hazard plot, possibly bootstrap-distribution overlay too). This branch produces the numbers; branch 3 draws them.

## Why same `ModelOutput` shape

The estimator dropdown and scheduler both consume `ModelOutput`. Producing the same shape means:

- Zero UI changes to surface bootstrap as an option.
- Direct A/B by switching the dropdown.
- Scheduler can use bootstrap as a drop-in replacement for any segment.
- No special-casing downstream.

The one place this might pinch: `extras: DeathExtras | None` on `ModelOutput`. `death_aware_rolling` populates it with the death/completion samples for the histogram. Bootstrap doesn't naturally produce a "death samples" payload (it samples whole episodes). Two options: leave `extras=None` on the bootstrap output (the death-distribution panel hides; fine for now), or populate `extras` with the cold-filtered samples computed the same way `death_aware_rolling` does (so the histogram still renders). Pick during planning — leaning toward `extras=None` for simplicity, with a backlog note to add it if the user misses the histogram on bootstrap segments.

## Cold-only filter

`is_hot` lives on the `attempts` table as of migration 0007. The estimator's `events: list[EventAttempt] | None` arg (per the `Estimator` ABC, already plumbed) carries the `is_hot` field as of branch 1's Task 2 fix to `_events_from_rows`. So the filter is just: `cold_events = [e for e in events if not e.is_hot]` before grouping into episodes. Drop hot lives entirely — don't include their parent episodes either (an episode with one hot life and three cold lives shouldn't half-count). Simplest rule: keep an episode iff every one of its events is cold. Document why in a comment.

## Implementation hints

- Existing estimator at [python/spinlab/estimators/death_aware_rolling.py](../../python/spinlab/estimators/death_aware_rolling.py) is the closest pattern. Follow its file structure: declared params + state class + estimator class + math helpers extracted as module-level functions.
- The decay weight helper `_compute_weights(n_episodes, halflife)` and `_group_into_episodes(events)` are good extraction candidates — move them to a shared `python/spinlab/estimators/_episode_helpers.py` (or similar) and import from both. Don't duplicate. (If extraction is too invasive, importing directly from `death_aware_rolling` is acceptable for this branch with a `_` prefix to signal "package-internal.")
- The bootstrap RNG should be seedable for testing. Take an optional `seed: int | None = None` on construction or as a declared param. Default None = nondeterministic.
- For the resampling pool, after filtering to cold episodes and applying the decay weight, sample N episodes with `random.choices(episodes, weights=weights, k=N)`. That's the whole algorithm.
- Episode time = sum of `event.time_ms` for events in the episode + `DEFAULT_DEATH_PENALTY_MS × n_deaths`. Already computed by the legacy roll-up adapter (`_roll_up_episode` in `python/spinlab/db/attempts.py`); reuse rather than recompute.

## Test coverage

- Bootstrap of a single-completion-only history returns that completion time (no variance).
- Bootstrap of a hot-only history returns `None`/empty output (cold filter removed everything).
- Bootstrap with `n_samples=1000` and a known mixed-outcome history produces a mean within tight tolerance of the analytic geometric mean — for cases where the i.i.d. assumption actually holds (e.g., no clustering). Use a seeded RNG.
- Bootstrap on a clustered-deaths history produces a mean **higher** than the analytic geometric mean by a measurable amount (this is the bias the bootstrap is designed to expose). Document the expected direction in the test.
- Registry: importing the module registers the estimator under `bootstrap_resample`.
- ModelOutput shape matches `death_aware_rolling` (both `total` and `clean` populated; `extras` either None or populated per the decision above).

## Sequencing

After branch 1 merged. Before branch 3 (hazard plot). Independent of any other in-flight work.

## Open questions for the planning session

1. Should `extras` be `None` or populated with the same cold-filtered samples `death_aware_rolling` produces? Recommend `None` with a backlog entry.
2. Where do the shared episode-grouping/decay-weight helpers live? Recommend extracting to `_episode_helpers.py` for this branch; defer if too invasive.
3. Does `bootstrap_resample` get exposed in the estimator dropdown by default, or behind a flag? Recommend default-on — the whole point is A/B comparison.
