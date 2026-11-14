-- A generation swap's commit (server earthd Connection.stale_swap; roads.lua swapBegin .. swapFinish; core.lua's
-- 'swap_begin' / 'swap_end' / 'swap_abort' and its frame step). The server shows a place from its last version and,
-- once this version is built, sends the ground that changed under a token: terrain blocks (held here) and road skins
-- (staged hidden by roads.lua). This module decides, frame by frame, what happens to one swap `sw`
-- { gen, terrains = {}, t0, ended, skins } through the functions `d` core hands it (so it runs in tests without the
-- engine). ALL TIMES ARE SECONDS (core's st.clock).
--   staging   until the server said it sent everything (ended) and roads.swapStaged says every part is made. Anything
--             that failed to stage, or MAX_S over: given up (roads.swapAbort), ok = false. Nothing was changed.
--   commit    one frame: the car is held (d.hold), the skins switched (roads.swapCommit: the old ones kept), the terrain
--             committed (d.commitTerrain). A terrain that cannot be imported: rollback.
--   settle    the frames until the collision of both is in effect (roads.swapSettled: the rebuild forced after the
--             terrain swap's quiet frames, and a frame more). Only then the old skins go (roads.swapFinish), the hold
--             is released and the server told ok = true. SETTLE_MAX_S over: rollback.
--   rollback  the old terrain committed again (d.restoreTerrain: the block that was in use, from its own files, which a
--             swap never writes over), the skins put back (roads.swapUndo), and the hold KEPT until the restored
--             collision is in effect (roads.swapRolledBack), ROLLBACK_MAX_S at most; then released, ok = false. When
--             the old terrain cannot be had again (no snapshot of it, or its import fails) there is no rollback: the
--             commit stands (this version's skins on this version's terrain), the hold kept until its collision is
--             in effect (STAND_MAX_S at most), ok = true.
-- From the commit on the swap is a local transaction: a lost connection, an abort or a deadline of the server no longer
-- stop it; it ends committed or rolled back, and only then is the car let go (M.live: core keeps stepping it).
-- Never an old road surface or sidewalk collision on the new terrain, nor the reverse, under a car that is free.
local M = {}

M.MAX_S = 120               -- staging at most
M.SETTLE_MAX_S = 5          -- from the commit to its collision in effect at most
M.ROLLBACK_MAX_S = 5        -- the hold after a rollback at most
M.STAND_MAX_S = 30          -- a commit that cannot be rolled back: its collision waited for this long at most

-- Is sw past its commit frame (a local transaction that must be stepped to its end whatever the server does)?
function M.live(sw)
  return sw ~= nil and (sw.phase == 'settle' or sw.phase == 'rollback' or sw.phase == 'stand')
end

-- One frame of swap sw. Returns nil while it runs, 'done' or 'failed' when it is over (the caller forgets it).
-- d: { clock (s), roads, minimized() -> bool, classify(msg) -> 'now' (the block in use) | 'later' (the block about to
-- be committed) | nil, commitTerrain(msg) -> bool, restoreTerrain() -> bool (false: no snapshot of the old block, or it
-- cannot be imported), setLater(msg), hold(on) -> true when this call changed the hold, send(msg) }
function M.step(sw, d)
  local roads = d.roads
  local function over(ok, why)
    if sw.held then d.hold(false); sw.held = nil end
    d.send({ type = 'swap_done', gen = sw.gen, ok = ok, why = why })
    return ok and 'done' or 'failed'
  end
  local function rollback(why)
    -- the old terrain first: without it the old skins must not come back
    if sw.now and not d.restoreTerrain() then
      sw.phase, sw.tStand, sw.why = 'stand', d.clock, why .. '; the old terrain could not be had again'
      return nil
    end
    roads.swapUndo(sw.gen)
    sw.phase, sw.tRollback, sw.why = 'rollback', d.clock, why
    return nil
  end
  if sw.phase == 'rollback' then
    if roads.swapRolledBack() or d.clock - sw.tRollback > M.ROLLBACK_MAX_S then return over(false, sw.why) end
    return nil
  end
  if sw.phase == 'stand' then
    if roads.swapSettled(sw.gen) or d.clock - sw.tStand > M.STAND_MAX_S then
      roads.swapFinish(sw.gen)
      if sw.later then d.setLater(sw.later) end
      return over(true, sw.why)
    end
    return nil
  end
  if sw.phase == 'settle' then
    if roads.swapSettled(sw.gen) then
      roads.swapFinish(sw.gen)
      if sw.later then d.setLater(sw.later) end
      return over(true)
    end
    if d.clock - sw.tCommit > M.SETTLE_MAX_S then return rollback('the collision of the swap did not come') end
    return nil
  end
  if sw.cancel then                                 -- (the server gave it up, or is gone, before the commit)
    roads.swapAbort(sw.gen)
    return over(false, sw.cancel)
  end
  if d.clock - sw.t0 > M.MAX_S then
    roads.swapAbort(sw.gen)
    return over(false, 'not staged in time')
  end
  local ready, failed = roads.swapStaged(sw.gen, sw.ended and sw.skins or nil)
  if failed then
    roads.swapAbort(sw.gen)
    return over(false, failed)
  end
  if not sw.ended or not ready or d.minimized() then return nil end
  local now, later = nil, nil
  for _, msg in ipairs(sw.terrains) do
    local c = d.classify(msg)
    if c == 'now' then now = msg elseif c == 'later' then later = msg
    else
      roads.swapAbort(sw.gen)
      return over(false, 'a block of the swap is no longer in use')
    end
  end
  if not now and roads.swapCount(sw.gen) == 0 then      -- nothing of the ground in use changed
    roads.swapCommit(sw.gen)
    roads.swapFinish(sw.gen)
    if later then d.setLater(later) end
    return over(true)
  end
  -- the commit: the car held, the skins and the terrain in this one frame
  sw.held = d.hold(true) or nil
  roads.swapCommit(sw.gen)
  sw.now, sw.later, sw.tCommit = nil, later, d.clock
  if now and not d.commitTerrain(now) then
    -- (the import failed: the old terrain is still the one in use, nothing to restore)
    roads.swapUndo(sw.gen)
    sw.phase, sw.tRollback, sw.why = 'rollback', d.clock, 'the terrain of the swap could not be imported'
    return nil
  end
  sw.now, sw.phase = now, 'settle'
  return nil
end

return M
