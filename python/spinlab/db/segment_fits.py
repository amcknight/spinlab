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
