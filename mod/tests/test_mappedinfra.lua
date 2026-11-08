-- Audited asset references, fitted wire endpoints and actual scene ownership contracts.
-- Scene stubs do not verify GPU rendering in BeamNG.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local m = require('beamngEarth/mappedinfra')
local function count() return #S.liveOf('TSStatic') end
local warnings, collision = 0, 0
log = function(level) if level == 'W' then warnings = warnings + 1 end end
m.onCollide = function() collision = collision + 1 end
local function row(id, asset)
  return { id = id or 'fixture:pole', asset = asset or 'pole', x = 10, y = 20, z = 3, yaw = 90, scale = 1 }
end
T.check(m.ASSETS.mast.shape == '/levels/italy/art/shapes/buildings/italy_bld_antenna.dae', 'installed Italy spelling')
T.check(m.ASSETS.tower.shape == '/levels/west_coast_usa/art/shapes/buildings/antenna.dae', 'verified antenna model')
T.check(m.ASSETS.pylon.shape == '/levels/automation_test_track/art/shapes/objects/pylon.dae', 'existing transmission pylon model')
T.check(m.ASSETS.celltower.shape == '/levels/johnson_valley/art/shapes/misc/celltower.dae', 'stock mobile phone tower')
T.check(m.ASSETS.pole.shape == '/art/shapes/common/power_lines_procedural/electric_pole_wood_old_01.dae',
  'installed electric pole spelling, no Automation Test Track replacement')
T.check(m.ASSETS.wire30.span == 30 and m.ASSETS.wire40.span == 40, 'both installed endpoint configurations')

-- Invert BeamNG's quaternion and rotate a local vector with the usual active formula.
local function rotate(q, x, y, z)
  local qx, qy, qz, w = -q[1], -q[2], -q[3], q[4]
  local tx, ty, tz = 2 * (qy * z - qz * y), 2 * (qz * x - qx * z), 2 * (qx * y - qy * x)
  return x + w * tx + qy * tz - qz * ty, y + w * ty + qz * tx - qx * tz,
    z + w * tz + qx * ty - qy * tx
end
for _, asset in ipairs({ 'wire30', 'wire40' }) do
  for _, delta in ipairs({ { 30, 0, 0 }, { 40, 0, 4 }, { -33, 17, -9 }, { 0, -150, 11 }, { 22, 18, 0 } }) do
    local r = row('fixture:wire', asset)
    r['end'] = { r.x + delta[1], r.y + delta[2], r.z + delta[3] }
    local scale, q = m.pose(r)
    local x, y, z = rotate(q, m.ASSETS[asset].span * scale[1], 0, 0)
    T.near(x, delta[1], 1e-8, 'wire endpoint x follows actual spacing')
    T.near(y, delta[2], 1e-8, 'wire endpoint y follows actual spacing')
    T.near(z, delta[3], 1e-8, 'wire endpoint z follows actual insulator elevation')
    T.check(scale[2] == 1 and scale[3] == 1, 'wire width and stock sag are retained')
  end
end
for _, asset in ipairs({ 'mast', 'tower', 'pole', 'celltower' }) do
  local r = row('fixture:' .. asset, asset); r.scale = 4.2
  local scale, q = m.pose(r)
  local x, y, z = rotate(q, 0, 0, 1)
  T.check(x == 0 and y == 0 and z == 1, 'structure remains upright, independent of terrain normal')
  T.check(scale[1] == 4.2 and scale[2] == 4.2 and scale[3] == 4.2, 'mapped height has no vegetation scale cap')
end

local r = row()
loadJsonMaterialsFile = function() T.check(count() == 0, 'materials load before their first shape') end
m.onRows('a:old', { r, r })
T.check(count() == 1, 'repeated source ID creates one scene object')
local o = S.liveOf('TSStatic')[1]
local firstID, firstName = o.id, o.name
T.check(firstName == 'bngEarthMapped_fixture_pole', 'deterministic scene identifier')
T.check(o.fields.collisionType == 'Collision Mesh', 'poles use their stock collision mesh')
m.onRows('b:new', { r })
T.check(count() == 1 and S.liveOf('TSStatic')[1].id == firstID, 'replacement owner shares existing scene object')
m.transform({ apply = function(_, x, y, z) return -y + 100, x + 200, z + 10 end })
T.near(S.liveOf('TSStatic')[1].pos.x, 80, 1e-8, 'rebase transforms scene position')
m.dropKey('a:old')
T.check(count() == 1, 'retiring tile cannot delete replacement owner')
T.near(S.liveOf('TSStatic')[1].pos.x, 80, 1e-8, 'all retained owners use rebased position')
m.onRows('b:new', {})
T.check(count() == 0, 'empty replacement unloads stale infrastructure')
m.onRows('reload', { r })
T.check(count() == 1 and S.liveOf('TSStatic')[1].name == firstName, 'reload preserves deterministic object name')
T.near(S.liveOf('TSStatic')[1].pos.z, r.z, 1e-8, 'reload preserves sampled terrain placement')
m.reset()
T.check(count() == 0 and collision > 0, 'reset releases collision through existing scheduler')

m.onRows('mobile', { row('fixture:mobile', 'celltower') })
T.check(count() == 1 and S.liveOf('TSStatic')[1].fields.shapeName == m.ASSETS.celltower.shape,
  'mobile tag record instantiates the installed cell phone tower mesh')
