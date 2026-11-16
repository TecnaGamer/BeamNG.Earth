-- Frame recorder for game-speed / frame-pacing A/B tests (tools/framerec.py sends this through MCP run_lua; an
-- extension-proxy event sink as tools/drive_driver.lua, no mod file). Per frame (onPreRender): the wall clock
-- (hptimer, ms), the engine's dtReal / dtSim / dtRaw (ms), the player's speed (m/s), the variant in force and a
-- flag for frames that held MCP work (tools/drive.py polls: BNGE_DRIVE.harnessTag). Optionally cycles variants
-- every SEG seconds: each variant may write the dtsmooth test control file (temp/beamngearth_dtsmooth_ctl, read by
-- the preload library when started with BNGEARTH_DTSMOOTH_CTL=1) and set the mod's frame pacer (framepacer.lua:
-- 'on' as shipped, 'off', or fields to override such as PERCENTILE).
--   FREC.start(cfg)  cfg = { seg = s, variants = { {name=, ctl=, pacer=}, ... } }
--   FREC.stop()      restores the pacer, removes the control file -> JSON summary
--   FREC.dump(path)  CSV of the frames to path (the game's file system: '/temp/...')
local R = rawget(_G, 'FREC') or {}
FREC = R

local function newClock()
  local hp = rawget(_G, 'hptimer')
  local t = hp and hp()
  local acc = 0
  if t then t:stopAndReset() end
  return function() acc = acc + t:stopAndReset(); return acc end
end

local CTL = '/temp/beamngearth_dtsmooth_ctl'

local function pacerMod()
  local core = rawget(_G, 'beamngEarth_core')
  return core and core._pacer
end

local function applyVariant(v)
  if v.ctl then writeFile(CTL, v.ctl) end
  local p = pacerMod()
  if not p then return end
  R.pacerOrig = R.pacerOrig or { step = p.step, fields = {} }
  p.step = R.pacerOrig.step
  for k, val in pairs(R.pacerOrig.fields) do p[k] = val end
  if R.pacerOrig.enabledSaved then p.ENABLED = R.pacerOrig.enabled end
  if not R.pacerOrig.enabledSaved then R.pacerOrig.enabledSaved, R.pacerOrig.enabled = true, p.ENABLED end
  if v.pacer == 'off' then
    p.step = function() return 0 end
    p.ENABLED = false
  elseif type(v.pacer) == 'table' then
    p.ENABLED = true        -- (forced: framepacer.lua paces by itself only where dtsmooth is missing)
    for k, val in pairs(v.pacer) do
      if R.pacerOrig.fields[k] == nil then R.pacerOrig.fields[k] = p[k] end
      p[k] = val
    end
  end
  if p.reset then p.reset() end
end

function R.start(cfg)
  if R.proxy then return 'already running' end
  if type(cfg) == 'string' then cfg = jsonDecode(cfg) end
  cfg = cfg or {}
  R.cfg = cfg
  R.clock = newClock()
  R.n = 0
  R.t, R.dr, R.ds, R.dw, R.v, R.var, R.h = {}, {}, {}, {}, {}, {}, {}
  R.vi = 1
  R.variants = cfg.variants or { { name = 'as is' } }
  R.seg = (cfg.seg or 20) * 1000
  R.segStart = R.clock()
  applyVariant(R.variants[1])
  local sink = {}
  function sink:onPreRender(dtReal, dtSim, dtRaw)
    local now = R.clock()
    if #R.variants > 1 and now - R.segStart >= R.seg then
      R.vi = R.vi % #R.variants + 1
      R.segStart = now
      pcall(applyVariant, R.variants[R.vi])
    end
    local n = R.n + 1
    R.n = n
    R.t[n] = now
    R.dr[n] = (dtReal or 0) * 1000
    R.ds[n] = (dtSim or 0) * 1000
    R.dw[n] = (dtRaw or 0) * 1000
    local veh = getPlayerVehicle(0)
    R.v[n] = veh and veh:getVelocity():length() or 0
    R.var[n] = R.vi
    local d = rawget(_G, 'BNGE_DRIVE')
    R.h[n] = (d and d.harnessTag) and 1 or 0
  end
  R.proxy = newExtensionProxy(nil, 'bneFrameRec')
  R.proxy:submitEventSinks({ sink }, {})
  return 'started'
end

function R.stop()
  if R.proxy then pcall(function() R.proxy:destroy() end) end
  R.proxy = nil
  local p = pacerMod()
  if p and R.pacerOrig then
    p.step = R.pacerOrig.step
    for k, val in pairs(R.pacerOrig.fields) do p[k] = val end
    if R.pacerOrig.enabledSaved then p.ENABLED = R.pacerOrig.enabled end
    if p.reset then p.reset() end
  end
  R.pacerOrig = nil
  if FS and FS.removeFile then pcall(FS.removeFile, FS, CTL) end
  return jsonEncode({ frames = R.n or 0 })
end

function R.dump(path)
  local out = { 't_ms,dtReal,dtSim,dtRaw,speed,variant,harness' }
  for i = 1, R.n or 0 do
    out[#out + 1] = string.format('%.3f,%.4f,%.4f,%.4f,%.2f,%d,%d', R.t[i], R.dr[i], R.ds[i], R.dw[i], R.v[i], R.var[i], R.h[i])
  end
  writeFile(path, table.concat(out, '\n') .. '\n')
  return jsonEncode({ frames = R.n or 0, path = path })
end

return 'ok'
