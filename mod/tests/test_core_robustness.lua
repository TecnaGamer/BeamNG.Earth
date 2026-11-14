-- luajit mod/tests/test_core_robustness.lua  (docs/ROADMAP.md "Robustness: no server, lost server, lost internet")
-- Drives beamngEarth/core.lua against stubbed engine globals, a vehicle whose physics integrates its
-- velocity, a flat ground ray and a fake luasocket whose server can be down:
--   1. no server at level start: "starting", then "not running" with the start command; the car on
--      the flat start block is held at its edge (warned first), nothing is sent;
--   2. the server comes: hello (sessions: none yet), the "connected" line, the auto-teleport; while
--      the server is healthy the guard never acts, even at the block's edge;
--   3. the server dies: "connection lost - reconnecting (attempt n)", the car is held at the edge;
--   4. a restarted server (a new session whose anchor ids start at 1 again): hello names the old
--      session, the resync keeps the old session's tiles under their own key (anchor 1 of both
--      sessions apart), pose.held never names the old session's anchors, pose.sessions names the old
--      folder until its last tile object is deleted;
--   5. a server that does not answer terrain requests (unanswered for STUCK_S, or a terrain_error):
--      the guard holds the car, the banner says why; an answer and its commit end it;
--   6. the server's offline note in the banner and on the settings page; hidden in the pause menu;
--   7. an error inside the guard turns it off once (logged), never every frame.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
-- (the v1.3.1 block, 1024² at 2 m: these checks are written for it; the 1 m default: test_lattice / test_follow)
require('beamngEarth/lattice').configure(1024, 2.0)
require('beamngEarth/follow').configure()
require('beamngEarth/rebase').configure(2048)
local framing = require('beamngEarth/framing')
local farfield = require('beamngEarth/farfield')
local banner = require('beamngEarth/banner')
local edgeguard = require('beamngEarth/edgeguard')
local calls = S.calls

local wire, wireN = {}, 0
function jsonEncode(msg) wireN = wireN + 1; wire['k' .. wireN] = msg; return 'k' .. wireN end
function jsonDecode(s) return wire[s] end

