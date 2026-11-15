-- luajit mod/tests/test_collisionfoci.lua  (collision islands for several vehicles, collisionfoci.lua): the foci of the
-- vehicles other than the active car (players' cars the active car's slow radius with solid trees, traffic a small
-- radius without, parked cars and cars right by the active one none), "near" with and without trees, and overlapping
-- circles merging into one island (apart: their own islands).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local cf = require('beamngEarth/collisionfoci')

local vehicles = {
  { id = 1, x = 0, y = 0, kind = 'player' },          -- the active car
  { id = 2, x = 2000, y = 0, kind = 'player' },       -- another player's car, far away
  { id = 3, x = 100, y = 50, kind = 'traffic' },      -- traffic near the active car
  { id = 4, x = 5000, y = 5000, kind = 'parked' },    -- a parked car
  { id = 5, x = 10, y = 5, kind = 'player' },         -- a car right by the active one
}
local foci = cf.compute(vehicles, 1, 0, 0)
T.check(#foci == 2, 'two foci (not the active car, not parked, not right by the active one): ' .. #foci)
local byId = {}
for _, f in ipairs(foci) do byId[f.id] = f end
T.check(byId[2] and byId[2].r == cf.PLAYER_R and byId[2].trees == true, 'another player: the full radius and trees')
T.check(byId[3] and byId[3].r == cf.TRAFFIC_R and byId[3].trees == false, 'traffic: the small radius, no trees')

-- near: within a focus' radius (+ extra); trees only by the foci that have them
T.check(cf.near(foci, 2000 + cf.PLAYER_R - 1, 0) == true, 'inside the far car\'s circle')
T.check(cf.near(foci, 2000 + cf.PLAYER_R + 50, 0) == false, 'outside it')
T.check(cf.near(foci, 2000 + cf.PLAYER_R + 50, 0, 100) == true, 'outside it, inside with the extra')
T.check(cf.near(foci, 100, 50 + cf.TRAFFIC_R - 1) == true, 'inside the traffic car\'s circle')
T.check(cf.near(foci, 100, 50 + cf.TRAFFIC_R - 1, 0, true) == false, 'no solid trees round traffic')

-- islands: the active car and the traffic car overlap (one island); the far car its own
local isl = cf.islands(foci, { x = 0, y = 0, r = cf.PLAYER_R })
T.check(#isl == 2, 'two islands: ' .. #isl)
local sizes = {}
for _, i in ipairs(isl) do sizes[#sizes + 1] = #i.members end
table.sort(sizes)
T.check(sizes[1] == 1 and sizes[2] == 2, 'the near traffic car merged with the active car, the far car alone')
-- as the far car comes close, the islands merge
local f2 = cf.compute({ { id = 1, x = 0, y = 0, kind = 'player' }, { id = 2, x = 500, y = 0, kind = 'player' } }, 1, 0, 0)
T.check(#cf.islands(f2, { x = 0, y = 0, r = cf.PLAYER_R }) == 1, 'cars 500 m apart: one island')
f2 = cf.compute({ { id = 1, x = 0, y = 0, kind = 'player' }, { id = 2, x = 700, y = 0, kind = 'player' } }, 1, 0, 0)
T.check(#cf.islands(f2, { x = 0, y = 0, r = cf.PLAYER_R }) == 2, 'cars 700 m apart: two islands')
local i1 = cf.islands(f2)[1]
T.check(i1.x0 == 700 - cf.PLAYER_R and i1.x1 == 700 + cf.PLAYER_R, 'an island\'s box spans its circles')

-- no active car: every player's car and traffic car is a focus
T.check(#cf.compute(vehicles, nil, nil, nil) == 4, 'no active car: all but the parked one')

-- the game's vehicles: walking mode's unicycle left out (GitHub #240: a focus and a far car of its own after the
-- player got back in a car, every collision rebuild ~4 s)
do
  local function veh(id, x, jbeam)
    return { getId = function() return id end, getPosition = function() return { x = x, y = 0, z = 0 } end,
      getJBeamFilename = function() return jbeam end }
  end
  local objs = { veh(1, 0, 'pickup'), veh(2, 900, 'unicycle'), veh(3, 50, 'etk800'), veh(4, 25000, 'spikestrip') }
  local oldBe = rawget(_G, 'be')
  be = { getObjectCount = function() return #objs end, getObject = function(_, i) return objs[i + 1] end }
  local ids = {}
  for _, v in ipairs(cf.vehicles()) do ids[#ids + 1] = v.id end
  T.check(#ids == 2 and ids[1] == 1 and ids[2] == 3, 'the walker and the spike strip left out: ' .. table.concat(ids, ','))
  be = oldBe
end

T.done('test_collisionfoci')
