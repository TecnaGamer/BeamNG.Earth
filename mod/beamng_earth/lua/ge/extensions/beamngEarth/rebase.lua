-- Floating origin, rare (docs/V1_1_SPEC.md 1b): when the player is more than REBASE_DISTANCE
-- (default 10 km) from the anchor origin, the world moves into a new anchor's ENU frame with one
-- exact rigid transform p_new = R*p_old + t (geodesy Anchor:transformTo).
-- decide() is the pure prefetch/commit rule (testable under plain luajit);
-- moveVehicles() is the engine side for vehicles.
--
-- Block size (docs/V1_4_SPEC.md 3.5, M4): configure(size) picks STALE_DIST per block size (450 m for
-- 1024², 192 m for 512²) and, for 512², the stale-anchor rule: a prepared anchor whose terrain centre
-- is farther than STALE_DIST from the player is still committed when the player is inside its valid
-- area with >= KEEP_EDGE to every edge (terrain-follow then prepares at once); otherwise it is
-- discarded and re-requested with its terrain where the player will be when the answer arrives
-- (core.lua rebaseHint: p + v * (measured anchor latency + LEAD_EXTRA_S)). 1024² keeps v1.3.1's
-- discard (KEEP_EDGE_1024 = 0: off).

local M = {}

-- 15 km: float32 positions have the same 0.98 mm step everywhere in [8192, 16384) m, so 15 km
-- costs no precision over 10 km and rebases a third less often; beyond 16384 m the step doubles
-- (docs/research/2026-10-10-origin-distance.md)
M.DEFAULT_DISTANCE = 15000
M.MIN_DISTANCE, M.MAX_DISTANCE = 2000, 16000
M.distance = M.DEFAULT_DISTANCE -- switch anchors at or beyond this horizontal distance
M.PREFETCH_MARGIN = 300  -- prepare_anchor this much earlier, so the anchor is ready by `distance`
M.STALE_DIST = 450       -- a prepared anchor whose terrain centre is this far (Chebyshev, new
                         -- frame) from the player would not help: re-request
M.STALE_DIST_1024 = 450  -- per block size (flat: modsim reads these); 512: a commit at 192 m leaves
M.STALE_DIST_512 = 192   -- 318 m to the collidable edge, as the follow table's commit distance does
M.KEEP_EDGE = 0          -- stale but >= this far inside every edge of its valid area: commit (0 = off)
M.KEEP_EDGE_1024 = 0
M.KEEP_EDGE_512 = 150
M.LATENCY_S = 1          -- anchor latency assumed until one is measured (s)
M.LEAD_EXTRA_S = 1       -- a re-requested terrain leads the player by the latency plus this
M.RETRY_S = 5            -- wait after an anchor_error before asking again

-- The session's block size (1024 or 512): the staleness distance and the stale-anchor rule.
function M.configure(size)
  local small = size == 512
  M.STALE_DIST = small and M.STALE_DIST_512 or M.STALE_DIST_1024
  M.KEEP_EDGE = small and M.KEEP_EDGE_512 or M.KEEP_EDGE_1024
  return small and 512 or 1024
end

function M.setDistance(m)
  m = tonumber(m)
  if not m then return M.distance end
  M.distance = math.max(M.MIN_DISTANCE, math.min(M.MAX_DISTANCE, m))
  return M.distance
end

