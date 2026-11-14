-- luajit mod/tests/test_framing.lua
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local framing = require('beamngEarth/framing')

-- header encoding: little-endian uint32
local function hdr(n) return framing.encode(string.rep('x', n)):sub(1, 4) end
T.check(hdr(0) == '\0\0\0\0', 'len 0')
T.check(hdr(1) == '\1\0\0\0', 'len 1')
T.check(hdr(255) == '\255\0\0\0', 'len 255')
T.check(hdr(256) == '\0\1\0\0', 'len 256')
T.check(hdr(65536 + 2) == '\2\0\1\0', 'len 65538')
T.check(framing.encode('{}') == '\2\0\0\0{}', 'encode {}')

-- a stream of mixed frames: empty, JSON, binary (NUL/0xFF), multi-byte length, UTF-8
local payloads = {
  '',
  '{"type":"hello","protocol":1,"serverVersion":"0.1.0","sessionId":"abc"}',
  '\0\255\0\1binary\0',
  string.rep('0123456789abcdef', 4096 + 3), -- 65584 bytes: 3 non-zero length bytes
  '{"type":"status","text":"Höhe 4421 m – ok"}',
  '{"type":"far_done","anchorId":7,"count":42}',
}
local stream = {}
for i, p in ipairs(payloads) do stream[i] = framing.encode(p) end
stream = table.concat(stream)

local function feedChunks(chunks)
  local r = framing.newReassembler()
  local out = {}
  for _, c in ipairs(chunks) do
    r:feed(c)
    while true do
      local p = r:pop()
      if not p then break end
      out[#out + 1] = p
    end
  end
  return out, r
end

local function same(out)
  if #out ~= #payloads then return false end
  for i = 1, #out do if out[i] ~= payloads[i] then return false end end
  return true
end

-- whole stream in one chunk
T.check(same(feedChunks({ stream })), 'single chunk')

-- every two-way split point within the first 200 bytes and around each frame boundary
local boundaries = {}
do
  local pos = 0
  for _, p in ipairs(payloads) do pos = pos + 4 + #p; boundaries[#boundaries + 1] = pos end
end
local splits = {}
for i = 1, 200 do splits[#splits + 1] = i end
for _, b in ipairs(boundaries) do for d = -5, 5 do splits[#splits + 1] = b + d end end
local okSplits = true
for _, s in ipairs(splits) do
  if s > 0 and s < #stream then
    if not same(feedChunks({ stream:sub(1, s), stream:sub(s + 1) })) then
      okSplits = false
      print('split at ' .. s .. ' failed')
    end
  end
end
T.check(okSplits, 'all two-way splits')

-- byte by byte (only the small frames; the big one is covered by random chunking)
do
  local small = framing.encode(payloads[2]) .. framing.encode('') .. framing.encode(payloads[3])
  local chunks = {}
  for i = 1, #small do chunks[i] = small:sub(i, i) end
  local out = feedChunks(chunks)
  T.check(#out == 3 and out[1] == payloads[2] and out[2] == '' and out[3] == payloads[3], 'byte-by-byte')
end

-- random chunkings, including empty chunks (deterministic seed)
math.randomseed(12345)
local okRandom = true
for trial = 1, 300 do
  local chunks, pos = {}, 1
  while pos <= #stream do
    local maxLen = (trial % 3 == 0) and 7 or ((trial % 3 == 1) and 300 or 20000)
    local len = math.random(0, maxLen)
    chunks[#chunks + 1] = stream:sub(pos, pos + len - 1)
    pos = pos + len
  end
  if not same(feedChunks(chunks)) then okRandom = false; print('random trial ' .. trial .. ' failed') end
end
T.check(okRandom, 'random chunkings')

-- a frame arrives only once complete; nothing is returned early
do
  local r = framing.newReassembler()
  local f = framing.encode('hello world')
  r:feed(f:sub(1, 3)); T.check(r:pop() == nil, 'partial header -> nil')
  r:feed(f:sub(4, 10)); T.check(r:pop() == nil, 'partial body -> nil')
  r:feed(f:sub(11)); T.check(r:pop() == 'hello world', 'completed frame')
  T.check(r:pop() == nil, 'drained')
end

-- a burst of small frames in 64 KB receives: the reassembler's own copies stay a few times the data, not the 60x of
-- the rest of the buffer copied after every 32 KB read (2026-11-08: much of the game's GE Lua garbage on a drive)
do
  local parts = {}
  for i = 1, 6000 do parts[#parts + 1] = framing.encode(string.rep(string.char(65 + i % 26), 100 + (i * 37) % 800)) end
  local s = table.concat(parts)
  local chunks = {}
  for p = 1, #s, 65536 do chunks[#chunks + 1] = s:sub(p, p + 65535) end
  local r = framing.newReassembler()
  collectgarbage(); collectgarbage('stop')
  local before = collectgarbage('count')
  for _, c in ipairs(chunks) do r:feed(c) end
  local n, ok = 0, true
  while true do
    local m = r:pop()
    if not m then break end
    n = n + 1
    if m ~= parts[n]:sub(5) then ok = false end
  end
  local kb = collectgarbage('count') - before
  collectgarbage('restart')
  T.check(n == 6000 and ok, 'burst: every frame, in order')
  T.check(kb < 4 * #s / 1024, string.format('burst: %.1fx the data allocated (under 4x)', kb * 1024 / #s))
end

-- oversized length header sets err and stops
do
  local r = framing.newReassembler()
  r:feed('\255\255\255\127rest')
  T.check(r:pop() == nil and r.err ~= nil, 'oversized frame -> err')
  r:reset()
  r:feed(framing.encode('ok'))
  T.check(r:pop() == 'ok' and r.err == nil, 'reset recovers')
end

T.done('test_framing')
