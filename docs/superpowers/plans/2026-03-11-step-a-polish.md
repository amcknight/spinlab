# Step A Polish Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four quick wins that make SpinLab comfortable to actually use: fix the rating button, add readable split names, add a proper CLI entry point with AHK hotkey, and polish the in-emulator overlay.

**Architecture:** Three isolated change areas — Lua script (A1, A4), Python package (A2, A3), and a new AHK script (A3). None touch the DB schema. A2 adds a `name` field to the manifest YAML and wires it through to the display layer. A3 creates `cli.py` and wires up `pyproject.toml` scripts. A4 redesigns `draw_practice_overlay()`.

**Tech Stack:** Lua (Mesen2), Python 3.11 + argparse + PyYAML, AutoHotkey v2

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `lua/spinlab.lua` | Modify | A1: button fix; A4: overlay redesign |
| `python/spinlab/capture.py` | Modify | A2: add `name: ""` to manifest entries |
| `python/spinlab/orchestrator.py` | Modify | A2: populate description from `name`; show name in stdout |
| `python/spinlab/cli.py` | Create | A3: argparse CLI (`practice`, `capture`, `stats`) |
| `pyproject.toml` | Modify | A3: add `[project.scripts]` entry |
| `scripts/spinlab.ahk` | Create | A3: AHK v2 toggle hotkey (Ctrl+Alt+W) |
| `tests/test_capture.py` | Modify | A2: add test for `name` field in manifest |
| `tests/test_cli.py` | Create | A3: test CLI dispatch |

---

## Chunk 1: A1 (button fix) + A2 (human-readable names)

### Task 1: Fix rating button L → R in Lua

No automated tests — Lua is tested manually in Mesen2.

**Files:**
- Modify: `lua/spinlab.lua`

- [ ] **Step 1: Update three lines in `lua/spinlab.lua`**

  Line 150 — comment:
  ```lua
  -- R+Left=again, R+Down=hard, R+Right=good, R+Up=easy
  ```

  Line 153 — guard condition:
  ```lua
  if not inp or not inp.r then
  ```

  Line 208 — overlay text:
  ```lua
      "R+< again  R+v hard  R+> good  R+^ easy",
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add lua/spinlab.lua
  git commit -m "fix(lua): switch rating button from L to R"
  ```

---

### Task 2: Add `name` field to manifest (capture.py)

