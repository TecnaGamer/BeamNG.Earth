-- Far geotile records map exactly, share stock objects with road owners, and clean up on drop/new session.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local geo = require('beamngEarth/geotile')
local renderer = require('beamngEarth/windturbines')
local stream = require('beamngEarth/turbinestream')
local lat, lon = 41.1397814, -104.9978882
local gx, gy = geo.tileOf(lat, lon, 15)
local key = geo.key(gx, gy, 15)
local clat, clon = geo.centre(15, gx, gy)
local sourceFrame = geo.frameOf({ frame = { clat, clon } })
local a = geodesy.newAnchor(lat, lon + 0.1)
local b = geodesy.newAnchor(lat, lon)
local r = { id = 'osm:node:1', x = 20, y = 30, z = 1800, yaw = 45, scale = 1, collide = true }
local msg = { key = key, anchorId = 2, frame = { clat, clon }, pivot = 1800,
  data = { sid = 's1', turbines = { r } } }
local function tower()
  for _, o in ipairs(S.liveOf('TSStatic')) do
    if o.fields.shapeName:find('turbine_tower', 1, true) then return o end
  end
end

T.check(stream.onTile(msg, nil, 's1'), 'tile arriving before terrain is retained')
T.check(renderer.status().objects == 0 and stream.status().tiles == 1, 'deferred without an anchor')
stream.reanchor(a)
T.check(renderer.status().objects == 2 and renderer.status().owners == 1, 'one far tower and rotor')
local original = tower()
local x, y, z = geo.worldMap(sourceFrame, a, r.x, r.y, r.z)
T.near(original.pos.x, x, 1e-3, 'far mapped x')
T.near(original.pos.y, y, 1e-3, 'far mapped y')
T.near(original.pos.z, z, 1e-3, 'far mapped z')
T.check(original.fields.collisionType == 'None', 'far transport enforces render-only parts')
T.check(stream.onTile(msg, a, 's1'), 'same-session future-anchor delivery mapped into the active frame')
T.check(tower() == original and renderer.status().owners == 1, 'duplicate message retains the same parts and owner')
stream.reanchor(b)
x, y, z = geo.worldMap(sourceFrame, b, r.x, r.y, r.z)
T.near(original.pos.x, x, 1e-3, 'rebase mapped x')
T.near(original.pos.y, y, 1e-3, 'rebase mapped y')
T.near(original.pos.z, z, 1e-3, 'rebase mapped z')
T.check(r.x == 20 and r.y == 30 and r.z == 1800 and r.collide == true, 'source content never mutated')

renderer.onRows('near-road', { { id = r.id, x = x, y = y, z = z + 4, yaw = 20, scale = 1 } })
stream.onTile(msg, b, 's1')
T.check(tower() == original and renderer.status().objects == 2, 'near and far records share one assembly')
T.near(original.pos.z, z + 4, 1e-3, 'near graded installation height wins over a later far message')
T.check(original.fields.collisionType == 'Collision Mesh', 'near owner enables collision')
stream.reset()
T.check(renderer.status().objects == 2 and renderer.status().owners == 1 and stream.status().tiles == 0,
  'new-session reset releases only far owners')
stream.onTile(msg, b, 's1')
renderer.dropKey('near-road')
T.near(original.pos.z, z, 1e-3, 'dropping road ownership restores far placement')
T.check(original.fields.collisionType == 'None', 'far-only again: collision disabled')
stream.onDrop({ key = key })
T.check(renderer.status().objects == 0 and stream.status().tiles == 0, 'far drop deletes its last-owned parts')
T.check(not stream.onTile({ key = 'broken', data = {} }, b, 's1'), 'malformed tile rejected')
T.check(not stream.onTile(msg, b, 's2') and renderer.status().objects == 0,
  'late previous-session tile cannot recreate objects after reset or drop')
stream.onTile(msg, b, 's1')
stream.onTile({ key = key, frame = msg.frame, data = { sid = 's1', turbines = {} } }, b, 's1')
T.check(renderer.status().objects == 0 and renderer.status().owners == 0, 'empty replacement removes stale IDs')
stream.reset()
renderer.reset()
T.done('test_turbinestream')
