-- Mapped boundaries use the ordinary streamed road-tile lifecycle, including split and render-only meshes.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end
local roads = require('beamngEarth/roads')
local gov = { try = function() return true end, charge = function() end, chargeMotion = function() end }
local function update() for i = 1, 10 do roads.update(gov, 2) end end
local function statics() return S.liveOf('TSStatic') end
files['/t/b.json'] = '{ chunks = {}, boundary = { shape = "/t/b.cdae", pos = { 0, 0, 0 } }, boundary2 = { shape = "/t/b2.cdae", pos = { 0, 0, 0 } }, hedge = { shape = "/t/h.cdae", pos = { 0, 0, 0 } } }'
local function tile()
  T.check(roads.onTile({ anchorId = 2, key = '0_0', path = '/t/b.json', n = 0 }, 2), 'boundary-only tile accepted')
  update()
end
tile()
T.check(#statics() == 3, 'all solid and hedge parts made')
for _, o in ipairs(statics()) do
  local hedge = o.fields.shapeName == '/t/h.cdae'
  T.check(o.fields.collisionType == (hedge and 'None' or 'Visible Mesh Final'), 'solid boundaries; render-only hedges')
end
roads.transform({ apply = function(_, x, y, z) return x, y - 20, z end,
  beamngQuat = function() return 0, 0, 0, 1 end },
  { quatMul = function(ax, ay, az, aw) return ax, ay, az, aw end })
for _, o in ipairs(statics()) do T.check(o.pos.y == -20, 'all parts move on a rebase') end
roads.clear()
roads.reset()
T.check(#statics() == 0, 'clear removes ghost boundaries before level reset')
tile()
roads.onDrop({ anchorId = 2, key = '0_0' })
update()
T.check(#statics() == 0, 'drop removes solid and render-only parts')
tile()
roads.setEnabled(false)
T.check(#statics() == 0, 'settings off removes all boundaries')
roads.setEnabled(true)
files['/t/old.json'] = '{ chunks = {} }'
T.check(roads.onTile({ anchorId = 2, key = '0_0', path = '/t/old.json', n = 0 }, 2), 'old tile without boundary fields accepted')
update()
T.check(#statics() == 0, 'absent optional fields make no objects')
roads.clear()
-- A fast approach makes solid boundary parts ahead of the ordinary stationary collision radius.
local collideCheck = roads.COLLIDE_CHECK_S
roads.COLLIDE_CHECK_S = 0
getPlayerVehicle = function() return {
  getPosition = function() return vec3(0, 128, 0) end,
  getVelocity = function() return vec3(100, 0, 0) end,
  getID = function() return 1 end
} end
roads.update(gov, 2)
files['/t/ahead.json'] = files['/t/b.json']:gsub('pos = { 0, 0, 0 }', 'pos = { 1024, 0, 0 }')
T.check(roads.onTile({ anchorId = 2, key = '4_0', path = '/t/ahead.json', n = 0 }, 2), 'ahead boundary tile accepted')
update()
T.check(#statics() == 3, 'solid boundary parts made ahead at 100 m/s')
roads.clear()
roads.COLLIDE_CHECK_S = collideCheck
getPlayerVehicle = nil
-- New geotile content owns these same parts, with render-only foliage and collision-only solids.
local geodesy = require('beamngEarth/geodesy')
local gt = require('beamngEarth/geotile')
local gx, gy = gt.tileOf(39.2834, -76.5934)
local key = gt.key(gx, gy)
local lat, lon = gt.centre(17, gx, gy)
local a = geodesy.newAnchor(lat, lon)
roads.setGeo(true)
roads.setSession('boundary-test')
files['/t/geo.json'] = string.format([[{ v=1, key="%s", frame={%.10f,%.10f}, h="boundaries",
 boundary={shape="/t/g_boundary.cdae",pos={0,0,0},pivot=0,relief=2},
 boundary2={shape="/t/g_boundary2.cdae",pos={0,0,0},pivot=0,relief=2},
 hedge={shape="/t/g_hedge.cdae",pos={0,0,0},pivot=0,relief=2},
 colliders={boundary={{-10,-10,0,10,10,2}},boundary2={{-10,-10,0,10,10,2}}} }]], key, lat, lon)
T.check(roads.onTile({anchorId=1,key=key,path='/t/geo.json',frame={lat,lon},h='boundaries'},1,a), 'boundary geotile accepted')
for _=1,8 do roads.update(gov,1,nil,nil,a) end
T.check(#statics()==3,'geotile makes each boundary part exactly once')
for _,o in ipairs(statics()) do
 local leaf=o.fields.shapeName=='/t/g_hedge.cdae'
 T.check(o.fields.collisionType==(leaf and 'None' or 'Visible Mesh Final'),'geotile preserves solid/foliage collision split')
end
local b=geodesy.newAnchor(lat,lon+.02)
roads.replace(b,2)
T.check(#statics()==3,'geotile rebase retains the three objects')
roads.onDrop({key=key})
for _=1,8 do roads.update(gov,2,nil,nil,b) end
T.check(#statics()==0,'geotile drop removes solids and foliage')
roads.clear()
local trees = require('beamngEarth/trees')
for _, group in ipairs({'chainlink','noisewall'}) do
  for _, height in ipairs({0.1, 0.5, 1.76, 6, 10}) do
    local _, scale = trees.pick(group, height, 0)
    T.near(scale * trees.CATALOG[group][1][2], height, 1e-9, 'mapped panel height is not clamped')
  end
end
T.done('test_barriers')
