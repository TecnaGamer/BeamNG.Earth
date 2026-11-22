-- Local low-cost traffic on BeamNG.Earth's streamed navgraph.
-- Cars are render-only; a bounded physics pool takes over near the player.
local M = {}
M.MAX_CARS = 1000
M.SPAWN_RADIUS = 950
M.MIN_SPAWN_RADIUS = 155
M.VISUAL_RADIUS = 920

local CAR_LEN, GAP, TIME_GAP = 4.5, 2.5, 1.35
local ACCEL, BRAKE, STOP_GAP = 1.5, 2.5, 2.0
local MAIN_GAP, SIDE_FORCE, NODE_CLEAR = 4.0, 35, 9
local STEP, MAX_SPEED = 0.08, 42

local function rng(F, n)
  F.seed = (F.seed * 1103515245 + 12345) % 2147483648
  return (F.seed / 2147483648) * (n or 1)
end

local function randomPaint(F)
  local shares, pick = { 24, 20, 18, 14, 8, 7, 5, 4 }, rng(F, 100)
  for i, share in ipairs(shares) do
    pick = pick - share
    if pick < 0 then return i end
  end
  return 1
end

local function randomBody(F)
  local pick = rng(F, 100)
  if pick < 15 then return 'pickup' end
  if pick < 29 then return 'suv' end
  if pick < 36 then return 'van' end
  return 'sedan'
end

function M.new()
  return { nodes = {}, links = {}, linkByKey = {}, cars = {}, nextId = 1, seed = 82731,
    now = 0, accum = 0, fillAt = 0, target = 0, graphSerial = nil, anchor = nil,
    leftHand = false, priorityCache = nil,
    stats = { spawned = 0, dropped = 0, turns = 0, stopped = 0 } }
end

local function insert(list, car)
  local i = #list + 1
  while i > 1 and list[i - 1].s > car.s do list[i] = list[i - 1]; i = i - 1 end
  list[i] = car
end

local function erase(list, car)
  for i = #list, 1, -1 do if list[i] == car then table.remove(list, i); return end end
end

