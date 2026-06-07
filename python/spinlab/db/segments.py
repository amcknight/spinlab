"""Segment and segment variant queries."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from ..models import Segment, WaypointSaveState


class SegmentRow(TypedDict):
    id: str
    game_id: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str
    active: int
    ordinal: int
    is_primary: int
    start_waypoint_id: str | None
    end_waypoint_id: str | None
    state_path: str | None


class MissingColdRow(TypedDict):
    segment_id: str
    hot_state_path: str
    level_number: int
    start_type: str
    start_ordinal: int
    end_type: str
    end_ordinal: int
    description: str


class SegmentsMixin:
    """Segment CRUD and variant management."""
    conn: sqlite3.Connection
    _row_to_segment: Callable[[sqlite3.Row], Segment]

    def upsert_segment(self, seg: Segment) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,
               end_type, end_ordinal, start_waypoint_id, end_waypoint_id, is_primary,
               description, active, ordinal,
               capture_run_id, capture_session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 active=excluded.active,
                 is_primary=excluded.is_primary,
                 updated_at=excluded.updated_at""",
            # NOTE: capture_run_id / capture_session_id are deliberately NOT
            # updated on conflict — the run that FIRST captured a segment keeps
            # ownership. Segments are shared rows keyed by a deterministic id, so
            # a later run that re-traverses the same segment (notably a Replay,
            # which re-runs the detector) must not steal it from the original
            # run, or run-scoped queries (cold-fill, sections_captured) break.
            (seg.id, seg.game_id, seg.level_number, seg.start_type,
             seg.start_ordinal, seg.end_type, seg.end_ordinal,
             seg.start_waypoint_id, seg.end_waypoint_id, int(seg.is_primary),
             seg.description, int(seg.active),
             seg.ordinal, seg.capture_run_id, seg.capture_session_id, now, now),
        )

    def get_active_segments(self, game_id: str) -> list[Segment]:
        rows = self.conn.execute(
            "SELECT * FROM segments WHERE game_id = ? AND active = 1", (game_id,)
        ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def deactivate_segment(self, segment_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET active = 0, updated_at = ? WHERE id = ?",
            (now, segment_id),
        )

    def get_segment_row(self, segment_id: str) -> SegmentRow | None:
        """Get a single segment row with its start-waypoint save state path.

        Variant precedence: prefer `cold` over `hot`. Cold is the deterministic
        post-respawn state (captured by cold-fill); hot is the mid-stride
        reference-run snapshot. Practice needs cold for repeatable starts; hot
        is only the fallback until cold-fill captures the cold variant.
        """
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                       s.end_type, s.end_ordinal, s.description,
                       s.active, s.ordinal, s.is_primary,
                       s.start_waypoint_id, s.end_waypoint_id,
                       (SELECT wss.state_path FROM waypoint_save_states wss
                        WHERE wss.waypoint_id = s.start_waypoint_id
                        ORDER BY CASE wss.variant_type
                                   WHEN 'cold' THEN 0
                                   WHEN 'hot'  THEN 1
                                   ELSE 2
                                 END
                        LIMIT 1) AS state_path
                FROM segments s
                WHERE s.id = ?""",
            (segment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        actual_cols = [desc[0] for desc in cur.description]
        return dict(zip(actual_cols, row))  # type: ignore[return-value]

    def get_all_segments_with_model(self, game_id: str, *,
                                    primary_only: bool = True,
                                    run_id: str | None = None) -> list[SegmentRow]:
        """Get all active segments with their start-waypoint save state path.

        Variant precedence: prefer `cold` over `hot` for every segment. Cold
        is the deterministic post-respawn state (captured by cold-fill for
        checkpoint segments, captured at entrance time for entrance segments);
        hot is the mid-stride reference-run snapshot, used only as a fallback
        until cold-fill captures the cold variant.

        Args:
            game_id: game to query
            primary_only: if True (default), only return is_primary=True segments
                          (used by practice loop); if False, return all
                          (used by dashboard segments view).
            run_id: if given, scope to segments the run *traversed* (>=1
                    non-invalidated attempt stamped with this run). ``None``
                    (default) = whole game. Mirrors the membership predicate in
                    ``segments_missing_cold`` and ``get_segments_for_run``.
        """
        primary_clause = "AND s.is_primary = 1" if primary_only else ""
        params: list = [game_id]
        run_clause = ""
        if run_id is not None:
            run_clause = ("AND s.id IN (SELECT DISTINCT segment_id FROM attempts "
                          "WHERE capture_run_id = ? AND invalidated = 0)")
            params.append(run_id)
        cur = self.conn.execute(
            f"""SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                       s.end_type, s.end_ordinal, s.description,
                       s.active, s.ordinal, s.is_primary,
                       s.start_waypoint_id, s.end_waypoint_id,
                       (SELECT wss.state_path FROM waypoint_save_states wss
                        WHERE wss.waypoint_id = s.start_waypoint_id
                        ORDER BY CASE wss.variant_type
                                   WHEN 'cold' THEN 0
                                   WHEN 'hot'  THEN 1
                                   ELSE 2
                                 END
                        LIMIT 1) AS state_path
                FROM segments s
                WHERE s.game_id = ? AND s.active = 1 {primary_clause} {run_clause}
                ORDER BY s.ordinal, s.level_number""",
            params,
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]  # type: ignore[return-value]

    def segments_missing_cold(self, game_id: str,
                              run_id: str | None = None) -> list[MissingColdRow]:
        """Return segments whose start waypoint has hot but not cold save state.

        ``run_id`` scopes to segments the run *traversed* (at least one
        non-invalidated attempt in the ``attempts`` table), not segments it
        owns; ``None`` = whole game. Mirrors ``get_segments_by_reference`` and
        ``count_segments_traversed_in_run``.
        """
        params: list = [game_id]
        run_clause = ""
        if run_id is not None:
            run_clause = ("AND s.id IN (SELECT DISTINCT segment_id FROM attempts "
                          "WHERE capture_run_id = ? AND invalidated = 0)")
            params.append(run_id)
        rows = self.conn.execute(
            f"""SELECT s.id AS segment_id, hot.state_path AS hot_state_path,
                      s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description
               FROM segments s
               JOIN waypoint_save_states hot
                 ON hot.waypoint_id = s.start_waypoint_id AND hot.variant_type = 'hot'
               LEFT JOIN waypoint_save_states cold
                 ON cold.waypoint_id = s.start_waypoint_id AND cold.variant_type = 'cold'
               WHERE s.game_id = ? AND s.active = 1 {run_clause}
                 AND cold.waypoint_id IS NULL
               ORDER BY s.ordinal, s.level_number, s.start_ordinal""",
            params,
        ).fetchall()
        cols = ["segment_id", "hot_state_path", "level_number",
                "start_type", "start_ordinal", "end_type", "end_ordinal", "description"]
        return [dict(zip(cols, r)) for r in rows]  # type: ignore[return-value]

    def update_segment(self, segment_id: str, **kwargs) -> None:
        """Partial update: pass description=, active= as kwargs."""
        allowed = {"description", "active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "active" in updates:
            updates["active"] = int(updates["active"])
        now = datetime.now(UTC).isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [now, segment_id]
        self.conn.execute(
            f"UPDATE segments SET {sets}, updated_at = ? WHERE id = ?", vals
        )

    def set_segment_is_primary(self, segment_id: str, is_primary: bool) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET is_primary = ?, updated_at = ? WHERE id = ?",
            (int(is_primary), now, segment_id),
        )

    def segment_exists(self, segment_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        return row is not None

    def get_segment_by_id(self, segment_id: str) -> Segment | None:
        row = self.conn.execute(
            "SELECT * FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_segment(row)

    def soft_delete_segment(self, segment_id: str) -> None:
        self.update_segment(segment_id, active=0)

    def add_save_state(self, s: WaypointSaveState) -> None:
        self.conn.execute(
            """INSERT INTO waypoint_save_states
               (waypoint_id, variant_type, state_path)
               VALUES (?, ?, ?)
               ON CONFLICT(waypoint_id, variant_type) DO UPDATE SET
                 state_path=excluded.state_path""",
            (s.waypoint_id, s.variant_type, s.state_path),
        )

    def get_save_state(self, waypoint_id: str,
                       variant_type: str) -> WaypointSaveState | None:
        row = self.conn.execute(
            """SELECT waypoint_id, variant_type, state_path
               FROM waypoint_save_states
               WHERE waypoint_id = ? AND variant_type = ?""",
            (waypoint_id, variant_type),
        ).fetchone()
        if row is None:
            return None
        return WaypointSaveState(
            waypoint_id=row[0], variant_type=row[1], state_path=row[2],
        )

    def get_segments_for_run(self, game_id: str, run_id: str) -> list[Segment]:
        """Return active segments this run *traversed*, ordered by ordinal.

        Membership = at least one non-invalidated row in the ``attempts`` table
        stamped with ``run_id``. This is the same predicate used by
        ``count_segments_traversed_in_run`` and ``segments_missing_cold(run_id=...)``,
        but returns full ``Segment`` objects instead of counts or partial rows.

        Contrast with ``get_active_segments``, which returns all active segments
        for a game regardless of which run recorded them. Use this function when
        you need to scope a view (e.g. Practice/Model/Segments) to a single
        reference run. Pooling (``get_segment_attempts``) is unaffected — it
        operates on the attempts table directly and is not scoped by this function.

        Args:
            game_id: game to query (guards against cross-game segment id collisions)
            run_id: the capture run whose traversals define membership
        """
        rows = self.conn.execute(
            """SELECT * FROM segments s
               WHERE s.game_id = ? AND s.active = 1
                 AND s.id IN (SELECT DISTINCT a.segment_id FROM attempts a
                              WHERE a.capture_run_id = ? AND a.invalidated = 0)
               ORDER BY s.ordinal, s.level_number""",
            (game_id, run_id),
        ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def count_segments_for_run(self, run_id: str, *, active_only: bool = False) -> int:
        """Count segments whose ``capture_run_id`` matches ``run_id``.

        ``active_only=True`` filters to ``active = 1`` (excludes soft-deleted).
        """
        if active_only:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE capture_run_id = ? AND active = 1",
                (run_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE capture_run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def count_segments_traversed_in_run(self, run_id: str) -> int:
        """Count distinct segments this run *traversed* — i.e. recorded at
        least one non-invalidated event for — regardless of which run *owns*
        the segment row.

        Ownership (``capture_run_id`` on ``segments``) is first-writer-wins
        (see ``upsert_segment``), so a re-record of an already-captured level
        owns 0 segments. But the recorder still writes an event row stamped
        with this run's id on every segment it closes, so the distinct
        ``segment_id`` count over those rows is the honest "segments captured
        this run" figure — what the live counter should show. Contrast
        ``count_segments_for_run``, which counts *ownership* and is the right
        tool for "did this run create anything new" (e.g. replay cleanup).
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT segment_id) FROM attempts "
            "WHERE capture_run_id = ? AND invalidated = 0",
            (run_id,),
        ).fetchone()
        return int(row[0])

    def count_segments_for_capture_session(self, session_id: str) -> int:
        """Count segments belonging to a specific capture session."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE capture_session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def has_competing_active_segment(
        self,
        *,
        game_id: str,
        level: int,
        start_type: str,
        start_ordinal: int,
        end_type: str,
        end_ordinal: int,
        exclude_segment_id: str,
    ) -> bool:
        """True if another *active* segment shares these endpoints."""
        row = self.conn.execute(
            """SELECT id FROM segments
               WHERE game_id = ? AND level_number = ?
                 AND start_type = ? AND start_ordinal = ?
                 AND end_type = ? AND end_ordinal = ?
                 AND active = 1 AND id != ?""",
            (game_id, level, start_type, start_ordinal,
             end_type, end_ordinal, exclude_segment_id),
        ).fetchone()
        return row is not None
