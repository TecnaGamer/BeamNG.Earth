-- BeamNG Earth-only OSM lane follower. It never installs a BeamNG traffic graph or calls gameplay_traffic.
local M = {}
local localplayers = require('beamngEarth/localplayers')

M.MAX_CARS = 32
M.SPAWN_INTERVAL_S = 0.8
M.OFF_ROUTE_M = 24
M.END_M = 14
M.SPEED_PROFILES = 20
M.RETIRE_DISTANCE_M = 1800
M.SPAWN_SEPARATION_M = 30

local enabled, amount = false, 0
local spawnSerial, spawnClock = 0, 0
local incoming = {}
local active, controlled = {}, {}
local diagnostics = { spawned = 0, despawned = 0, offRoad = 0, roadEnds = 0, occupied = 0, lastError = nil }

local function clamp(x, lo, hi) return math.max(lo, math.min(hi, x)) end

local function activeCount()
  local n = 0
  for _ in pairs(active) do n = n + 1 end
  return n
end

function M.speedProfileForSlot(slot)
  local n = ((math.floor(tonumber(slot) or 1) - 1) % M.SPEED_PROFILES) + 1
  if n <= 12 then return 'normal' end
  if n <= 18 then return 'cautious' end
  return 'near-limit'
end

function M.speedFactor(kind, sample)
  local r = clamp(tonumber(sample) or 0.5, 0, 1)
  if kind == 'cautious' then return 0.75 + 0.15 * r end
  if kind == 'near-limit' then return 0.95 + 0.05 * r end
  return 0.88 + 0.12 * r
end

function M.speedTarget(limitMps, slot)
  local kind = M.speedProfileForSlot(slot)
  local sample = (slot % 13) / 12
  return math.max(2.0, tonumber(limitMps or 0) * M.speedFactor(kind, sample))
end

