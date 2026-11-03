-- Terrain-follow: the drivable TerrainBlock is re-centred under the player in the SAME anchor
-- frame (docs/V1_1_SPEC.md 1a, docs/V1_2_DESIGN.md 1.2, 2.3, 12). Pure math + the prepare/commit
-- rule, so it runs under plain luajit.
--
-- The block has N = 1024 samples at squareSize 2.0 m (lattice.lua). Its collidable area is
-- [cx-1024, cx+1022] x [cy-1024, cy+1022] (2046 m, probe P1); it sits on the 8 x 8 L0 far tiles of
-- [cx-1024, cx+1024]^2. Its centre is always a multiple of 256 m.
--
-- Block size (docs/V1_4_SPEC.md 3.5, M4): configure(size) from the server's hello.blockSize picks the
-- table below and sets every active value (M.*) and derived local (REACH, ...). 512²: collidable
-- [c-512, c+510], centres on a 128 m grid, prepare at 128 m, commit at 192 m (hard 320 m), the
-- target 2 s ahead, the player kept >= 250 m inside the edges; fast mode above 150 m/s (target 3 s
-- ahead, >= 90 m inside, the prepare test 0.75 s ahead of the player); and the commit-time re-check:
-- a ready block that the player would leave the prepare box of within RECHECK_S is discarded and
-- re-prepared (two commits 0.3-0.4 s apart otherwise, 134 m/s at 37 deg) - but only while the
-- player stays out of the 150 m edge band for longer than the measured heightmap latency (at least
-- LATENCY_S) + RECHECK_SLACK_S (without that guard the re-check took a 250 m/s car off the block in
-- the offline model), not in fast mode, and never to re-request the same centre. A ready 512² block
-- that would still be no better than the current one at the hard commit distance is dropped too (it
-- blocked every new request until it left the reach). Every constant is flat (`M.X = expr`) so the server's tests read them
-- (server/tests/modsim.py lua_consts).

local lattice = require('beamngEarth/lattice')

local M = {}

local floor, ceil, abs, max, min = math.floor, math.ceil, math.abs, math.max, math.min

