-- luajit mod/tests/test_loadprogress.lua  (the status bar's loading line after a teleport)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local L = require('beamngEarth/loadprogress')
T.check(L.line(0, {}) == nil and not L.active(), 'no load: no line')
L.start(10)
T.check(L.line(10, { placed = false }) == 'Loading the ground', 'from the teleport request: the ground first')
local prog = { roads = { 34, 52 }, trees = { 10, 40 }, far = { 120, 405 } }
local line = L.line(15, { placed = true, progress = prog, pending = {} })
T.check(line == 'Loading 33%  |  roads & buildings 34/52  ·  trees 10/40  ·  distant land 120/405',
  'each part done / wanted, the share over all of them (ground + parts): ' .. tostring(line))
line = L.line(16, { placed = true, progress = { roads = { 52, 52 }, trees = { 40, 40 }, far = { 400, 405 } },
  pending = { roads = 3 } })
T.check(line:find('roads & buildings 52/52', 1, true) and line:find('distant land 400/405', 1, true)
  and not line:find('trees', 1, true), 'a part whose tiles are all in but still being placed stays; a finished one goes')
local withGround = L.line(17, { placed = true, progress = { ground = { 0, 1 }, roads = { 52, 52 }, trees = { 40, 40 },
  far = { 405, 405 } }, pending = {} })
T.check(withGround == 'Loading 99%  |  ground', 'the ground still to be replaced (graded block, full photo): listed '
  .. tostring(withGround))
local done = { ground = { 1, 1 }, roads = { 52, 52 }, trees = { 40, 40 }, far = { 405, 405 } }
T.check(L.line(20, { placed = true, progress = done, pending = {} }) == 'Loading 100%', 'all in: 100 %')
T.check(L.line(21, { placed = true, progress = done, pending = { trees = 1 } }):find('trees', 1, true),
  'something new comes: listed again')
T.check(L.line(22, { placed = true, progress = done, pending = {} }) == 'Loading 100%', 'in again')
T.check(L.line(24.9, { placed = true, progress = done, pending = {} }) == 'Loading 100%', 'not yet QUIET_S')
T.check(L.line(25.1, { placed = true, progress = done, pending = {} }) == nil and not L.active(), 'quiet for QUIET_S: over')
L.start(30)
T.check(L.line(31, { placed = true, progress = nil, pending = {} }) == 'Loading 99%', 'no counts yet: still loading')
T.check(L.line(30 + L.MAX_S + 1, { placed = false }) == nil and not L.active(), 'never longer than MAX_S')
L.start(1000)
T.check(L.line(1001, { placed = true, progress = { roads = { 0, 0 }, trees = { 0, 0 }, far = { 9, 9 } }, pending = {} })
  == 'Loading 100%', 'parts with nothing wanted (trees off) count as done')
L.reset()
T.check(not L.active(), 'reset')

-- the downloads the world waits on (the server's net: in flight, MB of this burst, MB/s)
T.check(L.downloadText({ 3, 12.4, 1.1 }) == 'downloading 3 files · 1.1 MB/s', 'downloads: files in flight and rate')
T.check(L.downloadText({ 1, 0.2, 0 }) == 'downloading 1 file', 'no rate yet: the count alone')
T.check(L.downloadText({ 14, 3.0, 1.1, 'worldwide satellite photos', true })
  == 'downloading 14 files · worldwide satellite photos, rate-limited · 1.1 MB/s', 'what the downloads are, rate-limited')
T.check(L.downloadText({ 2, 3.0, 8.4, 'terrain heights', false }) == 'downloading 2 files · terrain heights · 8.4 MB/s',
  'what they are, not held by our limit')
T.check(L.downloadText({ 2, 3.0, 8.4, '', false }) == 'downloading 2 files · 8.4 MB/s', 'an unknown host: as before')
T.check(L.downloadText({ 0, 5, 2 }) == nil and L.downloadText(nil) == nil, 'nothing in flight: no part')
L.start(2000)
line = L.line(2001, { placed = true, progress = { roads = { 3, 9 }, trees = { 1, 1 }, far = { 9, 9 }, net = { 2, 4.5, 0.9 } },
  pending = {} })
T.check(line and line:find('|  downloading 2 files · 0.9 MB/s  ·  roads & buildings 3/9', 1, true), 'the part first: '
  .. tostring(line))
line = L.line(2002, { placed = true, progress = { roads = { 9, 9 }, trees = { 1, 1 }, far = { 9, 9 }, net = { 2, 4.5, 0.9 } },
  pending = {} })
T.check(line == 'Loading 100%', 'downloads alone do not keep a load on (photos stream all the time): ' .. tostring(line))
L.reset()
-- driving: the line comes back while the world is behind the car, and ends once it has caught up for QUIET_S (at
-- speed far tiles always stream in: busy counts alone never end a driving load)
L.start(3000, 'driving')
T.check(L.kind() == 'driving', 'a driving load')
local busy = { placed = true, progress = { roads = { 3, 9 }, trees = { 1, 1 }, far = { 400, 420 } }, pending = {} }
busy.behind = true
T.check(L.line(3000, busy):find('roads & buildings 3/9', 1, true) ~= nil, 'behind: the counts as for a teleport')
busy.behind = false
T.check(L.line(3001, busy) == 'Loading 100%', 'caught up: done even with tiles still coming')
T.check(L.line(3001 + L.QUIET_S, busy) == nil and not L.active(), 'and gone after QUIET_S')
L.start(3100)
busy.behind = false
T.check(L.line(3100, busy):find('3/9', 1, true) ~= nil, 'a teleport load still waits for every count')
L.reset()
T.done('test_loadprogress')
