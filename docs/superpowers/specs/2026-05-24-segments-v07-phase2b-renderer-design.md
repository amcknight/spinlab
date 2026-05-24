# Segments-v07 Phase 2b — static HTML renderer — design spec

**Date:** 2026-05-24
**Status:** Approved
**Parent spec:** [2026-05-18-segments-v07-integration-design.md](2026-05-18-segments-v07-integration-design.md) — this fills in Phase 2's "renderer" bullet.
**Sibling work:** Phase 2a (CLI fit inspector, `spinlab fit show / list`) — shipped 2026-05-19.

## Problem

The v07 segments model has been silently fitting every closed segment since Phase 1 shipped on 2026-05-19. As of 2026-05-24 the Beto DB holds 32 fit rows across 19 segments — but **nothing in the dashboard uses them yet**. The allocator still reads Kalman / ExpDecay; the Model tab still shows their numbers.

Before redesigning the Model tab around v07 (Phase 3), the parent spec's decision rubric requires answering three questions against real fits:

1. **Does the model fit?** What fraction of segments PPC-fail?
2. **Are bands informative to the player?** Tight vs. wide M_clear intervals.
3. **Does the learning curve identify?** Or do asymptote-flat segments collapse?

Phase 2a's text dump (`spinlab fit show`) is too dense to triage 19 segments at once. We need a visual surface that lets Andrew eyeball the corpus, pick representative segments, and write findings into a Phase 3 input doc.

## Goal

A one-shot HTML report generator: `spinlab fit render --game <id> --out report.html` produces a single self-contained HTML page with all segments for a game. Each segment's section shows the latest fit visually (curve + bands + raw attempts + PPC) plus a small strip showing how the fit evolved across stored history. Static — no FastAPI, no JS frameworks, no dashboard integration. The output is an artifact Andrew opens once per audit pass and discards.

## Decision — Approach A (greenfield, matplotlib SVG)

A new `python/spinlab/fit_renderer.py` module, sibling to the existing Phase 2a `fit_inspector.py`. matplotlib renders each plot to an inline `<svg>`. Browser handles anchor-link navigation. No JavaScript.

**Approaches considered and rejected:**

- **Approach B — adapt the prototype's `pgm_inspect_v07.py`** (1267 lines). That file is built around a synth→fit→compare-to-truth workflow; adapting it to consume v1 envelopes means rewriting most of the data flow while inheriting baggage. The vendored prototype is frozen per project policy, so we'd be copy-extracting — worst of both worlds.
- **Approach C — Plotly interactive plots.** Plotly inlines ~3MB of JS per page. The user-facing interactivity ("click a segment, see the curve") is anchor-link navigation, not chart-hover. Plotly is premature for an audit tool; revisit in Phase 3 if the live dashboard wants it.

## Scope

### In

1. **New module `python/spinlab/fit_renderer.py`** — pure functions:
   - `build_report(game_label, segments_with_fits_and_attempts) -> str` returns HTML
   - `_render_learning_curve(payload) -> str` returns inline `<svg>`
   - `_render_attempts_strip(attempts) -> str` returns inline `<svg>`
   - `_render_history_strip(fits_oldest_first) -> str` returns a compact HTML table (one row per stored fit; may be revisited as an SVG sparkline if the table proves hard to scan — see Open questions)
   - No DB, no FastAPI, no JAX imports. Pure payload-and-attempt-list in, string out. Same isolation discipline as `fit_inspector.py`.

2. **CLI subcommand** in `python/spinlab/cli.py`:
   ```
   spinlab fit render --game <game_id> --out <path> [--open]
   ```
   - `--game` required
   - `--out` required (path to .html)
   - `--open` optional; calls `webbrowser.open()` after writing

3. **Data loading.** The CLI handler loads, in this order:
   - `db.iter_segment_fit_summaries(game_id)` — one row per segment with a fit (already exists from Phase 2a)
   - per segment: `db.iter_recent_segment_fits(segment_id, limit=50)` — for the history strip
   - per segment: `db.get_all_attempts_by_segment(segment_id)` — for the raw-attempts strip