-- dist: player's horizontal distance from the anchor origin.
-- pending: nil or { purpose = 'rebase'|'resync'|'teleport', ready = bool } (the request in flight).
-- staleDist: only when pending.ready: Chebyshev distance player -> the prepared terrain centre,
--   both in the new anchor's frame.
-- edgeDist: only when pending.ready: the player's distance to the nearest edge of that terrain's
--   valid (collidable) area, new frame (negative outside); used by the stale-anchor rule (KEEP_EDGE).
-- Returns 'prefetch', 'commit', 'discard' or nil. Teleport/resync requests commit as soon as they
-- are ready, which the caller handles; this rule only covers the distance-driven rebase.
-- Staleness is judged only at the commit: the terrain was centred for where the player will be
-- at the rebase distance (core.lua rebaseHint), up to PREFETCH_MARGIN + 424 m ahead of the player
-- at the prefetch, so judging it earlier discards a good answer and re-requests in a loop.
function M.decide(dist, pending, staleDist, now, retryAt, edgeDist)
  if not pending then
    if dist >= M.distance - M.PREFETCH_MARGIN and now >= (retryAt or 0) then return 'prefetch' end
    return nil
  end
  if pending.purpose ~= 'rebase' or not pending.ready then return nil end
  if dist < M.distance then return nil end
  if staleDist and staleDist > M.STALE_DIST then
    if M.KEEP_EDGE > 0 and edgeDist and edgeDist >= M.KEEP_EDGE then return 'commit' end
    return 'discard'
  end
  return 'commit'
end

-- Lead time (s) of a re-requested rebase terrain (the stale-anchor rule): the measured anchor
-- latency (nil: LATENCY_S) plus LEAD_EXTRA_S.
function M.aheadLead(latency)
  return (tonumber(latency) or M.LATENCY_S) + M.LEAD_EXTRA_S
end

-- Move every vehicle from the old anchor frame into the new one in place, keeping its motion
-- (phase 1 of a rebase; core.lua does the rest in the onUpdate of the frame whose physics step
-- applies this move, see commitRebase there).
--
-- veh:setClusterPosRelRot(node, x, y, z, qx, qy, qz, qw): puts the reference node at (x, y, z) and
-- turns the cluster about it by the world-frame rotation q (a BeamNG quaternion). Evidence:
--   tech/techCore.lua:909-912  diffRot = vehRot:inversed() * rot; setClusterPosRelRot(ref, pos, diffRot)
--                              (pos = the target position, used as given, so the turn is about it)
--   server/commands.lua:106-108  same, with pos = the camera position
--   spawn.lua:519-533  the caller rotates the ref-node offset itself (diffRot * offset) and passes the
--                      resulting position, plus the same diffRot; node -1 = "teleporting all clusters"
-- It does not zero velocity: every caller that wants that calls applyClusterVelocityScaleAdd(...,0,...)
-- afterwards (spawn.lua:534, commands.lua:109); probes/p6_rebase.lua measured speed kept 20.0->20.1.
-- Whether the engine also turns the velocity by q cannot be seen from Lua; if it does not, the
-- velocity direction is off by the rebase angle (0.09 deg at 10 km), which the spec accepts.
--
-- p_new = R*p + t is applied to veh:getPosition(), which is what spawn.lua:519 feeds in too.
-- Returns { [vehId] = { old = {x,y,z}, new = {x,y,z} } } and the player's id.
function M.moveVehicles(xf, geodesy)
  local qx, qy, qz, qw = xf:beamngQuat()
  local player = getPlayerVehicle(0)
  local playerId = player and player:getID()
  local moved = {}
  for _, veh in ipairs(getAllVehicles()) do
    local p = veh:getPosition()
    local x, y, z = xf:apply(p.x, p.y, p.z)
    veh:setClusterPosRelRot(-1, x, y, z, qx, qy, qz, qw)
    moved[veh:getID()] = { old = { p.x, p.y, p.z }, new = { x, y, z } }
    M.updateResetState(veh, xf, geodesy, x, y, z)
  end
  return moved, playerId
end

