---
date: 2026-05-19
status: shipped
shipped_at: 2026-05-19
shipped_commits: 5fa6799..b324c51
focus: "Phase 1 of segments-v07 integration — vendor the prototype + silent fit pipeline (refit per attempt, segment_fits table, JAX prewarm, manual pool CLI)."
spec: docs/superpowers/specs/2026-05-18-segments-v07-integration-design.md
predecessor: docs/superpowers/plans/2026-05-18-segments-v07-phase0-event-level-attempts.md (shipped 36c5536)
scope: phase-1 only; no UI (Phase 2), no allocator integration (Phase 3)
---

## Shipped 2026-05-19 in commits `5fa6799..b324c51`

Phase 1 executed in worktree `worktree-agent-a5e5afbf119866651` over five
implementation tasks plus a verification pass. Delivered:

- `5fa6799` — Task 1: vendored `segments_experiment/` under
  `python/spinlab/_segments_v07/` with sys.path shim; clean re-export
  facade at `python/spinlab/segments_model/`; `[fits]` optional extra
  (jax 0.10.0, jaxlib 0.10.0, jaxopt 0.8.5, numpyro 0.21.0);
  `tests/unit/segments_model/test_vendor_smoke.py`.
- `a2f4fc6` — Task 2: `segment_fits` table via migration
  `0003_segment_fits.sql`; `SegmentFitsMixin` with `save_segment_fit`,
  `load_latest_segment_fit`, `iter_recent_segment_fits`; status fields
  projected to columns for SQL-side filtering; `tests/unit/db/test_segment_fits.py`.
- `52b937f` — Task 3: silent refit on episode close —
  `Scheduler._maybe_refit_segment` runs `sv.refit_segment` after each
  closed episode using the previous fit as warm start; skips cleanly
  when `[fits]` is missing or attempt count below `_MIN_EVENTS_FOR_FIT=5`;
  `tests/unit/test_silent_fit_pipeline.py`.
- `a71bf39` — Task 4: JAX prewarm fires in a daemon thread on dashboard
  startup so first-byte latency stays unaffected; logs prewarm start /
  complete; tolerates missing `[fits]`; dashboard-boot test.
- `b324c51` — Task 5: `spinlab fit-pool` CLI runs an empirical-Bayes
  pool across active segments meeting `POOL_MIN_EVENTS=5`; persists
  one `pool_fit` row per segment; integration test exercises the
  end-to-end CLI.

Verification (Task 6, partial — in-worktree scope only):
- 930 fast tests green; 15 consecutive stress runs all green.
- Frontend typecheck + 65 frontend tests green.
- Pyright: 4 new errors over the baseline, all `reportAttributeAccessIssue`
  variants caused by pyright not being able to statically trace the
  `_segments_v07/__init__.py` `sys.path` shim. Numerics-preserving by
  design; tracked under the existing "pyright cleanup" backlog.
