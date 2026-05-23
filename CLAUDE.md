# SpinLab

Efficient practice system for SNES romhack speedrunning. Captures save states at segment boundaries during reference runs, serves them back in an intelligent practice loop. See `docs/ARCHITECTURE.md` for component details and design decisions.

## Coding Guidelines

- Python 3.11+. Type hints everywhere. `dataclasses` for models.
- YAML for config (Andrew's preference).
- The kaizosplits C# code in `reference/` is read-only reference — never import or compile it.

## Modeling & Numerics

- **No magic numbers.** Every numeric constant gets a named file-level variable with a comment explaining *why* that value.
- **No fudge factors.** If a model needs a tuning knob, it's a real parameter with a name, a unit, and a rationale — not a bare `0.7` buried in an expression.
- **Derive from principles first.** Prefer values that come from math, measurement, or domain knowledge. If a constant is empirical, document what it was tuned against and how to re-derive it.
- **Labels and thresholds must be earned.** Don't attach qualitative labels ("high confidence", "fast drift") to arbitrary cutoffs. If a threshold matters, justify it; if it doesn't, remove it.
- **Defaults in config, not in code.** Tunable parameters belong in YAML config or dataclass defaults with docstrings, not scattered through logic.

## Testing

Red-Green TDD. Keep only tests that document behavior or catch regressions.

Two markers, two suites:

- **Fast tests:** `pytest -m "not emulator"` (~16s, ~790 tests). The default workhorse — runs after every code change. Includes the frontend smoke tests, which require `cd frontend && npm run build` first and a Playwright Chromium install (no `frontend` marker — the environment is assumed).
- **Emulator tests:** `pytest -m emulator` (~36s, ~12 tests). Requires RetroArch installed. The RA poke harness (`RAHarness`/`RAPokeEngine`) drives every transition scenario, plus the end-to-end replay fixture (`test_replay_fixture.py`). Speed comes from clustered snapshot reads (6 NCI round-trips per frame instead of 11), FAST_FORWARD-on-replay (the 2273-frame movie finishes in ~3s instead of 38s), and quiescence-based scenario termination (scenarios end when the detector goes silent, not after a fixed 60-frame settle).
- **Everything:** `python -m pytest` (~52s). Run this BOTH as a baseline at the start of any code-changing session AND before declaring work done. Not `pytest -m "not emulator"`, not a subset — the full unfiltered suite. A red suite is never acceptable.

  **Two rules that are not optional:**

  1. **Skips count as failures.** A `SKIPPED` line means the test did not execute — same epistemic value as not running. Treating `1 passed, 11 skipped` as "tests pass" is the rationalization that keeps biting us. The harness conftest is built to start RA itself; if every emulator test skips with `ra_harness launch failed`, that is a bug to surface, not a green light to commit. The only acceptable skips are `@pytest.mark.skipif`'d cases with a written reason that Andrew has already accepted. Anything else — fixture launch failure, missing config, environmental drift, an env var the harness needs (e.g. `SPINLAB_TEST_ROM`) — is a failure, not a skip. Surface it before any commit.

  2. **Pre-existing failures are still failures.** "It was already broken on main" is not an excuse to leave it broken. Every time work has shipped over a red baseline, the next session inherits the red baseline as noise and the bug ages further. If the baseline run reveals failures, stop and ask before touching code; either fix them as the first commit of this session, or get explicit sign-off to defer and write a follow-up task — never silently move on.

  3. **Every flake must be documented before it is dismissed.** A test that fails intermittently is not a "pre-existing flake" until it is recorded in `memory/project_test_reliability_known_issues.md` with: the test name, observed failure signature, when first seen, and a hypothesis. "It passed in isolation" or "it's probably a race" are not acceptable substitutes for a written entry. The bar for a new flake entry is low — a sentence is enough — but the bar for silently ignoring a failure is zero. This rule exists because we have a long history of flakes that were dismissed session after session and never fixed.

**Replay fixture:** `tests/integration/test_replay_fixture.py`. Uses `tests/fixtures/love_yourself/one_level.replay` recorded on live RA. Validates the full replay→segment-capture pipeline; expects `state["replay"]["total"]` to populate from `ReplayStartedEvent.frame_count`. Gates on `sections_captured` (the content milestone) rather than wall-clock — the test ends as soon as the expected segments land in the DB, so the speedup from `FAST_FORWARD` actually shows up.
- **Schema changes:** add a new `python/spinlab/db/migrations/NNNN_name.sql`. Files are immutable once shipped — fix mistakes with another migration. Runner details: `python/spinlab/db/migrations/__init__.py`.
- **DB reset:** `spinlab db reset [--config config.yaml]` — deletes and recreates the database. Useful for local dev when you want a clean slate; not for prod recovery (use migrations).
- **Frontend tests:** `cd frontend && npm test` (~2s). Vitest + happy-dom. Pure logic and API contract tests.
- **Coverage:** `./scripts/coverage.sh` (unit), `--all` (unit+emulator), `--html` (opens report).

### Static Analysis

- **Type check:** `npx pyright python/` — same engine as Pylance in VS Code. Run when changing function signatures, model types, or TypedDict shapes.
- **Lint:** `ruff check python/` — unused imports, dead code, style. `ruff check --fix python/` auto-fixes safe issues.
- Don't introduce new errors. Existing errors are tracked and will be cleaned up over time.

### Integration test diagnostics

When an emulator/integration test fails, a diagnostic block is automatically appended to the pytest report showing: `/api/state` snapshot, DB row counts (segments, capture_runs, drafts), RA process status, and the last 30 lines from the `spinlab` logger ring buffer. Implemented in `tests/integration/conftest.py` via `pytest_runtest_makereport` hook. Use this output to diagnose intermittent failures — it captures the state that would otherwise be lost.

### Gotchas

- ROM overwrites memory every frame — RA poke engine holds values persistently (see `RAPokeEngine` in test conftest)
- `cheevos_hardcore_mode_enable = "false"` required in `retroarch.cfg` — RA silently drops NCI savestate commands when hardcore is on
- `run_ahead_secondary_instance = "true"` required — single-instance runahead corrupts save state buffers
- RA `log_to_file = "true"` is required cfg for the log-based replay slot parser in `MoviePlayer` to work

### Address map

`tests/integration/addresses.py` builds `ADDR_MAP` by importing constants from `python/spinlab/retroarch/addresses.py` (the source of truth). New SMW addresses get added once, in the production module; the integration map auto-picks them up.

## Frontend (TypeScript + Vite)

Source lives in `frontend/src/`. Built output goes to `python/spinlab/static/` (git-ignored).

- **Dev server:** `cd frontend && npm run dev` (port 5173, proxies /api to FastAPI on 8000)
- **Build:** `cd frontend && npm run build`
- **Tests:** `cd frontend && npm test`
- **Type check:** `cd frontend && npm run typecheck`

Run `npm run build` after frontend changes before testing with FastAPI directly.

**Frontend types are codegen'd from FastAPI's OpenAPI schema** by `npm run gen-types` (also invoked automatically by `npm run dev` and `npm run build`). The pipeline: `scripts/dump_openapi.py` writes `frontend/openapi.json` → `openapi-typescript` writes `frontend/src/api-types.ts`. `frontend/src/types.ts` is a thin re-export facade that gives the rest of the frontend friendly names (`AppState`, `ModelData`, etc.). Source of truth: `python/spinlab/api_schemas.py` — edit there, and the frontend types update on the next dev/build.

## Logging

Dashboard logs to `{data_dir}/spinlab.log` (rotating, 1 MB max, 3 backups). Configured automatically on `spinlab dashboard` startup. All `logger.info()` / `logger.warning()` / `logger.exception()` calls go to this file.

## Merging Branches

Before merging any branch to main, run the **full** test suite: `python -m pytest`. This includes unit, emulator, and frontend tests. All must pass. Do not merge with `pytest -m "not emulator"` or other partial runs — that's how bugs slip through.

## Worktrees

Worktrees live in `.worktrees/{name}/` with branch `worktree/{name}`.

- **Main checkout:** Full access to dashboard, TCP, emulator, Playwright.
- **In a worktree:** Code edits and unit tests OK. Binding ports, Playwright, or emulator — ask first.
- **Editable installs:** If imports fail in a worktree, re-run `pip install -e .` from worktree root. When merging back to main, re-run `pip install -e .` from main to fix the package path.
- **Cleanup:** `git worktree remove .worktrees/{name}` → `git branch -d worktree/{name}` → `git worktree prune`

## Superpowers Visual Companion (Windows)

Launch with `--foreground` and `run_in_background: true` — background mode dies immediately on Windows.

## Running in a Linux sandbox / container

Bootstrap the environment first:

```bash
scripts/bootstrap-sandbox.sh
export PATH=/tmp/spinlab-env/bin:$PATH
```

Don't pip-install by hand or use a different venv path — reuse the script.

### Line endings in sandboxes

`.gitattributes` normalizes on commit, but sandbox git can still commit CRLF when Windows working-tree bytes leak through a mount. Before committing from a sandbox:

```bash
git ls-files --eol <changed files>
```

Every file should show `i/lf`. If you see `i/crlf` or `i/mixed`, `git add --renormalize <file>` and recommit.
