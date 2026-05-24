# Segments-v07 Phase 2b Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a one-shot static HTML report generator (`spinlab fit render --game <id> --out report.html`) that visualizes every segment's latest v07 fit plus its fit history, so Andrew can eyeball the Beto corpus and write the Phase 2 findings doc.

**Architecture:** New pure-Python `fit_renderer` module (matplotlib SVG inline; no JS; no DB; no JAX). New `cli_fit_render` handler mirrors the Phase 2a `cli_fit_inventory` / `cli_fit_rebuild` pattern and registers under `spinlab fit render`. Data loaded via existing Phase 2a DB helpers (`iter_segment_fit_summaries`, `iter_recent_segment_fits`) plus `get_segment_event_rows` for the raw-attempts strip.

**Tech Stack:** Python 3.11+, matplotlib (new dep on `[fits]` extra), existing `spinlab.db` mixins, `argparse`, `webbrowser` (stdlib), pytest for tests.

**Spec:** [2026-05-24-segments-v07-phase2b-renderer-design.md](../specs/2026-05-24-segments-v07-phase2b-renderer-design.md)

**Branch:** `feat/segments-v07-phase2b-renderer` (spec already committed at 4e67744 off main).

---

## File map

**Create:**
- `python/spinlab/fit_renderer.py` — pure renderer. Functions: `build_report(game_label, game_id, bundle)`, `render_segment_section(...)`, `render_learning_curve_svg(payload)`, `render_attempts_strip_svg(events)`, `render_ppc_table_html(payload)`, `render_history_table_html(fits_newest_first)`, `render_game_index_html(summaries)`, plus an `_anchor_id(segment_row)` helper and a `_theta_n(log_inf, log_1, log_halflife, n)` helper for the learning-curve math.
- `python/spinlab/cli_fit_render.py` — CLI handler. Functions: `add_to_fit_subparsers(fit_sub)`, `run(parsed)`. Loads via DB, calls `fit_renderer.build_report`, writes file, optionally opens browser.
- `tests/unit/test_fit_renderer.py` — pure-function tests against constructed payloads / event lists. No DB, no subprocess.
- `tests/unit/test_cli_fit_render.py` — subprocess test running the registered CLI against a seeded SQLite DB.

**Modify:**
- `pyproject.toml` — add `matplotlib>=3.8` to `[project.optional-dependencies] fits`.
- `python/spinlab/cli_fit.py` — register `cli_fit_render.add_to_fit_subparsers(fit_sub)` alongside the existing inventory/rebuild registrations (line 105-107).
- `python/spinlab/cli.py` — add the `parsed.fit_command == "render"` branch under the existing `fit` dispatcher (line ~244).

**Bundle shape (informal type):** the CLI hands `build_report` a list of per-segment dicts, each with:
```python
{
    "segment_id": str,
    "segment_row": sqlite3.Row,   # for level_number, start_type, end_type, etc.
    "latest_summary": dict,        # one item from iter_segment_fit_summaries
    "history_newest_first": list[dict],   # from iter_recent_segment_fits
    "events": list[dict],          # from get_segment_event_rows
}
```

---

## Task 0: Baseline pytest run (CLAUDE.md required)

CLAUDE.md mandates a full pytest baseline before any code changes — both as a safety check and to catch pre-existing failures we'd otherwise inherit silently. No code in this task.

**Files:** none.

- [ ] **Step 1: Verify branch state**

Run: `git status && git rev-parse --abbrev-ref HEAD`
Expected: clean working tree (or only `.claude/settings.json`); branch is `feat/segments-v07-phase2b-renderer`.

If branch is wrong: stop. Do NOT proceed.

- [ ] **Step 2: Run full pytest**

Run: `python -m pytest`
Expected: all green. Skips count as failures per CLAUDE.md unless they are pre-recorded `@pytest.mark.skipif` cases.

- [ ] **Step 3: If baseline is red, stop and ask**

If any test fails or unexpectedly skips: stop, surface the failures, and ask the user how to proceed (fix as first commit, or get sign-off to defer with a follow-up task). Do NOT silently proceed.

---

## Task 1: Add matplotlib to the `[fits]` extra

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit pyproject.toml**

Add `matplotlib>=3.8` as the last line of the `[fits]` extras list. The block currently reads:
```toml
fits = [
    "jax==0.10.0",
    "jaxlib==0.10.0",
    "jaxopt==0.8.5",
    "numpyro==0.21.0",
]
```
Change to:
```toml
fits = [
    "jax==0.10.0",
    "jaxlib==0.10.0",
    "jaxopt==0.8.5",
    "numpyro==0.21.0",
    "matplotlib>=3.8",
]
```

- [ ] **Step 2: Install the new dep**

Run: `pip install -e ".[fits,dev]"`
Expected: matplotlib installs (~30 MB).

- [ ] **Step 3: Verify import**

Run: `python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; print(matplotlib.__version__)"`
Expected: prints a version >= 3.8. Note: `matplotlib.use('Agg')` MUST be called before `pyplot` is imported in production code to avoid GUI-backend errors on headless / Windows runs.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(deps): add matplotlib>=3.8 to [fits] extra for Phase 2b renderer"
```

---

## Task 2: Module skeleton + theta(n) helper

Create the `fit_renderer.py` file with a docstring, the `matplotlib.use('Agg')` guard, and one pure helper for the model's learning-curve formula. TDD: tests assert the formula matches the model's boundary cases (n=1 → theta_1; n→∞ → theta_inf).

**Files:**
- Create: `python/spinlab/fit_renderer.py`
- Create: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing test for `_theta_n`**

Create `tests/unit/test_fit_renderer.py` with:
```python
"""Pure-function tests for the v07 fit HTML renderer."""
from __future__ import annotations

import math

import pytest

from spinlab.fit_renderer import _theta_n


