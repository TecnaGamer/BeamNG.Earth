-- luajit mod/tests/test_edgeguard.lua  (never fall out of the world, edgeguard.lua; docs/ROADMAP.md "Robustness" 2)
-- Driven like the game with stubs: a vehicle whose velocity the guard sets (applyClusterVelocityScaleAdd
-- with scale 0) and a ground ray (castRayStatic) over a flat block. Checks: a car driving at an edge is
-- warned, then braked at no more than DECEL and comes to rest STOP_M inside, never past the edge, at
-- 30 and 134 m/s and diagonally into a corner; along the edge and back inwards it drives freely; far
-- inside nothing is called (no ray); a jump or an aircraft over the block is never touched and an
-- aircraft flies off the block untouched; a car that went off the edge and dropped is set down inside
-- once (safeTeleport without a reset, velocity zeroed), rate-limited; the ground ray's miss.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local edgeguard = require('beamngEarth/edgeguard')
local lattice = require('beamngEarth/lattice')

lattice.configure(512)
local BOX = { lattice.groundBox(0, 0) } -- [-512, 510]
T.check(BOX[1] == -512 and BOX[3] == 510, '512 block: collidable [c-512, c+510]')
local GROUND_Z = 100

local rays = 0
function castRayStatic(from, dir_, len)
  rays = rays + 1
  local inside = from.x >= BOX[1] and from.x <= BOX[3] and from.y >= BOX[2] and from.y <= BOX[4]
  if not inside then return len + 1 end -- off the block: nothing below (a miss returns >= the length)
  local d = from.z - GROUND_Z
  if d < 0 or d > len then return len + 1 end
  return d
