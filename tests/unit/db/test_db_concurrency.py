"""Concurrency regression: one Database used from many threads must never
return an empty result for a row that is permanently present.

Pre-fix (single shared sqlite connection across threads) this fails with
spurious None results and/or sqlite3.InterfaceError. Post-fix (thread-local
connections) reads are isolated per thread and it passes deterministically.
"""
from __future__ import annotations

import threading

from spinlab.db import Database
from spinlab.models import Segment

# Wall-clock window the reader/writer threads interleave for. A few seconds
# is ample to trigger the shared-connection corruption on pre-fix code (the
# isolation repro hit it within ~4s) while keeping the test — and the 15x
# stress run — fast. We bound by TIME, not by a read count: a busy-wait on the
# main thread would hold the GIL and starve the worker threads (a count-based
# loop took 23 minutes that way); Event.wait() sleeps and lets them run.
_RUN_SECONDS = 3.0


def _seg(seg_id: str) -> Segment:
    return Segment(
        id=seg_id, game_id="g", level_number=1,
        start_type="entrance", start_ordinal=0,
        end_type="checkpoint", end_ordinal=1, active=True,
    )


def test_concurrent_reads_never_miss_a_present_row(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(_seg(seg_id))
    assert db.get_segment_by_id(seg_id) is not None  # present before threads start

    stop = threading.Event()
    misses: list[str] = []
    errors: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                reads[0] += 1
                if db.get_segment_by_id(seg_id) is None:
                    misses.append("None")
            except Exception as e:  # InterfaceError etc.
                errors.append(repr(e))

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                db.update_segment(seg_id, description=f"d{i}")
                db.upsert_segment(_seg(seg_id))
            except Exception as e:
                errors.append(repr(e))

    threads = [threading.Thread(target=reader) for _ in range(4)] + \
              [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    # Bounded wall-clock window. Event.wait() sleeps (releasing the GIL) so the
    # worker threads actually run — see _RUN_SECONDS.
    stop.wait(_RUN_SECONDS)
    stop.set()
    for t in threads:
        t.join()

    assert reads[0] > 0, "readers never ran"  # guard against a no-op test
    assert db.get_segment_by_id(seg_id) is not None  # still present at the end
    assert errors == [], f"DB raised under concurrency: {errors[:3]}"
    assert misses == [], f"{len(misses)} spurious empty reads of a present row"
    db.close()


def test_memory_db_shares_state_across_operations():
    # :memory: keeps ONE shared connection; writes are visible to later reads
    # on the same Database (would be invisible if each call got a fresh
    # per-connection in-memory db).
    db = Database(":memory:")
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    db.upsert_segment(_seg(seg_id))
    assert db.get_segment_by_id(seg_id) is not None
    db.close()


def test_transaction_rollback_still_works(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.upsert_game("g", "G", "any%")
    seg_id = "g:1:entrance.0:checkpoint.1:aa:bb"
    try:
        with db.transaction():
            db.upsert_segment(_seg(seg_id))
            raise RuntimeError("boom")  # force rollback
    except RuntimeError:
        pass
    assert db.get_segment_by_id(seg_id) is None  # rolled back
    db.close()
