# Test-Suite Audit — 2026-05-11

**Status:** findings. No code changes proposed in this doc beyond the flake fix that
landed alongside it.
**Scope:** survey of the test suite as it stands the day after the RAClient hoist
landed. Goal is a punch-list Andrew can pick from, not a plan.

The flake fix itself shipped with this audit: per-harness UDP port allocation in
`tests/integration/ra_harness.py` + `tests/integration/conftest.py`, plus a new
[test_harness_isolation.py](../tests/integration/test_harness_isolation.py)
regression test. Section 3 covers it in detail.

## What landed in the follow-up pass (2026-05-11 afternoon)

Most of §4–§7 + a Phase-E-mis-diagnosis correction. Summary:

- **§3.2 was a misdiagnosis.** `test_replay_produces_segments` was failing because
  this morning's commit `d1478e0` ("post-RAClient wart sweep") over-deleted
  `replay_total` storage in `session_manager`. The cleanup correctly removed
  `ReplayProgressEvent` (no emitter under RA) but also dropped the
  `replay_total` field, which is sourced from `ReplayStartedEvent.frame_count`
  (still emitted by `MoviePlayer`). Restored in this pass:
  `session_manager._handle_replay_started` re-stores `event.frame_count`,
  `state_builder` re-emits `replay.total`, frontend `ReplayState` re-adds
  `total: number`, and `header.ts` displays the bounded form ("Replaying N
  frames…") since per-frame progress isn't observable under RA. Phase E option
  (b) was **not** outstanding — it shipped 2026-05-10 per
  [docs/retroarch-migration/status.md](retroarch-migration/status.md). xfail
  was therefore wrong; test now passes unconditionally.
- **§4 rename:** mechanical sweep across 14 test files. `FakeTcpManager →
  FakeEmuBackend`, `mock_tcp → mock_emu`, `fake_tcp → fake_emu`. Production
  `tcp=` kwargs and `self.tcp` attributes left alone — Pass C of the
  post-migration audit will rename those.
- **§5 trivial-test drops:** deleted `test_models_enums.py`, `test_system_state.py`,
  `test_fake_tcp.py`, and `test_addresses.py` (the last because
  `tests/integration/addresses.py` is now a thin re-export of the production
  ADDR_MAP — the test was circular). Trimmed `test_initial_state` from
  `test_transition_state.py`; kept the real `reset()` test.
- **§6.3 Protocol conformance meta-test:** added
  [test_emu_backend_protocol.py](../tests/unit/test_emu_backend_protocol.py).
  Single `isinstance(FakeEmuBackend(), EmuBackend)` assertion.
- **§7 misc:**
  - CLAUDE.md test-timing numbers refreshed (fast: 23→10s, emulator: 6→150s,
    full: 30→170s).
  - ADDR_MAP "must stay in sync" wording dropped — they're already linked
    by import.
  - `tests/playwright/` → `tests/screenshots/`.
  - The `frontend` marker stays in `pyproject.toml`; the audit's claim that
    it has zero Python consumers was wrong (`test_frontend_smoke.py` uses it).
- **Skip elimination** (separate from the audit's §3 work): deleted
  `test_multi_session_smoke.py` (unimplemented hard-`skip` stub; data-layer
  is covered exhaustively by `test_crash_recovery.py`). Dropped the
  defensive "only one estimator registered" skip in `test_scheduler_kalman`
  — multiple estimators always exist. Converted
  `test_retroarch_practice_smoke.py` to use the `ra_harness` fixture, so
  it no longer requires a manually-running RA on port 55355.

Net result: fast suite 802 → 777 tests (deletions); 0 skips/xfails in the fast
suite (down from 1); full suite green end-to-end, no xfails anywhere.

## Pass 2 — Production refactor + Mesen/Lua/TCP sweep (2026-05-11 evening)

Andrew lifted the test-only scope after Pass 1: "renames can seep into
production if they are good enough." This pass did the production rename
(Pass C of the post-migration audit) plus a wider cleanup of every
remaining Mesen/Lua/TCP reference that didn't pull its weight.

- **Production rename `tcp → emu`.** Word-boundary mechanical substitution
  across `python/spinlab/` and `tests/` (and `frontend/src/`). 283
  substitutions across 28 files. Covers `self.tcp → self.emu`, `tcp:
  EmuBackend` kwarg, `app.state.tcp`, the JSON state field
  `tcp_connected → emu_connected` (including the frontend types/usages
  and python test assertions), and the dashboard module constants
  `TCP_*_TIMEOUT_S → EMU_*_TIMEOUT_S`. `vite.py` was excluded — its
  "TCP connection" usage is about Vite's real HTTP socket, not the
  emulator backend.
- **Permanently-skipped POSIX test deleted.**
  `test_kill_on_close_job_returns_none_on_posix` only asserted `return
  None` / `return False` on the platform Andrew never runs. Win32 tests
  in the same file still exercise the real cross-platform branch.
  Eliminates the last skip from the fast suite.
- **Stale comments/docstrings/log strings updated.** "TCP client" →
  "emulator bridge" in `dashboard.py` module docstring; "Bridge TCP
  events" → "Bridge backend events" in `event_loop` docstring; "TCP
  not connected" / "TCP gone" log strings → "backend not connected" /
  "backend gone"; "Sent from orchestrator to Lua" → "Practice-loop
  directive" in `SegmentCommand` docstring; "matches Mesen's
  emu.readWord convention" → SNES-native-word-order phrasing in
  `condition_registry`. 18 phrase replacements across 10 test files plus
  the integration README.
- **Integration README rewrite.** `tests/integration/README.md` was
  describing the long-dead Mesen2+spinlab.lua headless poke engine —
  replaced with accurate RetroArch / `RAHarness` / `RAPokeEngine`
  documentation including the per-frame write-after-FRAMEADVANCE
  ordering and the per-harness UDP port allocation pattern.
- **`addresses.py` docstring rewrite.** Drops "Mesen-era compatibility
  shim" framing — the file is a string-keyed view that the poke parser
  needs, not a historical accident.

Legitimate remaining TCP/Mesen/Lua references (intentionally kept):

- `vite.py:185` — "TCP connection" (real Vite HTTP socket)
- `tests/integration/conftest.py:92` — "free TCP port" (uvicorn HTTP)
- `tests/integration/conftest.py:373` — "the legacy Mesen+TCP backend"
  (one-line historical contrast in a fixture docstring)
- `tests/integration/conftest.py:412` — "RA backend uses NCI, not TCP"
  (accurate technical note on why a stale config field is unused)

## Pass 3 — Emulator-suite speedup (2026-05-11 evening)

Two surgical changes cut the full suite from ~190s → ~90s (-55%):

**B. Wire `FAST_FORWARD` to `cmd.speed=SPEED_UNCAPPED`.** Discovered
that `ReplayCmd.speed` was dead data: the orchestrator's `_on_replay`
called `play_movie` and ignored the speed field. Lua used to honour it;
the RA port never wired it. Added `NCIClient.fast_forward_toggle()`,
exposed it on `RAClient`, and the orchestrator now toggles RA into
fast-forward when `speed == SPEED_UNCAPPED` (which the replay test
already passes) and toggles it back on stop. Symmetric — every call
flips state, and there's no NCI command to query it.

Independent fix: the replay test's `_wait_for_idle_with_progress`
was hardcoded to a 45s wall-clock wait (2273 frames / 60fps × 1.2)
before sending `/api/replay/stop`. That masked any RA-side speedup.
Rewrote to gate on `sections_captured >= expected_segments` (the actual
content milestone) and stop the moment RA has produced what we
asserted on. Combined effect on the replay test: 47s → ~3s call /
~9s total.

**A. Batch `read_snapshot` reads.** Was 11 individual `READ_CORE_RAM`
NCI calls per snapshot (one per address). Clustered into 6 contiguous
ranges:

- `$0071..$010B` (155B): `player_anim`, `game_mode`, `room_num`
- `$0906..$0DD5` (1232B): `fanfare`, `exit_mode`
- `$13BF..$13CE` (16B): `level_num`, `boss_defeat`, `midway`
- `$1935`, `$1DFB`, `$1B403` (1B each): three loners far apart

Saves 5 UDP round-trips per snapshot. Each transition test does this
~65 times per scenario, so the test-suite impact is ~30% per test
(~12s → ~6-8s). Production poller benefit too — the 60Hz `read_snapshot`
in `python/spinlab/retroarch/poller.py` consumes the same function;
the status doc mentions a "32 Hz observed" rate concern that this
should mitigate. Per-byte ROM-overwrite semantics are unchanged (we
still read whatever the ROM has put there).

**Per-test before/after**:

| Test | Before | After |
|---|---|---|
| `test_replay_produces_segments` | 47s | 3s call / 9s total |
| 9× `test_transitions.py::*` | ~12s each | ~6-8s each |
| **Emulator suite** | **171s** | **70s** (-59%) |
| **Full suite** | **190s** | **86s** (-55%) |

## Pass 4 — Quiescence-based scenario termination (2026-05-11 evening)

Replaced fixed `settle: 60` with detector-quiescence early exit in
`RAPokeEngine.run_scenario`. Tracks `frame_of_last_event` and terminates
when the detector has been silent for `QUIESCENCE_FRAMES` (12, ~200ms at
60Hz) past the last poke. The `.poke` file's `settle:` value remains
parsed but acts only as an upper-bound safety cap.

Simple scenarios (entrance_goal, key_exit, orb_exit, boss_defeat) ran
~80 frames blindly; now run ~30. ~5.8s/test → ~2.1s/test. Complex
scenarios saved fewer frames but still meaningful.

Emulator suite 70s → 36s. Full suite 86s → 52s.

## Known gap — cross-test isolation under shared RA

Attempted to merge `ra_harness` and `ra_harness_love_yourself` into one
session-scoped harness (saves ~3s of duplicate RA launch). Reverted —
the merge surfaced a real cross-test state leak that isn't trivial to
fix:

- The replay-fixture test plays Love Yourself to its level 44 goal,
  leaving ~8KB of dirty WRAM (player position, sprite state, etc.).
- Transition tests that run after it inherit that WRAM. Each scenario
  zeroes the 11 `ADDR_MAP` bytes it explicitly drives, but the ROM's
  game-side logic during `FRAMEADVANCE` reads bytes outside `ADDR_MAP`
  and writes back into addresses we care about (most notably
  `cp_entrance` at $1B403).
- Result: 3 of 9 transition tests started producing wrong event
  ordinals or missing entrance events post-replay, while passing
  cleanly in isolation.

The two-harness design pre-merge provided isolation by-accident-of-
architecture: separate RA processes meant no WRAM crosstalk. Today's
guarantees, ranked:

1. Process-level (was strongest — two separate RA processes)
2. Per-scenario `ADDR_MAP` zeroing in `RAPokeEngine`
3. Held-value re-assertion after each `FRAMEADVANCE` (only for
   addresses the scenario explicitly pokes — gaps for untracked bytes)
4. Fresh `TransitionDetector` per scenario

What's missing: a way to bring RA back to a known-clean state between
tests that share a harness. Options for a future pass:

- **Save-state-load isolation.** Capture a "clean boot, paused on
  title" state once at `RAHarness.launch`. Each scenario starts by
  loading it. ~50-150ms per scenario; bullet-proof byte-identical
  reset. Opens the door to a single shared harness AND eventually
  xdist parallelism.
- **Hard RESET between scenarios.** Two NCI RESETs with 300ms gap,
  re-pause, let title screen settle. ~800ms per scenario. Simpler
  than save-state but slower.
- **Held-default-zero for all `ADDR_MAP` bytes.** Attempted in this
  pass; broke `test_orb_exit` (the held-zero overwrote the io poke
  intermittently — likely a write-ordering interaction at the NCI
  layer when sending 11 writes per frame instead of the scenario's
  ~5). Would need investigation.

For now: kept the two-harness design. The 3s saving from collapsing
them isn't worth chasing without proper save-state isolation.

**ROM-specific feature coverage is a real concern, not just a nuisance.**
`cp_entrance` ($1B403) is a custom-ASM-style checkpoint that only patched
hacks (e.g. Toothpaste) populate — Love Yourself uses only the
standard `midway` tape. So testing exclusively on Love Yourself means
the `cp_entrance` detector branch in `predicates.check_checkpoint_hit`
gets exercised only by synthetic poke values, never by real ROM
behavior. The two-harness design (one ROM per harness) is actually
the right pattern long-term: different harnesses cover different
detector branches against the ROMs that exercise them. A future pass
should formalize this — declare which detector features each
harness's ROM exercises, and route tests accordingly.

**Suggested next-session shape:**

1. **Clean-boot save-state isolation** as a harness primitive.
   `RAHarness.launch` captures a "paused on title, byte-identical"
   state once; `run_scenario` loads it before each scenario.
   Bullet-proof cross-test isolation within a harness.
2. **Multi-ROM coverage matrix.** Pick a ROM per detector branch
   (Toothpaste for `cp_entrance`, Love Yourself for `midway`-only
   exits + replay fixture, maybe one more). Each harness pins its
   ROM and the tests that target its properties.
3. **Optional xdist** once (1) lands — true isolation makes parallel
   workers safe.

**Not done in this session:**

- D. `pytest-xdist` parallelism. The transition tests are isolated
  (each `run_scenario` re-zeroes `ADDR_MAP` and uses its own
  `TransitionDetector`); two harnesses coexist (see
  `test_harness_isolation.py`). Dynamic UDP port allocation per
  harness means k workers ≡ k RA processes on distinct ports.
  Ceiling: ~36s → ~15s with 4 workers. Skipped: the 52s full
  suite is comfortable, and clean-boot save-state isolation should
  precede xdist work (otherwise per-worker WRAM leaks would still
  bite us, just in parallel).
- Cross-test isolation hardening (see "Known gap" above).
- Redundant-scenario audit + new-scenario backfill. Not surveyed
  yet; expected to add/remove a handful of `.poke` files.

---

## 1. Test-category dashboard

Counts and timing as of this audit. Numbers are wall-clock on Andrew's main
desktop, single warm run. All counts come from `pytest --collect-only`.

| Marker | Files | Tests | Wall clock | Pass rate |
|---|---|---|---|---|
| (unmarked / fast) | 87 | 802 | ~10s | 802/802 + 1 skip |
| `slow` | (mixed in) | 8 | ~4s | 8/8 |
| `emulator` | 8 | 11 | ~140s | 10/11 (1 known Phase E failure — see §3) |
| `frontend` (Python-side) | 0 | 0 | n/a | marker exists, no users |
| frontend Vitest (`npm test`) | 9 | 65 | ~4.5s | 65/65 |
| Playwright | 0 | 0 | n/a | `tests/playwright/` is a screenshot dir only |

**Total Python tests collected:** 829 (827 of which run; 1 skipped + 1 deselected by markers).

Observations:

- The fast suite is the workhorse — running it after every code change is
  cheap. Slow tests adding only 4s on top means there's almost no reason to
  routinely exclude them. CLAUDE.md's "fast: not (emulator or slow or frontend)
  ~23s" is stale — measured 10s here.
- The `frontend` marker is reserved but has zero Python consumers. Either
  resurrect it (when Python-side static-asset tests exist) or delete the
  marker definition from `pyproject.toml`.
- "Playwright" appears nowhere in the Python test tree. The `tests/playwright/`
  directory holds only PNG screenshots — not tests. If Playwright is going to
  be a future test category, the absence is fine; if it's never coming, the
  directory could be renamed (e.g. `tests/screenshots/`) to stop misleading
  readers.

---

## 2. Coverage gaps

Fast-suite coverage report (`pytest -m "not (emulator or slow or frontend)"
--cov=spinlab`). Total: **86%**.

The standouts — files where coverage is materially below the project average:

| File | Coverage | Note |
|---|---|---|
| `retroarch/raclient.py` | **35%** | Just landed; almost every save/load/movie path exercised only by emulator tests. See §5 for missing unit tests. |
| `routes/system.py` | 72% | Reset/maintenance routes are mostly untested. |
| `session_manager.py` | 77% | Long branchy file; the uncovered ranges (lines 460–561) are largely error/recovery paths. |
| `speed_run.py` | 82% | Speed-run mode has its own dedicated test file, but several edge cases unreached. |
| `practice.py` | 84% | Reasonable; the missing lines are mostly disconnect/abort branches. |

The single biggest move is **adding a fast unit-test file for `RAClient`** —
see §5. Even getting it to 70% would lift the project to ~92% overall and
catch most of the wart-class bugs (save mtime poll, load slot copy, replay
slot resolution) that today only show up under live RA.

`session_manager.py` is the second-biggest, but its uncovered lines are
recovery paths that are intentionally hard to reach. Probably best left as
follow-up to the planned SessionManager split (Phase 3 of RAClient hoist
spec), not chased in isolation.

---

## 3. Determinism issues

### 3.1 NCI port conflict — **FIXED in this commit**

Previously: two session-scoped harnesses (`ra_harness` and
`ra_harness_love_yourself`) both called `RAHarness.launch()`, which hardcoded
`NCIClient()` to UDP port 55355. When `pytest -m emulator` collected both
fixtures, the second RA process couldn't bind, and `test_orb_exit`,
`test_checkpoint_cold_spawn`, `test_entrance_death_spawn` flaked
(diagnosed in agent memory as `project_emulator_fixture_port_conflict`).

**Fix:** [ra_harness.py](../tests/integration/ra_harness.py) now takes an
`nci_port: int | None = None` parameter; the conftest fixtures allocate a
free UDP port per harness via `_free_udp_port()` and write
`network_cmd_port = "<port>"` into the launched RA's appendconfig.
[test_harness_isolation.py](../tests/integration/test_harness_isolation.py)
is the regression guard.

Verified by running `pytest -m emulator` end-to-end: all three previously
flaky tests now pass with both harnesses active.

### 3.2 `test_replay_produces_segments` is failing, not xfailed

[test_replay_fixture.py:20](../tests/integration/test_replay_fixture.py#L20)
runs and fails. CLAUDE.md and the post-migration memory both describe this
test as "xfailed until Phase E option (b)" (poller starvation +
BSV+SAVE_STATE fix). The xfail marker is missing from the code.

**Why it matters:** CLAUDE.md says "a red suite is never acceptable" before
merging. Today, a fresh-clone `pytest -m emulator` produces a hard FAILED.
This is a doc/code drift, not a real regression.

**Recommended action:** add `pytest.mark.xfail(reason="Phase E option (b) —
poller starvation + slowmotion_ratio wiring; see docs/retroarch-migration/
status.md", strict=False)` to the test (or the class). Honors the existing
intent; lets the suite be green without hiding the issue.

### 3.3 Sleep-based waits

Several tests rely on absolute time bounds rather than condition polling:

- [ra_harness.py:29-30](../tests/integration/ra_harness.py#L29-L30)
  (`NCI_PING_RETRIES=10`, `NCI_PING_INTERVAL_S=0.5`) — fine, retry-loop with
  polling.
- [ra_harness.py:45-46](../tests/integration/ra_harness.py#L45-L46)
  (`WRAM_SANITY_RETRIES=5`, `WRAM_SANITY_RETRY_DELAY_S=0.3`) — also a poll
  loop, OK.
- [conftest.py:412-423](../tests/integration/conftest.py#L412-L423) (replay
  dashboard waits for orchestrator connection with `_time.sleep(0.25)` in a
  40-iteration loop) — already polling-based.

No determinism issues found beyond §3.1. The harness's sleep budgets are
defensive but bounded; on a fast Windows host they cost about 1.5s of fixture
setup per `RAHarness.launch()`. Tunable later if it becomes annoying.

### 3.4 Session-scoped fixture state

[fake_dashboard_server](../tests/integration/conftest.py#L131) and
[fake_game_loaded](../tests/integration/conftest.py#L208) are session-scoped.
The shared `db` and `session` survive across every fast emulator test that
imports them. Today no test mutates state in a way that pollutes its
neighbors, but the pattern is fragile — a new test that calls
`switch_game` to a different game, or that registers cleanup it forgets to
unregister, would have a hard-to-diagnose effect on the next test.

**Recommended action:** none yet, but worth re-evaluating if a frontend smoke
test ever starts behaving order-dependently. Function-scoping these would
add a per-test uvicorn restart (~3s) — not free.

---

## 4. Stale / mis-named test doubles

The RAClient hoist replaced three `_FakeNCI` scaffolds with one shared
[FakeRAClient](../tests/fakes/raclient.py). But one layer up, the dashboard's
"TCP" surface still has three test doubles named after the pre-RA era:

- [mock_tcp](../tests/conftest.py#L30-L41) (MagicMock fixture)
- [FakeTcpManager](../tests/conftest.py#L88-L123) (handwritten fake class)
- [fake_tcp](../tests/conftest.py#L120-L123) (fixture)

All three implement `send_command`, `send`, `disconnect`, `save_state`,
`load_state` — i.e. the `EmuBackend` Protocol that the orchestrator now
satisfies. There is no TCP at this layer under RA. The names are misleading
in 2026.

**Files affected (16):**
```
tests/conftest.py
tests/unit/capture/test_multi_session.py
tests/unit/capture/test_recorder.py
tests/unit/capture/test_reference.py
tests/unit/test_dashboard_integration.py
tests/unit/test_fake_tcp.py
tests/unit/test_invalidate_flow.py
tests/unit/test_practice.py
tests/unit/test_replay.py
tests/unit/test_session_manager.py
tests/unit/test_session_manager_conditions.py
tests/unit/test_state_builder.py
tests/integration/conftest.py
tests/integration/test_crash_recovery.py
tests/integration/test_frontend_smoke.py
tests/integration/test_multi_session_smoke.py
```

**Recommended action:** rename in one mechanical pass — `mock_tcp` →
`mock_emu`, `FakeTcpManager` → `FakeEmuBackend`, `fake_tcp` → `fake_emu`.
Also tighten `FakeEmuBackend` to declare it implements the `EmuBackend`
Protocol (use `typing.runtime_checkable` on the Protocol and add an
isinstance assertion in a meta-test) — that way the fake can't silently
drift from the real surface again. Bundles well with Pass C of the
post-migration audit (which renames `self.tcp` → `self.emu` in production
code).

**Cost:** small. Risk: low — it's a rename. Touches a lot of test files but
no production logic.

---

## 5. Trivial tests — drop candidates

Tests in the suite that verify language behavior, fixture meta-behavior, or
test-against-themselves patterns. Dropping these removes test maintenance
weight without losing real coverage.

### Drop

- **[test_models_enums.py](../tests/unit/test_models_enums.py)** (117 lines, 13 tests).
  Every test verifies that a `StrEnum` member's value is the string literal
  set in the source file, or that constructing one with an unknown string
  raises `ValueError`. Both are Python language guarantees. The "is the set
  of EventType members exactly these strings?" test is the one weak signal —
  it would catch a silently-added enum value — but the cost/benefit is poor.
- **[test_fake_tcp.py](../tests/unit/test_fake_tcp.py)** (19 lines, 3 tests).
  Meta-tests on the `fake_tcp` fixture: it records commands, it starts
  connected, you can disconnect it. If you trust the fixture (you have to —
  every consumer test trusts it), these tests add nothing.
- **[test_system_state.py](../tests/unit/test_system_state.py)** (11 lines,
  1 test). Asserts dataclass default values. The dataclass declaration is
  the test. If a future test depends on these defaults, write that test;
  testing the defaults in isolation tests `@dataclass` itself.

### Borderline (judgment call)

- **[test_transition_state.py](../tests/unit/retroarch/test_transition_state.py)**
  (27 lines, 2 tests). Tests dataclass defaults + a custom `reset()`
  method. The `reset()` test is real (it asserts behavior, not language
  guarantees). Keep that one, drop `test_initial_state`.
- **[test_addresses.py](../tests/unit/retroarch/test_addresses.py)**
  (24 lines, 2 tests). Pins SMW memory addresses to their values. The
  comment refers to `lua/addresses.lua` which no longer exists (Mesen+Lua
  is gone). The test is now circular — it pins the source-of-truth to
  itself.
  **Recommended:** either delete (the values are kaizosplits-derived and
  documented as such in [addresses.py](../python/spinlab/retroarch/addresses.py)),
  or rewrite to compare against `tests/integration/addresses.py`, which
  CLAUDE.md says "must stay in sync".
- **[test_reset_logging.py](../tests/unit/test_reset_logging.py)** (25 lines,
  1 test). Mocks `logger.warning`, calls a route handler, verifies a log
  call happened with the game ID. The route's contract is presumably "log
  the game ID on reset", so the test is legitimate — but it's done entirely
  with `unittest.mock.patch`, and the route itself is one logger.warning
  line. If the implementation moves the warning into a helper, the test
  silently passes when broken. Borderline.

### Keep

- **[test_romid.py](../tests/unit/test_romid.py)** (27 lines). Tests
  determinism of a checksum function and filename parsing. Real behavior.

**Estimated total dropped:** ~150 LOC of tests, no behavior lost.

---

## 6. Missing tests

These are tests that *should* exist given current code shape, ordered by
impact.

### 6.1 RAClient unit tests — **highest impact**

There is no `tests/unit/retroarch/test_raclient.py`. RAClient is at 35%
coverage; most of the missing 65% is testable against a `FakeNCI` (the
production module's `NCIClient`-mocking pattern is already established in
`tests/unit/retroarch/test_nci.py`).

What's currently untested at the unit level:

- `connect()` failure modes (NCI timeout, malformed GET_STATUS, unexpected
  state)
- `save_state()` mtime-poll timeout
- `save_state()` happy path (slot file appears → move to dest → return path)
- `load_state()` increments `state_version` exactly once per call
- `load_state()` filesystem failure path
- Replay slot resolution from RA log (the log-scraping logic — see
  `MoviePlayer.play`'s ancestry)
- Hotkey debounce timing (`HotkeyProfile.min_tap_gap_sec` is actually waited)
- `reset()` is exactly 2 RESET presses

Expected size: ~300 LOC, in line with the RAClient hoist spec's "Test
rewrite reveals coverage gaps" expectation.

### 6.2 State-version → poller resync integration test

The RAClient hoist replaced the explicit `poller.mark_state_loaded()` call
with a polled `state_version` counter. There are poller-only tests for
`resync_after_state_load()` and RAClient-only tests for the counter (would
be, per §6.1), but nothing wires them together. A future refactor that
breaks the counter→poller observation loop would be silently fine in unit
tests.

**Recommended test:** in `tests/unit/retroarch/test_poller.py`, add a test
that constructs a real Poller with a `FakeRAClient`, bumps the fake's
`_state_version`, ticks the poller, asserts detectors received
`resync_after_state_load()`. Already half-implemented by
[test_poller_resync_clears_phantom_edges](../tests/unit/retroarch/test_poller.py)
— may just need to confirm it's reading from the new counter, not the old
`mark_state_loaded()` API.

### 6.3 EmuBackend Protocol conformance

`FakeTcpManager` (or its post-rename successor `FakeEmuBackend`) implements
the `EmuBackend` Protocol structurally. If the Protocol grows a new method
and `FakeEmuBackend` doesn't, every consumer test still passes — but
production calls into the new method blow up at runtime.

**Recommended test:** make `EmuBackend` a `@runtime_checkable` Protocol
and add a meta-test:

```python
def test_fake_emu_backend_matches_protocol():
    from spinlab.emu_backend import EmuBackend
    from tests.conftest import FakeEmuBackend
    assert isinstance(FakeEmuBackend(), EmuBackend)
```

One line; catches an entire class of drift bug.

### 6.4 RAHarness multi-port (covered by this commit)

Already added — [test_harness_isolation.py](../tests/integration/test_harness_isolation.py).
Listed here for completeness.

---

## 7. Misc follow-ups

Smaller observations from the walk-through.

- **CLAUDE.md "fast tests ~23s" is stale.** Measured 10s on warm cache.
- **CLAUDE.md "emulator tests ~6s" is wildly stale.** Measured 140s — the
  emulator suite grew substantially during the migration.
- **`frontend` marker.** Either delete from `pyproject.toml` or wire it to
  something. The vitest suite (frontend/`npm test`) is the actual frontend
  testing; the Python `-m frontend` marker has no users.
- **`tests/playwright/` directory.** Holds PNG smoke screenshots, no tests.
  Rename to `tests/screenshots/` or move under `docs/` — anywhere that
  doesn't suggest "Playwright suite lives here".
- **`tests/integration/addresses.py` duplicates `python/spinlab/retroarch/
  addresses.py`.** CLAUDE.md mandates they stay in sync but nothing
  enforces it. A 5-line meta-test would. (Related to §5 borderline
  `test_addresses.py`.)
- **`fake_dashboard_server` and `fake_game_loaded` carry `@pytest.mark.
  emulator` via the module-level `pytestmark`** (the integration conftest
  applies it to the whole module on
  [conftest.py:37](../tests/integration/conftest.py#L37)). Fixtures
  themselves don't run as tests, so this is harmless — but it means any new
  *test* added to `tests/integration/conftest.py` would inherit the
  emulator marker. Not currently a problem; flag if it becomes one.
- **Frontend Vitest reports 4.5s wall clock, but 4.45s of that is
  environment setup.** Actual test execution is 64ms. If the dev loop ever
  feels slow, that's where to look — but right now it's fine.

---

## Appendix: what wasn't in scope

- Test reorganization (file moves, directory structure). Not the audit's
  job; if anything in §5 or §6 leads to a reorg, it gets its own spec.
- Production code refactors. Everything here lives under `tests/` (plus
  the docs/CLAUDE.md tweaks called out).
- Performance optimization of the emulator suite. 140s is dominated by
  RA's launch-to-WRAM-tick warmup (~10s × 11 tests). Reusing one harness
  across tests is a real win but a real refactor; deferred.
- Decisions about which items to act on. That's Andrew's call after
  reading this.
