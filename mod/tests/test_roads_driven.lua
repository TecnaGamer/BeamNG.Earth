-- luajit mod/tests/test_roads_driven.lua (roads_driven.lua: teleport split, trim, compact save)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

-- Mock file I/O
local savedFile = nil
local savedData = nil
local savedPretty = nil
local writeCount = 0

function jsonReadFile(path)
  if path == savedFile and savedData then
    return savedData
  end
  return nil
end

function jsonWriteFile(path, data, pretty)
  savedFile = path
  savedData = data
  savedPretty = pretty
  writeCount = writeCount + 1
  return true
end

-- Mock active driving vehicle
local mockCam = 'orbit'
local mockFreeCam = false
local mockVehicle = {
  getID = function() return 100 end
}

rawset(_G, 'getPlayerVehicle', function(id) return mockVehicle end)
rawset(_G, 'commands', { isFreeCamera = function() return mockFreeCam end })
rawset(_G, 'core_camera', { getActiveCamName = function() return mockCam end })
rawset(_G, 'MPVehicleGE', { isOwn = function(id) return true end })

local RD = require('beamngEarth/roads_driven')

-- Reset state
savedData = nil
RD._clear()

-- ---------------------------------------------------------------------------
-- 1. Continuous driving & teleport split
-- ---------------------------------------------------------------------------
-- Initial sample (San Francisco: 37.7749, -122.4194)
local p0 = { lat = 37.774900, lon = -122.419400, speed = 15 }
RD.update(0.1, p0, false)

