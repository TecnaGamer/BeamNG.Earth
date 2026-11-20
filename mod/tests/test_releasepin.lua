-- luajit mod/tests/test_releasepin.lua  (the map data's release of a level's world, releasepin.lua; server
-- overturepick.py, overture.effective). The pin comes from the LIVE connection's hello only; another release, or none
-- after a known one, is a mismatch (core.lua holds the world: test_core_robustness.lua 6c).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local rp = require('beamngEarth/releasepin')

local A, B = '2026-11-18.0', '2099-01-16.0'
local s = rp.new()
T.check(rp.hello(s, A, false) == 'ok' and rp.pinned(s) == nil, 'a hello at the menu: nothing pinned')
T.check(rp.levelStart(s) == A, 'a level started while connected: the live connection\'s release')
T.check(rp.hello(s, A, true) == 'ok', 'a restarted server of the same release: ok')
T.check(rp.hello(s, B, true) == 'mismatch' and rp.pinned(s) == A, 'another release mid-level: a mismatch, pin kept')
T.check(rp.hello(s, nil, true) == 'mismatch' and rp.hello(s, '../x', true) == 'mismatch',
  'a server too old to say (or a malformed id) after a known pin: a mismatch (Codex high 2)')
T.check(rp.hello(s, A, true) == 'ok', 'the pinned release back: ok')

-- Codex high 1: the level loaded again while the server is down is pinned by the NEXT server, not the gone one's
s = rp.new()
rp.hello(s, A, false)
rp.levelStart(s)
rp.disconnected(s)
T.check(rp.levelStart(s) == nil, 'a level started while disconnected: not pinned')
T.check(rp.hello(s, B, true) == 'ok' and rp.pinned(s) == B, 'the first hello in the level pins it')
rp.disconnected(s)
T.check(rp.hello(s, A, true) == 'mismatch', '... and holds it after a reconnect')

-- a level pinned by an older server (none): only that same server session back (Codex round 1: two older builds
-- read different releases and neither says which)
s = rp.new()
rp.hello(s, nil, false, 's1')
T.check(rp.levelStart(s) == rp.NONE, 'an older server: pinned to none')
rp.disconnected(s)
T.check(rp.hello(s, nil, true, 's1') == 'ok', 'the same older server session back: ok')
T.check(rp.hello(s, nil, true, 's2') == 'mismatch', 'another older server session: a mismatch')
T.check(rp.hello(s, nil, true, nil) == 'mismatch', 'one without a session: a mismatch')
T.check(rp.hello(s, A, true, 's3') == 'mismatch', 'a server that says a release after none: a mismatch')
T.check(rp.levelStart(s) == A, 'the next level start takes the live one')

T.done('test_releasepin')
