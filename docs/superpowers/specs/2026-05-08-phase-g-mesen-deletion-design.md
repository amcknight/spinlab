# Phase G — Delete Mesen + Lua

## Goal

Remove the Mesen-Lua backend from SpinLab entirely. After Phase G, `git grep -i mesen` returns only history references; the `lua/` directory is gone; `python/spinlab/tcp_manager.py` and `python/spinlab/spinrec.py` are gone; `EmulatorConfig` no longer has Mesen fields; the dashboard, capture flow, tests, and docs all assume RetroArch.

## Why now

Andrew (2026-05-08): "Even with this issue [Phase E's BSV+SAVE_STATE incompatibility], we have come far enough that I won't need Mesen anymore." The migration has been in "both backends present, RA the default" mode since the late-April F-live work; the Mesen path has been load-bearing for nothing on Andrew's daily flow for weeks. Carrying it costs:

- Two backends in every file with backend-aware code paths (≥10 files).
- A separate Lua codebase in `lua/` (5 files, ~1.5k lines).
- Mesen-only test files that skip silently when Mesen isn't available.
- Two layers of `EmulatorConfig` fields, two `pytest -m emulator` paths, two slot-management strategies (Mesen had its own, RA has the new mess documented in `slot-management.md`).

The Phase E option (a) work shipped enough — record/playback validated, the broken bits are documented as known limitations — that there's nothing pulling Mesen back into the hot path.

## Out of scope

- **Replay BSV record/playback hardening.** That's the slot-management followups. Phase G doesn't change BSV behavior.
- **Migrating CLAUDE.md / README narrative** beyond removing the Mesen requirement. The "migration in progress" framing in README needs to go but that's a separate small edit.
- **Removing `EmuBackend` Protocol.** Even with one production backend, the Protocol is useful for fakes in tests. Leave it.
- **Renaming the `retroarch` subpackage** to something neutral. Moot — there's only one backend now, but renaming is invasive and provides no functional gain. Leave.

## What gets deleted

### Files removed entirely

- `lua/` (whole directory): `addresses.lua`, `json.lua`, `overlay.lua`, `poke_engine.lua`, `spinlab.lua`, `spinrec.lua`
- `python/spinlab/tcp_manager.py` — Mesen TCP client
- `python/spinlab/spinrec.py` — `.spinrec` format reader/writer
- `tests/unit/test_tcp_manager.py`
- `tests/integration/test_lua_conditions.py` — Mesen-Lua-only test
- `tests/integration/test_smoke.py` — full-stack Mesen smoke; if a similar RA-side test is desirable, port it as part of Phase G or follow-up
- `python/spinlab/conditions/lua_conditions.py` (if exists; check)
- Any `tests/unit/test_lua_*.py` files

### Files modified

- `python/spinlab/config.py` — drop `path`, `lua_script`, `script_data_dir` (Mesen-only fields). Backend default flips to `"retroarch"` (or `backend` field gets removed entirely; see Decision 1).
- `python/spinlab/emu_backend.py` — drop Mesen-specific docstring notes; the Protocol stays (useful for testing).
- `python/spinlab/dashboard.py` — drop the Mesen-vs-RA backend selection branch in `create_app` (or wherever it lives). All paths assume RA.
- `python/spinlab/cli.py` — Mesen launch/setup commands removed.
- `python/spinlab/protocol.py` — drop any Mesen-only event types (e.g., `RecSavedEvent` if it was Mesen-Lua-emit-only). Audit each carefully — many events fire from both backends today. **Open question:** keep or drop `ReplayCmd.path` field? Today the route layer constructs a `.spinrec` path; orchestrator's `_on_replay` translates to `.replay`. Post-Phase-G, the route should construct `.replay` directly. `_on_replay`'s suffix-translation hack goes away.
- `python/spinlab/routes/reference.py` — `_resolve_spinrec_path` either renamed or replaced with `.replay` lookup. Affects `/api/replay/start` route and the `has_spinrec`/`has_replay` field pair on the Reference type (which is now redundant — keep only `has_replay`).
- `python/spinlab/routes/system.py` — Mesen launch path (if any). Simplifies `_launch_retroarch` → just `_launch_emulator`.
- `python/spinlab/capture/reference.py` — drop the `path=spinrec_path` argument from `ReferenceStartCmd` callers; spinrec is gone. Adjust `start_reference`/`resume_reference` accordingly.
- `python/spinlab/session_manager.py` — drop any backend conditionals.
- `python/spinlab/condition_registry.py` — Mesen Lua-callback hooks if any. Cold-fill and predicate framework already RA-only.
- `python/spinlab/db/capture_runs.py`, `db/capture_sessions.py`, `db/core.py` — schema may have `spinrec_path` columns. **Decision 2:** drop the column (small migration) vs. leave it nullable for backwards-compat with existing dbs.
- Anything referencing `MESEN_PATH` env var.
- `tests/integration/conftest.py` — drop `mesen_process`, `tcp_client`, `mesen_run_scenario`, `smoke_mesen_process`, `dashboard_server`, `replay_mesen_process`, `replay_dashboard` fixtures (some already deleted in Task 12 of Phase E; double-check).
- `tests/conftest.py` — `FakeTcpManager` if still referenced.
- `tests/unit/test_config_retroarch.py`, `test_dashboard_backend_select.py`, `test_*_backend.py` — simplify or delete tests that test backend selection.
- `tests/unit/capture/test_recorder.py`, `test_reference.py`, `test_multi_session.py` — drop Mesen-specific scenarios; the recorders are now RA-only.

### Documentation updates

