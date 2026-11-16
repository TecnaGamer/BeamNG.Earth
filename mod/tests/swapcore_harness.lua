-- The core.lua harness of the generation swap's integration tests (test_swap_core*.lua): test_core_robustness.lua's
-- stubbed engine, vehicle, ground and fake socket (generated from it: its lines between the stubs and its first
-- check), then a connected session with anchor 1 committed on its first block. Returns what the tests drive it with.
return function(dir, T, S)
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


  -- the terrain imports the mod makes (the heightmap of each), and one that can be made to fail
  local terrain = require('beamngEarth/terrain')
  local loads, failing = {}, {}
  local realLoad = terrain.load
  terrain.load = function(path, ...)
    if failing[path] then return false, 'unreadable heightmap' end
    loads[#loads + 1] = path
    return realLoad(path, ...)
  end
  core.onExtensionLoaded()
  setGround(0, 0)
  frames(3)
  server.down = false
  assert(pumpUntil(function() return #received('hello') == 1 end, 200), 'hello')
  serverSend({ type = 'hello', protocol = 4, format = 4, serverVersion = '0.4.0', sessionId = 's1', farFormat = 'cdae',
    material = 'bngEarthFar_v3', nearVisual = 'mesh' })
  assert(pumpUntil(function() return #received('prepare_anchor') == 1 end, 200), 'prepare_anchor')
  local req = received('prepare_anchor')[1]
  local function block(name)
    return { cx = 0, cy = 0, heightmap = '/temp/beamngEarth/s1/' .. name .. '.png', size = 1024, squareSize = 2,
      zBase = 0, maxHeight = 1024, q = 1 / 64 }
  end
  serverSend({ type = 'anchor_ready', requestId = req.requestId, anchorId = 1, lat = req.lat, lon = req.lon, groundZ = 0,
    terrain = block('hm_1_0_0') })
  assert(pumpUntil(function() return #received('commit_anchor') == 1 end, 200), 'commit_anchor')
  frames(5)
  return { core = core, st = st, frame = frame, frames = frames, pumpUntil = pumpUntil, server = server,
    serverSend = serverSend, received = received, last = last, veh = veh, sim = simTimeAuthority, loads = loads,
    failing = failing, block = block, DT = DT, roads = require('beamngEarth/roads'),
    swapgen = require('beamngEarth/swapgen') }
end
