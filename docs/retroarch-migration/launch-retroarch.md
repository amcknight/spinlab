# Launch workflow: SpinLab + RetroArch

Manual reference for running SpinLab against RetroArch instead of Mesen-Lua.

---

## Required RetroArch settings

Edit `C:\RetroArch-Win64\retroarch.cfg` and confirm (or set) these keys, then restart RetroArch:

```
network_cmd_enable = "true"          # NCI must be on; default port 55355
cheevos_hardcore_mode_enable = "false"  # REQUIRED — hardcore mode silently gates
                                        # NCI commands (SAVE_STATE, LOAD_STATE_SLOT,
                                        # PAUSE_TOGGLE) even when cheevos_enable = false
run_ahead_enabled = "true"           # the reason for this migration
runahead_frames = "3"                # 3 is a sensible default; tune to taste
```

`savestate_directory` — note the exact path RA writes slot files to.
Whatever it is, `emulator.savestate_dir` in `config.yaml` must match exactly.
Default after a fresh install: `C:/RetroArch-Win64/states/`.
RA also creates a per-core sub-directory; for the snes9x core the full path is
`<savestate_directory>/Snes9x/`.

**Core:** snes9x_libretro only. Other cores are untested and `READ_CORE_RAM`
returns "no memory map defined" on non-snes9x cores.

See the [spike log](spike-log.md) for the Phase B probe that settled these
requirements, and the [Phase B/C design spec](../superpowers/specs/2026-05-06-retroarch-migration-design.md)
for the full decision record.

---

## Required `config.yaml` keys for the RetroArch backend

```yaml
emulator:
  backend: retroarch
  retroarch_path: "C:/RetroArch-Win64/retroarch.exe"
  savestate_dir: "C:/RetroArch-Win64/states/Snes9x"
  spinlab_state_dir: "data/spinlab_states"
  ra_game_basename: "Toothpaste World"  # must match RA's slot filename prefix

network:
  nci_port: 55355  # default; matches RA's network_cmd_port

# All other keys (rom, game, scheduler, data) stay as before.
```

**`ra_game_basename`** is the prefix RA uses in slot filenames:
`<basename>.state0`, `<basename>.state1`, etc.
For a ROM named `Toothpaste World.smc`, RA writes `Toothpaste World.state0`,
`Toothpaste World.state1`, and so on.
Verify by manually saving a state in RA once and checking the filename in
`savestate_dir` — then set `ra_game_basename` to that prefix exactly.

---

## Step-by-step launch workflow

### 1. Start RetroArch with the ROM

Command-line launch (snes9x core path is an example):

```
"C:/RetroArch-Win64/retroarch.exe" -L "C:/RetroArch-Win64/cores/snes9x_libretro.dll" "<path-to-rom>"
```

Or open RetroArch normally and load the ROM via the menu.

### 2. Confirm NCI is alive

```
python scripts/probe_cp_entrance.py
```

Should print something like `RA version: 1.22.x` and start polling.
Ctrl-C to exit. If it hangs or errors, see Troubleshooting below.

### 3. Start the SpinLab dashboard

```
spinlab dashboard --config config.yaml
```

The `spinlab` CLI entry point is registered by the package (`pyproject.toml`
`[project.scripts]`). If it is not on PATH after editable install, use:

```
python -m spinlab.cli dashboard --config config.yaml
```

### 4. Open the dashboard

Navigate to the URL printed by the startup log — typically
`http://localhost:15483`.

### 5. Confirm connection

Check the dashboard log for:

- `RetroArchOrchestrator connected`
- A `rom_info` event with the game name

If these appear, SpinLab is wired to RetroArch and ready.

---

## Reserved slot 9999

SpinLab uses RA slot **9999** for its swap operations (Phase D Decision 6).
A file `<game>.state9999` will appear in `savestate_dir`. Do not manually save
to slot 9999. All other slots are yours.

---

## Known limitations during Phase F-live

- **No record/replay against RetroArch.** That is Phase E (BSV). To capture
  reference runs, switch to `emulator.backend: mesen-lua` temporarily.
- **No in-game L+Select invalidate combo.** NCI cannot pre-empt SMW's NMI
  controller handler (confirmed in Phase B, Probe 2). Use the dashboard
  "Invalidate" button (`POST /api/practice/invalidate`) instead.
- **Speed-run timing is a minimal port.** Verify behavior against the Lua
  reference during smoke testing; surface any mismatches as followups.
  See [lua-audit.md](lua-audit.md) for the full port checklist.
- **`ADDR_CP_ENTRANCE = 0x1B403`** is verified for hacks without ASM checkpoint
  patches. If you use a hack with ASM checkpoints and detection misfires, file
  a followup (per Phase C closeout).

---

## Troubleshooting

**NCI not responding.**
Confirm RA is running. Check `network_cmd_enable = "true"` in `retroarch.cfg`.
Run `python scripts/probe_cp_entrance.py`. If `READ_CORE_RAM` returns
"no memory map defined", you are on a non-snes9x core — switch cores.

**"Deep pause" symptom.**
RA core stops advancing frames but NCI still answers queries. Restart RA.
This is a known spike-log issue (Probe 3); surfaces rarely during normal use.
Do not try to recover via `PAUSE_TOGGLE` — that is what triggers the stuck state.

**Savestate dir mismatch.**
Loading a state appears to do nothing. Check that `emulator.savestate_dir`
exactly matches RA's `savestate_directory` cfg value (including any per-core
sub-directory). Also verify `ra_game_basename` matches RA's actual filename
prefix — save a state manually and check `savestate_dir` to confirm.

**Slot 9999 collision.**
If `<game>.state9999` appears as a recent file you did not make, that is
SpinLab. Ignore it.

**Practice loop never advances.**
If the dashboard sits at "loading" forever, the orchestrator's poller may not
be detecting events. Move in-game manually; check the dashboard log for
`level_entrance`, `death`, or similar events. If nothing appears, confirm the
correct backend is set in `config.yaml`.