- Full unfiltered `python -m pytest` (including emulator) and manual
  dashboard exercise with RA deferred to the parent session post-merge
  (the worktree can't launch RA).

Phase 2 (CLI inspector + static HTML renderer) is now unblocked. The
body below is preserved as the design that landed.

---

# Segments-v07 Phase 1 — Silent Fit Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `segments_experiment/` into SpinLab as the silent
background model. After every persisted attempt, a refit runs and the
v1 JSON payload is stored to a new `segment_fits` table. No UI, no
allocator change — `M_clear`, bands, PPC are written but not yet read.

**Architecture:**

```
PracticeTiming (Phase 0)
        │
        ▼
PracticeSession.receive_event_attempt
        │
        ▼
db.log_event_attempt   ──►  Scheduler.update_state_after_episode
                                 │
                                 ├─► existing estimators (Kalman/ExpDecay/RollingMean)
                                 │
                                 └─► sv.refit_segment(attempts, prev_result=last)
                                         │
                                         ▼
                                  db.save_segment_fit(payload)

CLI: spinlab fit-pool  ──►  sv.fit_pool([{segment_id, attempts}, ...])  ──► db.save_segment_fit(kind='pool')

Dashboard boot ──► background-thread sv.prewarm_buckets()
```

**Tech Stack:** Vendored `segments_v07` (JAX + NumPyro + scipy + numpy);
SQLite migration; FastAPI startup hook; argparse CLI subcommand.

## Anchor questions resolved

- **OQ2 (vendor location)** — `python/spinlab/_segments_v07/`. Underscore
  prefix signals "vendored, treat as opaque" per the spec recommendation.
- **OQ3 (pool orchestration)** — manual `spinlab fit-pool` CLI for v1.
  Cron / APScheduler deferred to Phase 2 once we know how often we
  actually want to re-pool.
- **Bare imports inside prototype** — the prototype uses flat imports
  (`import fit_jax`, `import config`, etc.) rather than relative
  imports. **We do NOT rewrite them.** The vendor package's
  `__init__.py` prepends the vendor directory to `sys.path` before
  importing, preserving bit-identical numerics with the prototype's
  validation harness. Rewriting to relative imports is a "later if it
  bites" follow-up — not Phase 1's problem. (See [[feedback_iterate]].)
- **JAX install footprint** — JAX/NumPyro/jaxopt land as optional
  dependency `spinlab[fits]`. Fast tests and `spinlab dashboard` boot
  without them; only the new `spinlab fit-*` CLI commands and the
  background refit require them. `Scheduler.update_state_after_episode`
  uses a `try/except ImportError` gate so an install without `[fits]`
  silently skips fits and keeps existing estimators working.

## Anchor questions still open at end of Phase 1

- **Pool re-trigger cadence.** Manual CLI for now. Phase 2 picks
  cron-vs-on-startup based on how stale the pool actually gets.
- **What to do on a non-converged fit during streaming.** Plan: write
  the empty-envelope row (so the failure is observable) and skip the
  warm-start update. Re-evaluate after first real fits land.

## File structure

### New files

- `python/spinlab/_segments_v07/` — the vendored prototype tree. Created
  by `git mv segments_experiment/ python/spinlab/_segments_v07/` plus
  one new `__init__.py`.
- `python/spinlab/_segments_v07/__init__.py` — sys.path shim. The only
  hand-written file inside the vendor dir.
- `python/spinlab/segments_model/__init__.py` — thin re-export facade
  that gives the rest of SpinLab a clean import path: `from
  spinlab.segments_model import refit_segment, fit_segment, fit_pool,
  prewarm_buckets`. Hides the `_segments_v07` underscore name from
  application code.
- `python/spinlab/db/segment_fits.py` — new mixin for `save_fit`,
  `load_latest_fit`, `iter_recent_fits`.
- `python/spinlab/db/migrations/0003_segment_fits.sql` — new table.
- `python/spinlab/cli/fit_pool.py` — `spinlab fit-pool` subcommand.
- `tests/unit/segments_model/test_vendor_smoke.py` — JAX boots, basic
  `fit_segment` returns a v1-schema dict.
- `tests/unit/db/test_segment_fits.py` — save / load / latest round-trip.
- `tests/unit/test_silent_fit_pipeline.py` — synthetic event stream →
  `segment_fits` row appears after the closing event.
- `tests/integration/test_fit_pool_cli.py` — `spinlab fit-pool` end-to-end.

### Modified files

- `pyproject.toml` — add `[fits]` optional extra.
- `python/spinlab/scheduler.py` — `Scheduler.__init__` resolves an
  optional `segments_model` import; `update_state_after_episode` calls
  `refit_segment` after the existing estimator loop.
- `python/spinlab/cli/__init__.py` — register `fit-pool` subcommand.
- `python/spinlab/dashboard.py` (or whichever module owns `spinlab
  dashboard` startup — TBD per "Task 6") — fire `prewarm_buckets()` in a
  background thread.
- `scripts/bootstrap-sandbox.sh` — install `[fits]` extra in the
  sandbox venv so Linux CI / fresh clones get the dependencies.
- `.gitignore` — leave `segments_experiment/` deletion clean; ensure
  no stale entry.

### Files NOT touched

- `python/spinlab/db/attempts.py` — Phase 0 already wrote
  `get_segment_event_rows()` for the segments-model adapter.
- `python/spinlab/timing.py`, `practice.py`, `speed_run.py` — Phase 0
  closed these out.
- The frontend — Phase 1 is silent. No UI surface.

---

## Task 1 — Vendor the prototype tree

**Files:**
- Create: `python/spinlab/_segments_v07/__init__.py`
- Move:   `segments_experiment/*` → `python/spinlab/_segments_v07/*` via `git mv`
- Modify: `pyproject.toml` (add `[fits]` extra)
- Modify: `scripts/bootstrap-sandbox.sh` (install `[fits]`)
- Test:   `tests/unit/segments_model/test_vendor_smoke.py`

- [ ] **Step 1.1: Inspect the current prototype directory once more**

Run: `git status -s segments_experiment/ ; ls segments_experiment/ ; wc -l segments_experiment/*.py`

Confirm: the directory is currently untracked (it shows up as `??` in
`git status`). Capture the file list so we know what we're moving.

- [ ] **Step 1.2: Add the [fits] optional extra to pyproject.toml**

Open `pyproject.toml`. Add a new optional-dependencies entry, after
the existing `dev` entry:

```toml
[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "httpx", "requests", "playwright>=1.58", "pytest-playwright>=0.7"]
fits = [
    # Pin to the versions the prototype's validation harness was
    # frozen against (segments_experiment/requirements.txt). Newer
    # jaxlib versions can drift numerics enough to break the pinned
    # ~5 sig-fig JSON tolerances.
    "jax==0.10.0",
    "jaxlib==0.10.0",
    "jaxopt==0.8.5",
    "numpyro==0.21.0",
]
```

(Numpy/scipy are already in the base `dependencies` list — no double-pin.)

- [ ] **Step 1.3: Install the [fits] extra locally**

Run: `pip install -e ".[fits]"`
Expected: jax, jaxlib, jaxopt, numpyro install. ~30s on first install.

If install fails (Windows wheel mismatch is the usual culprit), STOP and
ask the user — Windows JAX install is its own adventure and we don't
want to silently ship a broken extra.

- [ ] **Step 1.4: Sanity-check the prototype's own test suite still runs**

Run: `cd segments_experiment && python -m pytest tests/ -x -q 2>&1 | tail -20`
Expected: ~80 tests pass in ~100s. The NUTS suite is slow (~70s of that).

If the prototype's own tests fail, the vendored copy will too — STOP and
investigate the install/env before moving the directory.

- [ ] **Step 1.5: Move the prototype into the package**

```bash
git mv segments_experiment python/spinlab/_segments_v07
```

(One command; git tracks the rename so blame stays intact even though
it's currently untracked — `git mv` on an untracked dir is equivalent
to `mv` followed by `git add`, but explicit.)

Verify the move: `ls python/spinlab/_segments_v07/`. The 30+ `.py`
files and `tests/`, `external_docs/`, `validation/`, `V1_ESSENCE.md`,
`README.md` should all be there.

- [ ] **Step 1.6: Write the sys.path shim**

Create `python/spinlab/_segments_v07/__init__.py`:

```python
"""Vendored ``segments_experiment`` (V07 segments model).

Lives under an underscore-prefixed name so the rest of SpinLab treats it
as opaque. The clean re-export surface is `spinlab.segments_model`.

The prototype was written with flat imports (`import fit_jax`,
`import learning_model_v07 as lm_np`, etc.) rather than relative
imports. Rather than rewrite those — the prototype's validation harness
pins numerics to ~5 sig figs and a careless rewrite could drift JIT
compilation order — we prepend this directory to ``sys.path`` so the
flat imports resolve here. Cost: a (~30) module-name pollution risk.
Benefit: zero-touch on the validation harness, easy to refresh from
the prototype's upstream.

If a flat name ever collides with an external package SpinLab pulls in,
the fix is the relative-import rewrite, not deeper magic here.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_VENDOR_DIR = str(_Path(__file__).resolve().parent)
if _VENDOR_DIR not in _sys.path:
    # Prepend rather than append: in the unlikely event a flat name
    # collides with another package, the vendored copy wins for any
    # caller that goes through this module first.
    _sys.path.insert(0, _VENDOR_DIR)

# Trigger the public surface eagerly so any import-time errors surface
# at `import spinlab._segments_v07` rather than later. `segments_v07`
# re-exports the v1 contract helpers via `from api import ...`.
import segments_v07 as _sv  # noqa: E402,F401  (sys.path setup must happen first)
```

- [ ] **Step 1.7: Write the clean re-export facade**

Create `python/spinlab/segments_model/__init__.py`:

```python
"""Public SpinLab surface for the V07 segments model.

Re-exports the v1 contract helpers from the vendored
``_segments_v07`` package so application code can import a stable
clean name without the underscore prefix.

The reason for the indirection: the vendored tree uses flat module
names (`fit_jax`, `learning_model_v07`, `config`, ...) that we don't
want bleeding into autocomplete or grep hits in normal SpinLab work.
Code outside of `_segments_v07/` MUST import from here, not from the
vendored tree directly.
"""
from __future__ import annotations

# The vendor package's __init__ sets sys.path so the flat imports
# inside it resolve. Importing it as a side effect is what makes
# `segments_v07` available.
from spinlab._segments_v07 import _sv as _vendor  # noqa: F401

# The clean re-export surface. These are what the rest of SpinLab uses.
from segments_v07 import (
    SCHEMA,
    fit_segment,
    refit_segment,
    fit_pool,
    prewarm_buckets,
)

__all__ = [
    "SCHEMA",
    "fit_segment",
    "refit_segment",
    "fit_pool",
    "prewarm_buckets",
]
```

- [ ] **Step 1.8: Write the vendor smoke test**

Create `tests/unit/segments_model/__init__.py` (empty) and
`tests/unit/segments_model/test_vendor_smoke.py`:

```python
"""Smoke tests for the vendored segments_v07 package.

These do NOT exercise the math — that's the prototype's own validation
harness's job (which we still run via tox / the migration verification
step). They prove the *import path* into SpinLab works and that a
trivial fit returns a v1-schema dict.
"""
from __future__ import annotations

import pytest

pytest.importorskip("jax")          # skip cleanly when [fits] not installed
pytest.importorskip("numpyro")


def test_import_clean_surface():
    """The public re-export gives us the v1 contract helpers."""
    from spinlab.segments_model import (
        SCHEMA, fit_segment, refit_segment, fit_pool, prewarm_buckets,
    )
    assert SCHEMA == "segments-v1"
    assert callable(fit_segment)
    assert callable(refit_segment)
    assert callable(fit_pool)
    assert callable(prewarm_buckets)


def test_fit_segment_minimal_returns_v1_envelope():
    """A trivial 30-attempt sequence produces a well-formed v1 payload."""
    from spinlab.segments_model import fit_segment

    # 24 survives @ 20000ms, 6 deaths @ 8000ms — enough to exit `low_n`.
    attempts = (
        [{"outcome": "survived", "time_ms": 20000}] * 24
        + [{"outcome": "died", "time_ms": 8000}] * 6
    )
    payload = fit_segment(attempts, segment_id="smoke")

    assert payload["schema"] == "segments-v1"
    assert payload["kind"] == "segment_fit"
    assert payload["segment_id"] == "smoke"
    assert payload["n_attempts"] == 30
    assert payload["model"] == "haz1"
    assert "status" in payload and "converged" in payload["status"]
    assert "result" in payload
```

- [ ] **Step 1.9: Run the smoke test**

Run: `pytest tests/unit/segments_model/ -v`
Expected: 2 tests pass (or skipped if `[fits]` extra isn't installed in
this environment — the importorskip handles that). If installed, pass.

- [ ] **Step 1.10: Run the full SpinLab fast suite to confirm zero regressions**

Run: `pytest -m "not emulator" -q 2>&1 | tail -10`
Expected: same green count as the baseline (or the baseline + 2 new
tests; same green outcome). NO new failures.

- [ ] **Step 1.11: Update scripts/bootstrap-sandbox.sh to install [fits]**

Open `scripts/bootstrap-sandbox.sh`. Find the `pip install -e ".[dev]"`
line. Change to:

```bash
pip install -e ".[dev,fits]"
```

This ensures fresh sandbox bootstraps get JAX without a separate step.

- [ ] **Step 1.12: Commit Task 1**

```bash
git add python/spinlab/_segments_v07/ python/spinlab/segments_model/ \
        tests/unit/segments_model/ pyproject.toml scripts/bootstrap-sandbox.sh
git commit -m "segments-v07 phase 1: vendor segments_experiment into spinlab._segments_v07

Vendors the V07 prototype tree under an underscore-prefixed module name
and exposes the v1 contract helpers via spinlab.segments_model. The
vendor __init__ prepends the vendor dir to sys.path so the prototype's
flat imports keep working without a numerics-disturbing rewrite. JAX
and NumPyro land as the [fits] optional extra; fast tests skip cleanly
when not installed."
```

---

## Task 2 — segment_fits table + DB mixin

**Files:**
- Create: `python/spinlab/db/migrations/0003_segment_fits.sql`
- Create: `python/spinlab/db/segment_fits.py`
- Modify: `python/spinlab/db/__init__.py` (register the new mixin)
- Test:   `tests/unit/db/test_segment_fits.py`

- [ ] **Step 2.1: Write the failing DB tests first**

Create `tests/unit/db/test_segment_fits.py`:

```python
"""Round-trip tests for the segment_fits table + Database helpers."""
from __future__ import annotations

import json

import pytest

from spinlab.db import Database


@pytest.fixture()
def db(tmp_path):
    db = Database(tmp_path / "test.db")
    # Seed a game + segment so foreign keys hold.
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()
    return db


def _make_payload(segment_id="s1", n_attempts=42, fittable=True):
    return {
        "schema": "segments-v1",
        "kind": "segment_fit",
        "segment_id": segment_id,
        "n_attempts": n_attempts,
        "model": "haz1",
        "wall_time_s": 0.015,
        "status": {
            "converged": True, "band_source": "laplace",
            "laplace_pd": True, "ppc_tension": False,
            "fittable": fittable,
        },
        "result": {"map": {"log_theta": [0.0] * 10, "natural": {}},
                   "bands": {}, "derived": {}, "ppc": {}},
        "caveats": [] if fittable else ["unconverged"],
    }


def test_save_then_load_latest_returns_same_payload(db):
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=10))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=11))
    latest = db.load_latest_segment_fit("s1", "segment_fit")
    assert latest is not None
    assert latest["n_attempts"] == 11  # Newer write wins.


def test_load_latest_returns_none_for_missing_segment(db):
    assert db.load_latest_segment_fit("nope", "segment_fit") is None


def test_pool_kind_is_keyed_separately_from_segment_fit(db):
    seg_payload = _make_payload(n_attempts=10)
    pool_payload = {**_make_payload(n_attempts=999), "kind": "pool_fit"}
    db.save_segment_fit("s1", "segment_fit", seg_payload)
    db.save_segment_fit("s1", "pool_fit", pool_payload)
    assert db.load_latest_segment_fit("s1", "segment_fit")["n_attempts"] == 10
    assert db.load_latest_segment_fit("s1", "pool_fit")["n_attempts"] == 999


def test_iter_recent_fits_orders_newest_first(db):
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=1))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=2))
    db.save_segment_fit("s1", "segment_fit", _make_payload(n_attempts=3))
    rows = list(db.iter_recent_segment_fits("s1", limit=2))
    assert [r["n_attempts"] for r in rows] == [3, 2]


def test_save_fit_records_extracted_status_fields(db):
    """The mixin pulls status flags into columns so the inspector can
    filter without parsing JSON on every row."""
    db.save_segment_fit("s1", "segment_fit", _make_payload(fittable=False))
    row = db.conn.execute(
        "SELECT fittable, ppc_tension, band_source FROM segment_fits "
        "WHERE segment_id = 's1'"
    ).fetchone()
    assert row["fittable"] == 0
    assert row["ppc_tension"] == 0
    assert row["band_source"] == "laplace"
```

- [ ] **Step 2.2: Run the tests to verify they fail**

Run: `pytest tests/unit/db/test_segment_fits.py -v`
Expected: FAIL — `Database` has no `save_segment_fit` etc.

- [ ] **Step 2.3: Write migration 0003**

Create `python/spinlab/db/migrations/0003_segment_fits.sql`:

```sql
-- segments-v07 Phase 1: persistent storage for v1 JSON fit payloads.
--
-- Each row is one fit. (segment_id, kind, fitted_at) is the natural
-- key, but we keep an INTEGER PRIMARY KEY for cheap "most recent"
-- lookups by id. `payload_json` is the full v1 envelope; status
-- columns are projected out for SQL-side filtering (the inspector
-- wants "show me unfittable segments" without parsing every blob).

CREATE TABLE IF NOT EXISTS segment_fits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL REFERENCES segments(id),
  kind TEXT NOT NULL CHECK (kind IN ('segment_fit', 'pool_fit')),
  n_attempts INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  -- Status columns projected from the JSON envelope. NULLABLE because
  -- a non-converged envelope omits most of these (`band_source='none'`
  -- but `fittable` etc. still come through; defensive null tolerance
  -- avoids future-payload-shape lockout).
  band_source TEXT,
  fittable INTEGER,
  ppc_tension INTEGER,
  wall_time_ms INTEGER,  -- payload's wall_time_s * 1000, for SLO tracking
  fitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segment_fits_segment_kind_id
  ON segment_fits(segment_id, kind, id DESC);
```

- [ ] **Step 2.4: Write the mixin**

Create `python/spinlab/db/segment_fits.py`:

```python
"""Storage for the segments-v07 silent fit pipeline.

One row per fit. The full v1 envelope lives in `payload_json`; a few
status fields are projected out as columns for SQL-side filtering by
the upcoming inspector / pool CLI.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Literal

FitKind = Literal["segment_fit", "pool_fit"]


def _utc_now_iso() -> str:
    """ISO-8601 UTC. SQLite stores TEXT timestamps; consistent format
    keeps ORDER BY lexicographically correct."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SegmentFitsMixin:
    """save_/load_/iter_ helpers for the segment_fits table."""
    conn: sqlite3.Connection

    def save_segment_fit(
        self, segment_id: str, kind: FitKind, payload: dict[str, Any],
    ) -> int:
        """Persist a v1 fit envelope. Returns the rowid.

        We project a handful of status columns out of the envelope so
        the SQL layer can answer questions like "which segments fail
        PPC?" without scanning every blob. The JSON payload is the
        source of truth — column drift would be a bug.
        """
        status = payload.get("status", {})
        cur = self.conn.execute(
            """INSERT INTO segment_fits
               (segment_id, kind, n_attempts, payload_json,
                band_source, fittable, ppc_tension, wall_time_ms, fitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                segment_id, kind, int(payload.get("n_attempts", 0)),
                json.dumps(payload),
                status.get("band_source"),
                int(status["fittable"]) if "fittable" in status else None,
                int(status["ppc_tension"]) if "ppc_tension" in status else None,
                int(float(payload.get("wall_time_s", 0)) * 1000),
                _utc_now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def load_latest_segment_fit(
        self, segment_id: str, kind: FitKind,
    ) -> dict[str, Any] | None:
        """Most recent fit of ``kind`` for ``segment_id``, or None.

        The refit-per-attempt warm-start path calls this every event to
        get the previous payload for ``prev_result=``. Indexed lookup
        on (segment_id, kind, id DESC); should be ~µs even at scale.
        """
        row = self.conn.execute(
            """SELECT payload_json FROM segment_fits
               WHERE segment_id = ? AND kind = ?
               ORDER BY id DESC LIMIT 1""",
            (segment_id, kind),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def iter_recent_segment_fits(
        self, segment_id: str, *, limit: int = 50,
        kind: FitKind = "segment_fit",
    ) -> Iterator[dict[str, Any]]:
        """Iterate recent fits newest-first. Used by the inspector (Phase 2)."""
        rows = self.conn.execute(
            """SELECT payload_json FROM segment_fits
               WHERE segment_id = ? AND kind = ?
               ORDER BY id DESC LIMIT ?""",
            (segment_id, kind, int(limit)),
        ).fetchall()
        for row in rows:
            yield json.loads(row["payload_json"])
```

- [ ] **Step 2.5: Register the mixin on Database**

Open `python/spinlab/db/__init__.py`. Find the `class Database(...)` MRO
(it composes the existing mixins like `AttemptsMixin`, `SegmentsMixin`,
etc.). Add `SegmentFitsMixin` to the inheritance list. Example diff:

```python
from .segment_fits import SegmentFitsMixin

class Database(
    AttemptsMixin,
    SegmentsMixin,
    SessionsMixin,
    SegmentFitsMixin,      # NEW
    ...
):
    ...
```

(Exact MRO order matches the existing file — read it first; this snippet
illustrates only.)

- [ ] **Step 2.6: Run the tests to verify they pass**

Run: `pytest tests/unit/db/test_segment_fits.py -v`
Expected: 5 tests pass.

- [ ] **Step 2.7: Verify the migration applies on a fresh DB**

Run: `python -c "from spinlab.db import Database; from pathlib import Path; import tempfile; p = Path(tempfile.mkdtemp())/'t.db'; db = Database(p); rows = db.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='segment_fits'\").fetchall(); print('OK' if rows else 'MISSING')"`
Expected: prints `OK`.

- [ ] **Step 2.8: Run the full fast suite for regressions**

Run: `pytest -m "not emulator" -q 2>&1 | tail -10`
Expected: green; baseline count + 5 new tests.

- [ ] **Step 2.9: Commit Task 2**

```bash
git add python/spinlab/db/segment_fits.py \
        python/spinlab/db/migrations/0003_segment_fits.sql \
        python/spinlab/db/__init__.py tests/unit/db/test_segment_fits.py
git commit -m "segments-v07 phase 1: add segment_fits table + Database mixin

One row per v1 fit envelope; status flags projected out for SQL-side
filtering. The refit-per-attempt path will use load_latest_segment_fit
for warm-start chaining; the upcoming spinlab fit-pool CLI writes
kind='pool_fit' rows."
```

---

## Task 3 — Silent refit-per-attempt wiring in Scheduler

**Files:**
- Modify: `python/spinlab/scheduler.py`
- Test:   `tests/unit/test_silent_fit_pipeline.py`

- [ ] **Step 3.1: Write the failing pipeline test**

Create `tests/unit/test_silent_fit_pipeline.py`:

```python
"""End-to-end (in-process) test: events → DB → scheduler → segment_fits row."""
from __future__ import annotations

import pytest

pytest.importorskip("jax")
pytest.importorskip("numpyro")

from spinlab.db import Database
from spinlab.models import Attempt
from spinlab.scheduler import Scheduler


@pytest.fixture()
def seeded_db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) "
        "VALUES ('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO segments (id, game_id, level_number, "
        "start_type, end_type, created_at, updated_at) "
        "VALUES ('s1', 'g1', 1, 'entrance', 'exit', "
        "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"
    )
    db.conn.commit()
    return db


def test_episode_close_writes_segment_fit(seeded_db):
    """After one closed episode, a segment_fits row exists for that segment."""
    sched = Scheduler(seeded_db, game_id="g1")
    for i in range(30):
        sched.process_attempt(
            segment_id="s1", time_ms=20000, completed=True,
        )
    fit = seeded_db.load_latest_segment_fit("s1", "segment_fit")
    assert fit is not None
    assert fit["schema"] == "segments-v1"
    assert fit["n_attempts"] == 30


def test_subsequent_episode_warm_starts_from_previous(seeded_db, monkeypatch):
    """The second close should pass prev_result= so refit_segment runs
    the warm-start path."""
    from spinlab import scheduler as sched_mod

    calls: list[dict] = []
    real_refit = sched_mod._refit_segment

    def spy_refit(attempts, *, segment_id, prev_result=None):
        calls.append({"n": len(attempts), "warm": prev_result is not None})
        return real_refit(attempts, segment_id=segment_id, prev_result=prev_result)

    monkeypatch.setattr(sched_mod, "_refit_segment", spy_refit)

    sched = Scheduler(seeded_db, game_id="g1")
    for _ in range(2):
        sched.process_attempt(
            segment_id="s1", time_ms=20000, completed=True,
        )

    # First call: cold (no prev). Second: warm (prev present).
    assert [c["warm"] for c in calls] == [False, True]


def test_silent_fit_skipped_cleanly_without_extras(seeded_db, monkeypatch):
    """If the [fits] extra is not installed, the scheduler must NOT crash
    on episode close; existing estimators stay green and no segment_fits
    row is written."""
    from spinlab import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_refit_segment", None)

    sched = Scheduler(seeded_db, game_id="g1")
    sched.process_attempt(
        segment_id="s1", time_ms=20000, completed=True,
    )
    assert seeded_db.load_latest_segment_fit("s1", "segment_fit") is None
```

- [ ] **Step 3.2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_silent_fit_pipeline.py -v`
Expected: FAIL — `Scheduler` doesn't call `refit_segment`.

- [ ] **Step 3.3: Add the optional segments_model import + module-level handle in scheduler.py**

Open `python/spinlab/scheduler.py`. After the existing imports (around
the `if TYPE_CHECKING:` block), add:

```python
# Optional segments-v07 silent fit pipeline. The [fits] extra brings
# JAX/NumPyro; without them the import fails and the scheduler skips the
# fit path entirely. This is patched in tests via
# monkeypatch.setattr(scheduler, "_refit_segment", None).
try:
    from spinlab.segments_model import refit_segment as _refit_segment
except ImportError:  # pragma: no cover — only fires without [fits]
    _refit_segment = None  # type: ignore[assignment]

# Minimum number of attempt events before we bother trying a fit. The
# prototype's fit_segment can handle n=1 but produces wide-bands /
# unconverged envelopes that aren't useful and just burn CPU. Matches
# the V1_ESSENCE low_n threshold for "fit results not yet meaningful."
_MIN_EVENTS_FOR_FIT = 5
```

- [ ] **Step 3.4: Wire the refit call into update_state_after_episode**

In `update_state_after_episode`, AFTER the existing estimator loop
(after the `for est in [get_estimator(n) for n in list_estimators()]:`
block), add:

```python
        # Silent V07 fit. Off the request path but inline (~15ms p50
        # per V1_ESSENCE, well within an end-of-episode budget). Skips
        # cleanly when [fits] isn't installed or the segment has too
        # few events to fit meaningfully.
        self._maybe_refit_segment(segment_id)
```

Then add the helper method at the end of the class:

```python
    def _maybe_refit_segment(self, segment_id: str) -> None:
        """Run a streaming v07 refit for ``segment_id`` and persist the payload.

        Reads event-level rows directly (bypassing the episode roll-up)
        because the prototype consumes the raw (outcome, time_ms)
        sequence. Uses the most recent segment_fits row as the warm
        start; falls back to a cold fit on the first call.
        """
        if _refit_segment is None:
            return
        events = self.db.get_segment_event_rows(segment_id)
        if len(events) < _MIN_EVENTS_FOR_FIT:
            return
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in events
            if not int(e["invalidated"])
        ]
        if len(attempts) < _MIN_EVENTS_FOR_FIT:
            return
        prev = self.db.load_latest_segment_fit(segment_id, "segment_fit")
        try:
            payload = _refit_segment(
                attempts, segment_id=segment_id, prev_result=prev,
            )
        except Exception:
            logger.exception(
                "segments-v07 refit failed for segment=%s", segment_id,
            )
            return
        self.db.save_segment_fit(segment_id, "segment_fit", payload)
