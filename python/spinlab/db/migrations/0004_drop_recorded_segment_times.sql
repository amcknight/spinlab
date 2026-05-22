-- segments-v07 reference-event-level refactor: the SegmentRecorder now writes
-- per-event rows directly to `attempts` at segment close. The old
-- `recorded_segment_times` table existed only as a buffer that the finalize
-- path drained into seed-attempts; with the event-level recorder the buffer
-- has no readers left.
--
-- Forward-only drop. No data migration: the previous reference data shape
-- (one summary row per segment) is not recoverable into the new event-level
-- shape without per-event timestamps that were never captured.

DROP TABLE IF EXISTS recorded_segment_times;