4. **Per-segment view** (anchor target, repeated for every segment with at least one fit):
   - **Header line:** `[L<level> <start>→<end>] n=<latest_n>  fittable: Y/N  caveats: <comma-separated>`
   - **Headline stats card:** `M_clear <median_ms>s [<p5_ms>s, <p95_ms>s] · death_rate_next <0.xx>`
   - **Learning-curve plot (SVG):** three curves α(n), sf(n), ssp(n) over the full attempt range, with shaded p5–p95 bands derived from the latent posteriors via the model's `theta(n)` formula.
   - **Raw-attempts strip (SVG):** one dot per attempt, green=survived/red=died, x=attempt number, y=time_ms. Sanity-check that fit passes through actual data.
   - **PPC table:** four rows, one per stat, with `obs` value and `p_two_sided`.
   - **History strip:** one row per stored fit in chronological order, columns `n_attempts | fittable | M_clear band | wall_time_ms | fitted_at` so flips are obvious at a glance.

5. **Game-level index** at the top of the HTML: a table listing every segment with status icon (✓ fittable / ✗ not / no-fit), latest n, and an anchor link to its section.

6. **Dependency:** add `matplotlib>=3.8` to the `[fits]` extra in `pyproject.toml`. No other new packages.

7. **Tests:**
   - **Unit (`tests/unit/test_fit_renderer.py`):** three synthetic payloads (one fittable, one unfittable, one with PPC tension). Assert `build_report` returns HTML with expected anchor IDs, expected stat strings present, plot SVGs include at least one `<path>` element. No pixel comparison.
   - **Unit (per plot helper):** given a payload, SVG is non-empty, parses as XML, contains the expected axis count.
   - **CLI subprocess test (`tests/unit/test_fit_render_cli.py`):** seed a small in-memory DB, run `spinlab fit render --game <id> --out tmp.html`, assert file exists, contains expected segment count, parses as HTML.
   - No emulator test — this is offline, DB-only.

### Out (deferred)

- Pool-fit rendering. Add as a Phase 2c if useful — out of scope for the per-segment audit pass.
- Embedded raw JSON payload (collapsible) for power-user inspection — `spinlab fit show --json` already handles that.
- Auto-refresh / file-watcher modes — generate once, look, discard.
- Cross-game comparison views.
- Any dashboard integration — Phase 3 question.
- Allocator changes — explicitly Phase 3+.
- Filtering / pagination / sorting controls — the audit is "look at all of them"; if the segment count grows past what fits on one page, address then.

## Architecture sketch

```
spinlab fit render --game <id> --out report.html
        │
        ▼
cli.cmd_fit_render
   ├── db.iter_segment_fit_summaries(game_id)        # latest-per-segment overview
   ├── for each segment with a fit:
   │     ├── db.iter_recent_segment_fits(seg_id)     # full history
   │     └── db.get_all_attempts_by_segment(seg_id)  # raw attempt rows
   └── fit_renderer.build_report(game, bundle)       # pure: string → string
                │
                ▼
        HTML (matplotlib SVG inline, no JS, anchor-link nav)
                │
                ▼
        write to --out; if --open, webbrowser.open()
```

## Per-segment HTML sketch

```
<section id="seg-49-cp1-cp2">
  <h2>L49 cp1→cp2  ·  n=17  ·  fittable: Y  ·  caveats: low_n</h2>
  <div class="headline">
    M_clear 81.2s [53.2s, 153.2s]   ·   death_rate_next 0.75
  </div>
  <figure>{learning curve svg — α/sf/ssp over n with shaded bands}</figure>
  <figure>{raw attempts strip svg — green/red dots over n}</figure>
  <table class="ppc">
    <tr><td>died_rate</td><td>obs 0.88</td><td>p=0.998</td></tr>
    <tr><td>died_tau_skew</td><td>obs 0.91</td><td>p=0.608</td></tr>
    <tr><td>died_tau_kurt</td><td>obs 0.49</td><td>p=0.364</td></tr>
    <tr><td>died_s_mid_third</td><td>obs 0.20</td><td>p=0.560</td></tr>
  </table>
  <table class="history">
    <tr><th>n</th><th>fittable</th><th>M_clear</th><th>wall_ms</th><th>fitted_at</th></tr>
    <tr><td>5</td><td>Y</td><td>—</td><td>52</td><td>05-22 18:31</td></tr>
    ...
    <tr><td>17</td><td>Y</td><td>81.2s [53.2, 153.2]</td><td>49</td><td>05-22 19:42</td></tr>
  </table>
</section>
```