```

- [ ] **Step 3.5: Run the tests to verify they pass**

Run: `pytest tests/unit/test_silent_fit_pipeline.py -v`
Expected: 3 tests pass.

If the warm-start spy test fails because `monkeypatch.setattr` can't
find `_refit_segment`, double-check that it's a module-level name in
`scheduler.py` (not nested inside the class).

- [ ] **Step 3.6: Run the full fast suite for regressions**

Run: `pytest -m "not emulator" -q 2>&1 | tail -10`
Expected: green; baseline + new tests.

Note: this is the first time per-attempt fits run during the test
suite. If suite wall-clock blows up (>2x baseline), the
`_MIN_EVENTS_FOR_FIT=5` gate isn't filtering enough — investigate
which tests are calling `process_attempt` in tight loops and either
patch `_refit_segment` to a no-op for those tests or raise the gate.

- [ ] **Step 3.7: Commit Task 3**

```bash
git add python/spinlab/scheduler.py tests/unit/test_silent_fit_pipeline.py
git commit -m "segments-v07 phase 1: silent refit on episode close

After each episode close the scheduler runs a streaming
sv.refit_segment using the previous segment_fits row as warm start.
Failures (or absent [fits] extra) silently skip — existing estimators
continue to drive the live model unchanged."
```

---

## Task 4 — JAX prewarm hook on dashboard boot

**Files:**
- Modify: the FastAPI startup module (TBD: usually `python/spinlab/app.py`
  or `python/spinlab/dashboard.py`)
- Test:   reuse existing dashboard-smoke tests; add a unit test asserting
  the prewarm thread is spawned.

- [ ] **Step 4.1: Locate the dashboard startup**

Run: `grep -rn "on_startup\|lifespan\|@app.on_event" python/spinlab/`
Expected: one or two hits — the file that owns FastAPI lifespan / startup
hooks. (At time of plan writing, the most likely location is
`python/spinlab/app.py` or `python/spinlab/api/__init__.py`. Use whatever
the grep turns up.)

- [ ] **Step 4.2: Write the failing prewarm-hook test**

Create or extend a test (`tests/unit/test_dashboard_boot.py` if it
doesn't exist, otherwise the existing dashboard-startup test file):

```python
"""Dashboard boot wires the JAX prewarm in a background thread."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_prewarm_dispatched_in_background_thread(monkeypatch):
    """Boot should spawn a daemon thread that calls prewarm_buckets,
    never blocking the main startup."""
    from spinlab import app   # or wherever the startup lives

    spy = MagicMock()
    monkeypatch.setattr(app, "_prewarm_segments_model", spy)

    # Drive the startup hook directly. (Exact API depends on what step
    # 4.1 finds — adapt to either FastAPI lifespan or @on_event.)
    app._run_startup_hooks()  # to be created in step 4.3

    spy.assert_called_once()


def test_prewarm_failure_does_not_crash_boot(monkeypatch):
    """Missing [fits] extra → ImportError → boot continues silently."""
    from spinlab import app

    def boom():
        raise ImportError("jax not installed")
    monkeypatch.setattr(app, "_prewarm_segments_model", boom)

    # Should not raise.
    app._run_startup_hooks()
```

- [ ] **Step 4.3: Implement the prewarm hook**

In the dashboard startup module (from step 4.1), add:

```python
import logging
import threading

logger = logging.getLogger(__name__)


def _prewarm_segments_model() -> None:
    """JIT-compile the JAX kernels for the v07 model in a background
    thread. Without prewarm, the first refit per process pays a ~200-
    400ms JIT cost per attempt-bucket size; with prewarm, ~15ms
    steady-state. Total prewarm cost is ~10s; we eat it in a daemon
    thread so the dashboard responds before it finishes."""
    try:
        from spinlab.segments_model import prewarm_buckets
    except ImportError:
        logger.info("segments_model not installed ([fits] extra); skipping JAX prewarm.")
        return
    logger.info("segments-v07 JAX prewarm started in background thread")
    try:
        prewarm_buckets()
    except Exception:
        logger.exception("segments-v07 JAX prewarm failed")
        return
    logger.info("segments-v07 JAX prewarm complete")


def _run_startup_hooks() -> None:
    """Single entry point so tests can drive startup without spinning
    up a FastAPI app. Production wires this into the lifespan/on_event."""
    t = threading.Thread(
        target=_prewarm_segments_model,
        name="segments-v07-prewarm",
        daemon=True,
    )
    t.start()
```

Then wire `_run_startup_hooks` into whatever lifespan/on_event the
existing FastAPI app uses. If the app already has a startup hook,
append a single call to `_run_startup_hooks()` inside it.

- [ ] **Step 4.4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_dashboard_boot.py -v`
Expected: 2 tests pass.

- [ ] **Step 4.5: Manual smoke test**

Run: `spinlab dashboard 2>&1 | head -20` (kill after a few seconds)
Expected: log line `segments-v07 JAX prewarm started in background thread`.
After ~10s, `segments-v07 JAX prewarm complete`. The dashboard responds
on its port before prewarm finishes.

(Stop the dashboard cleanly with Ctrl-C; it's a foreground process.)

- [ ] **Step 4.6: Commit Task 4**

```bash
git add python/spinlab/app.py tests/unit/test_dashboard_boot.py
git commit -m "segments-v07 phase 1: JAX prewarm on dashboard boot

Fires in a daemon thread so first-byte latency stays unaffected. Logs
when the ~10s prewarm completes. Skips silently when [fits] isn't
installed (e.g. minimal dev environments)."
```

---

## Task 5 — `spinlab fit-pool` CLI

**Files:**
- Create: `python/spinlab/cli/fit_pool.py`
- Modify: `python/spinlab/cli/__init__.py` (register subcommand)
- Test:   `tests/integration/test_fit_pool_cli.py`

- [ ] **Step 5.1: Inspect the existing CLI shape**

Read `python/spinlab/cli/__init__.py` (or whichever file owns the
`spinlab` argparse). Note how existing subcommands are added so the new
one follows the same pattern.

- [ ] **Step 5.2: Write the failing CLI test**

Create `tests/integration/test_fit_pool_cli.py`:

```python
"""End-to-end: spinlab fit-pool over a tmp DB with seeded segments."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("jax")
pytest.importorskip("numpyro")