local st = RD._state()
T.check(#st.polylines == 1, 'initial sample creates 1 polyline')
T.check(#st.polylines[1] == 1, 'initial polyline has 1 point')
local rev0 = st.rev
T.check(rev0 > 0, 'initial rev > 0')

-- uiState metadata check
local ui0 = RD.uiState()
T.check(ui0.n == 1, 'uiState n is 1')
T.check(ui0.len == 1, 'uiState len is 1')
T.check(ui0.cur and ui0.cur[1] == 37.7749, 'uiState cur lat matches')

-- Small movement under 25m: should not add point
local pSmall = { lat = 37.774950, lon = -122.419400, speed = 15 }
RD.update(0.1, pSmall, false)
st = RD._state()
T.check(#st.polylines[1] == 1, 'movement under 25m ignored')
T.check(st.rev == rev0, 'rev unchanged under 25m')

-- Movement >= 25m: appends point to same line, rev remains unchanged (no 50k resync!)
local p1 = { lat = 37.775300, lon = -122.419400, speed = 15 } -- ~44m north
RD.update(0.1, p1, false)
st = RD._state()
T.check(#st.polylines[1] == 2, 'movement >= 25m appends point to currentLine')
T.check(st.rev == rev0, 'rev unchanged during continuous driving (Task 3)')
T.check(st.dirty == true, 'marked dirty after driving')
local ui1 = RD.uiState()
T.check(ui1.n == 1, 'uiState n is still 1 after step')
T.check(ui1.len == 2, 'uiState len is 2 after step')

-- Teleport jump >= 2000m (e.g. to Oakland: 37.8044, -122.2712, ~13 km away)
local pTele = { lat = 37.804400, lon = -122.271200, speed = 0 }
RD.update(0.1, pTele, false)
st = RD._state()
T.check(#st.polylines == 2, 'teleport jump >= 2000m starts a new polyline segment')
T.check(st.rev > rev0, 'rev incremented on teleport split')
T.check(#st.polylines[2] == 1, 'new polyline has 1 point at teleport destination')
local uiTele = RD.uiState()
T.check(uiTele.n == 2, 'uiState n is 2 after teleport')
T.check(uiTele.len == 1, 'uiState len is 1 for new line after teleport')

-- ---------------------------------------------------------------------------
-- 2. 3D Map / Camera view frame guard
-- ---------------------------------------------------------------------------
-- While in 3D map (player.camera = true), update should return early
local odoBeforeCam = RD._state().totalKm
local countBeforeCam = #RD._state().polylines
local pCam = { lat = 37.850000, lon = -122.250000, speed = 0, camera = true }
RD.update(0.1, pCam, false)
local stCam = RD._state()
T.check(#stCam.polylines == countBeforeCam, '3D map does not add polylines')
T.check(stCam.totalKm == odoBeforeCam, '3D map does not add to odometer')

-- ---------------------------------------------------------------------------
-- 3. Compact JSON saving (only when paused, mission end, or stopped >= 2s & >= 5min)
-- ---------------------------------------------------------------------------
savedData = nil
RD._clear()
writeCount = 0

-- Start driving
RD.update(0.1, { lat = 37.770000, lon = -122.419400, speed = 15 }, false)
RD.update(0.1, { lat = 37.770400, lon = -122.419400, speed = 15 }, false)
T.check(writeCount == 0, 'no save while driving at speed')

-- Stopped for only 1.0s (under 2s threshold) -> should NOT save yet
RD.update(1.0, { lat = 37.770400, lon = -122.419400, speed = 0 }, false)
T.check(writeCount == 0, 'no save when stopped for < 2s')

-- Stopped for 300s more (stoppedFor >= 2s, and clock - lastSave >= 300s / 5 min) -> should save!
RD.update(300.0, { lat = 37.770400, lon = -122.419400, speed = 0 }, false)
T.check(writeCount == 1, 'save triggered when stopped for >= 2s and >= 5min elapsed')
T.check(savedPretty == false, 'save wrote compact JSON (savedPretty == false)')

-- Next frame while still stopped: already clean -> should NOT save again
RD.update(1.0, { lat = 37.770400, lon = -122.419400, speed = 0 }, false)
T.check(writeCount == 1, 'no redundant save while clean')

-- Drive again (becomes dirty)
RD.update(0.1, { lat = 37.770800, lon = -122.419400, speed = 15 }, false)
T.check(RD._state().dirty == true, 'dirty again after driving')

-- Stop again briefly at a red light (stopped for 5s, but only 10s since last save < 300s) -> should NOT save
RD.update(5.0, { lat = 37.770800, lon = -122.419400, speed = 0 }, false)
T.check(writeCount == 1, 'red light stop does not trigger save if < 5min since last save')

-- When paused (isPaused = true): saves immediately even if < 5min!
RD.update(0.1, { lat = 37.770800, lon = -122.419400, speed = 0 }, true)
T.check(writeCount == 2, 'pause triggers immediate save')

-- ---------------------------------------------------------------------------
-- 4. trimPoints: end-to-end driving past MAX_POINTS via update()
-- ---------------------------------------------------------------------------
savedData = nil
RD._clear()
RD.MAX_POINTS = 5

-- Drive 5 points to reach budget
RD.update(0.1, { lat = 37.770000, lon = -122.419400, speed = 15 }, false) -- pt 1
RD.update(0.1, { lat = 37.770400, lon = -122.419400, speed = 15 }, false) -- pt 2
RD.update(0.1, { lat = 37.770800, lon = -122.419400, speed = 15 }, false) -- pt 3
RD.update(0.1, { lat = 37.771200, lon = -122.419400, speed = 15 }, false) -- pt 4
RD.update(0.1, { lat = 37.771600, lon = -122.419400, speed = 15 }, false) -- pt 5

local stTrim = RD._state()
T.check(#stTrim.polylines == 1, 'single line before trim')
T.check(#stTrim.polylines[1] == 5, '5 points reached')

-- 6th point: triggers trimPoints()!
RD.update(0.1, { lat = 37.772000, lon = -122.419400, speed = 15 }, false)
stTrim = RD._state()
T.check(#stTrim.polylines[1] <= 5, 'points trimmed to budget in-place')

-- 7th point: verify next point lands in the active line without being lost
RD.update(0.1, { lat = 37.772400, lon = -122.419400, speed = 15 }, false)
stTrim = RD._state()
T.check(#stTrim.polylines == 1, 'single line remains intact')
local lastPt = stTrim.polylines[1][#stTrim.polylines[1]]
T.near(lastPt[1], 37.772400, 0.0001, 'subsequent point after trim lands in active line (Item 5)')

-- Also test multi-line trim:
RD._clear()
RD.MAX_POINTS = 4
-- Line 1: 3 points
RD.update(0.1, { lat = 37.770000, lon = -122.419400, speed = 15 }, false)
RD.update(0.1, { lat = 37.770400, lon = -122.419400, speed = 15 }, false)
RD.update(0.1, { lat = 37.770800, lon = -122.419400, speed = 15 }, false)
-- Teleport jump >= 2000m -> starts Line 2 (pt 4)
RD.update(0.1, { lat = 37.804400, lon = -122.271200, speed = 15 }, false)
-- Line 2 point 2 (pt 5 -> trims Line 1!)
RD.update(0.1, { lat = 37.804800, lon = -122.271200, speed = 15 }, false)

local stMulti = RD._state()
local totalMulti = 0
for _, l in ipairs(stMulti.polylines) do totalMulti = totalMulti + #l end
T.check(totalMulti <= 4, 'multi-line trim enforces budget')

-- Drive another point on Line 2
RD.update(0.1, { lat = 37.805200, lon = -122.271200, speed = 15 }, false)
stMulti = RD._state()
local activeLine = stMulti.polylines[#stMulti.polylines]
local activeLastPt = activeLine[#activeLine]
T.near(activeLastPt[1], 37.805200, 0.0001, 'new point lands in Line 2 after multi-line trim')

RD.MAX_POINTS = 50000

-- ---------------------------------------------------------------------------
-- 5. Free-camera / spectator filtering
-- ---------------------------------------------------------------------------
savedData = nil
RD._clear()
mockFreeCam = true
local pFree = { lat = 37.774900, lon = -122.419400, speed = 15 }
RD.update(0.1, pFree, false)
st = RD._state()
T.check(#st.polylines == 0, 'free-camera positions are not recorded')
mockFreeCam = false

T.done('test_roads_driven')
