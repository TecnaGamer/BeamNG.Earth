-- BeamNG.Earth's BeamMP server plugin (Resources/Server/BeamNGEarth/main.lua; BeamMP server Lua 5.3).
--
-- BeamMP sends every car's position in its driver's world coordinates, and BeamNG.Earth's world coordinates are
-- metres from an origin (the "anchor") that each player's game picks for itself and moves as they drive (a rebase
-- every 15 km). Each client converts the other players' positions from their owner's anchor into its own
-- (mpanchor.lua, from Thai-v4's PR #88); this plugin tells every client where everyone's anchor is:
--   - bngeAnchors { pid -> { aLat, aLon, lat, lon, name, h } }: every player's anchor and place (from the client's
--     bngePose, every 2 s and at once when its anchor changes), its name and heading (degrees from north; none
--     without a car: the world map's arrows), to everyone each TICK_MS and at once when an anchor changes;
--   - bngeGoTo { lat, lon, resume?, height?, heading?, token? }: a returning player's checkpoint. Optional legacy
--     join-near-players is off by default. Clients acknowledge completed restores in their next stable driving pose;
--   - bngeConfig { sharePort }: the server's settings (above), to a player at their first pose;
--   - bngePeers { me, members = [{ id, host, port, code, weight }] }: the players on the same anchor whose world server
--     shares generation (their pose's sp / sc / sw; host: the address BeamMP sees; weight: its machine's capacity,
--     its share of the tiles), each tick when its group changed. Players
--     near each other share an anchor (the clients converge, mpsession.lua), so they split the tiles to build.
-- Positions stay latitude / longitude here: no coordinates are converted on the server. Nothing of BeamMP itself is
-- changed: only its events (MP.RegisterEvent, MP.TriggerClientEvent).
local G = {}

-- ===== Settings: edit here, then restart the BeamMP server ========================================================
-- The TCP port every player's world server listens on to share world building with the players near them (the
-- others connect to it: over the internet, each player forwards this port to their computer; on one network nothing
-- is needed). Sent to each player when they join. A player whose port is taken uses the next free one (up to 9 more).
G.SHARE_PORT = 47621
-- Per-account positions, owned by this server (independent of singleplayer Driving Progress).
G.SAVE_POSITIONS = true
G.SAVE_SECONDS = 10
G.SAVE_GUESTS = true       -- random per-server token retained by the guest's game profile
G.JOIN_NEAR_PLAYERS = false -- opt in to the old first-join teleport; saves always take priority
-- Relative to the server root, outside the plugin directory (no Lua reloads).
G.SAVE_DIR = 'data'
G.SAVE_FILES = { G.SAVE_DIR .. '/bnge_player_positions1.json', G.SAVE_DIR .. '/bnge_player_positions2.json' }
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
G.joining = {}              -- actual connections; a plugin hot reload must not teleport current players
G.clock = 0
G.checkpoints, G.progressDirty = {}, false
G.saveSequence, G.saveSlot, G.saveAt, G.nextRestore = 0, nil, 0, 0

local function finite(n)
  return type(n) == 'number' and n == n and math.abs(n) < math.huge
end

-- Identity is supplied by BeamMP, never by the pose packet or a mutable display name.
function G.accountId(ids, guest)
  local id = type(ids) == 'table' and ids.beammp or nil
  if type(id) ~= 'number' and type(id) ~= 'string' then return nil end
  id = tostring(id)
  return guest ~= true and #id <= 32 and id:match('^%d+$') and id:find('[1-9]') and id or nil
end

function G.validGuestToken(token)
  return type(token) == 'string' and #token == 32 and token:match('^[0-9a-f]+$') and token:find('[1-9a-f]') ~= nil
end

local function progressId(id)
  return G.accountId({beammp=id}) or (type(id)=='string' and id:sub(1,6)=='guest:' and G.validGuestToken(id:sub(7)))
end

local function newGuestToken()
  if not Util or not Util.RandomIntRange then return nil end
  for _=1,8 do
    local ok, token = pcall(function()
      local parts = {}
      for i=1,8 do parts[i] = string.format('%04x', Util.RandomIntRange(0,65535)) end
      return table.concat(parts)
    end)
    if ok and G.validGuestToken(token) and not G.checkpoints['guest:'..token] then
      local used = false
      for _,p in pairs(G.players) do if p.guestToken == token then used = true end end
      if not used then return token end
    end
  end
