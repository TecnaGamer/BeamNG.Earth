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
local CONNECT_TIMEOUT_S = 2.0
local RECV_CHUNK = 65536
local MAX_RECV_PER_FRAME = 64 -- chunks; 4 MiB per frame is plenty for JSON control traffic
-- A connected companion can stop reading while our nonblocking sends keep timing out. Bound the
-- retained strings and fail the connection explicitly, so ordinary reconnect/resync handles it.
-- Do not drop/coalesce ordered control messages within an otherwise live stream.
M.MAX_OUT_MESSAGES = 256
M.MAX_OUT_BYTES = 4 * 1024 * 1024
M.SEND_STALL_S = 10

local socket = nil
local sock = nil
local state = 'disconnected' -- 'disconnected' | 'connecting' | 'connected'
local timer = RECONNECT_S    -- first attempt happens on the first update
local rx = framing.newReassembler()
local outQueue = {}          -- encoded frames waiting to be sent
local outPos = 1             -- next byte of outQueue[1] to send
local outBytes, outPending = 0, 0 -- retained strings (including sent prefix) / unsent bytes
local sendStalledS = 0       -- update's wall seconds without forward progress while data waits
local events = {}
local attempts = 0           -- connection attempts since the last successful connection
local everConnected = false

M.lastError = nil

local function closeSocket(err)
  if sock then pcall(sock.close, sock) end
  local wasConnected = state == 'connected'
  sock, state, timer = nil, 'disconnected', 0
  rx:reset()
  outQueue, outPos = {}, 1
  outBytes, outPending, sendStalledS = 0, 0, 0
  M.lastError = err
  if wasConnected then events[#events + 1] = { type = '_disconnected', error = err } end
end

local function startConnect()
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
    attempts, everConnected = 0, true
    pcall(sock.setoption, sock, 'tcp-nodelay', true)
    events[#events + 1] = { type = '_connected' }
  end
end

local function pollConnect(dt)
  if sock:getpeername() then
    state = 'connected'
    M.lastError = nil
    attempts, everConnected = 0, true
    pcall(sock.setoption, sock, 'tcp-nodelay', true)
    events[#events + 1] = { type = '_connected' }
    return
  end
  timer = timer + dt
  if timer >= CONNECT_TIMEOUT_S then closeSocket('connect timeout / refused') end
end

local function flush()
  while sock and outQueue[1] do
    local data = outQueue[1]
    local last, err, partialLast = sock:send(data, outPos)
    if err and err ~= 'timeout' then
      closeSocket('send: ' .. tostring(err))
      return
    end
    -- LuaSocket returns an absolute last-byte index, not a count relative to outPos.
    local nextPos = math.min(#data, last or partialLast or (outPos - 1)) + 1
    local sent = math.max(0, nextPos - outPos)
    outPending = outPending - sent
    if sent > 0 then sendStalledS = 0 end
    outPos = outPos + sent
    if outPos > #data then
      table.remove(outQueue, 1)
      outBytes = outBytes - #data -- the whole string was retained until its last byte was sent
      outPos = 1
    end
    if err == 'timeout' or sent == 0 then return end -- kernel full; continue next frame
  end
end

local function receive(out)
  local closeErr = nil
  for _ = 1, MAX_RECV_PER_FRAME do
    local data, err, partial = sock:receive(RECV_CHUNK)
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
    if type(msg) == 'table' then out[#out + 1] = msg end
  end
  if rx.err then
    closeSocket(rx.err)
  elseif closeErr then
    closeSocket(closeErr == 'closed' and 'closed by server' or ('receive: ' .. tostring(closeErr)))
  end
end

-- Call once per frame. Returns an array of messages (may be empty).
function M.update(dt)
  if state == 'disconnected' then
    timer = timer + dt
    if timer >= RECONNECT_S then startConnect() end
  elseif state == 'connecting' then
    pollConnect(dt)
  end
  -- connect/disconnect events come before this frame's messages, a disconnect raised
  -- while receiving comes after them
  local out = events
  events = {}
  if state == 'connected' then
    if outPending > 0 then sendStalledS = sendStalledS + math.max(0, dt or 0) end
    flush()
    if sock and outPending > 0 and sendStalledS >= M.SEND_STALL_S then
      closeSocket('send stalled: companion not reading')
    end
    if sock then receive(out) end
  end
  for _, e in ipairs(events) do out[#out + 1] = e end
  events = {}
  return out
end

-- Queue a message table. False means disconnected or rejected by an explicit connection failure;
-- the caller receives _disconnected through update, as with an ordinary broken socket.
function M.send(msg)
  if state ~= 'connected' then return false end
  flush() -- allow a recovered reader to drain the queue before deciding whether it is full
  if state ~= 'connected' then return false end
  local payload = jsonEncode(msg)
  local size = #payload + 4
  if size > M.MAX_OUT_BYTES then
    closeSocket('outgoing frame exceeds byte limit')
    return false
  end
  if #outQueue >= M.MAX_OUT_MESSAGES or outBytes + size > M.MAX_OUT_BYTES then
    closeSocket('outgoing queue limit exceeded: companion not reading')
    return false
  end
  outQueue[#outQueue + 1] = framing.encode(payload)
  outBytes, outPending = outBytes + size, outPending + size
  flush()
  return state == 'connected'
end

-- Read-only diagnostics; retained bytes, rather than just the unsent suffix, enforce the memory cap.
function M.sendQueueState()
  return { messages = #outQueue, bytes = outBytes, pendingBytes = outPending, stalledS = sendStalledS }
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
