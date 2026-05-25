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

import io
import math
from typing import Any


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
    if m_clear is not None:
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


# Outcome → marker color. Plain hex so renders identically in every browser.
_OUTCOME_COLOR = {
    "survived": "#1a9850",   # green
    "died":     "#d73027",   # red
}
# Fallback color for outcomes not in _OUTCOME_COLOR (e.g. a future model
# emitting a new outcome value). Neutral grey so it stands out as "I
# don't know how to colour this" without competing visually with the
# known outcomes.
_UNKNOWN_OUTCOME_COLOR = "#888888"


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
    colors = [_OUTCOME_COLOR.get(e["outcome"], _UNKNOWN_OUTCOME_COLOR) for e in events]

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
        if m is not None:
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
        m_str = f"{_fmt_seconds(m['median_ms'])}s" if m is not None else "—"
        rows.append(
            f"<tr><td><a href='#{anchor}'>{label}</a></td>"
            f"<td>{n}</td><td>{icon}</td><td>{m_str}</td></tr>"
        )
    return "<table class='index'>\n" + header + "\n" + "\n".join(rows) + "\n</table>"


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
