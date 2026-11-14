-- beamngEarth_api: BeamNG.Earth's modding API for other mods (docs/ADDONS.md "Modding API v1"). A stable surface over
-- the mod's internals, so an add-on asks this instead of beamngEarth_core._state() and our module tables (Trucking's
-- earthAdapter.lua reads core._state().anchor:enuToGeodetic, places.here, statusbar.location, gps.setDestination and
-- core.teleport today: each change of ours could break it). Its names and fields only change with a new major
-- version (version()); new functions come with a new minor version and a has() name.
--
--   local api = rawget(_G, 'beamngEarth_api')            -- nil: BeamNG.Earth is not installed (or too old)
--   if api and api.version() == 1 and api.ready() then
--     local p = api.pose()                                 -- { lat, lon, alt, heading, speed, ve, vn, vu, ... }
--   end
--
-- Events, as the game's own hooks (any extension that defines the function gets it; extensions.hook):
--   onBeamNGEarthWorldReady(info)     the world around the player is loaded (after the level loads, a teleport)
--   onBeamNGEarthTeleported(info)     a teleport finished: info.lat / lon where the player is now
--   onBeamNGEarthRouteArrived(info)   the GPS destination reached: info.lat / lon / name
--   onBeamNGEarthWaypointReached(info) a GPS waypoint passed (1.2, has('gpsRoute')): info.lat / lon / name / left
--   onBeamNGEarthAddonOptions(info)   an add-on's options were saved on its Options tab: info.id, info.values
--   onBeamNGEarthRebased(info)        the world's origin moved (1.7, has('rebase')): info.lat / lon the new origin,
--                                     oldLat / oldLon the one before (nil at the first), anchorId, purpose
-- or with api.on(event, fn) ('worldReady', 'teleported', 'routeArrived', 'addonOptions', 'rebased'), which returns a
-- handle for api.off(handle).
--
-- Options (1.1, has('options')): an add-on declares them in its beamngEarth_addon.json ({"options": [{"key": "port",
-- "label": "Port", "type": "int", "default": 47700, "min": 1024, "max": 65535}]}, docs/ADDONS.md); the player sets them
-- on its Options tab; api.addonOptions(id) is { key = value } (the defaults until then), or nil before the list came.
-- Places (1.4, has('places') / has('buildings')): api.places(lat, lon, opts, callback) asks the BeamNG.Earth app for
-- Overture's places round a point (every kind, docs/ADDONS.md); the callback gets each answer (one at once from the map
-- cells on disk, one more later when some were missing: result.pending says whether another is coming).
-- Pure reads are cheap (no allocation beyond the returned table); nothing here blocks or talks to the network itself
-- (places only sends a request to the app, which answers from what it has).
-- A wide search over what is already on disk (1.8, has('cachedPlaces')): opts.cachedOnly takes the map cells the app
-- has and asks for no others, with no second answer, so opts.radius may go to 25 km; it must name opts.categories,
-- and result.truncated says the reach was cut back to the app's cell cap.
-- The road network round a point (has('roadGraph')): api.roadGraph(lat, lon, opts, callback) asks the app to build one
-- window of the road graph there out of what it has on disk already, and hands the graph to the callback (result.nodes
-- and result.edges; result.path is the file it wrote, for an add-on that would rather read it itself). It is not the
-- AI's graph, and nothing is downloaded for it -- no map cell and no elevation tile: result.complete says whether
-- everything the window needed was there. See docs/ADDONS.md.
local M = {}

-- 1.9: both of this change's calls, api.roadGraph and api.areaPlan / areaAdd / areaStatus. They were written on
-- branches of their own and each took the next minor; they are one change now, so they are one minor.
M.MAJOR, M.MINOR = 1, 9
local FEATURES = { pose = true, place = true, teleport = true, gps = true, notify = true, events = true, options = true,
  gpsRoute = true, inTunnel = true, weather = true, places = true, buildings = true, speedLimit = true,
  routeGeometry = true, transform = true, routeSteps = true, rebase = true, cachedPlaces = true,
  roadGraph = true, areaRoute = true }
-- The ground along a route, downloaded ahead (1.9, has('areaRoute')): api.areaPlan(points, opts, callback) says what
-- a corridor along points would download, and api.areaAdd(points, opts, callback) adds it to the player's own saved
-- areas, which download in the background and persist; api.areaStatus(id) follows one. opts.layers narrows it to the
-- layers an add-on needs ('roads' alone, say) instead of the whole ground. There is no fast mode here: it pauses the
-- player's game. See docs/ADDONS.md, and read areaAdd's own note before adding one -- an area is a persistent
-- object in the player's world, not a function call with a side effect.
local EVENTS = { worldReady = 'onBeamNGEarthWorldReady', teleported = 'onBeamNGEarthTeleported',
  routeArrived = 'onBeamNGEarthRouteArrived', addonOptions = 'onBeamNGEarthAddonOptions',
  waypointReached = 'onBeamNGEarthWaypointReached', rebased = 'onBeamNGEarthRebased' }

local function core() return rawget(_G, 'beamngEarth_core') end
local function state()
  local c = core()
  if not (c and c._state) then return nil end
  local ok, st = pcall(c._state)
  return ok and type(st) == 'table' and st or nil
end
local function mod(name)
  local ok, m = pcall(require, 'beamngEarth/' .. name)
  return ok and type(m) == 'table' and m or nil
