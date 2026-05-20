---
date: 2026-05-19
status: drafted
focus: "Phase 2a of segments-v07 integration — CLI inspector (`spinlab fit show`, `spinlab fit list`) over the segment_fits rows that Phase 1 already writes. Text-only; no HTML renderer yet."
spec: docs/superpowers/specs/2026-05-18-segments-v07-integration-design.md
predecessors:
  - docs/superpowers/plans/2026-05-18-segments-v07-phase0-event-level-attempts.md (shipped 36c5536)
  - docs/superpowers/plans/2026-05-19-segments-v07-phase1-silent-fits.md (shipped 862a5de)
scope: phase-2a only; HTML renderer is phase-2b; no dashboard / allocator changes
---

# Segments-v07 Phase 2a — Fit Inspector CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `spinlab fit show <segment_id>` and `spinlab fit list` so Andrew can inspect the v1 fit payloads that the silent Phase 1 pipeline has been writing into `segment_fits`. Text-only output — pretty-printed sections + JSON output for piping. No HTML renderer (that's Phase 2b). No allocator change (Phase 3).

**Architecture:**

```
spinlab fit list  [--game G] [--kind segment_fit|pool_fit] [--json]
        │
        ▼
db.iter_segment_fit_summaries(game_id, kind)  ──►  table of (segment_id, n, fittable, ppc, band_source, M_clear_p50, fitted_at)

spinlab fit show <segment_id> [--kind …] [--history N] [--json]
        │
        ├── default:    db.load_latest_segment_fit(sid, kind)  →  pretty-print sections
        ├── --json:     same row, but dumped raw
        └── --history N: db.iter_recent_segment_fits(sid, limit=N, kind)
                                 │
                                 ▼
                         one-line summaries per row (n, fittable, M_clear p50, fitted_at)
```

`db.load_latest_segment_fit`, `db.iter_recent_segment_fits`, and `db.save_segment_fit` already exist (shipped in Phase 1, see `python/spinlab/db/segment_fits.py`). We add one new DB helper — `iter_segment_fit_summaries` — that does a single indexed query joining latest-fit-per-segment to the segments table, so `fit list` doesn't N+1-query.

**Tech Stack:** Pure Python stdlib for formatting (no Rich/Click dependency — keep the CLI lightweight and match the existing argparse style in `python/spinlab/cli.py`). JSON output via `json.dumps(..., indent=2)`. No frontend or dashboard changes.

## Anchor questions resolved

- **Existing CLI shape.** `python/spinlab/cli.py` uses top-level argparse subcommands (`dashboard`, `replay`, `db`, `fit-pool`). `db` already nests subcommands (`db reset`). We follow the `db`-style nesting for the new `fit` parent: `spinlab fit show`, `spinlab fit list`. `fit-pool` stays as a sibling top-level subcommand — it's already in muscle memory and renaming it to `spinlab fit pool` would be a breaking change for zero gain. (See [[feedback_iterate]] — PoC-first means not paying refactor cost for a cosmetic win.)
- **Where the subcommand modules live.** Existing precedent: `python/spinlab/cli_fit_pool.py` is a single sibling module to `cli.py`, not nested under a `cli/` package. We match it with `python/spinlab/cli_fit.py` (one file, both `show` and `list` runners).
- **Payload pretty-print sections.** The v1 envelope has stable top-level fields (`schema`, `kind`, `segment_id`, `n_attempts`, `model`, `wall_time_s`, `status`, `result`, `caveats`). See `python/spinlab/_segments_v07/external_docs/api_contract.md`. We render: header (segment_id / n_attempts / model / fitted_at / wall_time_s), status (5 fields), derived (M_clear p5/p50/p95, death_rate_next), bands (per-latent log-space p5/p50/p95 with `null` rendered as "(suppressed)"), caveats (list).
- **Color / Unicode.** Default to plain ASCII — no color codes, no Unicode box-drawing. Andrew runs from PowerShell on Windows where ANSI handling is uneven and box characters render as `?` in some terminals. The output should look right when piped into a file or grep.
- **What `fit list` shows.** Eight columns: `segment_id` (truncated to 24c), `level` (level_number), `n` (n_attempts), `fittable` (Y/N/-), `ppc` (Y/N/-), `band` (`lap`/`nuts`/`-`), `M50` (M_clear.median_ms in ms, or `-` if absent), `fitted` (ISO date, no time). Limit to active segments by default; `--all` includes inactive. Tab-separated columns — readable and clipboard-friendly.

## Anchor questions still open at end of Phase 2a

- **What `--history N` should render per row** beyond `(n, fittable, M_clear p50, fitted_at)`. Phase 2a ships exactly those four; Phase 2b's renderer is the natural place for trend graphs over the same data.
- **Should `fit list` show pool_fit rows alongside segment_fit?** Phase 2a default: filter by `--kind` (default `segment_fit`). Cross-kind diffing is a Phase 2b concern.

## File structure

### New files

- `python/spinlab/cli_fit.py` — the new `spinlab fit show` + `spinlab fit list` subcommand module. Pure formatting + DB lookups; no JAX import (so `spinlab fit show` is fast even without `[fits]`).
- `python/spinlab/fit_inspector.py` — pure-Python formatter functions (`format_fit_payload`, `format_fit_summary_row`, `format_history_line`). Separated from the CLI driver so they're testable without subprocess overhead.
- `tests/unit/test_fit_inspector.py` — unit tests for the formatter functions.
- `tests/unit/test_cli_fit.py` — unit tests for the CLI run-functions (in-process; not subprocess).
- `tests/integration/test_cli_fit_subprocess.py` — one end-to-end subprocess test confirming `spinlab fit show` and `spinlab fit list` exit cleanly.

### Modified files

- `python/spinlab/cli.py` — register the `fit` parent subcommand and route `parsed.command == "fit"` to the new module.
- `python/spinlab/db/segment_fits.py` — add `iter_segment_fit_summaries(game_id, kind)` for the `fit list` SQL-side aggregation.
- `tests/unit/db/test_segment_fits.py` — add a test for the new summary helper.