end
function quatFromDir(d) return { d = d } end
local teleports = {}
spawn = { safeTeleport = function(v, pos, rot, a, b, c, d, reset)
  teleports[#teleports + 1] = { pos = vec3(pos), reset = reset, rot = rot }
  v.pos = vec3(pos.x, pos.y, GROUND_Z + 0.5)
end }

-- the game's pause (simTimeAuthority): physics stands still while paused
simTimeAuthority = { paused = false }
function simTimeAuthority.pause(on) simTimeAuthority.paused = on and true or false end
function simTimeAuthority.getPause() return simTimeAuthority.paused end

local function newVeh(x, y, z, vx, vy)
  local v = { pos = vec3(x, y, z), vel = vec3(vx, vy, 0), sets = 0 }
  function v:getPosition() return vec3(self.pos) end
  function v:getVelocity() return vec3(self.vel) end
  function v:getDirectionVector() return vec3(1, 0, 0) end
  function v:applyClusterVelocityScaleAdd(node, scale, ax, ay, az)
    self.sets = self.sets + 1
    self.lastNode, self.lastScale = node, scale
    self.vel = vec3(self.vel.x * scale + ax, self.vel.y * scale + ay, self.vel.z * scale + az)
  end
  return v
end

-- a drive: the car keeps its velocity (the driver holds the throttle), physics integrates
local function drive(veh, frames, dt, onFrame)
  local g = edgeguard.new()
  local clock, out = 0, { modes = {}, maxDecel = 0, minEdge = math.huge, firstWarn = nil, firstHold = nil }
  for f = 1, frames do
    clock = clock + dt
    local before = vec3(veh.vel)
    local mode = edgeguard.update(g, veh, BOX, clock)
    out.modes[mode] = (out.modes[mode] or 0) + 1
    if mode == 'warn' and not out.firstWarn then out.firstWarn = { f = f, x = veh.pos.x, y = veh.pos.y } end
    if mode == 'hold' and not out.firstHold then out.firstHold = { f = f, x = veh.pos.x } end
    -- (the last 1 m/s to rest brakes up to ~8 m/s^2: the profile is sampled once a frame; not counted)
    local dv = (before - veh.vel):length() / dt
    if dv > out.maxDecel and f > 1 and before:length() > 1 then out.maxDecel = dv end
    if not simTimeAuthority.paused then veh.pos = veh.pos + vec3(veh.vel.x * dt, veh.vel.y * dt, veh.vel.z * dt) end
    local p = veh.pos
    out.minEdge = math.min(out.minEdge, p.x - BOX[1], BOX[3] - p.x, p.y - BOX[2], BOX[4] - p.y)
    if onFrame then onFrame(f, g, mode) end
  end
  out.g = g
  return out
end

------------------------------------------------------------------------------------------
-- the pause at the edge (the user, 2026-10-18: the game's own pause instead of the soft wall)

T.check(edgeguard.warnDistance(0) == edgeguard.WARN_M and edgeguard.warnDistance(60) > 300, 'warn distance grows with speed')

local veh = newVeh(0, 0, GROUND_Z + 0.5, 30, 0)
local r = drive(veh, 60 * 40, 1 / 60)
T.check(r.firstWarn and r.firstWarn.x < BOX[3] - edgeguard.WARN_M + 1, '30 m/s east: warned >= 150 m before the edge')
T.check(r.firstHold and r.firstHold.x > r.firstWarn.x and simTimeAuthority.paused, '... then the game paused')
T.check(r.minEdge >= edgeguard.STOP_M - 0.5, string.format('never past the stop line (%.2f m inside)', r.minEdge))
T.check(veh.sets == 0 and veh.vel.x == 30, 'the car is never touched (its speed kept for the resume)')
T.check(r.g.pausedByUs, 'the guard knows it paused')
-- the player resumes to drive back inwards: no new pause meanwhile
simTimeAuthority.pause(false)
veh.vel = vec3(-15, 0, 0)
local g0 = r.g
local clock0 = 40
for f = 1, 60 do
  edgeguard.update(g0, veh, BOX, clock0 + f / 60)
  if not simTimeAuthority.paused then veh.pos = veh.pos + vec3(veh.vel.x / 60, 0, 0) end
end
T.check(not simTimeAuthority.paused and not g0.pausedByUs, 'resumed by the player: driving inwards, no pause')
-- a car at rest reading a velocity it does not have (a BeamMP spawn read 2346 mph): never held, frame after frame
do
  simTimeAuthority.pause(false)
  local g = edgeguard.new()
  local v3 = newVeh(0, -9, GROUND_Z + 0.5, 1048, 0)
  local holds = 0
  for f = 1, 120 do
    edgeguard.update(g, v3, BOX, 50 + f / 60)
    if simTimeAuthority.paused then holds = holds + 1 end
  end
  T.check(holds == 0 and not g.pausedByUs, 'a phantom speed at rest: never held')
  local vx = edgeguard.plausible({ lastX = 0, lastY = 0, lastT = 0 }, 1 / 60, 100 / 60, 0, 100, 0)
  T.check(math.abs(vx - 100) < 1e-6, 'plausible: a real 100 m/s kept')
  vx = edgeguard.plausible({}, 1, 0, 0, 1048, 0)
  T.check(math.abs(vx - edgeguard.SPEED_SLACK) < 1e-6, 'plausible: no sample: SPEED_SLACK at most')
end
-- a teleport: the car jumps far in one frame and reads a phantom speed towards an edge (8225 mph): no hold that the
-- next frame would end (it started again every frame); near the edge it is still paused
do
  simTimeAuthority.pause(false)
  local g = edgeguard.new()
  local v4 = newVeh(-2000, 0, GROUND_Z + 0.5, 3677, 0)
  edgeguard.update(g, v4, BOX, 60)
  v4.pos = vec3(0, 0, GROUND_Z + 0.5)
  local flips, was = 0, false
  for f = 1, 60 do
    edgeguard.update(g, v4, BOX, 60 + f / 60)
    if simTimeAuthority.paused ~= was then flips, was = flips + 1, simTimeAuthority.paused end
  end
  T.check(flips == 0, 'a teleport with a phantom speed: no hold flapping (' .. flips .. ' flips)')
  local st = edgeguard.new()
  local mode = edgeguard.step(st, { clock = 1, x = 0, y = 0, z = GROUND_Z + 0.5, vx = 3677, vy = 0, box = BOX, ground = 0.5 })
  T.check(mode ~= 'hold', 'step: 3677 m/s at the centre: no hold yet')
  mode = edgeguard.step(st, { clock = 1, x = BOX[3] - edgeguard.STOP_M - 40, y = 0, z = GROUND_Z + 0.5, vx = 3677, vy = 0,
    box = BOX, ground = 0.5 })
  T.check(mode == 'hold', 'step: the same speed 40 m before the stop line: held')
end
-- the block follows while paused: the car well inside the new box resumes by itself
do
  local g = edgeguard.new()
  local v2 = newVeh(BOX[3] - edgeguard.STOP_M - 5, 0, GROUND_Z + 0.5, 30, 0)
  g.lastX, g.lastY, g.lastT = v2.pos.x - 0.5, 0, 1 - 1 / 60      -- (the frame before: it drove here at 30 m/s)
  edgeguard.update(g, v2, BOX, 1)
  T.check(simTimeAuthority.paused and g.pausedByUs, 'heading off the edge: paused')
  local moved = { BOX[1] + 512, BOX[2], BOX[3] + 512, BOX[4] }
  edgeguard.update(g, v2, BOX, 2)
  T.check(simTimeAuthority.paused, 'the block not followed yet: still paused')
  edgeguard.update(g, v2, moved, 3)
  T.check(not simTimeAuthority.paused and not g.pausedByUs, 'the block followed: resumed by itself')
  -- the player had paused the game already: the guard never resumes it
  local g2 = edgeguard.new()
  simTimeAuthority.pause(true)
  edgeguard.update(g2, newVeh(BOX[3] - edgeguard.STOP_M - 5, 0, GROUND_Z + 0.5, 30, 0), BOX, 4)
  T.check(not g2.pausedByUs, 'already paused by the player: not the guard\'s pause')
  simTimeAuthority.pause(false)
end

-- 134 m/s: paused before the stop line, never off the block
veh = newVeh(-400, 0, GROUND_Z + 0.5, 134, 0)
r = drive(veh, 60 * 20, 1 / 60)
T.check(r.minEdge >= edgeguard.STOP_M - 1, string.format('134 m/s: stays on the block (%.2f m inside)', r.minEdge))
T.check(simTimeAuthority.paused, '134 m/s: paused')
simTimeAuthority.pause(false)
-- diagonally into the NE corner
veh = newVeh(100, 100, GROUND_Z + 0.5, 25, 25)
r = drive(veh, 60 * 30, 1 / 60)
T.check(r.minEdge >= edgeguard.STOP_M - 0.5, 'corner: paused before either edge')
simTimeAuthority.pause(false)

------------------------------------------------------------------------------------------
-- far inside: nothing called

rays = 0
veh = newVeh(0, 0, GROUND_Z + 0.5, 5, 0)
sets = veh.sets
local g = edgeguard.new()
T.check(edgeguard.update(g, veh, BOX, 1) == 'clear' and rays == 0 and veh.sets == sets, 'far inside: clear, no ray, no call')

------------------------------------------------------------------------------------------
-- off the ground: never touched

veh = newVeh(BOX[3] - 30, 0, GROUND_Z + 12, 30, 0) -- a jump 12 m above the ground near the edge
g = edgeguard.new()
T.check(edgeguard.update(g, veh, BOX, 1) == 'air' and veh.sets == 0, 'airborne over the block: untouched')
-- an aircraft at 500 m flies off the block: never stopped, never rescued
veh = newVeh(0, 0, GROUND_Z + 500, 80, 0)
teleports = {}
r = drive(veh, 60 * 20, 1 / 60)
T.check(veh.sets == 0 and #teleports == 0 and veh.pos.x > BOX[3] + 500, 'an aircraft flies off the block untouched')
-- a low aircraft that took off near the edge and climbs off the block: above its ground, never rescued
veh = newVeh(BOX[3] - 200, 0, GROUND_Z + 0.5, 70, 0)
teleports = {}
r = drive(veh, 60 * 10, 1 / 60, function(f, gg, mode)
  if f == 5 then veh.vel = vec3(70, 0, 8) end -- lift off at once
end)
T.check(#teleports == 0, 'a climbing aircraft leaving the block: no rescue')

------------------------------------------------------------------------------------------
-- rescue: the car went off the edge (the server died with the car already there) and falls

teleports = {}
g = edgeguard.new()
veh = newVeh(BOX[3] - 3, 50, GROUND_Z + 0.5, 0, 0)
edgeguard.update(g, veh, BOX, 1) -- on the ground near the edge: its ground recorded
T.check(g.lastGroundZ and math.abs(g.lastGroundZ - GROUND_Z) < 1e-9, 'the ground under the car is recorded near the edge')
veh.pos = vec3(BOX[3] + 4, 50, GROUND_Z - 1)
veh.vel = vec3(10, 0, -5)
T.check(edgeguard.update(g, veh, BOX, 1.1) == 'air' and #teleports == 0, 'off the edge, not yet below its ground: nothing yet')
veh.pos = vec3(BOX[3] + 6, 50, GROUND_Z - 4)
T.check(edgeguard.update(g, veh, BOX, 1.2) == 'hold' and #teleports == 1, 'dropped below its ground: rescued')
local tp = teleports[1]
T.near(tp.pos.x, BOX[3] - edgeguard.STOP_M, 1e-9, 'set down STOP_M inside the edge')
T.near(tp.pos.y, 50, 1e-9, '... at the same place along it')
T.check(tp.pos.z == GROUND_Z + 1 and tp.reset == false, '... above its last ground, without a reset (damage kept)')
T.check(veh.vel:length() == 0, '... and stopped')
veh.pos = vec3(BOX[3] + 8, 50, GROUND_Z - 8)
edgeguard.update(g, veh, BOX, 1.5)
T.check(#teleports == 1, 'rescues are rate-limited (one per RESCUE_GAP_S)')
edgeguard.update(g, veh, BOX, 2.3)
T.check(#teleports == 2, '... and repeated after it')
veh.pos = vec3(BOX[3] + 8, 50, GROUND_Z - 8)
edgeguard.update(g, veh, BOX, 1.2 + edgeguard.RESCUE_WINDOW_S + 5)
T.check(#teleports == 2, 'long after it left the ground: not rescued (not ours to catch)')
-- a new world frame (core: an anchor committed): the remembered ground height is the old frame's
g = edgeguard.new()
veh = newVeh(BOX[3] - 3, 50, GROUND_Z + 0.5, 0, 0)
edgeguard.update(g, veh, BOX, 30)
edgeguard.newFrame(g)
veh.pos = vec3(BOX[3] + 6, 50, GROUND_Z - 40) -- e.g. the new anchor's origin 40 m higher, off the block
T.check(edgeguard.update(g, veh, BOX, 30.5) == 'air' and #teleports == 2,
  'after a frame change: no rescue from the old frame\'s ground height')

------------------------------------------------------------------------------------------
-- the ground ray

castRayStatic = nil
T.check(edgeguard.groundBelow(vec3(0, 0, GROUND_Z + 1)) == nil, 'no ray function: no ground (nothing acts)')
function castRayStatic(from, d, len) return len end
T.check(edgeguard.groundBelow(vec3(0, 0, 0)) == nil, 'a miss (the full length): no ground')
function castRayStatic(from, d, len) return edgeguard.RAY_UP + 0.4 end
T.near(edgeguard.groundBelow(vec3(0, 0, 0)), 0.4, 1e-12, 'a hit: the height above the ground')

T.done('test_edgeguard')