-- The vehicle's reset ("original") transform and its recovery history live in the old frame too.
-- - setOriginalTransform(pos, rot) as spawn.lua:535 (pos = the same point that went into
--   setClusterPosRelRot, rot = the vehicle OOBB rotation). The engine exposes no getter for the
--   original rotation, and the old original transform lies outside the streamed terrain anyway
--   (the block follows the player), so the reset transform becomes the vehicle's current pose in
--   the new frame: its current OOBB rotation (getSpawnWorldOOBB() is the live OOBB, map.lua:3902
--   treats its centre as the moving object's position) turned by R.
-- - Vehicle Lua recovery points (vehicle/recovery.lua: recoveryPoints dequeue + homePoint, each
--   { pos, dirFront, dirUp } in world coordinates) feed the Insert-key rewind and loadHome. They are
--   mapped in the vehicle VM. A point recorded after the move (within metres of the new position)
--   is left alone, so the command is correct whichever physics frame it runs in. Only points within
--   50 m of the new position are tested that way: the new position's coordinates are small (near the
--   new origin), so a plain "nearer old than new" test would skip old-frame points near the OLD
--   origin, e.g. the home point saved at the teleport target.
function M.updateResetState(veh, xf, geodesy, x, y, z)
  local ok, err = pcall(function()
    local m = quat(veh:getSpawnWorldOOBB():getMatrix():toQuatF()) -- as spawn.lua:514
    local bx, by, bz, bw = xf:beamngQuat()
    -- BeamNG order: apply the vehicle's rotation first, then R  ->  rot * Rq
    local rx, ry, rz, rw = geodesy.quatMul(m.x, m.y, m.z, m.w, bx, by, bz, bw)
    veh:setOriginalTransform(x, y, z, rx, ry, rz, rw)
  end)
  if not ok then log('W', 'beamngEarth', 'setOriginalTransform skipped: ' .. tostring(err)) end

  local p = veh:getPosition()
  local r, t = xf.r, xf.t
  local cmd = string.format([[
local R = {%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g}
local T = {%.17g,%.17g,%.17g}
local ox, oy, oz, nx, ny, nz = %.17g,%.17g,%.17g,%.17g,%.17g,%.17g
local function rot(v) return vec3(R[1]*v.x+R[2]*v.y+R[3]*v.z, R[4]*v.x+R[5]*v.y+R[6]*v.z, R[7]*v.x+R[8]*v.y+R[9]*v.z) end
local function map(pt)
  if type(pt) ~= 'table' or not pt.pos then return end
  local q = pt.pos
  local dn = (q.x-nx)^2+(q.y-ny)^2+(q.z-nz)^2
  if dn < 2500 and (q.x-ox)^2+(q.y-oy)^2+(q.z-oz)^2 >= dn then return end
  local w = rot(q)
  pt.pos = vec3(w.x+T[1], w.y+T[2], w.z+T[3])
  if pt.dirFront then pt.dirFront = rot(pt.dirFront) end
  if pt.dirUp then pt.dirUp = rot(pt.dirUp) end
end
if recovery then
  local d = recovery.recoveryPoints
  if d and d.head and d.tail then for i = d.head + 1, d.tail do map(d[i]) end end
  map(recovery.homePoint)
end
-- the vehicle's copy of the road graph moved with it, as a new table: the AI drops its plan (ai.lua updateGFX: a
-- new mapmgr.mapData) and plans again on the moved points in this very step (it kept the old frame's points until the
-- moved graph came from GE a few frames later and swerved: Baltimore's North Front Street, 2026-10-29)
local md = mapmgr and mapmgr.mapData
if type(md) == 'table' and md.positions then
  local seen = {}
  local function mv(v)
    if type(v) ~= 'cdata' and type(v) ~= 'table' or seen[v] or not v.x then return end
    seen[v] = true
    local w = rot(v)
    v:set(w.x + T[1], w.y + T[2], w.z + T[3])
  end
  for _, q in pairs(md.positions) do mv(q) end
  for _, links in pairs(md.graph or {}) do
    if type(links) == 'table' then
      for _, e in pairs(links) do if type(e) == 'table' then mv(e.inPos) mv(e.outPos) end end
    end
  end
  local c = setmetatable({}, getmetatable(md))
  for k, v in pairs(md) do c[k] = v end
  mapmgr.mapData = c
end]], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], t[1], t[2], t[3], p.x, p.y, p.z, x, y, z)
  local ok2, err2 = pcall(veh.queueLuaCommand, veh, cmd)
  if not ok2 then log('W', 'beamngEarth', 'recovery point update skipped: ' .. tostring(err2)) end
