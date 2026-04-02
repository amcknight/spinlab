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
local MAX_RECORDING_FRAMES = 360000  -- 100 minutes at 60fps
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
local pending_saves = {}
local pending_loads = {}
local pending_reset = false

-- Keyboard debounce
local key_was_pressed = {}

-- Passive recorder state
local prev = {}              -- previous frame memory values
local level_start_frame = 0  -- frame when current level entrance was logged
local frame_counter = 0      -- increments every startFrame
local script_start_ms = os.clock() * 1000

-- Checkpoint tracking
local cp_acquired    = false  -- true when a new CP was hit without cold capture yet

-- Forward-declared so reset_detection_state() can reference it as an upvalue
local exit_this_frame = false

-- Transition detection state (grouped for clean reset)
local transition_state = {
  died_flag = false,
  cp_ordinal = 0,
  first_cp_entrance = 0,
  last_event_key = nil,
}

local function reset_transition_state()
  transition_state.died_flag = false
  transition_state.cp_ordinal = 0
  transition_state.first_cp_entrance = 0
  transition_state.last_event_key = nil
end

-- Global reset callable by poke_engine.lua between scenarios.
-- Zeros all detection state so the next scenario starts clean.
function reset_detection_state()
  for k, _ in pairs(prev) do prev[k] = 0 end
  reset_transition_state()
  cp_acquired = false
  level_start_frame = 0
  exit_this_frame = false
end

-- Practice mode state
local PSTATE_IDLE    = "idle"
local PSTATE_LOADING = "loading"
local PSTATE_PLAYING = "playing"
local PSTATE_RESULT  = "result"

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
    reset_transition_state()
end

-- Recording state (passive mode input capture)
local recording = {
  active = false,
  buffer = {},       -- array of uint16 bitmasks
  frame_index = 0,
  output_path = nil, -- .spinrec file path (set by reference_start)
}

local pending_rec_save = nil  -- separate from pending_save to avoid contention

-- Replay state
local replay = {
  active = false,
  frames = {},        -- array of uint16 bitmasks loaded from .spinrec
  index = 1,          -- current frame position
  total = 0,          -- total frames
  path = nil,         -- .spinrec file path
  speed = 0,          -- 0 = max, 100 = normal
  prev_speed = nil,   -- speed to restore after replay
  last_progress_ms = 0,  -- wall-clock time of last progress event
}

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

