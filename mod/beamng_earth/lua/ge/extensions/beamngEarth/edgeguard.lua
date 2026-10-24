-- Never fall out of the world (docs/ROADMAP.md "Robustness" 2). When the physics block cannot follow
-- the player (no server, the connection lost, the server not answering terrain requests), the mod
-- keeps the last block; beyond its collidable area there is nothing to stand on. core.lua runs this
-- guard ONLY then (never while the server is healthy, so it never meets a block commit) for the
-- player's vehicle:
--   clear  far inside the block: nothing (no ray, no call)
--   warn   within WARN_M of an edge, or of the braking distance plus WARN_LEAD at the current speed:
--          the banner says so
--   hold   the game's own pause (simTimeAuthority) when the car would reach STOP_M inside an edge within
--          PAUSE_LEAD_S, heading towards it; it resumes once the block followed (the car well inside again).
--          The player may resume by hand to drive back inwards: no new pause for RESUME_GRACE_S. (It was a
--          soft wall setting the car's velocity every frame; the user, 2026-10-18: "use this [the pause] for the
--          edge system instead of doing whatever it was doing before when it tried to stop the car".)
--   rescue the car left the block anyway (faster than the wall could brake it, or it was already
--          past the edge) and has dropped RESCUE_DROP below the ground it last stood on: it is set
--          down STOP_M inside the edge, heading kept (spawn.safeTeleport without a reset, as the
--          game's own recovery: core/recoveryPrompt.lua)
--   air    over the block but not on the ground (a jump, an aircraft): never touched. An aircraft
--          that flies off the block is never stopped either (it is above the ground it left).
-- "On the ground": a ray down from the vehicle (castRayStatic) hits static collision within
-- NEAR_GROUND_M of its reference node. Only while the vehicle is near an edge.
--
-- The velocity is set (applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz): scale 0 = replace, as
-- tools/drive_driver.lua and spawn.lua do), not added to: setting the same target again in a frame
-- whose velocity reading lags the last change does nothing more (an add would brake twice and
-- throw the car back inwards).
--
-- step() is pure (luajit tests: mod/tests/test_edgeguard.lua); update() does the engine calls.

local M = {}

M.WARN_M = 150          -- m from the collidable edge: the banner warns
M.WARN_LEAD = 30        -- m: warn this much before the braking distance too, whichever is further
M.STOP_M = 15           -- m inside the edge where the soft wall brings the car to rest
M.DECEL = 6             -- m/s^2 of the soft wall's braking profile
M.NEAR_GROUND_M = 6     -- the reference node at most this far above the ground: on the ground
M.RAY_UP = 2            -- the ground ray starts this far above the reference node
M.RESCUE_DROP = 3       -- off the block and this far below its last ground: set it back
M.RESCUE_WINDOW_S = 10  -- ... only this long after it was last on the ground of the block
M.RESCUE_GAP_S = 1      -- at most one rescue per this many seconds
M.SET_EPS = 0.05        -- m/s: a smaller velocity change is not applied
M.RAY_MARGIN = 50       -- m beyond the warn distance where the ray starts
-- Inside the stop line the wall pulls back: the velocity component towards the edge is capped at
-- -PULL * (STOP_M - d), at most PULL_MAX inwards. Capping it at 0 let a car under throttle creep
-- outwards (in-game 2026-10-11: 4.6 m in 50 s), since physics accelerates it again between two GE
-- frames by less than SET_EPS each time.
M.PULL = 0.5            -- 1/s: pull back speed per m past the stop line
M.PULL_MAX = 2.0        -- m/s: at most
M.PAUSE_LEAD_S = 0.5    -- s: pause when the car would reach the stop line this soon
M.PAUSE_MIN_SPEED = 0.3 -- m/s towards the edge below which it is not moving there
M.RESUME_INSIDE_M = 60  -- m past the stop line before a guard pause resumes by itself
M.RESUME_GRACE_S = 5    -- s: the player resumed by hand: no new pause meanwhile
-- The speed acted on is the vehicle's velocity, but no faster than it moved since the last frame plus SPEED_SLACK: a car
-- just spawned (or made active again) can read a velocity it does not have. A BeamMP spawn read 2346 mph at rest: held
-- (in BeamMP the hold hides the car, simhold.lua), let go the next frame (well inside), held again, every frame: the car
-- vanished (the user, 2026-10-23: "when i try to spawn a car it instantly gets despawned on BeamMP").
M.SPEED_SLACK = 5       -- m/s
M.SAMPLE_MAX_S = 0.5    -- s: an older position sample (the guard was off meanwhile) measures nothing

local sqrt, max, min, abs = math.sqrt, math.max, math.min, math.abs

function M.new()
  return { mode = 'off', lastGroundZ = nil, lastGroundT = nil, lastRescue = -math.huge, holds = 0, rescues = 0 }
end

-- The world frame changed (an anchor committed: teleport, rebase, resync): the ground height it
-- remembers for the rescue is in the old frame's coordinates.
function M.newFrame(g) g.lastGroundZ, g.lastGroundT, g.lastT = nil, nil, nil end

-- The highest speed towards an edge at distance d that can still stop STOP_M inside it; negative
-- (a pull back inwards) inside the stop line.
function M.cap(d)
  if d >= M.STOP_M then return sqrt(2 * M.DECEL * (d - M.STOP_M)) end
  return -min(M.PULL_MAX, M.PULL * (M.STOP_M - d))
end

-- Distance from the nearest edge at which a vehicle at `speed` is warned.
function M.warnDistance(speed)
  return max(M.WARN_M, M.STOP_M + speed * speed / (2 * M.DECEL) + M.WARN_LEAD)
end

local function clamp(v, a, b) return min(max(v, a), b) end

-- One frame (pure). g: M.new() state; inp = { clock, x, y, z, vx, vy, box = { x0, y0, x1, y1 } (the
-- collidable area), ground = the reference node's height above the ground or nil (no hit within
-- the ray) }. Returns mode, setV ({ vx, vy } to set, or nil), rescue ({ x, y, z } or nil).
function M.step(g, inp)
  local x, y, z, vx, vy = inp.x, inp.y, inp.z, inp.vx, inp.vy
  local b = inp.box
  local x0, y0, x1, y1 = b[1], b[2], b[3], b[4]
  local inside = x >= x0 and x <= x1 and y >= y0 and y <= y1
  local grounded = inp.ground ~= nil and inp.ground <= M.NEAR_GROUND_M
  if inside and grounded then g.lastGroundZ, g.lastGroundT = z - inp.ground, inp.clock end
  if not inside then
    if g.lastGroundZ and inp.clock - g.lastGroundT <= M.RESCUE_WINDOW_S and z < g.lastGroundZ - M.RESCUE_DROP
      and inp.clock - g.lastRescue >= M.RESCUE_GAP_S then
      g.lastRescue = inp.clock
      g.rescues = g.rescues + 1
      local m = min(M.STOP_M, (x1 - x0) / 2, (y1 - y0) / 2)
      return 'hold', nil, { clamp(x, x0 + m, x1 - m), clamp(y, y0 + m, y1 - m), g.lastGroundZ + 1 }
    end
    return 'air'
  end
  if not grounded then return 'air' end
  -- towards each edge: the distance past the stop line and the speed towards it
  local function soon(dEdge, s)
    return s > M.PAUSE_MIN_SPEED and dEdge - M.STOP_M <= s * M.PAUSE_LEAD_S + 1
  end
  local d = min(x - x0, x1 - x, y - y0, y1 - y)
  -- (never farther in than a hold resumes by itself (update): such a hold ended the next frame and started again, every
  -- frame: in BeamMP the car hidden for good after a teleport, the user 2026-10-23 "the car disappears when i teleport";
  -- a car that fast is still caught: the pause line is RESUME_INSIDE_M deep, a frame moves it far less)
  if d <= M.STOP_M + M.RESUME_INSIDE_M
      and (soon(x1 - x, vx) or soon(x - x0, -vx) or soon(y1 - y, vy) or soon(y - y0, -vy)) then
    g.holds = g.holds + 1
    return 'hold', nil, nil, true
  end
  if d <= M.STOP_M + 1 then return 'hold' end -- resting at the line
  if d < M.warnDistance(sqrt(vx * vx + vy * vy)) then return 'warn' end
  return 'clear'
