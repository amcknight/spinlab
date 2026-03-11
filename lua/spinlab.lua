-- SpinLab — Mesen2 Lua Script
-- Steps 1+2 complete: Save State PoC + Passive Recorder
-- Step 3: save state on level_entrance events (reference capture) — in progress
--
-- Keyboard (manual testing):
--   T = save state to test file
--   Y = load state from test file
-- TCP commands: ping, save, load, save:<path>, load:<path>, quit
--
-- Mesen2 API notes:
--   emu.createSavestate() / emu.loadSavestate(data) -- must call from cpuExec callback
--   emu.read(addr, emu.memType.snesMemory, false)    -- byte read, SNES bus
--   emu.addMemoryCallback(fn, emu.callbackType.exec, 0x0000, 0xFFFF)
--   Permissions required: IO functions, OS functions, Network access

local socket = require("socket.core")

-----------------------------------------------------------------------
-- CONFIG
-----------------------------------------------------------------------
local TCP_PORT   = 15482
local TCP_HOST   = "127.0.0.1"
local GAME_ID    = "smw_cod"   -- TODO: read from config.yaml in Step 6
local DATA_DIR   = emu.getScriptDataFolder()
local STATE_DIR  = DATA_DIR .. "/states"
local LOG_FILE   = DATA_DIR .. "/passive_log.jsonl"
local TEST_STATE_FILE = STATE_DIR .. "/test_state.mss"

-- Memory addresses (ported from kaizosplits/Memory.cs)
local ADDR_GAME_MODE   = 0x0100  -- game mode: 18=prepare level, 20=in level
local ADDR_LEVEL_NUM   = 0x13BF  -- current level number
local ADDR_ROOM_NUM    = 0x010B  -- current room/sublevel
local ADDR_PLAYER_ANIM = 0x0071  -- player animation: 9=death
local ADDR_EXIT_MODE   = 0x0DD5  -- 0=not exiting, non-zero=exiting level
local ADDR_IO          = 0x1DFB  -- SPC I/O: 3=orb, 4=goal, 7=key, 8=fadeout
local ADDR_FANFARE     = 0x0906  -- steps to 1 when goal reached

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local server    = nil   -- TCP server socket
local client    = nil   -- connected TCP client
local initialized = false

-- cpuExec-deferred save/load
local pending_save = nil
local pending_load = nil

-- Keyboard debounce
local key_was_pressed = {}

-- Passive recorder state
local prev = {}              -- previous frame memory values
local died_flag    = false   -- set on death, cleared on next fresh entrance
local level_start_frame = 0  -- frame when current level entrance was logged
local frame_counter = 0      -- increments every startFrame
local script_start_ms = os.clock() * 1000

-----------------------------------------------------------------------
-- HELPERS
-----------------------------------------------------------------------
local function log(msg)
  emu.log("[SpinLab] " .. msg)
end

local function ensure_dir(path)
  os.execute('mkdir -p "' .. path .. '"')
end

