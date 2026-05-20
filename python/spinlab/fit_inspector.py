"""Pretty-printer for segments-v07 v1 fit payloads.

Pure-Python (no DB, no JAX) so it's cheap to unit-test and easy to
reuse anywhere a v1 envelope needs to render as human text.

The format is deliberately plain ASCII — no color codes, no Unicode
box-drawing — so output stays correct when piped to a file or grep
and renders cleanly under PowerShell on Windows.

The v1 envelope contract lives in
``python/spinlab/_segments_v07/external_docs/api_contract.md`` and is
the source of truth for field semantics.
"""
from __future__ import annotations

from typing import Any

# Column width for the "segment_id" cell in `fit list` output. Long
# segment ids get truncated to this length; trims past 24 char are
# represented with a trailing ellipsis.
_SEGMENT_ID_COL_WIDTH = 24

# Short codes for status.band_source, matching the contract values
# `"laplace"`, `"nuts"`, `"none"`. Anything else (or None) renders as
# `-` so the list-row stays compact.
_BAND_SHORT = {"laplace": "lap", "nuts": "nuts", "none": "none"}


def _yn(flag: Any) -> str:
    """Render a status bool as Y / N / -. Anything other than True/False
    (including None or 0/1 ints from the DB layer) is normalized."""
    if flag is True or flag == 1:
        return "Y"
    if flag is False or flag == 0:
        return "N"
    return "-"


def _truncate_segment_id(sid: str, width: int = _SEGMENT_ID_COL_WIDTH) -> str:
    if len(sid) <= width:
        return sid
    # Reserve 3 chars for the ellipsis. For absurdly small widths
    # (width<4), the ellipsis won't fit — just hard-truncate.
    if width < 4:
        return sid[:width]
    return sid[: width - 3] + "..."


def format_fit_payload(
    payload: dict[str, Any], fitted_at: str | None = None,
) -> str:
    """Pretty-print a v1 fit envelope as multi-section text.

    Sections rendered (in order):
      1. Header — segment_id, kind, n_attempts, model, wall_time, fitted_at
      2. Status — the five status flags
      3. Derived — M_clear (median + 90% interval) and death_rate_next
                   (skipped for pool_fit; replaced with pool summary)
      4. Bands — per-latent log-space p5/p50/p95
                   (skipped for pool_fit and unconverged envelopes)
      5. Caveats — bullet list of stable caveat keys
    """
    kind = payload["kind"]
    lines: list[str] = []

    # Header. segment_id is the one mandatory-but-nullable field
    # (pool envelopes carry None); render as <pool> for the human.
    sid = payload.get("segment_id") or "<pool>"
    lines.append(f"=== {sid}")
    lines.append(f"  kind: {kind}")
    lines.append(f"  n_attempts: {payload['n_attempts']}")
    lines.append(f"  model: {payload['model']}")
    lines.append(f"  wall_time_s: {float(payload['wall_time_s']):.3f}")
    if fitted_at:
        lines.append(f"  fitted_at: {fitted_at}")

    # Status.
    status = payload["status"]
    lines.append("")
    lines.append("Status")
    for key in ("converged", "band_source", "laplace_pd", "ppc_tension", "fittable"):
        val = status[key]
        if isinstance(val, bool):
            shown = "yes" if val else "no"
        elif val is None:
            shown = "-"
        else:
            shown = str(val)
        lines.append(f"  {key}: {shown}")

    # Derived (segment_fit) or Pool summary (pool_fit).
    lines.append("")
    if kind == "pool_fit":
        pool = payload.get("result", {}).get("pool", {})
        lines.append("Pool")
        n_used = pool.get("n_segments_used", "?")
        lines.append(f"  n_segments_used: {n_used}")
        for hl_name in ("halflife_sf", "halflife_ssp", "halflife_alpha"):
            entry = pool.get(hl_name)
            if entry is None:
                continue
            lines.append(
                f"  {hl_name}: mean={entry['mean']:.3f}  sigma={entry['sigma']:.3f}"
            )
    else:
        derived = payload.get("result", {}).get("derived", {})
        lines.append("Derived")
        if not derived:
            lines.append("  (no derived stats — fit did not converge)")
        else:
            mc = derived.get("M_clear")
            if mc:
                lines.append(
                    f"  M_clear median: {int(mc['median_ms'])} ms"
                    f"  (90%: {int(mc['p5_ms'])}..{int(mc['p95_ms'])} ms)"
                )
            drn = derived.get("death_rate_next")
            if drn is not None:
                lines.append(f"  death_rate_next: {drn * 100:.1f}%")

    # Bands (only meaningful for segment_fit + converged).
    if kind == "segment_fit":
        bands = payload.get("result", {}).get("bands", {})
        if bands:
            lines.append("")
            lines.append("Bands (log-space)")
            for latent, band in bands.items():
                if band is None:
                    lines.append(f"  {latent}: (suppressed)")
                else:
                    lines.append(
                        f"  {latent}: p5={band['p5']:.3f}  "
                        f"p50={band['p50']:.3f}  p95={band['p95']:.3f}"
                    )

    # Caveats.
    lines.append("")
    lines.append("Caveats")
    caveats = payload["caveats"]
    if not caveats:
        lines.append("  (none)")
    else:
        for c in caveats:
            lines.append(f"  - {c}")

    return "\n".join(lines)


def format_fit_summary_row(summary: dict[str, Any]) -> str:
    """One tab-separated row for `spinlab fit list`.

    Eight columns: segment_id, level, n, fittable, ppc, band, M50, fitted.
    Empty / missing values render as `-` so the row stays a fixed shape.
    """
    sid = _truncate_segment_id(str(summary.get("segment_id", "?")))
    level = summary.get("level_number")
    level_s = str(level) if level is not None else "-"
    n = summary.get("n_attempts")
    n_s = str(n) if n is not None else "-"
    fittable_s = _yn(summary.get("fittable"))
    ppc_s = _yn(summary.get("ppc_tension"))
    band = summary.get("band_source")
    band_s = _BAND_SHORT.get(band, "-") if band else "-"

    derived = summary.get("payload", {}).get("result", {}).get("derived") or {}
    mc = derived.get("M_clear") or {}
    m50 = mc.get("median_ms")
    m50_s = str(int(m50)) if m50 is not None else "-"

    fitted_at = summary.get("fitted_at") or ""
    # Date portion only; the time-of-day clutters the column.
    fitted_s = fitted_at[:10] if len(fitted_at) >= 10 else "-"

    return "\t".join([sid, level_s, n_s, fittable_s, ppc_s, band_s, m50_s, fitted_s])


def format_history_line(payload: dict[str, Any], fitted_at: str) -> str:
    """One single-line summary for `spinlab fit show --history`.

    Format:  `<fitted_at>  n=<N>  fittable=<Y/N>  M50=<ms>  band=<source>`
    """
    status = payload["status"]
    derived = payload.get("result", {}).get("derived") or {}
    mc = derived.get("M_clear") or {}
    m50 = mc.get("median_ms")
    m50_s = str(int(m50)) if m50 is not None else "-"
    band = status["band_source"]
    return (
        f"{fitted_at}  n={payload['n_attempts']}  "
        f"fittable={_yn(status['fittable'])}  "
        f"M50={m50_s}  band={band}"
    )
