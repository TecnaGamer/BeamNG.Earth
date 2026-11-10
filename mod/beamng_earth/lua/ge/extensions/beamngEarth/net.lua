-- Non-blocking TCP client for the companion server (docs/V1_SPEC.md "Transport").
-- luasocket with settimeout(0): nothing here ever blocks the frame. Reconnects every 2 s.
-- update() returns the decoded messages of this frame, plus the pseudo-messages
-- {type='_connected'} / {type='_disconnected'} so the caller can (re)sync its state.
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
local events = {}
local profiling = false
local profile = { receivedBytes = 0, sentBytes = 0, errors = 0, connectionAttempts = 0 }
local attempts = 0           -- connection attempts since the last successful connection
local everConnected = false

M.lastError = nil

local function closeSocket(err)
  if profiling and err and err ~= 'closed' then profile.errors = profile.errors + 1 end
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

local function receive(out)
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
  while true do
    local payload = rx:pop()
    if not payload then break end
    local msg = jsonDecode(payload, 'beamngEarth/net')
    if type(msg) == 'table' then
      receivedOn[msg] = generation
      out[#out + 1] = msg
    end
  end
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

-- Call once per frame. Returns an array of messages (may be empty).
function M.update(dt)
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
  if state == 'connected' then
    flush()
    if sock then receive(out) end
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