end

function G.validProgress(p)
  return type(p) == 'table' and finite(p.lat) and math.abs(p.lat) <= 90
    and finite(p.lon) and math.abs(p.lon) <= 180 and finite(p.height)
    and finite(p.heading) and p.heading >= 0 and p.heading < 360
end

-- Retain a portable .pc configuration, never a local filename, vehicle collection,
-- serialized Lua, damage snapshot or another player's vehicle. Bounded plain data.
function G.vehicleConfig(model, config)
  if type(model) ~= 'string' or #model > 80 or not model:match('^[%w_-]+$') or model == 'unicycle'
    or type(config) ~= 'table' or type(config.parts) ~= 'table' then return nil end
  local out = {format=2, parts={}, vars={}}
  local count = 0
  for key, value in pairs(config.parts) do
    count = count + 1
    if count > 2048 or type(key) ~= 'string' or #key > 512 or type(value) ~= 'string' or #value > 256 then return nil end
    out.parts[key] = value
  end
  count = 0
  if config.vars ~= nil and type(config.vars) ~= 'table' then return nil end
  for key, value in pairs(config.vars or {}) do
    count = count + 1
    if count > 512 or type(key) ~= 'string' or #key > 128 or not finite(value) then return nil end
    out.vars[key] = value
  end
  if type(config.licenseName) == 'string' then out.licenseName = config.licenseName:sub(1,64) end
  if type(config.mainPartName) == 'string' then out.mainPartName = config.mainPartName:sub(1,128) end
  if type(config.paints) == 'table' then
    out.paints = {}
    for i=1,3 do
      local paint = config.paints[i]
      if paint then
        if type(paint) ~= 'table' or type(paint.baseColor) ~= 'table' then return nil end
        local p = {baseColor={}}
        for j=1,4 do
          if not finite(paint.baseColor[j]) then return nil end
          p.baseColor[j] = paint.baseColor[j]
        end
        for _,key in ipairs({'metallic','roughness','clearcoat','clearcoatRoughness'}) do
          if paint[key] ~= nil and not finite(paint[key]) then return nil end
          p[key] = paint[key]
        end
        out.paints[i] = p
      end
    end
  end
  return {model=model, config=out}
end

function G.playerVehicle(pid, serverId)
  if type(serverId) ~= 'string' or #serverId > 40 or not MP or not MP.GetPlayerVehicles then return nil end
  local owner, id = serverId:match('^(%d+)%-(%d+)$')
  if not owner or tonumber(owner) ~= pid then return nil end
  local ok, vehicles = pcall(MP.GetPlayerVehicles, pid)
  if not ok or type(vehicles) ~= 'table' then return nil end
  local raw = vehicles[tonumber(id)] or vehicles[id]
  if type(raw) ~= 'string' or #raw > 1024*1024 then return nil end
  local start = raw:find('{', 1, true)
  if not start then return nil end
  local parsed, data = pcall(Util.JsonDecode, raw:sub(start))
  if not parsed or type(data) ~= 'table' then return nil end
  return G.vehicleConfig(data.jbm, data.vcf)
end

local function progressCopy(p)
  return {lat=p.lat, lon=p.lon, height=p.height, heading=p.heading, savedAt=p.savedAt, vehicle=p.vehicle}
end

local function readProgress(path)
  local ok, data = pcall(function()
    local f = io.open(path, 'r')
    if not f then return nil end
    local text = f:read('*a')
    f:close()
    return Util.JsonDecode(text)
  end)
  if not ok or type(data) ~= 'table' or data.version ~= 1 or not finite(data.sequence)
    or data.sequence < 1 or data.sequence % 1 ~= 0 or type(data.players) ~= 'table' then return nil end
  for id, p in pairs(data.players) do
    if not progressId(id) or not G.validProgress(p) then return nil end
    p.vehicle = type(p.vehicle) == 'table' and G.vehicleConfig(p.vehicle.model, p.vehicle.config) or nil
  end
  return data