local function makeLink(F, from, to, a, b, lanes, width, speed, drive, roadId, oneWay, allLanes)
  if not a or not b or not lanes or lanes < 1 then return end
  local dx, dy, dz = b[1] - a[1], b[2] - a[2], b[3] - a[3]
  local len = math.sqrt(dx * dx + dy * dy + dz * dz)
  if len < 2 then return end
  local key = tostring(from) .. '>' .. tostring(to)
  if F.linkByKey[key] then return end
  local link = { key = key, from = tostring(from), to = tostring(to), a = a, b = b, len = len,
    ux = dx / len, uy = dy / len, uz = dz / len, lanes = lanes, half = width or 3.5,
    speed = math.max(5, math.min(MAX_SPEED, tonumber(speed) or 13)), drive = tonumber(drive) or .5,
    roadId = roadId, oneWay = oneWay, leftHand = F.leftHand, cars = {} }
  local total = math.max(1, allLanes or lanes)
  link.laneWidth = math.max(2.6, math.min(3.8, 2 * link.half / total))
  for i = 1, lanes do link.cars[i] = {} end
  F.linkByKey[key], F.links[#F.links + 1] = link, link
  local n1 = F.nodes[link.from] or { id = link.from, out = {}, inn = {}, neighbors = {}, queue = {} }
  local n2 = F.nodes[link.to] or { id = link.to, out = {}, inn = {}, neighbors = {}, queue = {} }
  F.nodes[link.from], F.nodes[link.to] = n1, n2
  n1.out[#n1.out + 1], n2.inn[#n2.inn + 1] = link, link
  n1.neighbors[link.to], n2.neighbors[link.from] = true, true
end

local nextLane, nextLink

-- Rebuild directed lanes from the same short, curved-road graph used by the stock AI.
-- Cars on unchanged links retain their position and lane across a streamed graph refresh.
function M.sync(F, graph, leftHand)
  if not graph or type(graph.nodes) ~= 'table' or type(graph.edges) ~= 'table' then return false end
  leftHand = leftHand == true
  if F.graphTable == graph and F.leftHand == leftHand then return false end
  F.leftHand = leftHand
  local oldCars, nodes, links, linkByKey = F.cars, {}, {}, {}
  F.nodes, F.links, F.linkByKey = nodes, links, linkByKey
  for _, e in ipairs(graph.edges) do
    local a, b = graph.nodes[e[1]], graph.nodes[e[2]]
    if a and b then
      local roadId = tostring(e[1]):match('^(.-)_[^_]+$') or tostring(e[1])
      local outL, inL = math.max(0, tonumber(e[4]) or 1), math.max(0, tonumber(e[5]) or 0)
      local oneWay = e[3] == 1 or e[3] == true
      makeLink(F, e[1], e[2], a, b, outL, e[8], e[6], e[7], roadId, oneWay, outL + inL)
      if not oneWay then makeLink(F, e[2], e[1], b, a, inL, e[8], e[6], e[7], roadId, false, outL + inL) end
    end
  end
  F.cars = {}
  for id, car in pairs(oldCars) do
    local link = F.linkByKey[car.linkKey]
    if link and car.lane <= link.lanes then
      car.link, car.s = link, math.min(car.s, link.len - .2)
      link.cars[car.lane][#link.cars[car.lane] + 1] = car
      F.cars[id] = car
    else
      F.stats.dropped = F.stats.dropped + 1
    end
  end
  for _, link in ipairs(F.links) do
    for _, list in ipairs(link.cars) do table.sort(list, function(a, b) return a.s < b.s end) end
  end
  for _, n in pairs(F.nodes) do
    n.junction = 0
    for _ in pairs(n.neighbors) do n.junction = n.junction + 1 end
    n.junction = n.junction >= 3
    local best, roadWeight, bestWeight, secondWeight = {}, {}, 0, 0
    for _, link in ipairs(n.inn) do
      local w = link.speed * link.drive * link.lanes
      roadWeight[link.roadId] = math.max(roadWeight[link.roadId] or 0, w)
    end
    for _, w in pairs(roadWeight) do
      if w > bestWeight then secondWeight, bestWeight = bestWeight, w
      elseif w > secondWeight then secondWeight = w end
    end
    if n.junction and bestWeight > math.max(0.1, secondWeight) * 1.18 then
      for roadId, w in pairs(roadWeight) do if w >= bestWeight * .88 then best[roadId] = true end end
    end
    n.mainRoads = next(best) and best or nil
    n.signalKind = nil
  end
  for _, sg in ipairs(graph.signals or {}) do
    local n = F.nodes[tostring(sg.j)]
    if n then
      n.signalKind = sg.kind
      n.signalApproaches = {}
      for i, app in ipairs(sg.app or {}) do
        n.signalApproaches[tostring(app[1])] = i
      end
    end
  end
  -- A streamed graph refresh replaces every link and node object, even when a retained car's
  -- current edge still exists. Rebind its planned edge and clear queue/holder state as well;
  -- otherwise the car can cross into a retired link and disappear from the live flow.
  for _, car in pairs(F.cars) do
    car.next = nextLink(F, car.link)
    car.nextLane = nextLane(car.link, car.lane, car.next)
    car.queued, car.granted, car.crossingNode = false, false, nil
    car.wait = 0
  end
  F.graphSerial, F.anchor, F.graphTable, F.leftHand = graph.serial, graph.anchor, graph, leftHand
  F.priorityCache = nil
  F.stats.links, F.stats.nodes = #F.links, (function() local c=0 for _ in pairs(F.nodes) do c=c+1 end return c end)()
  return true
end

local function laneOffset(link, lane)
  local w = link.laneWidth
  if link.oneWay then
    -- A one-way carriageway is centered as a whole; lane 1 is the keep-left/right lane.
    local side = (link.lanes / 2 - lane + .5) * w
    return link.leftHand and -side or side
  end
  -- Each direction keeps to its country's driving side; reversing the tangent mirrors the carriageway.
  local side = (lane - .5) * w
  return link.leftHand and -side or side
end

local function pose(link, s, lane)
  local t = math.max(0, math.min(1, s / link.len))
  local side = laneOffset(link, lane)
  local rx, ry = link.uy, -link.ux
  return { x = link.a[1] + (link.b[1] - link.a[1]) * t + rx * side,
    y = link.a[2] + (link.b[2] - link.a[2]) * t + ry * side,
    z = link.a[3] + (link.b[3] - link.a[3]) * t,
    dx = link.ux, dy = link.uy, dz = link.uz, yaw = math.atan2(link.uy, link.ux) }
end
M.pose = function(F, car)
  local p = pose(car.link, car.s, car.lane)
  p.edgeKey = car.link.from .. ':' .. car.link.to
  return p
end

-- Group both directions of the player's carriageway; divided highways often store each direction
-- as a separate road ID, while the low-cost flow should still use a single shared near-road quota.
function M.priorityRoadIds(F, x, y, road, headingX, headingY)
  if not road or not road.edgeKey or not road.dx or not road.dy then return nil end
  local cached = F.priorityCache
  local hx, hy = tonumber(headingX) or road.dx, tonumber(headingY) or road.dy
  local hlen = math.sqrt(hx * hx + hy * hy)
  if hlen < .01 then hx, hy = road.dx, road.dy else hx, hy = hx / hlen, hy / hlen end
  if cached and cached.edgeKey == road.edgeKey and cached.hx == hx and cached.hy == hy
      and (x-cached.x)^2 + (y-cached.y)^2 <= 100^2 then
    return cached.ids
  end
  local ids = { same = {}, opposite = {}, all = {}, hx = hx, hy = hy }
  local exact = F.linkByKey[tostring(road.edgeKey):gsub(':', '>')]
  if exact then
    ids.all[exact.roadId] = true
    if exact.ux * hx + exact.uy * hy >= 0 then ids.same[exact.roadId] = true
    else ids.opposite[exact.roadId] = true end
  end
  local corridor = math.max(18, math.min(28, (tonumber(road.halfWidth) or 4) * 2 + 8))
  local corridor2 = corridor * corridor
  for _, link in ipairs(F.links) do
    local alignment = link.ux * hx + link.uy * hy
    if math.abs(alignment) >= .94 then
      for lane = 1, link.lanes do
        local a, b = pose(link, 0, lane), pose(link, link.len, lane)
        local dx, dy = b.x - a.x, b.y - a.y
        local ll = dx*dx + dy*dy
        if ll > 1e-6 then
          local t = math.max(0, math.min(1, ((x-a.x)*dx + (y-a.y)*dy) / ll))
          if (a.x + t*dx - x)^2 + (a.y + t*dy - y)^2 <= corridor2 then
            ids.all[link.roadId] = true
            if alignment >= 0 then ids.same[link.roadId] = true
            else ids.opposite[link.roadId] = true end
            break
          end
        end
      end
    end
  end
  F.priorityCache = { edgeKey = road.edgeKey, x = x, y = y, hx = hx, hy = hy, ids = ids }
  return ids
end

-- Preserve the car's position across adjacent graph edges. Randomly changing lane index at every
-- short road segment made proxies jump sideways even when the road itself continued straight.
nextLane = function(link, lane, next)
  if not next then return 1 end
  local from = pose(link, link.len, lane)
  local best, bestD2 = 1, math.huge
  for candidate = 1, next.lanes do
    local to = pose(next, 0, candidate)
    local d2 = (from.x - to.x)^2 + (from.y - to.y)^2
    if d2 < bestD2 then best, bestD2 = candidate, d2 end
  end
  return best
end

nextLink = function(F, link)
  local n = F.nodes[link.to]
  if not n or #n.out == 0 then return nil end
  local opts, total, reverse = {}, 0, {}
  for _, o in ipairs(n.out) do
    local dot = link.ux * o.ux + link.uy * o.uy
    if dot > -.72 then
      local w = math.max(.05, o.speed * o.drive * o.lanes)
      opts[#opts + 1], total = { link = o, weight = w }, total + w
    else reverse[#reverse + 1] = o end
  end
  if #opts == 0 then
    for _, o in ipairs(reverse) do local w=math.max(.05,o.speed*o.drive*o.lanes); opts[#opts+1]={link=o,weight=w}; total=total+w end
  end
  if total <= 0 then return nil end
  local r = rng(F, total)
  for _, o in ipairs(opts) do r = r - o.weight; if r <= 0 then return o.link end end
  return opts[#opts].link
end

local function spawnOn(F, link, lane, s, speed, paint, body)
  if not link or lane < 1 or lane > link.lanes then return nil end
  local c = { id = F.nextId, link = link, linkKey = link.key, lane = lane,
    s = math.max(.1, math.min(link.len - .1, s)), v = math.max(0, speed or 0),
    want = .78 + rng(F, .2), paint = paint or randomPaint(F), body = body or randomBody(F), wait = 0 }
  F.nextId = F.nextId + 1
  c.next = nextLink(F, link)
  c.nextLane = nextLane(link, lane, c.next)
  F.cars[c.id] = c
  insert(link.cars[lane], c)
  F.stats.spawned = F.stats.spawned + 1
  return c
end

local function spawnSomewhere(F, fx, fy, priorityRoads, mode)
  -- Restrict samples to short lane segments intersecting the player's local radius. The former
  -- whole-graph lottery spent most attempts on distant roads and left the visible area underfilled.
  local choices, total = {}, 0
  for _, link in ipairs(F.links) do
    local preferred = priorityRoads and priorityRoads.all[link.roadId]
    local direction = priorityRoads and link.ux * priorityRoads.hx + link.uy * priorityRoads.hy or 1
    local allowed = not mode or mode == 'nearby'
      or (mode == 'same' and priorityRoads and priorityRoads.same[link.roadId] and direction >= .94)
      or (mode == 'opposite' and priorityRoads and priorityRoads.opposite[link.roadId] and direction <= -.94)
      or (mode == 'corridor' and priorityRoads and priorityRoads.all[link.roadId])
    if allowed then
      local multiplier = 1
      if mode == 'nearby' and preferred then multiplier = .18 end
      for lane = 1, link.lanes do
        local mid = pose(link, link.len * .5, lane)
        local reach = M.SPAWN_RADIUS + link.len * .5
        local d2 = (mid.x - fx)^2 + (mid.y - fy)^2
        if d2 <= reach * reach then
          local weight = link.len * math.max(.05, link.drive) * multiplier
          total = total + weight
          choices[#choices + 1] = { link = link, lane = lane, endWeight = total }
        end
      end
    end
  end
  if total <= 0 then return nil end
  for _ = 1, 20 do
    local r, lo, hi = rng(F, total), 1, #choices
    while lo < hi do
      local mid = math.floor((lo + hi) * .5)
      if r <= choices[mid].endWeight then hi = mid else lo = mid + 1 end
    end
    local c, chosen = choices[lo], choices[lo].link
    local s = rng(F, math.max(1, chosen.len - 8)) + 4
    local p = pose(chosen, s, c.lane)
    local d2 = (p.x - fx)^2 + (p.y - fy)^2
    if d2 >= M.MIN_SPAWN_RADIUS^2 and d2 <= M.SPAWN_RADIUS^2 then
      local clear = true
      for _, other in ipairs(chosen.cars[c.lane]) do if math.abs(other.s - s) < 18 then clear = false; break end end
      if clear then return spawnOn(F, chosen, c.lane, s, chosen.speed * (.55 + rng(F, .35))) end
    end
  end
end

local function spawnWithFallback(F, fx, fy, priorityRoads, mode)
  local car = spawnSomewhere(F, fx, fy, priorityRoads, mode)
  if car or mode == 'nearby' then return car end
  if mode == 'same' or mode == 'opposite' then
    car = spawnSomewhere(F, fx, fy, priorityRoads, 'corridor')
    if car then return car end
  end
  return spawnSomewhere(F, fx, fy, priorityRoads, 'nearby')
end

function M.setTarget(F, target)
  F.target = math.max(0, math.min(M.MAX_CARS, math.floor(tonumber(target) or 0)))
end

local function removeCar(F, car, count)
  if not car or not F.cars[car.id] then return end
  erase(car.link.cars[car.lane], car)
  F.cars[car.id] = nil
  car.dead = true
  if count then F.stats.dropped = F.stats.dropped + 1 end
end

local function priorityMode(F, focus, roads, primaryTarget, corridorTarget)
  if not roads then return 'nearby' end
  local same, opposite, corridor = 0, 0, 0
  for _, car in pairs(F.cars) do
    local id = car.link.roadId
    local p = pose(car.link, car.s, car.lane)
    if (p.x-focus.x)^2 + (p.y-focus.y)^2 <= 750 * 750 then
      if roads.all[id] then corridor = corridor + 1 end
      local direction = car.link.ux * roads.hx + car.link.uy * roads.hy
      if roads.same[id] and direction >= .94 then same = same + 1 end
      if roads.opposite[id] and direction <= -.94 then opposite = opposite + 1 end
    end
  end
  local oppositeTarget = math.max(0, (corridorTarget or 0) - (primaryTarget or 0))
  if same < (primaryTarget or 0) then return 'same' end
  if opposite < oppositeTarget and next(roads.opposite) then return 'opposite' end
  if corridor < (corridorTarget or 0) then return 'corridor' end
  return 'nearby'
end

local function reclaimForPriority(F, focus, roads, mode)
  if not roads or (mode ~= 'same' and mode ~= 'opposite' and mode ~= 'corridor') then return nil end
  local victim, farthest
  for _, car in pairs(F.cars) do
    local id, isPreferred = car.link.roadId, false
    if mode == 'same' then
      isPreferred = roads.same[id] and car.link.ux * roads.hx + car.link.uy * roads.hy >= .94
    elseif mode == 'opposite' then
      isPreferred = roads.opposite[id] and car.link.ux * roads.hx + car.link.uy * roads.hy <= -.94
    else
      isPreferred = roads.all[id]
    end
    if not isPreferred then
      local p = pose(car.link, car.s, car.lane)
      local d2 = (p.x-focus.x)^2 + (p.y-focus.y)^2
      if d2 > 350 * 350 and (not farthest or d2 > farthest) then victim, farthest = car, d2 end
    end
  end
  return victim
end

function M.take(F, car)
  removeCar(F, car, false)
end

local function signalAt(F, node, from, signalStates)
  if not node.signalKind then return nil end
  if node.signalKind == 'stop' then return 'stop' end
  local index = node.signalApproaches and node.signalApproaches[from]
  if not index then return 'red' end
  return signalStates and signalStates[node.id .. ':' .. from] or 'red'
end

local function canEnter(car)
  local link = car.next
  if not link then return false end
  local first = link.cars[car.nextLane] and link.cars[car.nextLane][1]
  return not first or first.s > CAR_LEN + GAP or (first.v > 1 and first.s > CAR_LEN + .5)
end

local function request(F, car, node)
  if car.queued then return end
  car.queued = true
  node.queue[#node.queue + 1] = car
end

local function serve(F, signalStates)
  for _, n in pairs(F.nodes) do
    for i = #n.queue, 1, -1 do
      local c = n.queue[i]
      if c.dead or c.link.to ~= n.id or c.granted then table.remove(n.queue, i) end
    end
    if #n.queue > 0 and n.mainRoads then
      local side
      for _, c in ipairs(n.queue) do
        if not (n.mainRoads and n.mainRoads[c.link.roadId]) and (not side or c.wait > side.wait) then side = c end
      end
      if side then
        local mainArrival = math.huge
        for _, incoming in ipairs(n.inn) do
          if n.mainRoads and n.mainRoads[incoming.roadId] then
            for _, lane in ipairs(incoming.cars) do
              local c = lane[#lane]
              if c then mainArrival = math.min(mainArrival, (incoming.len - c.s) / math.max(c.v, 1)) end
            end
          end
        end
        local holderClear = not n.holder or n.holder.dead or n.holder.link.to ~= n.id
        if holderClear and canEnter(side) and (mainArrival > MAIN_GAP or side.wait > SIDE_FORCE) then
          side.granted, n.holder = true, side
          for i = #n.queue, 1, -1 do if n.queue[i] == side then table.remove(n.queue, i); break end end
        end
      end
    elseif #n.queue > 0 and (n.signalKind == 'stop' or not n.signalKind) then
      -- All-way stop: first-come-first-served with a short departure headway.
      local c = n.queue[1]
      if F.now >= (n.busyUntil or -1)
          and (not n.holder or n.holder.dead or n.holder.link.to ~= n.id) and canEnter(c) then
        c.granted, n.holder = true, c
        n.busyUntil = F.now + 1.9
        table.remove(n.queue, 1)
      end
    end
  end
end

local function stopAt(F, car, signalStates)
  local n = F.nodes[car.link.to]
  if not n or not n.junction then return false end
  if not canEnter(car) then return true end
  if n.holder and n.holder ~= car and not n.holder.dead and n.holder.link.to == n.id then return true end
  local signal = signalAt(F, n, car.link.from, signalStates)
  if signal == 'red' or signal == 'yellow' then return true end
  if signal == 'stop' then return not car.granted end
  if n.mainRoads and not n.mainRoads[car.link.roadId] then return not car.granted end
  return false
end

local function obstacleAhead(car, p, obstacles, gap, leadV)
  local link = car.link
  for _, o in ipairs(obstacles or {}) do
    local dx, dy, dz = o.x - p.x, o.y - p.y, (o.z or p.z) - p.z
    local along = dx * link.ux + dy * link.uy
    local across = math.abs(dx * -link.uy + dy * link.ux)
    if along > 0 and along < 130 and across < 3.4 and math.abs(dz) < 4 then
      local g = along - CAR_LEN
      if not gap or g < gap then gap, leadV = math.max(.1, g), math.max(0, (o.vx or 0) * link.ux + (o.vy or 0) * link.uy) end
    end
  end
  return gap, leadV
end

local function advance(F, car, dt, signalStates, obstacles)
  local link, list = car.link, car.link.cars[car.lane]
  local aheadCar
  for i, c in ipairs(list) do if c == car then aheadCar = list[i + 1]; break end end
  local gap, leadV
  if aheadCar then gap, leadV = aheadCar.s - car.s - CAR_LEN, aheadCar.v end
  local p = pose(link, car.s, car.lane)
  gap, leadV = obstacleAhead(car, p, obstacles, gap, leadV)
  if car.next and stopAt(F, car, signalStates) then
    gap, leadV = math.min(gap or math.huge, link.len - car.s - STOP_GAP), 0
    local node = F.nodes[link.to]
    if node and node.signalKind ~= 'signal' then request(F, car, node) end
  end
  local v0 = link.speed * car.want
  if car.next and link.len - car.s < 28 then
    local d = link.ux * car.next.ux + link.uy * car.next.uy
    if d < .82 then v0 = math.min(v0, 8 + math.max(0, d) * 10) end
  end
  local free = 1 - (car.v / math.max(.1, v0))^4
  local a = ACCEL * free
  if gap then
    local star = GAP + math.max(0, car.v * TIME_GAP + car.v * (car.v - (leadV or 0)) /
      (2 * math.sqrt(ACCEL * BRAKE)))
    a = ACCEL * (free - (star / math.max(.1, gap))^2)
  end
  car.v = math.max(0, car.v + math.max(-9, math.min(ACCEL, a)) * dt)
  if gap then car.v = math.min(car.v, math.max(0, gap - .25) / math.max(dt, .016)) end
  car.s = car.s + car.v * dt
  car.wait = car.v < .5 and car.wait + dt or 0
end

local function cross(F, car)
  local safety = 0
  while car.s >= car.link.len and not car.dead and safety < 4 do
    safety = safety + 1
    local old, next = car.link, car.next
    if not next then removeCar(F, car, true); return end
    local first = next.cars[car.nextLane] and next.cars[car.nextLane][1]
    if first and first.s - (car.s - old.len) < CAR_LEN + GAP then car.s = old.len - .01; car.v = math.min(car.v, first.v); return end
    local node = F.nodes[old.to]
    erase(old.cars[car.lane], car)
    car.s, car.link, car.linkKey, car.lane, car.granted, car.queued = car.s - old.len, next, next.key, car.nextLane, false, false
    insert(next.cars[car.lane], car)
    if node and node.junction then
      if car.crossingNode and car.crossingNode ~= node.id then
        local prior = F.nodes[car.crossingNode]
        if prior and prior.holder == car then prior.holder = nil end
      end
      car.crossingNode = node.id
      node.holder = car
    end
    car.next = nextLink(F, next)
    car.nextLane = nextLane(next, car.lane, car.next)
    if node then
      for i = #node.queue, 1, -1 do if node.queue[i] == car then table.remove(node.queue, i) end end
    end
    F.stats.turns = F.stats.turns + 1
  end
end

function M.step(F, dt, focus, target, obstacles, signalStates, priorityRoads, primaryRoadTarget, corridorTarget)
  if not focus or #F.links == 0 then return end
  M.setTarget(F, target)
  F.accum = math.min(.4, F.accum + math.max(0, dt or 0))
  while F.accum >= STEP do
    F.accum, F.now = F.accum - STEP, F.now + STEP
    serve(F, signalStates)
    local ordered = {}
    for _, car in pairs(F.cars) do ordered[#ordered + 1] = car end
    for _, car in ipairs(ordered) do
      if F.cars[car.id] then
        if car.crossingNode and car.s > NODE_CLEAR then
          local n = F.nodes[car.crossingNode]
          if n and n.holder == car then n.holder = nil end
          car.crossingNode = nil
        end
        advance(F, car, STEP, signalStates, obstacles)
        cross(F, car)
      end
    end
    for _, link in ipairs(F.links) do
      for _, list in ipairs(link.cars) do table.sort(list, function(a,b) return a.s < b.s end) end
    end
    if F.now >= F.fillAt and next(F.cars) ~= nil then
      F.fillAt = F.now + .08
      local count = 0; for _ in pairs(F.cars) do count = count + 1 end
      local mode = priorityMode(F, focus, priorityRoads, primaryRoadTarget, corridorTarget)
      if count < F.target then spawnWithFallback(F, focus.x, focus.y, priorityRoads, mode)
      elseif mode ~= 'nearby' then
        local victim = reclaimForPriority(F, focus, priorityRoads, mode)
        if victim and spawnSomewhere(F, focus.x, focus.y, priorityRoads, mode) then
          removeCar(F, victim, false)
        end
      end
      if count > F.target then
        local worst, wd
        for _, c in pairs(F.cars) do local p=pose(c.link,c.s,c.lane); local d=(p.x-focus.x)^2+(p.y-focus.y)^2
          if not wd or d > wd then worst,wd=c,d end end
        if worst then removeCar(F, worst, false) end
      end
    elseif F.now >= F.fillAt then
      F.fillAt = F.now + .08
      local count = 0; for _ in pairs(F.cars) do count = count + 1 end
      local mode = priorityMode(F, focus, priorityRoads, primaryRoadTarget, corridorTarget)
      if count < F.target then spawnWithFallback(F, focus.x, focus.y, priorityRoads, mode)
      elseif mode ~= 'nearby' then
        local victim = reclaimForPriority(F, focus, priorityRoads, mode)
        if victim and spawnSomewhere(F, focus.x, focus.y, priorityRoads, mode) then
          removeCar(F, victim, false)
        end
      end
    end
  end
end

function M.near(F, x, y, radius)
  local out, r2 = {}, radius * radius
  for _, car in pairs(F.cars) do
    local p = pose(car.link, car.s, car.lane)
    local d2 = (p.x - x)^2 + (p.y - y)^2
    if d2 <= r2 then
      p.car, p.distance, p.speed, p.paint, p.body = car, math.sqrt(d2), car.v, car.paint, car.body
      p.vx, p.vy = p.dx * car.v, p.dy * car.v
      out[#out + 1] = p
    end
  end
  table.sort(out, function(a,b) return a.distance < b.distance end)
  return out
end

function M.put(F, x, y, z, hx, hy, v, paint, body, obstacles, ignoreId)
  local best, bestD2
  for _, link in ipairs(F.links) do
    local align = hx * link.ux + hy * link.uy
    if align > .45 then
      for lane = 1, link.lanes do
        local p0, p1 = pose(link, 0, lane), pose(link, link.len, lane)
        local dx, dy, dz = p1.x-p0.x, p1.y-p0.y, p1.z-p0.z
        local ll = dx*dx+dy*dy+dz*dz
        local t = ll > 0 and math.max(0,math.min(1,((x-p0.x)*dx+(y-p0.y)*dy+(z-p0.z)*dz)/ll)) or 0
        local ex,ey,ez=p0.x+dx*t-x,p0.y+dy*t-y,p0.z+dz*t-z
        local d2=ex*ex+ey*ey+ez*ez
        if d2 < 64 and (not bestD2 or d2 < bestD2) then best,bestD2={link=link,lane=lane,s=t*link.len},d2 end
      end
    end
  end
  if not best then return nil end
  for _, other in ipairs(best.link.cars[best.lane]) do
    if math.abs(other.s-best.s) < CAR_LEN+GAP then return nil end
  end
  local p = pose(best.link, best.s, best.lane)
  for _, o in ipairs(obstacles or {}) do
    if o.id ~= ignoreId then
      local dx,dy,dz=o.x-p.x,o.y-p.y,(o.z or p.z)-p.z
      local along=dx*best.link.ux+dy*best.link.uy
      local across=math.abs(dx*-best.link.uy+dy*best.link.ux)
      if math.abs(along)<CAR_LEN+GAP and across<3.3 and math.abs(dz)<4 then return nil end
    end
  end
  return spawnOn(F, best.link, best.lane, best.s, v, paint, body)
end

function M.status(F)
  local n=0; for _ in pairs(F.cars) do n=n+1 end
  return { cars=n, target=F.target, links=#F.links, nodes=F.stats.nodes or 0,
    turns=F.stats.turns, stopped=F.stats.stopped, dropped=F.stats.dropped }
end

function M.clear(F)
  F.nodes,F.links,F.linkByKey,F.cars={},{},{},{}
  F.graphSerial,F.anchor,F.graphTable=nil,nil,nil
  F.leftHand = false
end

return M
