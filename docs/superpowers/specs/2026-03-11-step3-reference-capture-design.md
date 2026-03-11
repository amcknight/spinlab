# Step 3 — Reference Capture Design

## Goal

Extend the passive recorder so that a reference run produces both a JSONL log and save state files, then post-process those into a YAML manifest that the practice loop can consume.

## Lua Changes

On `level_entrance` (game_mode 18 → not a retry), set `pending_save` pointing to `states/{game_id}_{level}_{room}.mss`. The cpuExec callback writes the state. Include `state_path` in the JSONL entry.

Capturing at game_mode 18 is intentional: the level-card/iris-in transition plays naturally after a state load, giving the player a beat before gaining control. It's also a robust signal across SMW romhacks.

State files use `{game_id}_{level}_{room}.mss` — no goal suffix. Multiple goal variants for the same entrance share one state file by design: the player enters the level the same way regardless of which exit they intend to take. The goal field in the manifest controls overlay display only, not which state is loaded.

Retry entrances are already suppressed by the `died_flag` logic in the Lua recorder — `capture.py` does not need to filter them.

## Python `capture.py`

Reads `passive_log.jsonl`, pairs each `level_entrance` with the next `level_exit` for the same level/room. The `elapsed_ms` field on the exit event becomes `reference_time_ms` in the manifest.

Outputs `{data_dir}/captures/{YYYY-MM-DD}_{game_id}_manifest.yaml`, where `data_dir` is read from `config.yaml` under `data.dir` (default: `data/` relative to repo root).

Manifest fields per split: `id`, `level_number`, `room_id`, `goal`, `state_path`, `reference_time_ms`.

Edge cases deferred — keep it simple for now.

Run via: `python -m spinlab.capture`

## Verification

Do a short reference run through 2-3 levels. Run `capture.py`. Confirm manifest is valid YAML and state files exist on disk.
