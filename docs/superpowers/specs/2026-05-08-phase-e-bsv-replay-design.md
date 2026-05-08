# Phase E — Movie Replay Validation

## Goal

Validate that movie record/playback under our snes9x_libretro + runahead=2 + secondary-instance=true setup is viable as a replacement for the Mesen `.spinrec` replay path, by getting `tests/integration/test_replay_fixture.py` running end-to-end through RetroArch. As a structural side effect, wire movie recording into `ReferenceController` so a real reference run produces the test fixture — bootstrapping toward the larger "full parity" goal (path-to-parity.md item 1: capture inputs alongside states) without committing to it before validation.

> **Naming history.** This spec was originally titled "BSV Replay" because libretro's deterministic movie format historically went by BSV (the bsnes-era movie format). The Task 4 smoke test (2026-05-08) found that RA 1.22.2 has evolved past that nomenclature: the NCI commands are `RECORD_REPLAY`/`HALT_REPLAY`, the on-disk format is `.replay<slot>`, and `GET_CONFIG_PARAM movie_directory` returns "unsupported" — RA writes movies to `<savestate_directory>/<core_name>/` instead. We've renamed the production code to `MovieRecorder`/`MoviePlayer`/`movie.py` to avoid colliding with `ReplayCmd` (which already means "play back a recording"). "Movie" is the neutral term that fits both the historical BSV name and the current `.replay` reality.

## Why now

The 2026-05-08 RA poke harness landing closed P1.2 — the integration tests that don't require input replay now pass under RA. The remaining backend-specific failures are all in replay territory (`test_replay_fixture.py` skips, `/api/replay/start` returns 501). Before committing to the full input-recording rewrite, Andrew wants validation that BSV is reachable, deterministic, and compatible with the live poller in our specific RA configuration. Three foundational unknowns have never been confirmed (per spike-log.md, BSV feasibility was deferred during Phase 2):

1. **Control path.** Whether NCI exposes a working movie record/playback toggle in RA 1.22.2. **(Resolved 2026-05-08 by Task 4 smoke test):** Yes — `RECORD_REPLAY`/`HALT_REPLAY` for record; playback commands TBD by Task 4 follow-up probe. The originally-assumed `BSV_RECORD_TOGGLE` does not exist in 1.22.2.
2. **Determinism under our config.** snes9x_libretro + runahead=2 + cheevos-off — does movie playback produce identical memory state every run? (Pending Task 7.)
3. **Replay-while-polling.** Can the NCI poller read RAM at 60Hz while RA is in movie playback without errors or starvation? (Pending Task 8.)

This spec validates those unknowns via cheap tests sequenced before the larger machinery, and ships test-only replay (option (a) from brainstorming). User-facing replay endpoint restoration and reference-run movie-by-default are option (b), gated on Andrew's smoke testing of (a).

## RA 1.22.2 movie format facts (empirical, from Task 4 smoke test)

These were unknowns when the spec was first written; the Task 4 probe resolved them. Subsequent tasks must follow these findings, not the original assumptions.

