-- luajit mod/tests/test_beammp_share.lua  (the BeamMP plugin's shared world generation v2: beammp/Server/BeamNGEarth/
-- main.lua's store, packs, relay and pacing, on an in-memory file layer; .claude/work/mp_share_v2.md)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local J = dofile(dir .. '/minijson.lua')
local G = dofile(dir .. '/../../beammp/Server/BeamNGEarth/main.lua')

-- the in-memory file layer
local files, dirs = {}, {}
local function parent(p) return p:match('^(.*)/[^/]+$') end
local function names(dir_, want_dirs)
  local out, seen = {}, {}
  local pool = want_dirs and dirs or files
  for p in pairs(pool) do
    if parent(p) == dir_ then
      local n = p:match('([^/]+)$')
      if not seen[n] then seen[n] = true; out[#out + 1] = n end
    end
  end
  table.sort(out)
  return out
end
G.json = J
G.fs = {
  read = function(p) return files[p] end,
  readAt = function(p, off, len) local f = files[p]; return f and f:sub(off + 1, off + len) or nil end,
  write = function(p, t) files[p] = t; return true end,
  size = function(p) return files[p] and #files[p] or nil end,
  remove = function(p) files[p] = nil end,
  list = function(d) return names(d, false) end,
  dirs = function(d) return names(d, true) end,
  mkdir = function(d) dirs[d] = true end,
}
G.SHARE_DIR = 'srv'

local CODE, OTHER = 'aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb'
local EXPECT = { bake_format = 1, plans_format = 2, frames = 'r10', signature = 'w1s1', versions = { overture = 'r1' } }
local function hello(pid, code, expect)
  G.onShare(pid, 'S|' .. J.encode({ t = 'hello', codes = { plans = code }, expects = { [code] = expect or EXPECT } }))
end
local function sends() return G.shareDrain(1e12) end
local function msgs(list, pid)
  local out = {}
  for _, e in ipairs(list) do
    if not pid or e[1] == pid then
      local frm, body = e[2]:match('^([^|]*)|(.*)$')
      local m = J.decode(body)
      m.from = frm
      out[#out + 1] = m
    end
  end
  return out
end
local function iid(n) return string.rep(string.format('%x', n % 16), 40) end
local function upload(pid, id_, key, data, code)
  local chunks = {}
  for i = 1, #data, G.SHARE_CHUNK do chunks[#chunks + 1] = G.b64(data:sub(i, i + G.SHARE_CHUNK - 1)) end
  for i, d in ipairs(chunks) do
    G.onShare(pid, 'S|' .. J.encode({ t = 'c', id = 'u1', i = i - 1, n = #chunks, iid = id_, key = key, c = code or CODE,
      k = 'plans', d = d }))
  end
end

G.players, G.clock = {}, 0
G.onPose(1, { v = G.PROTO, aLat = 40.75, aLon = -73.98, lat = 40.75, lon = -73.98 }, 'a')
G.onPose(2, { v = G.PROTO, aLat = 40.75, aLon = -73.98, lat = 40.76, lon = -73.97 }, 'b')
G.onPose(3, { v = G.PROTO, aLat = 45.4, aLon = 11.9, lat = 45.4, lon = 11.9 }, 'c')   -- (far away)
hello(1, CODE); hello(2, CODE); hello(3, CODE)
T.check(G.sh.seen[1] and G.sh.seen[1].codes.plans == CODE, 'hello: the codes and expects noted')
-- our base64: "-" for "Z" (BeamMP's launcher aborts on "Zp" in a message over 500 bytes)
local all = {}
for c = 0, 255 do all[#all + 1] = string.char(c) end
local enc = G.b64(string.rep(table.concat(all), 4))
T.check(not enc:find('Z', 1, true) and enc:find('-', 1, true) and G.b64len(enc) == 1024, 'b64: no Z, "-" for it')

-- an upload: 60 KB (3 chunks) kept in the store under its id; the store's index counts it
local data = string.rep('plans-npz-bytes-', 3840)        -- (61440 bytes)
upload(1, iid(1), 'plans/w1s1/14_4824_6159', data)
local stored = files[G.storePath(iid(1))]
T.check(stored ~= nil and G.sh.stats.items_in == 1, 'an upload kept in the store')
T.check(stored and stored:match('^[^\n]+') and J.decode(stored:match('^[^\n]+')).key == 'plans/w1s1/14_4824_6159',
  'its header line: the key')
T.check(G.storeIndex()[iid(1)] ~= nil and G.sh.total == #stored, 'the index counts it')

-- wanted by player 2: from the store, in the chunks it came in
G.onShare(2, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(1), key = 'plans/w1s1/14_4824_6159', c = CODE } } }))
local got = msgs(sends(), 2)
T.check(#got == 3 and got[1].t == 'c' and got[1].n == 3 and got[1].iid == iid(1) and got[1].from == 'S',
  'served from the store: 3 chunks')
local back = {}
for _, m in ipairs(got) do back[m.i + 1] = m.d end
T.check(table.concat(back) == table.concat((function()
  local c = {}
  for i = 1, #data, G.SHARE_CHUNK do c[#c + 1] = G.b64(data:sub(i, i + G.SHARE_CHUNK - 1)) end
  return c
end)()), 'the same base64 chunks')

-- a pack in bake/: answers a want whose expect its meta has, not another one
local items, body, off = {}, {}, 0
for k, v in pairs({ ['plans/w1s1/14_1_2'] = string.rep('x', 30000), ['plans/w1s1/14_1_3'] = 'small' }) do
  items[k] = { off, #v }
  body[#body + 1] = v
  off = off + #v
end
local index = J.encode({ meta = { bake_format = 1, plans_format = 2, frames = 'r10', signature = 'w1s1',
  versions = { overture = 'r1' }, bbox = { 40, -74, 41, -73 } }, items = items })
local n = #index
local len8 = ''
for _ = 1, 8 do len8 = len8 .. string.char(n % 256); n = math.floor(n / 256) end
files['srv/bake/nyc.bnge'] = 'BNGEBAKE1\n' .. len8 .. index .. table.concat(body)
G.sh.packs = nil
T.check(#G.packs() == 1, 'the pack read')
local packed = string.rep('x', 30000)
G.onShare(2, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(2), key = 'plans/w1s1/14_1_2', c = CODE } } }))
got = msgs(sends(), 2)
T.check(#got == 2 and G.sh.stats.served_pack == 1, 'served from the pack: 2 chunks of its 30 KB')
local dec = {}
for _, m in ipairs(got) do dec[m.i + 1] = m.d end
T.check(dec[1] == G.b64(packed:sub(1, G.SHARE_CHUNK)) and dec[2] == G.b64(packed:sub(G.SHARE_CHUNK + 1)),
  'the pack item\'s bytes')
hello(2, OTHER, { bake_format = 1, plans_format = 3 })
G.onShare(2, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(3), key = 'plans/w1s1/14_1_3', c = OTHER } } }))
got = msgs(sends(), 2)
T.check(#got == 1 and got[1].t == 'm' and got[1].items[1] == iid(3), 'another expect: a miss, not the pack')
hello(2, CODE)

-- a miss goes to the helper's spool, with the players' places and expects
G.onShare(1, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(4), key = 'plans/w1s1/14_9_9', c = CODE } } }))
sends()
local pj, mj = G.spool()
local sp, sm = J.decode(pj), J.decode(mj)
T.check(#sp.players == 3 and sp.players[1].pid == 1 and sp.players[1].expects[CODE].signature == 'w1s1',
  'spool: the sharing players')
