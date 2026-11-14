-- A generation swap's commit (server earthd Connection.stale_swap; roads.lua swapBegin .. swapFinish; core.lua's
-- 'swap_begin' / 'swap_end' / 'swap_abort' and its frame step). The server shows a place from its last version and,
-- once this version is built, sends the ground that changed under a token: terrain blocks (held here) and road skins
-- (staged hidden by roads.lua). This module decides, frame by frame, what happens to one swap `sw`
-- { gen, terrains = {}, t0, ended, skins } through the functions `d` core hands it (so it runs in tests without the
-- engine):
--   staging   until the server said it sent everything (ended) and roads.swapStaged says every part is made. Anything
--             that failed to stage, or MAX_MS over: given up (roads.swapAbort), ok = false.
--   commit    one frame: the car is held (d.hold), the skins switched (roads.swapCommit: the old ones kept), the terrain
--             committed (d.commitTerrain). A terrain that cannot be imported: the skins put back (roads.swapUndo), the
--             old terrain still there, the hold released, ok = false.
--   settle    the frames until the collision of both is in effect (roads.swapSettled: the rebuild forced after the
--             terrain swap's quiet frames, and a frame more). Only then the old skins go (roads.swapFinish), the hold
--             is released and the server told ok = true. SETTLE_MAX_MS over: the old terrain committed again
--             (d.restoreTerrain) and the skins put back, ok = false.
-- Never an old road surface or sidewalk collision on the new terrain, nor the reverse; never ok before both are in.
local M = {}

M.MAX_MS = 120000           -- staging at most
M.SETTLE_MAX_MS = 5000      -- from the commit to its collision in effect at most

-- One frame of swap sw. Returns nil while it runs, 'done' or 'failed' when it is over (the caller forgets it).
-- d: { clock (ms), roads, minimized() -> bool, classify(msg) -> 'now' (the block in use) | 'later' (the block about to
-- be committed) | nil, commitTerrain(msg) -> bool, restoreTerrain() -> bool, setLater(msg), hold(on) -> true when this
-- call changed the hold, send(msg) }
function M.step(sw, d)
  local roads = d.roads
  local function over(ok, why)
    if sw.held then d.hold(false); sw.held = nil end
    d.send({ type = 'swap_done', gen = sw.gen, ok = ok, why = why })
    return ok and 'done' or 'failed'
  end
  if sw.phase == 'settle' then
    if roads.swapSettled(sw.gen) then
      roads.swapFinish(sw.gen)
      if sw.later then d.setLater(sw.later) end
      return over(true)
    end
    if d.clock - sw.tCommit > M.SETTLE_MAX_MS then
      roads.swapUndo(sw.gen)
      if sw.now then d.restoreTerrain() end
      return over(false, 'the collision of the swap did not come')
    end
    return nil
  end
  if d.clock - sw.t0 > M.MAX_MS then
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
  if now and not d.commitTerrain(now) then
    roads.swapUndo(sw.gen)
    return over(false, 'the terrain of the swap could not be imported')
  end
  sw.now, sw.later, sw.phase, sw.tCommit = now, later, 'settle', d.clock
  return nil
end

return M