class TestThetaN:
    """The learning-curve formula must match boundary conditions exactly.

    log theta(n) = log theta_inf + (log theta_1 - log theta_inf) * 2^(-(n-1)/halflife)
    """

    def test_n_equals_1_returns_theta_1(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        log_halflife = math.log(20.0)
        assert _theta_n(log_inf, log_1, log_halflife, 1) == pytest.approx(0.5)

    def test_large_n_approaches_theta_inf(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        log_halflife = math.log(20.0)
        # After 20 halflives, residual gap is 2^-20 ~ 1e-6 of original.
        assert _theta_n(log_inf, log_1, log_halflife, 1 + 20 * 20) == pytest.approx(0.1, rel=1e-5)

    def test_n_equals_one_plus_halflife_halves_log_gap(self):
        log_inf = math.log(0.1)
        log_1 = math.log(0.5)
        halflife = 20.0
        log_halflife = math.log(halflife)
        # At n = 1 + halflife, log theta should be halfway between log_1 and log_inf.
        expected_log = log_inf + 0.5 * (log_1 - log_inf)
        assert math.log(_theta_n(log_inf, log_1, log_halflife, 1 + halflife)) == pytest.approx(expected_log)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: FAIL with `ImportError: cannot import name '_theta_n' from 'spinlab.fit_renderer'` (or module-not-found).

- [ ] **Step 3: Create the module with the helper**

Create `python/spinlab/fit_renderer.py`:
```python
"""Static HTML renderer for v07 segment_fits payloads (Phase 2b).

Produces a single self-contained HTML page per game showing every
segment's latest fit (learning curve + raw attempts strip + PPC table +
fit history table). matplotlib renders all plots inline as <svg>; no
JavaScript, no external assets.

Pure helpers in / strings out. No DB, no FastAPI, no JAX imports.
matplotlib is loaded with the Agg backend (headless) inside the plot
helpers so importing this module is cheap even on systems without a
display server.

Contract: input payloads follow the v1 envelope at
``python/spinlab/_segments_v07/external_docs/api_contract.md``.
"""
from __future__ import annotations

import math


def _theta_n(log_inf: float, log_1: float, log_halflife: float, n: float) -> float:
    """Evaluate the v07 learning-curve at attempt number ``n``.

    Formula (from ``_segments_v07/learning_model_v07.py``):
        log theta(n) = log_inf + (log_1 - log_inf) * 2 ^ (-(n-1) / halflife)

    Returns theta(n) in natural units. Used by the learning-curve plot
    helper to evaluate alpha/sf/ssp at each n along the x-axis.
    """
    halflife = math.exp(log_halflife)
    decay = 2.0 ** (-(n - 1) / halflife)
    log_theta = log_inf + (log_1 - log_inf) * decay
    return math.exp(log_theta)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): scaffold module + theta(n) learning-curve helper"
```

---

## Task 3: Headline stats formatter

Pure helper that turns a v1 payload into the two-line "headline" HTML block: status header + `M_clear / death_rate_next` card. Handles both fittable and unfittable payloads (unfittable shows "—" for derived stats).

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import render_headline_html


def _fittable_payload(**overrides):
    """Construct a complete fittable v1 envelope. Override-as-you-go."""
    payload = {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": "test-seg",
        "n_attempts": 17,
        "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [0.0] * 10, "natural": {
                "bpt_ms": 25000.0,
                "sf_inf": 0.07, "sf_1": 0.24,
                "ssp_inf": 0.46, "ssp_1": 0.46,
                "alpha_inf": 0.37, "alpha_1": 3.75,
                "halflife_sf": 34.0, "halflife_ssp": 28.0, "halflife_alpha": 21.0,
            }},
            "bands": {f"log_{k}": {"p5": -0.1, "p50": 0.0, "p95": 0.1} for k in (
                "bpt", "sf_inf", "ssp_inf", "alpha_inf",
                "sf_1", "ssp_1", "alpha_1",
                "hl_sf", "hl_ssp", "hl_alpha",
            )},
            "derived": {
                "M_clear": {"median_ms": 81200.0, "p5_ms": 53200.0, "p95_ms": 153200.0},
                "death_rate_next": 0.75,
            },
            "ppc": {
                "died_rate": {"obs": 0.88, "p_two_sided": 0.998},
                "died_tau_skew": {"obs": 0.91, "p_two_sided": 0.608},
                "died_tau_kurt": {"obs": 0.49, "p_two_sided": 0.364},
                "died_s_mid_third": {"obs": 0.20, "p_two_sided": 0.560},
            },
        },
        "caveats": ["low_n"],
    }
    payload.update(overrides)
    return payload


def _unfittable_payload():
    """Minimal envelope for a segment whose fit didn't converge."""
    return {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "test-seg", "n_attempts": 20, "model": "haz1",
        "wall_time_s": 0.0014,
        "status": {
            "converged": False, "band_source": "none",
            "laplace_pd": False, "ppc_tension": False, "fittable": False,
        },
        "result": {},
        "caveats": ["unconverged"],
    }


class TestRenderHeadlineHtml:
    def test_fittable_shows_m_clear_seconds(self):
        out = render_headline_html(_fittable_payload())
        assert "M_clear" in out
        # 81200 ms ⇒ 81.2 s
        assert "81.2" in out
        assert "53.2" in out
        assert "153.2" in out

    def test_fittable_shows_death_rate_next(self):
        out = render_headline_html(_fittable_payload())
        assert "death_rate_next" in out
        assert "0.75" in out

    def test_unfittable_uses_em_dash_for_derived(self):
        out = render_headline_html(_unfittable_payload())
        # Either the stat label is absent or rendered with em-dash; either
        # is acceptable as long as no fake number leaks through.
        assert "81.2" not in out
        assert "—" in out or "M_clear" not in out
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: `ImportError` on `render_headline_html`.

- [ ] **Step 3: Implement `render_headline_html`**

Add to `python/spinlab/fit_renderer.py`:
```python
from typing import Any


def _fmt_seconds(ms: float) -> str:
    """ms → '12.3' (one decimal). Used in headline stats."""
    return f"{ms / 1000.0:.1f}"


def render_headline_html(payload: dict[str, Any]) -> str:
    """Render the per-segment headline stats block as HTML.

    Two lines:
      Status line  — n=<N>  fittable: Y/N  caveats: <a, b>
      Stats card   — M_clear <median>s [<p5>s, <p95>s]  death_rate_next <0.xx>

    For unfittable payloads (no ``result.derived``) the stats card shows
    em-dashes rather than fabricating numbers.
    """
    status = payload["status"]
    n = payload["n_attempts"]
    fittable = "Y" if status["fittable"] else "N"
    caveats = payload.get("caveats") or []
    caveats_str = ", ".join(caveats) if caveats else "—"

    derived = payload.get("result", {}).get("derived") or {}
    m_clear = derived.get("M_clear")
    if m_clear:
        m_str = (
            f"M_clear {_fmt_seconds(m_clear['median_ms'])}s "
            f"[{_fmt_seconds(m_clear['p5_ms'])}s, "
            f"{_fmt_seconds(m_clear['p95_ms'])}s]"
        )
    else:
        m_str = "M_clear —"
    drn = derived.get("death_rate_next")
    drn_str = f"death_rate_next {drn:.2f}" if drn is not None else "death_rate_next —"

    return (
        f'<div class="status">n={n}  fittable: {fittable}  '
        f'caveats: {caveats_str}</div>\n'
        f'<div class="headline">{m_str}  ·  {drn_str}</div>\n'
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): headline stats HTML (fittable + unfittable paths)"
```

---

## Task 4: Learning curve SVG renderer

