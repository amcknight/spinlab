"""Segment and segment variant queries."""

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
    strat_version: int
    active: int
    ordinal: int | None
    state_path: str | None


class SegmentsMixin:
    """Segment CRUD and variant management."""

    # -- Segments --

    def upsert_segment(self, seg: Segment) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO segments (id, game_id, level_number, start_type, start_ordinal,
               end_type, end_ordinal, description, strat_version, active, ordinal,
               reference_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 description=excluded.description,
                 ordinal=excluded.ordinal,
                 reference_id=excluded.reference_id,
                 active=excluded.active,
                 updated_at=excluded.updated_at""",
            (seg.id, seg.game_id, seg.level_number, seg.start_type,
             seg.start_ordinal, seg.end_type, seg.end_ordinal,
             seg.description, seg.strat_version, int(seg.active),
             seg.ordinal, seg.reference_id, now, now),
        )
        self.conn.commit()

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
        self.conn.commit()

    def increment_strat_version(self, segment_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE segments SET strat_version = strat_version + 1, updated_at = ? WHERE id = ?",
            (now, segment_id),
        )
        self.conn.commit()

    def get_all_segments_with_model(self, game_id: str) -> list[SegmentRow]:
        """Get all active segments. state_path is always NULL until Task 8 rewrites
        this to join waypoint_save_states via start_waypoint_id."""
        cur = self.conn.execute(
            """SELECT s.id, s.game_id, s.level_number, s.start_type, s.start_ordinal,
                      s.end_type, s.end_ordinal, s.description, s.strat_version,
                      s.active, s.ordinal,
                      NULL AS state_path
               FROM segments s
               WHERE s.game_id = ? AND s.active = 1
               ORDER BY s.ordinal, s.level_number""",
            (game_id,),
        )
        actual_cols = [desc[0] for desc in cur.description]
        return [dict(zip(actual_cols, row)) for row in cur.fetchall()]

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
        self.conn.commit()

    def segment_exists(self, segment_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
        return row is not None

    def soft_delete_segment(self, segment_id: str) -> None:
        self.update_segment(segment_id, active=0)

    # -- Waypoint Save States --

    def add_save_state(self, s: WaypointSaveState) -> None:
        self.conn.execute(
            """INSERT INTO waypoint_save_states
               (waypoint_id, variant_type, state_path, is_default)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(waypoint_id, variant_type) DO UPDATE SET
                 state_path=excluded.state_path,
                 is_default=excluded.is_default""",
            (s.waypoint_id, s.variant_type, s.state_path, int(s.is_default)),
        )
        self.conn.commit()

    def get_save_state(self, waypoint_id: str,
                       variant_type: str) -> WaypointSaveState | None:
        row = self.conn.execute(
            """SELECT waypoint_id, variant_type, state_path, is_default
               FROM waypoint_save_states
               WHERE waypoint_id = ? AND variant_type = ?""",
            (waypoint_id, variant_type),
        ).fetchone()
        if row is None:
            return None
        return WaypointSaveState(
            waypoint_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )

    def get_default_save_state(self, waypoint_id: str) -> WaypointSaveState | None:
        row = self.conn.execute(
            """SELECT waypoint_id, variant_type, state_path, is_default
               FROM waypoint_save_states WHERE waypoint_id = ?
               ORDER BY is_default DESC LIMIT 1""",
            (waypoint_id,),
        ).fetchone()
        if row is None:
            return None
        return WaypointSaveState(
            waypoint_id=row[0], variant_type=row[1],
            state_path=row[2], is_default=bool(row[3]),
        )
