-- Roads Driven Tracker (Issue #365): tracks where the player has driven on the map,
-- maintaining an exploration odometer and visited route polylines. Persists across sessions.
local M = {}

M.FILE = '/settings/beamngEarth_driven.json'
M.VERSION = 1
M.MIN_STEP_M = 25.0       -- add point when moved at least this far from previous point
M.TELEPORT_GAP_M = 2000.0 -- jumps larger than this start a new polyline segment
M.SAVE_INTERVAL_S = 15.0  -- persist to disk every 15s if dirty
M.MAX_POINTS = 50000      -- max total points kept across lines

local totalKm = 0
local sessionKm = 0
local polylines = {}      -- list of { {lat, lon}, {lat, lon}, ... }
local currentLine = nil
local lastLat, lastLon = nil, nil
local dirty = false
local clock = 0
local lastSave = 0
local loaded = false

-- Haversine distance in metres between (lat1, lon1) and (lat2, lon2)
local function distM(lat1, lon1, lat2, lon2)
  local phi1 = math.rad(lat1)
  local phi2 = math.rad(lat2)
  local dphi = math.rad(lat2 - lat1)
  local dlam = math.rad(lon2 - lon1)
  local a = math.sin(dphi / 2)^2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2)^2
  return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
end
M.distM = distM

function M.load()
  if loaded then return end
  loaded = true
  if not jsonReadFile then return end
  local ok, data = pcall(jsonReadFile, M.FILE)
  if ok and type(data) == 'table' then
    totalKm = tonumber(data.totalKm) or 0
    if type(data.polylines) == 'table' then
      polylines = {}
      for _, line in ipairs(data.polylines) do
        if type(line) == 'table' and #line >= 2 then
          local clean = {}
          for _, pt in ipairs(line) do
            if type(pt) == 'table' and tonumber(pt[1]) and tonumber(pt[2]) then
              clean[#clean + 1] = { tonumber(pt[1]), tonumber(pt[2]) }
            end
          end
          if #clean >= 2 then polylines[#polylines + 1] = clean end
        end
      end
    end
  end
end

function M.save()
  if not dirty or not jsonWriteFile then return end
  dirty = false
  -- Trim excess old points if we exceeded MAX_POINTS
  local count = 0
  for _, line in ipairs(polylines) do count = count + #line end
  while count > M.MAX_POINTS and #polylines > 1 do
    count = count - #polylines[1]
    table.remove(polylines, 1)
  end

  local data = {
    version = M.VERSION,
    totalKm = math.floor(totalKm * 100) / 100,
    polylines = polylines,
  }
  pcall(jsonWriteFile, M.FILE, data, true)
end

-- Called periodically from core.lua or settings.lua
function M.update(dt, player)
  clock = clock + (dt or 0)
  if not loaded then M.load() end
  if not player or not player.lat or not player.lon then return end
  local lat, lon = player.lat, player.lon
  if not lastLat or not lastLon then
    lastLat, lastLon = lat, lon
    currentLine = { { lat, lon } }
    polylines[#polylines + 1] = currentLine
    return
  end

  local d = distM(lastLat, lastLon, lat, lon)
  if d >= M.MIN_STEP_M then
    if d >= M.TELEPORT_GAP_M then
      -- Player teleported: start a separate line so no streak across the globe
      currentLine = { { lat, lon } }
      polylines[#polylines + 1] = currentLine
    else
      local km = d / 1000
      sessionKm = sessionKm + km
      totalKm = totalKm + km
      if not currentLine then
        currentLine = { { lastLat, lastLon } }
        polylines[#polylines + 1] = currentLine
      end
      currentLine[#currentLine + 1] = { lat, lon }
      dirty = true
    end
    lastLat, lastLon = lat, lon
  end

  if dirty and clock - lastSave >= M.SAVE_INTERVAL_S then
    lastSave = clock
    M.save()
  end
end

function M.uiState()
  if not loaded then M.load() end
  return {
    totalKm = math.floor(totalKm * 10) / 10,
    sessionKm = math.floor(sessionKm * 10) / 10,
    polylines = polylines,
  }
end

function M.reset()
  if dirty then M.save() end
  currentLine = nil
  lastLat, lastLon = nil, nil
  dirty = false
end

return M