local errors = {}
log = function(level, tag, text) if level == 'E' then errors[#errors + 1] = tostring(text) end end

------------------------------------------------------------------------------------------
-- the vehicle: physics integrates the velocity every running frame

local DT = 0.05
local veh = { id = 1, pos = vec3(0, 0, 0.5), vel = vec3(0, 0, 0), sets = 0 }
function veh:getID() return self.id end
function veh:getPosition() return vec3(self.pos) end
function veh:getVelocity() return vec3(self.vel) end
function veh:getDirectionVector() return vec3(1, 0, 0) end
function veh:setClusterPosRelRot(node, x, y, z) self.pos = vec3(x, y, z) end
function veh:applyClusterVelocityScaleAdd(node, scale, ax, ay, az)
  self.sets = self.sets + 1
  self.vel = vec3(self.vel.x * scale + (ax or 0), self.vel.y * scale + (ay or 0), self.vel.z * scale + (az or 0))
end
function veh:setOriginalTransform() end
function veh:queueLuaCommand() end
function getPlayerVehicle() return veh end
function getAllVehicles() return { veh } end
spawn = { safeTeleport = function(v, pos) S.count('safeTeleport'); v.pos = vec3(pos.x, pos.y, 0.5); v.vel = vec3() end }
function quatFromDir() return quat(0, 0, 0, 1) end

-- the ground: z = 0 over the TerrainBlock's collidable area (whatever block is loaded), nothing elsewhere
local lattice = require('beamngEarth/lattice')
local groundBox = nil
function castRayStatic(from, d, len)
  local b = groundBox
  if not b or from.x < b[1] or from.x > b[3] or from.y < b[2] or from.y > b[4] or from.z < 0 then return len + 1 end
  return from.z <= len and from.z or len + 1
end
local function setGround(cx, cy) groundBox = { lattice.groundBox(cx, cy) } end

-- imgui: the banner is the text drawn near the top of the screen
local bannerTexts = {}
ui_imgui = setmetatable({
  Begin = function() return true end,
  BoolPtr = function(v) return { [0] = v } end,
  DoublePtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end,
  Button = function() return false end,
  GetMainViewport = function() return { Pos = { x = 0, y = 0 }, Size = { x = 1920, y = 1080 } } end,
  GetFontSize = function() return 16 end,
  CalcTextSize = function(t) return { x = #t * 8, y = 16 } end,
  ImVec2 = function(x, y) return { x = x, y = y } end,
  ImVec4 = function(r, g, b, a) return { r, g, b, a } end,
  GetColorU322 = function() return 0 end,
  GetBackgroundDrawList1 = function() return 'bg' end,
  ImDrawList_AddText1 = function(dl, pos, col, text) if pos.y < 100 then bannerTexts[#bannerTexts + 1] = text end end,
}, { __index = function() return function() end end })

------------------------------------------------------------------------------------------
-- fake luasocket: server.down refuses connections; failErr closes the open one

local server = { inbox = framing.newReassembler(), sent = {}, toClient = '', down = true, connects = 0 }
local function serverSend(msg) server.toClient = server.toClient .. framing.encode(jsonEncode(msg)) end
local function received(typ)
  local r = {}
  for _, m in ipairs(server.sent) do if m.type == typ then r[#r + 1] = m end end
  return r
end
local function last(typ) local r = received(typ); return r[#r] end
package.preload['socket'] = function()
  return { tcp = function()
    local c = {}
    function c:settimeout() end
    function c:connect()
      if server.down then return nil, 'connection refused' end
      server.connects = server.connects + 1
      return 1
    end
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
      local chunk = server.toClient
      server.toClient = ''
      if server.failErr then
        local e = server.failErr
        server.failErr = nil
        return nil, e, chunk
      end
      return nil, 'timeout', chunk
    end
    return c
  end }
end

local core = require('beamngEarth/core')
require('beamngEarth/statusbar').enabled = false   -- its own test: test_statusbar.lua (these count text draws)
local wallNow = 0
core._governor().clock = function() return wallNow end
local st = core._state
-- the game's pause (simTimeAuthority): the edge guard and the drop hold pause the game; physics stands still then
simTimeAuthority = { paused = false }
function simTimeAuthority.pause(on) simTimeAuthority.paused = on and true or false end
function simTimeAuthority.getPause() return simTimeAuthority.paused end
local function frame(paused)
  paused = paused or simTimeAuthority.paused
  wallNow = wallNow + 16
  bannerTexts = {}
  core.onUpdate(DT, paused and 0 or DT)
  if not paused then veh.pos = veh.pos + vec3(veh.vel.x * DT, veh.vel.y * DT, 0) end
  core.onPreRender(DT, paused and 0 or DT)
end
local function frames(n) for _ = 1, n do frame() end end
local function pumpUntil(cond, max)
  for _ = 1, max or 400 do
    if cond() then return true end
    frame()
  end
  return cond()
end
local function shown() return bannerTexts[1] end
local function has(list, v) for _, x in ipairs(list or {}) do if x == v then return true end end return false end

------------------------------------------------------------------------------------------
-- 1. no server at level start

core.onExtensionLoaded() -- our level: the flat block, the world active
setGround(0, 0)
frames(3)
T.check(banner.current().id == 'starting' and shown() and shown():find('looking for the BeamNG.Earth server', 1, true),
  'just started: "looking for the BeamNG.Earth server"')
T.check(#server.sent == 0, 'no server: nothing sent')
frames(120) -- 6 s
T.check(core.banner().id == 'noserver' and shown():find('python3 server/earthd.py', 1, true),
  'no server after a few seconds: "not running" with the start command')
T.check(core.banner().text == shown(), 'the settings page line is the banner text')
T.check(calls.safeTeleport == 1, 'the car is on the flat start block')
-- drive east towards the flat block's edge (1024: [-1024, 1022])
veh.pos, veh.vel = vec3(600, 0, 0.5), vec3(20, 0, 0)
local warned, held = false, false
for _ = 1, 60 * 20 do
  frame()
  if st().guard.mode == 'warn' and shown():find('Near the edge', 1, true) then warned = true end
  if st().guard.mode == 'hold' and shown():find('Paused at the edge', 1, true) then held = true end
end
T.check(warned and held, 'no server: warned, then held at the edge (banner second line)')
T.check(veh.pos.x <= 1022 - edgeguard.STOP_M + 0.5 and simTimeAuthority.paused,
  string.format('the game paused with the car inside the flat block (x %.1f)', veh.pos.x))
T.check(#server.sent == 0, 'still nothing sent')

------------------------------------------------------------------------------------------
-- 2. the server comes

veh.pos, veh.vel = vec3(0, 0, 0.5), vec3(0, 0, 0) -- back from the flat block's edge (held there until an anchor commits)
server.down = false
T.check(pumpUntil(function() return #received('hello') == 1 end, 60), 'connected: hello sent')
local h = received('hello')[1]
T.check(type(h.sessions) == 'table' and #h.sessions == 0, 'hello.sessions: none yet (no server files in use)')
T.check(banner.current().id == 'starting', 'connected, waiting for the server\'s hello')
serverSend({ type = 'hello', protocol = 4, format = 4, serverVersion = '0.4.0', sessionId = 's1', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'mesh' })
T.check(pumpUntil(function() return #received('prepare_anchor') == 1 end, 10), 'auto-teleport requested')
T.check(core.banner().id == 'recovered' and shown() == 'BeamNG.Earth server connected', 'the server came: "BeamNG.Earth server connected"')
local req = received('prepare_anchor')[1]
-- the start block fails once (#286: a damaged elevation download): the same teleport is asked again, not given up
serverSend({ type = 'anchor_error', requestId = req.requestId, error = 'DemUnavailable: undecodable tile' })
frames(20) -- 1 s
T.check(#received('prepare_anchor') == 1, 'a failed start block: not re-asked at once')
T.check(pumpUntil(function() return #received('prepare_anchor') == 2 end, 60 * 8), 'a failed start block is asked again')
local again = received('prepare_anchor')[2]
T.check(again.lat == req.lat and again.lon == req.lon and again.cx == req.cx and again.cy == req.cy,
  'the retry: the same place')
req = again
-- a healthy server, but the first anchor not built yet: terrain-follow cannot move the flat start
-- block at all (it follows anchors only), so the car on it is still held at its edge
veh.pos, veh.vel = vec3(950, 0, 0.5), vec3(20, 0, 0)
for _ = 1, 60 * 5 do frame() end
T.check(veh.pos.x <= 1022 - edgeguard.STOP_M + 0.5 and st().guard.mode == 'hold',
  string.format('server up, no anchor yet: held on the flat block (x %.1f, guard %s)', veh.pos.x, st().guard.mode))
veh.pos, veh.vel = vec3(0, 0, 0.5), vec3(0, 0, 0)
serverSend({ type = 'anchor_ready', requestId = req.requestId, anchorId = 1, lat = req.lat, lon = req.lon, groundZ = 0,
  terrain = { cx = 0, cy = 0, heightmap = '/temp/beamngEarth/s1/hm_1_0_0.png', size = 1024, squareSize = 2,
    zBase = 0, maxHeight = 1024, q = 1 / 64 } })
T.check(pumpUntil(function() return #received('commit_anchor') == 1 end, 10), 'teleport committed')
frames(5)
-- far tiles of session s1 (one with an imagery material)
local keys = {}
for i = 1, 4 do
  local k = '0_' .. i .. '_0'
  serverSend({ type = 'far_tile', anchorId = 1, key = k, level = 0, shape = '/temp/beamngEarth/s1/far_1_' .. k .. '.cdae',
    pos = { (i - 1) * 256 - 512, -128, 0 }, err = 0.1, state = 'group',
    material = i == 1 and 'bngEarthSat_1_0_1_0' or nil, texture = i == 1 and '/temp/beamngEarth/s1/far_1_0_1_0.dds' or nil,
    detail = i == 1 or nil })
  keys[i] = k
end
serverSend({ type = 'far_swap', anchorId = 1, id = 1, add = keys, remove = {}, dIn = 0, dOut = 0 })
serverSend({ type = 'far_done', anchorId = 1, count = 4 })
T.check(pumpUntil(function() return farfield.stats().live == 4 end, 100), 's1 tiles shown')
local nPose = #received('pose')
T.check(pumpUntil(function() return #received('pose') > nPose end, 30), 'poses')
T.check(has(last('pose').sessions, 's1') and #last('pose').sessions == 1, 'pose.sessions: the s1 folder')
T.check(farfield.session() == 's1', 'the far field knows its session')
for _ = 1, 100 do frame() end
T.check(core.banner().id == 'ok' and not shown(), 'all is well: the banner is hidden')
-- healthy: the guard never acts, even driving at the edge (terrain-follow's job)
veh.pos, veh.vel = vec3(990, 0, 0.5), vec3(20, 0, 0)
local sets = veh.sets
frames(10)
T.check(st().guard.mode ~= 'hold' and not st().guard.pausedByUs and veh.sets == sets and veh.vel.x == 20,
  'healthy server: no pause short of the edge, no call (the block follows)')
veh.pos, veh.vel = vec3(0, 0, 0.5), vec3(0, 0, 0)
-- answer the terrain request that drive caused, so the server is healthy again
local pt = last('prepare_terrain')
serverSend({ type = 'terrain_ready', requestId = pt.requestId, anchorId = 1, cx = pt.cx, cy = pt.cy,
  heightmap = '/temp/beamngEarth/s1/hm_1_x.png', size = 1024, squareSize = 2, zBase = 0, maxHeight = 1024, q = 1 / 64 })
frames(5)
T.check(st().terrainPending == nil or st().terrainPending.ready, 'terrain answered')

------------------------------------------------------------------------------------------
-- 3. the server dies

server.failErr, server.down = 'closed', true
frames(2)
T.check(not st().sessionId and core.banner().id == 'lost' and shown():find('reconnecting (attempt', 1, true),
  'connection lost: "reconnecting (attempt n)"')
frames(130) -- 6.5 s: a retry every 2 s
T.check(shown():find('(attempt 3)', 1, true) ~= nil, 'the attempt number counts up: ' .. tostring(shown()))
setGround(0, 0)
local b = st().block
veh.pos, veh.vel = vec3(b.cx + 400, b.cy, 0.5), vec3(25, 0, 0)
local nSent = #server.sent
for _ = 1, 60 * 20 do frame() end
local x1 = select(3, lattice.groundBox(b.cx, b.cy))
T.check(veh.pos.x <= x1 - edgeguard.STOP_M + 0.5 and simTimeAuthority.paused and st().guard.mode == 'hold',
  string.format('server gone: paused with the car %.1f m inside the edge', x1 - veh.pos.x))
T.check(shown():find('Paused at the edge', 1, true) and shown():find('Lost the BeamNG.Earth server', 1, true), 'banner: lost + held')
T.check(#server.sent == nSent, 'nothing sent while disconnected')

------------------------------------------------------------------------------------------
-- 4. a restarted server: a new session, anchor ids from 1 again

server.down = false
T.check(pumpUntil(function() return #received('hello') == 2 end, 60), 'reconnected: hello')
T.check(has(last('hello').sessions, 's1'), 'hello.sessions names the old session (its tiles are on screen)')
local nA = #received('prepare_anchor')
serverSend({ type = 'hello', protocol = 4, format = 4, serverVersion = '0.4.0', sessionId = 's2', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'mesh' })
T.check(pumpUntil(function() return #received('prepare_anchor') == nA + 1 end, 10), 'resync requested')
local rs = last('prepare_anchor')
T.check(rs.lat == req.lat and rs.lon == req.lon, 'resync at the same origin')
-- Until the resync commits, our anchor id is s1's: s2 numbers its own anchors from 1 too (its resync
-- anchor is 1), so a pose / prepare_terrain / commit_terrain naming anchor 1 would make s2 switch to
-- its anchor 1 and stream tiles the far field files under s1. Nothing names it, and the block cannot
-- follow meanwhile: the guard keeps holding the car (it was at rest at the wall; now it pushes on).
nPose = #received('pose')
local nPt0, nCt0 = #received('prepare_terrain'), #received('commit_terrain')
veh.vel = vec3(25, 0, 0)
for _ = 1, 60 * 4 do frame() end
T.check(#received('pose') == nPose and #received('prepare_terrain') == nPt0 and #received('commit_terrain') == nCt0,
  string.format('before the resync commits: no pose / prepare_terrain / commit_terrain with an s1 anchor id (%d %d %d)',
    #received('pose') - nPose, #received('prepare_terrain') - nPt0, #received('commit_terrain') - nCt0))
T.check(st().guard.mode == 'hold' and veh.pos.x <= x1 - edgeguard.STOP_M + 0.5,
  string.format('the server is back but the block cannot follow until the resync commits: still held (x %.1f, %s)',
    veh.pos.x, st().guard.mode))
-- the restarted server cannot build the resync's terrain (offline, never visited): asked again
serverSend({ type = 'anchor_error', requestId = rs.requestId, error = 'DemUnavailable' })
T.check(pumpUntil(function() return #received('prepare_anchor') == nA + 2 end, 60 * 8), 'a failed resync is asked again')
rs = last('prepare_anchor')
T.check(rs.lat == req.lat and rs.lon == req.lon, 'the retry: the same origin')
T.check(st().guard.mode == 'hold' and veh.pos.x <= x1 - edgeguard.STOP_M + 0.5, 'still held while the resync fails')
veh.vel = vec3(0, 0, 0)
serverSend({ type = 'anchor_ready', requestId = rs.requestId, anchorId = 1, lat = rs.lat, lon = rs.lon, groundZ = 0,
  terrain = { cx = rs.cx, cy = rs.cy, heightmap = '/temp/beamngEarth/s2/hm_1_r.png', size = 1024, squareSize = 2,
    zBase = 0, maxHeight = 1024, q = 1 / 64 } })
T.check(pumpUntil(function() return last('commit_anchor').anchorId == 1 and #received('commit_anchor') == 2 end, 20),
  'resync committed (anchor 1 of s2)')
setGround(rs.cx, rs.cy)
T.check(farfield.session() == 's2', 'the far field is in session s2')
T.check(farfield._old(1, keys[2], 's1') ~= nil and farfield._old(1, keys[2], 's2') == nil and farfield.stats().old == 4,
  's1 tiles kept as old tiles under their own session (anchor 1 of s1, not of s2)')
T.check(#farfield.heldAnchors() == 0, 'heldAnchors: never s1\'s numbers')
nPose = #received('pose')
T.check(pumpUntil(function() return #received('pose') > nPose end, 30), 'poses after the resync')
T.check(has(last('pose').sessions, 's1') and #last('pose').held == 0, 'pose.sessions still names s1 while its tiles are shown')
T.check(st().guard.mode ~= 'hold' and not st().guard.pausedByUs, 'healthy again: the guard holds nothing')
-- s2 covers the area and says far_done: the s1 tiles go, then the folder is released
local keys2 = {}
for i = 1, 4 do
  local k = '0_' .. i .. '_0'
  serverSend({ type = 'far_tile', anchorId = 1, key = k, level = 0, shape = '/temp/beamngEarth/s2/far_1_' .. k .. '.cdae',
    pos = { (i - 1) * 256 - 512, -128, 0 }, err = 0.1, state = 'group' })
  keys2[i] = k
end
serverSend({ type = 'far_swap', anchorId = 1, id = 1, add = keys2, remove = {}, dIn = 0, dOut = 0 })
serverSend({ type = 'far_done', anchorId = 1, count = 4 })
T.check(pumpUntil(function() return farfield.stats().old == 0 and farfield.stats().deleting == 0 end, 200),
  's1 tiles gone once s2 covers them')
T.check(pumpUntil(function() return not has(farfield.heldSessions(), 's1') end, 400), 'the s1 material deleted too')
nPose = #received('pose')
T.check(pumpUntil(function() return #received('pose') > nPose end, 30), 'a pose')
T.check(not has(last('pose').sessions, 's1') and has(last('pose').sessions, 's2'), 'pose.sessions: s1 released, s2 in use')

------------------------------------------------------------------------------------------
-- 5. a server that does not answer terrain requests

b = st().block
local _, _, ex1 = lattice.groundBox(b.cx, b.cy)
veh.pos, veh.vel = vec3(b.cx + 300, b.cy, 0.5), vec3(20, 0, 0)
local nPt = #received('prepare_terrain')
T.check(pumpUntil(function() return #received('prepare_terrain') > nPt end, 40), 'terrain-follow asks')
frames(20)
T.check(st().guard.mode == 'off', 'just asked: the guard waits (the server may be slow)')
for _ = 1, 60 * 20 do frame() end
T.check(veh.pos.x <= ex1 - edgeguard.STOP_M + 0.5 and simTimeAuthority.paused,
  string.format('unanswered for STUCK_S: paused %.1f m inside the edge', ex1 - veh.pos.x))
T.check(shown() == 'Paused at the edge of the loaded ground until the BeamNG.Earth server answers terrain requests again (resume to drive back)',
  'banner: the server does not answer terrain requests')
pt = last('prepare_terrain')
serverSend({ type = 'terrain_ready', requestId = pt.requestId, anchorId = 1, cx = pt.cx, cy = pt.cy,
  heightmap = '/temp/beamngEarth/s2/hm_1_f.png', size = 1024, squareSize = 2, zBase = 0, maxHeight = 1024, q = 1 / 64 })
T.check(pumpUntil(function() return st().block.cx == pt.cx end, 100), 'the answer arrives and commits once the car is past the commit distance')
setGround(pt.cx, pt.cy)
frames(2)
T.check(st().guard.mode ~= 'hold' and not st().guard.pausedByUs and not shown(), 'answered: the guard holds nothing, the banner hidden')
-- a terrain_error: held for ERROR_HOLD_S
veh.vel = vec3(0, 0, 0)
b = st().block
veh.pos = vec3(b.cx + 400, b.cy, 0.5)
T.check(pumpUntil(function() return st().terrainPending ~= nil end, 40), 'asks again')
serverSend({ type = 'terrain_error', requestId = st().terrainPending.requestId, anchorId = 1, error = 'no elevation data' })
frames(3)
T.check(st().guard.mode ~= 'off', 'terrain_error: the guard runs')

------------------------------------------------------------------------------------------
-- 6. the server's offline note; the pause menu

veh.pos = vec3(b.cx, b.cy, 0.5)
serverSend({ type = 'status', text = 'Far field ready | Offline - using cached data', backlog = 0 })
frames(2)
T.check(core.banner().id == 'offline' and shown():find('internet data sources unreachable', 1, true), 'offline: in the banner')
local settings = require('beamngEarth/settings')
beamngEarth_core = core
local ui = settings.uiState()
T.check(ui.status.banner and ui.status.banner.id == 'offline' and ui.status.banner.level == 'warn'
  and ui.status.banner.text:find('internet data sources', 1, true), 'the settings page gets the same state line')
beamngEarth_core = nil
core.onUiChangedState('menu', 'play')
frames(2)
T.check(not shown(), 'pause menu: the banner is hidden')
core.onUiChangedState('play', 'menu')
frames(1)
T.check(shown() ~= nil, 'back in play: shown')
serverSend({ type = 'status', text = 'Far field ready', backlog = 0 })
frames(2)
T.check(core.banner().id ~= 'offline', 'the note gone: the offline line goes')

------------------------------------------------------------------------------------------
-- 7. an error inside the guard: off once, logged, never thrown

server.failErr, server.down = 'closed', true
frames(2)
veh.pos, veh.vel = vec3(b.cx + 900, b.cy, 0.5), vec3(10, 0, 0)
castRayStatic = function() error('ray broke') end
errors = {}
local ok = pcall(frames, 20)
T.check(ok and st().guard.mode == 'off' and #errors == 1 and errors[1]:find('edge guard turned off', 1, true),
  'a guard error: turned off once, logged once, nothing raised')

core.onExtensionUnloaded()
T.done('test_core_robustness')
