-- luajit mod/tests/test_swap_core_teardown.lua
-- The mission (or the extension) ends in the middle of a generation swap: the hold is released and nothing of the
-- swap is left, in core.lua or in roads.lua (swapcore_harness.lua).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local H = dofile(dir .. '/swapcore_harness.lua')(dir, T, S)
local st, roads = H.st, H.roads

local realSettled = roads.swapSettled
roads.swapSettled = function() return false end
H.serverSend({ type = 'swap_begin', anchorId = 1, gen = 9 })
local m = H.block('hm_1_0_0_v9')
m.type, m.anchorId, m.swap = 'terrain_refresh', 1, 9
H.serverSend(m)
H.serverSend({ type = 'swap_end', anchorId = 1, gen = 9, skins = 0 })
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'settle' end, 50), 'a swap in its settling')
T.check(H.sim.paused and st().swap.held, 'the car held by it')
T.check(roads._swapState() ~= nil, 'roads has its state')
H.core.onClientEndMission()
T.check(st().swap == nil, 'the mission ended: no swap in core')
T.check(not H.sim.paused, 'its hold released')
T.check(roads._swapState() == nil and roads.swapRolledBack(), 'no swap state in roads')
roads.swapSettled = realSettled
-- the extension unloaded with one staged (before its commit)
roads.swapBegin(10)
st().swap = { gen = 10, terrains = {}, t0 = st().clock }
H.core.onExtensionUnloaded()
T.check(st().swap == nil and roads._swapState() == nil, 'the extension unloaded: nothing left either')
T.done('test_swap_core_teardown')