**Files:**
- Modify: `python/spinlab/capture.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 1: Write failing test**

  In `tests/test_capture.py`, add:

  ```python
  def test_build_manifest_includes_name_field():
      entrance = {"event": "level_entrance", "level": 14, "room": 0,
                  "state_path": "/states/smw_cod_14_0.mss"}
      exit_    = {"event": "level_exit", "level": 14, "room": 0,
                  "goal": "normal_exit", "elapsed_ms": 4100}
      manifest = build_manifest([(entrance, exit_)], game_id="smw_cod", category="any%")
      split = manifest["splits"][0]
      assert "name" in split
      assert split["name"] == ""
  ```

- [ ] **Step 2: Run test to verify it fails**

  ```
  pytest tests/test_capture.py::test_build_manifest_includes_name_field -v
  ```
  Expected: FAIL — `KeyError: 'name'`

- [ ] **Step 3: Add `name` field to `build_manifest` in `capture.py`**

  In the `splits.append(...)` block (around line 60), add `"name": ""` as the first key:

  ```python
  splits.append(
      {
          "name": "",
          "id": split_id,
          "level_number": entr["level"],
          "room_id": entr["room"],
          "goal": ex["goal"],
          "state_path": entr.get("state_path"),
          "reference_time_ms": ex["elapsed_ms"],
      }
  )
  ```

- [ ] **Step 4: Run test to verify it passes**

  ```
  pytest tests/test_capture.py::test_build_manifest_includes_name_field -v
  ```
  Expected: PASS

- [ ] **Step 5: Run full test suite to check nothing broke**

  ```
  pytest
  ```
  Expected: all tests pass

- [ ] **Step 6: Commit**

  ```bash
  git add python/spinlab/capture.py tests/test_capture.py
  git commit -m "feat(capture): add name placeholder field to manifest splits"
  ```

---

### Task 3: Wire `name` → `description` in orchestrator

**Files:**
- Modify: `python/spinlab/orchestrator.py`

- [ ] **Step 1: Update `seed_db_from_manifest` to read `name` as description**

  In `orchestrator.py`, the `seed_db_from_manifest` function builds a `Split`. Change the `description` line:

  ```python
  # Before:
  # description=split.description  (was empty / not set from manifest)

  # After — in the Split(...) constructor call inside seed_db_from_manifest:
  split = Split(
      id=entry["id"],
      game_id=game_id,
      level_number=entry["level_number"],
      room_id=entry.get("room_id"),
      goal=entry["goal"],
      description=entry.get("name", ""),   # <-- add this line
      state_path=entry.get("state_path"),
      reference_time_ms=entry.get("reference_time_ms"),
  )
  ```

- [ ] **Step 2: Update stdout display to use description**

  In the practice loop print line (around line 192), change:

  ```python
  # Before:
  print(f"{status} {result['split_id']}  {rating.value}  "
        f"{result.get('time_ms', '?')}ms")

  # After:
  label = cmd.description if cmd.description else result["split_id"]
  print(f"{status} {label}  {rating.value}  "
        f"{result.get('time_ms', '?')}ms")
  ```

- [ ] **Step 3: Run full test suite**

  ```
  pytest
  ```
  Expected: all tests pass

- [ ] **Step 4: Commit**

  ```bash
  git add python/spinlab/orchestrator.py
  git commit -m "feat(orchestrator): show split name in stdout, seed description from manifest"
  ```

---

### Task 4: Update Lua overlay to show split description

**Files:**
- Modify: `lua/spinlab.lua`

The `practice_split.description` is already parsed from the TCP payload by `parse_practice_split` (line 144). Just update the display.

- [ ] **Step 1: In `draw_practice_overlay`, show description instead of goal**

  In `PSTATE_PLAYING`/`PSTATE_LOADING` branch, the current line draws `practice_split.goal`. Change to show description (with fallback):

  ```lua
  local label = (practice_split.description ~= "" and practice_split.description)
                or practice_split.id or "?"
  draw_text(2, 2,
    "[PRACTICE] " .. label
    .. " " .. ms_to_display(elapsed)
    .. " ref:" .. ref_str,
    0xFFFFFF, 0x000000)
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add lua/spinlab.lua
  git commit -m "feat(lua): show split name in overlay"
  ```

---

## Chunk 2: A3 — CLI entry point + AHK hotkey

### Task 5: Create `cli.py`

**Files:**
- Create: `python/spinlab/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

  Create `tests/test_cli.py`:

  ```python
  """Tests for CLI dispatch."""
  from unittest.mock import patch
  import pytest
  from spinlab.cli import main


  def test_stats_subcommand_prints_stub(capsys):
      with pytest.raises(SystemExit) as exc:
          main(["stats"])
      assert exc.value.code == 0
      captured = capsys.readouterr()
      assert "Stats coming in a future step" in captured.out


  def test_unknown_subcommand_exits_nonzero():
      with pytest.raises(SystemExit) as exc:
          main(["notacommand"])
      assert exc.value.code != 0


  def test_practice_calls_orchestrator_run():
      # Smoke test: orchestrator.run is accessible from cli
      from spinlab import orchestrator
      assert hasattr(orchestrator, "run")


  def test_capture_calls_capture_main():
      from spinlab import capture
      assert hasattr(capture, "main")
  ```

- [ ] **Step 2: Run tests to verify they fail**

  ```
  pytest tests/test_cli.py -v
  ```
  Expected: FAIL — `ModuleNotFoundError: No module named 'spinlab.cli'`

