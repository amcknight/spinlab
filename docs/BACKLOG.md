# SpinLab Backlog

Open follow-ups, ideas, and known tech debt. Items here are not tracked in any external system. Severity tags: **[S]** small (under a day), **[M]** medium (1-3 days), **[L]** large (>3 days).

When an item ships, delete it from this file rather than checking it off — the backlog is the live to-do list, not a changelog. Use git history for the historical record.

## Active workstreams

The arc currently in motion. Frozen specs/plans under `docs/superpowers/` hold the detail; this section is the live status. Shipped sub-items get deleted from here, not checked off. (`docs/superpowers/plans/` is otherwise a frozen archive of *completed* work — not a live list.)

### Practice UI overhaul — Plan D

Spec: `docs/superpowers/specs/2026-06-01-practice-ui-overhaul-design.md`. Plans A/B/C and D-Live (BE + FE1/FE2 + BE2) shipped. Remaining sub-projects:

- **[M] D-Layout — responsive two-mode layout.** Merge the 4 tabs into one responsive app: narrow live strip (during practice) ↔ wide review/planning window (after stop), composing A+B+C. Includes (2026-06-04 smoke): keep the whole run stats bar visible after stopping practice instead of collapsing to a bare Idle; shrink the HyperPlay/Practice → Idle jump so context isn't lost. Also: wire Plan C's Now/Baseline window picker to drive Plan A's verdict; persist the picker across SSE re-renders; drop the slope heatmaps.
- **[M] D-Viz — expected-time distribution graphs.** Top feature ask ("we NEED expected time distributions", reiterated 2026-06-04). Starting spec: `docs/ideas/changing-histogram-design.md`. Clear-time distribution evolving over attempts in the swappable graph slot; classic bars default, current + one comparison ref, optional fading trail, one shared y-axis. **Data feed (rolled in from bootstrap follow-ups):** expose the bootstrap estimator's full sample distribution (`BootstrapExtras.total_samples`) at the route level — bootstrap already computes the N sampled totals; only the mean is surfaced today.
- **[S] D-Live-FE3 — liveliness.** Deferred live-view polish, confirmed still missing 2026-06-04: upticking timer / frame-by-frame climbing dot (needs an attempt-start timestamp not yet in `AppState`), session-start vertical line on the graph, flash-on-change animation, last data point rendered yellow + climbing. Spec: `docs/superpowers/specs/2026-06-02-live-practice-view-design.md`.
- **[S] D-Sim — Simulator fixes.** Re-evaluate the Simulator panel on SSE pushes (today it computes once on tab-open, frozen thereafter); add a header to the "expected now → after practicing once" row; debounce the synchronous Monte-Carlo evaluate.

### 2026-06-04 smoke breakages — triage before resuming Plan D

Found clicking around Cute Kaizo. These block core flows; recommend fixing before more UI work. Sizes are estimates pending diagnosis.

- **[M] `/api/state` returns 500 on dashboard launch.** Hard crash on first load ("running the dashboard the first time doesn't work"). Investigate the state route + its init dependencies. Likely the root of the next two items.
- **[M] Segments section of Manage tab shows nothing.** No segments listed after stop / replay / save. Visibility regression — check whether it's downstream of the `/api/state` 500.
- **[M] Model tab doesn't follow the selected reference run.** Stuck on one run; switching run in Manage doesn't update Model. Selection isn't propagating (may partly dissolve when tabs merge, but the wiring is broken).
- **[S] Two save-states appear on level start in Reference Run (expected 1).** Possibly an entrance double-state, or leftover staged replay-slot files.
- **[M] Replay + Fast Replay regressed again.** Replay first-click no-op + bounces back to Idle (movie maybe unsaved); Fast Replay loads state then does a short Start→Stop without playing. The 2026-05-29 window-slot staging fix isn't holding on the live path. Instrumentation branch `debug/replay-slot-instrumentation` (commit `d743942`, unmerged) re-captures the slot story — re-run the decisive test, then make playback deterministic despite no NCI set-slot command.
- **[M] Practice/lifecycle lag + 6s flicker.** After the thread-local-connection fix, UI is still slow: ~4s game-select, ~2s stop→Draft, ~4-5s Draft→Idle, ~3-4s Close→Idle, plus a major full-tab flicker every ~6s on Practice (segment times only update right after the flicker — looks like a full `innerHTML` re-render on a slow poll, not a diff). Use the `perf:` BE logs + `[perf]` FE console timing already in place to localize: per-SSE-push double-fetch, synchronous MC evaluate, or full re-render.
- **[S] Cold-capture button polish.** "Start Cold Capture" is unstyled and shows even when there are no missing-cold segments — gate on the actual `segments_missing_cold` count, not just `has_active_run`. ("Didn't trigger" when a cold state already exists from another run is correct behavior, not a bug.)