end

------------------------------------------------------------------------------------------
-- version and state

-- The API's version: major (a change that can break callers), minor (additions).
function M.version() return M.MAJOR, M.MINOR end
-- Whether this API has a feature: 'pose', 'place', 'teleport', 'gps', 'notify', 'events', ... ('places', 'buildings'
-- since 1.4; docs/ADDONS.md lists them all).
function M.has(name) return FEATURES[name] == true end
-- BeamNG.Earth's app version ("1.8.2-beta"), once its app is connected; nil before.
function M.earthVersion()
  local c = core()
  local h = c and c.serverHello and c.serverHello()
  return h and type(h.appVersion) == 'string' and h.appVersion or nil
end
-- The BeamNG.Earth app (the companion that builds the world) connected?
function M.connected()
  local ok, net = pcall(require, 'beamngEarth/net')
  return ok and net.isConnected() == true
end
-- The BeamNG.Earth level loaded?
function M.inWorld()
  local st = state()
  return st ~= nil and st.inOurLevel == true
end
-- The world loaded round the player and nothing on its way (not teleporting, not being set down): the time to read
-- a position that means something, or to start a job.
function M.ready()
  local st, c = state(), core()
  -- (a rebase moves the origin under the player without a stop: still ready; a teleport is not)
  local tp = st and st.pending and st.pending.purpose == 'teleport'
  if not (st and st.inOurLevel and st.anchor and st.player and not tp and M.connected()) then return false end
  local ok, ground = pcall(c.groundReady)
  return ok and ground == true
end

------------------------------------------------------------------------------------------
-- where the player is

-- The player (their car, or the free camera with no car) now, or nil before the world is there:
--   lat, lon (degrees, WGS84), alt (m over the WGS84 ellipsoid), heading (degrees clockwise from true north: the car's
--   direction; nil without a car), speed (m/s), ve, vn, vu (m/s east, north, up), camera (true: no car, the camera),
--   anchorLat, anchorLon (the world's origin: game x, y are metres east / north of it, to within a fraction of a
--   degree near it), x, y, z (game coordinates).
function M.pose()
  local st = state()
  local pl = st and st.player
  local a = st and st.anchor
  if not (pl and a and pl.lat and pl.lon) then return nil end
  local _, _, alt = a:worldToGeodetic(pl.x, pl.y, pl.z)
  -- the frame's north turns away from true north off the origin (anchor:northAt): velocity and heading turned back
  local nx, ny = 0, 1
  if a.northAt then
    local ok, x, y = pcall(a.northAt, a, pl.x, pl.y)
    if ok and tonumber(x) and tonumber(y) then nx, ny = x, y end
  end
  local vx, vy = pl.vx or 0, pl.vy or 0
  local p = { lat = pl.lat, lon = pl.lon, alt = alt, speed = pl.speed or 0, camera = pl.camera == true,
    ve = vx * ny - vy * nx, vn = vx * nx + vy * ny, vu = pl.vz or 0, x = pl.x, y = pl.y, z = pl.z,
    anchorLat = a.lat, anchorLon = a.lon }
  local veh = not pl.camera and getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local ok, dir = pcall(function() return veh:getDirectionVector() end)
    if ok and dir and math.abs(dir.x) + math.abs(dir.y) >= 1e-6 then
      local h = math.deg(math.atan2(dir.x, dir.y)) - math.deg(math.atan2(nx, ny))
      p.heading = (h % 360 + 360) % 360
    end
  end
  return p
end

-- Is the player (or x, y, z in game coordinates) under a roof: a tunnel, a covered arch, a parking structure (1.3,
-- has('inTunnel'); GitHub #1028: an add-on's automatic headlights)? true / false; nil before the world is there. The
-- rule the tunnel sound uses (ambience.inTunnel); the player's point is 1 m over its position (a car's cabin).
function M.inTunnel(x, y, z)
  local amb = mod('ambience')
  if not (amb and type(amb.inTunnel) == 'function') then return nil end
  if x == nil then
    local st = state()
    local pl = st and st.player
    if not (pl and pl.x and pl.y and pl.z) then return nil end
    x, y, z = pl.x, pl.y, pl.z + 1
  end
  local ok, r = pcall(amb.inTunnel, x, y, z)
  if not ok then return nil end
  return r == true
end

