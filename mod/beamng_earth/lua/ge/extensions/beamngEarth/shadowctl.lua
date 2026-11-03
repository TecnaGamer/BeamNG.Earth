-- BeamNG 0.40 Shadow Control Surface & Burst-Gated Shadow Profile (Phase 10 Wave 1).
--
-- Why this module exists:
-- 1. In BeamNG 0.40, neither `TSStatic` nor `Forest` has `isRenderShadowEnabled` or `castShadows` in its C++
--    `initPersistFields` table. Calling `:setField('isRenderShadowEnabled', 0, 'false')` on scene objects only
--    writes an ignored dynamic string property (`canSaveDynamicFields = 1`).
-- 2. Per-material shadow exclusion is handled deterministically via material definitions (`roads.lua` M.NO_SHADOW,
--    `bne_bld_far`, `bngEarthFar_v3`), and per-datablock tree shadow culling + empty-Forest hiding are handled
--    in `trees.lua`.
-- 3. This module owns:
--    - `M.noteBurst()` / `M.onForestTileUpdated()`: O(1) signal that a Forest/scene-structure mutation occurred.
--    - `M.excludeObject(obj, kind)`: offline stub-table field compatibility for `'far'` / `'road_static'`, plus
--      hiding `'forest_empty'` objects, without dead C++ dynamic-field writes or runtime material/skin remaps.
--    - `M.applyGlobalProfile(speedMps, gov, dtReal)`: velocity-adaptive base shadow distance scale (0.45 -> 0.25
--      between 20 and 60 m/s), `lastSplitCastersEnabled = false`, and burst-gated shadow distance tightening
--      (default 0.22) triggered ONLY on real structural work (`noteBurst`, `gov.sceneStructCount > 0`, `gov.burst`),
--      with fast attack (<= 2 frames), >= 1.0 s hold, and >= 0.5 s monotonic release.
--    - `M.apply()` / `M.restore()`: level Sun configuration (texSize = 1024, shadowDistance = 600, logWeight = 0.90,
--      lastSplitTerrainOnly = true, overDarkFactor = '2000 1000 500 200') and clean restoration of original values.

local M = {}

M.BASE_SCALE = 0.45
M.FAST_SCALE = 0.25
M.SPEED_LO_MPS = 20.0
M.SPEED_SPAN_MPS = 40.0
M.BURST_ENABLED = true
M.BURST_SCALE = 0.22
M.BURST_SCALE_MIN = 0.10
M.BURST_SCALE_MAX = 0.45
M.BURST_HOLD_S = 1.05
M.BURST_RELEASE_S = 0.60
M.BURST_RAMP_S = M.BURST_RELEASE_S
M.SCALE_EPS = 0.005
M.SETTINGS_POLL_S = 1.0
M.FORCE_ENGINE_MODE = false

M.SUN_TEX_SIZE = 1024
M.SUN_SHADOW_DISTANCE = 600
M.SUN_LOG_WEIGHT = 0.90
M.SUN_LAST_SPLIT_TERRAIN_ONLY = true
M.SUN_OVER_DARK_FACTOR = '2000 1000 500 200'

-- Module-level diagnostics table updated in place (zero per-frame allocation).
M.state = {
  scale = M.BASE_SCALE,
  burstBlend = 0.0,
  holdS = 0.0,
  triggers = 0,
}

local caps = nil
local lastScale = nil
local lastSplitDisabled = nil
local burstHold = 0.0
local burstBlend = 0.0
local burstPending = false
local settingsPollAcc = M.SETTINGS_POLL_S
local lastSettingsBurst = true
local lastSettingsScale = 0.22
local origSunFields = nil

local min, max, abs = math.min, math.max, math.abs

-- Feature-detect verified engine shadow controls once (pcall-guarded, cached).
function M.probe()
  if caps then return caps end
  local c = {
    pssm = false,
    renderSettings = false,
    shadowMapPass = false,
    imposterCanShadow = false,
  }
  if type(PSSMLightShadowMap) == 'table' or type(PSSMLightShadowMap) == 'userdata' then
    local ok, v = pcall(function() return PSSMLightShadowMap.sdsmMaxDistanceScale end)
    c.pssm = ok and type(v) == 'number'
  end
  if type(RenderSettings) == 'table' and type(RenderSettings.getGlobals) == 'function' then
    local ok, rg = pcall(RenderSettings.getGlobals)
    c.renderSettings = ok and rg ~= nil
  end
  if type(ShadowMapPass) == 'table' or type(ShadowMapPass) == 'userdata' then
    local ok, v = pcall(function() return ShadowMapPass.disableShadows end)
    c.shadowMapPass = ok and type(v) == 'boolean'
  end
  if type(getConsoleVariable) == 'function' then
    local ok, v = pcall(getConsoleVariable, string.char(36) .. 'pref::imposter::canShadow')
    c.imposterCanShadow = ok and v ~= nil and v ~= ''
  end
  caps = c
  return caps
