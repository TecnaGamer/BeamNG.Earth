-- KyzoFox 1.5.4: the road guard. The road tiles (roads.lua) come from the server a few seconds to minutes
-- before the car needs them, but not always: a slow or failed download, a long bridge chain, a busy server. A bridge
-- deck missing or not solid yet under a truck at highway speed was a fall and a hard crash (the user, 2026-10-20:
-- Tuscaloosa and Greene County, AL; "sometimes I have to hit J to pause the game to let the roads render").
-- So the mod does what the player did by hand, anywhere in the world: while the car is driving, the squares it will
-- cover in the next LEAD_S seconds must be ready (roads.squareState: road tile made, decks and tunnels solid, or no
-- roads there at all); if one is not, the game's own pause holds it (as edgeguard.lua and drophold.lua do), with a
-- message, until they are, at most MAX_HOLD_S. The player resuming by hand wins: no new hold for GRACE_S.
-- step() is pure (luajit tests: mod/tests/test_roadguard.lua).

local M = {}

M.enabled = true
M.LEAD_S = 4            -- s of driving ahead that must be ready
M.MIN_AHEAD_M = 40      -- m ahead at least
M.STEP_M = 24           -- m between the points checked (squares are 256 m)
M.MIN_SPEED = 4         -- m/s: slower (parking, crawling) is never held
M.MAX_HOLD_S = 25       -- s: a hold ends after this whatever is missing
M.GRACE_S = 10          -- s after a hold ended (or the player resumed): no new hold
M.WAITING = { missing = true, building = true, solidifying = true }

function M.new()
  return { pausedByUs = false, since = nil, endedAt = -math.huge, holds = 0, last = nil }
end

-- The points the car covers in the next LEAD_S seconds (its position first). Pure.
function M.points(x, y, vx, vy)
  local v = math.sqrt(vx * vx + vy * vy)
  local pts = { { x, y } }
  if v < 1e-6 then return pts end
  local ahead = math.max(M.MIN_AHEAD_M, v * M.LEAD_S)
  local ux, uy = vx / v, vy / v
  local d = M.STEP_M
  while d < ahead do
    pts[#pts + 1] = { x + ux * d, y + uy * d }
    d = d + M.STEP_M
  end
  pts[#pts + 1] = { x + ux * ahead, y + uy * ahead }
  return pts
end

-- The first point's state that is waited for, or nil when all are ready. state(x, y) -> roads.squareState's. Pure.
function M.waitingFor(pts, state)
  for _, p in ipairs(pts) do
    local s = state(p[1], p[2])
    if M.WAITING[s] then return s, p end
  end
  return nil
end

-- One frame. g: M.new(); car: { x, y, vx, vy } (anchor frame; nil: no car); paused: the game is paused now; now: s;
-- state: as waitingFor. Returns 'pause', 'resume' or nil (and g.last: what it waits for).
function M.step(g, car, paused, now, state)
  if g.pausedByUs then
    if not paused then                                  -- the player resumed by hand
      g.pausedByUs, g.since, g.endedAt = false, nil, now
      return nil
    end
    local c = g.car
    local w = c and M.waitingFor(M.points(c.x, c.y, c.vx, c.vy), state)
    g.last = w
    if not w or now - g.since >= M.MAX_HOLD_S then
      g.pausedByUs, g.since, g.endedAt = false, nil, now
      return 'resume'
    end
    return nil
  end
  if not M.enabled or paused or not car or now - g.endedAt < M.GRACE_S then return nil end
  if car.vx * car.vx + car.vy * car.vy < M.MIN_SPEED * M.MIN_SPEED then return nil end
  local w = M.waitingFor(M.points(car.x, car.y, car.vx, car.vy), state)
  g.last = w
  if not w then return nil end
  g.pausedByUs, g.since, g.car, g.holds = true, now, { x = car.x, y = car.y, vx = car.vx, vy = car.vy }, g.holds + 1
  return 'pause'
end

-- The game is left running (extension unload, a teleport or rebase coming): a hold ends now.
function M.release(g, now)
  if g.pausedByUs then
    local sta = rawget(_G, 'simTimeAuthority')
    if sta and sta.pause and sta.getPause and sta.getPause() then sta.pause(false) end
  end
  g.pausedByUs, g.since, g.endedAt = false, nil, now or g.endedAt
end

return M
