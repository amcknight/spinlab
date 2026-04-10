# SpinLab

Efficient practice system for SNES romhack speedrunning. Captures save states at segment boundaries during reference runs, serves them back in an intelligent practice loop. See `docs/ARCHITECTURE.md` for component details and design decisions.

## Coding Guidelines

- Python 3.11+. Type hints everywhere. `dataclasses` for models.
- Lua: readable, liberal comments. Memory addresses in config section at top of script.
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

- **Fast tests:** `pytest -m "not (emulator or slow or frontend)"` (~23s). Run after any code change.
- **Slow tests:** `pytest -m slow` (~4s). Run when touching practice loop or TCP wait logic.
- **Emulator tests:** `pytest -m emulator` (~6s). Run when touching Lua scripts or transition detection.
- **Smoke tests:** Included in `pytest -m emulator`. Full-stack: Mesen headless + dashboard + DB. See `tests/integration/test_smoke.py`.
- **Static asset tests:** `pytest -m frontend`. Requires `cd frontend && npm run build` first.
- **Everything:** `pytest` (~30s). Run before committing.
- **DB reset:** `spinlab db reset [--config config.yaml]` — deletes and recreates the database. Useful after schema changes during development.
- **Frontend tests:** `cd frontend && npm test` (~2s). Vitest + happy-dom. Pure logic and API contract tests.
- **Coverage:** `./scripts/coverage.sh` (unit), `--all` (unit+emulator), `--html` (opens report).

### Gotchas

- `emu.isKeyPressed()` crashes in `--testRunner` — guarded with `pcall` in `spinlab.lua`
- ROM overwrites memory every frame — poke engine holds values persistently
- TCP requires `tcp-nodelay` to avoid Nagle buffering at max emulation speed

### Address maps (must stay in sync)

- `lua/spinlab.lua` lines 43-53 (source of truth)
- `lua/poke_engine.lua` ADDR_MAP
- `tests/integration/addresses.py` ADDR_MAP

## Frontend (TypeScript + Vite)

Source lives in `frontend/src/`. Built output goes to `python/spinlab/static/` (git-ignored).

- **Dev server:** `cd frontend && npm run dev` (port 5173, proxies /api to FastAPI on 8000)
- **Build:** `cd frontend && npm run build`
- **Tests:** `cd frontend && npm test`
- **Type check:** `cd frontend && npm run typecheck`

Run `npm run build` after frontend changes before testing with FastAPI directly.
Types in `frontend/src/types.ts` must stay in sync with Python response models.

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