def _seed_db(db_path):
    from spinlab.db import Database
    db = Database(db_path)
    db.conn.execute(
        "INSERT INTO games (id, name, category, created_at) VALUES "
        "('g1', 'Test', 'Any%', '2026-05-19T00:00:00Z')"
    )
    # Five segments, each with ≥5 attempts so they meet the pool floor.
    from spinlab.models import EventAttempt, AttemptOutcome, AttemptSource
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for sid in [f"s{i}" for i in range(5)]:
        db.conn.execute(
            "INSERT INTO segments (id, game_id, level_number, "
            "start_type, end_type, created_at, updated_at) "
            "VALUES (?, 'g1', 1, 'entrance', 'exit', "
            "'2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')",
            (sid,),
        )
        db.conn.execute(
            "INSERT INTO sessions (id, game_id, started_at) "
            "VALUES (?, 'g1', '2026-05-19T00:00:00Z')",
            (f"sess-{sid}",),
        )
        ep = f"ep-{sid}"
        for i in range(10):
            db.log_event_attempt(EventAttempt(
                segment_id=sid, episode_id=ep,
                outcome=AttemptOutcome.SURVIVED, time_ms=20000,
                source=AttemptSource.PRACTICE,
                session_id=f"sess-{sid}", capture_run_id=None,
                chosen_allocator=None, invalidated=False,
                created_at=now,
            ))
    db.conn.commit()