## High-priority follow-ups

- **[M] RetroArch migration — field-verify remaining follow-ups.** Backend works for reference + practice (unit + live); migration audit A–G complete and Phase E (BSV recording + replay) shipped. The live Replay/Fast Replay regression is tracked under Active workstreams above. Remaining: field-verify the provisional fixes for cold-fill on cp-respawn hacks and second-death practice reload, plus SpeedRunTiming under RA (see RA backend tech debt below). Detailed status: `docs/retroarch-migration/status.md`.
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

## Test gaps

- **[M] Mutation-test-worthy assertions.** Some new tests would not catch silent regressions. Example: `test_save_and_finish_seeds_attempts_and_finalizes` does not fail if you delete the `set_active_capture_run` call. One pass: for each new test, ask "would this fail if I deleted line X of the implementation?"
- **[S] Pyright cleanup sweep.** Pre-existing `pyright` errors remain across the tree (the previously-listed `kalman.py` errors are gone with the model purge; the segments-v07 sys.path shim contributes a large chunk that should NOT be "fixed"). Re-run `npx pyright python/` for the current list before a focused cleanup PR.
- **[S] Practice attempt observed-conditions.** `practice.py` does not populate `observed_start_conditions` / `observed_end_conditions` on attempts (the schema columns exist; the code path just leaves them empty). Either persist them so condition-aware estimators can use them, or remove the columns.

## UX polish

- **[S] "Session N of M" indicator during recording.** During a long multi-session run, the user may forget which session they're in. The recording panel could show "Recording — Session 3 — N segments captured this run".
- **[M] Save & Finish dialog UX.** Currently inline `<input>` near the buttons. A modal "Name this run" prompt on click is more deliberate, matches typical save-dialog convention, and prevents accidental saves with the default placeholder name.
- **[S] Visual differentiation of paused vs recording.** Both states use similar styling. A clearer cue — "paused" color/icon vs "active" color — helps the user instantly know the state.
- **[S] Per-segment delete confirmation.** Already has confirm but it's generic. Including segment description + capture time in the prompt makes it less ambiguous what's being deleted.
- **[S] Pre-start pause for hot starts.** A pre-start pause that shows the game paused/greyed with the buttons-to-hold prompt before starting. Helps tricky hot starts. (Reinforced 2026-06-04: loaded state sometimes appears "too far back in time" — load visible-but-paused/greyed, then unpause.)
- **[M] Practice-mode time should count retry time.** Practice time should include time the user would have spent retrying in a real run. Needs reference-run between-gameplay time to compute.
- **[M] Empty per-session segment count rendering.** Decide on em-dash vs `0` for the per-session count column when a session captured no segments.

## Modeling

_Direction (2026-06-04): lean on **more Monte-Carlo rollouts** rather than Bayesian priors. Full Bayesian uncertainty tracking + population priors + prior-derivation are de-prioritized — the "correct" way, but the rollout engine is where the traction is. Keep the structural/feature work below; drop the priors-flavored items._

- **Room and other consistent subsections.** Support segments that aren't bounded by checkpoints — sub-level structure that's still routinely repeatable.
- **Merging and splitting of segments.** UI + DB support for combining adjacent segments or splitting one in two without losing attempt history.
- **Run library for testing.** A small library of real runs (including gnarly weird ones) recorded and replayable, used to seed estimators and exercise new functionality.
- **Critical path focus.** Just work on critical-path stuff to get real value for a simple run. (Periodic reminder, not a task.)

### Death-aware-rolling follow-ups (deferred from the 2026-05-24 PoC)

- **[M] Death-aware allocator.** New allocator class (`death_aware_greedy`) that folds `p_die_per_life`/`expected_death_time_ms` into the scoring formula. The current greedy allocator consumes only `total.expected_ms`, so it doesn't currently differentiate "high p_die" from "low p_die at the same expected time." Needs a real session of data to see the per-segment `p_die` distribution before picking the scoring formula.
- **[L] Screen-awareness.** Per-screen death-rate and timing breakdown. Structural change — probably warrants a new `extras` variant or a separate output type, since the question shifts from "when do I die?" to "where do I die?". Wait until at least one real session shows whether per-screen context would meaningfully change practice loop or stat displays.
- **[L] Learning-curve / asymptote projections.** "Expected gold after infinite practice", "expected practice before WR", "expected time saved by 1 more rep." Requires fitting a parametric learning curve (exponential decay or power law) to the rolling-min or low quantile of the completion-time series. Deferred from this PoC because we wanted to validate the simpler decayed-mean + geometric-formula model first.
- **[S] Spec wording fix — sample truncation granularity.** The 2026-05-24 spec says `death_samples`/`completion_samples` are "capped at ~5×halflife per outcome", but the implementation truncates per *episode* (in `_compute_aggregates`), so multi-death episodes can push `death_samples` higher than the per-outcome budget. Behavior is fine (storage stays bounded by ~5×halflife × deaths-per-episode-in-window); spec text should be updated to say "~5×halflife episodes" to match the code.

