-- The ground's height anywhere the world is drawn, for other mods (api.groundHeight, 1.13) and for the game's own
-- terrain query (core_terrain.getTerrainHeight) on BeamNG.Earth's level (the 2026-11-27 AWS Tornado study,
-- ~/bnge-tests/compat/AWS_COMPAT.md).
--
-- Why: a stock level's TerrainBlock covers the whole map, so core_terrain.getTerrainHeight and be:getSurfaceHeightBelow
-- answer the ground everywhere a mod looks. Ours is one block about 1 km wide that follows the player (terrain.lua);
-- past it the ground is drawn by the far tiles (farfield.lua: render-only, no collision). There the stock terrain query
-- answers the block's floor (its zBase, the lowest ground round the player) and the physics queries answer 0 (in game
-- at Boulder, 2026-11-27: the land 1.6-3 km up), so a mod placing something on the ground a few km away put it at the
-- wrong height, and it jumped to the right one once the block reached it.
--
-- What: height(x, y) in the current anchor's game coordinates:
--   inside the block's collidable box: the terrain's own height (TerrainBlock:getHeight, what the stock query answers);
--   past it: the land's height from the app (earthd 'ground': the DEM at the zoom of the far tile drawn there), on a
--   world-locked CELL m lattice, blended between a cell's four corners (bilinear), so a point moving across the land
--   gets a smooth height and the same point the same answer.
-- A corner not known yet is asked for (batched, sent from core.lua's update while any wait: flush) and the call answers
-- nil meanwhile (getTerrainHeight: the stock answer); the answer comes a frame or a few later. Corners are kept per
-- anchor (a rebase or teleport starts afresh). Asking is capped (ASK_PER_S corners a second), so a caller asking for a
-- whole map at once gets nil past the cap rather than flooding the app.
-- Why not a ray through the drawn far tiles (Engine.castRay with render geometry, the first version): in game it cost
-- ~80 us a ray warm but 1-50 ms on the first ray through a mesh (and 0.9 s for the session's first), so one far point
-- could make a long frame; the app's answer costs the game nothing.
-- Nothing here runs unless something asks: no per-frame work while nothing waits (core.lua checks one flag), no objects.
--
-- Not covered: be:getSurfaceHeightBelow and castRayStatic (engine methods, physics only) still answer 0 past the block
-- and the colliders; mods should prefer api.groundHeight (docs/ADDONS.md "The ground").
local M = {}

M.CELL = 16               -- m: the lattice of the land's heights past the block
M.MAX_CORNERS = 60000     -- kept at most (then all dropped and asked afresh)
M.BATCH = 512             -- corners a request (earthd GROUND_POINTS_MAX)
M.IN_FLIGHT = 4           -- requests waiting for an answer at most
M.WAIT_S = 5              -- s: a request not answered by then is given up (its corners asked again when needed)
M.ASK_PER_S = 2000        -- corners asked a second at most ...
M.ASK_BURST = 2000        -- ... and at once
M.EDGE_M = 0.5            -- m inside the block's collidable box still counts as the block

local lattice = require('beamngEarth/lattice')

local floor, min = math.floor, math.min

local corners, cornerCount, cacheAnchor = {}, 0, nil   -- key -> z (number) or false (no height there)
local waiting = {}         -- key -> true: asked (queued or sent)
local queue = {}           -- { key, x, y } not sent yet
local requests, nextId = {}, 0      -- id -> { keys, at, anchorId }
local inFlight = 0
local allowance, allowanceAt = M.ASK_BURST, nil
local clock = 0
local stats = { calls = 0, terrain = 0, land = 0, waits = 0, none = 0, asked = 0, answered = 0, capped = 0,
  requests = 0, lost = 0, drops = 0 }
local orig, wrapper, wrappedTable = nil, nil, nil
M.dirty = false           -- corners queued: core.lua calls flush

local function coreState()
  local c = rawget(_G, 'beamngEarth_core')
  if not (c and c._state) then return nil end
  local ok, st = pcall(c._state)
  return ok and type(st) == 'table' and st or nil
end

-- (tests) the clock (s) and the connection
M._now, M._net = nil, nil
local function now()
  if M._now then return M._now() end
  return os.clock()       -- (in the game os.clock reads wall time: stopwatch.lua)
end
local function net()
  if M._net then return M._net end
  local ok, n = pcall(require, 'beamngEarth/net')
  return ok and n or nil
end

local function key(i, j) return (i + 1048576) * 2097152 + (j + 1048576) end

local function forget(aid)
  corners, cornerCount, cacheAnchor = {}, 0, aid
  waiting, queue, requests, inFlight = {}, {}, {}, 0
  M.dirty = false
end

-- May one more corner be asked for now?
local function mayAsk()
  local t = now()
  if allowanceAt then allowance = min(M.ASK_BURST, allowance + (t - allowanceAt) * M.ASK_PER_S) end
  allowanceAt = t
  if allowance < 1 then return false end
  allowance = allowance - 1
  return true
end

-- The land's height at lattice corner (i, j): z, false (none there), or nil (not known yet: asked for, or the asking
-- cap reached).
local function corner(i, j)
  local k = key(i, j)
  local z = corners[k]
  if z ~= nil then return z end
  if waiting[k] then return nil end
  local n = net()
  if not (n and n.isConnected and n.isConnected()) then return nil end
  if not mayAsk() then stats.capped = stats.capped + 1 return nil end
  waiting[k] = true
  queue[#queue + 1] = { k, i * M.CELL, j * M.CELL }
  stats.asked = stats.asked + 1
  M.dirty = true
  return nil
end

-- Is (x, y) on the committed block's collidable ground?
local function onBlock(st, x, y)
  local b = st and st.block
  if not b then return false end
  local x0, y0, x1, y1 = lattice.groundBox(b.cx, b.cy)
  local e = M.EDGE_M
  return x >= x0 + e and x <= x1 - e and y >= y0 + e and y <= y1 - e
end

-- The block's own height at (x, y) (what the stock getTerrainHeight answers: core_terrain.getTerrain():getHeight)
local function blockHeight(x, y)
  local ct = rawget(_G, 'core_terrain')
  if not (ct and ct.getTerrain) then return nil end
  local ok, h = pcall(function()
    local tb = ct.getTerrain()
    return tb and tb:getHeight(vec3(x, y, 0))
  end)
  if ok and type(h) == 'number' and h == h then return h end
  return nil
end

-- The ground's height at (x, y) (game coordinates of the current anchor): z, kind ('terrain': the block's ground,
-- what vehicles drive on; 'land': the land's height past it, as drawn, nothing collides there), or nil (off
-- BeamNG.Earth's level, not known yet: asked for, a frame or a few; no elevation there; the asking cap reached).
function M.height(x, y)
  x, y = tonumber(x), tonumber(y)
  if not x or not y or x ~= x or y ~= y then return nil end
  stats.calls = stats.calls + 1
  local st = coreState()
  if not (st and st.inOurLevel and st.anchorId) then stats.none = stats.none + 1 return nil end
  if onBlock(st, x, y) then
    local h = blockHeight(x, y)
    if h then stats.terrain = stats.terrain + 1 return h, 'terrain' end
  end
  if cacheAnchor ~= st.anchorId then forget(st.anchorId) end
  local C = M.CELL
  local fx, fy = x / C, y / C
  local i, j = floor(fx), floor(fy)
  local u, v = fx - i, fy - j
  -- (all four asked at once, so one request carries them)
  local z00, z10, z01, z11 = corner(i, j), corner(i + 1, j), corner(i, j + 1), corner(i + 1, j + 1)
  if z00 == nil or z10 == nil or z01 == nil or z11 == nil then stats.waits = stats.waits + 1 return nil end
  if not (z00 and z10 and z01 and z11) then stats.none = stats.none + 1 return nil end
  stats.land = stats.land + 1
  return (z00 * (1 - u) + z10 * u) * (1 - v) + (z01 * (1 - u) + z11 * u) * v, 'land'
end

-- (core.lua's update, only while M.dirty) the queued corners sent, BATCH a request, IN_FLIGHT at most; requests not
-- answered in WAIT_S given up.
function M.flush()
  local t = now()
  for id, r in pairs(requests) do
    if t - r.at > M.WAIT_S then
      for _, k in ipairs(r.keys) do waiting[k] = nil end
      requests[id], inFlight, stats.lost = nil, inFlight - 1, stats.lost + 1
    end
  end
  local n = net()
  if not (n and n.send and cacheAnchor) then M.dirty = #queue > 0 return end
  while #queue > 0 and inFlight < M.IN_FLIGHT do
    local keys, pts = {}, {}
    local take = min(#queue, M.BATCH)
    for q = 1, take do
      local e = queue[q]
      keys[q] = e[1]
      pts[2 * q - 1], pts[2 * q] = e[2], e[3]
    end
    local rest = {}
    for q = take + 1, #queue do rest[#rest + 1] = queue[q] end
    queue = rest
    nextId = nextId + 1
    requests[nextId] = { keys = keys, at = t, anchorId = cacheAnchor }
    inFlight = inFlight + 1
    stats.requests = stats.requests + 1
    n.send({ type = 'ground', id = nextId, anchorId = cacheAnchor, pts = pts })
  end
  M.dirty = #queue > 0 or next(requests) ~= nil
end

-- (core.lua) the app's ground_result {id, anchorId, z = {m or nil...}, error?}
function M.onResult(msg)
  local r = type(msg) == 'table' and requests[msg.id]
  if not r then return end
  requests[msg.id], inFlight = nil, inFlight - 1
  M.dirty = #queue > 0 or next(requests) ~= nil
  if r.anchorId ~= cacheAnchor or msg.anchorId ~= cacheAnchor then return end    -- (a frame since gone)
  local z = type(msg.z) == 'table' and msg.z or {}
  local failed = type(msg.error) == 'string'
  for q, k in ipairs(r.keys) do
    waiting[k] = nil
    if not failed then
      if cornerCount >= M.MAX_CORNERS then
        corners, cornerCount, stats.drops = {}, 0, stats.drops + 1
      end
      local v = tonumber(z[q])
      if corners[k] == nil then cornerCount = cornerCount + 1 end
      corners[k] = (v and v == v) and v or false
      stats.answered = stats.answered + 1
    end
  end
end

-- core_terrain.getTerrainHeight on our level: the terrain's own answer on the block, the land's height past it (where
-- the stock query answers the block's floor), the stock answer where none is known yet. Each install wraps the
-- function it found (base), so a chain with another mod's wrap never calls round in a loop.
local function makeWrapper(base)
  return function(point)
    local x, y = point and tonumber(point.x), point and tonumber(point.y)
    if x and y then
      local st = coreState()
      if st and st.inOurLevel and not onBlock(st, x, y) then
        local h = M.height(x, y)
        if h then return h end
      end
    end
    return base(point)
  end
end

-- Our level in play (core.lua onLevelStart): core_terrain.getTerrainHeight answers the land past the block; false
-- (another level, the level's end): the stock function back. A wrap another mod put over ours since is left alone
-- (ours under it falls back to the function it wrapped off our level).
function M.setLevel(ours)
  local ct = rawget(_G, 'core_terrain')
  if ours then
    if not (ct and type(ct.getTerrainHeight) == 'function') then return false end
    if wrappedTable == ct and ct.getTerrainHeight == wrapper then return true end
    orig, wrappedTable = ct.getTerrainHeight, ct
    wrapper = makeWrapper(orig)
    ct.getTerrainHeight = wrapper
    return true
  end
  if wrappedTable and wrappedTable.getTerrainHeight == wrapper then wrappedTable.getTerrainHeight = orig end
  orig, wrapper, wrappedTable = nil, nil, nil
  forget(nil)
  return false
end

function M.wrapped()
  local ct = rawget(_G, 'core_terrain')
  return ct ~= nil and wrapper ~= nil and ct.getTerrainHeight == wrapper
end

function M.stats()
  local out = { corners = cornerCount, queued = #queue, inFlight = inFlight }
  for k, v in pairs(stats) do out[k] = v end
  return out
end

function M._reset()
  M.setLevel(false)
  forget(nil)
  allowance, allowanceAt, nextId = M.ASK_BURST, nil, 0
  for k in pairs(stats) do stats[k] = 0 end
end

return M