- [ ] **Step 3: Create `python/spinlab/cli.py`**

  ```python
  """SpinLab CLI entry point."""
  from __future__ import annotations

  import argparse
  import sys
  from pathlib import Path


  def main(args: list[str] | None = None) -> None:
      parser = argparse.ArgumentParser(
          prog="spinlab",
          description="SpinLab — spaced repetition practice for SNES speedrunning",
      )
      sub = parser.add_subparsers(dest="command", required=True)

      # practice
      p_practice = sub.add_parser("practice", help="Start a practice session")
      p_practice.add_argument(
          "--config", default="config.yaml", help="Path to config.yaml"
      )

      # capture
      sub.add_parser("capture", help="Process passive log into a manifest")

      # stats
      sub.add_parser("stats", help="Show practice statistics (coming soon)")

      parsed = parser.parse_args(args)

      if parsed.command == "practice":
          from spinlab import orchestrator
          orchestrator.run(Path(parsed.config))

      elif parsed.command == "capture":
          from spinlab.capture import main as capture_main
          capture_main()

      elif parsed.command == "stats":
          print("Stats coming in a future step.")
          sys.exit(0)


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests to verify they pass**

  ```
  pytest tests/test_cli.py -v
  ```
  Expected: PASS

- [ ] **Step 5: Run full test suite**

  ```
  pytest
  ```
  Expected: all tests pass

- [ ] **Step 6: Add `[project.scripts]` to `pyproject.toml`**

  Add after `[project.optional-dependencies]`:

  ```toml
  [project.scripts]
  spinlab = "spinlab.cli:main"
  ```

- [ ] **Step 7: Reinstall package to pick up the new entry point**

  ```
  pip install -e ".[dev]"
  ```

- [ ] **Step 8: Smoke-test the entry point**

  ```
  spinlab stats
  ```
  Expected output: `Stats coming in a future step.`

  ```
  spinlab --help
  ```
  Expected: shows subcommands list.

- [ ] **Step 9: Commit**

  ```bash
  git add python/spinlab/cli.py pyproject.toml tests/test_cli.py
  git commit -m "feat(cli): add spinlab entry point with practice/capture/stats subcommands"
  ```

---

### Task 6: Create AHK toggle script

**Files:**
- Create: `scripts/spinlab.ahk`

No automated tests — AHK is tested manually.

- [ ] **Step 1: Create `scripts/spinlab.ahk`**

  ```autohotkey
  #Requires AutoHotkey v2.0
  #SingleInstance Force

  global spinlabPID := 0

  ; Ctrl+Alt+W — toggle SpinLab practice session
  ^!w:: {
      global spinlabPID
      if (spinlabPID != 0 && ProcessExist(spinlabPID)) {
          ; Session is running — kill it (hard kill; session ended_at will be NULL)
          Run "taskkill /PID " spinlabPID " /F",, "Hide"
          spinlabPID := 0
          ToolTip "SpinLab stopped"
          SetTimer () => ToolTip(), -2000
      } else {
          ; Start a new session in a minimised cmd window
          Run 'cmd /c spinlab practice', '', 'Min', &spinlabPID
          ToolTip "SpinLab started (PID " spinlabPID ")"
          SetTimer () => ToolTip(), -2000
      }
  }
  ```

  > Note: requires AutoHotkey v2 installed and `spinlab` on PATH (installed via `pip install -e .`). The hard-kill means `sessions.ended_at` will be NULL — acceptable until a graceful shutdown mechanism is added.

- [ ] **Step 2: Commit**

  ```bash
  git add scripts/spinlab.ahk
  git commit -m "feat(ahk): add Ctrl+Alt+W toggle hotkey for practice sessions"
  ```

---

## Chunk 3: A4 — Overlay visual polish

### Task 7: Redesign `draw_practice_overlay` in Lua

No automated tests — visual output must be verified in Mesen2.

**Files:**
- Modify: `lua/spinlab.lua`

**Design:**
- Semi-transparent dark background box behind all text
- Row 1 (y=6): split name — muted gray
- Row 2 (y=24): `elapsed / reference` — green if ahead, red if behind
- Row 3 (y=42, rating state only): `R+< again  R+v hard  R+> good  R+^ easy` — yellow

Colors use `0xAARRGGBB` format:
- Box background: `0xCC000000`
- Split name: `0xFFCCCCCC`
- Timer ahead (elapsed < ref): `0xFF44FF44`
- Timer behind (elapsed >= ref): `0xFFFF4444`
- Timer (no reference): `0xFFFFFFFF`
- Rating prompt: `0xFFFFFF00`

Box dimensions: x=0, y=0, width=246, height=62 (covers all 3 rows with margin).

- [ ] **Step 1: Replace `draw_practice_overlay` in `lua/spinlab.lua`**

  Find the existing `draw_practice_overlay` function (lines 189–211) and replace it entirely:

  ```lua
  local function draw_practice_overlay()
    if not practice_mode then return end

    -- Background box (covers all rows)
    emu.drawRectangle(0, 0, 246, 62, 0xCC000000, true, 1)

    local label = (practice_split and practice_split.description ~= "" and practice_split.description)
                  or (practice_split and practice_split.id) or "?"

    if practice_state == PSTATE_PLAYING or practice_state == PSTATE_LOADING then
      local elapsed = ts_ms() - practice_start_ms
      local ref     = practice_split.reference_time_ms

      -- Row 1: split name
      draw_text(4, 6, label, 0xFFCCCCCC, 0x00000000)

      -- Row 2: timer / reference, color-coded
      local timer_color
      if ref then
        timer_color = (elapsed < ref) and 0xFF44FF44 or 0xFFFF4444
      else
        timer_color = 0xFFFFFFFF
      end
      local ref_str = ref and ms_to_display(ref) or "?"
      draw_text(4, 24, ms_to_display(elapsed) .. " / " .. ref_str, timer_color, 0x00000000)

    elseif practice_state == PSTATE_RATING then
      local prefix = practice_completed and "Clear!" or "Abort"

      -- Row 1: split name
      draw_text(4, 6, label, 0xFFCCCCCC, 0x00000000)

      -- Row 2: result time / reference (mirrors PLAYING layout)
      local ref = practice_split.reference_time_ms
      local timer_color
      if ref then
        timer_color = (practice_elapsed_ms < ref) and 0xFF44FF44 or 0xFFFF4444
      else
        timer_color = 0xFFFFFFFF
      end
      local ref_str2 = ref and ms_to_display(ref) or "?"
      draw_text(4, 24, prefix .. "  " .. ms_to_display(practice_elapsed_ms) .. " / " .. ref_str2, timer_color, 0x00000000)

      -- Row 3: rating prompt
      draw_text(4, 42, "R+< again  R+v hard  R+> good  R+^ easy", 0xFFFFFF00, 0x00000000)
    end
  end
  ```

  Also update the passive-mode indicator at the bottom of `on_start_frame` (line ~520) — currently `draw_text(2, 2, "SpinLab", ...)`. Move it out of the way of the practice overlay (it only shows when `not practice_mode`, so no conflict, but tidy it up):

  ```lua
  if not practice_mode then
    draw_text(2, 2, "SpinLab", 0xFF888888, 0x00000000)
  end
  ```

  (Just change the color from `0xFFFFFF` to `0xFF888888` — subtler when not practicing.)

- [ ] **Step 2: Commit**

  ```bash
  git add lua/spinlab.lua
  git commit -m "feat(lua): redesign practice overlay with background, color-coded timer, layout"
  ```

---

## Manual verification checklist (do after all tasks)

After all code changes are in, test end-to-end in Mesen2:

- [ ] Load Mesen2, run the Lua script
- [ ] Start `spinlab practice` via AHK (Ctrl+Alt+W) — verify a cmd window opens
- [ ] Verify overlay shows split name (not raw ID) and color-coded timer
- [ ] Complete a split — verify rating prompt shows `R+` prefix, not `L+`
- [ ] Rate with R+right (good) — verify result logged in orchestrator stdout with name
- [ ] Press Ctrl+Alt+W again — verify process is killed
- [ ] Check `spinlab --help` and `spinlab stats` in a plain terminal

---

## Notes

- `emu.drawRectangle` signature in Mesen2: `emu.drawRectangle(x, y, width, height, color, filled, duration)`. Use `filled=true`, `duration=1`.
- Background color `0x00000000` (transparent) is used for `draw_text` bg parameter so text background doesn't paint over the rect.
- The `name` field in the manifest must be hand-edited in the YAML after capture. A future CLI command will let you set names interactively.
- AHK hard-kill means `sessions.ended_at` stays NULL. To get a clean session end, use Ctrl+C in the cmd window instead.
