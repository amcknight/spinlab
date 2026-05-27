# SpinLab Backlog

Open follow-ups, ideas, and known tech debt. Items here are not tracked in any external system. Severity tags: **[S]** small (under a day), **[M]** medium (1-3 days), **[L]** large (>3 days).

When an item ships, delete it from this file rather than checking it off — the backlog is the live to-do list, not a changelog. Use git history for the historical record.

## High-priority follow-ups

- **[L] RetroArch migration — finish to parity.** Backend works for reference + practice in unit tests and basic live testing; cold-fill on cp-respawn hacks and second-death practice reload have provisional fixes that need field verification. Phase E (BSV input recording + replay) is not started. Detailed status: `docs/retroarch-migration/status.md`. Path to parity: `docs/retroarch-migration/path-to-parity.md`.
- **[M] Long-run / load test.** No test exercises >100 segments or multi-GB accumulation across many sessions. A test that simulates 1000 segments across 20 sessions then runs recovery would catch scaling regressions early. The whole point of the multi-session work was to support 50-hour runs.
- **[M] Playwright crash-and-resume smoke test.** Scaffold exists at `tests/integration/test_multi_session_smoke.py` (skipped). Extend the `test_frontend_smoke.py` fixture chain to support process restart against a shared DB file. Python-level crash test (`test_crash_recovery.py`) covers the data layer; this is purely UI confidence.

## Architecture

- **[M] Consolidate `paused_run_id` and `recorder.capture_run_id` state.** `ReferenceController` has two state fields managed by separate methods. The `_assert_run_state_invariant` helper guards against drift, but the next refactor that touches state transitions is at risk. Either consolidate into a single state object or expand the invariant assertions.
- **[M] `is_primary` auto-promotion on segment delete.** Today: deleting the primary segment for a geography silently drops it from practice scheduling. Now that segments carry `capture_session_id`, the natural behavior is to auto-promote the most-recent active sibling.

## Efficiency

- **[M] `recorder._close_segment` ordinal is O(n).** Each close runs `SELECT COUNT(*) FROM segments WHERE reference_id = ?`. With many segments this is O(n²) over a full run. Track in-memory per session — ordinal isn't load-bearing for correctness, only display ordering, so a slight desync after crash is acceptable.
- **[S] `get_paused_state` runs 2 DB queries per SSE event.** Called from `state_builder` on every backend event. For high-event-rate flows (rapid segment closes, deaths), this is real overhead. Cache and invalidate only on lifecycle transitions, or fold into one query.
- **[S] `_compute_is_primary` runs a query per segment close.** N segments per session = N queries. Could batch at session-end with one query that updates all segments captured in the session. Or precompute primary-by-geography at start of session.
- **[M] Spinrec file accumulation.** Discarded runs clean up; finalized runs do not. Spinrecs accumulate forever in `data/<game>/rec/`. Add a per-reference "delete spinrec" action, or an automatic sweep ("references older than N months without practice activity"). Probably matters at the 1+ year mark.
- **[S] `recover_paused_capture_run` N+1 on draft cleanup.** When multiple stranded drafts exist, `hard_delete_capture_run` is called per row. Bulk delete possible but rarely matters (multiple drafts is the failure case, not the norm).

## RetroArch backend tech debt

- **[M] No live test for SpeedRunTiming under RA.** Unit tests pass; never exercised in a real session. Speed-run mode is the riskiest untested code path in the new backend.
- **[S] Anonymous state-key resolver leaks abstraction.** `StateIO.resolve_event_path` returns paths keyed by `entrance_<level>_<room>` and `cp_<level>_<ord>_hot` for events whose true `segment_id` isn't known yet; F-live does the bridging downstream. Cleaner: orchestrator looks up segment_id before calling resolve.
- **[S] `config.ra_game_basename` is now informational only.** Orchestrator overrides it from `GET_STATUS` at connect; keeping the field invites confusion. Either remove it or document it as "ignored — auto-detected at connect."

## Emulator-test isolation (from 2026-05-11 test audit)

- **[M] Cross-test WRAM state leaks between scenarios under a shared harness.** Today's two-harness design (`ra_harness` + `ra_harness_love_yourself`) provides isolation by-architecture (separate RA processes). Merging them surfaces a real leak: scenarios zero only the 11 `ADDR_MAP` bytes, but the ROM writes to other WRAM during `FRAMEADVANCE`. Fix: clean-boot save-state primitive — `RAHarness.launch` captures a "paused on title" state once; `run_scenario` loads it before each scenario. Bullet-proof byte-identical reset. Opens the door to xdist parallelism (a true isolation primitive makes per-worker harnesses safe).
- **[M] Multi-ROM detector coverage.** `cp_entrance` ($1B403) is a custom-ASM-style checkpoint that only patched hacks (Toothpaste-style) populate; midway-tape checkpoints work everywhere. Testing exclusively on Love Yourself means the `cp_entrance` branch in `predicates.check_checkpoint_hit` only sees synthetic poke values, never real ROM behavior. A future pass should declare which detector features each harness's ROM exercises and route tests accordingly.
- **[S] `routes/system._launch_retroarch` hardcodes core path.** `C:\RetroArch-Win64\cores\snes9x_libretro.dll` is in the source. Surface as config so non-Windows / non-snes9x users can run.

