-- Last driven position, independent of bookmarks and settings. Engine-light for offline tests.
local M = { FILE = '/settings/beamngEarth_lastPosition.json', VERSION = 1, INTERVAL = 5 }
local saved, lastAttempt = nil, -math.huge
local warned = false

local function finite(x)
  return type(x) == 'number' and x == x and x ~= math.huge and x ~= -math.huge
end

local function clean(p)
  if type(p) ~= 'table' or not finite(p.lat) or not finite(p.lon)
      or math.abs(p.lat) > 90 or math.abs(p.lon) > 180 then return nil end
  local out = { version = M.VERSION, lat = p.lat, lon = p.lon }
  if finite(p.h) and p.h >= -12000 and p.h <= 100000 then out.h = p.h end
  if finite(p.heading) then out.heading = p.heading % (2 * math.pi) end
  return out
end

function M.load()
  saved, lastAttempt = nil, -math.huge
  local read = rawget(_G, 'jsonReadFile')
  if read then
    local ok, data = pcall(read, M.FILE)
    if ok and type(data) == 'table' and data.version == M.VERSION then saved = clean(data) end
  end
  if not saved then return nil end
  return clean(saved)
end

local function same(a, b)
  return a and b and math.abs(a.lat-b.lat) < 1e-7 and math.abs(a.lon-b.lon) < 1e-7
    and math.abs((a.h or 0)-(b.h or 0)) < .2
    and math.abs(((a.heading or 0)-(b.heading or 0)+math.pi) % (2*math.pi)-math.pi) < .01
end

function M.update(position, clock, force)
  local p = clean(position)
  if not p then return false end
  clock = finite(clock) and clock or 0
  if not force and clock-lastAttempt < M.INTERVAL then return false end
  if same(saved, p) then return false end
  lastAttempt = clock
  local write = rawget(_G, 'jsonWriteFile')
  if not write then return false end
  local ok, result = pcall(write, M.FILE, p, true)
  if ok and result ~= false then
    saved = clean(p)
    warned = false
    return true
  end
  if not warned then
    warned = true
    log('W', 'beamngEarth', 'Could not save the last player position: ' .. tostring(result))
  end
  return false
end

return M