### Files NOT touched

- `python/spinlab/cli_fit_pool.py` — Phase 1 CLI. Already shipped, stays as a sibling.
- `python/spinlab/segments_model/__init__.py` — no new model surface needed; we only consume what's already persisted.
- The frontend — Phase 2a is silent on the dashboard.
- `python/spinlab/_segments_v07/` — vendored; treat as opaque.

---

## Task 1 — DB helper: `iter_segment_fit_summaries`

**Files:**
- Modify: `python/spinlab/db/segment_fits.py`
- Test:   `tests/unit/db/test_segment_fits.py`

The `fit list` command needs to show one row per segment with the latest fit's status fields. A naive implementation would loop segments and call `load_latest_segment_fit` per segment — N+1 queries. We add a single SQL query that does the join + window pick in one round-trip.

- [ ] **Step 1.1: Read existing tests for the mixin**

Run: `cat tests/unit/db/test_segment_fits.py`

Expected: 5 existing tests (save+load round-trip, missing segment, pool kind keyed separately, iter recent ordering, status-column extraction). The fixture pattern (`db` fixture that seeds one `games` row + one `segments` row) is what we'll extend.

- [ ] **Step 1.2: Write the failing test for the new helper**

Open `tests/unit/db/test_segment_fits.py`. Append at the end:

```python
def test_iter_segment_fit_summaries_returns_one_row_per_segment(db):
    """Multiple fits on the same segment → only the latest is in the summary."""
    # Seed a second segment so we can confirm the helper returns both.
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s2', 'g1', 2, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()

    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=11))
    db.save_segment_fit("s2", "segment_fit", _make_payload(n_attempts=20))

    rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    by_id = {r["segment_id"]: r for r in rows}
    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"]["n_attempts"] == 11  # Latest, not 10.
    assert by_id["s2"]["n_attempts"] == 20
    # Status fields are projected to columns; the helper exposes them.
    assert by_id["s1"]["fittable"] == 1
    assert by_id["s1"]["band_source"] == "laplace"
    # Each row carries the latest fit's payload so the caller can dig into
    # `derived.M_clear` etc. without a second query.
    assert by_id["s1"]["payload"]["n_attempts"] == 11


def test_iter_segment_fit_summaries_skips_segments_with_no_fits(db):
    """A segment with no fits at all does NOT appear in the summary —
    the list view is "show me what we know about", not "show me every
    segment". Empty-segment rendering is the caller's concern."""
    # Seed a second segment but write NO fits for it.
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s2', 'g1', 2, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()

    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))

    rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    assert [r["segment_id"] for r in rows] == ["s1"]


def test_iter_segment_fit_summaries_filters_by_kind(db):
    """A pool_fit on s1 should not appear in a segment_fit summary."""
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    pool_payload = {**_make_payload(n_attempts=999), "kind": "pool_fit"}
    db.save_segment_fit("s1", "pool_fit", pool_payload)

    seg_rows = list(db.iter_segment_fit_summaries("g1", kind="segment_fit"))
    pool_rows = list(db.iter_segment_fit_summaries("g1", kind="pool_fit"))
    assert [r["n_attempts"] for r in seg_rows] == [10]
    assert [r["n_attempts"] for r in pool_rows] == [999]
```

- [ ] **Step 1.3: Run the new tests to verify they fail**

Run: `pytest tests/unit/db/test_segment_fits.py::test_iter_segment_fit_summaries_returns_one_row_per_segment -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'iter_segment_fit_summaries'`.

- [ ] **Step 1.4: Implement `iter_segment_fit_summaries`**

Open `python/spinlab/db/segment_fits.py`. Add this method on the `SegmentFitsMixin` class (after `iter_recent_segment_fits`):

```python
    def iter_segment_fit_summaries(
        self, game_id: str, *, kind: FitKind = "segment_fit",
    ) -> Iterator[dict[str, Any]]:
        """One summary row per segment in ``game_id`` that has a fit of ``kind``.

        Returns the latest (highest-id) row per segment in a single
        indexed query so ``spinlab fit list`` is one round-trip even with
        hundreds of segments. Segments with no fits at all are skipped —
        the list view's contract is "show me what we know about", not
        "show me every segment". The caller renders empty-segment
        bookkeeping separately if it wants to.

        Each yielded dict has:
          - segment_id (str)
          - level_number (int from segments.level_number)
          - active (int: 0|1)
          - kind, n_attempts, band_source, fittable, ppc_tension,
            wall_time_ms, fitted_at (projected columns)
          - payload (dict, parsed from payload_json) so the caller can
            dig into derived stats without a second query.
        """
        # SQLite's "GROUP BY + MAX(id) row identity" trick: in SQLite, a
        # GROUP BY with a non-aggregated column returns the row matching
        # MAX/MIN of the aggregate column. We rely on that to pick the
        # latest fit per segment in one query. (This is documented
        # behavior since SQLite 3.7.11 — "bare columns in an aggregate
        # query".)
        rows = self.conn.execute(
            """
            SELECT
              sf.segment_id AS segment_id,
              s.level_number AS level_number,
              s.active AS active,
              sf.kind AS kind,
              sf.n_attempts AS n_attempts,
              sf.band_source AS band_source,
              sf.fittable AS fittable,
              sf.ppc_tension AS ppc_tension,
              sf.wall_time_ms AS wall_time_ms,
              sf.fitted_at AS fitted_at,
              sf.payload_json AS payload_json,
              MAX(sf.id) AS _latest_id
            FROM segment_fits sf
            JOIN segments s ON s.id = sf.segment_id
            WHERE s.game_id = ? AND sf.kind = ?
            GROUP BY sf.segment_id
            ORDER BY s.ordinal ASC, s.level_number ASC, s.id ASC
            """,
            (game_id, kind),
        ).fetchall()
        for row in rows:
            yield {
                "segment_id": row["segment_id"],
                "level_number": row["level_number"],
                "active": row["active"],
                "kind": row["kind"],
                "n_attempts": row["n_attempts"],
                "band_source": row["band_source"],
                "fittable": row["fittable"],
                "ppc_tension": row["ppc_tension"],
                "wall_time_ms": row["wall_time_ms"],
                "fitted_at": row["fitted_at"],
                "payload": json.loads(row["payload_json"]),
            }
```

