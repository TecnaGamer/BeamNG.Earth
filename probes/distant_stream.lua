-- Isolated far-road snapshot probe. The live core and companion keep running; only our module is ticked here.
local M = {}
local runtime, files, at, elapsed, feedIn, requested = nil, {}, 1, 0, 0, false
local errors = 0
local base = '/temp/beamngEarthTrafficProbe/'

function M.stop()
  if runtime then runtime.reset() end
  runtime, files, requested = nil, {}, false
end
M.onExtensionUnloaded = M.stop
M.onClientEndMission = M.stop

function M.start()
  M.stop()
  local pure = dofile(base .. 'distanttraffic.lua')
  local env = setmetatable({ require = function(name)
    if name == 'beamngEarth/distanttraffic' then return pure end
    if name == 'beamngEarth/net' then return { send = function(m) requested = m.enabled end } end
    return require(name)
  end }, { __index = _G })
  local chunk = assert(loadfile(base .. 'trafficvisuals.lua'))
  setfenv(chunk, env)
  runtime = chunk()
  runtime.session('far-road-probe', true)
  files = jsonReadFile(base .. 'routes/manifest.json') or {}
  at, elapsed, feedIn, errors = 1, 0, 0, 0
  return #files > 0
end

local function tick(dtReal, dtSim)
  elapsed, feedIn = elapsed + dtReal, feedIn - dtReal
  if elapsed > 600 then M.stop(); return end
  local s = beamngEarth_core._state()
  if not s.active then M.stop(); return end
  local box = { require('beamngEarth/lattice').groundBox(s.block.cx, s.block.cy) }
  if requested and feedIn <= 0 and files[at] then
    local data = jsonReadFile(base .. 'routes/' .. files[at])
    if data then runtime.onMessage({ type = 'traffic_tile', key = data.key, path = base .. 'routes/' .. files[at], data = data }) end
    at, feedIn = at + 1, 0.5
  end
  runtime.update(dtReal, dtSim, s, box)
end

function M.onUpdate(dtReal, dtSim)
  if not runtime then return end
  local ok, err = pcall(tick, dtReal, dtSim)
  if not ok then errors = errors + 1; M.stop(); log('E', 'beamngEarth', 'far traffic probe: ' .. tostring(err)) end
end

function M.status()
  local s = runtime and runtime.status() or {}
  s.loaded, s.files, s.seconds, s.errors = at - 1, #files, elapsed, errors
  return s
end

function M.inspect() return M.status() end
function M.setEnabled(v) if runtime then runtime.setEnabled(v) end end

return M
