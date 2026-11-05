-- Existing road tile delivery through the real turbine renderer: one ID, exact placement, rebase, reload and drop.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')
local roads = require('beamngEarth/roads')
local turbines = require('beamngEarth/windturbines')
turbines.onCollide = roads.markCollisionDirty
roads.setGeo(true)
roads.setSession('isolated-turbine-test')

local lat, lon = 41.1397814, -104.9978882
local gx, gy = geotile.tileOf(lat, lon)
local key = geotile.key(gx, gy)
local clat, clon = geotile.centre(17, gx, gy)
local frame = geotile.frameOf({ frame = { clat, clon } })
local A = geodesy.newAnchor(lat, lon + 0.15)
local B = geodesy.newAnchor(lat, lon)
local rec = { id = 'osm:node:1175681635', x = 8, y = 9, z = 1800, yaw = 0, scale = 1,
  lat = lat, lon = lon, dimensions = { basis = 'model_default' } }
local data = { key = key, frame = { clat, clon }, h = 'with-turbine', turbines = { rec } }
local function msg(aid, contents)
  return { anchorId = aid, key = key, n = 0, frame = { clat, clon }, h = contents.h, data = contents }
end
local function tower()
  for _, o in ipairs(S.liveOf('TSStatic')) do
    if o.fields.shapeName:find('turbine_tower', 1, true) then return o end
  end
end

T.check(roads.onTile(msg(1, data), 1, A), 'road geotile accepted')
T.check(turbines.status().turbines == 1 and turbines.status().objects == 2 and turbines.status().owners == 1,
  'one turbine with two installed stock parts')
local originalTower = tower()
local ex, ey, ez = geotile.worldMap(frame, A, rec.x, rec.y, rec.z)
T.near(originalTower.pos.x, ex, 1e-3, 'initial base x')
T.near(originalTower.pos.y, ey, 1e-3, 'initial base y')
T.near(originalTower.pos.z, ez, 1e-3, 'initial base z')
T.check(roads.onTile(msg(1, data), 1, A) and tower() == originalTower,
  'same tile delivery keeps its existing tower')

roads.replace(B, 2)
ex, ey, ez = geotile.worldMap(frame, B, rec.x, rec.y, rec.z)
T.check(tower() == originalTower and turbines.status().objects == 2, 'rebase keeps the same two objects')
T.near(originalTower.pos.x, ex, 1e-3, 'rebased base x')
T.near(originalTower.pos.y, ey, 1e-3, 'rebased base y')
T.near(originalTower.pos.z, ez, 1e-3, 'rebased base z')
T.check(originalTower.rot.x == 0 and originalTower.rot.y == 0, 'tower stays upright')
T.check(rec.x == 8 and rec.y == 9 and rec.z == 1800 and rec.yaw == 0,
  'placement leaves decoded content unchanged')

roads.newSession()
roads.onTile(msg(2, data), 2, B)
T.check(tower() == originalTower and turbines.status().owners == 1,
  'server reconnect with same hash keeps its tower and single owner')

local empty = { key = key, frame = data.frame, h = 'removed-turbine' }
roads.onTile(msg(2, empty), 2, B)
T.check(turbines.status().objects == 0 and turbines.status().owners == 0,
  'replacement content without a turbine deletes its objects and owner')
roads.onTile(msg(2, data), 2, B)
T.check(turbines.status().objects == 2, 'returning tile makes one turbine again')
roads.onDrop({ anchorId = 2, key = key })
T.check(turbines.status().objects == 0 and turbines.status().turbines == 0,
  'road drop cleans both parts and identity')
roads.reset()
turbines.reset()
T.done('test_windturbine_streaming')