Plot α(n), sf(n), ssp(n) over the full attempt range as three stacked subplots with shaded p5–p95 bands. Use endpoint substitution for the bands (true joint sampling isn't available from the payload); add a code comment explaining the approximation.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
import xml.etree.ElementTree as ET

from spinlab.fit_renderer import render_learning_curve_svg


class TestRenderLearningCurveSvg:
    def test_fittable_returns_parseable_svg(self):
        svg = render_learning_curve_svg(_fittable_payload())
        # Strip xmlns prefixes for terse parsing.
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        # Three subplots (α, sf, ssp), each with at least a line path.
        paths = root.findall(".//{http://www.w3.org/2000/svg}path")
        assert len(paths) >= 3

    def test_unfittable_returns_placeholder(self):
        svg = render_learning_curve_svg(_unfittable_payload())
        # Either an empty <svg/> or a string containing "no fit" — never an
        # SVG with fabricated curves.
        assert "<svg" in svg
        assert "fit" not in svg or "no" in svg.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderLearningCurveSvg`
Expected: `ImportError`.

- [ ] **Step 3: Implement `render_learning_curve_svg`**

Add to `python/spinlab/fit_renderer.py` (at module top, before the helpers):
```python
import io


def _matplotlib_module():
    """Lazy-import matplotlib with the Agg backend forced.

    Called from inside each plot helper rather than at module top so
    importing ``spinlab.fit_renderer`` stays cheap (the CLI subcommand
    registration imports the module unconditionally).
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    return matplotlib, plt
```

Then add the renderer at the bottom:
```python
# Learning-curve x-axis range: extrapolate one halflife past the
# observed n so the asymptote is visible. Lower bound is 1 (the
# model's reference attempt).
_CURVE_MIN_N = 1
_CURVE_EXTRAPOLATE_HALFLIVES = 1


# Which latent indices in the payload's `bands` map to each curve.
# Keys come straight from the v1 envelope contract; see api_contract.md
# section "result.bands".
_CURVE_LATENTS = [
    ("alpha", "log_alpha_inf", "log_alpha_1", "log_hl_alpha"),
    ("sf",    "log_sf_inf",    "log_sf_1",    "log_hl_sf"),
    ("ssp",   "log_ssp_inf",   "log_ssp_1",   "log_hl_ssp"),
]


def _placeholder_svg(message: str) -> str:
    """Tiny SVG carrying a plain-text 'no fit' marker. Used for unfittable
    payloads so the per-segment layout still has the slot filled."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="40">'
        f'<text x="10" y="25" font-family="monospace">{message}</text>'
        f'</svg>'
    )


def render_learning_curve_svg(payload: dict[str, Any]) -> str:
    """Render alpha/sf/ssp learning curves with p5-p95 bands.

    Bands are an APPROXIMATION: the payload stores only per-latent
    p5/p50/p95 in log space, not the joint posterior. We evaluate the
    curve at the marginal endpoints (log_inf=p5/p95, log_1=p5/p95,
    log_halflife=p50) to produce a band envelope. Faithful enough for
    'tight vs. wide' triage; would be wrong to read as a true
    credible interval. This is fine — Phase 2b is a visual triage
    tool, not a calibrated reporting surface.
    """
    if not payload["status"].get("fittable") or "bands" not in payload.get("result", {}):
        return _placeholder_svg("no fit (unfittable or missing bands)")

    _, plt = _matplotlib_module()
    bands = payload["result"]["bands"]
    n_obs = payload["n_attempts"]

    # Extrapolate the x-axis a bit past the data to reveal the asymptote.
    max_hl = max(
        math.exp(bands["log_hl_alpha"]["p50"]),
        math.exp(bands["log_hl_sf"]["p50"]),
        math.exp(bands["log_hl_ssp"]["p50"]),
    )
    n_max = max(n_obs + 1, int(n_obs + _CURVE_EXTRAPOLATE_HALFLIVES * max_hl))
    import numpy as np  # local to keep top-of-module light
    n_grid = np.linspace(_CURVE_MIN_N, n_max, 200)

    fig, axes = plt.subplots(3, 1, figsize=(8, 5), sharex=True)
    for ax, (label, k_inf, k_1, k_hl) in zip(axes, _CURVE_LATENTS):
        b_inf = bands[k_inf]
        b_1 = bands[k_1]
        log_hl_p50 = bands[k_hl]["p50"]
        center = np.array([_theta_n(b_inf["p50"], b_1["p50"], log_hl_p50, n) for n in n_grid])
        lo = np.array([_theta_n(b_inf["p5"], b_1["p5"], log_hl_p50, n) for n in n_grid])
        hi = np.array([_theta_n(b_inf["p95"], b_1["p95"], log_hl_p50, n) for n in n_grid])
        # Element-wise min/max so the band is always lo<=center<=hi visually.
        band_lo = np.minimum(lo, hi)
        band_hi = np.maximum(lo, hi)
        ax.fill_between(n_grid, band_lo, band_hi, alpha=0.2)
        ax.plot(n_grid, center, linewidth=1.5)
        ax.axvline(n_obs, color="grey", linestyle=":", linewidth=0.8)
        ax.set_ylabel(label)
    axes[-1].set_xlabel("attempt #")
    fig.tight_layout()

    buf = io.StringIO()
    fig.savefig(buf, format="svg")
    plt.close(fig)
    # matplotlib emits an XML prolog; strip it so the SVG embeds cleanly
    # inside the HTML body without a stray <?xml...?> tag in mid-document.
    svg = buf.getvalue()
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[1].lstrip()
    return svg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderLearningCurveSvg`
Expected: 2 passed.

- [ ] **Step 5: Run all fit_renderer tests**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): learning-curve SVG with p5-p95 endpoint-substitution bands"
```

---

## Task 5: Raw attempts strip SVG renderer

Strip plot: x = attempt number (1..N), y = time_ms, dots colored green=survived / red=died. One dot per event row from `get_segment_event_rows`.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import render_attempts_strip_svg


def _event_row(outcome: str, time_ms: int) -> dict:
    """Minimal event-row dict matching the get_segment_event_rows shape."""
    return {"outcome": outcome, "time_ms": time_ms}


class TestRenderAttemptsStripSvg:
    def test_returns_parseable_svg_with_at_least_one_marker(self):
        events = [
            _event_row("died", 12000), _event_row("died", 9000),
            _event_row("survived", 25000),
            _event_row("died", 8000), _event_row("survived", 24000),
        ]
        svg = render_attempts_strip_svg(events)
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        # Scatter markers render as <path> or <use> elements with `clip-path`
        # attributes set. Either way at least one drawing element.
        assert len(root.findall(".//*")) > 5

    def test_empty_events_returns_placeholder(self):
        svg = render_attempts_strip_svg([])
        assert "<svg" in svg
        assert "no attempts" in svg.lower() or "—" in svg
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderAttemptsStripSvg`
Expected: `ImportError`.

- [ ] **Step 3: Implement `render_attempts_strip_svg`**

Append to `python/spinlab/fit_renderer.py`:
```python
# Outcome → marker color. Plain hex so renders identically in every browser.
_OUTCOME_COLOR = {
    "survived": "#1a9850",   # green
    "died":     "#d73027",   # red
}