## Test gaps

- **[M] Mutation-test-worthy assertions.** Some new tests would not catch silent regressions. Example: `test_save_and_finish_seeds_attempts_and_finalizes` does not fail if you delete the `set_active_capture_run` call. One pass: for each new test, ask "would this fail if I deleted line X of the implementation?"
- **[S] Pyright cleanup sweep.** Several pre-existing `pyright` errors: `cold_fill.py` (`str | None` for `state_path`), `recorder.py` (×2, `str` passed where `EndpointType` expected), `reference.py` (`str | None` for fill-gap `state_path`), `kalman.py` (×4, `EstimatorState` attribute access). Each is small in isolation; one focused PR could clean them all.
- **[S] Practice attempt observed-conditions.** `practice.py` does not populate `observed_start_conditions` / `observed_end_conditions` on attempts (the schema columns exist; the code path just leaves them empty). Either persist them so condition-aware estimators can use them, or remove the columns.

## UX polish

- **[S] "Session N of M" indicator during recording.** During a long multi-session run, the user may forget which session they're in. The recording panel could show "Recording — Session 3 — N segments captured this run".
- **[M] Save & Finish dialog UX.** Currently inline `<input>` near the buttons. A modal "Name this run" prompt on click is more deliberate, matches typical save-dialog convention, and prevents accidental saves with the default placeholder name.
- **[S] Visual differentiation of paused vs recording.** Both states use similar styling. A clearer cue — "paused" color/icon vs "active" color — helps the user instantly know the state.
- **[S] Per-segment delete confirmation.** Already has confirm but it's generic. Including segment description + capture time in the prompt makes it less ambiguous what's being deleted.
- **[S] Pre-start pause for hot starts.** A pre-start pause that shows the game paused/greyed with the buttons-to-hold prompt before starting. Helps tricky hot starts.
- **[M] Practice-mode time should count retry time.** Practice time should include time the user would have spent retrying in a real run. Needs reference-run between-gameplay time to compute.
- **[M] Empty per-session segment count rendering.** Decide on em-dash vs `0` for the per-session count column when a session captured no segments.

## Modeling

- **Find The Model.** Replace SM-2-style scheduling with a domain model that captures speedrun practice properly. Could simplify a lot if found.
- **Bayesian uncertainty and priors.** Build a new estimator generation with full Bayesian uncertainty tracking and population priors.
- **Backfill and tune estimator hyperparameters.** Once enough run data exists, derive priors and per-game tunings rather than picking constants by feel.
- **Room and other consistent subsections.** Support segments that aren't bounded by checkpoints — sub-level structure that's still routinely repeatable.
- **Merging and splitting of segments.** UI + DB support for combining adjacent segments or splitting one in two without losing attempt history.
- **Run library for testing.** A small library of real runs (including gnarly weird ones) recorded and replayable, used to seed estimators and exercise new functionality.
- **Critical path focus.** Just work on critical-path stuff to get real value for a simple run. (Periodic reminder, not a task.)
- See `docs/model-improvements-spec.md` for the per-estimator improvement plan (Phases 2-4 are open).

### Death-aware-rolling follow-ups (deferred from the 2026-05-24 PoC)