-- 1024² (v1.3.1)
M.GRID_1024 = 256
M.PREPARE_DIST_1024 = 300
M.COMMIT_DIST_1024 = 450
M.HARD_COMMIT_DIST_1024 = 700
M.EDGE_MARGIN_1024 = 600
M.REACH_1024 = 1020 - M.EDGE_MARGIN_1024     -- 420 m; the collidable east/north edge is at +1022
M.LOOKAHEAD_S_1024 = 4
M.RECHECK_S_1024 = 0                         -- off
M.FAST_V_1024 = 1e9                          -- no fast mode
-- 512² (M4)
M.GRID_512 = 128
M.PREPARE_DIST_512 = 128
M.COMMIT_DIST_512 = 192
-- The next block's centre at least MIN_LEAD ahead of a moving player (M.target): at town speeds LOOKAHEAD_S put it one
-- grid step ahead, a move every 128 m, each a ~50 ms frame and a redraw of the sun's shadow map on a Steam Deck
-- (2026-10-23, 17 of 21 such redraws on a Baltimore drive); 256 m halves the moves at 16-67 mph, the player still
-- >= 229 m inside the edge in the offline model (the user: "you can try letting it get closer to the edge")
M.MIN_LEAD_512 = 256        -- the next block's centre at least this far ahead of a moving player (M.target)
M.MIN_LEAD_1024 = 0
M.MIN_LEAD_V = 2            -- (moving: faster than this, m/s)
M.HARD_COMMIT_DIST_512 = 320
M.EDGE_MARGIN_512 = 250
M.REACH_512 = 510 - M.EDGE_MARGIN_512        -- 260 m; the collidable east/north edge is at +510
M.LOOKAHEAD_S_512 = 5                       -- (was 2: a hop of 128-256 m at road speeds; 5 s hops 256-384 m: each
                                             -- move is a ~50 ms frame, the new block's first draw; 2026-10-15)
M.RECHECK_S_512 = 2
M.RECHECK_SAFE_512 = 510 - 150               -- the re-check waits only while the player stays this close
M.FAST_V_512 = 150
-- 512² fast mode (|v| > FAST_V)
M.FAST_V = 150
M.EDGE_MARGIN_FAST = 90
M.REACH_FAST = 510 - M.EDGE_MARGIN_FAST      -- 420 m
M.LOOKAHEAD_FAST_S = 3
M.PREP_AHEAD_FAST_S = 0.75
M.RECHECK_SLACK_S = 0.5
M.LATENCY_S = 1                              -- heightmap latency assumed until one is measured, and the
                                             -- re-check's minimum (s)

-- the active table (configure; the 1024² one at load)
M.SIZE = lattice.N             -- samples per side
M.SQUARE_SIZE = lattice.SQUARE -- m
M.HALF = lattice.HALF          -- block SW corner = centre - HALF
M.GRID = M.GRID_1024           -- block centres snap to this grid
M.PREPARE_DIST = M.PREPARE_DIST_1024         -- Chebyshev distance player -> centre that requests a new terrain
M.COMMIT_DIST = M.COMMIT_DIST_1024           -- ... that commits a ready one (once its uncovered strip is live)
M.MIN_LEAD = M.MIN_LEAD_1024
M.HARD_COMMIT_DIST = M.HARD_COMMIT_DIST_1024 -- ... that commits a ready one regardless (still >= 320 m from the edge)
M.EDGE_MARGIN = M.EDGE_MARGIN_1024           -- the new centre keeps the player at least this far from every edge
M.LOOKAHEAD_S = M.LOOKAHEAD_S_1024           -- target = snap256(player + velocity * LOOKAHEAD_S)
M.RECHECK_S = M.RECHECK_S_1024
M.RECHECK_SAFE = M.RECHECK_SAFE_512
M.FAST_MODE_V = M.FAST_V_1024
M.RETRY_S = 5                  -- wait after a terrain_error before asking again

-- Max |centre - player| per axis: 1020 - 600 = 420 m (design 1.2; the server's predict_centre uses
-- the same value). The collidable east/north edge is at +1022, so this keeps >= 602 m there.
M.REACH = M.REACH_1024
local REACH = M.REACH

-- The session's block size (1024 or 512; anything else: 1024): the active table and every value
-- derived from the lattice. Returns the size in use.
function M.configure(size)
  local n = (size == 512) and 512 or 1024
  local t = n == 512 and '_512' or '_1024'
  M.SIZE, M.HALF = n, n * M.SQUARE_SIZE / 2
  M.GRID, M.PREPARE_DIST, M.COMMIT_DIST = M['GRID' .. t], M['PREPARE_DIST' .. t], M['COMMIT_DIST' .. t]
  M.MIN_LEAD = M['MIN_LEAD' .. t]
  M.HARD_COMMIT_DIST, M.EDGE_MARGIN, M.REACH = M['HARD_COMMIT_DIST' .. t], M['EDGE_MARGIN' .. t], M['REACH' .. t]
  M.LOOKAHEAD_S, M.RECHECK_S, M.FAST_MODE_V = M['LOOKAHEAD_S' .. t], M['RECHECK_S' .. t], M['FAST_V' .. t]
  REACH = M.REACH
  return n
end

-- (lookahead s, reach m, prepare-test lead s) at this velocity: the fast mode above FAST_MODE_V.
local function mode(vx, vy)
  vx, vy = vx or 0, vy or 0
  if vx * vx + vy * vy > M.FAST_MODE_V * M.FAST_MODE_V then
    return M.LOOKAHEAD_FAST_S, M.REACH_FAST, M.PREP_AHEAD_FAST_S
  end
  return M.LOOKAHEAD_S, REACH, 0
end

-- The reach at this velocity (REACH; fast mode: REACH_FAST).
function M.reachFor(vx, vy)
  local _, reach = mode(vx, vy)
  return reach
end

function M.snap(v) return floor(v / M.GRID + 0.5) * M.GRID end

function M.cheb(ax, ay, bx, by) return max(abs(ax - bx), abs(ay - by)) end

-- Snap one axis, then pull it back onto the grid inside [p - reach, p + reach].
-- The interval is 840 m wide (512²: 520 m), so it always holds a multiple of the grid.
local function clampAxis(c, p, reach)
  if c > p + reach then
    c = floor((p + reach) / M.GRID) * M.GRID
  elseif c < p - reach then
    c = ceil((p - reach) / M.GRID) * M.GRID
  end
  return c
end

-- Block centre nearest to (tx, ty) that keeps a player at (px, py) within reach (default: the
-- active REACH) of it on both axes.
function M.centreNear(tx, ty, px, py, reach)
  reach = reach or REACH
  return clampAxis(M.snap(tx), px, reach), clampAxis(M.snap(ty), py, reach)
end

-- Centre of the next block for a player at (px, py) moving at (vx, vy) m/s.
function M.target(px, py, vx, vy)
  local t, reach = mode(vx, vy)
  local lx, ly = (vx or 0) * t, (vy or 0) * t
  -- at town speeds LOOKAHEAD_S puts the next block one grid step ahead: a move every 128 m; at least MIN_LEAD ahead
  -- (while moving), every move goes about twice as far
  local v2 = (vx or 0) * (vx or 0) + (vy or 0) * (vy or 0)
  local l2 = lx * lx + ly * ly
  if M.MIN_LEAD > 0 and v2 > M.MIN_LEAD_V * M.MIN_LEAD_V and l2 < M.MIN_LEAD * M.MIN_LEAD then
    local k = M.MIN_LEAD / math.sqrt(v2)
    lx, ly = vx * k, vy * k
  end
  return M.centreNear(px + lx, py + ly, px, py, reach)
end

-- Seconds until a player at (px, py) moving at (vx, vy) is `dist` (Chebyshev) from (cx, cy)
-- (0 if already past it on an axis it moves along; math.huge at rest).
local function axisTime(p, v, c, dist)
  if abs(v) <= 1e-9 then return math.huge end
  return max(0, ((v > 0 and c + dist or c - dist) - p) / v)
end

function M.timeTo(px, py, vx, vy, cx, cy, dist)
  return min(axisTime(px, vx, cx, dist), axisTime(py, vy, cy, dist))
end

-- Block SW corner for a centre (the TerrainBlock position).
function M.blockOrigin(cx, cy) return lattice.blockOrigin(cx, cy) end

-- True when the target for this pose is not the pending block (a re-request would ask for another one).
local function retargets(px, py, vx, vy, pending)
  local tx, ty = M.target(px, py, vx, vy)
  return tx ~= pending.cx or ty ~= pending.cy
end

-- The prepare/commit rule.
--   px, py, vx, vy: player position / velocity in the current anchor frame
--   cx, cy: centre of the committed block (nil before the first terrain)
--   pending: nil or { cx, cy, ready } (the terrain request in flight)
--   gate: optional function(ncx, ncy) -> true when every far tile that the move uncovers
--     (old block - new block) is live and the new block's area is split down to L0 (design 2.3
--     "commit gating", farfield.uncoverReady); nil = always true
--   latency: the last measured heightmap latency, s (the 512² re-check; nil = LATENCY_S)
-- Returns 'prepare' (+ new cx, cy), 'commit', 'discard' or nil. 'commit' is a request: the caller
-- runs it in a governor heavy slot (governor.lua), so it may be returned on several frames.
-- Pure: is the block's centre still ahead of a player moving at town speed, the target set by MIN_LEAD (decide)?
function M.centreAhead(px, py, vx, vy, cx, cy)
  vx, vy = vx or 0, vy or 0
  local v2 = vx * vx + vy * vy
  if M.MIN_LEAD <= 0 or v2 <= M.MIN_LEAD_V * M.MIN_LEAD_V or v2 * M.LOOKAHEAD_S * M.LOOKAHEAD_S >= M.MIN_LEAD * M.MIN_LEAD then
    return false
  end
  return (cx - px) * vx + (cy - py) * vy > 0 and M.cheb(px, py, cx, cy) <= M.MIN_LEAD + M.GRID
