-- The season's date (the user, 2026-11-23: "an option in BeamNG.Earth's settings for where the date comes from"): the
-- trees' leaves (seasonlook.lua), the grass (groundcover.lua) and, through the server, the ground photo's season grade
-- (server season.py, --season auto) follow the day of the year from
--   'clock': the PC's date (as before, dev's);
--   'game':  the game's environment date (Time & Weather; core_environment.getTimeOfDay's year, month, day), so a scene
--            keeps its season whatever day it is played or screenshot again.
-- The server is told the game's day (message season_date, rounded to SEND_STEP days: its photo grade and its caches
-- change weekly, not daily); with 'clock' it goes back to its own date.

local M = {}

M.source = 'clock'      -- settings seasonDate
M.SEND_STEP = 7         -- days: the server's photo grade moves in weeks
local DAYS = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }

function M.setSource(v)
  M.source = (v == 'game') and 'game' or 'clock'
end

-- Pure: the day of the year (1..366) of year, month, day; nil for a bad date.
function M.dayOfYear(y, m, d)
  y, m, d = tonumber(y), tonumber(m), tonumber(d)
  if not (y and m and d) or m < 1 or m > 12 or d < 1 or d > 31 then return nil end
  local leap = (y % 4 == 0 and y % 100 ~= 0) or y % 400 == 0
  local n = d
  for i = 1, m - 1 do n = n + DAYS[i] + ((i == 2 and leap) and 1 or 0) end
  return n
end

-- The game's environment date's day of the year, or nil (no environment yet).
function M.gameDay()
  local env = rawget(_G, 'core_environment')
  if not env or not env.getTimeOfDay then return nil end
  local ok, t = pcall(env.getTimeOfDay)
  if not ok or type(t) ~= 'table' then return nil end
  return M.dayOfYear(t.year, t.month, t.day)
end

-- The day of the year the season follows now: the game's when the source is 'game' and it has one, else the PC's.
function M.doy()
  if M.source == 'game' then
    local d = M.gameDay()
    if d then return d end
  end
  return tonumber((os.date('*t') or {}).yday) or 1
end

-- What the server is told: the game's day rounded down to SEND_STEP days with 'game', nil (its own date) with 'clock'.
function M.serverDay()
  if M.source ~= 'game' then return nil end
  local d = M.gameDay()
  if not d then return nil end
  return d - (d - 1) % M.SEND_STEP
end

-- Send the server's day when it changed (net: the connection; force: after a hello). Called by frame, once a second.
local sent = false      -- (false: nothing sent this connection; else the last value, nil meaning 'clock')
function M.sync(net, force)
  if force then sent = false end
  if not net or not net.isConnected or not net.isConnected() then return false end
  local want = M.serverDay()
  if sent ~= false and sent == want then return false end
  net.send({ type = 'season_date', doy = want })
  sent = want
  return true
end
function M.resetSent() sent = false end

-- The frame's call from core (one line there, out of its busy season lines), its work at most once every FRAME_S
-- (the review of PR #1545: not every frame): the server's day sent when it changed (sync; with 'clock' only the one
-- message after a hello or a switch that hands the server back its own date, no environment read), and the anchor's
-- place to the grass (groundcover.setLatitude: the season's hemisphere, the Atlantic's Csb). now: os.clock() (tests
-- pass their own). Returns true when it did the work.
M.FRAME_S = 1.0
local nextFrame = -math.huge
function M.frame(net, anchor, now)
  now = now or os.clock()
  if now < nextFrame then return false end
  nextFrame = now + M.FRAME_S
  if M.source == 'game' or sent ~= nil then M.sync(net) end
  local ok, gc = pcall(require, 'beamngEarth/groundcover')
  if ok and type(gc) == 'table' and gc.setLatitude then gc.setLatitude(anchor and anchor.lat, anchor and anchor.lon) end
  return true
end

return M