- **[M] Death-aware allocator.** New allocator class (`death_aware_greedy`) that folds `p_die_per_life`/`expected_death_time_ms` into the scoring formula. The current greedy allocator consumes only `total.expected_ms`, so it doesn't currently differentiate "high p_die" from "low p_die at the same expected time." Needs a real session of data to see the per-segment `p_die` distribution before picking the scoring formula.
- **[M] Frontend death-curve plot.** Render `DeathExtras.death_samples` (weighted `(time_ms, weight)` points) as a KDE or histogram per segment in the Model tab. Data is already on the wire via the OpenAPI codegen; consumer needs to opt in. Likely also surface `expected_death_time_ms` and `p_die_per_attempt` as headline stats next to the existing `expected_ms`.
- **[S] Population priors for `death_aware_rolling`.** Override `get_priors` once we have ≥5 segments with ≥20 attempts each. Useful values to pool: typical `p_die_per_life` per game, typical `expected_completion_time_ms` shape per level. Currently `get_priors` returns `{}` and cold-start segments get nothing from siblings.
- **[L] Screen-awareness.** Per-screen death-rate and timing breakdown. Structural change — probably warrants a new `extras` variant or a separate output type, since the question shifts from "when do I die?" to "where do I die?". Wait until at least one real session shows whether per-screen context would meaningfully change practice loop or stat displays.
- **[L] Learning-curve / asymptote projections.** "Expected gold after infinite practice", "expected practice before WR", "expected time saved by 1 more rep." Requires fitting a parametric learning curve (exponential decay or power law) to the rolling-min or low quantile of the completion-time series. Deferred from this PoC because we wanted to validate the simpler decayed-mean + geometric-formula model first.
- **[S] Spec wording fix — sample truncation granularity.** The 2026-05-24 spec says `death_samples`/`completion_samples` are "capped at ~5×halflife per outcome", but the implementation truncates per *episode* (in `_compute_aggregates`), so multi-death episodes can push `death_samples` higher than the per-outcome budget. Behavior is fine (storage stays bounded by ~5×halflife × deaths-per-episode-in-window); spec text should be updated to say "~5×halflife episodes" to match the code.

### Cold/hot follow-ups (deferred from the 2026-05-26 is_hot landing)

- **[M] Hyper-play hot data collection.** Hyper-play currently emits cold-only attempts because `hyper_play._record_attempt` writes via the legacy `log_attempt` path (one row per sub-segment with `deaths=0`). To gather hot data, refactor to detect carry-over from a completed prior sub-segment and tag the first attempt of the next sub-segment with `is_hot=True`. Mirrors the reference recorder's logic. Blocked on no urgent need; revisit once the cold/hot modeling story matures and hot data starts to matter for the scheduler.
- **[L] Hot↔cold transfer modeling.** Treat cold and hot attempts as partially-pooled populations rather than fully independent. Empirically, improvement on cold transfers ~80% to hot (and vice versa), but the death-aware rolling estimator and future bootstrap estimator currently treat them as disjoint. A learnable transfer weight derived from per-segment hot/cold sample pairs would let cold data inform hot estimates when hot data is sparse. Defer until meaningful hot sample sizes exist to validate against.
- **[S] Reference-run hot/cold backfill edge cases.** Migration 0007's backfill marks the first attempt of an episode HOT iff the immediately prior attempt in the same `capture_run` survived from a different episode. Paused-and-resumed runs (where a `capture_session` boundary sits inside a `capture_run`) can mislabel: a fresh-resume attempt could look like a carry-over if the prior session ended on a survival. Acceptable today (rare and disclosed in `docs/GLOSSARY.md`), but worth revisiting if hot data quality becomes important — a derive script keyed off `capture_session_id` could re-tag historical rows correctly.

### Bootstrap-resample follow-ups (deferred from the 2026-05-27 branch-2 landing)

- **[S] Bootstrap distribution exposure.** Bootstrap naturally produces a full distribution (the N sampled totals), but branch 2 only surfaces the mean. Persist the samples in a new extras payload (`BootstrapExtras` with `total_samples: list[float]`) once the user-facing histogram in branch 3 needs them. Cheapest landing: expose at the route level only, no DB persistence.
- **[S] Death-distribution panel for bootstrap segments.** The bootstrap estimator currently sets `extras=None`, so the `DeathExtras`-driven death-time histogram hides on segments using bootstrap. Either (a) compute the cold-filtered samples and populate `DeathExtras` alongside the bootstrap result, or (b) add a bootstrap-specific panel. Revisit if the missing histogram is annoying in practice.
- **[M] Bias-as-learning meta-loop.** Run the bootstrap with multiple reweighting schemes (different halflives or alternative decay shapes) and select the one that best predicts held-out future episodes. The picked weighting then *is* the player's learning rate estimate. Premature to implement; document the idea so it isn't lost.
- **[S] Bootstrap-vs-geometric divergence note.** The original branch-2 spec said "bootstrap mean > geometric mean on clustered deaths." The implementation analysis showed the direction is data-dependent: with aborted episodes in the pool, bootstrap is LOWER (geometric pretends every attempt completes-by-attrition); with all-completed clustered-death data, the two often agree exactly because `p/(1-p)` over the lives-weighted marginal recovers `E[deaths_per_attempt]` by construction. The meaningful divergence between the two estimators is in the FULL distribution (variance, tail, multi-modality), not the mean. Revisit the spec wording when branch 3's distribution overlay lands.

## Documentation

- **[S] Lock the superpowers spec/plan tree.** Make `docs/superpowers/specs/`, `docs/superpowers/plans/`, and `docs/superpowers/archive/` read-only somehow (CI check, pre-commit hook, or just convention). They are frozen historical artifacts and editing them post-hoc destroys the narrative.
