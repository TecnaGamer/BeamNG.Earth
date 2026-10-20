-- luajit mod/tests/test_recouple.lua  (KyzoFox 1.5.4, rebase.lua: trailers are hitched again after a rebase)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local rebase = require('beamngEarth/rebase')

-- no trailer system: nothing to do
T.check(#rebase.couplings() == 0, 'no core_trailerRespawn: no couplings')
T.check(rebase.recoupleStart({}, 0) == nil, 'no couplings: no plan')

-- a truck (101) towing a trailer (102) on fifth wheel node 7 / kingpin node 3; a broken entry is ignored
core_trailerRespawn = { getTrailerData = function()
  return { [101] = { trailerId = 102, node = 7, trailerNode = 3 }, [200] = -1 } end }
local cs = rebase.couplings()
T.check(#cs == 1 and cs[1].veh == 101 and cs[1].trailer == 102 and cs[1].node == 7 and cs[1].trailerNode == 3,
  'the coupling read from the trailer registry')
local cmd = rebase.recoupleCommand(cs[1])
T.check(cmd:find('attachExternalCoupler%(N, TID, TN') and cmd:find('local N, TID, TN = 7, 102, 3', 1, true)
  and cmd:find('isCouplerAttached'), 'the vehicle command attaches node 7 to 102:3 only if nothing is attached')
T.check(load(cmd) ~= nil, 'the vehicle command is valid Lua')

-- the attempts go out at RECOUPLE_AT, to the head vehicle, while both vehicles exist
local sent = {}
local function veh(id)
  local o = createObject('TSStatic')
  o.id = id
  function o:queueLuaCommand(c) sent[#sent + 1] = { self.id, c } end
  S.objects[id] = o
  return o
end
veh(101); veh(102)
local plan = rebase.recoupleStart(cs, 10)
plan = rebase.recoupleTick(plan, 10)
T.check(#sent == 1 and sent[1][1] == 101, 'first attempt at once, to the truck')
plan = rebase.recoupleTick(plan, 10.1)
T.check(#sent == 1, 'nothing more before the next time')
plan = rebase.recoupleTick(plan, 10.5)
T.check(#sent == 3, 'two attempts due by 0.5 s')
plan = rebase.recoupleTick(plan, 20)
T.check(plan == nil and #sent == #rebase.RECOUPLE_AT, 'every attempt sent, then done')

-- a trailer gone meanwhile: nothing sent
sent = {}
S.objects[102] = nil
plan = rebase.recoupleTick(rebase.recoupleStart(cs, 0), 0)
T.check(#sent == 0, 'no attempt for a deleted trailer')
core_trailerRespawn = nil

T.done('test_recouple')
