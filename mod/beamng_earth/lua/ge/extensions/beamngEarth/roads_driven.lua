-- Roads Driven Tracker (Issue #365): tracks where the player has driven on the map,
-- maintaining an exploration odometer and visited route polylines. Persists across sessions.
local M = {}

M.FILE = '/settings/beamngEarth_driven.json'
M.VERSION = 1
M.MIN_STEP_M = 25.0       -- add point when moved at least this far from previous point
M.TELEPORT_GAP_M = 2000.0 -- jumps larger than this start a new polyline segment
M.MAX_POINTS = 50000      -- max total points kept across lines

local totalKm = 0
local sessionKm = 0
local polylines = {}      -- list of { {lat, lon}, {lat, lon}, ... }
local currentLine = nil
local lastLat, lastLon = nil, nil
local dirty = false
local clock = 0
local lastSave = 0
local stoppedFor = 0
local loaded = false
local rev = 1

-- Check if player is actively driving their own car (not free-cam, not spectating in BeamMP)
local function isDrivingVehicle()
  local gpv = rawget(_G, 'getPlayerVehicle')
  local veh = gpv and gpv(0)
  if not veh then return false end

  -- Check if view is free camera (Shift+C)
  local c = rawget(_G, 'commands')
  if c and c.isFreeCamera then
    local ok, v = pcall(c.isFreeCamera)
    if ok and v then return false end
  end
  local cam = rawget(_G, 'core_camera')
  if cam and cam.getActiveCamName then
    local ok, name = pcall(cam.getActiveCamName)
    if ok and name == 'free' then return false end
  end

  -- In BeamMP: check if the vehicle belongs to this player (not spectating another player)
  local id = veh.getID and veh:getID()
  local mp = rawget(_G, 'MPVehicleGE')
  if id and type(mp) == 'table' and type(mp.isOwn) == 'function' then
    local ok, own = pcall(mp.isOwn, id)
    if ok and not own then return false end
  end

  return true
end
M.isDrivingVehicle = isDrivingVehicle

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

-- Quantize to 6 decimal places (~11 cm resolution; shrinks JSON disk & IPC payload by ~40%)
local function quantize6(v)
  return math.floor(v * 1000000 + 0.5) / 1000000
end
M.quantize6 = quantize6

-- Trim oldest points when total point budget exceeds MAX_POINTS
local function trimPoints()
  local count = 0
  for _, line in ipairs(polylines) do count = count + #line end
  if count <= M.MAX_POINTS then return end

  while count > M.MAX_POINTS and #polylines > 0 do
    local first = polylines[1]
    local excess = count - M.MAX_POINTS
    if #polylines > 1 and #first <= excess then
      count = count - #first
      table.remove(polylines, 1)
      if currentLine == first then
        currentLine = polylines[#polylines]
      end
      rev = rev + 1
    else
      -- Slice the head of the oldest line in place so table reference and currentLine remain in sync
      local toRemove = math.min(#first - 2, excess)
      if toRemove <= 0 then
        if #polylines > 1 then
          count = count - #first
          table.remove(polylines, 1)
          if currentLine == first then currentLine = polylines[#polylines] end
          rev = rev + 1
        else
          break
        end
      else
        local n = #first
        for i = 1, n - toRemove do
          first[i] = first[i + toRemove]
        end
        for i = n - toRemove + 1, n do
          first[i] = nil
        end
        count = count - toRemove
        rev = rev + 1
      end
      if count <= M.MAX_POINTS or #polylines == 0 then break end
    end
  end
  if #polylines == 0 then currentLine = nil end
end
M.trimPoints = trimPoints

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
      trimPoints()
      rev = rev + 1
    end
  end
end

-- Save compact JSON to disk
function M.save()
  if not dirty or not jsonWriteFile then return end
  dirty = false
  trimPoints()

  local data = {
    version = M.VERSION,
    totalKm = math.floor(totalKm * 100) / 100,
    polylines = polylines,
  }
  -- false: compact JSON (no indentation), avoiding heavy disk I/O
  pcall(jsonWriteFile, M.FILE, data, false)
end

-- Called each frame from core.lua: onUpdate(dtReal, dtSim)
function M.update(dtReal, player, isPaused)
  clock = clock + (dtReal or 0)
  if not loaded then M.load() end

  -- Ignore when player object is missing or when in camera/view frame (e.g. 3D map)
  if not player or not player.lat or not player.lon or player.camera then
    currentLine = nil
    lastLat, lastLon = nil, nil
    return
  end

  local lat, lon = player.lat, player.lon
  if not lastLat or not lastLon then
    -- Initial sample: verify driving state before starting
    if not isDrivingVehicle() then return end
    lastLat, lastLon = lat, lon
    currentLine = { { quantize6(lat), quantize6(lon) } }
    polylines[#polylines + 1] = currentLine
    rev = rev + 1
    return
  end

  -- Fast delta check before expensive haversine:
  -- 25m at the equator is ~0.000225 deg. If |dlat| < 0.00015 and |dlon| < 0.00015,
  -- vehicle moved at most ~16m, so distance cannot be >= 25m.
  local dlat = math.abs(lat - lastLat)
  local dlon = math.abs(lon - lastLon)
  if dlat >= 0.00015 or dlon >= 0.00015 then
    local d = distM(lastLat, lastLon, lat, lon)
    if d >= M.MIN_STEP_M then
      -- Step threshold reached: only now check isDrivingVehicle()
      if not isDrivingVehicle() then
        currentLine = nil
        lastLat, lastLon = nil, nil
        return
      end

      if d >= M.TELEPORT_GAP_M then
        -- Player teleported: start a separate line so no streak across the globe
        currentLine = { { quantize6(lat), quantize6(lon) } }
        polylines[#polylines + 1] = currentLine
        rev = rev + 1
      else
        local km = d / 1000
        sessionKm = sessionKm + km
        totalKm = totalKm + km
        if not currentLine then
          currentLine = { { quantize6(lastLat), quantize6(lastLon) } }
          polylines[#polylines + 1] = currentLine
          rev = rev + 1
        end
        currentLine[#currentLine + 1] = { quantize6(lat), quantize6(lon) }
        dirty = true
        trimPoints()
      end
      lastLat, lastLon = lat, lon
    end
  end

  -- Persist on pause, or when stopped for >= 2s and >= 5 min since last save
  local speed = player.speed or 0
  if speed < 0.5 then
    stoppedFor = stoppedFor + (dtReal or 0)
  else
    stoppedFor = 0
  end

  if dirty then
    if isPaused then
      lastSave = clock
      stoppedFor = 0
      M.save()
    elseif stoppedFor >= 2.0 and (clock - lastSave) >= 300.0 then
      lastSave = clock
      stoppedFor = 0
      M.save()
    end
  end
end

-- Lightweight state for periodic polling (uiMapState)
function M.uiState()
  if not loaded then M.load() end
  local cur = nil
  if currentLine and #currentLine >= 1 then
    cur = currentLine[#currentLine]
  end
  return {
    totalKm = math.floor(totalKm * 10) / 10,
    sessionKm = math.floor(sessionKm * 10) / 10,
    rev = rev,
    cur = cur,
    n = #polylines,
    len = currentLine and #currentLine or 0,
  }
end

-- Full polylines requested on demand when rev changes
function M.uiLines()
  if not loaded then M.load() end
  return polylines
end

function M.reset()
  if dirty then M.save() end
  currentLine = nil
  lastLat, lastLon = nil, nil
  dirty = false
end

-- Test helpers (used by mod/tests/test_roads_driven.lua)
function M._state()
  return {
    totalKm = totalKm,
    sessionKm = sessionKm,
    polylines = polylines,
    currentLine = currentLine,
    dirty = dirty,
    clock = clock,
    lastSave = lastSave,
    stoppedFor = stoppedFor,
    rev = rev,
  }
end

function M._clear()
  totalKm = 0
  sessionKm = 0
  polylines = {}
  currentLine = nil
  lastLat, lastLon = nil, nil
  dirty = false
  clock = 0
  lastSave = 0
  stoppedFor = 0
  loaded = false
  rev = 1
end

return M