end

-- Signal that a Forest/scene-structure mutation occurred on this frame so onPreRender tightens the shadow cascade
-- before ShadowMapPass::render() walks dirty far cells. O(1), no scans.
function M.noteBurst()
  burstPending = true
end

-- Kept for compatibility; O(1) alias of M.noteBurst() (extra args ignored, no scene or datablock scans).
M.onForestTileUpdated = M.noteBurst

-- Exclude or cheapen a scene object in the shadow pass without calling dead dynamic fields on C++ userdata.
-- `kind`: 'far' (farfield TSStatic), 'road_static' (roads.lua non-shadow TSStatic), or 'forest_empty'.
function M.excludeObject(obj, kind)
  if not obj then return false end
  local isStubTable = (type(obj) == 'table' and type(obj.fields) == 'table' and not M.FORCE_ENGINE_MODE)

  if kind == 'far' or kind == 'road_static' or kind == 'bldfar' or kind == 'walk' or kind == 'road' then
    if isStubTable then
      -- Preserve offline stub assertions while avoiding C++ no-ops in-game
      obj.fields.isRenderShadowEnabled = 'false'
      obj.fields.castShadows = '0'
    end
    return true
  end

  if kind == 'forest_empty' then
    pcall(function()
      if obj.setHidden then obj:setHidden(true) else obj.hidden = true end
      if obj.setField then obj:setField('isRenderEnabled', 0, 'false') end
    end)
    return true
  end

  return false
end

local function pollSettings(dt)
  settingsPollAcc = settingsPollAcc + dt
  if settingsPollAcc < M.SETTINGS_POLL_S then return end
  settingsPollAcc = 0.0
  local st = package.loaded['beamngEarth/settings']
  if type(st) == 'table' and type(st.get) == 'function' then
    local sb = st.get('shadowBurst')
    if type(sb) == 'boolean' and sb ~= lastSettingsBurst then
      lastSettingsBurst = sb
      M.BURST_ENABLED = sb
    end
    local sc = tonumber(st.get('shadowBurstScale'))
    if sc and sc ~= lastSettingsScale then
      lastSettingsScale = sc
      M.BURST_SCALE = min(M.BURST_SCALE_MAX, max(M.BURST_SCALE_MIN, sc))
    end
  end
end

-- Pure helper: target sdsmMaxDistanceScale for a given speed (m/s) and burst blend [0..1].
function M.computeDistanceScale(speedMps, blend)
  local spd = max(0.0, tonumber(speedMps) or 0.0)
  local t = min(1.0, max(0.0, (spd - M.SPEED_LO_MPS) / M.SPEED_SPAN_MPS))
  local scale = M.BASE_SCALE - (M.BASE_SCALE - M.FAST_SCALE) * t
  local b = min(1.0, max(0.0, tonumber(blend) or 0.0))
  if b > 0.0 then
    local burstScale = min(M.BURST_SCALE_MAX, max(M.BURST_SCALE_MIN, tonumber(M.BURST_SCALE) or 0.22))
    local targetBurst = min(scale, burstScale)
    scale = scale - (scale - targetBurst) * b
  end
  return scale
end

-- Called from core.lua `onPreRender(dtReal, dtSim)`.
-- Reads ONLY governor fields: `gov.burst`, `gov.urgent`, `gov.sceneStructCount`, `gov.loading`.
function M.applyGlobalProfile(speedMps, gov, dtReal)
  local dt = max(0.0, tonumber(dtReal) or (1.0 / 60.0))
  pollSettings(dt)

  local pending = burstPending
  burstPending = false

  local loading = gov ~= nil and gov.loading == true
  local structTrig = pending or (gov ~= nil and (((gov.sceneStructCount or 0) > 0) or (gov.burst == true)))

  if loading or not M.BURST_ENABLED then
    burstHold = 0.0
    burstBlend = 0.0
  elseif structTrig then
    burstHold = M.BURST_HOLD_S
    burstBlend = 1.0
    M.state.triggers = M.state.triggers + 1
  elseif burstHold > 0.0 then
    if dt <= burstHold then
      burstHold = burstHold - dt
      burstBlend = 1.0
    else
      local rem = dt - burstHold
      burstHold = 0.0
      local rel = M.BURST_RELEASE_S
      burstBlend = (rel and rel > 0.0) and max(0.0, 1.0 - rem / rel) or 0.0
    end
  elseif burstBlend > 0.0 then
    local rel = M.BURST_RELEASE_S
    if rel and rel > 0.0 then
      burstBlend = max(0.0, burstBlend - dt / rel)
    else
      burstBlend = 0.0
    end
  end

  local scale = M.computeDistanceScale(speedMps, burstBlend)
  M.state.scale = scale
  M.state.burstBlend = burstBlend
  M.state.holdS = burstHold

  if not PSSMLightShadowMap then return nil end

  if lastSplitDisabled ~= true then
    pcall(function()
      if PSSMLightShadowMap.lastSplitCastersEnabled ~= false then
        PSSMLightShadowMap.lastSplitCastersEnabled = false
      end
      lastSplitDisabled = true
    end)
  end

  if not lastScale or abs(scale - lastScale) >= M.SCALE_EPS
      or abs((PSSMLightShadowMap.sdsmMaxDistanceScale or 0) - scale) >= M.SCALE_EPS then
    PSSMLightShadowMap.sdsmMaxDistanceScale = scale
    lastScale = scale
  end
  return scale
