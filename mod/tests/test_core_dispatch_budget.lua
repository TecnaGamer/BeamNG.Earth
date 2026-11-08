-- Deterministic wall-time/engine stubs. A slow handler may exceed a budget by its own atomic cost, never by a
-- fraction of a growing backlog. Credits must acknowledge only handled metadata from the current transport.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local now = 0
function hptimer()
  local last = now
  return { stopAndReset = function() local d = now - last; last = now; return d end }
end
local inbox, sent = {}, {}
local current = setmetatable({}, { __mode = 'k' })
local rejectAck = false
package.loaded['beamngEarth/net'] = setmetatable({
  update = function() local q = inbox; inbox = {}; return q end,
  isConnected = function() return false end,
  state = function() return 'disconnected' end,
  isReady = function() return false end,
  isCurrent = function(msg) return current[msg] ~= false end,
  lease = function() return nil end,
  send = function(msg)
    if msg.type == 'stream_ack' and rejectAck then return false end
    sent[#sent + 1] = msg
    return true
  end,
}, { __index = function() return function() end end })
function getPlayerVehicle() return nil end
function getAllVehicles() return {} end
local core = require('beamngEarth/core')
require('beamngEarth/ui').visible = false
core._state().inOurLevel = true
local roads = require('beamngEarth/roads')
local handled = {}
roads.onTile = function(msg) handled[#handled + 1] = msg.key; now = now + 5 end
local function frame()
  now = now + 16
  local t0 = now
  core.onUpdate(0.016, 0.016)
  return now - t0
end
inbox = { { type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 's1', dispatchWindow = 32 } }
frame()
core._state().pending, core._state().loadingUntil = nil, 0
for k = 1, 100 do
  local msg = { type = 'roads_tile', key = k, streamSeq = k }
  current[msg] = true
  inbox[#inbox + 1] = msg
end
inbox[#inbox + 1] = { type = 'ground_ahead', anchorId = 1, z = 100 }
T.check(frame() == 5 and #handled == 1 and core.netBacklog() == 99,
  'a 100-message backlog stops after one 5 ms atomic handler, not ceil(backlog/16) handlers')
T.check(core._state().groundAhead.z == 100, 'urgent ground metadata bypasses the normal queue')
T.check(sent[#sent].type == 'stream_ack' and sent[#sent].seq == 1, 'only handled metadata acknowledged')
rejectAck = true
frame()
T.check(core._netq.sentAck == 1 and core._netq.ack == 2, 'a rejected ack remains pending')
rejectAck = false
local maxMs = 0
while core.netBacklog() > 0 do maxMs = math.max(maxMs, frame()) end
T.check(maxMs == 5 and #handled == 100, 'bounded dispatch eventually consumes every message')
local ordered = true
for k, v in ipairs(handled) do if k ~= v then ordered = false end end
T.check(ordered and sent[#sent].seq == 100, 'FIFO preserved and cumulative ack eventually catches up')
T.check(core._governor().frameSpent >= 5, 'dispatch work charged before the rest of the frame')

-- A transport replacement resets counters. An old queued message must not acknowledge the new connection's work.
local stale = { type = 'roads_tile', key = 'stale', streamSeq = 900 }
current[stale] = false
inbox = { { type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 's2', dispatchWindow = 32 }, stale,
  { type = 'roads_tile', key = 'new', streamSeq = 1 } }
current[inbox[3]] = true
local prior = #handled
frame()
T.check(#handled == prior + 1 and handled[#handled] == 'new' and core.netBacklog() == 0,
  'stale transport metadata cannot reach a world handler after the ownership snapshot')
T.check(core._netq.sentAck == 1 and sent[#sent].seq == 1, 'only the new transport consumes/returns current credit')

inbox = { { type = 'roads_tile', key = 'before2', streamSeq = 2 },
  { type = 'roads_tile', key = 'before3', streamSeq = 3 }, { type = 'roads_tile', key = 'before4', streamSeq = 4 } }
for _, msg in ipairs(inbox) do current[msg] = true end
frame()
T.check(core.netBacklog() == 2, 'the playing frame defers a real tail before the menu transition')
core._state().inOurLevel, core._state().active = false, false
inbox = { { type = 'roads_tile', key = 'menu', streamSeq = 5 } }
current[inbox[1]] = true
frame()
T.check(core._netq.sentAck == 5 and core.netBacklog() == 0,
  'main-menu dispatch also returns credits, so entering the level cannot deadlock')
T.check(handled[#handled - 2] == 'before3' and handled[#handled - 1] == 'before4' and handled[#handled] == 'menu',
  'a menu transition preserves FIFO across the deferred tail and newly received messages')
core._state().inOurLevel = true

-- Legacy servers cannot pace their sender: retain the old drain rule for that negotiated compatibility path.
inbox = { { type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 'old' } }
frame()
local before = #handled
for k = 1, 80 do inbox[#inbox + 1] = { type = 'roads_tile', key = k } end
frame()
T.check(#handled - before == 5, 'older server retains its existing catch-up rule')
-- With credits, a frame whose decode (or urgent answers) alone used the whole budget still handles one ordinary
-- message and acknowledges it: handling none acknowledged nothing, and the sender's window never reopened.
while core.netBacklog() > 0 do frame() end
inbox = { { type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 's3', dispatchWindow = 32 } }
frame()
local slowDecode = package.loaded['beamngEarth/net'].update
package.loaded['beamngEarth/net'].update = function() now = now + 6; return slowDecode() end
before = #handled
for k = 1, 3 do
  local msg = { type = 'roads_tile', key = 'slow' .. k, streamSeq = k }
  current[msg] = true
  inbox[#inbox + 1] = msg
end
frame()
T.check(#handled - before == 1 and sent[#sent].type == 'stream_ack' and sent[#sent].seq == 1,
  'a frame whose decode exceeded the budget still makes progress and returns a credit')
package.loaded['beamngEarth/net'].update = slowDecode
T.done('test_core_dispatch_budget')
