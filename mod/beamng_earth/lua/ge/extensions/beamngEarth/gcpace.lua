-- Lua garbage collection pacing (the user, 2026-11-11: "look into better garbage collectors ... That might be causing
-- some of the ram or long session issues").
--
-- BeamNG runs GE Lua's collector itself once a frame (the engine's LuaGC section; BeamNG.drive.x64 0.40, read from its
-- machine code on 2026-11-11, the function that calls lua_gc COUNT / STEP / COLLECT):
--   kb = the heap (KB); rate = rate + 0.1 * (max(1, kb - lastKb) - rate)      (its smoothed growth a frame)
--   if kb > T then FULL COLLECTION; T = max(ratio, 1.33) * the heap after it  (ratio: a slow average of before/after)
--   else lua_gc(LUA_GCSTEP, rate) end                                         (one incremental step)
-- (LuaJIT's own incremental collector runs too, stepping on allocations.) So whenever the heap passes 1.33x what it was
-- after the engine's last full collection - garbage outran the running incremental cycle (a burst of decoded
-- messages), or the live data grew by a third - the whole heap is marked and swept in one frame. Checked in-game on the
-- Windows test PC (2026-11-11, an aged session): a ballast lifting the heap 157 -> 252 MB gave a 310 ms LuaGC frame
-- in the next frame and the heap fell to 223 MB (the ballast still held); a full collectgarbage() of the 124 MB live
-- heap took 116-127 ms. Players' telemetry: LuaGC 100-180 ms frames, a 6-core player's logs 140-190 ms. The GE heap
-- is mostly the game's own (UI, vehicles, gameplay; code): our modules' tables were ~10 MB of 124 MB (trees' rows are
-- FFI arrays already), so a smaller heap of ours would not make these much cheaper.
--
-- This module keeps those full collections out of play:
--  1. pacing (PACE): once the garbage over the live data (the smallest heap of the last LIVE_S) has taken PACE_START of
--     the room left under the engine's trigger, it steps the incremental collector itself (collectgarbage('step',
--     STEP_KB)) within a small time budget a frame (BUDGET_MS, rising to MAX_MS as the room runs out; one step that
--     meets the cycle's atomic phase can take a few ms more), so the running cycle ends and frees it first;
--  2. moving the unavoidable one (PROVOKE): in a frame the player is not driving in (the loading cover after a
--     teleport, the game paused, the 3D map open) with the live data grown PROVOKE_LIVE of the room (or the heap past
--     PROVOKE_AT of it), it lets the engine do its full collection there: a ballast (BALLAST of the heap, at least
--     past the trigger) held over that frame, so the next trigger is reckoned from the larger heap (1.33 x (live +
--     ballast)); the ballast is dropped once the collection is seen. A full collection in play is then one the
--     pacing could not keep off. (Test PC, 24 minutes of drives and teleports: one provoked in a loading cover, the
--     trigger then at 205 MB over a 124-168 MB heap, none in play; pacing 0.004 ms a frame on average.)
-- The engine's trigger is not readable from Lua: T is estimated from the heap seen right after a full collection
-- (a fall of more than FULL_DROP in one frame, PROVOKED_DROP after a provoked frame), raised whenever the heap is
-- above it (the engine would have collected), and before the first collection seen from the smallest heap (a
-- provoked frame that brought none raises that guess for good: floor).
-- Our level only (core.lua calls update); other maps keep the stock behaviour. M.PACE / M.PROVOKE = false turn the
-- parts off (beamngEarth_core._gcpace from the console).

local M = {}

M.PACE = true
M.PROVOKE = true
M.RATIO = 1.33          -- the engine's (its constant)
M.FULL_DROP = 0.8       -- the heap falling below this share of the last frame's: a full collection ran
M.FULL_MIN_KB = 4096    -- ... by at least this much
M.PACE_START = 0.5      -- share of the room over the live data (live .. T) the garbage takes before we step
M.STEP_KB = 32          -- collectgarbage('step', n) granularity
M.BUDGET_MS = 0.4       -- per frame at PACE_START, rising to MAX_MS when the room is used up
M.MAX_MS = 2.0
M.PROVOKE_AT = 0.8      -- or the heap this far up the room (base .. T): provoke when hidden
M.BALLAST = 0.15        -- of the heap, held over the provoked collection (its allocation also lifts the engine's step
                        -- size for ~6 frames: 10-50 ms LuaGC frames after a 94 MB one on the Windows test PC)
M.PROVOKE_GAP_S = 5     -- at most one provoked collection this often
M.PROVOKE_FRAMES = 3    -- frames the ballast is held for the engine's collection
M.PROVOKED_DROP = 0.95  -- ... and the fall that shows it ran then (the ballast stays: a smaller fall)
M.PROVOKE_LIVE = 0.5    -- live data grown this share of the room since the last full collection: provoke when hidden
M.LIVE_S = 20           -- seconds the live data is taken over (the smallest heap)
M.PACE_MIN_KB = 2048    -- garbage below this: no pacing

local st = {
  base = nil,           -- the heap (KB) after the engine's last full collection (estimate)
  exact = false,        -- base seen right after a full collection (not only bounded from below)
  floor = 0,            -- before that: a provoked frame that brought no collection bounds the base from below
  prevKb = nil,
  mins = {}, minAt = nil, curMin = nil,   -- the smallest heap of each of the last LIVE_S seconds (the live data)
  ballast = nil,        -- held over the provoked frame
  provokedAt = nil, provokeKb = nil, provokeFrames = 0,
  lastProvoke = -math.huge,
}
local stats = { fulls = 0, fullsHidden = 0, provoked = 0, provokeMissed = 0, paceMs = 0, paceFrames = 0, paceMax = 0,
  cycles = 0, frames = 0, lastFullKb = nil }

-- The ballast: n bytes the collector counts (an FFI byte array: no copy, no string interning; a string without FFI).
local newBallast = (function()
  local ok, ffi = pcall(require, 'ffi')
  if ok and type(ffi) == 'table' and type(ffi.new) == 'function' and pcall(ffi.new, 'uint8_t[?]', 1) then
    return function(n) return ffi.new('uint8_t[?]', n) end
  end
  return function(n) return string.rep('\0', n) end
end)()
function M.setBallast(f) newBallast = f end       -- (tests)

local clock = os.clock
-- (os.clock is the wall clock in BeamNG's Lua: engine-shape-load-cost notes; a test may replace it)
function M.setClock(f) clock = f or os.clock end

local collect = collectgarbage
function M.setCollector(f) collect = f or collectgarbage end      -- (tests)

function M.reset()
  st.base, st.exact, st.prevKb, st.ballast, st.provokedAt, st.provokeKb = nil, false, nil, nil, nil, nil
  st.floor = 0
  st.mins, st.minAt, st.curMin = {}, nil, nil
  st.lastProvoke = -math.huge
end

-- The engine's trigger estimate (KB) or nil.
function M.trigger() return st.base and st.base * M.RATIO or nil end

-- The live data estimate (KB): the smallest heap over the last LIVE_S seconds, or nil.
function M.live()
  local m = st.curMin
  for i = 1, #st.mins do if not m or st.mins[i] < m then m = st.mins[i] end end
  return m
end

local function noteMin(now, kb)
  if not st.minAt then st.minAt = now end
  if not st.curMin or kb < st.curMin then st.curMin = kb end
  if now - st.minAt >= 1 then
    table.insert(st.mins, st.curMin)
    if #st.mins > M.LIVE_S then table.remove(st.mins, 1) end
    st.minAt, st.curMin = now, kb
  end
end

-- One frame (our level, after the mod's own work). now: seconds (any clock); hidden: the player is not driving in this
-- frame (loading cover, paused, the 3D map). Returns the ms spent stepping.
function M.update(now, hidden)
  stats.frames = stats.frames + 1
  local kb = collect('count')
  local prev = st.prevKb
  local drop = prev and prev - kb or 0
  if drop >= M.FULL_MIN_KB and (kb < prev * M.FULL_DROP or (st.provokedAt and kb < prev * M.PROVOKED_DROP)) then
    -- the engine's full collection ran since the last frame: the heap now is its new base
    st.base, st.exact = kb, true
    stats.fulls, stats.lastFullKb = stats.fulls + 1, kb
    if hidden then stats.fullsHidden = stats.fullsHidden + 1 end
    st.provokedAt, st.ballast = nil, nil       -- (the ballast is garbage now: the collections free it)
    st.mins, st.minAt, st.curMin = {}, nil, nil
  elseif st.provokedAt then
    st.provokeFrames = st.provokeFrames + 1
    if st.provokeFrames > M.PROVOKE_FRAMES then
      -- no full collection: the trigger is above what we held (our estimate was low)
      stats.provokeMissed = stats.provokeMissed + 1
      if st.provokeKb then
        st.floor = math.max(st.floor, st.provokeKb / M.RATIO)
        st.base = math.max(st.base or 0, st.floor)
      end
      st.provokedAt, st.ballast = nil, nil
    end
  end
  -- the engine's trigger is always above the heap (it collects in the frame the heap passes it, and then reckons it
  -- from the heap after): a heap above our estimate means the base is higher (a collection that freed too little to be
  -- seen, after the live data grew); before the first collection seen, the smallest heap is the guess
  if not st.base then st.base = kb
  elseif kb > st.base * M.RATIO then st.base = kb / M.RATIO
  elseif not st.exact and kb < st.base and not st.provokedAt then st.base = math.max(kb, st.floor) end
  noteMin(now, kb)
  local T, live = st.base * M.RATIO, M.live() or kb
  local spent = 0
  if st.provokedAt then
    -- (waiting for the engine's collection: nothing else)
  elseif M.PROVOKE and hidden and now - st.lastProvoke >= M.PROVOKE_GAP_S
      and (live - st.base > M.PROVOKE_LIVE * (T - st.base) or kb - st.base > M.PROVOKE_AT * (T - st.base)) then
    -- the live data took much of the room since the last full collection (or the heap is near the trigger): lift the
    -- heap past the trigger for the engine's collection (this frame's or the next), with ballast still live over it
    local want = math.max(T * 1.02 - kb, kb * M.BALLAST)
    st.ballast = newBallast(math.floor(want * 1024))
    st.provokedAt, st.provokeKb, st.lastProvoke, st.provokeFrames = now, kb + want, now, 0
    stats.provoked = stats.provoked + 1
  elseif M.PACE and T > live and kb - live >= M.PACE_MIN_KB then
    -- the garbage since the live level took more than PACE_START of what is left under the trigger: step the running
    -- incremental cycle on so it ends (and frees it) first
    local used = (kb - live) / (T - live)
    if used > M.PACE_START then
      local urgency = math.min(1, (used - M.PACE_START) / (1 - M.PACE_START))
      local budget = (M.BUDGET_MS + (M.MAX_MS - M.BUDGET_MS) * urgency) / 1000
      local t0 = clock()
      repeat
        if collect('step', M.STEP_KB) then stats.cycles = stats.cycles + 1; break end
      until clock() - t0 >= budget
      spent = (clock() - t0) * 1000
      stats.paceMs, stats.paceFrames = stats.paceMs + spent, stats.paceFrames + 1
      if spent > stats.paceMax then stats.paceMax = spent end
    end
  end
  st.prevKb = collect('count')
  return spent
end

-- Diagnostics: counters since the level started and the estimate.
function M.status()
  local kb = collect('count')
  return { fulls = stats.fulls, fullsHidden = stats.fullsHidden, provoked = stats.provoked,
    provokeMissed = stats.provokeMissed, cycles = stats.cycles, paceFrames = stats.paceFrames,
    paceMs = math.floor(stats.paceMs + 0.5), paceMax = math.floor(stats.paceMax * 10 + 0.5) / 10,
    frames = stats.frames, heapMB = math.floor(kb / 1024 + 0.5),
    baseMB = st.base and math.floor(st.base / 1024 + 0.5), triggerMB = st.base and math.floor(st.base * M.RATIO / 1024 + 0.5),
    liveMB = M.live() and math.floor(M.live() / 1024 + 0.5), exact = st.exact }
end

function M.resetStats()
  for k, v in pairs(stats) do if type(v) == 'number' then stats[k] = 0 end end
  stats.lastFullKb = nil
end

return M
