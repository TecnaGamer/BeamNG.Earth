-- luajit mod/tests/test_teleport_nocar.lua
-- A teleport with no car (the user, 2026-10-28: "teleport to locations without being in a vehicle"): core.lua against
-- stubbed engine globals and a scripted server, as test_core_stubbed.lua, with no player vehicle. The free camera goes
-- over the target in the commit frame and over its ground once placed (placePlayer: the ground ray, safeSpot), and is
-- the player there even while the game still reports the old spot (heldCamera); poses follow it. With a car and the
-- free camera on, the camera goes along behind the car; with a car and its own camera, the camera is left alone.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
require('beamngEarth/farfield').SPARE_MAX = 0
local framing = require('beamngEarth/framing')
local calls = S.calls

local wire, wireN = {}, 0
function jsonEncode(msg) wireN = wireN + 1; wire['k' .. wireN] = msg; return 'k' .. wireN end
function jsonDecode(s) return wire[s] end

------------------------------------------------------------------------------------------
-- no car; the free camera (core/camera.lua getPositionXYZ / getForwardXYZ / setPosRot). camLag: a setPosRot shows in
-- getPositionXYZ only that many frames later (the game may report the camera's move late)

local veh = nil
function getPlayerVehicle() return veh end
function getAllVehicles() return veh and { veh } or {} end
spawn = { safeTeleport = function(v, pos) S.count('safeTeleport'); v.pos = vec3(pos.x, pos.y, pos.z) end }
local freeCam = true
commands = { isFreeCamera = function() return freeCam end }
core_celestial = { setLocation = function() end }
local cam = { pos = vec3(40, -30, 1500), fx = 1, fy = 0, fz = 0, lag = 0, queue = {}, puts = {} }
local frameNo = 0
core_camera = {
  getPositionXYZ = function() return cam.pos.x, cam.pos.y, cam.pos.z end,
  getForwardXYZ = function() return cam.fx, cam.fy, cam.fz end,
  getFovDeg = function() return 65 end,
  setPosRot = function(_, x, y, z, qx, qy, qz, qw)
    S.count('camSetPosRot')
    cam.puts[#cam.puts + 1] = { frame = frameNo, x = x, y = y, z = z }
    cam.queue[#cam.queue + 1] = { due = frameNo + cam.lag, pos = vec3(x, y, z) }
  end,
}
local function cameraStep()
  local keep = {}
  for _, q in ipairs(cam.queue) do
    if q.due <= frameNo then cam.pos = q.pos else keep[#keep + 1] = q end
  end
  cam.queue = keep
end
-- the static collision: ground at groundAt(x, y, fromZ) (nil: a hole), a ray down from `from` meeting it (fromZ: a
-- tunnel's floor under the hill for a ray from inside the tube)
local groundAt = function() return nil end
function castRayStatic(from, dirv, len)
  local g = groundAt(from.x, from.y, from.z)
  if not g or g > from.z or from.z - g > len then return nil end
  return from.z - g
end
ui_imgui = setmetatable({ Begin = function() return true end, BoolPtr = function(v) return { [0] = v } end,
  DoublePtr = function(v) return { [0] = v } end, FloatPtr = function(v) return { [0] = v } end,
  Button = function() return false end }, { __index = function() return function() end end })
package.loaded['core/cameraUpdate'] = { onPreRender = function() return { main = { camData = { pos = vec3(), prevPos = vec3(),
  vehPos = vec3(), prevVehPos = vec3(), vel = vec3() }, finalCameraData = { pos = vec3() }, lastValidData = { pos = vec3() },
  vehicleCamerasCache = {}, globalCamerasCache = {} } } end }

------------------------------------------------------------------------------------------
-- fake luasocket + scripted server

local server = { inbox = framing.newReassembler(), sent = {}, toClient = '' }
local function serverSend(msg) server.toClient = server.toClient .. framing.encode(jsonEncode(msg)) end
local function received(typ)
  local r = {}
  for _, m in ipairs(server.sent) do if m.type == typ then r[#r + 1] = m end end
  return r
end
local function last(typ) local r = received(typ); return r[#r] end
math.randomseed(11)
package.preload['socket'] = function()
  local socket = {}
  function socket.gettime() return os.clock() end
  function socket.tcp()
    local c = {}
    function c:settimeout() end
    function c:connect() return 1 end
    function c:setoption() end
    function c:getpeername() return '127.0.0.1', 47613 end
    function c:close() end
    function c:send(data, i)
      server.inbox:feed(data:sub(i or 1))
      while true do
        local p = server.inbox:pop()
        if not p then break end
        server.sent[#server.sent + 1] = jsonDecode(p)
      end
      return #data
    end
    function c:receive()
      local k = math.min(#server.toClient, math.random(0, 40))
      local chunk = server.toClient:sub(1, k)
      server.toClient = server.toClient:sub(k + 1)
      return nil, 'timeout', chunk
    end
    return c
  end
  return socket
end

------------------------------------------------------------------------------------------

local core = require('beamngEarth/core')
require('beamngEarth/statusbar').enabled = false
local wallNow = 0
core._governor().clock = function() return wallNow end
local function frame()
  frameNo = frameNo + 1
  wallNow = wallNow + 16
  core.onUpdate(0.05, 0.05)
  if veh then veh:physics() end
  cameraStep()
  core.onPreRender(0.05, 0.05)
end
local function frames(n) for _ = 1, n do frame() end end
local function pumpUntil(cond, max)
  for _ = 1, max or 400 do
    if cond() then return true end
    frame()
  end
  return cond()
end
local SQ = require('beamngEarth/follow').SQUARE_SIZE
local function terrainMsg(cx, cy, path, zBase)
  return { cx = cx, cy = cy, heightmap = path, size = 1024, squareSize = SQ, zBase = zBase or 1100, maxHeight = 3500 }
end
local function near(a, b, eps) return math.abs(a - b) <= (eps or 1e-6) end
local nAnchor = 0
-- a teleport's anchor ready: the commit; returns the request
local function answer(groundZ, extra)
  local req = last('prepare_anchor')
  nAnchor = nAnchor + 1
  local msg = { type = 'anchor_ready', requestId = req.requestId, anchorId = 10 + nAnchor, lat = req.lat, lon = req.lon,
    groundZ = groundZ, px = req.px, py = req.py, terrain = terrainMsg(req.cx or 0, req.cy or 0, '/temp/beamngEarth/s1/hm_' .. nAnchor .. '.png', 1100) }
  for k, v in pairs(extra or {}) do msg[k] = v end
  serverSend(msg)
  return req
end

core.onExtensionLoaded()
local st = core._state()       -- (the level's state: made fresh on load)
frames(3)
T.check(#received('hello') == 1, 'connected (hello sent)')
serverSend({ type = 'hello', protocol = 4, format = 3, serverVersion = '0.4.0', sessionId = 's1', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'terrain' })
T.check(pumpUntil(function() return #received('prepare_anchor') == 1 end), 'the level start\'s teleport asked with no car')

-- 1. the level start, no car, the camera reporting its moves at once: over the target in the commit frame, then 3 m
-- over the ground there (a snapped teleport: the server's height, as for a car; the ray: scenario 1b)
T.check(core.SNAP_TELEPORTS == true, 'teleports snapped (the default)')
groundAt = function() return 1176 end
local req = answer(1180)
local commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit with no car')
local put = cam.puts[#cam.puts]
T.check(put and near(put.x, req.px or 0) and near(put.y, req.py or 0) and near(put.z, 1181 + 5),
  string.format('commit frame: the camera over the target (%.1f, %.1f, %.1f)', put and put.x or 0 / 0, put and put.y or 0 / 0, put and put.z or 0 / 0))
T.check(put and put.frame == frameNo, 'moved in the commit frame itself')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
put = cam.puts[#cam.puts]
local px, py = req.px or 0, req.py or 0
T.check(st.placeSnapped == true and (px ~= 0 or py ~= 0), 'the level start: snapped, the spot off the anchor')
T.check(near(put.x, px) and near(put.y, py) and near(put.z, 1183),
  string.format('placed: 3 m over the ground at the target (%.1f, want 1183)', put.z))
frames(2)
T.check(st.player and st.player.camera and near(st.player.x, px) and near(st.player.y, py), 'the player is the camera, at the target')
T.check(st.camAt == nil, 'the camera read back there: no longer held')
T.check((calls.safeTeleport or 0) == 0, 'no car set down')
T.check(pumpUntil(function() local p = last('pose'); return p and p.anchorId == 10 + nAnchor end, 40)
  and near(last('pose').x, px) and near(last('pose').y, py), 'poses from the target (the server builds round it)')

-- 1b. onto another player's anchor (BeamMP, mpsession.adoptAnchor): not snapped, so the camera goes over the collision
-- the ray finds (graded ground 4 m under the server's height), as a car would
local mps = require('beamngEarth/mpsession')
local adopt = mps.adoptAnchor
mps.adoptAnchor = function(lat, lon) return lat + 0.001, lon end
core.teleport(36.7, -118.2)
groundAt = function() return 1176 end
req = answer(1180)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (an adopted anchor)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
put = cam.puts[#cam.puts]
T.check(st.placeSnapped == false and near(put.x, req.px) and near(put.y, req.py) and req.py < -100,
  'adopted anchor: the target off its origin, not snapped')
T.check(near(put.z, 1176 + 1 + core.CAM_ABOVE_M), string.format('adopted anchor: 3 m over the ground the ray found (%.1f)', put.z))
mps.adoptAnchor = adopt

-- 2. a map teleport with the camera's move reported 3 frames late: the target is the player meanwhile (not the old
-- spot, anywhere in the new frame), poses wait for the placement as with a car, then the camera there
local places = require('beamngEarth/places')
beamngEarth_core = core
T.check(places.cannotGo() == nil, 'Go is on with no car (connected, in the level)')
cam.lag = 3
cam.pos = vec3(cam.pos.x + 500, cam.pos.y, cam.pos.z + 900)      -- flown away and up
local okGo = places.goTo(47.37, 8.54, 'Zurich')
T.check(okGo, 'places.goTo with no car: sent')
T.check(places.cannotGo() == 'Loading the ground: wait a moment', 'a teleport under way: Go greyed until its ground is in')
groundAt = function() return 400 end
req = answer(405)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit')
local tx, ty = req.px or 0, req.py or 0
frame()
T.check(not near(cam.pos.x, tx) or not near(cam.pos.z, 406 + 5), 'the game still reports the old spot (lag)')
T.check(st.player and near(st.player.x, tx) and near(st.player.y, ty) and st.player.z > 400,
  string.format('held: the player is the target (%.1f, %.1f, %.1f), not the old spot', st.player and st.player.x or 0 / 0,
    st.player and st.player.y or 0 / 0, st.player and st.player.z or 0 / 0))
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
T.check(pumpUntil(function() return st.camAt == nil end, 10), 'the camera read back there: let go')
T.check(places.cannotGo() == nil, 'on the ground there: Go on again')
T.check(near(cam.pos.x, tx) and near(cam.pos.y, ty), 'the camera at the target')
T.check(st.player and near(st.player.x, tx) and near(st.player.z, cam.pos.z), 'the player follows the camera again')
local flown = vec3(cam.pos.x + 30, cam.pos.y, cam.pos.z + 40)
cam.pos = flown
frames(2)
T.check(near(st.player.x, flown.x) and near(st.player.z, flown.z), 'flown on afterwards: the player is where the camera is')
cam.lag = 0

-- 2b. Go never waits for good: a teleport whose ground failed (anchor_error, asked again later) lets another place
-- be asked; so does one the server has not answered in GO_WAIT_MAX_S
core.teleport(30.0, 30.0)
T.check(places.cannotGo() ~= nil, 'asked: greyed')
serverSend({ type = 'anchor_error', requestId = last('prepare_anchor').requestId, error = 'test: elevation download failed' })
T.check(pumpUntil(function() return st.teleportRetry ~= nil end, 20), 'its ground failed: to be asked again')
T.check(places.cannotGo() == nil, 'its ground failed: Go on (another place may be asked)')
core.teleport(31.0, 31.0)
T.check(places.cannotGo() ~= nil and st.pending and st.pending.purpose == 'teleport', 'a new place asked: greyed')
st.pending.sentAt = st.clock - core.GO_WAIT_MAX_S - 1
T.check(places.cannotGo() == nil, 'not answered in GO_WAIT_MAX_S: Go on again')
answer(405)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'answered late: committed')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10) and places.cannotGo() == nil, 'placed: Go on')

-- 3. a camera that never reads back (a setPosRot the game ignores): let go after CAM_HOLD_FRAMES, the camera's own
-- spot again (as before this change)
local ignore = core_camera.setPosRot
core_camera.setPosRot = function(...) S.count('camSetPosRot') end
core.teleport(46.0, 7.0)
groundAt = function() return 900 end
req = answer(905)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (camera move ignored)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
local heldPoses, wrongPoses, heldFrames = 0, 0, 0
for _ = 1, core.CAM_HOLD_FRAMES + 5 do
  local n0 = #server.sent
  frame()
  if st.camAt then
    heldFrames = heldFrames + 1
    if not (near(st.player.x, req.px or 0) and near(st.player.y, req.py or 0)) then wrongPoses = wrongPoses + 100 end
    for i = n0 + 1, #server.sent do
      local m = server.sent[i]
      if m.type == 'pose' and m.anchorId == 10 + nAnchor then
        heldPoses = heldPoses + 1
        if not (near(m.x, req.px or 0) and near(m.y, req.py or 0)) then wrongPoses = wrongPoses + 1 end
      end
    end
  end
end
T.check(heldFrames >= core.CAM_HOLD_FRAMES - 3, 'not read back: held at the target for CAM_HOLD_FRAMES (' .. heldFrames .. ')')
T.check(heldPoses >= 1 and wrongPoses == 0, 'poses while held: from the target (' .. heldPoses .. ', ' .. wrongPoses .. ' off)')
T.check(st.camAt == nil and near(st.player.x, cam.pos.x) and near(st.player.z, cam.pos.z), 'never read back: let go')

-- 3b. a rebase while the camera is held: the held target (the old frame) let go at its commit
core.teleport(46.1, 7.1)
answer(905)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (held again)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10) and st.camAt ~= nil, 'held')
local nPrep = #received('prepare_anchor')
T.check(core.rebaseTo(st.anchor.lat + 0.01, st.anchor.lon), 'a rebase asked while held')
T.check(pumpUntil(function() return #received('prepare_anchor') == nPrep + 1 end, 5), 'its anchor asked')
answer(905)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end, 60), 'the rebase committed')
T.check(st.camAt == nil, 'a rebase lets the held target go (it was in the old frame)')
core_camera.setPosRot = ignore

-- 4. a hole at the target (a tunnel's portal before its road tile): the camera onto the nearest ground (safeSpot)
core.teleport(45.19, 5.72)
groundAt = function(x, y) local o = last('prepare_anchor'); local dx, dy = x - (o.px or 0), y - (o.py or 0)
  if dx * dx + dy * dy < 15 * 15 then return nil end return 300 end
req = answer(320)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (a hole)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
put = cam.puts[#cam.puts]
local d = math.sqrt((put.x - (req.px or 0)) ^ 2 + (put.y - (req.py or 0)) ^ 2)
T.check(d >= 15 and d <= 15 + core.SAFE_RING_M and near(put.z, 300 + 1 + core.CAM_ABOVE_M),
  string.format('a hole: the camera over the nearest ground (%.0f m away, z %.1f)', d, put.z))

-- 4b. a teleport into a tunnel (floorZ): set down over the hill first; once the tube's floor collides, down onto it
-- if the camera is still where it was put; a free camera flown away meanwhile is left alone (the user flew off)
local roadsMod = require('beamngEarth/roads')
local settled = roadsMod.settledFor
roadsMod.settledFor = function() return true end
local floorIn = false
local function tunnelGround(_, _, fromZ)
  if fromZ and fromZ < 1180 then return floorIn and 1100 or nil end      -- (inside the hill: the tube's floor)
  return 1180
end
for _, flyOff in ipairs({ false, true }) do
  floorIn = false
  groundAt = tunnelGround
  core.teleport(44.0 + (flyOff and 0.1 or 0), 6.0)
  answer(1180, { floorZ = 1100 })
  commits = #received('commit_anchor')
  T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (a tunnel)')
  T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
  T.check(near(cam.pos.z, 1183) and st.floorZ == 1100, 'a tunnel: first over the hill, the floor waited for')
  frames(3)
  if flyOff then cam.pos = vec3(cam.pos.x + 2000, cam.pos.y + 1500, 1483) end
  frames(3)
  local before = #cam.puts
  floorIn = true
  frames(3)
  T.check(st.floorZ == nil, 'the floor collides: done waiting')
  if flyOff then
    T.check(#cam.puts == before and near(cam.pos.z, 1483),
      'flown away meanwhile: the camera left where the player flew it')
  else
    T.check(#cam.puts == before + 1 and near(cam.pos.z, 1100 + 1 + core.CAM_ABOVE_M),
      string.format('still there: down onto the floor of the tube (%.1f)', cam.pos.z))
  end
end
roadsMod.settledFor = settled

-- 5. a car and the free camera on: the car set down at the target, the camera behind it (CAM_BEHIND_M, CAM_OVER_M)
veh = { id = 1, pos = vec3(0, 0, 500), vel = vec3(0, 0, 0) }
function veh:getID() return self.id end
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function veh:getVelocity() return self.vel end
function veh:getDirectionVector() return vec3(0, 1, 0) end
function veh:setClusterPosRelRot(_, x, y, z) self.pos = vec3(x, y, z) end
function veh:applyClusterVelocityScaleAdd() end
function veh:physics() end
function veh:queueLuaCommand() end
function veh:getSpawnWorldOOBB()
  return { getMatrix = function() return { toQuatF = function() return { x = 0, y = 0, z = 0, w = 1 } end } end }
end
frames(2)
T.check(st.player and not st.player.camera, 'a car: the player is the car')
local puts0, set0 = #cam.puts, calls.safeTeleport or 0
core.teleport(40.0, -105.0)
groundAt = function() return 1600 end
req = answer(1600)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (car, free camera)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
T.check((calls.safeTeleport or 0) == set0 + 1, 'the car set down')
put = cam.puts[#cam.puts]
T.check(#cam.puts == puts0 + 2 and near(put.x, veh.pos.x) and near(put.y, veh.pos.y - core.CAM_BEHIND_M)
  and near(put.z, veh.pos.z + core.CAM_OVER_M), string.format('free camera: behind the car (%.1f, %.1f, %.1f; car %.1f, %.1f, %.1f)',
    put.x, put.y, put.z, veh.pos.x, veh.pos.y, veh.pos.z))
T.check(st.camAt == nil, 'a car: no camera hold')
-- 5b. the ground rising behind the car (10 m up 6 m behind): the camera over it, not in the hill
core.teleport(40.1, -105.0)
groundAt = function(x, y) local o = last('prepare_anchor'); return (y < (o.py or 0) - 3) and 1610 or 1600 end
answer(1600)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (a slope behind)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
put = cam.puts[#cam.puts]
T.check(near(put.y, veh.pos.y - core.CAM_BEHIND_M) and near(put.z, 1610 + 1 + core.CAM_ABOVE_M),
  string.format('a slope behind the car: the camera over it (%.1f)', put.z))

-- 5c. a car and the free camera, into a tunnel: once the tube collides, the car down onto its floor and the camera
-- close behind it and low (CAM_TUBE_BEHIND_M, CAM_TUBE_OVER_M: in the tube, not in the rock beside or over it)
do
  local roadsMod = require('beamngEarth/roads')
  local settled = roadsMod.settledFor
  roadsMod.settledFor = function() return true end
  local floorIn = false
  groundAt = function(_, _, fromZ)
    if fromZ and fromZ < 1180 then return floorIn and 1100 or nil end
    return 1180
  end
  core.teleport(44.2, 6.0)
  answer(1180, { floorZ = 1100 })
  commits = #received('commit_anchor')
  T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (car, free camera, a tunnel)')
  T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed over the hill')
  frames(3)
  floorIn = true
  frames(3)
  put = cam.puts[#cam.puts]
  T.check(st.floorZ == nil and near(veh.pos.z, 1101), 'the car down onto the tube floor')
  T.check(near(put.z, 1100 + 1 + core.CAM_TUBE_OVER_M) and near(put.y, veh.pos.y - core.CAM_TUBE_BEHIND_M),
    string.format('the camera close behind it in the tube (%.1f m over the floor)', put.z - 1100))
  roadsMod.settledFor = settled
end

-- 6. a car and its own camera (not free): the camera is left to the game
freeCam = false
puts0 = #cam.puts
core.teleport(41.0, -106.0)
answer(1500)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (car camera)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
T.check(#cam.puts == puts0, 'the car\'s own camera: not moved')

-- 7. the car gone again (free camera): a teleport with no car moves the camera as in 1
veh, freeCam = nil, true
puts0 = #cam.puts
core.teleport(42.0, -107.0)
answer(1300)
commits = #received('commit_anchor')
T.check(pumpUntil(function() return #received('commit_anchor') == commits + 1 end), 'commit (no car again)')
T.check(pumpUntil(function() return st.placeFrames == nil end, 10), 'placed')
T.check(#cam.puts == puts0 + 2, 'no car again: the camera moved at the commit and the placement')

-- 8. the level start's terrain fails to import (commitTeleport: no terrain in the answer): nothing is on its way and
-- there is no ground, so Go must not stay greyed (another place may be asked)
core.onExtensionUnloaded()
core.onExtensionLoaded()
st = core._state()
frames(3)
serverSend({ type = 'hello', protocol = 4, format = 3, serverVersion = '0.4.0', sessionId = 's2', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'terrain' })
local nPrep = #received('prepare_anchor')
T.check(pumpUntil(function() return #received('prepare_anchor') == nPrep + 1 end), 'a new level: its start asked')
T.check(places.cannotGo() ~= nil, 'the start under way: greyed')
local rq = last('prepare_anchor')
serverSend({ type = 'anchor_ready', requestId = rq.requestId, anchorId = 99, lat = rq.lat, lon = rq.lon, groundZ = 1180 })
frames(10)
T.check(st.anchor == nil and st.pending == nil and st.teleportRetry == nil, 'its terrain failed: no ground, nothing on its way')
T.check(places.cannotGo() == nil, 'its terrain failed: Go on (not greyed for good)')

core.onExtensionUnloaded()
T.done('test_teleport_nocar')
