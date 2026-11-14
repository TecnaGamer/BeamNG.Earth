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

-- Teleport jump >= 2000m (e.g. to Oakland: 37.8044, -122.2712, ~13 km away)
local pTele = { lat = 37.804400, lon = -122.271200, speed = 0 }
RD.update(0.1, pTele, false)
st = RD._state()
T.check(#st.polylines == 2, 'teleport jump >= 2000m starts a new polyline segment')
T.check(st.rev > rev0, 'rev incremented on teleport split')
T.check(#st.polylines[2] == 1, 'new polyline has 1 point at teleport destination')

-- ---------------------------------------------------------------------------
-- 2. Compact JSON saving (only when stopped, paused, or mission end)
-- ---------------------------------------------------------------------------
-- While driving at speed (speed = 20 m/s), update should NOT save mid-drive
writeCount = 0
local pDrive = { lat = 37.804800, lon = -122.271200, speed = 20 }
RD.update(0.1, pDrive, false)
T.check(writeCount == 0, 'no disk write while driving at speed (Task 4)')

-- When stopped (speed < 0.5 m/s), save should trigger immediately
local pStopped = { lat = 37.805200, lon = -122.271200, speed = 0 }
RD.update(0.1, pStopped, false)
T.check(writeCount == 1, 'save triggered when car stopped')
T.check(savedPretty == false, 'save wrote compact JSON (savedPretty == false)')
T.check(savedData and savedData.polylines, 'saved data contains polylines')
T.check(savedData.version == 1, 'saved data has version 1')

-- Subsequent update while still stopped and not dirty should not save again
RD.update(0.1, pStopped, false)
T.check(writeCount == 1, 'no redundant save while stopped if clean')

-- When paused (isPaused = true), save should trigger if dirty
local pDrive2 = { lat = 37.805600, lon = -122.271200, speed = 15 }
RD.update(0.1, pDrive2, false)
T.check(writeCount == 1, 'dirty again while driving, not saved yet')
RD.update(0.1, pDrive2, true) -- paused
T.check(writeCount == 2, 'save triggered when paused')

-- ---------------------------------------------------------------------------
-- 3. trimPoints: in-place slice, currentLine in step, universal cap
-- ---------------------------------------------------------------------------
RD._clear()
RD.MAX_POINTS = 5

-- Create line 1 with 3 points
local line1 = { { 37.1, -122.1 }, { 37.2, -122.2 }, { 37.3, -122.3 } }
-- Create line 2 with 4 points (total 7 points > MAX_POINTS 5)
local line2 = { { 38.1, -121.1 }, { 38.2, -121.2 }, { 38.3, -121.3 }, { 38.4, -121.4 } }

local st2 = RD._state()
table.insert(st2.polylines, line1)
table.insert(st2.polylines, line2)
st2.currentLine = line2

RD.trimPoints()

-- Total points should now be <= 5
local totalPts = #st2.polylines[1] + (#st2.polylines > 1 and #st2.polylines[2] or 0)
T.check(totalPts <= 5, 'trimPoints enforces MAX_POINTS budget')

-- Test in-place slicing on a single active line
RD._clear()
RD.MAX_POINTS = 4
local singleLine = { { 10.1, 20.1 }, { 10.2, 20.2 }, { 10.3, 20.3 }, { 10.4, 20.4 }, { 10.5, 20.5 } }
local st3 = RD._state()
table.insert(st3.polylines, singleLine)
st3.currentLine = singleLine

RD.trimPoints()

T.check(#st3.polylines == 1, 'single line kept')
T.check(#st3.polylines[1] <= 4, 'single line trimmed to budget')
T.check(st3.currentLine == st3.polylines[1], 'currentLine remains identical table reference (Task 2)')
T.near(st3.currentLine[#st3.currentLine][1], 10.5, 0.0001, 'latest point preserved after in-place slice')

-- Restore MAX_POINTS
RD.MAX_POINTS = 50000

-- ---------------------------------------------------------------------------
-- 4. Free-camera / spectator filtering
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
