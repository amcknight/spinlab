-- death_spike.lua — Experiment: can we force a death via memory poke?
--
-- Usage: Load this script in Mesen2 while a romhack is running.
--        Play until you hit a checkpoint (midway tape), then press:
--          F5 = Save test state (at checkpoint, "hot" state)
--          F6 = Load test state + poke player_anim=9 after 30 frames
--          F7 = Load test state + poke Y-position below kill plane after 30 frames
--          F8 = Load test state + poke lives=0 then player_anim=9 (game-over test)
--
-- Watch the Mesen2 log window for results.

local SNES = emu.memType.snesMemory

-----------------------------------------------------------------------
-- SMW ADDRESSES
-----------------------------------------------------------------------
local ADDR_PLAYER_ANIM = 0x0071   -- 9 = death animation
local ADDR_LEVEL_START = 0x1935   -- 0→1 on level entry
local ADDR_PLAYER_Y    = 0x0096   -- player Y position (low byte)
local ADDR_PLAYER_Y_HI = 0x0097  -- player Y position (high byte)
local ADDR_LIVES       = 0x0DBE  -- lives counter
local ADDR_MIDWAY      = 0x13CE  -- midway checkpoint flag
local ADDR_GAME_MODE   = 0x0100  -- 18=prepare, 20=in level

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local test_state_path = "spike_test.mss"  -- save to file (like spinlab does)
local has_saved_state = false
local poke_pending = nil          -- {type="anim"|"ypos"|"gameover"|"auto"|"hold_y", frame=N}
local frame_counter = 0
local monitoring = false          -- true after a poke, watching for respawn
local auto_retry = false          -- when true, inject A presses during retry textbox
local hold_y_until = 0            -- keep poking Y below kill plane until this frame

-- Deferred queues (executed in cpuExec, like spinlab)
local pending_save = false
local pending_load = false

-- Snapshot of key addresses before poke
local pre_poke = {}

local function read_state()
  return {
    player_anim = emu.read(ADDR_PLAYER_ANIM, SNES, false),
    level_start = emu.read(ADDR_LEVEL_START, SNES, false),
    player_y    = emu.read(ADDR_PLAYER_Y, SNES, false)
                + emu.read(ADDR_PLAYER_Y_HI, SNES, false) * 256,
    lives       = emu.read(ADDR_LIVES, SNES, false),
    midway      = emu.read(ADDR_MIDWAY, SNES, false),
    game_mode   = emu.read(ADDR_GAME_MODE, SNES, false),
  }
end

local function log_state(label, s)
  emu.log(string.format("[Spike] %s: anim=%d level_start=%d y=%d lives=%d midway=%d mode=%d",
    label, s.player_anim, s.level_start, s.player_y, s.lives, s.midway, s.game_mode))
end

-----------------------------------------------------------------------
-- FILE-BASED SAVE/LOAD (must run inside cpuExec)
-----------------------------------------------------------------------
local function do_save()
  local data = emu.createSavestate()
  if not data then
    emu.log("[Spike] ERROR: createSavestate returned nil")
    return
  end
  -- Write next to this script
  local script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
  local path = script_dir .. test_state_path
  local f = io.open(path, "wb")
  if not f then
    emu.log("[Spike] ERROR: Could not open file: " .. path)
    return
  end
  f:write(data)
  f:close()
  has_saved_state = true
  local s = read_state()
  emu.log("[Spike] === STATE SAVED to " .. path .. " ===")
  log_state("Saved", s)
end

local function do_load()
  local script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
  local path = script_dir .. test_state_path
  local f = io.open(path, "rb")
  if not f then
    emu.log("[Spike] ERROR: Could not open file: " .. path)
    return
  end
  local data = f:read("*a")
  f:close()
  emu.loadSavestate(data)
  emu.log("[Spike] State loaded from " .. path .. ", will poke in 30 frames")
end

-----------------------------------------------------------------------
-- KEYBOARD (with pcall guard for testRunner mode)
-----------------------------------------------------------------------
local function key_pressed(k)
  local ok, val = pcall(emu.isKeyPressed, k)
  return ok and val
end

local key_was = {}
local function key_just_pressed(k)
  local down = key_pressed(k)
  local was = key_was[k]
  key_was[k] = down
  return down and not was
end

-----------------------------------------------------------------------
-- MONITORING: watch what happens after a poke
-----------------------------------------------------------------------
local monitor_start_frame = 0
local prev_state = nil