def render_attempts_strip_svg(events: list[dict[str, Any]]) -> str:
    """Render the raw attempt sequence as a scatter strip.

    x = position in the event sequence (1..N), y = time_ms. Green
    survived, red died. Lets the reader sanity-check whether the
    fitted curve passes through the actual data.

    Empty input → placeholder SVG with a 'no attempts' marker; never
    a blank canvas (the per-segment layout always allocates the slot).
    """
    if not events:
        return _placeholder_svg("no attempts recorded")

    _, plt = _matplotlib_module()
    import numpy as np
    xs = np.arange(1, len(events) + 1)
    ys = np.array([e["time_ms"] for e in events], dtype=float)
    colors = [_OUTCOME_COLOR.get(e["outcome"], "#888888") for e in events]

    fig, ax = plt.subplots(figsize=(8, 1.6))
    ax.scatter(xs, ys, c=colors, s=20, alpha=0.85)
    ax.set_xlabel("attempt #")
    ax.set_ylabel("time_ms")
    fig.tight_layout()

    buf = io.StringIO()
    fig.savefig(buf, format="svg")
    plt.close(fig)
    svg = buf.getvalue()
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[1].lstrip()
    return svg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderAttemptsStripSvg`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): raw attempts strip SVG (green=survived/red=died)"
```

---

## Task 6: PPC table HTML

Four-row HTML `<table>` for the PPC stats. Handles missing PPC blocks (unfittable payloads) by returning a placeholder.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import render_ppc_table_html


class TestRenderPpcTableHtml:
    def test_fittable_emits_all_four_stats(self):
        out = render_ppc_table_html(_fittable_payload())
        for stat in (
            "died_rate", "died_tau_skew", "died_tau_kurt", "died_s_mid_third",
        ):
            assert stat in out
        assert "0.998" in out  # died_rate p_two_sided
        assert "0.608" in out  # died_tau_skew

    def test_unfittable_emits_placeholder(self):
        out = render_ppc_table_html(_unfittable_payload())
        assert "no PPC" in out or "—" in out
        assert "0.998" not in out
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderPpcTableHtml`
Expected: `ImportError`.

- [ ] **Step 3: Implement `render_ppc_table_html`**

Append to `python/spinlab/fit_renderer.py`:
```python
def render_ppc_table_html(payload: dict[str, Any]) -> str:
    """Four-row HTML table of PPC diagnostic stats.

    Row format: ``<stat>  obs <value>  p=<p_two_sided>``. Missing PPC
    block (unfittable payload) → '<p>no PPC</p>' so the slot stays
    occupied. Stat keys come from the v1 envelope contract; we render
    them in fixed order so reports stay visually comparable.
    """
    ppc = payload.get("result", {}).get("ppc")
    if not ppc:
        return "<p class='ppc-missing'>no PPC (unfittable)</p>"
    rows = []
    # Fixed order matches the contract's enumeration; new stats added
    # by future model versions land at the bottom unchanged.
    for stat in ("died_rate", "died_tau_skew", "died_tau_kurt", "died_s_mid_third"):
        cell = ppc.get(stat)
        if cell is None:
            continue
        rows.append(
            f"<tr><td>{stat}</td><td>obs {cell['obs']:.3f}</td>"
            f"<td>p={cell['p_two_sided']:.3f}</td></tr>"
        )
    return "<table class='ppc'>\n" + "\n".join(rows) + "\n</table>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderPpcTableHtml`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): PPC table HTML"
```

---

## Task 7: Fit history HTML table

Compact table of all stored fits for one segment (one row per fit, oldest-first), columns `n_attempts | fittable | M_clear band | wall_time_ms | fitted_at`. Designed to make fittability flips visible at a glance.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import render_history_table_html


class TestRenderHistoryTableHtml:
    def test_two_fits_oldest_first_with_flip_visible(self):
        # n=14 fittable, n=20 not — the L1 entrance flip we're auditing for.
        history = [
            {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
            {**_unfittable_payload(), "fitted_at": "2026-05-24T08:00:00Z"},
        ]
        out = render_history_table_html(history)
        # Headers
        for h in ("n", "fittable", "M_clear", "wall_ms", "fitted_at"):
            assert h in out
        # Both rows present
        assert ">14<" in out
        assert ">20<" in out
        # Fittable flip is visible as both Y and N
        assert ">Y<" in out
        assert ">N<" in out

    def test_empty_history_emits_placeholder(self):
        out = render_history_table_html([])
        assert "no fit history" in out.lower()

    def test_oldest_first_ordering(self):
        # Caller passes oldest-first; renderer preserves order. Hand a
        # newest-first list to verify the renderer does NOT silently
        # reorder.
        h = [
            {**_fittable_payload(n_attempts=20), "fitted_at": "2026-05-24T08:00:00Z"},
            {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
        ]
        out = render_history_table_html(h)
        # 20 should appear before 14 in the rendered table — renderer
        # respects caller-supplied order.
        idx_20 = out.find(">20<")
        idx_14 = out.find(">14<")
        assert 0 <= idx_20 < idx_14
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderHistoryTableHtml`
Expected: `ImportError`.

- [ ] **Step 3: Implement `render_history_table_html`**

Append to `python/spinlab/fit_renderer.py`:
```python
def render_history_table_html(history: list[dict[str, Any]]) -> str:
    """Compact table of every stored fit for one segment.

    Caller decides ordering — the renderer preserves it. The DB helper
    ``iter_recent_segment_fits`` yields newest-first; the CLI handler
    is expected to reverse to oldest-first so flips read top-to-bottom
    chronologically. Each ``history`` item is a v1 envelope dict with
    an extra ``fitted_at`` key spliced in from the row.

    Columns: n | fittable | M_clear band | wall_ms | fitted_at.
    """
    if not history:
        return "<p class='history-missing'>no fit history</p>"
    header = (
        "<tr><th>n</th><th>fittable</th><th>M_clear</th>"
        "<th>wall_ms</th><th>fitted_at</th></tr>"
    )
    rows = []
    for fit in history:
        n = fit["n_attempts"]
        fittable = "Y" if fit["status"]["fittable"] else "N"
        derived = fit.get("result", {}).get("derived") or {}
        m = derived.get("M_clear")
        if m:
            m_str = (
                f"{_fmt_seconds(m['median_ms'])}s "
                f"[{_fmt_seconds(m['p5_ms'])}, {_fmt_seconds(m['p95_ms'])}]"
            )
        else:
            m_str = "—"
        wall_ms = int(float(fit.get("wall_time_s", 0)) * 1000)
        fitted_at = fit.get("fitted_at") or "—"
        rows.append(
            f"<tr><td>{n}</td><td>{fittable}</td><td>{m_str}</td>"
            f"<td>{wall_ms}</td><td>{fitted_at}</td></tr>"
        )
    return "<table class='history'>\n" + header + "\n" + "\n".join(rows) + "\n</table>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderHistoryTableHtml`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): fit-history HTML table"
