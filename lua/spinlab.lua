-- SpinLab — Mesen2 Lua Script
-- Steps 1+2 complete: Save State PoC + Passive Recorder
-- Step 4: Practice loop MVP
--
-- Keyboard (manual testing):
--   T = save state to test file
--   Y = load state from test file
-- TCP commands: JSON messages via send_command() (see handle_json_message)
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
local AUTO_ADVANCE_DEFAULT_MS = 2000
-- Default delay between dying and reloading the cold save state in speed run mode.
-- The cold save state is captured at level_start 0->1, which lands during the
-- post-death fade-in, so loading it instantly looks like it's replaying the
-- death.  A short blackout gives the death some weight without making practice
-- feel sluggish.  Overridden per run via speed_run_load.death_delay_ms.
local DEATH_DELAY_DEFAULT_MS = 1500
local REPLAY_PROGRESS_INTERVAL_MS = 100
-- API uses speed=0 to mean "uncapped", but Mesen's setSpeed(0) means "paused".
-- Use this constant to make the intent clear and avoid accidentally pausing.
local SPEED_UNCAPPED = 0
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

-- Shared modules — loaded from lua/ directory via dofile().
-- Bootstrap: resolve lua dir so dofile can find sibling scripts.
local _boot_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
if _boot_dir == "" then
    local f = io.open(DATA_DIR .. "/lua_dir.txt", "r")
    if f then _boot_dir = f:read("*l") or ""; f:close() end
end
LUA_DIR = _boot_dir
dofile(LUA_DIR .. "addresses.lua")
dofile(LUA_DIR .. "json.lua")
dofile(LUA_DIR .. "overlay.lua")
dofile(LUA_DIR .. "spinrec.lua")

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
local state_just_loaded = false  -- set by on_cpu_exec after loading a save state

-- Keyboard debounce
local key_was_pressed = {}

-- Condition definitions, populated via TCP set_conditions command.
-- Each entry: { name=string, address=int, size=int }
local condition_defs = {}

-- Invalidation combo (SNES button names). Overridden via set_invalidate_combo TCP command.
local invalidate_combo = { "L", "Select" }
local invalidate_prev_down = false

-- Passive recorder state
local prev = {}              -- previous frame memory values
local level_start_frame = 0  -- frame when current level entrance was logged

-- Cold-fill state (captures cold starts after reference run)
local cold_fill = {
  active = false,
  state = nil,            -- "waiting_death" or "waiting_spawn"
  segment_id = nil,
  prev_anim = 0,
  prev_level_start = 0,
}
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

-- Practice / speed-run mode state
local PSTATE_IDLE    = "idle"
local PSTATE_LOADING = "loading"
local PSTATE_PLAYING = "playing"
local PSTATE_DYING   = "dying"   -- speed run only: post-death blackout before respawn
local PSTATE_RESULT  = "result"

local practice = {
    active = false,
    state = PSTATE_IDLE,
    segment = nil,
    start_ms = 0,
    elapsed_ms = 0,
    completed = false,
    result_start_ms = 0,
    auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS,
    deaths = 0,           -- number of deaths in the current attempt
    last_death_ms = 0,    -- timestamp of most recent death reload
}

local function reset_mode_state(tbl)
    tbl.active = false
    tbl.state = PSTATE_IDLE
    tbl.segment = nil
    tbl.start_ms = 0
    tbl.elapsed_ms = 0
    tbl.result_start_ms = 0
    tbl.auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS
    reset_transition_state()
end

local function practice_reset()
    reset_mode_state(practice)
    practice.completed = false
    practice.deaths = 0
    practice.last_death_ms = 0
end

-- Speed run state
local speed_run = {
    active = false,
    state = PSTATE_IDLE,
    segment = nil,
    start_ms = 0,
    split_ms = 0,
    elapsed_ms = 0,
    respawn_path = "",
    cp_index = 0,
    result_start_ms = 0,
    result_split_ms = 0,
    auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS,
    death_delay_ms = DEATH_DELAY_DEFAULT_MS,
    death_started_ms = 0,
}