local function start_monitoring(poke_type)
  monitoring = true
  monitor_start_frame = frame_counter
  prev_state = read_state()
  pre_poke = read_state()
  emu.log("[Spike] === MONITORING START (" .. poke_type .. ") ===")
  log_state("Pre-poke", pre_poke)
end

local function monitor_frame()
  if not monitoring then return end

  local curr = read_state()
  local elapsed = frame_counter - monitor_start_frame

  -- Log interesting transitions
  if curr.player_anim ~= prev_state.player_anim then
    emu.log(string.format("[Spike] +%df: anim %d -> %d", elapsed, prev_state.player_anim, curr.player_anim))
  end
  if curr.level_start ~= prev_state.level_start then
    emu.log(string.format("[Spike] +%df: level_start %d -> %d", elapsed, prev_state.level_start, curr.level_start))
  end
  if curr.lives ~= prev_state.lives then
    emu.log(string.format("[Spike] +%df: lives %d -> %d", elapsed, prev_state.lives, curr.lives))
  end
  if curr.game_mode ~= prev_state.game_mode then
    emu.log(string.format("[Spike] +%df: game_mode %d -> %d", elapsed, prev_state.game_mode, curr.game_mode))
  end
  if curr.midway ~= prev_state.midway then
    emu.log(string.format("[Spike] +%df: midway %d -> %d", elapsed, prev_state.midway, curr.midway))
  end

  -- Detect respawn: either level_start 0→1 (normal) or game_mode returning
  -- to 20 after leaving it (fast retry stays in-level)
  local respawn_detected = false
  if prev_state.level_start == 0 and curr.level_start == 1 and elapsed > 10 then
    respawn_detected = true
  end
  -- Fast retry: game_mode goes 20→15→16→17→18→20, level_start may stay 1
  if prev_state.game_mode ~= 20 and curr.game_mode == 20 and elapsed > 10 then
    respawn_detected = true
  end
  if respawn_detected then
    emu.log("[Spike] === RESPAWN DETECTED ===")
    log_state("Post-respawn", curr)
    emu.log(string.format("[Spike] Lives delta: %d -> %d (diff: %d)",
      pre_poke.lives, curr.lives, curr.lives - pre_poke.lives))
    emu.log(string.format("[Spike] Midway preserved: %s (was %d, now %d)",
      tostring(curr.midway == pre_poke.midway), pre_poke.midway, curr.midway))
    emu.log(string.format("[Spike] Total frames to respawn: %d (~%.1fs)",
      elapsed, elapsed / 60.0))
    emu.log("[Spike] === MONITORING COMPLETE ===")
    monitoring = false
    auto_retry = false
  end

  -- Timeout after 1800 frames (30 seconds)
  if elapsed > 1800 then
    emu.log("[Spike] === MONITORING TIMEOUT (no respawn in 30s) ===")
    log_state("Final state", curr)
    monitoring = false
  end

  prev_state = curr
end

-----------------------------------------------------------------------
-- CPU EXEC CALLBACK (save/load must happen here)
-----------------------------------------------------------------------
emu.addMemoryCallback(function()
  if pending_save then
    pending_save = false
    do_save()
  end
  if pending_load then
    pending_load = false
    do_load()
  end
end, emu.callbackType.exec, 0x0000, 0xFFFF)