```

---

## Task 8: Per-segment section assembly + anchor helper

Combine the headline, learning curve, attempts strip, PPC table, and history table into one `<section>` element with a stable anchor id. The anchor id is derived from the segment row (level + start_type/ord + end_type/ord) so the game-level index can link to it.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import _anchor_id, render_segment_section


def _segment_row(**overrides):
    """Minimal sqlite-Row-like dict matching the segments table shape."""
    row = {
        "id": "test-seg",
        "level_number": 49,
        "start_type": "checkpoint", "start_ordinal": 1,
        "end_type": "checkpoint", "end_ordinal": 2,
    }
    row.update(overrides)
    return row


class TestAnchorId:
    def test_stable_format(self):
        assert _anchor_id(_segment_row()) == "seg-49-checkpoint_1-checkpoint_2"

    def test_entrance_to_checkpoint(self):
        row = _segment_row(
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=1,
        )
        assert _anchor_id(row) == "seg-49-entrance_0-checkpoint_1"


class TestRenderSegmentSection:
    def test_includes_all_six_parts(self):
        section = render_segment_section(
            segment_row=_segment_row(),
            latest_payload=_fittable_payload(),
            history_oldest_first=[
                {**_fittable_payload(n_attempts=14), "fitted_at": "2026-05-24T07:00:00Z"},
                {**_fittable_payload(n_attempts=17), "fitted_at": "2026-05-24T08:00:00Z"},
            ],
            events=[_event_row("died", 9000), _event_row("survived", 25000)],
        )
        assert 'id="seg-49-checkpoint_1-checkpoint_2"' in section
        assert "<section" in section
        # Each of the five rendered blocks present:
        assert "fittable: Y" in section          # headline
        assert "<svg" in section                  # learning curve + attempts strip
        assert "died_rate" in section             # PPC table
        assert "wall_ms" in section               # history table
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k "TestAnchorId or TestRenderSegmentSection"`
Expected: `ImportError`.

- [ ] **Step 3: Implement `_anchor_id` and `render_segment_section`**

Append to `python/spinlab/fit_renderer.py`:
```python
def _anchor_id(segment_row: Any) -> str:
    """Stable HTML anchor id for a segment row.

    Format: ``seg-<level>-<start_type>_<start_ord>-<end_type>_<end_ord>``.
    Built from segment_row's ``level_number / start_type / start_ordinal /
    end_type / end_ordinal`` columns. Stable across runs as long as the
    segment's geographic identity is unchanged; survives segment-id
    rehashing if start/end waypoints change.
    """
    return (
        f"seg-{segment_row['level_number']}-"
        f"{segment_row['start_type']}_{segment_row['start_ordinal']}-"
        f"{segment_row['end_type']}_{segment_row['end_ordinal']}"
    )


def _segment_human_label(segment_row: Any) -> str:
    """Display label for headings: 'L49 checkpoint_1→checkpoint_2'."""
    return (
        f"L{segment_row['level_number']} "
        f"{segment_row['start_type']}_{segment_row['start_ordinal']}→"
        f"{segment_row['end_type']}_{segment_row['end_ordinal']}"
    )


def render_segment_section(
    segment_row: Any,
    latest_payload: dict[str, Any],
    history_oldest_first: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> str:
    """Assemble one per-segment <section> for the report.

    Order: heading → headline → learning curve → attempts strip → PPC
    table → history table. Each piece is built by its dedicated helper;
    this function is pure layout glue.
    """
    anchor = _anchor_id(segment_row)
    label = _segment_human_label(segment_row)
    return (
        f'<section id="{anchor}">\n'
        f'  <h2>{label}</h2>\n'
        f'  {render_headline_html(latest_payload)}\n'
        f'  <figure>{render_learning_curve_svg(latest_payload)}</figure>\n'
        f'  <figure>{render_attempts_strip_svg(events)}</figure>\n'
        f'  {render_ppc_table_html(latest_payload)}\n'
        f'  {render_history_table_html(history_oldest_first)}\n'
        f'</section>\n'
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: all tests green.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): per-segment <section> assembly + anchor id helper"
```

---

## Task 9: Game-level index table

A table at the top of the page listing every segment with its fittable status, latest n, and a link to its anchor. Makes the L1-flip and L16-never-fit patterns visible without scrolling.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import render_game_index_html


class TestRenderGameIndexHtml:
    def test_lists_segments_with_anchor_links(self):
        bundle = [
            {
                "segment_row": _segment_row(level_number=1, start_type="entrance",
                                            start_ordinal=0, end_type="checkpoint",
                                            end_ordinal=1),
                "latest_payload": _unfittable_payload(),
            },
            {
                "segment_row": _segment_row(level_number=49, start_type="checkpoint",
                                            start_ordinal=1, end_type="checkpoint",
                                            end_ordinal=2),
                "latest_payload": _fittable_payload(n_attempts=17),
            },
        ]
        out = render_game_index_html(bundle)
        # Both segments present
        assert "L1 entrance_0→checkpoint_1" in out
        assert "L49 checkpoint_1→checkpoint_2" in out
        # Anchor links present
        assert "#seg-1-entrance_0-checkpoint_1" in out
        assert "#seg-49-checkpoint_1-checkpoint_2" in out
        # Status icons: one ✓ for fittable, one ✗ for unfittable
        assert "✓" in out
        assert "✗" in out

    def test_empty_bundle_emits_placeholder(self):
        out = render_game_index_html([])
        assert "no segments" in out.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderGameIndexHtml`
Expected: `ImportError`.

- [ ] **Step 3: Implement `render_game_index_html`**

Append to `python/spinlab/fit_renderer.py`:
```python
def render_game_index_html(bundle: list[dict[str, Any]]) -> str:
    """Top-of-page jump table: one row per segment, links to its section.

    Each ``bundle`` item carries ``segment_row`` and ``latest_payload``.
    Status column uses ✓ for fittable, ✗ for unfittable so the
    L1-style flip is visible at a glance. M_clear column shows the
    median in seconds, or '—' when no derived stats are available.
    """
    if not bundle:
        return "<p class='index-missing'>no segments with fits</p>"
    header = (
        "<tr><th>Segment</th><th>n</th><th>status</th><th>M_clear</th></tr>"
    )
    rows = []
    for item in bundle:
        seg = item["segment_row"]
        payload = item["latest_payload"]
        label = _segment_human_label(seg)
        anchor = _anchor_id(seg)
        n = payload["n_attempts"]
        ok = payload["status"]["fittable"]
        icon = "✓" if ok else "✗"
        derived = payload.get("result", {}).get("derived") or {}
        m = derived.get("M_clear")
        m_str = f"{_fmt_seconds(m['median_ms'])}s" if m else "—"
        rows.append(
            f"<tr><td><a href='#{anchor}'>{label}</a></td>"
            f"<td>{n}</td><td>{icon}</td><td>{m_str}</td></tr>"
        )
    return "<table class='index'>\n" + header + "\n" + "\n".join(rows) + "\n</table>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestRenderGameIndexHtml`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): game-level index table with status icons"
