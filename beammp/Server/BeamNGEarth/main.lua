-- BeamNG.Earth's BeamMP server plugin (Resources/Server/BeamNGEarth/main.lua; BeamMP server Lua 5.3).
--
-- BeamMP sends every car's position in its driver's world coordinates, and BeamNG.Earth's world coordinates are
-- metres from an origin (the "anchor") that each player's game picks for itself and moves as they drive (a rebase
-- every 15 km). Each client converts the other players' positions from their owner's anchor into its own
-- (mpanchor.lua, from Thai-v4's PR #88); this plugin tells every client where everyone's anchor is:
--   - bngeAnchors { pid -> { aLat, aLon, lat, lon, name, h } }: every player's anchor and place (from the client's
--     bngePose, every 2 s and at once when its anchor changes), its name and heading (degrees from north; none
--     without a car: the world map's arrows), to everyone each TICK_MS and at once when an anchor changes;
--   - bngeGoTo { lat, lon }: a player who joins is sent to the others (the longest-joined player's place);
--   - bngeConfig { time, weather }: the server's settings (below), to a player at their first pose.
--   - bngeShare: shared world generation v2 (below): this server keeps and serves its players' world building.
-- (v1 shared world building, its bngeConfig sharePort and bngePeers, is commented out below: off since 2026-10-23.)
-- Positions stay latitude / longitude here: no coordinates are converted on the server. Nothing of BeamMP itself is
-- changed: only its events (MP.RegisterEvent, MP.TriggerClientEvent).
local G = {}

-- ===== Settings: edit here, then restart the BeamMP server ========================================================
-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- The TCP port every player's world server listens on to share world building with the players near them (the
-- -- others connect to it: over the internet, each player forwards this port to their computer; on one network nothing
-- -- is needed). Sent to each player when they join. A player whose port is taken uses the next free one (up to 9 more).
-- G.SHARE_PORT = 47621
-- The BeamNG.Earth server list (the "BeamNG.Earth" tab of BeamMP's server browser, seen only by players with
-- BeamNG.Earth). In ServerConfig.toml, Private = false with your AuthKey puts this server on it; this plugin then keeps
-- it off BeamMP's own public list (every BeamMP player sees that one, with or without BeamNG.Earth) by switching
-- BeamMP's Private setting on while the server runs (the file is not changed). Private = true, or no AuthKey: on
-- neither list, players join with Direct Connect. G.LISTED = false keeps a public server off our list too.
G.LISTED = true
-- Time of day for everyone on the server: 'real' (each player's real local time where they are, the default), or a
-- fixed local time 'HH:MM' (e.g. '20:30' for a night server).
G.TIME = 'real'
-- With a fixed time: 0 = it stays there; 1 = it runs at real speed; 60 = an hour of the day in every real minute.
G.TIME_SPEED = 0
-- Weather for everyone: 'real' (the live weather where each player is, the default), or one of 'clear', 'cloudy',
-- 'rain', 'storm', 'snow', 'fog'.
G.WEATHER = 'real'
-- ===================================================================================================================

G.LIST_URL = 'https://earth.tgforever.net/announce'   -- (the list service, native/serverlist)
G.ANNOUNCE_MS = 60000       -- (the list drops a server 3 minutes after its last announce)
G.ANNOUNCE_FILE = 'bnge_announce.json'

G.PROTO = 2                 -- (2: anchors for the clients' conversion; 1 grouped players by anchor)
G.MEET_M = 3000             -- a joining player farther than this from the others is sent to them
G.STALE_S = 15              -- a player's pose this old: left out
G.TICK_MS = 2000

G.players = {}              -- pid -> { name, aLat, aLon, lat, lon, t, joinedAt }
G.WEATHERS = { clear = true, cloudy = true, rain = true, storm = true, snow = true, fog = true }
G.started = os.time()       -- (a running server clock starts here: every player computes the same time from it)
G.clock = 0

-- The notice printed when the server starts (the BeamMP team, 2026-10-27: players kept asking them for help with
-- BeamNG.Earth): where to get help, and why players without BeamNG.Earth never see this server.
G.DISCORD = 'https://discord.gg/tgf'
function G.supportNotice()
  local bar = string.rep('=', 78)
  return {
    bar,
    'Join the TGForever Discord to get help with this server, ask questions,',
    'report bugs, chat with other players or help develop the mod:',
    '    ' .. G.DISCORD,
    '',
    'BeamNG.Earth is not like a normal BeamNG mod: it needs its own app running',
    'next to the game. If something is not working, it is almost certainly a',
    'BeamNG.Earth problem, not a BeamMP one: do NOT contact the BeamMP team or',
    'open BeamMP support tickets about it.',
    '',
    'This server is hidden from players who do not run BeamNG.Earth. BeamMP cannot',
    'install BeamNG.Earth for players who join (it needs its own app: a launcher and',
    'a world server on each player\'s computer), so BeamMP\'s own server list never',
    'shows it. Players with the BeamNG.Earth app find it in the "BeamNG.Earth" tab of',
    'BeamMP\'s server browser, or join by Direct Connect.',
    bar,
  }
end

function G.key(aLat, aLon) return string.format('%.6f,%.6f', aLat, aLon) end

-- Metres between two latitude / longitude points (equirectangular: the distances here are a few km).
function G.dist(lat1, lon1, lat2, lon2)
  local k = math.pi / 180
  local dLon = (lon2 - lon1 + 180) % 360 - 180
  local x = dLon * k * math.cos((lat1 + lat2) / 2 * k)
  local y = (lat2 - lat1) * k
  return 6371000 * math.sqrt(x * x + y * y)
end

-- The players with a pose newer than STALE_S.
function G.live()
  local out = {}
  for pid, p in pairs(G.players) do
    if G.clock - (p.t or -math.huge) <= G.STALE_S then out[pid] = p end
  end
  return out
end

-- Pure: the anchors message { tostring(pid) -> { aLat, aLon, lat, lon, name, h } } of players.
function G.anchors(players)
  local out = {}
  for pid, p in pairs(players) do
    out[tostring(pid)] = { aLat = p.aLat, aLon = p.aLon, lat = p.lat, lon = p.lon, name = p.name, h = p.h }
  end
  return out
end

-- Pure: where a joining player goes: the place of the longest-joined other player (nil: alone). { lat, lon }
function G.meetingPlace(players, newPid)
  local best = nil
  for pid, p in pairs(players) do
    if pid ~= newPid and (not best or (p.joinedAt or 0) < (players[best].joinedAt or 0)
        or ((p.joinedAt or 0) == (players[best].joinedAt or 0) and pid < best)) then
      best = pid
    end
  end
  return best and { lat = players[best].lat, lon = players[best].lon } or nil
end

-- A client's pose { v, aLat, aLon, lat, lon, joined, h }. Returns place (the meeting place for its first pose after
-- joining, or nil) and moved (its anchor is new or changed: the anchors go out at once).
function G.onPose(pid, d, name)
  if type(d) ~= 'table' or tonumber(d.v) ~= G.PROTO then return nil, false end
  local aLat, aLon, lat, lon = tonumber(d.aLat), tonumber(d.aLon), tonumber(d.lat), tonumber(d.lon)
  -- Positive range checks reject NaN as well as infinity and missing values.
  if not (aLat and aLon and lat and lon and math.abs(lat) <= 90 and math.abs(lon) <= 180
      and math.abs(aLat) <= 90 and math.abs(aLon) <= 180) then
    return nil, false
  end
  local p = G.players[pid]
  local first = p == nil
  if first then
    p = { joinedAt = G.clock }
    G.players[pid] = p
  end
  local moved = first or p.aLat ~= aLat or p.aLon ~= aLon
  p.name, p.aLat, p.aLon, p.lat, p.lon, p.t = name, aLat, aLon, lat, lon, G.clock
  local h = tonumber(d.h)
  p.h = (h and h >= 0 and h <= 360) and h or nil
  -- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
  -- local sp = tonumber(d.sp)
  -- p.sp = (sp and sp >= 1 and sp <= 65535 and sp % 1 == 0) and sp or nil
  -- p.sc = type(d.sc) == 'string' and #d.sc <= 64 and d.sc or nil
  -- local sw = tonumber(d.sw)
  -- p.sw = (sw and sw >= 0.1 and sw <= 1000) and sw or 1
  if first and d.joined == true then
    local place = G.meetingPlace(G.live(), pid)
    if place and G.dist(place.lat, place.lon, lat, lon) > G.MEET_M then return place, moved end
  end
  return nil, moved
end

function G.onLeave(pid) G.players[pid] = nil end

-- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
-- -- Pure: each sharing player's group { pid -> { me, members } }: the players with the same anchor that share (a
-- -- port, a code and an address), sorted by id (the same list for every member).
-- function G.peers(players)
--   local byKey = {}
--   for pid, p in pairs(players) do
--     if p.sp and p.sc and p.ip then
--       local k = G.key(p.aLat, p.aLon)
--       byKey[k] = byKey[k] or {}
--       table.insert(byKey[k], { id = tostring(pid), host = p.ip, port = p.sp, code = p.sc, weight = p.sw })
--     end
--   end
--   local out = {}
--   for pid, p in pairs(players) do
--     local list = p.sp and p.sc and p.ip and byKey[G.key(p.aLat, p.aLon)]
--     if list then
--       table.sort(list, function(a, b) return a.id < b.id end)
--       out[pid] = { me = tostring(pid), members = list }
--     end
--   end
--   return out
-- end

-- -- Pure: a group's signature (sent again only when it changes).
-- function G.peersKey(g)
--   local parts = {}
--   for _, m in ipairs(g.members) do
--     parts[#parts + 1] = m.id .. '@' .. m.host .. ':' .. m.port .. '/' .. m.code .. 'x' .. tostring(m.weight)
--   end
--   return table.concat(parts, ',')
-- end

-- G.sentPeers = {}            -- pid -> the signature last sent

-- ===== Shared world generation v2 (.claude/work/mp_share_v2.md) =================================================
-- This server is the cache of its players' world building: what a player builds of the expensive, anchor-free items
-- (a map cell's road levelling plans: bake packs' items) comes here, is kept in SHARE_DIR/store, and goes to every
-- player that asks for it, this session or a later one; .bnge packs put in SHARE_DIR/bake answer too; a helper (our
-- app on this machine, --share-helper) reads SHARE_DIR/spool (the players' places, what was missed) and fills the
-- store. Messages ride BeamMP events: a client's bngeShare "<to>|<json>" (to: S for this server, or a player id: the
-- relay, players near each other only), this server's bngeShare "S|<json>" / "<from>|<json>". Items as base64 chunks
-- of SHARE_CHUNK bytes; what goes out is paced (SHARE_OUT_KBPS for all players together).
G.SHARE_DIR = 'Resources/Server/BeamNGEarth'
G.SHARE_STORE_MB = 2048     -- the store's size at most (the items used longest ago go first)
G.SHARE_OUT_KBPS = 1024     -- KB/s of items this server sends at most, all players together
G.SHARE_NEAR_M = 15000      -- players this near each other with the same code relay to each other
G.SHARE_CHUNK = 24576       -- bytes of an item per message
G.SHARE_ITEM_MAX = 8 * 1024 * 1024
G.SHARE_QUEUE_MAX = 32 * 1024 * 1024   -- bytes queued for one player at most (more: dropped; it asks again)
G.SHARE_DRAIN_MS = 50
G.SHARE_MISS_S = 300        -- a miss stays in the helper's spool this long

G.sh = { up = {}, out = {}, outHead = 1, queued = {}, index = nil, total = 0, packs = nil, seen = {}, misses = {},
         seq = 0, stats = { items_in = 0, items_out = 0, served_store = 0, served_pack = 0, missed = 0, dropped = 0,
         relayed = 0 } }

-- (our base64: "-" for "Z". BeamMP's launcher aborts on a message over 500 bytes from the game with "Zp" in it, its
-- Network/GlobalHandler.cpp ServerSend: the apps' uploads must never hold it; the same alphabet here)
local B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXY-abcdefghijklmnopqrstuvwxyz0123456789+/'
local B64I = {}
for i = 1, 64 do B64I[B64:byte(i)] = i - 1 end

-- Pure: s as base64 (arithmetic only: Lua 5.3 on the server, LuaJIT in the tests).
function G.b64(s)
  local out, n = {}, #s
  for i = 1, n, 3 do
    local a, b, c = s:byte(i, i + 2)
    local v = a * 65536 + (b or 0) * 256 + (c or 0)
    local q1, q2 = math.floor(v / 262144), math.floor(v / 4096) % 64
    local q3, q4 = math.floor(v / 64) % 64, v % 64
    out[#out + 1] = B64:sub(q1 + 1, q1 + 1) .. B64:sub(q2 + 1, q2 + 1)
      .. (b and B64:sub(q3 + 1, q3 + 1) or '=') .. (c and B64:sub(q4 + 1, q4 + 1) or '=')
  end
  return table.concat(out)
end

-- Pure: the decoded length of a base64 string, nil when it is no base64.
function G.b64len(s)
  if type(s) ~= 'string' or #s % 4 ~= 0 or s:find('[^A-Ya-z0-9+/=%-]') then return nil end
  local pad = s:sub(-2) == '==' and 2 or (s:sub(-1) == '=' and 1 or 0)
  return #s / 4 * 3 - pad
end

local function hex(s, n) return type(s) == 'string' and #s == n and not s:find('[^0-9a-f]') end
-- an item's key: a bake pack's ("plans/<sig>/<z>_<x>_<y>", "facts/<kind>/<extra>/<cell>"): its file name is its id,
-- never the key, but a key of other characters is refused anyway
function G.validItemKey(k)
  return type(k) == 'string' and #k <= 200 and (k:sub(1, 6) == 'plans/' or k:sub(1, 6) == 'facts/')
    and not k:find('[^%w_/%[%],%s"%.:{}%-]') and not k:find('..', 1, true)
end

function G.storePath(iid) return G.SHARE_DIR .. '/store/' .. iid:sub(1, 2) .. '/' .. iid end

local function deepEqual(a, b)
  if type(a) ~= type(b) then return false end
  if type(a) ~= 'table' then return a == b end
  for k, v in pairs(a) do if not deepEqual(v, b[k]) then return false end end
  for k in pairs(b) do if a[k] == nil then return false end end
  return true
end

-- Pure: does a pack's meta have every (key, value) of expect?
function G.matches(meta, expect)
  if type(meta) ~= 'table' or type(expect) ~= 'table' then return false end
  for k, v in pairs(expect) do if not deepEqual(meta[k], v) then return false end end
  return true
end

-- The packs in SHARE_DIR/bake: { { path, meta, items, base } }, read once (bakepack.py's format: MAGIC, the index's
-- length as 8 bytes little endian, the JSON index, the items).
function G.packs()
  if G.sh.packs then return G.sh.packs end
  local out = {}
  local dir = G.SHARE_DIR .. '/bake'
  for _, name in ipairs(G.fs.list(dir) or {}) do
    if tostring(name):match('%.bnge$') then
      local path = dir .. '/' .. name
      local head = G.fs.readAt(path, 0, 18)
      if head and #head == 18 and head:sub(1, 10) == 'BNGEBAKE1\n' then
        local n = 0
        for i = 8, 1, -1 do n = n * 256 + head:byte(10 + i) end
        local okJ, index = pcall(G.json.decode, G.fs.readAt(path, 18, n) or '')
        if okJ and type(index) == 'table' and type(index.items) == 'table' then
          out[#out + 1] = { path = path, meta = index.meta or {}, items = index.items, base = 18 + n }
          print(string.format('[BeamNG.Earth] bake pack %s', tostring(name)))
        end
      end
    end
  end
  G.sh.packs = out
  return out
end

-- The store's index { iid -> { size, t } } and its total, from SHARE_DIR/store (once).
function G.storeIndex()
  if G.sh.index then return G.sh.index end
  local index, total = {}, 0
  for _, sub in ipairs(G.fs.dirs(G.SHARE_DIR .. '/store') or {}) do
    if hex(sub, 2) then
      for _, iid in ipairs(G.fs.list(G.SHARE_DIR .. '/store/' .. sub) or {}) do
        if hex(iid, 40) then
          local size = G.fs.size(G.storePath(iid)) or 0
          index[iid] = { size = size, t = 0 }
          total = total + size
        end
      end
    end
  end
  G.sh.index, G.sh.total = index, total
  return index
end

-- Keep the store under SHARE_STORE_MB: the items used longest ago go (to 90 %).
function G.storeTrim()
  local cap = G.SHARE_STORE_MB * 1024 * 1024
  if G.sh.total <= cap then return 0 end
  local list = {}
  for iid, e in pairs(G.storeIndex()) do list[#list + 1] = { iid = iid, t = e.t, size = e.size } end
  table.sort(list, function(a, b) return a.t < b.t or (a.t == b.t and a.iid < b.iid) end)
  local gone = 0
  for _, e in ipairs(list) do
    if G.sh.total <= cap * 0.9 then break end
    G.fs.remove(G.storePath(e.iid))
    G.sh.index[e.iid] = nil
    G.sh.total = G.sh.total - e.size
    gone = gone + 1
  end
  return gone
end

local function queueOut(pid, payload)
  local q = G.sh.queued[pid] or 0
  if q + #payload > G.SHARE_QUEUE_MAX then G.sh.stats.dropped = G.sh.stats.dropped + 1 return false end
  G.sh.queued[pid] = q + #payload
  G.sh.out[#G.sh.out + 1] = { pid, payload }
  return true
end

-- An item's chunks (base64 strings) to player pid.
local function sendChunks(pid, iid, key, code, kind, chunks, z)
  G.sh.seq = G.sh.seq + 1
  local sid = 's' .. G.sh.seq
  for i, d in ipairs(chunks) do
    queueOut(pid, 'S|' .. G.json.encode({ t = 'c', id = sid, i = i - 1, n = #chunks, k = kind, key = key, iid = iid,
      c = code, z = z, d = d }))
  end
  G.sh.stats.items_out = G.sh.stats.items_out + 1
end

-- Answer one wanted item { iid, key, c, k } for pid from the store or a pack; false: neither has it.
function G.serve(pid, it)
  if type(it) ~= 'table' or not hex(it.iid, 40) or not G.validItemKey(it.key) or not hex(it.c, 16) then return false end
  local kind = it.key:sub(1, 5)
  local text = G.fs.read(G.storePath(it.iid))
  if text then
    local chunks = {}
    for line in text:gmatch('[^\n]+') do chunks[#chunks + 1] = line end
    local okH, head = pcall(G.json.decode, table.remove(chunks, 1) or '')     -- (the header line)
    if #chunks > 0 then
      local e = G.storeIndex()[it.iid]
      if e then e.t = G.clock end
      sendChunks(pid, it.iid, it.key, it.c, kind, chunks, okH and type(head) == 'table' and head.z == 1 and 1 or nil)
      G.sh.stats.served_store = G.sh.stats.served_store + 1
      return true
    end
  end
  local seen = G.sh.seen[pid]
  local expect = seen and type(seen.expects) == 'table' and seen.expects[it.c]
  if expect then
    for _, pack in ipairs(G.packs()) do
      local at = pack.items[it.key]
      if type(at) == 'table' and tonumber(at[1]) and tonumber(at[2]) and at[2] <= G.SHARE_ITEM_MAX
          and G.matches(pack.meta, expect) then
        local data = G.fs.readAt(pack.path, pack.base + at[1], at[2])
        if data and #data == at[2] then
          local chunks = {}
          for i = 1, math.max(#data, 1), G.SHARE_CHUNK do chunks[#chunks + 1] = G.b64(data:sub(i, i + G.SHARE_CHUNK - 1)) end
          sendChunks(pid, it.iid, it.key, it.c, kind, chunks)
          G.sh.stats.served_pack = G.sh.stats.served_pack + 1
          return true
        end
      end
    end
  end
  G.sh.misses[it.iid] = { key = it.key, c = it.c, t = G.clock, pid = pid }
  G.sh.stats.missed = G.sh.stats.missed + 1
  return false
end

-- An upload's chunk from pid; the item kept in the store once all its chunks are in.
local function upload(pid, m)
  local i, n = tonumber(m.i), tonumber(m.n)
  if not (i and n and hex(m.iid, 40) and G.validItemKey(m.key) and hex(m.c, 16) and type(m.d) == 'string')
      or i < 0 or i >= n or n * G.SHARE_CHUNK > G.SHARE_ITEM_MAX + G.SHARE_CHUNK or not G.b64len(m.d)
      or G.b64len(m.d) > G.SHARE_CHUNK then
    G.sh.stats.dropped = G.sh.stats.dropped + 1
    return
  end
  local k = tostring(pid) .. ':' .. tostring(m.id)
  local u = G.sh.up[k]
  if not u then
    u = { n = n, iid = m.iid, key = m.key, c = m.c, z = m.z == 1 and 1 or nil, chunks = {}, got = 0, t = G.clock }
    G.sh.up[k] = u
  end
  if u.n ~= n or u.iid ~= m.iid then return end
  if not u.chunks[i + 1] then u.got = u.got + 1 end
  u.chunks[i + 1] = m.d
  if u.got < n then return end
  G.sh.up[k] = nil
  local index = G.storeIndex()
  if index[u.iid] then return end            -- (had it: the first one stays)
  local text = G.json.encode({ iid = u.iid, key = u.key, c = u.c, n = n, z = u.z }) .. '\n' .. table.concat(u.chunks, '\n')
  G.fs.mkdir(G.SHARE_DIR .. '/store/' .. u.iid:sub(1, 2))
  if G.fs.write(G.storePath(u.iid), text) then
    index[u.iid] = { size = #text, t = G.clock }
    G.sh.total = G.sh.total + #text
    G.sh.misses[u.iid] = nil
    G.sh.stats.items_in = G.sh.stats.items_in + 1
    G.storeTrim()
  end
end

-- Do players a and b relay to each other (both share with the same plans code, within SHARE_NEAR_M)?
function G.nearShare(a, b)
  local pa, pb, sa, sb = G.players[a], G.players[b], G.sh.seen[a], G.sh.seen[b]
  if not (pa and pb and sa and sb and sa.codes and sb.codes) or sa.codes.plans ~= sb.codes.plans then return false end
  return G.dist(pa.lat, pa.lon, pb.lat, pb.lon) <= G.SHARE_NEAR_M
end

-- A client's bngeShare "<to>|<json>".
function G.onShare(pid, text)
  if type(text) ~= 'string' or #text > 2 * G.SHARE_CHUNK then G.sh.stats.dropped = G.sh.stats.dropped + 1 return end
  local to, body = text:match('^([^|]*)|(.*)$')
  if not to then return end
  if to ~= 'S' then
    local t = tonumber(to)
    if t and t ~= pid and G.nearShare(pid, t) then
      queueOut(t, tostring(pid) .. '|' .. body)
      G.sh.stats.relayed = G.sh.stats.relayed + 1
    end
    return
  end
  local ok, m = pcall(G.json.decode, body)
  if not ok or type(m) ~= 'table' then return end
  if m.t == 'hello' then
    if type(m.codes) == 'table' and type(m.expects) == 'table' then
      G.sh.seen[pid] = { codes = m.codes, expects = m.expects }
      G.sh.spoolDirty = true
    end
  elseif m.t == 'c' then
    upload(pid, m)
  elseif m.t == 'w' and type(m.items) == 'table' then
    local missed = {}
    for n, it in ipairs(m.items) do
      if n > 64 then break end
      if not G.serve(pid, it) and type(it) == 'table' and hex(it.iid, 40) then missed[#missed + 1] = it.iid end
    end
    if #missed > 0 then queueOut(pid, 'S|' .. G.json.encode({ t = 'm', items = missed })) end
    G.sh.spoolDirty = true
  end
end

function G.shareLeave(pid)
  G.sh.seen[pid], G.sh.queued[pid] = nil, nil
  for k in pairs(G.sh.up) do if k:match('^(%d+):') == tostring(pid) then G.sh.up[k] = nil end end
end

-- What goes out now: { { pid, payload } } within budget bytes (SHARE_OUT_KBPS for the time since the last call).
function G.shareDrain(budget)
  local sends, used = {}, 0
  local out = G.sh.out
  while G.sh.outHead <= #out do
    local e = out[G.sh.outHead]
    if used > 0 and used + #e[2] > budget then break end
    sends[#sends + 1] = e
    used = used + #e[2]
    G.sh.queued[e[1]] = math.max(0, (G.sh.queued[e[1]] or 0) - #e[2])
    G.sh.outHead = G.sh.outHead + 1         -- (no hole made: # of a table with holes is undefined)
  end
  if G.sh.outHead > #out then G.sh.out, G.sh.outHead = {}, 1
  elseif G.sh.outHead > 4096 then           -- (a long queue's front dropped now and then)
    local rest = {}
    for i = G.sh.outHead, #out do rest[#rest + 1] = out[i] end
    G.sh.out, G.sh.outHead = rest, 1
  end
  return sends
end

-- The helper's inbox (SHARE_DIR/spool): players.json (the sharing players' places, headings and expects) and
-- misses.json (what was asked for and not had, the last SHARE_MISS_S). -> the two texts.
function G.spool()
  local players = {}
  for pid, s in pairs(G.sh.seen) do
    local p = G.players[pid]
    if p then
      players[#players + 1] = { pid = pid, lat = p.lat, lon = p.lon, h = p.h, codes = s.codes, expects = s.expects }
    end
  end
  table.sort(players, function(a, b) return a.pid < b.pid end)
  local misses = {}
  for iid, m in pairs(G.sh.misses) do
    if G.clock - m.t > G.SHARE_MISS_S then G.sh.misses[iid] = nil
    else misses[#misses + 1] = { iid = iid, key = m.key, c = m.c, t = m.t } end
  end
  table.sort(misses, function(a, b) return a.t > b.t or (a.t == b.t and a.iid < b.iid) end)
  return G.json.encode({ players = players, clock = G.clock }), G.json.encode({ misses = misses, clock = G.clock })
end

-- Every TICK_MS: the anchors message for the clients.
function G.tick(dt)
  G.clock = G.clock + (dt or 0)
  return G.anchors(G.live())
end

-- Pure: G.TIME / G.TIME_SPEED as the clients' clock { hours, speed, epoch } (nil: the real time; a bad value too).
function G.timeConfig(time, speed, epoch)
  local h, m = tostring(time or ''):match('^%s*(%d%d?):(%d%d)%s*$')
  h, m = tonumber(h), tonumber(m)
  if not h or h > 23 or m > 59 then return nil end
  return { hours = h + m / 60, speed = math.max(0, tonumber(speed) or 0), epoch = epoch }
end

-- The settings each player gets (bngeConfig): the server's time and weather (nil: the real ones). (Its sharePort,
-- shared world building's, is commented out: off since 2026-10-23.)
function G.config()
  local w = tostring(G.WEATHER or 'real'):lower()
  return { time = G.timeConfig(G.TIME, G.TIME_SPEED, G.started), weather = G.WEATHERS[w] and w or nil }
end

-- Pure: the server's name, description, tags, map, port, player cap, resource folder and whether it has a password
-- (pass: never the password itself) from ServerConfig.toml's text.
function G.readConfig(text)
  local cfg = {}
  for line in tostring(text or ''):gmatch('[^\r\n]+') do
    local k, v = line:match('^%s*(%w+)%s*=%s*(.-)%s*$')
    if k then
      local str = v:match('^"(.-)"')        -- (a string first: a # inside it is not a comment)
      v = v:gsub('%s*#.*$', '')
      if k == 'Name' and str then cfg.name = str
      elseif k == 'Description' and str then cfg.desc = str
      elseif k == 'Tags' and str then cfg.tags = str
      elseif k == 'Map' and str then cfg.map = str
      elseif k == 'ResourceFolder' and str then cfg.resources = str
      elseif k == 'Password' then cfg.pass = (str or '') ~= ''
      elseif k == 'Port' then cfg.port = tonumber(v)
      elseif k == 'MaxPlayers' then cfg.maxPlayers = tonumber(v)
      elseif k == 'Private' then cfg.private = v == 'true'
      elseif k == 'AuthKey' and str then cfg.authKey = str end
    end
  end
  return cfg
end

-- Pure: is this a real BeamMP key (keymaster.beammp.com's form, not the placeholder of zeros)?
function G.validKey(k)
  return type(k) == 'string' and k:match('^%x+%-%x+%-%x+%-%x+%-%x+$') ~= nil and (k:gsub('[0%-]', '')) ~= ''
end

-- Pure: does this server go on the BeamNG.Earth list? (ServerConfig public with a real key, and G.LISTED)
function G.listable(cfg)
  return G.LISTED ~= false and cfg.private == false and G.validKey(cfg.authKey)
end

-- Pure: what the list shows of this server: what BeamMP's own heartbeat sends its list (BeamMP-Server
-- THeartbeatThread: name, desc, tags, map, players, maxplayers, port, version, modlist, modstotalsize, modstotal,
-- playerslist, pass; not its key, address or private flag: the list takes the address the announce comes from) and
-- place (where the players are, to a tenth of a degree, from the longest-joined player; nil when no one is on).
-- info (optional): { version = BeamMP server version, names = { connected players' names }, mods = { names = {...},
-- size = bytes, count = n } } (the server's, gathered by serverInfo).
function G.announcement(cfg, players, info)
  info = info or {}
  local first = nil
  for _, p in pairs(players) do
    if not first or (p.joinedAt or 0) < (first.joinedAt or 0) then first = p end
  end
  local n = 0
  for _ in pairs(players) do n = n + 1 end
  local names, mods = info.names, info.mods or {}
  local modlist = {}
  for _, m in ipairs(mods.names or {}) do modlist[#modlist + 1] = '/' .. m .. ';' end
  return { port = cfg.port, name = cfg.name, desc = cfg.desc, tags = cfg.tags, map = cfg.map, pass = cfg.pass == true,
    players = names and #names or n, maxPlayers = cfg.maxPlayers, version = info.version,
    playerslist = names and (table.concat(names, ';') .. (#names > 0 and ';' or '')) or nil,
    modlist = table.concat(modlist), modstotalsize = mods.size or 0, modstotal = mods.count or #(mods.names or {}),
    place = first and string.format('%.1f, %.1f', first.lat, first.lon) or nil }
end

-- Pure: the shell command that posts the announcement file to the list in the background (curl ships with Linux and
-- with Windows 10 and later; BeamMP's server Lua has no HTTP of its own). The body goes in a file: a server name is
-- never put on a command line.
function G.announceCmd(url, file, windows)
  local curl = string.format('curl -s -m 10 -H "Content-Type: application/json" --data-binary @%s "%s"', file, url)
  if windows then return 'start "" /b ' .. curl .. ' >NUL 2>&1' end
  return curl .. ' >/dev/null 2>&1 &'
end

-- Pure: the announcement as JSON text, with encode (BeamMP's Util.JsonEncode). That encoder wraps an integer over
-- 2^31 - 1 to a negative one (30 client mods of 2.44 GB went out as modstotalsize -1853754211), which the list
-- refuses (400 "bad json": its modstotalsize is a u64), and the server drops off the list with nothing in its log
-- (curl's answer goes to /dev/null). So a size over that is encoded capped and the real total put into the text.
-- (Only the "modstotalsize" key itself matches: inside a JSON string its quotes are escaped.)
G.INT32_MAX = 2147483647
function G.encodeAnnouncement(ann, encode)
  local size = math.floor(tonumber(ann.modstotalsize) or 0)
  if size < 0 then size = 0 end
  if size <= G.INT32_MAX then
    ann.modstotalsize = size
    return encode(ann)
  end
  ann.modstotalsize = G.INT32_MAX
  local text = encode(ann)
  ann.modstotalsize = size
  return (text:gsub('("modstotalsize"%s*:%s*)%-?%d+', function(k) return k .. string.format('%d', size) end, 1))
end

-- ---------------------------------------------------------------------------------------------------------------
-- BeamMP wiring (absent in the offline tests)
if MP and MP.RegisterEvent then
  local function encode(t) return Util.JsonEncode(t) end

  function BNGE_onPose(pid, data)
    local ok, d = pcall(Util.JsonDecode, data)
    if not ok then return end
    local known = G.players[pid] ~= nil
    local place, moved = G.onPose(pid, d, MP.GetPlayerName(pid))
    local p = G.players[pid]
    if p and not p.ip and MP.GetPlayerIdentifiers then      -- (the address the others reach its world server at)
      local okI, ids = pcall(MP.GetPlayerIdentifiers, pid)
      if okI and type(ids) == 'table' and type(ids.ip) == 'string' then p.ip = ids.ip end
    end
    if p and not known then
      MP.TriggerClientEvent(pid, 'bngeConfig', encode(G.config()))
      print(string.format('[BeamNG.Earth] %s at %.5f, %.5f (anchor %s)', tostring(p.name), p.lat, p.lon,
        G.key(p.aLat, p.aLon)))
    end
    -- (the anchors first: the joiner teleports onto the others' anchor, mpsession.adoptAnchor)
    if moved then MP.TriggerClientEvent(-1, 'bngeAnchors', encode(G.anchors(G.live()))) end
    if place then
      print(string.format('[BeamNG.Earth] %s sent to the others at %.5f, %.5f', tostring(p and p.name), place.lat,
        place.lon))
      MP.TriggerClientEvent(pid, 'bngeGoTo', encode(place))
    end
  end

  function BNGE_onLeave(pid) G.onLeave(pid); G.shareLeave(pid) end

  -- shared world generation v2: the file layer (BeamMP's FS and Lua's io), the events, the paced sends
  local function base(f) return tostring(f):match('([^/\\]+)$') end
  local function names(fn, dir)
    local okL, list = pcall(fn, dir)
    local out = {}
    if okL and type(list) == 'table' then for _, f in ipairs(list) do out[#out + 1] = base(f) end end
    return out
  end
  G.json = { encode = Util.JsonEncode, decode = Util.JsonDecode }
  G.fs = {
    read = function(path)
      local f = io.open(path, 'rb')
      if not f then return nil end
      local t = f:read('*a')
      f:close()
      return t
    end,
    readAt = function(path, off, len)
      local f = io.open(path, 'rb')
      if not f then return nil end
      f:seek('set', off)
      local t = f:read(len)
      f:close()
      return t
    end,
    write = function(path, text)
      local tmp = path .. '.tmp'
      local f = io.open(tmp, 'wb')
      if not f then return false end
      f:write(text)
      f:close()
      os.remove(path)
      return os.rename(tmp, path) ~= nil
    end,
    size = function(path)
      local f = io.open(path, 'rb')
      if not f then return nil end
      local n = f:seek('end')
      f:close()
      return n
    end,
    remove = function(path) os.remove(path) end,
    list = function(dir) return names(FS.ListFiles, dir) end,
    dirs = function(dir) return names(FS.ListDirectories, dir) end,
    mkdir = function(dir) pcall(FS.CreateDirectory, dir) end,
  }
  for _, d in ipairs({ '/store', '/bake', '/spool' }) do G.fs.mkdir(G.SHARE_DIR .. d) end

  function BNGE_share(pid, data)
    local ok, err = pcall(G.onShare, pid, data)
    if not ok then print('[BeamNG.Earth] share: ' .. tostring(err)) end
  end

  local drainAt = os.clock()
  function BNGE_shareDrain()
    local now = os.clock()
    local budget = G.SHARE_OUT_KBPS * 1024 * math.max(0.001, math.min(1, now - drainAt + G.SHARE_DRAIN_MS / 1000))
    drainAt = now
    for _, e in ipairs(G.shareDrain(budget)) do MP.TriggerClientEvent(e[1], 'bngeShare', e[2]) end
  end

  function BNGE_tick()
    MP.TriggerClientEvent(-1, 'bngeAnchors', encode(G.tick(G.TICK_MS / 1000)))
    if next(G.sh.seen) then          -- (the helper's inbox, while anyone shares)
      local players, misses = G.spool()
      G.fs.write(G.SHARE_DIR .. '/spool/players.json', players)
      G.fs.write(G.SHARE_DIR .. '/spool/misses.json', misses)
    end
    -- (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
    -- for pid, g in pairs(G.peers(G.live())) do
    --   local sig = G.peersKey(g)
    --   if G.sentPeers[pid] ~= sig then
    --     G.sentPeers[pid] = sig
    --     MP.TriggerClientEvent(pid, 'bngePeers', encode(g))
    --     print(string.format('[BeamNG.Earth] %s shares generation with %d player(s)', tostring(G.players[pid].name),
    --       #g.members - 1))
    --   end
    -- end
  end

  local function serverConfig()
    local f = io.open('ServerConfig.toml', 'r')
    local cfg = G.readConfig(f and f:read('*a') or '')
    if f then f:close() end
    return cfg
  end

  for _, line in ipairs(G.supportNotice()) do print('[BeamNG.Earth] ' .. line) end

  -- what BeamMP's heartbeat sends its list besides the config (G.announcement's info): the server version, the
  -- connected players' names and the client mods (<ResourceFolder>/Client/*.zip, as BeamMP's TResourceManager counts)
  function G.serverInfo(cfg)
    local info = {}
    local ok, a, b, c = pcall(MP.GetServerVersion)
    if ok and a then info.version = string.format('%s.%s.%s', tostring(a), tostring(b or 0), tostring(c or 0)) end
    local okP, list = pcall(MP.GetPlayers)
    if okP and type(list) == 'table' then
      local names = {}
      for _, name in pairs(list) do names[#names + 1] = tostring(name) end
      table.sort(names)
      info.names = names
    end
    local dir = (cfg.resources or 'Resources') .. '/Client'
    local okF, files = pcall(FS.ListFiles, dir)
    if okF and type(files) == 'table' then
      local mods = { names = {}, size = 0, count = 0 }
      for _, f in ipairs(files) do
        local base = tostring(f):match('([^/\\]+)%.zip$')
        if base then
          mods.names[#mods.names + 1] = base
          mods.count = mods.count + 1
          local h = io.open(dir .. '/' .. base .. '.zip', 'rb')
          if h then mods.size = mods.size + (h:seek('end') or 0); h:close() end
        end
      end
      table.sort(mods.names)
      info.mods = mods
    end
    return info
  end

  -- a public server with its key goes on our list and off BeamMP's (its Private setting on while it runs: BeamMP's
  -- heartbeat sends it each time); otherwise direct connect only
  local startCfg = serverConfig()
  if G.listable(startCfg) then
    local okSet = pcall(MP.Set, MP.Settings.Private, true)
    print('[BeamNG.Earth] public server: on the BeamNG.Earth server list'
      .. (okSet and ', kept off BeamMP\'s public list' or ' (could not keep it off BeamMP\'s public list)'))
  else
    print('[BeamNG.Earth] not on any server list (Private = true or no AuthKey in ServerConfig.toml): players join with '
      .. 'Direct Connect')
  end

  -- this server on the BeamNG.Earth list, every ANNOUNCE_MS and at start
  function BNGE_announce()
    local cfg = serverConfig()
    if not cfg.port or not G.listable(cfg) then return end
    local out = io.open(G.ANNOUNCE_FILE, 'w')
    if not out then return end
    out:write(G.encodeAnnouncement(G.announcement(cfg, G.live(), G.serverInfo(cfg)), encode))
    out:close()
    os.execute(G.announceCmd(G.LIST_URL, G.ANNOUNCE_FILE, package.config:sub(1, 1) == '\\'))
  end

  MP.RegisterEvent('BNGE_announce', 'BNGE_announce')
  MP.CreateEventTimer('BNGE_announce', G.ANNOUNCE_MS)
  pcall(BNGE_announce)
  MP.RegisterEvent('bngePose', 'BNGE_onPose')
  MP.RegisterEvent('onPlayerDisconnect', 'BNGE_onLeave')
  MP.RegisterEvent('BNGE_tick', 'BNGE_tick')
  MP.CreateEventTimer('BNGE_tick', G.TICK_MS)
  MP.RegisterEvent('bngeShare', 'BNGE_share')
  MP.RegisterEvent('BNGE_shareDrain', 'BNGE_shareDrain')
  MP.CreateEventTimer('BNGE_shareDrain', G.SHARE_DRAIN_MS)
  local cfg = G.config()
  local timeText = 'real'
  if cfg.time then
    timeText = tostring(G.TIME) .. (cfg.time.speed > 0 and (' running at x' .. tostring(cfg.time.speed)) or ', fixed')
  end
  print(string.format('[BeamNG.Earth] plugin loaded (time %s, weather %s)', timeText, cfg.weather or 'real'))
  if tostring(G.TIME or 'real'):lower() ~= 'real' and not cfg.time then
    print('[BeamNG.Earth] G.TIME "' .. tostring(G.TIME) .. '" is not "real" or HH:MM: the real time is used')
  end
  if tostring(G.WEATHER or 'real'):lower() ~= 'real' and not cfg.weather then
    print('[BeamNG.Earth] G.WEATHER "' .. tostring(G.WEATHER) .. '" is not one of real, clear, cloudy, rain, storm, '
      .. 'snow, fog: the real weather is used')
  end
end

return G
