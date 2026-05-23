-- Rename AttemptSource "speed_run" → "hyper_play" to match the mode rename.
UPDATE attempts SET source = 'hyper_play' WHERE source = 'speed_run';
