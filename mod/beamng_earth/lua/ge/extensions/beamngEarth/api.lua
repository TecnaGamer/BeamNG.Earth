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
local M = {}

M.MAJOR, M.MINOR = 1, 8
local FEATURES = { pose = true, place = true, teleport = true, gps = true, notify = true, events = true, options = true,
  gpsRoute = true, inTunnel = true, weather = true, places = true, buildings = true, speedLimit = true,
  routeGeometry = true, transform = true, routeSteps = true, rebase = true, cachedPlaces = true }
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

local function placesTimeouts(dt)
  clock = clock + (tonumber(dt) or 0)
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
  if next(placeReqs) then placesTimeouts(dtReal) end
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

function M._reset() listeners, wasReady, teleporting, placeReqs, clock = {}, false, false, {}, 0 end

return M