end

-- Pure: the velocity (vx, vy) scaled down to what the car really moved since the last sample (g.lastX/Y/T, updated):
-- at most the moved distance / dt + SPEED_SLACK. No usable sample (the first, an old one): SPEED_SLACK at most.
function M.plausible(g, clock, x, y, vx, vy)
  local moved = 0
  if g.lastT and clock > g.lastT and clock - g.lastT <= M.SAMPLE_MAX_S then
    local dx, dy = x - g.lastX, y - g.lastY
    moved = sqrt(dx * dx + dy * dy) / (clock - g.lastT)
  end
  if not (g.lastT and clock == g.lastT) then g.lastX, g.lastY, g.lastT = x, y, clock end
  local v = sqrt(vx * vx + vy * vy)
  local cap = moved + M.SPEED_SLACK
  if v <= cap or v ~= v then return (v == v) and vx or 0, (v == v) and vy or 0 end
  return vx * cap / v, vy * cap / v
end

-- The reference node's height above static collision (castRayStatic; a miss returns >= the length),
-- or nil: no ground within NEAR_GROUND_M (or no ray function).
function M.groundBelow(p)
  local ray = rawget(_G, 'castRayStatic')
  if not ray then return nil end
  local len = M.RAY_UP + M.NEAR_GROUND_M + 1
  local d = ray(vec3(p.x, p.y, p.z + M.RAY_UP), vec3(0, 0, -1), len)
  if type(d) ~= 'number' or d >= len then return nil end
  return d - M.RAY_UP
