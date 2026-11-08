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
--   onBeamNGEarthAddonOptions(info)   an add-on's options were saved on its Options tab: info.id, info.values
-- or with api.on(event, fn) ('worldReady', 'teleported', 'routeArrived', 'addonOptions'), which returns a handle for
-- api.off(handle).
--
-- Options (1.1, has('options')): an add-on declares them in its beamngEarth_addon.json ({"options": [{"key": "port",
-- "label": "Port", "type": "int", "default": 47700, "min": 1024, "max": 65535}]}, docs/ADDONS.md); the player sets them
-- on its Options tab; api.addonOptions(id) is { key = value } (the defaults until then), or nil before the list came.
-- Pure reads are cheap (no allocation beyond the returned table); nothing here blocks or talks to the network itself.
local M = {}

M.MAJOR, M.MINOR = 1, 1
local FEATURES = { pose = true, place = true, teleport = true, gps = true, notify = true, events = true, options = true }
local EVENTS = { worldReady = 'onBeamNGEarthWorldReady', teleported = 'onBeamNGEarthTeleported',
  routeArrived = 'onBeamNGEarthRouteArrived', addonOptions = 'onBeamNGEarthAddonOptions' }

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
-- Whether this API has a feature: 'pose', 'place', 'teleport', 'gps', 'notify', 'events'.
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

function M.gpsClear()
  local gps = mod('gps')
  if gps then gps.clear() end
end

-- The GPS destination and the way there: { lat, lon, name, remaining (m), eta (s), planning }, or nil without one.
function M.gps()
  local gps = mod('gps')
  local s = gps and gps.status and gps.status()
  if type(s) ~= 'table' then return nil end
  return { lat = s.lat, lon = s.lon, name = s.name, remaining = s.remaining, eta = s.eta, planning = s.planning == true }
end

-- A short message on the screen (the game's own messages), seconds (default 5).
function M.notify(text, seconds)
  if type(text) ~= 'string' or text == '' then return false end
  if ui_message then pcall(ui_message, text:sub(1, 300), tonumber(seconds) or 5, 'beamngEarthApi') return true end
  return false
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

function M.onUpdate()
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

function M._reset() listeners, wasReady, teleporting = {}, false, false end

return M
