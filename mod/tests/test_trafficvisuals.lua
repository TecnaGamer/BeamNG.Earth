-- luajit mod/tests/test_trafficvisuals.lua: bounded engine objects, lifecycle, traffic/clock gating and rebases.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local objects, names, serial, renderUpdates = {}, {}, 0, 0
local sent, night, traffic, hour = {}, true, 'on', 17
os.clockhp = os.clock
function vec3(x, y, z) return { x = x, y = y, z = z } end
function quatFromDir() return { x = 0, y = 0, z = 0, w = 1 } end
local forward = setmetatable({ x = 1, y = 0, z = 0 }, { __unm = function(v) return v end })
core_camera = { getPosition = function() return vec3(0, 0, 100) end, getForward = function() return forward end }
core_camera.getRight = function() return vec3(forward.y, -forward.x, 0) end
core_camera.getUp = function() return vec3(0, 0, 1) end
core_camera.getFovDeg = function() return 65 end
ui_imgui = { GetIO = function() return { DisplaySize = { x = 1920, y = 1080 } } end }
core_environment = { getTimeOfDay = function() return { time = (hour / 24 + 0.5) % 1 } end }
gameplay_traffic = { getState = function() return traffic end }
function loadJsonMaterialsFile() end
scenetree = { MissionGroup = { addObject = function() end }, findObject = function(n) return names[n] end,
  findObjectById = function(id) return objects[id] end }
function createObject(kind)
  serial = serial + 1
  local o = { id = serial, kind = kind }
  function o:setField(k, _, v) self[k] = v end
  function o:setHidden(v) self.hidden = v end
  function o:setScale(v) self.scale = v end
  function o:setPosRot(x, y, z) self.x, self.y, self.z = x, y, z end
  function o:updateInstanceRenderData() renderUpdates = renderUpdates + 1 end
  function o:addObject() end
  function o:registerObject(n) self.name = n; objects[self.id] = self; if n ~= '' then names[n] = self end end
  function o:getID() return self.id end
  function o:delete() objects[self.id] = nil; if self.name then names[self.name] = nil end end
  return o