end

-- One frame with the engine: veh (the player's vehicle), box (the collidable area), clock (s).
-- Returns the mode. Far inside the block nothing is called at all.
-- (in a BeamMP session: our own car held, not every car on the screen: simhold.lua)
local simhold = require('beamngEarth/simhold')
local function simPaused() return simhold.paused() end
local function simPause(on) simhold.pause(on) end

-- A guard pause ends: the block followed (the car RESUME_INSIDE_M past the stop line), or the guard is turned off
-- (the server healthy again: core.lua calls this then).
function M.release(g)
  if g.pausedByUs and simPaused() then simPause(false) end
  g.pausedByUs = nil
end

function M.update(g, veh, box, clock)
  local p, vel = veh:getPosition(), veh:getVelocity()
  local vx, vy = M.plausible(g, clock, p.x, p.y, vel.x, vel.y)
  local v = { x = vx, y = vy }
  local d = min(p.x - box[1], box[3] - p.x, p.y - box[2], box[4] - p.y)
  if g.pausedByUs then
    if not simPaused() then                       -- the player resumed: let them drive back inwards
      g.pausedByUs, g.resumedAt = nil, clock
    elseif d > M.STOP_M + M.RESUME_INSIDE_M then  -- the block followed
      M.release(g)
    else
      g.mode = 'hold'
      return g.mode
    end
  end
  if d > M.warnDistance(sqrt(v.x * v.x + v.y * v.y)) + M.RAY_MARGIN then
    g.mode = 'clear'
    return g.mode
  end
  local mode, _, rescue, pause = M.step(g, { clock = clock, x = p.x, y = p.y, z = p.z, vx = v.x, vy = v.y, box = box,
    ground = M.groundBelow(p) })
  if pause and not simPaused() and clock - (g.resumedAt or -math.huge) >= M.RESUME_GRACE_S then
    simPause(true)
    g.pausedByUs = true
  end
  if rescue then
    local dir = veh:getDirectionVector()
    dir = vec3(dir.x, dir.y, 0)
    if dir:length() < 1e-3 then dir = vec3(0, 1, 0) end
    -- no reset (damage kept): the game's own upright-recovery call (core/recoveryPrompt.lua:147)
    spawn.safeTeleport(veh, vec3(rescue[1], rescue[2], rescue[3]), quatFromDir(dir:normalized()), nil, nil, nil, nil, false)
    veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
  end
  g.mode = mode
  return mode
end

return M
