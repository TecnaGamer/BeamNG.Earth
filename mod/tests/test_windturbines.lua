-- luajit mod/tests/test_windturbines.lua: stock ETK assembly, OSM ownership, tile reload/rebase, and resource failures.
-- Scene stubs verify our renderer contract; stock asset rendering and ambient playback still require in-game testing.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local wt = require('beamngEarth/windturbines')
local collided, warnings = 0, 0
wt.onCollide = function() collided = collided + 1 end
log = function(level) if level == 'W' then warnings = warnings + 1 end end

local function row(id, x, y, z, yaw, scale)
  return { id = id or 'osm:node:1', x = x or 10, y = y or 20, z = z or 1600, yaw = yaw or 0, scale = scale or 1,
    lat = 41.14, lon = -105.0, dimensions = { basis = 'model_default' } }
end
local function parts()
  local out = {}
  for _, o in ipairs(S.liveOf('TSStatic')) do out[o.fields.shapeName] = o end
  return out[wt.PARTS[1].shape], out[wt.PARTS[2].shape]
end
local function count() return #S.liveOf('TSStatic') end

T.near(wt.MODEL.hub, 101.410286, 1e-8, 'hub offset measured from stock prefab')
T.near(wt.MODEL.rotorDiameter, 69.158482, 1e-6, 'model rotor diameter, not an invented real turbine measurement')
local r = row()
local pp = wt.partPositions(r)
T.near(pp[1][3], 1600, 1e-8, 'stock tower installation origin is terrain grade')
T.near(pp[2][1], 10, 1e-8, 'rotor shares stock tower x')
T.near(pp[2][2], 16.47972655, 1e-8, 'stock rotor forward offset')
T.near(pp[2][3], 1701.410286, 1e-8, 'stock rotor hub height')
pp = wt.partPositions(row(nil, 10, 20, 1600, 90, 2))
T.near(pp[2][1], 17.0405469, 1e-8, 'yaw rotates the scaled rotor offset')
T.near(pp[2][2], 20, 1e-8, 'yaw rotates about upright tower')
T.near(pp[2][3], 1802.820572, 1e-8, 'uniform scale includes assembly offsets')

