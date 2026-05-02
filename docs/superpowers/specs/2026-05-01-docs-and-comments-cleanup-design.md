# Docs & Comments Cleanup — Design

**Date:** 2026-05-01
**Status:** Approved — ready to execute
**Scope:** Live docs only (superpowers/ specs and plans are frozen and out of scope)

## Why

Live docs have rotted: README references commands that don't exist (`spinlab capture`, `spinlab stats`), estimator names (`model_a`, `model_b`) that were renamed to `rolling_mean` / `exp_decay` long ago, a `capture_controller.py` module that doesn't exist, a `docs/DESIGN.md` link that is wrong, and a project layout that still describes `static/` as HTML/JS/CSS instead of Vite-built TS. A second architecture doc (`DESIGN.md` at repo root, 601 lines) competes with the actual current `docs/ARCHITECTURE.md` (56 lines) — neither references the other and they disagree.

Two backlog files (`future.txt`, `multi-session-followups.txt`) sit at the repo root in plain text with stale "Wave 2" framing and no integration with the model-improvements spec.

In-code comments suffer from the same rot: file-path-as-header banners, section-label decorations, what-comments restating the next line, "previously did X" historical narration, and a Lua header still announcing "Step 4 MVP" that has long since shipped.

The codebase has exactly **one** TODO (`practice.py:183-190`); zero in Lua and TS. The half-finished promises live in prose, not markers.

## Phases

### Phase A — Doc structure + content

1. Read root `DESIGN.md`. Salvage still-true sections (DB schema overview, IPC contract, scheduler pipeline) into `docs/ARCHITECTURE.md`. Discard "Build Order" / "Open Questions" / anything contradicted by current code.
2. Expand `docs/ARCHITECTURE.md` with: IDLE→RECORDING→PAUSED state machine, the three "session" concepts (capture_session vs practice session vs `attempts.parent_id`), capture_runs, paused-run lifecycle, recovery flow.
3. Rewrite README sections — CLI commands (drop nonexistent ones, add `db reset`), Project Layout (current package tree), `static/` description, Dashboard tabs (add Manage), How It Works. Add multi-session callout.
4. Update `docs/GLOSSARY.md`: add `capture_run`, `capture_session`, `paused_run`, `parent_id`, `recorded_segment_times`. Drop deprecated StartPoint entry if not referenced in code.
5. Reconcile `docs/model-improvements-spec.md`: mark Phase 1 status, move "Bug Fixes from this testing session" out (it's changelog, not spec). Strip "Model A" / "Model B" headers — use `Rolling Mean` / `Exp Decay`.
6. Convert `future.txt` + `multi-session-followups.txt` → `docs/BACKLOG.md` (markdown, organized by area, drop already-shipped items).
7. Delete root `DESIGN.md`, `future.txt`, `multi-session-followups.txt`.
8. Replace `model_a` / `model_b` in live docs with the real names (`rolling_mean` / `exp_decay`). Superpowers tree is excluded.

### Phase B — Verifications + Python TODO

9. Verify `db/capture_runs.py:125` FK CASCADE comment against schema.
10. Verify `routes/reference.py:48` ordinal-ordering claim.
11. Triage `practice.py:183-190` TODO — likely move to BACKLOG.md and remove the marker.
12. Tighten 5-6 verbose multi-line WHY blocks where identified by the audit.

### Phase C — Mechanical comment sweep

13. Strip file-path-as-first-line headers across Python.
14. Strip `# -- Section --` / `# === banner ===` decorative banners across Python and TS.
15. Strip what-comments restating obvious code.
16. Strip historical framing ("previously / was X / pre-multi-session / v1 / legacy fallback") — keep the operative WHY where one exists.
17. Replace Lua `spinlab.lua:1-3` archeology header with a one-line current scope.
18. Strip `frontend/src/api-contract.test.ts:131-132` historical bug-narrative.

## Out of Scope

- Anything under `docs/superpowers/specs/`, `docs/superpowers/plans/`, or `docs/superpowers/archive/` — those are frozen historical artifacts.
- Test files — comment audit covered Python source and TS source only.
- Implementing the deferred items in BACKLOG.md.

## Verification

After each phase: `python -m pytest` (full suite per CLAUDE.md), `npx pyright python/`, `ruff check python/`, `cd frontend && npm test && npm run typecheck`.
