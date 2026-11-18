-- luajit mod/tests/test_swapgen_commit.lua
-- swapgen.lua: a generation swap's commit, frame by frame, with stand-ins for roads.lua and the engine. The skins and
-- the terrain are committed in one frame with the car held; the server is told ok only once the collision of both is
-- in effect; a terrain that cannot be imported, a skin that could not be staged or a collision that never comes leave
-- the old ground whole (the skins put back, the old terrain committed again) and are answered ok = false.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
package.path = dir .. '/../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local T = dofile(dir .. '/testlib.lua')
local swapgen = require('beamngEarth/swapgen')

local function world(o)
  o = o or {}
  local w = { log = {}, sent = {}, held = o.alreadyHeld == true, clock = 0, terrain = 'old', skins = 'old', collision = 'old',
    settleAfter = o.settleAfter or 2, frames = 0 }
  local function note(x) w.log[#w.log + 1] = x end
  w.roads = {
    swapStaged = function(gen, skins)
      if o.stageFails then return false, 'skin g1 could not be staged' end
      if o.notReady and w.clock < o.notReady then return false end
      return true
    end,
    swapCount = function() return o.noSkins and 0 or 3 end,
    swapCommit = function() if not o.noSkins then w.skins = 'new' end; note('skins new'); w.frames = 0; return true end,
    swapSettled = function()
      if o.neverSettles then return false end
      w.frames = w.frames + 1
      if w.frames >= w.settleAfter then w.collision = w.terrain .. '+' .. w.skins; return true end
      return false
    end,
    swapFinish = function() note('finish'); return true end,
    swapUndo = function() w.skins = 'old'; note('skins undone'); w.backFrames = 0; return true end,
    swapRolledBack = function()
      if o.rollbackNeverSettles then return false end
      w.backFrames = (w.backFrames or 0) + 1
      if w.backFrames >= (o.backAfter or 2) then w.collision = w.terrain .. '+' .. w.skins; return true end
      return false
    end,
    swapAbort = function() note('abort'); return true end,
  }
  w.d = function()
    return { clock = w.clock, roads = w.roads, minimized = function() return o.minimized == true end,
      classify = function(msg) return msg.where end,
      commitTerrain = function(msg)
        note('terrain ' .. (o.terrainFails and 'FAILS' or 'new') .. (w.held and ' (held)' or ' (NOT held)'))
        if o.terrainFails then return false end
        w.terrain = 'new'
        return true
      end,
      restoreTerrain = function()
        if o.restoreFails then note('restore FAILS'); return false end
        w.terrain = 'old'; note('terrain restored'); return true
      end,
      setLater = function(msg) note('later set') end,
      hold = function(on)
        if on and o.alreadyHeld then return false end
        w.held = on
        note(on and 'hold' or 'release')
        return true
      end,
      send = function(m) w.sent[#w.sent + 1] = m; note('send ' .. tostring(m.ok)) end }
  end
  w.run = function(sw, n)
    local res
    for _ = 1, n or 50 do
      res = swapgen.step(sw, w.d())
      -- the invariant, every frame: never an old skin on the new terrain or the reverse while the car is free
      if not w.held then
        T.check(w.terrain == w.skins, 'free car: terrain ' .. w.terrain .. ' with skins ' .. w.skins)
      end
      if res then break end
      w.clock = w.clock + 0.016       -- (seconds, as core's st.clock)
    end
    return res
  end
  return w
end
local function swap(terrains) return { gen = 5, terrains = terrains or { { where = 'now' } }, t0 = 0, ended = true, skins = 2 } end
local function has(w, x) for _, l in ipairs(w.log) do if l == x then return true end end return false end
local function index(w, x) for i, l in ipairs(w.log) do if l == x then return i end end return nil end

-- ---- the good path -----------------------------------------------------------------------------------------
do
  local w = world()
  local sw = swap()
  T.check(swapgen.step(sw, w.d()) == nil, 'the commit frame is not the end')
  T.check(w.held, 'the car is held from the commit')
  T.check(w.skins == 'new' and w.terrain == 'new', 'skins and terrain switched in that one frame')
  T.check(#w.sent == 0, 'nothing said to the server yet: the collision is not in effect')
  T.check(index(w, 'hold') < index(w, 'skins new') and index(w, 'skins new') < index(w, 'terrain new (held)'),
    'held, then the skins, then the terrain')
  local res = w.run(sw)
  T.check(res == 'done' and #w.sent == 1 and w.sent[1].ok == true and w.sent[1].gen == 5, 'ok once settled')
  T.check(not w.held, 'the hold released')
  T.check(index(w, 'finish') < index(w, 'release') and index(w, 'release') < index(w, 'send true'),
    'the old skins go, the car is let go, the server is told: in that order')
  T.check(w.collision == 'new+new', 'the collision was in effect before the answer')
end

-- ---- a terrain that cannot be imported: nothing of the swap stays ---------------------------------------------
do
  local w = world({ terrainFails = true })
  local res = w.run(swap())
  T.check(res == 'failed' and w.sent[1].ok == false, 'answered ok = false')
  T.check(w.skins == 'old' and w.terrain == 'old', 'the old skins back on the old terrain')
  T.check(has(w, 'skins undone') and not has(w, 'finish'), 'the commit taken back, the old skins never deleted')
  T.check(not has(w, 'terrain restored'), 'the failed import left the old terrain in use: nothing to restore')
  T.check(not w.held, 'the hold released')
  T.check(index(w, 'skins undone') < index(w, 'send false'), 'restored before the server is told')
  T.check(w.collision == 'old+old' and index(w, 'release') > index(w, 'skins undone'),
    'the hold kept until the restored collision was in effect')
end

-- ---- a rollback keeps the hold until its own collision is in effect -------------------------------------------
do
  local w = world({ terrainFails = true, backAfter = 7 })
  local sw = swap()
  T.check(swapgen.step(sw, w.d()) == nil, 'the commit frame: rolled back, not over')
  T.check(w.held and #w.sent == 0 and w.skins == 'old', 'skins back, the car still held, the server not told')
  for i = 1, 5 do
    T.check(swapgen.step(sw, w.d()) == nil and w.held and #w.sent == 0, 'rollback frame ' .. i .. ': held, not told')
    w.clock = w.clock + 0.016
  end
  T.check(w.run(sw) == 'failed' and not w.held and w.sent[1].ok == false, 'then released and told')
  -- a rollback whose collision never comes: released after ROLLBACK_MAX_S, never later
  local w2 = world({ terrainFails = true, rollbackNeverSettles = true })
  local res2 = w2.run(swap(), 2000)
  T.check(res2 == 'failed' and not w2.held and w2.clock > swapgen.ROLLBACK_MAX_S and w2.clock < swapgen.ROLLBACK_MAX_S + 1,
    'a rollback\'s hold ends after ROLLBACK_MAX_S')
end

-- ---- the old terrain cannot be had again: no half rollback, the commit stands -------------------------------
do
  local w = world({ neverSettles = true, restoreFails = true })
  local sw = swap()
  local res = w.run(sw, 4000)
  T.check(has(w, 'restore FAILS') and not has(w, 'skins undone'), 'the skins are NOT put back on a terrain that is not')
  T.check(res == 'done' and w.sent[1].ok == true, 'the commit stands: ok')
  T.check(w.skins == 'new' and w.terrain == 'new' and not w.held, 'this version\'s skins on this version\'s terrain')
  T.check(w.clock > swapgen.SETTLE_MAX_S + swapgen.STAND_MAX_S - 1, 'after waiting for its collision as long as it may')
  T.check(type(w.sent[1].why) == 'string', 'and the server is told why it was late')
end

-- ---- the units: seconds --------------------------------------------------------------------------------------
do
  T.check(swapgen.SETTLE_MAX_S == 5 and swapgen.MAX_S == 120 and swapgen.ROLLBACK_MAX_S == 5, 'the limits are seconds')
  local w = world({ neverSettles = true })
  local sw = swap()
  swapgen.step(sw, w.d())
  w.clock = 4.9
  T.check(swapgen.step(sw, w.d()) == nil and sw.phase == 'settle', '4.9 s after the commit: still settling')
  w.clock = 5.1
  swapgen.step(sw, w.d())
  T.check(sw.phase == 'rollback', '5.1 s after it: rolled back (it was 5,000 s when the clock was read as ms)')
end

-- ---- past its commit a swap is a local transaction (core steps it whatever the server does) ------------------
do
  local sw = swap()
  T.check(not swapgen.live(sw) and not swapgen.live(nil), 'staging: not live')
  local w = world({ settleAfter = 5 })
  swapgen.step(sw, w.d())
  T.check(swapgen.live(sw), 'from the commit on: live')
  -- before the commit a cancel (the server's abort, a lost connection) drops it at the next step
  local w2 = world({ notReady = 1e12 })
  local sw2 = swap()
  swapgen.step(sw2, w2.d())
  sw2.cancel = 'the connection was lost'
  T.check(swapgen.step(sw2, w2.d()) == 'failed' and has(w2, 'abort') and w2.sent[1].why == 'the connection was lost',
    'a cancel before the commit: aborted')
end

-- ---- a skin that could not be staged: no commit at all ---------------------------------------------------------
do
  local w = world({ stageFails = true })
  local res = w.run(swap())
  T.check(res == 'failed' and w.sent[1].ok == false and tostring(w.sent[1].why):find('staged'), 'answered ok = false, why')
  T.check(has(w, 'abort') and not has(w, 'skins new') and not has(w, 'hold'), 'nothing committed, nothing held')
end

-- ---- the collision never comes: the old terrain committed again ----------------------------------------------
do
  local w = world({ neverSettles = true })
  local sw = swap()
  local res = w.run(sw, 2000)
  T.check(res == 'failed' and w.sent[1].ok == false, 'answered ok = false')
  T.check(w.clock > swapgen.SETTLE_MAX_S and w.clock < swapgen.SETTLE_MAX_S + swapgen.ROLLBACK_MAX_S + 1, 'after the settle time')
  T.check(has(w, 'terrain restored') and has(w, 'skins undone') and not has(w, 'finish'), 'both put back')
  T.check(w.skins == 'old' and w.terrain == 'old' and not w.held, 'the old ground whole, the car free')
  T.check(#w.sent == 1, 'one answer')
end

-- ---- collision timing: no answer, no release, before it is in effect -----------------------------------------
do
  local w = world({ settleAfter = 6 })
  local sw = swap()
  for i = 1, 5 do
    T.check(swapgen.step(sw, w.d()) == nil, 'frame ' .. i .. ': still settling')
    T.check(w.held and #w.sent == 0, 'frame ' .. i .. ': held, the server not told')
    w.clock = w.clock + 0.016
  end
  T.check(w.run(sw) == 'done' and w.sent[1].ok == true, 'then done')
end

-- ---- staging still running, the server not done, a minimized window: wait -------------------------------------
do
  local w = world({ notReady = 0.1 })
  local sw = swap()
  T.check(swapgen.step(sw, w.d()) == nil and #w.log == 0, 'not staged yet: nothing happens')
  w.clock = 0.2
  sw.ended = false
  T.check(swapgen.step(sw, w.d()) == nil and #w.log == 0, 'the server has not sent all: nothing happens')
  sw.ended = true
  T.check(w.run(sw) == 'done', 'then it goes')
  local w2 = world({ minimized = true })
  T.check(swapgen.step(swap(), w2.d()) == nil and #w2.log == 0, 'a minimized window: no terrain import now')
  local w3 = world({ stageFails = false, notReady = 1e12 })
  local sw3 = swap()
  T.check(w3.run(sw3, 9000) == 'failed' and has(w3, 'abort') and w3.sent[1].ok == false, 'never staged: given up, not released')
end

-- ---- a block of the swap that is no longer in use: given up whole ---------------------------------------------
do
  local w = world()
  local res = w.run(swap({ { where = nil } }))
  T.check(res == 'failed' and has(w, 'abort') and not has(w, 'skins new'), 'nothing committed')
end

-- ---- nothing of the ground in use changed: no hold, ok at once; a pending block's message replaced -----------
do
  local w = world({ noSkins = true })
  local res = w.run(swap({ { where = 'later' } }))
  T.check(res == 'done' and w.sent[1].ok == true and not has(w, 'hold') and has(w, 'later set'), 'ok without a hold')
end

-- ---- somebody else holds the car already: not ours to release ------------------------------------------------
do
  local w = world({ alreadyHeld = true })
  local res = w.run(swap())
  T.check(res == 'done' and not has(w, 'release') and not has(w, 'hold'), 'their hold is left alone')
end

T.done('test_swapgen_commit')
