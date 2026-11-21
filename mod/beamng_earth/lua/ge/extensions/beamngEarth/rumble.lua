-- Approximate longitudinal audio-tactile line markings on selected Australasian and US highway corridors.
-- The player vehicle's wheel contact points are sampled in its own VM and checked against the streamed road graph.
-- This is an audio cue only; it never changes tire, suspension or road physics.
local M = {}

M.ON = true
M.SAMPLE_S = 0.08
M.SENSOR_STALE_S = 0.35
M.MIN_SPEED_MS = 2.5
M.LINE_ENTER_M = 0.34
M.LINE_EXIT_M = 0.58
-- Road half-widths are inferred from OSM, not painted-line geometry; allow a little more room for outer strips.
M.EDGE_LINE_ENTER_M = 0.75
M.EDGE_LINE_EXIT_M = 0.95
M.FADE_IN_S = 0.14
M.FADE_OUT_S = 0.22
M.SOUND_FILE = '/levels/beamng_earth/art/sound/rumble_strip.ogg'
M.VOLUME = 1.35
M.REFERENCE_DISTANCE_M = 1.8
M.MAX_DISTANCE_M = 8.0
M.SENSOR_ENABLE_COMMAND = 'if not extensions.beamngEarth_rumbleSensor then extensions.load("beamngEarth_rumbleSensor") end; if extensions.beamngEarth_rumbleSensor then extensions.beamngEarth_rumbleSensor.setEnabled(true) end'
M.SENSOR_DISABLE_COMMAND = 'if extensions.beamngEarth_rumbleSensor then extensions.beamngEarth_rumbleSensor.setEnabled(false) end'

local sample = 0
local sensorVehicleId, sensorVehicle = nil, nil
local wheelPositions, wheelPositionsAt = nil, nil
local activeLines = {}
local emitters = {}
local warnedMissingSound, warnedEmitter, warnedSensor = false, false, false

local function finite(n)
  return type(n) == 'number' and n == n and n ~= math.huge and n ~= -math.huge
end

-- Existing tests and callers may still want the broad vehicle-centre zones.
function M.hitZones(lateral, halfWidth, oneWay)
  local d = math.abs(tonumber(lateral) or math.huge)
  local w = tonumber(halfWidth) or 0
  return { centre = not oneWay and d <= 0.95,
    edge = w >= 2 and d >= math.max(1.2, w - 0.95) }
end

-- A wheel contact point is considered on a marking only when it is within the narrow painted/rumble band.
-- The caller uses a wider exit threshold than the enter threshold to avoid toggling on graph jitter.
function M.wheelLineAt(lateral, halfWidth, oneWay, tolerance, edgeTolerance)
  local signed, width = tonumber(lateral), tonumber(halfWidth)
  local tol = tonumber(tolerance) or M.LINE_ENTER_M
  local edgeTol = tonumber(edgeTolerance) or tol
  if not (finite(signed) and finite(width) and finite(tol) and finite(edgeTol)) or width < 2 or tol < 0 or edgeTol < 0 then return nil end
  local d = math.abs(signed)
  if not oneWay and d <= tol then return 'centre' end
  if math.abs(d - width) <= edgeTol then return 'edge' end
  return nil
end

function M.fadeGain(gain, touching, dt)
  local g, step = math.max(0, tonumber(gain) or 0), math.max(0, tonumber(dt) or 0)
  if touching then
    return M.FADE_IN_S <= 0 and 1 or math.min(1, g + step / M.FADE_IN_S)
  end
  return M.FADE_OUT_S <= 0 and 0 or math.max(0, g - step / M.FADE_OUT_S)
end

-- Invoked by the vehicle-VM sensor. Reject messages from a replaced/old player vehicle.
function M.receiveWheelPositions(id, positions)
  if tonumber(id) ~= tonumber(sensorVehicleId) or type(positions) ~= 'table' then return false end
  local result = {}
  for _, p in ipairs(positions) do
    if type(p) == 'table' then
      local index, x, y, z = tonumber(p.index), tonumber(p.x), tonumber(p.y), tonumber(p.z)
      if finite(index) and index >= 1 and index % 1 == 0 and finite(x) and finite(y) and finite(z) then
        result[index] = { x = x, y = y, z = z }
      end
    end
  end
  wheelPositions, wheelPositionsAt = result, os.clockhp()
  return true
end

local function queueSensor(vehicle, command)
  local ok = pcall(function() vehicle:queueLuaCommand(command) end)
  if not ok and not warnedSensor then
    log('W', 'beamngEarth', 'could not start the player-vehicle rumble sensor')
    warnedSensor = true
  end
  return ok
end

local function disarmSensor()
  if sensorVehicle and sensorVehicleId then queueSensor(sensorVehicle, M.SENSOR_DISABLE_COMMAND) end
  sensorVehicleId, sensorVehicle = nil, nil
  wheelPositions, wheelPositionsAt, activeLines = nil, nil, {}
end

local function ensureSensor(vehicle)
  local ok, id = pcall(function() return vehicle:getID() end)
  id = ok and tonumber(id) or nil
  if not id then return false end
  if id ~= sensorVehicleId then
    disarmSensor()
    sensorVehicleId, sensorVehicle = id, vehicle
    queueSensor(vehicle, M.SENSOR_ENABLE_COMMAND)
  end
  return true