end

function G.loadProgress()
  G.checkpoints, G.saveSequence, G.saveSlot, G.progressDirty, G.saveAt = {}, 0, nil, false, G.clock
  for slot, path in ipairs(G.SAVE_FILES) do
    local data = readProgress(path)
    if data and data.sequence > G.saveSequence then
      G.checkpoints, G.saveSequence, G.saveSlot = data.players, data.sequence, slot
    end
  end
  if G.saveSlot == nil then
    -- One-time import. Once data/ has a valid save, legacy root files are ignored.
    for _,path in ipairs({'bnge_player_positions1.json', 'bnge_player_positions2.json'}) do
      local data = readProgress(path)
      if data and data.sequence > G.saveSequence then
        G.checkpoints, G.saveSequence, G.progressDirty = data.players, data.sequence, true
      end
    end
    if G.progressDirty then G.saveProgress() end
  end
end

function G.saveProgress()
  if not G.SAVE_POSITIONS or not G.progressDirty then return false end
  local slot = G.saveSlot == 1 and 2 or 1
  local sequence = G.saveSequence + 1
  local ok = pcall(function()
    if FS and FS.CreateDirectory then
      local made, why = FS.CreateDirectory(G.SAVE_DIR)
      assert(made, why or 'cannot create save directory')
    end
    local text = Util.JsonEncode({version=1, sequence=sequence, players=G.checkpoints})
    assert(type(text) == 'string', 'JSON encoding failed')
    local f = assert(io.open(G.SAVE_FILES[slot], 'w'))
    local wrote, why = f:write(text)
    local closed, closeWhy = f:close()
    assert(wrote and closed, why or closeWhy or 'write failed')
  end)
  local check = ok and readProgress(G.SAVE_FILES[slot]) or nil
  if not check or check.sequence ~= sequence then
    print('[BeamNG.Earth] Player position save failed; previous checkpoint retained')
    return false
  end
  G.saveSlot, G.saveSequence, G.progressDirty = slot, sequence, false
  return true
end

function G.remember(p, d, pid)
  if not G.SAVE_POSITIONS then return 'disabled' end
  if not p.account then return 'identity' end
  if not G.validProgress(d.progress) then return 'vehicle' end
  -- Do not create an unreachable guest save before the client has stored its token.
  if p.guestToken and (not G.SAVE_GUESTS or d.guestToken ~= p.guestToken) then return 'guest-token' end
  local vehicle = G.playerVehicle(pid, d.progress.vehicleId)
  -- A modern client can have a car while its spawn is awaiting server acceptance.
  -- Do not replace a good save with an unconfirmed car or a walking-mode avatar.
  if tonumber(d.vehicleResume) == 1 and not vehicle then return 'vehicle-config' end
  if p.restorePlace then
    if d.resumeAck ~= p.restorePlace.token then return 'restoring' end
    p.restorePlace = nil
  end
  local saved = progressCopy(d.progress)
  saved.vehicle = vehicle -- never trust a configuration embedded in the pose packet
  saved.savedAt = os.time()
  G.checkpoints[p.account], G.progressDirty = saved, true
  if not p.progressLogged then
    p.progressLogged = true
    print(string.format('[BeamNG.Earth] %s checkpoint active (%s, %s) at %.5f, %.5f', tostring(p.name),
      p.guestToken and 'guest' or 'account', vehicle and vehicle.model or 'position only', saved.lat, saved.lon))
  end
  return 'active'
end

-- Explain blocked guest saves once per state change; never log their private token.
function G.progressNotice(p, d, guest)
  if not G.SAVE_POSITIONS or guest ~= true then return nil end
  if not G.SAVE_GUESTS then return 'Guest checkpoints are disabled on this server' end
  if tonumber(d.guestResume) ~= 1 then
    if (tonumber(d.clientRuntime) or 0) < 3 then
      return 'Guest checkpoint blocked: outdated client runtime; fully restart BeamNG and reconnect with the updated server mod'
    end
    return 'Guest checkpoint blocked: client cannot access its server address or local identity files'
  end
  if not p.guestToken then return 'Guest checkpoint blocked: server could not issue a guest identity' end
  if d.guestToken ~= p.guestToken then return 'Guest checkpoint waiting for client to store its identity' end
  if p.saveState == 'vehicle' then return 'Guest checkpoint waiting for a stable owned vehicle after loading' end
  if p.saveState == 'vehicle-config' then return 'Guest checkpoint waiting for BeamMP to accept the active vehicle configuration' end
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

