-- luajit mod/tests/test_net_decode.lua  (net.lua: decoding spread over frames, DECODE_BYTES_PER_FRAME; order kept,
-- urgent types at once, everything before a disconnect, all at once when the caller says unlimited)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local framing = require('beamngEarth/framing')

-- payloads: '{"type":"<t>","n":<n>}' padded to a size; the decoder records what it decoded
local decodedBytes = 0
jsonEncode = function(msg) return msg.payload end
jsonDecode = function(s)
  decodedBytes = decodedBytes + #s
  local ty, n = s:match('^{"type":"([%w_]+)","n":(%d+)')
  if not ty then ty, n = s:match('^{"n":(%d+),"type":"([%w_]+)"'); ty, n = n, ty end
  return { type = ty, n = tonumber(n) }
end

local mode, peers
package.preload.socket = function()
  return { tcp = function()
    local c = {}
    peers[#peers + 1] = c
    function c:settimeout() end
    function c:setoption() end
    function c:connect() return 1 end
    function c:getpeername() return '127.0.0.1', 47613 end
    function c:close() self.closed = true end
    function c:receive()
      local data = mode.incoming or ''
      mode.incoming = nil
      if mode.closeNext then mode.closeNext = nil; return nil, 'closed', data end
      return nil, 'timeout', data
    end
    function c:send(data) return #data end
    return c
  end }
end

local function fresh()
  mode, peers = {}, {}
  package.loaded['beamngEarth/net'] = nil
  local net = require('beamngEarth/net')
  net.update(0)
  net.serverHello(nil, nil)
  return net
end

local seq = 0
local function payload(ty, size)
  seq = seq + 1
  local s = string.format('{"type":"%s","n":%d,"pad":"', ty, seq)
  return s .. string.rep('x', math.max(0, size - #s - 2)) .. '"}'
end
local function wire(list)
  local parts = {}
  for _, p in ipairs(list) do parts[#parts + 1] = framing.encode(p) end
  return table.concat(parts)
end
local function ns(msgs)
  local out = {}
  for _, m in ipairs(msgs) do if m.n then out[#out + 1] = m.n end end
  return out
end

-- small traffic: decoded in the frame it came, as always
do
  local net = fresh()
  local first = seq + 1
  mode.incoming = wire({ payload('far_tile', 300), payload('far_tile', 300), payload('road_tile', 300) })
  local got = ns(net.update(0))
  T.check(#got == 3 and got[1] == first and got[3] == first + 2, 'small messages: all decoded in their frame, in order')
  T.check(net.decodeBacklog() == 0, 'nothing waits')
  net.close()
end

-- a burst over the budget: spread over frames, in order, each frame within the budget (at least one message)
do
  local net = fresh()
  local B = net.DECODE_BYTES_PER_FRAME
  local list = {}
  for _ = 1, 40 do list[#list + 1] = payload('far_tile', 100 * 1024) end   -- 4 MB
  local first = seq - 39
  mode.incoming = wire(list)
  local all, frames, worst = {}, 0, 0
  repeat
    decodedBytes = 0
    for _, n in ipairs(ns(net.update(0))) do all[#all + 1] = n end
    worst = math.max(worst, decodedBytes)
    frames = frames + 1
  until net.decodeBacklog() == 0 or frames > 100
  local inOrder = #all == 40
  for i = 1, #all do if all[i] ~= first + i - 1 then inOrder = false end end
  T.check(inOrder, 'a 4 MB burst: every message, in order (' .. #all .. ')')
  T.check(frames >= 7 and worst <= B, 'spread over ' .. frames .. ' frames, at most ' .. worst .. ' bytes decoded a frame')
  net.close()
end

-- one message bigger than the budget: decoded alone in its frame (at least one a frame)
do
  local net = fresh()
  local big = payload('far_tile', 1024 * 1024)
  local small = payload('far_tile', 200)
  mode.incoming = wire({ big, small })
  local got1 = ns(net.update(0))
  local got2 = ns(net.update(0))
  T.check(#got1 == 1 and #got2 == 1 and got2[1] == got1[1] + 1, 'a 1 MB message alone in its frame, the next one after')
  net.close()
end

-- new messages wait behind those still waiting (order), urgent types are decoded at once
do
  local net = fresh()
  net.setUrgent({ terrain_ready = true })
  local list = {}
  for _ = 1, 12 do list[#list + 1] = payload('far_tile', 100 * 1024) end
  local first = seq - 11
  mode.incoming = wire(list)
  local f1 = ns(net.update(0))
  local urgent = payload('terrain_ready', 200)
  local un = seq
  local later = payload('far_tile', 200)
  mode.incoming = wire({ urgent, later })
  local f2 = net.update(0)
  local hasUrgent, sawLater = false, false
  for _, m in ipairs(f2) do
    if m.n == un then hasUrgent = true end
    if m.n == un + 1 then sawLater = true end
  end
  T.check(#f1 < 12 and hasUrgent and not sawLater, 'terrain_ready decoded at once past the backlog; a tile behind it waits')
  local all = {}
  for _, n in ipairs(f1) do all[#all + 1] = n end
  for _, m in ipairs(f2) do if m.n ~= un then all[#all + 1] = m.n end end
  for _ = 1, 20 do for _, n in ipairs(ns(net.update(0))) do all[#all + 1] = n end end
  local ok = #all == 13
  for i = 1, 12 do if all[i] ~= first + i - 1 then ok = false end end
  T.check(ok and all[13] == un + 1, 'the rest in order, the later tile last')
  net.close()
end

-- unlimited (core: the loading cover): everything in its frame
do
  local net = fresh()
  local list = {}
  for _ = 1, 30 do list[#list + 1] = payload('far_tile', 100 * 1024) end
  mode.incoming = wire(list)
  T.check(#ns(net.update(0, true)) == 30 and net.decodeBacklog() == 0, 'unlimited: all 30 (3 MB) in one frame')
  net.close()
end

-- the switch: DECODE_BYTES_PER_FRAME 0 decodes everything (the old way)
do
  local net = fresh()
  net.DECODE_BYTES_PER_FRAME = 0
  local list = {}
  for _ = 1, 30 do list[#list + 1] = payload('far_tile', 100 * 1024) end
  mode.incoming = wire(list)
  T.check(#ns(net.update(0)) == 30, 'DECODE_BYTES_PER_FRAME 0: all at once (the old way)')
  net.close()
end

-- a disconnect: what was received is decoded before the disconnect event, in order
do
  local net = fresh()
  local list = {}
  for _ = 1, 20 do list[#list + 1] = payload('far_tile', 100 * 1024) end
  local first = seq - 19
  mode.incoming = wire(list)
  local all = ns(net.update(0))
  T.check(#all < 20, 'a backlog waits')
  mode.closeNext = true
  local msgs = net.update(0)
  for _, n in ipairs(ns(msgs)) do all[#all + 1] = n end
  local ok = #all == 20
  for i = 1, #all do if all[i] ~= first + i - 1 then ok = false end end
  T.check(ok and msgs[#msgs].type == '_disconnected', 'closed by the server: every message, in order, then _disconnected')
  T.check(net.decodeBacklog() == 0, 'nothing left')
  -- a close from our side (net.close: a level's end) drops what waits
  local net2 = fresh()
  mode.incoming = wire(list)
  net2.update(0)
  net2.close()
  T.check(net2.decodeBacklog() == 0 and #ns(net2.update(0)) == 0, 'net.close(): what waited is dropped with the transport')
end

-- a payload without the type first: no peek, it waits its turn
do
  local net = fresh()
  net.setUrgent({ terrain_ready = true })
  local list = {}
  for _ = 1, 12 do list[#list + 1] = payload('far_tile', 100 * 1024) end
  mode.incoming = wire(list)
  net.update(0)
  seq = seq + 1
  local odd = string.format('{"n":%d,"type":"terrain_ready"}', seq)
  mode.incoming = wire({ odd })
  local found = false
  for _, m in ipairs(net.update(0)) do if m.n == seq then found = true end end
  local waited = 0
  for _ = 1, 20 do
    for _, m in ipairs(net.update(0)) do if m.n == seq then found = true end end
    waited = waited + 1
  end
  T.check(found, 'a payload not starting with its type still comes, in its turn')
  net.close()
end

T.done('test_net_decode')