### Cold/hot follow-ups (deferred from the 2026-05-26 is_hot landing)

- **[M] Hyper-play hot data collection.** Hyper-play currently emits cold-only attempts because `hyper_play._record_attempt` writes via the legacy `log_attempt` path (one row per sub-segment with `deaths=0`). To gather hot data, refactor to detect carry-over from a completed prior sub-segment and tag the first attempt of the next sub-segment with `is_hot=True`. Mirrors the reference recorder's logic. Blocked on no urgent need; revisit once the cold/hot modeling story matures and hot data starts to matter for the scheduler.
- **[L] Hot↔cold transfer modeling.** Treat cold and hot attempts as partially-pooled populations rather than fully independent. Empirically, improvement on cold transfers ~80% to hot (and vice versa), but the death-aware rolling estimator and future bootstrap estimator currently treat them as disjoint. A learnable transfer weight derived from per-segment hot/cold sample pairs would let cold data inform hot estimates when hot data is sparse. Defer until meaningful hot sample sizes exist to validate against.
- **[S] Reference-run hot/cold backfill edge cases.** Migration 0007's backfill marks the first attempt of an episode HOT iff the immediately prior attempt in the same `capture_run` survived from a different episode. Paused-and-resumed runs (where a `capture_session` boundary sits inside a `capture_run`) can mislabel: a fresh-resume attempt could look like a carry-over if the prior session ended on a survival. Acceptable today (rare and disclosed in `docs/GLOSSARY.md`), but worth revisiting if hot data quality becomes important — a derive script keyed off `capture_session_id` could re-tag historical rows correctly.

### Bootstrap-resample follow-ups (deferred from the 2026-05-27 branch-2 landing)

_(Bootstrap distribution exposure rolled into D-Viz under Active workstreams. Death-distribution panel dropped — same on-hold family as the death-curve plot.)_

- **[M] Bias-as-learning meta-loop.** Run the bootstrap with multiple reweighting schemes (different halflives or alternative decay shapes) and select the one that best predicts held-out future episodes. The picked weighting then *is* the player's learning rate estimate. Premature to implement; document the idea so it isn't lost.
- **[S] Bootstrap-vs-geometric divergence note.** The original branch-2 spec said "bootstrap mean > geometric mean on clustered deaths." The implementation analysis showed the direction is data-dependent: with aborted episodes in the pool, bootstrap is LOWER (geometric pretends every attempt completes-by-attrition); with all-completed clustered-death data, the two often agree exactly because `p/(1-p)` over the lives-weighted marginal recovers `E[deaths_per_attempt]` by construction. The meaningful divergence between the two estimators is in the FULL distribution (variance, tail, multi-modality), not the mean. Revisit the spec wording when branch 3's distribution overlay lands.

## Documentation

- **[S] Lock the superpowers spec/plan tree.** Make `docs/superpowers/specs/`, `docs/superpowers/plans/`, and `docs/superpowers/archive/` read-only somehow (CI check, pre-commit hook, or just convention). They are frozen historical artifacts and editing them post-hoc destroys the narrative.

### Cold distribution / hazard follow-ups (post-2026-05-27)

- **Hot-view toggle**: add [Cold]/[Hot] sub-filter to the segment-detail
  distribution panel. Today the panel shows cold only; hot data is rare
  but a per-segment hot view is interesting once it accumulates.
- **Bootstrap filter consistency**: `bootstrap_resample` filters at the
  episode level (drops any episode containing a hot attempt); the
  cold_distribution layer filters at the attempt level. Decide whether
  bootstrap should be brought into line.
- **Confidence intervals on hazard**: opacity-as-confidence is the only
  signal today. KM-style binomial confidence bands are a reasonable next
  iteration if opacity proves insufficient.
- **Toggle persistence**: Histogram/Hazard tab choice resets to Histogram
  on every detail-page open. Persist across the session if useful.
- **`SegmentAttempt` episode aggregate**: do we even need it? Branch 3's
  cold_distribution.py works on a flat list of attempts and never touches
  the episode aggregate. Refactor candidate.
- **Histogram bar weighting**: histogram uses raw counts (n_deaths,
  n_completions). If users find the divergence from hazard's weighted
  view confusing, revisit.
