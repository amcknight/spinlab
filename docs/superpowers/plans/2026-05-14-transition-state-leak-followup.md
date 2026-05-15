# Transition test state-leak follow-up

> Created 2026-05-14 from C1-C3 execution surfacing this pre-existing fragility.
> 2026-05-14 (later): Promoted from "design doc" to active plan. Option B picked. Scope expanded per /improve CV1 convergence (see [2026-05-14-improve-2030.md](../scans/2026-05-14-improve-2030.md)) — once savestate is the boot mechanism, the FRAMEADVANCE warm-up probe becomes obsolete and rom_key stops being an implicit `default` fallback. Tasks 5-7 cover those follow-ons.

**Symptom:** `tests/integration/test_transitions.py::test_key_exit` and `::test_orb_exit` fail when run after `::test_entrance_goal` (both pass in isolation). The session-scoped `ra_harness` shares one snes9x_libretro process across every transition test, and prior tests leave the ROM in a state (mid-fanfare music, post-exit animation) where the audio engine continuously writes to `$1DFB` (`ADDR_IO`) and `$0906` (`ADDR_FANFARE`). When the next scenario pokes `io_port=7` (key) or `io_port=3` (orb) at frame 15, the ROM's $1DFB writes overwrite the poke between `RAPokeEngine`'s `write_ram` and `read_snapshot`, so the detector sees `io_port=0` → `goal_type` returns `"abort"` instead of `"key"`/`"orb"`.

