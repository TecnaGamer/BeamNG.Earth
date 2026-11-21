-- BeamNG.Earth v1.4 M4 in-game probes (docs/V1_4_SPEC.md 3.5 "Prerequisite probes", section 7):
--   P2 (extended)  512² re-import into the live 1024² block and back: first and steady-state import
--                  ms with and without the layer map, reloadCollision ms, the collidable extent by
--                  castRayStatic ([c-512, c+510] after the switch, [c-1024, c+1022] after the way
--                  back), heights unchanged where the blocks overlap, setRenderEnabled(false) kept
--                  across importMaps.
--   P7 (a)         timing wrapper over the rebase / commit parts (terrain.load, farfield.transform,
--                  farfield.switchAnchor, farfield.onBlockCommit, camshift.shift, rebase.moveVehicles;
--                  reloadCollision = the gap to the next wrapped call), with frame numbers and the
--                  wall ms of every frame, over a drive with rebases (tools/drive.py).
--   P7 (b)         double-buffered terrain: a second, hidden TerrainBlock imported ahead (+DZ m so a
--                  ray tells the blocks apart), whether it collides before / after reloadCollision,
--                  and the swap (delete the live block, rename, reloadCollision) cost and result.
--   P11            a commit without reloadCollision: does physics (rays, the parked car) follow a
--                  re-imported block by itself?
-- Driven by tools/m4_probes.py (it sends this chunk with MCP run_lua, then calls BNGE_M4.<step>(cfg)
-- and evaluates the pass criteria). Every step returns JSON. Run ONLY while the user is not playing:
-- P2 / P7(b) pause physics for a few seconds, P11 lifts the parked car by DZ. Every step that
-- changes the terrain ends by re-importing the live block's own heightmap at its own position
-- (BNGE_M4.restore), and reports the heights before / after so a bad restore is visible.
-- The camera is never touched; the player's inputs are zeroed before any terrain change.

local M = rawget(_G, 'BNGE_M4')
if type(M) == 'table' and M.p7aUninstall then pcall(M.p7aUninstall) end
M = {}
_G.BNGE_M4 = M

local RAY_UP, RAY_LEN = 400, 3000
local NAME = 'bngEarthTerrain'
local ZERO_INPUTS = "input.event('throttle', 0, 2); input.event('brake', 0, 2); input.event('steering', 0, 2); "
  .. "input.event('clutch', 0, 2); input.event('parkingbrake', 1, 2)"

local function toJson(v)
  local je = rawget(_G, 'jsonEncode')
  if je then return je(v) end
  return dumps(v)
end

local function decode(cfg)
  if type(cfg) == 'string' then return jsonDecode(cfg) end
  return cfg or {}
end