-- A client's pose { v, aLat, aLon, lat, lon, joined, h, sp, sc, sw } (its world server's sharing port, code, weight). Returns place (the meeting place for its first pose after
-- joining, or nil) and moved (its anchor is new or changed: the anchors go out at once).
function G.onPose(pid, d, name, account, guest)
  if type(d) ~= 'table' or tonumber(d.v) ~= G.PROTO then return nil, false end
  local aLat, aLon, lat, lon = tonumber(d.aLat), tonumber(d.aLon), tonumber(d.lat), tonumber(d.lon)
  if not (finite(aLat) and finite(aLon) and finite(lat) and finite(lon)) or math.abs(lat) > 90 or math.abs(lon) > 180
      or math.abs(aLat) > 90 or math.abs(aLon) > 180 then
    return nil, false
  end
  local p = G.players[pid]
  local first = p == nil
  if first then
    p = { joinedAt = G.clock, account = G.accountId({beammp=account}) }
    G.players[pid] = p
  end
  -- Identity APIs or the client's per-server token can become ready after the first pose.
  -- Bind once per connection; neither names nor IP addresses select a checkpoint.
  if not p.account then
    p.account = G.accountId({beammp=account})
    if not p.account and guest == true and G.SAVE_POSITIONS and G.SAVE_GUESTS and tonumber(d.guestResume) == 1 then
      p.guestToken = G.validGuestToken(d.guestToken) and d.guestToken or newGuestToken()
      if p.guestToken then p.account = 'guest:'..p.guestToken end
    end
  end
  local moved = first or p.aLat ~= aLat or p.aLon ~= aLon
  local joining = (first and G.joining[pid]) or (d.joined == true and not p.receivedJoin)
  if joining then p.receivedJoin, p.joinPending, G.joining[pid] = true, true, nil end
  p.name, p.aLat, p.aLon, p.lat, p.lon, p.t = name, aLat, aLon, lat, lon, G.clock
  local h = tonumber(d.h)
  p.h = (h and h >= 0 and h <= 360) and h or nil
  local sp = tonumber(d.sp)
  p.sp = (sp and sp >= 1 and sp <= 65535 and sp % 1 == 0) and sp or nil
  p.sc = type(d.sc) == 'string' and #d.sc <= 64 and d.sc or nil
  local sw = tonumber(d.sw)
  p.sw = (sw and sw >= 0.1 and sw <= 1000) and sw or 1
  local checkSave = p.joinPending and p.account and tonumber(d.resume) == 1
  local saved = checkSave and G.SAVE_POSITIONS and G.checkpoints[p.account]
  if checkSave then p.joinPending = nil end
  if saved then
    G.nextRestore = G.nextRestore + 1
    local place = progressCopy(saved)
    place.resume, place.token = true, tostring(os.time()) .. ':' .. G.nextRestore
    p.restorePlace, p.restoreSent = place, G.clock
    p.saveState = 'restoring'
    return place, moved -- never save the temporary join pose over this checkpoint
  end
  p.saveState = G.remember(p, d, pid)
  if joining and G.JOIN_NEAR_PLAYERS then
    local place = G.meetingPlace(G.live(), pid)
    if place and G.dist(place.lat, place.lon, lat, lon) > G.MEET_M then return place, moved end
  end
  return nil, moved
end

function G.onLeave(pid)
  G.saveProgress() -- flush the latest stable pose before the runtime player ID can be reused
  G.players[pid] = nil
  G.joining[pid] = nil
  G.sentPeers[pid] = nil
end

