-- luajit mod/tests/test_spaceclouds.lua
-- The clouds seen from space (spaceclouds.lua, 2026-11-06): shown far above the clouds (hysteresis), asked for again
-- when the camera moved a quarter of the shell's reach, after a rebase or when old; a request when the camera is high.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local C = require('beamngEarth/spaceclouds')

T.check(not C.shownAt(11000, false) and C.shownAt(13000, false), 'shown above SHOW_M')
T.check(C.shownAt(11000, true) and not C.shownAt(9000, true), 'hidden again under HIDE_M (hysteresis)')
T.check(math.abs(C.distance(0, 0, 1, 0) - 111195) < 50, 'great-circle distance: a degree of latitude')
local s = { anchorId = 3, lat = 43.7, lon = 7.27, radius = 1e6, at = 0 }
T.check(C.stale(nil, 3, 43.7, 7.27, 0), 'no shell: ask')
T.check(not C.stale(s, 3, 44.0, 7.27, 10), 'moved 33 km of a 1000 km reach: keep it')
T.check(C.stale(s, 3, 46.2, 7.27, 10), 'moved 280 km: ask again')
T.check(C.stale(s, 4, 43.7, 7.27, 10), 'another anchor: ask again')
T.check(C.stale(s, 3, 43.7, 7.27, C.REFRESH_S + 1), 'old: ask again')
T.check(math.abs(C.reach(100000) / 1000 - (1129 + 276)) < 5, 'reach: the server\'s radius_for')
T.check(not C.stale(s, 3, 43.7, 7.27, 10, 60000) and C.stale(s, 3, 43.7, 7.27, 10, 400000),
  'rose to 400 km: the 1000 km shell is too small, ask again')

-- a camera 100 km up asks once, a low one never
local sent = {}
local camZ = 100000
core_camera = { getPosition = function() return { x = 0, y = 0, z = camZ } end }
local anchor = { enuToGeodetic = function(_, x, y, z) return 43.7, 7.27, 0 end }
C.update(2, anchor, 3, function(m) sent[#sent + 1] = m end)
T.check(#sent == 1 and sent[1].type == 'space_clouds_req' and sent[1].anchorId == 3 and sent[1].alt == 100000,
  'a high camera asks for the shell')
C.update(2, anchor, 3, function(m) sent[#sent + 1] = m end)
T.check(#sent == 1, 'not again while the request is out')
C.clear()
camZ = 2000
C.update(2, anchor, 3, function(m) sent[#sent + 1] = m end)
T.check(#sent == 1, 'a camera under the clouds asks for nothing')
T.done('test_spaceclouds')
