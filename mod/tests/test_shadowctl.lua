-- luajit mod/tests/test_shadowctl.lua  (Phase 10 Wave 1: shadowctl.lua unit tests)
local dir = (arg and arg[0] or ''):match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local shadowctl = require('beamngEarth/shadowctl')

-- 1. Pure velocity & streaming-burst distance scale calculation
T.near(shadowctl.computeDistanceScale(0, 0), 0.45, 1e-6, 'stationary, no burst: 0.45')
T.near(shadowctl.computeDistanceScale(20, 0), 0.45, 1e-6, '20 m/s threshold: 0.45')
T.near(shadowctl.computeDistanceScale(40, 0), 0.35, 1e-6, '40 m/s midpoint: 0.35')
T.near(shadowctl.computeDistanceScale(60, 0), 0.25, 1e-6, '60 m/s high speed: 0.25')
T.near(shadowctl.computeDistanceScale(90, 0), 0.25, 1e-6, '90 m/s clamped: 0.25')
T.near(shadowctl.computeDistanceScale(0, 1.0), 0.22, 1e-6, 'stationary + full burst blend: tightened to 0.22')
T.near(shadowctl.computeDistanceScale(0, 0.5), 0.335, 1e-6, 'stationary + half burst blend: interpolated')

-- 2. Global PSSMLightShadowMap profile + setter quantization + diagnostics table identity
local pssmWrites = 0
local pssmState = { lastSplitCastersEnabled = true, sdsmMaxDistanceScale = 0.5 }
PSSMLightShadowMap = setmetatable({}, {
  __index = function(_, k) return pssmState[k] end,
  __newindex = function(_, k, v)
    pssmWrites = pssmWrites + 1
    pssmState[k] = v
  end,
})

shadowctl.reset()
local stateRef = shadowctl.state
T.check(type(stateRef) == 'table', 'M.state exposed as module-level table')

local caps = shadowctl.probe()
T.check(caps.pssm == true, 'probe detects PSSMLightShadowMap')

local sc1 = shadowctl.applyGlobalProfile(10, { frameUnits = 0, spent = 0, sceneStructCount = 0, burst = false }, 1 / 60)
T.near(sc1, 0.45, 1e-6, 'normal frame applies 0.45 scale')
T.check(pssmState.lastSplitCastersEnabled == false, 'lastSplitCastersEnabled forced false')
local writesAfterFirst = pssmWrites
shadowctl.applyGlobalProfile(10, { frameUnits = 0, spent = 0, sceneStructCount = 0, burst = false }, 1 / 60)
T.check(pssmWrites == writesAfterFirst, 'steady speed skips redundant C++ userdata writes')

-- Simulate cameraUpdate.lua clobbering sdsmMaxDistanceScale back to 0.5
pssmState.sdsmMaxDistanceScale = 0.5
shadowctl.applyGlobalProfile(10, { frameUnits = 0, spent = 0, sceneStructCount = 0, burst = false }, 1 / 60)
T.near(pssmState.sdsmMaxDistanceScale, 0.45, 1e-6, 'restores scale if cameraUpdate overwrote it')

-- 3. R3: No trigger on plain governor units (frameUnits > 0, spent > 0, sceneStructCount == 0, burst == false)
shadowctl.reset()
for _ = 1, 10 do
  local sc = shadowctl.applyGlobalProfile(10, {
    frameUnits = 3,
    spent = 2.8,
    sceneStructCount = 0,
    burst = false,
    loading = false,
  }, 1 / 60)
  T.near(sc, 0.45, 1e-6, 'plain non-structural governor units do not tighten shadow scale')
end
T.check(shadowctl.state.triggers == 0 and shadowctl.state.burstBlend == 0, 'no triggers recorded on plain units')

-- 4. R3: Trigger on structural work -> reaches burst scale within <= 2 frames, holds >= 1.0 s,
--    releases monotonically over >= 0.5 s
shadowctl.reset()
local scTrig = shadowctl.applyGlobalProfile(10, {
  frameUnits = 1,
  sceneStructCount = 1,
  burst = false,
  loading = false,
}, 1 / 60)
T.near(scTrig, 0.22, 1e-6, 'structural trigger reaches burst scale (0.22) within <= 2 frames')
T.check(shadowctl.state.triggers == 1 and shadowctl.state.burstBlend == 1.0, 'M.state records trigger and burstBlend=1')
T.check(shadowctl.state == stateRef, 'M.state updated in place without table allocation')

