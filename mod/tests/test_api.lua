-- luajit mod/tests/test_api.lua  (the modding API for other mods, api.lua: beamngEarth_api; docs/ADDONS.md)
-- version / has; ready only in our level with the world there (a teleport under way: not; a rebase: still ready);
-- the pose (lat / lon / alt / heading from true north / speed / east-north-up velocity) from core's player and anchor,
-- also off the origin where the frame's north turns; the place names; teleport / GPS through places.lua and gps.lua;
-- notify; the events as game hooks and listeners (worldReady after loading, teleported after a teleport only,
-- routeArrived from gps.lua), a failing listener contained.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')

local connected = true
package.loaded['beamngEarth/net'] = { isConnected = function() return connected end, send = function() end }
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
package.loaded['beamngEarth/gps'] = {
  setDestination = function(lat, lon, name) dest = { lat = lat, lon = lon, name = name } return true end,
  clear = function() dest = nil end,
  status = function() return dest and { lat = dest.lat, lon = dest.lon, name = dest.name, remaining = 1200, eta = 90 } end,
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

T.check(api.version() == 1 and select(2, api.version()) == 1, 'version 1.1 (options)')
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

T.check(api.has('options') and select(2, api.version()) >= 1, 'options: has() and minor version 1')
package.loaded['beamngEarth/addons'] = { optionValues = function(id) return id == 'x' and { port = 1 } or nil end }
T.check(api.addonOptions('x').port == 1 and api.addonOptions('y') == nil, 'addonOptions from the add-ons list')
api._event('addonOptions', { id = 'x', values = { port = 2 } })
T.check(hooks[#hooks][1] == 'onBeamNGEarthAddonOptions' and hooks[#hooks][2].values.port == 2, 'options hook')
package.loaded['beamngEarth/addons'] = nil

T.done('test_api')
