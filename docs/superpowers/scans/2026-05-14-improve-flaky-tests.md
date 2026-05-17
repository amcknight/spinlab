---
date: 2026-05-14
focus: "messy flaky test situation"
git_head: 27860df
lenses_run: [architect, control-inversion, test-skeptic, dead-code, types, observability]
critiques_run: [skeptic, convergence]
findings_count: ~70 raw lens → 15 clusters after dedup → 4 picked-eligible after critique/verify
status: full_scan
---

## Top wins

### must-fix

- **CV1 — Per-scenario fresh-boot via savestate restore (executes existing followup plan option B)** — Anchors: `tests/integration/ra_poke_engine.py:56` (`run_scenario` entry — insert `self.reset_to_baseline()`), `tests/integration/conftest.py:76-79` (ROM_REGISTRY → wire fresh-boot savestate path per ROM), `tests/integration/ra_harness.py:225-266` (FRAMEADVANCE probe — obsoleted once savestate baseline exists). Plan already written at `docs/superpowers/plans/2026-05-14-transition-state-leak-followup.md`. Absorbs C1.1, C1.2, C1.3, C1.4, C3.1, C3.2, C3.3, C3.4, C4.1, C4.4 — TEN findings collapse into one fix. Mechanism per convergence hunter: a known-good RAM+SPC+CPU snapshot per scenario kills the io_port/fanfare leak between test_entrance_goal and test_key_exit (C1.x); the FRAMEADVANCE "is the core deep-frozen?" probe is obsolete because the baseline savestate is by construction a live frame (C3.x); loading a savestate is meaningless without a rom_key, so rom_key stops being implicit (C4.1, C4.4). Verifier confirmed plan puts savestate load in `RAPokeEngine.run_scenario` while rom_key→fresh_state_path mapping lives in the factory/conftest layer. Tests fail intermittently RIGHT NOW per CLAUDE.md "skips count as failures + pre-existing failures still count" rules — must-fix tier, not nice-to-have. — size: **big** (architectural: new savestate per ROM committed, plan execution is multi-step).

### high-leverage

- **CV2 — Typed `IntegrationTestContext` + generic diagnostic hook** — Anchor: `tests/integration/conftest.py:572` (the hardcoded `for fixture_name in ("replay_ra_dashboard",):` loop — verified literal). Absorbs C2.1, C2.2, C2.3, C2.4, C2.5, C7.2, C15.1. Root cause: each integration fixture invents its own yield shape (callable for `run_scenario`, tuple for `replay_ra_dashboard`), so the hook can't generalize. Fix: introduce a typed `IntegrationTestContext` dataclass (harness, dashboard_url, db, poke_engine, last_scenario_metadata) that every integration fixture yields. Hook iterates `item.funcargs` looking for `isinstance(v, IntegrationTestContext)`. Adding new diagnostic fields (poll_count, _read_failing, state_version, cold_fill state, orchestrator task health) becomes additive on the dataclass. The `<unavailable: reason>` labels (C2.4) become methods on the context. Verifier confirmed both fixtures have incompatible shapes today. — size: **medium**.

### convergent win

- **CV3 — `PokeScenario` as typed value object (deferred unless CV1 demands it)** — Anchor: `tests/integration/poke_parser.py:30` (parse_poke return) + `tests/integration/ra_poke_engine.py:1-26` (engine __init__). Absorbs C7.1, C7.3, C7.4, C10.5. Promote `parse_poke` to return a `PokeScenario` dataclass (name, source_file, pokes, expected_exits) so engine can attach scenario.name to log lines and parser errors carry file context. Note: skeptic correctly pushed back on the C10.2/C12.1/C12.2 portions (parser unit tests are correctly scoped — verifier confirmed test_transitions.py provides the end-to-end coverage via run_scenario). Likely lands naturally as CV1 wires fresh_state_path through — at that moment the engine grows context anyway. — size: **medium**, but probably becomes trivial-as-rider on CV1.