**Pre-existing**, surfaced by C1-C3:
- Historical comment at [tests/integration/ra_poke_engine.py:25](tests/integration/ra_poke_engine.py#L25): "test_orb_exit intermittently seeing io_port=0 instead of the poked io_port=3" — already known flake. The "write held values *after* `frame_advance`" inversion landed earlier was a partial fix.
- Tests have been silently skipping for an unknown duration via the C1-C3-replaced fragile alphabetical ROM picker → the deep-freeze bug masked them.
- C1-C3 + SRAM isolation made all 12 emulator tests actually run; this leak now visible.

**What was tried during C1-C3 execution (and ruled out):**

1. **NCI `RESET` (twice) at scenario start.** Broke the harness — reset puts RA in PLAYING+menu-confirmation state; subsequent `frame_advance` deep-freezes. Would need substantial post-reset recovery (re-pause, wait for core reload, re-run sanity probe). Not a quick fix.
2. **240-frame "drain" loop holding `ADDR_MAP` at 0 before scenario pokes start.** Partially worked: `test_key_exit` started passing after `test_entrance_goal`, but `test_orb_exit` still failed after `test_key_exit`. Adds ~3-4s per scenario. Insufficient AND slows suite.

The drain doesn't work because the leak isn't in WRAM — it's in the SPC700 audio chip's internal state and the 65816 CPU's program counter / register state. WRAM-only writes can't reach those.

---

## Approaches (pick one)

### Option A: Function-scoped `ra_harness_fresh` for transitions only

Add a new fixture `ra_harness_fresh` (function-scoped) alongside the existing session-scoped `ra_harness` (default rom). Update `tests/integration/test_transitions.py` to use `ra_harness_fresh` via `run_scenario`. Other tests (harness_isolation, retroarch_practice_smoke, replay_fixture) keep session scope.

**Cost:** 9 RA launches per transition test run × ~3-5s each = +30-45s to emulator suite. Suite goes from ~36s to ~70s. Still under 2 min total.

**Pro:** Bulletproof — fresh RA per test means zero state leakage.
**Con:** Slow. Also, the ra_harness_factory caching design assumes per-key reuse; function-scoped requires bypassing the cache (or a new factory variant).

### Option B: Fresh-boot savestate, loaded before each scenario

Create a "fresh boot" savestate file for each ROM in `ROM_REGISTRY`, commit it under `tests/integration/states/`. Modify `RAPokeEngine.run_scenario` to copy that savestate into RA's reserved slot 9999 and `LOAD_STATE_SLOT 9999` before zeroing ADDR_MAP. After load, RA needs a brief settle (frame_advance × N) before the scenario starts.

**Cost:**
- One-time: per-ROM savestate creation tooling. Could be a small CLI: launch RA, frame_advance ~120 frames past boot, save_state to slot 9999, copy file out. ~1-2hr to write + run.
- Per-test: ~10ms to copy + LOAD_STATE_SLOT + ~100ms to settle. Effectively free.

**Pro:** Fast (negligible per-test overhead). Truly hermetic — load resets CPU + SPC + WRAM in one shot.
**Con:** Requires ROM-keyed savestate files in the repo (small, ~300KB each). Setup script needs to handle the snes9x_libretro state-format quirks.

### Option C: Accept "abort" as a valid alternative classification

Loosen `test_key_exit` / `test_orb_exit` assertions to accept either `"key"`/`"orb"` OR `"abort"` when the prior test left the ROM in fanfare state. **Strongly NOT recommended** — defeats the purpose of the test (verifying classification works). Listed only for completeness.

---

## Decision: Option B

**Option B (savestate-based reset)** is the right long-term answer. It scales to any new transition test, doesn't slow the suite, and matches how the production cold-fill orchestrator already works (loads from a known state, [project_cold_capture_reset.md](project_cold_capture_reset.md)).

The work splits into seven tasks. Tasks 1-4 land the savestate mechanism. Tasks 5-7 are the CV1 convergent payoff — once savestates are the boot mechanism, the FRAMEADVANCE warm-up probe is dead code and rom_key becomes a real parameter.

### Task 1 — Savestate creation tool
Write `scripts/make_fresh_boot_state.py`. Launches RA via `RAHarness`-equivalent code, advances past boot (~120 frames is the prior empirical settle), `SAVE_STATE_SLOT` to a reserved slot, copies the resulting `.state` file to `tests/integration/states/<rom_basename>.state`. CLI takes `--rom-key` (looks up via `ROM_REGISTRY`). Run once per ROM at landing time AND any time `ROM_REGISTRY` gains a new entry.

### Task 2 — Commit savestate files
Generate one `.state` file per `ROM_REGISTRY` entry. Commit under `tests/integration/states/`. ~300KB each. **Toothpaste.state will need the FRAMEADVANCE probe disabled to generate, since the probe currently rejects Toothpaste** — this is the chicken/egg moment; expect to generate Toothpaste.state via a one-time bypass flag.

### Task 3 — Wire fresh_state_path through harness factory
- `RAPokeEngine.__init__` accepts `fresh_state_path: Path | None`
- `RAHarness.launch` passes it through from a new `fresh_state_path` constructor arg
- `tests/integration/conftest.py` adds `STATE_REGISTRY: dict[str, Path]` parallel to `ROM_REGISTRY`; the harness factory looks up `STATE_REGISTRY[rom_key]` and threads it through

### Task 4 — Load state at scenario start
In `RAPokeEngine.run_scenario`: if `self._fresh_state_path` is set, copy it to RA's reserved savestate slot, `LOAD_STATE_SLOT`, settle ~30 frames (let snes9x re-init the SPC core), THEN zero ADDR_MAP and proceed with poke schedule. Verify `test_key_exit` and `test_orb_exit` pass when run after `test_entrance_goal`.

### Task 5 — Restore ROM_REGISTRY to honest state (CV1)
Once Task 4 lands and the state-leak is gone, point `ROM_REGISTRY["default"]` back at vanilla SMW (`_clean.smc`) — the temp-mapping to `Love Yourself.smc` from commit 222bf98 was only there because the FRAMEADVANCE probe rejected vanilla. Verify `test_transitions.py` passes against vanilla level numbering (no more level 105 vs vanilla mismatches). Delete the `TOOTHPASTE_ROM_NAME` and `CLEAN_SMW_ROM_NAME` constants' "unused" status by adding them to `ROM_REGISTRY`.

### Task 6 — Delete the FRAMEADVANCE warm-up probe (CV1)
Once savestate-based boot is the load mechanism, the WRAM warm-up probe is dead code. A loaded savestate is *by construction* a live frame.
- `tests/integration/ra_harness.py:225-266` — delete the probe loop entirely
- Delete `WRAM_SANITY_RETRIES`, `WRAM_SANITY_PROBE_BYTES`, `RAHarnessLaunchError` "deep-freeze" path
- Update `tests/integration/conftest.py:43-69` comment block to reflect the new world (or delete it)

### Task 7 — Make rom_key load-bearing in test fixture path (CV1)
Per [[project_test_rom_declaration_design]] — the `default` rom_key has been a silent fallback. With savestates landed, every test should declare which ROM it wants:
- `test_transitions.py` → `ra_harness_vanilla_smw` (or whichever rom matches its scenarios)
- `test_replay_fixture.py` → `ra_harness_love_yourself` (already explicit)
- `test_harness_isolation.py` → already uses both fixtures by name
- Drop the implicit `default` key from `ROM_REGISTRY` once all callers are explicit. Hard-fail any test that requests a non-registered key.

---

## Estimate

- Tasks 1-4 (state-leak fix proper): 2-4 hours including savestate creation tooling
- Tasks 5-7 (CV1 cleanup): 1-2 hours, mostly test-file edits and verifying the green baseline
- **Total: 3-6 hours**

## Files Likely Touched

- `tests/integration/states/` (new directory)
- `tests/integration/states/Toothpaste.state`, `Love Yourself.state`, `_clean.state` (new, generated)
- `tests/integration/ra_harness.py` — accept fresh_state_path; delete FRAMEADVANCE probe (Task 6)
- `tests/integration/ra_poke_engine.py` — load state at scenario start (Task 4)
- `tests/integration/conftest.py` — STATE_REGISTRY; restore default→vanilla (Task 5); update stale comment block (Task 6); rename `ra_harness` → explicit per-rom fixtures (Task 7)
- `tests/integration/test_transitions.py` — switch fixture name (Task 7)
- `scripts/make_fresh_boot_state.py` (new) — savestate-creation CLI (Task 1)

## Out of Scope

- Fixing `RAPokeEngine`'s held-value race more aggressively (would help even without savestate reset, but the savestate approach makes it moot for these tests).
- Switching test_transitions.py to function-scoped harness (Option A above) — only worth it if Option B turns out to be infeasible.
- Diagnostic hook generalization (CV2 from the same scan) — separate medium, not blocking this work.
- The poke DSL `PokeScenario` dataclass conversion — only worth doing if scenario metadata becomes load-bearing for breadcrumbs; defer.