local loaded = {}
loadJsonMaterialsFile = function(path)
  T.check(count() == 0, 'stock materials loaded before any shape is registered')
  loaded[#loaded + 1] = path
end
wt.onRows('old:0_0', { r, row(nil, 999) })
local tower, rotor = parts()
T.check(count() == 2 and wt.status().turbines == 1, 'one OSM ID creates one two-part assembly')
T.check(wt.status().items[r.id].lat == r.lat and wt.status().items[r.id].loaded,
  'diagnostics retain the geographic identity and loaded state')
T.check(#loaded == 2 and loaded[1] == wt.MATERIAL_FILES[1] and loaded[2] == wt.MATERIAL_FILES[2],
  'global vertex-color and plastic-trim stock material files are loaded')
T.near(tower.pos.x, 10, 1e-8, 'first duplicate record wins')
T.check(rotor.fields.playAmbient == '1' and tower.fields.playAmbient == '0', 'engine animates rotor ambient sequence')
T.check(tower.fields.collisionType == 'Collision Mesh' and rotor.fields.collisionType == 'Collision Mesh',
  'stock collision meshes, not visible triangle collision')
T.check(not tower.hidden and tower.fields.isRenderEnabled ~= 'false', 'tower is rendered despite stock prefab legacy field')
T.check(collided == 1, 'assembly requests one batched collision rebuild')

local towerId, rotorId = tower.id, rotor.id
wt.onRows('old:0_0', { r })
wt.onRows('new:0_0', { r })
T.check(count() == 2 and parts().id == towerId, 'duplicate deliveries and overlapping tile owners preserve scene IDs')
T.check(wt.status().owners == 2 and collided == 1, 'new owner adds no duplicate collision request')
wt.dropKey('old:0_0')
T.check(count() == 2 and wt.status().owners == 1, 'assembly survives while another tile still owns it')

local scaled = row(nil, 10, 20, 1600, 90, 2)
wt.placeRows('new:0_0', { scaled })
tower, rotor = parts()
T.check(tower.id == towerId and rotor.id == rotorId, 'placement update moves existing objects')
T.near(rotor.pos.x, 17.0405469, 1e-8, 'renderer places scaled rotated rotor')
T.near(tower.scale.z, 2, 1e-8, 'tower uses uniform scale')
T.near(tower.rot.x, 0, 1e-8, 'tower has no roll')
T.near(tower.rot.y, 0, 1e-8, 'tower has no pitch')
T.near(tower.rot.z, -math.sqrt(0.5), 1e-8, 'counter-clockwise yaw uses BeamNG quaternion convention')

-- Rotate the frame by +90 degrees and translate it, including a small change of base elevation.
wt.transform({ apply = function(_, x, y, z) return 100 - y, 200 + x, z + 3 end })
tower, rotor = parts()
T.near(tower.pos.x, 80, 1e-8, 'rebase transforms installation x')
T.near(tower.pos.y, 210, 1e-8, 'rebase transforms installation y')
T.near(tower.pos.z, 1603, 1e-8, 'rebase transforms base elevation once')
T.near(rotor.pos.x, 80, 1e-8, 'rebased rotor shares rotated hub x')
T.near(rotor.pos.y, 217.0405469, 1e-8, 'rebased rotor offset follows new heading')
T.near(rotor.pos.z, 1805.820572, 1e-8, 'rebase preserves upright hub height')
T.near(tower.rot.x, 0, 1e-8, 'rebase keeps tower roll zero')
T.near(tower.rot.y, 0, 1e-8, 'rebase keeps tower pitch zero')
T.near(scaled.x, 10, 1e-8, 'rebase never mutates decoded input record')

local current = row(nil, 80, 210, 1603, 180, 2)
wt.placeRows('new:0_0', { current })
T.check(count() == 2 and parts().id == towerId, 'current-frame tile delivery keeps rebased assembly')
wt.onRows('new:0_0', {})
T.check(count() == 0 and wt.status().turbines == 0, 'empty replacement records remove stale turbine and ownership')
wt.onRows('reload:0_0', { r })
tower, rotor = parts()
T.near(tower.pos.x, 10, 1e-8, 'tile reload restores same base x')
T.near(tower.pos.z, 1600, 1e-8, 'tile reload restores same sampled ground elevation')
T.near(rotor.pos.z, 1701.410286, 1e-8, 'tile reload restores same hub height')
tower:delete()
wt.onRows('reload:0_0', { r })
T.check(count() == 2, 'externally deleted scene part is repaired without leaving duplicate rotor')
wt.reset()
T.check(count() == 0 and wt.status().owners == 0, 'reset deletes parts and releases all owners')

-- Missing installed asset: preserve ownership for a later delivery, warn once, create no placeholders.
local missing = wt.PARTS[2].shape
FS.fileExists = function(_, path) return path ~= missing end
local warningBefore = warnings
wt.onRows('missing:0_0', { r })
wt.onRows('missing:0_0', { r })
T.check(count() == 0 and wt.status().unavailable == 1, 'unavailable mesh skips whole assembly')
T.check(warnings == warningBefore + 1, 'unavailable mesh is logged once across retries')
wt.transform({ apply = function(_, x, y, z) return x + 1, y, z end })
T.check(count() == 0, 'rebase does not instantiate an unavailable assembly')
FS.fileExists = function() return true end
wt.onRows('missing:0_0', { r })
T.check(count() == 2, 'next tile delivery retries an installed asset that becomes available')
wt.reset()

missing = wt.MATERIAL_FILES[2]
FS.fileExists = function(_, path) return path ~= missing end
wt.onRows('materials:0_0', { r })
T.check(count() == 0, 'missing required material file skips whole assembly')
wt.reset()
FS.fileExists = function() return true end
loadJsonMaterialsFile = function() error('isolated material load failure') end
wt.onRows('materials:0_0', { r })
T.check(count() == 0 and wt.status().unavailable == 1, 'material load exception is contained without placeholders')
wt.reset()
loadJsonMaterialsFile = function() return false end
wt.onRows('materials:0_0', { r })
T.check(count() == 0 and wt.status().unavailable == 1, 'explicit material load failure is contained too')
wt.reset()
loadJsonMaterialsFile = function() end

-- A failure after the tower is registered must clean that tower up too.
local baseCreate = createObject
createObject = function(class)
  local o = baseCreate(class)
  if count() == 1 then function o:registerObject() error('isolated rotor registration failure') end end
  return o
end
wt.onRows('broken:0_0', { r })
T.check(count() == 0, 'partial shape failure cleans up the registered tower')
createObject = baseCreate
wt.onRows('broken:0_0', { r })
T.check(count() == 2, 'failed creation can recover on next delivery')
wt.reset()

local noOptional = { id = 'osm:node:2', x = 0, y = 0, z = 0 }
wt.onRows('validation:0_0', { noOptional, { id = 'bad', x = 0 / 0, y = 0, z = 0 },
  row('zero-scale', 0, 0, 0, 0, 0), { id = 'infinite-yaw', x = 0, y = 0, z = 0, yaw = math.huge } })
tower, rotor = parts()
T.check(count() == 2 and wt.status().turbines == 1, 'missing optional dimensions use model defaults; invalid records rejected')
T.near(tower.scale.x, 1, 1e-8, 'missing scale defaults to stock model scale')
T.near(rotor.pos.z, 101.410286, 1e-8, 'no optional height metadata required for documented model hub')
wt.dropKey('validation:0_0')
T.check(count() == 0, 'last owner removes final assembly')

-- Full-farm stress case: far owners render without entering the global collision tree. The assertions count
-- actual work rather than setting an unstable timing threshold on an engine stub.
wt.reset()
local fileChecks, materialLoads = 0, 0
FS.fileExists = function() fileChecks = fileChecks + 1; return true end
loadJsonMaterialsFile = function() materialLoads = materialLoads + 1 end
local farm, near = {}, {}
for i = 1, 300 do
  farm[i] = row('osm:node:' .. (1000 + i), (i % 20) * 300, math.floor(i / 20) * 300, 1600)
  farm[i].collide = false
  if i <= 20 then near[i] = row(farm[i].id, farm[i].x, farm[i].y, 1620, 30, 1.5) end
end
local farmStart = os.clock()
local madeBefore, movedBefore, collisionBefore = S.calls.createObject_TSStatic or 0, S.calls.setPosRot or 0, collided
wt.onRows('wind:a', farm)
local farmStatus = wt.status()
T.check(count() == 600 and farmStatus.turbines == 300 and farmStatus.colliding == 0,
  '300 far turbines produce exactly 600 native scene objects without collision')
T.check((S.calls.createObject_TSStatic or 0) - madeBefore == 600, 'exactly two shape instances per turbine')
T.check((S.calls.setPosRot or 0) - movedBefore == 600, 'one initial transform per native scene object')
T.check(fileChecks == 4 and materialLoads == 2 and farmStatus.resourcesReady,
  'healthy installation resources checked and material files loaded once for entire farm')
T.check(collided == collisionBefore, 'far-only farm creates no global collision rebuild')
local nativeRotor, nativeTower, nativeFields = 0, 0, true
for _, o in ipairs(S.liveOf('TSStatic')) do
  if o.fields.playAmbient == '1' then nativeRotor = nativeRotor + 1 else nativeTower = nativeTower + 1 end
  nativeFields = nativeFields and o.fields.collisionType == 'None' and o.fields.dynamic == 'false' and
    o.fields.forceDetail == '-1'
end
T.check(nativeFields, 'all far shapes use no collision, stock static optimization, and automatic native LOD')
T.check(nativeRotor == 300 and nativeTower == 300 and wt.update == nil,
  '300 native ambient rotors and static towers need no Lua animation tick')
local initialIds = {}
for _, o in ipairs(S.liveOf('TSStatic')) do initialIds[o.id] = true end
madeBefore, movedBefore = S.calls.createObject_TSStatic or 0, S.calls.setPosRot or 0
for _ = 1, 5 do wt.onRows('wind:a', farm); wt.onRows('wind:b', farm) end
T.check(count() == 600 and wt.status().owners == 2, 'repeated overlapping farm deliveries add ownership only')
T.check((S.calls.createObject_TSStatic or 0) == madeBefore and (S.calls.setPosRot or 0) == movedBefore,
  'duplicate farm deliveries perform no imports or scene transforms')
T.check(fileChecks == 4 and materialLoads == 2, 'duplicate farm deliveries repeat no healthy resource work')

wt.onRows('road:near', near)
farmStatus = wt.status()
T.check(count() == 600 and farmStatus.colliding == 20 and collided == collisionBefore + 1,
  '20 near turbines enable dedicated collision with one batched rebuild and no duplicate objects')
T.near(farmStatus.items[farm[1].id].z, 1620, 1e-8, 'near terrain-grade placement takes precedence over far record')
T.near(farmStatus.items[farm[1].id].scale, 1.5, 1e-8, 'near owner controls scale')
T.near(farmStatus.items[farm[1].id].yaw, 30, 1e-8, 'near owner controls heading')
for i = 1, #farm do farm[i].z = 1500 end
wt.onRows('wind:a', farm)
farmStatus = wt.status()
T.near(farmStatus.items[farm[1].id].z, 1620, 1e-8, 'later far delivery cannot override a near owner')
T.check(farmStatus.items[farm[1].id].collide and collided == collisionBefore + 1,
  'later far delivery cannot turn off required nearby collision')
wt.dropKey('road:near')
farmStatus = wt.status()
T.check(farmStatus.colliding == 0 and collided == collisionBefore + 2, 'last near owner disables collision once')
T.near(farmStatus.items[farm[1].id].z, 1500, 1e-8, 'last near owner releases placement to retained far record')
T.near(farmStatus.items[farm[1].id].scale, 1, 1e-8, 'far owner scale is restored without recreating parts')

-- Two near owners remain deterministic regardless of arrival order, and both survive an origin rebase.
local nearA = row(farm[1].id, farm[1].x, farm[1].y, 1700)
local nearB = row(farm[1].id, farm[1].x, farm[1].y, 1800)
wt.onRows('road:z', { nearB })
wt.onRows('road:a', { nearA })
T.near(wt.status().items[farm[1].id].z, 1700, 1e-8, 'same-priority owners use stable key ordering')
wt.onRows('road:z', { nearB })
T.near(wt.status().items[farm[1].id].z, 1700, 1e-8, 'same-priority placement does not depend on delivery order')
madeBefore, movedBefore = S.calls.createObject_TSStatic or 0, S.calls.setPosRot or 0
local transformCollision = collided
wt.transform({ apply = function(_, x, y, z) return 100 - y, 200 + x, z + 3 end })
T.check((S.calls.createObject_TSStatic or 0) == madeBefore and (S.calls.setPosRot or 0) - movedBefore == 600,
  'whole farm rebase moves each existing object once without imports')
T.check(collided == transformCollision + 1, 'whole farm rebase batches its relevant collision rebuild once')
local preserved = true
for _, o in ipairs(S.liveOf('TSStatic')) do preserved = preserved and initialIds[o.id] and o.rot.x == 0 and o.rot.y == 0 end
T.check(preserved, 'farm rebase preserves every scene ID and upright tower axis')
wt.dropKey('road:a')
T.near(wt.status().items[farm[1].id].z, 1803, 1e-8, 'dropping first near owner selects transformed second near record')
T.check(wt.status().colliding == 1, 'another near owner keeps collision alive')
wt.dropKey('road:z')
farmStatus = wt.status()
T.near(farmStatus.items[farm[1].id].z, 1503, 1e-8, 'dropping last near owner selects transformed far record')
T.near(farmStatus.items[farm[1].id].x, 100 - farm[1].y, 1e-8, 'far retained coordinates use the rebased frame')
T.check(farmStatus.colliding == 0 and not farmStatus.items[farm[1].id].collide,
  'far-only diagnostic state records collision disabled')
collisionBefore = collided
wt.dropKey('wind:a')
T.check(count() == 600, 'dropping one far owner preserves all shared farm objects')
wt.onRows('wind:b', {})
T.check(count() == 0 and wt.status().turbines == 0 and wt.status().owners == 0,
  'last far owner cleanup releases every turbine and all 600 native objects')
T.check(collided == collisionBefore, 'far-only farm cleanup performs no irrelevant collision rebuild')
T.check(fileChecks == 4 and materialLoads == 2, 'whole farm lifecycle repeats no healthy resource checks')
print(string.format('farm stress: 300 turbines, 600 native objects, %.3f s in scene stubs', os.clock() - farmStart))

wt.reset()
fileChecks, materialLoads = 0, 0
FS.fileExists = function(_, path) fileChecks = fileChecks + 1; return path ~= wt.PARTS[2].shape end
wt.onRows('wind:missing', farm)
T.check(count() == 0 and wt.status().unavailable == 300, 'missing model leaves no partial objects in a dense farm')
T.check(fileChecks == 4 and materialLoads == 0, 'failed installation checked once per batch, not once per turbine')
wt.onRows('wind:missing', farm)
T.check(fileChecks == 8, 'failed installation is retried once on the next delivery')
FS.fileExists = function() fileChecks = fileChecks + 1; return true end
wt.onRows('wind:missing', farm)
T.check(count() == 600 and materialLoads == 2 and fileChecks == 12,
  'dense farm recovers in one resource check when installed assets become available')
wt.reset()
T.check(count() == 0 and wt.status().owners == 0, 'farm reset cleans all scene objects and ownership')
T.done('test_windturbines')
