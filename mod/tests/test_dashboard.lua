-- luajit mod/tests/test_dashboard.lua (vehicle satellite-map projection and tile neighborhood)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local dashboard = require('beamngEarth/dashboard')

local x, y = dashboard.project(0, 0, 3)
T.near(x, 4, 1e-12, 'equator and prime meridian project to the center tile')
T.near(y, 4, 1e-12, 'equator projects to the vertical world center')

local layout = dashboard.tileLayout(0.3, 0.3, 3)
T.check(layout and #layout.requests >= 9 and #layout.requests <= 16 and #layout.tiles == #layout.requests,
  'the tiles within the view radius of the car (' .. #layout.requests .. ')')
T.check(layout.requests[1][1] == 3 and layout.requests[1][2] == 4 and layout.requests[1][3] == 3
  and layout.requests[1][4] == 'sat', "the car's own tile is requested first")
-- every screen corner of a 512 x 256 screen with the car 62 % down is on a tile, wherever the car is in its tile
do
  local covered = true
  for _, f in ipairs({ { 0.01, 0.01 }, { 0.99, 0.01 }, { 0.5, 0.5 }, { 0.01, 0.99 }, { 0.99, 0.99 } }) do
    local z = 17
    local n = 2 ^ z
    local lon = (64000 + f[1]) / n * 360 - 180
    local yt = 40000 + f[2]
    local s = math.pi * (1 - 2 * yt / n)
    local lat = math.deg(math.atan(0.5 * (math.exp(s) - math.exp(-s))))
    local L = dashboard.tileLayout(lat, lon, z)
    local have = {}
    for _, t in ipairs(L.tiles) do have[t.x .. ',' .. t.y] = true end
    for a = 0, 359, 5 do      -- (the map turns with the car: the corners sweep a circle of ~300 px)
      local px = L.pixelX + 300 * math.cos(math.rad(a))
      local py = L.pixelY + 300 * math.sin(math.rad(a))
      if not have[math.floor(px / 256) .. ',' .. math.floor(py / 256)] then covered = false end
    end
  end
  T.check(covered, 'no bare screen corner: every point 300 px from the car is on a requested tile')
end
local seen = {}
for _, request in ipairs(layout.requests) do
  local key = request[2] .. '/' .. request[3]
  seen[key] = (seen[key] or 0) + 1
end
for key, count in pairs(seen) do T.check(count == 1, 'tile request is deduplicated: ' .. key) end

local edge = dashboard.tileLayout(0, 179.9, 3)
local wrapped = false
for _, request in ipairs(edge.requests) do
  if request[2] == 0 then wrapped = true end
end
T.check(wrapped, 'the neighborhood wraps satellite tile x across the antimeridian')

local pole = dashboard.tileLayout(90, 0, 3)
local inside = true
for _, r in ipairs(pole.requests) do if r[3] < 0 or r[3] >= 8 then inside = false end end
T.check(pole and #pole.requests > 0 and inside, 'north-pole view omits tile rows outside the Mercator world')

-- the roads over the photo
T.check(dashboard.roadClass(1.0) == 3 and dashboard.roadClass(0.9) == 3, 'main and tertiary roads: class 3')
T.check(dashboard.roadClass(0.8) == 2, 'town streets: class 2')
T.check(dashboard.roadClass(0.4) == 1 and dashboard.roadClass(0.5) == 1, 'service roads and living streets: class 1')
T.check(dashboard.roadClass(0.2) == 0, 'tracks and busways: class 0')
do
  local g = { anchor = 3, serial = 1,
    nodes = { a = { 100.04, -20.0, 5, 3 }, b = { 120.0, 7.55, 5, 3 }, c = { 101, -19, 0, 2 }, bad = { 0 / 0, 1, 0, 1 } },
    edges = { { 'a', 'b', 0, 1, 1, 11, 0.8, 3.0 }, { 'b', 'a', 0, 1, 1, 11, 0.8, 3.0 }, { 'b', 'missing', 0, 1, 1, 11, 1, 2 },
              { 'c', 'c', 0, 1, 1, 5, 1, 1 }, { 'b', 'c', 1, 1, 0, 5, 0.4 }, { 'a', 'bad', 0, 1, 1, 5, 1 } } }
  local text, roads = dashboard.roadsText(g, 'k1')
  T.check(roads == 2, 'each road once (a reverse duplicate, a missing node, a loop and a NaN node left out)')
  text = text:gsub('(%d)%.0([,%]}])', '%1%2')
  T.check(text == '{"key":"k1","anchor":3,"ox":100,"oy":-20,"n":[0,0,20,27.6,1,1],"e":[0,1,2,30,1,2,1,30]}',
    'nodes from the first node (0.1 m), roads as node, node, class, half width (dm): ' .. text)
  local co = coroutine.create(function() return dashboard.roadsText(g, 'k1', 1) end)
  local resumes, ok, t2 = 0, true, nil
  repeat ok, t2 = coroutine.resume(co); resumes = resumes + 1 until coroutine.status(co) == 'dead'
  T.check(ok and t2:gsub('(%d)%.0([,%]}])', '%1%2') == text and resumes > 2,
    'the conversion pauses between roads and gives the same text')
  local _, none = dashboard.roadsText({ nodes = {}, edges = {} }, 'e')
  T.check(none == 0, 'no roads: none')
end
T.near(dashboard.metresPerPixel(0, 17), 1.1943, 1e-3, 'metres a pixel at z17 on the equator')
T.near(dashboard.metresPerPixel(60, 17), 0.5972, 1e-3, '... half that at 60 degrees')
T.check(dashboard.zoomFor(0) == dashboard.ZOOM_CITY and dashboard.zoomFor(60) == dashboard.ZOOM_FAST,
  'the live speed selects an appropriate dashboard zoom')

-- the player's screen: our page loaded, then the roads once per graph (acknowledged by the page: PR #472's handshake),
-- again after its page reloaded, and the view carries their frame
local J = dofile(dir .. '/minijson.lua')
-- the page's echo: the Lua string the mod sent as an update's last argument (navDrawn), run as the page would
local function ackOf(code) local q = code:match(',("if beamngEarth_dashboard.-end")%)$') return q and J.decode(q) end
local function runLua(code) assert(loadstring(code))() end
do
  local js = {}
  local autoAck = true
  local veh = { queueJSUITexture = function(_, tag, code)
    js[#js + 1] = code
    if autoAck and (code:find('setRoads(', 1, true) or code:find('setTiles(', 1, true)) then
      local ack = ackOf(code)
      if ack then runLua(ack) end
    end
  end }
  local nav = { requestVehicleDashboardMap = function() return true end }
  local saved = { scenetree = rawget(_G, 'scenetree'), be = rawget(_G, 'be'), extensions = rawget(_G, 'extensions'),
    jsonEncode = rawget(_G, 'jsonEncode') }
  scenetree = { findObjectById = function(id) return id == 5 and veh or nil end }
  be = { getPlayerVehicleID = function() return 5 end }
  jsonEncode = J.encode
  beamngEarth_dashboard = dashboard
  local graph = { anchor = 2, serial = 1, nodes = { a = { 0, 0, 0, 3 }, b = { 50, 0, 0, 3 } },
    edges = { { 'a', 'b', 0, 1, 1, 11, 1, 3 } } }
  package.loaded['beamngEarth/navgraph'] = { graph = function() return graph end }
  local tilesAsked = 0
  local settingsStub = {
    mapTilePath = function(layer, z, x, y)
      return (layer == 'satp' and '/temp/beamngEarth/maptiles/prov/' or '/temp/beamngEarth/maptiles/') .. z .. '/' .. x .. '/' .. y .. '.jpg'
    end,
    mapTiles = function(reqs)
      tilesAsked = tilesAsked + 1
      local r1, r2 = reqs[1], reqs[2]
      -- the car's own tile there, the next one only as a stand-in (server maptiles.PROV_DIR)
      return { string.format('sat/%d/%d/%d', r1[1], r1[2], r1[3]), string.format('satp/%d/%d/%d', r2[1], r2[2], r2[3]) }
    end }
  extensions = { ui_uinavi = nav, beamngEarth_settings = settingsStub, beamngEarth_core = { dashboardContext = function()
    return { active = true, connected = true, mapPort = 1, anchorId = 2,
      player = { lat = 51.5, lon = -0.13, speed = 0, heading = 10, x = 20, y = 5, north = 0.01 } } end } }
  local clock, t = os.clock, 3000
  os.clock = function() return t end
  dashboard.onExtensionLoaded()
  dashboard.onClientStartMission()
  nav.requestVehicleDashboardMap('@vivace_gps_screen', nil, 5)
  local function count(pat)
    local c = 0
    for _, code in ipairs(js) do if code:find(pat, 1, true) then c = c + 1 end end
    return c
  end
  local function step(n, dt) for _ = 1, n do t = t + (dt or 1); dashboard.onUpdate() end end
  T.check(count('window.location.replace(') == 1, 'our page loaded on the screen')
  step(2)
  T.check(count('setRoads(') == 0 and count('setView(') == 0, 'nothing sent before the page said it loaded')
  dashboard.screenReady(5, '@vivace_gps_screen')
  step(5)
  T.check(count('setRoads({"key":') == 1, 'the roads go to the screen once (' .. count('setRoads(') .. ')')
  local tiles
  for _, code in ipairs(js) do if code:find('setTiles(', 1, true) then tiles = code end end
  T.check(count('setTiles(') == 1 and tiles:find('/maptiles/prov/', 1, true) and tiles:find('"key":"t1"', 1, true),
    'the tiles once, a stand-in from its own path (' .. count('setTiles(') .. ')')
  T.check(tiles and ackOf(tiles) and ackOf(tiles):find('navDrawn(5,"@vivace_gps_screen","t1")', 1, true),
    'the tiles carry the Lua the page runs back once it drew them')
  local view
  for _, code in ipairs(js) do if code:find('setView(', 1, true) then view = code end end
  T.check(view and view:find('"anchor":2', 1, true) and view:find('"x":20', 1, true) and view:find('"mpp":', 1, true),
    'the view carries the car in the world frame, its anchor and the metres a pixel')
  graph = { anchor = 2, serial = 2, nodes = graph.nodes, edges = graph.edges }
  step(3)
  T.check(count('setRoads({"key":') == 2, 'a new graph: sent again')
  dashboard.screenReady(5, '@vivace_gps_screen')
  step(3)
  T.check(count('setRoads({"key":') == 3, 'a page loaded again: the roads sent again')

  -- not acknowledged: sent again every ACK_S, ACK_TRIES times in all
  autoAck = false
  graph = { anchor = 2, serial = 3, nodes = graph.nodes, edges = graph.edges }
  local probes0 = count('navLost')
  step(30, 0.5)
  T.check(count('setRoads({"key":') == 3 + dashboard.ACK_TRIES,
    'an update the page never acknowledged: sent ' .. dashboard.ACK_TRIES .. ' times (' .. count('setRoads({"key":') - 3 .. ')')
  T.check(count('navLost') == probes0, 'no probe while an update is not acknowledged')
  autoAck = true
  local last
  for _, code in ipairs(js) do if code:find('setRoads(', 1, true) then last = code end end
  runLua(ackOf(last))                          -- (the page drew it after all)

  -- the probe: a page that lost its roads gets them again, a page that is not ours is loaded again
  local before = #js
  step(math.ceil(dashboard.PROBE_S) + 1)
  local probe
  for i = before + 1, #js do if js[i]:find('navLost', 1, true) then probe = js[i] end end
  T.check(probe ~= nil, 'a drawn screen is probed after PROBE_S')
  local lost = {}
  for q in probe:gmatch('S%(("if beamngEarth_dashboard.-end")%)') do lost[#lost + 1] = J.decode(q) end
  T.check(#lost == 2 and lost[1]:find('"page"', 1, true) and lost[2]:find('"data"', 1, true),
    'the probe carries the two answers the mod built')
  local sends = count('setRoads({"key":')
  runLua(lost[2])
  step(1)
  T.check(count('setRoads({"key":') == sends + 1, 'its data lost: the roads sent again')
  before = #js
  step(math.ceil(dashboard.PROBE_S) + 1)
  for i = before + 1, #js do if js[i]:find('navLost', 1, true) then probe = js[i] end end
  runLua(lost[2])
  step(1)
  T.check(count('setRoads({"key":') == sends + 1, 'an answer to an older probe is ignored')
  lost = {}
  for q in probe:gmatch('S%(("if beamngEarth_dashboard.-end")%)') do lost[#lost + 1] = J.decode(q) end
  local loads = count('window.location.replace(')
  runLua(lost[1])
  T.check(count('window.location.replace(') == loads + 1, 'not our page any more: loaded again')
  -- a page that never says it loaded is loaded again after APPLY_S, APPLY_TRIES times in all
  step(40)
  T.check(count('window.location.replace(') == loads + dashboard.APPLY_TRIES,
    'a page that never said it loaded: loaded ' .. dashboard.APPLY_TRIES .. ' times (' .. count('window.location.replace(') - loads .. ')')
  T.check(dashboard.navDrawn(5, '@nope', 'x') == false and dashboard.navLost(99, '@vivace_gps_screen', 'page', 1) == false,
    'answers for screens we do not know are ignored')
  os.clock = clock
  dashboard.onClientEndMission()
  package.loaded['beamngEarth/navgraph'] = nil
  beamngEarth_dashboard = nil
  scenetree, be, extensions, jsonEncode = saved.scenetree, saved.be, saved.extensions, saved.jsonEncode
end

-- a vehicle replaced in place keeps its id, not its screens: no more calls to the old screen afterwards
do
  local calls = 0
  local veh = { queueJSUITexture = function() calls = calls + 1 end }
  local nav = { requestVehicleDashboardMap = function() return true end }
  local saved = { scenetree = rawget(_G, 'scenetree'), be = rawget(_G, 'be'), extensions = rawget(_G, 'extensions'),
    jsonEncode = rawget(_G, 'jsonEncode') }
  scenetree = { findObjectById = function(id) return id == 7 and veh or nil end }
  be = { getPlayerVehicleID = function() return 7 end }
  jsonEncode = jsonEncode or function() return '{}' end
  extensions = { ui_uinavi = nav, beamngEarth_core = { dashboardContext = function()
    return { active = true, connected = true, mapPort = 1, player = { lat = 39.28, lon = -76.6, speed = 0 } } end } }
  dashboard.onExtensionLoaded()
  dashboard.onClientStartMission()
  nav.requestVehicleDashboardMap('@scintilla_gps_screen', {}, 7)   -- (the car's navigator registers its screen)
  T.check(calls > 0, 'the screen of the player\'s car is taken over')
  dashboard.onVehicleReplaced(7)
  local before, clock, t = calls, os.clock, 1000
  os.clock = function() return t end          -- (the view goes out every VIEW_INTERVAL_S)
  for _ = 1, 5 do t = t + 1; dashboard.onUpdate() end
  os.clock = clock
  T.check(calls == before, 'replaced in place: the old screen is not called again (' .. (calls - before) .. ' calls)')
  dashboard.onClientEndMission()
  scenetree, be, extensions, jsonEncode = saved.scenetree, saved.be, saved.extensions, saved.jsonEncode
end

-- a car whose navigator's one request at spawn never registered (the user, 2026-11-08: "the sat nav on cars with a
-- screen is blank") is asked once, after ASK_AFTER_S, to make it again; a car with its screen registered is not
do
  local queued, jsCalls = {}, 0
  local veh = { queueJSUITexture = function() jsCalls = jsCalls + 1 end,
    queueLuaCommand = function(_, code) queued[#queued + 1] = code end }
  local registered = {}
  local nav = { requestVehicleDashboardMap = function(tag, initmap, id) registered[#registered + 1] = { tag, initmap, id } end }
  local saved = { scenetree = rawget(_G, 'scenetree'), be = rawget(_G, 'be'), extensions = rawget(_G, 'extensions'),
    jsonEncode = rawget(_G, 'jsonEncode') }
  local pid = 9
  scenetree = { findObjectById = function(id) return (id == 9 or id == 10) and veh or nil end }
  be = { getPlayerVehicleID = function() return pid end }
  jsonEncode = jsonEncode or function() return '{}' end
  extensions = { ui_uinavi = nav, beamngEarth_core = { dashboardContext = function()
    return { active = true, connected = true, mapPort = 1, player = { lat = 39.28, lon = -76.6, speed = 0 } } end } }
  dashboard.onExtensionLoaded()
  dashboard.onClientStartMission()
  local clock, t = os.clock, 2000
  os.clock = function() return t end
  dashboard.onUpdate()
  T.check(#queued == 0, 'not asked at once (the car may still be setting up)')
  t = t + dashboard.ASK_AFTER_S + 0.1
  dashboard.onUpdate()
  T.check(#queued == 1, 'asked once after ASK_AFTER_S with no screen registered')
  t = t + 10; dashboard.onUpdate(); dashboard.onUpdate()
  T.check(#queued == 1, 'only once per car')
  -- the vehicle side: its navigators' requests made again through ui_uinavi (beamNavigator and gauges navigationData)
  local sent = {}
  local env = setmetatable({
    obj = { getID = function() return 9 end, queueGameEngineLua = function(_, c) sent[#sent + 1] = c end },
    v = { data = { controller = {
      { fileName = 'beamNavigator', screenMaterialName = '@vivace_gps_screen' },
      { fileName = 'gauges/genericGauges', materialName = '@dash_screen',
        customModules = { { moduleName = 'environmentData' }, { moduleName = 'navigationData' } } },
      { fileName = 'gauges/genericGauges', materialName = '@other', customModules = { { moduleName = 'environmentData' } } },
    } } } }, { __index = _G })
  local chunk = assert(loadstring(queued[1]))
  setfenv(chunk, env)()
  T.check(#sent == 2, 'one request per navigation screen (' .. #sent .. ')')
  for _, code in ipairs(sent) do assert(loadstring(code))() end
  T.check(registered[1] and registered[1][1] == '@vivace_gps_screen' and registered[1][2] == nil and registered[1][3] == 9,
    'beamNavigator screen asked again by its material')
  T.check(registered[2] and registered[2][1] == '@dash_screen' and registered[2][2] == 'initMap',
    'gauges navigation screen asked again with initMap')
  T.check(jsCalls > 0, 'the screens registered through the hook are taken over')
  -- a car that registered its screen at spawn is never asked
  pid = 10
  nav.requestVehicleDashboardMap('@scintilla_gps_screen', nil, 10)
  t = t + 1; dashboard.onUpdate(); t = t + 5; dashboard.onUpdate()
  T.check(#queued == 1, 'a car with its screen registered is not asked')
  os.clock = clock
  dashboard.onClientEndMission()
  scenetree, be, extensions, jsonEncode = saved.scenetree, saved.be, saved.extensions, saved.jsonEncode
end

-- a car whose jbeam lists a navigator is asked again until a screen registers (ASK_TRIES at most; PR #472: the first
-- request can miss the hook); a car with none listed is not asked
do
  local queued = {}
  local veh = { queueJSUITexture = function() end, queueLuaCommand = function(_, code) queued[#queued + 1] = code end }
  local nav = { requestVehicleDashboardMap = function() end }
  local saved = { scenetree = rawget(_G, 'scenetree'), be = rawget(_G, 'be'), extensions = rawget(_G, 'extensions'),
    jsonEncode = rawget(_G, 'jsonEncode'), mgr = rawget(_G, 'core_vehicle_manager') }
  local pid = 21
  local data = { [21] = { vdata = { controller = { { fileName = 'beamNavigator', screenMaterialName = '@s' } } } },
                 [22] = { vdata = { controller = { { fileName = 'gauges/genericGauges', customModules = {} } } } } }
  core_vehicle_manager = { getVehicleData = function(id) return data[id] end }
  scenetree = { findObjectById = function(id) return data[id] and veh or nil end }
  be = { getPlayerVehicleID = function() return pid end }
  jsonEncode = J.encode
  extensions = { ui_uinavi = nav, beamngEarth_core = { dashboardContext = function()
    return { active = true, connected = true, mapPort = 1, player = { lat = 39.28, lon = -76.6, speed = 0 } } end } }
  dashboard.onExtensionLoaded()
  dashboard.onClientStartMission()
  local clock, t = os.clock, 5000
  os.clock = function() return t end
  dashboard.onUpdate()
  for _ = 1, 40 do t = t + 1; dashboard.onUpdate() end
  T.check(#queued == dashboard.ASK_TRIES, 'a car with a navigator and no screen registered: asked ' .. dashboard.ASK_TRIES
    .. ' times (' .. #queued .. ')')
  queued = {}
  pid = 22
  dashboard.onUpdate()
  for _ = 1, 40 do t = t + 1; dashboard.onUpdate() end
  T.check(#queued == 0, 'a car with no navigator in its jbeam: not asked')
  -- registered after the first ask: not asked again
  dashboard.onVehicleSpawned(21)
  pid = 21
  dashboard.onUpdate()
  t = t + dashboard.ASK_AFTER_S + 0.1; dashboard.onUpdate()
  T.check(#queued == 1, 'asked once')
  dashboard.onExtensionLoaded()          -- (the hook)
  nav.requestVehicleDashboardMap('@s', nil, 21)
  for _ = 1, 40 do t = t + 1; dashboard.onUpdate() end
  T.check(#queued == 1, 'its screen registered: not asked again')
  os.clock = clock
  dashboard.onClientEndMission()
  core_vehicle_manager = saved.mgr
  scenetree, be, extensions, jsonEncode = saved.scenetree, saved.be, saved.extensions, saved.jsonEncode
end

T.done('test_dashboard')
