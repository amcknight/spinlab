# RetroArch Migration Spike Log

Chronological log of config changes, probes, and findings during Phase 0 + Phase 2 testing. Source-of-truth for what was changed in `C:\RetroArch-Win64\retroarch.cfg` and other system state, so changes can be reverted or replayed.

Format per entry: timestamp · what · why · revert step.

---

## Config changes

### 2026-05-06 (early session)

- **Changed:** `network_cmd_enable = "false"` → `"true"` in `C:/RetroArch-Win64/retroarch.cfg` line 3075.
- **Why:** Required to send any UDP commands to RetroArch (NCI handshake, memory reads).
- **Revert:** Set back to `"false"` and restart RetroArch.
- **Side effects:** RA listens on UDP 55355 (configured on line 3076, default).

### 2026-05-06 (state observations, no change made)

- `state_slot = "484"` (as found, line 3263) — pre-existing high value from prior testing.
- `savestate_auto_index = "true"` (line 3218) — **explains the "savestate round-trip failure" in the spike.** Each `SAVE_STATE` increments the slot, so `LOAD_STATE` reads from a different slot than was just saved. Working as designed, just incompatible with naive save-then-load.
- `savestate_directory = ":\states"` (line 3221) — i.e. `C:/RetroArch-Win64/states/`. RA per-core subdir: `states/Snes9x/Toothpaste.state<N>`.

---

## Probes / experiments

### 2026-05-06 — Spike 1 (`scripts/spike_retroarch.py`)

- Steps 1–4 PASS (NCI alive, memory read, 60Hz polling, runahead coexistence).
- Step 5 (savestate round-trip) initially FAIL. Diagnosis: auto-index splitting save/load slots — see config note above. Confirmed by inspecting `states/Snes9x/`: `Toothpaste.state485` was created at the spike's SAVE_STATE moment, while LOAD_STATE was reading current-slot which had advanced past it.

### 2026-05-06 — Probe 2 (`c:/tmp/probe_write.py`)

Game state at probe time: stuck at Retry screen (Mario dead, physics frozen).

- **WRITE_CORE_RAM works.** Wrote `AA BB` to `$7E0094` (Mario X). Read back returned `AA BB` immediately, 50ms later, 500ms later — the value persisted because Retry screen has Mario's physics frozen. Confirms basic write primitive.
- **PAUSE_TOGGLE works.** `aa bb` survived pause→write→unpause→read.
- **Input injection via WRITE to `$7E0015` / `$7E0016` FAILS.** Wrote `0x10` (START bit) to both controller-held and pressed-this-frame addresses. Read back `00` immediately. SMW's NMI handler is reading the auto-joypad register (`$4218`) and clobbering `$7E0015`+ each frame; our writes don't survive one round-trip. Direct WRITE_CORE_RAM cannot pre-empt the NMI.