-- Step 60 calm frames (1.0 s at 60 Hz): must still hold at burst scale 0.22
local calmGov = { frameUnits = 1, sceneStructCount = 0, burst = false, loading = false }
for i = 1, 60 do
  local scHold = shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
  T.near(scHold, 0.22, 1e-6, 'hold stays at burst scale through 1.0 s (frame ' .. i .. ')')
end

-- Advance until release starts (burstBlend < 1.0)
local prevScale = shadowctl.state.scale
local releaseFrames = 0
local monotonic = true
local inRelease = false
for _ = 1, 120 do
  local scRel = shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
  if scRel > 0.22 + 1e-6 then
    inRelease = true
  end
  if inRelease then
    releaseFrames = releaseFrames + 1
    if scRel + 1e-9 < prevScale then
      monotonic = false
    end
    prevScale = scRel
    if scRel >= 0.45 - 1e-6 then
      break
    end
  end
end
T.check(monotonic, 'release is monotonic from burst scale back to base scale')
T.check(releaseFrames >= 30, 'release takes >= 0.5 s (>= 30 frames at 60 Hz, got ' .. releaseFrames .. ')')
T.near(shadowctl.state.scale, 0.45, 1e-6, 'scale returns to 0.45 after release completes')

-- 5. R3: Re-trigger during hold extends hold; re-trigger during release re-attacks
shadowctl.reset()
shadowctl.applyGlobalProfile(10, { sceneStructCount = 2, burst = false }, 1 / 60)
for _ = 1, 40 do
  shadowctl.applyGlobalProfile(10, calmGov, 1 / 60) -- 0.67 s into first hold
end
-- Re-trigger during hold via gov.burst = true
shadowctl.applyGlobalProfile(10, { sceneStructCount = 0, burst = true }, 1 / 60)
for _ = 1, 60 do
  shadowctl.applyGlobalProfile(10, calmGov, 1 / 60) -- another full 1.0 s must still hold
end
T.near(shadowctl.state.scale, 0.22, 1e-6, 're-trigger during hold extends hold for another >= 1.0 s')

-- Step partway into release so scale is between 0.22 and 0.45
for _ = 1, 20 do
  shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
end
T.check(shadowctl.state.scale > 0.25 and shadowctl.state.scale < 0.44, 'mid-release scale is intermediate')

-- Re-trigger during release via noteBurst()
shadowctl.noteBurst()
local scReattack = shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
T.near(scReattack, 0.22, 1e-6, 're-trigger during release re-attacks to burst scale within <= 2 frames')

-- 6. R3: gov.loading == true -> no tightening
shadowctl.reset()
shadowctl.noteBurst()
local scLoad = shadowctl.applyGlobalProfile(10, {
  sceneStructCount = 4,
  burst = true,
  loading = true,
}, 1 / 60)
T.near(scLoad, 0.45, 1e-6, 'gov.loading=true prevents burst tightening and keeps base profile')
T.check(shadowctl.state.burstBlend == 0, 'burstBlend stays 0 while loading')

-- 7. Setting / constant gate: BURST_ENABLED=false -> no tightening; settings.lua integration & 1 Hz poll
shadowctl.reset()
shadowctl.BURST_ENABLED = false
shadowctl.noteBurst()
local scOff = shadowctl.applyGlobalProfile(10, { sceneStructCount = 2, burst = true }, 1 / 60)
T.near(scOff, 0.45, 1e-6, 'BURST_ENABLED=false disables burst tightening')

shadowctl.reset()
local getCalls = 0
local mockBurst = true
local mockScale = 0.18
package.loaded['beamngEarth/settings'] = {
  get = function(k)
    getCalls = getCalls + 1
    if k == 'shadowBurst' then return mockBurst end
    if k == 'shadowBurstScale' then return mockScale end
    return nil
  end,
}
-- First call after reset polls immediately; next 30 frames (0.5 s < 1.0 s) must not poll settings.get
shadowctl.applyGlobalProfile(10, { sceneStructCount = 1 }, 1 / 60)
local callsAfterFirst = getCalls
T.near(shadowctl.state.scale, 0.18, 1e-6, 'shadowBurstScale setting (0.18) applied from settings.get')
for _ = 1, 30 do
  shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