-----------------------------------------------------------------------
-- FRAME CALLBACK (keyboard, poke timing, monitoring)
-----------------------------------------------------------------------
emu.addEventCallback(function()
  frame_counter = frame_counter + 1

  -- Hold Y below kill plane for multiple frames if active
  if hold_y_until > 0 and frame_counter <= hold_y_until then
    emu.write(ADDR_PLAYER_Y, 0x60, SNES)       -- low byte  (0x260 = 608)
    emu.write(ADDR_PLAYER_Y_HI, 0x02, SNES)    -- high byte
  end

  -- Handle pending poke (delay 30 frames after load for state to settle)
  if poke_pending and frame_counter >= poke_pending.frame then
    local t = poke_pending.type
    poke_pending = nil

    if t == "anim" then
      emu.log("[Spike] POKING: player_anim = 9")
      emu.write(ADDR_PLAYER_ANIM, 9, SNES)
      start_monitoring("player_anim=9")

    elseif t == "ypos" then
      emu.log("[Spike] POKING: player Y = 512 (below kill plane)")
      emu.write(ADDR_PLAYER_Y, 0, SNES)
      emu.write(ADDR_PLAYER_Y_HI, 2, SNES)
      start_monitoring("y=512")

    elseif t == "hold_y" then
      emu.log("[Spike] HOLDING: player Y = 608 for 60 frames + A-mash")
      hold_y_until = frame_counter + 60
      auto_retry = true
      emu.write(ADDR_PLAYER_Y, 0x60, SNES)
      emu.write(ADDR_PLAYER_Y_HI, 0x02, SNES)
      start_monitoring("hold_y=608 x60f + A-mash")

    elseif t == "auto" then
      -- Use a gentler Y value — just below visible screen, not a huge teleport
      -- SMW visible area is ~224px; screen Y + 240 should trigger kill plane
      local curr_y = emu.read(ADDR_PLAYER_Y, SNES, false)
                   + emu.read(ADDR_PLAYER_Y_HI, SNES, false) * 256
      local kill_y = curr_y + 256  -- push well below current position
      emu.log(string.format("[Spike] POKING: player Y = %d -> %d (auto-retry enabled)", curr_y, kill_y))
      emu.write(ADDR_PLAYER_Y, kill_y % 256, SNES)
      emu.write(ADDR_PLAYER_Y_HI, math.floor(kill_y / 256), SNES)
      start_monitoring("auto: y+" .. kill_y)

    elseif t == "gameover" then
      emu.log("[Spike] POKING: lives=0 then player_anim=9")
      emu.write(ADDR_LIVES, 0, SNES)
      emu.write(ADDR_PLAYER_ANIM, 9, SNES)
      start_monitoring("lives=0+anim=9")
    end
  end

  -- Monitor state changes after poke
  monitor_frame()

  -- Keyboard handlers
  if key_just_pressed("F5") then
    pending_save = true
  end

  if key_just_pressed("F6") then
    if not has_saved_state then
      emu.log("[Spike] No state saved! Press F5 first.")
    else
      pending_load = true
      poke_pending = {type = "anim", frame = frame_counter + 31}
      emu.log("[Spike] F6: Will load state then poke player_anim=9")
    end
  end

  if key_just_pressed("F7") then
    if not has_saved_state then
      emu.log("[Spike] No state saved! Press F5 first.")
    else
      pending_load = true
      poke_pending = {type = "ypos", frame = frame_counter + 31}
      emu.log("[Spike] F7: Will load state then poke Y-position")
    end
  end

  if key_just_pressed("F8") then
    if not has_saved_state then
      emu.log("[Spike] No state saved! Press F5 first.")
    else
      pending_load = true
      poke_pending = {type = "gameover", frame = frame_counter + 31}
      emu.log("[Spike] F8: Will load state then poke lives=0 + anim=9")
    end
  end

  if key_just_pressed("G") then
    if not has_saved_state then
      emu.log("[Spike] No state saved! Press F5 first.")
    else
      pending_load = true
      poke_pending = {type = "hold_y", frame = frame_counter + 31}
      emu.log("[Spike] G: Will load state then HOLD Y below kill plane for 60 frames")
    end
  end

  if key_just_pressed("P") then
    if not has_saved_state then
      emu.log("[Spike] No state saved! Press F5 first.")
    else
      pending_load = true
      poke_pending = {type = "auto", frame = frame_counter + 31}
      auto_retry = true
      emu.log("[Spike] F9: Full auto — Y-poke + auto-retry")
    end
  end

end, emu.eventType.startFrame)

-----------------------------------------------------------------------
-- INPUT POLLED CALLBACK (setInput must happen here, not startFrame)
-----------------------------------------------------------------------
emu.addEventCallback(function()
  if auto_retry then
    local mode = emu.read(ADDR_GAME_MODE, SNES, false)
    -- Only mash A after death animation finishes (game_mode leaves 20)
    -- Don't press anything while death anim plays or it corrupts state
    if mode ~= 20 and mode ~= 0 then
      if frame_counter % 2 == 0 then
        emu.setInput({a = true}, 0)
      else
        emu.setInput({}, 0)
      end
    end
  end
end, emu.eventType.inputPolled)

emu.log("[Spike] death_spike.lua loaded!")
emu.log("[Spike] F5 = Save state | F6 = Poke anim=9 | F7 = Poke Y-pos | F8 = Poke lives+anim")
emu.log("[Spike] G = Hold Y below kill plane (60 frames)")
emu.log("[Spike] P = Full auto (Y-poke + auto-dismiss retry)")
