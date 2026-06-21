"""Capture run (reference) queries.

Status / kind / active are orthogonal:
- ``status`` ('draft' | 'saved') — lifecycle. Draft runs are paused; saved runs
  are finalized.
- ``kind`` ('live' | 'replay') — capture method. Live runs come from a real
  recording session; replay runs come from playing back an existing .replay
  file. Recovery on restart only surfaces ``kind='live'`` drafts — replay
  drafts are ephemeral.
- ``active`` (0 | 1) — per-game selection. At most one ``status='saved'`` run
  per game is the chosen reference for practice; only saved runs are
  selectable. Enforced in code (``set_active_capture_run``), not by index.
"""

import sqlite3
from datetime import UTC, datetime
from typing import TypedDict


class CaptureRunRow(TypedDict):
    id: str
    game_id: str
    name: str
    created_at: str
    status: str
    active: int
    kind: str



class CaptureRunsMixin:
    """Reference run CRUD and draft lifecycle.

    Methods that need internal atomicity call ``self.transaction()``, provided
    by ``DatabaseCore`` via composition in the ``Database`` class. No forward
    declaration here — adding a runtime stub would shadow the real method via
    MRO, and a TYPE_CHECKING stub would clash on the return type.
    """
    conn: sqlite3.Connection

    def create_capture_run(
        self, run_id: str, game_id: str, name: str,
        *, kind: str = "live",
    ) -> None:
        """Create a fresh draft capture_run. ``kind`` is 'live' or 'replay'."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO capture_runs (id, game_id, name, created_at, status, active, kind) "
            "VALUES (?, ?, ?, ?, 'draft', 0, ?)",
            (run_id, game_id, name, now, kind),
        )

    def list_capture_runs(self, game_id: str) -> list[CaptureRunRow]:
        """List finalized capture_runs for a game (drafts excluded)."""
        rows = self.conn.execute(
            "SELECT id, game_id, name, created_at, status, active, kind "
            "FROM capture_runs "
            "WHERE game_id = ? AND status = 'saved' ORDER BY created_at",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[return-value]

    def set_active_capture_run(self, run_id: str) -> None:
        """Mark ``run_id`` as the active reference for its game.

        No-op if ``run_id`` doesn't exist. Only saved runs can be active; this
        is enforced by callers (finalize sets status='saved' first).
        """
        row = self.conn.execute(
            "SELECT game_id FROM capture_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        game_id = row[0]
        with self.transaction():  # type: ignore[attr-defined]
            self.conn.execute(
                "UPDATE capture_runs SET active = 0 WHERE game_id = ?", (game_id,)
            )
            self.conn.execute(
                "UPDATE capture_runs SET active = 1 WHERE id = ?", (run_id,)
            )

    def get_active_capture_run(self, game_id: str) -> str | None:
        """Return the id of the active (active=1) capture run for a game, or None."""
        row = self.conn.execute(
            "SELECT id FROM capture_runs WHERE game_id = ? AND active = 1",
            (game_id,),
        ).fetchone()
        return row[0] if row else None

    def rename_capture_run(self, run_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE capture_runs SET name = ? WHERE id = ?", (name, run_id)
        )

    def delete_capture_run(self, run_id: str, *, purge_data: bool) -> None:
        """Delete a reference run. Two modes, selected by ``purge_data``.

        ``run_only`` (``purge_data=False``): remove the ``capture_runs`` row and
        this run's membership ``attempts`` (rows stamped ``capture_run_id =
        run``). Segments stay ACTIVE — they re-surface under any future run over
        the same geography. Geography-pooled practice history (session-stamped
        attempts) and OTHER runs' attempts survive.

        ``run_and_data`` (``purge_data=True``): everything ``run_only`` does PLUS
        purge segments + their save-states + their attempts + ``model_state`` +
        ``segment_fits`` that are EXCLUSIVE to this run — a segment with no OTHER
        run's non-invalidated attempts referencing it. Segments SHARED with
        another run are protected.

        Both modes null ``segments.capture_session_id`` first so the cascade
        chain (capture_runs → capture_sessions → segments via ON DELETE CASCADE)
        doesn't delete segments out from under their non-cascading attempts FK,
        and reassign ``active`` to the most-recent remaining saved run for the
        game when the deleted run was active.
        """
        now = datetime.now(UTC).isoformat()
        with self.transaction():  # type: ignore[attr-defined]
            run_row = self.conn.execute(
                "SELECT game_id, active FROM capture_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run_row:
                return  # no-op, matches old behavior
            game_id, was_active = run_row[0], bool(run_row[1])

            if purge_data:
                self._purge_exclusive_segments(run_id)

            # Break the segments→capture_sessions cascade so surviving segments
            # stay alive when capture_sessions cascade-delete from the run.
            self.conn.execute(
                """
                UPDATE segments SET capture_session_id = NULL, updated_at = ?
                WHERE capture_session_id IN (
                    SELECT id FROM capture_sessions WHERE capture_run_id = ?
                )
                """,
                (now, run_id),
            )
            # Drop this run's ownership stamp so the capture_runs delete can't
            # cascade surviving segments away. Keep them ACTIVE (the run_only
            # difference from the old soft-delete).
            self.conn.execute(
                "UPDATE segments SET capture_run_id = NULL, updated_at = ? "
                "WHERE capture_run_id = ?",
                (now, run_id),
            )
            # Remove this run's membership/traversal attempts.
            self.conn.execute(
                "DELETE FROM attempts WHERE capture_run_id = ?", (run_id,)
            )
            # capture_sessions cascade from capture_runs.
            self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))

            if was_active:
                remaining = self.list_capture_runs(game_id)
                if remaining:
                    # list_capture_runs is ORDER BY created_at asc → last is newest.
                    # set_active_capture_run is SAVEPOINT-composable under the
                    # outer transaction and reuses the zero-all-others invariant.
                    self.set_active_capture_run(remaining[-1]["id"])

    def _purge_exclusive_segments(self, run_id: str) -> None:
        """Delete segments (and their model_state, attempts, fits, save-states)
        that are EXCLUSIVE to ``run_id``. Must run BEFORE the run's ownership stamp
        and attempts are removed, since exclusivity is computed from both.

        Candidate set = segments OWNED by the run (capture_run_id = run) UNION
        segments TRAVERSED by the run (a row in attempts with capture_run_id =
        run). A candidate is EXCLUSIVE iff no OTHER run's non-invalidated
        attempts reference it. Shared candidates are left fully intact.
        """
        candidates = {
            r[0] for r in self.conn.execute(
                "SELECT id FROM segments WHERE capture_run_id = ?", (run_id,)
            ).fetchall()
        }
        candidates |= {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT segment_id FROM attempts WHERE capture_run_id = ?",
                (run_id,),
            ).fetchall()
        }
        if not candidates:
            return

        shared = {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT segment_id FROM attempts "
                "WHERE capture_run_id != ? AND invalidated = 0",
                (run_id,),
            ).fetchall()
        }
        exclusive = [s for s in candidates if s not in shared]
        if not exclusive:
            return

        placeholders = ",".join("?" * len(exclusive))
        # Waypoints referenced by the segments about to be deleted.
        doomed_waypoints = {
            wp
            for row in self.conn.execute(
                f"SELECT start_waypoint_id, end_waypoint_id FROM segments "
                f"WHERE id IN ({placeholders})",
                exclusive,
            ).fetchall()
            for wp in row
            if wp is not None
        }
        # Waypoints still referenced by a SURVIVING segment (not being deleted).
        # Geography waypoints can be shared across runs/segments — never delete
        # a save-state still anchoring a segment that lives on.
        survivor_waypoints: set[str] = set()
        if doomed_waypoints:
            wp_ph = ",".join("?" * len(doomed_waypoints))
            seg_ph = ",".join("?" * len(exclusive))
            survivor_waypoints = {
                wp
                for row in self.conn.execute(
                    f"SELECT start_waypoint_id, end_waypoint_id FROM segments "
                    f"WHERE id NOT IN ({seg_ph}) "
                    f"AND (start_waypoint_id IN ({wp_ph}) "
                    f"     OR end_waypoint_id IN ({wp_ph}))",
                    [*exclusive, *doomed_waypoints, *doomed_waypoints],
                ).fetchall()
                for wp in row
                if wp is not None
            }
        purgeable_waypoints = doomed_waypoints - survivor_waypoints

        # Order matters: every table with an enforced FK to segments(id) and no
        # ON DELETE CASCADE (model_state, attempts, segment_fits) must lose its
        # rows BEFORE the segment rows they reference, or the segments DELETE
        # aborts with FOREIGN KEY constraint failed (PRAGMA foreign_keys=ON).
        self.conn.execute(
            f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
            exclusive,
        )
        self.conn.execute(
            f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
            exclusive,
        )
        self.conn.execute(
            f"DELETE FROM segment_fits WHERE segment_id IN ({placeholders})",
            exclusive,
        )
        if purgeable_waypoints:
            wp_ph = ",".join("?" * len(purgeable_waypoints))
            self.conn.execute(
                f"DELETE FROM waypoint_save_states WHERE waypoint_id IN ({wp_ph})",
                list(purgeable_waypoints),
            )
        self.conn.execute(
            f"DELETE FROM segments WHERE id IN ({placeholders})", exclusive
        )

    def promote_draft(self, run_id: str, name: str) -> None:
        """Promote a draft capture run to saved: rename and set status='saved'."""
        self.conn.execute(
            "UPDATE capture_runs SET status = 'saved', name = ? WHERE id = ?",
            (name, run_id),
        )

    def is_run_draft(self, run_id: str) -> bool:
        """True if ``run_id`` exists and is in draft state. Missing runs return False."""
        row = self.conn.execute(
            "SELECT status FROM capture_runs WHERE id = ?", (run_id,),
        ).fetchone()
        return bool(row and row[0] == "draft")

    def hard_delete_capture_run(self, run_id: str) -> None:
        """Hard delete: remove run, segments, model_state, and attempts."""
        with self.transaction():  # type: ignore[attr-defined]
            seg_ids = [
                r[0] for r in self.conn.execute(
                    "SELECT id FROM segments WHERE capture_run_id = ?", (run_id,)
                ).fetchall()
            ]
            if seg_ids:
                placeholders = ",".join("?" * len(seg_ids))
                self.conn.execute(
                    f"DELETE FROM model_state WHERE segment_id IN ({placeholders})",
                    seg_ids,
                )
                self.conn.execute(
                    f"DELETE FROM attempts WHERE segment_id IN ({placeholders})",
                    seg_ids,
                )
                self.conn.execute(
                    "DELETE FROM segments WHERE capture_run_id = ?", (run_id,),
                )
            # capture_sessions CASCADE from capture_runs
            self.conn.execute("DELETE FROM capture_runs WHERE id = ?", (run_id,))

