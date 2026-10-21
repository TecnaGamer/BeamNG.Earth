-- luajit mod/tests/test_core_dispatch.lua
-- Exercise the actual core dispatch/lifecycle wiring with deterministic costly tile handlers.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local now, polls, closes, incoming, seen, errors = 0, 0, 0, {}, {}, {}
package.loaded['beamngEarth/stopwatch'] = {
  clock = function() return function() return now end end,
  new = function() local start = now; return function() return now - start end end,
}
package.loaded['beamngEarth/net'] = {
  PORT = 47613,
  update = function() polls = polls + 1; local batch = incoming; incoming = {}; return batch end,
  isConnected = function() return false end,
  close = function() closes = closes + 1; incoming = {} end,
}
log = function(level, _, text) if level == 'E' then errors[#errors + 1] = text end end
local core = require('beamngEarth/core')
local roads = require('beamngEarth/roads')
roads.onTile = function(msg, anchor)
  now = now + (msg.cost or 2)
  seen[#seen + 1] = { id = msg.id, anchor = anchor }
  if msg.fail then error('injected road file failure') end
end
scenetree.MissionGroup = nil -- update stops after dispatch; no level/world rendering is needed
local function frame() core.onUpdate(0.016, 0.016) end
local st = core._state()
st.inOurLevel, st.anchorId, st.sessionId = true, 11, 'old'
incoming = { { type = 'roads_tile', id = 1 }, { type = 'roads_tile', id = 2, fail = true },
  { type = 'roads_tile', id = 3 }, { type = '_disconnected', error = 'closed by server' } }
frame()
T.check(#seen == 2 and st.messages.processed == 2 and st.messages.queued == 2 and st.messages.ms == 4,
  'core applies the 3 ms driving dispatch budget and exposes backlog/progress')
T.check(#errors == 1 and errors[1]:find('roads_tile') and st.sessionId == 'old',
  'handler errors are logged once; a deferred disconnect does not overtake earlier messages')
local stats = st.messages
core.onClientEndMission()
T.check(core._state().messages == stats and stats.queued == 2, 'level reset retains the continuous connection FIFO and diagnostics')
frame()
T.check(polls == 1 and #seen == 3 and core._state().sessionId == nil and stats.queued == 0,
  'the menu drains the pending final tile before disconnect after a level reset, without another poll')
T.check(seen[3].anchor == nil, 'deferred tile sees the reset anchor rather than reviving its old level anchor')

st = core._state()
st.loadingUntil = st.clock + 10
incoming = {}
for i = 1, 10 do incoming[i] = { type = 'roads_tile', id = i + 3 } end
frame()
T.check(st.messages.processed == 4 and st.messages.queued == 6 and st.messages.budgetMs == 8,
  'core uses the 8 ms loading dispatch budget')
core.reconnect()
T.check(closes == 1 and st.messages.queued == 0 and st.sessionId == nil,
  'explicit reconnect discards old-session decoded messages along with the wire buffer')
frame()
T.check(#seen == 7 and polls == 3, 'discarded old-session messages never run after reconnect')

st.loadingUntil, st.anchorId = 0, 7
st.terrainPending = { requestId = 30, cx = 0, cy = 0 }
incoming = { { type = 'roads_tile', id = 14, cost = 4 },
  { type = 'terrain_ready', requestId = 30, anchorId = 7, size = 1024, cx = 0, cy = 0 } }
frame()
T.check(st.messages.queued == 1 and not st.terrainPending.ready, 'a slow atomic handler defers the following terrain answer')
st.anchorId = 8 -- the normal core stale-anchor guard must still apply at dispatch time
frame()
T.check(not st.terrainPending.ready and st.messages.queued == 0,
  'deferred terrain answer for the old anchor is rejected by the existing core guard')

T.done('test_core_dispatch')