def test_fit_pool_writes_pool_kind_rows(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    cfg = tmp_path / "spinlab.yaml"
    cfg.write_text(f"data_dir: {tmp_path}\ngame: g1\n")

    result = subprocess.run(
        [sys.executable, "-m", "spinlab", "fit-pool",
         "--config", str(cfg)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    from spinlab.db import Database
    db = Database(db_path)
    rows = db.conn.execute(
        "SELECT segment_id, kind FROM segment_fits WHERE kind='pool_fit'"
    ).fetchall()
    # One pool-kind row per segment in the pool.
    assert len(rows) == 5
```

- [ ] **Step 5.3: Run the test to verify it fails**

Run: `pytest tests/integration/test_fit_pool_cli.py -v`
Expected: FAIL — `spinlab fit-pool` doesn't exist.

- [ ] **Step 5.4: Implement the subcommand**

Create `python/spinlab/cli/fit_pool.py`:

```python
"""spinlab fit-pool — manual EB pool refit across a game's segments.

Phase 1 trigger model is intentionally manual: Andrew runs this after a
session, daily, or on demand. Phase 2 picks cron-vs-on-startup based on
how stale the pool actually gets in practice.

The CLI is a thin wrapper around sv.fit_pool with DB persistence
plumbed in.
"""
from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)

# Matches V1_ESSENCE POOL_MIN_PER_SEGMENT. Segments below this floor
# don't have enough data for their per-segment posterior to inform the
# pool prior; including them drags pool variance toward noise.
POOL_MIN_EVENTS = 5


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    sp = subparsers.add_parser(
        "fit-pool",
        help="Run an empirical-Bayes pool fit across a game's segments.",
    )
    sp.add_argument("--config", required=False, default="config.yaml",
                    help="Path to the SpinLab YAML config.")
    sp.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    # Lazy imports so `spinlab --help` doesn't pay the JAX boot cost.
    try:
        from spinlab.segments_model import fit_pool
    except ImportError:
        print("error: install the [fits] extra: pip install spinlab[fits]",
              file=__import__("sys").stderr)
        return 2

    from spinlab.config import load_config
    from spinlab.db import Database

    cfg = load_config(args.config)
    db = Database(cfg.db_path)

    game_id = cfg.game
    segs = [
        row for row in db.conn.execute(
            "SELECT id FROM segments WHERE game_id = ? AND active = 1",
            (game_id,),
        ).fetchall()
    ]
    if not segs:
        print(f"no active segments for game {game_id!r}")
        return 0

    inputs = []
    for row in segs:
        sid = row["id"]
        events = db.get_segment_event_rows(sid)
        attempts = [
            {"outcome": e["outcome"], "time_ms": int(e["time_ms"])}
            for e in events
            if not int(e["invalidated"])
        ]
        if len(attempts) >= POOL_MIN_EVENTS:
            inputs.append({"segment_id": sid, "attempts": attempts})

    if len(inputs) < 2:
        print(f"only {len(inputs)} segment(s) meet n>={POOL_MIN_EVENTS}; "
              f"pool needs ≥2. nothing to do.")
        return 0

    logger.info("fit-pool: %d segments, total %d attempts",
                len(inputs), sum(len(s["attempts"]) for s in inputs))
    pool_payload = fit_pool(inputs)

    # Persist each per-segment fit under kind='pool_fit'. The wrapper
    # envelope itself is informational; the per-segment bodies are what
    # the inspector/UI will eventually consume.
    for seg in pool_payload["result"]["segments"]:
        sid = seg["segment_id"]
        # Reconstruct a per-segment envelope so save_segment_fit's
        # status-column extraction works (it expects the outer shape).
        n_attempts = next(
            len(s["attempts"]) for s in inputs if s["segment_id"] == sid
        )
        per_seg_envelope = {
            "schema": pool_payload["schema"],
            "kind": "pool_fit",
            "segment_id": sid,
            "n_attempts": n_attempts,
            "model": pool_payload["model"],
            "wall_time_s": pool_payload["wall_time_s"],
            "status": seg["status"],
            "result": seg["result"],
            "caveats": seg["caveats"],
        }
        db.save_segment_fit(sid, "pool_fit", per_seg_envelope)

    print(f"fit-pool: wrote {len(pool_payload['result']['segments'])} pool_fit rows; "
          f"wall {pool_payload['wall_time_s']:.1f}s")
    return 0
```

- [ ] **Step 5.5: Register the subparser**

Open `python/spinlab/cli/__init__.py`. Find where existing subcommands
are registered (likely a chain of `add_subparser(...)` calls in the
`main` function). Add:

```python
from . import fit_pool as fit_pool_cli
fit_pool_cli.add_subparser(subparsers)
```

- [ ] **Step 5.6: Run the test**

Run: `pytest tests/integration/test_fit_pool_cli.py -v`
Expected: pass. Wall time may be 30-60s on first run because the
subprocess pays JAX cold-boot cost (no prewarm in CLI mode).

If the test is slow enough to be annoying in CI, mark it
`@pytest.mark.slow` (the marker is already declared in
`pyproject.toml`'s testpaths section if added; otherwise add a
`slow` marker first).

- [ ] **Step 5.7: Manual smoke test**

Run: `spinlab fit-pool --config <your dev config>.yaml`
Expected: prints a line like "fit-pool: wrote N pool_fit rows; wall X.Xs"
and rows appear in `segment_fits`. Verify with:

```bash
sqlite3 <data_dir>/spinlab.db "SELECT segment_id, kind, n_attempts, fittable FROM segment_fits WHERE kind='pool_fit' ORDER BY id DESC LIMIT 10"
```

- [ ] **Step 5.8: Commit Task 5**

```bash
git add python/spinlab/cli/fit_pool.py python/spinlab/cli/__init__.py \
        tests/integration/test_fit_pool_cli.py
git commit -m "segments-v07 phase 1: spinlab fit-pool CLI

Manual EB pool refit across active segments meeting the n>=5 floor.
Writes per-segment pool_fit rows alongside the streaming segment_fit
rows from the silent pipeline. Cron orchestration deferred to Phase 2."
```

---

## Task 6 — Full verification + stress run

- [ ] **Step 6.1: Full pytest baseline (closing edge)**

Run: `python -m pytest 2>&1 | tail -20`
Expected: green. All emulator tests still run; no skips. New tests:
2 vendor smoke + 5 segment_fits + 3 silent-fit-pipeline + 2 dashboard
boot + 1 fit-pool CLI = 13 net new.

- [ ] **Step 6.2: Type check**

Run: `npx pyright python/`
Expected: no NEW errors over the established baseline. (Pre-existing
261 pyright errors per `project_test_reliability_known_issues` are
not Phase 1's job.)

- [ ] **Step 6.3: Frontend type check + tests (smoke that nothing reaches frontend)**

Run: `cd frontend && npm run typecheck && npm test`
Expected: both green. Phase 1 is silent — no frontend changes — so
this should be unaffected; running it confirms.

- [ ] **Step 6.4: Stress-run the full suite**

Run: `for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do echo "--- run $i ---"; python -m pytest -q --tb=no 2>&1 | tail -3; done`
Expected: 15 consecutive green runs. Per
[[feedback_stress_test_flakes]], one green run is noise; this confirms
the silent-fit path doesn't introduce new flakes.

If any run flakes, identify whether it's a pre-existing flake (from
`project_test_reliability_known_issues`) or new. New flakes block Phase 1
shipping — debug and re-run.

- [ ] **Step 6.5: Manual dashboard exercise**

Run: `spinlab dashboard` in one terminal. In another:
1. Launch RA (use the standard practice flow).
2. Practice a segment for a few attempts (deaths + a clear).
3. Check `sqlite3 <data_dir>/spinlab.db "SELECT segment_id, kind, n_attempts, fittable, wall_time_ms FROM segment_fits ORDER BY id DESC LIMIT 5"`.
4. Expected: rows appear with `n_attempts` matching the event count,
   `wall_time_ms < 100` (warm-started), `fittable=1` once n≥5.

If wall_time_ms is consistently >200ms, the prewarm hook didn't fire
or didn't warm the right bucket — investigate.

- [ ] **Step 6.6: Update memory + plan status**

Open `C:/Users/thedo/.claude/projects/C--Users-thedo-git-spinlab/memory/project_segments_v07_integration.md`
and update the State block: Phase 1 shipped on <YYYY-MM-DD>, commit
<hash>; Phase 2 (CLI inspector + static HTML renderer) is next.

Open this plan file (`docs/superpowers/plans/2026-05-19-segments-v07-phase1-silent-fits.md`)
and change `status: drafted` to `status: shipped` with a `shipped_at` field.

- [ ] **Step 6.7: Final commit**

```bash
git add docs/superpowers/plans/2026-05-19-segments-v07-phase1-silent-fits.md \
        "C:/Users/thedo/.claude/projects/C--Users-thedo-git-spinlab/memory/project_segments_v07_integration.md"
git commit -m "docs: segments-v07 phase 1 shipped — silent fit pipeline live

Plan file marked shipped; project memory updated. Phase 2 (CLI
inspector + static HTML renderer for Andrew's evidence-gathering pass
before the Phase 3 UI decision) is now unblocked."
```

---

## Risks and known gaps

- **Per-attempt 15ms fit budget blows the test suite wall-clock.** Most
  tests that exercise `Scheduler.process_attempt` are unit-level and
  don't go through DB persistence — the `_MIN_EVENTS_FOR_FIT` gate
  filters them. The remaining hits are the new pipeline tests
  themselves, which are budget-tolerant. If wall-clock balloons,
  raise the gate to 10 events or stub `_refit_segment` at the fixture
  layer for non-fit-targeted tests.

- **`segments_experiment/tests/` get pulled into pytest collection
  after the rename.** They use the prototype's own conftest, which
  expects to run from inside the prototype dir. Fix: add an
  `__init__.py` (already present) or a `conftest.py` at
  `python/spinlab/_segments_v07/` with `collect_ignore = ['tests']`
  so SpinLab's pytest treats them as opaque vendored content. If
  collection breaks at Task 1 step 1.10, this is why.

- **Windows JAX install footprint.** jaxlib wheels are available for
  Windows on the pinned 0.10 version, but version drift is a hazard.
  Task 1 step 1.3 is the early failure surface; STOP and ask there if
  it doesn't install cleanly.

- **Pool fit wall time** on real data is "minutes" per V1_ESSENCE.
  The CLI test runs on 5 segments × 10 attempts, which should be
  seconds. Real-world pool runs may need to be backgrounded or run
  during off-hours — observe in Task 6.5.

- **Migration `0003` is destructive only for the new table** —
  `CREATE TABLE IF NOT EXISTS` is safe to apply to a DB that already
  ran the migration (the runner won't re-apply, but defensively the
  SQL is idempotent).

## Phase 1 done = Phase 2 unblocked

Phase 2 builds the static HTML inspector + the `spinlab fit show`
CLI on top of the rows this phase writes. The inspector is what
generates the evidence Andrew uses in Phase 3 to pick the UI shape
(B-ish vs C-ish per the spec's decision rubric). No UI changes in
Phase 1 — that's deliberate.

## Verification gate before declaring done

Per [[feedback_run_all_tests]] + [[feedback_fix_preexisting_failures]]:

- [ ] `python -m pytest` green at session START (baseline)
- [ ] `python -m pytest` green at session END
- [ ] No new skips. Skips count as failures.
- [ ] Pre-existing failures (if baseline was already red) acknowledged
      and either fixed first or written into the followup queue with
      explicit deferral sign-off.
