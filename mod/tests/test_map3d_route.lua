-- luajit mod/tests/test_map3d_route.lua  (the 3D map's GPS line heights: map3d.routeHeights, the route's elevation
-- profile by distance along its points; the user, 2026-11-08: "the gps line isn't fully drawn in the 3d map")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')
package.path = (arg[0]:match('^(.*)[/\\]') or '.') .. '/../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local m3 = require('beamngEarth/map3d')

-- a route north along a meridian: 0.01 deg of latitude is 1112 m
local pts = {}
for i = 0, 10 do pts[#pts + 1] = {40 + i * 0.01, -105} end
local elev = {{0, 1600}, {5560, 2100}, {11120, 1700}}
local h = m3.routeHeights(pts, elev)
T.check(#h == #pts, 'a height for every point')
T.check(math.abs(h[1] - 1600) < 1e-6, 'the start: the profile\'s first height')
T.check(math.abs(h[6] - 2100) < 1, 'half way (5,560 m): the profile\'s middle height, not sea level')
T.check(math.abs(h[3] - (1600 + 500 * 2224 / 5560)) < 1, 'between profile points: interpolated by distance')
T.check(math.abs(h[11] - 1700) < 1, 'the end: the profile\'s last height')

-- past the profile's end (the server resampled a little short): its last height, not nil
local h2 = m3.routeHeights({{40, -105}, {40.2, -105}}, {{0, 10}, {1000, 20}})
T.check(h2[2] == 20, 'past the profile: its last height')

-- string keys (the UI bridge can hand tables over as {"1": ..., "2": ...})
local h3 = m3.routeHeights({{['1'] = 40, ['2'] = -105}, {['1'] = 40.01, ['2'] = -105}}, {{['1'] = 0, ['2'] = 5}, {['1'] = 1112, ['2'] = 15}})
T.check(math.abs(h3[2] - 15) < 0.1, 'string keys read as well')

-- no profile (an older server, or none yet): no heights, the line falls back to sea level and the loaded ground
T.check(next(m3.routeHeights(pts, nil)) == nil and next(m3.routeHeights(pts, {})) == nil, 'no profile: no heights')

T.done()
