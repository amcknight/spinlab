# Merge Setup Segments Tables — Design

**Date:** 2026-06-20
**Status:** Approved, ready for implementation plan

## Problem

The Setup page lists every segment **twice**:

1. **`#segment-table`** (the "Segments" editor, `manage.ts`): run-scoped. Columns
   — Session # · Name (editable) · Level · Segment (start→end) · State (cold
   ✅ / ❌ fill-gap) · delete ✕. Source: `/api/references/{id}/segments` →
   `ReferenceSegment`.
2. **`#segments-view-container`** (the untitled grouped view at the bottom of
   Setup, `segments-view.ts`): game-scoped, grouped by Level. Columns — Segment
   (start→end) · Conditions · Primary (checkbox) · Cold (✓/✗). Source:
   `/api/segments?game_id=` → `ApiSegment`.

Both render the same underlying segment rows in two different shapes. They share
the start→end and cold-state columns outright. The bottom table is game-scoped,
so when more than one run exists it shows cross-run segments that are noise for a
single-route practice workflow.

(For reference, the Play page also lists segments twice — Model State and
Practice Simulator — but that is **out of scope** here. This design covers only
the two Setup tables.)

## Goal

Collapse the two Setup tables into **one Route-scoped table, grouped by Level**,
with a per-row expander hiding the rarely-needed columns so the default view
stays uncrowded.

## Scope decision: everything is per-Route

The table is scoped to the **active reference run** — its rows are the segments
that run traversed. This is the single move that makes the merged table "feel
per-Route" and removes the cross-run noise from the old game-scoped view.

Per-column intrinsic ownership (for the record — informs nothing structural now
that the table is uniformly run-scoped):

| Column | Intrinsic owner | Note |
|---|---|---|
| Level | Game (geography) | becomes the section header |
| Segment (start→end) | Game (geography) | identity of the segment |
| Conditions (`start_conditions`) | Game (geography) | entry state |
| Session # (`session_ordinal`) | Route | which capture session in this run produced it |
| Cold / State (`has_cold_state`, `state_path`) | Route | savestate captured in this run |
| Name (`description`) | judgment call → treat per-Route | see Known Limitation |
| Primary (`is_primary`) | judgment call → treat per-Route | see Known Limitation |

## Layout

Group by Level. One `Level N` section header per level; segments within a level
sorted by `start_ordinal`.

**Base row (always visible) — 4 columns + chevron:**

```
Level 5
 ▸  Yoshi's House___  entrance → goal   ☑ Pri  ✅
 ▾  Donut Plains 1__  midway   → goal   ☐ Pri  ❌ Fill
       Conditions: powerup=cape, yoshi=false
       Session 2  ·  state: dp1_mid.state        [Delete]

Level 6
 ▸  Green Switch____  entrance → goal   ☑ Pri  ✅
```

- **chevron** — expand/collapse the detail row
- **Name** — editable input (PATCH `description` on blur)
- **Segment** — `start → end` endpoint label (read-only)
- **Primary** — checkbox (PATCH `is_primary` on toggle)
- **Cold** — ✅ when `has_cold_state`; otherwise an ❌ **Fill** button that
  triggers fill-gap for that segment

**Detail row (revealed by the chevron):**

- **Conditions** — `start_conditions`, formatted `k=v, k=v` (the main
  width-hog; this is why it moves off the base row)
- **Session #** — `session_ordinal`
- **state path** — `state_path` (diagnostic)
- **Delete** — destructive ✕ action, tucked away to avoid accidental clicks

## Data source — one API change (access only, not storage)

The merged table is run-scoped, so it reads from the run-scoped query
(`get_segments_by_reference`, already traversal-membership based on `main`).
That serialization (`ReferenceSegment`) currently lacks three fields the merged
table needs, which today come only from the game-scoped `ApiSegment`:

- `is_primary`
- `has_cold_state`
- `start_conditions`

**Change:** widen the run-scoped segment serialization to include these three
fields. They already exist on the segment rows in the DB and are already
returned by `ApiSegment` — this is purely widening what the run-scoped endpoint
exposes. **No migration, no new column, no change to where or how anything is
stored.** Result: the merged table reads everything from a single fetch and the
dual-fetch in `manage.ts` / `segments-view.ts` collapses to one.

## Preserved behaviors

- Name PATCH on blur, Primary PATCH on toggle, fill-gap on ❌, delete-with-confirm.
- The cold-capture toolbar (`#segments-toolbar` / Start Cold Capture) and the
  cold-fill banner (`#cold-fill-banner`) stay directly above the merged table.
- Recording, Run-paused, and Data sections of Setup are unchanged.

## Removed

- The standalone game-scoped grouped view (`#segments-view-container` and
  `segments-view.ts`'s render path) — its capabilities (Primary, Conditions,
  Cold, Level grouping) all fold into the merged table.
- The explicit **Level** column (now a section header).
- Cross-run segment rows — a run-scoped table only lists the active run's
  traversed segments.

## Placement

The merged table replaces the existing **Segments** section in its current spot
(after Run-paused, before Data). The old untitled grouped container at the very
bottom of Setup is deleted.

## Known limitation (deferred, accepted)

Segment `id` is geography-keyed (`game:level:start:end:waypoints`, no run or
category), so the *same row* is shared across every run that traverses that
geography. `description` (Name) and `is_primary` (Primary) are stored on that
shared row, not per-run. Consequently, although the UI behaves per-Route (each
run's table lists only its own traversed segments), an edit to Name or Primary
**bleeds into any other run that shares the geography** — there is only one place
to store the value.

This is invisible in a single-active-route workflow and Andrew does not currently
rename segments. Making Name/Primary truly per-run would require a per-run storage
location (new column or table) — real schema work, out of scope here. Documented
now; revisit only if overlapping multi-route maintenance becomes a real need.

## Out of scope

- The Play-page tables (Model State, Practice Simulator).
- Per-run storage of Name/Primary (the Known Limitation above).
- Any change to the run↔segment membership / traversal queries (already shipped).
