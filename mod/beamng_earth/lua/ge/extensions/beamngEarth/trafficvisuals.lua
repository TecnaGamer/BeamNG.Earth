-- Distant traffic (#565): a bounded pool of emissive planes following visual-road geotiles, never real vehicles.
local M = {}
local paths = require('beamngEarth/distanttraffic')
local geo = require('beamngEarth/geotile')
local net = require('beamngEarth/net')
M.PROTO, M.MAX_CARS = paths.PROTO, 2048
M.QUALITY_LIMITS = { low = 512, medium = 1024, high = M.MAX_CARS }
local MAX_CARS, FADE_S = M.MAX_CARS, 2
local quality, populationLimit = 'high', M.QUALITY_LIMITS.high
local SHAPE = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.cdae'
local MATERIALS = '/levels/beamng_earth/art/shapes/beamngEarth/traffic/traffic.materials.json'
local GROUP = 'bneDistantTraffic'
local store = paths.newRoutes()
local actors, pool, selected = {}, {}, {}
local enabled, withoutTraffic, capable, session, requested = false, false, false, nil, nil
local fresh = true
local elapsed, checkIn, selectIn, idle, clock = 0, 0, 0, 0, 0
local requestedAt = 0
local scan, scanAnchor, scanFocus, scanBox, scanDensity, dirty = nil, nil, nil, nil, nil, true
local deferred, deferredCount, deferredSerial = {}, 0, 0
local retry, retryAt, retryDirty, retryUse, retryTiles, retryIn, resendAt = nil, 1, false, 0, 0, 0, 0
local resend = false
local temp = {}
local hot, dueHeap, wake, lastWake = {}, {}, true, nil    -- (M.update's schedule: visit)
local WAKE_COS, WAKE_M = math.cos(math.rad(1.5)), 15     -- a turn / a move of the view that looks at every car
local visit, heapPop
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
  hot, dueHeap, wake, stats.visible = {}, {}, true, 0
  scanFocus, scanBox, scanDensity = nil, nil, nil
end

function M.reset()
  if capable and requested then net.send({ type = 'traffic_visuals', enabled = false }) end
  clearObjects()
  store, elapsed, checkIn, selectIn, idle = paths.newRoutes(), 0, 0, 0, 0
  capable, session, requested, scanAnchor, dirty = false, nil, nil, nil, true
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
function M.setQuality(v)
  local limit = M.QUALITY_LIMITS[v]
  if not limit then return false end
  if quality ~= v then quality, populationLimit, dirty = v, limit, true end
  return true
end
function M.onTeleport() clearObjects(); scanAnchor, dirty = nil, true end

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

local function sample(route, phase, anchor, out, at)
  paths.sample(route, phase + (at or elapsed) * route.speed, out)
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

local function densityNow()
  local tod = core_environment.getTimeOfDay()
  return paths.density((((tod and tod.time or 0) + 0.5) % 1) * 24)
end

local function readyFade(car)
  return math.max(0, math.min(1, (clock - math.max(car.route.readyAt or clock, requestedAt, car.admittedAt)) / FADE_S))
end

-- The population belongs to the streamed world around the player, not the view. Redistributing its budget into
-- the frustum made established roads gain cars when zooming and left new directions empty until a scan finished.
-- Selection is cooperative; the old population keeps moving and the caps include retiring assignments.
local function selection(anchor, focus, box, density)
  local ids, best, demand = {}, { {}, {}, {} }, { 0, 0, 0 }
  local limit = math.max(1, math.floor(populationLimit * density + 0.5))
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
          -- Rank a slot at its fixed phase, not its moving pose. Crossing a range band or an endpoint must not
          -- replace it with a new car elsewhere on an established road. The renderer still samples current motion.
          local p = sample(route, phase, anchor, temp, 0)
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

local function place(item, p, q, r, red, a, along)
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
  local want = requested and selected[key] and store.routes[car.route.id] == car.route
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
    local want = enabled and state.active and box ~= nil and state.anchor ~= nil
      and require('beamngEarth/roads').enabled and require('beamngEarth/nightlights').isNight()
      and (withoutTraffic or gameplay_traffic and gameplay_traffic.getState() == 'on')
    want = want == true
    if want ~= requested then
      net.send({ type = 'traffic_visuals', enabled = want, reset = want and fresh or nil })
      if want then fresh, requestedAt = false, clock end
      requested, dirty, wake = want, true, true
    end
  end
  if not state.anchor or not box then return end
  -- (off, by day or by choice, with no car shown or fading and no pair kept: nothing to look at this frame)
  if not requested and next(actors) == nil and #pool == 0 and not scan then return end
  local cam, f = core_camera.getPosition(), core_camera.getForward()
  local player = state.player
  local focus = player or { x = (box[1] + box[3]) / 2, y = (box[2] + box[4]) / 2, z = 0 }
  if scanAnchor ~= state.anchor then
    scan, scanAnchor, dirty = nil, state.anchor, true
    for _, car in pairs(actors) do car.nextAt = 0 end   -- every plane moves with the origin, including the far LOD
    wake = true
  end
  if scanFocus and ((focus.x - scanFocus.x) ^ 2 + (focus.y - scanFocus.y) ^ 2
      + (focus.z - scanFocus.z) ^ 2 > 2500 or box[1] ~= scanBox[1] or box[2] ~= scanBox[2]
      or box[3] ~= scanBox[3] or box[4] ~= scanBox[4]) then
    dirty, scan = true, nil
  end
  if selectIn <= 0 then
    selectIn = 2
    if not scanDensity or math.abs(densityNow() - scanDensity) >= 1 / MAX_CARS then dirty = true end
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
      scanDensity = density
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
  local active = next(actors) ~= nil
  idle = active and 0 or idle + dtReal
  if idle > 30 and #pool > 0 then
    -- Retire a few idle pairs per frame; deleting the whole city pool at once makes disabling it hitch.
    for _ = 1, math.min(8, #pool) do deletePair(table.remove(pool)) end
    if #pool == 0 then clearObjects() end
  elseif #pool > populationLimit then
    local removed = 0
    for i = #pool, 1, -1 do
      if not pool[i].busy then
        deletePair(pool[i]); table.remove(pool, i)
        removed = removed + 1
        if removed >= 8 or #pool <= populationLimit then break end
      end
    end
  end
  local ms = (os.clockhp() - started) * 1000
  stats.frames, stats.totalMs, stats.maxMs = stats.frames + 1, stats.totalMs + ms, math.max(stats.maxMs, ms)
end

function M.status()
  local n = 0
  for _ in pairs(actors) do n = n + 1 end
  return { enabled = enabled, withoutTraffic = withoutTraffic, capable = capable, requested = requested,
    quality = quality, populationLimit = populationLimit,
    populationScans = stats.populationScans or 0,
    tiles = store.tileCount, routes = store.count,
    points = store.points, pendingPoints = store.pendingPoints, cars = n, planes = #pool * 2, visible = stats.visible,
    demand = stats.demand, quotas = stats.quotas,
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