T.check(S.liveOf('TSStatic')[1].fields.collisionType == 'Collision Mesh', 'cell phone tower keeps stock collision')
m.onRows('mobile:new', { row('fixture:mobile', 'celltower') })
m.dropKey('mobile')
T.check(count() == 1, 'mobile tower preserves shared ownership on tile replacement')
m.reset()

-- Wire ownership keeps a pylon visible beyond the ordinary prop tile/culling radius.
local farPylon = row('fixture:required-pylon', 'pylon')
farPylon.x, farPylon.collide = 2200, false
m.onRows('a:wire-owner', { farPylon })
T.check(count() == 1 and S.liveOf('TSStatic')[1].fields.collisionType == 'None',
  'wire-only support stays visible without a far collider')
T.near(S.liveOf('TSStatic')[1].pos.x, 2200, 1e-8, 'required support has no independent Forest distance cutoff')
local supportID = S.liveOf('TSStatic')[1].id
m.onRows('b:other-span', { farPylon })
T.check(count() == 1, 'adjacent wire spans share one pylon object')
local ownPylon = row('fixture:required-pylon', 'pylon')
ownPylon.x, ownPylon.collide = 2200, true
m.onRows('z:base-owner', { ownPylon })
T.check(count() == 1 and S.liveOf('TSStatic')[1].id == supportID, 'base ownership retains the same visible pylon')
T.check(S.liveOf('TSStatic')[1].fields.collisionType == 'Collision Mesh', 'safe base owner enables stock collision')
m.dropKey('z:base-owner')
T.check(count() == 1 and S.liveOf('TSStatic')[1].fields.collisionType == 'None',
  'unloading the base tile keeps the wire-owned pylon visible')
m.dropKey('a:wire-owner')
T.check(count() == 1, 'remaining incident span retains its required support')
m.transform({ apply = function(_, x, y, z) return x - 2000, y, z end })
T.near(S.liveOf('TSStatic')[1].pos.x, 200, 1e-8, 'wire-owned pylon follows the floating origin')
m.dropKey('b:other-span')
T.check(count() == 0, 'last incident span unload removes its support')

loadJsonMaterialsFile = function() end
local w = row('fixture:wire', 'wire40'); w['end'] = { 70, 25, 8 }
local endx = w['end'][1]
m.onRows('wire:old', { w })
T.check(count() == 1 and S.liveOf('TSStatic')[1].fields.collisionType == 'None', 'wire mesh is render-only')
m.onRows('wire:new', { w })
m.transform({ apply = function(_, x, y, z) return x + 100, y - 50, z + 2 end })
m.dropKey('wire:old')
T.check(count() == 1 and w['end'][1] == endx, 'rebase preserves source records and surviving wire ownership')
T.near(S.liveOf('TSStatic')[1].pos.x, w.x + 100, 1e-8, 'rebased wire starts at shifted attachment')
m.reset()

-- Missing meshes/materials warn once, skip instead of substituting, recover on a later delivery.
for _, missing in ipairs({ m.ASSETS.mast.shape, m.ASSETS.celltower.shape, m.ASSETS.pole.shape, m.ASSETS.pole.materials[2] }) do
  local asset = missing == m.ASSETS.mast.shape and 'mast' or (missing == m.ASSETS.celltower.shape and 'celltower' or 'pole')
  FS.fileExists = function(_, path) return path ~= missing end
  local before = warnings
  m.onRows('missing', { row('fixture:missing', asset) })
  m.onRows('missing', { row('fixture:missing', asset) })
  T.check(count() == 0 and m.status().unavailable == 1, 'unavailable installed resource has no placeholder')
  T.check(warnings == before + 1, 'unavailable resource warns once')
  m.transform({ apply = function(_, x, y, z) return x + 1, y, z end })
  T.check(count() == 0, 'rebase does not load unavailable resource')
  FS.fileExists = function() return true end
  m.onRows('missing', { row('fixture:missing', asset) })
  T.check(count() == 1, 'later tile delivery retries available resources')
  m.reset()
end
FS.fileExists = function(_, path) return path ~= m.ASSETS.pole.shape end
m.onRows('missing-pole', { w })
T.check(count() == 0, 'missing pole mesh also suppresses floating wires')
m.reset(); FS.fileExists = function() return true end
loadJsonMaterialsFile = function() error('isolated material failure') end
m.onRows('broken', { r })
T.check(count() == 0, 'material load errors are contained')
m.reset(); loadJsonMaterialsFile = function() end

local invalid = row('fixture:invalid'); invalid.x = 0 / 0
m.onRows('invalid', { invalid, { id = 'fixture:unknown', asset = 'unlisted', x = 0, y = 0, z = 0, scale = 1, yaw = 0 } })
T.check(count() == 0, 'invalid coordinates and unknown model keys are rejected')

local roads = require('beamngEarth/roads')
local pl = { r = { 1, 0, 0, 0, 1, 0, 0, 0, 1 }, t = { 100, 200, 10 }, c = { 0, 0, 0 }, pz = 0 }
local mapped = roads.mapInfrastructure(pl, { w })[1]
T.near(mapped.x, w.x + 100, 1e-8, 'geotile placement maps wire start')
T.near(mapped['end'][1], w['end'][1] + 100, 1e-8, 'geotile placement maps wire end')
T.check(mapped.id == w.id and mapped.asset == w.asset and w['end'][1] == endx, 'placement retains identity and immutable input')
m.reset()
T.done('test_mappedinfra')