end

function M.decide(px, py, vx, vy, cx, cy, pending, now, retryAt, gate, latency)
  if not cx then return nil end
  local _, reach, ahead = mode(vx, vy)
  local d = M.cheb(px, py, cx, cy)
  if pending then
    local pd = M.cheb(px, py, pending.cx, pending.cy)
    -- past the commit distance a ready block is taken whenever it is better than the current one,
    -- even if it no longer keeps the full 600 m margin (late answer): the next request follows at once
    if pending.ready and pd < d and d > M.COMMIT_DIST then
      -- 512² commit-time re-check (see the top): a block the player would soon need to replace is
      -- re-prepared instead, while there is time for that
      -- (the latency is at least LATENCY_S: the last measured one says little about the next request)
      -- (not where MIN_LEAD sets the target: the block is then that far ahead by design, and the box test, symmetric,
      -- took it for one the player would soon leave: ready blocks thrown away and asked again at town speeds)
      local r = M.RECHECK_S
      local lt = M.LOOKAHEAD_S
      local leadSet = M.MIN_LEAD > 0 and ((vx or 0) ^ 2 + (vy or 0) ^ 2) * lt * lt < M.MIN_LEAD * M.MIN_LEAD
      if r > 0 and ahead == 0 and not leadSet and d <= M.HARD_COMMIT_DIST
        and M.cheb(px + (vx or 0) * r, py + (vy or 0) * r, pending.cx, pending.cy) > M.PREPARE_DIST
        and M.timeTo(px, py, vx or 0, vy or 0, cx, cy, M.RECHECK_SAFE)
          > max(latency or M.LATENCY_S, M.LATENCY_S) + M.RECHECK_SLACK_S
        and retargets(px, py, vx, vy, pending) then
        return 'discard'
      end
      if d > M.HARD_COMMIT_DIST or not gate or gate(pending.cx, pending.cy) then return 'commit' end
      return nil -- better than the current block: wait for the uncovered strip's tiles, keep it
    end
    -- 512²: a ready block that is still no better than the current one where the
    -- player reaches the hard commit distance (straight on) is never committed, and it blocks every
    -- new request until it leaves the reach, which is only 250 m (fast mode 90 m) from the edge: drop
    -- it, and the next frame asks for the current target. (A block prepared while the player was
    -- behind the centre, one grid step to the side of the way it goes, took the player off the block
    -- at 200 m/s in the offline model.)
    if pending.ready and M.RECHECK_S > 0 and pd >= d then
      local t = M.timeTo(px, py, vx or 0, vy or 0, cx, cy, M.HARD_COMMIT_DIST)
      if t < math.huge then
        local qx, qy = px + (vx or 0) * t, py + (vy or 0) * t
        if M.cheb(qx, qy, pending.cx, pending.cy) >= M.cheb(qx, qy, cx, cy) and retargets(px, py, vx, vy, pending) then
          return 'discard'
        end
      end
    end
    -- a prepared centre that would no longer keep the player 600 m inside its edges is useless
    if pd > reach then return 'discard' end
    return nil
  end
  -- the prepare test point: the player (fast mode: PREP_AHEAD_FAST_S ahead)
  if M.cheb(px + (vx or 0) * ahead, py + (vy or 0) * ahead, cx, cy) <= M.PREPARE_DIST or now < (retryAt or 0) then
    return nil
  end
  -- (MIN_LEAD: the block just committed is centred ahead of the player; the box test, symmetric, asked for the next one
  -- at once, and a prediction made then was overtaken by the bend: the next one once the player is past this centre)
  if M.centreAhead(px, py, vx, vy, cx, cy) then return nil end
  local tx, ty = M.target(px, py, vx, vy)
  if tx == cx and ty == cy then return nil end -- heading back towards the centre: nothing to gain
  local r = M.preferReady(px, py, vx, vy, tx, ty, cx, cy, reach)
  if r then return 'prepare', r[1], r[2] end
  return 'prepare', tx, ty
