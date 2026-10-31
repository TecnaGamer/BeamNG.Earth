-- luajit mod/tests/test_dashboard.lua (the cars' navigation screens: our roads and satellite tiles into the stock page)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local geodesy = require('beamngEarth/geodesy')

-- a small JSON encoder (the game's jsonEncode): arrays for 1..n tables, objects otherwise
local function enc(v)
  local t = type(v)
  if t == 'number' then return tostring(v) end
  if t == 'boolean' then return tostring(v) end
  if t == 'string' then return '"' .. v:gsub('[%c"\\]', function(c) return string.format('\\u%04x', c:byte()) end) .. '"' end
  if t ~= 'table' then return 'null' end
  if #v > 0 or next(v) == nil then
    local out = {}
    for i = 1, #v do out[i] = enc(v[i]) end
    return '[' .. table.concat(out, ',') .. ']'
  end
  local out = {}
  for k, x in pairs(v) do out[#out + 1] = enc(tostring(k)) .. ':' .. enc(x) end
  return '{' .. table.concat(out, ',') .. '}'
end
jsonEncode = enc
log = function() end

local dashboard = require('beamngEarth/dashboard')

-- the stock colours
T.check(dashboard.screenDrivability(1.0) == 1 and dashboard.screenDrivability(0.8) == 1,
  'main and town roads draw white on the stock map')
T.check(dashboard.screenDrivability(0.4) == 0.5 and dashboard.screenDrivability(0.5) == 0.5,
  'service roads and living streets draw khaki')
T.check(dashboard.screenDrivability(0.1) == 0, 'tracks draw brown (0: the renderer rounds 0.05 up to khaki)')

-- the stock nodes
local g = {
  nodes = { a = { 10.04, -2.0, 5, 3 }, b = { 20.0, 7.55, 5, 3 }, c = { 0, 0, 0 }, bad = { 0 / 0, 1, 0, 1 } },
  edges = { { 'a', 'b', 0, 1, 1, 11, 0.8, 3.0 }, { 'b', 'a', 0, 1, 1, 11, 0.8, 3.0 }, { 'b', 'missing', 0, 1, 1, 11, 1, 2 },
            { 'c', 'c', 0, 1, 1, 5, 1, 1 }, { 'b', 'c', 1, 1, 0, 5, 0.4 }, { 'a', 'bad', 0, 1, 1, 5, 1 } },
}
local text, box, roads = dashboard.stockNodes(g)
text = text:gsub('(%d)%.0([,%]}])', '%1%2')       -- (Lua 5.3 writes 10.0 where LuaJIT writes 10: both JSON numbers)
T.check(roads == 2, 'each road once (a reverse duplicate, a missing node, a loop and a NaN node are left out)')
T.check(text == '{"n1":{"pos":[10,-2],"radius":3,"links":{"n2":{"drivability":1}}},'
  .. '"n2":{"pos":[20,7.6],"radius":3,"links":{"n3":{"drivability":0.5}}},'
  .. '"n3":{"pos":[0,0],"radius":2,"links":{}}}',
  'nodes have string ids, positions to 0.1 m, a radius always, links from one end to present nodes only')
T.check(box and box[1] == 0 and box[2] == -2 and box[3] == 20 and box[4] == 7.55, 'the nodes\' box')
local co = coroutine.create(function() return dashboard.stockNodes(g, 1) end)
local resumes, ok, t2 = 0, true, nil
repeat ok, t2 = coroutine.resume(co); resumes = resumes + 1 until coroutine.status(co) == 'dead'
T.check(ok and t2:gsub('(%d)%.0([,%]}])', '%1%2') == text and resumes > 2, 'the conversion pauses between roads and gives the same text')
T.check(dashboard.stockNodes({ nodes = {}, edges = {} }) == '{}', 'no roads: an empty object')

-- the frame: the server's window with the nodes
local f = dashboard.frameTile(100, -50, { -2000, -10, 0, 30 })
T.check(f.offset[1] == -2000 and f.offset[2] == 1050 and f.size[1] == 3200 and f.size[2] == 2200,
  'the frame is the window around (cx, cy) widened to the nodes, offset = west / north')
T.check(dashboard.frameTile(nil, nil, { 0, 0, 10, 5 }).size[1] == 10, 'no centre (an older server): the nodes\' box')
T.check(dashboard.frameTile(nil, nil, nil) == nil, 'nothing: no frame')

-- satellite: tiles on the anchor's frame, snapped to whole canvas pixels
local anchor = geodesy.newAnchor(42.7, -87.8)
local area = dashboard.frameTile(0, 0, nil)
local L = dashboard.satLayout(anchor, area, 0, 0)
T.check(L and L.z == 16 and #L.tiles > 0 and #L.tiles <= dashboard.SAT_MAX_TILES, 'z16 tiles at 42 degrees, at most 121')
T.check(#L.requests == #L.tiles and L.requests[1][4] == 'sat', 'one request a tile')
local function onLattice(v) return math.abs(v / L.p - math.floor(v / L.p + 0.5)) < 1e-6 end
local lattice, covered = true, { math.huge, math.huge, -math.huge, -math.huge }
for _, t in ipairs(L.tiles) do
  local o, s = t.entry.offset, t.entry.size
  lattice = lattice and onLattice(o[1]) and onLattice(o[2]) and onLattice(o[1] + s[1]) and onLattice(o[2] - s[2])
  covered = { math.min(covered[1], o[1]), math.min(covered[2], o[2] - s[2]), math.max(covered[3], o[1] + s[1]),
    math.max(covered[4], o[2]) }
end
T.check(lattice, 'every tile edge is on the canvas pixel lattice (no seams)')
T.near(L.frame.size[1], dashboard.RES_PX * L.p, 1e-6, 'the frame is exactly RES_PX lattice steps (scale 1/p)')
T.check(covered[1] <= area.offset[1] and covered[4] >= area.offset[2] and covered[3] >= area.offset[1] + area.size[1]
  and covered[2] <= area.offset[2] - area.size[2], 'the tiles cover the whole map area')
local fr = L.frame
T.check(fr.offset[1] <= covered[1] and fr.offset[2] >= covered[4] and fr.offset[1] + fr.size[1] >= covered[3]
  and fr.offset[2] - fr.size[2] <= covered[2], 'the frame holds every tile')
local function d2(t) return ((t.r.w + t.r.e) / 2) ^ 2 + ((t.r.n + t.r.s) / 2) ^ 2 end
T.check(d2(L.tiles[1]) < d2(L.tiles[#L.tiles]), 'the tiles nearest the car are asked first')
-- neighbours share their edge (before the snap): no gap, no overlap
local byXY = {}
for _, t in ipairs(L.tiles) do byXY[t.x .. ',' .. t.y] = t end
local shared = true
for _, t in ipairs(L.tiles) do
  local right = byXY[(t.x + 1) .. ',' .. t.y]
  if right and math.abs(right.r.w - t.r.e) > 1e-6 then shared = false end
end
local below = true
for _, t in ipairs(L.tiles) do
  local under = byXY[t.x .. ',' .. (t.y + 1)]
  if under and (math.abs(under.r.n - t.r.s) > 1e-6 or math.abs(under.r.w - t.r.w) > 1e-6) then below = false end
end
T.check(shared, 'side by side tiles meet exactly')
T.check(below, 'tiles above one another share their whole edge (no holes where four meet)')
-- far from the anchor at a high latitude the grid turns: still no holes
local far = dashboard.satLayout(geodesy.newAnchor(70, 20), dashboard.frameTile(15000, 0, nil), 15000, 0)
local closed = true
local fxy = {}
for _, t in ipairs(far.tiles) do fxy[t.x .. ',' .. t.y] = t end
for _, t in ipairs(far.tiles) do
  local r, d = fxy[(t.x + 1) .. ',' .. t.y], fxy[t.x .. ',' .. (t.y + 1)]
  if (r and (r.entry.offset[1] ~= t.entry.offset[1] + t.entry.size[1] or r.entry.offset[2] ~= t.entry.offset[2]))
    or (d and (d.entry.offset[2] ~= t.entry.offset[2] - t.entry.size[2] or d.entry.offset[1] ~= t.entry.offset[1])) then
    closed = false
  end
end
T.check(closed and #far.tiles > 0, '15 km from the anchor at 70 degrees the tiles still close up')
T.check(dashboard.satZoom(75, 2200) < 17 and dashboard.satZoom(0, 2200) >= 15, 'the zoom by latitude')
T.check(dashboard.satLayout(nil, area) == nil, 'no anchor: no satellite')
-- across the antimeridian: every request a real tile (x wrapped), the rectangles still side by side
local am = geodesy.newAnchor(0, 179.996)              -- (the map area reaches past 180)
local AL = dashboard.satLayout(am, dashboard.frameTile(0, 0, nil), 0, 0)
local wrappedOk, n2 = true, 2 ^ AL.z
for _, r in ipairs(AL.requests) do if r[2] < 0 or r[2] >= n2 then wrappedOk = false end end
local crossed = false
for _, t in ipairs(AL.tiles) do if t.tileX ~= t.x then crossed = true end end
T.check(wrappedOk and crossed, 'tiles past the antimeridian are asked for by their real x')

-- a rebase: the last graph moved into the new frame (the same roads, the new coordinates)
local a1, a2 = geodesy.newAnchor(42.7, -87.8), geodesy.newAnchor(42.7, -87.62)      -- ~14.7 km east
local moved = dashboard.movedGraph({ nodes = { a = { 14700, 0, 0, 3 } }, edges = {}, cx = 14700, cy = 0 }, a1, a2)
T.check(math.abs(moved.nodes.a[1]) < 60 and math.abs(moved.nodes.a[2]) < 60 and math.abs(moved.cx) < 60,
  'a road 14.7 km east of the old anchor is near the new one')
T.check(dashboard.movedGraph(g, nil, a2) == nil, 'no old anchor: nothing to move')


-- the whole path: the stock request, our update after it, the ack, the probe, another graph, a rebase, satellite
local queued = {}
local carX, carY = 500, -300
local function vehicle(id)
  return { id = id, queueJSUITexture = function(_, tag, js) queued[#queued + 1] = { id = id, tag = tag, js = js } end,
    getPosition = function() return { x = carX, y = carY, z = 0 } end }
end
-- the map updates queued (probes left out), and the newest one
local function updates()
  local out = {}
  for _, q in ipairs(queued) do if q.js:find('__beNav({"key":', 1, true) then out[#out + 1] = q end end
  return out
end
local function lastUpdate() local u = updates() return u[#u] end
local function probes()
  local c = 0
  for _, q in ipairs(queued) do if q.js:find('window.__beLast.key!==', 1, true) then c = c + 1 end end
  return c
end
local vehicles = { [1] = vehicle(1), [2] = vehicle(2) }
local player = 1
be = { getPlayerVehicleID = function() return player end }
scenetree = { findObjectById = function(id) return vehicles[id] end }
local stockCalls = {}
local loads = 0
extensions = setmetatable({}, { __index = function(t, k) loads = loads + 1 end })   -- (the game's: a miss loads it)
dashboard.onUpdate()
T.check(loads == 0, 'the stock ui_uinavi is never loaded by us (its per-frame work stays off in cars without it)')
-- loaded by a gauges cluster first: kept under the name it was asked for
extensions.ui_uiNavi = { requestVehicleDashboardMap = function(tag, initmap, vehId)
  stockCalls[#stockCalls + 1] = { tag, initmap, vehId } end }
local ctx = { active = true, connected = true, anchorId = 1, anchor = anchor, player = { lat = 42.7, lon = -87.8 } }
extensions.beamngEarth_core = { dashboardContext = function() return ctx end }
local graph = { anchor = 1, serial = 1, cx = 0, cy = 0, nodes = g.nodes, edges = g.edges }
package.loaded['beamngEarth/navgraph'] = { graph = function() return graph end }
local clock = 0
local realClock = os.clock
os.clock = function() return clock end
local function frames(n, dt) for _ = 1, n do clock = clock + (dt or 0.3); dashboard.onUpdate() end end

dashboard.onExtensionLoaded()
local request = function(...) return extensions.ui_uiNavi.requestVehicleDashboardMap(...) end
request('@screen', nil, 1)              -- the player's car's BeamNavigator
request('@gauges', 'initMap', 1)        -- a gauges screen's own map
request('@screen', nil, 2)              -- another car
T.check(#stockCalls == 3, 'the stock request still runs for every screen (the hook found as ui_uiNavi)')
frames(3)
T.check(#updates() == 1 and updates()[1].id == 1 and updates()[1].tag == '@screen',
  "only the player's BeamNavigator screen gets our map")
local js = updates()[1].js
T.check(js:find('window.__beNav=function(d){', 1, true) == 1 and js:find('__beNav({"key":"1:1/map"', 1, true),
  'the page function travels with the update, keyed by the graph')
T.check(js:find('"nodes":{"n1":', 1, true) and js:find('"terrainTiles":[{', 1, true), 'the roads and the frame')
T.check(not js:find('location', 1, true), 'the page is not replaced')
T.check(js:find('nav.setData=function(x){sd.call(nav,x);', 1, true) and js:find('window.__beLast=d;', 1, true),
  'after a stock setData the page draws our last update again')
T.check(js:find('__beSeq', 1, true) and js:find('clearTimeout(window.__beT);', 1, true),
  'only the newest update draws (a waiting older one gives up)')
T.check(js:find('"credit":""', 1, true), 'the roads map has no credit line')
frames(30)
T.check(#updates() == dashboard.ACK_TRIES, 'a page that never said it drew is sent it again, a few times')
frames(20)
T.check(#updates() == dashboard.ACK_TRIES and probes() == 0, '... and no more (and not probed)')
local n = #updates()
graph = { anchor = 1, serial = 2, cx = 0, cy = 0, nodes = g.nodes, edges = g.edges }
frames(2)
T.check(#updates() == n + 1 and lastUpdate().js:find('"key":"1:2/map"', 1, true), 'a new graph: sent at once')
dashboard.navDrawn(1, '@screen', '1:2/map')
local m = #updates()
frames(20)
T.check(#updates() == m, 'a page that drew it is not sent it again')
T.check(probes() >= 1, 'a drawn page is asked now and then whether it still has the update')
-- the page lost it (a reload we did not see): sent again, and at most LOST_TRIES times for the same update
dashboard.navLost(1, '@screen', '1:2/map')
frames(1)
T.check(#updates() == m + 1, 'a page that lost the update is sent it again')
for _ = 1, dashboard.LOST_TRIES do
  dashboard.navDrawn(1, '@screen', '1:2/map')
  dashboard.navLost(1, '@screen', '1:2/map')
  frames(1)
end
dashboard.navDrawn(1, '@screen', '1:2/map')
local afterLost = #updates()
frames(40)
T.check(#updates() == afterLost and afterLost <= m + 1 + dashboard.LOST_TRIES, 'a page that keeps losing it is left be')
-- the car reloaded (Ctrl+R): its screen asks again: sent again
request('@screen', nil, 1)
frames(1)
T.check(#updates() == afterLost + 1, 'a screen that asks for its map again gets ours again')
dashboard.navDrawn(1, '@screen', '1:2/map')
-- a rebase: the new anchor's graph not here yet -> the last roads moved into the new frame
ctx.anchorId = 2
frames(3)
local last = lastUpdate().js
T.check(last:find('"key":"2:moved:1:2/map"', 1, true) and last:find('"nodes":{"n1":', 1, true),
  'a rebase: the roads stay (moved into the new frame) until the new graph comes')
-- a teleport: no roads near the car -> the grid around it
carX = 50000
ctx.anchorId = 3
frames(3)
last = lastUpdate().js
T.check(last:find('"key":"3:none"', 1, true) and last:find('"nodes":{}', 1, true),
  'a teleport: the old roads go, the grid around the car stays')
carX = 500
graph = { anchor = 3, serial = 1, cx = 0, cy = 0, nodes = g.nodes, edges = g.edges }
frames(2)
T.check(lastUpdate().js:find('"key":"3:1/map"', 1, true), 'its graph: drawn')
-- a big graph is converted over several frames (no hitch)
dashboard.NODES_YIELD = 1
local before = #updates()
graph = { anchor = 3, serial = 2, cx = 0, cy = 0, nodes = g.nodes, edges = g.edges }
frames(1)
T.check(#updates() == before, 'the frame a new graph comes in sends nothing yet')
frames(20)
T.check(lastUpdate().js:find('"key":"3:2/map"', 1, true), 'a few frames later it is sent')
dashboard.NODES_YIELD = 400
-- satellite: the frame and the ready tiles' files
local asked
local saved, creditsOn = 'satellite', true
extensions.beamngEarth_settings = {
  get = function(k)
    if k == 'dashboardMap' then return saved end
    if k == 'creditsVisible' then return creditsOn end
  end,
  mapTiles = function(list) asked = list; return { string.format('sat/%d/%d/%d', list[1][1], list[1][2], list[1][3]) } end,
  mapTilePath = function(layer, z, x, y) return string.format('/temp/beamngEarth/maptiles/%d/%d/%d.jpg', z, x, y) end,
  isMapOpen = function() return false end,
}
package.loaded['beamngEarth/credits'] = { serverText = function() return 'Imagery: Test source' end }
-- (no setStyle call: the saved setting is read by the screens themselves, as when this extension loaded before the
-- settings file or after settings applied it)
frames(2)
last = lastUpdate().js
T.check(dashboard.style() == 'satellite', 'a saved satellite choice is picked up without being told')
T.check(asked and #asked > 0 and last:find('"key":"3:2/sat1.1"', 1, true) and last:find('/temp/beamngEarth/maptiles/', 1, true),
  'satellite: the tiles are asked for and the ready ones drawn under the roads')
T.check(last:find('"credit":"Imagery: Test source"', 1, true), "satellite: with the imagery's credit line")
local firstAsk = #asked
frames(3)
T.check(#asked < firstAsk and #asked >= firstAsk - 3, 'then only the tiles not there yet are asked for')
-- every tile there: one more redraw, then no more asking
extensions.beamngEarth_settings.mapTiles = function(list)
  asked = list
  local out = {}
  for i, r in ipairs(list) do out[i] = string.format('sat/%d/%d/%d', r[1], r[2], r[3]) end
  return out
end
frames(8)
local allKey = lastUpdate().js:match('"key":"([^"]+)"')
asked = nil
frames(10)
T.check(asked == nil and allKey:find('/sat1.', 1, true), 'all tiles drawn: the asking stops')
saved = 'map'
frames(2)
T.check(lastUpdate().js:find('"key":"3:2/map"', 1, true), 'the setting back to the default: the roads only')
saved, creditsOn = 'satellite', false
frames(2)
last = lastUpdate().js
T.check(last:find('/sat2.1', 1, true), 'a new satellite layout has keys of its own (sent again)')
T.check(last:find('"credit":""', 1, true), 'the on-screen credits off: no credit line')
-- no road graph at all (the server's --traffic off), no earlier roads near the car: the satellite around it
graph = nil
carX = 50000
ctx.anchorId = 4
frames(3)
last = lastUpdate().js
T.check(last:find('"key":"4:car', 1, true) and last:find('"nodes":{}', 1, true) and last:find('maptiles', 1, true),
  'no road graph: the satellite around the car')
carX = 500
-- the car switched to another: its screen gets the map
player = 2
frames(2)
T.check(lastUpdate().id == 2, "the new car's screen gets the map")
-- a car whose screen asked before the hook was in (or before a reload): found from its jbeam controllers
vehicles[3] = vehicle(3)
core_vehicle_manager = { getVehicleData = function(id)
  return id == 3 and { vdata = { controller = { { fileName = 'beamNavigator', screenMaterialName = '@car3_gps' },
    { fileName = 'gauges/genericGauges' } } } } or nil end }
player = 3
dashboard.onVehicleSwitched(2, 3)
frames(2)
T.check(lastUpdate().id == 3 and lastUpdate().tag == '@car3_gps',
  "a screen found from the car's controllers gets the map")
-- out of our level: nothing more
ctx.active = false
local k = #queued
frames(10)
T.check(#queued == k, 'outside BeamNG.Earth the stock screens are left alone')
os.clock = realClock

T.done('test_dashboard')