-- A monotonic wall clock in ms (hptimer, as the mod's stopwatch.lua).
local function newClock()
  local hp = rawget(_G, 'hptimer')
  if hp then
    local ok, t = pcall(hp)
    if ok and t and t.stopAndReset then
      local acc = 0
      t:stopAndReset()
      return function() acc = acc + t:stopAndReset(); return acc end
    end
  end
  local socket = require('socket')
  local t0 = socket.gettime()
  return function() return (socket.gettime() - t0) * 1000 end
end
local clock = newClock()

local function vehicle() return getPlayerVehicle(0) end

local function zeroInputs()
  local veh = vehicle()
  if veh then pcall(veh.queueLuaCommand, veh, ZERO_INPUTS) end
end

-- Static-collision ground height under (x, y) (the TerrainBlock; far tiles are not collidable), or nil.
local function ray(x, y, zTop, len)
  len = len or RAY_LEN
  local d = castRayStatic(vec3(x, y, zTop), vec3(0, 0, -1), len)
  if d and d < len then return zTop - d end
end

-- A ray start above anything the block can hold (zBase + maxHeight) and its length to below zBase.
local function above(o) return o.z + o.maxH + 50, o.maxH + 150 end

local function coreStatus()
  local core = rawget(_G, 'beamngEarth_core')
  if not core or not core.status then return nil end
  local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
  return ok and st or nil
end

local function physicsRunning()
  local ok, v = pcall(function() return be:getPhysicsRunning() end)
  if ok and type(v) == 'boolean' then return v end
  return true
end

local function renderFlag(tb)
  local ok, v = pcall(function() return tb:isRenderEnabled() end)
  if ok then return v end
  return 'n/a'
end

local function timed(fn)
  local t0 = clock()
  local ok, r = pcall(fn)
  return clock() - t0, ok, r
end

-- importMaps as terrain.lua does it (layer: the layer map path, or nil = no material list)
local function import(tb, path, maxH, layer)
  local mats, maps = {}, {}
  if layer then
    mats = { 'beamngEarthDirt' }
    maps = { { path = layer, selected = false, material = 'beamngEarthDirt', materialId = 0, channel = 'R', channelId = 0 } }
  end
  local ms, ok, r = timed(function() return tb:importMaps(path, 2.0, maxH, '', mats, maps, false) end)
  return ms, ok and r ~= false, (not ok) and tostring(r) or nil
end

local function reload()
  local ms = timed(function() be:reloadCollision() end)
  return ms
end

-- Ray heights at points relative to a centre: { {dx, dy}, ... } -> { {x, y, h|false}, ... }
local function rays(cx, cy, o, pts)
  local zTop, len = above(o)
  local out = {}
  for _, p in ipairs(pts) do
    local x, y = cx + p[1], cy + p[2]
    out[#out + 1] = { x = x, y = y, dx = p[1], dy = p[2], h = ray(x, y, zTop, len) or false }
  end
  return out
end

-- The extent probes for a block of half size h around (cx, cy): inside the collidable area
-- [c-h, c+h-2] near each edge, and just outside it (the 2 m N/E strip and beyond the S/W edge).
local function extentPoints(h)
  return {
    { 0, 0 }, { -h + 1, 0 }, { h - 3, 0 }, { 0, -h + 1 }, { 0, h - 3 }, { -h + 1, -h + 1 }, { h - 3, h - 3 },
    { h - 1, 0 }, { 0, h - 1 }, { -h - 1, 0 }, { 0, -h - 1 }, { h + 50, 0 },
  }
end

-- Heights of the live block at lattice points (tb:getHeight: the terrain data, exactly at samples).
local function latticeHeights(tb, x0, y0, n)
  local out = {}
  for _, g in ipairs({ { 1, 1 }, { n / 2, n / 2 }, { n - 2, 7 }, { 13, n - 3 }, { n / 4, 3 * n / 4 }, { 3 * n / 4, n / 4 + 5 } }) do
    local x, y = x0 + 2 * g[1], y0 + 2 * g[2]
    local ok, h = pcall(function() return tb:getHeight(vec3(x, y, 0)) end)
    out[#out + 1] = { gx = g[1], gy = g[2], x = x, y = y, h = ok and h or false }
  end
  return out
end

------------------------------------------------------------------------------------------
-- info: the live block and the preconditions (no change to the game)
function M.info()
  local tb = scenetree.findObject(NAME)
  local st = coreStatus()
  local veh = vehicle()
  local out = { mod = st ~= nil, terrain = tb ~= nil, physicsRunning = physicsRunning() }
  if st then
    out.anchorId, out.block, out.blockSize, out.nearVisual = st.anchorId, st.block, st.blockSize or 1024, st.nearVisual
    local live = beamngEarth_core._state and beamngEarth_core._state().blockMsg
    out.heightmap = live and live.heightmap
    out.session = st.session
  end
  if veh then
    local p, v = veh:getPosition(), veh:getVelocity()
    out.car = { x = p.x, y = p.y, z = p.z, speed = v:length() }
  end
  if tb then
    local p = tb:getPosition()
    out.tb = { x = p.x, y = p.y, z = p.z, render = renderFlag(tb) }
    pcall(function() out.tb.heightScaleUser = tb:getHeightScaleUser() end)
    pcall(function() out.tb.squareSize = tb:getSquareSize() end)
    local n = (st and st.blockSize) or 1024
    out.samples = latticeHeights(tb, p.x, p.y, n)
  end
  return toJson(out)
end

------------------------------------------------------------------------------------------
-- restore: the live block's own heightmap at its own position, collision rebuilt, render flag as it was
-- cfg: { orig = {path, maxH, x, y, z, render, layer} }
local function restoreTable(cfg)
  local o = cfg.orig
  local tb = scenetree.findObject(NAME)
  if not tb then return { error = 'no ' .. NAME } end
  local ms, ok, err = import(tb, o.path, o.maxH, o.layer)
  tb:setPosition(vec3(o.x, o.y, o.z))
  if o.render ~= 'n/a' then pcall(function() tb:setRenderEnabled(o.render) end) end
  local rl = reload()
  return { importMs = ms, ok = ok, error = err, reloadMs = rl, render = renderFlag(tb) }
end

-- the player's car, deactivated by p11b, back once the collision is rebuilt (restoreTable)
local function unfreezeCar(r)
  local id = M.frozenCar
  if not id then return end
  M.frozenCar = nil
  local v = be:getObjectByID(id)
  if v then v:setActive(1) end
  r.carUnfrozen = v ~= nil
end

function M.restore(cfg)
  cfg = decode(cfg)
  local r = restoreTable(cfg)
  unfreezeCar(r)
  if cfg.resume ~= false then be:setPhysicsRunning(true) end
  return toJson(r)
end

------------------------------------------------------------------------------------------
-- P2 (extended). cfg: { orig = {...}, hm512 = path, layer512 = path, cx, cy, steady = 5 }
-- p2a: physics paused, first 512² import into the live 1024² block, same-frame rays
function M.p2a(cfg)
  cfg = decode(cfg)
  local tb = scenetree.findObject(NAME)
  if not tb then return toJson({ error = 'no ' .. NAME }) end
  zeroInputs()
  M.p2 = { wasRunning = physicsRunning() }
  be:setPhysicsRunning(false)
  local cx, cy, o = cfg.cx, cfg.cy, cfg.orig
  local out = { before = rays(cx, cy, o, extentPoints(1024)), before512 = rays(cx, cy, o, extentPoints(512)) }
  out.renderBefore = renderFlag(tb)
  local ms, ok, err = import(tb, cfg.hm512, o.maxH, cfg.layer512)
  out.first512 = { importMs = ms, ok = ok, error = err }
  out.renderAfterImport = renderFlag(tb)             -- before anything re-applies it
  tb:setPosition(vec3(cx - 512, cy - 512, o.z))
  out.first512.reloadMs = reload()
  out.sameFrame = rays(cx, cy, o, extentPoints(512))
  return toJson(out)
end

-- p2b (a later frame): rays again, steady-state imports, the way back to 1024², restore
function M.p2b(cfg)
  cfg = decode(cfg)
  local tb = scenetree.findObject(NAME)
  if not tb then return toJson({ error = 'no ' .. NAME }) end
  local cx, cy, o = cfg.cx, cfg.cy, cfg.orig
  local out = { laterFrame = rays(cx, cy, o, extentPoints(512)) }
  local okRun, e = pcall(function()
    local n = tonumber(cfg.steady) or 5
    local function series(path, layer, x0, y0, count)
      local r = {}
      for i = 1, count do
        local ms, ok, err = import(tb, path, o.maxH, layer)
        tb:setPosition(vec3(x0, y0, o.z))
        r[i] = { importMs = ms, ok = ok, error = err, reloadMs = reload() }
      end
      return r
    end
    out.steady512Layer = series(cfg.hm512, cfg.layer512, cx - 512, cy - 512, n)
    out.steady512NoLayer = series(cfg.hm512, nil, cx - 512, cy - 512, n)
    out.renderAfter512 = renderFlag(tb)
    out.back1024 = series(o.path, o.layer, o.x, o.y, 1)[1]
    out.back1024Rays = rays(cx, cy, o, extentPoints(1024))
    out.steady1024Layer = series(o.path, o.layer, o.x, o.y, math.max(2, math.floor(n / 2)))
  end)
  if not okRun then out.error = tostring(e) end
  out.restore = restoreTable(cfg)
  out.after = rays(cx, cy, o, extentPoints(1024))
  be:setPhysicsRunning(M.p2 and M.p2.wasRunning ~= false)
  zeroInputs()
  return toJson(out)
end

------------------------------------------------------------------------------------------
-- P11: physics running, car parked near the block centre (parking brake on, inputs zero).
-- cfg: { orig = {...}, dz = 0.3, hm512, layer512, cx, cy }
-- p11a: re-import the live block's heightmap DZ higher, no reloadCollision
function M.p11a(cfg)
  cfg = decode(cfg)
  local tb = scenetree.findObject(NAME)
  local veh = vehicle()
  if not tb or not veh then return toJson({ error = 'no terrain / vehicle' }) end
  zeroInputs()
  be:setPhysicsRunning(true)
  local o, dz = cfg.orig, tonumber(cfg.dz) or 0.3
  local p = veh:getPosition()
  local out = { carBefore = { p.x, p.y, p.z }, rayBefore = ray(p.x, p.y, p.z + RAY_UP) or false }
  local ms, ok, err = import(tb, o.path, o.maxH, o.layer)
  tb:setPosition(vec3(o.x, o.y, o.z + dz))
  if o.render ~= 'n/a' then pcall(function() tb:setRenderEnabled(o.render) end) end
  out.importMs, out.ok, out.error = ms, ok, err
  out.raySameFrame = ray(p.x, p.y, p.z + RAY_UP) or false
  M.p11 = { x = p.x, y = p.y, z = p.z, t0 = clock() }
  return toJson(out)
end

-- p11sample (called by the tool at ~0.25, 0.5, 1, 2 s): the ray at the car's start and the car's z
function M.p11sample()
  local veh = vehicle()
  local s = M.p11
  if not veh or not s then return toJson({ error = 'p11a first' }) end
  local p = veh:getPosition()
  return toJson({ ms = clock() - s.t0, ray = ray(s.x, s.y, s.z + RAY_UP) or false, carZ = p.z,
    carMoved = math.sqrt((p.x - s.x) ^ 2 + (p.y - s.y) ^ 2) })
end

-- p11b: the 512² crop at the same centre, no reloadCollision: does the area the block no longer covers
-- (between c+512 and c+1022) stop colliding by itself? Then rays at once; the tool samples again.
-- The player's car is deactivated first and comes back in restore: without reloadCollision physics
-- keeps the 1024² block's origin and size over the 512² data, so the ground under a car anywhere
-- east / north of the centre is gone (in-game 2026-10-10: the parked car 258 m from it fell through).
function M.p11b(cfg)
  cfg = decode(cfg)
  local tb = scenetree.findObject(NAME)
  if not tb then return toJson({ error = 'no ' .. NAME }) end
  zeroInputs()
  local veh = vehicle()
  if veh and not M.frozenCar then
    M.frozenCar = veh:getId()
    veh:setActive(0)
  end
  local o, cx, cy = cfg.orig, cfg.cx, cfg.cy
  local out = { before = rays(cx, cy, o, { { 0, 0 }, { 700, 0 }, { 0, -700 } }) }
  local ms, ok, err = import(tb, cfg.hm512, o.maxH, cfg.layer512)
  tb:setPosition(vec3(cx - 512, cy - 512, o.z))
  if o.render ~= 'n/a' then pcall(function() tb:setRenderEnabled(o.render) end) end
  out.importMs, out.ok, out.error = ms, ok, err
  out.sameFrame = rays(cx, cy, o, { { 0, 0 }, { 700, 0 }, { 0, -700 } })
  M.p11b = { cx = cx, cy = cy, o = o, t0 = clock() }
  return toJson(out)
end

function M.p11bSample()
  local s = M.p11b
  if not s then return toJson({ error = 'p11b first' }) end
  return toJson({ ms = clock() - s.t0, rays = rays(s.cx, s.cy, s.o, { { 0, 0 }, { 700, 0 }, { 0, -700 } }) })
end

------------------------------------------------------------------------------------------
-- P7 (a): wrap the parts of a rebase / terrain-follow commit (module fields: core.lua looks them up
-- at call time), count frames with an extension proxy, keep every call and every frame's wall ms.
local WRAP = {
  { 'beamngEarth/terrain', 'load' }, { 'beamngEarth/farfield', 'transform' }, { 'beamngEarth/farfield', 'switchAnchor' },
  { 'beamngEarth/farfield', 'onBlockCommit' }, { 'beamngEarth/camshift', 'shift' }, { 'beamngEarth/rebase', 'moveVehicles' },
  { 'beamngEarth/net', 'send' },
}
local MAX_CALLS, MAX_FRAMES = 20000, 200000

function M.p7aInstall()
  if M.p7a then return toJson({ error = 'already installed' }) end
  if not rawget(_G, 'newExtensionProxy') then return toJson({ error = 'newExtensionProxy missing' }) end
  local P = { calls = {}, frames = {}, frame = 0, orig = {}, last = clock() }
  M.p7a = P
  local ok, err = pcall(function()
    for _, w in ipairs(WRAP) do
      local mod = package.loaded[w[1]]
      local orig = mod and mod[w[2]]
      if type(orig) == 'function' then
        P.orig[#P.orig + 1] = { mod = mod, key = w[2], fn = orig }
        local name = w[1]:match('/(.*)$') .. '.' .. w[2]
        local isSend = w[2] == 'send'
        mod[w[2]] = function(...)
          local t0 = clock()
          local a, b, c = orig(...)
          if #P.calls < MAX_CALLS then
            local msg = isSend and select(1, ...) or nil
            -- net.send: only the commit messages (their start marks the end of the reloadCollision before them)
            if not isSend or (type(msg) == 'table' and (msg.type == 'commit_anchor' or msg.type == 'commit_terrain')) then
              P.calls[#P.calls + 1] = { name = isSend and ('send.' .. msg.type) or name, frame = P.frame, t0 = t0, t1 = clock() }
            end
          end
          return a, b, c
        end
      end
    end
    local sink = {}
    function sink:onPreRender()
      local now = clock()
      if #P.frames < MAX_FRAMES then P.frames[#P.frames + 1] = now - P.last end
      P.last = now
      P.frame = P.frame + 1
    end
    P.proxy = newExtensionProxy(nil, 'bngeM4P7a')
    P.proxy:submitEventSinks({ sink }, {})
  end)
  if not ok then
    pcall(M.p7aUninstall)
    return toJson({ error = 'install: ' .. tostring(err) })
  end
  -- reloadCollision alone in this scene, for reference (3 calls)
  local rl = {}
  for i = 1, 3 do rl[i] = reload() end
  return toJson({ ok = true, wrapped = #P.orig, reloadCollisionMs = rl })
end

-- The records since the last read (frame = the onPreRender count when the call ran; frames: wall ms
-- of every frame since install, index = frame number + 1).
function M.p7aRead()
  local P = M.p7a
  if not P then return toJson({ error = 'not installed' }) end
  -- frames[i] is the wall ms of frame number frameBase + i (the interval that ends at that onPreRender;
  -- a call made with frame = f ran in frame number f + 1)
  local out = { calls = P.calls, frames = P.frames, frame = P.frame, frameBase = P.frameBase or 0 }
  P.calls, P.frames = {}, {}
  P.frameBase = P.frame
  return toJson(out)
end

function M.p7aUninstall()
  local P = M.p7a
  if not P then return 'not installed' end
  if P.proxy then pcall(function() P.proxy:destroy() end) end
  for _, o in ipairs(P.orig) do o.mod[o.key] = o.fn end
  M.p7a = nil
  return 'uninstalled'
end

------------------------------------------------------------------------------------------
-- P7 (b): double-buffered terrain. cfg: { orig = {...}, hm512, layer512, cx, cy, dz = 5 }
-- Physics paused; the new block B is the 512² crop DZ higher (a ray tells the blocks apart).
function M.p7b(cfg)
  cfg = decode(cfg)
  local A = scenetree.findObject(NAME)
  if not A then return toJson({ error = 'no ' .. NAME }) end
  zeroInputs()
  local wasRunning = physicsRunning()
  be:setPhysicsRunning(false)
  local o, cx, cy, dz = cfg.orig, cfg.cx, cfg.cy, tonumber(cfg.dz) or 5
  local pts = { { 0, 0 }, { -511, 0 }, { 509, 0 }, { 0, 509 }, { 700, 0 } }
  local out = { before = rays(cx, cy, o, pts) }
  local B
  local okRun, e = pcall(function()
    local ms
    ms = timed(function()
      B = TerrainBlock()
      B:setName('bngEarthTerrainM4B')
      B:registerObject('bngEarthTerrainM4B')
      B.canSave = false
      B:setTerrFileLvlFolder('/temp/beamngEarth/') -- as terrain.lua: B may become the live block
      B:setField('materialTextureSet', 0, 'beamngEarthTerrainMaterialTextureSet')
      scenetree.MissionGroup:addObject(B)
      pcall(function() B:addMaterial('beamngEarthDirt', -1); B:updateMaterial(0, 'beamngEarthDirt') end)
      pcall(function() B:setRenderEnabled(false) end)
    end)
    out.createMs = ms
    local ims, ok, err = import(B, cfg.hm512, o.maxH, cfg.layer512)
    B:setPosition(vec3(cx - 512, cy - 512, o.z + dz))
    pcall(function() B:setRenderEnabled(false) end)
    out.hiddenImport = { importMs = ims, ok = ok, error = err }
    out.withBNoReload = rays(cx, cy, o, pts)        -- A's heights expected (B must not collide yet)
    out.reloadWithBMs = reload()
    out.withBReload = rays(cx, cy, o, pts)          -- still A's? (a second block does not collide, ARCHITECTURE.md)
    -- the swap: A goes, B takes its name, one collision rebuild
    out.swapMs = timed(function()
      A:delete()
      B:setName(NAME)
      be:reloadCollision()
    end)
    out.afterSwap = rays(cx, cy, o, pts)            -- B's heights (+dz) expected, c+700 a miss
    out.bRender = renderFlag(B)
  end)
  if not okRun then out.error = tostring(e) end
  -- restore: whatever block carries the name now (B, or A if the swap failed) gets the original back;
  -- a leftover second block is deleted
  local leftover = scenetree.findObject('bngEarthTerrainM4B')
  if leftover then pcall(function() leftover:delete() end) end
  if not scenetree.findObject(NAME) then
    -- no live block at all (A deleted, B gone): the mod's terrain.load recreates it
    local T = package.loaded['beamngEarth/terrain']
    if T and T.load then pcall(T.load, o.path, 2.0, o.maxH, o.x, o.y, o.z) end
  end
  out.restore = restoreTable(cfg)
  out.after = rays(cx, cy, o, pts)
  be:setPhysicsRunning(wasRunning)
  zeroInputs()
  return toJson(out)
end

return 'ok'
