# SpinLab Roadmap & Step A Design

## Full Roadmap

### A — Quick wins (this spec)
1. L2 button fix in Lua
2. Human-readable split names (manifest → display)
3. `spinlab practice` CLI entry point + AHK start/stop hotkey
4. Overlay visual polish

### B — Loop quality
5. Best time tracked in DB, shown on overlay and output
6. Session goal (time-limited or count-limited)

### C — Reference run architecture (big structural piece)
7. Maximal save stating: capture at `entrance`, `checkpoint`, `goal` events
8. Segment windows: practice segment = `(start_point, end_condition)` pair
9. Config (YAML-first, UI later) to select which segments to practice
10. Multiple reference runs / routes per game+category

### D — Stream / display
11. Stats TUI or browser overlay, using real data from B+C

---

## Step A Design

### A1 — L2 button fix

**Current:** rating combos use `l` (L button) + d-pad.
**Change:** switch to `r` (R / right shoulder) + d-pad.

In `lua/spinlab.lua`, update:

1. The guard condition `inp.l` → `inp.r` in the rating input check
2. The rating prompt overlay text (`"L+< again..."` → `"R+< again..."`)
3. The comment above listing the combos

> Note: Mesen2 input field names from `emu.getInput(0)`: `l`, `r`, `up`, `down`, `left`, `right`, `a`, `b`, `x`, `y`, `start`, `select`. `r` = right shoulder = L2 on SNES.

### A2 — Human-readable split names

**Problem:** split IDs are `smw_cod:14:0:normal_exit` — meaningless on display.

**Design:**
- Add optional `name` field to manifest YAML entries (e.g. `"Yoshi's Island 1"`)
- `capture.py` needs to be updated to write `name: ""` placeholder for each split entry
- `db.py`: `splits.description` column already exists — populate it from `name` during `seed_db_from_manifest`
- Display: orchestrator stdout, Lua overlay — show `description` when non-empty, fall back to split ID

**Manifest entry example:**
```yaml
splits:
  - id: smw_cod:14:0:normal_exit
    name: "Yoshi's Island 1"
    level_number: 14
    room_id: 0
    goal: normal_exit
    state_path: ...
    reference_time_ms: 4100
```

Short-term: names are hand-edited in the manifest YAML. A future CLI command can set them interactively.

### A3 — CLI entry point + AHK hotkey

**CLI entry point:**

Add a `spinlab` console script via `pyproject.toml`:

```toml
[project.scripts]
spinlab = "spinlab.cli:main"
```

`cli.py` — minimal at this stage:
```
spinlab practice          # starts orchestrator.run()
spinlab capture           # runs capture.py
spinlab stats             # stub for Step D
```

Use `argparse` (no heavy deps). `practice` subcommand passes `--config` path option. `stats` stub prints `"Stats coming in a future step."` and exits 0.

**AHK hotkey (Windows):**

A short `scripts/spinlab.ahk` script:

- `Ctrl+Alt+W` → launches `spinlab practice` in a new `cmd` window via `Run`
- Same hotkey again → kills the process via `taskkill /PID <pid> /F`

Simple toggle: store PID after spawn. If PID is set and process is running → taskkill; else → spawn and store new PID. Use `WinExist` or `Process, Exist` to confirm before killing. Note: this sends a hard kill, not Ctrl+C — `orchestrator.py` writes the session end on `KeyboardInterrupt`, so the session's `ended_at` will be NULL on force-kill. Acceptable for now.

### A4 — Overlay visual polish

**Current problems:**
- Text is small and hard to read
- No clear visual hierarchy (split name vs timer vs rating prompt look the same)
- Colors are flat / ugly

**Design — overlay layout:**

```
┌─────────────────────────────────┐
│ Yoshi's Island 1    [good 4.1s] │   ← split name + last result (top bar)
│                                 │
│         3.8s / 4.1s             │   ← current time / reference time (center)
│                                 │
│   < again  v hard  > good  ^ easy   │  ← rating prompt (bottom, only when waiting)
└─────────────────────────────────┘
```

**Implementation:**
- Top bar: split name left, last result right — smaller font, muted color
- Timer: large, centered, color-coded (green = ahead of ref, red = behind)
- Rating row: only visible during post-completion liminal state
- Background: semi-transparent dark rectangle behind all text
- Use `emu.drawRectangle` for the background box, then `draw_text` per row

**Colors (approximate hex):**
- Background box: `0xCC000000` (black 80% opacity)
- Split name: `0xFFCCCCCC` (light gray)
- Timer ahead: `0xFF44FF44` (green)
- Timer behind: `0xFFFF4444` (red)
- Rating prompt: `0xFFFFFF00` (yellow)

---

## Dependencies & notes

- A1 has no deps, do it first
- A2 requires editing a manifest YAML by hand for the first real session — that's fine
- A3 requires `cli.py` to exist (new file) and `pyproject.toml` update
- A4 is Lua-only, no Python changes
- None of A1–A4 touch the DB schema
- None require a new reference capture run
