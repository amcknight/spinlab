# Transition test state-leak follow-up

> Created 2026-05-14 from C1-C3 execution surfacing this pre-existing fragility. Not yet ready to execute — needs a design choice between approaches.

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

## Recommendation

**Option B (savestate-based reset)** is the right long-term answer. It scales to any new transition test, doesn't slow the suite, and matches how the production cold-fill orchestrator already works (loads from a known state, [project_cold_capture_reset.md](project_cold_capture_reset.md)).

The work splits cleanly:

1. Write `scripts/make_fresh_boot_state.py` — launches RA, advances past boot, saves to a known path. Run once per ROM in `ROM_REGISTRY`.
2. Commit the resulting `.state` files to `tests/integration/states/<rom_basename>.state`.
3. Modify `RAPokeEngine.__init__` to take a `fresh_state_path: Path | None` and `RAHarness.launch` to wire it from the registry.
4. In `run_scenario`: if a fresh state is configured, copy it to RA's reserved slot, `LOAD_STATE_SLOT`, settle ~30 frames (let snes9x re-init), then proceed.

**Estimate:** 2-4 hours including the savestate-creation tooling and verifying both ROMs work.

---

## Files Likely Touched

- `tests/integration/states/` (new directory)
- `tests/integration/states/Toothpaste.state` (new, generated)
- `tests/integration/states/Love Yourself.state` (new, generated, optional — replay fixture may not need it)
- `tests/integration/ra_harness.py` — accept fresh_state_path parameter
- `tests/integration/ra_poke_engine.py` — load state at scenario start
- `tests/integration/conftest.py` — wire fresh_state_path through factory; add `STATE_REGISTRY` parallel to `ROM_REGISTRY`
- `scripts/make_fresh_boot_state.py` (new) — savestate-creation CLI

## Out of Scope

- Fixing `RAPokeEngine`'s held-value race more aggressively (would help even without savestate reset, but the savestate approach makes it moot for these tests).
- Switching test_transitions.py to function-scoped harness (Option A above) — only worth it if Option B turns out to be infeasible.
