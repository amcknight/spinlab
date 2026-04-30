-- Poke Engine — Integration test harness for spinlab.lua
--
-- Usage: Mesen.exe --testrunner <rom> lua/poke_engine.lua
--
-- Boot sequence:
--   1. This script registers a startFrame callback (poke injector)
--   2. dofile loads spinlab.lua, which registers its own callbacks
--   3. Mesen fires callbacks in registration order:
--      poke_engine emu.write() → spinlab emu.read() → detect_transitions()
--
-- Protocol (multi-scenario):
--   Python connects, sends game_context, then sends poke_scenario messages
--   sequentially. After each scenario completes (settle window expires), the
--   engine sends {"event":"scenario_done"}, resets all state, and waits for
--   the next poke_scenario. Send {"event":"quit"} to stop the emulator.

local SNES = emu.memType.snesMemory

-----------------------------------------------------------------------
-- SHARED MODULES (addresses, JSON helpers)
-----------------------------------------------------------------------
local _pe_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])") or ""
if _pe_dir == "" then
    local _pe_data = emu.getScriptDataFolder()
    local f = io.open(_pe_data .. "/lua_dir.txt", "r")
    if f then _pe_dir = f:read("*l") or ""; f:close() end
end
dofile(_pe_dir .. "addresses.lua")
dofile(_pe_dir .. "json.lua")

local ADDR_MAP = {
  game_mode    = ADDR_GAME_MODE,
  level_num    = ADDR_LEVEL_NUM,
  room_num     = ADDR_ROOM_NUM,
  level_start  = ADDR_LEVEL_START,
  player_anim  = ADDR_PLAYER_ANIM,
  exit_mode    = ADDR_EXIT_MODE,
  io_port      = ADDR_IO,
  fanfare      = ADDR_FANFARE,
  boss_defeat  = ADDR_BOSS_DEFEAT,
  midway       = ADDR_MIDWAY,
  cp_entrance  = ADDR_CP_ENTRANCE,
}

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local poke_schedule = {}   -- {[frame_number] = {{addr=int, value=int}, ...}}
local held_values = {}     -- {[addr] = value} — actively held until overridden
local scenario_loaded = false
local scenario_start_frame = nil
local last_poke_frame = 0
local settle_frames = 30
local own_frame_counter = 0

-----------------------------------------------------------------------
-- STATE RESET (between scenarios)
-----------------------------------------------------------------------
local function reset_poke_state()
  poke_schedule = {}
  held_values = {}
  scenario_loaded = false
  scenario_start_frame = nil
  last_poke_frame = 0
  settle_frames = 30
  -- Zero all tracked addresses so spinlab sees clean 0→X transitions
  for _, addr in pairs(ADDR_MAP) do
    emu.write(addr, 0, SNES)
  end
end

-----------------------------------------------------------------------
-- MINIMAL JSON PARSER (for poke_scenario message)
-----------------------------------------------------------------------
-- We only need to parse: {"event":"poke_scenario","settle_frames":N,"pokes":[...]}
-- where each poke is {"frame":N,"addr":N,"value":N}

local function parse_poke_scenario(json_str)
  -- Extract settle_frames
  local sf = json_str:match('"settle_frames"%s*:%s*(%d+)')
  if sf then settle_frames = tonumber(sf) end

  -- Extract pokes array and iterate over objects
  local pokes_str = json_str:match('"pokes"%s*:%s*(%[.-%])')
  if not pokes_str then
    emu.log("[PokeEngine] ERROR: no pokes array found")
    return false
  end

  -- Match each {...} object in the array
  for obj in pokes_str:gmatch("{(.-)}") do
    local frame = tonumber(obj:match('"frame"%s*:%s*(%d+)'))
    local addr  = tonumber(obj:match('"addr"%s*:%s*(%d+)'))
    local value = tonumber(obj:match('"value"%s*:%s*(%d+)'))
    if frame and addr and value then
      if not poke_schedule[frame] then
        poke_schedule[frame] = {}
      end
      table.insert(poke_schedule[frame], {addr = addr, value = value})
      if frame > last_poke_frame then
        last_poke_frame = frame
      end
    end
  end

  emu.log("[PokeEngine] Loaded scenario: " .. last_poke_frame .. " frames + " .. settle_frames .. " settle")
  return true
end

-----------------------------------------------------------------------
-- POKE HANDLER (set as global before dofile so spinlab.lua can call it)
-----------------------------------------------------------------------
poke_handler = function(line)
  if line:sub(1, 1) ~= "{" then return false end
  local event = line:match('"event"%s*:%s*"(.-)"')
  if event == "poke_scenario" then
    if parse_poke_scenario(line) then
      scenario_loaded = true
    end
    return true  -- handled
  elseif event == "quit" then
    emu.log("[PokeEngine] Quit received, stopping emulator")
    emu.stop(0)
    return true
  end
  return false  -- not our message, let spinlab handle it
end

-----------------------------------------------------------------------
-- FRAME CALLBACK (registered BEFORE spinlab.lua's dofile)
-----------------------------------------------------------------------
local function on_poke_frame()
  own_frame_counter = own_frame_counter + 1

  if not scenario_loaded then return end

  -- Set start frame on first frame after scenario load
  if not scenario_start_frame then
    scenario_start_frame = own_frame_counter
    emu.log("[PokeEngine] Scenario starts at frame " .. scenario_start_frame)
  end

  local rel_frame = own_frame_counter - scenario_start_frame

  -- Update held values from schedule
  local pokes = poke_schedule[rel_frame]
  if pokes then
    for _, p in ipairs(pokes) do
      held_values[p.addr] = p.value
    end
  end

  -- Write ALL held values every frame (ROM overwrites single-frame pokes)
  for addr, value in pairs(held_values) do
    emu.write(addr, value, SNES)
  end

  -- After settle window: send scenario_done, reset, wait for next scenario
  if rel_frame > last_poke_frame + settle_frames then
    emu.log("[PokeEngine] Scenario complete, sending scenario_done")
    send_raw_event('{"event":"scenario_done"}')
    reset_poke_state()
    -- Reset spinlab detection state for clean next scenario
    if reset_detection_state then
      reset_detection_state()
    end
  end
end

-- Register BEFORE dofile so this fires before spinlab's on_start_frame
emu.addEventCallback(on_poke_frame, emu.eventType.startFrame)

-----------------------------------------------------------------------
-- LOAD SPINLAB
-----------------------------------------------------------------------
-- dofile executes spinlab.lua which registers its own callbacks.
-- Since on_poke_frame was registered first, emu.write() happens before
-- spinlab's read_mem() on each frame.
local script_dir = debug.getinfo(1, "S").source:match("@?(.*[\\/])")
dofile(script_dir .. "spinlab.lua")

emu.log("[PokeEngine] Harness loaded, waiting for poke_scenario commands")
