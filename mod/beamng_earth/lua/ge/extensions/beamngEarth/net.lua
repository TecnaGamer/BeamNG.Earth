-- Non-blocking TCP client for the companion server (docs/V1_SPEC.md "Transport").
-- luasocket with settimeout(0): nothing here ever blocks the frame. Reconnects every 2 s.
-- update() returns the decoded messages of this frame, plus the pseudo-messages
-- {type='_connected'} / {type='_disconnected'} so the caller can (re)sync its state.
-- Decoding is spread over frames (DECODE_BYTES_PER_FRAME): what does not fit waits as raw payloads, in order, for the
-- next frames; the urgent types (setUrgent) are decoded at once, as the caller handles them at once.
-- attempts() / everConnected(): connection attempts since the last connection (the on-screen
-- banner's "reconnecting (attempt n)") and whether this game ever reached the server.

local framing = require('beamngEarth/framing')

local M = {}

M.HOST, M.PORT = '127.0.0.1', 47613
-- A second game on one PC (tests: two BeamMP players) talks to its own server: BeamNG.drive.exe -bngeport 47614
do
  local ok, args = pcall(function() return Engine.getStartingArgs() end)
  if ok and type(args) == 'table' then
    for i, a in ipairs(args) do
      if a == '-bngeport' and tonumber(args[i + 1]) then M.PORT = tonumber(args[i + 1]) end
    end
  end
end
local RECONNECT_S = 2.0
-- Off our level (M.setIdle): the retries back off, RECONNECT_S doubling up to IDLE_MAX_S, while no world server
-- answers. The game with the mod installed and the app closed tried every 2 s on every map, its console full of
-- connection attempts (GitHub #741); our level loaded (setIdle(false)): every RECONNECT_S again, at once.
M.IDLE_MAX_S = 30.0
local idle = false
local wait = RECONNECT_S
local CONNECT_TIMEOUT_S = 2.0
local RECV_CHUNK = 65536
local MAX_RECV_PER_FRAME = 64 -- chunks; 4 MiB per frame is plenty for JSON control traffic
-- JSON decoded a frame (bytes of payload), at least one message and 1/DECODE_DIV of those waiting (a lasting backlog
-- drains); update(dt, true) (core: the loading cover after a teleport) and 0 / nil: no limit. Every message of a frame
-- was decoded in that frame: up to 4 MiB, inline tile JSON up to ~1 MB a message, the game's decoder ~250 MB/s and the
-- collector stopped while it runs (research 2026-11-11). Rollback: net.DECODE_BYTES_PER_FRAME = 0.
M.DECODE_BYTES_PER_FRAME, M.DECODE_DIV = 512 * 1024, 16
M.MAX_SEND_BYTES, M.MAX_SEND_MESSAGES = 8 * 1024 * 1024, 1024
M.SEND_BYTES_PER_FRAME, M.SEND_CALLS_PER_FRAME = 256 * 1024, 32

local socket = nil
local sock = nil
local state = 'disconnected' -- 'disconnected' | 'connecting' | 'connected'
local timer = RECONNECT_S    -- first attempt happens on the first update
local rx = framing.newReassembler()
local outQueue = {}          -- encoded frames waiting to be sent
local outHead, outTail, outPos, outBytes = 1, 0, 1, 0
local outRetained = 0          -- the partial head frame still retains its already-sent prefix
local sendBytes, sendCalls = 0, 0
local leaseSession, leaseId = nil, nil -- server-issued game ownership; survives transport/level resets
local ready = false          -- hello must complete on this transport before world requests/pose acknowledgements
local generation = 0
local receivedOn = setmetatable({}, { __mode = 'k' }) -- metadata follows queued messages without extending their lifetime
local rawQ, rawH, rawT, rawBytes = {}, 1, 0, 0  -- payloads received, not decoded yet (FIFO)
local urgentTypes = nil                       -- types decoded at once (setUrgent)
local events = {}
local profiling = false
local profile = { receivedBytes = 0, sentBytes = 0, errors = 0, connectionAttempts = 0 }
local attempts = 0           -- connection attempts since the last successful connection
local everConnected = false

M.lastError = nil

-- The message type of a payload as the server writes it ({"type":"name",... first: protocol.py json.dumps of dicts
-- that start with it), or nil (then it simply waits its turn).
local function peekType(payload)
  if payload:sub(1, 9) ~= '{"type":"' then return nil end
  local e = payload:find('"', 10, true)
  if not e or e > 80 then return nil end
  return payload:sub(10, e - 1)
end

local function decodeInto(payload, out)
  local msg = jsonDecode(payload, 'beamngEarth/net')
  if type(msg) == 'table' then
    receivedOn[msg] = generation
    out[#out + 1] = msg
  end
end

-- The waiting payloads into out, in order, while within limit bytes (and at least minN of them).
local function decodeWaiting(out, limit, minN)
  local done, bytes = 0, 0
  while rawH <= rawT do
    local p = rawQ[rawH]
    if done >= minN and bytes + #p > limit then break end
    done, bytes = done + 1, bytes + #p
    rawQ[rawH], rawH, rawBytes = nil, rawH + 1, rawBytes - #p
    decodeInto(p, out)
  end
  if rawH > rawT then rawQ, rawH, rawT, rawBytes = {}, 1, 0, 0 end
  return bytes
end

local function closeSocket(err)
  if profiling and err and err ~= 'closed' then profile.errors = profile.errors + 1 end
  -- what this transport received is decoded before its disconnect, in order (as when every frame decoded it all)
  if rawH <= rawT then decodeWaiting(events, math.huge, 0) end
  if sock then pcall(sock.close, sock) end
  local wasConnected = state == 'connected'
  sock, state, timer = nil, 'disconnected', 0
  ready = false
  rx:reset()
  outQueue, outHead, outTail, outPos, outBytes = {}, 1, 0, 1, 0
  outRetained = 0
  M.lastError = err
  if wasConnected then events[#events + 1] = { type = '_disconnected', error = err } end
end

local function connectedEvent()
  generation, ready = generation + 1, false
  local event = { type = '_connected' }
  receivedOn[event] = generation
  events[#events + 1] = event
end

local function startConnect()
  if profiling then profile.connectionAttempts = profile.connectionAttempts + 1 end
  attempts = attempts + 1
  socket = socket or require('socket')
  sock = socket.tcp()
  sock:settimeout(0)
  -- A non-blocking connect returns nil,'timeout' while in progress. luasocket turns the
  -- master into a tcp{client} even then (no second connect() call is possible), so
  -- completion is detected with getpeername() in pollConnect().
  local ok, err = sock:connect(M.HOST, M.PORT)
  if ok then
    state = 'connected'
  elseif err == 'timeout' or err == 'Operation already in progress' then
    state, timer = 'connecting', 0
  else
    closeSocket(err)
    return
  end
  if state == 'connected' then
    M.lastError = nil
    attempts, everConnected, wait = 0, true, RECONNECT_S
    pcall(sock.setoption, sock, 'tcp-nodelay', true)
    connectedEvent()
  end
end

local function pollConnect(dt)
  if sock:getpeername() then
    state = 'connected'
    M.lastError = nil
    attempts, everConnected, wait = 0, true, RECONNECT_S
    pcall(sock.setoption, sock, 'tcp-nodelay', true)
    connectedEvent()
    return
  end
  timer = timer + dt
  if timer >= CONNECT_TIMEOUT_S then closeSocket('connect timeout / refused') end
end

local function flush()
  while sock and outHead <= outTail and sendBytes > 0 and sendCalls > 0 do
    local data = outQueue[outHead]
    local stop = math.min(#data, outPos + sendBytes - 1)
    local last, err, partialLast = sock:send(data, outPos, stop)
    sendCalls = sendCalls - 1
    local sent = math.max(0, (last or partialLast or (outPos - 1)) - outPos + 1)
    outPos, outBytes, sendBytes = outPos + sent, outBytes - sent, sendBytes - sent
    if profiling then profile.sentBytes = profile.sentBytes + sent end
    if outPos > #data then
      outRetained = outRetained - #data
      outQueue[outHead], outHead, outPos = nil, outHead + 1, 1
      if outHead > outTail then outQueue, outHead, outTail = {}, 1, 0 end
    end
    if not last and err ~= 'timeout' then
      closeSocket('send: ' .. tostring(err))
      return
    end
    if not last then return end -- kernel buffer full; preserve the partial frame and FIFO
  end
end

local function receive(out, limit, spent)
  local closeErr = nil
  for _ = 1, MAX_RECV_PER_FRAME do
    local data, err, partial = sock:receive(RECV_CHUNK)
    if profiling then profile.receivedBytes = profile.receivedBytes + #(data or partial or "") end
    rx:feed(data or partial)
    -- 'timeout' = drained for this frame. Anything else ('closed', 'connection reset by
    -- peer', ...) is fatal; luasocket keeps returning it, so the socket must be closed.
    if err and err ~= 'timeout' then closeErr = err; break end
    if not data then break end
  end
  -- decode before closing: closeSocket() resets the reassembler, and the server's last
  -- frames (e.g. an anchor_error) may have arrived together with the close
  local closing = rx.err ~= nil or closeErr ~= nil
  while true do
    local payload = rx:pop()
    if not payload then break end
    if rawH > rawT and (spent == 0 or spent + #payload <= limit) then
      spent = spent + #payload           -- (nothing waits and it fits: decoded now, as always)
      decodeInto(payload, out)
    else
      local ty = peekType(payload)
      if urgentTypes and ty and urgentTypes[ty] then
        decodeInto(payload, out)         -- (the caller handles it at once, ahead of its own queue: decoded at once too)
      else
        rawT = rawT + 1
        rawQ[rawT], rawBytes = payload, rawBytes + #payload
      end
    end
  end
  if closing and rawH <= rawT then decodeWaiting(out, math.huge, 0) end
  if rx.err then
    closeSocket(rx.err)
  elseif closeErr then
    closeSocket(closeErr == 'closed' and 'closed by server' or ('receive: ' .. tostring(closeErr)))
  end
end

-- Off our level (true): the reconnects back off up to IDLE_MAX_S; false: every RECONNECT_S, the next one at once.
function M.setIdle(on)
  on = on and true or false
  if on == idle then return end
  idle = on
  if not on then
    wait = RECONNECT_S
    if state == 'disconnected' then timer = RECONNECT_S end
  end
end

-- The message types decoded at once even while others wait (core: its urgent types, handled ahead of its queue).
function M.setUrgent(types) urgentTypes = types end

-- Payloads received and not decoded yet: count, bytes.
function M.decodeBacklog() return rawT - rawH + 1, rawBytes end

-- Call once per frame. Returns an array of messages (may be empty). unlimited: decode everything received.
function M.update(dt, unlimited)
  sendBytes, sendCalls = M.SEND_BYTES_PER_FRAME, M.SEND_CALLS_PER_FRAME
  if state == 'disconnected' then
    timer = timer + dt
    if timer >= (idle and wait or RECONNECT_S) then
      if idle then wait = math.min(M.IDLE_MAX_S, wait * 2) end
      startConnect()
    end
  elseif state == 'connecting' then
    pollConnect(dt)
  end
  -- connect/disconnect events come before this frame's messages, a disconnect raised
  -- while receiving comes after them
  local out = events
  events = {}
  local limit = M.DECODE_BYTES_PER_FRAME
  if unlimited or not limit or limit <= 0 then limit = math.huge end
  -- what waits from earlier frames first, in order
  local spent = 0
  if rawH <= rawT then spent = decodeWaiting(out, limit, math.ceil((rawT - rawH + 1) / M.DECODE_DIV)) end
  if state == 'connected' then
    flush()
    if sock then receive(out, limit, spent) end
  end
  for _, e in ipairs(events) do out[#out + 1] = e end
  events = {}
  return out
end

-- Queue a message table; returns false when not connected (the message is dropped).
function M.send(msg)
  if state ~= 'connected' then return false end
  if not ready and msg.type ~= 'hello' then return false end
  local data = framing.encode(jsonEncode(msg))
  if outRetained + #data > M.MAX_SEND_BYTES or outTail - outHead + 1 >= M.MAX_SEND_MESSAGES then
    closeSocket('outgoing queue limit exceeded')
    return false -- reconnect's complete ownership snapshot reconciles any dropped acknowledgements
  end
  outTail = outTail + 1
  outQueue[outTail], outBytes = data, outBytes + #data
  outRetained = outRetained + #data
  flush()
  return state == 'connected'
end

function M.isCurrent(msg) return not receivedOn[msg] or receivedOn[msg] == generation end
function M.serverHello(session, id, msg)
  if msg and not M.isCurrent(msg) then
    -- A hello decoded on the previous transport can still name the lease of its queued objects.
    -- Keep that identity for the next snapshot, but never make a newer transport ready with it.
    if not ready then leaseSession, leaseId = session, id end
    return false
  end
  leaseSession, leaseId, ready = session, id, true
  return true
end
function M.lease() return leaseSession, leaseId end
function M.isReady() return state == 'connected' and ready end

function M.setProfiling(on)
  on = on == true
  if on ~= profiling then profile = { receivedBytes = 0, sentBytes = 0, errors = 0, connectionAttempts = 0 } end
  profiling = on
end

function M.profileState()
  local out = { state = state, endpoint = M.HOST .. ":" .. tostring(M.PORT), enabled = profiling,
    queuedBytes = outBytes, retainedBytes = outRetained, queuedMessages = outTail - outHead + 1 }
  for k, v in pairs(profile) do out[k] = v end
  return out
end

function M.state() return state end
function M.isConnected() return state == 'connected' end
function M.attempts() return attempts end
function M.everConnected() return everConnected end

function M.close()
  closeSocket('closed')
  events = {}
  timer = RECONNECT_S
end

return M
