-- Outgoing transport lifecycle with scripted LuaSocket backpressure. No BeamNG process or native
-- crash attribution: these regressions establish bounded memory, ordering and reconnect behavior.
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local framing = require('beamngEarth/framing')

jsonEncode = function(m) return m.payload or m.type end
jsonDecode = function() return {} end

local function connect(limit)
  local sockets, controls = {}, { limit = limit }
  package.loaded.socket = nil
  package.preload.socket = function()
    return { tcp = function()
      local s = { limit = controls.limit, sent = {}, starts = {}, closed = false }
      function s:settimeout(value) self.timeout = value end
      function s:setoption() end
      function s:connect() return true end
      function s:getpeername() return '127.0.0.1' end
      function s:receive() return nil, 'timeout', '' end
      function s:close() self.closed = true end
      function s:send(data, first)
        self.starts[#self.starts + 1] = first
        if self.fail then return nil, self.fail, first - 1 end
        local n = math.min(#data - first + 1, self.limit or #data)
        if n > 0 then self.sent[#self.sent + 1] = data:sub(first, first + n - 1) end
        if n < #data - first + 1 or self.completeAsTimeout then return nil, 'timeout', first + n - 1 end
        return #data
      end
      sockets[#sockets + 1] = s
      return s
    end }
  end
  package.loaded['beamngEarth/net'] = nil
  local net = require('beamngEarth/net')
  local events = net.update(0)
  T.check(#events == 1 and events[1].type == '_connected', 'initial connection event')
  T.check(sockets[1].timeout == 0, 'socket remains nonblocking')
  return net, sockets, controls
end

local function empty(net)
  local q = net.sendQueueState()
  return q.messages == 0 and q.bytes == 0 and q.pendingBytes == 0 and q.stalledS == 0
end

do -- Reproduce the reported unbounded queue fixture, now with the real module's default caps.
  local net, sockets = connect(0)
  local accepted, maxMessages, maxBytes = 0, 0, 0
  local bounded = true
  for i = 1, 20000 do
    if net.send({ payload = string.rep('x', 500) .. tostring(i) }) then accepted = accepted + 1 end
    local q = net.sendQueueState()
    maxMessages, maxBytes = math.max(maxMessages, q.messages), math.max(maxBytes, q.bytes)
    bounded = bounded and q.messages <= net.MAX_OUT_MESSAGES and q.bytes <= net.MAX_OUT_BYTES
  end
  T.check(bounded and accepted == net.MAX_OUT_MESSAGES, '20,000 backpressured sends never exceed caps')
  T.check(maxMessages == 256 and maxBytes > 0, 'fixture exercised actual retained queue before failure')
  T.check(net.state() == 'disconnected' and sockets[1].closed and empty(net), 'overflow closes socket and releases data')
  local events = net.update(0)
  T.check(#events == 1 and events[1].type == '_disconnected' and events[1].error:find('queue limit', 1, true),
    'one explicit disconnect event, not silent control-message loss')
  T.check(#net.update(0) == 0, 'no repeated disconnect events from refused sends')
end

do -- Bytes include the prefix of a partially sent frame while Lua still retains that string.
  local net, sockets = connect(0)
  net.MAX_OUT_BYTES = 20
  local s = sockets[1]
  T.check(net.send({ payload = 'abcdefgh' }), '12-byte encoded frame queued')
  s.limit = 5
  net.update(1)
  local q = net.sendQueueState()
  T.check(q.bytes == 12 and q.pendingBytes == 7 and q.messages == 1, 'partial prefix remains charged as retained bytes')
  s.limit = 2
  net.update(1)
  T.check(s.starts[#s.starts] == 6 and net.sendQueueState().pendingBytes == 5, 'absolute partial index resumes at byte six')
  s.limit = 0
  T.check(net.send({ payload = 'ijkl' }), 'second frame reaches exact 20-byte retained cap')
  q = net.sendQueueState()
  T.check(q.bytes == 20 and q.pendingBytes == 13 and q.messages == 2, 'remaining and retained byte accounting differ correctly')
  T.check(not net.send({ payload = '' }) and empty(net), 'retained-byte overflow fails closed even with fewer pending bytes')
end

do -- A single oversized frame is rejected before framing creates another copy.
  local net = connect(0)
  net.MAX_OUT_BYTES = 8
  T.check(not net.send({ payload = '12345' }) and empty(net), 'oversized encoded frame not retained')
  T.check(net.lastError == 'outgoing frame exceeds byte limit', 'oversize reason is explicit')
end

do -- Partial sends retain exact control-message ordering, with neither dropped nor repeated bytes.
  local net, sockets = connect(0)
  local s = sockets[1]
  net.send({ payload = 'first-control' })
  net.send({ payload = 'second-control' })
  s.limit = 5
  for _ = 1, 20 do net.update(0.1) end
  T.check(table.concat(s.sent) == framing.encode('first-control') .. framing.encode('second-control'),
    'ordered controls survive repeated partial writes exactly')
  T.check(empty(net) and net.isConnected(), 'complete drain resets retained and pending counters')
  net.update(100)
  T.check(net.isConnected(), 'an idle connection with no queued data never hits send-stall timeout')
end

do -- A timeout can report that the final byte reached the kernel: never resend its header or suffix.
  local net, sockets = connect(0)
  net.send({ payload = 'done' })
  local s = sockets[1]
  s.limit, s.completeAsTimeout = 100, true
  net.update(1)
  T.check(empty(net) and table.concat(s.sent) == framing.encode('done'), 'final-byte timeout releases completed frame')
end

do -- No progress disconnects at the deadline; progress resets it, even before a whole frame finishes.
  local net, sockets, controls = connect(0)
  local s = sockets[1]
  net.send({ payload = 'waiting' })
  net.update(9.5)
  T.check(net.isConnected() and net.sendQueueState().stalledS == 9.5, 'stalled connection survives before deadline')
  s.limit = 1
  net.update(0.5)
  T.check(net.isConnected() and net.sendQueueState().stalledS == 0, 'partial progress resets stall deadline')
  s.limit = 0
  net.update(9.5)
  local events = net.update(0.5)
  T.check(#events == 1 and events[1].type == '_disconnected' and events[1].error:find('send stalled', 1, true),
    'no progress at deadline uses ordinary disconnect event')
  T.check(empty(net) and s.closed, 'timeout clears retained state')
  controls.limit = nil
  T.check(#net.update(1) == 0 and #sockets == 1, 'normal reconnect interval is preserved')
  events = net.update(1)
  T.check(#events == 1 and events[1].type == '_connected' and #sockets == 2 and net.lastError == nil,
    'normal reconnect establishes a clean new stream')
  T.check(net.send({ payload = 'new-session-control' }), 'new session accepts control')
  T.check(table.concat(sockets[2].sent) == framing.encode('new-session-control') and empty(net),
    'old incomplete frames never leak into the reconnected stream')
end

do
  local net, sockets = connect(0)
  net.send({ payload = 'before-close' })
  sockets[1].fail = 'closed'
  T.check(not net.send({ payload = 'after-close' }) and empty(net), 'send reports immediate socket failure and clears accounting')
  net.close()
  T.check(empty(net) and #net.update(0) == 1, 'explicit close retains existing immediate reconnect behavior')
end

T.done('test_net')