- **Record start command:** `RECORD_REPLAY` (not `BSV_RECORD_TOGGLE`). Fire-and-forget. Requires content already loaded and in PLAYING state.
- **Record stop command:** `HALT_REPLAY`. Fire-and-forget. Finalizes the file.
- **Playback commands:** TBD — Task 4 follow-up probe. Candidates: `PLAY_REPLAY`, `MOVIE_PLAYBACK_TOGGLE`, or hotkey-sim equivalents.
- **File format:** `.replay<slot>` (e.g. `the.replay2`). RA auto-increments the slot suffix on each new recording.
- **Output directory:** `<savestate_directory>/<core_name>/`, e.g. `C:\RetroArch-Win64\states\Snes9x\`. The `movie_directory` config key returns "unsupported" via `GET_CONFIG_PARAM`. The `<core_name>` subdirectory is determined by RA at content load time and is not directly queryable via NCI; the orchestrator either takes it as config (`EmulatorConfig.ra_movie_dir` set explicitly) or derives it from another path.
- **Stability:** Confirmed RA stays responsive after `RECORD_REPLAY`/`HALT_REPLAY`; FRAMEADVANCE continues to tick the core. No deep-pause side effects observed.

These facts shape the production-code naming chosen below.

## Scope

### In scope

1. **`MovieRecorder` integrated into `ReferenceController`.** Reference runs produce `<refid>.bsv` alongside `<refid>.mss`. None-by-default in unit tests; constructed and injected by the dashboard wiring.
2. **`MoviePlayer` driven from the orchestrator's `ReplayCmd`.** Replaces `_unsupported_phase_e` for `ReplayCmd` and `ReplayStopCmd`. Reuses existing `ReplayStartedEvent` / `ReplayProgressEvent` / `ReplayFinishedEvent` protocol; no schema changes.
3. **Three new RA-backend integration tests** in `tests/integration/test_bsv_smoke.py`: record-toggle smoke, playback determinism, polling-during-playback.
4. **Port `tests/integration/test_replay_fixture.py`** from Mesen+`.spinrec` to RA+`.bsv`. Same segment-count and segment-structure assertions.
5. **`tests/fixtures/love_yourself/one_level.bsv`** — Andrew records this manually after step 2 lands. Replaces `two_level.spinrec` as the replay fixture input.
6. **Unit tests** for `MovieRecorder` and `MoviePlayer` against a fake NCI client.

### Out of scope (deferred)

- **`.spinrec` → `.bsv` converter.** Andrew's existing fixtures are re-recorded from scratch.
- **User-facing BSV record toggle** for arbitrary play sessions. Reference-run record only.
- **Replay-step-frame / replay-while-paused UI controls.** Same surface as today.
- **Multi-game replay.** Single-game per session, unchanged.
- **Deletion of `python/spinlab/spinrec.py`, `lua/`, `TcpManager`, the Mesen replay code paths.** That's Phase G, gated on smoke testing of this work.
- **Production code changes to the Mesen backend.** None.
- **The Mesen-side `test_replay_fixture.py`.** Deleted as part of step 6 (option (a)) — keeping two replay fixture tests creates ambiguity about which one defines "correct" replay, and Plan 2 just landed specifically to avoid dual-backend test ambiguity. The Mesen-side smoke test (`test_smoke.py`) is unaffected.

## Sequenced implementation

Each step's tests pass before the next begins. Steps 1 and 4 are real go/no-go gates: if step 1 fails on every candidate control path, BSV is unreachable from our setup and the spec halts (see "Risks and mitigations" for the contingency).

### Step 1 — `test_bsv_record_toggle_smoke`

**Goal.** Validate that we can start and stop BSV recording from Python via NCI, mid-session, and produce a non-zero `.bsv` file. ~½ day.

**File.** `tests/integration/test_bsv_smoke.py`. Uses the existing `ra_harness` session fixture from Plan 2.

**Candidate control paths**, tried in order; first that produces a non-zero file wins and gets recorded as the canonical mechanism in `movie.py`:

1. NCI `BSV_RECORD_TOGGLE` (per libretro NCI doc lineage).
2. NCI hotkey-sim equivalent for `bsv_record_toggle`. May be filtered at RA's input layer (Phase 0 found this for `MENU_TOGGLE`).
3. Out-of-band: write a movie-target path into a temp `retroarch.cfg` and relaunch RA. Slow and fixture-hostile but works as a fallback.

**Test body.** Two toggles bracketing a frame-advance window:

```python
def test_bsv_record_toggle_smoke(ra_harness):
    movie_dir = discover_movie_dir(ra_harness.client)   # via GET_CONFIG_PARAM or fs sweep
    baseline_files = set(movie_dir.glob("*.bsv"))

    bsv_record_toggle(ra_harness.client)   # candidate path
    for _ in range(30):
        ra_harness.client.frame_advance()
    bsv_record_toggle(ra_harness.client)

    new_files = set(movie_dir.glob("*.bsv")) - baseline_files
    assert len(new_files) == 1
    assert new_files.pop().stat().st_size > 0
    assert ra_harness.client.get_status().is_responsive   # no deep-pause, no crash