-- The posted speed limit of the road the player is on (1.5, has('speedLimit'); GitHub #1061: a sat nav add-on):
-- { ms = the limit in m/s, mapped = true (the map's) / false (the road class's default) / nil (not known: where
-- the limit is the AI's own speed the graph does not say), unit = 'mph' | 'kmh' (the country's signs) }; nil off the
-- roads, before the world is there or off our level. The AI's target speed (lower in bends) is not it: the graph's
-- posted field, else its speed where the two are the same.
M.SPEED_ROAD_M = 4.0        -- m past the road's half width the player still counts as on it
function M.speedLimit()
  if not M.inWorld() then return nil end
  local nav = mod('navgraph')
  local g = nav and nav.currentGraph and nav.currentGraph()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not (g and veh and map and map.getMap and map.findClosestRoad) then return nil end
  local ok, m = pcall(map.getMap)
  if not ok or not m or not m.nodes then return nil end
  local p = veh:getPosition()
  local n1, n2, dist = map.findClosestRoad(p)
  local a, b = n1 and m.nodes[n1], n2 and m.nodes[n2]
  local e = a and b and a.links and (a.links[n2] or (b.links and b.links[n1]))
  if not e or not e.speedLimit then return nil end
  if dist and dist > (e.inRadius or 4) + M.SPEED_ROAD_M then return nil end
  local raw = tonumber(e.posted)
  local mapped = nil
  if raw then mapped = raw > 0 end
  return { ms = raw and math.abs(raw) or e.speedLimit, mapped = mapped,
           unit = g.mph == false and 'kmh' or 'mph' }
end

-- The weather the world shows now (1.3, has('weather')): { rain = 0..1, snow = 0..1 (how hard it falls where the
-- camera is: none above the cloud base), code (WMO weather code), tempC (at sea level), windMs, gustMs, visibilityM,
-- rainMmH, snowCmH }, fields nil where not known; nil when there is no weather yet (before the first report, or the
-- weather off).
function M.weather()
  local w = mod('weather')
  if not w then return nil end
  local m = type(w.current) == 'function' and w.current() or nil
  if type(m) ~= 'table' then return nil end
  local wind = type(m.wind) == 'table' and m.wind or {}
  return { rain = tonumber(w.rainNow) or 0, snow = tonumber(w.snowNow) or 0, code = tonumber(m.code),
    tempC = tonumber(m.slTempC), windMs = tonumber(wind.ms), gustMs = tonumber(wind.gustMs),
    visibilityM = tonumber(m.visM), rainMmH = tonumber(m.rainMmH), snowCmH = tonumber(m.snowCmH) }
end

-- lat, lon of the player now (nil before the world is there).
function M.position()
  local st = state()
  local pl = st and st.player
  if pl and pl.lat and pl.lon then return pl.lat, pl.lon end
  return nil
end

-- The place by name, as the status bar at the top of the screen shows it: { street, place, region, country } (each
-- a string or nil), or nil when none is known yet (just teleported, the status bar or online names off).
function M.place()
  local bar = mod('statusbar')
  local l = bar and bar.location and bar.location()
  if type(l) ~= 'table' then return nil end
  local out, any = {}, false
  for _, k in ipairs({ 'street', 'place', 'region', 'country' }) do
    if type(l[k]) == 'string' and l[k] ~= '' then out[k], any = l[k], true end
  end
  return any and out or nil
end

------------------------------------------------------------------------------------------
-- actions

-- Teleport the player to lat, lon (name: for the recent places, optional), as the map's Go does. True once BeamNG.Earth
-- took it (the place loads and the player is set down some seconds later: onBeamNGEarthTeleported), else false and
-- why ('Load the BeamNG.Earth level first', 'Companion server not connected', bad coordinates ...).
function M.teleport(lat, lon, name)
  local places = mod('places')
  if not places then return false, 'BeamNG.Earth is not loaded' end
  return places.goTo(lat, lon, type(name) == 'string' and name or nil)
end

-- Set the GPS destination (the route is planned on the roads, drawn on the map, with arrows on the road). True, or
-- false and why.
function M.gpsSet(lat, lon, name)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then return false, 'bad coordinates' end
  local gps = mod('gps')
  if not gps then return false, 'BeamNG.Earth is not loaded' end
  return gps.setDestination(lat, lon, type(name) == 'string' and name or nil) == true
end

-- The GPS through waypoints (1.2, has('gpsRoute'); GitHub #935): points { { lat, lon, name? }... }, the last the
-- destination, at most 11 (10 waypoints), planned as one route. True, or false and why.
function M.gpsSetRoute(points)
  local gps = mod('gps')
  if not gps or not gps.setRoute then return false, 'BeamNG.Earth is not loaded' end
  return gps.setRoute(points)
end

function M.gpsClear()
  local gps = mod('gps')
  if gps then gps.clear() end
end

-- Route legs are pairs of distance/time. Neither public read may hand an add-on the GPS's own rows.
local function copyLegs(legs)
  if type(legs) ~= 'table' then return nil end
  local out = {}
  for i, leg in ipairs(legs) do out[i] = { leg[1], leg[2] } end
  return out
end

-- The GPS destination and the way there: { lat, lon, name, remaining (m), eta (s), planning, waypoints, legs }, or
-- nil without one (waypoints: those still ahead, { { lat, lon, name }... }; legs: the planned route's { { dist m,
-- dur s }... } from the start through each waypoint, with waypoints only).
function M.gps()
  local gps = mod('gps')
  local s = gps and gps.status and gps.status()
  if type(s) ~= 'table' then return nil end
  return { lat = s.lat, lon = s.lon, name = s.name, remaining = s.remaining, eta = s.eta, planning = s.planning == true,
           waypoints = s.waypoints or {}, legs = copyLegs(s.legs), off = s.off, routeVersion = s.routeVersion }
end

local function copyRouteValue(value)
  if type(value) ~= 'table' then return value end
  local out = {}
  for k, v in pairs(value) do out[k] = copyRouteValue(v) end
  return out
end