end
T.check(getCalls == callsAfterFirst, 'settings.get polled at most once per second (not per frame)')
mockBurst = false
for _ = 1, 35 do
  shadowctl.applyGlobalProfile(10, { sceneStructCount = 1 }, 1 / 60)
end
T.check(getCalls > callsAfterFirst and shadowctl.BURST_ENABLED == false, 'settings poll picks up shadowBurst=false after 1 s')
T.near(shadowctl.state.scale, 0.45, 1e-6, 'shadowBurst=false via settings poll keeps base scale')
package.loaded['beamngEarth/settings'] = nil

-- Verify real settings.lua schema validation and M.APPLY wiring for shadowBurst & shadowBurstScale
local realSettings = require('beamngEarth/settings')
T.check(realSettings.validate('shadowBurst', false) == false, 'settings.validate shadowBurst bool')
T.near(realSettings.validate('shadowBurstScale', 0.02), 0.10, 1e-6, 'settings.validate clamps shadowBurstScale min to 0.10')
T.near(realSettings.validate('shadowBurstScale', 0.80), 0.45, 1e-6, 'settings.validate clamps shadowBurstScale max to 0.45')
realSettings.set('shadowBurst', false)
T.check(shadowctl.BURST_ENABLED == false, 'settings.set(shadowBurst, false) updates shadowctl.BURST_ENABLED')
realSettings.set('shadowBurstScale', 0.16)
T.near(shadowctl.BURST_SCALE, 0.16, 1e-6, 'settings.set(shadowBurstScale, 0.16) updates shadowctl.BURST_SCALE')
realSettings.set('shadowBurst', true)
realSettings.set('shadowBurstScale', 0.22)
package.loaded['beamngEarth/settings'] = nil

-- 8. R2: onForestTileUpdated is O(1) and does NOT call scenetree or Sim.getDataBlockSet
shadowctl.reset()
local sceneCalls = 0
local origFind = scenetree.findObject
local origFindById = scenetree.findObjectById
scenetree.findObject = function(...)
  sceneCalls = sceneCalls + 1
  return origFind(...)
end
scenetree.findObjectById = function(...)
  sceneCalls = sceneCalls + 1
  return origFindById(...)
end
Sim = {
  getDataBlockSet = function()
    sceneCalls = sceneCalls + 1
    return nil
  end,
}
shadowctl.onForestTileUpdated(42, true, 'extra')
T.check(sceneCalls == 0, 'onForestTileUpdated is O(1) and performs zero scenetree or datablock scans')
local scForest = shadowctl.applyGlobalProfile(10, calmGov, 1 / 60)
T.near(scForest, 0.22, 1e-6, 'onForestTileUpdated triggers burst tightening like noteBurst')
scenetree.findObject = origFind
scenetree.findObjectById = origFindById
Sim = nil

-- 9. R2 & R4: Deleted functions are absent, excludeObject writes stub fields offline and does no skin remap in engine mode
T.check(shadowctl.syncTreeDatablocks == nil, 'syncTreeDatablocks removed')
T.check(shadowctl.sweepEmptyForests == nil, 'sweepEmptyForests removed')

local stubFar = createObject('TSStatic')
shadowctl.excludeObject(stubFar, 'far')
T.check(stubFar:getField('isRenderShadowEnabled') == 'false' and stubFar:getField('castShadows') == '0',
  'excludeObject writes stub fields for offline tests')

shadowctl.FORCE_ENGINE_MODE = true
local engObj = createObject('TSStatic')
shadowctl.excludeObject(engObj, 'road_static')
T.check(engObj:getField('skin') == nil, 'R4: excludeObject does not perform runtime skin remap')
T.check(engObj:getField('isRenderShadowEnabled') == nil, 'engine mode does not write dead isRenderShadowEnabled field')
shadowctl.FORCE_ENGINE_MODE = false

local fEmpty = createObject('Forest')
shadowctl.excludeObject(fEmpty, 'forest_empty')
T.check(fEmpty.hidden == true and fEmpty:getField('isRenderEnabled') == 'false', 'forest_empty hides object')

T.done('test_shadowctl')