- [ ] **Step 1.5: Run the new tests to verify they pass**

Run: `pytest tests/unit/db/test_segment_fits.py -v`
Expected: 8 tests pass (5 existing + 3 new).

- [ ] **Step 1.6: Commit Task 1**

```bash
git add python/spinlab/db/segment_fits.py tests/unit/db/test_segment_fits.py
git commit -m "segments-v07 phase 2a: add iter_segment_fit_summaries DB helper

Single indexed query joins latest-fit-per-segment to the segments table
so the upcoming \`spinlab fit list\` is one round-trip. Skips segments
with no fits at all — the list view's contract is 'show me what we know
about'."
```

---

## Task 2 — Pure-Python payload formatter

**Files:**
- Create: `python/spinlab/fit_inspector.py`
- Test:   `tests/unit/test_fit_inspector.py`

A pure function `format_fit_payload(payload) -> str` that takes a v1 envelope and returns the pretty-printed multi-section text. Separating this from the CLI driver makes it testable without subprocess overhead and easy to reuse if we ever want to drop it into a dashboard endpoint.

- [ ] **Step 2.1: Write the failing test**

Create `tests/unit/test_fit_inspector.py`:

```python
"""Pure-Python tests for the v07 fit-payload pretty-printer.

Drives the formatter directly with constructed payloads — no DB, no
subprocess, no JAX. Each test is one rendering rule.
"""
from __future__ import annotations

from spinlab.fit_inspector import (
    format_fit_payload, format_fit_summary_row, format_history_line,
)


def _full_payload(**overrides):
    """Construct a complete v1 envelope; overrides patch the top level."""
    payload = {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": "w1-2-castle",
        "n_attempts": 234,
        "model": "haz1",
        "wall_time_s": 0.043,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False,
            "fittable": True,
        },
        "result": {
            "map": {
                "log_theta": [9.901] + [0.0] * 9,
                "natural": {
                    "bpt_ms": 20000.0,
                    "sf_inf": 0.10, "sf_1": 0.20,
                    "ssp_inf": 0.20, "ssp_1": 0.30,
                    "alpha_inf": 0.30, "alpha_1": 0.40,
                    "halflife_sf": 15.0, "halflife_ssp": 15.0,
                    "halflife_alpha": 15.0,
                },
            },
            "bands": {
                "log_bpt": {"p5": 9.85, "p50": 9.90, "p95": 9.95},
                "log_hl_ssp": None,
            },
            "derived": {
                "M_clear": {
                    "median_ms": 31250.0, "p5_ms": 28900.0, "p95_ms": 35100.0,
                },
                "death_rate_next": 0.21,
            },
            "ppc": {
                "died_rate": {"obs": 0.32, "p_two_sided": 0.43},
            },
        },
        "caveats": [],
    }
    payload.update(overrides)
    return payload


def test_format_fit_payload_includes_all_headline_sections():
    """A well-formed envelope renders the header + 5 sections."""
    out = format_fit_payload(_full_payload(), fitted_at="2026-05-19T12:00:00Z")
    # Header
    assert "w1-2-castle" in out
    assert "n_attempts: 234" in out
    assert "model: haz1" in out
    # Status section
    assert "Status" in out
    assert "converged: yes" in out
    assert "band_source: laplace" in out
    assert "fittable: yes" in out
    # Derived section
    assert "Derived" in out
    assert "M_clear" in out
    # The headline number rendered as ms with seconds in parens.
    assert "31250 ms" in out  # median
    assert "death_rate_next: 21.0%" in out
    # Bands section
    assert "Bands" in out
    assert "log_bpt" in out
    assert "log_hl_ssp" in out
    assert "(suppressed)" in out  # null band
    # Caveats section — empty case shows "(none)" so the reader sees nothing was withheld.
    assert "Caveats" in out
    assert "(none)" in out


def test_format_fit_payload_renders_caveats_list():
    p = _full_payload(caveats=["low_n", "nuts_fallback"])
    out = format_fit_payload(p)
    assert "low_n" in out
    assert "nuts_fallback" in out


def test_format_fit_payload_handles_unconverged_envelope():
    """When converged=false, we don't promise bands or derived stats —
    the renderer should print the status block and a warning, not crash
    on missing `derived` fields."""
    p = _full_payload()
    p["status"]["converged"] = False
    p["status"]["fittable"] = False
    p["status"]["band_source"] = "none"
    p["result"]["derived"] = {}
    p["caveats"] = ["unconverged"]
    out = format_fit_payload(p)
    assert "converged: no" in out
    assert "band_source: none" in out
    # The renderer marks an absent derived block explicitly.
    assert "Derived" in out
    assert "(no derived stats — fit did not converge)" in out


def test_format_fit_payload_handles_pool_fit_kind():
    """Pool envelopes carry a different `result` shape (pool + segments
    instead of map/bands/derived). The renderer should detect kind and
    print a short pool summary."""
    pool = {
        "schema": "segments-v1",
        "kind": "pool_fit",
        "segment_id": None,
        "n_attempts": 712,
        "model": "haz1",
        "wall_time_s": 14.2,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "pool": {
                "halflife_sf":    {"mean": 2.71, "sigma": 0.58},
                "halflife_ssp":   {"mean": 2.83, "sigma": 0.42},
                "halflife_alpha": {"mean": 2.90, "sigma": 0.71},
                "n_segments_used": 3,
            },
            "segments": [],
        },
        "caveats": [],
    }
    out = format_fit_payload(pool)
    assert "kind: pool_fit" in out
    assert "halflife_sf" in out
    assert "halflife_alpha" in out
    assert "n_segments_used: 3" in out


def test_format_fit_summary_row_renders_tab_separated_columns():
    """One row of `spinlab fit list` output."""
    summary = {
        "segment_id": "w1-2-castle",
        "level_number": 2,
        "active": 1,
        "kind": "segment_fit",
        "n_attempts": 234,
        "band_source": "laplace",
        "fittable": 1,
        "ppc_tension": 0,
        "wall_time_ms": 43,
        "fitted_at": "2026-05-19T12:00:00.000Z",
        "payload": _full_payload(),
    }
    row = format_fit_summary_row(summary)
    # Tab-separated. The first field is the truncated segment id.
    parts = row.split("\t")
    assert parts[0].startswith("w1-2-castle")
    # Eight columns total per the design (segment_id, level, n, fittable,
    # ppc, band, M50, fitted).
    assert len(parts) == 8
    assert parts[1] == "2"             # level
    assert parts[2] == "234"           # n
    assert parts[3] == "Y"             # fittable
    assert parts[4] == "N"             # ppc tension
    assert parts[5] == "lap"           # band_source short
    assert parts[6] == "31250"         # M_clear.median_ms
    assert parts[7] == "2026-05-19"    # fitted_at date only


def test_format_fit_summary_row_renders_dash_when_derived_missing():
    """Pool fits or unconverged envelopes have no derived.M_clear — the
    row shows `-` rather than crashing."""
    summary = {
        "segment_id": "s1", "level_number": 1, "active": 1,
        "kind": "segment_fit", "n_attempts": 3,
        "band_source": None, "fittable": 0, "ppc_tension": None,
        "wall_time_ms": 50, "fitted_at": "2026-05-19T12:00:00.000Z",
        "payload": {"result": {"derived": {}}, "status": {}},
    }
    parts = format_fit_summary_row(summary).split("\t")
    assert parts[3] == "N"   # fittable=0
    assert parts[4] == "-"   # ppc_tension None
    assert parts[5] == "-"   # band_source None
    assert parts[6] == "-"   # M50 absent


def test_format_history_line_is_one_line_per_fit():
    """`--history N` renders one line per fit. Format:
       `<fitted_at>  n=<N>  fittable=<Y/N>  M50=<ms>  band=<source>`"""
    p = _full_payload()
    line = format_history_line(p, fitted_at="2026-05-19T12:00:00.000Z")
    # Single line; no embedded newlines.
    assert "\n" not in line
    assert "2026-05-19T12:00:00" in line
    assert "n=234" in line
    assert "fittable=Y" in line
    assert "M50=31250" in line
    assert "band=laplace" in line
```

