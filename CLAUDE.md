# SpinLab

Efficient practice system for SNES romhack speedrunning. Captures save states at segment boundaries during reference runs, serves them back in an intelligent practice loop. See `docs/ARCHITECTURE.md` for component details and design decisions.

## Coding Guidelines

- Python 3.11+. Type hints everywhere. `dataclasses` for models.
- Lua: readable, liberal comments. Memory addresses in config section at top of script.
- YAML for config (Andrew's preference).
- The kaizosplits C# code in `reference/` is read-only reference — never import or compile it.

## Testing

Red-Green TDD. Keep only tests that document behavior or catch regressions.

- **Unit tests:** `pytest tests/` (~30s). In-memory SQLite, mocked TCP, FastAPI TestClient.
- **Integration tests:** `pytest -m integration` — Mesen2 headless mode. See `tests/integration/README.md`.
- **Coverage:** `./scripts/coverage.sh` (unit), `--all` (unit+integ), `--html` (opens report).

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
