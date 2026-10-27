-- luajit mod/tests/test_gasstations.lua  (gas stations: the road tiles' stations spawned as West Coast USA's props
-- with a refuel area per pump, handed to the stock freeroam_gasStations; the user, 2026-10-23: "the refueling is the
-- main part")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local dir = arg[0]:match('^(.*)[/\\]') or '.'
local S = dofile(dir .. '/stubs.lua')

local clears = 0
gameplay_rawPois = { clear = function() clears = clears + 1 end }
local gasSetting, careerOn = true, false
settings = { getValue = function(k) if k == 'enableGasStationsInFreeroam' then return gasSetting end end }
career_career = { isActive = function() return careerOn end }
freeroam_gasStations = { formatGasStationPoi = function(f) return { id = f.id, data = { type = 'gasStation', facility = f } } end }

local G = require('beamngEarth/gasstations')
G.enabled = true
local clock = 0
G.now = function() return clock end
local collide = 0
G.onCollide = function() collide = collide + 1 end
local gov = { try = function() return true end, charge = function() end }

local function rec(id, x)
  x = x or 0
  return { id = id, name = 'Speedway', brand = '', ll = { 40.44, -80.0 },
    props = {
      { shape = '/levels/west_coast_usa/art/shapes/buildings/s_fuel_structure_03.dae', pos = { x, 0, 1 }, yaw = 90, scale = { 1.1, 1, 1 }, collide = true },
      { shape = '/levels/west_coast_usa/art/shapes/buildings/s_fuel_pump_2015_apex.dae', pos = { x + 2, 0, 1.2 }, yaw = 90, scale = { 1, 1, 1 }, collide = false } },
    areas = {
      { pos = { x + 2, 0, 2 }, yaw = 90, half = { 1.6, 2.2, 2.75 }, icon = { x + 2, 0, 4.5 } },
      { pos = { x - 2, 0, 2 }, yaw = 90, half = { 1.6, 2.2, 2.75 }, icon = { x - 2, 0, 4.5 } } } }
end

local function count(class) return #S.liveOf(class) end

-- spawned: props as TSStatics, an area and an icon per pump, named for the stock module
G.onRows(1, '1:0_0', { rec('abc') })
T.check(count('TSStatic') == 0, 'nothing before update')
G.update(gov)
T.check(count('TSStatic') == 2, 'two props: ' .. count('TSStatic'))
T.check(count('BeamNGGameplayArea') == 2 and count('BeamNGPointOfInterest') == 2, 'an area and an icon per pump')
T.check(scenetree.findObject('bne_fuel_abc_a1') ~= nil and scenetree.findObject('bne_fuel_abc_i2') ~= nil, 'named')
local solid, ghost
for _, o in ipairs(S.liveOf('TSStatic')) do
  if o.fields.shapeName:find('structure') then solid = o else ghost = o end
end
-- the shapes' own collision meshes, as West Coast USA places them (its items leave collisionType at the default)
T.check(solid.fields.collisionType == 'Collision Mesh' and ghost.fields.collisionType == 'None', 'collide flags')
T.near(solid.scale.x, 1.1, 1e-9, 'scale')
-- yaw 90 (counter-clockwise from +x) in setPosRot's conjugate form: (0, 0, -sin 45, cos 45)
T.near(solid.rot.z, -math.sqrt(0.5), 1e-9, 'yaw quaternion z')
T.near(solid.rot.w, math.sqrt(0.5), 1e-9, 'yaw quaternion w')
local area = scenetree.findObject('bne_fuel_abc_a1')
T.near(area.scale.x, 1.6, 1e-9, 'area half extents as its scale')
T.check(collide == 1, 'a collision rebuild asked once per spawned station')

-- the stock POI list: one station, its pumps as (area, icon) names
local els = {}
G.pois('beamng_earth', els)
T.check(#els == 1 and #els[1].data.facility.pumps == 2, 'one POI with two pumps')
T.check(els[1].data.facility.pumps[1][1] == 'bne_fuel_abc_a1' and els[1].data.facility.pumps[1][2] == 'bne_fuel_abc_i1',
  'pump pairs')
T.check(els[1].data.facility.energyTypes[1] == 'gasoline', 'gasoline and diesel')
-- the game's own switch (Options > Gameplay: gas stations in freeroam), as freeroam/gasStations.lua honours it; career
-- always has them
gasSetting = false
els = {}
G.pois('beamng_earth', els)
T.check(#els == 0, 'no POIs with the freeroam gas stations setting off')
careerOn = true
els = {}
G.pois('beamng_earth', els)
T.check(#els == 1, 'career: POIs whatever the setting')
gasSetting, careerOn = true, false

-- the POIs gathered again after a change, at most every CLEAR_S
T.check(clears == 1, 'a clear with the spawn: ' .. clears)
clock = clock + 1; G.update(gov)
T.check(clears == 1, 'none without a change')
local c0 = clears
G.onRows(1, '1:0_1', { rec('def', 50) }); G.update(gov)
clock = clock + 0.1
G.onRows(1, '1:0_2', { rec('ghi', 90) }); G.update(gov)
T.check(clears == c0 + 1, 'debounced within CLEAR_S: ' .. (clears - c0))
clock = clock + G.CLEAR_S + 0.01; G.update(gov)
T.check(clears == c0 + 2, 'then one clear')

-- the same station from a second tile (the new anchor's after a rebase): no second set, re-placed from the new record
local n0 = count('TSStatic')
G.onRows(2, '2:5_5', { rec('abc', 10) }); G.update(gov)
T.check(count('TSStatic') == n0, 'not spawned twice')
T.near(scenetree.findObject('bne_fuel_abc_a1').pos.x, 12, 1e-9, 're-placed from the new record')
G.dropKey('1:0_0')
T.check(scenetree.findObject('bne_fuel_abc_a1') ~= nil, 'kept while another tile has it')
G.dropKey('2:5_5')
T.check(scenetree.findObject('bne_fuel_abc_a1') == nil and count('TSStatic') == n0 - 2, 'gone with its last tile')

-- a rebase: every object moved by the transform, POIs gathered again
local xf = { apply = function(_, x, y, z) return x, y - 20, z end, beamngQuat = function() return 0, 0, 0, 1 end }
local before = scenetree.findObject('bne_fuel_def_a1').pos.y
clock = clock + 1; G.update(gov)
local c1 = clears
G.transform(xf)
T.near(scenetree.findObject('bne_fuel_def_a1').pos.y, before - 20, 1e-9, 'moved by the transform')
clock = clock + 1; G.update(gov)
T.check(clears == c1 + 1, 'a clear after the rebase')

-- a level end: everything goes
G.reset()
T.check(count('TSStatic') == 0 and count('BeamNGGameplayArea') == 0 and count('BeamNGPointOfInterest') == 0, 'reset')
els = {}
G.pois('beamng_earth', els)
T.check(#els == 0, 'no POIs after reset')

-- review I1: a station still queued at a rebase is not spawned from its old-frame record; the new anchor's tile brings
-- the record it is spawned from
G.reset()
G.onRows(1, '1:9_9', { rec('late', 300) })
G.transform(xf)
G.update(gov)
T.check(scenetree.findObject('bne_fuel_late_a1') == nil, 'not spawned in the old frame')
G.onRows(2, '2:9_9', { rec('late', 400) }); G.update(gov)
T.near(scenetree.findObject('bne_fuel_late_a1').pos.x, 402, 1e-9, 'spawned from the new anchor record')
-- a made station whose new record has other pumps (the new frame's layout): made again from it
local r2 = rec('late', 400); r2.areas[3] = { pos = { 410, 0, 2 }, yaw = 90, half = { 1.6, 2.2, 2.75 }, icon = { 410, 0, 4.5 } }
G.onRows(2, '2:9_8', { r2 }); G.update(gov)
T.check(scenetree.findObject('bne_fuel_late_a3') ~= nil and count('BeamNGGameplayArea') == 3, 'made again with 3 pumps')
-- a station's colliding props deleted: the collision rebuilt (no invisible pump left behind)
local cbefore = collide
G.dropKey('1:9_9'); G.dropKey('2:9_9'); G.dropKey('2:9_8')
T.check(collide == cbefore + 1, 'a collision rebuild after the delete')
G.reset()

-- the region's shapes (the server's anchor_ready fuelShapes; only that region's, a Steam Deck's memory) loaded once,
-- hidden and not colliding, so the first station does not pay their first load
G.prewarm(nil)
T.check(#S.liveOf('TSStatic') == 0, 'nothing named, nothing loaded (gas stations off)')
G.prewarm({ '/x/a.dae', '/x/b.dae' })
local warm = S.liveOf('TSStatic')
T.check(#warm == 2, 'one hidden TSStatic per shape named: ' .. #warm)
T.check(warm[1].hidden == true and warm[1].fields.collisionType == 'None', 'hidden, not colliding')
G.prewarm({ '/x/a.dae', '/x/b.dae' })
T.check(#S.liveOf('TSStatic') == 2, 'once (a rebase in the same region)')
G.prewarm({ '/x/a.dae', '/x/c.dae' })
T.check(#S.liveOf('TSStatic') == 3, 'another region: only its new shapes')
G.reset()
T.check(#S.liveOf('TSStatic') == 3, 'kept over a reset (the level end deletes them with the level)')
for _, o in ipairs(S.liveOf('TSStatic')) do o:delete() end             -- (the level loaded again)
G.prewarm({ '/x/a.dae' })
T.check(#S.liveOf('TSStatic') == 1, 'made again after the level went')

-- the world map's "Gas stations" overlay (settings.uiMapState -> gasstations.mapPins): every known station as
-- lat/lon, queued or spawned alike, by the active anchor (a fake one here: enuToGeodetic's only caller in this file)
G.reset()
T.check(#G.mapPins(nil) == 0, 'no anchor: no pins')
G.onRows(1, '1:3_3', { rec('map1', 500) })
local fakeAnchor = { enuToGeodetic = function(self, x, y, z) return 40.0 + x * 1e-5, -80.0 + y * 1e-5, z end }
local pins = G.mapPins(fakeAnchor)
T.check(#pins == 1 and pins[1].id == 'map1' and pins[1].name == 'Speedway', 'a pin for the queued (not yet spawned) station too')
T.near(pins[1].lat, 40.005, 1e-9, 'converted by the anchor, from its first prop (not yet spawned)')
G.update(gov)
local pins2 = G.mapPins(fakeAnchor)
T.check(#pins2 == 1 and pins2[1].id == 'map1', 'still one pin once spawned')
G.reset()

T.done('test_gasstations')
