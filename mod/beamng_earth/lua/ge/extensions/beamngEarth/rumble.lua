-- Approximate longitudinal audio-tactile line markings on selected Australasian and US highway corridors.
-- OSM generally does not map these installations; server/navgraph.py marks only the road classes documented in
-- docs/RUMBLE_STRIPS.md. Internal lane separators on divided carriageways are never candidates.
local M = {}

M.ON = true
M.SAMPLE_S = 0.08
M.MIN_SPEED_MS = 2.5
M.CENTRE_HIT_M = 0.95
M.CENTRE_RESET_M = 1.45
M.EDGE_HIT_M = 0.95
M.EDGE_RESET_M = 1.55
M.COOLDOWN_S = 0.45
M.SOUND_LENGTH_S = 0.65
M.SOUND_FILE = '/levels/beamng_earth/art/sound/rumble_strip.wav'
-- This is a driver cue, not a world-space sound. A positional emitter at the vehicle becomes inaudible from
-- exterior cameras because BeamNG attenuates from the camera/listener position.
M.VOLUME = 0.7

local sample = 0
local lastHit = -math.huge
local centreActive, edgeActive = false, false
local emitters = {}
local warnedMissingSound, warnedEmitter = false, false

function M.hitZones(lateral, halfWidth, oneWay)
  local d = math.abs(tonumber(lateral) or math.huge)
  local w = tonumber(halfWidth) or 0
  return { centre = not oneWay and d <= M.CENTRE_HIT_M,
    edge = w >= 2 and d >= math.max(1.2, w - M.EDGE_HIT_M) }
end

local function clearState()
  centreActive, edgeActive = false, false
end

local function play(pos, now)
  if not (FS and FS.fileExists and FS:fileExists(M.SOUND_FILE)) then
    if not warnedMissingSound then
      log('W', 'beamngEarth', 'rumble sound file is not available: ' .. M.SOUND_FILE)
      warnedMissingSound = true
    end
    return false
  end
  local ok, emitter = pcall(function()
    local e = Sim.upcast(createObject('SFXEmitter'))
    if not e then return nil end
    e:setTransform(MatrixF(true))
    e:setField('fileName', 0, M.SOUND_FILE)
    e:setField('playOnAdd', 0, '1')
    e:setField('is3D', 0, '0')
    e:setField('isLooping', 0, '0')
    e:setField('volume', 0, tostring(M.VOLUME))
    e.canSave = false
    e:registerObject('bngEarthRumble_' .. (Engine and Engine.generateUUID and Engine.generateUUID() or tostring(now)))
    e:play()
    return e
  end)
  if ok and emitter then
    emitters[#emitters + 1] = { object = emitter, expires = now + M.SOUND_LENGTH_S }
    return true
  end
  if not warnedEmitter then
    log('W', 'beamngEarth', 'could not create rumble sound emitter: ' .. tostring(emitter))
    warnedEmitter = true
  end
  return false
end

local function prune(now)
  for i = #emitters, 1, -1 do
    local e = emitters[i]
    if now >= e.expires then
      if e.object and simObjectExists(e.object) then pcall(e.object.delete, e.object) end
      table.remove(emitters, i)
    end
  end
end

function M.setEnabled(on)
  M.ON = on == true
  if not M.ON then clearState() end
end

-- Called after the navigation graph updates; a short sample interval is enough to catch line approaches without
-- putting graph lookup on every renderer callback.
function M.update(dtReal, graph, running)
  local dt = math.max(0, tonumber(dtReal) or 0)
  local now = os.clockhp()
  prune(now)
  if not M.ON or running == false then clearState(); sample = 0; return end
  sample = sample + dt
  if sample < M.SAMPLE_S then return end
  sample = 0
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh or not graph then clearState(); return end
  local ok, pos, speed = pcall(function()
    local p, v = veh:getPosition(), veh:getVelocity()
    return p, v:length()
  end)
  if not ok or not pos or not speed or speed < M.MIN_SPEED_MS then clearState(); return end
  local okRoad, road = pcall(require('beamngEarth/navgraph').rumbleLineAt, pos.x, pos.y, pos.z)
  if not okRoad or not road then clearState(); return end
  local zones = M.hitZones(road.lateral, road.halfWidth, road.oneWay)
  local atCentre = zones.centre
  local atEdge = zones.edge
  local candidate = (atCentre and not centreActive) or (atEdge and not edgeActive)
  if candidate and now - lastHit >= M.COOLDOWN_S then
    if play(pos, now) then lastHit = now end
  end
  local d, w = math.abs(road.lateral), road.halfWidth
  if d > M.CENTRE_RESET_M then centreActive = false elseif atCentre then centreActive = true end
  if d < math.max(1.2, w - M.EDGE_RESET_M) then edgeActive = false elseif atEdge then edgeActive = true end
end

function M.reset()
  clearState()
  sample, lastHit = 0, -math.huge
  for _, e in ipairs(emitters) do
    if e.object and simObjectExists(e.object) then pcall(e.object.delete, e.object) end
  end
  emitters = {}
end

return M
