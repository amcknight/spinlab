-- SpinLab — Mesen2 Lua Script
-- Steps 1+2 complete: Save State PoC + Passive Recorder
-- Step 4: Practice loop MVP
--
-- Keyboard (manual testing):
--   T = save state to test file
--   Y = load state from test file
-- TCP commands: ping, save, load, save:<path>, load:<path>,
--               practice_load:<json>, practice_stop, quit
--
-- Mesen2 API notes:
--   emu.createSavestate() / emu.loadSavestate(data) -- must call from cpuExec callback
--   emu.read(addr, emu.memType.snesMemory, false)    -- byte read, SNES bus
--   emu.addMemoryCallback(fn, emu.callbackType.exec, 0x0000, 0xFFFF)
--   emu.getInput(0) fields: l, r, up, down, left, right, a, b, x, y, start, select
--   Permissions required: IO functions, OS functions, Network access

local socket = require("socket.core")

-----------------------------------------------------------------------
-- CONFIG
-----------------------------------------------------------------------
local TCP_PORT   = 15482
local TCP_HOST   = "127.0.0.1"
local JSONL_LOGGING = false  -- set true to enable passive_log.jsonl (debugging)
local game_id    = nil  -- set dynamically from dashboard via game_context
local DATA_DIR   = emu.getScriptDataFolder()
local STATE_DIR  = DATA_DIR .. "/states"
local LOG_FILE   = DATA_DIR .. "/passive_log.jsonl"
local TEST_STATE_FILE = STATE_DIR .. "/test_state.mss"

-- Get ROM filename from Mesen
local function get_rom_filename()
    local info = emu.getRomInfo()
    if info and info.fileName then
        return info.fileName
    end
    -- Fallback: try other API
    local name = emu.getRomName and emu.getRomName() or "unknown"
    return name .. ".sfc"
end

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

-- cpuExec-deferred save/load/reset
local pending_save  = nil
local pending_load  = nil
local pending_reset = false

-- Keyboard debounce
local key_was_pressed = {}

-- Passive recorder state
local prev = {}              -- previous frame memory values
local died_flag    = false   -- set on death, cleared on next fresh entrance
local level_start_frame = 0  -- frame when current level entrance was logged
local frame_counter = 0      -- increments every startFrame
local script_start_ms = os.clock() * 1000

-- Practice mode state
local PSTATE_IDLE    = "idle"
local PSTATE_LOADING = "loading"
local PSTATE_PLAYING = "playing"
local PSTATE_RESULT  = "RESULT"

local practice_mode       = false   -- true while in practice mode
local practice_state      = PSTATE_IDLE
local practice_split      = nil     -- current split info table
local practice_start_ms   = 0       -- ts_ms() when current attempt started
local practice_elapsed_ms = 0       -- elapsed at clear/abort (for display + result)
local practice_completed  = false   -- true if clear, false if abort
local practice_result_start_ms = 0          -- when RESULT state began
local practice_auto_advance_ms = 2000       -- delay before auto-advancing (from load command)

-----------------------------------------------------------------------
-- HELPERS
-----------------------------------------------------------------------
local function log(msg)
  emu.log("[SpinLab] " .. msg)
end

local function ts_ms()
  return math.floor(os.clock() * 1000 - script_start_ms)
end

local function ensure_dir(path)
  if package.config:sub(1, 1) == "\\" then
    -- Windows: mkdir creates parent dirs by default, 2>NUL suppresses "already exists"
    os.execute('mkdir "' .. path:gsub("/", "\\") .. '" 2>NUL')
  else
    os.execute('mkdir -p "' .. path .. '" 2>/dev/null')
  end
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

-- Extract a string field from a flat JSON object string.
-- Handles backslash-escaped backslashes (e.g. Windows paths).
local function json_get_str(json_str, key)
  local raw = json_str:match('"' .. key .. '"%s*:%s*"(.-)"[%s,}%]]')
  if not raw then return nil end
  -- unescape in the correct order: \\ -> \ first, then \" -> "
  return (raw:gsub('\\\\', '\\'):gsub('\\"', '"'))
end

-- Extract a number field from a flat JSON object string.
local function json_get_num(json_str, key)
  return tonumber(json_str:match('"' .. key .. '"%s*:%s*(%d+)'))
end

-- Parse practice_load JSON payload into a table.
local function parse_practice_split(json_str)
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    goal                   = json_get_str(json_str, "goal") or "",
    description            = json_get_str(json_str, "description") or "",
    reference_time_ms      = json_get_num(json_str, "reference_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or 2000,
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
  }
end

-- drawString renders one char per row vertically in Mesen2 — work around it
-- by drawing one character at a time with manual x offsets.
local CHAR_W = 6  -- measureString("A", 1).width

local function draw_text(x, y, text, fg, bg)
  for i = 1, #text do
    emu.drawString(x + (i - 1) * CHAR_W, y, text:sub(i, i), fg, bg, 1)
  end
end