## Game-level index sketch

```
<table class="index">
  <tr><th>Segment</th><th>n</th><th>status</th><th>M_clear</th></tr>
  <tr><td><a href="#seg-1-entrance">L1 entrance</a></td><td>20</td><td>✗</td><td>—</td></tr>
  <tr><td><a href="#seg-16-entrance">L16 entrance</a></td><td>20</td><td>✗</td><td>—</td></tr>
  <tr><td><a href="#seg-49-cp1-cp2">L49 cp1→cp2</a></td><td>17</td><td>✓</td><td>81.2s</td></tr>
  ...
</table>
```

The status column makes the L1-flip and L16-never-fit pattern visible without scrolling.

## What "done" looks like

After Phase 2b ships:

1. Andrew runs `spinlab fit render --game 01c9321b576c3415 --out beto-2026-05-24.html --open`.
2. Eyeballs the report; picks 5–10 representative segments per the parent spec's instructions.
3. Writes `docs/superpowers/specs/YYYY-MM-DD-segments-v07-phase2-findings.md` answering the three decision-rubric questions.
4. Those answers select the Phase 3 path (B-ish vs C-ish, + optional A) per the parent spec's locked rubric.

The Phase 2b spec does not commit Andrew to a specific findings shape — that doc is written *after* looking at the report.

## Open questions

None blocking implementation. Items the plan-writing pass may need to resolve in passing:

- **History strip — SVG or HTML table?** The design currently shows a table for readability. If a sparkline of n vs. M_clear-median over time would carry more signal, the implementation may switch to SVG with no spec change.
- **Caveat ordering / formatting.** Strings come straight from the prototype's `caveats` array. Pretty-printing (e.g., expanding `low_n` to "low sample count") is a cosmetic tweak left to implementation.

## Risks

- **matplotlib install footprint.** The `[fits]` extra already includes jax/jaxlib/numpyro; matplotlib adds ~30 MB. Acceptable for an audit tool; flagged here so it's not a surprise during plan execution.
- **Plot rendering speed.** ~20 segments × ~3 plots = ~60 matplotlib calls. Empirically ~10–15 ms per simple plot ⇒ ~1 s total wall time. If this balloons (e.g., many-segment games), batch optimization is a follow-up, not a blocker.
- **HTML file size.** SVG plots are text; 60 small plots ≈ 200 KB. Self-contained file ships fine as an artifact.
- **Discoverability.** The CLI is a new subcommand under `spinlab fit`. No dashboard surface, by design. Documented in the Phase 2 findings doc by its existence — Phase 3 specs reference both.

## Resolution log

Decisions made during the 2026-05-24 brainstorm:

- **Scope is per-segment audit, not dashboard integration.** Andrew flagged concern that this isn't moving us toward integration; accepted that the spec's "look before you leap" detour is the explicit bet, with the option to skip 2b later if the CLI alone makes the Phase 3 path obvious.
- **One HTML per game, all segments.** Not one file per segment, not multi-file. Single-file artifact is easy to email/check-in/discard.
- **Per-segment layout: minimal + raw attempts strip.** Headline stats + learning curve + raw-attempts strip + PPC table + history strip. Per-latent posterior bands (the 10-panel small-multiples option) deferred — can be added if Phase 2 findings show fit failures we can't diagnose without them.
- **Latest fit as main view + small history strip.** Spec literally said "single payload"; we add the history strip because the data is already in `segment_fits` and surfacing fit evolution (e.g., L1 entrance going non-fittable at n=20 after being fittable at n=14) is a Phase 2-question signal.
- **matplotlib SVG inline, no Plotly, no JS.** Smallest surface; SVG scales/prints; matches the audit-tool framing.
- **Greenfield renderer, not adapting the prototype's `pgm_inspect_v07.py`.** The prototype's renderer is designed for synth-data inspection workflows; adapting it inherits baggage. The v1 envelope contract is the stable interface.