-- The planned route (1.6, has('routeGeometry'); GitHub #1075): { version, points = { { lat, lon }... }, dist (m),
-- dur (s), elev = { { distM, elevM }... }, legs }, as the map draws it, or nil without one. version changes with each
-- new plan (a replan after a wrong turn too); gps().routeVersion is the current one. A copy: the caller may keep it.
function M.gpsRoute()
  local gps = mod('gps')
  local r = gps and gps.route and gps.route()
  if type(r) ~= 'table' or type(r.points) ~= 'table' then return nil end
  local pts = {}
  for i, p in ipairs(r.points) do pts[i] = { p[1], p[2] } end
  local elev = nil
  if type(r.elev) == 'table' then
    elev = {}
    for i, e in ipairs(r.elev) do elev[i] = { e[1], e[2] } end
  end
  return { version = r.version, points = pts, dist = r.dist, dur = r.dur, elev = elev, legs = copyLegs(r.legs),
    steps = copyRouteValue(r.steps) }       -- 1.7, has('routeSteps'); nil with an older companion
end

------------------------------------------------------------------------------------------
-- coordinates (1.6, has('transform'); GitHub #1075)

-- lat, lon (and h m over the WGS84 ellipsoid, default 0) to the game's x, y, z in the current anchor's frame, as
-- pose() gives them; nil before the world is there. Game coordinates change at a rebase (onBeamNGEarthTeleported, and
-- the quiet rebases while driving): convert again rather than keep them.
function M.toGame(lat, lon, h)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return nil end
  local st = state()
  local a = st and st.inOurLevel and st.anchor
  if not a or not a.geodeticToWorld then return nil end
  return a:geodeticToWorld(lat, lon, tonumber(h) or 0)
end

-- The game's x, y (z: default 0) to lat, lon, h (m over the ellipsoid) in the current anchor's frame; nil before the
-- world is there.
function M.toGeo(x, y, z)
  x, y = tonumber(x), tonumber(y)
  if not x or not y then return nil end
  local st = state()
  local a = st and st.inOurLevel and st.anchor
  if not a or not a.worldToGeodetic then return nil end
  return a:worldToGeodetic(x, y, tonumber(z) or 0)
end

-- Is the world's origin being moved right now (1.7, has('rebase'))? True between a rebase's two frames: the vehicles
-- are in the new frame already, toGame / toGeo and pose() still in the old one. Leave positions alone until
-- onBeamNGEarthRebased, which comes in the frame that ends it.
function M.rebasing()
  local st = state()
  return st ~= nil and st.rebasing ~= nil
end

-- A short message on the screen (the game's own messages), seconds (default 5).
function M.notify(text, seconds)
  if type(text) ~= 'string' or text == '' then return false end
  if ui_message then pcall(ui_message, text:sub(1, 300), tonumber(seconds) or 5, 'beamngEarthApi') return true end
  return false
end

------------------------------------------------------------------------------------------
-- places (1.4)

-- (an answer the app never sent, a lost connection: the callback still hears once, after this long. The app's own
-- wait for missing cells is 2 min, earthd PLACES_TRIES)
M.PLACES_TIMEOUT_S = 150
local placeReqs, placeReqId, clock = {}, 0, 0

local function placesRequest(lat, lon, opts, callback, buildings)
  if type(opts) == 'function' and callback == nil then opts, callback = nil, opts end
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then return nil, 'bad coordinates' end
  if type(callback) ~= 'function' then return nil, 'no callback' end
  local net = mod('net')
  if not (net and M.connected()) then return nil, 'Companion server not connected' end
  opts = type(opts) == 'table' and opts or {}
  local cats = nil
  if type(opts.categories) == 'table' then
    cats = {}
    for _, c in ipairs(opts.categories) do if type(c) == 'string' then cats[#cats + 1] = c end end
    if #cats == 0 then cats = nil end       -- (an empty table goes as JSON {}, not []: none sent, every kind)
  end
  placeReqId = placeReqId + 1
  placeReqs[placeReqId] = { fn = callback, t = clock }
  net.send({ type = 'places', id = placeReqId, lat = lat, lon = lon, radius = tonumber(opts.radius),
    categories = cats, limit = tonumber(opts.limit), cursor = type(opts.cursor) == 'string' and opts.cursor or nil,
    buildings = buildings or opts.buildings == true, minConfidence = tonumber(opts.minConfidence),
    cachedOnly = opts.cachedOnly == true })
  return placeReqId
end

-- Overture's places round lat, lon (1.4, has('places')), every kind (not only the 3D map's). opts (all optional):
--   radius (m, default 1000, at most 3000, or 25000 with cachedOnly), categories ({ 'gas_station', 'car_*',
--   'group:food' }: Overture basic_category values, prefixes ending in '*', or the map's icon groups), limit
--   (default 50, at most 200), cursor (a result's next: the next page), minConfidence (0..1), buildings (true: the
--   footprints too, as api.buildings), cachedOnly (1.8: only the cells the app already has, see below).
-- callback(result) for each answer: { places = { { id, name, category, lat, lon, confidence, open }... } nearest first
-- (open: Overture's operating_status, '' when not known), buildings (asked only), complete (every map cell round the
-- point was read), pending (another answer for this request is coming), next (the next page's cursor, nil on the
-- last), truncated (1.8: the reach was cut back to the app's cell cap), error (why there are none: no map data, bad
-- request, timed out) }. Returns the request's id, or nil and why.
-- Only places in map cells the app has or fetches in the background: nothing is downloaded ahead of the world.
-- cachedOnly = true asks for no cell the app does not already have on disk: it queues no download of its own, the
-- first answer is the only one (pending false, complete false where cells were missing), and radius may go to 25 km.
-- Such a search past 3000 m must name categories, and result.truncated says its reach was cut back (far north a cell
-- is small, so the cap comes sooner). Cheap, but it finds only what the player has already been near, and a cell
-- whose stored copy is damaged is still rebuilt from its sources.
function M.places(lat, lon, opts, callback) return placesRequest(lat, lon, opts, callback, false) end

-- The buildings' footprints round lat, lon (1.4, has('buildings')): as places, with result.buildings
-- { { id, lat, lon, height (m), levels, name, class, outline = { { lat, lon }... } }... } nearest first (height,
-- levels nil where not mapped), within 500 m and at most 100.
function M.buildings(lat, lon, opts, callback) return placesRequest(lat, lon, opts, callback, true) end

local function placesCall(r, result)
  local ok, err = pcall(r.fn, result)
  if not ok and log then log('W', 'beamngEarth', 'api: a places callback failed: ' .. tostring(err)) end
end

-- (ours: core.lua hands the app's places_result here)
function M._onPlaces(msg)
  local r = type(msg) == 'table' and placeReqs[msg.id]
  if not r then return end
  local result = { places = type(msg.places) == 'table' and msg.places or {}, complete = msg.complete == true,
    pending = msg.pending == true, next = type(msg.next) == 'string' and msg.next or nil,
    error = type(msg.error) == 'string' and msg.error or nil, truncated = msg.truncated == true,
    buildings = type(msg.buildings) == 'table' and msg.buildings or nil }
  if not result.pending then placeReqs[msg.id] = nil end
  placesCall(r, result)
end

local function placesTimeouts()
  local late = {}
  for id, r in pairs(placeReqs) do
    if clock - r.t > M.PLACES_TIMEOUT_S then late[#late + 1] = id end
  end
  -- (called after the walk: a callback may ask again, and a key added during pairs() is undefined in Lua)
  table.sort(late)
  for _, id in ipairs(late) do
    local r = placeReqs[id]
    placeReqs[id] = nil
    placesCall(r, { places = {}, complete = false, pending = false, error = 'timed out' })
  end
end

------------------------------------------------------------------------------------------
-- the road graph round a point (has('roadGraph'))

-- (its own wait, shorter than PLACES_TIMEOUT_S: no map cell is fetched for a road graph, so unlike a places request
-- it never waits on a download, and PLACES_TIMEOUT_S is 150 s because the app's own wait for missing cells is 2 min.
-- What is left to wait for is the build itself and the requests queued ahead of it on the one thread the app answers
-- API requests on: a road-graph window is a wider read than a places query and the app assembles nodes, lanes,
-- speeds and bridge decks from it, which is seconds, so 90 s is well past several of them in a row)
M.ROAD_GRAPH_TIMEOUT_S = 90
local graphReqs, graphReqId = {}, 0

-- One window of the road network round lat, lon (has('roadGraph')), built by the app out of the map cells it already
-- has. opts (all optional):
--   radius (m, half the side of the square around the point: default 1100, at most 2000).
-- callback(result) once: { nodes = { [name] = { x, y, z, radius } }, edges = { { in, out, oneway, lanesOut, lanesIn,
-- speed m/s, drivability, radius, ... } }, signals, rules, lines, mph, complete (every map cell of the window was on
-- disk), anchorId, serial, path, error (why there is no graph: no map data, the app started without roads, no world
-- yet, bad coordinates, timed out, or the graph could not be read back) }. Returns the request's id, or nil and why.
-- x, y, z are in the current anchor's frame (api.toGeo turns x, y back into lat, lon) and z is the drivable surface.
-- A node's name is stable across windows and across anchors, so two windows that overlap name the same node once:
-- stitching them is keying on the name, not matching coordinates.
-- nodes and edges are nil when error is set. `path` is the file they were read from, for an add-on that would rather
-- read it itself -- but it is in our session folder and we keep only the last couple of graphs of an anchor, so an
-- add-on that holds the path rather than the tables will find it gone.
-- What it does not promise:
--   * No map cell is downloaded for it. The app reads the window's layers only where it has them on disk; a window
--     over ground the player has never been near comes back with few roads and complete = false, which is the honest
--     answer rather than a slow one. There is deliberately no fetching form of the request.
--   * It is not the AI's graph. The file is the add-on's own copy and never reaches the game's map, so writing to it
--     or ignoring it moves no car and no traffic.
--   * There is no bridge flag. A node on a bridge takes the deck's height, which is the right height, but nothing in
--     nodes or edges says that stretch is a bridge, and the heights alone cannot be read as one.
--   * It depends on the app having roads at all: one started with roads off answers with error and no path, and
--     has('roadGraph') cannot see that (it is this mod's version, not the app's state).
function M.roadGraph(lat, lon, opts, callback)
  if type(opts) == 'function' and callback == nil then opts, callback = nil, opts end
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then return nil, 'bad coordinates' end
  if type(callback) ~= 'function' then return nil, 'no callback' end
  local net = mod('net')
  if not (net and M.connected()) then return nil, 'Companion server not connected' end
  opts = type(opts) == 'table' and opts or {}
  graphReqId = graphReqId + 1
  graphReqs[graphReqId] = { fn = callback, t = clock }
  net.send({ type = 'road_graph', id = graphReqId, lat = lat, lon = lon, radius = tonumber(opts.radius),
    cachedOnly = true })
  return graphReqId
end

local function roadGraphCall(r, result)
  local ok, err = pcall(r.fn, result)
  if not ok and log then log('W', 'beamngEarth', 'api: a roadGraph callback failed: ' .. tostring(err)) end
end

-- (ours: core.lua hands the app's road_graph_result here)
function M._onRoadGraph(msg)
  local r = type(msg) == 'table' and graphReqs[msg.id]
  if not r then return end
  graphReqs[msg.id] = nil                 -- (one answer a request: nothing follows it)
  local path = type(msg.path) == 'string' and msg.path or nil
  local out = { path = path, anchorId = tonumber(msg.anchorId), serial = tonumber(msg.serial),
                complete = msg.complete == true,
                error = type(msg.error) == 'string' and msg.error or nil }
  -- The graph itself, read and decoded here rather than handed over as a file name: api.places answers with its rows
  -- the same way, an add-on has no business knowing the layout of our session folder, and every caller would decode
  -- it anyway. `path` stays in the answer for one that would rather read the file itself.
  if path and not out.error then
    local txt = rawget(_G, 'readFile')
    txt = txt and txt(path) or nil
    local okDecode, g = pcall(jsonDecode, txt or '')
    if okDecode and type(g) == 'table' and type(g.nodes) == 'table' and type(g.edges) == 'table' then
      out.nodes, out.edges = g.nodes, g.edges
      out.signals = type(g.signals) == 'table' and g.signals or nil
      out.rules = type(g.rules) == 'table' and g.rules or nil
      out.lines = type(g.lines) == 'table' and g.lines or nil
      out.mph = g.mph
    else
      -- (the file is written before the message is sent, so this is a real failure and not a race)
      out.complete, out.error = false, 'the graph was built but could not be read back from ' .. tostring(path)
    end
  end
  roadGraphCall(r, out)
end

local function roadGraphTimeouts()
  local late = {}
  for id, r in pairs(graphReqs) do
    if clock - r.t > M.ROAD_GRAPH_TIMEOUT_S then late[#late + 1] = id end
  end
  -- (called after the walk, as placesTimeouts is: a callback may ask again, and a key added during pairs() is
  --  undefined in Lua)
  table.sort(late)
  for _, id in ipairs(late) do
    local r = graphReqs[id]
    graphReqs[id] = nil
    roadGraphCall(r, { complete = false, error = 'timed out' })
  end
end

-- a saved area along a route (has('areaRoute'))

-- (its own wait, and a long one: an estimate walks every item of the plan and asks each imagery source what it
--  would fetch for every square, and an add walks the same plan again. A 300 m corridor over a 20 km stage is tens
--  of thousands of items, and it runs on the app's shared executor behind whatever else is queued there.)
M.AREA_TIMEOUT_S = 120
M.AREA_MAX_POINTS = 20000           -- (the app's own areas.ROUTE_MAX_POINTS; decimate long before this, see below)
local areaReqs, areaReqId = {}, 0

-- Every layer name api.areaPlan / api.areaAdd take (the app's areas.all_layers(); 'terrain' is the elevation and
-- 'imagery' the aerial photography, which an area downloads too and which a caller after one layer of the ground
-- has no use for either).
M.AREA_LAYERS = { 'roads', 'connectors', 'buildings', 'water', 'land_use', 'infrastructure', 'landcover', 'canopy',
                  'building_parts', 'places', 'terrain', 'imagery' }

local function areaPoints(points)
  if type(points) ~= 'table' then return nil, 'points must be a list of { lat, lon }' end
  local lat, lon, n = {}, {}, 0
  for i, p in ipairs(points) do
    local a, o
    if type(p) == 'table' then a, o = tonumber(p[1] or p.lat), tonumber(p[2] or p.lon) end
    if not a or not o or a <= -85 or a >= 85 or o < -180 or o > 180 then
      return nil, 'point ' .. tostring(i) .. ' is not on the map'
    end
    lat[i], lon[i], n = a, o, i
  end
  if n < 2 then return nil, 'a corridor needs at least two points' end
  if n > M.AREA_MAX_POINTS then return nil, 'at most ' .. M.AREA_MAX_POINTS .. ' points (decimate the line first)' end
  return { lat = lat, lon = lon }
end

local function areaLayers(v)
  if v == nil then return nil end
  if type(v) ~= 'table' then return nil, 'layers must be a list of layer names' end
  local out, n = {}, 0
  for i, name in ipairs(v) do
    if type(name) ~= 'string' then return nil, 'layers must be a list of layer names' end
    out[i], n = name, i
  end
  if n == 0 then return nil, 'an area with no layers would download nothing' end
  return out
end

-- The arguments checked and the request booked: (id, body, net) or nil, why. The two calls below put the message's
-- own type on it rather than taking it as an argument: a literal `type = '...'` in each net.send is what a reader
-- and the app's protocol agreement table both go by.
local function areaBook(points, opts, callback)
  if type(callback) ~= 'function' then return nil, 'no callback' end
  local route, why = areaPoints(points)
  if not route then return nil, why end
  opts = type(opts) == 'table' and opts or {}
  local layers
  layers, why = areaLayers(opts.layers)
  if why then return nil, why end
  local half = tonumber(opts.half)
  if not half then return nil, 'opts.half (the width each side, m) is needed' end
  local net = mod('net')
  if not (net and M.connected()) then return nil, 'Companion server not connected' end
  areaReqId = areaReqId + 1
  areaReqs[areaReqId] = { fn = callback, t = clock }
  return areaReqId, { route = route, half = half, layers = layers,
                      name = type(opts.name) == 'string' and opts.name or nil }, net
end

-- What a saved area along `points` would download, without downloading or saving anything (has('areaRoute')).
-- points: { { lat, lon }, ... } (api.gpsRoute().points is exactly this shape). opts:
--   half    (m, the corridor's width each side: 200 .. 5000, needed)
--   layers  (M.AREA_LAYERS, a subset; absent: all of them)
-- callback(result) once: { ok, km (the line's length), km2 (the corridor's own area), items (the plan's steps),
-- missing (those not on disk), downloadBytes, diskBytes, seconds, days, hosts, excluded, layers (the subset the app
-- honoured, {} for all of them), error }. Returns the request's id, or nil and why.
--
-- Show the player `downloadBytes` and ask them before calling areaAdd. Not `seconds` (a serial-request model with
-- no bandwidth in it), not `days` (0 nearly everywhere), and not `km2` (the corridor's geometry, far smaller than
-- the footprint actually downloaded: the plan dilates it by two map cells all round).
-- `layers` in the result is there to be read: an older BeamNG.Earth without this call ignores the field and
-- downloads everything, and has('areaRoute') is this mod's version, not the app's.
function M.areaPlan(points, opts, callback)
  local id, body, net = areaBook(points, opts, callback)
  if not id then return nil, body end
  -- (no name: nothing is saved by a plan, so there is nothing to call anything)
  net.send({ type = 'area_route_plan', id = id, route = body.route, half = body.half, layers = body.layers })
  return id
end

-- A new saved area along `points`, downloaded in the background (has('areaRoute')). Arguments as areaPlan, plus
-- opts.name (what the player sees in their area list: say which add-on it is for).
-- callback(result) once: { ok, id (the new area's id, for areaStatus), error }. Returns the request's id, or nil
-- and why.
--
-- An area is not a function call with a side effect. It is a persistent object in the player's world, and an
-- add-on that adds one has to live with all of this:
--   * **The player sees it** in the 3D map's area list, its line drawn, and can pause, resume or delete it. Name
--     it so they know what it is.
--   * **There are ten slots** (areas.MAX_AREAS) and you are competing for them. When they are full your add fails
--     -- and so does the player's own next one, which is the worse half of that.
--   * **Its data is pinned**: never evicted, and "Clear cache" keeps it. An add that would take the saved areas
--     past their share of the cache is refused with a message naming the setting to raise. Surface it; it is the
--     player's to act on, not yours to swallow.
--   * **It survives a restart** and resumes on its own, with no mod and no client involved.
--   * **One area downloads at a time, in list order.** Yours is appended last, so it can legitimately sit at zero
--     behind the player's own. A progress display must not read that as broken.
--   * **It is not removed for you.** There is no remove in this API: the area and its data stay for the player to
--     keep or delete. Do not add one per press of a button.
--   * **"done" does not mean the data arrived** -- see areaStatus.
-- Decimate the line to a point every 20-50 m first. The points are persisted verbatim into the app's area store
-- and re-validated on every load, and a 6000-point line is a quarter of a megabyte of JSON rewritten every time
-- any area changes.
-- There is no fast mode here, deliberately. Fast mode skips the gates that keep a download from slowing the world,
-- and the pause that makes that survivable is applied by BeamNG.Earth itself for any client on any fast area -- so
-- an add-on able to set it could stop the player's physics in the middle of whatever they were doing. If a player
-- wants it, they can press it on their own area list.
function M.areaAdd(points, opts, callback)
  local id, body, net = areaBook(points, opts, callback)
  if not id then return nil, body end
  -- (no `fast`, on purpose and for good: see above. There is nothing to leave out, because there is no argument
  --  for it anywhere in this file.)
  net.send({ type = 'area_route_add', id = id, route = body.route, half = body.half, layers = body.layers,
             name = body.name })
  return id
end

-- The saved area `id` (the id areaAdd handed back), as the app last broadcast it, or nil when it is not in the
-- player's list any more (deleted, or a store that would not load):
--   { id, name, state, done, total, unavailable, msg, mode, half, layers }
-- state is 'queued' (waiting its turn), 'checking' (walking the plan), 'running', 'waiting' (a host's daily limit
-- or being offline: it tries again by itself), 'paused' (the player) or 'done'. done / total are items, not bytes.
-- **'done' does not mean the data arrived.** After three passes with failures the items still failing are counted
-- in `unavailable`, the area is marked done anyway, and `msg` becomes "N items unavailable (no server had them in
-- 3 tries): Resume tries again". So: re-check what you asked for rather than trusting 'done', and put `msg` in
-- front of the player when it is not empty -- resuming is an action only they can take.
-- A copy: the caller may keep it. Cheap enough to call every frame (no request, no allocation beyond the table).
function M.areaStatus(id)
  local c = core()
  local st = c and c.areasState and c.areasState()
  if type(st) ~= 'table' or type(st.areas) ~= 'table' or id == nil then return nil end
  for _, a in ipairs(st.areas) do
    if type(a) == 'table' and a.id == id then
      local layers = nil
      if type(a.layers) == 'table' then
        layers = {}
        for i, name in ipairs(a.layers) do layers[i] = name end
      end
      return { id = a.id, name = type(a.name) == 'string' and a.name or nil,
               state = type(a.state) == 'string' and a.state or nil,
               done = tonumber(a.done) or 0, total = tonumber(a.total) or 0,
               unavailable = tonumber(a.unavailable) or 0,
               msg = type(a.msg) == 'string' and a.msg ~= '' and a.msg or nil,
               mode = type(a.mode) == 'string' and a.mode or nil, half = tonumber(a.half), layers = layers }
    end
  end
  return nil
end

local function areaCall(r, result)
  local ok, err = pcall(r.fn, result)
  if not ok and log then log('W', 'beamngEarth', 'api: an area callback failed: ' .. tostring(err)) end
end

-- (ours: core.lua hands the app's area_route_plan_result here)
function M._onAreaPlan(msg)
  local r = type(msg) == 'table' and areaReqs[msg.id]
  if not r then return end
  areaReqs[msg.id] = nil                  -- (one answer a request: nothing follows it)
  local layers = nil
  if type(msg.layers) == 'table' then
    layers = {}
    for i, name in ipairs(msg.layers) do layers[i] = name end
  end
  areaCall(r, { ok = msg.ok == true, km = tonumber(msg.km), km2 = tonumber(msg.km2), items = tonumber(msg.items),
                missing = tonumber(msg.missing), downloadBytes = tonumber(msg.downloadBytes),
                diskBytes = tonumber(msg.diskBytes), seconds = tonumber(msg.seconds), days = tonumber(msg.days),
                hosts = type(msg.hosts) == 'table' and msg.hosts or nil,
                excluded = type(msg.excluded) == 'table' and msg.excluded or nil, layers = layers,
                error = type(msg.error) == 'string' and msg.error or nil })
end

-- (ours: ... and the app's area_route_add_result here)
function M._onAreaAdd(msg)
  local r = type(msg) == 'table' and areaReqs[msg.id]
  if not r then return end
  areaReqs[msg.id] = nil
  local id = type(msg.area) == 'string' and msg.area or nil
  areaCall(r, { ok = msg.ok == true and id ~= nil, id = id,
                error = type(msg.error) == 'string' and msg.error or nil })
end

local function areaTimeouts()
  local late = {}
  for id, r in pairs(areaReqs) do
    if clock - r.t > M.AREA_TIMEOUT_S then late[#late + 1] = id end
  end
  -- (after the walk, as placesTimeouts is: a callback may ask again, and a key added during pairs() is undefined)
  table.sort(late)
  for _, id in ipairs(late) do
    local r = areaReqs[id]
    areaReqs[id] = nil
    -- (an areaAdd that timed out may still have been made: the area list is the truth, not this)
    areaCall(r, { ok = false, error = 'timed out' })
  end
end

------------------------------------------------------------------------------------------
-- events

local listeners, nextHandle = {}, 0

function M.on(event, fn)
  if not EVENTS[event] or type(fn) ~= 'function' then return nil end
  nextHandle = nextHandle + 1
  listeners[nextHandle] = { event = event, fn = fn }
  return nextHandle
end

function M.off(handle) listeners[handle] = nil end

-- { key = value } of add-on id's options (its Options tab), or nil (no such add-on, or the list not come yet)
function M.addonOptions(id)
  local addons = mod('addons')
  return addons and type(addons.optionValues) == 'function' and addons.optionValues(id) or nil
end

-- (ours: the mod's modules say when something happens; gps.lua at arrival)
function M._event(event, info)
  local hook = EVENTS[event]
  if not hook then return end
  info = type(info) == 'table' and info or {}
  for _, l in pairs(listeners) do
    if l.event == event then
      local ok, err = pcall(l.fn, info)
      if not ok and log then log('W', 'beamngEarth', 'api: an ' .. event .. ' listener failed: ' .. tostring(err)) end
    end
  end
  if extensions and extensions.hook then pcall(extensions.hook, hook, info) end
end

local wasReady, teleporting = false, false

function M.onUpdate(dtReal)
  -- (the one clock both request kinds date themselves by: carried forward only while something is in flight, so a
  --  request made after a long quiet spell is not already late)
  if next(placeReqs) or next(graphReqs) or next(areaReqs) then clock = clock + (tonumber(dtReal) or 0) end
  if next(placeReqs) then placesTimeouts() end
  if next(graphReqs) then roadGraphTimeouts() end
  if next(areaReqs) then areaTimeouts() end
  local st = state()
  if not st then wasReady, teleporting = false, false return end
  if st.pending and st.pending.purpose == 'teleport' then teleporting = true end
  local ready = M.ready()
  if ready and not wasReady then
    local lat, lon = M.position()
    if teleporting then M._event('teleported', { lat = lat, lon = lon }) end
    M._event('worldReady', { lat = lat, lon = lon })
    teleporting = false
  end
  wasReady = ready
end

function M._reset()
  listeners, wasReady, teleporting, placeReqs, clock = {}, false, false, {}, 0
  graphReqs = {}
  areaReqs = {}
end

return M
