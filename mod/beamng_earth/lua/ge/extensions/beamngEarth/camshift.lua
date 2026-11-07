-- Carry the game camera across an origin rebase without any visible motion (docs/V1_1_SPEC.md 1b).
--
-- Why the orbit camera swung in front of the car (v1): core/cameraModes/orbit.lua keeps WORLD-space
-- history - camLastPos2 (a point trailing the car at `relaxation` metres), camLastPosPerp,
-- camLastTargetPos(2), camLastPos. Each frame it aims along  dir = targetPos - camLastPos2
-- (orbit.lua:399-412) and puts the camera at -dir * dist. After the vehicle jumped by D, that vector
-- became ~D: pointing back towards the old position, i.e. against the direction of travel, so the
-- camera sat in front of the car until the trailing point was dragged around again.
-- chase.lua does the same with lastDataPos (its velocity estimate, chase.lua:194) and flips to the
-- reverse view; driver.lua (prevCarPos), external.lua (camPos/lastCamPos/carPos/...), droneChase.lua
-- (pos), topDown.lua (posSmoothX/Y/Z), smooth.lua filter (posX/Y/Z springs), transition.lua
-- (camLastPosAbs), the collision filter (lastNearClipCenter) and free.lua (pos) all hold world
-- positions as well.
--
-- No engine hook re-seats them: vehicle resets call cam:reset() (camera.lua:1396-1406), which
-- snaps orbit/chase back to their DEFAULT rotation and distance (orbit.lua:128-134, 259-339), and
-- setClusterPosRelRot fires only 'onSetClusterPosRelRot' (main.lua:1103), which no camera handles.
-- The core teleport detector objectTeleported() (common/utils.lua:1452, called by
-- cameraUpdate.lua:160) only zeroes camData.vel and lets a few modes snap; orbit just resets its
-- pitch smoothing (orbit.lua:273-276).
--
-- So the rebase transforms the camera STATE itself, with the same p' = R*p + t as the world:
--  1. every camera instance of every context - vehicle cameras (ctx.vehicleCamerasCache, or
--     core_camera.getCameraDataById(vid), camera.lua:775) and global cameras
--     (core_camera.getGlobalCameras(), camera.lua:150) - is walked recursively (filters such as
--     external's smooth/handheld are nested camera objects). Every vec3 field that is a world
--     position near the player, and every X/Y/Z triple of scalar smoothers (filters.lua: `.state`
--     for temporal smoothers/springs, `.st` for exponential ones) that is one, is mapped. Offsets,
--     rotations, directions and velocities are small vectors near the origin; the rebase only
--     happens >= 2 km from it, so "within RADIUS of the player" separates them reliably.
--     The free camera's position is always mapped, wherever it is. The 3D map's orbit target
--     (earthGlobe.target) the same: it is the ground being looked at, often kilometres from the car.
--  2. the per-context frame data in core/cameraUpdate.lua (camData.pos/prevPos/vehPos/prevVehPos,
--     finalCameraData.pos, lastValidData.pos) is mapped too, so objectTeleported() sees continuous
--     motion and camData.vel stays valid (no one-frame dynamic-FOV dip, no pitch-smoothing reset).
--     That table is module-local; it is reached through debug.getupvalue on
--     cameraUpdate.onPreRender (`contexts`). If that is not possible the shift still works; only the
--     teleport flag fires for one frame.
-- Rotations stored by the modes are not turned: they are re-derived from the vehicle every frame,
-- and the free camera's 0.09 deg (at 10 km) is invisible.
--
-- It must run in the frame in which the camera first sees the vehicle at its new position, before
-- core_camera's onPreRender; core.lua takes care of that.

local M = {}

M.RADIUS_MIN, M.RADIUS_MAX = 1000, 5000

local ffiOk, ffi = pcall(require, 'ffi')
local vecCType = nil
if ffiOk and ffi then
  local ok, ct = pcall(ffi.typeof, 'struct __luaVec3_t') -- mathlib.lua's vec3 (LuaVec3.__mul checks this type)
  if ok then vecCType = ct end
end

local function isVec(v)
  local tv = type(v)
  if tv == 'table' then
    -- a quaternion has a w
    return type(v.x) == 'number' and type(v.y) == 'number' and type(v.z) == 'number' and v.w == nil
  elseif tv == 'cdata' then
    return vecCType ~= nil and ffi.istype(vecCType, v)
  end
  return false
end

local function smootherValue(s)
  if type(s) ~= 'table' then return nil end
  if type(s.state) == 'number' then return 'state', s.state end
  if type(s.st) == 'number' then return 'st', s.st end
  return nil
end

-- A shift job: the transform, the player's old position (the pivot of "near the player") and stats.
local function newJob(xf, px, py, pz)
  local radius = math.max(M.RADIUS_MIN, math.min(M.RADIUS_MAX, 0.5 * math.sqrt(px * px + py * py)))
  return { xf = xf, px = px, py = py, pz = pz, r2 = radius * radius, seen = {}, points = 0, triples = 0 }
end

local function near(job, x, y, z)
  local dx, dy, dz = x - job.px, y - job.py, z - job.pz
  return dx * dx + dy * dy + dz * dz < job.r2
end

local function mapVec(job, v, force)
  if job.seen[v] then return end
  job.seen[v] = true
  if not force and not near(job, v.x, v.y, v.z) then return end
  v.x, v.y, v.z = job.xf:apply(v.x, v.y, v.z)
  job.points = job.points + 1
end

local function walk(job, obj, depth)
  if job.seen[obj] or depth > 4 then return end
  job.seen[obj] = true
  -- collect first: fields are modified while iterating otherwise
  local keys = {}
  for k, v in pairs(obj) do
    if type(k) == 'string' and (type(v) == 'table' or type(v) == 'cdata') then keys[#keys + 1] = k end
  end
  -- pass 1: X/Y/Z triples of scalar smoothers holding a world position (before anything walks
  -- into those smoother tables and marks them seen)
  for _, k in ipairs(keys) do
    local base = k:match('^(.*)X$')
    local sx, sy, sz = obj[k], base and obj[base .. 'Y'], base and obj[base .. 'Z']
    local fx, x = smootherValue(sx)
    local fy, y = smootherValue(sy)
    local fz, z = smootherValue(sz)
    if fx and fy and fz and not job.seen[sx] and not job.seen[sy] and not job.seen[sz] then
      job.seen[sx], job.seen[sy], job.seen[sz] = true, true, true
      if near(job, x, y, z) then
        sx[fx], sy[fy], sz[fz] = job.xf:apply(x, y, z)
        job.triples = job.triples + 1
      end
    end
  end
  -- pass 2: vec3 fields, and nested camera/filter objects (plus plain sub-tables of a camera)
  for _, k in ipairs(keys) do
    local v = obj[k]
    if isVec(v) then
      mapVec(job, v)
    elseif type(v) == 'table' and not job.seen[v] and (getmetatable(v) ~= nil or depth < 2) then
      walk(job, v, depth + 1)
    end
  end
end

-- `contexts` of core/cameraUpdate.lua, or nil.
local function findContexts()
  local cu = package.loaded['core/cameraUpdate']
  local dbg = rawget(_G, 'debug')
  if type(cu) ~= 'table' or type(cu.onPreRender) ~= 'function' or not dbg or not dbg.getupvalue then return nil end
  for i = 1, 200 do
    local ok, name, val = pcall(dbg.getupvalue, cu.onPreRender, i)
    if not ok or not name then break end
    if name == 'contexts' and type(val) == 'table' then return val end
  end
  return nil
end

local function mapCamData(job, ctx)
  local cd = ctx.camData
  if type(cd) == 'table' then
    for _, k in ipairs({ 'pos', 'prevPos', 'vehPos', 'prevVehPos' }) do
      if isVec(cd[k]) then mapVec(job, cd[k], true) end
    end
    -- res is rebuilt every frame; keep its vectors out of the generic walk
    if type(cd.res) == 'table' then job.seen[cd.res] = true end
    for _, k in ipairs({ 'resPos', 'resTargetPos' }) do if cd[k] then job.seen[cd[k]] = true end end
    job.seen[cd] = true
  end
  for _, k in ipairs({ 'finalCameraData', 'lastValidData' }) do
    local t = ctx[k]
    if type(t) == 'table' and isVec(t.pos) then mapVec(job, t.pos, true) end
  end
end

local function walkGlobal(job, cams)
  if type(cams) ~= 'table' then return end
  local free = cams.free
  if type(free) == 'table' and isVec(free.pos) then mapVec(job, free.pos, true) end
  -- before the walk: a far target would be marked seen and left in the old frame
  local globe = cams.earthGlobe
  if type(globe) == 'table' and isVec(globe.target) then
    mapVec(job, globe.target, true)
    globe.groundSmooth = nil          -- (that z was the old frame's ground; the next pose takes the new one)
  end
  for _, cam in pairs(cams) do
    if type(cam) == 'table' then walk(job, cam, 1) end
  end
end

local function walkVehicleData(job, vdatas)
  if type(vdatas) ~= 'table' then return end
  for _, vdata in pairs(vdatas) do
    if type(vdata) == 'table' and type(vdata.cameras) == 'table' then
      for _, cam in pairs(vdata.cameras) do
        if type(cam) == 'table' then walk(job, cam, 1) end
      end
    end
  end
end

-- Map the whole camera state through xf (p' = R*p + t). (px, py, pz) = the player's position in the
-- old frame. Returns a stats table { points, triples, contexts, viaDebug }.
function M.shift(xf, px, py, pz)
  local job = newJob(xf, px, py, pz)
  local stats = { contexts = 0, viaDebug = false }
  local contexts = findContexts()
  if contexts then
    stats.viaDebug = true
    for _, ctx in pairs(contexts) do
      if type(ctx) == 'table' then
        stats.contexts = stats.contexts + 1
        mapCamData(job, ctx)
        walkVehicleData(job, ctx.vehicleCamerasCache)
        walkGlobal(job, ctx.globalCamerasCache)
      end
    end
  elseif core_camera then
    -- public accessors (main context only)
    stats.contexts = 1
    local vdatas = {}
    if core_camera.getCameraDataById then
      for _, veh in ipairs(getAllVehicles and getAllVehicles() or {}) do
        local cams = core_camera.getCameraDataById(veh:getID())
        if cams then vdatas[#vdatas + 1] = { cameras = cams } end
      end
    end
    walkVehicleData(job, vdatas)
    if core_camera.getGlobalCameras then walkGlobal(job, core_camera.getGlobalCameras()) end
  end
  stats.points, stats.triples = job.points, job.triples
  return stats
end

M._isVec = isVec -- for tests

return M