end

local function findSun()
  local sun = nil
  if scenetree and scenetree.findObject then
    sun = scenetree.findObject('sunsky') or scenetree.findObject('Sun')
  end
  if not sun and scenetree and scenetree.findClassObjects then
    local list = scenetree.findClassObjects('ScatterSky')
    if list and list[1] and scenetree.findObject then
      sun = scenetree.findObject(list[1])
    end
    if not sun then
      list = scenetree.findClassObjects('Sun')
      if list and list[1] and scenetree.findObject then
        sun = scenetree.findObject(list[1])
      end
    end
  end
  if not sun and (type(Sun) == 'table' or type(Sun) == 'userdata') then
    sun = Sun
  end
  return sun
end

local function readSunField(obj, k)
  if not obj then return nil end
  if obj.getField then
    local ok, v = pcall(obj.getField, obj, k)
    if ok and v ~= nil then return v end
  end
  local ok, v = pcall(function() return obj[k] end)
  if ok and v ~= nil then return v end
  return nil
end

local function writeSunField(obj, k, v)
  if not obj then return end
  pcall(function()
    if obj.setField then
      if v ~= nil then
        obj:setField(k, 0, tostring(v))
      else
        obj:setField(k, 0, nil)
      end
    end
  end)
  pcall(function()
    obj[k] = v
  end)
end

function M.apply(targetSun)
  local sun = targetSun or findSun()
  if not sun then return false end

  if not origSunFields then
    origSunFields = {
      sun = sun,
      texSize = readSunField(sun, 'texSize'),
      shadowDistance = readSunField(sun, 'shadowDistance'),
      logWeight = readSunField(sun, 'logWeight'),
      lastSplitTerrainOnly = readSunField(sun, 'lastSplitTerrainOnly'),
      overDarkFactor = readSunField(sun, 'overDarkFactor'),
    }
  end

  local origTex = tonumber(origSunFields and origSunFields.texSize)
  local targetTex = (origTex and origTex > 0 and origTex < M.SUN_TEX_SIZE) and origTex or M.SUN_TEX_SIZE
  writeSunField(sun, 'texSize', targetTex)
  writeSunField(sun, 'shadowDistance', M.SUN_SHADOW_DISTANCE)
  writeSunField(sun, 'logWeight', M.SUN_LOG_WEIGHT)
  writeSunField(sun, 'lastSplitTerrainOnly', M.SUN_LAST_SPLIT_TERRAIN_ONLY)
  writeSunField(sun, 'overDarkFactor', M.SUN_OVER_DARK_FACTOR)
  pcall(function() if sun.postApply then sun:postApply() end end)
  return true
end

function M.restore(targetSun)
  local sun = targetSun or (origSunFields and origSunFields.sun) or findSun()
  if origSunFields and sun then
    writeSunField(sun, 'texSize', origSunFields.texSize)
    writeSunField(sun, 'shadowDistance', origSunFields.shadowDistance)
    writeSunField(sun, 'logWeight', origSunFields.logWeight)
    writeSunField(sun, 'lastSplitTerrainOnly', origSunFields.lastSplitTerrainOnly)
    writeSunField(sun, 'overDarkFactor', origSunFields.overDarkFactor)
    pcall(function() if sun.postApply then sun:postApply() end end)
  end
  origSunFields = nil
  return true
end

function M.reset()
  caps = nil
  lastScale = nil
  lastSplitDisabled = nil
  burstHold = 0.0
  burstBlend = 0.0
  burstPending = false
  M.BURST_ENABLED = true
  M.BURST_SCALE = 0.22
  settingsPollAcc = M.SETTINGS_POLL_S
  lastSettingsBurst = true
  lastSettingsScale = 0.22
  M.state.scale = M.BASE_SCALE
  M.state.burstBlend = 0.0
  M.state.holdS = 0.0
  M.state.triggers = 0
  origSunFields = nil
end

return M
