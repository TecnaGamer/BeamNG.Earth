-- Approximate longitudinal audio-tactile line markings on selected Australasian and US highway corridors.
-- The player vehicle's wheel contact points are sampled in its own VM and checked against the streamed road graph.
-- This is an audio cue only; it never changes tire, suspension or road physics.
local M = {}

M.ON = true
M.SAMPLE_S = 0.02
M.SENSOR_STALE_S = 0.35
M.MIN_SPEED_MS = 2.5
M.LINE_ENTER_M = 0.34
M.LINE_EXIT_M = 0.58
-- Road half-widths are inferred from OSM, not painted-line geometry. Keep edge activation tight and a little
-- outside the inferred footprint so the sound starts near the visible edge stripe, not a metre inboard.
M.EDGE_LINE_OUTSET_M = 0.20
M.EDGE_LINE_ENTER_M = 0.20
M.EDGE_LINE_EXIT_M = 0.40
M.FADE_IN_S = 0.14
M.FADE_OUT_S = 0.22
M.CROSSING_HOLD_S = 0.16
M.SOUND_FILE = '/levels/beamng_earth/art/sound/rumble_strip.ogg'
M.VOLUME = 1.35
M.REFERENCE_DISTANCE_M = 1.8
M.MAX_DISTANCE_M = 8.0
M.SENSOR_ENABLE_COMMAND = 'if not beamngEarth_rumbleSensor then extensions.load("beamngEarth_rumbleSensor") end; if beamngEarth_rumbleSensor then beamngEarth_rumbleSensor.setEnabled(true) end'
M.SENSOR_DISABLE_COMMAND = 'if beamngEarth_rumbleSensor then beamngEarth_rumbleSensor.setEnabled(false) end'

local sample = 0
local sensorVehicleId, sensorVehicle = nil, nil
local wheelPositions, wheelPositionsAt = nil, nil
local activeLines = {}
local previousRoads, crossingUntil, crossingPositions = {}, {}, {}
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
  if not oneWay and d <= tol + 1e-6 then return 'centre' end
  if math.abs(d - (width + M.EDGE_LINE_OUTSET_M)) <= edgeTol + 1e-6 then return 'edge' end
  return nil
end

-- Detect a narrow line crossed between two sensor samples, even when neither sample lands on the line.
-- `fraction` identifies where along the tyre's movement the crossing happened.
function M.crossedLine(previousLateral, lateral, halfWidth, oneWay, edgeOutset)
  local previous, current, width = tonumber(previousLateral), tonumber(lateral), tonumber(halfWidth)
  if not (finite(previous) and finite(current) and finite(width)) or width < 2 then return nil end
  local delta = current - previous
  if math.abs(delta) < 1e-6 then return nil end
  if not oneWay and previous * current <= 0 then
    return 'centre', math.max(0, math.min(1, -previous / delta))
  end
  local edgeWidth = width + (tonumber(edgeOutset) or M.EDGE_LINE_OUTSET_M)
  for _, edge in ipairs({ -edgeWidth, edgeWidth }) do
    local before, after = previous - edge, current - edge
    if math.abs(before) <= 1e-6 or math.abs(after) <= 1e-6 or before * after < 0 then
      return 'edge', math.max(0, math.min(1, (edge - previous) / delta))
    end
  end
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
  previousRoads, crossingUntil, crossingPositions = {}, {}, {}
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
    -- Put file-based audio on BeamNG's Environment channel; without a description SFXEmitter falls back to Master.
    e:setField('description', 0, 'AudioEnvironment')
    e:setField('sourceGroup', 0, 'AudioChannelEnvironment')
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
    previousRoads, crossingUntil, crossingPositions = {}, {}, {}
    advanceEmitters(dt, {})
    return
  end

  if not wheelPositions or not wheelPositionsAt or now - wheelPositionsAt > M.SENSOR_STALE_S then
    activeLines = {}
    previousRoads, crossingUntil, crossingPositions = {}, {}, {}
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
      local contactPos = pos
      local previous = previousRoads[index]
      if not line and road and previous and previous.edgeKey == road.edgeKey then
        local crossed, fraction = M.crossedLine(previous.lateral, road.lateral,
          math.min(previous.halfWidth, road.halfWidth), road.oneWay, M.EDGE_LINE_OUTSET_M)
        if crossed then
          line = crossed
          contactPos = {
            x = previous.pos.x + (pos.x - previous.pos.x) * fraction,
            y = previous.pos.y + (pos.y - previous.pos.y) * fraction,
            z = previous.pos.z + (pos.z - previous.pos.z) * fraction,
          }
          crossingUntil[index] = now + M.CROSSING_HOLD_S
          crossingPositions[index] = contactPos
        end
      end
      if line then
        contacts[index] = { pos = contactPos, line = line }
        activeLines[index] = line
      else
        activeLines[index] = nil
      end
      if road then
        previousRoads[index] = { lateral = road.lateral, halfWidth = road.halfWidth,
          edgeKey = road.edgeKey, oneWay = road.oneWay, pos = pos }
      else
        previousRoads[index] = nil
      end
    end
  end
  -- Keep a swept crossing audible long enough for the fade to rise, then fade it out normally.
  for index, untilTime in pairs(crossingUntil) do
    if contacts[index] then
      crossingUntil[index], crossingPositions[index] = nil, nil
    elseif now < untilTime then
      contacts[index] = { pos = crossingPositions[index], line = 'crossing' }
    else
      crossingUntil[index], crossingPositions[index] = nil, nil
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
