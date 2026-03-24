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
    if info and info.name then
        return info.name
    end
    return "unknown.sfc"
end

-- Memory addresses (ported from kaizosplits/Memory.cs)
local ADDR_GAME_MODE   = 0x0100  -- game mode: 18=prepare level, 20=in level
local ADDR_LEVEL_NUM   = 0x13BF  -- current level number
local ADDR_ROOM_NUM    = 0x010B  -- current room/sublevel
local ADDR_LEVEL_START = 0x1935  -- 0→1 when player appears in level (kaizosplits "levelStart")
local ADDR_PLAYER_ANIM = 0x0071  -- player animation: 9=death
local ADDR_EXIT_MODE   = 0x0DD5  -- 0=not exiting, non-zero=exiting level
local ADDR_IO          = 0x1DFB  -- SPC I/O: 3=orb, 4=goal, 7=key, 8=fadeout
local ADDR_FANFARE     = 0x0906  -- steps to 1 when goal reached
local ADDR_BOSS_DEFEAT = 0x13C6  -- 0=alive, non-zero=defeated
local ADDR_MIDWAY      = 0x13CE  -- midway checkpoint tape: 0→1 when touched
local ADDR_CP_ENTRANCE = 0x1B403 -- ASM-style checkpoint entrance

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

-- Checkpoint tracking
local cp_ordinal     = 0      -- per-level counter, incremented on each new CP
local cp_acquired    = false  -- true when a new CP was hit without cold capture yet
local first_cp_entrance = 0   -- initial cpEntrance value at level start

-- Practice mode state
local PSTATE_IDLE    = "idle"
local PSTATE_LOADING = "loading"
local PSTATE_PLAYING = "playing"
local PSTATE_RESULT  = "RESULT"

local practice = {
    active = false,
    state = PSTATE_IDLE,
    segment = nil,
    start_ms = 0,
    elapsed_ms = 0,
    completed = false,
    result_start_ms = 0,
    auto_advance_ms = 2000,
}

local function practice_reset()
    practice.active = false
    practice.state = PSTATE_IDLE
    practice.segment = nil
    practice.start_ms = 0
    practice.elapsed_ms = 0
    practice.completed = false
    practice.result_start_ms = 0
    practice.auto_advance_ms = 2000
end

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

-- Extract a boolean field from a flat JSON object string. Returns nil if absent.
local function json_get_bool(json_str, key)
  local val = json_str:match('"' .. key .. '"%s*:%s*(%a+)')
  if val == "true" then return true
  elseif val == "false" then return false
  else return nil end
end

-- Parse practice_load JSON payload into a table.
local function parse_practice_segment(json_str)
  local end_on_goal = json_get_bool(json_str, "end_on_goal")
  if end_on_goal == nil then end_on_goal = true end  -- default on
  local end_type = json_get_str(json_str, "end_type") or "goal"  -- "goal" or "checkpoint"
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    goal                   = json_get_str(json_str, "goal") or "",
    description            = json_get_str(json_str, "description") or "",
    reference_time_ms      = json_get_num(json_str, "reference_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or 2000,
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
    end_on_goal            = end_on_goal,
    end_type               = end_type,
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

local function draw_timer_row(y, elapsed, compare_time, prefix)
  local timer_color
  if compare_time then
    timer_color = (elapsed < compare_time) and 0xFF44FF44 or 0xFFFF4444
  else
    timer_color = 0xFFFFFFFF
  end
  local cmp_str = compare_time and ms_to_display(compare_time) or "?"
  local text = (prefix and (prefix .. "  ") or "") .. ms_to_display(elapsed) .. " / " .. cmp_str
  draw_text(4, y, text, 0x00000000, timer_color)
end

local function draw_practice_overlay()
  if not practice.active then return end

  local label = practice.segment and format_goal(practice.segment.goal) or "?"
  -- Use expected time (Kalman μ) for comparison, fall back to reference time
  local compare_time = nil
  if practice.segment then
    compare_time = practice.segment.expected_time_ms or practice.segment.reference_time_ms
  end

  if practice.state == PSTATE_PLAYING or practice.state == PSTATE_LOADING then
    local elapsed = ts_ms() - practice.start_ms
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)
    draw_timer_row(12, elapsed, compare_time)

  elseif practice.state == PSTATE_RESULT then
    local prefix = practice.completed and "Clear!" or "Abort"
    draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)
    draw_timer_row(12, practice.elapsed_ms, compare_time, prefix)

    -- Row 3: countdown to auto-advance
    local remaining = practice.auto_advance_ms - (ts_ms() - practice.result_start_ms)
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
    level_start = emu.read(ADDR_LEVEL_START, SNES, false),
    player_anim = emu.read(ADDR_PLAYER_ANIM, SNES, false),
    exit_mode   = emu.read(ADDR_EXIT_MODE,   SNES, false),
    io_port     = emu.read(ADDR_IO,          SNES, false),
    fanfare     = emu.read(ADDR_FANFARE,     SNES, false),
    boss_defeat = emu.read(ADDR_BOSS_DEFEAT, SNES, false),
    midway      = emu.read(ADDR_MIDWAY,      SNES, false),
    cp_entrance = emu.read(ADDR_CP_ENTRANCE, SNES, false),
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