### nearby cleanup

- C4.2 (`tests/integration/conftest.py:50-51`) — `TOOTHPASTE_ROM_NAME` / `CLEAN_SMW_ROM_NAME` constants defined but unused. After CV1 lands and the FRAMEADVANCE probe is obsolete, these constants get USED in ROM_REGISTRY — don't delete prematurely. — size: trivial-as-part-of-CV1.
- C9.5 (`python/spinlab/retroarch/poller.py:43`) — Gravestone docstring referencing deleted `mark_state_loaded` flag. Verified: literal text "replaces the old mark_state_loaded flag". — size: trivial.
- C14.1 (`tests/integration/conftest.py:43-49`) — Comment block referencing project memories. Skeptic right that the memories exist (verifier saw them in MEMORY.md); but the comment fingers them by name without explaining what state they're in. After CV1 lands, the entire comment block becomes obsolete. — size: trivial-as-part-of-CV1.
- C14.3 (`tests/unit/test_dashboard_integration.py:73-74`) — `TODO(Task 8)` actionable; off-topic for flaky tests. — size: trivial.
- C13.1 (`python/spinlab/session_manager.py:93-96`) — Carry-over I1 from prior scans; skeptic right that recent encapsulation pass already addressed seams. Leave for next architectural sweep, not this scan. — no action.
- C8.1/C8.2/C8.3 (`state_builder.py`, `reference.py:183`, `cold_fill.py:127`) — J3 carry-over; off-topic for flaky tests focus. Already on prior scan's high-leverage list. — defer.
- C5.1/C5.2/C5.4 (helper duplication) — verified ~6 lines each per loop and `_api()` is literally 2 lines; skeptic right these are ceremony at current scale. — drop.
- C9.1/C9.2/C9.3/C9.4 (production observability gaps) — already swept in Bucket A/B; skeptic right these are off-topic. — drop.
- C11.1/C11.2/C11.3 (smoke/replay test thinness) — verifier confirmed test_retroarch_practice_smoke is correctly scoped as a smoke test (4 assertions including `hasattr(first_event, "filename")`); test_harness_isolation legitimately tests the harness layer (verified ports differ + version() works); skeptic right. test_crash_recovery uses FakeEmuBackend — real gap but separate concern. — defer crash-recovery item, drop the others.

## Picked this session

- TBD — see Phase 8 picker output.

## Dropped during critique

