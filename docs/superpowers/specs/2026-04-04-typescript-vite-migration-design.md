# TypeScript + Vite Migration Design

## Problem

The SpinLab dashboard frontend is ~500 lines of vanilla ES6 JavaScript across 6 modules, served raw by FastAPI with no build step. This code was written incrementally by AI agents and has:

- **No type safety.** API response shapes, function signatures, and DOM element types are all implicit. Bugs from mismatched field names or wrong argument types pass silently.
- **No tests.** Pure logic (formatting, data transforms) is tangled with DOM manipulation, making it untestable without a browser.
- **No compile-time feedback.** AI agents writing this code have zero signal that something is wrong until Andrew loads the page and notices it misbehaving.

The dashboard will grow — charting, new tabs, richer interactions. The current architecture doesn't scale for confident AI-assisted development.

## Goals

1. **Type safety** — compiler catches mismatched API contracts, wrong field names, bad function calls.
2. **Testability** — pure logic separated from DOM, testable with Vitest in CI.
3. **Charting readiness** — build tooling that makes adding TS-typed charting libraries (uPlot, Chart.js) trivial.
4. **Minimal disruption** — same visual result, same FastAPI serving model, same CSS.

## Non-goals

- No framework adoption (no Preact/React/Svelte). Revisit if/when the UI outgrows vanilla DOM.
- No CSS changes or redesign.
- No new features — this is a pure infrastructure migration.

## Architecture

### Directory structure

```
frontend/                     # New top-level directory
  src/
    app.ts                    # Entry point (was app.js)
    api.ts                    # HTTP/SSE layer
    format.ts                 # Pure formatting utilities
    header.ts                 # Header UI + game selector
    model.ts                  # Model tab UI
    manage.ts                 # Manage tab UI
    types.ts                  # Shared type definitions (API shapes, domain types)
  index.html                  # Root HTML (Vite entry point)
  style.css                   # Unchanged CSS
  vite.config.ts
  tsconfig.json
  package.json

python/spinlab/static/        # Build output target (git-ignored, except .gitkeep)
```

### Key type definitions (`types.ts`)

Define interfaces for:
- **`AppState`** — the shape returned by `/api/state` and SSE events. This is the central contract between backend and frontend.
- **`ModelData`** — shape from `/api/model` (segments, estimators, model outputs).
- **`Reference`, `Segment`** — shapes from `/api/references` and `/api/references/{id}/segments`.
- **`TuningParams`** — shape from `/api/estimator-params`.

These types are derived from reading the existing Python response models and the current JS code. Having them explicit means the compiler catches drift between what the backend sends and what the frontend expects.

### Separation of concerns

Each module gets split into two layers where it makes sense:

- **Pure logic** (testable): data formatting, state derivation, any computation that doesn't touch the DOM. These are functions that take typed inputs and return typed outputs.
- **DOM bindings** (thin): functions that take typed data and update specific DOM elements. These are small and hard to get wrong — they just set `.textContent`, toggle classes, etc.

`format.ts` is already pure logic — it migrates as-is with type annotations. `api.ts` is mostly pure (fetch wrappers). The meaty modules (`model.ts`, `manage.ts`, `header.ts`) have interleaved logic and DOM code that gets teased apart during migration.

### Build and dev workflow

**Development:**
```bash
npm run dev    # Vite dev server with HMR on port 5173
               # Proxies /api/* to FastAPI on port 8000
```

**Production build:**
```bash
npm run build  # Compiles to python/spinlab/static/
               # FastAPI serves these files exactly as before
```

**Tests:**
```bash
npm test       # Vitest runs tests against pure logic modules
```

### Vite configuration

- Output directory: `python/spinlab/static/` (same place FastAPI already mounts).
- Dev server proxy: `/api` and `/api/events` forward to `http://localhost:8000`.
- Source maps in dev, minified output in production.

### FastAPI changes

Minimal. In dev mode, you'd access the Vite dev server directly (`localhost:5173`) which proxies API calls to FastAPI. In production, FastAPI serves the built files from `static/` exactly as it does now. The `NoCacheStaticMiddleware` and static mount stay unchanged.

The only code change: `index.html` moves to `frontend/` (Vite's entry point), and the built output replaces it in `static/`. The `root()` endpoint continues serving `static/index.html`.

### What gets tested

With Vitest + `happy-dom` (lightweight DOM shim):

- **`format.ts`**: `segmentName()`, `formatTime()`, `elapsedStr()` — pure functions, easy to test exhaustively.
- **`api.ts`**: `fetchJSON()`, `postJSON()` — mock `fetch`, verify error handling and toast behavior.
- **`types.ts`**: Type-level only — no runtime tests, but the compiler validates all usage.
- **Logic extracted from `model.ts`/`manage.ts`**: Any data transforms, state derivation, conditional display logic that currently lives inline with DOM code.

DOM rendering itself is not tested — it's kept thin enough that type safety + visual inspection covers it.

## Migration strategy

File-by-file, one module at a time:

1. **Scaffold** — `package.json`, `vite.config.ts`, `tsconfig.json`, proxy setup.
2. **`types.ts`** — define API response interfaces by reading the Python models and existing JS.
3. **`format.ts`** — rename, add types, write tests. Zero DOM, easiest starting point.
4. **`api.ts`** — add types, write tests for fetch/SSE logic.
5. **`header.ts`** — add types, extract any testable logic.
6. **`model.ts`** — add types, separate pure logic from DOM updates, test the logic.
7. **`manage.ts`** — same as model.
8. **`app.ts`** — add types to orchestration, update imports.
9. **`index.html`** — move to `frontend/`, update script tag for Vite.
10. **Wire up build** — verify `npm run build` produces working output in `static/`, verify FastAPI serves it.

Each step produces a working dashboard — no big-bang switchover.

## Charting path

Once this migration lands, adding a charting library is:

```bash
npm install uplot     # 35KB, fast, TS types included
```

Then import it in the relevant module with full type safety. uPlot is the likely first choice for SpinLab — it's designed for real-time time-series data, handles streaming updates well, and is tiny. Chart.js is the fallback if uPlot's API is too low-level for a specific visualization.

## Risk and rollback

- **Risk:** The migration is mechanical — rename files, add types, fix compiler errors. The main risk is subtle behavior changes from misunderstanding the existing JS. Mitigation: migrate one file at a time, visually verify the dashboard after each.
- **Rollback:** The old JS files stay in git history. If something goes wrong, `git revert` the migration commits and the dashboard is back to vanilla JS.