local function speed_run_reset()
    reset_mode_state(speed_run)
    speed_run.split_ms = 0
    speed_run.respawn_path = ""
    speed_run.cp_index = 0
    speed_run.result_split_ms = 0
    speed_run.death_delay_ms = DEATH_DELAY_DEFAULT_MS
    speed_run.death_started_ms = 0
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
  speed = SPEED_UNCAPPED,  -- SPEED_UNCAPPED = max, 100 = normal
  prev_speed = nil,   -- speed to restore after replay
  last_progress_ms = 0,  -- wall-clock time of last progress event
}

-----------------------------------------------------------------------
-- HELPERS
-----------------------------------------------------------------------
function log(msg)
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
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or AUTO_ADVANCE_DEFAULT_MS,
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
    death_penalty_ms       = json_get_num(json_str, "death_penalty_ms") or 3200,
    end_on_goal            = end_on_goal,
    end_type               = end_type,
  }
end

-- Parse a JSON array of checkpoint objects from speed_run_load.
local function parse_checkpoints(json_str)
  local arr_str = json_get_arr(json_str, "checkpoints")
  if not arr_str or arr_str == "[]" then return {} end

  local result = {}
  for obj in arr_str:gmatch('%{[^}]+%}') do
    local ordinal = json_get_num(obj, "ordinal") or 0
    local state_path = json_get_str(obj, "state_path") or ""
    result[#result + 1] = { ordinal = ordinal, state_path = state_path }
  end
  return result
end

local function parse_speed_run_segment(json_str)
  return {
    id                     = json_get_str(json_str, "id") or "",
    state_path             = json_get_str(json_str, "state_path") or "",
    description            = json_get_str(json_str, "description") or "",
    checkpoints            = parse_checkpoints(json_str),
    expected_time_ms       = json_get_num(json_str, "expected_time_ms"),
    auto_advance_delay_ms  = json_get_num(json_str, "auto_advance_delay_ms") or AUTO_ADVANCE_DEFAULT_MS,
    death_delay_ms         = json_get_num(json_str, "death_delay_ms") or DEATH_DELAY_DEFAULT_MS,
  }
end

-----------------------------------------------------------------------
-- JSONL LOGGER
-----------------------------------------------------------------------

local function send_event(event)
  if not client then return end
  if practice.active then return end
  if speed_run.active then return end
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
-- CONDITIONS
-----------------------------------------------------------------------

-- Parse a JSON array of condition objects: [{"name":"...","address":N,"size":N}, ...]
-- Returns a Lua array of tables, or nil + error message on failure.
local function parse_conditions_json(json_str)
  local defs = {}
  -- Strip outer array brackets
  local body = json_str:match("^%s*%[(.*)%]%s*$")
  if not body then
    return nil, "expected JSON array"
  end
  -- Iterate over object entries: {...}
  for obj in body:gmatch("{[^}]+}") do
    local name    = json_get_str(obj, "name")
    local address = json_get_num(obj, "address")
    local size    = json_get_num(obj, "size")
    if not name or not address or not size then
      return nil, "each condition must have name, address, and size: " .. obj
    end
    defs[#defs + 1] = { name = name, address = address, size = size }
  end
  return defs
end

-- Read raw condition values from memory at the current frame.
-- Returns an empty table if no conditions are configured.
local function read_conditions()
  local out = {}
  for _, d in ipairs(condition_defs) do
    if d.size == 1 then
      out[d.name] = emu.read(d.address, SNES, false)
    elseif d.size == 2 then
      out[d.name] = emu.readWord(d.address, SNES, false)
    else
      -- Fail loud: unexpected condition size
      error("unsupported condition size: " .. tostring(d.size) .. " for " .. tostring(d.name))
    end
  end
  return out
end

-----------------------------------------------------------------------
-- TRANSITION DETECTION
-----------------------------------------------------------------------
local function goal_type(curr)
  if curr.io_port == IO_KEY then return "key"
  elseif curr.io_port == IO_ORB then return "orb"
  elseif curr.boss_defeat ~= 0 and curr.fanfare == 1 then return "boss"
  elseif curr.fanfare == 1 or curr.io_port == IO_GOAL then return "normal"
  else return "abort"  -- start+select, death exit, etc.
  end
end

local function on_level_entrance(curr, state_path)
  level_start_frame = frame_counter
  local event_data = {
    event        = "level_entrance",
    level        = curr.level_num,
    room         = curr.room_num,
    frame        = frame_counter,
    timestamp_ms = ts_ms(),
    session    = "passive",
    state_path = state_path or "",
    conditions = read_conditions(),
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
      conditions = read_conditions(),
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
    event        = "level_exit",
    level        = curr.level_num,
    room         = curr.room_num,
    goal         = goal,
    elapsed_ms   = elapsed,
    frame        = frame_counter,
    timestamp_ms = ts_ms(),
    session    = "passive",
    conditions = read_conditions(),
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
  local got_orb     = curr.io_port == IO_ORB
  local got_goal    = curr.fanfare == 1 or curr.io_port == IO_GOAL
  local got_key     = curr.io_port == IO_KEY
  local got_fadeout = curr.io_port == IO_FADEOUT

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
        conditions  = read_conditions(),
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
  -- Fast retry: SMW can skip the 0→1 edge on level_start. If we know the
  -- player died (died_flag) and the death anim just ended (anim was 9, now
  -- isn't), treat that as a respawn even if level_start stayed at 1.
  local edge_spawn = curr.level_start == 1 and prev.level_start == 0
  local fast_retry = transition_state.died_flag
      and curr.level_start == 1
      and curr.player_anim ~= 9 and prev.player_anim == 9
  if (edge_spawn or fast_retry) and not exit_this_frame then
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
        conditions     = read_conditions(),
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
-- COLD-FILL MODE (captures cold save states after reference run)
-----------------------------------------------------------------------
local CFSTATE_WAITING_DEATH = "waiting_death"
local CFSTATE_WAITING_SPAWN = "waiting_spawn"

local function handle_cold_fill()
  local anim = emu.read(ADDR_PLAYER_ANIM, SNES, false)
  local level_start = emu.read(ADDR_LEVEL_START, SNES, false)

  if cold_fill.state == CFSTATE_WAITING_DEATH then
    -- Detect death: player_anim transitions to 9
    if anim == 9 and cold_fill.prev_anim ~= 9 then
      cold_fill.state = CFSTATE_WAITING_SPAWN
      log("Cold-fill: death detected, waiting for spawn")
    end

  elseif cold_fill.state == CFSTATE_WAITING_SPAWN then
    -- Detect spawn: level_start 0→1 (normal respawn) OR player leaves death
    -- anim while level_start is already 1 (fast retry skips the 0→1 edge).
    local edge_spawn = level_start == 1 and cold_fill.prev_level_start == 0
    local fast_retry = level_start == 1 and anim ~= 9 and cold_fill.prev_anim == 9
    if edge_spawn or fast_retry then
      -- Capture cold save state
      local game_dir = STATE_DIR .. "/" .. (game_id or "unknown")
      ensure_dir(game_dir)
      local path = game_dir .. "/cold_" .. cold_fill.segment_id:gsub("[:/]", "_") .. ".mss"
      table.insert(pending_saves, path)
      send_event({
        event = "spawn",
        is_cold_cp = true,
        state_captured = true,
        state_path = path,
        segment_id = cold_fill.segment_id,
        conditions = read_conditions(),
      })
      log("Cold-fill: spawn captured for " .. cold_fill.segment_id)
      cold_fill.active = false
      cold_fill.state = nil
      cold_fill.segment_id = nil
    end
  end

  cold_fill.prev_anim = anim
  cold_fill.prev_level_start = level_start
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
      practice.deaths = practice.deaths + 1
      practice.last_death_ms = ts_ms()
      table.insert(pending_loads, practice.segment.state_path)
      log("Practice: death #" .. practice.deaths .. " — reloading state")

    elseif practice.segment.end_type == "checkpoint" and check_checkpoint_hit(curr) then
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: CHECKPOINT — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. ", penalty=" .. penalty .. "ms)")

    elseif practice.segment.end_on_goal and detect_finish(curr) then
      -- Early finish: goal/orb/key/boss detected, skip fanfare wait
      local finish_goal = detect_finish(curr)
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = true
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: FINISH (" .. finish_goal .. ") — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. ", penalty=" .. penalty .. "ms)")

    elseif is_exit_frame(curr) then
      -- Late exit: full exit_mode transition (fallback when end_on_goal is off)
      local goal = goal_type(curr)
      local penalty = practice.segment.death_penalty_ms * practice.deaths
      practice.elapsed_ms = ts_ms() - practice.start_ms + penalty
      practice.completed  = (goal ~= "abort")
      practice.state      = PSTATE_RESULT
      practice.result_start_ms = ts_ms()
      log("Practice: RESULT (" .. goal .. ") — " .. practice.elapsed_ms .. "ms (deaths=" .. practice.deaths .. ", penalty=" .. penalty .. "ms)")
    end

  elseif practice.state == PSTATE_RESULT then
    -- Auto-advance after delay
    local elapsed_in_result = ts_ms() - practice.result_start_ms
    if elapsed_in_result >= practice.auto_advance_ms then
      -- Compute clean_tail_ms: time from last death reload to completion,
      -- or full elapsed (minus penalty) if no deaths occurred.
      local raw_elapsed = practice.elapsed_ms - (practice.segment.death_penalty_ms * practice.deaths)
      local clean_tail = nil
      if practice.completed then
        if practice.deaths == 0 then
          clean_tail = math.floor(raw_elapsed)
        elseif practice.last_death_ms > 0 then
          clean_tail = math.floor(practice.result_start_ms - practice.last_death_ms)
        end
      end

      -- Send result to orchestrator
      local result = to_json({
        event         = "attempt_result",
        segment_id    = practice.segment.id,
        completed     = practice.completed,
        time_ms       = math.floor(practice.elapsed_ms),
        deaths        = practice.deaths,
        clean_tail_ms = clean_tail,
        goal          = practice.segment.goal,
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
-- SPEED RUN STATE MACHINE
-----------------------------------------------------------------------
local function handle_speed_run(curr)
  if speed_run.state == PSTATE_LOADING then
    speed_run.state    = PSTATE_PLAYING
    speed_run.start_ms = ts_ms()
    speed_run.split_ms = ts_ms()

  elseif speed_run.state == PSTATE_PLAYING then
    -- Death check (highest priority).  We send the death event immediately
    -- so split timing is honest, but defer the actual respawn to PSTATE_DYING
    -- so the player gets a brief blackout instead of an instant snap that
    -- looks like the cold save state replaying its own fade-in.
    if is_death_frame(curr) then
      local elapsed = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      if client then
        client:send(to_json({
          event      = "speed_run_death",
          elapsed_ms = math.floor(elapsed),
          split_ms   = math.floor(split),
        }) .. "\n")
      end
      speed_run.state = PSTATE_DYING
      speed_run.death_started_ms = ts_ms()
      log("Speed run: death — blackout for " .. speed_run.death_delay_ms .. "ms")

    elseif check_checkpoint_hit(curr) then
      local elapsed = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      local cps = speed_run.segment.checkpoints
      speed_run.cp_index = speed_run.cp_index + 1
      if speed_run.cp_index <= #cps then
        speed_run.respawn_path = cps[speed_run.cp_index].state_path
        local ordinal = cps[speed_run.cp_index].ordinal
        if client then
          client:send(to_json({
            event      = "speed_run_checkpoint",
            ordinal    = ordinal,
            elapsed_ms = math.floor(elapsed),
            split_ms   = math.floor(split),
          }) .. "\n")
        end
        log("Speed run: checkpoint " .. ordinal .. " — " .. math.floor(elapsed) .. "ms")
      end

    elseif detect_finish(curr) or is_exit_frame(curr) then
      speed_run.elapsed_ms = ts_ms() - speed_run.start_ms
      local split = ts_ms() - speed_run.split_ms
      speed_run.state = PSTATE_RESULT
      speed_run.result_start_ms = ts_ms()
      speed_run.result_split_ms = split
      log("Speed run: GOAL — " .. math.floor(speed_run.elapsed_ms) .. "ms")
    end

  elseif speed_run.state == PSTATE_DYING then
    -- Hold a black overlay (drawn by the speed-run overlay function) while
    -- waiting out death_delay_ms, then queue the cold-state load and resume.
    -- We bump start_ms by the blackout length so the level timer doesn't
    -- count time the player spent staring at a black screen.
    if ts_ms() - speed_run.death_started_ms >= speed_run.death_delay_ms then
      table.insert(pending_loads, speed_run.respawn_path)
      local elapsed_blackout = ts_ms() - speed_run.death_started_ms
      speed_run.start_ms = speed_run.start_ms + elapsed_blackout
      speed_run.split_ms = ts_ms()
      speed_run.state = PSTATE_PLAYING
      log("Speed run: respawn — reloading " .. speed_run.respawn_path)
    end

  elseif speed_run.state == PSTATE_RESULT then
    local elapsed_in_result = ts_ms() - speed_run.result_start_ms
    if elapsed_in_result >= speed_run.auto_advance_ms then
      if client then
        client:send(to_json({
          event      = "speed_run_complete",
          elapsed_ms = math.floor(speed_run.elapsed_ms),
          split_ms   = math.floor(speed_run.result_split_ms),
        }) .. "\n")
      end
      speed_run_reset()
      log("Speed run: level complete, sent result")
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
  if speed_run.active then
    speed_run_reset()
    pending_loads     = {}
    pending_saves     = {}
    pending_reset     = true
    log("Speed run auto-cleared on disconnect — reset queued")
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
      local speed = json_get_num(line, "speed") or SPEED_UNCAPPED
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
          replay.prev_speed = json_get_num(line, "prev_speed") or 100
          replay.requested_speed = speed
          -- SPEED_UNCAPPED means "run as fast as possible". In --testRunner
          -- mode, emulation is already uncapped. Only call setSpeed for an
          -- explicit speed request (GUI mode).
          if emu.setSpeed and speed ~= SPEED_UNCAPPED then
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
  elseif decoded_event == "fill_gap_load" then
    local path = json_get_str(line, "state_path")
    if not path then
      client:send(to_json({event = "error", message = "fill_gap_load requires state_path"}) .. "\n")
    else
      table.insert(pending_loads, path)
      cold_fill.active = true
      cold_fill.state = CFSTATE_WAITING_DEATH
      cold_fill.segment_id = "fill_gap"
      cold_fill.prev_anim = 0
      cold_fill.prev_level_start = 0
      client:send("ok:fill_gap\n")
      log("Fill-gap: loaded state -- die to capture cold start")
    end
  elseif decoded_event == "cold_fill_load" then
    local path = json_get_str(line, "state_path")
    local seg_id = json_get_str(line, "segment_id")
    if not path or not seg_id then
      client:send(to_json({event = "error", message = "cold_fill_load requires state_path and segment_id"}) .. "\n")
    else
      table.insert(pending_loads, path)
      cold_fill.active = true
      cold_fill.state = CFSTATE_WAITING_DEATH
      cold_fill.segment_id = seg_id
      cold_fill.prev_anim = 0
      cold_fill.prev_level_start = 0
      client:send("ok:cold_fill\n")
      log("Cold-fill: loaded " .. seg_id .. " -- die to capture cold start")
    end
  elseif decoded_event == "set_conditions" then
    local defs_str = json_get_arr(line, "definitions")
    if not defs_str then
      client:send("err:set_conditions_invalid\n")
      return
    end
    local defs, err = parse_conditions_json(defs_str)
    if not defs then
      log("set_conditions: invalid payload — " .. tostring(err))
      client:send("err:set_conditions_invalid\n")
      return
    end
    condition_defs = defs
    client:send("ok:conditions_set\n")
    log("set_conditions: loaded " .. #condition_defs .. " conditions")
  elseif decoded_event == "set_invalidate_combo" then
    local combo_str = json_get_arr(line, "combo")
    if not combo_str then
      client:send("err:set_invalidate_combo_invalid\n")
      return
    end
    local ok, result = pcall(parse_string_array, combo_str)
    if not ok then
      log("set_invalidate_combo: invalid payload — " .. tostring(result))
      client:send("err:set_invalidate_combo_invalid\n")
      return
    end
    invalidate_combo = result
    client:send("ok:invalidate_combo_set\n")
    log("set_invalidate_combo: " .. table.concat(result, ","))
  elseif decoded_event == "practice_load" then
    practice.segment = parse_practice_segment(line)
    practice.auto_advance_ms = practice.segment.auto_advance_delay_ms or 2000
    practice.active = true
    practice.state = PSTATE_LOADING
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
  elseif decoded_event == "practice_stop" then
    practice_reset()
    pending_loads = {}
    client:send("ok\n")
    log("Practice mode stopped")
  elseif decoded_event == "speed_run_load" then
    speed_run.segment = parse_speed_run_segment(line)
    speed_run.auto_advance_ms = speed_run.segment.auto_advance_delay_ms or 2000
    speed_run.death_delay_ms = speed_run.segment.death_delay_ms or DEATH_DELAY_DEFAULT_MS
    speed_run.respawn_path = speed_run.segment.state_path
    speed_run.cp_index = 0
    speed_run.active = true
    speed_run.state = PSTATE_LOADING
    local sp = speed_run.segment.state_path
    if not sp or sp == "" then
      log("ERROR: No valid state_path for speed_run segment " .. (speed_run.segment.id or "?"))
      client:send("err:no_state_path\n")
      speed_run_reset()
    else
      table.insert(pending_loads, sp)
      speed_run.start_ms = ts_ms()
      speed_run.split_ms = ts_ms()
      client:send("ok:queued\n")
      log("Speed run load queued: " .. (speed_run.segment.id or "?"))
    end
  elseif decoded_event == "speed_run_stop" then
    speed_run_reset()
    pending_loads = {}
    client:send("ok\n")
    log("Speed run stopped")
  else
    log("ERROR: unknown JSON command: " .. tostring(decoded_event))
    client:send(to_json({event = "error", message = "unknown command: " .. tostring(decoded_event)}) .. "\n")
  end
end

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

  log("ERROR: unknown command: " .. line)
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

-- Returns true if all buttons in invalidate_combo are held on controller 0.
local function combo_pressed()
  local input = emu.getInput(0)
  for _, btn in ipairs(invalidate_combo) do
    if not input[btn] then return false end
  end
  return true
end

-- Fire attempt_invalidated event on the rising edge of the invalidation combo,
-- but only while practice mode is active. Resets edge state when leaving practice.
local function check_invalidate_combo()
  if not practice.active then
    invalidate_prev_down = false
    return
  end
  local down = combo_pressed()
  if down and not invalidate_prev_down then
    if client then
      client:send(to_json({ event = "attempt_invalidated" }) .. "\n")
    end
    log("attempt_invalidated: combo pressed")
  end
  invalidate_prev_down = down
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
    state_just_loaded = true
  end
  if pending_reset then
    pending_reset = false
    emu.reset()
    state_just_loaded = true
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
    emu.setInput(decode_input(replay.frames[replay.index]))
    replay.index = replay.index + 1
    local now = os.clock() * 1000
    if now - replay.last_progress_ms >= REPLAY_PROGRESS_INTERVAL_MS then
      replay.last_progress_ms = now
      send_event({event = "replay_progress", frame = replay.index - 1, total = replay.total})
    end
    if replay.index > replay.total then
      if replay.prev_speed and emu.setSpeed then
        emu.setSpeed(replay.prev_speed)
      end
      send_event({event = "replay_finished", path = replay.path, frames_played = replay.total})
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

  -- After a save-state load, memory has been replaced wholesale.  Re-sync
  -- prev so that edge-detection helpers (detect_finish, check_checkpoint_hit,
  -- is_death_frame, is_exit_frame) don't see phantom transitions from the
  -- stale pre-load snapshot.
  if state_just_loaded then
    prev = read_mem()
    state_just_loaded = false
  end

  local curr = read_mem()
  if cold_fill.active then
    handle_cold_fill()
  elseif practice.active then
    handle_practice(curr)
  elseif speed_run.active then
    handle_speed_run(curr)
  else
    detect_transitions(curr)
  end
  prev = curr

  pcall(check_keyboard)
  pcall(check_invalidate_combo)
  handle_tcp()

  draw_practice_overlay(practice, ts_ms())
  draw_speed_run_overlay(speed_run, ts_ms())
end

-- Register callbacks
emu.addEventCallback(on_start_frame, emu.eventType.startFrame)
emu.addEventCallback(on_input_polled, emu.eventType.inputPolled)
log("SpinLab script loaded")
