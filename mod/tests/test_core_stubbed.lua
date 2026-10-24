-- luajit mod/tests/test_core_stubbed.lua
-- Drives beamngEarth/core.lua through a whole session against stubbed engine globals and a
-- fake luasocket (protocol 4, docs/V1_3_SPEC.md E over docs/V1_2_DESIGN.md 7): connect -> hello ->
-- auto-teleport -> far tiles + swap groups -> gated terrain-follow in a governor heavy slot -> origin
-- rebase (two-phase, camera shift) -> far_done swap -> a restarted server in mesh near-visual mode
-- (terrain render disabled across imports) with satellite-textured tiles and the attribution line.
-- It checks the mod's own logic only; every engine call is a stub (see the report for what
-- still has to be verified in-game).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
require('beamngEarth/farfield').SPARE_MAX = 0 -- deletion timing as before; the spare pool: test_farfield_spares.lua
local framing = require('beamngEarth/framing')
local geodesy = require('beamngEarth/geodesy')
local follow = require('beamngEarth/follow')
local farfield = require('beamngEarth/farfield')
local crossover = require('beamngEarth/crossover')
local calls, objects = S.calls, S.objects

-- JSON stubs: payloads are opaque keys into a table on both directions
local wire = {}
local wireN = 0
function jsonEncode(msg) wireN = wireN + 1; wire['k' .. wireN] = msg; return 'k' .. wireN end
function jsonDecode(s) return wire[s] end

------------------------------------------------------------------------------------------
-- vehicle stub. A frame is onUpdate -> physics -> onPreRender (main.lua:819-824, 920).
-- setClusterPosRelRot is applied by the physics step `engineLag` frames later (0 = the same frame),
-- or at once with engineLag = 'sync'.

