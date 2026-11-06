-- Controlled non-blocking socket backpressure; no engine or live network.
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local framing = require('beamngEarth/framing')
jsonEncode = function(msg) return msg.payload end
jsonDecode = function() return {} end

local mode, peers
package.preload.socket = function()
  return { tcp = function()
    local c = { wire = '', calls = 0 }
    peers[#peers + 1] = c
    function c:settimeout() end
    function c:setoption() end
    function c:connect() return 1 end
    function c:getpeername() return '127.0.0.1', 47613 end
    function c:close() self.closed = true end
    function c:receive()
      local data = mode.incoming or ''
      mode.incoming = nil
      return nil, 'timeout', data
    end
    function c:send(data, first, last)
      self.calls = self.calls + 1
      first, last = first or 1, last or #data
      if mode.error then return nil, mode.error, first - 1 end
      local n = math.min(last - first + 1, mode.allow)
      if mode.allow ~= math.huge then mode.allow = mode.allow - n end
      self.wire = self.wire .. data:sub(first, first + n - 1)
      if first + n - 1 == last then return last end
      return nil, 'timeout', first + n - 1
    end
    return c
  end }
end

local function fresh(allow, beforeHello)
  mode, peers = { allow = allow }, {}
  package.loaded['beamngEarth/net'] = nil
  local net = require('beamngEarth/net')
  local events = net.update(0)
  T.check(net.isConnected(), 'fixture connects')
  if not beforeHello then net.serverHello(nil, nil) end
  return net, events[1]
end

do
  local net, oldEvent = fresh(math.huge, true)
  mode.incoming = framing.encode('old hello')
  local oldHello = net.update(0)[1]
  net.close()
  local newEvent = net.update(0)[1]
  T.check(not net.isCurrent(oldEvent) and net.isCurrent(newEvent), 'queued connect event belongs to its transport')
  net.serverHello('server-A', 'old-owner', oldHello)
  local session, id = net.lease()
  T.check(not net.isReady() and session == 'server-A' and id == 'old-owner', 'delayed old hello retains ownership without unlocking new transport')
  mode.incoming = framing.encode('new hello')
  local newHello = net.update(0)[1]
  net.serverHello('server-A', 'current-owner', newHello)
  T.check(net.isReady(), 'current transport hello unlocks world requests')
  net.serverHello('server-A', 'stale-owner', oldHello)
  session, id = net.lease()
  T.check(id == 'current-owner', 'old hello cannot overwrite an established replacement')
  net.close()
end

do
  local net = fresh(math.huge, true)
  T.check(not net.isReady() and not net.send({type='pose',payload='premature'}), 'pose blocked before hello')
  T.check(net.send({type='hello',payload='hello'}), 'hello allowed before handshake completes')
  net.serverHello('server-A', 'ownership-token')
  T.check(net.isReady() and net.send({type='pose',payload='ready'}), 'world traffic follows server hello')
  net.close()
  net.update(0)
  T.check(not net.isReady() and not net.send({type='pose',payload='premature again'}), 'reconnect needs a fresh handshake')
  net.close()
end

do
  local net = fresh(3)
  net.setProfiling(true)
  local a, b = 'first payload', 'second payload'
  T.check(net.send({payload=a}), 'partial frame accepted')
  T.check(net.send({payload=b}), 'later frame queued behind partial frame')
  T.check(net.profileState().queuedBytes == #a + #b + 8 - 3, 'unsent bytes include partial head')
  T.check(net.profileState().retainedBytes == #a + #b + 8, 'retained bytes include sent prefix')
  mode.allow = math.huge
  net.update(0)
  T.check(peers[1].wire == framing.encode(a) .. framing.encode(b), 'partial writes preserve exact FIFO framing')
  T.check(net.profileState().queuedMessages == 0 and net.profileState().retainedBytes == 0, 'drained frames released')
  T.check(net.profileState().sentBytes == #a + #b + 8, 'partial send accounting')
  net.close()
end

do
  local net = fresh(30)
  net.serverHello('server-A', 'ownership-token')
  net.MAX_SEND_BYTES = 64
  net.send({payload=string.rep('a', 40)})
  T.check(not net.send({payload=string.rep('b', 20)}), 'cap counts retained prefix, not only unsent bytes')
  T.check(not net.isConnected(), 'overflow closes transport')
  T.check(net.profileState().retainedBytes == 0, 'overflow releases queue strings')
  local events = net.update(0)
  T.check(#events == 1 and events[1].type == '_disconnected', 'one disconnect event')
  mode.allow = math.huge
  net.update(2)
  local session, id = net.lease()
  T.check(session == 'server-A' and id == 'ownership-token', 'ownership survives reconnect')
  T.check(#peers == 2 and peers[2].wire == '', 'new stream receives no previous partial-frame tail')
  net.close()
end

do
  local net = fresh(0)
  net.MAX_SEND_MESSAGES = 2
  T.check(net.send({payload='one'}) and net.send({payload='two'}), 'bounded queue accepts two')
  T.check(not net.send({payload='three'}), 'message count bounded independently of bytes')
  T.check(not net.isConnected(), 'message cap closes blocked transport')
  net.close()
end

do
  local net = fresh(math.huge)
  net.SEND_BYTES_PER_FRAME, net.SEND_CALLS_PER_FRAME = 16, 2
  net.update(0)
  local payload = string.rep('x', 96)
  net.send({payload=payload})
  local initial = #peers[1].wire
  net.send({payload='end'})
  T.check(initial == 16 and #peers[1].wire == initial, 'send calls share one frame byte budget')
  for _ = 1, 10 do
    local bytes, calls = #peers[1].wire, peers[1].calls
    net.update(0)
    T.check(#peers[1].wire - bytes <= 16 and peers[1].calls - calls <= 2, 'bounded work per update')
  end
  T.check(peers[1].wire == framing.encode(payload) .. framing.encode('end'), 'frame budgeting does not truncate data')
  net.close()
end

do
  local net = fresh(0)
  local a, b = net.send({payload='blocked'}), nil
  mode.error = 'closed'
  b = net.send({payload='next'})
  T.check(a and not b, 'send reports a transport failure')
  T.check(net.profileState().retainedBytes == 0, 'fatal send clears backlog')
  net.close()
end

do
  local net = fresh(math.huge)
  local frames = framing.newReassembler()
  local accepted = true
  for i = 1, 10000 do
    if i % 20 == 0 then net.update(0) end
    accepted = net.send({payload=tostring(i)}) and accepted
  end
  net.update(0)
  frames:feed(peers[1].wire)
  local ordered = true
  for i = 1, 10000 do ordered = frames:pop() == tostring(i) and ordered end
  T.check(accepted and ordered and frames:pop() == nil, '10,000 frames delivered in order')
  T.check(net.profileState().queuedBytes == 0 and net.profileState().retainedBytes == 0, 'no queue retained after soak')
  net.close()
end

T.done('test_net_backpressure')