local function ms_to_display(ms)
  -- Format milliseconds as M:SS.d (e.g. 75340 -> "1:15.3")
  if not ms then return "?" end
  local total_s = math.floor(ms / 100) / 10
  local m = math.floor(total_s / 60)
  local s = total_s - m * 60
  return string.format("%d:%04.1f", m, s)
end

local function format_goal(goal)
  if not goal or goal == "" then return "?" end
  return "Exit: " .. goal:sub(1, 1):upper() .. goal:sub(2)
end

local function draw_practice_overlay()
  if not practice_mode then return end

  local label = practice_split and format_goal(practice_split.goal) or "?"
  -- Use expected time (Kalman μ) for comparison, fall back to reference time
  local compare_time = nil
  if practice_split then
    compare_time = practice_split.expected_time_ms or practice_split.reference_time_ms
  end

  if practice_state == PSTATE_PLAYING or practice_state == PSTATE_LOADING then
    local elapsed = ts_ms() - practice_start_ms

    -- Row 1: goal label
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)

    -- Row 2: timer / compare_time, color-coded
    local timer_color
    if compare_time then
      timer_color = (elapsed < compare_time) and 0xFF44FF44 or 0xFFFF4444
    else
      timer_color = 0xFFFFFFFF
    end
    local cmp_str = compare_time and ms_to_display(compare_time) or "?"
    draw_text(4, 12, ms_to_display(elapsed) .. " / " .. cmp_str, 0x00000000, timer_color)

  elseif practice_state == PSTATE_RESULT then
    local prefix = practice_completed and "Clear!" or "Abort"

    -- Row 1: goal label
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)

    -- Row 2: result time / compare_time
    local timer_color
    if compare_time then
      timer_color = (practice_elapsed_ms < compare_time) and 0xFF44FF44 or 0xFFFF4444
    else
      timer_color = 0xFFFFFFFF
    end
    local cmp_str2 = compare_time and ms_to_display(compare_time) or "?"
    draw_text(4, 12, prefix .. "  " .. ms_to_display(practice_elapsed_ms) .. " / " .. cmp_str2, 0x00000000, timer_color)

    -- Row 3: countdown to auto-advance
    local remaining = practice_auto_advance_ms - (ts_ms() - practice_result_start_ms)
    local secs = string.format("%.1f", math.max(0, remaining / 1000))
    draw_text(4, 22, "Next in " .. secs .. "s", 0x00000000, 0xFF888888)
  end
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
    log("Death at level " .. curr.level_num .. " (not logged to JSONL)")
  end

  -- Level entrance: gameMode transitions to 18 (GmPrepareLevel)
  if curr.game_mode == 18 and prev.game_mode ~= 18 then
    if not died_flag then
      level_start_frame = frame_counter
      -- Determine state path and whether to save (requires game_id)
      local state_path
      if not game_id then
        log("No game context yet, skipping state save")
        pending_save = nil
      else
        local state_fname = curr.level_num .. "_" .. curr.room_num .. ".mss"
        state_path  = STATE_DIR .. "/" .. game_id .. "/" .. state_fname
        if pending_save then
          log("WARNING: pending_save overwritten (was: " .. pending_save .. ")")
        end
        pending_save = state_path
      end
      local event_data = {
        event      = "level_entrance",
        level      = curr.level_num,
        room       = curr.room_num,
        frame      = frame_counter,
        ts_ms      = ts_ms(),
        session    = "passive",
        state_path = state_path or "",
      }
      if JSONL_LOGGING then
        log_jsonl(event_data)
      end
      -- Forward over TCP for live reference capture
      if client and not practice_mode then
        client:send(to_json(event_data) .. "\n")
      end
      log("Level entrance: " .. curr.level_num .. " -> " .. (state_path and ("queued state save: " .. state_path) or "no game context, save skipped"))
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
    local event_data = {
      event      = "level_exit",
      level      = curr.level_num,
      room       = curr.room_num,
      goal       = goal,
      elapsed_ms = elapsed,
      frame      = frame_counter,
      ts_ms      = ts_ms(),
      session    = "passive",
    }
    if JSONL_LOGGING then
      log_jsonl(event_data)
    end
    -- Forward over TCP for live reference capture
    if client and not practice_mode then
      client:send(to_json(event_data) .. "\n")
    end
    log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
  end
end

