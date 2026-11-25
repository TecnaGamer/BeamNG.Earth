-- luajit mod/tests/test_rainstreaks.lua
-- beamngEarth/rainstreaks.lua (weather phase B): the shells' lean (the top upwind, at most TILT_MAX), and the runtime:
-- made on the first rain, following the camera, the strength written only when it moves, hidden when dry, nothing at
-- all while dry and hidden.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local R = require('beamngEarth/rainstreaks')
local check, near = T.check, T.near

local function rotZ(qx, qy, qz, qw)     -- the rotated +z axis
  return 2 * (qx * qz + qw * qy), 2 * (qy * qz - qw * qx), 1 - 2 * (qx * qx + qy * qy)
end
local qx, qy, qz, qw = R.tiltQuat(0, 0)
check(qx == 0 and qy == 0 and qz == 0 and qw == 1, 'still air: upright')
qx, qy, qz, qw = R.tiltQuat(8, 0, 8)           -- the air moves east at the fall speed: 45 degrees, the top to the west
local x, y, z = rotZ(qx, qy, qz, qw)
near(x, -math.sqrt(0.5), 1e-9, 'top leans upwind (west)'); near(y, 0, 1e-9, 'no north lean'); near(z, math.sqrt(0.5), 1e-9, '45 deg')
qx, qy, qz, qw = R.tiltQuat(0, -100, 8)         -- driving north fast: the air comes from the north, the top leans north
x, y, z = rotZ(qx, qy, qz, qw)
check(y > 0 and math.abs(math.acos(z) - R.TILT_MAX) < 1e-9, 'at speed: leans towards the travel, at most TILT_MAX')

local camP = vec3(100, 200, 30)
core_camera = { getPosition = function() return camP end }
check(R.update(0, nil, 0.016) == false and #S.liveOf('TSStatic') == 0, 'dry: nothing made')
check(R.update(0.5, { x = 0, y = 0 }, 0.016) == true, 'rain: shown')
local o = S.liveOf('TSStatic')[1]
check(o and o.fields.shapeName == R.SHAPE and o.hidden == false, 'the shells made and shown')
check(o.pos.x == 100 and o.pos.y == 200 and o.pos.z == 30, 'at the camera')
check(o.fields.instanceColor == string.format('1 1 1 %.4f', math.floor(0.5 * R.GAIN / R.ALPHA_STEP + 0.5) * R.ALPHA_STEP), 'its strength')
local w0 = R.writes
camP = vec3(101, 200, 30)
R.update(0.505, { x = 0, y = 0 }, 0.016)
check(R.writes == w0 and o.pos.x == 101, 'follows the camera; the strength not written again for a hair')
R.update(0, nil, 0.016)
check(o.hidden == true and R.shown() == false, 'dry again: hidden')
local n = S.calls.setPosRot or 0
R.update(0, nil, 0.016)
check((S.calls.setPosRot or 0) == n, 'dry and hidden: no work')
check(o.fields.shapeName == R.SHAPES.near, 'one shell by default (the Deck paid ~4.3 ms for three)')
R.SHELLS = 'full'
R.update(0.5, nil, 0.016)
local o2 = S.liveOf('TSStatic')[1]
check(#S.liveOf('TSStatic') == 1 and o2.fields.shapeName == R.SHAPES.full, 'the full set: the object made again with its shape')
R.SHELLS = 'near'
R.reset()
check(#S.liveOf('TSStatic') == 0, 'reset: deleted')

T.done('test_rainstreaks')