local function on_level_entrance(curr, state_path)
  level_start_frame = frame_counter
  local event_data = {
    event      = "level_entrance",
    level      = curr.level_num,
    room       = curr.room_num,
    frame      = frame_counter,
    ts_ms      = ts_ms(),
    session    = "passive",
    state_path = state_path or "",
  }
  if JSONL_LOGGING then log_jsonl(event_data) end
  if client and not practice.active then
    client:send(to_json(event_data) .. "\n")
  end
  log("Level entrance: " .. curr.level_num .. " -> " ..
      (state_path and ("queued state save: " .. state_path) or "no game context, save skipped"))
end

local function on_death(curr)
  if not died_flag then
    local event_data = {
      event      = "death",
      level_num  = curr.level_num,
      timestamp_ms = ts_ms(),
    }
    if client and not practice.active then
      client:send(to_json(event_data) .. "\n")
    end
  end
  died_flag = true
  log("Death at level " .. curr.level_num)
end

local function on_level_exit(curr)
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
  if JSONL_LOGGING then log_jsonl(event_data) end
  if client and not practice.active then
    client:send(to_json(event_data) .. "\n")
  end
  log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
end

local function detect_transitions(curr)
  if curr.player_anim == 9 and prev.player_anim ~= 9 then
    on_death(curr)
  end

  -- Checkpoint detection (composite condition)
  local got_orb     = curr.io_port == 3
  local got_goal    = curr.fanfare == 1 or curr.io_port == 4
  local got_key     = curr.io_port == 7
  local got_fadeout = curr.io_port == 8

  -- Midway: 0→1 transition, excluding goal/orb/key/fadeout
  local midway_hit = (prev.midway == 0 and curr.midway == 1)
      and not got_orb and not got_goal and not got_key and not got_fadeout

  -- CPEntrance: value shifted, not to firstRoom, excluding goal/orb/key/fadeout
  local cp_entrance_hit = (prev.cp_entrance ~= nil and curr.cp_entrance ~= prev.cp_entrance
      and curr.cp_entrance ~= first_cp_entrance)
      and not got_orb and not got_goal and not got_key and not got_fadeout

  local cp_hit = midway_hit or cp_entrance_hit

  if cp_hit then
    cp_ordinal = cp_ordinal + 1
    cp_acquired = true
    -- After first CP, clear firstRoom so future cpEntrance shifts are real CPs
    -- Setting to 0 is safe: cpEntrance values are room IDs (non-zero in levels)
    first_cp_entrance = 0
    -- Capture hot save state
    if game_id then
      local state_path = STATE_DIR .. "/" .. game_id .. "/" .. curr.level_num .. "_cp" .. cp_ordinal .. "_hot.mss"
      pending_save = state_path
      local event_data = {
        event       = "checkpoint",
        level_num   = curr.level_num,
        cp_type     = midway_hit and "midway" or "cp_entrance",
        cp_ordinal  = cp_ordinal,
        timestamp_ms = ts_ms(),
        state_path  = state_path,
      }
      if client and not practice.active then
        client:send(to_json(event_data) .. "\n")
      end
      log("Checkpoint: level " .. curr.level_num .. " cp" .. cp_ordinal .. " (" .. (midway_hit and "midway" or "cp_entrance") .. ")")
    end
  end

  -- Level entrance: levelStart 0→1 (kaizosplits "LevelStart").
  -- Fires once when the player appears in the level — does NOT fire for
  -- sublevel pipe/door transitions, only for fresh level entry or death respawn.
  if curr.level_start == 1 and prev.level_start == 0 then
    if died_flag then
      -- Spawn: respawn after death
      local state_captured = false
      local state_path = nil
      local was_cp_acquired = cp_acquired  -- capture before clearing
      if cp_acquired and game_id then
        state_path = STATE_DIR .. "/" .. game_id .. "/" .. curr.level_num .. "_cp" .. cp_ordinal .. "_cold.mss"
        pending_save = state_path
        state_captured = true
        cp_acquired = false  -- only capture first cold spawn per CP
      end
      local event_data = {
        event          = "spawn",
        level_num      = curr.level_num,
        is_cold_cp     = was_cp_acquired,
        cp_ordinal     = cp_ordinal,
        timestamp_ms   = ts_ms(),
        state_captured = state_captured,
        state_path     = state_path or "",
      }
      if client and not practice.active then
        client:send(to_json(event_data) .. "\n")
      end
      died_flag = false
      log("Spawn at level " .. curr.level_num .. (was_cp_acquired and (" — cold CP" .. cp_ordinal .. " captured") or ""))
    else
      -- Put: fresh level entry
      cp_ordinal = 0
      cp_acquired = false
      first_cp_entrance = curr.cp_entrance  -- record initial entrance
      local state_path
      if not game_id then
        log("No game context yet, skipping state save")
        if client and not practice.active then
          client:send(to_json({event = "error", message = "No game context — save state skipped"}) .. "\n")
        end
      else
        local state_fname = curr.level_num .. "_" .. curr.room_num .. ".mss"
        state_path = STATE_DIR .. "/" .. game_id .. "/" .. state_fname
        if pending_save then
          log("WARNING: pending_save overwritten (was: " .. pending_save .. ")")
        end
        pending_save = state_path
      end
      on_level_entrance(curr, state_path)
    end
  end

  if curr.exit_mode ~= 0 and prev.exit_mode == 0 then
    on_level_exit(curr)
  end
