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

-- a big station (the Buc-ee's in New Braunfels: 354 props, 23.8 ms in one frame) spawns over several frames: one
-- object a governor slot, made (POIs, the collision rebuild) only once all of it is there
local budget = 0
local gov10 = { try = function() if budget > 0 then budget = budget - 1 return true end return false end,
                charge = function() end }
local function frame() budget = 10; G.update(gov10) end
local function big(id, x)
  local r = rec(id, x)
  local p1 = r.props[1]
  r.props = {}
  for i = 1, 40 do
    r.props[i] = { shape = p1.shape, pos = { (x or 0) + i, 5, 1 }, yaw = 0, scale = { 1, 1, 1 }, collide = (i % 2 == 0) }
  end
  return r
end
G.reset()
for _, o in ipairs(S.liveOf('TSStatic')) do o:delete() end             -- (the prewarm's hidden shape above)
collide = 0
G.onRows(1, '1:7_7', { big('buc', 0) })
frame()
T.check(count('TSStatic') == 10, 'frame 1: 10 objects: ' .. count('TSStatic'))
T.check(G.count() == 0 and collide == 0, 'not made yet, no collision rebuild yet')
frame(); frame(); frame()
T.check(count('TSStatic') == 40 and count('BeamNGGameplayArea') == 0, 'frame 4: the props, no areas yet')
frame()
T.check(count('BeamNGGameplayArea') == 2 and count('BeamNGPointOfInterest') == 2, 'frame 5: the areas')
T.check(G.count() == 1 and collide == 1, 'made once, one collision rebuild: ' .. collide)
local els5 = {}
G.pois('beamng_earth', els5)
T.check(#els5 == 1, 'its POIs once made')

-- dropped mid-spawn: what was made goes, with a collision rebuild (its pumps may be in the collision already)
G.reset()
collide = 0
G.onRows(1, '1:7_8', { big('half', 0) })
frame()
G.dropKey('1:7_8')
T.check(count('TSStatic') == 0, 'the half-made station gone')
T.check(collide == 1, 'a collision rebuild after deleting it')
frame()
T.check(count('TSStatic') == 0, 'nothing more made after the drop')

-- a rebase mid-spawn: the objects made move with the world; the rest wait for the new frame's record
G.reset()
G.onRows(1, '1:7_9', { big('reb', 0) })
frame()
local first = scenetree.findObject and S.liveOf('TSStatic')[1]
local y0 = first.pos.y
G.transform(xf)
T.near(first.pos.y, y0 - 20, 1e-9, 'a made object moved by the transform')
frame()
T.check(count('TSStatic') == 10, 'nothing more from the old frame record')
G.onRows(2, '2:7_9', { big('reb', 0) })      -- the new anchor's tile
for _ = 1, 5 do frame() end
T.check(count('TSStatic') == 40 and G.count() == 1, 'finished from the new record')
G.reset()

-- (final review) a road tile sent again while a big station is still spawning, with another layout: started again
-- from the new record, not carried on into it (no prop doubled or missing)
G.onRows(1, '1:5_5', { big('mid', 0) })
frame()
local shifted = big('mid', 100); table.remove(shifted.props)            -- 39 props, all elsewhere
G.onRows(1, '1:5_6', { shifted })
for _ = 1, 6 do frame() end
local wrong = 0
for _, o in ipairs(S.liveOf('TSStatic')) do
  if o.pos.x < 100 then wrong = wrong + 1 end
end
T.check(count('TSStatic') == 39 and wrong == 0, 'all 39 from the new record, none left from the old: ' .. wrong)
G.reset()

-- a re-sent road tile whose record has a prop more or fewer (its neighbour's buildings arrived): the props kept by
-- shape and place, only the difference made or deleted, so nothing blinks; the refuel areas (invisible) made again
-- when they change
local function ids()
  local out = {}
  for _, o in ipairs(S.liveOf('TSStatic')) do out[o:getID()] = o.fields.shapeName end
  return out
end
local function same(a, b, except)
  for id in pairs(a) do if id ~= except and not b[id] then return false end end
  return true
end
G.onRows(1, '1:6_6', { rec('rs', 0) }); G.update(gov)
local before3 = ids()
local queued = false
local fewer = rec('rs', 0); table.remove(fewer.props, 2)
collide = 0
G.onRows(1, '1:6_7', { fewer })
T.check(count('TSStatic') == 1, 'one prop fewer: one object deleted at once: ' .. count('TSStatic'))
local after = ids()
local kept = next(after)
T.check(before3[kept] ~= nil, 'the other prop kept (same object)')
G.update(gov)
T.check(count('TSStatic') == 1 and count('BeamNGGameplayArea') == 2, 'nothing respawned')
T.check(collide == 1, 'one collision rebuild for the deleted prop')
local more = rec('rs', 0)
more.props[3] = { shape = '/levels/west_coast_usa/art/shapes/buildings/s_fuel_structure_07.dae', pos = { 6, 0, 1 }, yaw = 90,
  scale = { 1, 1, 1 }, collide = true }
G.onRows(1, '1:6_8', { more })
T.check(count('TSStatic') == 3, 'a prop more (and the one back): made at once: ' .. count('TSStatic'))
T.check(ids()[kept] ~= nil, 'the kept prop still the same object')
local areaIds = {}
for _, o in ipairs(S.liveOf('BeamNGGameplayArea')) do areaIds[o:getID()] = true end
local moreAreas = rec('rs', 0)
moreAreas.props[3] = more.props[3]
moreAreas.areas[3] = { pos = { 6, 0, 2 }, yaw = 90, half = { 1.6, 2.2, 2.75 }, icon = { 6, 0, 4.5 } }
local propIds = ids()
local c3 = clears
G.onRows(1, '1:6_9', { moreAreas })
T.check(count('BeamNGGameplayArea') == 3 and count('BeamNGPointOfInterest') == 3, 'a pump more: its area and icon')
T.check(scenetree.findObject('bne_fuel_rs_a3') ~= nil, 'named in order')
T.check(same(propIds, ids()), 'no prop touched for an area change')
clock = clock + G.CLEAR_S + 0.01; G.update(gov)
T.check(clears == c3 + 1, 'the POIs gathered again')
local els6 = {}
G.pois('beamng_earth', els6)
T.check(#els6 == 1 and #els6[1].data.facility.pumps == 3, 'three pumps in the POI')
-- a record in another frame (nothing matches: the new anchor's tile after a rebase): made again, as before
local moved = rec('rs', 500)
G.onRows(2, '2:6_9', { moved }); G.update(gov)
T.check(count('TSStatic') == 2, 'made again from the new frame: ' .. count('TSStatic'))
T.near(scenetree.findObject('bne_fuel_rs_a1').pos.x, 502, 1e-9, 'at the new place')
G.reset()

-- an EV charger (evchargers.py, the user 2026-10-29: "real life ev stations ... real recharging in the game"): its
-- record's energy handed to the stock POI (the charge prompt, the bolt icon); unnamed: 'EV charger'
do
  local evr = rec('evx', 50)
  evr.name, evr.energy = '', { 'electricEnergy' }
  G.onRows(1, '1:1_0', { evr, rec('gas1', 90) })
  G.update(gov)
  local got = {}
  G.pois('beamng_earth', got)
  local byId = {}
  for _, e in ipairs(got) do byId[e.id] = e end
  local f = byId['bne_fuel_evx'] and byId['bne_fuel_evx'].data.facility
  T.check(f ~= nil and f.energyTypes[1] == 'electricEnergy' and #f.energyTypes == 1, 'a charger: electric energy only')
  T.check(f ~= nil and f.name == 'EV charger', 'an unnamed charger: EV charger')
  T.check(byId['bne_fuel_gas1'] ~= nil and byId['bne_fuel_gas1'].data.facility.energyTypes[1] == 'gasoline',
    'a gas station beside it: gasoline')
  G.reset()
end

T.done('test_gasstations')
