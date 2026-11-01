-- Distant traffic (#565): a bounded pool of emissive planes following visual-road geotiles, never real vehicles.
local M = {}
local paths = require('beamngEarth/distanttraffic')
local geo = require('beamngEarth/geotile')
local net = require('beamngEarth/net')
M.PROTO, M.MAX_CARS = paths.PROTO, 512
local MAX_CARS, FADE_S = M.MAX_CARS, 2
local SHAPE = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.cdae'
local MATERIALS = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.materials.json'
local GROUP = 'bneDistantTraffic'
local store = paths.newRoutes()
local actors, pool, selected = {}, {}, {}
local enabled, withoutTraffic, capable, session, requested = true, false, false, nil, nil
local fresh = true
local elapsed, checkIn, selectIn, idle, clock = 0, 0, 0, 0, 0
local requestedAt = 0
local scan, scanAnchor, scanView, dirty = nil, nil, nil, true
local views = {}
local deferred, deferredCount, deferredSerial = {}, 0, 0
local retry, retryAt, retryDirty, retryUse, retryTiles, retryIn, resendAt = nil, 1, false, 0, 0, 0, 0
local resend = false
local temp = {}
local stats = { visible = 0, frames = 0, totalMs = 0, maxMs = 0 }

local function deletePair(pair)
  for _, item in ipairs(pair) do
    local o = scenetree.findObjectById(item.id)
    if o then o:delete() end
  end
end

local function clearObjects()
  for _, pair in ipairs(pool) do deletePair(pair) end
  local group = scenetree.findObject(GROUP)
  if group then group:delete() end
  actors, pool, selected, scan = {}, {}, {}, nil
  scanView = nil
end

function M.reset()
  if capable and requested then net.send({ type = 'traffic_visuals', enabled = false }) end
  clearObjects()
  store, elapsed, checkIn, selectIn, idle = paths.newRoutes(), 0, 0, 0, 0
  capable, session, requested, scanAnchor, dirty = false, nil, nil, nil, true
  views = {}
  deferred, deferredCount, deferredSerial = {}, 0, 0
  retry, retryAt, retryDirty, retryUse, retryTiles, retryIn, resendAt = nil, 1, false, 0, 0, 0, 0
  resend = false
  fresh = true
  stats = { visible = 0, frames = 0, totalMs = 0, maxMs = 0 }
end

function M.session(id, available)
  if id ~= session then M.reset() end
  session, capable, requested = id, available == true, nil
end

function M.setEnabled(v) enabled, checkIn, dirty = v == true, 0, true end
function M.setWithoutTraffic(v) withoutTraffic, checkIn, dirty = v == true, 0, true end
function M.onTeleport() clearObjects(); views, scanAnchor, dirty = {}, nil, true end

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
    net.send({ type = 'traffic_visuals', enabled = true, reset = true })
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

local function sample(route, phase, anchor, out)
  paths.sample(route, phase + elapsed * route.speed, out)
  local p = placement(route, anchor)
  out.x, out.y, out.z = geo.apply(p, out.x, out.y, out.z)
  local dx, dy = out.dx, out.dy
  out.dx, out.dy = p.r[1] * dx + p.r[2] * dy, p.r[4] * dx + p.r[5] * dy
  return out
end

local function alpha(p, cam, box, reach)
  local d = math.sqrt((cam.x - p.x) ^ 2 + (cam.y - p.y) ^ 2 + (cam.z - p.z) ^ 2)
  reach = reach or 10000
  return p.alpha * paths.distanceFade(d, 350, 300, reach, math.min(2000, reach * 0.25))
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

local function sameView(a, b)
  local p, q = a.cam, b.cam
  return (p.x - q.x) ^ 2 + (p.y - q.y) ^ 2 + (p.z - q.z) ^ 2 <= 2500
    and a.forward.x * b.forward.x + a.forward.y * b.forward.y + a.forward.z * b.forward.z > 0.985
    and a.up.x * b.up.x + a.up.y * b.up.y + a.up.z * b.up.z > 0.985
    and math.abs(a.tanV - b.tanV) < 0.001 and math.abs(a.tanH - b.tanH) < 0.001
end

local function densityNow()
  local tod = core_environment.getTimeOfDay()
  return paths.density((((tod and tod.time or 0) + 0.5) % 1) * 24)
end

local function readyFade(route)
  return math.max(0, math.min(1, (clock - math.max(route.readyAt or clock, requestedAt)) / FADE_S))
end

-- Keep four recent draw choices, not their objects or geometry. A return turn need not wait for another full scan.
local function remember(view, density)
  for i = #views, 1, -1 do if sameView(views[i].view, view) then table.remove(views, i) end end
  local entries = {}
  for _, car in pairs(selected) do
    entries[#entries + 1] = { key = car.key, id = car.route.id, phase = car.phase, score = car.score }
  end
  table.sort(entries, better)
  table.insert(views, 1, { view = view, density = density, at = clock, entries = entries })
  if #views > 4 then table.remove(views) end
end

local function recall(view)
  local density = densityNow()
  for i, entry in ipairs(views) do
    if clock - entry.at <= 30 and math.abs(entry.density - density) <= 0.05 and sameView(entry.view, view) then
      local take, n = math.max(1, math.floor(MAX_CARS * density + 0.5)), 0
      selected = {}
      for _, car in ipairs(entry.entries) do
        local route = store.routes[car.id]
        if route and n < take and paths.rank(car.key .. ':density') < density then
          selected[car.key] = { key = car.key, route = route, phase = car.phase, score = car.score }
          if actors[car.key] then actors[car.key].fade = math.max(actors[car.key].fade, readyFade(route)) end
          n = n + 1
        end
      end
      table.remove(views, i)
      table.insert(views, 1, entry)
      stats.cacheHits = (stats.cacheHits or 0) + 1
      return true
    end
  end
  return false
end

-- Selection is spread across frames, including geotile placement after a rebase. The old selection keeps moving
-- meanwhile. Candidate order is stable; the actor and plane caps include those fading out of an old selection.
local function selection(anchor, view, box, density)
  local cam = view.cam
  local ids, best, demand = {}, { {}, {}, {}, {} }, { 0, 0, 0 }
  local limit = math.max(1, math.floor(MAX_CARS * density + 0.5))
  for id in pairs(store.routes) do ids[#ids + 1] = id end
  table.sort(ids)
  for _, id in ipairs(ids) do
    local route = store.routes[id]
    if route then
      local slots = paths.slots(route)
      for i = 1, slots do
        local key = id .. ':' .. i
        if paths.rank(key .. ':density') < density then
          local phase = paths.phase(route, i, slots)
          local p = sample(route, phase, anchor, temp)
          local a, d = alpha(p, cam, box, route.reach)
          local edge = paths.viewEdge(p.x, p.y, p.z, view)
          if a > 0.01 and edge <= 1.15 then
            local band = d < 2500 and 1 or d < 6000 and 2 or 3
            if edge > 1 then band = 4 else demand[band] = demand[band] + 1 end
            local cap, score = band == 4 and 8 or limit, paths.rank(key .. ':budget')
            local h = best[band]
            if #h < cap or score <= h[1].score then
              offer(h, { key = key, route = route, phase = phase, score = score }, cap)
            end
          end
        end
        if i % 16 == 0 then coroutine.yield() end
      end
    end
    coroutine.yield()
  end
  selected = {}
  local quotas, n = paths.bandQuotas(demand, limit), 0
  for band = 1, 4 do
    table.sort(best[band], better)
    local take = band == 4 and math.min(8, limit - n) or quotas[band]
    for i = 1, take do
      local car = best[band][i]
      if car then selected[car.key], n = car, n + 1 end
    end
  end
  stats.demand, stats.quotas = demand, quotas
  remember(view, density)
end

local function viewOf(cam, forward)
  local io = ui_imgui and ui_imgui.GetIO()
  local size = io and io.DisplaySize
  local aspect = size and size.y > 0 and size.x / size.y or 16 / 9
  local tanV = math.tan(math.rad(core_camera.getFovDeg()) * 0.5)
  local function copy(v) return { x = v.x, y = v.y, z = v.z } end
  return { cam = copy(cam), forward = copy(forward), right = copy(core_camera.getRight()), up = copy(core_camera.getUp()),
    tanV = tanV, tanH = tanV * aspect }
end

local function pair(create)
  for _, p in ipairs(pool) do if not p.busy then p.busy = true; return p, false end end
  if not create or #pool >= MAX_CARS then return nil end
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

local function place(item, p, q, r, red, a, along)
  local o = scenetree.findObjectById(item.id)
  if not o then return end
  a = math.floor(math.min(1, a) * 255 + 0.5)
  local hidden = a < 1
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

function M.update(dtReal, dtSim, state, box, gov)
  if not capable then return end
  local started = os.clockhp()
  dtReal, dtSim = math.max(0, dtReal or 0), math.max(0, dtSim or 0)
  elapsed, checkIn, selectIn, clock = elapsed + dtSim, checkIn - dtReal, selectIn - dtReal, clock + dtReal
  if checkIn <= 0 then
    checkIn = 0.5
    local want = enabled and state.active and box ~= nil and state.anchor ~= nil
      and require('beamngEarth/roads').enabled and require('beamngEarth/nightlights').isNight()
      and (withoutTraffic or gameplay_traffic and gameplay_traffic.getState() == 'on')
    want = want == true
    if want ~= requested then
      net.send({ type = 'traffic_visuals', enabled = want, reset = want and fresh or nil })
      if want then fresh, requestedAt = false, clock end
      requested, dirty = want, true
    end
  end
  if not state.anchor or not box then return end
  local cam, f = core_camera.getPosition(), core_camera.getForward()
  if scanAnchor ~= state.anchor then
    scan, scanAnchor, dirty, views = nil, state.anchor, true, {}
    for _, car in pairs(actors) do car.nextAt = 0 end   -- every plane moves with the origin, including the far LOD
  end
  if scanView then
    local p, v = scanView.cam, scanView.forward
    if (cam.x - p.x) ^ 2 + (cam.y - p.y) ^ 2 + (cam.z - p.z) ^ 2 > 2500
        or f.x * v.x + f.y * v.y + f.z * v.z < 0.985 then
      dirty, scan = true, nil
      local view = viewOf(cam, f)
      if requested and recall(view) then scanView = view end
    end
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
    if not scan and (dirty or selectIn <= 0) then
      local density = densityNow()
      scanView = viewOf(cam, f)
      local view = scanView
      scan = coroutine.create(function() selection(state.anchor, view, box, density) end)
      dirty, selectIn = false, 2
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
        -- A camera turn changes the draw budget, not the age of the stream. Only new data or enabling it fades in.
        car.pair, car.fade = p, readyFade(car.route)
        actors[key], assigned = car, assigned + 1
        if created then made = made + 1 end
        if assigned >= 64 then break end
      end
    end
  end
  if requested and room and gov then gov:charge('trafficVisuals', (os.clockhp() - prepStarted) * 1000) end
  local q = quatFromDir(-f)
  local view
  stats.visible = 0
  local active = false
  for key, car in pairs(actors) do
    local want = requested and selected[key] and store.routes[car.route.id] == car.route
    car.fade = want and readyFade(car.route) or math.max(0, car.fade - dtReal / FADE_S)
    if clock >= (car.nextAt or 0) or car.fade <= 0 then
      local p = sample(car.route, car.phase, state.anchor, temp)
      if requested and not selected[key] then
        view = view or viewOf(cam, f)
        -- Retiring an off-screen assignment must not hold the pool for two seconds while the new view waits.
        if paths.viewEdge(p.x, p.y, p.z, view) > 1.15 then car.fade = 0 end
      end
      local a, d = alpha(p, cam, box, car.route.reach)
      a = a * paths.smooth(car.fade)
      local head, tail = paths.facing(p.dx, p.dy, cam.x - p.x, cam.y - p.y)
      car.visible = a * math.max(head, tail) > 0.01
      local r = paths.glowRadius(d)
      place(car.pair[1], p, q, r, false, a * head, 1.5)
      place(car.pair[2], p, q, r, true, a * tail, -1.5)
      car.nextAt = clock + (d < 2500 and 1 / 45 or d < 6000 and 1 / 24 or 1 / 12)
    end
    if car.visible then stats.visible = stats.visible + 1 end
    if car.fade <= 0 and not want then
      car.pair.busy, actors[key] = false, nil
    else active = true end
  end
  idle = active and 0 or idle + dtReal
  if idle > 30 and #pool > 0 then
    -- Retire a few idle pairs per frame; deleting the whole city pool at once makes disabling it hitch.
    for _ = 1, math.min(8, #pool) do deletePair(table.remove(pool)) end
    if #pool == 0 then clearObjects() end
  end
  local ms = (os.clockhp() - started) * 1000
  stats.frames, stats.totalMs, stats.maxMs = stats.frames + 1, stats.totalMs + ms, math.max(stats.maxMs, ms)
end

function M.status()
  local n = 0
  for _ in pairs(actors) do n = n + 1 end
  return { enabled = enabled, withoutTraffic = withoutTraffic, capable = capable, requested = requested,
    tiles = store.tileCount, routes = store.count,
    points = store.points, pendingPoints = store.pendingPoints, cars = n, planes = #pool * 2, visible = stats.visible,
    demand = stats.demand, quotas = stats.quotas, cachedViews = #views, cacheHits = stats.cacheHits or 0,
    deferredTiles = deferredCount,
    meanMs = stats.totalMs / math.max(1, stats.frames), maxMs = stats.maxMs, frames = stats.frames }
end

function M.hasWork()
  if not capable or not requested then return false end
  local retryDue = retry and clock >= retryIn or not retry and retryDirty and deferredCount > 0
  if dirty or scan or selectIn <= 0 or store:hasWork() or retryDue or resendDue() then return true end
  for key in pairs(selected) do if not actors[key] then return true end end
  return false
end

return M