- [ ] **Step 2.2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_fit_inspector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.fit_inspector'`.

- [ ] **Step 2.3: Implement the formatter module**

Create `python/spinlab/fit_inspector.py`:

```python
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
    kind = payload.get("kind", "segment_fit")
    lines: list[str] = []

    # Header.
    sid = payload.get("segment_id") or "<pool>"
    lines.append(f"=== {sid}")
    lines.append(f"  kind: {kind}")
    lines.append(f"  n_attempts: {payload.get('n_attempts', 0)}")
    lines.append(f"  model: {payload.get('model', '?')}")
    lines.append(f"  wall_time_s: {float(payload.get('wall_time_s', 0)):.3f}")
    if fitted_at:
        lines.append(f"  fitted_at: {fitted_at}")

    # Status.
    status = payload.get("status", {})
    lines.append("")
    lines.append("Status")
    for key in ("converged", "band_source", "laplace_pd", "ppc_tension", "fittable"):
        val = status.get(key)
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
    caveats = payload.get("caveats", [])
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
    status = payload.get("status", {})
    derived = payload.get("result", {}).get("derived") or {}
    mc = derived.get("M_clear") or {}
    m50 = mc.get("median_ms")
    m50_s = str(int(m50)) if m50 is not None else "-"
    band = status.get("band_source") or "-"
    return (
        f"{fitted_at}  n={payload.get('n_attempts', '?')}  "
        f"fittable={_yn(status.get('fittable'))}  "
        f"M50={m50_s}  band={band}"
    )
```

- [ ] **Step 2.4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_fit_inspector.py -v`
Expected: all 7 tests pass.

- [ ] **Step 2.5: Run the full fast suite for regressions**

Run: `pytest -m "not emulator" -q 2>&1 | tail -10`
Expected: green; baseline + 7 new tests.

- [ ] **Step 2.6: Commit Task 2**

```bash
git add python/spinlab/fit_inspector.py tests/unit/test_fit_inspector.py
git commit -m "segments-v07 phase 2a: pure-Python v1 payload pretty-printer

format_fit_payload renders the v1 envelope as plain-ASCII multi-section
text. format_fit_summary_row drives the upcoming \`spinlab fit list\`
output; format_history_line drives \`spinlab fit show --history\`.
Separated from the CLI driver so the formatter is unit-testable
without subprocess overhead."
```

---

## Task 3 — `spinlab fit show` and `spinlab fit list` subcommands

**Files:**
- Create: `python/spinlab/cli_fit.py`
- Modify: `python/spinlab/cli.py` (register `fit` parent subcommand)
- Test:   `tests/unit/test_cli_fit.py`

- [ ] **Step 3.1: Read the existing CLI shape**

Run: `cat python/spinlab/cli.py | head -30 ; echo --- ; grep -n 'sub.add_parser\|add_subparser\|p_db\|p_replay' python/spinlab/cli.py`

Note: existing pattern is one `sub.add_parser("<name>", ...)` per top-level subcommand. The `db` subcommand nests its own subparser (see `db_sub = p_db.add_subparsers(...)` lines around 172-174). We'll match this style for `fit`.