local frameNo, simRunning = 0, true
local veh = { id = 1, pos = vec3(0, 0, 0.5), vel = vec3(0, 0, 0), engineLag = 0, queue = {}, cmds = {} }
function veh:getID() return self.id end
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function veh:getVelocity() return self.vel end
function veh:getDirectionVector() return vec3(0, 1, 0) end
function veh:setClusterPosRelRot(node, x, y, z, qx, qy, qz, qw)
  S.count('setClusterPosRelRot')
  self.lastCluster = { node = node, x = x, y = y, z = z, qx = qx, qy = qy, qz = qz, qw = qw }
  if self.engineLag == 'sync' then self.pos = vec3(x, y, z)
  else self.queue[#self.queue + 1] = { pos = vec3(x, y, z), due = frameNo + self.engineLag } end
end
local physicsLog = {} -- world state seen by the physics step that applies a move
function veh:physics()
  if not simRunning then return end
  local keep = {}
  for _, q in ipairs(self.queue) do
    if q.due <= frameNo then
      self.pos = q.pos
      local tbo = scenetree.findObject('bngEarthTerrain')
      physicsLog[#physicsLog + 1] = { frame = frameNo, terrainX = tbo and tbo.pos.x, pos = q.pos }
    else
      keep[#keep + 1] = q
    end
  end
  self.queue = keep
end
function veh:applyClusterVelocityScaleAdd(node, scale) S.count('zeroVelocity'); if scale == 0 then self.vel = vec3() end end
function veh:setOriginalTransform(x, y, z, qx, qy, qz, qw) S.count('setOriginalTransform'); self.orig = { x, y, z, qx, qy, qz, qw } end
function veh:getSpawnWorldOOBB()
  return { getMatrix = function() return { toQuatF = function() return { x = 0, y = 0, z = 0, w = 1 } end } end }
end
function veh:queueLuaCommand(s) self.cmds[#self.cmds + 1] = s end
function getPlayerVehicle() return veh end
function getAllVehicles() return { veh } end
spawn = { safeTeleport = function(v, pos) S.count('safeTeleport'); v.pos = vec3(pos.x, pos.y, pos.z) end }
commands = { isFreeCamera = function() return false end }
local skyLoc = nil
core_celestial = { setLocation = function(lat, lon) skyLoc = { lat, lon } end }
-- the rendering camera (core/camera.lua getPositionXYZ / getForwardXYZ / getFovDeg): behind the car, looking +x
core_camera = {
  getPositionXYZ = function() return veh.pos.x - 6, veh.pos.y, veh.pos.z + 2 end,
  getForwardXYZ = function() return 1, 0, 0 end,
  getFovDeg = function() return 65 end,
}

-- camera: an orbit camera with world-space history, reached through core/cameraUpdate's `contexts`
local orbit = setmetatable({ camLastPos2 = vec3(), camLastTargetPos = vec3(), camRot = vec3(0, -12, 0) }, {})
local camData = { pos = vec3(), prevPos = vec3(), vehPos = vec3(), prevVehPos = vec3(), vel = vec3() }
local contexts = { main = { camData = camData, finalCameraData = { pos = vec3() }, lastValidData = { pos = vec3() },
  vehicleCamerasCache = { [1] = { cameras = { orbit = orbit } } }, globalCamerasCache = {} } }
package.loaded['core/cameraUpdate'] = { onPreRender = function() return contexts end }
-- what the camera would hold after rendering a frame behind the car (driving east)
local function cameraFollows()
  local p = veh.pos
  orbit.camLastPos2 = vec3(p.x - 1.5, p.y, p.z + 1)
  orbit.camLastTargetPos = vec3(p.x, p.y, p.z + 1)
  camData.pos = vec3(p.x, p.y, p.z)
end

local uiText = {} -- every string the window drew in the last frame
ui_imgui = setmetatable({
  TextUnformatted = function(t) uiText[#uiText + 1] = t end,
  Begin = function() uiText = {}; return true end,
  BoolPtr = function(v) return { [0] = v } end,
  DoublePtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end,
  Button = function() return false end,
}, { __index = function() return function() end end })

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

math.randomseed(7)
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
      i = i or 1
      server.inbox:feed(data:sub(i))
      while true do
        local p = server.inbox:pop()
        if not p then break end
        server.sent[#server.sent + 1] = jsonDecode(p)
      end
      return #data
    end
    function c:receive(n)
      -- deliver a random-sized slice so frames arrive split across frames
      local k = math.min(#server.toClient, math.random(0, 40))
      local chunk = server.toClient:sub(1, k)
      server.toClient = server.toClient:sub(k + 1)
      if server.failErr and server.toClient == '' then
        local err = server.failErr
        server.failErr = nil -- one-shot: the next connection works again
        return nil, err, chunk
      end
      return nil, 'timeout', chunk
    end
    return c
  end
  return socket
end

------------------------------------------------------------------------------------------
-- the session

local core = require('beamngEarth/core')
require('beamngEarth/statusbar').enabled = false   -- its own test: test_statusbar.lua (these count text draws)
-- the governor's wall clock: 16 ms per frame (deterministic heavy-slot timing)
local wallNow = 0
core._governor().clock = function() return wallNow end
local preRenderLog = {} -- what the camera would see: car position + orbit history at onPreRender
local function frame(paused)
  frameNo = frameNo + 1
  wallNow = wallNow + 16
  simRunning = not paused
  local dtSim = paused and 0 or 0.05
  core.onUpdate(0.05, dtSim)
  veh:physics()
  core.onPreRender(0.05, dtSim)
  preRenderLog[frameNo] = { car = vec3(veh.pos), last2 = vec3(orbit.camLastPos2), terrainX = scenetree.findObject('bngEarthTerrain').pos.x }
end
local function frames(n) for _ = 1, n do frame() end end
local function pumpUntil(cond, max)
  for _ = 1, max or 400 do
    if cond() then return true end
    frame()
  end
  return cond()
end
local SQ = follow.SQUARE_SIZE
local function terrainMsg(cx, cy, path, zBase)
  return { cx = cx, cy = cy, heightmap = path, size = 1024, squareSize = SQ, zBase = zBase or 1100, maxHeight = 3500 }
end

core.onExtensionLoaded() -- a level is "loaded": our level -> flat ground + activate
T.check(calls.importMaps == 1, 'flat terrain imported on level start')
T.check(scenetree.theLevelInfo.visibleDistance == 100000, 'visibleDistance 100 km')
local tb = scenetree.findObject('bngEarthTerrain')
T.check(tb and tb.fields.materialTextureSet == 'beamngEarthTerrainMaterialTextureSet', 'terrain uses our texture set')
T.check(tb and tb.lastImport.mats[1] == 'beamngEarthDirt' and tb.lastImport.maps[1].material == 'beamngEarthDirt', 'material list + layer map')
T.check(tb and tb.lastImport.maps[1].path:match('beamngEarth_layer_1024%.png$') ~= nil, '1024 layer map')
T.check(tb and tb.lastImport.path:match('beamngEarth_flat_1024%.png$') ~= nil, '1024 flat heightmap')
T.check(tb and tb.lastImport.flip == false, 'flipY false')
T.check(tb and tb.lastImport.sq == SQ and tb.pos.x == -1024 and tb.pos.y == -1024, 'flat block 2048 m centred on the origin')

frames(3)
T.check(#received('hello') == 1 and received('hello')[1].protocol == 4 and received('hello')[1].format == 4,
  'hello (protocol 4, format 4: the newest the mod reads) sent on connect')
T.check(calls.safeTeleport == 1, 'player placed on flat ground')

-- capture the mod's warnings (the stub log() drops everything)
local warnings = {}
local stubLog = log
log = function(level, tag, text) if level == 'W' then warnings[#warnings + 1] = tostring(text) end end
local function warned(pat)
  for _, w in ipairs(warnings) do if w:match(pat) then return true end end
  return false
end
serverSend({ type = 'hello', protocol = 4, format = 3, serverVersion = '0.4.0', sessionId = 's1', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'terrain', attribution = 'Imagery: test credits; Elevation: test' })
T.check(pumpUntil(function() return #received('prepare_anchor') == 1 end), 'auto-teleport requested in our level')
do -- the level start's teleport is the first recent place (places.lua; no file API here: memory only)
  local r = require('beamngEarth/places').recentList()[1]
  T.check(r and r.kind == 'start' and r.lat == 36.596718 and r.lon == -118.169991,
    'recent places: the level start recorded')
end
local function uiShows(pat)
  for _, t in ipairs(uiText) do if tostring(t):match(pat) then return true end end
  return false
end
frames(1)
T.check(uiShows('Imagery: test credits'), 'the server\'s attribution line is shown (hello.attribution)')
T.check(not warned('material'), 'hello: the server\'s far-tile material is the one the mod ships')
T.check(crossover.cells() == 32 and farfield.stats().exactLevel == -2,
  'FORMAT 3 hello (no cells): 32-cell tiles, exact level L-2 (v1.3.1)')
local req = received('prepare_anchor')[1]
T.near(req.lat, 36.596718, 1e-9, 'default lat'); T.near(req.lon, -118.169991, 1e-9, 'default lon')
T.check(req.cx == 0 and req.cy == 0, 'teleport terrain centred at (0,0)')

scenetree.PostFxMotionBlur = { strength = 0.3 } -- the player's motion blur setting
serverSend({ type = 'anchor_ready', requestId = req.requestId, anchorId = 1, lat = req.lat, lon = req.lon, groundZ = 1180,
  terrain = terrainMsg(0, 0, '/temp/beamngEarth/s1/hm_1_0_0.png', 1100) })
T.check(pumpUntil(function() return #received('commit_anchor') == 1 end), 'commit_anchor sent')
T.check(scenetree.PostFxMotionBlur.strength == 0, 'teleport: motion blur off in the commit frame (no full-screen smear)')
T.check(received('commit_anchor')[1].anchorId == 1, 'commit anchorId')
local ct = last('commit_terrain')
T.check(ct and ct.anchorId == 1 and ct.cx == 0 and ct.cy == 0, 'commit_terrain for the anchor terrain')
T.check(veh.pos.x == 0 and veh.pos.y == 0 and veh.pos.z > 1181, 'player lifted above the new origin in the commit frame')
T.check(calls.zeroVelocity == 1, 'lifted player has zero velocity')
frames(5)
T.check(scenetree.PostFxMotionBlur.strength == 0.3, 'teleport: motion blur restored a few frames later')
T.check(tb.pos.x == -1024 and tb.pos.y == -1024 and tb.pos.z == 1100, 'block position = (cx-1024, cy-1024, zBase)')
T.check(tb.lastImport.sq == 2.0 and tb.lastImport.maxH == 3500, 'importMaps squareSize 2.0 + height')
T.check(pumpUntil(function() return calls.safeTeleport == 2 end, 10), 'teleport placement')
T.near(veh.pos.z, 1181, 1e-9, 'placed at groundZ + 1')

-- far tiles: loaded hidden within the governor budget, shown by their swap group
local farKeys = {}
for i = 1, 5 do
  serverSend({ type = 'far_tile', anchorId = 1, key = '1_' .. i .. '_0', level = 1, shape = '/temp/beamngEarth/s1/far_1_1_' .. i .. '_0.cdae',
    pos = { 3000 + i, 0, 1000 }, err = 0.8, state = 'group' })
  farKeys[i] = '1_' .. i .. '_0'
end
T.check(pumpUntil(function() return (calls.createObject_TSStatic or 0) == 5 end), 'far tiles spawned')
for _, o in ipairs(S.liveOf('TSStatic')) do T.check(o.hidden, 'loaded hidden until the swap') end
serverSend({ type = 'far_swap', anchorId = 1, id = 1, add = farKeys, remove = {}, dIn = 0, dOut = 0 })
serverSend({ type = 'far_done', anchorId = 1, count = 5 })
T.check(pumpUntil(function() return farfield.stats().live == 5 end), 'swap group shown')
for _, o in ipairs(S.liveOf('TSStatic')) do
  T.check(not o.hidden, 'far tile shown')
  T.check(o.fields.collisionType == 'None', 'far tile collisionType None')
  T.check(o.name:match('^bngEarthFar_%d+_1_%d_0$') ~= nil, 'far tile name sanitized: ' .. o.name)
end
serverSend({ type = 'far_remove', anchorId = 1, key = '1_5_0' })
T.check(pumpUntil(function() return #S.liveOf('TSStatic') == 4 end, 50), 'far_remove applied')

-- poses at ~2 Hz, with velocity
veh.vel = vec3(20, 1, 0)
local p0 = #received('pose')
frames(20) -- 1 s
local np = #received('pose') - p0
T.check(np >= 1 and np <= 3, 'pose rate ~2 Hz (' .. np .. ' in 1 s)')
local pose = last('pose')
T.check(pose.anchorId == 1 and pose.lat and pose.lon and pose.vx == 20 and pose.vy == 1, 'pose fields incl. vx, vy')
T.check(pose.cam and pose.cam.fx == 1 and pose.cam.fovDeg == 65 and pose.cam.x == veh.pos.x - 6, 'pose carries the camera (protocol 3)')

------------------------------------------------------------------------------------------
-- terrain-follow (same anchor)
local imports = calls.importMaps
local clusterMoves = calls.setClusterPosRelRot
veh.pos = vec3(290, 0, 1200)
frames(5)
T.check(#received('prepare_terrain') == 0, 'below 300 m: no terrain request')
local velWas = veh.vel
veh.pos, veh.vel = vec3(305, 0, 1200), vec3(0 / 0, 0 / 0, 0)    -- an unstable car (NaN), before the game resets it
frames(5)
T.check(#received('prepare_terrain') == 0, 'a NaN speed: no terrain request')
veh.pos, veh.vel = vec3(290, 0, 1200), velWas
veh.pos = vec3(301, 0, 1200)
T.check(pumpUntil(function() return #received('prepare_terrain') == 1 end, 5), 'prepare_terrain past 300 m')
local pt = received('prepare_terrain')[1]
T.check(pt.anchorId == 1 and pt.cx == 256 and pt.cy == 0, 'target = snap256(player + 4 s * v) = (256, 0)')
serverSend({ type = 'terrain_ready', requestId = pt.requestId, anchorId = 1, cx = 256, cy = 0,
  heightmap = '/temp/beamngEarth/s1/hm_1_256_0.png', size = 1024, squareSize = SQ, zBase = 1110, maxHeight = 1024, q = 1 / 64 })
frames(40)
T.check(calls.importMaps == imports, 'ready but below 450 m: no commit yet')
local farBefore = {}
for _, o in ipairs(S.liveOf('TSStatic')) do farBefore[o.id] = o:getPosition() end
veh.pos = vec3(451, 0, 1200)
frames(20)
T.check(calls.importMaps == imports, 'past 450 m, but the strip the move uncovers has no live tiles: commit gated')
-- the server streams the L0 tiles under the block (class 0); the west column is what the move uncovers
local col = {}
for j = -4, 3 do
  serverSend({ type = 'far_tile', anchorId = 1, key = '0_-4_' .. j, level = 0, shape = '/temp/beamngEarth/s1/far_1_0_-4_' .. j .. '.cdae',
    pos = { -1024, j * 256, 1100 }, err = 0.2, sink = 1.0, state = 'group' })
  col[#col + 1] = '0_-4_' .. j
end
serverSend({ type = 'far_swap', anchorId = 1, id = 2, add = col, remove = {}, dIn = 0, dOut = 0 })
T.check(pumpUntil(function() return farfield._tile('0_-4_0') ~= nil end, 300), 'covered L0 tiles swapped in')
local colObj = objects[farfield._tile('0_-4_0').id]
T.check(not colObj.hidden and colObj.pos.z == 1100 - 1.0, 'edge ring tile under the block: shown, sunk by its sink')
-- every block import drops the grass on the block: loadBlock asks for its rebuild right after the
-- import, in the same call (groundcover.lua, docs/ARCHITECTURE.md "Ground surfaces")
local gcmod = require('beamngEarth/groundcover')
local gcOrig, gcAt = gcmod.afterBlockChange, {}
gcmod.afterBlockChange = function() gcAt[#gcAt + 1] = calls.importMaps; return gcOrig() end
T.check(pumpUntil(function() return calls.importMaps == imports + 1 end, 100), 'terrain-follow commit once the strip is live')
T.check(#gcAt == 1 and gcAt[1] == imports + 1, 'grass rebuild requested once, right after the import')
gcmod.afterBlockChange = gcOrig
T.check(core._governor():isHeavyFrame(), 'the commit ran in a governor heavy slot')
T.check(colObj.pos.z == 1100 and not colObj.hidden, 'uncovered strip tile at true height in the commit frame')
T.check(tb.lastImport.maxH == 1024, 'maxHeight = q * 65536')
T.check(tb.pos.x == 256 - 1024 and tb.pos.y == -1024 and tb.pos.z == 1110, 'block moved to (cx-1024, cy-1024, zBase)')
T.check(tb.lastImport.path == '/temp/beamngEarth/s1/hm_1_256_0.png', 'imported the prepared heightmap')
ct = last('commit_terrain')
T.check(ct.anchorId == 1 and ct.cx == 256 and ct.cy == 0, 'commit_terrain sent')
T.check(calls.setClusterPosRelRot == clusterMoves and veh.pos.x == 451, 'terrain-follow does not move vehicles')
for id, p in pairs(farBefore) do T.check(objects[id].pos.x == p.x, 'terrain-follow does not move far tiles') end
-- stale / foreign answers are ignored
serverSend({ type = 'terrain_ready', requestId = 999, anchorId = 1, cx = 0, cy = 0, heightmap = '/x.png', size = 1024 })
frames(5)
T.check(calls.importMaps == imports + 1, 'stale terrain_ready ignored')

------------------------------------------------------------------------------------------
-- origin rebase at 10 km
T.check(core.setRebaseDistance(500) == 2000 and core.setRebaseDistance(10000) == 10000, 'setRebaseDistance clamps')
veh.pos = vec3(9690, 0, 1200)
veh.vel = vec3(30, 0, 0)
frames(5)
local nAnchorReq = #received('prepare_anchor')
T.check(nAnchorReq == 1, 'no anchor request before 9700 m')
veh.pos = vec3(9710, 0, 1200)
T.check(pumpUntil(function() return #received('prepare_anchor') == 2 end, 5), 'prefetch at distance - 300 m')
local req2 = received('prepare_anchor')[2]
local a1 = geodesy.newAnchor(req.lat, req.lon)
local plat, plon = a1:enuToGeodetic(9710, 0, 1200)
T.near(req2.lat, plat, 1e-9, 'prefetch lat = player lat'); T.near(req2.lon, plon, 1e-9, 'prefetch lon = player lon')
T.check(req2.cx == 512 and req2.cy == 0, 'terrain hint = snap256(predicted position at the commit + 4 s * v) = (512, 0), got ' .. req2.cx)
-- (terrain-follow keeps working in the old frame meanwhile; answer nothing to it)
serverSend({ type = 'anchor_ready', requestId = req2.requestId, anchorId = 2, lat = req2.lat, lon = req2.lon, groundZ = 1190,
  terrain = terrainMsg(256, 0, '/temp/beamngEarth/s1/hm_2_256_0.png', 1120) })
frames(40)
T.check(#received('commit_anchor') == 1, 'ready but below 10 km: no commit yet')

-- cross 10 km (engine applies the move in the same frame's physics step, the expected case)
veh.pos = vec3(10000, 10, 1200)
cameraFollows()
imports = calls.importMaps
farBefore = {}
for _, o in ipairs(S.liveOf('TSStatic')) do farBefore[o.id] = o:getPosition() end
local orbitOld = { orbit.camLastPos2.x, orbit.camLastPos2.y, orbit.camLastPos2.z }
local camOld = { camData.pos.x, camData.pos.y, camData.pos.z }
frame()
local fN = frameNo
T.check(calls.setClusterPosRelRot > clusterMoves, 'rebase: vehicles moved')
local a2 = geodesy.newAnchor(req2.lat, req2.lon)
local xf = a1:transformTo(a2)
local ex, ey, ez = xf:apply(10000, 10, 1200)
local lc = veh.lastCluster
T.check(lc.node == -1, 'all clusters')
T.near(lc.x, ex, 1e-6, 'vehicle x in new frame'); T.near(lc.y, ey, 1e-6, 'vehicle y in new frame'); T.near(lc.z, ez, 1e-6, 'vehicle z in new frame')
T.near(lc.x, 290, 0.5, 'vehicle ~290 m from the new origin')
local bqx, bqy, bqz, bqw = xf:beamngQuat()
T.near(lc.qx, bqx, 1e-15, 'q_rel = R (BeamNG quat) x'); T.near(lc.qy, bqy, 1e-15, 'q_rel y'); T.near(lc.qz, bqz, 1e-15, 'q_rel z')
T.near(lc.qw, bqw, 1e-15, 'q_rel w')
T.check(math.abs(lc.qz) > 1e-4, 'q_rel is a real rotation (meridian convergence + tilt)')
T.check(calls.setOriginalTransform >= 1 and math.abs(veh.orig[1] - ex) < 1e-6, 'original transform moved to the new frame')
T.check(#veh.cmds >= 1, 'recovery points mapped in the vehicle VM')
-- run that vehicle-Lua command against a fake recovery module
do
  local env = { vec3 = vec3, type = type, recovery = {
    recoveryPoints = { head = 0, tail = 3, { pos = vec3(9990, 10, 1200), dirFront = vec3(1, 0, 0), dirUp = vec3(0, 0, 1) },
      { pos = vec3(10000, 10, 1200), dirFront = vec3(1, 0, 0), dirUp = vec3(0, 0, 1) },
      { pos = vec3(lc.x + 1, lc.y, lc.z), dirFront = vec3(1, 0, 0), dirUp = vec3(0, 0, 1) } }, -- recorded after the move
    homePoint = { pos = vec3(0, 0, 1181), dirFront = vec3(1, 0, 0), dirUp = vec3(0, 0, 1) } } } -- saved at the teleport target
  local fn = assert(loadstring(veh.cmds[#veh.cmds]))
  setfenv(fn, env)
  fn()
  local rp = env.recovery.recoveryPoints
  local x1 = xf:apply(9990, 10, 1200)
  T.near(rp[1].pos.x, x1, 1e-6, 'recovery point mapped')
  T.near(rp[3].pos.x, lc.x + 1, 1e-9, 'point already in the new frame left alone (idempotent)')
  T.near(env.recovery.homePoint.pos.x, (xf:apply(0, 0, 1181)), 1e-6, 'home point at the old origin mapped')
  local fx = xf:rotate(1, 0, 0)
  T.near(rp[1].dirFront.x, fx, 1e-12, 'recovery direction rotated')
end
-- one frame: physics saw the new terrain, the camera saw car + its own history in the new frame
local ph = physicsLog[#physicsLog]
T.check(ph.frame == fN and ph.terrainX == 256 - 1024, 'the physics step that moved the car already had the new terrain')
T.check(calls.importMaps == imports + 1 and tb.lastImport.path == '/temp/beamngEarth/s1/hm_2_256_0.png', 'terrain of the new frame imported')
T.check(tb.pos.x == 256 - 1024 and tb.pos.z == 1120, 'block at the anchor terrain')
T.check(#received('commit_anchor') == 2 and last('commit_anchor').anchorId == 2, 'commit_anchor')
ct = last('commit_terrain')
T.check(ct.anchorId == 2 and ct.cx == 256 and ct.cy == 0, 'commit_terrain')
local ox, oy, oz = xf:apply(orbitOld[1], orbitOld[2], orbitOld[3])
local seen = preRenderLog[fN]
T.near(seen.last2.x, ox, 1e-6, 'camera at preRender: orbit trailing point x mapped'); T.near(seen.last2.y, oy, 1e-6, 'orbit y')
T.near(seen.last2.z, oz, 1e-6, 'orbit z')
T.near(seen.car.x, ex, 1e-6, 'camera at preRender: car already at its new position')
T.check(seen.car.x - seen.last2.x > 1.4, 'orbit camera stays behind the car (aims along +x)')
T.near(camData.pos.x, (xf:apply(camOld[1], camOld[2], camOld[3])), 1e-6, 'cameraUpdate camData.pos mapped (no teleport flag)')
T.check(orbit.camRot.y == -12, 'camera angles untouched')
for id, p in pairs(farBefore) do
  local o = objects[id]
  T.check(o ~= nil, 'old far tile kept until far_done')
  if o then
    local tx, ty, tz = xf:apply(p.x, p.y, p.z)
    T.near(o.pos.x, tx, 1e-6, 'old far tile x = R*T + t'); T.near(o.pos.z, tz, 1e-6, 'old far tile z = R*T + t')
    T.near(o.rot.w, bqw, 1e-15, 'old far tile rotated by R')
  end
end
T.check(calls.safeTeleport == 2, 'rebase does not reset the vehicle')
T.check(core._rebaseLag() == 0, 'measured lag = 0 frames (same frame)')

-- new anchor's far tiles, then far_done -> old tiles deleted progressively
serverSend({ type = 'far_tile', anchorId = 1, key = 'late', shape = '/x.dae', pos = { 0, 0, 0 } }) -- stale
local nOld = 0
for _ in pairs(farBefore) do nOld = nOld + 1 end
serverSend({ type = 'far_tile', anchorId = 2, key = '1_0_0', level = 1, shape = '/temp/beamngEarth/s1/far_2_1_0_0.cdae', pos = { 3000, 0, 1000 }, err = 1, state = 'group' })
serverSend({ type = 'far_swap', anchorId = 2, id = 1, add = { '1_0_0' }, remove = {}, dIn = 0, dOut = 0 })
serverSend({ type = 'far_done', anchorId = 2, count = 1 })
local created = calls.createObject_TSStatic
T.check(pumpUntil(function() return (calls.delete_TSStatic or 0) == nOld + 1 end),
  'old far tiles deleted after new far_done (' .. nOld .. ' + 1 removed earlier)')
T.check(calls.createObject_TSStatic == created + 1, 'stale far tile ignored')

-- terrain-follow continues in the new frame
local nPrep = #received('prepare_terrain')
veh.pos = vec3(lc.x + 300, lc.y, lc.z) -- ~590 m: > 300 m from the block centre 256
T.check(pumpUntil(function() return #received('prepare_terrain') > nPrep end, 5), 'terrain-follow in the new frame')
T.check(last('prepare_terrain').anchorId == 2, 'new anchor id in prepare_terrain')

-- an engine that applies the move one frame later: the first rebase measures it, the next ones
-- run phase 2 one frame after phase 1
core.setRebaseDistance(2000)
veh.engineLag = 1
local function rebaseTo(n, x)
  veh.pos = vec3(x - 250, 0, 1200)
  veh.vel = vec3(25, 0, 0)
  T.check(pumpUntil(function() return #received('prepare_anchor') == n end, 5), 'prefetch #' .. n)
  local r = received('prepare_anchor')[n]
  serverSend({ type = 'anchor_ready', requestId = r.requestId, anchorId = n, lat = r.lat, lon = r.lon, groundZ = 1190,
    terrain = terrainMsg(r.cx, r.cy, '/temp/beamngEarth/s1/hm_' .. n .. '.png', 1120) })
  frames(30) -- ready, but below the distance
  veh.pos = vec3(x, 0, 1200)
  cameraFollows()
  return r
end
local moves = calls.setClusterPosRelRot
rebaseTo(3, 2001)
frame()
local fA = frameNo
T.check(calls.setClusterPosRelRot > moves and #received('commit_anchor') == 3, 'first lagging rebase: both phases at once (lag not known yet)')
T.check(preRenderLog[fA].car.x > 1500, 'the camera saw one mismatched frame (car still in the old frame)')
frame()
T.check(type(core.status()) == 'string', 'status works')
local function lagOf() local v = core._rebaseLag(); return v end
T.check(lagOf() == 1, 'measured lag = 1 frame')

moves = calls.setClusterPosRelRot
local nCommits = #received('commit_anchor')
rebaseTo(4, 2001)
imports = calls.importMaps
frame()
fA = frameNo
T.check(calls.setClusterPosRelRot > moves, 'phase 1 (vehicles) in frame N')
T.check(calls.importMaps == imports and #received('commit_anchor') == nCommits, 'phase 2 waits for frame N+1')
T.check(preRenderLog[fA].car.x > 1500 and preRenderLog[fA].terrainX ~= nil, 'frame N renders car and world both in the old frame')
frame(true) -- paused frames do not count and do not apply physics
T.check(calls.importMaps == imports, 'paused: still waiting')
frame()
T.check(calls.importMaps == imports + 1 and #received('commit_anchor') == nCommits + 1, 'phase 2 in frame N+1')
ph = physicsLog[#physicsLog]
T.check(ph.frame == frameNo and ph.terrainX == scenetree.findObject('bngEarthTerrain').pos.x, 'the physics step that moved the car had the new terrain')
seen = preRenderLog[frameNo]
T.check(seen.car.x < 1500 and seen.car.x - seen.last2.x > 1.4, 'frame N+1: car, world and camera all in the new frame, camera behind')
veh.engineLag = 0
core.setRebaseDistance(10000)
local req4 = received('prepare_anchor')[4]

-- sky follows the player (throttled)
frames(120)
T.check(skyLoc ~= nil and math.abs(skyLoc[1] - req4.lat) < 0.05, 'TimeOfDay location follows the player')

-- a reset connection (not just 'closed') is detected, and frames that arrived with it are kept
local connects = #received('hello')
local before = calls.createObject_TSStatic
serverSend({ type = 'far_tile', anchorId = 4, key = '1_1_0', shape = '/temp/beamngEarth/s1/far_4_1_1_0.dae', pos = { 3100, 0, 1000 } })
server.failErr = 'connection reset by peer'
T.check(pumpUntil(function() return calls.createObject_TSStatic == before + 1 end, 50), 'final frame decoded before close')
T.check(pumpUntil(function() return #received('hello') == connects + 1 end, 100), 'reconnected after reset')

-- the (restarted) server says hello again: re-register the SAME origin under a new anchorId,
-- terrain where terrain-follow would put it; nothing in the world moves
local nAnchor = #received('prepare_anchor')
local cur = received('prepare_anchor')[4]
veh.pos = vec3(700, 30, 1200)
veh.vel = vec3(20, 0, 0)
frames(2)
local movesBefore, setOrig = calls.setClusterPosRelRot, calls.setOriginalTransform
T.check(tb.renderEnabled == nil and (calls.setRenderEnabled or 0) == 0, 'terrain mode: the block is drawn, render flag never touched')
T.check(tb.fields.castShadows == '0', 'the block casts no shadows (no shadow-map redraw at its moves)')
T.check(farfield.nearVisual() == 'terrain', 'terrain near visual')
local roadsMod = require('beamngEarth/roads')
local roadClears, realClear = 0, roadsMod.clear
roadsMod.clear = function(...) roadClears = roadClears + 1; return realClear(...) end
serverSend({ type = 'hello', protocol = 4, format = 3, serverVersion = '0.4.0', sessionId = 's2', farFormat = 'cdae',
  material = 'bngEarthFar_v9', nearVisual = 'mesh' })
T.check(pumpUntil(function() return #received('prepare_anchor') == nAnchor + 1 end, 10), 'resync requested on hello')
T.check(roadClears == 1, 'a new server session: the old session\'s road tiles cleared')
roadsMod.clear = realClear
T.check(tb.renderEnabled == false and farfield.nearVisual() == 'mesh', 'hello nearVisual = mesh: block render disabled, far field in mesh mode')
T.check(tb.fields.castShadows == '0', 'mesh mode: still no shadows')
T.check(warned('material mismatch: server bngEarthFar_v9, mod bngEarthFar_v3'), 'hello: a different far-tile material is reported')
log = stubLog
local rs = last('prepare_anchor')
T.check(rs.lat == cur.lat and rs.lon == cur.lon, 'resync keeps the current geodetic origin')
local ecx, ecy = follow.target(700, 30, 20, 0)
T.check(rs.cx == ecx and rs.cy == ecy, 'resync terrain at the terrain-follow target')
-- the player drives off before the answer: stale -> asked again
veh.pos = vec3(1300, 30, 1200)
serverSend({ type = 'anchor_ready', requestId = rs.requestId, anchorId = 5, lat = rs.lat, lon = rs.lon, groundZ = 0,
  terrain = terrainMsg(rs.cx, rs.cy, '/temp/beamngEarth/s2/hm_5.png', 1120) })
T.check(pumpUntil(function() return #received('prepare_anchor') == nAnchor + 2 end, 10), 'stale resync re-requested')
rs = last('prepare_anchor')
-- regression (in-game 2026-10-10): a server that keeps answering with a block the mod deems stale
-- caused ~4 re-requests per second forever. A second stale answer must not be re-requested at once.
serverSend({ type = 'anchor_ready', requestId = rs.requestId, anchorId = 55, lat = rs.lat, lon = rs.lon, groundZ = 0,
  terrain = terrainMsg(768, 0, '/temp/beamngEarth/s2/hm_55.png', 1120) })
frames(40) -- 2 s at 0.05 s per frame, less than RETRY_S
T.check(#received('prepare_anchor') == nAnchor + 2, 'stale resync not re-requested within RETRY_S')
T.check(pumpUntil(function() return #received('prepare_anchor') == nAnchor + 3 end, 120), 'stale resync re-requested after RETRY_S')
rs = last('prepare_anchor')
local nc = #received('commit_anchor')
serverSend({ type = 'anchor_ready', requestId = rs.requestId, anchorId = 6, lat = rs.lat, lon = rs.lon, groundZ = 0,
  terrain = terrainMsg(rs.cx, rs.cy, '/temp/beamngEarth/s2/hm_6.png', 1120) })
T.check(pumpUntil(function() return #received('commit_anchor') == nc + 1 end, 10), 'resync committed')
T.check(last('commit_anchor').anchorId == 6 and last('commit_terrain').anchorId == 6, 'resync commits the new anchor id')
T.check(calls.setClusterPosRelRot == movesBefore and calls.setOriginalTransform == setOrig and veh.pos.x == 1300,
  'resync does not move vehicles')
T.check(tb.pos.x == rs.cx - 1024 and tb.lastImport.path == '/temp/beamngEarth/s2/hm_6.png', 'resync terrain imported')
-- mesh mode: the render flag is re-applied after every importMaps (persistence across imports unverified)
local renderCalls = calls.setRenderEnabled
T.check(tb.renderEnabled == false and renderCalls >= 2, 'mesh: render disabled again after the import (' .. renderCalls .. ' calls)')
-- terrain-follow in mesh mode: not gated on far tiles (none are live for anchor 6)
local imp6 = calls.importMaps
veh.pos = vec3(rs.cx + 320, rs.cy, 1200)
veh.vel = vec3(20, 0, 0)
T.check(pumpUntil(function() return last('prepare_terrain').anchorId == 6 end, 10), 'mesh: terrain-follow prepares')
local pt6 = last('prepare_terrain')
serverSend({ type = 'terrain_ready', requestId = pt6.requestId, anchorId = 6, cx = pt6.cx, cy = pt6.cy,
  heightmap = '/temp/beamngEarth/s2/hm_6b.png', size = 1024, squareSize = SQ, zBase = 1120, maxHeight = 1024, q = 1 / 64 })
frames(5)
veh.pos = vec3(rs.cx + 460, rs.cy, 1200)
T.check(pumpUntil(function() return calls.importMaps == imp6 + 1 end, 100), 'mesh: commit without far tiles live (no gating)')
T.check(tb.renderEnabled == false and calls.setRenderEnabled > renderCalls, 'mesh: render disabled again after the follow commit')
-- protocol-4 far tiles with satellite imagery: per-tile Material, attribution line
local satName = 'bngEarthSat_6_1_9_0'
serverSend({ type = 'far_tile', anchorId = 6, key = '1_9_0', level = 1, shape = '/temp/beamngEarth/s2/far_6_1_9_0.cdae',
  pos = { 4608, 0, 1000 }, err = 0.8, state = 'group', material = satName, texture = '/temp/beamngEarth/s2/far_6_1_9_0.dds',
  detail = true })
serverSend({ type = 'far_swap', anchorId = 6, id = 1, add = { '1_9_0' }, remove = {}, dIn = 0, dOut = 0 })
T.check(pumpUntil(function() return farfield._tile('1_9_0') ~= nil end, 100), 'textured tile shown')
local satMat = scenetree.findObject(satName)
T.check(satMat and satMat.class == 'Material' and satMat.fields.mapTo == satName
  and satMat.fields.colorMap == '/temp/beamngEarth/s2/far_6_1_9_0.dds', 'per-tile Material created (mapTo, colorMap)')
frames(1)
T.check(uiShows('Imagery: USGS The National Map') and uiShows('EOX') and uiShows('Elevation: Mapterhorn'),
  'no server attribution: the built-in imagery credits shown once textured tiles arrive')

-- console teleport validation
T.check(core.teleport(91, 0) == false, 'invalid lat rejected')
T.check(core.teleport(46.5, 7.9) == true, 'console teleport accepted')
do
  local places = require('beamngEarth/places')
  local r = places.recentList()[1]
  T.check(r.kind == 'teleport' and r.lat == 46.5 and r.lon == 7.9 and r.name == nil, 'recent places: a console teleport recorded')
  local n = #places.recentList()
  T.check(core.teleport(91, 0) == false and #places.recentList() == n, 'recent places: a refused teleport is not recorded')
  core.teleport(46.5001, 7.9, 'Named')
  T.check(#places.recentList() == n and places.recentList()[1].name == 'Named', 'recent places: teleport(lat, lon, name) records the name')
  local n2, top = #places.recentList(), places.recentList()[1]
  T.check(core.teleport(10.5, 20.5, nil, { record = false }) == true and #places.recentList() == n2
    and places.recentList()[1] == top, 'recent places: a tool teleport (record = false) is not recorded')
end
T.check(core.setFarBudget(3) == 3 and core.setFarBudget(10) == 4, 'far budget setter (governor target, 1-4 ms)')

-- M1 (docs/V1_4_SPEC.md 3.1): a FORMAT 4 server sends hello.cells; the mod reads formats 3 and 4
local infos = {}
log = function(level, tag, text) if level == 'I' then infos[#infos + 1] = tostring(text) end end
local function said(pat)
  for _, w in ipairs(infos) do if w:match(pat) then return true end end
  return false
end
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, serverVersion = '0.4.0', sessionId = 's3',
  farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(2)
T.check(crossover.cells() == 64 and crossover.defaultErr(0) == 1, 'hello.cells 64: crossover.defaultErr from it')
T.check(farfield.stats().exactLevel == -1, 'hello.cells 64: the exact level (exactMiss metric) is L-1')
T.check(not said('Format mismatch'), 'FORMAT 4 accepted')
serverSend({ type = 'hello', protocol = 4, format = 5, serverVersion = '0.4.0', sessionId = 's4', farFormat = 'cdae',
  material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(2)
T.check(said('Format mismatch: server 5, mod 3/4'), 'an unknown format is reported')
T.check(crossover.cells() == 32 and farfield.stats().exactLevel == -2, 'no hello.cells: back to 32 cells, L-2')
log = stubLog

-- M4 (docs/V1_4_SPEC.md 3.5): hello.blockSize 512 switches the lattice, the follow table, the layer /
-- flat maps and the rebase staleness rule before the resync; a 512² terrain is accepted and imported
local lattice = require('beamngEarth/lattice')
local rebase = require('beamngEarth/rebase')
veh.vel = vec3(0, 0, 0)
local nA = #received('prepare_anchor')
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, blockSize = 512, serverVersion = '0.4.0', sessionId = 's5',
  farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
T.check(pumpUntil(function() return #received('prepare_anchor') == nA + 1 end, 10), '512: resync requested on hello')
T.check(lattice.N == 512 and lattice.HALF == 512 and follow.GRID == 128 and follow.REACH == 260 and follow.PREPARE_DIST == 128
  and rebase.STALE_DIST == 192, 'hello.blockSize 512: lattice, follow table and staleness rule switched')
local rq = last('prepare_anchor')
T.check(rq.cx % 128 == 0 and rq.cy % 128 == 0 and math.abs(rq.cx - veh.pos.x) <= follow.MIN_LEAD + 64, 'resync hint on the 128 m grid (at most MIN_LEAD ahead)')
local imp512 = calls.importMaps
-- the graded block (terrain_refresh) can come before its anchor's commit: kept and swapped in after it
serverSend({ type = 'terrain_refresh', anchorId = 7, cx = rq.cx, cy = rq.cy, heightmap = '/temp/beamngEarth/s5/hm_7g.png',
  size = 512, squareSize = SQ, zBase = 1120, maxHeight = 1024, q = 1 / 64 })
frames(2)
T.check(calls.importMaps == imp512, 'a refresh for an anchor not committed yet waits')
serverSend({ type = 'anchor_ready', requestId = rq.requestId, anchorId = 7, lat = rq.lat, lon = rq.lon, groundZ = 0,
  terrain = { cx = rq.cx, cy = rq.cy, heightmap = '/temp/beamngEarth/s5/hm_7.png', size = 512, squareSize = SQ,
    zBase = 1120, maxHeight = 1024, q = 1 / 64 } })
T.check(pumpUntil(function() return calls.importMaps >= imp512 + 1 end, 10), '512: the size-512 anchor terrain is accepted and imported')
frames(2)
T.check(calls.importMaps == imp512 + 2 and tb.lastImport.path == '/temp/beamngEarth/s5/hm_7g.png',
  'the graded block that came first is swapped in once its anchor is committed')
T.check(last('commit_anchor').anchorId == 7, '512: resync committed')
T.check(tb.pos.x == rq.cx - 512 and tb.pos.y == rq.cy - 512, '512: block position = (cx-512, cy-512)')
T.check(tb.lastImport.maps[1].path:match('beamngEarth_layer_512%.png$') ~= nil, '512: the 512 layer map with the 512 heightmap')
-- terrain-follow on the 512 table: prepare past 128 m, a 1024² answer is refused
veh.pos = vec3(rq.cx + 140, rq.cy, 1200)
veh.vel = vec3(30, 0, 0)
T.check(pumpUntil(function() return last('prepare_terrain').anchorId == 7 end, 10), '512: terrain-follow prepares past 128 m')
local pt7 = last('prepare_terrain')
T.check(pt7.cx == rq.cx + 384 and pt7.cy == rq.cy, '512: target snap128(p + max(5 s v, MIN_LEAD)) = cx + 384')
serverSend({ type = 'terrain_ready', requestId = pt7.requestId, anchorId = 7, cx = pt7.cx, cy = pt7.cy,
  heightmap = '/temp/beamngEarth/s5/hm_7b.png', size = 1024, squareSize = SQ, zBase = 1120, maxHeight = 1024, q = 1 / 64 })
local statusLog = {}
log = function(level, tag, text) statusLog[#statusLog + 1] = tostring(text) end
frames(3)
log = stubLog
local refused = false
for _, w in ipairs(statusLog) do if w:match('Unexpected terrain_ready %(size 1024%)') then refused = true end end
T.check(refused and #received('prepare_terrain') > 0, 'a 1024² terrain in a 512 session is refused')
-- a level change in a 512 session: the flat start block is the 512² one
core.onClientEndMission()
local impFlat = calls.importMaps
core.onClientStartMission()
T.check(calls.importMaps == impFlat + 1 and tb.lastImport.path:match('beamngEarth_flat_512%.png$') ~= nil,
  '512: level start imports the 512² flat map')
T.check(tb.lastImport.maps[1].path:match('beamngEarth_layer_512%.png$') ~= nil and tb.pos.x == -512 and tb.pos.y == -512,
  '512: flat block 1024 m centred on the origin')
-- a server without hello.blockSize: back to the v1.3.1 block
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, serverVersion = '0.4.0', sessionId = 's6',
  farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
T.check(lattice.N == 1024 and follow.GRID == 256 and follow.REACH == 420 and rebase.STALE_DIST == 450,
  'no hello.blockSize: the 1024² block again')

-- M1h (docs/V1_4_SPEC.md 3.6): hello.horizon sets the sky's planet radius to the tiles' sphere;
-- status.radius drives LevelInfo.visibleDistance (+10 %, hysteresis, 100 km minimum, postApply after
-- every change); the level's values come back at level end and with a server without horizon
local sky = S.newObject('ScatterSky')
sky.atmoPlanetRadius = 6360
sky:registerObject('sunsky')
local postApplies = 0
function scenetree.theLevelInfo:postApply() postApplies = postApplies + 1 end
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and sky.atmoPlanetRadius == 6360 and postApplies == 0,
  'no horizon: the level\'s sky and the 100 km view distance, untouched')
serverSend({ type = 'status', text = 'x', backlog = 0, radius = 300000 })
frames(3)
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and postApplies == 0, 'status.radius without hello.horizon: ignored')
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, horizon = true, serverVersion = '0.4.0',
  sessionId = 's7', farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
T.check(sky.atmoPlanetRadius == 6371, 'hello.horizon: sky planet radius 6371 km')
T.check(scenetree.theLevelInfo.visibleDistance == 330000 and postApplies == 1,
  'the radius already known (300 km): visibleDistance 330 km, with postApply')
serverSend({ type = 'status', text = 'Far field loading', backlog = 5, radius = 310000 })
frames(3)
T.check(scenetree.theLevelInfo.visibleDistance == 330000 and postApplies == 1, '+3 %: within the hysteresis')
serverSend({ type = 'status', text = 'Far field ready', backlog = 0, radius = 545000 })
frames(3)
T.near(scenetree.theLevelInfo.visibleDistance, 599500, 1e-6, 'radius 545 km: 600 km')
T.check(postApplies == 2, 'postApply after the change')
serverSend({ type = 'status', text = 'x', backlog = 0, radius = 80000 })
frames(3)
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and postApplies == 3, 'radius 80 km: the 100 km minimum')
serverSend({ type = 'status', text = 'x', backlog = 0, radius = 400000 })
frames(3)
T.near(scenetree.theLevelInfo.visibleDistance, 440000, 1e-6, '400 km: 440 km')
-- a level change: the sky goes back before the level ends, and is applied again on the next start
core.onClientEndMission()
T.check(sky.atmoPlanetRadius == 6360, 'level end: the level\'s sky planet radius restored')
scenetree.theLevelInfo.visibleDistance = 1000             -- the next level's LevelInfo
core.onClientStartMission()
T.check(sky.atmoPlanetRadius == 6371, 'level start in horizon mode: planet radius applied again')
T.near(scenetree.theLevelInfo.visibleDistance, 440000, 1e-6, 'level start: visibleDistance from the known radius')
-- a server without horizon: the level's values back
local pa = postApplies
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, serverVersion = '0.4.0', sessionId = 's8',
  farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
T.check(sky.atmoPlanetRadius == 6360, 'no hello.horizon: the level\'s sky planet radius back')
T.check(scenetree.theLevelInfo.visibleDistance == 100000 and postApplies == pa + 1, 'no hello.horizon: 100 km back, with postApply')
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, horizon = true, serverVersion = '0.4.0',
  sessionId = 's9', farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
serverSend({ type = 'status', text = 'x', backlog = 0, radius = 500000 })
frames(3)
T.check(sky.atmoPlanetRadius == 6371 and math.abs(scenetree.theLevelInfo.visibleDistance - 550000) < 1e-6, 'horizon again')
-- real weather: core hands the message to the beamngEarth_weather extension (when it is loaded)
serverSend({ type = 'weather', v = 1, code = 3, tempC = 20.5 })
frames(2)                                               -- no extension: ignored
local gotWeather = nil
beamngEarth_weather = { onWeather = function(m) gotWeather = m end }
serverSend({ type = 'weather', v = 1, code = 3, tempC = 20.5 })
frames(2)
T.check(gotWeather and gotWeather.code == 3 and gotWeather.tempC == 20.5, 'weather message handed to beamngEarth_weather')
-- real gravity: the same hand-off; a new session (hello) clears the previous session's value
local gotGravity, cleared = nil, 0
beamngEarth_weather = { onGravity = function(m) gotGravity = m end, clearGravity = function() cleared = cleared + 1 end }
serverSend({ type = 'gravity', v = 1, g = 9.7867, lat = 36.58, lon = -118.29, h = 4400, source = 'egm2008' })
frames(2)
T.check(gotGravity and gotGravity.g == 9.7867 and gotGravity.source == 'egm2008', 'gravity message handed to beamngEarth_weather')
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, horizon = true, serverVersion = '0.4.0',
  sessionId = 's10', farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
T.check(cleared == 1, 'hello: the previous session\'s gravity cleared')
beamngEarth_weather = nil

-- the on-screen data credits (credits.lua): the attribution line until the server's credits arrive,
-- then near + far; one text draw per frame; hidden in the pause menu; a new session clears them
local creditsMod = require('beamngEarth/credits')
creditsMod._reset()
local drawnCredits = {}
ui_imgui.GetMainViewport = function() return { Pos = { x = 0, y = 0 }, Size = { x = 1920, y = 1080 } } end
ui_imgui.GetFont = function() error("GetFont() fails in 0.39") end
ui_imgui.GetFontSize = function() return 16 end
ui_imgui.CalcTextSize = function(t) return { x = #t * 8, y = 16 } end
ui_imgui.ImVec2 = function(x, y) return { x = x, y = y } end
ui_imgui.ImDrawList_AddText1 = function(dl, pos, col, text) drawnCredits[#drawnCredits + 1] = text end
frames(1)
local function words(x) return (x:gsub('%s+', ' ')) end -- the overlay wraps long lines at spaces
T.check(#drawnCredits == 1 and words(drawnCredits[1]) == words(core.attribution()), 'credits: the attribution line before any credits message')
serverSend({ type = 'credits', v = 1, near = 'Imagery: USGS The National Map | Elevation: Mapterhorn (see mapterhorn.com/attribution)',
  far = 'Far imagery: EOX', ids = { 'img:usgs', 'dem:mapterhorn' }, farIds = { 'img:eox2023' } })
drawnCredits = {}
frames(3)
T.check(#drawnCredits == 3, 'credits: one text draw per frame')
T.check(drawnCredits[3] == 'Imagery: USGS The National Map | Elevation: Mapterhorn (see mapterhorn.com/attribution)\nFar imagery: EOX',
  'credits: the server\'s near and far lines')
T.check(core.credits() == drawnCredits[3], 'core.credits(): the text on screen (the settings page)')
core.onUiChangedState('menu', 'play')
drawnCredits = {}
frames(2)
T.check(#drawnCredits == 0, 'credits: hidden while the pause menu is open')
core.onUiChangedState('play', 'menu')
frames(1)
T.check(#drawnCredits == 1, 'credits: back after the pause menu')
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, horizon = true, serverVersion = '0.4.0',
  sessionId = 's11', farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh', attribution = 'Elevation: s11' })
drawnCredits = {}
frames(3)
T.check(drawnCredits[#drawnCredits] == 'Elevation: s11', 'credits: a new session shows its attribution line until its credits arrive')

-- the download cache (server cache.py) for the settings page: hello sends the page's cap, the
-- server's 'cache' is kept (outliving levels) and a clear's answer counts; the requests go out only
-- with a server session; a disconnect or a new session forgets the numbers
local function sentCaches() return received('cache') end
local nCache = #sentCaches()
beamngEarth_settings = { cacheCapGb = function() return 50 end }
serverSend({ type = 'hello', protocol = 4, format = 4, cells = 64, horizon = true, serverVersion = '0.4.0',
  sessionId = 's12', farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'mesh' })
frames(3)
T.check(#sentCaches() == nCache + 1 and sentCaches()[nCache + 1].capGb == 50, 'hello: the page\'s cache cap sent')
T.check(core.cacheState() == nil, 'no cache numbers before the server sends them')
local okClear, whyClear = core.cacheClear()
T.check(not okClear and whyClear == 'the server has no download cache' and #sentCaches() == nCache + 1,
  'clear: not sent to a server without a cache (no answer would come; the page would wait 2 min)')
serverSend({ type = 'cache', v = 1, usedBytes = 1234567890, capBytes = 50000000000, files = 321, ready = true, managed = true })
frames(2)
local cs = core.cacheState()
T.check(cs and cs.usedBytes == 1234567890 and cs.capBytes == 50e9 and cs.files == 321 and cs.ready and cs.managed
  and cs.clears == 0 and cs.clearedBytes == nil, 'cache message kept for the page')
T.check(core.cacheSetCap(0) and core.cacheClear() and core.cacheQuery(), 'cache requests sent while connected')
local sc = sentCaches()
T.check(sc[#sc - 2].capGb == 0 and sc[#sc - 1].clear == true and sc[#sc].capGb == nil and sc[#sc].clear == nil,
  'cache requests: cap (0 = unlimited), clear, a plain usage request')
serverSend({ type = 'cache', v = 1, usedBytes = 200000000, capBytes = 0, files = 12, ready = true, managed = true,
  clearedBytes = 1034567890 })
frames(2)
cs = core.cacheState()
T.check(cs.clears == 1 and cs.clearedBytes == 1034567890 and cs.usedBytes == 2e8, 'a clear\'s answer counted')
serverSend({ type = 'cache', v = 1, usedBytes = 210000000, capBytes = 0, files = 13, ready = true, managed = true })
frames(2)
T.check(core.cacheState().clears == 1 and core.cacheState().clearedBytes == 1034567890, 'later usage keeps the last clear')
core.reconnect()
T.check(core.cacheState() == nil and not core.cacheClear(), 'disconnected: numbers gone, no request sent')
beamngEarth_settings = nil

core.onExtensionUnloaded()
T.check(sky.atmoPlanetRadius == 6360 and scenetree.theLevelInfo.visibleDistance == 100000,
  'unload: the level keeps running with its own sky and the 100 km view distance')
T.check(scenetree.findObject('bngEarthTerrain') == nil, 'terrain deleted on unload')
T.check(#S.liveOf('Material') == 0 and #S.liveOf('TSStatic') == 0, 'far tiles and their materials deleted on unload')

T.done('test_core_stubbed')
