-- luajit mod/tests/test_srvrestart.lua  (the settings page's "Restart world server", srvrestart.lua; server srvrestart.py)
-- The settings waiting for a restart (the server's start-time values against the player's, Max RAM from the 'memory'
-- answer), the phases (a yes, the connection gone, the new session's hello, the world back), every phase's time limit
-- (never held for good), the hold (from the yes, let go for the resync's commit in single player, kept in BeamMP where
-- only our car is held, none outside our level or once the player resumed), the lines, and the banner's restart line
-- in place of "lost". core.lua's wiring with a fake server: test_core_robustness.lua 6b.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local sr = require('beamngEarth/srvrestart')
local banner = require('beamngEarth/banner')

-- pending
local order = { 'groundGrid', 'expAiTunnels', 'serverCores', 'maxRamGb', 'trees' }
local start = { groundGrid = '1', expAiTunnels = false, serverCores = 'auto', unknownNew = true }
local p = sr.pending({ groundGrid = '2', expAiTunnels = false, serverCores = 'auto', trees = 'high' }, start, false, order)
T.check(#p == 1 and p[1] == 'groundGrid', 'pending: the one that differs (settings the server does not name never count)')
p = sr.pending({ groundGrid = '1', expAiTunnels = true, serverCores = '4' }, start, true, order)
T.check(#p == 3 and p[1] == 'expAiTunnels' and p[2] == 'serverCores' and p[3] == 'maxRamGb',
  'pending: in the page\'s order, Max RAM from the memory answer')
T.check(#sr.pending({ groundGrid = '2' }, nil, false, order) == 0, 'an older server (no startSettings): nothing')
p = sr.pending({ groundGrid = '2', expAiTunnels = true, serverCores = 'auto' }, start, false, order, { 'groundGrid' })
T.check(#p == 1 and p[1] == 'expAiTunnels', 'set by hand in the server\'s environment (startFixed): never pending')
p = sr.pending({ groundGrid = '2' }, start, false, order, {})
T.check(#p == 1, 'startFixed as Lua\'s empty table: nothing left out')

local function ctx(t, over)
  local c = { clock = t, inLevel = true, resyncReady = false, worldReady = false, mp = false }
  for k, v in pairs(over or {}) do c[k] = v end
  return c
end

-- a whole restart
local r = sr.new()
T.check(sr.step(r, ctx(0)) == false and sr.text(r) == nil, 'idle: no hold, no line')
sr.request(r, 0, 's1')
T.check(r.phase == 'asked' and sr.step(r, ctx(0.1)) == false, 'asked: not held before the server says yes')
sr.answer(r, 0.2, true)
T.check(r.phase == 'stopping' and sr.step(r, ctx(0.3)) == true, 'yes: held')
T.check(sr.text(r, true):find('the car is held still', 1, true) ~= nil, 'the line says the car is held')
sr.hello(r, 0.4, 's1')
T.check(r.phase == 'stopping', 'the same session\'s hello is not the restarted server')
sr.disconnected(r, 0.5)
T.check(r.phase == 'starting' and sr.step(r, ctx(5)) == true, 'gone: waiting for it, held')
sr.hello(r, 6, 's2')
T.check(r.phase == 'loading' and r.newSession == 's2', 'a new session: loading')
T.check(sr.step(r, ctx(6.1)) == true, 'loading: held')
T.check(sr.step(r, ctx(6.2, { resyncReady = true })) == false, 'the resync ready: let go so it can commit')
T.check(sr.step(r, ctx(6.2, { resyncReady = true, mp = true })) == true, 'BeamMP: only our car is held, the commit needs no let-go')
T.check(sr.step(r, ctx(6.3)) == true, 'committed, the world not in yet: held again')
T.check(sr.step(r, ctx(7, { worldReady = true })) == false and r.phase == 'done', 'the world back: done, let go')
T.check(sr.text(r) == sr.TEXT.done and sr.level(r) == 'ok', 'the done line')
sr.step(r, ctx(7 + sr.DONE_S + 0.1))
T.check(r.phase == nil, 'the done line goes')

-- refusals and time limits
r = sr.new()
sr.request(r, 0, 's1')
sr.answer(r, 1, false, 'this world server was not started by the BeamNG.Earth app')
T.check(r.phase == 'failed' and sr.text(r):find('not started by the BeamNG.Earth app', 1, true) and sr.level(r) == 'warn',
  'refused: the reason')
T.check(sr.step(r, ctx(2)) == false, 'refused: never held')
for _, case in ipairs({
  { 'asked', function(x) end, sr.ASK_S, 'no answer' },
  { 'stopping', function(x) sr.answer(x, 0, true) end, sr.STOP_S, 'did not stop' },
  { 'starting', function(x) sr.answer(x, 0, true); sr.disconnected(x, 0) end, sr.DOWN_S, 'did not come back' },
}) do
  r = sr.new()
  sr.request(r, 0, 's1')
  case[2](r)
  T.check(r.phase == case[1], case[1] .. ': the phase')
  sr.step(r, ctx(case[3] - 0.5))
  T.check(r.phase == case[1], case[1] .. ': waited for within its limit')
  T.check(sr.step(r, ctx(case[3] + 0.5)) == false and r.phase == 'failed' and r.error:find(case[4], 1, true),
    case[1] .. ': past its limit, failed and let go: ' .. tostring(r.error))
end
r = sr.new()
sr.request(r, 0, 's1'); sr.answer(r, 0, true); sr.disconnected(r, 1); sr.hello(r, 2, 's2')
sr.step(r, ctx(2 + sr.LOAD_S + 0.5))
T.check(r.phase == 'done', 'the world never reports back: done after LOAD_S anyway (the car never held for good)')

-- no hold outside our level, or once the player resumed
r = sr.new()
sr.request(r, 0, 's1'); sr.answer(r, 0, true)
T.check(sr.step(r, ctx(1, { inLevel = false })) == false, 'not in our level (the main menu): nothing to hold')
r.letGo = true
T.check(sr.step(r, ctx(1)) == false, 'the player resumed: not held again')
sr.request(r, 2, 's1')
T.check(r.letGo == false, 'a new request: held again from its yes')
-- the new server goes too while loading: waiting for it again
r = sr.new()
sr.request(r, 0, 's1'); sr.answer(r, 0, true); sr.disconnected(r, 1); sr.hello(r, 2, 's2'); sr.disconnected(r, 3)
T.check(r.phase == 'starting', 'loading, gone again: waits for the server again')

-- the banner: the restart's line in place of "lost"
local function info(t)
  local i = { net = 'disconnected', session = false, everConnected = true, attempts = 2, port = 47613 }
  for k, v in pairs(t or {}) do i[k] = v end
  return i
end
local id, level, text = banner.compute(info(), 10)
T.check(id == 'lost', 'without a restart: lost')
id, level, text = banner.compute(info({ restart = sr.TEXT.starting, restartLevel = 'info' }), 10)
T.check(id == 'restart' and level == 'info' and text == sr.TEXT.starting, 'a restart: its line, not "lost"')
id, level, text = banner.compute(info({ restart = 'x', restartLevel = 'info', edge = 'hold' }), 10)
T.check(id == 'restart' and text:find('Paused at the edge', 1, true) and level == 'error', 'the edge guard\'s line still added')
local st = banner.update(0.1, info({ restart = sr.TEXT.done, restartLevel = 'ok', net = 'connected', session = true }))
T.check(st.id == 'restart' and st.visible, 'shown')
st = banner.update(0.1, info({ net = 'connected', session = true }))
T.check(st.id == 'ok' and not st.visible, 'over: hidden at once (no "connected" line after our own restart)')

T.done('test_srvrestart')
