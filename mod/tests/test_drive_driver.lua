-- luajit mod/tests/test_drive_driver.lua  (tools/drive_driver.lua, docs/V1_4_SPEC.md 3.0 M0)
-- The GE-side per-frame driver under stubs: profile evaluation, the yaw quaternion (checked with
-- BeamNG's quat * vec3), install (extension proxy sink, rebase wrapper, zero inputs), per-frame
-- placement and velocity without resets, ground following and falls, frame flags (heavy, harness,
-- rebase, miss), a rebase moving the driver's frame, jump / end / pause, poll and uninstall.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local DRIVER = dir .. '/../../tools/drive_driver.lua'

------------------------------------------------------------------------------------------
-- engine stubs

local function ground(x, y) return 100 + 0.02 * x - 0.01 * y end
local MISS_X = 5000 -- no static collision beyond x = 5000 (off the terrain block)
function castRayStatic(pos, dirv, len)
  if pos.x > MISS_X then return len end
  local d = pos.z - ground(pos.x, pos.y)
  if d < 0 or d > len then return len end
  return d
end

local veh = { pos = vec3(10, 20, ground(10, 20) + 0.6), vel = vec3(0, 0, 0), heading = 90, calls = {}, cmds = {} }
function veh:getID() return 7 end
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function veh:getVelocity() return vec3(self.vel.x, self.vel.y, self.vel.z) end
function veh:getDirectionVector()
  local h = math.rad(self.heading)
  return vec3(math.sin(h), math.cos(h), 0)
end
function veh:setClusterPosRelRot(node, x, y, z, qx, qy, qz, qw)
  self.calls[#self.calls + 1] = { 'pos', node, x, y, z, qx, qy, qz, qw }
  self.pos = vec3(x, y, z)
  -- the relative turn applied to the forward vector (BeamNG quat * vec3)
  local d = self:getDirectionVector()
  local fx, fy = geodesy.beamngRotate(qx, qy, qz, qw, d.x, d.y, d.z)
  self.heading = math.deg(math.atan2(fx, fy)) % 360
end
function veh:applyClusterVelocityScaleAdd(node, scale, vx, vy, vz)
  self.calls[#self.calls + 1] = { 'vel', node, scale, vx, vy, vz }
  self.vel = vec3(self.vel.x * scale + vx, self.vel.y * scale + vy, self.vel.z * scale + vz)
end
function veh:queueLuaCommand(c) self.cmds[#self.cmds + 1] = c end
local player = veh
function getPlayerVehicle() return player end

local proxies = {}
function newExtensionProxy(parent, prefix)
  local p = { prefix = prefix, sinks = nil, destroyed = false }
  function p:submitEventSinks(sinks, deps) self.sinks, self.deps = sinks, deps end
  function p:destroy() self.destroyed = true end
  proxies[#proxies + 1] = p
  return p
end

local heavy = false
local gov = {}
function gov:isHeavyFrame() return heavy end
beamngEarth_core = {
  _governor = function() return gov end,
  status = function() return '{ anchorId = 3, far = { live = 811, queued = 4 }, serverBacklog = 12 }' end,
}
local tagged = 0
beamngEarth_perfmon = { tagHarness = function() tagged = tagged + 1 end, snapshot = function() return { fps = 60 } end }

local exact = { level = -2, old = false, miss = false }
local farfieldStub = { exactState = function() return exact.level, exact.old, exact.miss end }
package.loaded['beamngEarth/farfield'] = farfieldStub
local moveCalls = 0
local rebaseMod = {}
function rebaseMod.moveVehicles(xf)
  moveCalls = moveCalls + 1
  local x, y, z = xf:apply(veh.pos.x, veh.pos.y, veh.pos.z)
  veh.pos = vec3(x, y, z)
  local d = veh:getDirectionVector() -- turned by R, as setClusterPosRelRot(-1, ..., xf:beamngQuat()) does
  local qx, qy, qz, qw = xf:beamngQuat()
  local fx, fy = geodesy.beamngRotate(qx, qy, qz, qw, d.x, d.y, d.z)
  veh.heading = math.deg(math.atan2(fx, fy)) % 360
  return { [7] = true }, 7
end
local origMove = rebaseMod.moveVehicles
package.loaded['beamngEarth/rebase'] = rebaseMod

local now = 0
local DT = 1 / 60
-- one engine frame: the mod's onUpdate (optional), physics (the velocity moves the car), onPreRender
local function frame(dtSim, onUpdate)
  now = now + 1000 * DT
  if onUpdate then onUpdate() end
  local d = dtSim or DT
  veh.pos = vec3(veh.pos.x + veh.vel.x * d, veh.pos.y + veh.vel.y * d, veh.pos.z + veh.vel.z * d)
  local p = proxies[#proxies]
  for _, s in ipairs(p.sinks) do s:onPreRender(DT, d) end
end

local function load()
  T.check(dofile(DRIVER) == 'ok', 'the chunk returns ok')
  BNGE_DRIVE.clock = function() return now end
  return BNGE_DRIVE
end

------------------------------------------------------------------------------------------
-- pure helpers
local D = load()
T.near(D.headingOf(1, 0), 90, 1e-9, 'heading east')
T.near(D.headingOf(0, -1), 180, 1e-9, 'heading south')
T.near(D.headingOf(-1, 0), 270, 1e-9, 'heading west')
T.near(D.angleDiff(10, 350), 20, 1e-9, 'angleDiff across north')
T.near(D.angleDiff(350, 10), -20, 1e-9, 'angleDiff across north, back')
T.near(D.angleDiff(180, 0), 180, 1e-9, 'angleDiff half turn')
for _, a in ipairs({ 0, 37, 90, 200, 359 }) do
  for _, dh in ipairs({ -170, -53, -1, 0, 0.5, 45, 179 }) do
    local qx, qy, qz, qw = D.yawQuat(dh)
    local h = math.rad(a)
    local fx, fy, fz = geodesy.beamngRotate(qx, qy, qz, qw, math.sin(h), math.cos(h), 0)
    T.near(D.angleDiff(D.headingOf(fx, fy), (a + dh) % 360), 0, 1e-9, 'yawQuat turns heading ' .. a .. ' by ' .. dh)
    T.near(fz, 0, 1e-12, 'yawQuat stays level')
  end
end

local prof, err = D.makeProfile({ { t = 0, speed = 10, heading = 350 }, { t = 10, speed = 30, heading = 10 },
  { t = 20, speed = 30 } }, 123)
T.check(prof and prof.key == 't' and prof.total == 20 and not prof.flight, 'time profile')
local v, h = D.profileAt(prof, 5)
T.near(v, 20, 1e-9, 'speed interpolated')
T.near(h, 0, 1e-9, 'heading interpolated along the short arc (350 -> 10)')
v, h = D.profileAt(prof, 15)
T.near(h, (10 + 0.5 * D.angleDiff(123, 10)) % 360, 1e-9, 'a point without heading uses heading0')
v, h = D.profileAt(prof, 99)
T.check(v == 30 and h == 123, 'held after the last point')
v = D.profileAt(prof, 0)
T.check(v == 10, 'cursor resets when u goes back')
T.check(select(2, D.makeProfile({ { t = 0, speed = 1 }, { t = 0, speed = 2 } })) ~= nil, 't must increase')
T.check(select(2, D.makeProfile({ { s = 0, speed = -1 } })) ~= nil, 'speed >= 0')
T.check(select(2, D.makeProfile({})) ~= nil, 'empty profile')
local fp = D.makeProfile({ { s = 0, speed = 250, heading = 0, alt = 300, vz = 0 }, { s = 1000, speed = 250, alt = 500 } })
T.check(fp.key == 's' and fp.flight, 'distance-keyed flight profile')
local _, _, alt = D.profileAt(fp, 500)
T.near(alt, 400, 1e-9, 'alt interpolated')
T.check(D.encode({ a = 1, b = { 1.5, 'x"y' }, c = true, e = {} }) == '{"a":1,"b":[1.5,"x\\u0022y"],"c":true,"e":[]}',
  'fallback JSON encoder: ' .. D.encode({ a = 1, b = { 1.5, 'x"y' }, c = true, e = {} }))

------------------------------------------------------------------------------------------
-- install
local res = D.install({ points = { { s = 0, speed = 40, heading = 37 } }, km = 1.0 })
T.check(res:match('"ok":true') ~= nil, 'install ok: ' .. res)
T.check(D.installed and #proxies == 1 and proxies[1].prefix == 'beamngEarthDrive', 'one extension proxy')
T.check(type(proxies[1].sinks[1].onPreRender) == 'function' and type(proxies[1].sinks[1].onVehicleResetted) == 'function',
  'sink hooks onPreRender and onVehicleResetted')
T.check(rebaseMod.moveVehicles ~= origMove, 'rebase.moveVehicles wrapped')
T.check(#veh.cmds == 1 and veh.cmds[1]:match("input.event%('throttle', 0") ~= nil, 'inputs zeroed at install')
T.near(D.state.clearance, 0.6, 1e-9, 'clearance measured at install')
T.check(D.install({ points = { { s = 0, speed = 1 } } }):match('already installed') ~= nil, 'no double install')

-- straight drive at 40 m/s, heading 37, from heading 90
veh.calls = {}
for _ = 1, 60 do frame() end
local posCalls, velCalls = 0, 0
local first
for _, c in ipairs(veh.calls) do
  if c[1] == 'pos' then
    posCalls = posCalls + 1
    first = first or c
    T.check(c[2] == -1, 'setClusterPosRelRot on every cluster')
  else
    velCalls = velCalls + 1
    T.check(c[2] == -1 and c[3] == 0, 'velocity replaced (scale 0)')
    T.near(math.sqrt(c[4] ^ 2 + c[5] ^ 2), 40, 1e-9, 'velocity = profile speed')
    T.near(D.headingOf(c[4], c[5]), 37, 1e-9, 'velocity along the heading')
  end
end
T.check(posCalls == 60 and velCalls == 60, 'one placement and one velocity per frame')
T.near(D.angleDiff(veh.heading, 37), 0, 1e-9, 'the car turned to the profile heading')
T.check(first[9] < 0.95, 'first frame turned the car (90 -> 37)')
local last = veh.calls[#veh.calls - 1]
T.near(last[9], 1, 1e-12, 'then no further turn')
T.near(D.state.s, 40, 1e-6, '1 s of sim time = 40 m')
local hx, hy = math.sin(math.rad(37)), math.cos(math.rad(37))
T.near(D.state.x, 10 + 40 * hx, 1e-6, 'x along the heading')
T.near(D.state.y, 20 + 40 * hy, 1e-6, 'y along the heading')
T.near(veh.pos.z, ground(D.state.x, D.state.y) + 0.6, 1e-6, 'placed at ground + clearance')
local vlast = veh.calls[#veh.calls]
T.near(vlast[6], 40 * (0.02 * hx - 0.01 * hy), 1e-6, 'vz follows the ground slope')

-- poll: the frames since install, the harness tag on the next frame
local pt = D.pollTable()
T.check(#pt.frames.ms == 60, 'poll returns 60 frames')
T.near(pt.frames.ms[30], 1000 * DT, 1e-2, 'frame ms from the clock')
T.check(pt.frames.under[1] == -2 and pt.frames.flags[1] == D.FLAG_HARNESS and pt.frames.flags[5] == 0,
  'under level and flags per frame; the install call lands in the first frame')
T.check(pt.mod and pt.mod.serverBacklog == 12 and pt.mod.far.live == 811, 'mod status snapshot parsed')
T.check(pt.perf and pt.perf.fps == 60 and tagged == 1, 'perfmon tagged and snapshotted')
T.check(pt.drive.vN == 59 and math.abs(pt.drive.vSum / pt.drive.vN - 40) < 1e-6, 'physics speed sampled (= profile)')
T.near(pt.wallMs, 1000, 1e-6, 'wall ms since install')
local savedEngine = Engine
Engine = { getCurrentFpsLimitReason = function() return { windowFocused = false, limit = 90 } end }
local limitPoll = D.pollTable()
T.check(limitPoll.fpsLimit and limitPoll.fpsLimit.windowFocused == false and limitPoll.fpsLimit.limit == 90,
  'poll records the actual FPS limiter reason and background focus')
Engine.getCurrentFpsLimitReason = function() error('unavailable') end
T.check(D.pollTable().fpsLimit == nil, 'missing limiter telemetry does not break the drive')
Engine = savedEngine
frame()
pt = D.pollTable()
T.check(#pt.frames.ms == 1 and pt.frames.flags[1] == D.FLAG_HARNESS, 'the frame after a poll is a harness frame')
frame()
pt = D.pollTable()
T.check(pt.frames.flags[1] == D.FLAG_HARNESS, 'every poll tags one frame')
frame(); frame()
pt = D.pollTable()
T.check(pt.frames.flags[1] == D.FLAG_HARNESS and pt.frames.flags[2] == 0, 'only the next frame')

-- heavy frame, exact miss on an old tile
heavy, exact.level, exact.old, exact.miss = true, -1, true, true
frame()
heavy, exact.level, exact.old, exact.miss = false, nil, false, true
frame()
exact.level, exact.miss = -2, false
pt = D.pollTable()
T.check(pt.frames.flags[1] == D.FLAG_HEAVY + D.FLAG_MISS + D.FLAG_OLD + D.FLAG_HARNESS, 'heavy + miss + old flags')
T.check(pt.frames.under[1] == -1 and pt.frames.under[2] == D.NO_TILE, 'no tile under the player = NO_TILE')

-- a rebase: the mod moves the car in onUpdate; the driver follows with the same transform
local a1 = geodesy.newAnchor(36.5785, -118.2923)
local a2 = geodesy.newAnchor(36.66, -118.20)
local xf = a1:transformTo(a2)
local sx, sy, sz = D.state.x, D.state.y, D.state.z
local s0 = D.state.s
veh.calls = {}
frame(nil, function() rebaseMod.moveVehicles(xf, geodesy) end)
T.check(moveCalls == 1, 'the original moveVehicles ran')
local ex, ey = xf:apply(sx, sy, sz)
T.check(#veh.calls == 0, 'no placement in the rebase frame (the mod moved the car)')
T.near(D.state.x, ex + 40 * DT * math.sin(math.rad((37 + D.state.hOff) % 360)), 1e-6, 'driver state moved by the transform (x)')
T.near(D.state.y, ey + 40 * DT * math.cos(math.rad((37 + D.state.hOff) % 360)), 1e-6, 'driver state moved by the transform (y)')
T.near(D.state.s - s0, 40 * DT, 1e-9, 'distance still advances in the rebase frame')
local rx, ry = xf:rotate(hx, hy, 0)
T.near(D.angleDiff(D.state.hOff, D.angleDiff(D.headingOf(rx, ry), 37)), 0, 1e-6, 'heading offset = the rebase turn')
T.check(math.abs(D.state.hOff) > 0.01 and math.abs(D.state.hOff) < 0.2, 'a ~0.1 deg turn for ~12 km')
for _ = 1, 10 do frame() end
T.check(not D.state.done, 'no jump detected after a rebase')
T.near(D.angleDiff(veh.heading, (37 + D.state.hOff) % 360), 0, 1e-6, 'the car keeps the rotated heading')
pt = D.pollTable()
T.check(pt.frames.flags[1] == D.FLAG_REBASE + D.FLAG_SKIP + D.FLAG_HARNESS, 'rebase frame flagged and skipped')
T.check(pt.drive.rebases == 1, 'rebase counted')

-- reset events and pause
proxies[1].sinks[1]:onVehicleResetted(7)
proxies[1].sinks[1]:onVehicleResetted(99)
local px = D.state.x
frame(0)
T.check(D.state.x == px, 'paused: nothing moves')
pt = D.pollTable()
T.check(pt.drive.resets == 1, 'player resets counted (others ignored)')
T.check(pt.frames.flags[#pt.frames.flags] % (2 * D.FLAG_SKIP) >= D.FLAG_SKIP, 'paused frame flagged skip')

-- fall: off the static collision the last ground height is kept
D.state.x = MISS_X + 10
veh.pos = vec3(D.state.x, D.state.y, veh.pos.z)
local gz = D.state.ground
frame()
T.check(D.state.falls == 1 and D.state.ground == gz, 'a ray miss is a fall; the last ground kept')
pt = D.pollTable()
T.check(math.floor(pt.frames.flags[1] / D.FLAG_FALL) % 2 == 1, 'fall flag')
D.state.x = 1000
veh.pos = vec3(D.state.x, D.state.y, veh.pos.z)
frame()

-- end of the profile: km reached, parked
for _ = 1, 3000 do
  if D.state.done then break end
  frame()
end
T.check(D.state.done and D.state.reason == 'end' and D.state.s >= 1000, 'ends at km')
T.check(veh.vel:length() == 0, 'parked at the end')
local n = #veh.calls
frame()
T.check(#veh.calls == n, 'nothing placed after the end')

-- uninstall: proxy gone, wrapper restored, parked, inputs zeroed
local cmds = #veh.cmds
T.check(D.uninstall() == 'uninstalled' and proxies[1].destroyed, 'proxy destroyed')
T.check(rebaseMod.moveVehicles == origMove, 'moveVehicles restored')
T.check(#veh.cmds == cmds + 1 and veh.vel:length() == 0, 'inputs zeroed, at rest')
T.check(not D.installed, 'not installed')

------------------------------------------------------------------------------------------
-- jump (not a rebase): the run stops
D = load()
D.install({ points = { { t = 0, speed = 30, heading = 0 }, { t = 100, speed = 30 } } })
for _ = 1, 10 do frame() end
veh.pos = vec3(veh.pos.x + 3000, veh.pos.y, veh.pos.z)
frame()
T.check(D.state.done and D.state.reason == 'jump', 'a 3 km jump outside a rebase stops the run')
-- loading the chunk again uninstalls the driver a dead harness left
local p2 = proxies[#proxies]
D = load()
T.check(p2.destroyed and rebaseMod.moveVehicles == origMove, 'reload uninstalls the previous driver')

-- flight: start ground + alt; a time profile ends at its last point
D.install({ points = { { t = 0, speed = 100, heading = 90, alt = 300 }, { t = 1, speed = 100, alt = 400 } } })
local base = D.state.base
for _ = 1, 30 do frame() end
T.near(veh.pos.z, base + 300 + 100 * 29 / 60, 1e-6, 'flight: start ground + alt (profile at the frame start)')
for _ = 1, 40 do frame() end
T.check(D.state.done and D.state.reason == 'end', 'time profile ends at its last t')
D.uninstall()

-- a v1.3.1 mod (no exactState): key lookup of the current anchor's tiles
D = load()
local tiles = {}
package.loaded['beamngEarth/farfield'] = { _tile = function(k) return tiles[k] end }
D.install({ points = { { s = 0, speed = 0, heading = 0 } }, km = 1 })
local px0, py0 = veh.pos.x, veh.pos.y
tiles['-1_' .. math.floor(px0 / 128) .. '_' .. math.floor(py0 / 128)] = { shown = true }
frame()
tiles['-2_' .. math.floor(px0 / 64) .. '_' .. math.floor(py0 / 64)] = { shown = true }
frame()
pt = D.pollTable()
T.check(pt.frames.under[1] == -1 and pt.frames.flags[1] == D.FLAG_MISS + D.FLAG_HARNESS,
  'fallback: L-1 under the car is a miss (first frame after install: a harness frame)')
T.check(pt.frames.under[2] == -2 and pt.frames.flags[2] == 0, 'fallback: L-2 is exact')
D.uninstall()
package.loaded['beamngEarth/farfield'] = farfieldStub

-- errors
D = load()
player = nil
T.check(D.install({ points = { { s = 0, speed = 1 } } }):match('no player vehicle') ~= nil, 'no vehicle')
player = veh
local nep = newExtensionProxy
newExtensionProxy = nil
T.check(D.install({ points = { { s = 0, speed = 1 } } }):match('newExtensionProxy') ~= nil, 'no proxy API')
newExtensionProxy = nep
T.check(D.install({ points = {} }):match('no points') ~= nil, 'bad profile')
T.check(not D.installed, 'nothing installed after errors')

-- a failing install step undoes the steps before it (wrapper, proxy); nothing is left installed
newExtensionProxy = function(parent, prefix)
  local p = nep(parent, prefix)
  function p:submitEventSinks() error('sink rejected') end
  return p
end
veh.vel = vec3(5, 0, 0)
res = D.install({ points = { { s = 0, speed = 1 } }, km = 1 })
newExtensionProxy = nep
T.check(res:match('install: .*sink rejected') ~= nil, 'install error reported: ' .. res)
T.check(not D.installed and D.proxy == nil and proxies[#proxies].destroyed, 'failed install: proxy destroyed')
T.check(rebaseMod.moveVehicles == origMove, 'failed install: moveVehicles restored')
T.check(veh.vel:length() == 0, 'failed install: car at rest')

-- an error inside a frame stops the run and parks the car (the harness uninstalls later)
D = load()
D.install({ points = { { s = 0, speed = 30, heading = 0 } }, km = 5 })
for _ = 1, 5 do frame() end
local gpv = veh.getVelocity
veh.getVelocity = function() error('boom') end
frame()
veh.getVelocity = gpv
T.check(D.state.done and D.state.reason == 'error' and D.state.error:match('boom') ~= nil, 'frame error stops the run')
T.check(veh.vel:length() == 0 and veh.cmds[#veh.cmds] == D.ZERO_INPUTS, 'frame error: parked, inputs zero')
T.check(rebaseMod.moveVehicles == origMove, 'frame error: moveVehicles restored')
local nc = #veh.calls
frame()
T.check(#veh.calls == nc, 'nothing placed after the error')
D.uninstall()

-- watchdog: polls keep the run alive; without one for WATCHDOG_MS the driver stops and parks
D = load()
D.install({ points = { { s = 0, speed = 20, heading = 0 } }, km = 50 })
local frames = math.ceil(D.WATCHDOG_MS / (1000 * DT)) + 10
for i = 1, frames do
  frame()
  if i % 60 == 0 then D.pollTable() end
end
T.check(not D.state.done, 'polled once a second: still driving after WATCHDOG_MS')
for _ = 1, frames do frame() end
T.check(D.state.done and D.state.reason == 'watchdog', 'no poll for WATCHDOG_MS: stopped')
T.check(veh.vel:length() == 0 and rebaseMod.moveVehicles == origMove, 'watchdog: parked, moveVehicles restored')
T.check(D.installed and not proxies[#proxies].destroyed, 'watchdog: the proxy stays (inert) until uninstall')
-- a new harness loads the chunk again: the stopped driver is uninstalled
local pw = proxies[#proxies]
D = load()
T.check(pw.destroyed, 'reload uninstalls a stopped driver')
-- and a half-installed one (installed flag lost, wrapper and proxy still there)
D.install({ points = { { s = 0, speed = 1 } }, km = 1 })
local ph = proxies[#proxies]
D.installed = false
D = load()
T.check(ph.destroyed and rebaseMod.moveVehicles == origMove, 'reload uninstalls a half-installed driver')


-- a physics blow-up stops the run (in-game 2026-10-10: the user's Hopper went unstable at 67 m/s,
-- its velocity read inf and the engine removed it; the old driver kept going)
T.check(D.sane(vec3(30, 0, 0), 30) and D.sane(vec3(0, 0, 18000), 100), 'sane: profile speed, flight climb')
T.check(not D.sane(vec3(0 / 0, 0, 0), 30), 'sane: NaN')
T.check(not D.sane(vec3(math.huge, 0, 0), 30) and not D.sane(vec3(0, 0, -math.huge), 30), 'sane: inf')
T.check(not D.sane(vec3(130, 0, 0), 30), 'sane: far above the profile speed')
for _, bad in ipairs({ vec3(0 / 0, 0, 0), vec3(-math.huge, 0, 0), vec3(500, 0, 0) }) do
  D = load()
  D.install({ points = { { s = 0, speed = 30, heading = 0 } }, km = 5 })
  for _ = 1, 3 do frame() end
  local gv = veh.getVelocity
  veh.getVelocity = function() return bad end
  frame()
  veh.getVelocity = gv
  T.check(D.state.done and D.state.reason == 'unstable' and D.state.error:match('unstable') ~= nil,
    'unstable vehicle stops the run: ' .. tostring(bad.x))
  T.check(rebaseMod.moveVehicles == origMove, 'unstable: moveVehicles restored')
  local n = #veh.calls
  frame()
  T.check(#veh.calls == n, 'unstable: nothing placed afterwards')
  D.uninstall()
end

T.done('test_drive_driver')
