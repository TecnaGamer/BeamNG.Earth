-- luajit mod/tests/test_rebase.lua  (the pure origin-rebase prefetch/commit rule, V1_1_SPEC 1b)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local rebase = require('beamngEarth/rebase')
local d = rebase.decide
T.check(rebase.STALE_DIST == 192 and rebase.KEEP_EDGE == 150, 'default: the 1 km block (1024 x 1 m)')
rebase.configure(2048)     -- (the v1.3.1 2 km block, the rules below first)

T.check(rebase.distance == 15000, 'default rebase distance 15 km')
rebase.setDistance(10000) -- the rules below are written for 10 km
T.check(d(9699, nil, nil, 0, 0) == nil, 'no prefetch below distance - 300 m')
T.check(d(9700, nil, nil, 0, 0) == 'prefetch', 'prefetch at distance - 300 m')
T.check(d(9800, nil, nil, 3, 5) == nil, 'no prefetch before retryAt')
T.check(d(9800, nil, nil, 5, 5) == 'prefetch', 'prefetch after retryAt')
T.check(d(9900, { purpose = 'rebase' }, nil, 0, 0) == nil, 'request in flight: wait')
T.check(d(10300, { purpose = 'rebase' }, nil, 0, 0) == nil, 'not ready past the distance: wait')
T.check(d(9900, { purpose = 'rebase', ready = true }, 200, 0, 0) == nil, 'ready below the distance: hold')
T.check(d(10000, { purpose = 'rebase', ready = true }, 300, 0, 0) == 'commit', 'ready at the distance: commit')
T.check(d(11000, { purpose = 'rebase', ready = true }, 400, 0, 0) == 'commit', 'ready late: commit at once')
T.check(d(10050, { purpose = 'rebase', ready = true }, 451, 0, 0) == 'discard', 'prepared terrain no longer under the player: discard')
T.check(d(9710, { purpose = 'rebase', ready = true }, 502, 0, 0) == nil,
  'ready below the distance: hold, though centred for the player 300 m ahead (no re-request loop)')
T.check(d(9800, { purpose = 'rebase', ready = true }, 900, 0, 0) == nil, 'player went elsewhere: judged at the distance')
T.check(d(10000, { purpose = 'rebase', ready = true }, 900, 0, 0) == 'discard', 'player went elsewhere: discard at the distance')
T.check(d(50000, { purpose = 'teleport', ready = true }, 0, 0, 0) == nil, 'teleport is not distance-driven')

-- configurable 2-16 km (console beamngEarth_core.setRebaseDistance / UI slider); above 16384 m
-- the float32 position step doubles
T.check(rebase.setDistance(500) == 2000, 'clamped to 2 km')
T.check(rebase.setDistance(99999) == 16000, 'clamped to 16 km')
T.check(rebase.setDistance('abc') == 16000, 'invalid input keeps the value')
rebase.setDistance(4000)
T.check(d(3699, nil, nil, 0, 0) == nil and d(3700, nil, nil, 0, 0) == 'prefetch', 'prefetch follows the distance')
T.check(d(4000, { purpose = 'rebase', ready = true }, 0, 0, 0) == 'commit', 'commit follows the distance')
rebase.setDistance(10000)

-- M4 (docs/V1_4_SPEC.md 3.5): STALE_DIST per block size, and the stale-anchor rule (512² only)
T.check(rebase.STALE_DIST == 450 and rebase.KEEP_EDGE == 0, '1024: STALE_DIST 450, stale anchors discarded (v1.3.1)')
T.check(d(10050, { purpose = 'rebase', ready = true }, 451, 0, 0, 500) == 'discard', '1024: stale is discarded however far inside')
T.check(rebase.configure(1024) == 1024 and rebase.STALE_DIST == 192 and rebase.KEEP_EDGE == 150, '512: STALE_DIST 192, keep edge 150 m')
T.check(d(10000, { purpose = 'rebase', ready = true }, 192, 0, 0, 318) == 'commit', '512: at 192 m: commit')
T.check(d(10000, { purpose = 'rebase', ready = true }, 250, 0, 0, 260) == 'commit',
  '512: stale (250 m) but 260 m inside every edge: commit (terrain-follow prepares next)')
T.check(d(10000, { purpose = 'rebase', ready = true }, 360, 0, 0, 150) == 'commit', '512: exactly 150 m inside: commit')
T.check(d(10000, { purpose = 'rebase', ready = true }, 361, 0, 0, 149) == 'discard', '512: stale and < 150 m from an edge: discard')
T.check(d(10000, { purpose = 'rebase', ready = true }, 600, 0, 0, -90) == 'discard', '512: off the prepared terrain: discard')
T.check(d(10000, { purpose = 'rebase', ready = true }, 300, 0, 0, nil) == 'discard', '512: no edge distance: discard')
T.check(d(9900, { purpose = 'rebase', ready = true }, 300, 0, 0, 200) == nil, '512: below the distance: hold')
T.check(rebase.aheadLead(nil) == rebase.LATENCY_S + rebase.LEAD_EXTRA_S and rebase.aheadLead(3) == 4,
  're-request lead = measured anchor latency + 1 s')
T.check(rebase.configure(2048) == 2048 and rebase.STALE_DIST == 450 and rebase.KEEP_EDGE == 0, 'configure(2048) restores v1.3.1')
T.check(rebase.configure(nil) == 1024, 'no width: 1 km')

T.done('test_rebase')