**Implications:**
- Read/write memory + pause + savestate are all usable from NCI. That's enough for the practice loop's hot path.
- Input injection requires either **Network RetroPad** (UDP virtual controller, needs `network_remote_enable = "true"` in cfg + RA restart) or **BSV movie playback**. Neither is necessary for the *live* practice loop (user's real controller drives the game); both are options for *replay*.
- **Side effect of probe:** `$7E0094`/`$7E0095` left with `AA BB` written. Will be cleared automatically on next level transition / retry — no cleanup needed.

### 2026-05-06 — Probe 4 (`scripts/probe_input_dispatch.py`)

Goal: settle the NCI hotkey-sim failure. Smoke test pre-test pattern: NCI memory R/W and queries work, NCI hotkey-sim (SAVE_STATE, MENU_TOGGLE, PAUSE_TOGGLE, LOAD_STATE_SLOT) all silently fail. Game Focus is OFF (manual F1 opens the menu), keyboard hotkeys work, manual gamepad save works.

**Pre-test cfg changes (made, RA restart required):**

- `cheevos_hardcore_mode_enable = "true"` → `"false"` (line 1373 in retroarch.cfg). Default-after-restart: `false`.
  - Why: rule out cheevos hardcore mode as the gate on NCI save-state. Even though `cheevos_enable = false`, RA source has hardcore checks that may apply to NCI-routed commands.
  - Revert: set back to `"true"` and restart.
- `network_remote_enable = "false"` → `"true"` (line 3079).
  - Why: enable Network RetroPad as a fallback path for input injection if NCI hotkey-sim is permanently broken.
  - Revert: set back to `"false"` and restart.
- `network_remote_enable_user_p1 = "false"` → `"true"` (line 3080).
  - Why: enable Player 1 for Network RetroPad reception.
  - Revert: set back to `"false"` and restart.
- `run_ahead_enabled = "true"` → `"false"` (line ~3203, search for it).
  - Why: rule out runahead as a confound. We can re-enable later once we know whether it interacts with the save-state path.
  - Revert: set back to `"true"` and restart.

**Network RetroPad protocol notes (from RA source):**

- UDP, port 55400 (configurable: `network_remote_base_port`).
- Packet format: `struct remote_message { int port; int device; int index; int id; uint16_t state; }` with C struct padding → 20 bytes total on x64.
- `device = 1` (RETRO_DEVICE_JOYPAD), `id` = button index (0=B, 1=Y, 2=SELECT, 3=START, 4=UP, 5=DOWN, 6=LEFT, 7=RIGHT, 8=A, 9=X, 10=L, 11=R), `state` = 1 (pressed) or 0 (released).
- Note: NRP IDs are RetroPad-layout; user's `input_save_state_btn = "4"` is raw HID button index — they may not align without a re-binding to a RetroPad button id.

### 2026-05-06 — Probe 3 (`c:/tmp/probe_slots.py` and follow-up)

Game state: stuck at SMW Retry screen (game mode `$7E0100 = 0x14`), Mario dead, no user at keyboard.

- `SAVE_STATE` reply was `None` and **no new file appeared** in `states/Snes9x/`. Latest file remained `Toothpaste.state485` (created at 18:47, earlier in this session). Different from earlier behavior in the same session where SAVE_STATE *did* produce state485.
- Diagnosed: emulator core is frozen. Frame counter at `$7E0013` did not advance over 1+ second of polling. Compared 256 bytes of WRAM across 0.5s — bit-identical. RA's NCI service is responsive (`VERSION`, `GET_STATUS`, `READ_CORE_RAM` all work) but the core thread has stalled.
- `GET_STATUS` reports `PLAYING super_nes,Toothpaste,crc32=41b3c49d` — i.e. RA *thinks* it's running, but isn't.
- Tried unsticking via `PAUSE_TOGGLE` (single, doubled), `MENU_TOGGLE` (single, doubled), `FRAMEADVANCE` (×5), and combinations. Frame counter stayed at `0x6D` throughout. RA is unrecoverable via NCI alone.
- Likely cause: window focus / OS-level interaction. `pause_nonactive = "false"` is set in cfg, so this isn't the documented auto-pause-on-focus-loss feature, but some interaction during the earlier `PAUSE_TOGGLE` round-trips left RA in a "deep pause" state that NCI commands can't toggle out of.

**Recovery needed:** Andrew clicks the RA window when next at the machine, or restarts RA.

**Implication for SpinLab design:** Don't `PAUSE_TOGGLE` blindly during automation. Either always `GET_STATUS` first to confirm state, or use a different pause primitive (e.g. set a flag the practice loop respects, leaving RA always running). Document this gotcha in Phase B's NCI client.

### Open probe queue

Blocked until RA is responsive again:
- Slot save/load probe (`SAVE_STATE`, `LOAD_STATE_SLOT N`, `STATE_SLOT_PLUS/MINUS`).
- Confirm `SAVE_STATE_SLOT N` exists vs. doesn't (would let SpinLab use a reserved high slot range without disturbing the user's manual sequential saves).

Pending RA restart:
- Network RetroPad — requires `network_remote_enable = "true"` + at least `network_remote_enable_user_p1 = "true"` on lines 3079–3080 of `retroarch.cfg`. Default port `55400`. Cfg edit is safe to do at any time; takes effect on next RA launch. **Not yet edited** — awaiting Andrew's go-ahead per his "log all config changes" rule.

Deferred:
- BSV record/playback feasibility.
