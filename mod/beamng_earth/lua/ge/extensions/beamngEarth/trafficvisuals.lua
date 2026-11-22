-- Distant traffic (#565): a bounded pool of emissive planes following visual-road geotiles, never real vehicles.
local M = {}
local paths = require('beamngEarth/distanttraffic')
local geo = require('beamngEarth/geotile')
local net = require('beamngEarth/net')
local localplayers = require('beamngEarth/localplayers')
local collisionfoci = require('beamngEarth/collisionfoci')
local trafficflow = require('beamngEarth/trafficflow')
M.PROTO, M.MAX_CARS = paths.PROTO, 2048
M.QUALITY_LIMITS = { low = 512, medium = 1024, high = M.MAX_CARS }
M.RESERVED_DONORS = 6
M.MAX_ACTIVE_PROXY_CARS = 4
local DONOR_PROVIDER = 'beamngEarthProxyTraffic'
local MAX_CARS, FADE_S = M.MAX_CARS, 2
local quality, populationLimit = 'high', M.QUALITY_LIMITS.high
local proxyCarCount = 1000
local SHAPE = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.cdae'
local MATERIALS = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.materials.json'
local PROXY_PAINT_NAMES = { 'white', 'black', 'silver', 'grey', 'blue', 'red', 'beige', 'green' }
local PROXY_BODY_CLASSES = { 'sedan', 'pickup', 'suv', 'van' }
local PROXY_SHAPE = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/proxy_sedan.dae'
local PROXY_SHAPES = {}
for _, body in ipairs(PROXY_BODY_CLASSES) do
  PROXY_SHAPES[body] = {}
  for i, name in ipairs(PROXY_PAINT_NAMES) do
    PROXY_SHAPES[body][i] = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/proxy_' .. body .. '_' .. name .. '.dae'
  end
end
local GROUP = 'bneDistantTraffic'
local PROXY_GROUP = 'bneProxyTraffic'
local PROMOTE_MAX_M = collisionfoci.PLAYER_R - 40
local PROXY_NEAR_M, PROXY_FAR_M, PROXY_DEMOTE_M = 120, 1800, 300
local PROXY_FADE_IN_M, PROXY_FADE_OUT_M = 30, 180
local PROXY_CLEANUP_MIN_M, PROXY_CLEANUP_MAX_M, PROXY_CLEANUP_LOOKAHEAD_S = 50, 180, 2.5
local PROXY_SELECTION_HOLD_M = 2050
-- Keep the colored 3D proxy bodies visible across the flow's full view radius. Fading them at 180 m
-- left only tiny light impostors in daylight, which looked like black dots from a high camera.
local PROXY_BODY_DISTANCE_M, PROXY_BODY_FADE_M = 920, 120
local store = paths.newRoutes()
local actors, pool, selected = {}, {}, {}
local proxyActors, proxyPool, proxySelected = {}, {}, {}
local suppressedProxies = {}
local proxyLightPool = {}
local flowState, flowActors = trafficflow.new(), {}
local flowSignalStates, flowSignalIn, flowRenderIn = {}, 0, 0
local phaseOverrides = {}
local promoted, promotionControlled, promotionIn, promotedCheckIn = {}, {}, 0, 0
local proxyObstacles, obstacleIn, proxyYieldIn = {}, 0, 0
local handoffAvailable = false
local enabled, withoutTraffic, proxyEnabled, capable, session, requested, farRequested, lightsWanted =
  false, false, false, false, nil, nil, nil, false
local fresh = true
local elapsed, checkIn, selectIn, proxyIn, idle, proxyIdle, clock = 0, 0, 0, 0, 0, 0, 0
local requestedAt = 0
local scan, scanAnchor, scanFocus, scanBox, scanDensity, dirty = nil, nil, nil, nil, nil, true
local deferred, deferredCount, deferredSerial = {}, 0, 0
local retry, retryAt, retryDirty, retryUse, retryTiles, retryIn, resendAt = nil, 1, false, 0, 0, 0, 0
local resend = false
local temp = {}
local hot, dueHeap, wake, lastWake = {}, {}, true, nil    -- (M.update's schedule: visit)
local WAKE_COS, WAKE_M = math.cos(math.rad(1.5)), 15     -- a turn / a move of the view that looks at every car
local visit, heapPop, place
local stats = { visible = 0, frames = 0, totalMs = 0, maxMs = 0 }

local function setProxyVisible(item, o, visible)
  local hidden = not visible
  if item.hidden == hidden then return false end
  o:setHidden(hidden)
  item.hidden = hidden
  -- TSStatic uses instance render data; flush visibility changes as well as transforms/colors.
  o:updateInstanceRenderData()
  return true
end

local function hideProxy(actor)
  if not actor or not actor.item then return end
  local item = actor.item
  local o = scenetree.findObjectById(item.id)
  if o then setProxyVisible(item, o, false) end
  item.busy, item.actor = false, nil
  local lights = actor.lights
  if lights then
    for i = 1, 2 do
      local light = lights[i]
      local lightObject = light and scenetree.findObjectById(light.id)
      if lightObject then setProxyVisible(light, lightObject, false) end
    end
    lights.busy, lights.actor = false, nil
  end
  actor.item = nil
  actor.lights = nil
end

local function suppressProxy(key, radius)
  suppressedProxies[key] = math.max(PROXY_CLEANUP_MIN_M, tonumber(radius) or PROXY_CLEANUP_MIN_M) + 20
  proxySelected[key] = nil
  local actor = proxyActors[key]
  if actor then hideProxy(actor); proxyActors[key] = nil end
end

local function deletePair(pair)
  for _, item in ipairs(pair) do
    local o = scenetree.findObjectById(item.id)
    if o then o:delete() end
  end
end

local function clearObjects()
  local gt = gameplay_traffic
  for _, car in pairs(promoted) do
    if car.owned and gt and gt.removeTraffic then
      local o = getObjectByID(car.id)
      pcall(gt.removeTraffic, car.id, true)
      if o then o:delete() end
    elseif car.reserved and gt and gt.returnReservedVehicle then
      pcall(gt.returnReservedVehicle, car.id, false)
    end
  end
  for _, pair in ipairs(pool) do deletePair(pair) end
  for _, item in ipairs(proxyPool) do
    local o = scenetree.findObjectById(item.id)
    if o then o:delete() end
  end
  local group = scenetree.findObject(GROUP)
  if group then group:delete() end
  local proxyGroup = scenetree.findObject(PROXY_GROUP)
  if proxyGroup then proxyGroup:delete() end
  actors, pool, selected, proxyActors, proxyPool, proxySelected, proxyLightPool, phaseOverrides, promoted, scan =
    {}, {}, {}, {}, {}, {}, {}, {}, {}, nil
  suppressedProxies = {}
  flowActors, flowSignalStates, flowSignalIn, flowRenderIn = {}, {}, 0, 0
  trafficflow.clear(flowState)
  promotionIn, promotedCheckIn, proxyIdle = 0, 0, 0
  proxyObstacles, obstacleIn, proxyYieldIn = {}, 0, 0
  hot, dueHeap, wake, stats.visible = {}, {}, true, 0
  scanFocus, scanBox, scanDensity = nil, nil, nil
end

function M.reset()
  if capable and requested then net.send({ type = 'traffic_visuals', enabled = false, far = false }) end
  clearObjects()
  store, elapsed, checkIn, selectIn, idle = paths.newRoutes(), 0, 0, 0, 0
  capable, session, requested, farRequested, scanAnchor, dirty = false, nil, nil, nil, nil, true
  deferred, deferredCount, deferredSerial = {}, 0, 0
  retry, retryAt, retryDirty, retryUse, retryTiles, retryIn, resendAt = nil, 1, false, 0, 0, 0, 0
  resend = false
  fresh = true
  stats = { visible = 0, frames = 0, totalMs = 0, maxMs = 0 }
end

function M.session(id, available)
  if id ~= session then M.reset() end
  session, capable, requested, farRequested = id, available == true, nil, nil
end

function M.setEnabled(v) enabled, checkIn, dirty = v == true, 0, true end
function M.setWithoutTraffic(v) withoutTraffic, checkIn, dirty = v == true, 0, true end
function M.registerDonorProvider()
  local gt = gameplay_traffic
  if not (gt and gt.registerSpecialVehicleProvider) then return false end
  local utils = gameplay_traffic_trafficUtils
  if not (utils and utils.createTrafficGroup) then return false end
  local settings = rawget(_G, 'beamngEarth_settings')
  local proxyOn = proxyEnabled or settings and settings.get and settings.get('proxyTraffic') == true
  local count = proxyOn and M.RESERVED_DONORS or 0
  local ok = pcall(gt.registerSpecialVehicleProvider, {
    name = DONOR_PROVIDER,
    priority = -10,
    reserved = count,
    buildGroup = function(n, options)
      options = options or {}
      return utils.createTrafficGroup(n, options.allMods, options.allConfigs, options.simpleVehs)
    end
  })
  if not ok then return false end
  if proxyOn and gt.getState and gt.getState() == 'on' and gt.ensureReservedVehicles then
    pcall(gt.ensureReservedVehicles, DONOR_PROVIDER, M.RESERVED_DONORS)
  end
  return true
end
function M.setProxyEnabled(v)
  proxyEnabled, checkIn, dirty = v == true, 0, true
  if not proxyEnabled then proxySelected = {} end
  pcall(function() require('beamngEarth/navgraph').setProxyOnly(proxyEnabled) end)
  M.registerDonorProvider()
end
function M.setProxyActiveCap(v)
  M.MAX_ACTIVE_PROXY_CARS = math.max(1, math.min(12, math.floor(tonumber(v) or 4)))
end
function M.setProxyCarCount(v)
  proxyCarCount = math.max(10, math.min(1000, math.floor(tonumber(v) or 1000)))
  dirty, checkIn = true, 0
end
function M.setProxyDonorCap(v)
  M.RESERVED_DONORS = math.max(1, math.min(16, math.floor(tonumber(v) or 6)))
  M.registerDonorProvider()
end
function M.setQuality(v)
  local limit = M.QUALITY_LIMITS[v]
  if not limit then return false end
  if quality ~= v then quality, populationLimit, dirty = v, limit, true end
  return true
end
function M.onTeleport() clearObjects(); trafficflow.clear(flowState); scanAnchor, dirty = nil, true end

local function forgetDeferred(key)
  if deferred[key] then deferred[key], deferredCount = nil, deferredCount - 1 end
end

local function admit(key, wireKey, path, data)
  if not data or data.key ~= wireKey then return end
  local accepted, reason = store:tile({ v = data.v, key = key, frame = data.frame, pivot = data.pivot, paths = data.paths })
  if accepted then
    forgetDeferred(key)
    dirty = true
  elseif reason == 'capacity' and type(path) == 'string' then
    if not deferred[key] then
      if deferredCount >= paths.MAX_TILES then resend = true; return end
      deferredCount, deferredSerial = deferredCount + 1, deferredSerial + 1
      deferred[key] = { key = key, wireKey = wireKey, path = path, serial = deferredSerial }
    end
  end
end

function M.onMessage(msg)
  if not capable or type(msg.key) ~= 'string' then return end
  local far = msg.type == 'traffic_far_tile' or msg.type == 'traffic_far_drop'
  local key = far and (msg.key .. ':far') or msg.key
  if msg.type == 'traffic_drop' or msg.type == 'traffic_far_drop' then
    store:drop(key)
    wake = true
    forgetDeferred(key)
    retryDirty = true
    dirty = true
  elseif msg.type == 'traffic_tile' or msg.type == 'traffic_far_tile' then
    local data = msg.data
    if not data and type(msg.path) == 'string' then data = jsonReadFile(msg.path) end
    admit(key, msg.key, msg.path, data)
  end
end

local function resendDue()
  return resend and clock >= resendAt and deferredCount < paths.MAX_TILES
    and store.tileCount < paths.MAX_TILES and store.points + store.pendingPoints < paths.MAX_ROUTE_POINTS
end

local function retryDeferred()
  if not retry and retryDirty and deferredCount > 0 then
    retry, retryAt, retryDirty = {}, 1, false
    for _, tile in pairs(deferred) do retry[#retry + 1] = tile end
    table.sort(retry, function(a, b) return a.serial < b.serial end)
  end
  if retry and clock >= retryIn then
    local tile = retry[retryAt]
    retryAt = retryAt + 1
    if tile and deferred[tile.key] == tile then
      -- Only a file descriptor waits at capacity, not another copy of its geometry. One bounded JSON read at a time.
      local data = jsonReadFile(tile.path)
      if data then admit(tile.key, tile.wireKey, tile.path, data)
      else forgetDeferred(tile.key); resend = true end
      retryIn = clock + 0.05
    end
    if retryAt > #retry then retry = nil end
  end
  if resendDue() then
    net.send({ type = 'traffic_visuals', enabled = true, far = lightsWanted, reset = true })
    resend, resendAt = false, clock + 10
  end
end

local function placement(route, anchor)
  if route.anchor ~= anchor then
    local src = geo.frameOf({ frame = route.frame })
    route.pl, route.anchor = geo.placement(src, anchor, 0, 0, route.pivot), anchor
  end
  return route.pl
end

local function sample(route, phase, anchor, out, at)
  paths.sample(route, phase + (at or elapsed) * route.speed, out)
  local p = placement(route, anchor)
  out.x, out.y, out.z = geo.apply(p, out.x, out.y, out.z)
  local dx, dy = out.dx, out.dy
  out.dx, out.dy = p.r[1] * dx + p.r[2] * dy, p.r[4] * dx + p.r[5] * dy
  return out
end

local function routeProjection(route, anchor, pos)
  local pl, points = placement(route, anchor), route.points
  local bestD2, bestS = math.huge, nil
  for i = 2, #points do
    local a, b = points[i - 1], points[i]
    local ax, ay, az = geo.apply(pl, a[1], a[2], a[3])
    local bx, by, bz = geo.apply(pl, b[1], b[2], b[3])
    local dx, dy, dz = bx - ax, by - ay, bz - az
    local len2 = dx * dx + dy * dy + dz * dz
    if len2 > 1e-6 then
      local t = math.max(0, math.min(1, ((pos.x - ax) * dx + (pos.y - ay) * dy + (pos.z - az) * dz) / len2))
      local ex, ey, ez = ax + dx * t - pos.x, ay + dy * t - pos.y, az + dz * t - pos.z
      local d2 = ex * ex + ey * ey + ez * ez
      if d2 < bestD2 then bestD2, bestS = d2, a[4] + t * (b[4] - a[4]) end
    end
  end
  return bestS, bestD2
end

local function alpha(p, cam, box, reach)
  local d = math.sqrt((cam.x - p.x) ^ 2 + (cam.y - p.y) ^ 2 + (cam.z - p.z) ^ 2)
  reach = reach or 10000
  local near = proxyEnabled and 600 or 350
  return p.alpha * paths.distanceFade(d, near, proxyEnabled and 100 or 300, reach, math.min(2000, reach * 0.25))
    * paths.smooth((paths.outside(box, p.x, p.y) - 100) / 400), d
end

local function better(a, b) return a.score < b.score or (a.score == b.score and a.key < b.key) end

-- Worst at the root: a bounded heap avoids inserting every candidate through a city-sized sorted array.
local function offer(h, item, limit)
  if #h < limit then
    h[#h + 1] = item
    local i = #h
    while i > 1 do
      local parent = math.floor(i / 2)
      if not better(h[parent], h[i]) then break end
      h[parent], h[i], i = h[i], h[parent], parent
    end
  elseif better(item, h[1]) then
    h[1] = item
    local i = 1
    while i * 2 <= #h do
      local child = i * 2
      if child < #h and better(h[child], h[child + 1]) then child = child + 1 end
      if not better(h[i], h[child]) then break end
      h[i], h[child], i = h[child], h[i], child
    end
  end
end

local function densityNow()
  local tod = core_environment.getTimeOfDay()
  return paths.density((((tod and tod.time or 0) + 0.5) % 1) * 24)
end

local function activePromotedCount()
  local count = 0
  for _, car in pairs(promoted) do
    if car.reserved and car.id then count = count + 1 end
  end
  return count
end

-- Closely spaced navgraph segments have different edge keys even when they are the same road.
-- Treat parallel, nearby lane segments as the player's road for handoff priority.
local function sameRoadCorridor(a, b, ax, ay, bx, by)
  if not a or not b then return false end
  local alignment = math.abs((a.dx or 0) * (b.dx or 0) + (a.dy or 0) * (b.dy or 0))
  if alignment < 0.9 then return false end
  local dx, dy = bx - ax, by - ay
  local across = math.abs(-a.dy * dx + a.dx * dy)
  local width = math.max(9, (tonumber(a.halfWidth) or 3.5) + (tonumber(b.halfWidth) or 3.5) + 4)
  return across <= width
end

-- Treat a close cross-road approach at the same height as a junction handoff candidate too.
-- This keeps a fake car from being the first thing met while joining a street or intersection,
-- without pulling parallel traffic off a nearby highway.
local function crossingApproach(a, b, ax, ay, az, bx, by, bz, distance)
  if not a or not b or distance > PROXY_CLEANUP_MAX_M or math.abs((bz or 0) - (az or 0)) > 6 then return false end
  local alignment = math.abs((a.dx or 0) * (b.dx or 0) + (a.dy or 0) * (b.dy or 0))
  if alignment >= 0.72 then return false end
  local dx, dy = bx - ax, by - ay
  local acrossA = math.abs(-a.dy * dx + a.dx * dy)
  local acrossB = math.abs(-b.dy * dx + b.dx * dy)
  return acrossA <= 50 and acrossB <= 50
end

local function proxyCleanupRadius(p, player, speed)
  local dx, dy = p.x - player.x, p.y - player.y
  local distance = math.sqrt(dx * dx + dy * dy)
  local rvx = (p.vx or (p.dx or 0) * (speed or 0)) - (player.vx or 0)
  local rvy = (p.vy or (p.dy or 0) * (speed or 0)) - (player.vy or 0)
  local closing = math.max(0, -(dx * rvx + dy * rvy) / math.max(distance, 1))
  return math.min(PROXY_CLEANUP_MAX_M,
    math.max(PROXY_CLEANUP_MIN_M, closing * PROXY_CLEANUP_LOOKAHEAD_S))
end

local function adaptiveTrafficFactor()
  local ng = require('beamngEarth/navgraph')
  local status = ng.trafficStatus and ng.trafficStatus()
  if not (status and status.enabled == true) then return 1 end
  local profile = status.profile
  local target = profile and tonumber(profile.target)
  -- The adaptive profile's target is a geography-based desired vehicle count (0 to 15). Keep a small floor
  -- so remote highways still get occasional traffic; the existing local-hour curve provides the rush-hour swing.
  if not target then return 1 end
  return math.max(0.05, math.min(1, target / 15))
end

local function proxyDensity(density)
  -- Adaptive traffic scales the explicit 3D proxy car count; local time does not reduce it.
  return adaptiveTrafficFactor()
end

local function flowPopulation(density)
  local ng = require('beamngEarth/navgraph')
  local trafficOn = gameplay_traffic and gameplay_traffic.getState and gameplay_traffic.getState() == 'on'
  if not proxyEnabled or not requested or not trafficOn or not (ng.currentGraph and ng.currentGraph()) then return 0 end
  -- A present navgraph is not enough: it can contain no drivable connected lanes in the active window.
  -- In that case don't reserve the population budget for a flow tier that cannot actually spawn cars.
  local flowStatus = trafficflow.status(flowState)
  if not flowStatus or (flowStatus.links or 0) == 0 then return 0 end
  return math.min(trafficflow.MAX_CARS,
    math.floor(proxyCarCount * proxyDensity(density) + .5))
end

local function hasFreeProxyDonor()
  local gt = gameplay_traffic
  if activePromotedCount() >= M.MAX_ACTIVE_PROXY_CARS
      or not (gt and gt.getState and gt.getState() == 'on' and gt.getTrafficData) then return false end
  for _, traffic in pairs(gt.getTrafficData() or {}) do
    if traffic.reserved == DONOR_PROVIDER and not traffic.claimed then return true end
  end
  return false
end

-- Free a donor for an urgent proxy handoff by recycling the farthest real Earth car moving away.
-- Preserve its low-cost flow slot when the road has room; if not, retire it while it is out of view.
local function preemptRecedingProxy(state, view)
  local gt = gameplay_traffic
  if not (gt and gt.returnReservedVehicle and gt.getTrafficData) then return false end
  local player = state and state.player
  if not player then return false end
  local data = gt.getTrafficData() or {}
  local best, bestD2
  for key, p in pairs(promoted) do
    if p.reserved and (p.route or p.flow) then
      local t, o = data[p.id], getObjectByID(p.id)
      if t and o and o.getActive and o:getActive() then
        local pos, vel = o:getPosition(), o:getVelocity()
        local dx, dy = pos.x - player.x, pos.y - player.y
        local d2 = dx * dx + dy * dy
        local rvx, rvy = vel.x - (player.vx or 0), vel.y - (player.vy or 0)
        local distance = math.sqrt(d2)
        local radialMotion = dx * rvx + dy * rvy
        -- Only preempt distant cars moving away or barely closing; never pop an approaching car near the player.
        if d2 > (PROXY_DEMOTE_M + 40) ^ 2 and radialMotion >= -distance * 3
            and (not view or paths.viewEdge(pos.x, pos.y, pos.z, view) > 1.15)
            and (not bestD2 or d2 > bestD2) then
          best, bestD2 = { key = key, record = p, pos = pos, vel = vel, object = o }, d2
        end
      end
    end
  end
  if not best then return false end
  local p = best.record
  local fake
  if p.flow then
    local vel, dir = best.vel, best.object:getDirectionVector()
    fake = trafficflow.put(flowState, best.pos.x, best.pos.y, best.pos.z, dir.x, dir.y,
      math.sqrt(vel.x^2 + vel.y^2 + vel.z^2), p.paint, p.body, proxyObstacles, p.id)
  end
  local ok, result = pcall(gt.returnReservedVehicle, p.id, true)
  if not ok or result == false then
    if fake then trafficflow.take(flowState, fake) end
    return false
  end
  if not fake then stats.farCarsRetired = (stats.farCarsRetired or 0) + 1 end
  if not p.flow then
    local along, routeD2 = routeProjection(p.route, state.anchor, best.pos)
    if along and routeD2 <= 45 * 45 then
      phaseOverrides[best.key] = { route = p.route.id,
        phase = (along - elapsed * p.route.speed) % p.route.length }
    end
  end
  promoted[best.key] = nil
  stats.preemptions = (stats.preemptions or 0) + 1
  dirty, wake = true, true
  return true
end

local function refreshPromoted(state, cam, forward, view)
  local gt = gameplay_traffic
  local data = gt and gt.getTrafficData and gt.getTrafficData() or {}
  local changed = false
  for key, p in pairs(promoted) do
    local t = data[p.id]
    local o = getObjectByID(p.id)
    if not t or not o or not o:getActive() or t.respawnCount ~= p.respawnCount then
      promoted[key], changed = nil, true
    elseif proxyEnabled and requested and (p.reserved or p.owned) and state and state.anchor then
      local player = state.player or { x = cam.x, y = cam.y, z = cam.z }
      local pos = o:getPosition()
      local dx, dy, dz = pos.x - player.x, pos.y - player.y, pos.z - player.z
      local d2 = dx * dx + dy * dy + dz * dz
      if d2 >= PROXY_DEMOTE_M * PROXY_DEMOTE_M and paths.viewEdge(pos.x, pos.y, pos.z, view) > 1.15 then
        local along, routeD2
        if p.route then along, routeD2 = routeProjection(p.route, state.anchor, pos) end
        local ok = false
        if p.owned and gt.removeTraffic then
          local called, result = pcall(gt.removeTraffic, p.id, true)
          ok = called and result ~= false
          if ok then o:delete() end
        elseif p.reserved and gt.returnReservedVehicle then
          if p.flow then
            local vel, dir = o:getVelocity(), o:getDirectionVector()
            local fake = trafficflow.put(flowState, pos.x, pos.y, pos.z, dir.x, dir.y,
              math.sqrt(vel.x^2 + vel.y^2 + vel.z^2), p.paint, p.body, proxyObstacles, p.id)
            if fake then
              local called, result = pcall(gt.returnReservedVehicle, p.id, true)
              ok = called and result ~= false
              if not ok then trafficflow.take(flowState, fake) end
            end
          else
            local called, result = pcall(gt.returnReservedVehicle, p.id, true)
            ok = called and result ~= false
          end
        end
        -- If the lane is full, recycle the real car once it is far enough away to be invisible.
        -- This prevents a blocked demotion from permanently occupying one of the limited donors.
        if not ok and p.reserved and d2 >= (PROXY_DEMOTE_M + 250) ^ 2 and gt.returnReservedVehicle then
          local called, result = pcall(gt.returnReservedVehicle, p.id, true)
          ok = called and result ~= false
          if ok then stats.farCarsRetired = (stats.farCarsRetired or 0) + 1 end
        end
        if ok then
          if along and routeD2 <= 45 * 45 then
            phaseOverrides[key] = { route = p.route.id,
              phase = (along - elapsed * p.route.speed) % p.route.length }
          end
          promoted[key], changed = nil, true
          stats.demotions = (stats.demotions or 0) + 1
        end
      end
    end
  end
  if changed then dirty, wake = true, true end
end

local function readyFade(car)
  return math.max(0, math.min(1, (clock - math.max(car.route.readyAt or clock, requestedAt, car.admittedAt)) / FADE_S))
end

-- The population belongs to the streamed world around the player, not the view. Redistributing its budget into
-- the frustum made established roads gain cars when zooming and left new directions empty until a scan finished.
-- Selection is cooperative; the old population keeps moving and the caps include retiring assignments.
local bodyFromRank

local function selection(anchor, focus, box, density)
  local ids, best, demand = {}, { {}, {}, {} }, { 0, 0, 0 }
  -- Treat the connected flow as a near-field tier, but always keep route proxies available as a
  -- fallback. The flow graph can be absent or temporarily have no usable lanes after a rebase/load.
  local flowNow = trafficflow.status(flowState)
  local proxyBest, proxyLimit = {}, math.max(0, proxyCarCount - (flowNow and flowNow.cars or 0))
  local trafficOn = gameplay_traffic and gameplay_traffic.getState and gameplay_traffic.getState() == 'on'
  local proxyDensityNow = proxyEnabled and trafficOn and proxyDensity(density) or 0
  local limit = math.max(1, math.floor(populationLimit * density + 0.5))
  for id in pairs(store.routes) do ids[#ids + 1] = id end
  table.sort(ids)
  for _, id in ipairs(ids) do
    local route = store.routes[id]
    if route then
      local slots = paths.slots(route)
      for i = 1, slots do
        local key = id .. ':' .. i
      -- Keep the traffic amount independent of road class; motorways need full representation too.
      local roadFactor = 1
      local proxySlot = proxyDensityNow > 0 and not promoted[key]
          and paths.rank(key .. ':proxy-density') < proxyDensityNow * roadFactor
        local lightSlot = lightsWanted and paths.rank(key .. ':density') < density
        if proxySlot or lightSlot then
          local override = phaseOverrides[key]
          local phase = override and override.route == id and override.phase or paths.phase(route, i, slots)
          if override and override.route ~= id then phaseOverrides[key] = nil end
          -- Rank a slot at its fixed phase, not its moving pose. Crossing a range band or an endpoint must not
          -- replace it with a new car elsewhere on an established road. The renderer still samples current motion.
          local p = sample(route, phase, anchor, temp, elapsed)
          if proxySlot then
            local body = bodyFromRank(paths.rank(key .. ':body'))
            local dx, dy, dz = p.x - focus.x, p.y - focus.y, p.z - focus.z
            local d2 = dx * dx + dy * dy + dz * dz
            local suppressed = suppressedProxies[key]
            if suppressed and d2 > suppressed * suppressed then
              suppressedProxies[key], suppressed = nil, nil
            end
            local hold = proxyActors[key] and d2 <= PROXY_SELECTION_HOLD_M * PROXY_SELECTION_HOLD_M
            if not suppressed and proxyLimit > 0 and d2 <= PROXY_SELECTION_HOLD_M * PROXY_SELECTION_HOLD_M then
              -- Retain an existing proxy through modest camera/player movement so a rescan does not
              -- replace it with another route sample and make it visibly blink out.
              offer(proxyBest, { key = key, route = route, phase = phase, body = body,
                score = d2 * (hold and 0.55 or 1) }, proxyLimit)
            end
          end
          if lightSlot then
            local a, d = alpha(p, focus, box, route.reach)
            if a > 0.01 then
              local band = d < 2500 and 1 or d < 6000 and 2 or 3
              demand[band] = demand[band] + 1
              local score = paths.rank(key .. ':budget')
              local h = best[band]
              if #h < limit or score <= h[1].score then
                offer(h, { key = key, route = route, phase = phase, score = score }, limit)
              end
            end
          end
        end
        if i % 16 == 0 then coroutine.yield() end
      end
    end
    coroutine.yield()
  end
  selected = {}
  local quotas = paths.bandQuotas(demand, limit)
  for band = 1, 3 do
    table.sort(best[band], better)
    for i = 1, quotas[band] do
      local car = best[band][i]
      if car then selected[car.key] = car end
    end
  end
  table.sort(proxyBest, better)
  proxySelected = {}
  for i = 1, #proxyBest do proxySelected[proxyBest[i].key] = proxyBest[i] end
  stats.demand, stats.quotas = demand, quotas
  wake = true                                   -- (what is wanted changed: every car looked at once)
  stats.populationScans = (stats.populationScans or 0) + 1
end

-- (one table, refilled each frame: a closure and five tables a frame made this the mod's largest per-frame garbage,
-- 3.2 MB in 40 s, 2026-11-07)
local viewT = { cam = {}, forward = {}, right = {}, up = {} }
local function fill(t, v) t.x, t.y, t.z = v.x, v.y, v.z end
local function viewOf(cam, forward)
  local io = ui_imgui and ui_imgui.GetIO()
  local size = io and io.DisplaySize
  local aspect = size and size.y > 0 and size.x / size.y or 16 / 9
  local tanV = math.tan(math.rad(core_camera.getFovDeg()) * 0.5)
  local t = viewT
  fill(t.cam, cam); fill(t.forward, forward); fill(t.right, core_camera.getRight()); fill(t.up, core_camera.getUp())
  t.tanV, t.tanH, t.pix = tanV, tanV * aspect, 2 * tanV / math.max(240, size and size.y or 1080)
  return t
end

local function pair(create)
  for _, p in ipairs(pool) do if not p.busy then p.busy = true; return p, false end end
  if not create or #pool >= populationLimit then return nil end
  if not scenetree.findObject(GROUP) then
    loadJsonMaterialsFile(MATERIALS)
    local group = createObject('SimGroup')
    group.canSave = false
    group:registerObject(GROUP)
    scenetree.MissionGroup:addObject(group)
  end
  local p = { busy = true }
  for i = 1, 2 do
    local o = createObject('TSStatic')
    o:setField('shapeName', 0, SHAPE)
    o:setField('collisionType', 0, 'None')
    o:setField('decalType', 0, 'None')
    o:setField('castShadows', 0, 'false')
    o.useInstanceRenderData, o.canSave = true, false
    o:setField('instanceColor', 0, '1 1 1 0')
    o:setHidden(true)
    o:registerObject('')
    scenetree.findObject(GROUP):addObject(o)
    p[i] = { id = o:getID(), hidden = true }
  end
  pool[#pool + 1] = p
  return p, true
end

local function proxyLightPair(create)
  for _, p in ipairs(proxyLightPool) do
    if not p.busy then p.busy = true; return p, false end
  end
  if not create or #proxyLightPool >= proxyCarCount then return nil end
  local p, freshPair = pair(true)
  if not p then return nil end
  proxyLightPool[#proxyLightPool + 1] = p
  return p, freshPair
end

local function forgetProxyLightPair(pairToForget)
  for i = #proxyLightPool, 1, -1 do
    if proxyLightPool[i] == pairToForget then table.remove(proxyLightPool, i) end
  end
end

local function ensureProxyGroup()
  if not scenetree.findObject(PROXY_GROUP) then
    loadJsonMaterialsFile(MATERIALS)
    local group = createObject('SimGroup')
    group.canSave = false
    group:registerObject(PROXY_GROUP)
    scenetree.MissionGroup:addObject(group)
  end
end

local function proxyShape(body, paint)
  local shapes = PROXY_SHAPES[body] or PROXY_SHAPES.sedan
  return shapes and shapes[paint] or PROXY_SHAPE
end

local function newProxyObject(paint, body)
  ensureProxyGroup()
  local o = createObject('TSStatic')
  -- Select the paint mesh before registration so TSStatic loads the correct fixed material.
  o:setField('shapeName', 0, proxyShape(body, paint))
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o:setField('castShadows', 0, 'false')
  o.useInstanceRenderData, o.canSave = true, false
  o:setField('instanceColor', 0, '1 1 1 1')
  o:setHidden(true)
  o:registerObject('')
  scenetree.findObject(PROXY_GROUP):addObject(o)
  return o
end

local function proxyItem(create, paint, body)
  paint = math.max(1, math.min(#PROXY_PAINT_NAMES, math.floor(tonumber(paint) or 1)))
  for _, item in ipairs(proxyPool) do
    if not item.busy and item.paint == paint and item.body == body then item.busy = true; return item, false end
  end
  if not create then return nil end
  if #proxyPool >= proxyCarCount then
    -- Recycle an idle slot of another paint by creating its replacement with the correct shape
    -- before registration; changing shapeName on a registered TSStatic can reuse stale materials.
    for _, item in ipairs(proxyPool) do
      if not item.busy then
        local old = scenetree.findObjectById(item.id)
        if old then old:delete() end
        local o = newProxyObject(paint, body)
        item.id, item.paint, item.body, item.shape = o:getID(), paint, body, proxyShape(body, paint)
        item.hidden, item.busy, item.alpha, item.x = true, true, nil, nil
        return item, true
      end
    end
    return nil
  end
  local o = newProxyObject(paint, body)
  local item = { id = o:getID(), paint = paint, body = body, shape = proxyShape(body, paint), hidden = true, busy = true }
  proxyPool[#proxyPool + 1] = item
  return item, true
end

local PAINTS = { '0.86 0.86 0.84 1', '0.05 0.05 0.06 1', '0.62 0.63 0.65 1', '0.33 0.34 0.36 1',
  '0.10 0.17 0.35 1', '0.55 0.07 0.06 1', '0.70 0.64 0.52 1', '0.12 0.24 0.16 1' }
local PAINT_SHARES = { 24, 20, 18, 14, 8, 7, 5, 4 }

local function paintFromRank(rank)
  local pick = math.max(0, math.min(.999999, tonumber(rank) or 0)) * 100
  for i, share in ipairs(PAINT_SHARES) do
    pick = pick - share
    if pick < 0 then return i end
  end
  return 1
end

bodyFromRank = function(rank)
  local pick = math.max(0, math.min(.999999, tonumber(rank) or 0))
  if pick < .15 then return 'pickup' end
  if pick < .29 then return 'suv' end
  if pick < .36 then return 'van' end
  return 'sedan'
end

local function setProxyPaint(item, obj, paint, alpha, body)
  paint = math.max(1, math.min(#PROXY_PAINT_NAMES, math.floor(tonumber(paint) or 1)))
  alpha = math.max(0, math.min(1, tonumber(alpha) or 1))
  if item.paint ~= paint or (body and item.body ~= body) then return false end
  local changed = false
  if item.alpha ~= alpha then
    obj:setField('instanceColor', 0, string.format('1 1 1 %.3f', alpha))
    item.alpha = alpha
    changed = true
  end
  if changed then obj:updateInstanceRenderData() end
  return changed
end

local function paintRgb(paint)
  local s = PAINTS[paint or 1] or PAINTS[1]
  local r, g, b = s:match('([%d%.]+)%s+([%d%.]+)%s+([%d%.]+)')
  return tonumber(r) or .78, tonumber(g) or .82, tonumber(b) or .9
end

local function applyPaint(obj, paint)
  local r, g, b = paintRgb(paint)
  pcall(function()
    obj.color = ColorF(r, g, b, 1):asLinear4F()
    if createVehiclePaint then
      obj:setMetallicPaintData({ createVehiclePaint({ x = r, y = g, z = b, w = 1 }, {.4, .35, 1, .05}) })
    end
  end)
end

local function donorBody(id, traffic)
  local o = getObjectByID(id)
  local length, width = tonumber(traffic and traffic.length), tonumber(traffic and traffic.width)
  if not length and o and o.initialNodePosBB then
    local ext = o.initialNodePosBB:getExtents()
    length, width = tonumber(ext.y), tonumber(ext.x)
  end
  local modelKey = tostring(traffic and traffic.model or o and o.jbeam or ''):lower()
  local modelName = tostring(traffic and traffic.modelName or ''):lower()
  local meta
  if modelKey ~= '' and core_vehicles and core_vehicles.getModel then
    local ok, data = pcall(core_vehicles.getModel, modelKey)
    if ok and data then meta = data.model or data end
  end
  local modelType = tostring(meta and meta.Type or ''):lower()
  local style = tostring(meta and (meta['Body Style'] or meta.BodyStyle or meta.bodyStyle) or ''):lower()
  local identity = modelKey .. ' ' .. modelName .. ' ' .. style
  if identity:find('hseries', 1, true) or identity:find('h_series', 1, true)
      or identity:find('van', 1, true) or style:find('minivan', 1, true) then return 'van' end
  if identity:find('pickup', 1, true) or identity:find('d_series', 1, true)
      or identity:find('d-series', 1, true) or identity:find('ute', 1, true)
      or style:find('truck', 1, true) or style:find('pickup', 1, true) then
    return length and length > 6.6 and 'van' or 'pickup'
  end
  if identity:find('roamer', 1, true) or identity:find('hopper', 1, true)
      or style:find('suv', 1, true) or style:find('crossover', 1, true) then return 'suv' end
  if modelType == 'truck' then return length and length > 6.6 and 'van' or 'pickup' end
  if length and width and (length > 5.8 or width > 2.2) then return 'van' end
  return 'sedan'
end

-- Match each low-cost body family to the same family in the reserved BeamNG pool.
local function donorFits(id, traffic, body)
  if donorBody(id, traffic) ~= body then return false end
  local o = getObjectByID(id)
  local length, width = tonumber(traffic and traffic.length), tonumber(traffic and traffic.width)
  if not length and o and o.initialNodePosBB then
    local ext = o.initialNodePosBB:getExtents()
    length, width = tonumber(ext.y), tonumber(ext.x)
  end
  if not length or not width then return true end
  if body == 'pickup' then return length >= 4.2 and length <= 6.6 and width >= 1.6 and width <= 2.5 end
  if body == 'suv' then return length >= 3.6 and length <= 5.8 and width >= 1.5 and width <= 2.35 end
  if body == 'van' then return length >= 4.2 and length <= 8.0 and width >= 1.65 and width <= 2.8 end
  return length >= 3.4 and length <= 5.6 and width >= 1.35 and width <= 2.2
end

local function refreshProxyObstacles(dtReal)
  obstacleIn = obstacleIn - math.max(0, dtReal or 0)
  if obstacleIn > 0 then return end
  obstacleIn = 0.25
  for i = #proxyObstacles, 1, -1 do proxyObstacles[i] = nil end
  local gt = gameplay_traffic
  if not (gt and gt.getTrafficList and gt.getTrafficData) then return end
  local data = gt.getTrafficData() or {}
  localplayers.snapshot(promotionControlled)
  for _, id in ipairs(gt.getTrafficList() or {}) do
    local t, o = data[id], getObjectByID(id)
    if t and t.isAi ~= false and o and o.getActive and o:getActive()
        and not localplayers.controlled(id, o, promotionControlled) then
      local p, v = o:getPosition(), o:getVelocity()
      proxyObstacles[#proxyObstacles + 1] = { id = id, x = p.x, y = p.y, z = p.z, vx = v.x, vy = v.y }
    end
  end
end

-- BeamNG's vehicle AI only sees physical traffic. Feed promoted cars a temporary speed cap for
-- a stopped/slow proxy ahead so the real vehicle queues behind the render-only car instead of
-- driving through it. Restore the proxy's own speed cap after the lane clears.
local function updateFlowYield(dtReal)
  proxyYieldIn = proxyYieldIn - math.max(0, dtReal or 0)
  if proxyYieldIn > 0 then return end
  proxyYieldIn = 0.15
  for _, traffic in pairs(promoted) do
    if traffic.flow and traffic.reserved and traffic.id then
      local o = getObjectByID(traffic.id)
      if o and o.getActive and o:getActive() then
        local pos, vel, dir = o:getPosition(), o:getVelocity(), o:getDirectionVector()
        local headingLen = math.sqrt(dir.x * dir.x + dir.y * dir.y)
        if headingLen > 0.1 then
          local hx, hy = dir.x / headingLen, dir.y / headingLen
          local realSpeed = math.max(0, vel.x * hx + vel.y * hy)
          local speedCap = traffic.proxySpeed or realSpeed
          for _, fake in ipairs(trafficflow.near(flowState, pos.x, pos.y, 150)) do
            local dx, dy = fake.x - pos.x, fake.y - pos.y
            local along = dx * hx + dy * hy
            local across = math.abs(dx * -hy + dy * hx)
            local dz = math.abs((fake.z or pos.z) - pos.z)
            local sameLaneDirection = hx * fake.dx + hy * fake.dy > 0.65
            if along > 0 and along < 150 and across < 3.1 and dz < 3.5 and sameLaneDirection then
              local gap = along - 4.5
              local leadSpeed = math.max(0, fake.speed or 0)
              local safeCap
              if gap <= 4 then
                safeCap = 0
              else
                local followCap = leadSpeed + math.max(0, gap - 6) / 1.35
                local brakingCap = math.sqrt(leadSpeed * leadSpeed + 5 * math.max(0, gap - 2))
                safeCap = math.min(followCap, brakingCap)
              end
              speedCap = math.min(speedCap, safeCap)
            end
          end
          local oldCap = traffic.aiProxyCap
          if oldCap == nil or math.abs(speedCap - oldCap) >= 0.8 or clock - (traffic.aiProxyCapAt or 0) >= 0.8 then
            o:queueLuaCommand(string.format('ai.setSpeedMode("limit"); ai.setSpeed(%.2f)', speedCap))
            traffic.aiProxyCap, traffic.aiProxyCapAt = speedCap, clock
          end
        end
      end
    end
  end
end

local function placeProxy(actor, state, box, view)
  local item, lights = actor.item, actor.lights
  local o = item and scenetree.findObjectById(item.id)
  if not o then return end
  local p = sample(actor.route, actor.phase, state.anchor, actor.pos or {})
  actor.pos = p
  local lightObjects = lights and { scenetree.findObjectById(lights[1].id), scenetree.findObjectById(lights[2].id) }
  local player = state.player or { x = (box[1] + box[3]) * 0.5, y = (box[2] + box[4]) * 0.5, z = 0 }
  local dx, dy, dz = p.x - player.x, p.y - player.y, p.z - paths.LIGHT_HEIGHT_M - player.z
  local distance = math.sqrt(dx * dx + dy * dy + dz * dz)
  local z = p.z - paths.LIGHT_HEIGHT_M + 0.15
  -- Keep the proxy visible right up to a confirmed successful handoff. Donor availability alone is
  -- not proof that a compatible car was respawned; fading early caused a visible hole on failed attempts.
  local nearFade = 5
  local fade = paths.smooth((distance - nearFade) / (nearFade < PROXY_NEAR_M and 20 or PROXY_FADE_IN_M))
    * paths.smooth((PROXY_FAR_M - distance) / PROXY_FADE_OUT_M)
  local routeFade = math.max(p.alpha, paths.smooth((300 - distance) / 180))
  if fade <= 0.01 or routeFade <= 0.01 then
    setProxyVisible(item, o, false)
    for i = 1, 2 do if lightObjects and lightObjects[i] then setProxyVisible(lights[i], lightObjects[i], false) end end
    return
  end
  for _, obstacle in ipairs(proxyObstacles) do
    if math.abs(obstacle.z - z) < 4 and (obstacle.x - p.x) ^ 2 + (obstacle.y - p.y) ^ 2 < 7 * 7 then
      setProxyVisible(item, o, false)
      for i = 1, 2 do if lightObjects and lightObjects[i] then setProxyVisible(lights[i], lightObjects[i], false) end end
      actor.nextAt = clock + 0.25
      return
    end
  end
  local bodyBlend = lights and paths.smooth((PROXY_BODY_DISTANCE_M - distance) / PROXY_BODY_FADE_M) or 1
  local bodyAlpha = fade * bodyBlend * routeFade
  setProxyVisible(item, o, bodyAlpha > 0.01)
  if bodyAlpha > 0.01 then
    -- The low-poly mesh's long axis is +X, so yaw rotates that axis onto the road tangent.
    local halfYaw = math.atan2(p.dy, p.dx) * 0.5
    local q = actor.quat or {}
    actor.quat = q
    q.x, q.y, q.z, q.w = 0, 0, math.sin(halfYaw), math.cos(halfYaw)
    local changed = false
    local paint = actor.paint or paintFromRank(paths.rank(actor.key .. ':paint'))
    actor.paint = paint
    changed = setProxyPaint(item, o, paint, bodyAlpha, actor.body) or changed
    if item.x ~= p.x or item.y ~= p.y or item.z ~= z or item.qz ~= q.z or item.qw ~= q.w then
      o:setPosRot(p.x, p.y, z, q.x, q.y, q.z, q.w)
      item.x, item.y, item.z, item.qz, item.qw = p.x, p.y, z, q.z, q.w
      changed = true
    end
    if changed then o:updateInstanceRenderData() end
  end
  if lightObjects then
    local head, tail = paths.facing(p.dx, p.dy, player.x - p.x, player.y - p.y)
    local lightPos = { x = p.x, y = p.y, z = p.z, dx = p.dx, dy = p.dy }
    local lightForward = view and view.forward
    local lightQ = quatFromDir(lightForward and vec3(-lightForward.x, -lightForward.y, -lightForward.z)
      or -core_camera.getForward())
    local radius = paths.glowRadius(distance)
    place(lights[1], lightPos, lightQ, radius, false, fade * routeFade * (1 - bodyBlend) * head, 1.5)
    place(lights[2], lightPos, lightQ, radius, true, fade * routeFade * (1 - bodyBlend) * tail, -1.5)
  end
  local hz = distance < 250 and 25 or distance < 450 and 15 or distance < 1000 and 8 or 5
  actor.nextAt = clock + 1 / hz
end

-- Use a prewarmed BeamNG traffic pool; never create physics vehicles during driving. Eight real proxy cars can
-- be active at once, with four dormant spares ready for handoff. This prevents the proxy population from becoming
-- an unbounded number of soft-body vehicles and mirrors the pool strategy used by EarthNG.
local function promoteProxy(candidate, state, box, view)
  local gt = gameplay_traffic
  if not (gt and gt.getState and gt.getState() == 'on' and gt.getTrafficData
      and gt.respawnVehicle and gt.useReservedVehicle and gt.returnReservedVehicle) then
    return false
  end
  if activePromotedCount() >= M.MAX_ACTIVE_PROXY_CARS then return false end

  local isFlow = candidate.flowCar ~= nil
  local p = isFlow and trafficflow.pose(flowState, candidate.flowCar)
    or sample(candidate.route, candidate.phase, state.anchor, {}, elapsed)
  if not isFlow and p.alpha < 0.1 then return false end
  local player = state.player or { x = (box[1] + box[3]) * 0.5, y = (box[2] + box[4]) * 0.5,
    z = 0, vx = 0, vy = 0, speed = 0 }
  local x, y, z = p.x, p.y, isFlow and (p.z + 0.15) or (p.z - paths.LIGHT_HEIGHT_M + 0.15)
  local dx, dy, dz = x - player.x, y - player.y, z - player.z
  local distance = math.sqrt(dx * dx + dy * dy + dz * dz)
  if distance > PROMOTE_MAX_M or distance < 25 then return false end
  local speed = isFlow and candidate.speed or candidate.route.speed
  local paint = candidate.paint or paintFromRank(paths.rank(candidate.key .. ':paint'))
  local body = candidate.body or 'sedan'
  local cvx, cvy = p.dx * speed - (player.vx or 0), p.dy * speed - (player.vy or 0)
  local closing = math.max(0, -(dx * cvx + dy * cvy) / math.max(distance, 1))
  local promoteRadius = math.min(PROMOTE_MAX_M, math.max(180, closing * 4))
  if distance > promoteRadius then return false end

  local claimedOk, claimed = pcall(gt.useReservedVehicle, DONOR_PROVIDER, 1, nil,
    function(id, traffic) return donorFits(id, traffic, body) end)
  local donor = claimedOk and type(claimed) == 'table' and claimed[1] or nil
  if not donor then return false end
  local obj = getObjectByID(donor)
  if not obj then
    pcall(gt.returnReservedVehicle, donor, true)
    return false
  end

  local rot = quatFromDir(vec3(p.dx, p.dy, 0), vec3(0, 0, 1))
  local respawnCalled, respawnResult = pcall(gt.respawnVehicle, donor, vec3(x, y, z), rot)
  if not respawnCalled or respawnResult == false then pcall(gt.returnReservedVehicle, donor, true); return false end
  local actual = obj:getPosition()
  if (actual.x - x) ^ 2 + (actual.y - y) ^ 2 + (actual.z - z) ^ 2 > 30 * 30 then
    pcall(gt.returnReservedVehicle, donor, true)
    return false
  end
  applyPaint(obj, paint)
  -- Reset the dormant donor's AI and seed a delayed launch at the proxy route speed.
  obj:queueLuaCommand('ai.setMode("traffic")')
  obj:queueLuaCommand('ai.setAggression(0.3)')
  obj:queueLuaCommand('ai.setSpeedMode("legal")')
  obj:queueLuaCommand('ai.driveInLane("on")')
  obj:queueLuaCommand('ai.reset()')
  local t = (gt.getTrafficData() or {})[donor]
  pcall(require('beamngEarth/navgraph').noteTrafficRespawn, donor, speed)
  if isFlow then trafficflow.take(flowState, candidate.flowCar) end
  promoted[candidate.key] = { id = donor, respawnCount = t and t.respawnCount, reserved = true,
    route = isFlow and nil or candidate.route, phase = candidate.phase, flow = isFlow,
    body = body, paint = paint, proxySpeed = speed }
  stats.promotions = (stats.promotions or 0) + 1
  return true
end

local function renderFlowTraffic(dtReal, dtSim, state, box, ng, playerRoad, adaptiveEnabled, view)
  local player = state.player or { x = (box[1] + box[3]) * .5, y = (box[2] + box[4]) * .5,
    z = 0, vx = 0, vy = 0 }
  local graph = ng.currentGraph and ng.currentGraph()
  local trafficOn = gameplay_traffic and gameplay_traffic.getState and gameplay_traffic.getState() == 'on'
  if not (proxyEnabled and requested and trafficOn and graph) then
    for id, actor in pairs(flowActors) do hideProxy(actor); flowActors[id] = nil end
    trafficflow.setTarget(flowState, 0)
    return
  end
  if trafficflow.sync(flowState, graph, ng.rules and ng.rules.rightHandDrive) then flowRenderIn = 0 end
  local density = densityNow()
  local target = flowPopulation(density)
  local priorityRoads = trafficflow.priorityRoadIds(flowState, player.x, player.y, playerRoad,
    player.vx, player.vy)
  local highway = playerRoad and (tonumber(playerRoad.speedLimit) or 0) >= 22
  -- Allocate the selected population, rather than treating 10+3 as a cap for the current road.
  -- Highways keep most cars on the player's carriageway; local streets spread more traffic nearby.
  local primaryRoadTarget = math.floor(target * (highway and .70 or .35) + .5)
  local corridorTarget = math.max(primaryRoadTarget,
    math.floor(target * (highway and .90 or .50) + .5))
  local obstacles = {}
  for _, o in ipairs(proxyObstacles) do obstacles[#obstacles + 1] = o end
  obstacles[#obstacles + 1] = { x = player.x, y = player.y, z = player.z or 0,
    vx = player.vx or 0, vy = player.vy or 0 }
  flowSignalIn = flowSignalIn - math.max(0, dtReal or 0)
  if flowSignalIn <= 0 then
    flowSignalStates = ng.flowSignalStates and ng.flowSignalStates() or {}
    flowSignalIn = .2
  end
  trafficflow.step(flowState, dtSim or dtReal or 0, player, target, obstacles, flowSignalStates,
    priorityRoads, primaryRoadTarget, corridorTarget)
  updateFlowYield(dtReal)

  local visible = trafficflow.near(flowState, player.x, player.y, trafficflow.VISUAL_RADIUS + 35)
  local wanted = {}
  for _, p in ipairs(visible) do wanted[p.car.id] = p end
  for id, actor in pairs(flowActors) do
    if not wanted[id] then hideProxy(actor); flowActors[id] = nil end
  end

  -- Promote the nearest same-road flow car first; adaptive traffic enables the user's existing
  -- 120 m secondary slot for a nearby crossing road. Predicted pass distance breaks equal-distance ties.
  if promotionIn <= 0 then
    local candidate
    for _, p in ipairs(trafficflow.near(flowState, player.x, player.y, PROMOTE_MAX_M)) do
      local currentActor = flowActors[p.car.id]
      local dx, dy, dz = p.x-player.x, p.y-player.y, p.z-player.z
      local d = math.sqrt(dx*dx+dy*dy+dz*dz)
      local rvx, rvy = p.vx-(player.vx or 0), p.vy-(player.vy or 0)
      local dot, w2 = dx*rvx+dy*rvy, rvx*rvx+rvy*rvy
      local closing = math.max(0, -dot/math.max(d,1))
      local radius = math.min(PROMOTE_MAX_M, math.max(180, closing*4))
      local soon = d
      if dot < 0 and w2 > 1e-6 then
        local t = math.min(4, -dot/w2)
        soon = math.sqrt((dx+rvx*t)^2+(dy+rvy*t)^2+dz*dz)
      end
      local lanePriority
      if (not currentActor or (currentActor.handoffRetryAt or 0) <= clock)
          and dot < 0 and d >= 25 and d <= radius then
        local onPlayerRoad = playerRoad and (p.edgeKey == playerRoad.edgeKey
          or sameRoadCorridor(playerRoad, p, player.x, player.y, p.x, p.y))
        local joiningAtIntersection = crossingApproach(playerRoad, p, player.x, player.y, player.z,
          p.x, p.y, p.z, d)
        if onPlayerRoad then lanePriority = 0
        elseif joiningAtIntersection then lanePriority = 1
        elseif adaptiveEnabled and d <= PROXY_NEAR_M then lanePriority = 2 end
      end
      if lanePriority ~= nil
          and (lanePriority == 0 or d <= PROXY_NEAR_M)
          and (not candidate or lanePriority < candidate.lanePriority
            or (lanePriority == candidate.lanePriority and (d < candidate.distance
              or (d == candidate.distance and soon < candidate.soon)))) then
        candidate = { key = 'flow:' .. tostring(p.car.id), flowCar = p.car, speed = p.speed,
          paint = p.paint, body = p.body, lanePriority = lanePriority, soon = soon,
          distance = d, x = p.x, y = p.y, dx = p.dx, dy = p.dy, vx = p.vx, vy = p.vy }
      end
    end
    if candidate and (activePromotedCount() >= M.MAX_ACTIVE_PROXY_CARS or not hasFreeProxyDonor())
        and (candidate.distance <= PROXY_NEAR_M or candidate.soon <= 4) then
      if preemptRecedingProxy(state, view) then handoffAvailable = hasFreeProxyDonor() end
    end
    if candidate and promoteProxy(candidate, state, box, nil) then
      local id = candidate.flowCar.id
      hideProxy(flowActors[id]); flowActors[id] = nil
      promotionIn = .2
    elseif candidate then
      local actor = flowActors[candidate.flowCar.id]
      if candidate.lanePriority <= 1 and candidate.distance <= proxyCleanupRadius(candidate, player) then
        local id = candidate.flowCar.id
        trafficflow.take(flowState, candidate.flowCar)
        wanted[id] = nil
        if actor then hideProxy(actor); flowActors[id] = nil end
      elseif actor then
        actor.handoffBlocked, actor.handoffRetryAt = true, clock + 1
      end
      promotionIn = .15
    end
  end

  -- Remove unconverted same-road/crossing proxies from the speed-aware near-player zone,
  -- even when their motion did not make them an eligible handoff candidate this frame.
  for _, p in ipairs(visible) do
    local onPlayerRoad = playerRoad and (p.edgeKey == playerRoad.edgeKey
      or sameRoadCorridor(playerRoad, p, player.x, player.y, p.x, p.y))
    local joiningAtIntersection = crossingApproach(playerRoad, p, player.x, player.y, player.z,
      p.x, p.y, p.z, p.distance)
    if (onPlayerRoad or joiningAtIntersection) and p.distance <= proxyCleanupRadius(p, player) then
      local id = p.car.id
      trafficflow.take(flowState, p.car)
      wanted[id] = nil
      local actor = flowActors[id]
      if actor then hideProxy(actor); flowActors[id] = nil end
    end
  end

  local created = 0
  flowRenderIn = flowRenderIn - math.max(0, dtReal or 0)
  for id, p in pairs(wanted) do
    local actor = flowActors[id]
    if not actor then
      local item, freshItem = proxyItem(created < 2, p.paint, p.body)
      if item then
        if freshItem then created = created + 1 end
        local lights, freshLights = proxyLightPair(created < 2)
        if lights then
          if freshLights then created = created + 1 end
          actor = { key = 'flow:' .. tostring(id), car = p.car, body = p.body, item = item, lights = lights, nextAt = 0 }
          item.actor, lights.actor = actor, actor
          flowActors[id] = actor
        else
          item.busy = false
        end
      end
    else
      actor.car = p.car
    end
    if actor and (flowRenderIn <= 0 or not actor.item.x) then
      local item, o = actor.item, scenetree.findObjectById(actor.item.id)
      if o then
        local alpha = p.distance < 820 and 1 or paths.smooth((trafficflow.VISUAL_RADIUS-p.distance)/100)
        if p.distance < 5 then alpha = 0 end
        local bodyBlend = actor.lights and paths.smooth((PROXY_BODY_DISTANCE_M-p.distance)/PROXY_BODY_FADE_M) or 1
        local bodyAlpha = alpha * bodyBlend
        setProxyPaint(item, o, p.paint, bodyAlpha, p.body)
        if bodyAlpha > .01 then
          local z = p.z + .15
          -- The proxy DAE's long axis is +X; rotate it onto the road tangent.
          local halfYaw = math.atan2(p.dy,p.dx)*.5
          local qz,qw=math.sin(halfYaw),math.cos(halfYaw)
          if item.x ~= p.x or item.y ~= p.y or item.z ~= z or item.qz ~= qz or item.qw ~= qw then
            o:setPosRot(p.x,p.y,z,0,0,qz,qw)
            item.x,item.y,item.z,item.qz,item.qw=p.x,p.y,z,qz,qw
            o:updateInstanceRenderData()
          end
        end
        setProxyVisible(item, o, bodyAlpha > .01)
        if actor.lights then
          local cam = view and view.cam or core_camera.getPosition()
          local head, tail = paths.facing(p.dx,p.dy,cam.x-p.x,cam.y-p.y)
          local lightPos = { x=p.x, y=p.y, z=p.z+paths.LIGHT_HEIGHT_M, dx=p.dx, dy=p.dy }
          local lightForward = view and view.forward
          local lightQ = quatFromDir(lightForward and vec3(-lightForward.x, -lightForward.y, -lightForward.z)
            or -core_camera.getForward())
          local radius = paths.glowRadius(p.distance)
          place(actor.lights[1],lightPos,lightQ,radius,false,alpha*(1-bodyBlend)*head,1.5)
          place(actor.lights[2],lightPos,lightQ,radius,true,alpha*(1-bodyBlend)*tail,-1.5)
        end
      end
      actor.nextAt = clock + 1/25
    end
  end
  if flowRenderIn <= 0 then flowRenderIn = 1/25 end
end

local function updateProxies(dtReal, dtSim, state, box)
  local cam, forward = core_camera.getPosition(), core_camera.getForward()
  local view = viewOf(cam, forward)
  local ng = require('beamngEarth/navgraph')
  local player = state.player or { x = (box[1] + box[3]) * 0.5, y = (box[2] + box[4]) * 0.5, z = 0 }
  local playerRoad = ng.roadContextAt and ng.roadContextAt(player.x, player.y, player.z)
  local trafficStatus = ng.trafficStatus and ng.trafficStatus()
  local adaptiveEnabled = trafficStatus and trafficStatus.enabled == true
  local trafficOn = gameplay_traffic and gameplay_traffic.getState and gameplay_traffic.getState() == 'on'
  promotedCheckIn = promotedCheckIn - math.max(0, dtReal or 0)
  if promotedCheckIn <= 0 then
    refreshPromoted(state, cam, forward, view)
    handoffAvailable = hasFreeProxyDonor()
    promotedCheckIn = 0.5
  end
  if not proxyEnabled or not requested or not trafficOn then
    proxySelected = {}
    for key, actor in pairs(proxyActors) do hideProxy(actor); proxyActors[key] = nil end
  end
  refreshProxyObstacles(dtReal)
  for key, actor in pairs(proxyActors) do
    if not proxySelected[key] or store.routes[actor.route.id] ~= actor.route then
      hideProxy(actor)
      proxyActors[key] = nil
    end
  end
  promotionIn = promotionIn - math.max(0, dtReal or 0)
  renderFlowTraffic(dtReal, dtSim, state, box, ng, playerRoad, adaptiveEnabled, view)
  if promotionIn <= 0 and next(proxySelected) then
    local candidate
    for _, item in pairs(proxySelected) do
      local p = sample(item.route, item.phase, state.anchor, {}, elapsed)
      local player = state.player or { x = (box[1] + box[3]) * 0.5, y = (box[2] + box[4]) * 0.5,
        z = 0, vx = 0, vy = 0 }
      local dx, dy, dz = p.x - player.x, p.y - player.y, p.z - player.z
      local distance = math.sqrt(dx * dx + dy * dy + dz * dz)
      local rvx, rvy = p.dx * item.route.speed - (player.vx or 0),
        p.dy * item.route.speed - (player.vy or 0)
      local dot, relativeSpeed2 = dx * rvx + dy * rvy, rvx * rvx + rvy * rvy
      local closing = math.max(0, -dot / math.max(distance, 1))
      -- Start the physics handoff several seconds before closest approach. The old 2.5 s window
      -- was too short for BeamNG's traffic AI to settle after a pooled vehicle was respawned.
      local radius = math.min(PROMOTE_MAX_M, math.max(180, closing * 4))
      local soon = distance
      if dot < 0 and relativeSpeed2 > 1e-6 then
        local t = math.min(4, -dot / relativeSpeed2)
        soon = math.sqrt((dx + rvx * t) ^ 2 + (dy + rvy * t) ^ 2 + dz * dz)
      end
      local lanePriority
      local currentActor = proxyActors[item.key]
      if (not currentActor or (currentActor.handoffRetryAt or 0) <= clock)
          and distance >= 25 and distance <= radius and dot < 0 and p.alpha >= 0.1 then
        if playerRoad then
          local sameCorridor = item.route and sameRoadCorridor(playerRoad, p, player.x, player.y, p.x, p.y)
          local joiningAtIntersection = crossingApproach(playerRoad, p, player.x, player.y, player.z,
            p.x, p.y, p.z, distance)
          if sameCorridor then
            lanePriority = 0
          elseif joiningAtIntersection then
            lanePriority = 1
          elseif adaptiveEnabled and distance <= PROXY_NEAR_M then
            lanePriority = 2
          end
        elseif adaptiveEnabled and distance <= PROXY_NEAR_M then
          lanePriority = 2
        end
      end
      item.handoffEligible, item.lanePriority = lanePriority ~= nil, lanePriority
      -- Prefer the player's current road and shortest distance; side roads keep their fake cars.
      if lanePriority ~= nil and (lanePriority == 0 or distance <= PROXY_NEAR_M)
          and (not candidate or lanePriority < candidate.lanePriority
            or (lanePriority == candidate.lanePriority and (distance < candidate.distance
              or (distance == candidate.distance and soon < candidate.soon)))) then
        item.distance, item.soon = distance, soon
        candidate = item
      end
    end
    if candidate and (activePromotedCount() >= M.MAX_ACTIVE_PROXY_CARS or not hasFreeProxyDonor())
        and (candidate.distance <= PROXY_NEAR_M or candidate.soon <= 4) then
      if preemptRecedingProxy(state, view) then handoffAvailable = hasFreeProxyDonor() end
    end
    if candidate and promoteProxy(candidate, state, box, view) then
      proxySelected[candidate.key] = nil
      hideProxy(proxyActors[candidate.key])
      proxyActors[candidate.key] = nil
      promotionIn = 0.2
    elseif candidate then
      local actor = proxyActors[candidate.key]
      local candidatePose = sample(candidate.route, candidate.phase, state.anchor, {}, elapsed)
      local radius = proxyCleanupRadius(candidatePose, player, candidate.route and candidate.route.speed)
      if candidate.lanePriority <= 1 and candidate.distance <= radius then
        suppressProxy(candidate.key, radius)
      elseif actor then
        actor.handoffBlocked, actor.handoffRetryAt = true, clock + 1
      end
      promotionIn = 0.15
    elseif next(proxySelected) then
      promotionIn = 0.15
    end
  end
  -- Retire every unconverted same-road/crossing route proxy in the speed-aware near-player zone.
  -- Suppression lasts until that route slot passes the cleanup distance plus a 20 m buffer.
  for key, item in pairs(proxySelected) do
    local p = sample(item.route, item.phase, state.anchor, {}, elapsed)
    local dx, dy, dz = p.x-player.x, p.y-player.y, p.z-player.z
    local distance = math.sqrt(dx*dx+dy*dy+dz*dz)
    local onPlayerRoad = playerRoad and sameRoadCorridor(playerRoad, p, player.x, player.y, p.x, p.y)
    local joiningAtIntersection = crossingApproach(playerRoad, p, player.x, player.y, player.z,
      p.x, p.y, p.z, distance)
    local radius = proxyCleanupRadius(p, player, item.route.speed)
    if distance <= radius and (onPlayerRoad or joiningAtIntersection) then
      suppressProxy(key, radius)
    end
  end
  local created = 0
  for key, candidate in pairs(proxySelected) do
    local actor = proxyActors[key]
    if not actor then
      local paint = paintFromRank(paths.rank(key .. ':paint'))
      actor = { key = key, route = candidate.route, phase = candidate.phase, paint = paint, body = candidate.body,
        handoffEligible = candidate.handoffEligible }
      local item, freshItem = proxyItem(created < 2, paint, actor.body)
      if item then
        if freshItem then created = created + 1 end
        local lights, freshLights = proxyLightPair(created < 2)
        if lights then
          if freshLights then created = created + 1 end
          actor.item, actor.lights = item, lights
          item.actor, lights.actor = actor, actor
          proxyActors[key] = actor
          placeProxy(actor, state, box, view)
        else
          item.busy = false
        end
      end
    else
      actor.route, actor.phase = candidate.route, candidate.phase
    end
    actor.handoffEligible = candidate.handoffEligible
  end
  proxyIn = proxyIn - math.max(0, dtReal or 0)
  if proxyIn <= 0 then
    proxyIn = 1 / 25
    for _, actor in pairs(proxyActors) do
      if not actor.nextAt or clock >= actor.nextAt then placeProxy(actor, state, box, view) end
    end
  end
end

place = function(item, p, q, r, red, a, along)
  a = math.floor(math.min(1, a) * 255 + 0.5)
  local hidden = a < 1
  if hidden and item.hidden then return end
  local o = scenetree.findObjectById(item.id)
  if not o then return end
  local changed = item.hidden ~= hidden
  if changed then o:setHidden(hidden); item.hidden = hidden end
  if hidden then return end
  if not item.r or math.abs(item.r - r) > 0.01 then o:setScale(vec3(r, r, r)); item.r, changed = r, true end
  local x, y = p.x + p.dx * along, p.y + p.dy * along
  if item.x ~= x or item.y ~= y or item.z ~= p.z or item.qx ~= q.x or item.qy ~= q.y or item.qz ~= q.z or item.qw ~= q.w then
    o:setPosRot(x, y, p.z, q.x, q.y, q.z, q.w)
    item.x, item.y, item.z, item.qx, item.qy, item.qz, item.qw = x, y, p.z, q.x, q.y, q.z, q.w
    changed = true
  end
  if item.alpha ~= a then
    o:setField('instanceColor', 0, string.format('1 %.3f %.3f %.3f', red and 0.025 or 0.9,
      red and 0.012 or 0.75, a / 255))
    item.alpha = a
    changed = true
  end
  if changed then o:updateInstanceRenderData() end
end

-- The schedule (M.update): a min-heap of { t, key, car } (stale entries skipped: car.queued is its live one).
local function heapPush(h, e)
  h[#h + 1] = e
  local i = #h
  while i > 1 do
    local parent = math.floor(i / 2)
    if h[parent].t <= h[i].t then break end
    h[parent], h[i], i = h[i], h[parent], parent
  end
end

heapPop = function(h)
  local top, n = h[1], #h
  h[1] = h[n]                   -- (two statements: a multiple assignment's order is undefined, h[1] is h[n] at n = 1)
  h[n] = nil
  n = n - 1
  local i = 1
  while i * 2 <= n do
    local c = i * 2
    if c < n and h[c + 1].t < h[c].t then c = c + 1 end
    if h[i].t <= h[c].t then break end
    h[i], h[c], i = h[c], h[i], c
  end
  return top
end

local function setVisible(car, v)
  if (car.visible == true) ~= (v == true) then stats.visible = stats.visible + (v and 1 or -1) end
  car.visible = v
end

-- One car: its fade, and its pose when due (the old per-frame loop's body, as it was).
visit = function(key, car, state, cam, box, view, q, dtReal)
  local want = lightsWanted and requested and selected[key] and store.routes[car.route.id] == car.route
  stats.visits = (stats.visits or 0) + 1
  local fade0 = car.fade
  car.fade = want and readyFade(car) or math.max(0, car.fade - dtReal / FADE_S)
  local p = car.pos
  local travelled = math.max(0, elapsed - (car.sampleAt or elapsed)) * car.route.speed
  local edge, guard = math.huge, math.huge
  if p then edge, guard = paths.viewEdge(p.x, p.y, p.z, view, travelled * 2) end
  local nearView = guard <= 1.15
  local wrapped = (car.metres or 0) + travelled >= car.route.length
  if clock >= (car.nextAt or 0) or car.fade <= 0 or wrapped or nearView and not car.nearView
      or car.fade ~= fade0 then
    p = sample(car.route, car.phase, state.anchor, p or {})
    stats.samples = (stats.samples or 0) + 1
    car.pos = p
    car.sampleAt, car.metres = elapsed, (car.phase + elapsed * car.route.speed) % car.route.length
    local a, d = alpha(p, cam, box, car.route.reach)
    a = a * paths.smooth(car.fade)
    local head, tail = paths.facing(p.dx, p.dy, cam.x - p.x, cam.y - p.y)
    car.lit = a * math.max(head, tail) > 0.01
    edge = paths.viewEdge(p.x, p.y, p.z, view)
    nearView = edge <= 1.15
    local r = paths.glowRadius(d)
    place(car.pair[1], p, q, r, false, nearView and a * head or 0, 1.5)
    place(car.pair[2], p, q, r, true, nearView and a * tail or 0, -1.5)
    -- Retain surrounding actors, caching off-screen motion at 2 Hz without uploading hidden poses. Their swept
    -- motion and wraps also wake them, so entering the view or turning refreshes the pose before drawing it.
    -- (in view: as often as it moves about two thirds of a pixel across the screen, 4-45 Hz: 45 Hz to 2.5 km had
    -- most cars sampled several times a pixel, 35 poses a frame at High, 2026-11-03)
    local hz = nearView and math.max(4, math.min(45, 1.5 * car.route.speed / (math.max(d, 1) * (view.pix or 0.001))))
    car.nextAt = clock + (nearView and 1 / hz or 0.5)
  end
  car.nearView = nearView
  setVisible(car, car.lit and edge <= 1)
  if car.fade <= 0 and not want then
    setVisible(car, false)
    car.pair.busy, actors[key], hot[key] = false, nil, nil
    return
  end
  -- fading in or out, or waiting for its route: every frame; else at its nextAt
  if (want and car.fade < 1) or (not want and car.fade > 0) then
    hot[key] = car
  else
    hot[key] = nil
    if car.queued ~= car.nextAt then
      car.queued = car.nextAt
      heapPush(dueHeap, { t = car.nextAt, key = key, car = car })
    end
  end
end

function M.update(dtReal, dtSim, state, box, gov)
  if not capable then return end
  local started = os.clockhp()
  dtReal, dtSim = math.max(0, dtReal or 0), math.max(0, dtSim or 0)
  elapsed, checkIn, selectIn, clock = elapsed + dtSim, checkIn - dtReal, selectIn - dtReal, clock + dtReal
  if checkIn <= 0 then
    checkIn = 0.5
    local worldReady = state.active and box ~= nil and state.anchor ~= nil and require('beamngEarth/roads').enabled
    local trafficOn = gameplay_traffic and gameplay_traffic.getState() == 'on'
    local wantLights = enabled and require('beamngEarth/nightlights').isNight() and (withoutTraffic or trafficOn)
    local wantProxies = proxyEnabled and trafficOn
    wantLights = worldReady and wantLights or false
    if lightsWanted ~= wantLights then lightsWanted, dirty, wake = wantLights, true, true end
    local want = worldReady and (wantLights or wantProxies) or false
    if want ~= requested or wantLights ~= farRequested then
      net.send({ type = 'traffic_visuals', enabled = want, far = wantLights, reset = want and fresh or nil })
      if want then fresh, requestedAt = false, clock end
      requested, farRequested, dirty, wake = want, wantLights, true, true
    end
  end
  if not state.anchor or not box then return end
  -- (off, by day or by choice, with no car shown or fading and no pair kept: nothing to look at this frame)
  if not requested and next(actors) == nil and #pool == 0 and next(proxyActors) == nil and next(flowActors) == nil
      and next(promoted) == nil and #proxyPool == 0 and not scan then return end
  local cam, f = core_camera.getPosition(), core_camera.getForward()
  local player = state.player
  local focus = player or { x = (box[1] + box[3]) / 2, y = (box[2] + box[4]) / 2, z = 0 }
  if scanAnchor ~= state.anchor then
    scan, scanAnchor, dirty = nil, state.anchor, true
    flowRenderIn = 0
    for _, car in pairs(actors) do car.nextAt = 0 end   -- every plane moves with the origin, including the far LOD
    for _, car in pairs(proxyActors) do car.nextAt = 0 end
    wake = true
  end
  if scanFocus and ((focus.x - scanFocus.x) ^ 2 + (focus.y - scanFocus.y) ^ 2
      + (focus.z - scanFocus.z) ^ 2 > 2500 or box[1] ~= scanBox[1] or box[2] ~= scanBox[2]
      or box[3] ~= scanBox[3] or box[4] ~= scanBox[4]) then
    dirty, scan = true, nil
  end
  if selectIn <= 0 then
    selectIn = 2
    if not scanDensity or math.abs(proxyDensity(densityNow()) - scanDensity) >= 1 / MAX_CARS then dirty = true end
  end
  local use = store.points + store.pendingPoints
  if use < retryUse or store.tileCount < retryTiles then retryDirty = true end
  retryUse, retryTiles = use, store.tileCount
  local room = M.hasWork() and (not gov or gov:try('trafficVisuals'))
  local prepStarted = os.clockhp()
  if requested and room then
    retryDeferred()
    for _ = 1, 16 do
      local did, route = store:step()
      if not did then break end
      dirty = true
      if route and not route.readyAt then route.readyAt = clock end
      if os.clockhp() >= prepStarted + 0.00015 then break end
    end
    if not scan and dirty then
      local density = densityNow()
      scanDensity = proxyDensity(density)
      scanFocus = { x = focus.x, y = focus.y, z = focus.z }
      scanBox = { box[1], box[2], box[3], box[4] }
      local at, bounds = scanFocus, scanBox
      scan = coroutine.create(function() selection(state.anchor, at, bounds, density) end)
      dirty = false
    end
    local untilTime = prepStarted + 0.0003
    while scan and os.clockhp() < untilTime do
      local ok, err = coroutine.resume(scan)
      if not ok then error(err) end
      if coroutine.status(scan) == 'dead' then scan = nil end
    end
    local made, assigned = 0, 0
    for key, car in pairs(selected) do
      if not actors[key] and store.routes[car.route.id] == car.route then
        local p, created = pair(made < 2)
        if not p then break end
        car.pair, car.fade, car.admittedAt = p, 0, clock
        actors[key], assigned = car, assigned + 1
        hot[key] = car                          -- (fading in: every frame until it has)
        if created then made = made + 1 end
        if assigned >= 64 then break end
      end
    end
  end
  if requested and room and gov then gov:charge('trafficVisuals', (os.clockhp() - prepStarted) * 1000) end
  updateProxies(dtReal, dtSim, state, box)
  local q = quatFromDir(-f)
  local view = viewOf(cam, f)
  -- Each car is looked at when it is due (its nextAt, from a heap), while it fades in or out (hot), and all of them
  -- when the view turned or moved enough or what is wanted changed (wake): not every car every frame, which was
  -- ~1.2 us a car a frame (0.8 ms at High's 655 cars, Griffith Observatory, 2026-11-03) for the few due.
  local lw = lastWake
  if wake or not lw or f.x * lw.fx + f.y * lw.fy + f.z * lw.fz < WAKE_COS
      or (cam.x - lw.x) ^ 2 + (cam.y - lw.y) ^ 2 + (cam.z - lw.z) ^ 2 > WAKE_M * WAKE_M then
    wake, lastWake = false, { fx = f.x, fy = f.y, fz = f.z, x = cam.x, y = cam.y, z = cam.z }
    stats.wakes = (stats.wakes or 0) + 1
    for key, car in pairs(actors) do visit(key, car, state, cam, box, view, q, dtReal) end
  else
    for key, car in pairs(hot) do visit(key, car, state, cam, box, view, q, dtReal) end
    local h = dueHeap
    while h[1] and h[1].t <= clock do
      local e = heapPop(h)
      local car = e.car
      if actors[e.key] == car and car.queued == e.t and not hot[e.key] then
        car.queued = nil
        visit(e.key, car, state, cam, box, view, q, dtReal)
      end
    end
  end
  local active = next(actors) ~= nil or next(proxyActors) ~= nil or next(flowActors) ~= nil
  idle = active and 0 or idle + dtReal
  proxyIdle = (next(proxyActors) ~= nil or next(flowActors) ~= nil) and 0 or proxyIdle + dtReal
  if idle > 30 and #pool > 0 then
    -- Retire a few idle pairs per frame; deleting the whole city pool at once makes disabling it hitch.
    for _ = 1, math.min(8, #pool) do
      local retiring = table.remove(pool)
      forgetProxyLightPair(retiring)
      deletePair(retiring)
    end
    if #pool == 0 then clearObjects() end
  elseif #pool > populationLimit then
    local removed = 0
    for i = #pool, 1, -1 do
      if not pool[i].busy then
        local retiring = table.remove(pool, i)
        forgetProxyLightPair(retiring)
        deletePair(retiring)
        removed = removed + 1
        if removed >= 8 or #pool <= populationLimit then break end
      end
    end
  end
  if #proxyPool > proxyCarCount then
    local removed = 0
    for i = #proxyPool, 1, -1 do
      local item = proxyPool[i]
      if not item.busy then
        local o = scenetree.findObjectById(item.id)
        if o then o:delete() end
        table.remove(proxyPool, i)
        removed = removed + 1
        if removed >= 8 or #proxyPool <= proxyCarCount then break end
      end
    end
    if #proxyPool == 0 then
      local group = scenetree.findObject(PROXY_GROUP)
      if group then group:delete() end
    end
  end
  if proxyIdle > 30 and #proxyPool > 0 then
    for _ = 1, math.min(8, #proxyPool) do
      local item = table.remove(proxyPool)
      local o = scenetree.findObjectById(item.id)
      if o then o:delete() end
    end
    if #proxyPool == 0 then
      local group = scenetree.findObject(PROXY_GROUP)
      if group then group:delete() end
    end
  end
  local ms = (os.clockhp() - started) * 1000
  stats.frames, stats.totalMs, stats.maxMs = stats.frames + 1, stats.totalMs + ms, math.max(stats.maxMs, ms)
end

function M.status()
  local n, pnActive, stockTraffic, reservedTraffic = 0, 0, 0, 0
  local flow = trafficflow.status(flowState)
  for _ in pairs(actors) do n = n + 1 end
  local pn, pv = 0, 0
  for _ in pairs(proxyActors) do pn = pn + 1 end
  for _ in pairs(promoted) do pnActive = pnActive + 1 end
  local gt = gameplay_traffic
  local trafficData = gt and gt.getTrafficData and gt.getTrafficData() or {}
  for id, traffic in pairs(trafficData) do
    local o = getObjectByID(id)
    if o and o:getActive() and traffic.isAi ~= false then
      if traffic.reserved == DONOR_PROVIDER then reservedTraffic = reservedTraffic + 1
      else stockTraffic = stockTraffic + 1 end
    end
  end
  for _, item in ipairs(proxyPool) do if not item.hidden then pv = pv + 1 end end
  return { enabled = enabled, withoutTraffic = withoutTraffic, proxyEnabled = proxyEnabled, lightsWanted = lightsWanted,
    capable = capable, requested = requested,
    quality = quality, populationLimit = populationLimit,
    populationScans = stats.populationScans or 0,
    tiles = store.tileCount, routes = store.count,
    points = store.points, pendingPoints = store.pendingPoints, cars = n, planes = #pool * 2, visible = stats.visible,
    demand = stats.demand, quotas = stats.quotas,
    proxyCars = pn, proxyVisible = pv, proxyLimit = proxyCarCount, promotedVehicles = pnActive,
    flowCars = flow.cars, flowTarget = flow.target, flowLinks = flow.links, flowJunctions = flow.nodes,
    activeStockTraffic = stockTraffic, activeReservedTraffic = reservedTraffic,
    promotions = stats.promotions or 0, demotions = stats.demotions or 0,
    preemptions = stats.preemptions or 0,
    farCarsRetired = stats.farCarsRetired or 0,
    deferredTiles = deferredCount,
    meanMs = stats.totalMs / math.max(1, stats.frames), maxMs = stats.maxMs, frames = stats.frames,
    visits = stats.visits or 0, samples = stats.samples or 0, wakes = stats.wakes or 0, hot = (function()
      local h = 0 for _ in pairs(hot) do h = h + 1 end return h end)() }
end

function M.hasWork()
  if not capable or not requested then return false end
  local retryDue = retry and clock >= retryIn or not retry and retryDirty and deferredCount > 0
  if dirty or scan or selectIn <= 0 or store:hasWork() or retryDue or resendDue() then return true end
  for key in pairs(selected) do if not actors[key] then return true end end
  return false
end

return M