end

-- (from KyzoFox's 1.5.4, PR #64) trailers stay hitched through a rebase. moveVehicles moves the truck and its trailer with two
-- requests; the physics can apply them a step apart, the coupler between them then stretched by the whole rebase
-- distance for that step and broke (the user, 2026-10-19 between Canton, GA and Tuscaloosa, AL: the trailer came off at
-- every "Loading n/222", tyres screeching). The fifth wheel does not latch again by itself until its kingpin has been
-- 0.6 m away (vehicle/controller/couplings/fifthwheel.lua: state 'detaching'), so the couplings known before the move
-- (core_trailerRespawn's registry; it keeps an entry through a break, onCouplerDetached returns on breakForce > 0) are
-- attached again after it, a few times over RECOUPLE_AT, with obj:attachExternalCoupler as the fifth wheel itself does
-- (its onCouplerAttached then sets it attached), only where the vehicle reports nothing attached.
M.RECOUPLE_AT = { 0, 0.15, 0.4, 0.8, 1.5, 2.5 }   -- s after the rebase's phase 2

-- The couplings now: { { veh, trailer, node, trailerNode } } (head vehicle id, trailer id, their coupler node cids).
function M.couplings()
  local out = {}
  local tr = rawget(_G, 'core_trailerRespawn')
  if type(tr) ~= 'table' or type(tr.getTrailerData) ~= 'function' then return out end
  local ok, reg = pcall(tr.getTrailerData)
  if not ok or type(reg) ~= 'table' then return out end
  for vehId, c in pairs(reg) do
    if type(c) == 'table' and tonumber(c.trailerId) and tonumber(c.node) and tonumber(c.trailerNode) then
      out[#out + 1] = { veh = tonumber(vehId), trailer = tonumber(c.trailerId), node = tonumber(c.node),
        trailerNode = tonumber(c.trailerNode) }
    end
  end
  table.sort(out, function(a, b) return a.veh < b.veh end)
  return out
end

-- The vehicle Lua that attaches coupling c again if its vehicle has nothing attached. Pure.
function M.recoupleCommand(c)
  return string.format([[
local N, TID, TN = %d, %d, %d
local att = (beamstate.isCouplerAttached and beamstate.isCouplerAttached())
  or (extensions.couplings and extensions.couplings.isCouplerAttached and extensions.couplings.isCouplerAttached())
local nd = v and v.data and v.data.nodes and v.data.nodes[N]
if not att and nd then
  obj:attachExternalCoupler(N, TID, TN, nd.couplerStrength or 10000000, math.max(nd.couplerRadius or 0.5, 0.5),
    nd.couplerLockRadius or 0.025, nd.couplerLatchSpeed or 0.3, 0)
end]], c.node, c.trailer, c.trailerNode)
end

-- A re-hitch plan for these couplings starting at time now (s), or nil when there are none.
function M.recoupleStart(couplings, now)
  if type(couplings) ~= 'table' or #couplings == 0 then return nil end
  return { couplings = couplings, t0 = now, next = 1 }
end

-- Send the plan's due attempts at time now (s); returns the plan, or nil once it is done.
function M.recoupleTick(plan, now)
  if not plan then return nil end
  while plan.next <= #M.RECOUPLE_AT and now - plan.t0 >= M.RECOUPLE_AT[plan.next] do
    for _, c in ipairs(plan.couplings) do
      local veh = scenetree.findObjectById(c.veh)
      if veh and scenetree.findObjectById(c.trailer) then pcall(veh.queueLuaCommand, veh, M.recoupleCommand(c)) end
    end
    plan.next = plan.next + 1
  end
  if plan.next > #M.RECOUPLE_AT then return nil end
  return plan
end

return M