local function flush_spinrec(path, game_id_str, buffer)
  -- Header: SREC (4) + version (2) + game_id (16) + frame_count (4) + reserved (6) = 32
  local f = io.open(path, "wb")
  if not f then
    log("ERROR: Cannot write spinrec: " .. path)
    return false
  end
  -- Magic
  f:write("SREC")
  -- Version (uint16 LE)
  f:write(string.char(1, 0))
  -- Game ID (16 bytes ASCII, pad with zeros if shorter)
  local gid = (game_id_str or ""):sub(1, 16)
  f:write(gid .. string.rep("\0", 16 - #gid))
  -- Frame count (uint32 LE)
  local n = #buffer
  f:write(string.char(n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF))
  -- Reserved (6 zeros)
  f:write(string.rep("\0", 6))
  -- Body: 2 bytes per frame (uint16 LE)
  for _, mask in ipairs(buffer) do
    f:write(string.char(mask & 0xFF, (mask >> 8) & 0xFF))
  end
  f:close()
  log("Wrote spinrec: " .. path .. " (" .. n .. " frames)")
  return true
end

local function read_spinrec(path)
  local f = io.open(path, "rb")
  if not f then return nil, "file not found: " .. path end
  local data = f:read("*a")
  f:close()
  if #data < 32 then return nil, "file too short" end
  -- Validate magic
  if data:sub(1, 4) ~= "SREC" then return nil, "bad magic" end
  -- Parse header
  local b = function(i) return data:byte(i) end
  local frame_count = b(23) + b(24) * 256 + b(25) * 65536 + b(26) * 16777216
  local gid = data:sub(7, 22)
  -- Validate body length
  local expected_body = frame_count * 2
  if #data - 32 < expected_body then return nil, "body truncated" end
  -- Parse frames
  local frames = {}
  for i = 1, frame_count do
    local offset = 32 + (i - 1) * 2
    frames[i] = b(offset + 1) + b(offset + 2) * 256
  end
  return {game_id = gid, frame_count = frame_count, frames = frames}, nil
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

  local label = practice.segment and practice.segment.description or "?"
  if label == "" then label = "?" end
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

local function send_event(event)
  if not client then return end
  if practice.active then return end
  if replay.active then
    event.source = "replay"
  end
  client:send(to_json(event) .. "\n")
end

-- Global: allows poke_engine.lua to send events over the TCP client
function send_raw_event(json_str)
  if not client then return end
  client:send(json_str .. "\n")
end

-- Input bitmask encoding: matches SNES joypad register layout
local INPUT_BITS = {
  b = 0, y = 1, select = 2, start = 3,
  up = 4, down = 5, left = 6, right = 7,
  a = 8, x = 9, l = 10, r = 11,
}

local function encode_input(tbl)
  local mask = 0
  for name, bit in pairs(INPUT_BITS) do
    if tbl[name] then mask = mask + (1 << bit) end
  end
  return mask
end

local function decode_input(mask)
  local tbl = {}
  for name, bit in pairs(INPUT_BITS) do
    tbl[name] = (mask & (1 << bit)) ~= 0
  end
  return tbl
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
  if curr.io_port == 7 then return "key"
  elseif curr.io_port == 3 then return "orb"
  elseif curr.boss_defeat ~= 0 and curr.fanfare == 1 then return "boss"
  elseif curr.fanfare == 1 or curr.io_port == 4 then return "normal"
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
  send_event(event_data)
  log("Level entrance: " .. curr.level_num .. " -> " ..
      (state_path and ("queued state save: " .. state_path) or "no game context, save skipped"))
end

local function on_death(curr)
  if not transition_state.died_flag then
    local event_data = {
      event      = "death",
      level_num  = curr.level_num,
      timestamp_ms = ts_ms(),
    }
    send_event(event_data)
  end
  transition_state.died_flag = true
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
  send_event(event_data)
  log("Level exit: " .. curr.level_num .. " goal=" .. goal .. " elapsed=" .. elapsed .. "ms")
end

-- Shared detection predicates (used by both passive and practice modes)

local function is_death_frame(curr)
  return curr.player_anim == 9 and prev.player_anim ~= 9
end

-- Returns "midway" or "cp_entrance" if a checkpoint was hit this frame, nil otherwise.
local function check_checkpoint_hit(curr)
  local got_orb     = curr.io_port == 3
  local got_goal    = curr.fanfare == 1 or curr.io_port == 4
  local got_key     = curr.io_port == 7
  local got_fadeout = curr.io_port == 8

  local midway_hit = (prev.midway == 0 and curr.midway == 1)
      and not got_orb and not got_goal and not got_key and not got_fadeout

  local cp_entrance_hit = (curr.level_num ~= 0
      and prev.cp_entrance ~= nil and curr.cp_entrance ~= prev.cp_entrance
      and curr.cp_entrance ~= transition_state.first_cp_entrance)
      and not got_orb and not got_goal and not got_key and not got_fadeout

  if midway_hit then return "midway"
  elseif cp_entrance_hit then return "cp_entrance"
  else return nil end
end

local function is_exit_frame(curr)
  return curr.exit_mode ~= 0 and prev.exit_mode == 0
end

-- Passive mode handlers (use shared predicates + emit TCP events)

local function detect_death(curr)
  if is_death_frame(curr) then
    on_death(curr)
  end
end

local function detect_checkpoint(curr)
  local cp_type = check_checkpoint_hit(curr)
  if cp_type then
    transition_state.cp_ordinal = transition_state.cp_ordinal + 1
    cp_acquired = true
    -- After first CP, clear firstRoom so future cpEntrance shifts are real CPs
    -- Setting to 0 is safe: cpEntrance values are room IDs (non-zero in levels)
    transition_state.first_cp_entrance = 0
    -- Capture hot save state
    if game_id then
      local state_path = STATE_DIR .. "/" .. game_id .. "/" .. curr.level_num .. "_cp" .. transition_state.cp_ordinal .. "_hot.mss"
      table.insert(pending_saves, state_path)
      local event_data = {
        event       = "checkpoint",
        level_num   = curr.level_num,
        cp_type     = cp_type,
        cp_ordinal  = transition_state.cp_ordinal,
        timestamp_ms = ts_ms(),
        state_path  = state_path,
      }
      send_event(event_data)
      log("Checkpoint: level " .. curr.level_num .. " cp" .. transition_state.cp_ordinal .. " (" .. cp_type .. ")")
    end
  end
end

local function detect_exit(curr)
  -- Exit detection MUST come before entrance detection.  If level_start and
  -- exit_mode both transition 0→1 on the same frame (common during SMW goal
  -- sequences), exit must consume ref_pending_start first so the entrance
  -- handler doesn't overwrite it.
  exit_this_frame = is_exit_frame(curr)
  if exit_this_frame then
    on_level_exit(curr)
  end
end

local function detect_entrance(curr)
  -- Level entrance: levelStart 0→1 (kaizosplits "LevelStart").
  -- Fires once when the player appears in the level — does NOT fire for
  -- sublevel pipe/door transitions, only for fresh level entry or death respawn.
  -- Suppress if exit_mode also transitioned this frame (spurious transition
  -- during goal sequence — not a real level entry).
  if curr.level_start == 1 and prev.level_start == 0 and not exit_this_frame then
    if transition_state.died_flag then
      -- Spawn: respawn after death
      local state_captured = false
      local state_path = nil
      local was_cp_acquired = cp_acquired  -- capture before clearing
      if cp_acquired and game_id then
        state_path = STATE_DIR .. "/" .. game_id .. "/" .. curr.level_num .. "_cp" .. transition_state.cp_ordinal .. "_cold.mss"
        table.insert(pending_saves, state_path)
        state_captured = true
        cp_acquired = false  -- only capture first cold spawn per CP
      end
      local event_data = {
        event          = "spawn",
        level_num      = curr.level_num,
        is_cold_cp     = was_cp_acquired,
        cp_ordinal     = transition_state.cp_ordinal,
        timestamp_ms   = ts_ms(),
        state_captured = state_captured,
        state_path     = state_path or "",
      }
      send_event(event_data)
      transition_state.died_flag = false
      log("Spawn at level " .. curr.level_num .. (was_cp_acquired and (" — cold CP" .. transition_state.cp_ordinal .. " captured") or ""))
    else
      -- Put: fresh level entry
      transition_state.cp_ordinal = 0
      cp_acquired = false
      transition_state.first_cp_entrance = curr.cp_entrance
      local state_path
      if not game_id then
        log("No game context yet, skipping state save")
        if client and not practice.active then
          client:send(to_json({event = "error", message = "No game context — save state skipped"}) .. "\n")
        end
      else
        local state_fname = curr.level_num .. "_" .. curr.room_num .. ".mss"
        state_path = STATE_DIR .. "/" .. game_id .. "/" .. state_fname
        table.insert(pending_saves, state_path)
      end
      on_level_entrance(curr, state_path)
    end
  end
end

local function detect_transitions(curr)
  detect_death(curr)
  detect_checkpoint(curr)
  -- Exit MUST come before entrance (same-frame ordering)
  detect_exit(curr)
  detect_entrance(curr)
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
    if is_death_frame(curr) then
      table.insert(pending_loads, practice.segment.state_path)
      log("Practice: death — reloading state")

    elseif practice.segment.end_type == "checkpoint" and check_checkpoint_hit(curr) then
      practice.elapsed_ms = ts_ms() - practice.start_ms
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: CHECKPOINT — " .. practice.elapsed_ms .. "ms")

    elseif practice.segment.end_on_goal and detect_finish(curr) then
      -- Early finish: goal/orb/key/boss detected, skip fanfare wait
      local finish_goal = detect_finish(curr)
      practice.elapsed_ms = ts_ms() - practice.start_ms
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: FINISH (" .. finish_goal .. ") — " .. practice.elapsed_ms .. "ms")

    elseif is_exit_frame(curr) then
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
-- DISCONNECT CLEANUP
-----------------------------------------------------------------------
local function disconnect_cleanup()
  if practice.active then
    practice_reset()
    pending_loads     = {}
    pending_saves     = {}
    pending_reset     = true
    log("Practice auto-cleared on disconnect — reset queued")
  end
  if recording.active then
    recording.active = false
    recording.buffer = {}
    recording.frame_index = 0
    if recording.output_path then
      local mss = recording.output_path:gsub("%.spinrec$", ".mss")
      os.remove(mss)
    end
    recording.output_path = nil
    log("Recording auto-cleared on disconnect")
  end
  if replay.active then
    replay.active = false
    replay.frames = {}
    replay.index = 1
    if replay.prev_speed and emu.setSpeed then
      emu.setSpeed(replay.prev_speed)
    end
    replay.path = nil
    log("Replay auto-cleared on disconnect")
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

local function tcp_accept()
  local c = server:accept()
  if c then
    c:settimeout(0)
    c:setoption("tcp-nodelay", true)
    client = c
    heartbeat_counter = 0
    log("TCP client connected")
    -- Send ROM info for game auto-discovery
    local rom_fname = get_rom_filename()
    c:send(to_json({event = "rom_info", filename = rom_fname}) .. "\n")
    log("Sent rom_info: " .. rom_fname)
  end
end

local function tcp_heartbeat()
  heartbeat_counter = heartbeat_counter + 1
  if heartbeat_counter >= HEARTBEAT_INTERVAL then
    heartbeat_counter = 0
    local _, send_err = client:send("heartbeat\n")
    if send_err then
      log("TCP heartbeat failed: " .. tostring(send_err) .. " — client is dead")
      pcall(function() client:close() end)
      client = nil
      disconnect_cleanup()
      return false
    end
  end
  return true
end

local function handle_json_message(line)
  local decoded_event = json_get_str(line, "event")
  if decoded_event == "game_context" then
    game_id = json_get_str(line, "game_id")
    local gname = json_get_str(line, "game_name") or game_id or "unknown"
    if game_id then
      ensure_dir(STATE_DIR .. "/" .. game_id)
    end
    log("Game context: " .. gname .. " (" .. (game_id or "nil") .. ")")
  elseif decoded_event == "reference_start" then
    local path = json_get_str(line, "path")
    if not path or path == "" then
      client:send(to_json({event = "error", message = "reference_start requires path"}) .. "\n")
    else
      recording.active = true
      recording.buffer = {}
      recording.frame_index = 0
      recording.output_path = path
      client:send("ok:recording\n")
      log("Recording started: " .. path)
    end
  elseif decoded_event == "reference_stop" then
    if recording.active then
      recording.active = false
      local path = recording.output_path
      local count = #recording.buffer
      if count > 0 and path then
        flush_spinrec(path, game_id, recording.buffer)
        send_event({event = "rec_saved", path = path, frame_count = count})
      end
      recording.buffer = {}
      recording.frame_index = 0
      recording.output_path = nil
      client:send("ok:stopped\n")
      log("Recording stopped: " .. count .. " frames")
    else
      client:send("ok:not_recording\n")
    end
  elseif decoded_event == "replay" then
    if practice.active or recording.active then
      client:send(to_json({event = "replay_error", message = "cannot replay during practice or recording"}) .. "\n")
    else
      local path = json_get_str(line, "path")
      local speed = json_get_num(line, "speed") or 0
      if not path then
        client:send(to_json({event = "replay_error", message = "replay requires path"}) .. "\n")
      else
        local rec, read_err = read_spinrec(path)
        if not rec then
          client:send(to_json({event = "replay_error", message = read_err}) .. "\n")
        elseif game_id and rec.game_id:gsub("%z+$", "") ~= game_id then
          client:send(to_json({event = "replay_error", message = "game_id mismatch"}) .. "\n")
        else
          replay.frames = rec.frames
          replay.total = rec.frame_count
          replay.index = 1
          replay.path = path
          replay.speed = speed
          replay.last_progress_ms = os.clock() * 1000
          -- Load companion .mss
          local mss_path = path:gsub("%.spinrec$", ".mss")
          table.insert(pending_loads, mss_path)
          -- Set speed (PoC validation: confirm emu.setSpeed exists)
          replay.prev_speed = json_get_num(line, "prev_speed") or 100
          if emu.setSpeed then
            emu.setSpeed(speed)
          end
          replay.active = true
          client:send(to_json({event = "replay_started", path = path, frame_count = rec.frame_count}) .. "\n")
          log("Replay started: " .. path .. " (" .. rec.frame_count .. " frames, speed=" .. speed .. ")")
        end
      end
    end
  elseif decoded_event == "replay_stop" then
    if replay.active then
      replay.active = false
      replay.frames = {}
      replay.index = 1
      if replay.prev_speed and emu.setSpeed then
        emu.setSpeed(replay.prev_speed)
      end
      replay.path = nil
      client:send("ok:replay_stopped\n")
      log("Replay stopped by command")
    else
      client:send("ok:not_replaying\n")
    end
  end
end

local text_commands = {
  save = function()
    table.insert(pending_saves, TEST_STATE_FILE)
    client:send("ok:queued\n")
  end,
  load = function()
    table.insert(pending_loads, TEST_STATE_FILE)
    client:send("ok:queued\n")
  end,
  practice_stop = function()
    practice_reset()
    pending_loads     = {}
    client:send("ok\n")
    log("Practice mode stopped")
  end,
  reset = function()
    practice_reset()
    pending_loads     = {}
    pending_saves     = {}
    pending_reset     = true
    client:send("ok\n")
    log("Reset queued: practice cleared, SNES reset on next cpuExec")
  end,
  ping = function()
    client:send("pong\n")
  end,
  quit = function()
    client:send("bye\n")
    client:close()
    client = nil
    log("TCP client disconnected")
  end,
}

local prefixed_commands = {
  ["load"] = function(arg)
    table.insert(pending_loads, arg)
    client:send("ok:queued\n")
  end,
  ["save"] = function(arg)
    table.insert(pending_saves, arg)
    client:send("ok:queued\n")
  end,
  ["practice_load"] = function(arg)
    local json_str = arg
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
      table.insert(pending_loads, sp)
      practice.start_ms = ts_ms()
      client:send("ok:queued\n")
      log("Practice load queued: " .. (practice.segment.id or "?"))
    end
  end,
}

local function tcp_dispatch(line)
  log("TCP received: " .. line)

  -- Extension hook: let external scripts handle messages first
  if poke_handler then
    local handled = poke_handler(line)
    if handled then return end
  end

  if line:sub(1, 1) == "{" then
    handle_json_message(line)
    return
  end

  -- Exact-match text commands
  local handler = text_commands[line]
  if handler then
    handler()
    return
  end

  -- Prefixed commands (prefix:argument)
  local prefix, arg = line:match("^([^:]+):(.+)$")
  if prefix then
    local pfx_handler = prefixed_commands[prefix]
    if pfx_handler then
      pfx_handler(arg)
      return
    end
  end

  client:send("err:unknown_command\n")
end

local function handle_tcp()
  if not client then
    tcp_accept()
    return
  end
  if not tcp_heartbeat() then return end

  local line, err = client:receive("*l")
  if line then
    tcp_dispatch(line)
  elseif err ~= "timeout" then
    -- any error other than "no data yet" = connection gone (closed, reset, etc.)
    log("TCP client disconnected: " .. tostring(err))
    pcall(function() client:close() end)  -- safe close, may already be dead
    client = nil
    disconnect_cleanup()
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
  if key_just_pressed("T") then table.insert(pending_saves, TEST_STATE_FILE) end
  if key_just_pressed("Y") then table.insert(pending_loads, TEST_STATE_FILE) end
end

local function on_cpu_exec(address)
  while #pending_saves > 0 do
    local path = table.remove(pending_saves, 1)
    save_state_to_file(path)
  end
  if pending_rec_save then
    local path = pending_rec_save
    pending_rec_save = nil
    save_state_to_file(path)
  end
  while #pending_loads > 0 do
    local path = table.remove(pending_loads, 1)
    load_state_from_file(path)
  end
  if pending_reset then
    pending_reset = false
    emu.reset()
    log("SNES reset executed")
  end
end

local function on_input_polled()
  if recording.active then
    if recording.frame_index == 0 then
      -- Capture frame 0 save state via dedicated pending variable
      local mss_path = recording.output_path:gsub("%.spinrec$", ".mss")
      pending_rec_save = mss_path
    end
    local input = emu.getInput(0)
    recording.buffer[#recording.buffer + 1] = encode_input(input)
    recording.frame_index = recording.frame_index + 1
    if recording.frame_index >= MAX_RECORDING_FRAMES then
      log("WARNING: Recording hit MAX_RECORDING_FRAMES (" .. MAX_RECORDING_FRAMES .. "), auto-stopping")
      local path = recording.output_path
      local count = #recording.buffer
      if count > 0 and path then
        flush_spinrec(path, game_id, recording.buffer)
        send_event({event = "rec_saved", path = path, frame_count = count})
      end
      recording.active = false
      recording.buffer = {}
      recording.frame_index = 0
      recording.output_path = nil
    end
  elseif replay.active and replay.index <= replay.total then
    emu.setInput(0, decode_input(replay.frames[replay.index]))
    replay.index = replay.index + 1
    -- Progress reporting (wall-clock throttled)
    local now = os.clock() * 1000
    if now - replay.last_progress_ms >= 100 then
      replay.last_progress_ms = now
      send_event({event = "replay_progress", frame = replay.index - 1, total = replay.total})
    end
    -- Check if replay finished
    if replay.index > replay.total then
      send_event({event = "replay_finished", path = replay.path, frames_played = replay.total})
      -- Restore speed
      if replay.prev_speed then
        emu.setSpeed(replay.prev_speed)
      end
      replay.active = false
      replay.frames = {}
      replay.index = 1
      replay.path = nil
      log("Replay finished")
    end
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

  pcall(check_keyboard)
  handle_tcp()

  draw_practice_overlay()
end

-- Register callbacks
emu.addEventCallback(on_start_frame, emu.eventType.startFrame)
emu.addEventCallback(on_input_polled, emu.eventType.inputPolled)
log("SpinLab script loaded")
