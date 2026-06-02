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
    # Spin enough iterations to exercise interleaving without a wall-clock sleep
    # dependency: stop once readers have done a lot of work.
    while reads[0] < 40000 and not errors:
        pass
    stop.set()
    for t in threads:
        t.join()

    assert db.get_segment_by_id(seg_id) is not None  # still present at the end
    assert errors == [], f"DB raised under concurrency: {errors[:3]}"
    assert misses == [], f"{len(misses)} spurious empty reads of a present row"
    db.close()
