-- Deck guard (2026-11-15; the user, playing a BeamMP session in Baltimore: "I've fallen off the road many times because a
-- small bridge didn't generate in time"). A bridge deck is part of its road tile, and the terrain block, which comes
-- first, already has the gap under it (the river, the valley, the road below): when the tile is late (that session:
-- tiles waiting on road plans, median 6.8 s, p90 15.7 s, max 46 s, at 40-160 mph) the car drives into the gap. The edge
-- guard (edgeguard.lua) and the soft slow-down (softdrag.lua) only know the block's edge.
--
-- The server lists the bridges round the car from its map data (server deckspans.py, `deck_spans`): polylines in the
-- anchor's world, each chain of a bridge's mapped pieces joined, with the ends that stand on the ground (abutments).
-- Every LOOK_S (only then; nothing at all while no span comes within reach of the car, and nothing per frame while no
-- gap is ahead) the guard follows the car's path ahead (an arc: its heading and turn rate) for its braking distance and
-- LEAD_S more, and finds the first place on a span the car drives onto (along it, entered at an end at the deck's level,
-- or already on it) whose road tile has not arrived (roads.deckGrid: every tile its line crosses, walked along the span
-- as far as the look reaches) where the block's ground lies DROP_M or more under the deck's level (the abutments'
-- ground heights): the gap. Then, every frame, as softdrag.lua does (the whole vehicle's
-- velocity scaled: in BeamMP too, nothing paused, only our own car):
--   drag    within DRAG_S of the gap a little drag, smoothstep up to K_MAX at FULL_S (no jolt)
--   brake   the speed capped at sqrt(2 A (d - STOP_M)) (the car comes to rest STOP_M short of the gap), at most A_MAX
--           taken off a second; at the line no speed towards it at all
--   hold    a car that cannot stop before the gap even so (the spans came late, a teleport onto the approach): held
--           (simhold.lua: the game's pause in single player, only our own car in BeamMP) HOLD_LEAD_S before it, until the
--           tile is in; the player may resume by hand (no new hold for RESUME_GRACE_S)
--   rescue  it went over anyway and is RESCUE_DROP under the deck's level, in the span's corridor, within RESCUE_WINDOW_S:
--           set back where it last was on the approach (as edgeguard.lua: spawn.safeTeleport, no reset)
-- The tile comes (roads.deckAt 'ready': its content in; making its collider solid in time is roads.lua's own job, its
-- NEED / URGENT rebuilds): the guard lets go at once. A car stopped at the line GIVE_UP_S: let go for that span (the
-- player's choice). While it waits the pose names the tile (pose.deckWait): the server builds it first.
-- No collision object, no ray, no engine call but the velocity scale while braking. The pure parts (look, brake, fell)
-- are tested under luajit: mod/tests/test_deckguard.lua.
local M = {}

M.PROTO = 1                 -- hello.deckGuard (server deckspans.PROTO)
M.ON = true
M.LOOK_S = 0.25             -- s between two looks along the path
M.STEP_M = 8                -- m between the path's samples
M.LAT_M = 9                 -- m from a span's line the path counts as on it ...
M.COS_MIN = 0.75            -- ... going along it (cos of the angle: ~41 deg)
M.END_M = 40                -- m from a span's end the path may enter it (farther in: it crosses it or passes under)
M.LEVEL_M = 6               -- m: the car this near the deck's level is at it
M.LEVEL_UP_M = 4            -- m more over the abutments' line a car on it may be mid-span (a deck raised over a road)
M.LEVEL_NEAR_M = 80         -- ... tested only this near the end it enters (farther, the road there may climb or fall)
M.DROP_M = 2.0              -- m: the block's ground this far under the deck's level is a gap (less: driven on as ground)
M.A = 4.0                   -- m/s^2: the braking profile (at 4, a tile 600 m ahead, where they arrive on time at
                            -- ~180 mph, slows a car only from ~155 mph up)
M.A_MAX = 7.0               -- m/s^2 at most taken off
M.STOP_M = 12               -- m short of the gap the car comes to rest
M.DRAG_S = 8                -- s to the gap where the drag starts ...
M.FULL_S = 2.5              -- ... and where it is full
M.K_MAX = 0.08              -- of the speed lost per second at full drag
M.LEAD_S = 2                -- s of driving looked at past the braking distance
M.LOOK_MIN_M = 60
M.LOOK_MAX_M = 900          -- (the server lists the spans within 1200 m: deckspans.RADIUS_M)
M.MOVE_MIN = 0.5            -- m/s: slower, the path keeps the last heading (a car at rest at the line keeps its gap)
M.YAW_MAX = 0.35            -- rad/s at most of the path's turn rate
M.HOLD_LEAD_S = 0.4         -- s: held when the gap is this near and the car cannot stop before it
M.HOLD_PAD_M = 2
M.HOLD_MIN_MS = 1.0
M.RESUME_GRACE_S = 5
M.RESCUE_DROP = 3           -- m under the deck's level
M.RESCUE_WINDOW_S = 10
M.RESCUE_GAP_S = 1
M.GIVE_UP_S = 45            -- s at rest at the line (or held): let go
M.WAIT_MS = 2.0             -- m/s: slower near the line is waiting (pose.deckWait, guardHold, the give-up clock)
M.MAX_DT = 0.1

local sqrt, min, max, abs, floor = math.sqrt, math.min, math.max, math.abs, math.floor
local cos, sin, atan2, pi = math.cos, math.sin, math.atan2 or math.atan, math.pi

-- The settings page's "Slow down before bridges still loading" (settings.lua deckGuard, on by default).
function M.setEnabled(on) M.ON = on ~= false end

function M.new()
  return { spans = {}, aid = nil, riding = {}, released = {}, nextLook = -math.huge, danger = nil, ep = nil,
    px = {}, py = {}, pdx = {}, pdy = {}, mode = 'clear',
    stats = { looks = 0, dangers = 0, frames = 0, lost = 0, holds = 0, rescues = 0, giveUps = 0, spans = 0 } }
end

-- A span from the message: pts { x1, y1, x2, y2, ... } (>= 2 points), abut bits (1 first end, 2 last end on the ground).
local function makeSpan(flat, bits)
  if type(flat) ~= 'table' then return nil end
  local n = floor(#flat / 2)
  if n < 2 then return nil end
  local pts, cum = {}, { 0 }
  local x0, y0, x1, y1 = math.huge, math.huge, -math.huge, -math.huge
  for i = 1, 2 * n do
    local v = tonumber(flat[i])
    if not v or v ~= v or abs(v) > 1e7 then return nil end
    pts[i] = v
  end
  for k = 1, n do
    local x, y = pts[2 * k - 1], pts[2 * k]
    x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    if k > 1 then
      local dx, dy = x - pts[2 * k - 3], y - pts[2 * k - 2]
      cum[k] = cum[k - 1] + sqrt(dx * dx + dy * dy)
    end
  end
  bits = tonumber(bits) or 0
  return { pts = pts, n = n, cum = cum, len = cum[n], box = { x0, y0, x1, y1 },
    abut0 = bits % 2 == 1, abut1 = floor(bits / 2) % 2 == 1,
    id = string.format('%.0f_%.0f_%.0f_%.0f', pts[1], pts[2], pts[2 * n - 1], pts[2 * n]) }
end
M._makeSpan = makeSpan

-- Server message `deck_spans` {anchorId, cx, cy, r, spans, abut}: the bridges round the car (deckspans.py).
function M.onSpans(g, msg, currentAnchor)
  if type(msg) ~= 'table' or msg.anchorId ~= currentAnchor or type(msg.spans) ~= 'table' then return false end
  local out, abut = {}, type(msg.abut) == 'table' and msg.abut or {}
  for i, flat in ipairs(msg.spans) do
    local sp = makeSpan(flat, abut[i])
    if sp then out[#out + 1] = sp end
  end
  g.spans, g.aid = out, msg.anchorId
  g.stats.spans = #out
  return true
end

-- A new frame (a rebase, a teleport): the spans are the old frame's, the car's history too.
function M.newFrame(g)
  g.spans, g.aid, g.riding, g.released, g.danger, g.ep, g.theta, g.thetaT = {}, nil, {}, {}, nil, nil, nil, nil
end

-- (span, x, y) -> distance to its line, the along-span m of the nearest point, the segment's unit direction, the point.
local function nearest(sp, x, y)
  local p, best, bu, bsx, bsy, bqx, bqy = sp.pts, math.huge, 0, 1, 0, x, y
  for k = 1, sp.n - 1 do
    local ax, ay, bx, by = p[2 * k - 1], p[2 * k], p[2 * k + 1], p[2 * k + 2]
    local dx, dy = bx - ax, by - ay
    local l2 = dx * dx + dy * dy
    local t = l2 > 1e-9 and ((x - ax) * dx + (y - ay) * dy) / l2 or 0
    if t < 0 then t = 0 elseif t > 1 then t = 1 end
    local qx, qy = ax + t * dx, ay + t * dy
    local ex, ey = x - qx, y - qy
    local d2 = ex * ex + ey * ey
    if d2 < best then
      local l = sqrt(l2)
      best, bu, bqx, bqy = d2, sp.cum[k] + t * l, qx, qy
      if l > 1e-6 then bsx, bsy = dx / l, dy / l end
    end
  end
  return sqrt(best), bu, bsx, bsy, bqx, bqy
end
M._nearest = nearest

-- The deck's level (m) at u along the span: its abutments' ground (groundAt, once known), between two of them a straight
-- line; nil while none is known.
local function levelAt(sp, u, groundAt)
  if sp.abut0 and sp.z0 == nil then sp.z0 = groundAt(sp.pts[1], sp.pts[2]) end
  if sp.abut1 and sp.z1 == nil then sp.z1 = groundAt(sp.pts[2 * sp.n - 1], sp.pts[2 * sp.n]) end
  local a, b = sp.z0, sp.z1
  if a and b then
    local f = sp.len > 1e-6 and min(1, max(0, u / sp.len)) or 0
    return a + (b - a) * f
  end
  return a or b
end

local function inBox(b, x, y, m)
  return x >= b[1] - m and x <= b[3] + m and y >= b[2] - m and y <= b[4] + m
end

local entersAt, scanSpan

-- The point at u m along the span.
local function pointAt(sp, u)
  local p, cum = sp.pts, sp.cum
  if u <= 0 then return p[1], p[2] end
  for k = 2, sp.n do
    if cum[k] >= u then
      local l = cum[k] - cum[k - 1]
      local f = l > 1e-9 and (u - cum[k - 1]) / l or 0
      return p[2 * k - 3] + (p[2 * k - 1] - p[2 * k - 3]) * f, p[2 * k - 2] + (p[2 * k] - p[2 * k - 2]) * f
    end
  end
  return p[2 * sp.n - 1], p[2 * sp.n]
end

-- Does the path drive onto span sp at its sample k (going d along it, u m along it there)? On it now (k == 1): entered
-- at its level before (riding), or at its known level (LEVEL_M under to LEVEL_M + LEVEL_UP_M over the abutments' line;
-- Codex review round 3: a higher road in the corridor), or, with no abutment height known, off a deck it was riding.
-- Ahead (k > 1): only at an end (END_M), only with that end's abutment height known, the path's own ground just short
-- of it at that height (a road under the bridge stays low there: Codex review round 4, a road below braked from far
-- off) and, near it, the car too. An end on another deck (a fork's branch) never ahead; on it now only coming off a deck
-- the car rides (and look scans it only when it is the one branch round the car there).
entersAt = function(g, sp, k, d, u, x, y, z, groundAt, px, py)
  local fromEnd = d > 0 and u or (sp.len - u)
  if k == 1 and g.riding[sp.id] then return true end
  local abutted = (d > 0 and sp.abut0) or (d < 0 and sp.abut1)
  local ez = abutted and levelAt(sp, d > 0 and 0 or sp.len, groundAt) or nil
  if fromEnd <= M.END_M then
    local ex, ey = (d > 0) and sp.pts[1] or sp.pts[2 * sp.n - 1], (d > 0) and sp.pts[2] or sp.pts[2 * sp.n]
    local near = (ex - x) ^ 2 + (ey - y) ^ 2 <= M.LEVEL_NEAR_M * M.LEVEL_NEAR_M
    if ez then
      if near and abs(z - ez) > M.LEVEL_M then return false end     -- (rejected: nothing overrides it)
      if k > 1 then
        local gb = groundAt(px[k - 1], py[k - 1])
        return gb ~= nil and abs(gb - ez) <= M.LEVEL_M
      end
      return true
    end
    if not abutted then return k == 1 and next(g.riding) ~= nil end   -- (off a deck it rides, at a fork)
    if k > 1 then return false end                            -- (its height not known yet: no braking for it yet)
  elseif k > 1 then
    return false                                              -- (into its middle: it is crossed or passed under)
  end
  local lz = levelAt(sp, u, groundAt)
  return lz ~= nil and z >= lz - M.LEVEL_M and z <= lz + M.LEVEL_M + M.LEVEL_UP_M
end

M.SCAN_M = 2                -- m between the span's points looked at for a drop
M.PIECE_M = 64              -- m: a span's segments cut into pieces at most this long for the tile grid's walk

-- The cells of the tile grid a straight piece crosses, in order, from fractional grid point (f0x, f0y) to (f1x, f1y)
-- (Amanatides-Woo): visit(ix, iy, t0, t1) for each, t the piece's share; stops when visit returns true. Every cell the
-- piece passes through is visited (one passed at a corner exactly too, with no length).
local function cellsAlong(f0x, f0y, f1x, f1y, visit)
  local ix, iy = floor(f0x), floor(f0y)
  local dx, dy = f1x - f0x, f1y - f0y
  local sx = dx > 0 and 1 or (dx < 0 and -1 or 0)
  local sy = dy > 0 and 1 or (dy < 0 and -1 or 0)
  local inf = math.huge
  local tmx = sx > 0 and (ix + 1 - f0x) / dx or (sx < 0 and (f0x - ix) / -dx or inf)
  local tmy = sy > 0 and (iy + 1 - f0y) / dy or (sy < 0 and (f0y - iy) / -dy or inf)
  local tdx = sx ~= 0 and 1 / abs(dx) or inf
  local tdy = sy ~= 0 and 1 / abs(dy) or inf
  local t = 0
  for _ = 1, 100000 do
    local tn = min(tmx, tmy, 1)
    if visit(ix, iy, t, tn) then return true end
    if tn >= 1 then return false end
    if tmx < tmy then ix, t, tmx = ix + sx, tmx, tmx + tdx else iy, t, tmy = iy + sy, tmy, tmy + tdy end
  end
  return false
end
M._cellsAlong = cellsAlong

-- The first gap on span sp from a to b m along it (going dir): every road tile its line crosses on the way (walked over
-- the tile grid: grid.xy(x, y) -> fractional cell, grid.at(ix, iy) -> 'missing' | 'ready', key), and in each one whose
-- tile is missing, the block's ground at its entry, every SCAN_M and at its exit, DROP_M or more under the deck's level
-- there (or either unknown). Codex review rounds 4 / 5: the path's 8 m samples skipped an 8 m bridge, and 32 m probes a
-- missing tile's corner clipped between two ready ones. { u, x, y, key, level } or nil.
scanSpan = function(sp, dir, a, b, grid, groundAt)
  if (b - a) * dir < 0 then return nil end
  -- the stretch's points: its ends and the span's points between them, pieces at most PIECE_M long
  local us = { a }
  local cum = sp.cum
  if dir > 0 then
    for k = 1, sp.n do if cum[k] > a and cum[k] < b then us[#us + 1] = cum[k] end end
  else
    for k = sp.n, 1, -1 do if cum[k] < a and cum[k] > b then us[#us + 1] = cum[k] end end
  end
  us[#us + 1] = b
  local found = nil
  local function cell(u0, u1, ix, iy)
    local st, key = grid.at(ix, iy)
    if st ~= 'missing' then return false end
    local len = abs(u1 - u0)
    local m = max(1, math.ceil(len / M.SCAN_M))
    for j = 0, m do
      local u = u0 + (u1 - u0) * j / m
      local qx, qy = pointAt(sp, u)
      local lz = levelAt(sp, u, groundAt)
      local gz = lz and groundAt(qx, qy)
      if not (lz and gz) or gz < lz - M.DROP_M then
        found = { u = u, x = qx, y = qy, key = key, level = lz }
        return true
      end
    end
    return false
  end
  local lastU, lfx, lfy = nil, nil, nil
  for i = 1, #us - 1 do
    local u0, u1 = us[i], us[i + 1]
    local pieces = max(1, math.ceil(abs(u1 - u0) / M.PIECE_M))
    for j = 1, pieces do
      local pa = u0 + (u1 - u0) * (j - 1) / pieces
      local pb = u0 + (u1 - u0) * j / pieces
      local f0x, f0y
      if lastU == pa then f0x, f0y = lfx, lfy else f0x, f0y = grid.xy(pointAt(sp, pa)) end
      local f1x, f1y = grid.xy(pointAt(sp, pb))
      lastU, lfx, lfy = pb, f1x, f1y
      if cellsAlong(f0x, f0y, f1x, f1y, function(ix, iy, t0, t1)
        return cell(pa + (pb - pa) * t0, pa + (pb - pa) * t1, ix, iy)
      end) then return found end
    end
  end
  return nil
end

-- A tile grid from a point question deckAt(x, y) -> state, key (tests, tools): cells POINT_CELL_M square, each asked at
-- its centre.
M.POINT_CELL_M = 2
function M.pointGrid(deckAt)
  local c = M.POINT_CELL_M
  return {
    xy = function(x, y) return x / c, y / c end,
    at = function(ix, iy) return deckAt((ix + 0.5) * c, (iy + 0.5) * c) end,
  }
end

-- Pure: one look along the path. inp = { clock, x, y, z, vx, vy, vehId, grid (the road tiles: xy(x, y) -> fractional
-- cell, at(ix, iy) -> 'missing' | 'ready', key; roads.deckGrid) or deckAt(x, y) -> 'missing' | 'ready', key (pointGrid);
-- groundAt(x, y) -> the block's ground height there or nil }. Sets g.danger = { d (m along the path to the gap), x, y
-- (its place on the span), id (the span's), span, key (the tile), level (the deck's level there), at } or nil; returns it.
function M.look(g, inp)
  g.stats.looks = g.stats.looks + 1
  local spans = g.spans
  local x, y, z, vx, vy = inp.x, inp.y, inp.z, inp.vx, inp.vy
  local v = sqrt(vx * vx + vy * vy)
  local th = nil
  if v >= M.MOVE_MIN then th = atan2(vy, vx) else th = g.theta end
  local w = 0
  if v >= M.MOVE_MIN and g.theta and g.thetaT and inp.clock > g.thetaT and inp.clock - g.thetaT <= 1 then
    local d = th - g.theta
    if d > pi then d = d - 2 * pi elseif d < -pi then d = d + 2 * pi end
    w = min(M.YAW_MAX, max(-M.YAW_MAX, d / (inp.clock - g.thetaT)))
  end
  if v >= M.MOVE_MIN then g.theta, g.thetaT = th, inp.clock end
  if not th or #spans == 0 then g.danger, g.dNow, g.riding = nil, nil, {}; return nil end
  local L = min(M.LOOK_MAX_M, max(M.LOOK_MIN_M, v * v / (2 * M.A) + M.STOP_M + v * M.LEAD_S))
  -- the spans that come within reach (none: nothing else done)
  local cands = nil
  local r = L + M.LAT_M
  for i = 1, #spans do
    local b = spans[i].box
    local dx, dy = max(b[1] - x, x - b[3], 0), max(b[2] - y, y - b[4], 0)
    if dx * dx + dy * dy <= r * r then cands = cands or {}; cands[#cands + 1] = spans[i] end
  end
  if not cands then g.danger, g.dNow, g.riding, g.released = nil, nil, {}, {}; return nil end    -- (off them all)
  -- the path's samples: an arc at the car's turn rate
  local px, py, pdx, pdy = g.px, g.py, g.pdx, g.pdy
  local kappa = w / max(v, M.MOVE_MIN)
  local n = floor(L / M.STEP_M) + 1
  local cx, cy, a, step = x, y, th, M.STEP_M
  for k = 1, n do
    px[k], py[k], pdx[k], pdy[k] = cx, cy, cos(a), sin(a)
    local am = a + kappa * step * 0.5
    cx, cy = cx + cos(am) * step, cy + sin(am) * step
    a = a + kappa * step
  end
  -- riding: the spans the car is on now, entered at their level (kept to the next look: no level asked again on them);
  -- onPath: every span the path drives onto, a predicted entry too (its level asked again at every look, near it the
  -- abutment's height decides: Codex review 2026-11-15, a road under a bridge once taken far off kept for good)
  local riding, onPath, best = {}, {}, nil
  local groundAt, grid = inp.groundAt, inp.grid or M.pointGrid(inp.deckAt)
  -- where the path drives onto span sp: its sample kin, direction, along-span m there; nil if it does not
  local function enter(sp)
    for k = 1, n do
      local hx, hy = px[k], py[k]
      if inBox(sp.box, hx, hy, M.LAT_M) then
        local dist, u, sx, sy = nearest(sp, hx, hy)
        local c = pdx[k] * sx + pdy[k] * sy
        if dist <= M.LAT_M and abs(c) >= M.COS_MIN then
          local d = c > 0 and 1 or -1
          if not entersAt(g, sp, k, d, u, x, y, z, groundAt, px, py) then return nil end
          return { sp = sp, kin = k, dir = d, uin = u }
        end
      end
    end
    return nil
  end
  local ins = {}
  for _, sp in ipairs(cands) do
    local e = enter(sp)
    if e then
      ins[#ins + 1] = e
      onPath[sp.id] = true
      if e.kin == 1 then riding[sp.id] = true end
    end
  end
  -- a fork's branches (spans entered from an end on another deck): round the fork the car is in the corridors of all of
  -- them (a shallow one for a long way), and which one it takes cannot be told from its path; such a branch is scanned
  -- only when it is the one round the car from that end, else none of them (Codex review rounds 6 / 7: the branch not
  -- taken, its tile missing, braked and held the car on the one taken; a gap just past a fork is not guarded)
  local atFork = {}
  for _, e in ipairs(ins) do
    local sp = e.sp
    e.forkEnd = not ((e.dir > 0 and sp.abut0) or (e.dir < 0 and sp.abut1))
    if e.forkEnd then
      local ex, ey = (e.dir > 0) and sp.pts[1] or sp.pts[2 * sp.n - 1], (e.dir > 0) and sp.pts[2] or sp.pts[2 * sp.n]
      e.node = string.format('%.0f_%.0f', ex, ey)
      atFork[e.node] = (atFork[e.node] or 0) + 1
    end
  end
  for _, e in ipairs(ins) do
    local sp, kin, dir, uin = e.sp, e.kin, e.dir, e.uin
    if g.released[sp.id] == nil and not (e.forkEnd and atFork[e.node] > 1) then
      -- from where the path drives onto it (its end, when that is ahead) along it as far as the look reaches: a car on
      -- a bridge follows the bridge (the path's arc may leave its corridor on a curved one)
      local fromEnd = dir > 0 and uin or (sp.len - uin)
      local a = (kin > 1 and fromEnd <= M.END_M) and (dir > 0 and 0 or sp.len) or uin
      local rem = max(0, L - (kin - 1) * step)
      local b = dir > 0 and min(sp.len, uin + rem) or max(0, uin - rem)
      local gap = scanSpan(sp, dir, a, b, grid, groundAt)
      if gap then
        local sd = max(0, (kin - 1) * step + (gap.u - uin) * dir)
        if not best or sd < best.d then
          best = { d = sd, x = gap.x, y = gap.y, id = sp.id, span = sp, key = gap.key,
            level = gap.level or (z - 0.5), at = inp.clock }
        end
      end
    end
  end
  -- a span let go (GIVE_UP_S) is the guard's again once the car is off it
  for id in pairs(g.released) do
    if not onPath[id] then g.released[id] = nil end
  end
  g.riding = riding
  if best then
    local old = g.danger
    if old and old.id == best.id then best.since = old.since or old.at else best.since = inp.clock end
    if not old or old.id ~= best.id then g.stats.dangers = g.stats.dangers + 1 end
    -- the episode the rescue looks back on: the gap's span, its level, where the car last was on the approach
    local ep = g.ep
    if not ep or ep.id ~= best.id or ep.vehId ~= inp.vehId then
      ep = { id = best.id, span = best.span, vehId = inp.vehId }
      g.ep = ep
    end
    ep.level, ep.untilT = best.level, inp.clock + M.RESCUE_WINDOW_S
    if riding[best.id] then ep.confirmed = true end
    -- (on the approach at the deck's level: well short of the gap, or, found late, the nearest place short of it)
    if best.d >= 3 and z >= best.level - M.LEVEL_M and (not ep.safe or best.d >= M.STOP_M + 3) then
      ep.safe = { x, y, z, th }
    end
  end
  -- the rescue's episode: confirmed once the car is on its span, entered at the deck's level; one only predicted from
  -- the path is dropped as soon as a look no longer drives onto that span (its level check rejected it: a road falling
  -- away under the bridge, Codex review 2026-11-15 round 2)
  local ep = g.ep
  if ep and riding[ep.id] then ep.confirmed = true end
  if ep and not ep.confirmed and not onPath[ep.id] then g.ep = nil end
  g.danger = best
  g.dNow = best and best.d or nil
  return best
end

-- Pure: this frame's velocity scale (1: none) and whether to hold. inp = { v (m/s towards the gap: the velocity along
-- the heading the gap was found on; negative backing away, never braked), dt (s of simulation) }. g.dNow: m to the gap,
-- less what the car drove towards it since the look.
function M.brake(g, inp)
  local dg = g.danger
  if not dg or not g.dNow then return 1, false end
  local v, dt = inp.v, min(max(inp.dt or 0, 0), M.MAX_DT)
  if v <= 0 then return 1, false end
  g.dNow = g.dNow - v * dt
  local d = g.dNow
  local scale = 1
  if v > 1e-3 then
    local t = max(0, d) / v
    if t < M.DRAG_S then
      local u = min(1, max(0, (M.DRAG_S - t) / (M.DRAG_S - M.FULL_S)))
      scale = 1 - M.K_MAX * u * u * (3 - 2 * u) * dt
    end
    local cap = sqrt(2 * M.A * max(0, d - M.STOP_M))
    if v > cap then
      local target = max(cap, v - M.A_MAX * dt)
      scale = min(scale, target / v)
    end
  end
  local hold = v > M.HOLD_MIN_MS and d - M.HOLD_PAD_M <= v * M.HOLD_LEAD_S
    and v * v / (2 * M.A_MAX) > d - M.HOLD_PAD_M
  return scale, hold
end

-- Pure: the place to set the car back to ({ x, y, z, heading }) when it went into the gap anyway, or nil.
-- inp = { clock, x, y, z, vx, vy, groundAt, vehId (the car's: only the one that came up to the gap is set back) }: RESCUE_DROP under the deck's level where it is (its abutments' line), in
-- the span's corridor going along it, within RESCUE_WINDOW_S of the last gap seen on that span, which the car entered
-- at the deck's level (ep.confirmed: a look had it on the span, riding).
function M.fell(g, inp)
  local ep = g.ep
  if not ep or not ep.safe or inp.clock > (ep.untilT or -math.huge) or g.released[ep.id] ~= nil then return nil end
  if ep.vehId ~= inp.vehId then return nil end      -- (another car than the one that drove up to it)
  if not ep.confirmed then return nil end           -- (never on the span at its level: only predicted)
  if inp.clock - (g.lastRescue or -math.huge) < M.RESCUE_GAP_S then return nil end
  local sp = ep.span
  if not inBox(sp.box, inp.x, inp.y, M.LAT_M + 4) then return nil end
  local dist, u, sx, sy = nearest(sp, inp.x, inp.y)
  if dist > M.LAT_M + 4 then return nil end
  local lz = inp.groundAt and levelAt(sp, u, inp.groundAt) or ep.level
  if inp.z >= (lz or ep.level) - M.RESCUE_DROP then return nil end
  local v = sqrt(inp.vx * inp.vx + inp.vy * inp.vy)
  if v >= M.MOVE_MIN and abs((inp.vx * sx + inp.vy * sy) / v) < M.COS_MIN then return nil end   -- (a road under it)
  g.lastRescue = inp.clock
  g.stats.rescues = g.stats.rescues + 1
  return ep.safe
end

-- The road geotile keys the guard waits on now (pose.deckWait), or nil.
function M.waitKeys(g)
  local dg = g.danger
  if not dg or type(dg.key) ~= 'string' or not dg.key:match('^g%d+_') then return nil end
  return { dg.key }
end

-- Is the guard holding the car or has it at rest at a gap (pose.guardHold: the server on every core meanwhile)?
function M.waiting(g) return g.held == true or g.mode == 'wait' end

-- With the engine. ctx = { clock, dtSim, dtReal, pl (core st.player: x y z vx vy), anchorId, getVeh() (the player's
-- vehicle or nil: called only with a gap ahead), grid (roads.deckGrid) or deckAt, groundAt, hold (simhold) }. Returns the mode: 'off' | 'clear' | 'slow' | 'wait' | 'hold'.
function M.update(g, ctx)
  local pl = ctx.pl
  if not M.ON or not pl or pl.camera or g.aid == nil or g.aid ~= ctx.anchorId then
    M.release(g, ctx.hold)
    g.danger, g.mode = nil, 'off'
    return g.mode
  end
  local clock = ctx.clock
  if pl.vehicleId ~= g.vehId then M.switched(g, ctx.hold, pl.vehicleId) end    -- (another car: nothing of the old one's)
  if clock >= g.nextLook then
    g.nextLook = clock + M.LOOK_S
    M.look(g, { clock = clock, x = pl.x, y = pl.y, z = pl.z, vx = pl.vx or 0, vy = pl.vy or 0, vehId = pl.vehicleId,
      grid = ctx.grid, deckAt = ctx.deckAt, groundAt = ctx.groundAt })
  end
  -- (nothing ahead and no gap just passed: no engine call at all)
  if not g.danger and not (g.ep and g.ep.confirmed and clock <= (g.ep.untilT or -math.huge)) then
    M.release(g, ctx.hold)
    g.mode = 'clear'
    return g.mode
  end
  local veh = ctx.getVeh and ctx.getVeh()
  if not veh then g.mode = 'clear'; return g.mode end
  local vx, vy = pl.vx or 0, pl.vy or 0
  -- (the speed towards the gap: along the heading it was found on; backing away from it is never braked)
  local th = g.theta or 0
  local v = vx * cos(th) + vy * sin(th)
  local rescue = M.fell(g, { clock = clock, x = pl.x, y = pl.y, z = pl.z, vx = vx, vy = vy, groundAt = ctx.groundAt,
    vehId = pl.vehicleId })
  if rescue then
    M.release(g, ctx.hold)
    log('W', 'beamngEarth', string.format('deck guard: the car went into the gap of a bridge not loaded yet: set back '
      .. 'to (%.1f, %.1f)', rescue[1], rescue[2]))
    local th = rescue[4] or 0
    spawn.safeTeleport(veh, vec3(rescue[1], rescue[2], rescue[3] + 1), quatFromDir(vec3(cos(th), sin(th), 0)), nil, nil,
      nil, nil, false)
    veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
    g.danger, g.dNow, g.nextLook, g.mode = nil, nil, clock + 0.5, 'rescue'
    return g.mode
  end
  if not g.danger then M.release(g, ctx.hold); g.mode = 'clear'; return g.mode end
  local h = ctx.hold
  if g.held then
    if h and not h.paused() then              -- (the player resumed by hand: no new hold for a while)
      g.held, g.resumedAt = nil, clock
    end
  end
  local scale, hold = M.brake(g, { v = v, dt = ctx.dtSim })
  -- waiting at the line (or held): the give-up clock runs
  local atLine = (g.dNow or math.huge) <= M.STOP_M + 10 and abs(v) < M.WAIT_MS
  if atLine or g.held then
    g.waitS = (g.waitS or 0) + (ctx.dtReal or ctx.dtSim or 0)
    if g.waitS >= M.GIVE_UP_S then
      g.released[g.danger.id] = clock
      g.stats.giveUps = g.stats.giveUps + 1
      log('W', 'beamngEarth', string.format('deck guard: the road tile %s of a bridge ahead not loaded after %.0f s: '
        .. 'let go', tostring(g.danger.key), g.waitS))
      M.release(g, h)
      g.danger, g.dNow, g.waitS, g.mode = nil, nil, 0, 'clear'
      return g.mode
    end
  else
    g.waitS = 0
  end
  if hold and not g.held and h and not h.paused() and clock - (g.resumedAt or -math.huge) >= M.RESUME_GRACE_S then
    h.pause(true)
    g.held = true
    g.stats.holds = g.stats.holds + 1
    log('I', 'beamngEarth', string.format('deck guard: held %.0f m before a bridge whose road tile %s is not loaded',
      g.dNow or -1, tostring(g.danger.key)))
  end
  if g.held then g.mode = 'hold'; return g.mode end
  if scale < 1 then
    veh:applyClusterVelocityScaleAdd(-1, scale, 0, 0, 0)
    g.stats.frames = g.stats.frames + 1
    g.stats.lost = g.stats.lost + v * (1 - scale)
  end
  g.mode = atLine and 'wait' or (scale < 1 and 'slow' or 'clear')
  return g.mode
end

-- The player's car is another one (a vehicle switch, a new car; Codex review 2026-11-15): the old one's hold let go, its
-- gap, approach (the rescue's), riding and heading forgotten; the next look is the new car's.
function M.switched(g, h, vehId)
  M.release(g, h)
  g.vehId, g.danger, g.dNow, g.ep, g.riding, g.released, g.theta, g.thetaT = vehId, nil, nil, nil, {}, {}, nil, nil
  g.waitS, g.resumedAt, g.nextLook = 0, nil, -math.huge
end

-- Let go of a hold the guard made (the tile came, the guard off, a new frame).
function M.release(g, h)
  if g.held and h and h.paused() then h.pause(false) end
  g.held = nil
end

return M