end
package.loaded['beamngEarth/net'] = { send = function(m) sent[#sent + 1] = m end }
package.loaded['beamngEarth/roads'] = { enabled = true }
package.loaded['beamngEarth/nightlights'] = { isNight = function() return night end }
local geo = require('beamngEarth/geodesy')
local m = require('beamngEarth/trafficvisuals')
local state = { active = true, anchor = geo.newAnchor(40, -76) }
local box = { -500, -500, 500, 500 }
local function frames(n, gov, sim)
  for _ = 1, n do m.update(1 / 30, sim == false and 0 or 1 / 30, state, box, gov) end
end
local function feed(kind)
  local paths = {}
  for i = 1, 32 do paths[i] = { id = 'road' .. i, points = { { 800, i, 10 }, { 4500, i, 10 } }, speed = 20 } end
  m.onMessage({ type = kind or 'traffic_tile', key = 'a', data = { v = m.PROTO, key = 'a', frame = { 40, -76 }, pivot = 10, paths = paths } })
end
m.session('old', false)
feed(); frames(1)
T.check(#sent == 0 and m.status().planes == 0, 'an old server causes no requests or scene objects')
m.session('new', true)
feed()
local blocked = { try = function() return false end, charge = function() error('blocked frame was charged') end }
frames(1, blocked)
T.check(m.status().routes == 0 and m.status().planes == 0, 'no preparation or creates in a blocked frame')
frames(1, nil, false)
T.check(m.status().planes <= 4, 'cold pool creation remains at most two cars per frame')
frames(400, nil, false)
local s = m.status()
T.check(s.routes == 32 and s.cars == m.MAX_CARS and s.planes == m.MAX_CARS * 2, 'hard actor and plane caps')
T.check(s.visible > 0 and sent[#sent].enabled == true, 'night with traffic requests data and shows lights')
T.check(sent[#sent].reset == true, 'a fresh client cache asks for the complete stream')
feed('traffic_far_tile'); frames(3, nil, false)
T.check(m.status().tiles == 2 and m.status().routes == 32, 'near and far streams share one prepared route')
m.onMessage({ type = 'traffic_far_drop', key = 'a' }); frames(3, nil, false)
T.check(m.status().tiles == 1 and m.status().routes == 32, 'far drop preserves the near owner')
for _, o in pairs(objects) do
  if o.kind == 'TSStatic' and not o.hidden then
    T.check(o.x > box[3] + 100, 'no cosmetic car in the physics box')
    T.check(o.collisionType == 'None' and o.castShadows == 'false', 'no collision or real lighting')
  end
end
local before = {}
for id, o in pairs(objects) do if o.kind == 'TSStatic' and not o.hidden then before[id] = o.x end end
local beforeUpdates = renderUpdates
frames(10, nil, false)
T.check(renderUpdates == beforeUpdates, 'paused, unchanged lights do not re-upload their render data')
for id, x in pairs(before) do if objects[id] and not objects[id].hidden then T.near(objects[id].x, x, 1e-9, 'paused simulation does not move cars') end end
traffic = 'off'; frames(100)
T.check(m.status().visible == 0 and not sent[#sent].enabled, 'traffic off fades all cars and stops requests')
m.setWithoutTraffic(true); frames(100)
T.check(m.status().visible > 0 and sent[#sent].enabled, 'independent lights work without physical traffic')
T.check(m.status().planes <= m.MAX_CARS * 2 and m.status().cars <= m.MAX_CARS, 'independent lights retain the same object limits')
m.setEnabled(false); frames(100)
T.check(m.status().visible == 0 and not sent[#sent].enabled, 'the main switch still disables independent lights')
m.setEnabled(true); night = false; frames(100)
T.check(m.status().visible == 0 and not sent[#sent].enabled, 'independent lights still need nighttime')
night = true; package.loaded['beamngEarth/roads'].enabled = false; frames(100)
T.check(m.status().visible == 0 and not sent[#sent].enabled, 'independent lights still need roads')
package.loaded['beamngEarth/roads'].enabled = true; frames(100)
T.check(m.status().visible > 0 and sent[#sent].enabled, 'restoring roads resumes independent lights')
m.setWithoutTraffic(false); frames(100)
T.check(m.status().visible == 0 and not sent[#sent].enabled, 'turning off independence restores the physical-traffic gate')
traffic, night = 'on', false; frames(20)
T.check(not sent[#sent].enabled, 'daylight does not request data')
night = true; frames(180)
T.check(m.status().visible > 0, 'resume reuses geotiles kept while disabled')
T.check(sent[#sent].reset == nil, 'ordinary toggles do not request the retained stream again')
state.anchor = geo.newAnchor(40, -75.995)
frames(120)
T.check(m.status().visible > 0 and m.status().planes <= m.MAX_CARS * 2, 'rebase re-places geotiles without duplicate planes')
hour = 4; frames(220, nil, false)
T.check(m.status().cars < m.MAX_CARS / 4 and m.status().visible < m.MAX_CARS / 4, 'quiet-hour density lowers the global population cap')
m.onMessage({ type = 'traffic_drop', key = 'a' }); frames(100)
T.check(m.status().routes == 0 and m.status().visible == 0, 'last owner drop fades actors and frees paths')
m.setEnabled(false)
local allocated, waited = m.status().planes, 0
repeat frames(1); waited = waited + 1 until m.status().planes < allocated or waited > 1000
T.check(m.status().planes > 0 and m.status().planes < allocated, 'idle pool retirement is spread across frames')
frames(100)
T.check(m.status().planes == 0 and not scenetree.findObject('bneDistantTraffic'), 'idle retirement removes the whole pool and group')
m.setEnabled(true)
m.reset()
T.check(not next(objects) and m.status().planes == 0, 'reset removes every owned object')
state.anchor, hour = geo.newAnchor(40, -76), 17
m.session('view', true)
local viewPaths = {}
for i = 1, 24 do
  viewPaths[#viewPaths + 1] = { id = 'front' .. i, points = { { 2300, i * 3, 10 }, { 7300, i * 3, 10 } }, speed = 20 }
end
for i = 1, 8 do
  viewPaths[#viewPaths + 1] = { id = 'side' .. i, points = { { 0, 1800 + i * 3, 10 }, { 2000, 1800 + i * 3, 10 } }, speed = 20 }
end
m.onMessage({ type = 'traffic_tile', key = 'view', data = { v = m.PROTO, key = 'view', frame = { 40, -76 }, pivot = 10, paths = viewPaths } })
frames(300, nil, false)
local inView, far = 0, 0
local tv = math.tan(math.rad(65) / 2)
for _, o in pairs(objects) do
  if o.kind == 'TSStatic' and not o.hidden and o.x > 0 and math.abs(o.y) <= o.x * tv * 1920 / 1080
      and math.abs(o.z - 100) <= o.x * tv then
    inView = inView + 1
    if math.sqrt(o.x ^ 2 + o.y ^ 2 + (o.z - 100) ^ 2) > 6000 then far = far + 1 end
  end
end
T.check(inView >= m.MAX_CARS - 16, 'available in-view roads fill the visible budget, got ' .. inView)
T.check(far >= 32, 'nearby demand does not consume the distant traffic budget')
local farBefore = {}
for id, o in pairs(objects) do
  if o.kind == 'TSStatic' and not o.hidden and o.x > 6000 then farBefore[id] = o.x end
end
state.anchor = geo.newAnchor(40, -75.997)
frames(1, nil, false)
for id, x in pairs(farBefore) do
  if objects[id] and not objects[id].hidden then T.check(x - objects[id].x > 200, 'far planes follow a rebase in its first frame') end
end
m.reset()
state.anchor, hour = geo.newAnchor(40, -76), 17
m.session('turn', true)
m.onMessage({ type = 'traffic_tile', key = 'turn', data = { v = m.PROTO, key = 'turn', frame = { 40, -76 }, pivot = 10,
  paths = { { id = 'turn-road', points = { { 1200, 0, 10 }, { 2200, 0, 10 } }, speed = 20, spacing = 100 } } } })
frames(150, nil, false)
local function lit()
  local n = 0
  for _, o in pairs(objects) do
    if o.kind == 'TSStatic' and not o.hidden and tonumber(o.instanceColor:match('([^ ]+)$')) > 0.9 then
      local depth = o.x * forward.x + o.y * forward.y
      if depth > 0 and math.abs(o.x * forward.y - o.y * forward.x) <= depth * tv * 1920 / 1080
          and math.abs(o.z - 100) <= depth * tv then n = n + 1 end
    end
  end
  return n
end
local warm = lit()
local allocated = m.status().planes
T.check(warm >= 8, 'camera-turn fixture has an established bright stream')
forward.x = -1
frames(150, nil, false)
forward.x = 1
frames(1, nil, false)
local returned = lit()
T.check(returned >= warm * 0.9, 'returning to a cached road keeps its lights bright immediately: ' .. returned .. '/' .. warm)
T.check(m.status().planes == allocated and m.status().routes == 1, 'camera turns reuse the same pool and route data')
m.setEnabled(false); frames(100, nil, false)
m.setEnabled(true); frames(1, nil, false)
T.check(lit() == 0, 'enabling the feature still fades in')
frames(100, nil, false)
T.check(lit() >= warm * 0.9, 'enabling restores the established stream after its fade')
m.reset()

-- A slow full scan and a saturated pool: returning to a recently viewed road uses cached choices, without
-- waiting for a new scan or allocating more objects. The clock makes the cooperative work limit deterministic.
local realClock, workClock = os.clockhp, 0
os.clockhp = function() workClock = workClock + 0.00001; return workClock end
forward.x, forward.y = 1, 0
local turnPaths = {}
for i = 1, 64 do
  turnPaths[#turnPaths + 1] = { id = 'turn-front' .. i, points = { { 1200, i, 10 }, { 4800, i, 10 } }, speed = 20, spacing = 300 }
  turnPaths[#turnPaths + 1] = { id = 'turn-back' .. i, points = { { -1200, i, 10 }, { -4800, i, 10 } }, speed = 20, spacing = 300 }
end
m.session('turn-full', true)
m.onMessage({ type = 'traffic_tile', key = 'turn-full', data = { v = m.PROTO, key = 'turn-full', frame = { 40, -76 }, pivot = 10, paths = turnPaths } })
frames(400, nil, false)
T.check(lit() >= m.MAX_CARS - 16, 'camera-turn fixture fills the pool')
forward.x = -1; frames(150, nil, false)
forward.x = 1; frames(3, nil, false)
T.check(lit() >= m.MAX_CARS / 5, 'cached view returns bright cars before the slow full scan finishes')
frames(15, nil, false)
T.check(lit() >= m.MAX_CARS - 16 and m.status().planes == m.MAX_CARS * 2, 'return turn fills the same bounded pool')
for _, angle in ipairs({ -0.6, -0.3, 0, 0.3, 0.6, math.pi }) do
  forward.x, forward.y = math.cos(angle), math.sin(angle)
  frames(50, nil, false)
  T.check(m.status().cachedViews <= 4 and m.status().planes <= m.MAX_CARS * 2, 'sweeping the camera keeps both caches bounded')
end
m.onMessage({ type = 'traffic_drop', key = 'turn-full' })
forward.x, forward.y = 1, 0
frames(100, nil, false)
T.check(m.status().routes == 0 and m.status().cars == 0, 'cached choices cannot resurrect dropped routes')
m.reset()
T.check(m.status().cachedViews == 0 and not next(objects), 'reset clears view choices and owned objects')
os.clockhp = realClock
do
  local paths = require('beamngEarth/distanttraffic')
  local pointCap, tileCap = paths.MAX_ROUTE_POINTS, paths.MAX_TILES
  paths.MAX_ROUTE_POINTS = 6
  local points = { { 1200, 0, 10 }, { 1700, 0, 10 }, { 2200, 0, 10 } }
  local function data(key, ids)
    local routes = {}
    for _, id in ipairs(ids) do routes[#routes + 1] = { id = id, points = points, speed = 20 } end
    return { v = m.PROTO, key = key, frame = { 40, -76 }, pivot = 10, paths = routes }
  end
  local files, reads = {}, 0
  jsonReadFile = function(path) reads = reads + 1; return files[path] end
  m.session('pressure', true)
  m.onMessage({ type = 'traffic_tile', key = 'a', data = data('a', { 'a' }) })
  frames(150, nil, false)
  files.b = data('b', { 'b1', 'b2' })
  m.onMessage({ type = 'traffic_tile', key = 'b', path = 'b', data = files.b })
  T.check(m.status().deferredTiles == 1 and m.status().routes == 1, 'a full point cache retains a file descriptor, not partial geometry')
  frames(40, nil, false)
  T.check(reads == 0, 'no busy retry while capacity remains full')
  m.onMessage({ type = 'traffic_drop', key = 'a' })
  frames(150, nil, false)
  T.check(reads == 1 and m.status().routes == 2 and m.status().deferredTiles == 0,
    'freed capacity restores the entire deferred tile from disk without a server reconnect')
  T.check(m.status().points <= paths.MAX_ROUTE_POINTS, 'recovery respects the same point budget')
  paths.MAX_TILES = 2
  for _, key in ipairs({ 'c', 'd', 'e' }) do
    files[key] = data(key, { key })
    m.onMessage({ type = 'traffic_tile', key = key, path = key, data = files[key] })
  end
  T.check(m.status().deferredTiles == 2, 'deferred descriptors are bounded too')
  local sentBefore = #sent
  m.onMessage({ type = 'traffic_drop', key = 'b' })
  frames(100, nil, false)
  local resync = false
  for i = sentBefore + 1, #sent do if sent[i].reset and sent[i].enabled then resync = true end end
  T.check(resync, 'descriptor overflow requests current streams again when capacity recovers')
  m.reset()
  T.check(m.status().deferredTiles == 0, 'session reset releases deferred descriptors')
  paths.MAX_ROUTE_POINTS, paths.MAX_TILES = pointCap, tileCap
end
-- Repeat full pool creation/cleanup, as after leaving and re-entering the level.
for cycle = 1, 12 do
  m.session('churn' .. cycle, true)
  feed(); frames(400, nil, false)
  T.check(m.status().planes == m.MAX_CARS * 2, 'churn reaches the full pool')
  m.reset()
  local count = 0
  for _ in pairs(objects) do count = count + 1 end
  local clean = m.status()
  T.check(count == 0 and clean.planes == 0 and clean.routes == 0 and clean.pendingPoints == 0,
    'reset releases every engine object and route after repeated full-pool use')
end
T.done('test_trafficvisuals')
