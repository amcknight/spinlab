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
        on (segment_id, kind, id DESC); should be ~us even at scale.
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
              MAX(sf.id)
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