```

---

## Task 10: `build_report` top-level assembler

The public entry point. Wraps the index + per-segment sections in a complete `<html>` document with `<head>`, minimal inline CSS, and a `<body>`.

**Files:**
- Modify: `python/spinlab/fit_renderer.py`
- Modify: `tests/unit/test_fit_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fit_renderer.py`:
```python
from spinlab.fit_renderer import build_report


class TestBuildReport:
    def test_returns_full_html_document(self):
        bundle = [{
            "segment_row": _segment_row(),
            "latest_payload": _fittable_payload(),
            "history_oldest_first": [_fittable_payload()],
            "events": [_event_row("died", 9000), _event_row("survived", 25000)],
        }]
        html = build_report(game_label="Beto", game_id="01c9", bundle=bundle)
        assert html.startswith("<!doctype html>") or html.startswith("<!DOCTYPE html>")
        assert "<head>" in html and "</head>" in html
        assert "<body>" in html and "</body>" in html
        # Title contains game label
        assert "Beto" in html
        # Index table present
        assert "class='index'" in html or 'class="index"' in html
        # Per-segment section present
        assert 'id="seg-49-checkpoint_1-checkpoint_2"' in html

    def test_empty_bundle_still_returns_valid_document(self):
        html = build_report(game_label="Empty", game_id="00", bundle=[])
        assert "<body>" in html
        assert "no segments" in html.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestBuildReport`
Expected: `ImportError`.

- [ ] **Step 3: Implement `build_report`**

Append to `python/spinlab/fit_renderer.py`:
```python
# Minimal CSS. Inline so the HTML is fully self-contained. Just enough
# to make tables readable; visual polish is explicitly out of Phase 2b
# scope (this is an audit tool, not a product surface).
_INLINE_CSS = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px;
       margin: 1em auto; padding: 0 1em; color: #222; }
table { border-collapse: collapse; margin: 0.5em 0; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left;
         font-size: 14px; }
