-- overlay.lua — HUD overlay drawing for Mesen2.
-- All functions are global (loaded via dofile).
-- Overlay functions take state tables as arguments to avoid coupling.

-- drawString renders one char per row vertically in Mesen2 — work around it
-- by drawing one character at a time with manual x offsets.
local CHAR_W = 6  -- measureString("A", 1).width

-- SNES screen dimensions (used for full-screen overlays such as the death blackout).
local SCREEN_W = 256
local SCREEN_H = 224

function draw_text(x, y, text, fg, bg)
  for i = 1, #text do
    emu.drawString(x + (i - 1) * CHAR_W, y, text:sub(i, i), fg, bg, 1)
  end
end

function ms_to_display(ms)
  -- Format milliseconds as M:SS.d (e.g. 75340 -> "1:15.3")
  if not ms then return "?" end
  local total_s = math.floor(ms / 100) / 10
  local m = math.floor(total_s / 60)
  local s = total_s - m * 60
  return string.format("%d:%04.1f", m, s)
end

-- Draw the elapsed timer.  If compare_time is given the timer color hints
-- whether the player is ahead (green) or behind (red), but the compare value
-- itself is intentionally not rendered — a stale "/ ?" reads as broken UI.
function draw_timer_row(y, elapsed, compare_time, prefix)
  local timer_color
  if compare_time then
    timer_color = (elapsed < compare_time) and 0xFF44FF44 or 0xFFFF4444
  else
    timer_color = 0xFFFFFFFF
  end
  local text = (prefix and (prefix .. "  ") or "") .. ms_to_display(elapsed)
  draw_text(4, y, text, 0x00000000, timer_color)
end

-- Practice mode overlay. Pass the practice state table and current timestamp (ms).
function draw_practice_overlay(prac, now_ms)
  if not prac.active then return end

  local label = prac.segment and prac.segment.description or "?"
  if label == "" then label = "?" end
  local compare_time = prac.segment and prac.segment.expected_time_ms

  draw_text(4, 2, label, 0x00000000, 0xFFFFFFFF)

  if prac.state == "playing" or prac.state == "loading" then
    local elapsed = now_ms - prac.start_ms
    draw_timer_row(12, elapsed, compare_time)
  elseif prac.state == "result" then
    draw_timer_row(12, prac.elapsed_ms, compare_time, prac.completed and "Clear!" or "")
  end
end

-- Speed run overlay. Pass the speed_run state table and current timestamp (ms).
function draw_speed_run_overlay(sr, now_ms)
  if not sr.active then return end

  local label = sr.segment and sr.segment.description or "?"
  if label == "" then label = "?" end
  local compare_time = sr.segment and sr.segment.expected_time_ms

  if sr.state == "playing" or sr.state == "loading" then
    local elapsed = now_ms - sr.start_ms
    draw_text(4, 2, label, 0x00000000, 0xFF44DDFF)
    draw_timer_row(12, elapsed, compare_time)

  elseif sr.state == "dying" then
    -- Black out the full screen so the player isn't looking at a confusing
    -- frame from the cold save state (mid fade-in or similar).
    emu.drawRectangle(0, 0, SCREEN_W, SCREEN_H, 0xFF000000, true)
    draw_text(4, 2, label, 0x00000000, 0xFF44DDFF)
    local remaining = sr.death_delay_ms - (now_ms - sr.death_started_ms)
    local secs = string.format("%.1f", math.max(0, remaining / 1000))
    draw_text(4, 12, "Respawning in " .. secs .. "s", 0x00000000, 0xFFFF8888)

  elseif sr.state == "result" then
    draw_text(4, 2, label, 0x00000000, 0xFF44DDFF)
    draw_timer_row(12, sr.elapsed_ms, compare_time, "Clear!")

    local remaining = sr.auto_advance_ms - (now_ms - sr.result_start_ms)
    local secs = string.format("%.1f", math.max(0, remaining / 1000))
    draw_text(4, 22, "Next in " .. secs .. "s", 0x00000000, 0xFF888888)
  end
end