```

**Open during step 1.** Where the `.bsv` file lands. RA's default movie dir is configurable (`movie_directory` in cfg). Test reads via NCI `GET_CONFIG_PARAM` if available, else does an mtime sweep of plausible directories. Whichever works gets fixed in `EmulatorConfig.ra_movie_dir`.

### Step 2 — `MovieRecorder` integrated into `ReferenceController`

**New module.** `python/spinlab/retroarch/movie.py`:

```python
@dataclass
class MovieRecorder:
    client: NCIClient
    movie_dir: Path                   # where RA writes .bsv (from cfg)
    _active_dest: Path | None = None
    _baseline_mtime: float | None = None

    def start(self, dest: Path) -> None:
        # 1. Snapshot movie_dir mtime baseline
        # 2. Send record toggle (mechanism chosen by step 1 smoke test)
        # 3. Confirm via GET_STATUS that RA is still responsive
        # Stash dest for stop() to move-rename into

    def stop(self) -> Path:
        # 1. Send record toggle
        # 2. Poll movie_dir for the new .bsv (mtime > baseline), 5 retries × 200ms
        # 3. Move it to self._active_dest
        # Returns the final path

    def is_recording(self) -> bool: ...
```

The move-into-final-path shuffle mirrors what `StateIO` already does for `.state` files (mtime baseline + retry + atomic rename); reuse helpers where they exist.

**Wiring into `ReferenceController`.** `__init__` gains an optional `bsv_recorder: MovieRecorder | None`. `start()` calls `recorder.start(rec_dir / f"{ref_id}.bsv")` when present. `stop()` calls `recorder.stop()`. None-by-default — unit tests don't need a recorder; the dashboard's `RetroArchOrchestrator` constructs one and passes it in.

**Failure mode.** If recording fails (NCI error, file never appears, move fails), log a warning and continue. Reference runs are about state captures; BSV is supplementary until step 5 wires playback into something user-facing. A failed recording does not abort or invalidate the reference run.

**Config.** New field `EmulatorConfig.ra_movie_dir: Path`. Default `<retroarch_path>/states/movies/` — TBD against step 1's findings.

### Step 3 — Andrew records the fixture (manual)

After step 2 lands, Andrew runs a 1-level reference on Love Yourself, finishes the level, saves. The resulting `.bsv` is written to `{config.data_dir}/{game_id}/rec/{ref_id}.bsv`. Andrew copies it to `tests/fixtures/love_yourself/one_level.bsv`. A sibling `tests/fixtures/love_yourself/one_level.json` records:

- `frame_count: int`
- `expected_segments: int` (for the replay fixture test)
- `determinism_probe: { frame: int, addr: int, expected_byte: int }` — used by the determinism test as the known-state checkpoint

This is a manual step; no automation. The fixture is committed to git (binary, ~tens of KB).

### Step 4 — `test_bsv_playback_deterministic` and `test_poller_runs_during_playback`

Both tests live in `test_bsv_smoke.py` alongside step 1's test, both use the real fixture from step 3.

**`test_bsv_playback_deterministic`.** Validates BSV playback produces deterministic memory state under our exact config (runahead=2, secondary-instance=true, cheevos-off):

```python
def test_bsv_playback_deterministic(ra_harness, fixture_metadata):
    fixture = Path("tests/fixtures/love_yourself/one_level.bsv")
    probe = fixture_metadata["determinism_probe"]

    bytes_run_1 = play_to_frame_and_read(ra_harness, fixture, probe["frame"], probe["addr"])
    bytes_run_2 = play_to_frame_and_read(ra_harness, fixture, probe["frame"], probe["addr"])

    assert bytes_run_1 == probe["expected_byte"]
    assert bytes_run_1 == bytes_run_2
```

Two runs in the same RA session — the cheap form of "deterministic across runs" without process teardown. If Andrew's smoke testing later finds cross-process flakes, we add a fresh-process variant.

**`test_poller_runs_during_playback`.** Validates the live `Poller` can keep up during BSV playback:

```python
def test_poller_runs_during_playback(ra_harness, fixture_metadata):
    fixture = Path("tests/fixtures/love_yourself/one_level.bsv")
    target_frames = 60   # one second of playback

    player = MoviePlayer(ra_harness.client, ra_harness.movie_dir)
    poller = Poller(ra_harness.client, ...)

    player.play(fixture)
    successful_reads, errors = run_poller_for_frames(poller, target_frames)
    player.stop()

    assert errors == 0
    assert successful_reads >= int(target_frames * 0.9)