- **C6.1** (replay_ra_dashboard pytest.skip on missing savestate_dir) — Skeptic argued this is exactly the kind of acknowledged-environmental-precondition that warrants `skipif`; the C1-C3 sweep targeted skip-as-pass for `ra_harness launch failed`, not for genuinely absent test config. Verifier confirmed the skip triggers on missing `emulator.savestate_dir` config (a deliberate dashboard-required value). Dropped.
- **C6.2** (test_replay_fixture class-level skipif on FIXTURE_REPLAY) — Verifier confirmed `FIXTURE_REPLAY = tests/fixtures/love_yourself/one_level.replay` is checked into the repo. If it goes missing it's an env breakdown — but if it's reliably present, the skipif is dead code more than a violation. Demoted to nearby cleanup (delete the skipif since fixture is repo-checked-in), but not picked.
- **C10.3** (RAPokeEngine silently swallows detector.step exceptions) — **DEBUNKED**. Verifier read lines 86-87: `snap = read_snapshot(...)` then immediately `new_events = list(detector.step(...))` — no bare except exists. Lens hallucinated.
- **C5.1/C5.2/C5.3/C5.4** (helper duplication, factory two-layer) — Verifier confirmed ~6 lines per polling loop, `_api()` is literally 2 lines, `_HarnessFactory` class+function is the deliberate C1-C3 design just landed. Skeptic right that dedupe ceremony costs more than it saves at this scale.
- **C12.1/C12.2** (test_poke_parser tests internals) — Verifier confirmed test_transitions.py provides end-to-end coverage of parser → engine; parser unit tests are correctly scoped to assert dict shape (parser's own contract). Skeptic right.
- **C9.x** (production observability) — Off-topic for flaky-tests focus; already covered by prior Bucket A/B sweeps.
- **C13.x** (SessionManager controller wiring) — Off-topic; just addressed in encapsulation pass / Bundle 1-3.
- **C8.x** (state_builder typed return) — Off-topic; J3 carry-over from prior scan, already pinned there.
- **C14.2** (one_level.json:3 outdated comment) — Skeptic argued the self-rewriting IS the auto-validation policy; verifier confirmed comment text but couldn't fully verify the rewrite mechanism. Low-confidence either way; drop.
- **C10.4** (polling intervals brittle under xdist) — `project_pytest_xdist_experiment` deferred xdist; YAGNI to future-proof.

## New finds from VERIFY pass

- **CV2 fix is medium-not-trivial** — verifier confirmed `_collect_diagnostics` calls `item.funcargs.get(fixture_name)` and silently returns when None (no exception, just empty diagnostic block). Generalizing requires both the fixture-side dataclass change AND the hook-side discovery rewrite — not a one-liner.
- **CV1 plan locates rom_key in factory/conftest layer, savestate load in RAPokeEngine** — V18 confirms the followup plan is written exactly the way convergence hunter described. Path-not-rom-key is what gets passed to the engine, preserving its current Protocol-only constructor cleanly.
- **C10.3 (silent detector.step except) was a hallucination** — useful to record for calibration; the test-skeptic and observability lenses both flagged something that doesn't exist in the code.
- **C4.2 dead constants are pre-positioned** — TOOTHPASTE_ROM_NAME / CLEAN_SMW_ROM_NAME are placeholders for when the FRAMEADVANCE-probe-replacement (CV1) re-enables those ROMs. Don't delete in isolation.

## Full merged list (collapsed by lens)

### Lens 1 — Principal architect
- diagnostic hook only covers `replay_ra_dashboard` (conftest.py:572)
- `RAPokeEngine.run_scenario` does too much (ra_poke_engine.py:56-99)
- fixture shape mismatch (run_scenario callable vs replay tuple)
- `run_scenario` function-scoped on session-scoped harness, tests can't request rom_key
- ROM_REGISTRY convention-based dict; no schema
- FRAMEADVANCE probe too narrow (ra_harness.py:250-266)
- helper duplication (polling, config-load, _api wrapper)
- factory two-layer indirection
- Pattern: session-scope creates state leaks; fixture polymorphism without contracts; ROM declaration implicit; engine boundary blurry

### Lens 2 — Control inversion
- run_scenario function-scoped over session-scoped harness (no isolation hook)
- frame_advance/write_ram ordering doesn't fix SPC state leak
- factory caches session-wide; transition tests assume hermetic
- diagnostic hook hardcoded fixture name
- session_manager controller construction (carry-over I1)
- session_manager._clear_ref_and_idle side-effect orchestration
- Pattern: late-binding circular wiring; controllers stateful when stateless preferred; fixture scope mismatch; diagnostic hook pattern locked

### Lens 3 — Test skeptic
- session-scoped harness state leak (test_transitions.py)
- diagnostic hook only covers one fixture
- FRAMEADVANCE probe rejects valid ROMs
- ROM_REGISTRY hidden ROM mapping
- replay_ra_dashboard pytest.skip violates CLAUDE.md (DROPPED — skeptic correct)
- test_poke_parser asserts dict shape only (DROPPED — appropriate scope)
- test_harness_isolation tests test infra (DROPPED — legitimately tests harness)
- test_retroarch_practice_smoke too thin (DROPPED — smoke scoped right)
- test_crash_recovery uses FakeEmuBackend + in-memory DB (real gap; defer)
- test_replay_fixture class-level skipif (DROPPED — fixture checked in)
- polling loops hardcoded (DROPPED — xdist deferred)
- WRAM_SANITY_RETRIES magic numbers (folded into CV1)
- _api / _wait_for_replay_mode timeline opacity (low priority)
- session-loop-scope on per-function tests (low priority)
- _collect_diagnostics silent on sub-failures (folded into CV2)
- Pattern: fixture lifecycle asymmetry; diagnostic hooks incomplete; ROM-keyed registry conflating concerns; tests mocking what should be integrated; timing tests don't scale

### Lens 4 — Dead code
- poller.py:43 mark_state_loaded gravestone
- TOOTHPASTE_ROM_NAME / CLEAN_SMW_ROM_NAME unused (placeholders — keep)
- "three transition tests currently FAIL" comment without xfail
- WRAM_SANITY dishonest naming (folded into CV1)
- TODO(Task 8) in test_dashboard_integration
- conftest.py:43-49 comment refs project memories (folded into CV1 cleanup)
- _collect_diagnostics one-fixture loop (in CV2)
- one_level.json:3 outdated comment (DROPPED — self-rewriting)
- Pattern: gravestones from refactored backends; ROM registry friction; fixture-heavy infra without visibility hooks; honest naming

### Lens 5 — Types
- run_scenario(scenario: dict) -> list (in CV3)
- run_scenario fixture closure yields untyped (in CV2/CV3)
- state_builder.build() -> dict (carry-over J3 — defer)
- _build_speed_run_state untyped base mutation (J3)
- _collect_diagnostics fixture_val Any (in CV2)
- _load_config -> dict (folded into CV2 — IntegrationTestContext)
- parse_poke -> dict (in CV3)
- pokes: list[dict] (in CV3)
- get_paused_state / cold_fill get_state -> dict | None (J3)
- Pattern: state construction anti-pattern; fixture unpacking without validation; scenario DSL stringy; held values implicit; event list untyped

### Lens 6 — Observability
- bare assertions in test_transitions (DROPPED — pytest auto-prints locals)
- diagnostic hook one-fixture (in CV2)
- parse_poke errors no file context (in CV3)
- ra_poke_engine no log on poke fail / io_port=0 (low priority — would spam)
- raclient file-move retry no per-attempt log (off-topic)
- run_scenario timeout opaque on which phase (in CV2)
- nci.read_ram -1 missing request log (off-topic)
- ra_poke_engine silent on detector exception (DEBUNKED)
- cold_fill_detector resync silent (off-topic)
- _collect_diagnostics no DB sanity (in CV2)
- ra_harness probe message doesn't log frozen bytes (folded into CV1 — probe is being deleted)
- poller _read_failing no count (off-topic)
- Pattern: flaky-test blind spots in assertions; diagnostic hook design assumes dashboard; silent recovery patterns; parse errors lack context; retry/timeout no breadcrumbs

## Synergies

- **CV1 absorbs ten findings across three different lens framings** (state leak, FRAMEADVANCE probe, ROM declaration). One architectural move closes the entire flaky-tests cluster. Without CV1, none of those individual findings has a clean fix.
- **CV2 absorbs seven findings** (diagnostic hook hardcoded, fixture shape asymmetry, untyped funcargs, run_scenario closure type, missing fields, silent sub-failures). The IntegrationTestContext dataclass is one boundary that fixes all the symptoms.
- **CV3 likely lands automatically inside CV1** — once CV1 wires fresh_state_path through to the engine, attaching a `PokeScenario` dataclass becomes a one-additional-line change.

## Anti-convergences (per convergence hunter)

- state-leak ≠ ROM-declaration entirely: CV1 absorbs both PARTIALLY but C4.2 (dead constants) and the "tests should declare ROM" enforcement are pure cleanup that could be deferred.
- helper duplication ≠ controller wiring: looks adjacent but live opposite sides of the test/prod boundary; don't bundle.
- production observability ≠ test debuggability: different audiences, log destinations.