-----------------------------------------------------------------------
-- PRACTICE MODE STATE MACHINE
-----------------------------------------------------------------------
local function handle_practice(curr)
  if practice_state == PSTATE_LOADING then
    -- pending_load was queued; by next frame cpuExec will have fired.
    -- Transition to PLAYING and start the timer.
    practice_state    = PSTATE_PLAYING
    practice_start_ms = ts_ms()

  elseif practice_state == PSTATE_PLAYING then
    -- Death check (higher priority than exit_mode)
    if curr.player_anim == 9 and prev.player_anim ~= 9 then
      pending_load = practice_split.state_path
      log("Practice: death — reloading state")

    elseif curr.exit_mode ~= 0 and prev.exit_mode == 0 then
      local goal = goal_type(curr)
      practice_elapsed_ms = ts_ms() - practice_start_ms
      practice_completed  = (goal ~= "abort")
      practice_state      = PSTATE_RESULT
      practice_result_start_ms = ts_ms()
      log("Practice: RESULT (" .. goal .. ") — " .. practice_elapsed_ms .. "ms")
    end

  elseif practice_state == PSTATE_RESULT then
    -- Auto-advance after delay
    local elapsed_in_result = ts_ms() - practice_result_start_ms
    if elapsed_in_result >= practice_auto_advance_ms then
      -- Send result to orchestrator
      local result = to_json({
        event      = "attempt_result",
        split_id   = practice_split.id,
        completed  = practice_completed,
        time_ms    = math.floor(practice_elapsed_ms),
        goal       = practice_split.goal,
      })
      if client then
        client:send(result .. "\n")
      end
      -- Reset state
      practice_state = PSTATE_IDLE
      practice_mode  = false
      practice_split = nil
      log("Practice: auto-advanced, sent result")
    end
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

local heartbeat_counter = 0
local HEARTBEAT_INTERVAL = 60  -- frames between heartbeat pings (~1 second)

local function handle_tcp()
  if not client then
    local c = server:accept()
    if c then
      c:settimeout(0)
      client = c
      heartbeat_counter = 0
      log("TCP client connected")
      -- Send ROM info for game auto-discovery
      local rom_fname = get_rom_filename()
      c:send(to_json({event = "rom_info", filename = rom_fname}) .. "\n")
      log("Sent rom_info: " .. rom_fname)
    end
  end

  if client then
    -- Periodic heartbeat: send() detects dead connections that receive() misses
    heartbeat_counter = heartbeat_counter + 1
    if heartbeat_counter >= HEARTBEAT_INTERVAL then
      heartbeat_counter = 0
      local _, send_err = client:send("heartbeat\n")
      if send_err then
        log("TCP heartbeat failed: " .. tostring(send_err) .. " — client is dead")
        pcall(function() client:close() end)
        client = nil
        if practice_mode then
          practice_mode     = false
          practice_state    = PSTATE_IDLE
          practice_split    = nil
          pending_load      = nil
          pending_save      = nil
          died_flag         = false
          pending_reset     = true
          log("Practice auto-cleared on disconnect — reset queued")
        end
        return
      end
    end

    local line, err = client:receive("*l")
    if line then
      log("TCP received: " .. line)
      if line:sub(1, 1) == "{" then
        -- Handle JSON messages from dashboard
        local decoded_event = json_get_str(line, "event")
        if decoded_event == "game_context" then
          game_id = json_get_str(line, "game_id")
          local gname = json_get_str(line, "game_name") or game_id or "unknown"
          if game_id then
            ensure_dir(STATE_DIR .. "/" .. game_id)
          end
          log("Game context: " .. gname .. " (" .. (game_id or "nil") .. ")")
        end
        -- Don't fall through to text command parsing for JSON messages
      elseif line == "save" then
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
      elseif line:sub(1, 14) == "practice_load:" then
        local json_str = line:sub(15)
        practice_split           = parse_practice_split(json_str)
        practice_auto_advance_ms = practice_split.auto_advance_delay_ms or 2000
        practice_mode            = true
        practice_state           = PSTATE_LOADING
        pending_load             = practice_split.state_path
        practice_start_ms        = ts_ms()
        client:send("ok:queued\n")
        log("Practice load queued: " .. (practice_split.id or "?"))
      elseif line == "practice_stop" then
        practice_mode     = false
        practice_state    = PSTATE_IDLE
        practice_split    = nil
        pending_load      = nil  -- prevent ghost reload if stopped mid-death-retry
        client:send("ok\n")
        log("Practice mode stopped")
      elseif line == "reset" then
        practice_mode     = false
        practice_state    = PSTATE_IDLE
        practice_split    = nil
        pending_load      = nil
        pending_save      = nil
        died_flag         = false  -- clear so first post-reset entrance is recorded
        pending_reset     = true
        client:send("ok\n")
        log("Reset queued: practice cleared, SNES reset on next cpuExec")
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
    elseif err ~= "timeout" then
      -- any error other than "no data yet" = connection gone (closed, reset, etc.)
      log("TCP client disconnected: " .. tostring(err))
      pcall(function() client:close() end)  -- safe close, may already be dead
      client = nil
      if practice_mode then
        -- Orchestrator died while practice was active — auto-clear and reset
        practice_mode     = false
        practice_state    = PSTATE_IDLE
        practice_split    = nil
        pending_load      = nil
        pending_save      = nil
        died_flag         = false
        pending_reset     = true
        log("Practice auto-cleared on disconnect — reset queued")
      end
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
  if pending_reset then
    pending_reset = false
    emu.reset()
    log("SNES reset executed")
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
  if practice_mode then
    handle_practice(curr)
  else
    detect_transitions(curr)
  end
  prev = curr

  check_keyboard()
  handle_tcp()

  draw_practice_overlay()
end

-- Register callbacks
emu.addEventCallback(on_start_frame, emu.eventType.startFrame)
log("SpinLab script loaded")
