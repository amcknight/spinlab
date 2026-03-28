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
-- Protocol:
--   After Python connects and sends game_context, it sends a poke_scenario
--   JSON message. The engine parses the poke schedule, then on each frame
--   writes the scheduled values to SNES memory. After the last poke plus
--   settle_frames, it calls emu.stop(0).

local SNES = emu.memType.snesMemory

-----------------------------------------------------------------------
-- ADDRESS MAP (must match spinlab.lua lines 43-53)
-----------------------------------------------------------------------
local ADDR_MAP = {
  game_mode    = 0x0100,
  level_num    = 0x13BF,
  room_num     = 0x010B,
  level_start  = 0x1935,
  player_anim  = 0x0071,
  exit_mode    = 0x0DD5,
  io_port      = 0x1DFB,
  fanfare      = 0x0906,
  boss_defeat  = 0x13C6,
  midway       = 0x13CE,
  cp_entrance  = 0x1B403,
}

-----------------------------------------------------------------------
-- STATE
-----------------------------------------------------------------------
local poke_schedule = {}   -- {[frame_number] = {{addr=int, value=int}, ...}}
local held_values = {}     -- {[addr] = value} — actively held until overridden or released
local scenario_loaded = false
local scenario_start_frame = nil
local last_poke_frame = 0
local settle_frames = 30
local own_frame_counter = 0

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

  -- Stop after settle window
  if rel_frame > last_poke_frame + settle_frames then
    emu.log("[PokeEngine] Scenario complete, stopping emulator")
    emu.stop(0)
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

emu.log("[PokeEngine] Harness loaded, waiting for poke_scenario command")