-- Pure: nearest route segment, cross-track distance and remaining route length.
function M.routeState(points, px, py, previous)
  if type(points) ~= 'table' or #points < 2 then return nil, math.huge, math.huge end
  local start = math.max(1, math.min(#points - 1, previous or 1))
  local best, bestD2, bestT = start, math.huge, 0
  for i = math.max(1, start - 1), #points - 1 do
    local a, b = points[i], points[i + 1]
    local dx, dy = b.x - a.x, b.y - a.y
    local l2 = dx * dx + dy * dy
    if l2 > 1e-6 then
      local t = clamp(((px - a.x) * dx + (py - a.y) * dy) / l2, 0, 1)
      local ex, ey = px - (a.x + t * dx), py - (a.y + t * dy)
      local d2 = ex * ex + ey * ey
      if d2 < bestD2 then best, bestD2, bestT = i, d2, t end
    end
  end
  local a, b = points[best], points[best + 1]
  local remain = (1 - bestT) * math.sqrt((b.x - a.x) ^ 2 + (b.y - a.y) ^ 2)
  for i = best + 1, #points - 1 do
    a, b = points[i], points[i + 1]
    remain = remain + math.sqrt((b.x - a.x) ^ 2 + (b.y - a.y) ^ 2)
  end
  return best, math.sqrt(bestD2), remain
end

local function commandForRoute(points, target)
  local entries = {}
  for i, p in ipairs(points) do entries[i] = string.format('{x=%.3f,y=%.3f}', p.x, p.y) end
  return 'extensions.load("beamngEarth_earthLaneFollower"); '
    .. 'beamngEarth_earthLaneFollower.setRoute({' .. table.concat(entries, ',') .. '},'
    .. string.format('%.3f', target) .. ')'
end

local function worldRoute(raw, anchor)
  local out = {}
  for _, p in ipairs(raw or {}) do
    local x, y, z = anchor:geodeticToWorld(tonumber(p[1]), tonumber(p[2]), tonumber(p[3]) or 0)
    out[#out + 1] = { x = x, y = y, z = z }
  end
  return out
end

local function release(rec, reason)
  local veh = rec and rec.vehicle
  if not veh then return end
  local id = veh:getId()
  if localplayers.controlled(id, veh, controlled) then
    pcall(veh.queueLuaCommand, veh, 'if beamngEarth_earthLaneFollower then beamngEarth_earthLaneFollower.stop() end')
    active[id] = nil
    diagnostics.occupied = diagnostics.occupied + 1
    return
  end
  pcall(veh.queueLuaCommand, veh, 'if beamngEarth_earthLaneFollower then beamngEarth_earthLaneFollower.stop() end')
  pcall(function() veh:delete() end)
  active[id] = nil
  diagnostics.despawned = diagnostics.despawned + 1
  if reason == 'off-road' then diagnostics.offRoad = diagnostics.offRoad + 1 end
  if reason == 'end' then diagnostics.roadEnds = diagnostics.roadEnds + 1 end
end

function M.setEnabled(on, count)
  local nextEnabled = on == true
  local nextAmount = clamp(math.floor(tonumber(count) or amount), 0, M.MAX_CARS)
  enabled, amount = nextEnabled, nextAmount
  incoming = {}
  if not enabled then
    local releaseList = {}
    for _, rec in pairs(active) do releaseList[#releaseList + 1] = rec end
    for _, rec in ipairs(releaseList) do release(rec, 'disabled') end
  end
  return enabled, amount
end

function M.setAmount(count)
  return M.setEnabled(enabled, count)
end

function M.poseFields()
  if not enabled then return { earthAI = false, earthAICount = 0, earthAIActive = 0, earthAIQueued = 0 } end
  return { earthAI = true, earthAICount = amount, earthAIActive = activeCount(), earthAIQueued = #incoming }
end

function M.onMessage(msg, anchorId)
  if not enabled or type(msg) ~= 'table' or msg.type ~= 'earth_ai_graph' or msg.anchorId ~= anchorId then return false end
  if type(msg.routes) ~= 'table' then return false end
  incoming = msg.routes
  diagnostics.lastError = nil
  return true
end

local function updateActive(anchorId, anchor, player)
  local occupied = localplayers.snapshot(controlled)
  for id, rec in pairs(active) do
    local veh = rec.vehicle
    if not veh or not scenetree.findObjectById(id) then
      active[id] = nil
    elseif localplayers.controlled(id, veh, occupied) then
      release(rec, 'occupied')
    else
      if rec.anchorId ~= anchorId then
        rec.route = worldRoute(rec.raw.route, anchor)
        rec.anchorId = anchorId
        if #rec.route >= 2 then
          pcall(veh.queueLuaCommand, veh, commandForRoute(rec.route, rec.target))
        end
      end
      local p = veh:getPosition()
      local index, distance, remain = M.routeState(rec.route, p.x, p.y, rec.progress)
      local far = player and math.sqrt((p.x - player.x) ^ 2 + (p.y - player.y) ^ 2) > M.RETIRE_DISTANCE_M
      if far then
        release(rec, 'far')
      elseif not index or distance > M.OFF_ROUTE_M then
        release(rec, 'off-road')
      elseif remain <= M.END_M then
        release(rec, 'end')
      else
        rec.progress = math.max(rec.progress or 1, index)
      end
    end
  end
end

local function spawnOne(raw, anchor, anchorId)
  local route = worldRoute(raw.route, anchor)
  if #route < 2 then return false end
  if raw.lat and raw.lon and raw.h then
    local x, y, z = anchor:geodeticToWorld(tonumber(raw.lat), tonumber(raw.lon), tonumber(raw.h))
    route[1] = { x = x, y = y, z = z }
  end
  for _, rec in pairs(active) do
    local p = rec.vehicle:getPosition()
    if (p.x - route[1].x) ^ 2 + (p.y - route[1].y) ^ 2 < M.SPAWN_SEPARATION_M ^ 2 then return false end
  end
  local dir = vec3(route[2].x - route[1].x, route[2].y - route[1].y, 0)
  if dir:length() < 0.1 then return false end
  local pos = vec3(route[1].x, route[1].y, route[1].z)
  local rot = quatFromDir(dir:normalized(), vec3(0, 0, 1))
  local ok, veh = pcall(core_vehicles.spawnNewVehicle, 'etk800', {
    pos = pos, rot = rot, autoEnterVehicle = false, vehicleName = 'Earth lane AI',
  })
  if not ok or not veh then
    diagnostics.lastError = tostring(veh)
    return false
  end
  spawnSerial = spawnSerial + 1
  local target = M.speedTarget(tonumber(raw.speed) or 8.0, spawnSerial)
  local command = commandForRoute(route, target)
  pcall(veh.queueLuaCommand, veh, command)
  local id = veh:getId()
  active[id] = { vehicle = veh, raw = raw, route = route, target = target, progress = 1, anchorId = anchorId }
  diagnostics.spawned = diagnostics.spawned + 1
  return true
end

function M.update(dt, st)
  spawnClock = spawnClock + (tonumber(dt) or 0)
  if not st or not st.inOurLevel or not st.anchor or not st.anchorId then return end
  updateActive(st.anchorId, st.anchor, st.player)
  if not enabled then return end
  local n = activeCount()
  while n > amount do
    local _, rec = next(active)
    if not rec then break end
    release(rec, 'count')
    n = activeCount()
  end
  if n >= amount or #incoming == 0 or spawnClock < M.SPAWN_INTERVAL_S then return end
  spawnClock = 0
  local raw = table.remove(incoming, 1)
  if spawnOne(raw, st.anchor, st.anchorId) then return end
end

function M.status()
  return { enabled = enabled, target = amount, active = activeCount(), queued = #incoming,
    spawned = diagnostics.spawned, despawned = diagnostics.despawned, offRoad = diagnostics.offRoad,
    roadEnds = diagnostics.roadEnds, occupied = diagnostics.occupied, lastError = diagnostics.lastError }
end

function M.reset()
  M.setEnabled(false, 0)
  active, incoming, controlled = {}, {}, {}
  spawnClock, spawnSerial = 0, 0
end

return M