local function save_state_to_file(path)
  local data = emu.createSavestate()
  if not data then
    log("ERROR: createSavestate returned nil")
    return false
  end
  local f = io.open(path, "wb")
  if not f then
    log("ERROR: Could not open file for writing: " .. path)
    return false
  end
  f:write(data)
  f:close()
  log("Saved state to: " .. path .. " (" .. #data .. " bytes)")
  return true
end

local function load_state_from_file(path)
  local f = io.open(path, "rb")
  if not f then
    log("ERROR: Could not open file for reading: " .. path)
    return false
  end
  local data = f:read("*a")
  f:close()
  if not data or #data == 0 then
    log("ERROR: State file is empty: " .. path)
    return false
  end
  emu.loadSavestate(data)
  log("Loaded state from: " .. path .. " (" .. #data .. " bytes)")
  return true
end

-----------------------------------------------------------------------
-- JSONL LOGGER
-----------------------------------------------------------------------

-- Minimal JSON serializer for flat string/number/bool tables
local function to_json(t)
  local parts = {}
  for k, v in pairs(t) do
    local val
    if type(v) == "string" then
      val = '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
    elseif type(v) == "boolean" then
      val = tostring(v)
    else
      val = tostring(v)
    end
    parts[#parts + 1] = '"' .. k .. '":' .. val
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

local function log_jsonl(obj)
  local f = io.open(LOG_FILE, "a")
  if not f then
    log("ERROR: Could not open log file: " .. LOG_FILE)
    return
  end
  f:write(to_json(obj) .. "\n")
  f:close()
end

-----------------------------------------------------------------------
-- MEMORY READER
-----------------------------------------------------------------------
local SNES = emu.memType.snesMemory

local function read_mem()
  return {
    game_mode   = emu.read(ADDR_GAME_MODE,   SNES, false),
    level_num   = emu.read(ADDR_LEVEL_NUM,   SNES, false),
    room_num    = emu.read(ADDR_ROOM_NUM,    SNES, false),
    player_anim = emu.read(ADDR_PLAYER_ANIM, SNES, false),
    exit_mode   = emu.read(ADDR_EXIT_MODE,   SNES, false),
    io_port     = emu.read(ADDR_IO,          SNES, false),
    fanfare     = emu.read(ADDR_FANFARE,     SNES, false),
  }
end

local function ts_ms()
  return math.floor(os.clock() * 1000 - script_start_ms)
end

-----------------------------------------------------------------------
-- TRANSITION DETECTION
-----------------------------------------------------------------------
local function goal_type(curr)
  if curr.fanfare == 1 or curr.io_port == 4 then return "normal"
  elseif curr.io_port == 7 then return "key"
  elseif curr.io_port == 3 then return "orb"
  else return "abort"  -- start+select, death exit, etc.
  end
end

local function detect_transitions(curr)
  -- Death: player animation transitions to 9
  if curr.player_anim == 9 and prev.player_anim ~= 9 then
    died_flag = true
    log_jsonl({
      event   = "death",
      level   = curr.level_num,
      room    = curr.room_num,
      frame   = frame_counter,
      ts_ms   = ts_ms(),
      session = "passive",
    })
    log("Death at level " .. curr.level_num)
  end

  -- Level entrance: gameMode transitions to 18 (GmPrepareLevel)
  if curr.game_mode == 18 and prev.game_mode ~= 18 then
    if not died_flag then
      level_start_frame = frame_counter
      local state_fname = GAME_ID .. "_" .. curr.level_num .. "_" .. curr.room_num .. ".mss"
      local state_path  = STATE_DIR .. "/" .. state_fname
      if pending_save then
        log("WARNING: pending_save overwritten (was: " .. pending_save .. ")")
      end
      pending_save = state_path
      log_jsonl({
        event      = "level_entrance",
        level      = curr.level_num,
        room       = curr.room_num,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
        state_path = state_path,
      })
      -- Note: state_path is logged optimistically; save may fail (on_cpu_exec logs errors)
      log("Level entrance: " .. curr.level_num .. " -> queued state save: " .. state_fname)
    else
      -- Quick retry respawn — reset died flag, don't log as entrance
      died_flag = false
      log("Quick retry at level " .. curr.level_num .. " (not logged as entrance)")
    end
  end

  -- Level exit: exitMode leaves 0
  if curr.exit_mode ~= 0 and prev.exit_mode == 0 then
    local elapsed = math.floor((frame_counter - level_start_frame) / 60.0 * 1000)
    local goal = goal_type(curr)
    log_jsonl({
      event      = "level_exit",
      level      = curr.level_num,
      room       = curr.room_num,
      goal       = goal,
      elapsed_ms = elapsed,
      frame      = frame_counter,
      ts_ms      = ts_ms(),
      session    = "passive",
    })
    log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
  end
end

-----------------------------------------------------------------------
-- TCP SERVER
-----------------------------------------------------------------------
local function init_tcp()
  server = socket.tcp()
  server:setoption("reuseaddr", true)
  local ok, err = server:bind(TCP_HOST, TCP_PORT)
  if not ok then
    log("ERROR: TCP bind failed: " .. tostring(err))
    return false
  end
  server:listen(1)
  server:settimeout(0)  -- non-blocking
  log("TCP server listening on " .. TCP_HOST .. ":" .. TCP_PORT)
  return true
end

local function handle_tcp()
  if not client then
    local c = server:accept()
    if c then
      c:settimeout(0)
      client = c
      log("TCP client connected")
    end
  end

  if client then
    local line, err = client:receive("*l")
    if line then
      log("TCP received: " .. line)
      if line == "save" then
        pending_save = TEST_STATE_FILE
        client:send("ok:queued\n")
      elseif line == "load" then
        pending_load = TEST_STATE_FILE
        client:send("ok:queued\n")
      elseif line:sub(1, 5) == "load:" then
        pending_load = line:sub(6)
        client:send("ok:queued\n")
      elseif line:sub(1, 5) == "save:" then
        pending_save = line:sub(6)
        client:send("ok:queued\n")
      elseif line == "ping" then
        client:send("pong\n")
      elseif line == "quit" then
        client:send("bye\n")
        client:close()
        client = nil
        log("TCP client disconnected")
      else
        client:send("err:unknown_command\n")
      end
    elseif err == "closed" then
      log("TCP client disconnected")
      client = nil
    end
  end
end

-----------------------------------------------------------------------
-- KEYBOARD SHORTCUTS (manual testing)
-----------------------------------------------------------------------
local function key_just_pressed(key)
  local down = emu.isKeyPressed(key)
  local fired = down and not key_was_pressed[key]
  key_was_pressed[key] = down
  return fired
end

local function check_keyboard()
  -- T = save state, Y = load state (fires once per keypress)
  if key_just_pressed("T") then pending_save = TEST_STATE_FILE end
  if key_just_pressed("Y") then pending_load = TEST_STATE_FILE end
end

local function on_cpu_exec(address)
  if pending_save then
    local path = pending_save
    pending_save = nil
    save_state_to_file(path)
  end
  if pending_load then
    local path = pending_load
    pending_load = nil
    load_state_from_file(path)
  end
end

-----------------------------------------------------------------------
-- MAIN FRAME CALLBACK
-----------------------------------------------------------------------
local function on_start_frame()
  if not initialized then
    ensure_dir(STATE_DIR)
    init_tcp()
    emu.addMemoryCallback(on_cpu_exec, emu.callbackType.exec, 0x0000, 0xFFFF)

    -- Seed prev with current memory so first-frame diffs don't false-fire
    prev = read_mem()

    initialized = true
    log("SpinLab initialized (passive recorder active) — log: " .. LOG_FILE)
  end

  frame_counter = frame_counter + 1

  local curr = read_mem()
  detect_transitions(curr)
  prev = curr

  check_keyboard()
  handle_tcp()

  emu.drawString(2, 2, "SpinLab", 0xFFFFFF, 0x000000, 1)
end

-- Register callbacks
emu.addEventCallback(on_start_frame, emu.eventType.startFrame)
log("SpinLab script loaded")