end

-- Blocks the server has built already ('terrain_ready_set': its cache round the player, prefetched): { ["cx_cy"] = {cx,
-- cy} }. A request may take one of them instead of its own target (M.preferReady): at 500 mph the server's guess of the
-- next request missed by a grid step or two half the time, and the block asked for was built from scratch (the game
-- held the car for its ground, Indianapolis 2026-10-30).
M.ready = {}
M.READY_STEPS = 2       -- grid steps a ready block may be from the target (Chebyshev)
function M.setReady(list)
  local r = {}
  for _, c in ipairs(list or {}) do
    if type(c) == 'table' and tonumber(c[1]) and tonumber(c[2]) then r[c[1] .. '_' .. c[2]] = { tonumber(c[1]), tonumber(c[2]) } end
  end
  M.ready = r
end
-- Pure (but for M.ready): the ready centre to ask for instead of the target (tx, ty), or nil: within READY_STEPS grid
-- steps of it, keeping the player within reach, not the current centre; the nearest to the target, then the one
-- furthest along the way the player goes.
function M.preferReady(px, py, vx, vy, tx, ty, cx, cy, reach)
  local best, bd, ba = nil, math.huge, -math.huge
  for _, c in pairs(M.ready) do
    local d = M.cheb(c[1], c[2], tx, ty)
    if d <= M.READY_STEPS * M.GRID and M.cheb(px, py, c[1], c[2]) <= reach and not (c[1] == cx and c[2] == cy) then
      local along = (c[1] - px) * (vx or 0) + (c[2] - py) * (vy or 0)
      if d < bd or (d == bd and along > ba) then best, bd, ba = c, d, along end
    end
  end
  return best