- [ ] **Step 3.2: Write the failing CLI tests**

Create `tests/unit/test_cli_fit.py`:

```python
"""In-process tests for `spinlab fit show` and `spinlab fit list`.

Drives the runner functions directly with constructed argparse
Namespaces. End-to-end subprocess coverage lives in
`tests/integration/test_cli_fit_subprocess.py`.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest

from spinlab.db import Database


def _seed_db(tmp_path):
    """Seed one game, two segments, one fit on each."""
    cfg_path = tmp_path / "spinlab.yaml"
    cfg_path.write_text(
        f"data_dir: {tmp_path}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    db = Database(tmp_path / "spinlab.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    for sid, lvl in [("s1", 1), ("s2", 2)]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, "
            "start_type, end_type, ordinal, created_at, updated_at) "
            "VALUES (?, 'g1', ?, 'entrance', 'exit', ?, "
            "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
            (sid, lvl, lvl),
        )
    db.conn.commit()

    payload_s1 = {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "s1", "n_attempts": 30,
        "model": "haz1", "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [9.9] + [0.0] * 9, "natural": {}},
            "bands": {"log_bpt": {"p5": 9.85, "p50": 9.9, "p95": 9.95}},
            "derived": {
                "M_clear": {"median_ms": 25000, "p5_ms": 22000, "p95_ms": 28000},
                "death_rate_next": 0.18,
            },
            "ppc": {},
        },
        "caveats": [],
    }
    db.save_segment_fit("s1", "segment_fit", payload_s1)
    payload_s2 = {**payload_s1, "segment_id": "s2", "n_attempts": 10,
                  "result": {**payload_s1["result"],
                             "derived": {"M_clear": {"median_ms": 40000,
                                                     "p5_ms": 35000, "p95_ms": 50000},
                                         "death_rate_next": 0.45}}}
    db.save_segment_fit("s2", "segment_fit", payload_s2)
    return cfg_path


def _run(cmd: str, **ns_overrides) -> tuple[int, str]:
    """Invoke the named runner with a constructed Namespace; capture stdout."""
    from spinlab import cli_fit
    runner = {"show": cli_fit.run_show, "list": cli_fit.run_list}[cmd]
    ns = argparse.Namespace(**ns_overrides)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = runner(ns)
    return code, buf.getvalue()


def test_fit_show_prints_payload_for_existing_segment(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=None, json_output=False, game="g1",
    )
    assert code == 0
    assert "s1" in out
    assert "n_attempts: 30" in out
    assert "M_clear median: 25000 ms" in out
    assert "fittable: yes" in out


def test_fit_show_json_dumps_raw_payload(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=None, json_output=True, game="g1",
    )
    assert code == 0
    parsed = json.loads(out)
    assert parsed["segment_id"] == "s1"
    assert parsed["n_attempts"] == 30
    assert parsed["status"]["fittable"] is True


def test_fit_show_returns_nonzero_when_segment_has_no_fit(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="nonexistent",
        kind="segment_fit", history=None, json_output=False, game="g1",
    )
    assert code == 1
    assert "no fit found" in out.lower()


def test_fit_show_history_prints_one_line_per_recent_fit(tmp_path):
    cfg_path = _seed_db(tmp_path)
    # Write two more fits on s1 so we have 3 total.
    db = Database(tmp_path / "spinlab.db")
    for n in (31, 32):
        db.save_segment_fit("s1", "segment_fit", {
            "schema": "segments-v1", "kind": "segment_fit",
            "segment_id": "s1", "n_attempts": n,
            "model": "haz1", "wall_time_s": 0.05,
            "status": {"converged": True, "band_source": "laplace",
                       "laplace_pd": True, "ppc_tension": False, "fittable": True},
            "result": {"derived": {"M_clear": {"median_ms": 25000 - n,
                                               "p5_ms": 0, "p95_ms": 0}}},
            "caveats": [],
        })
    code, out = _run(
        "show",
        config=str(cfg_path), segment_id="s1",
        kind="segment_fit", history=10, json_output=False, game="g1",
    )
    assert code == 0
    # Three fits → three lines (newest first).
    lines = [ln for ln in out.splitlines() if ln.startswith("20")]
    assert len(lines) == 3
    # Each line carries an n= field.
    assert all("n=" in ln for ln in lines)


def test_fit_list_renders_one_row_per_segment_with_fits(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "list",
        config=str(cfg_path), game="g1",
        kind="segment_fit", json_output=False,
    )
    assert code == 0
    # A header line + two data rows.
    data_lines = [ln for ln in out.splitlines() if "\t" in ln]
    assert len(data_lines) >= 2
    sids = {ln.split("\t")[0].strip() for ln in data_lines}
    assert {"s1", "s2"} <= sids


def test_fit_list_json_returns_array_of_summaries(tmp_path):
    cfg_path = _seed_db(tmp_path)
    code, out = _run(
        "list",
        config=str(cfg_path), game="g1",
        kind="segment_fit", json_output=True,
    )
    assert code == 0
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    sids = {row["segment_id"] for row in parsed}
    assert sids == {"s1", "s2"}


def test_fit_list_empty_game_prints_message_and_exits_zero(tmp_path):
    """A game with no fits should exit 0 with an informational message
    — not crash, not return nonzero."""
    cfg_path = _seed_db(tmp_path)
    # Reuse the seeded config but pass a game id that has no rows.
    code, out = _run(
        "list",
        config=str(cfg_path), game="g-empty",
        kind="segment_fit", json_output=False,
    )
    assert code == 0
    assert "no fits found" in out.lower()
```

- [ ] **Step 3.3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_cli_fit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.cli_fit'`.

- [ ] **Step 3.4: Implement `cli_fit.py`**

Create `python/spinlab/cli_fit.py`:

```python
"""spinlab fit — read-only inspector over the segment_fits table.

Two subcommands:

  spinlab fit show <segment_id> [--kind ...] [--history N] [--json]
      Pretty-prints the latest v1 envelope for a segment (default) or
      a one-line summary per recent fit (--history). --json dumps raw.

  spinlab fit list --game <id> [--kind ...] [--json]
      One row per segment in the game that has a fit. Tab-separated by
      default; --json dumps a list of summary dicts.

This module is intentionally JAX-free — it only reads what the silent
pipeline has already persisted, so `spinlab fit show` runs in <100ms
even without the [fits] extra installed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from spinlab.fit_inspector import (
    format_fit_payload,
    format_fit_summary_row,
    format_history_line,
)

# `fit list` header. Match the column order in
# `fit_inspector.format_fit_summary_row` exactly. Reader-friendly: the
# header is also tab-separated so it lines up with the data rows in
# most monospaced terminals.
_LIST_HEADER = "\t".join(
    ["segment_id", "lvl", "n", "fit", "ppc", "band", "M50", "fitted"]
)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `spinlab fit` parent subcommand and its children."""
    p_fit = subparsers.add_parser(
        "fit", help="Inspect segments-v07 silent fit payloads.",
    )
    fit_sub = p_fit.add_subparsers(dest="fit_command", required=True)

    p_show = fit_sub.add_parser(
        "show", help="Pretty-print the latest fit for a segment.",
    )
    p_show.add_argument("segment_id", help="Segment id to inspect.")
    p_show.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p_show.add_argument(
        "--kind", choices=("segment_fit", "pool_fit"), default="segment_fit",
        help="Which fit kind to load (default: segment_fit).",
    )
    p_show.add_argument(
        "--history", type=int, default=None, metavar="N",
        help="Print one-line summaries for the most recent N fits "
             "instead of pretty-printing the latest.",
    )
    p_show.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output raw JSON (the v1 envelope) instead of pretty text.",
    )

    p_list = fit_sub.add_parser(
        "list", help="One row per segment with a fit; tab-separated.",
    )
    p_list.add_argument(
        "--config", default="config.yaml",
        help="Path to the SpinLab YAML config (default: config.yaml).",
    )
    p_list.add_argument(
        "--game", required=True,
        help="Game id to list (must exist in the games table).",
    )
    p_list.add_argument(
        "--kind", choices=("segment_fit", "pool_fit"), default="segment_fit",
        help="Which fit kind to summarize (default: segment_fit).",
    )
    p_list.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output a JSON array of summary objects.",
    )


def _open_db(config_path: str):
    """Resolve config + open the SQLite DB the dashboard uses."""
    from spinlab.config import AppConfig
    from spinlab.db import Database
    cfg = AppConfig.from_yaml(Path(config_path))
    return Database(cfg.data_dir / "spinlab.db")


def run_show(parsed: argparse.Namespace) -> int:
    db = _open_db(parsed.config)
    segment_id = parsed.segment_id
    kind = parsed.kind

    if parsed.history is not None:
        # History mode: newest-first, one line per fit. We can't pull
        # fitted_at from the payload (it's only in the row), so we drop
        # back to a raw SQL fetch that returns both columns together.
        rows = db.conn.execute(
            """SELECT payload_json, fitted_at FROM segment_fits
               WHERE segment_id = ? AND kind = ?
               ORDER BY id DESC LIMIT ?""",
            (segment_id, kind, int(parsed.history)),
        ).fetchall()
        if not rows:
            print(f"no fits found for segment {segment_id!r} kind={kind!r}")
            return 1
        for row in rows:
            payload = json.loads(row["payload_json"])
            print(format_history_line(payload, row["fitted_at"]))
        return 0

    # Default: pretty-print the latest payload.
    payload = db.load_latest_segment_fit(segment_id, kind)  # type: ignore[arg-type]
    if payload is None:
        print(f"no fit found for segment {segment_id!r} kind={kind!r}")
        return 1
    fitted_at_row = db.conn.execute(
        """SELECT fitted_at FROM segment_fits
           WHERE segment_id = ? AND kind = ?
           ORDER BY id DESC LIMIT 1""",
        (segment_id, kind),
    ).fetchone()
    fitted_at = fitted_at_row["fitted_at"] if fitted_at_row else None

    if parsed.json_output:
        print(json.dumps(payload, indent=2, sort_keys=False))
        return 0

    print(format_fit_payload(payload, fitted_at=fitted_at))
    return 0


def run_list(parsed: argparse.Namespace) -> int:
    db = _open_db(parsed.config)
    game_id = parsed.game
    kind = parsed.kind

    summaries = list(db.iter_segment_fit_summaries(game_id, kind=kind))
    if not summaries:
        if parsed.json_output:
            print("[]")
        else:
            print(f"no fits found for game {game_id!r} kind={kind!r}")
        return 0

    if parsed.json_output:
        # The payload field is already a dict — JSON-dump everything.
        print(json.dumps(summaries, indent=2, sort_keys=False, default=str))
        return 0

    print(_LIST_HEADER)
    for summary in summaries:
        print(format_fit_summary_row(summary))
    return 0
```

- [ ] **Step 3.5: Register the `fit` parent in `cli.py`**

Open `python/spinlab/cli.py`. After the existing `fit-pool` registration (the block at lines ~176-179: `from spinlab import cli_fit_pool ; cli_fit_pool.add_subparser(sub)`), add:

```python
    # Register the segments-v07 fit inspector (Phase 2a). Read-only over
    # segment_fits; intentionally does not import segments_model so it
    # stays usable without the [fits] extra.
    from spinlab import cli_fit
    cli_fit.add_subparser(sub)
```

Then, at the end of the `main()` function (after the `elif parsed.command == "fit-pool":` block), add:

```python
    elif parsed.command == "fit":
        from spinlab import cli_fit
        if parsed.fit_command == "show":
            sys.exit(cli_fit.run_show(parsed))
        elif parsed.fit_command == "list":
            sys.exit(cli_fit.run_list(parsed))
```