- `README.md` — remove Mesen requirement, simplify config example, drop the "migration in progress" warning at the top.
- `docs/ARCHITECTURE.md` — update to describe the RA-only architecture.
- `docs/retroarch-migration/status.md` — finalize. The migration is done.
- `docs/retroarch-migration/path-to-parity.md` — close P2.2; update other items.
- Various spec/plan docs in `docs/superpowers/` reference Mesen — leave as historical artifacts (they're frozen per project memory).
- `CLAUDE.md` — remove Mesen-specific guidance; simplify test markers if any.

## Open decisions (need Andrew input)

### Decision 1 — keep or drop `EmulatorConfig.backend` field?

Two options:

- **(a) Drop `backend` entirely.** Only RA exists. `config.yaml`'s `emulator.backend` is ignored or rejected.
- **(b) Keep `backend` but only `"retroarch"` is valid.** Forwards-compat in case a future backend (a different libretro core, or a different emulator entirely) gets added.

I'd lean (a) — YAGNI. If a third emulator gets added later, this gets re-introduced then. Keeping it as a forward-compat hook is cheap but invites confusion ("which backends does it support?").

### Decision 2 — DB schema: drop or nullable-keep `spinrec_path` column?

Two options:

- **(a) Drop the column.** Small migration. Existing databases need a `spinlab db migrate` invocation. Cleaner long-term.
- **(b) Leave nullable.** Existing databases work as-is. Column sits unused. Less invasive.

I'd lean (b) — Andrew's local DB has months of attempt history; not breaking it is worth a few unused bytes per row. The column can be dropped in a later cleanup.

### Decision 3 — port `test_smoke.py` to RA, or just delete?

The current Mesen-bound `test_smoke.py` is a full-stack smoke (dashboard + DB + emulator). After Phase G, it goes. Two options:

- **(a) Just delete.** Other coverage exists: `test_replay_fixture.py` (currently xfailed) is the closest equivalent. Unit + RA harness tests cover most failure modes.
- **(b) Port to RA.** Write `tests/integration/test_smoke_ra.py` mirroring the original's assertions but on RA backend. ~2 hours of work.

I'd lean (a) for the Phase G commit (just delete) and add (b) as a follow-up if desired. Keeps Phase G's diff focused on deletion.

### Decision 4 — ordering: one big commit or several?

Phase G is a "big delete-fest" per existing notes. Options:

- **(a) Single commit** — one big diff. Hard to review but no half-state. ~30 file changes, ~5k lines deleted.
- **(b) 5-7 commits in sequence** — e.g.: (1) tests, (2) lua/, (3) tcp_manager, (4) spinrec, (5) config + routes, (6) docs. Each easier to review; main works at every commit.

I'd lean (b) — match the rename pass we did for BSV→movie. Each commit is verifiable independently.

## Acceptance criteria

- `git grep -i mesen` returns no matches in current code (history fine; spec/plan markdown fine as historical artifacts).
- `git grep -i tcpmanager` returns no matches in current code.
- `git grep "\.spinrec"` returns no matches in current code (excluding doc references in slot-management.md / path-to-parity.md if they reference the old format historically).
- `python -m pytest` runs clean. xfails from Phase E (`test_replay_fixture.py`, `test_poller_runs_during_playback`) remain xfailed; nothing else fails.
- `lua/` directory does not exist.
- `EmulatorConfig` has no Mesen fields.
- README's "Migration in progress" warning is gone.

## Risks

- **Risk: Mesen-only test coverage hides RA-side bugs.** Some flows currently only have Mesen smoke coverage; deleting them might leave a coverage gap. Mitigation: audit which tests are skipped vs. xfailed under RA; if a critical flow is uncovered, port the test before deleting.
- **Risk: silent Mesen-side state in DB.** Existing Andrew-DB rows reference `spinrec_path` paths that no longer exist on disk. Decision 2 (nullable-keep) mitigates: rows are read but the path is null/orphaned. Practice loop already runs from `state_path` (the `.mss`/`.state`), not spinrec.
- **Risk: regression in capture/reference flow.** That code has Mesen branches that get deleted; need to verify the simplified flow handles all states correctly. Strong unit-test coverage helps; if test counts drop significantly during Phase G, that's a signal to add tests before continuing.

## Estimate

- Audit + decisions: ½ day (mostly Andrew, can be done while reading the spec).
- Implementation across 5-7 commits: 1-2 days.
- Doc updates: ½ day.
- Final test pass + review: ½ day.

Roughly 2-3 working days of focused effort.

## What this enables

After Phase G:

- Single backend = simpler reasoning across the codebase.
- One slot-management strategy (the RA mess in `slot-management.md`), one savestate convention, one launch sequence.
- README + ARCHITECTURE describe one system, not two-with-a-switch.
- Future RA work (BSV mid-record fix, throttle for poller starvation, etc.) has fewer touched files because the Mesen scaffolding is gone.
- Frees `EmuBackend`'s docstrings and the dual-backend test fixtures from carrying Mesen baggage.

## References

- Path-to-parity item P2.2: [`docs/retroarch-migration/path-to-parity.md`](../../retroarch-migration/path-to-parity.md)
- Migration status: [`docs/retroarch-migration/status.md`](../../retroarch-migration/status.md)
- Phase E (the immediate predecessor): [`docs/superpowers/specs/2026-05-08-phase-e-movie-replay-design.md`](2026-05-08-phase-e-movie-replay-design.md)
- Mesen→RA architectural decision document: `docs/superpowers/specs/2026-05-06-retroarch-migration-design.md` (frozen historical artifact)