end

local function emitterPosition(object, pos)
  if object and object.setPosition then pcall(object.setPosition, object, vec3(pos.x, pos.y, pos.z)) end
end

local function setGain(state, gain)
  state.gain = gain
  if state.object and state.object.setVolumePitchCT then
    pcall(state.object.setVolumePitchCT, state.object, M.VOLUME * gain, 1, 0, 0)
  end
end

local function deleteEmitter(index)
  local state = emitters[index]
  if not state then return end
  if state.object then
    if state.object.stop then pcall(state.object.stop, state.object) end
    if simObjectExists(state.object) then pcall(state.object.delete, state.object) end
  end
  emitters[index] = nil
end

local function newEmitter(index, pos)
  if not (FS and FS.fileExists and FS:fileExists(M.SOUND_FILE)) then
    if not warnedMissingSound then
      log('W', 'beamngEarth', 'rumble-strip field recording is not available: ' .. M.SOUND_FILE)
      warnedMissingSound = true
    end
    return nil
  end
  local ok, object = pcall(function()
    local e = Sim.upcast(createObject('SFXEmitter'))
    if not e then return nil end
    e:setField('fileName', 0, M.SOUND_FILE)
    e:setField('playOnAdd', 0, '0')
    e:setField('is3D', 0, '1')
    e:setField('isLooping', 0, '1')
    e:setField('isStreaming', 0, '0')
    e:setField('volume', 0, '0')
    e:setField('referenceDistance', 0, tostring(M.REFERENCE_DISTANCE_M))
    e:setField('maxDistance', 0, tostring(M.MAX_DISTANCE_M))
    e:setField('fadeInTime', 0, tostring(M.FADE_IN_S))
    e:setField('fadeOutTime', 0, tostring(M.FADE_OUT_S))
    e.canSave = false
    e:registerObject('bngEarthRumbleTyre' .. tostring(index) .. '_' .. tostring(os.clockhp()))
    emitterPosition(e, pos)
    e:play()
    return e
  end)
  if ok and object then return { object = object, gain = 0 } end
  if not warnedEmitter then
    log('W', 'beamngEarth', 'could not create positional rumble emitter: ' .. tostring(object))
    warnedEmitter = true
  end
  return nil
end

local function advanceEmitters(dt, contacts)
  for index, contact in pairs(contacts) do
    local state = emitters[index]
    if not state then
      state = newEmitter(index, contact.pos)
      if state then emitters[index] = state end
    end
    if state then
      emitterPosition(state.object, contact.pos)
      setGain(state, M.fadeGain(state.gain, true, dt))
    end
  end
  for index, state in pairs(emitters) do
    if not contacts[index] then
      setGain(state, M.fadeGain(state.gain, false, dt))
      if state.gain <= 0 then deleteEmitter(index) end
    end
  end
end

function M.setEnabled(on)
  M.ON = on == true
  if not M.ON then
    disarmSensor()
    activeLines = {}
  end
end

-- The player-vehicle VM reports each wheel's ground contact point; this side maps each tyre independently.
function M.update(dtReal, graph, running)
  local dt = math.max(0, tonumber(dtReal) or 0)
  sample = sample + dt
  if sample < M.SAMPLE_S then return end
  sample = sample % M.SAMPLE_S

  local now = os.clockhp()
  if not M.ON or running == false or not graph then
    disarmSensor()
    advanceEmitters(dt, {})
    return
  end

  local vehicle = getPlayerVehicle and getPlayerVehicle(0)
  if not vehicle then
    disarmSensor()
    advanceEmitters(dt, {})
    return
  end
  if not ensureSensor(vehicle) then
    advanceEmitters(dt, {})
    return
  end

  local speed = 0
  local okSpeed = pcall(function() speed = vehicle:getVelocity():length() end)
  if not okSpeed or not finite(speed) or speed < M.MIN_SPEED_MS then
    activeLines = {}
    advanceEmitters(dt, {})
    return
  end

  if not wheelPositions or not wheelPositionsAt or now - wheelPositionsAt > M.SENSOR_STALE_S then
    activeLines = {}
    advanceEmitters(dt, {})
    return
  end

  local nav
  local okNav, result = pcall(require, 'beamngEarth/navgraph')
  if okNav then nav = result end
  local contacts = {}
  if nav and nav.rumbleLineAt then
    for index, pos in pairs(wheelPositions) do
      local okRoad, road = pcall(nav.rumbleLineAt, pos.x, pos.y, pos.z)
      local tolerance = activeLines[index] and M.LINE_EXIT_M or M.LINE_ENTER_M
      local edgeTolerance = activeLines[index] == 'edge' and M.EDGE_LINE_EXIT_M or M.EDGE_LINE_ENTER_M
      local line = okRoad and road and M.wheelLineAt(road.lateral, road.halfWidth, road.oneWay, tolerance, edgeTolerance)
      if line then
        contacts[index] = { pos = pos, line = line }
        activeLines[index] = line
      else
        activeLines[index] = nil
      end
    end
  end
  advanceEmitters(dt, contacts)
end

function M.reset()
  disarmSensor()
  sample = 0
  for index in pairs(emitters) do deleteEmitter(index) end
  emitters = {}
end

return M