th { background: #f4f4f4; }
section { border-top: 2px solid #ccc; padding-top: 1em; margin-top: 2em; }
.status { font-family: monospace; color: #555; }
.headline { font-weight: 600; margin: 0.5em 0 1em 0; }
.ppc, .history { font-size: 12px; font-family: monospace; }
figure { margin: 1em 0; }
a { color: #1a6fc4; text-decoration: none; }
a:hover { text-decoration: underline; }
"""


def build_report(
    game_label: str, game_id: str, bundle: list[dict[str, Any]],
) -> str:
    """Build the complete self-contained HTML report for one game.

    ``bundle`` order is preserved both in the index and in the
    per-segment sections — the CLI handler decides sort order (e.g.
    by level + ordinal). Empty bundle still returns a valid document
    with an explanatory placeholder.
    """
    index_html = render_game_index_html(bundle)
    if bundle:
        sections_html = "\n".join(
            render_segment_section(
                segment_row=item["segment_row"],
                latest_payload=item["latest_payload"],
                history_oldest_first=item["history_oldest_first"],
                events=item["events"],
            )
            for item in bundle
        )
    else:
        sections_html = ""
    title = f"spinlab v07 fit report — {game_label}"
    return (
        "<!doctype html>\n"
        "<html lang='en'>\n<head>\n"
        f"  <meta charset='utf-8'>\n"
        f"  <title>{title}</title>\n"
        f"  <style>{_INLINE_CSS}</style>\n"
        "</head>\n<body>\n"
        f"  <h1>{title}</h1>\n"
        f"  <p class='meta'>game_id: <code>{game_id}</code></p>\n"
        f"  {index_html}\n"
        f"  {sections_html}\n"
        "</body>\n</html>\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v -k TestBuildReport`
Expected: 2 passed.

- [ ] **Step 5: Run all fit_renderer tests**

Run: `python -m pytest tests/unit/test_fit_renderer.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/fit_renderer.py tests/unit/test_fit_renderer.py
git commit -m "feat(fit_renderer): build_report top-level HTML document assembler"
```

---

## Task 11: CLI handler `cli_fit_render.py`

New module mirroring `cli_fit_inventory.py` / `cli_fit_rebuild.py`. Registers `render` under `spinlab fit`, loads data via the DB helpers, calls `build_report`, writes the HTML file, optionally opens a browser.

**Files:**
- Create: `python/spinlab/cli_fit_render.py`
- Modify: `python/spinlab/cli_fit.py` (line 105-107: add registration call)

- [ ] **Step 1: Create the CLI module**

Create `python/spinlab/cli_fit_render.py`:
```python
"""spinlab fit render — write the Phase 2b static HTML audit report.

Loads every segment with a stored fit for the requested game, plus
each segment's full fit history and raw attempt events, and renders a
single self-contained HTML file via ``spinlab.fit_renderer``.

Read-only over the DB. Does NOT import spinlab.segments_model (no
JAX at CLI time) — the renderer is pure-Python over the v1 envelope.
"""
from __future__ import annotations

import argparse
import logging
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)


def add_to_fit_subparsers(fit_sub: argparse._SubParsersAction) -> None:
    """Attach `render` to the existing `spinlab fit` parent subparser."""
    p = fit_sub.add_parser(
        "render",
        help="Write a static HTML audit report of all fits for a game.",
    )
    p.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p.add_argument(
        "--game", required=True,
        help="Game id to render (must exist in the games table).",
    )
    p.add_argument(
        "--out", required=True,
        help="Path to write the HTML report to.",
    )
    p.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="After writing, open the report in the default web browser.",
    )


def _open_db(config_path: str):
    """Resolve config + open the SQLite DB (mirrors cli_fit._open_db)."""
    from spinlab.cli_common import resolve_config_path
    from spinlab.config import AppConfig
    from spinlab.db import Database
    resolved = resolve_config_path(config_path)
    cfg = AppConfig.from_yaml(resolved)
    return Database(cfg.data_dir / "spinlab.db")


def run(parsed: argparse.Namespace) -> int:
    """Load → render → write. Returns process exit code."""
    from spinlab import fit_renderer
    db = _open_db(parsed.config)
    game_id = parsed.game

    # Look up the game label for the report title; fall back to the id
    # if the row is missing rather than erroring out — the report is a
    # diagnostic, not a precision instrument.
    game_row = db.conn.execute(
        "SELECT name FROM games WHERE id = ?", (game_id,)
    ).fetchone()
    game_label = game_row["name"] if game_row else game_id

    summaries = list(db.iter_segment_fit_summaries(game_id))
    if not summaries:
        print(f"no fits found for game {game_id!r}")
        return 1

    bundle = []
    for summary in summaries:
        seg_id = summary["segment_id"]
        seg_row = db.conn.execute(
            "SELECT id, level_number, start_type, start_ordinal, "
            "       end_type, end_ordinal "
            "FROM segments WHERE id = ?",
            (seg_id,),
        ).fetchone()
        if seg_row is None:
            # The fit references a segment that has been hard-deleted.
            # Skip and log; the report is still useful without it.
            logger.warning("fit references missing segment %s; skipping", seg_id)
            continue
        # iter_recent_segment_fits yields newest-first; reverse so the
        # history table reads top-to-bottom chronologically.
        history_newest_first = list(db.iter_recent_segment_fits(seg_id, limit=50))
        history_oldest_first = list(reversed(history_newest_first))
        events = list(db.get_segment_event_rows(seg_id))
        bundle.append({
            "segment_row": seg_row,
            "latest_payload": summary["payload"],
            "history_oldest_first": history_oldest_first,
            "events": events,
        })

    # Sort by level then start ordinal so the report reads in
    # speedrun order. iter_segment_fit_summaries doesn't guarantee
    # ordering by geography — it groups by latest fit id.
    bundle.sort(key=lambda item: (
        item["segment_row"]["level_number"],
        item["segment_row"]["start_ordinal"],
    ))

    html = fit_renderer.build_report(
        game_label=game_label, game_id=game_id, bundle=bundle,
    )

    out_path = Path(parsed.out)
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote {out_path} ({len(html)} bytes, {len(bundle)} segments)")

    if parsed.open_browser:
        webbrowser.open(out_path.resolve().as_uri())

    return 0
```

- [ ] **Step 2: Register the subcommand in `cli_fit.py`**

In `python/spinlab/cli_fit.py`, edit lines 105-107. Current:
```python
    from spinlab import cli_fit_inventory, cli_fit_rebuild
    cli_fit_inventory.add_to_fit_subparsers(fit_sub)
    cli_fit_rebuild.add_to_fit_subparsers(fit_sub)
```
Change to:
```python
    from spinlab import cli_fit_inventory, cli_fit_rebuild, cli_fit_render
    cli_fit_inventory.add_to_fit_subparsers(fit_sub)
    cli_fit_rebuild.add_to_fit_subparsers(fit_sub)
    cli_fit_render.add_to_fit_subparsers(fit_sub)
```

- [ ] **Step 3: Add the dispatcher branch in `cli.py`**

In `python/spinlab/cli.py`, find the existing dispatcher block at lines ~238-249:
```python
    elif parsed.command == "fit":
        from spinlab import cli_fit
        if parsed.fit_command == "show":
            sys.exit(cli_fit.run_show(parsed))
        elif parsed.fit_command == "list":
            sys.exit(cli_fit.run_list(parsed))
        elif parsed.fit_command == "inventory":
            from spinlab import cli_fit_inventory
            sys.exit(cli_fit_inventory.run(parsed))
        elif parsed.fit_command == "rebuild":
            from spinlab import cli_fit_rebuild
            sys.exit(cli_fit_rebuild.run(parsed))
```
Add a new `elif` branch at the bottom of the chain:
```python
        elif parsed.fit_command == "render":
            from spinlab import cli_fit_render
            sys.exit(cli_fit_render.run(parsed))
```

- [ ] **Step 4: Smoke-test the CLI registration**

Run: `python -m spinlab.cli fit --help`
Expected: output includes a `render` subcommand line.

Run: `python -m spinlab.cli fit render --help`
Expected: usage line shows `--game`, `--out`, and `--open`.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cli_fit_render.py python/spinlab/cli_fit.py python/spinlab/cli.py
git commit -m "feat(cli): add 'spinlab fit render' subcommand for Phase 2b HTML report"
```

---

## Task 12: CLI subprocess integration test

End-to-end test that the registered CLI produces a valid HTML file when run against a seeded SQLite DB. Subprocess so the registration path is exercised; in-process DB seeding so the test is fast.

**Files:**
- Create: `tests/unit/test_cli_fit_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cli_fit_render.py`:
```python
"""End-to-end test for the `spinlab fit render` CLI.

Seeds a small DB via the production helpers (so we exercise the same
write paths the real pipeline uses), runs the CLI as a subprocess to
exercise the parser + dispatcher, asserts the output file is
well-formed HTML with the expected segments.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from spinlab.db import Database
from spinlab.models import (
    AttemptOutcome, AttemptSource, EndpointType, EventAttempt, Segment,
)


def _write_config(tmp_path: Path) -> Path:
    """Mirror the config schema used by other CLI tests (see test_cli_db_reset)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "data": {"dir": str(data_dir)},
        "network": {"port": 15482, "dashboard_port": 15483},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    return config_path


def _make_fittable_payload(segment_id: str, n: int) -> dict:
    """Construct a minimal but renderer-complete v1 envelope."""
    bands = {
        f"log_{k}": {"p5": -0.1, "p50": 0.0, "p95": 0.1}
        for k in (
            "bpt", "sf_inf", "ssp_inf", "alpha_inf",
            "sf_1", "ssp_1", "alpha_1",
            "hl_sf", "hl_ssp", "hl_alpha",
        )
    }
    return {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": segment_id, "n_attempts": n, "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [0.0] * 10, "natural": {}},
            "bands": bands,
            "derived": {
                "M_clear": {"median_ms": 25000.0, "p5_ms": 20000.0, "p95_ms": 35000.0},
                "death_rate_next": 0.4,
            },
            "ppc": {
                "died_rate": {"obs": 0.4, "p_two_sided": 0.5},
                "died_tau_skew": {"obs": 0.0, "p_two_sided": 0.5},
                "died_tau_kurt": {"obs": 0.0, "p_two_sided": 0.5},
                "died_s_mid_third": {"obs": 0.3, "p_two_sided": 0.5},
            },
        },
        "caveats": [],
    }


@pytest.fixture
def seeded_db(tmp_path: Path):
    """Create a config.yaml + DB with one game, one segment, one fit, two events.

    All writes go through production helpers so this fixture also
    serves as a smoke test that the schema + helpers haven't drifted.
    """
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    db = Database(str(data_dir / "spinlab.db"))

    db.upsert_game("game1", "TestGame", "any%")
    seg = Segment(
        id="game1:49:checkpoint.1:checkpoint.2:abcd1234:ef01abcd",
        game_id="game1", level_number=49,
        start_type=EndpointType.CHECKPOINT, start_ordinal=1,
        end_type=EndpointType.CHECKPOINT, end_ordinal=2,
    )
    db.upsert_segment(seg)
    db.save_segment_fit(seg.id, "segment_fit", _make_fittable_payload(seg.id, 5))
    for outcome, time_ms in (
        (AttemptOutcome.DIED, 9000),
        (AttemptOutcome.SURVIVED, 25000),
    ):
        db.log_event_attempt(EventAttempt(
            segment_id=seg.id, episode_id="ep1",
            outcome=outcome, time_ms=time_ms,
            source=AttemptSource.PRACTICE,
        ))
    db.close()
    return config_path, "game1"


def test_render_writes_valid_html(seeded_db, tmp_path: Path):
    config_path, game_id = seeded_db
    out_path = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "render",
         "--config", str(config_path),
         "--game", game_id,
         "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"CLI failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")
    # Game title rendered
    assert "TestGame" in html
    # Segment section rendered
    assert "seg-49-checkpoint_1-checkpoint_2" in html
    # Well-formed enough that opening/closing tag counts balance.
    # Full HTML parsing isn't worth a bs4 dep; this catches gross
    # malformedness like unclosed tags.
    assert html.count("<section") == html.count("</section>")
    assert html.count("<table") == html.count("</table>")


def test_render_no_fits_for_game_exits_nonzero(tmp_path: Path):
    """Empty-fits path: the CLI exits 1 and prints a message."""
    config_path = _write_config(tmp_path)
    data_dir = tmp_path / "data"
    db = Database(str(data_dir / "spinlab.db"))
    db.upsert_game("emptygame", "Empty", "any%")
    db.close()

    out_path = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, "-m", "spinlab.cli", "fit", "render",
         "--config", str(config_path),
         "--game", "emptygame",
         "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "no fits found" in result.stdout
    assert not out_path.exists()
```

- [ ] **Step 2: Run to verify the test scaffolding works**

Run: `python -m pytest tests/unit/test_cli_fit_render.py -v`
Expected: both tests pass — the CLI registration from Task 11 has already shipped, so this should be green on first run.

If anything fails, debug ONLY the test scaffolding; do not modify the renderer or CLI code unless a real bug is found. Common gotchas:
- `Database.upsert_segment(seg)` takes a `Segment` dataclass, not a dict — note the type hints
- `EndpointType.CHECKPOINT` (StrEnum) not the string "checkpoint" — though they compare equal, the field type matters
- Production code requires `_segments_v07/V1_ESSENCE.md` cardinality (10 latents, 4 PPC stats); shortcuts in the payload may surface as KeyError in the renderer

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cli_fit_render.py
git commit -m "test(cli): subprocess integration test for 'spinlab fit render'"
```

---

## Task 13: Smoke test against Beto

Run the CLI against the real Beto DB (32 fits, 19 segments). Eyeball the output. This is a manual verification step; no commit unless something needs fixing.

**Files:** none.

- [ ] **Step 1: Generate the report**

Run:
```
python -m spinlab.cli fit render --game 01c9321b576c3415 --out /tmp/beto-phase2b.html
```
Expected: `wrote /tmp/beto-phase2b.html (... bytes, 19 segments)`.

- [ ] **Step 2: Open and eyeball**

Run: `python -c "import webbrowser; webbrowser.open('file:///tmp/beto-phase2b.html')"` (or open the file manually).

Verify in the browser:
1. Title shows "Beto"
2. Index table lists 19 segments (the count in stdout)
3. Clicking an entry jumps to that segment's section
4. Fittable segments show a learning curve with three subplots
5. Unfittable segments (L1 entrance n=20, L16 entrance all-N) show the "no fit" placeholder, NOT a fake curve
6. Raw attempts strip shows the right number of dots, colored by outcome
7. PPC table shows four rows for fittable, "no PPC" for unfittable
8. History table shows multiple rows for segments with multiple stored fits (L16 entrance should have ~7 rows; the L1 entrance Y→N flip should be visible)

- [ ] **Step 3: If anything is broken, add a fix task and a regression test**

If the smoke test reveals an issue not covered by unit tests, write a unit test that fails on the issue, then fix. Add this work as a new task to this plan rather than silently editing past tasks.

- [ ] **Step 4: No commit; this task produces an artifact, not source**

The `/tmp/beto-phase2b.html` file is not checked in. The Phase 2 findings doc (next task) will reference what was seen.

---

## Task 14: Full pytest + commit hygiene

CLAUDE.md mandates the full unfiltered suite must pass before declaring work done. This includes emulator tests if the environment supports them; surface any skips that aren't pre-recorded.

**Files:** none.

- [ ] **Step 1: Run the full pytest suite**

Run: `python -m pytest`
Expected: all green.

If unexpected skips: surface them. Per CLAUDE.md, `SKIPPED` ≠ passing. Add a flake entry to `memory/project_test_reliability_known_issues.md` if the skip is environmental and out of scope.

- [ ] **Step 2: Run pyright on the new files**

Run: `npx pyright python/spinlab/fit_renderer.py python/spinlab/cli_fit_render.py`
Expected: zero new errors. Pre-existing errors elsewhere in the codebase are tracked separately.

- [ ] **Step 3: Run ruff on the new files**

Run: `ruff check python/spinlab/fit_renderer.py python/spinlab/cli_fit_render.py`
Expected: zero issues.

- [ ] **Step 4: Verify git state is clean and on the right branch**

Run: `git status && git log --oneline main..HEAD`
Expected: clean working tree; commits on `feat/segments-v07-phase2b-renderer` form a coherent sequence (one commit per task, ~11-12 commits total).

- [ ] **Step 5: Hand off**

The branch is ready for the user to review. Per CLAUDE.md, merging is the user's call — surface the branch and let them decide whether to merge directly or open a PR.

---

## Self-review notes

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| New `fit_renderer.py` module with the four named helpers | Tasks 2-10 |
| `_render_history_strip` as HTML table | Task 7 (renamed `render_history_table_html` for clarity) |
| CLI subcommand `spinlab fit render --game <id> --out <path> [--open]` | Task 11 |
| Loads `iter_segment_fit_summaries`, `iter_recent_segment_fits`, raw attempts | Task 11 (uses `get_segment_event_rows` instead of `get_all_attempts_by_segment` — see deviation note below) |
| Per-segment view: header + headline + learning curve + attempts strip + PPC + history | Tasks 3-8 |
| Game-level index with status icons | Task 9 |
| matplotlib added to `[fits]` extra | Task 1 |
| Three test classes (renderer unit, per-helper, CLI subprocess) | Tasks 2-10, 12 |

**Spec deviation:** the spec lists `db.get_all_attempts_by_segment` as the data source for the raw-attempts strip, but that method returns episode-aggregated rows (one entry per episode). For a per-event strip plot we need raw event rows, so the plan uses `db.get_segment_event_rows(segment_id)` per segment instead. Functionally equivalent for the audit use; flagging here so the spec is not silently contradicted.

**Type consistency:** `bundle` shape (the dict the CLI hands to `build_report`) is defined in the File map at the top and reused identically across Tasks 8-12. The helper names (`render_headline_html`, `render_learning_curve_svg`, `render_attempts_strip_svg`, `render_ppc_table_html`, `render_history_table_html`, `render_game_index_html`, `render_segment_section`, `build_report`) match between definition and call sites.

**Placeholders:** none. Every step shows the actual code or command.