- [ ] **Step 3.6: Run the unit tests to verify they pass**

Run: `pytest tests/unit/test_cli_fit.py -v`
Expected: all 7 tests pass.

- [ ] **Step 3.7: Run the full fast suite for regressions**

Run: `pytest -m "not emulator" -q 2>&1 | tail -10`
Expected: green; baseline + 7 new tests.

- [ ] **Step 3.8: Commit Task 3**

```bash
git add python/spinlab/cli_fit.py python/spinlab/cli.py tests/unit/test_cli_fit.py
git commit -m "segments-v07 phase 2a: spinlab fit show / fit list subcommands

\`spinlab fit show <segment_id>\` pretty-prints the latest v1 envelope
or, with --history N, one line per recent fit. \`spinlab fit list
--game G\` renders a tab-separated table of all segments with fits.
Both support --json for piping. The fit inspector is JAX-free; it only
reads rows the silent pipeline already wrote."
```

---

## Task 4 — Subprocess smoke test

**Files:**
- Create: `tests/integration/test_cli_fit_subprocess.py`

A single end-to-end subprocess test confirming that the CLI strings (argparse parses correctly, the module is importable from the installed entry point, exit codes propagate). Unit tests in Task 3 cover the runner behavior; this test catches packaging-level regressions.

- [ ] **Step 4.1: Write the failing test**

Create `tests/integration/test_cli_fit_subprocess.py`:

```python
"""End-to-end smoke test: `python -m spinlab fit ...` exits cleanly."""
from __future__ import annotations

import subprocess
import sys

import pytest

from spinlab.db import Database


def _seed(tmp_path):
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(
        f"data_dir: {tmp_path}\n"
        f"network:\n  port: 15400\n  dashboard_port: 15401\n"
    )
    db = Database(tmp_path / "spinlab.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, ordinal, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', 1, "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    payload = {
        "schema": "segments-v1", "kind": "segment_fit",
        "segment_id": "s1", "n_attempts": 10, "model": "haz1",
        "wall_time_s": 0.05,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False, "fittable": True,
        },
        "result": {
            "map": {"log_theta": [9.9] + [0.0] * 9, "natural": {}},
            "bands": {},
            "derived": {
                "M_clear": {"median_ms": 25000, "p5_ms": 22000, "p95_ms": 28000},
                "death_rate_next": 0.18,
            },
            "ppc": {},
        },
        "caveats": [],
    }
    db.save_segment_fit("s1", "segment_fit", payload)
    db.conn.commit()
    return cfg


def test_fit_show_subprocess_round_trip(tmp_path):
    cfg = _seed(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "spinlab", "fit", "show", "s1",
         "--config", str(cfg)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "s1" in result.stdout
    assert "M_clear median: 25000 ms" in result.stdout


def test_fit_list_subprocess_round_trip(tmp_path):
    cfg = _seed(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "spinlab", "fit", "list",
         "--game", "g1", "--config", str(cfg)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "segment_id" in result.stdout  # header
    assert "s1" in result.stdout
```

- [ ] **Step 4.2: Run the test**

Run: `pytest tests/integration/test_cli_fit_subprocess.py -v`
Expected: 2 tests pass. Wall time should be 2-4s per test (subprocess Python startup, no JAX).

- [ ] **Step 4.3: Manual smoke**

In a terminal:

```powershell
spinlab fit show <some-real-segment-id> --config <your-config>.yaml
spinlab fit list --game <your-game-id> --config <your-config>.yaml
spinlab fit show <some-real-segment-id> --history 5 --config <your-config>.yaml
spinlab fit show <some-real-segment-id> --json --config <your-config>.yaml | python -m json.tool | head -30
```

Expected:
- `show` prints a pretty multi-section block with non-zero M_clear if the segment has converged fits, or "no fit found ..." with exit 1 if it doesn't.
- `list` prints a header + one row per segment with fits.
- `--history 5` prints up to 5 single-line summaries.
- `--json` prints valid JSON that `json.tool` re-pretty-prints cleanly.

If any command crashes, STOP and capture the traceback. The unit tests don't exercise the AppConfig path or the real DB schema, so a packaging-level mismatch will show up here first.

- [ ] **Step 4.4: Commit Task 4**

```bash
git add tests/integration/test_cli_fit_subprocess.py
git commit -m "segments-v07 phase 2a: subprocess smoke test for \`spinlab fit\`

One end-to-end \`python -m spinlab fit show\` + \`fit list\` test.
Unit tests in test_cli_fit.py cover runner behavior; this test catches
packaging-level regressions (argparse wiring, entry-point routing,
config-path resolution)."
```

---

## Task 5 — Full verification + plan/memory updates

- [ ] **Step 5.1: Full pytest baseline**

Run: `python -m pytest 2>&1 | tail -20`

Expected: green. New tests: 3 (DB summary) + 7 (formatter) + 7 (CLI in-process) + 2 (subprocess) = 19 net new.

Per [[feedback_run_all_tests]] and [[feedback_fix_preexisting_failures]], the **full** suite (no `-m "not emulator"`, no `-k` filter) must be green. Skips count as failures (see [[feedback_run_emulator_tests]]).

If the baseline was already red at session start, STOP — fix the existing failures as the first commit of this session, or get explicit deferral sign-off. Do not silently move on.

- [ ] **Step 5.2: Type check**

Run: `npx pyright python/`

Expected: no NEW errors over the established baseline (~265 pre-existing, per [[project_test_reliability_known_issues]] and [[project_segments_v07_known_issues]]). The `_segments_v07/` shim contributes the vendored noise; this phase shouldn't add to it.

- [ ] **Step 5.3: Lint**

Run: `ruff check python/spinlab/cli_fit.py python/spinlab/fit_inspector.py python/spinlab/db/segment_fits.py`

Expected: clean. Any unused imports, dead branches, or style hits get fixed here before commit.

