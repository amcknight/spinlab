# Vite Dev Server Integration

**Date:** 2026-04-07
**Status:** Approved

## Problem

Starting the SpinLab dashboard requires two manual steps: `spinlab dashboard` and `cd frontend && npm run dev`. The second step is easy to forget, leaving the user with no UI.

## Decision

The `spinlab dashboard` command spawns the Vite dev server (`npm run dev`) automatically. The user opens `localhost:5173` for the UI; Vite proxies `/api` requests to FastAPI on the dashboard port (already configured in `vite.config.ts`).

## Design

### Subprocess Management

On dashboard startup, after config/DB setup but before `uvicorn.run()`:

1. Spawn `npm run dev` as a subprocess with `cwd=frontend/` (resolved relative to the project root via `__file__` or config).
2. Pipe stdout/stderr and log output to `spinlab.log`.
3. **Startup check:** Wait up to 10 seconds for Vite to become ready (poll port 5173 for TCP accept). If it fails, abort the dashboard with a clear error message.
4. **Lifecycle:** The subprocess is terminated during FastAPI lifespan shutdown. Send SIGTERM, then SIGKILL after a short grace period. The AHK `Ctrl+Alt+X` shutdown already kills the dashboard process tree (`taskkill /T`), which covers this.

### Static File Serving Removal

- Remove the `/static` mount from `dashboard.py`.
- Remove `NoCacheStaticMiddleware`.
- Remove the root route that serves `index.html` or the "Frontend not built" error.

### Port File Update

Add `vite_port=5173` to `.spinlab-ports` so external tools (AHK, etc.) can open the correct URL.

### What Doesn't Change

- Config loading, DB setup, TCP manager, session manager, practice loop.
- Emulator launch via `/api/emulator/launch`.
- `npm run build` still works (just no longer the default path).
- `vite.config.ts` proxy configuration (already correct).

### Error Handling

Fail hard. If the Vite dev server doesn't start (missing `node_modules`, port conflict, Node not installed), the dashboard refuses to start and prints a clear diagnostic.

## Files Changed

- `python/spinlab/cli.py` — spawn Vite subprocess, startup check, write `vite_port` to ports file
- `python/spinlab/dashboard.py` — remove static mount, middleware, root route; manage Vite subprocess lifecycle in lifespan