```

If success rate falls below 90%, the spec adapts in step 5 to throttle playback speed via NCI rather than complicate the poller.

### Step 5 — Wire `_on_replay_cmd` in `RetroArchOrchestrator`

Replaces `_unsupported_phase_e` for `ReplayCmd` and `ReplayStopCmd`. Constructs a `MoviePlayer`, calls `.play()` with the fixture path and the anchor state (resolved from the reference's `.mss` file), transitions session state to `replay`. `_on_replay_stop` calls `player.stop()` and emits `ReplayFinishedEvent`.

```python
@dataclass
class MoviePlayer:
    client: NCIClient
    movie_dir: Path

    def play(self, src: Path, anchor_state: Path | None = None) -> None:
        # If anchor_state: load via NCI before playback start
        # Copy src into RA's movie_dir under a known name
        # Send playback-start command
        # Confirm via GET_STATUS

    def stop(self) -> None: ...
```

The poller doesn't change. It polls memory regardless of whether RA is in BSV playback or live play, and the same `_handle_replay_*` codepath in `session_manager.py` produces the dashboard frame-progress events from the polled state.

### Step 6 — Port `test_replay_fixture.py` to RA + BSV

Replace `replay_dashboard` fixture's Mesen process with an RA process (likely a new `replay_ra_process` fixture in `conftest.py`). Replace `.spinrec` copy with `.bsv` copy. Same assertions about segment counts (4 segments for two_level → adapts to whatever step 3 produces for one_level; metadata file declares the expected count) and segment structure (entrance→checkpoint, checkpoint→goal pairs per level).

Mesen-side `test_replay_fixture.py` is deleted in this step. Mesen-side `test_smoke.py` is untouched (different scope).

## Anchoring, determinism, and the deep unknowns

### BSV anchor models

Three plausible models in libretro cores. The smoke test in step 1 will reveal which snes9x_libretro uses. **Design must not commit until then.**

1. **Power-on only.** BSV always records from cartridge insert. To capture a mid-run reference, BSV would need to be running from RA launch and truncated at reference boundaries. Replay always starts from boot.
2. **Savestate-anchored.** BSV record toggles relative to current state; replay loads an associated `.state` and plays inputs from there. This is what the original Phase E spec assumed.
3. **Hybrid.** Record toggles mid-session but encodes "start state inline" or "start at frame N from boot." Replay uses whichever was encoded.

What this affects:
- **Model 1:** `MovieRecorder.start()` is a "split point marker," not a fresh record. Reference runs would need to pair `.bsv` with the boot-time state, OR we accept that replay always starts from boot (ugly for partial-run replays).
- **Model 2:** clean. `MoviePlayer.play(src, anchor_state)` does what it says.
- **Model 3:** depends on what's encoded. Handle each case explicitly.

### Determinism risks

Even if BSV plays back, three things could break determinism:

- **Runahead.** Secondary instance is enabled (per Phase 0's `run_ahead_secondary_instance = "true"` requirement); interactions between runahead and BSV are undocumented. The determinism test must run with runahead=2 enabled to catch this.
- **Cheevos hardcore.** Already known to silently no-op state ops (spike-log.md). If it also disables BSV, step 1 fails. Test asserts cheevos is off and skips with a clear message if not.
- **Wall-clock-driven core code.** Some libretro cores read host time on deterministic-emulation paths. Snes9x is generally clean here; observed as flaky determinism if not.

### Polling-during-playback risk

BSV playback proceeds at full speed by default. If the poller can't keep up, transitions get missed during replay. The polling test in step 4 measures success rate; if <90%, throttle BSV playback speed via NCI rather than complicate the poller.

## Risks and mitigations

- **Risk: step 1 fails on every candidate control path.** BSV is unreachable from our setup. Recovery options, in order: (a) investigate whether a different RA build behaves differently and update; (b) Network RetroPad as input-injection layer (Phase 0 noted this as the fallback) — drops back to building our own movie format on top of NCI input commands, significantly larger; (c) document Phase E as blocked and reconsider whether RA replay is required for "full parity" or whether replay is a feature we drop. Decision deferred to the failure event.
- **Risk: BSV playback is non-deterministic.** Step 4's test catches this. If the determinism test flakes, investigation order: (1) confirm cheevos and runahead config; (2) compare against Andrew's smoke-testing observations; (3) consider whether snes9x_libretro version or core options affect determinism. The spec halts at this discovery — replay built on non-deterministic playback is not useful.
- **Risk: poller starvation during playback.** Step 4's test catches this. Mitigation: throttle BSV playback speed (NCI `SLOWMOTION_RATIO` or equivalent) until reads succeed at the required rate. Acceptable to slow replay for the trade — replay is not real-time-critical the way live play is.
- **Risk: writing recorded `.bsv` files to disk consumes space if playback is broken.** Small. `.bsv` files for one level are tens of KB. Accept the dead bytes if they happen.
- **Risk: BSV format changes between RA versions.** Out of scope — we ship against RA 1.22.2 and the test suite catches break-on-upgrade. Address if it becomes a real problem.

## Definition of done

- [ ] `tests/integration/test_bsv_smoke.py` passes under `backend=retroarch`: three tests covering record-toggle, playback determinism, polling-during-playback.
- [ ] `MovieRecorder` integrated into `ReferenceController`; reference runs produce `<refid>.bsv` alongside `<refid>.mss`. Recording failures log a warning and don't abort the run.
- [ ] `MoviePlayer` integrated into `RetroArchOrchestrator`; `ReplayCmd` and `ReplayStopCmd` route to it (no longer raise `BackendNotImplementedError`). Existing `ReplayStartedEvent` / `ReplayProgressEvent` / `ReplayFinishedEvent` protocol used unchanged.
- [ ] `tests/fixtures/love_yourself/one_level.bsv` committed, with sibling `one_level.json` declaring `frame_count`, `expected_segments`, and `determinism_probe`.
- [ ] `tests/integration/test_replay_fixture.py` ported to RA + BSV, passes 5 consecutive runs with no flakes.
- [ ] Mesen-side `tests/integration/test_replay_fixture.py` deleted (option (a)).
- [ ] `tests/unit/test_bsv_recorder.py` and `tests/unit/test_bsv_player.py` cover the recorder and player against a fake NCI client (file-shuffle, state machine, error paths).
- [ ] `python -m pytest` runs clean.

### Smoke-testing gates (Andrew → option (b))

After this spec ships, Andrew smoke-tests against real reference runs. The following gates move us from option (a) → option (b) (full parity, including user-facing replay endpoint and reference-run BSV-by-default):

- `test_replay_fixture.py` passes 5 consecutive runs with no flakes (already in DoD above).
- A real reference run on a hack other than Love Yourself produces a `.bsv` whose replay reproduces the same segment count + structure as the original capture.
- Memory state at known frames is byte-identical between original capture and replay (manual probe).

If those pass, option (b) is mostly orchestrator/UI wiring on top of this work. If any flake, investigate before committing.

## Future work

- **Option (b): full parity.** User-facing `/api/replay/start` accepts arbitrary `.bsv` files. Reference-run BSV becomes load-bearing for the practice loop, not just a regression-test fixture.
- **`.spinrec` → `.bsv` converter.** Low priority — Andrew has acknowledged re-recording is acceptable.
- **Replay-step-frame UI.** Frame-by-frame replay control via the dashboard.
- **Multi-game replay.** Out of scope today; lift when the rest of multi-game support arrives.
- **Phase G: delete `python/spinlab/spinrec.py`, `lua/`, `TcpManager`, the Mesen replay code paths.** Gated on (b) being stable and at least one full speedrun completed end-to-end on RA.

## References

- Plan 2 (RA poke harness) implementation: [`docs/superpowers/plans/2026-05-08-headless-ra-test-harness.md`](../plans/2026-05-08-headless-ra-test-harness.md)
- Original Phase E sketch (frozen historical artifact): [`docs/superpowers/specs/2026-05-06-retroarch-migration-design.md`](2026-05-06-retroarch-migration-design.md) §"Phase E — Replay via BSV"
- Migration status: [`docs/retroarch-migration/status.md`](../../retroarch-migration/status.md)
- Path to full parity: [`docs/retroarch-migration/path-to-parity.md`](../../retroarch-migration/path-to-parity.md)
- Spike log (BSV deferred): [`docs/retroarch-migration/spike-log.md`](../../retroarch-migration/spike-log.md)
- RetroArch NCI docs: https://docs.libretro.com/development/retroarch/network-control-interface/
