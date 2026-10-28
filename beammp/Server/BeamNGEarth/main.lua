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
-- (Shared world building, its bngeConfig sharePort and bngePeers, is commented out below: off since 2026-10-23.)
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
  local x = (lon2 - lon1) * k * math.cos((lat1 + lat2) / 2 * k)
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
  if not (aLat and aLon and lat and lon) or math.abs(lat) > 90 or math.abs(lon) > 180
      or math.abs(aLat) > 90 or math.abs(aLon) > 180 then
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

-- Pure: the server's name, port and player cap from ServerConfig.toml's text.
function G.readConfig(text)
  local cfg = {}
  for line in tostring(text or ''):gmatch('[^\r\n]+') do
    local k, v = line:match('^%s*(%w+)%s*=%s*(.-)%s*$')
    if k then
      local str = v:match('^"(.-)"')        -- (a string first: a # inside it is not a comment)
      v = v:gsub('%s*#.*$', '')
      if k == 'Name' and str then cfg.name = str
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

-- Pure: what the list shows of this server: { port, name, players, maxPlayers, place } (place: where the players are,
-- to a tenth of a degree, from the longest-joined player; nil when no one is on).
function G.announcement(cfg, players)
  local first = nil
  for _, p in pairs(players) do
    if not first or (p.joinedAt or 0) < (first.joinedAt or 0) then first = p end
  end
  local n = 0
  for _ in pairs(players) do n = n + 1 end
  return { port = cfg.port, name = cfg.name, players = n, maxPlayers = cfg.maxPlayers,
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

  function BNGE_onLeave(pid) G.onLeave(pid) end

  function BNGE_tick()
    MP.TriggerClientEvent(-1, 'bngeAnchors', encode(G.tick(G.TICK_MS / 1000)))
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
    out:write(encode(G.announcement(cfg, G.live())))
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
