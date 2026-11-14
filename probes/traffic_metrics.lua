-- Temporary off/on frame samples for #565. No scene objects, input or environment changes.
local M = {}
local samples, metrics, label = {}, {}, nil
local active, last, deadline = false, nil, 0
local camera, forward, context

local function state()
  return { time = core_environment.getTimeOfDay().time, vehicles = be:getObjectCount(),
    fov = core_camera.getFovDeg(), limit = settings.getValue('fpsLimit'),
    limitEnabled = settings.getValue('fpsLimitEnabled'), backgroundLimit = settings.getValue('fpsLimitBackground'),
    backgroundLimitEnabled = settings.getValue('fpsLimitBackgroundEnabled'),
    traffic = beamngEarth_core._trafficvisuals.status() }
end

function M.start(name, seconds)
  label, samples, last = tostring(name), {}, nil
  local p = core_camera.getPosition()
  camera = { p.x, p.y, p.z }
  local f = core_camera.getForward()
  forward = { f.x, f.y, f.z }
  context = state()
  deadline, active = os.clockhp() + math.max(1, math.min(30, seconds or 20)), true
end

function M.onUpdate()
  if not active then return end
  local now = os.clockhp()
  if now >= deadline or #samples >= 3000 then active = false; return end
  if last then
    local ok = pcall(Engine.Debug.getLastPerformanceMetrics, metrics)
    if ok then
      local parts = beamngEarth_core.frameMarks()
      samples[#samples + 1] = { (now - last) * 1000, metrics.Lua or 0, metrics.Render or 0,
        metrics.FPSLimiter or 0, metrics.PhysicsAndVLUA or 0, parts.trafficvisuals or 0 }
    end
  end
  last = now
end

function M.result()
  local p = core_camera.getPosition()
  local f = core_camera.getForward()
  return { label = label, done = not active, samples = samples, cameraStart = camera, cameraEnd = { p.x, p.y, p.z },
    forwardStart = forward, forwardEnd = { f.x, f.y, f.z },
    stateStart = context, stateEnd = state(),
    columns = { 'frameMs', 'luaMs', 'renderMs', 'limiterMs', 'physicsLuaMs', 'trafficMs' },
    traffic = beamngEarth_core._trafficvisuals.status() }
end

function M.stop() active = false end
M.onExtensionUnloaded = M.stop
M.onClientEndMission = M.stop
return M
