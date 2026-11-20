-- luajit mod/tests/test_api.lua  (the modding API for other mods, api.lua: beamngEarth_api; docs/ADDONS.md)
-- version / has; ready only in our level with the world there (a teleport under way: not; a rebase: still ready);
-- the pose (lat / lon / alt / heading from true north / speed / east-north-up velocity) from core's player and anchor,
-- also off the origin where the frame's north turns; the place names; teleport / GPS through places.lua and gps.lua;
-- notify; the events as game hooks and listeners (worldReady after loading, teleported after a teleport only,
-- routeArrived from gps.lua), a failing listener contained; places / buildings (1.4): the request sent, each answer
-- to its own callback (a pending one kept for the next), paging, a timeout heard once; cachedOnly (1.8): the flag
-- sent and result.truncated surfaced; roadGraph: the request sent cached-only, one answer a request, every result
-- field defended, its own timeout beside the places one.
-- sent and result.truncated surfaced; a saved area along a route (1.9): the request as it goes out (no fast, no
-- mode), the arguments refused before anything is sent, the estimate and the new area's id decoded, areaStatus
-- over the app's area list (unavailable and msg surfaced, and a copy), and a timeout heard once.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')

local connected = true
local sent = {}
package.loaded['beamngEarth/net'] = { isConnected = function() return connected end,
  send = function(m) sent[#sent + 1] = m end }
local where = nil
package.loaded['beamngEarth/statusbar'] = { location = function() return where end }
local went = {}
package.loaded['beamngEarth/places'] = {
  goTo = function(lat, lon, name)
    if type(lat) ~= 'number' then return false, 'latitude must be -90..90 and longitude -180..180' end
    went[#went + 1] = { lat, lon, name }
    return true
  end }
local dest = nil
local routeData = { version = 7, points = { { 45.0, 7.0 }, { 45.01, 7.0 } }, dist = 1112,
  dur = 80, elev = { { 0, 250 }, { 1112, 260 } } }
package.loaded['beamngEarth/gps'] = {
  setDestination = function(lat, lon, name) dest = { lat = lat, lon = lon, name = name } return true end,
  setRoute = function(points) dest = { lat = points[#points].lat, lon = points[#points].lon, name = 'route' } return true end,
  clear = function() dest = nil end,
  status = function() return dest and { lat = dest.lat, lon = dest.lon, name = dest.name, remaining = 1200, eta = 90,
    off = 3.5, routeVersion = 7, legs = routeData.legs } end,
  route = function() return dest and routeData end,
}

local anchor = geodesy.newAnchor(45.0, 7.0)
local st = { inOurLevel = true, anchor = anchor, player = nil, pending = nil }
local ground = true
beamngEarth_core = {
  _state = function() return st end,
  groundReady = function() return ground end,
  serverHello = function() return { appVersion = '1.8.2-beta' } end,
}
local dirV = vec3(0, 1, 0)
local veh = { getDirectionVector = function() return dirV end }
function getPlayerVehicle() return veh end
local messages = {}
function ui_message(text, s) messages[#messages + 1] = { text, s } end
local hooks = {}
extensions = { hook = function(name, info) hooks[#hooks + 1] = { name, info } end }

local api = require('beamngEarth/api')

T.check(api.version() == 1 and select(2, api.version()) == 9,
  'version 1.9 (routeSteps, rebase from 1.7; cachedPlaces 1.8; roadGraph and areaRoute here)')
T.check(api.has('roadGraph') and api.has('areaRoute'), 'both of this change\'s calls are offered')
T.check(api.has('pose') and api.has('events') and not api.has('flying'), 'features')
T.check(api.earthVersion() == '1.8.2-beta', 'the app version from the server hello')

-- not ready before the player is there
T.check(not api.ready() and api.pose() == nil and api.position() == nil, 'no player yet: not ready, no pose')

local function setPlayer(x, y, z, vx, vy, vz, camera)
  local lat, lon = anchor:worldToGeodetic(x, y, z)
  st.player = { x = x, y = y, z = z, vx = vx, vy = vy, vz = vz, lat = lat, lon = lon, speed = math.sqrt(vx * vx + vy * vy + vz * vz),
    camera = camera }
end
setPlayer(0, 0, 250, 0, 20, 0)
T.check(api.ready(), 'ready: our level, the world there, connected')
local p = api.pose()
T.near(p.lat, 45.0, 1e-9, 'pose lat')
T.near(p.lon, 7.0, 1e-9, 'pose lon')
T.near(p.alt, 250, 1e-3, 'pose alt: over the ellipsoid')
T.near(p.heading, 0, 1e-6, 'heading north at the origin')
T.near(p.vn, 20, 1e-9, 'velocity north')
T.near(p.ve, 0, 1e-9, 'velocity east')
T.check(p.speed == 20 and p.camera == false, 'speed, a car')

-- off the origin the frame's north turns: heading and velocity are true north's
setPlayer(15000, 0, 0, 0, 10, 0)
dirV = vec3(0, 1, 0)
p = api.pose()
local nx, ny = anchor:northAt(15000, 0)
local turn = math.deg(math.atan2(nx, ny))
T.check(math.abs(turn) > 0.05, 'the frame north turns 15 km east at 45 deg (' .. turn .. ')')
T.near(p.heading, (-turn) % 360, 1e-6, 'heading measured from true north')
T.near(math.sqrt(p.ve ^ 2 + p.vn ^ 2), 10, 1e-9, 'velocity length kept')
T.near(math.deg(math.atan2(p.ve, p.vn)), -turn, 1e-6, 'velocity direction from true north')
dirV = vec3(1, 0, 0)
T.near(api.pose().heading, (90 - turn) % 360, 1e-6, 'facing frame east')

-- the free camera: no heading
setPlayer(0, 0, 0, 0, 0, 0, true)
T.check(api.pose().camera == true and api.pose().heading == nil, 'no car: camera, no heading')

-- not ready: other level, not connected, ground loading, teleport under way; a rebase is fine
setPlayer(0, 0, 0, 0, 0, 0)
st.inOurLevel = false
T.check(not api.ready() and not api.inWorld(), 'another level')
st.inOurLevel = true
connected = false
T.check(not api.ready() and not api.connected(), 'not connected')
connected = true
ground = false
T.check(not api.ready(), 'ground loading')
ground = true
st.pending = { purpose = 'rebase' }
T.check(api.ready(), 'a rebase: still ready')
st.pending = { purpose = 'teleport' }
T.check(not api.ready(), 'a teleport under way: not ready')
st.pending = nil

-- place names
T.check(api.place() == nil, 'no place known yet')
where = { street = 'Via Roma', place = 'Torino', region = '', country = 'Italy' }
local pl = api.place()
T.check(pl.street == 'Via Roma' and pl.place == 'Torino' and pl.region == nil and pl.country == 'Italy', 'place names, empty ones nil')

-- actions
T.check(api.teleport(46.5, 8.0, 'Grimsel') and went[1][1] == 46.5 and went[1][3] == 'Grimsel', 'teleport through places.goTo')
local ok, why = api.teleport('x', 8)
T.check(not ok and why, 'teleport refused with why')
T.check(api.gpsSet(46.5, 8.0, 'Depot') and api.gps().name == 'Depot' and api.gps().remaining == 1200, 'GPS set, read')
T.check(not api.gpsSet(91, 0), 'GPS: bad coordinates refused')
T.check(api.has('gpsRoute') and api.gpsSetRoute({ { lat = 46.6, lon = 8.1 }, { lat = 46.7, lon = 8.2 } })
  and api.gps().lat == 46.7 and #api.gps().waypoints == 0, 'GPS route through a waypoint (gps.setRoute)')
api.gpsClear()
T.check(api.gps() == nil, 'GPS cleared')
T.check(api.notify('Load delivered', 3) and messages[1][1] == 'Load delivered', 'notify')
T.check(not api.notify(''), 'notify: nothing to say')

-- events
api._reset()
hooks = {}
local got = {}
local h = api.on('teleported', function(info) got[#got + 1] = info end)
api.on('worldReady', function() error('a broken listener') end)
T.check(api.on('nonsense', print) == nil, 'unknown event refused')
st.player, st.pending = nil, nil
api.onUpdate()
setPlayer(0, 0, 0, 0, 0, 0)
api.onUpdate()
T.check(#hooks == 1 and hooks[1][1] == 'onBeamNGEarthWorldReady' and #got == 0, 'loaded: worldReady, not teleported')
api.onUpdate()
T.check(#hooks == 1, 'once')
st.pending = { purpose = 'teleport' }
api.onUpdate()
st.pending = nil
ground = false
api.onUpdate()
ground = true
api.onUpdate()
T.check(hooks[2][1] == 'onBeamNGEarthTeleported' and hooks[3][1] == 'onBeamNGEarthWorldReady', 'teleported, then world ready')
T.check(#got == 1 and math.abs(got[1].lat - 45) < 1e-9, 'the listener got where the player is')
st.pending = { purpose = 'rebase' }
api.onUpdate()
st.pending = nil
api.onUpdate()
T.check(#hooks == 3, 'a rebase is not an event')
api.off(h)
api._event('teleported', { lat = 1, lon = 2 })
T.check(#got == 1, 'off: no more calls')
api._event('routeArrived', { lat = 3, lon = 4, name = 'Depot' })
T.check(hooks[#hooks][1] == 'onBeamNGEarthRouteArrived' and hooks[#hooks][2].name == 'Depot', 'route arrived hook')
api._event('waypointReached', { lat = 3, lon = 4, name = 'Charger', left = 1 })
T.check(hooks[#hooks][1] == 'onBeamNGEarthWaypointReached' and hooks[#hooks][2].left == 1, 'waypoint reached hook')

-- 1.7: the world's origin moved (core.lua says so at a rebase's end and at a teleport's commit)
T.check(api.has('rebase') and select(2, api.version()) >= 7, 'rebase: has() and minor version 7')
local moved = {}
local hr = api.on('rebased', function(info) moved[#moved + 1] = info end)
T.check(hr ~= nil, 'rebased: a listener is taken')
api._event('rebased', { lat = 45.1, lon = 7.2, oldLat = 45.0, oldLon = 7.0, anchorId = 9, purpose = 'rebase' })
T.check(hooks[#hooks][1] == 'onBeamNGEarthRebased' and hooks[#hooks][2].anchorId == 9, 'rebased hook')
T.check(#moved == 1 and moved[1].oldLat == 45.0 and moved[1].lat == 45.1 and moved[1].purpose == 'rebase',
  'rebased: the listener got the old and the new origin')
api.off(hr)
T.check(api.rebasing() == false, 'rebasing: not between a rebase\'s frames')
st.rebasing = { purpose = 'rebase' }
T.check(api.rebasing() == true, 'rebasing: between a rebase\'s two frames')
st.rebasing = nil

T.check(api.has('options') and select(2, api.version()) >= 1, 'options: has() and minor version 1')
package.loaded['beamngEarth/addons'] = { optionValues = function(id) return id == 'x' and { port = 1 } or nil end }
T.check(api.addonOptions('x').port == 1 and api.addonOptions('y') == nil, 'addonOptions from the add-ons list')
api._event('addonOptions', { id = 'x', values = { port = 2 } })
T.check(hooks[#hooks][1] == 'onBeamNGEarthAddonOptions' and hooks[#hooks][2].values.port == 2, 'options hook')
package.loaded['beamngEarth/addons'] = nil

-- 1.3: under a roof (ambience.inTunnel's rule, the player 1 m over its position) and the weather now
T.check(api.has('inTunnel') and api.has('weather') and select(2, api.version()) >= 3, 'inTunnel / weather: has()')
local asked = nil
package.loaded['beamngEarth/ambience'] = { inTunnel = function(x, y, z) asked = { x, y, z } return x > 100 end }
st.player = { lat = 45.0, lon = 7.0, x = 150, y = 2, z = 10 }
T.check(api.inTunnel() == true and asked[3] == 11, 'inTunnel: the player, 1 m up')
T.check(api.inTunnel(5, 6, 7) == false and asked[1] == 5, 'inTunnel: any point')
st.player = nil
T.check(api.inTunnel() == nil, 'inTunnel: nil before the world')
package.loaded['beamngEarth/ambience'] = { inTunnel = function() error('boom') end }
T.check(api.inTunnel(1, 2, 3) == nil, 'inTunnel: an error is nil')
package.loaded['beamngEarth/ambience'] = nil
local report = nil
package.loaded['beamngEarth/weather'] = { rainNow = 0.4, snowNow = 0, current = function() return report end }
T.check(api.weather() == nil, 'weather: nil before the first report')
report = { code = 63, slTempC = 12, visM = 8000, rainMmH = 2.5, snowCmH = 0, wind = { ms = 6, gustMs = 10 } }
local w = api.weather()
T.check(w and w.rain == 0.4 and w.code == 63 and w.windMs == 6 and w.gustMs == 10 and w.visibilityM == 8000
  and w.rainMmH == 2.5 and w.tempC == 12, 'weather: the report and the rain now')
package.loaded['beamngEarth/weather'] = nil

-- 1.4: places and buildings round a point (the app's places_result, server places.py)
api._reset()
sent = {}
T.check(api.has('places') and api.has('buildings') and select(2, api.version()) >= 4, 'places / buildings: has()')
local answers = {}
local id = api.places(39.2847, -76.5925, { radius = 2000, categories = { 'gas_station', 'group:food', 7 }, limit = 20 },
  function(r) answers[#answers + 1] = r end)
local q = sent[#sent]
T.check(id and q.type == 'places' and q.id == id and q.radius == 2000 and q.limit == 20 and q.buildings == false,
  'places: the request sent')
T.check(#q.categories == 2 and q.categories[2] == 'group:food', 'places: only string categories sent')
api._onPlaces({ id = id, places = { { id = 'p1', name = 'Joe\'s', category = 'restaurant', lat = 39.28, lon = -76.59,
  confidence = 0.9, open = 'open' } }, complete = false, pending = true, next = '120.00|p1' })
T.check(#answers == 1 and answers[1].places[1].name == "Joe's" and answers[1].pending and not answers[1].complete
  and answers[1].next == '120.00|p1', 'places: the first answer, more to come')
api._onPlaces({ id = id + 100, places = {}, complete = true })
T.check(#answers == 1, 'places: another request\'s answer not ours')
api._onPlaces({ id = id, places = {}, complete = true, pending = false })
T.check(#answers == 2 and answers[2].complete and answers[2].next == nil, 'places: the last answer')
api._onPlaces({ id = id, places = {}, complete = true, pending = false })
T.check(#answers == 2, 'places: nothing after the last answer')
local page2 = api.places(39.2847, -76.5925, { cursor = '120.00|p1' }, function() end)
T.check(page2 and sent[#sent].cursor == '120.00|p1' and sent[#sent].categories == nil, 'places: the next page by cursor')
api.places(1, 2, { categories = {} }, function() end)
T.check(sent[#sent].categories == nil, 'places: no categories: none sent (every kind)')
local bl = nil
T.check(api.buildings(39.2847, -76.5925, function(r) bl = r end) and sent[#sent].buildings == true,
  'buildings: opts may be left out; buildings asked')
api._onPlaces({ id = sent[#sent].id, places = {}, complete = true, buildings = { { id = 'b1', lat = 39.28, lon = -76.59,
  height = 12, outline = { { 39.28, -76.59 }, { 39.281, -76.59 }, { 39.281, -76.591 } } } } })
T.check(bl and bl.buildings[1].height == 12 and #bl.buildings[1].outline == 3, 'buildings: footprints in the result')
-- 1.8: a wide search over only the cells the app has (cachedOnly), and the truncated it can answer with
T.check(api.has('cachedPlaces') and select(2, api.version()) >= 8, 'cachedPlaces: has()')
T.check(sent[#sent].cachedOnly == false, 'places: cachedOnly false unless asked')
local cut = nil
local wide = api.places(69.6492, 18.9553, { radius = 25000, cachedOnly = true, categories = { 'car_dealer' } },
  function(r) cut = r end)
T.check(wide and sent[#sent].cachedOnly == true and sent[#sent].radius == 25000,
  'places: cachedOnly sent with the reach')
api._onPlaces({ id = wide, places = {}, complete = false, pending = false, truncated = true })
T.check(cut and cut.truncated == true and cut.pending == false and cut.complete == false,
  'places: truncated surfaced, the answer final')
local plain = api.places(1, 2, { cachedOnly = true }, function(r) cut = r end)
api._onPlaces({ id = plain, places = {}, complete = true, pending = false })
T.check(cut.truncated == false and cut.complete, 'places: truncated false where the app does not say it')

local n0, why = api.places(91, 0, {}, print)
T.check(n0 == nil and why == 'bad coordinates', 'places: bad coordinates refused')
T.check(api.places(1, 2, {}) == nil, 'places: no callback refused')
connected = false
local n1, why1 = api.places(1, 2, {}, print)
T.check(n1 == nil and why1 and why1:find('not connected'), 'places: not connected')
connected = true
-- a request never answered is heard once, timed out; a failing callback contained
local late = {}
api.places(1, 2, {}, function(r) late[#late + 1] = r error('a broken callback') end)
st.player = nil
api.onUpdate(api.PLACES_TIMEOUT_S - 1)
T.check(#late == 0, 'places: not timed out yet')
api.onUpdate(2)
T.check(#late == 1 and late[1].error == 'timed out' and late[1].pending == false, 'places: timed out once')
api.onUpdate(api.PLACES_TIMEOUT_S * 2)
T.check(#late == 1, 'places: only once')

-- the posted speed limit (1.5, GitHub #1061): the link's posted field, else its speed; nil off the road
T.check(api.has('speedLimit'), 'has speedLimit')
veh.getPosition = function() return vec3(0, 0, 0) end
local roadDist = 1.0
local link = { speedLimit = 22.35, inRadius = 3.5 }
map = { getMap = function() return { nodes = { a = { links = { b = link } }, b = { links = { a = link } } } } end,
        findClosestRoad = function() return 'a', 'b', roadDist end }
package.loaded['beamngEarth/navgraph'] = { currentGraph = function() return { mph = true } end }
local sl = api.speedLimit()
T.check(sl and sl.ms == 22.35 and sl.mapped == nil and sl.unit == 'mph', 'no posted field: the edge speed')
link.speedLimit, link.posted = 15.0, 22.4      -- (a bend: the AI's target lower than the limit)
sl = api.speedLimit()
T.check(sl.ms == 22.4 and sl.mapped == true, 'a bend: the posted limit, not the target')
link.posted = -13.9
sl = api.speedLimit()
T.check(sl.ms == 13.9 and sl.mapped == false, 'the class default: not mapped')
package.loaded['beamngEarth/navgraph'] = { currentGraph = function() return { mph = false } end }
T.check(api.speedLimit().unit == 'kmh', 'a km/h country')
roadDist = 20
T.check(api.speedLimit() == nil, 'off the road: nil')
st.inOurLevel = false
roadDist = 1
T.check(api.speedLimit() == nil, 'another level: nil')
st.inOurLevel = true

-- the route geometry and the coordinate transform (1.6, GitHub #1075)
T.check(api.has('routeGeometry') and api.has('transform'), 'has routeGeometry, transform')
T.check(api.gpsRoute() == nil, 'no destination: no route')
api.gpsSet(45.01, 7.0, 'north')
local r = api.gpsRoute()
T.check(r and r.version == 7 and #r.points == 2 and r.points[2][1] == 45.01 and r.elev[2][2] == 260, 'the route')
r.points[1][1] = 0
T.check(api.gpsRoute().points[1][1] == 45.0, 'a copy: the caller changes nothing')
local g = api.gps()
T.check(g.off == 3.5 and g.routeVersion == 7, 'gps(): off the route, its version')
T.check(g.legs == nil and r.legs == nil, 'a route without waypoints still has no legs')
routeData.legs = { { 400, 30 }, { 712, 50 } }
local a, b = api.gpsRoute(), api.gps()
a.legs[1][1] = -1
a.legs[2] = nil
b.legs[1][2] = -2
b.legs[3] = { 1, 1 }
T.check(#routeData.legs == 2 and routeData.legs[1][1] == 400 and routeData.legs[1][2] == 30
  and routeData.legs[2][1] == 712, 'editing either API snapshot leaves the GPS legs intact')
T.check(api.gpsRoute().legs[1][1] == 400 and api.gps().legs[1][2] == 30,
  'subsequent API reads get the original route legs')
r.elev[1][2] = 0
T.check(api.gpsRoute().elev[1][2] == 250, 'elevation rows are detached too')
T.check(api.has('routeSteps') and r.steps == nil, 'routeSteps capability, nil with an older companion')
routeData.steps = { { type = 'roundabout', exit = 3, name = 'Main Road', leg = 1, offset = 400, distance = 712,
  location = { lat = 45.01, lon = 7 }, lanes = { { valid = true, indications = { 'right' } } } } }
local steps = api.gpsRoute().steps
T.check(steps[1].exit == 3 and steps[1].location.lat == 45.01 and steps[1].lanes[1].valid,
  'the route maneuvers, position and turn lanes')
steps[1].location.lat = 0
steps[1].lanes[1].indications[1] = 'left'
steps[1].exit = 99
T.check(routeData.steps[1].location.lat == 45.01 and routeData.steps[1].lanes[1].indications[1] == 'right'
  and api.gpsRoute().steps[1].exit == 3, 'nested step data is detached from the GPS and other callers')
api.gpsClear()
local gx, gy, gz = api.toGame(45.0, 7.0)
T.near(gx, 0, 1e-6, 'toGame: the origin x')
T.near(gy, 0, 1e-6, 'toGame: the origin y')
local la, lo = api.toGeo(gx, gy, gz)
T.near(la, 45.0, 1e-9, 'toGeo: back')
T.near(lo, 7.0, 1e-9, 'toGeo: back lon')
local ex, ey = api.toGame(45.0, 7.01)
T.check(ex > 780 and ex < 800 and math.abs(ey) < 2, 'toGame: 0.01 deg east is ~787 m east (' .. ex .. ', ' .. ey .. ')')
local bl, bo = api.toGeo(ex, ey)
T.near(bo, 7.01, 1e-8, 'toGeo round trip')
st.inOurLevel = false
T.check(api.toGame(45.0, 7.0) == nil and api.toGeo(0, 0) == nil, 'another level: nil')
st.inOurLevel = true

-- the road graph round a point (has('roadGraph'); server earthd Connection.road_graph, navgraph.py)
api._reset()
sent = {}
T.check(api.has('roadGraph'), 'roadGraph: has()')
T.check(select(2, api.version()) > 8, 'roadGraph: the minor version bumped past the cachedPlaces one')
local graphs = {}
local gid = api.roadGraph(39.2847, -76.5925, { radius = 1500 }, function(r) graphs[#graphs + 1] = r end)
local gq = sent[#sent]
T.check(gid and gq.type == 'road_graph' and gq.id == gid and gq.lat == 39.2847 and gq.radius == 1500,
  'roadGraph: the request sent')
T.check(gq.cachedOnly == true, 'roadGraph: cached-only is its only mode')
-- the graph is read and decoded for the add-on, not left as a file name for it to find (in game, 2026-11-13: an
-- add-on written against an earlier draft of this call checked `r.nodes` and saw every window as "no answer")
local graphFiles = {
  ['/temp/beamngEarth/s1/roadgraph_3_1.json'] = 'GRAPH1',
  ['/temp/beamngEarth/s1/roadgraph_3_2.json'] = 'GRAPH2',
}
function readFile(path) return graphFiles[path] end
function jsonDecode(text)
  if text == 'GRAPH1' then
    return { nodes = { a = { 10, 20, 30, 4 }, b = { 11, 21, 31, 5 } },
             edges = { { 'a', 'b', false, 1, 1, 25, 1.0, 4 } },
             signals = { x = 1 }, rules = { right = true }, lines = { { 1, 2, 3, 4 } }, mph = false }
  end
  if text == 'GRAPH2' then return { nodes = {}, edges = {} } end
  error('not a graph: ' .. tostring(text))
end
api._onRoadGraph({ id = gid, path = '/temp/beamngEarth/s1/roadgraph_3_1.json', anchorId = 3, serial = 1,
  complete = true })
T.check(#graphs == 1 and graphs[1].path == '/temp/beamngEarth/s1/roadgraph_3_1.json' and graphs[1].anchorId == 3
  and graphs[1].serial == 1 and graphs[1].complete == true and graphs[1].error == nil, 'roadGraph: the answer')
T.check(type(graphs[1].nodes) == 'table' and graphs[1].nodes.a[1] == 10 and graphs[1].nodes.b[4] == 5,
  'roadGraph: the nodes come decoded, keyed by their stable names')
T.check(type(graphs[1].edges) == 'table' and #graphs[1].edges == 1 and graphs[1].edges[1][1] == 'a'
  and graphs[1].edges[1][2] == 'b', 'roadGraph: the edges come decoded, naming their two nodes')
T.check(graphs[1].signals.x == 1 and graphs[1].rules.right == true and #graphs[1].lines == 1
  and graphs[1].mph == false, 'roadGraph: the graph\'s other fields carried through')
api._onRoadGraph({ id = gid, path = '/temp/beamngEarth/s1/roadgraph_3_2.json', complete = true })
T.check(#graphs == 1, 'roadGraph: one answer a request, nothing after it')
-- the graph was built but cannot be read back: an error, not a silent empty answer the caller cannot tell apart
local gone = nil
local goneId = api.roadGraph(1, 2, {}, function(r) gone = r end)
api._onRoadGraph({ id = goneId, path = '/temp/beamngEarth/s1/roadgraph_9_9.json', anchorId = 9, complete = true })
T.check(gone and gone.nodes == nil and gone.edges == nil and gone.complete == false
  and gone.error and gone.error:find('could not be read back'),
  'roadGraph: a graph file that will not read is an error, and not complete')
api._onRoadGraph({ id = gid + 100, path = '/x', complete = true })
T.check(#graphs == 1, 'roadGraph: another request\'s answer not ours')
-- a result that says nothing useful: every field defended, nothing invented
local bare = nil
local bid = api.roadGraph(1, 2, function(r) bare = r end)
T.check(bid and sent[#sent].radius == nil, 'roadGraph: opts may be left out; no radius sent (the app\'s default)')
api._onRoadGraph({ id = bid, path = 7, anchorId = 'three', serial = {}, complete = 'yes', error = 42 })
T.check(bare and bare.path == nil and bare.anchorId == nil and bare.serial == nil and bare.complete == false
  and bare.error == nil, 'roadGraph: wrong types are nil, not passed on')
local err = nil
local eid = api.roadGraph(1, 2, {}, function(r) err = r end)
api._onRoadGraph({ id = eid, complete = false, error = 'no map data (BeamNG.Earth\'s roads are off)' })
T.check(err and err.path == nil and err.nodes == nil and err.error:find('no map data') and err.complete == false,
  'roadGraph: the app\'s reason surfaced, with no graph')
local g0, gwhy = api.roadGraph(91, 0, {}, print)
T.check(g0 == nil and gwhy == 'bad coordinates', 'roadGraph: bad coordinates refused')
T.check(api.roadGraph(1, 2, {}) == nil, 'roadGraph: no callback refused')
connected = false
local g1, gwhy1 = api.roadGraph(1, 2, {}, print)
T.check(g1 == nil and gwhy1 and gwhy1:find('not connected'), 'roadGraph: not connected')
connected = true
-- never answered: heard once, timed out, and a failing callback contained
local lateG = {}
api.roadGraph(1, 2, {}, function(r) lateG[#lateG + 1] = r error('a broken callback') end)
api.onUpdate(api.ROAD_GRAPH_TIMEOUT_S - 1)
T.check(#lateG == 0, 'roadGraph: not timed out yet')
api.onUpdate(2)
T.check(#lateG == 1 and lateG[1].error == 'timed out' and lateG[1].complete == false, 'roadGraph: timed out once')
api.onUpdate(api.ROAD_GRAPH_TIMEOUT_S * 2)
T.check(#lateG == 1, 'roadGraph: only once')
T.check(api.ROAD_GRAPH_TIMEOUT_S > 0 and api.ROAD_GRAPH_TIMEOUT_S < api.PLACES_TIMEOUT_S,
  'roadGraph: its own wait, under the places one (no download is ever waited on)')
-- the two request kinds keep their own tables and their own waits
api._reset()
sent = {}
local bothP, bothG = {}, {}
local pid = api.places(1, 2, {}, function(r) bothP[#bothP + 1] = r end)
local ggid = api.roadGraph(1, 2, {}, function(r) bothG[#bothG + 1] = r end)
api.onUpdate(api.ROAD_GRAPH_TIMEOUT_S + 1)
T.check(#bothG == 1 and bothG[1].error == 'timed out' and #bothP == 0,
  'the road graph times out on its own clock, the places request still waiting')
api.onUpdate(api.PLACES_TIMEOUT_S)
T.check(#bothP == 1 and bothP[1].error == 'timed out', 'and the places request on its own')
api._onPlaces({ id = pid, places = {}, complete = true })
api._onRoadGraph({ id = ggid, path = '/x', complete = true })
T.check(#bothP == 1 and #bothG == 1, '_reset\'s tables: a late answer to a swept request is dropped')
------------------------------------------------------------------------------------------
-- a saved area along a route (1.9, has('areaRoute'))

T.check(api.has('areaRoute'), 'has areaRoute')
local LINE = { { 45.0, 7.0 }, { 45.004, 7.004 }, { 45.008, 7.008 } }
sent = {}
local areaRes = nil
local function onArea(r) areaRes = r end

-- refused before anything goes on the wire
local bad = {
  { 'no callback', function() return api.areaPlan(LINE, { half = 300 }) end },
  { 'one point', function() return api.areaPlan({ LINE[1] }, { half = 300 }, onArea) end },
  { 'no points', function() return api.areaPlan('nope', { half = 300 }, onArea) end },
  { 'a point off the map', function() return api.areaPlan({ { 91, 0 }, { 0, 0 } }, { half = 300 }, onArea) end },
  { 'a point that is not a pair', function() return api.areaPlan({ 1, 2 }, { half = 300 }, onArea) end },
  { 'no half width', function() return api.areaPlan(LINE, {}, onArea) end },
  { 'no opts at all', function() return api.areaPlan(LINE, nil, onArea) end },
  { 'an empty layers list', function() return api.areaPlan(LINE, { half = 300, layers = {} }, onArea) end },
  { 'layers that is not a list', function() return api.areaPlan(LINE, { half = 300, layers = 'roads' }, onArea) end },
  { 'a layer that is not a name', function() return api.areaPlan(LINE, { half = 300, layers = { 1 } }, onArea) end },
}
for _, case in ipairs(bad) do
  local id, why = case[2]()
  T.check(id == nil and type(why) == 'string' and #why > 0, 'areaPlan refuses ' .. case[1] .. ' (' .. tostring(why) .. ')')
end
T.check(#sent == 0, 'and put nothing on the wire for any of them')
connected = false
local id, why = api.areaPlan(LINE, { half = 300 }, onArea)
T.check(id == nil and why == 'Companion server not connected', 'not connected: says so, sends nothing')
connected = true
T.check(#sent == 0, 'still nothing sent')

-- the request as it really goes out
local planId = api.areaPlan(LINE, { half = 300, layers = { 'roads' } }, onArea)
T.check(planId ~= nil and #sent == 1, 'areaPlan sent one message')
local m = sent[1]
T.check(m.type == 'area_route_plan' and m.id == planId, 'an area_route_plan with the request id')
T.check(type(m.route) == 'table' and #m.route.lat == 3 and #m.route.lon == 3
  and m.route.lat[2] == 45.004 and m.route.lon[3] == 7.008, 'the points as two parallel lists, in order')
T.check(m.half == 300 and m.layers[1] == 'roads', 'the half width and the layers subset')
T.check(m.fast == nil and m.mode == nil and m.name == nil,
  'no fast (it pauses the player game), no mode, no name on a plan')

-- the estimate back, decoded
api._onAreaPlan({ id = planId, ok = true, km = 1.2, km2 = 3.4, items = 51, missing = 51, downloadBytes = 30600000,
  diskBytes = 67300000, seconds = 20, days = 0, hosts = {}, excluded = {}, layers = { 'roads' } })
T.check(areaRes ~= nil and areaRes.ok == true, 'the callback was called, ok')
T.check(areaRes.km == 1.2 and areaRes.items == 51 and areaRes.downloadBytes == 30600000
  and areaRes.diskBytes == 67300000, 'the numbers came over as numbers')
T.check(type(areaRes.layers) == 'table' and areaRes.layers[1] == 'roads' and areaRes.error == nil,
  'and the subset the app honoured')
areaRes = nil
api._onAreaPlan({ id = planId, ok = true, km = 9 })
T.check(areaRes == nil, 'one answer a request: a second is ignored')
api._onAreaPlan({ id = 9999, ok = true })
T.check(areaRes == nil, "another request's answer is ignored")
api._onAreaPlan(nil)
api._onAreaPlan({ })
T.check(areaRes == nil, 'a malformed answer is ignored rather than thrown')

-- every result field defended against a wrong type
sent = {}
planId = api.areaPlan(LINE, { half = 300 }, onArea)
T.check(sent[1].layers == nil, 'no subset asked for: no layers field on the wire')
areaRes = nil
api._onAreaPlan({ id = planId, ok = 'yes', km = 'far', items = {}, layers = 'roads', error = 7 })
T.check(areaRes ~= nil and areaRes.ok == false and areaRes.km == nil and areaRes.items == nil
  and areaRes.layers == nil and areaRes.error == nil, 'wrong types are dropped, not passed through')

-- a refusal from the app reaches the add-on in words
sent = {}
planId = api.areaPlan(LINE, { half = 300 }, onArea)
areaRes = nil
api._onAreaPlan({ id = planId, ok = false, error = 'saved areas need the download cache' })
T.check(areaRes.ok == false and areaRes.error == 'saved areas need the download cache', 'the reason, and ok false')

-- the add: the same request plus a name, still no fast
sent = {}
local addId = api.areaAdd(LINE, { half = 300, layers = { 'roads' }, name = 'Earth Rally: stage 3' }, onArea)
T.check(addId ~= nil and #sent == 1 and sent[1].type == 'area_route_add', 'areaAdd sent an area_route_add')
T.check(sent[1].name == 'Earth Rally: stage 3' and sent[1].layers[1] == 'roads', 'the name and the subset')
T.check(sent[1].fast == nil and sent[1].mode == nil, 'and no fast and no mode, on the add either')
areaRes = nil
api._onAreaAdd({ id = addId, ok = true, area = 'a7' })
T.check(areaRes.ok == true and areaRes.id == 'a7', "the new area's id, as a string")
areaRes = nil
sent = {}
addId = api.areaAdd(LINE, { half = 300 }, onArea)
api._onAreaAdd({ id = addId, ok = true, area = 42 })
T.check(areaRes.ok == false and areaRes.id == nil, 'an id that is not a string is no id, and not ok')
areaRes = nil
sent = {}
addId = api.areaAdd(LINE, { half = 300 }, onArea)
api._onAreaAdd({ id = addId, ok = false, error = 'at most 10 saved areas: delete one first' })
T.check(areaRes.ok == false and areaRes.error:find('10 saved areas'), 'the refusal, for the add-on to show')

-- areaStatus over the app's own area list
T.check(api.areaStatus('a7') == nil, 'no area list yet: nil')
T.check(api.areaStatus(nil) == nil, 'no id: nil')
local areaList = { areas = { { id = 'a7', name = 'Earth Rally: stage 3', state = 'done', done = 51, total = 51,
  unavailable = 6, msg = '6 items unavailable (no server had them in 3 tries): Resume tries again', mode = 'data',
  half = 300, layers = { 'roads' } } }, fast = '' }
beamngEarth_core.areasState = function() return areaList end
local a7 = api.areaStatus('a7')
T.check(a7 ~= nil and a7.id == 'a7' and a7.state == 'done' and a7.done == 51 and a7.total == 51, 'the area by its id')
T.check(a7.unavailable == 6 and a7.msg:find('Resume tries again'),
  "done is not the data arrived: unavailable and msg reach the add-on")
T.check(a7.half == 300 and a7.layers[1] == 'roads', 'its width and the subset it was added with')
a7.layers[1] = 'buildings'
T.check(api.areaStatus('a7').layers[1] == 'roads', 'a copy: the caller changes nothing in the mod')
areaList.areas[1].msg = ''
T.check(api.areaStatus('a7').msg == nil, 'an empty msg is nil rather than an empty string to print')
areaList.areas[1].unavailable = nil
T.check(api.areaStatus('a7').unavailable == 0, 'an older app without the field reads as none')
T.check(api.areaStatus('a1') == nil, 'an area that is not in the list (deleted): nil')
areaList.areas = {}
T.check(api.areaStatus('a7') == nil, 'an empty list: nil')
beamngEarth_core.areasState = function() return nil end

-- a request the app never answers
sent = {}
areaRes = nil
local lateId = api.areaPlan(LINE, { half = 300 }, onArea)
api.onUpdate(1.0)
T.check(areaRes == nil and lateId ~= nil, 'nothing handed over while it is in flight')
api.onUpdate(api.AREA_TIMEOUT_S + 1.0)
T.check(areaRes ~= nil and areaRes.ok == false and areaRes.error == 'timed out', 'after AREA_TIMEOUT_S: timed out')
areaRes = nil
api.onUpdate(1000.0)
T.check(areaRes == nil, 'and heard once')
-- a callback that throws is contained, and the request still forgotten
sent = {}
local boomId = api.areaAdd(LINE, { half = 300 }, function() error('boom') end)
api._onAreaAdd({ id = boomId, ok = true, area = 'a8' })
api._onAreaAdd({ id = boomId, ok = true, area = 'a8' })
T.check(true, 'a failing area callback is contained')

T.done('test_api')
