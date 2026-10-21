-- luajit mod/tests/test_messagequeue.lua
-- Handler costs are deterministic wall milliseconds; no game, sockets or sleeping required.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local queue = require('beamngEarth/messagequeue')
local now = 0
local q = queue.new({ clock = function() return now end })
local polls, seen, failures = 0, {}, {}
local incoming = {}
local function poll() polls = polls + 1; return incoming end
local function handle(msg)
  seen[#seen + 1] = msg.id
  now = now + msg.cost
  if msg.fail then error('bad payload') end
end
local function onError(msg) failures[#failures + 1] = msg.id end
local function step(ms) return q:update(poll, handle, ms or queue.DRIVING_MS, onError) end

for i = 1, 12 do incoming[i] = { type = 'roads_tile', id = i, cost = 1 } end
local s = step()
T.check(s.processed == 3 and s.queued == 9 and s.ms == 3, '3 ms driving budget splits a burst at the wall deadline')
T.check(q.head == 4 and q.tail == 12 and q.items[1] == nil and q.items[4].id == 4,
  'consumed references are released without shifting the queue')
T.check(s.maxQueued == 12 and s.maxProcessed == 3 and s.maxHandlerMs == 1 and s.maxHandlerType == 'roads_tile',
  'diagnostics report backlog, processed count and slowest atomic handler')
step(); step(); step()
T.check(polls == 1 and #seen == 12 and seen[1] == 1 and seen[12] == 12 and q.stats.queued == 0,
  'one received batch drains FIFO before the next network poll')
T.check(q.head == 1 and q.tail == 0 and next(q.items) == nil, 'a drained batch releases its storage and resets indices')

incoming = { { type = 'navgraph', id = 13, cost = 12 }, { type = 'roads_tile', id = 14, cost = 1 } }
s = step()
T.check(s.processed == 1 and s.queued == 1 and s.ms == 12 and s.maxHandlerMs == 12 and s.maxHandlerType == 'navgraph',
  'one over-budget atomic handler still progresses and stops the burst')
step()
T.check(seen[#seen] == 14 and s.total == 14, 'the next message remains available after an oversized handler')

incoming = {}
for i = 1, 8 do incoming[i] = { type = 'veg_tile', id = 14 + i, cost = 2 } end
s = step(queue.LOADING_MS)
T.check(s.processed == 4 and s.queued == 4 and s.ms == 8 and s.budgetMs == 8, 'loading mode permits 8 ms rather than 3 ms')
step(queue.LOADING_MS)

incoming = { { type = 'roads_tile', id = 23, cost = 1, fail = true },
  { type = 'far_tile', id = 24, cost = 1 }, { type = 'far_swap', id = 25, cost = 1 } }
s = step()
T.check(s.processed == 3 and s.errors == 1 and failures[1] == 23 and seen[#seen] == 25,
  'an errored handler is reported once and does not discard later messages')

-- A real net.update batch can end in _disconnected after its final complete frames. Even if
-- dispatch spans frames, a new connection must not overtake the old batch's last tile/swap.
local session, anchor, accepted, order = 'old', 1, {}, {}
incoming = { { type = 'tile', session = 'old', anchor = 1 }, { type = 'anchor', anchor = 2 },
  { type = 'tile', session = 'old', anchor = 1 }, { type = 'swap', anchor = 2 }, { type = '_disconnected' } }
local function ordered(msg)
  now = now + 3
  order[#order + 1] = msg.type
  if msg.type == 'anchor' then anchor = msg.anchor
  elseif msg.type == 'tile' and msg.session == session and msg.anchor == anchor then accepted[#accepted + 1] = msg.anchor
  elseif msg.type == '_disconnected' then session, anchor = nil, nil
  elseif msg.type == '_connected' then session, anchor = 'new', 1 end
end
local beforePolls = polls
q:update(poll, ordered, 3)
incoming = { { type = '_connected' } } -- offered by a later poll, not appended ahead of the disconnect
for _ = 1, 4 do q:update(poll, ordered, 3) end
T.check(polls == beforePolls + 1 and order[5] == '_disconnected' and session == nil,
  'terminal disconnect follows all old-session messages across budget boundaries')
T.check(#accepted == 1 and accepted[1] == 1, 'a deferred stale-anchor tile still reaches the normal handler check in order')
q:update(poll, ordered, 3)
T.check(order[6] == '_connected' and session == 'new', 'the next session starts only after the old FIFO drains')

-- Continuing traffic cannot grow the Lua queue while a batch is pending. The socket remains
-- unread until the one bounded net.update batch is consumed; its next poll sees the later batch.
incoming = {}
for i = 1, 200 do incoming[i] = { type = 'roads_tile', id = i, cost = 1 } end
beforePolls = polls
for _ = 1, 20 do step() end
T.check(polls == beforePolls + 1 and q.stats.queued == 140 and q.stats.maxQueued == 200,
  'sustained traffic uses backpressure rather than accumulating a new batch every frame')
q:clear()
T.check(q.stats.queued == 0 and next(q.items) == nil, 'explicit connection close can discard old-session queued references')

-- Receiving/decoding is included in the wall deadline, while the first handler always runs.
incoming = { { type = 'roads_tile', id = 201, cost = 1 }, { type = 'roads_tile', id = 202, cost = 1 } }
s = q:update(function() now = now + 5; return poll() end, handle, 3, onError)
T.check(s.processed == 1 and s.queued == 1 and s.ms == 6, 'a slow receive still permits exactly one handler for progress')

T.done('test_messagequeue')
