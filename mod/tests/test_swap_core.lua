-- luajit mod/tests/test_swap_core.lua
-- A generation swap through core.lua itself (swapcore_harness.lua: the stubbed engine, a connected session, anchor 1
-- on its first block): the real clock's units, the rollback to the INITIAL anchor's block, a connection lost in the
-- middle of the settling.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local H = dofile(dir .. '/swapcore_harness.lua')(dir, T, S)
local st, roads, swapgen = H.st, H.roads, H.swapgen

local function swapMsgs(gen, name)
  H.serverSend({ type = 'swap_begin', anchorId = 1, gen = gen })
  local m = H.block(name)
  m.type, m.anchorId, m.swap = 'terrain_refresh', 1, gen
  H.serverSend(m)
  H.serverSend({ type = 'swap_end', anchorId = 1, gen = gen, skins = 0 })
end
local function done(gen)
  for _, m in ipairs(H.received('swap_done')) do if m.gen == gen then return m end end
end

-- ---- every terrain install leaves the snapshot a rollback needs: the anchor's first block too ----------------
T.check(type(st().blockMsg) == 'table' and tostring(st().blockMsg.heightmap):find('hm_1_0_0', 1, true),
  'the initial anchor\'s block is the snapshot (commitTeleport, not only commitTerrain)')
T.check(H.loads[#H.loads]:find('hm_1_0_0', 1, true), 'and it is the terrain in use')

-- ---- a swap that goes through: held from the commit, ok only once settled ---------------------------------
swapMsgs(1, 'hm_1_0_0_v1')
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'settle' end, 50), 'committed')
T.check(H.sim.paused, 'the car is held from the commit')
T.check(H.loads[#H.loads]:find('_v1', 1, true), 'the swap\'s terrain imported')
T.check(done(1) == nil, 'the server not told before the collision is in effect')
T.check(H.pumpUntil(function() return done(1) ~= nil end, 100), 'answered')
T.check(done(1).ok == true and not H.sim.paused and st().swap == nil, 'ok, the hold released')
T.check(tostring(st().blockMsg.heightmap):find('_v1', 1, true), 'the new block is the snapshot now')

-- ---- the real clock: a collision that never comes is rolled back after 5 SECONDS ----------------------------
local realSettled = roads.swapSettled
roads.swapSettled = function() return false end
local t0 = st().clock
swapMsgs(2, 'hm_1_0_0_v2')
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'settle' end, 50), 'committed')
T.check(H.loads[#H.loads]:find('_v2', 1, true), 'the new terrain in')
local n = 0
while st().swap and st().swap.phase == 'settle' and n < 100000 do H.frame(); n = n + 1 end
local took = st().clock - t0
T.check(took > swapgen.SETTLE_MAX_S and took < swapgen.SETTLE_MAX_S + 2,
  string.format('rolled back %.1f s after the commit on core\'s own clock (5,000 s when it was read as ms)', took))
T.check(H.loads[#H.loads]:find('_v1', 1, true), 'the block that was in use is installed again, from its own file')
T.check(H.pumpUntil(function() return done(2) ~= nil end, 400), 'answered')
T.check(done(2).ok == false and not H.sim.paused and st().swap == nil, 'ok = false, the hold released after the rollback')
T.check(tostring(st().blockMsg.heightmap):find('_v1', 1, true), 'the snapshot is the old block again')
roads.swapSettled = realSettled

-- ---- the old terrain cannot be had again: the rollback is refused, the commit stands --------------------------
roads.swapSettled = function() return false end
swapMsgs(3, 'hm_1_0_0_v3')
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'settle' end, 50), 'committed')
H.failing['/temp/beamngEarth/s1/hm_1_0_0_v1.png'] = true        -- (the old block's file is gone)
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'stand' end, 400), 'no rollback: it stands')
T.check(H.sim.paused and H.loads[#H.loads]:find('_v3', 1, true), 'still held, on the new terrain')
roads.swapSettled = realSettled
T.check(H.pumpUntil(function() return done(3) ~= nil end, 100), 'answered once its collision is in')
T.check(done(3).ok == true and not H.sim.paused, 'ok: this version whole, the hold released')
H.failing['/temp/beamngEarth/s1/hm_1_0_0_v1.png'] = nil

-- ---- a terrain that cannot be imported: nothing changes -----------------------------------------------------
H.failing['/temp/beamngEarth/s1/hm_1_0_0_v4.png'] = true
swapMsgs(4, 'hm_1_0_0_v4')
T.check(H.pumpUntil(function() return done(4) ~= nil end, 400), 'answered')
T.check(done(4).ok == false and not H.sim.paused and st().swap == nil, 'ok = false, the car free')
T.check(H.loads[#H.loads]:find('_v3', 1, true) and tostring(st().blockMsg.heightmap):find('_v3', 1, true),
  'the terrain in use is still the one of before')

-- ---- a connection lost in the middle of the settling: the transaction goes on to its end ----------------------
roads.swapSettled = function() return false end
swapMsgs(5, 'hm_1_0_0_v5')
T.check(H.pumpUntil(function() return st().swap ~= nil and st().swap.phase == 'settle' end, 50), 'committed')
H.server.failErr, H.server.down = 'closed', true
H.frames(3)
T.check(not st().sessionId, 'disconnected')
T.check(st().swap ~= nil and st().swap.phase == 'settle' and H.sim.paused,
  'the swap is still there and the car still held (it was dropped with the new terrain under the old skins)')
roads.swapSettled = realSettled
T.check(H.pumpUntil(function() return st().swap == nil end, 200), 'it ends by itself')
T.check(not H.sim.paused, 'and only then is the car let go')
T.check(H.loads[#H.loads]:find('_v5', 1, true), 'committed whole: the new terrain')
-- ... and one lost before its commit is dropped, nothing changed
H.server.down = false
T.done('test_swap_core')