local found = false
for _, m in ipairs(sm.misses) do if m.iid == iid(4) and m.key == 'plans/w1s1/14_9_9' then found = true end end
T.check(found, 'spool: the miss')
upload(2, iid(4), 'plans/w1s1/14_9_9', 'built by player two')
T.check(G.sh.misses[iid(4)] == nil, 'uploaded: no longer a miss')

-- what is refused: an id or key that could be a path, a chunk too big, another kind of key, an item over the cap
local before = G.sh.stats.items_in
upload(1, '../../etc/passwd/xxxxxxxxxxxxxxxxxxxxxxx', 'plans/w1s1/14_1_1', 'x')
upload(1, iid(5), 'plans/../../x', 'x')
upload(1, iid(6), 'roads/w1s1/14_1_1', 'x')
G.onShare(1, 'S|' .. J.encode({ t = 'c', id = 'big', i = 0, n = 1, iid = iid(7), key = 'plans/w1s1/14_2_2', c = CODE,
  d = G.b64(string.rep('y', G.SHARE_CHUNK + 3)) }))
G.onShare(1, 'S|' .. J.encode({ t = 'c', id = 'many', i = 0, n = 10000, iid = iid(8), key = 'plans/w1s1/14_3_3',
  c = CODE, d = G.b64('z') }))
