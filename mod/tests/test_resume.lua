local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local resume = require('beamngEarth/resume')
local files, writes = {}, 0
function jsonReadFile(path)
  if files[path] == 'BROKEN' then error('invalid json') end
  return files[path]
end
function jsonWriteFile(path,data)
  writes = writes + 1
  local copy = {}; for k,v in pairs(data) do copy[k] = v end
  files[path] = copy
  return true
end
T.check(resume.load() == nil, 'no saved drive: normal start')
local p = {lat=39.1,lon=-86.2,h=220,heading=math.pi/2}
T.check(resume.update(p,10), 'first valid drive position saved')
T.check(files[resume.FILE].lat == p.lat and files[resume.FILE].h == 220, 'coordinates and height persisted')
p.lon = -86.21
T.check(not resume.update(p,11) and writes == 1, 'driving does not write every frame')
T.check(resume.update(p,15) and writes == 2, 'updated on five-second interval')
T.check(not resume.update(p,20) and writes == 2, 'stationary position does not rewrite the file')
p.lat = 39.2
T.check(resume.update(p,20.1,true), 'leaving the level flushes the latest position')
local loaded = resume.load()
T.check(loaded.lat == 39.2 and loaded.lon == -86.21 and loaded.heading == math.pi/2, 'next session restores the saved drive')
loaded.lat = 0
T.check(resume.load().lat == 39.2, 'caller cannot mutate the stored location')
T.check(not resume.update({lat=0/0,lon=0},25,true), 'NaN coordinates rejected')
T.check(not resume.update({lat=91,lon=0},25,true), 'out-of-range coordinates rejected')
T.check(not resume.update({lat=0,lon=math.huge},25,true), 'infinite coordinates rejected')
T.check(resume.load().lat == 39.2, 'invalid sample cannot erase the last valid drive')
files[resume.FILE] = 'BROKEN'
T.check(resume.load() == nil, 'broken save falls back to normal start')
files[resume.FILE] = {version=2,lat=10,lon=20}
T.check(resume.load() == nil, 'unknown format ignored')
files[resume.FILE] = {version=1,lat=10,lon=20,h=math.huge,heading=-math.pi/2}
loaded = resume.load()
T.check(loaded.h == nil and math.abs(loaded.heading-3*math.pi/2)<1e-10, 'invalid altitude omitted and heading normalized')
jsonWriteFile = function() error('disk unavailable') end
T.check(not resume.update(p,30,true), 'write failures do not break the mod')
T.done('test_resume')