- [ ] **Step 5.4: Frontend type check + tests (smoke that nothing reaches frontend)**

Run: `cd frontend && npm run typecheck && npm test`
Expected: both green. Phase 2a is silent on the frontend — no changes — so this should be unaffected; running it confirms.

- [ ] **Step 5.5: Stress-run the full suite**

Run: `for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do echo "--- run $i ---"; python -m pytest -q --tb=no 2>&1 | tail -3; done`

Expected: 15 consecutive green runs (per [[feedback_stress_test_flakes]] — one green run is statistical noise). If any run flakes, identify whether it's a known pre-existing flake or new; new flakes block shipping.

- [ ] **Step 5.6: Real-data exercise**

Run against the live SpinLab DB (the one the dashboard uses, not a tmp_path):

```powershell
spinlab fit list --game <real-game-id>
```

Pick 5-10 segments showing varied state (high N, low N, fittable=N, ppc=Y, band=nuts). For each:

```powershell
spinlab fit show <segment-id>
spinlab fit show <segment-id> --history 10
```

Note in a scratch file what looks honest, surprising, or wrong. This is the evidence-gathering pass Phase 2 is for — the spec's three Phase 2 questions (decision rubric for Phase 3 UI) get their answers from this exercise.

Things to confirm:
- `M_clear.median_ms` is plausible vs the segment's actual practice times.
- `death_rate_next` is plausible vs how often deaths happen.
- `caveats` fire on the segments you expect (low-N, NUTS-fallback).
- The `(suppressed)` band on `log_hl_ssp` shows up under pool fits (run `spinlab fit show <id> --kind pool_fit` to check).

If output is unhelpfully formatted in real terminals (column alignment, line lengths), capture the specifics and feed them into Phase 2b's renderer spec — don't fix in 2a unless it's actively misleading.

- [ ] **Step 5.7: Update plan status + memory**

Open this plan file (`docs/superpowers/plans/2026-05-19-segments-v07-phase2a-fit-inspector-cli.md`) and change `status: drafted` to `status: shipped` with a `shipped_at: <YYYY-MM-DD>` field and a `shipped_commits: <range>` field.

Open `C:/Users/thedo/.claude/projects/C--Users-thedo-git-spinlab/memory/project_segments_v07_integration.md` and update the State block: append a "**Phase 2a shipped <YYYY-MM-DD>** in commits ..." bullet; update the "**Phase 2 next**" line to "**Phase 2b next:** static HTML renderer over the same segment_fits rows".

- [ ] **Step 5.8: Final commit**

```bash
git add docs/superpowers/plans/2026-05-19-segments-v07-phase2a-fit-inspector-cli.md \
        "C:/Users/thedo/.claude/projects/C--Users-thedo-git-spinlab/memory/project_segments_v07_integration.md"
git commit -m "docs: segments-v07 phase 2a shipped — fit inspector CLI live

Plan file marked shipped; project memory updated. Phase 2b (static HTML
renderer over the same segment_fits rows that 2a inspects via CLI) is
now unblocked."
```

---

## Risks and known gaps

- **`AppConfig.from_yaml` schema drift.** The CLI tests construct a minimal YAML (`data_dir`, `network.port`, `network.dashboard_port`). If `AppConfig` requires additional fields the dashboard sets but `fit show` doesn't care about, the in-process tests will fail at config-load time. Fix: read `python/spinlab/config.py`'s required-field list and pad the test YAML. The existing `cli_fit_pool.py` follows the same load path, so if it works in `tests/integration/test_fit_pool_cli.py`, this should too — read that test for the canonical seed pattern if Task 3 step 3.6 fails.

- **`segments.ordinal` may not be set in the test fixtures.** The `iter_segment_fit_summaries` query orders by `s.ordinal ASC, s.level_number ASC, s.id ASC`. If the test fixtures don't set `ordinal`, all rows get ordinal=0 (the column default) and ordering falls through to `level_number`. That's intentional — the order should be reproducible whether ordinal is set or not. Confirm by reading the test rows back in the order you wrote them.

- **`json.dumps` on a `payload` containing non-JSON-native values.** The `payload` field returned by `iter_segment_fit_summaries` is already a `dict` parsed from `payload_json`, so by construction it is JSON-native. The `default=str` argument in `run_list` is defensive belt-and-suspenders for any future field that sneaks in (e.g., a `Path` object); remove only if pyright forces it.

- **`spinlab fit list` over thousands of segments.** Current SpinLab DBs have ≤200 segments per game. The `iter_segment_fit_summaries` query is one indexed scan, fast at this scale. If a future game grows past O(10k), consider adding pagination — but YAGNI for Phase 2a.

- **History mode reads payload_json twice per row** if a future refactor changes `iter_recent_segment_fits` to return `fitted_at` too. Right now `run_show` issues a raw `SELECT payload_json, fitted_at` to get both. Acceptable redundancy; collapsing into one helper that always returns both is a Phase 2b cleanup if needed.

## Phase 2a done = Phase 2b unblocked

Phase 2b adds a static HTML renderer that takes the same v1 envelope and produces learning-curve + band plots. The CLI from 2a is what Andrew uses to surface segments worth rendering (`fit list` → pick interesting ids → render to HTML). Phase 3's UI shape decision is informed by what 2a+2b together reveal — see the design spec's decision rubric.

## Verification gate before declaring done

Per [[feedback_run_all_tests]] + [[feedback_fix_preexisting_failures]]:

- [ ] `python -m pytest` green at session START (baseline)
- [ ] `python -m pytest` green at session END
- [ ] No new skips. Skips count as failures.
- [ ] Pre-existing failures (if baseline was already red) acknowledged
      and either fixed first or written into the followup queue with
      explicit deferral sign-off.
- [ ] 15× stress run all green.
- [ ] Real-data exercise (step 5.6) produced notes — Phase 2b spec will
      reference them.