end

-----------------------------------------------------------------------
-- EARLY FINISH DETECTION (kaizosplits "LevelFinish" conditions)
-----------------------------------------------------------------------
-- Returns goal type string if a finish condition fired this frame, nil otherwise.
-- Uses prev/curr transitions matching kaizosplits Watchers.cs logic.
local function detect_finish(curr)
  -- Goal tape: fanfare 0→1, boss alive, no orb
  if curr.fanfare == 1 and prev.fanfare == 0 and curr.boss_defeat == 0 and curr.io_port ~= 3 then
    return "normal"
  end
  -- Boss: fanfare 0→1, boss defeated
  if curr.fanfare == 1 and prev.fanfare == 0 and curr.boss_defeat ~= 0 then
    return "boss"
  end
  -- Orb: io shifts to 3, boss alive
  if curr.io_port == 3 and prev.io_port ~= 3 and curr.boss_defeat == 0 then
    return "orb"
  end
  -- Key: io shifts to 7
  if curr.io_port == 7 and prev.io_port ~= 7 then
    return "key"
  end
  return nil
end

-----------------------------------------------------------------------
-- PRACTICE MODE STATE MACHINE
-----------------------------------------------------------------------
local function handle_practice(curr)
  if practice.state == PSTATE_LOADING then
    -- pending_load was queued; by next frame cpuExec will have fired.
    -- Transition to PLAYING and start the timer.
    practice.state    = PSTATE_PLAYING
    practice.start_ms = ts_ms()

  elseif practice.state == PSTATE_PLAYING then
    -- Death check (higher priority than exit/finish)
    if curr.player_anim == 9 and prev.player_anim ~= 9 then
      pending_load = practice.segment.state_path
      log("Practice: death — reloading state")

    elseif practice.segment.end_type == "checkpoint" then
      -- Checkpoint end-condition: composite CP detection (same as passive)
      local got_orb     = curr.io_port == 3
      local got_goal    = curr.fanfare == 1 or curr.io_port == 4
      local got_key     = curr.io_port == 7
      local got_fadeout = curr.io_port == 8

      local midway_hit = (prev.midway == 0 and curr.midway == 1)
          and not got_orb and not got_goal and not got_key and not got_fadeout
      local cp_entrance_hit = (prev.cp_entrance ~= nil and curr.cp_entrance ~= prev.cp_entrance
          and curr.cp_entrance ~= first_cp_entrance)
          and not got_orb and not got_goal and not got_key and not got_fadeout

      if midway_hit or cp_entrance_hit then
        practice.elapsed_ms = ts_ms() - practice.start_ms
        practice.completed  = true
        practice.state      = PSTATE_RESULT
        practice.result_start_ms = ts_ms()
        log("Practice: CHECKPOINT — " .. practice.elapsed_ms .. "ms")
      end

    elseif practice.segment.end_on_goal and detect_finish(curr) then
      -- Early finish: goal/orb/key/boss detected, skip fanfare wait
      local finish_goal = detect_finish(curr)
      practice.elapsed_ms = ts_ms() - practice.start_ms
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: FINISH (" .. finish_goal .. ") — " .. practice.elapsed_ms .. "ms")

    elseif curr.exit_mode ~= 0 and prev.exit_mode == 0 then
      -- Late exit: full exit_mode transition (fallback when end_on_goal is off)
      local goal = goal_type(curr)
      practice.elapsed_ms = ts_ms() - practice.start_ms
      practice.completed  = (goal ~= "abort")
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: RESULT (" .. goal .. ") — " .. practice.elapsed_ms .. "ms")
    end

  elseif practice.state == PSTATE_RESULT then
    -- Auto-advance after delay
    local elapsed_in_result = ts_ms() - practice.result_start_ms
    if elapsed_in_result >= practice.auto_advance_ms then
      -- Send result to orchestrator
      local result = to_json({
        event      = "attempt_result",
        segment_id = practice.segment.id,
        completed  = practice.completed,
        time_ms    = math.floor(practice.elapsed_ms),
        goal       = practice.segment.goal,
      })
      if client then
        client:send(result .. "\n")
      end
      -- Reset state
      practice_reset()
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
        if practice.active then
          practice_reset()
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
        practice.segment         = parse_practice_segment(json_str)
        practice.auto_advance_ms = practice.segment.auto_advance_delay_ms or 2000
        practice.active          = true
        practice.state           = PSTATE_LOADING
        local sp = practice.segment.state_path
        if not sp or sp == "" then
          log("ERROR: No valid state_path for segment " .. (practice.segment.id or "?"))
          client:send("err:no_state_path\n")
          practice_reset()
        else
          pending_load      = sp
          practice.start_ms = ts_ms()
          client:send("ok:queued\n")
          log("Practice load queued: " .. (practice.segment.id or "?"))
        end
      elseif line == "practice_stop" then
        practice_reset()
        pending_load      = nil  -- prevent ghost reload if stopped mid-death-retry
        client:send("ok\n")
        log("Practice mode stopped")
      elseif line == "reset" then
        practice_reset()
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
      if practice.active then
        -- Orchestrator died while practice was active — auto-clear and reset
        practice_reset()
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
  if practice.active then
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