-- Pure: each sharing player's group { pid -> { me, members } }: the players with the same anchor that share (a
-- port, a code and an address), sorted by id (the same list for every member).
function G.peers(players)
  local byKey = {}
  for pid, p in pairs(players) do
    if p.sp and p.sc and p.ip then
      local k = G.key(p.aLat, p.aLon)
      byKey[k] = byKey[k] or {}
      table.insert(byKey[k], { id = tostring(pid), host = p.ip, port = p.sp, code = p.sc, weight = p.sw })
    end
  end
  local out = {}
  for pid, p in pairs(players) do
    local list = p.sp and p.sc and p.ip and byKey[G.key(p.aLat, p.aLon)]
    if list then
      table.sort(list, function(a, b) return a.id < b.id end)
      out[pid] = { me = tostring(pid), members = list }
    end
  end
  return out
end

-- Pure: a group's signature (sent again only when it changes).
function G.peersKey(g)
  local parts = {}
  for _, m in ipairs(g.members) do
    parts[#parts + 1] = m.id .. '@' .. m.host .. ':' .. m.port .. '/' .. m.code .. 'x' .. tostring(m.weight)
  end
  return table.concat(parts, ',')
end

G.sentPeers = {}            -- pid -> the signature last sent

-- Every TICK_MS: the anchors message for the clients.
function G.tick(dt)
  G.clock = G.clock + (dt or 0)
  local interval = tonumber(G.SAVE_SECONDS) or 10
  if not finite(interval) then interval = 10 end
  if G.clock - G.saveAt >= math.max(1, interval) then
    G.saveAt = G.clock
    G.saveProgress()
  end
  return G.anchors(G.live())
end

-- Pure: G.TIME / G.TIME_SPEED as the clients' clock { hours, speed, epoch } (nil: the real time; a bad value too).
function G.timeConfig(time, speed, epoch)
  local h, m = tostring(time or ''):match('^%s*(%d%d?):(%d%d)%s*$')
  h, m = tonumber(h), tonumber(m)
  if not h or h > 23 or m > 59 then return nil end
  return { hours = h + m / 60, speed = math.max(0, tonumber(speed) or 0), epoch = epoch }
end

-- The settings each player gets (bngeConfig): the sharing port, when it is a usable one; the server's time and weather
-- (nil: the real ones).
function G.config()
  local port = tonumber(G.SHARE_PORT)
  port = port and math.floor(port)
  local w = tostring(G.WEATHER or 'real'):lower()
  return { sharePort = (port and port >= 1024 and port <= 65535) and port or nil,
    joinNearPlayers = G.JOIN_NEAR_PLAYERS == true,
    time = G.timeConfig(G.TIME, G.TIME_SPEED, G.started), weather = G.WEATHERS[w] and w or nil }
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
  G.loadProgress()

  function BNGE_onPose(pid, data)
    local ok, d = pcall(Util.JsonDecode, data)
    if not ok or type(d) ~= 'table' then return end
    local known = G.players[pid] ~= nil
    local ids, guest
    if MP.GetPlayerIdentifiers then
      local okI, value = pcall(MP.GetPlayerIdentifiers, pid)
      if okI then ids = value end
    end
    if MP.IsPlayerGuest then
      local okG, value = pcall(MP.IsPlayerGuest, pid)
      if okG then guest = value end
    end
    local account = guest ~= nil and G.accountId(ids, guest) or nil
    local place, moved = G.onPose(pid, d, MP.GetPlayerName(pid), account, guest)
    local p = G.players[pid]
    if p and not p.ip and type(ids) == 'table' and type(ids.ip) == 'string' then
      p.ip = ids.ip -- the address the others reach its world server at
    end
    if p and not known then
      MP.TriggerClientEvent(pid, 'bngeConfig', encode(G.config()))
      print(string.format('[BeamNG.Earth] %s at %.5f, %.5f (anchor %s)', tostring(p.name), p.lat, p.lon,
        G.key(p.aLat, p.aLon)))
    end
    if p then
      local revision = tonumber(d.clientRuntime) or 0
      if p.reportedRuntime ~= revision then
        p.reportedRuntime = revision
        print(string.format('[BeamNG.Earth] %s client runtime %s; guest-save support %s', tostring(p.name),
          revision > 0 and tostring(revision) or 'legacy', tonumber(d.guestResume)==1 and 'ready' or 'missing'))
      end
      local notice = G.progressNotice(p, d, guest)
      if notice and notice ~= p.progressNotice then print('[BeamNG.Earth] '..tostring(p.name)..': '..notice) end
      p.progressNotice = notice
    end
    if p and p.guestToken and d.guestToken ~= p.guestToken then
      -- Private to this connection; never put guest tokens in the anchors broadcast or logs.
      MP.TriggerClientEvent(pid, 'bngeGuestIdentity', encode({token=p.guestToken}))
    end
    -- (the anchors first: the joiner teleports onto the others' anchor, mpsession.adoptAnchor)
    if moved then MP.TriggerClientEvent(-1, 'bngeAnchors', encode(G.anchors(G.live()))) end
    if place then
      print(string.format('[BeamNG.Earth] %s %s at %.5f, %.5f', tostring(p and p.name),
        place.resume and 'resuming' or 'sent to the others', place.lat, place.lon))
      MP.TriggerClientEvent(pid, 'bngeGoTo', encode(place))
    end
  end

  function BNGE_onLeave(pid) G.onLeave(pid) end
  function BNGE_onJoining(pid)
    G.players[pid], G.sentPeers[pid], G.joining[pid] = nil, nil, true
  end

  function BNGE_tick()
    MP.TriggerClientEvent(-1, 'bngeAnchors', encode(G.tick(G.TICK_MS / 1000)))
    for pid, p in pairs(G.live()) do
      if p.restorePlace and G.clock - (p.restoreSent or 0) >= 10 then
        p.restoreSent = G.clock
        MP.TriggerClientEvent(pid, 'bngeGoTo', encode(p.restorePlace))
      end
    end
    for pid, g in pairs(G.peers(G.live())) do
      local sig = G.peersKey(g)
      if G.sentPeers[pid] ~= sig then
        G.sentPeers[pid] = sig
        MP.TriggerClientEvent(pid, 'bngePeers', encode(g))
        print(string.format('[BeamNG.Earth] %s shares generation with %d player(s)', tostring(G.players[pid].name),
          #g.members - 1))
      end
    end
  end

  local function serverConfig()
    local f = io.open('ServerConfig.toml', 'r')
    local cfg = G.readConfig(f and f:read('*a') or '')
    if f then f:close() end
    return cfg
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
    out:write(encode(G.announcement(cfg, G.live())))
    out:close()
    os.execute(G.announceCmd(G.LIST_URL, G.ANNOUNCE_FILE, package.config:sub(1, 1) == '\\'))
  end

  MP.RegisterEvent('BNGE_announce', 'BNGE_announce')
  MP.CreateEventTimer('BNGE_announce', G.ANNOUNCE_MS)
  pcall(BNGE_announce)
  MP.RegisterEvent('bngePose', 'BNGE_onPose')
  MP.RegisterEvent('onPlayerDisconnect', 'BNGE_onLeave')
  MP.RegisterEvent('onPlayerJoining', 'BNGE_onJoining')
  function BNGE_saveProgress() G.saveProgress() end
  MP.RegisterEvent('onShutdown', 'BNGE_saveProgress')
  MP.RegisterEvent('BNGE_tick', 'BNGE_tick')
  MP.CreateEventTimer('BNGE_tick', G.TICK_MS)
  local cfg = G.config()
  local timeText = 'real'
  if cfg.time then
    timeText = tostring(G.TIME) .. (cfg.time.speed > 0 and (' running at x' .. tostring(cfg.time.speed)) or ', fixed')
  end
  print(string.format('[BeamNG.Earth] plugin loaded (time %s, weather %s; players share world building on TCP port %s)',
    timeText, cfg.weather or 'real', tostring(cfg.sharePort or 'their own default')))
  if tostring(G.TIME or 'real'):lower() ~= 'real' and not cfg.time then
    print('[BeamNG.Earth] G.TIME "' .. tostring(G.TIME) .. '" is not "real" or HH:MM: the real time is used')
  end
  if tostring(G.WEATHER or 'real'):lower() ~= 'real' and not cfg.weather then
    print('[BeamNG.Earth] G.WEATHER "' .. tostring(G.WEATHER) .. '" is not one of real, clear, cloudy, rain, storm, '
      .. 'snow, fog: the real weather is used')
  end
end

return G
