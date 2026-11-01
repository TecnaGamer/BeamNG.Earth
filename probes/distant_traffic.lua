-- Temporary renderer/road probe for #565, loaded by probes/distant_traffic.py. Never loaded by the installed mod.
local M = {}
local traffic = dofile('/temp/beamngEarthTrafficProbe/distanttraffic.lua')
local objects, ids, cars, paths = {}, {}, {}, {}
local mode, elapsed, anchor, graph, count = nil, 0, nil, nil, 0
local stats = { frames = 0, totalMs = 0, maxMs = 0, errors = 0 }
local name = 'bneTrafficProbeObjects'
local scratch = {}

local function clear()
  for _, id in ipairs(ids) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
  local group = scenetree.findObject(name)
  if group then group:delete() end
  for _, materialName in ipairs({ 'bne_traffic_probe_glow', 'bne_traffic_probe_occluder' }) do
    local material = scenetree.findObject(materialName)
    if material then material:delete() end
  end
  objects, ids, cars, paths, mode, anchor, graph = {}, {}, {}, {}, nil, nil, nil
  stats.eligibleCars = 0
end
M.stop = clear
M.onExtensionUnloaded = clear
M.onClientEndMission = clear

local function glow(shape)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, '/temp/beamngEarthTrafficProbe/' .. (shape or 'traffic') .. '.cdae')
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o:setField('castShadows', 0, 'false')
  o.useInstanceRenderData, o.canSave = true, false
  o:setField('instanceColor', 0, '1 1 1 0')
  o:registerObject('')
  scenetree.findObject(name):addObject(o)
  objects[#objects + 1] = o
  ids[#ids + 1] = o:getID()
  return o
end

local function place(o, x, y, z, r, q, red, alpha)
  o:setHidden(alpha < 0.002)
  if alpha < 0.002 then return end
  o:setScale(vec3(r, r, r))
  o:setPosRot(x, y, z, q.x, q.y, q.z, q.w)
  o:setField('instanceColor', 0, string.format('1 %.3f %.3f %.3f', red and 0.025 or 0.9, red and 0.012 or 0.75, alpha))
  o:updateInstanceRenderData()
end

-- This deliberately uses the current short-range graph only for the renderer probe. Production far paths need
-- their own visual data; extending the stock AI graph or reconstructing roads from lamp positions is not tested here.
local function graphPaths(g)
  local arcs, outgoing = {}, {}
  local function arc(a, b, e)
    if not (g.nodes[a] and g.nodes[b]) then return end
    local k = a .. '>' .. b
    arcs[k] = { key = k, a = a, b = b, speed = e[6], width = e[8], both = e[5] > 0 }
    outgoing[a] = outgoing[a] or {}
    outgoing[a][#outgoing[a] + 1] = k
  end
  for _, e in ipairs(g.edges) do
    arc(e[1], e[2], e)
    if e[3] ~= 1 and e[5] > 0 then arc(e[2], e[1], e) end
  end
  local keys = {}
  for k in pairs(arcs) do keys[#keys + 1] = k end
  table.sort(keys)
  local seen, result = {}, {}
  for _, key in ipairs(keys) do
    if not seen[key] then
      local e, pts, slow = arcs[key], {}, math.huge
      local first = e
      pts[1] = g.nodes[e.a]
      while e and not seen[e.key] and #pts < 512 do
        seen[e.key] = true
        pts[#pts + 1] = g.nodes[e.b]
        slow = math.min(slow, e.speed or 12)
        local nextEdge, choices = nil, 0
        for _, k in ipairs(outgoing[e.b] or {}) do
          local n = arcs[k]
          if n.b ~= e.a and not seen[k] then nextEdge, choices = n, choices + 1 end
        end
        e = choices == 1 and nextEdge or nil
      end
      local side = g.rules and g.rules.rightHandDrive and -1 or 1
      local offset = first.both and side * math.min(1.7, (first.width or 3) * 0.45) or 0
      local p = traffic.prepare(key, pts, math.min(slow, 24), offset)
      if p and p.length >= 100 then result[#result + 1] = p end
    end
  end
  return result
end

function M.start(opt)
  clear()
  opt = opt or {}
  elapsed, count = 0, math.max(1, math.min(64, math.floor(opt.count or 32)))
  stats = { frames = 0, totalMs = 0, maxMs = 0, errors = 0 }
  local group = createObject('SimGroup')
  group.canSave = false
  group:registerObject(name)
  scenetree.MissionGroup:addObject(group)
  loadJsonMaterialsFile('/temp/beamngEarthTrafficProbe/traffic.materials.json')
  local material = scenetree.findObject('bne_traffic_probe_glow')
  material:setField('emissiveIntensityNits', 0, '320')
  material:flush()
  material:reload()
  mode = opt.mode or 'calibration'
  if mode == 'calibration' or mode == 'occlusion' then
    local cam, f = core_camera.getPosition(), core_camera.getForward()
    local right = f:cross(vec3(0, 0, 1)):normalized()
    local q = quatFromDir(-f)
    for row = 0, 1 do
      for i, alpha in ipairs({ 1, 0.5, 0.1, 0 }) do
        local p = cam + f * 20 + right * ((i - 2.5) * 1.6) + vec3(0, 0, row * 1.3)
        place(glow(), p.x, p.y, p.z, 0.4, q, row == 1, alpha)
      end
    end
    if mode == 'occlusion' then
      local p = cam + f * 10 - right * 1.2 + vec3(0, 0, 0.325)
      place(glow('occluder'), p.x, p.y, p.z, 0.75, q, false, 1)
    end
  else
    local c = beamngEarth_core._state()
    graph, anchor = require('beamngEarth/navgraph').currentGraph(), c.anchorId
    if not graph then clear(); return false end
    paths = graphPaths(graph)
    local cam, forward = core_camera.getPosition(), core_camera.getForward()
    local function score(p)
      local v = p.points[math.ceil(#p.points / 2)]
      local dx, dy = v[1] - cam.x, v[2] - cam.y
      return math.abs(math.sqrt(dx * dx + dy * dy) - 800) + (dx * forward.x + dy * forward.y < 0 and 10000 or 0)
    end
    table.sort(paths, function(a, b)
      local da, db = score(a), score(b)
      if math.abs(da - db) < 1e-6 then return a.id < b.id end
      return da < db
    end)
    for _, p in ipairs(paths) do
      local n = math.max(1, math.floor(p.length / 150))
      for i = 1, n do
        if #cars >= count then break end
        local phase = traffic.rank(p.id .. ':' .. i) * p.length
        cars[#cars + 1] = { path = p, distance = phase, head = glow(), tail = glow() }
      end
      if #cars >= count then break end
    end
  end
  return true
end

local function tick(dtReal, dtSim)
  elapsed = elapsed + math.max(0, dtReal or 0)
  if elapsed > 600 then clear(); return end
  if mode ~= 'roads' then return end
  local state = beamngEarth_core._state()
  if not state.active or state.anchorId ~= anchor or require('beamngEarth/navgraph').currentGraph() ~= graph then
    clear(); return
  end
  local cam, f = core_camera.getPosition(), core_camera.getForward()
  local q = quatFromDir(-f)
  local live = gameplay_traffic and gameplay_traffic.getState() == 'on'
  local night = require('beamngEarth/nightlights').isNight()
  stats.eligibleCars = 0
  for _, car in ipairs(cars) do
    car.distance = car.distance + math.max(0, dtSim or 0) * car.path.speed
    local p = traffic.sample(car.path, car.distance, scratch)
    local vx, vy = cam.x - p.x, cam.y - p.y
    local distance = math.sqrt(vx * vx + vy * vy + (cam.z - p.z) ^ 2)
    local a = live and night and p.alpha * traffic.distanceFade(distance, 250, 200, 2400, 500) or 0
    local head, tail = traffic.facing(p.dx, p.dy, vx, vy)
    if a * head > 0.01 or a * tail > 0.01 then stats.eligibleCars = stats.eligibleCars + 1 end
    local r = math.max(0.35, math.min(6, distance * 0.004))
    place(car.head, p.x + p.dx * 1.5, p.y + p.dy * 1.5, p.z, r, q, false, a * head)
    place(car.tail, p.x - p.dx * 1.5, p.y - p.dy * 1.5, p.z, r, q, true, a * tail)
  end
end

function M.onUpdate(dtReal, dtSim)
  if not mode then return end
  local t = os.clockhp()
  local ok, err = pcall(tick, dtReal, dtSim)
  if not ok then stats.errors = stats.errors + 1; clear(); log('E', 'beamngEarth', 'traffic probe: ' .. tostring(err)) end
  local ms = (os.clockhp() - t) * 1000
  stats.frames, stats.totalMs, stats.maxMs = stats.frames + 1, stats.totalMs + ms, math.max(stats.maxMs, ms)
end

function M.status()
  return { mode = mode, cars = #cars, objects = #objects, paths = #paths, seconds = elapsed, frames = stats.frames,
    meanMs = stats.totalMs / math.max(1, stats.frames), maxMs = stats.maxMs,
    eligibleCars = stats.eligibleCars, errors = stats.errors }
end

function M.inspect()
  local cam, f = core_camera.getPosition(), core_camera.getForward()
  local out = {}
  for i, car in ipairs(cars) do
    local p = traffic.sample(car.path, car.distance)
    local delta = vec3(p.x - cam.x, p.y - cam.y, p.z - cam.z)
    local distance = delta:length()
    local dir = delta / distance
    out[#out + 1] = { car = i, x = p.x, y = p.y, z = p.z, distance = distance,
      view = dir:dot(f), hit = castRayStatic(cam, dir, distance),
      ground = castRayStatic(vec3(p.x, p.y, p.z + 3), vec3(0, 0, -1), 10) }
  end
  return out
end

return M