end

-- Distances for the UI: to the prepare threshold and to the commit threshold (<= 0 = passed).
-- Altitude rule (the user, 2026-10-13: "at high flying altitude we can stop the block following but bring it
-- back at low altitude"). High above the ground nothing needs the physics block, and at 300 m/s its builds
-- cannot keep up (~1 s per answer). The ground is the server's ground_ahead (the highest ground near the
-- straight path for the next 10 s; the far tiles do not collide, so the mod cannot measure it). Holds above
-- HOLD_AGL; follows again once the height, less what HOLD_LEAD_S of the current descent takes, is below
-- RESUME_AGL, so the block is under the player before it gets low. groundZ nil (no answer, a stale one):
-- never holds.
M.HOLD_AGL = 300        -- m (~1,000 ft)
M.RESUME_AGL = 150      -- m
M.HOLD_LEAD_S = 8       -- s of descent looked ahead
M.GROUND_STALE_S = 3    -- s: an older ground_ahead does not count

function M.altitudeHold(holding, z, vz, groundZ)
  if not groundZ then return false end
  local agl = z - groundZ + min(0, vz or 0) * M.HOLD_LEAD_S
  if holding then return agl > M.RESUME_AGL end
  return agl > M.HOLD_AGL
end

function M.thresholds(px, py, cx, cy)
  if not cx then return nil end
  local d = M.cheb(px, py, cx, cy)
  return M.PREPARE_DIST - d, M.COMMIT_DIST - d, d
end

return M