T.check(G.sh.stats.items_in == before, 'nothing of those kept')
for p in pairs(files) do T.check(p:sub(1, 4) == 'srv/', 'no file outside the share folder: ' .. p) end

-- the relay: players near each other with the same code only
G.onShare(1, '2|' .. J.encode({ t = 'probe' }))
G.onShare(1, '3|' .. J.encode({ t = 'probe' }))
got = sends()
T.check(#got == 1 and got[1][1] == 2 and got[1][2]:sub(1, 2) == '1|', 'relayed to the near player only, from 1')

-- pacing: a budget lets through what fits (at least one message)
upload(1, iid(9), 'plans/w1s1/14_5_5', string.rep('q', 3 * G.SHARE_CHUNK))
G.onShare(2, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(9), key = 'plans/w1s1/14_5_5', c = CODE } } }))
local first = G.shareDrain(G.SHARE_CHUNK)
T.check(#first == 1, 'a chunk\'s budget: one message now')
T.check(#G.shareDrain(1e12) == 2, 'the rest after')

-- the cap: the items used longest ago go first
G.SHARE_STORE_MB = 0.2
G.clock = 100
upload(1, iid(10), 'plans/w1s1/14_6_6', string.rep('r', 100000))
T.check(G.storeIndex()[iid(10)] ~= nil and G.sh.total <= 0.2 * 1024 * 1024, 'trimmed under the cap, the new one kept')
T.check(G.storeIndex()[iid(1)] == nil and files[G.storePath(iid(1))] == nil, 'an old one gone')

-- a compressed upload (z) is served with its z
G.SHARE_STORE_MB = 2048
upload(1, iid(12), 'plans/w1s1/14_8_8', 'zipped')
local hd = J.decode(files[G.storePath(iid(12))]:match('^[^\n]+'))
T.check(hd.z == nil, 'not compressed: no z')
G.onShare(1, 'S|' .. J.encode({ t = 'c', id = 'zz', i = 0, n = 1, iid = iid(13), key = 'plans/w1s1/14_8_9', c = CODE,
  z = 1, d = G.b64('compressed') }))
G.onShare(2, 'S|' .. J.encode({ t = 'w', items = { { iid = iid(13), key = 'plans/w1s1/14_8_9', c = CODE } } }))
got = msgs(sends(), 2)
T.check(#got == 1 and got[1].z == 1, 'served with its z')

-- a player leaving: its uploads in progress and queue go
G.onShare(1, 'S|' .. J.encode({ t = 'c', id = 'half', i = 0, n = 2, iid = iid(11), key = 'plans/w1s1/14_7_7', c = CODE,
  d = G.b64('a') }))
G.shareLeave(1)
T.check(G.sh.up['1:half'] == nil and G.sh.seen[1] == nil, 'left: its uploads in progress dropped')

T.done('test_beammp_share')
