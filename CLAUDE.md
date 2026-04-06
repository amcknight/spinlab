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
- **Static asset tests:** `pytest -m frontend`. Requires `cd frontend && npm run build` first.
- **Everything:** `pytest` (~30s). Run before committing.
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

## Worktrees

Worktrees live in `.worktrees/{name}/` with branch `worktree/{name}`.

- **Main checkout:** Full access to dashboard, TCP, emulator, Playwright.
- **In a worktree:** Code edits and unit tests OK. Binding ports, Playwright, or emulator — ask first.
- **Editable installs:** If imports fail in a worktree, re-run `pip install -e .` from worktree root.
- **Cleanup:** `git worktree remove .worktrees/{name}` → `git branch -d worktree/{name}` → `git worktree prune`

## Superpowers Visual Companion (Windows)

Launch with `--foreground` and `run_in_background: true` — background mode dies immediately on Windows.
