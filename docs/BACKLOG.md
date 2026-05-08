# SpinLab Backlog

Open follow-ups, ideas, and known tech debt. Items here are not tracked in any external system. Severity tags: **[S]** small (under a day), **[M]** medium (1-3 days), **[L]** large (>3 days).

When an item ships, delete it from this file rather than checking it off — the backlog is the live to-do list, not a changelog. Use git history for the historical record.

## High-priority follow-ups

- **[L] RetroArch migration — finish to parity.** Backend works for reference + practice in unit tests and basic live testing; cold-fill on cp-respawn hacks and second-death practice reload have provisional fixes that need field verification. Phase E (BSV input recording + replay) is not started. Detailed status: `docs/retroarch-migration/status.md`. Path to parity: `docs/retroarch-migration/path-to-parity.md`.
- **[L] Replace `_init_schema` rebuild-on-mismatch with real migrations.** `db/core.py:_init_schema` drops any table whose columns drift from `_expected_columns()`. Greenfield-friendly today; catastrophic once references and attempts represent meaningful accumulated work. Move to alembic or a homegrown forward-only migration log **before** the data starts mattering. Single biggest tech debt item in the repo.
- **[M] Long-run / load test.** No test exercises >100 segments or multi-GB accumulation across many sessions. A test that simulates 1000 segments across 20 sessions then runs recovery would catch scaling regressions early. The whole point of the multi-session work was to support 50-hour runs.
- **[M] Playwright crash-and-resume smoke test.** Scaffold exists at `tests/integration/test_multi_session_smoke.py` (skipped). Extend the `test_frontend_smoke.py` fixture chain to support process restart against a shared DB file. Python-level crash test (`test_crash_recovery.py`) covers the data layer; this is purely UI confidence.

## Architecture

- **[M] Consolidate `paused_run_id` and `recorder.capture_run_id` state.** `ReferenceController` has two state fields managed by separate methods. The `_assert_run_state_invariant` helper guards against drift, but the next refactor that touches state transitions is at risk. Either consolidate into a single state object or expand the invariant assertions.
- **[M] `is_primary` auto-promotion on segment delete.** Today: deleting the primary segment for a geography silently drops it from practice scheduling. Now that segments carry `capture_session_id`, the natural behavior is to auto-promote the most-recent active sibling.
- **[M] Replay-creates-ephemeral-capture_run is awkward.** Replay needs a `capture_run` row purely so the recorder has somewhere to attach segments. The `id LIKE 'replay_%'` filter in recovery is a hack working around it. Cleaner: scratch run that's not in `capture_runs` at all. Big refactor, low priority — the hack works.
- **[S] `save_and_finish_run` atomicity uses raw SQL.** Works correctly. Cleaner long-term API would be teaching the mixin methods to honor an open transaction. Defer until another transaction-spanning operation needs the same pattern.

## Efficiency

- **[M] `recorder._close_segment` ordinal is O(n).** Each close runs `SELECT COUNT(*) FROM segments WHERE reference_id = ?`. With many segments this is O(n²) over a full run. Track in-memory per session — ordinal isn't load-bearing for correctness, only display ordering, so a slight desync after crash is acceptable.
- **[S] `get_paused_state` runs 2 DB queries per SSE event.** Called from `state_builder` on every TCP event. For high-event-rate flows (rapid segment closes, deaths), this is real overhead. Cache and invalidate only on lifecycle transitions, or fold into one query.
- **[S] `_compute_is_primary` runs a query per segment close.** N segments per session = N queries. Could batch at session-end with one query that updates all segments captured in the session. Or precompute primary-by-geography at start of session.
- **[M] Spinrec file accumulation.** Discarded runs clean up; finalized runs do not. Spinrecs accumulate forever in `data/<game>/rec/`. Add a per-reference "delete spinrec" action, or an automatic sweep ("references older than N months without practice activity"). Probably matters at the 1+ year mark.
- **[S] `recover_paused_capture_run` N+1 on draft cleanup.** When multiple stranded drafts exist, `hard_delete_capture_run` is called per row. Bulk delete possible but rarely matters (multiple drafts is the failure case, not the norm).

## RetroArch backend tech debt

- **[M] Mesen-only integration tests fail under RA backend.** `tests/integration/test_transitions.py` and `tests/integration/test_replay_fixture.py` connect via `TcpManager`; under `backend == "retroarch"` they all fail with `ConnectionError: Not connected`. Either add backend-aware skips or port them to drive `RetroArchOrchestrator` directly. Until then, "full pytest is green" gate is not achievable on RA.
- **[M] No live test for SpeedRunTiming under RA.** Unit tests pass; never exercised in a real session. Speed-run mode is the riskiest untested code path in the new backend.
- **[S] Anonymous state-key resolver leaks abstraction.** `StateIO.resolve_event_path` returns paths keyed by `entrance_<level>_<room>` and `cp_<level>_<ord>_hot` for events whose true `segment_id` isn't known yet; F-live does the bridging downstream. Cleaner: orchestrator looks up segment_id before calling resolve. Defer until Phase E touches this surface area.
- **[S] `config.ra_game_basename` is now informational only.** Orchestrator overrides it from `GET_STATUS` at connect; keeping the field invites confusion. Either remove it or document it as "ignored — auto-detected at connect."
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

## Documentation

- **[S] Lock the superpowers spec/plan tree.** Make `docs/superpowers/specs/`, `docs/superpowers/plans/`, and `docs/superpowers/archive/` read-only somehow (CI check, pre-commit hook, or just convention). They are frozen historical artifacts and editing them post-hoc destroys the narrative.
