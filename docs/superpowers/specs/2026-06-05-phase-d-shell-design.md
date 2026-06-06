# Phase D — Two-Tab Shell (Practice UI capstone) — Design Spec

**Date:** 2026-06-05
**Status:** Skeleton approved via visual brainstorm (mockups in `.superpowers/brainstorm/`). First iteration to be planned and built, then re-evaluated live ("start this process again once we see it running IRL").
**Parent:** Plan D of the practice UI overhaul (`2026-06-01-practice-ui-overhaul-design.md`). Composes the shipped A/B/C work and D-Live (`2026-06-02-live-practice-view-design.md`) into one shell. This spec is the **D-Layout** sub-project — the responsive/state-driven composition — reframed by the brainstorm into a two-tab shell.

## Guiding principle

**Stay close to the existing Practice/HyperPlay live view (D-Live).** It is "pretty nice" already and is the gold standard. Phase D *promotes and surrounds* it — it does not rewrite it. Everything else reorganizes around that centerpiece.

## The locked structure

Collapse today's **four tabs** (Model / Manage / Segments / Simulator) into **two**, plus a **shared spine bar** that is always visible on both:

```
┌─ SHARED SPINE (always visible) ────────────────────────────┐
│  <Game> · <category>   Exp.Run · Exp.Deaths · Saved 6.2s ● │
│  (the whole-run / "Run" context — lives HERE, on the shell) │
├────────────────────────────────────────────────────────────┤
│  [ PLAY ]  ·  [ SETUP* ]    ← tiny edge tab, horizontal sweep│
└────────────────────────────────────────────────────────────┘
```

- **Navigation = one tiny edge tab** clinging to the side it sweeps toward (right edge "Setup" in Play; flips to left edge "Play" in Setup). The transition is a **horizontal slide**, not a disappear/reappear. The two pages carry slightly different background tints so you always know where you are. Exact tab placement is tuned once we know where real content lands (must not cover info).
- **The spine is the persistent context.** Game title + whole-run aggregates (Exp. Run, Exp. Deaths) + "Saved this session" with a live-ticking session clock. This is the run-level info, and it is available the whole time — independent of which tab or practice state you are in.

### PLAY page (was: Model + Simulator)

The live view, promoted to the centerpiece, with the rest of the model surfaces stacked below it. **Density is driven by practice state, not by scrolling:**

- **While practicing/HyperPlay — compact, no scroll** (it is used LiveSplit-style beside the game, and it is hard to read mid-play):
  - The **focused segment** = the current segment's D-Live view (segment summary + episode-time graph + climbing dot). Nothing else.
- **When stopped/idle — the same surface unfolds** (now you can study what you just did; scrolling is fine):
  - **Deep-dive** on the focused segment — a **swappable graph slot** (episode trend / clean-clear trend / death histogram / expected-time distribution; possibly more than one visible eventually). Contents decided live; do not pre-commit.
  - **All segments** — the Model State table, relocated here. Clicking a segment **focuses it** in the live view above (master-detail; defaults to the segment you just practiced).
  - **Practice next** — the Simulator, merged and simplified (ranked list; Advanced collapsed).

### SETUP\* page (was: Manage + Segments)

The "set up / record / manage" surface — distinct *kind* of activity from the practice loop, but inside the same shell:

- References · Start Reference Run
- Replay / Fast Replay
- Cold capture · Paused run
- **Segments table** (merged in from the old Segments tab — rarely used, no longer deserves a top-level tab)
- Data · Clear all

**\*Name is open and explicitly NOT "Runs"** — the run-level info lives on the spine/Play side, so "Runs" would mislead. Candidates: Setup / Manage / Capture. Finalize during build.

## What is deferred to live iteration (do NOT design up front)

Per the PoC-first preference, the following are intentionally left to tune once the shell is running on real RA data:

- The exact focus interaction (click-to-focus behavior, what "focus" persists across SSE pushes).
- Which graphs occupy the swappable slot, defaults vs picker, and whether multiple show at once.
- The second tab's final name.
- Whole-run *graphs* (as opposed to the spine's aggregates): wanted, available the whole time, belong on Play — placement TBD.
- D-Live-FE3 polish (climbing dot already specced; session-start line; flash-on-change).
- Picking up the deferred Plan-A polish (last-attempt callout, PB marker, death-rate arrow) and wiring C's Now/Baseline window picker into the verdict.

## Precursor already landed this session

The **Practice-not-showing bug** is fixed (working tree, uncommitted): `start_practice` broadcast SSE before `run_loop` had selected the first segment, and nothing re-broadcast on segment load, so the live card stayed hidden until the first attempt result (HyperPlay was immune — its `current_segment` exists at start). Fix: an `on_segment_load` callback in `PracticeSession.run_one`, wired to `_notify_sse` in `start_practice`. Red→green unit test added. This is a prerequisite — the Play page is unusable in Practice mode without it.

## Scope

- **Frontend-led restructure.** No modeling changes. The first iteration is the *shell*: two tabs + shared spine + sweep, the existing D-Live view as the Play centerpiece, the Model table + Simulator relocated under Play, Manage + Segments merged into Setup. Reuse existing components (`live-view.ts`, `route-bar.ts`, `segment-summary.ts`, `episode-graph.ts`, `practice-engine.ts`, `segments-view.ts`, `model.ts`); relocate, don't rewrite.
- Kill the surviving legacy widgets that overlap the live view (the old `improvement-view` / `savings-panel` / `em-suite-panel` siblings in `#practice-card`) only where they duplicate the D-Live view — confirm during planning by reading current `index.html` / `model.ts`.

## Testing

- Playwright smoke: both tabs render; the sweep toggles; spine persists across the toggle; Play shows the live view while practicing and unfolds the table/simulator when idle; Setup shows references/segments/data.
- Vitest for any new pure helpers (tab state, sweep state).
- Full `pytest` (incl. emulator) green before merge.

## Iteration loop

Build the shell → run it on real RA data → re-brainstorm the next slice (focus interaction, graphs, naming, FE3 polish) from what we see. This spec is the structural anchor, not the final word.
