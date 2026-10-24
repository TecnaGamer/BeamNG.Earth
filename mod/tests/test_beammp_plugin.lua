-- luajit mod/tests/test_beammp_plugin.lua  (BeamNG.Earth's BeamMP server plugin, beammp/Server/BeamNGEarth/main.lua,
-- its pure part; the server runs Lua 5.3, this file keeps to what both have): every player's anchor and place for
-- the clients' position conversion, sent at once when an anchor changes; a joining player sent to the others.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local G = dofile(dir .. '/../../beammp/Server/BeamNGEarth/main.lua')

local M_PER_DEG = 111194.9     -- (G.dist's sphere)

T.check(math.abs(G.dist(0, 0, 1000 / M_PER_DEG, 0) - 1000) < 1, 'dist: 1 km north')

-- the first player: alone, nowhere to go; its anchor new: sent at once
G.players, G.clock = {}, 0
local place, moved = G.onPose(1, { v = G.PROTO, aLat = 36, aLon = -118, lat = 36, lon = -118, joined = true }, 'a')
T.check(place == nil and moved == true, 'the first player: alone, its anchor sent at once')
place, moved = G.onPose(1, { v = G.PROTO, aLat = 36, aLon = -118, lat = 36.001, lon = -118 }, 'a')
T.check(place == nil and moved == false, 'the same anchor: not sent at once (the tick sends it)')
place, moved = G.onPose(1, { v = G.PROTO, aLat = 36.2, aLon = -118, lat = 36.2, lon = -118 }, 'a')
T.check(moved == true, 'a rebase: the anchors go out at once')

-- a second player joining 80 miles away: sent to the first; near: not
G.tick(2)
place = G.onPose(2, { v = G.PROTO, aLat = 35, aLon = -118.7, lat = 35, lon = -118.7, joined = true }, 'b')
T.check(place and place.lat == 36.2 and place.lon == -118, 'a joining player far away: sent to the first player')
place = G.onPose(3, { v = G.PROTO, aLat = 36.2, aLon = -118, lat = 36.201, lon = -118, joined = true }, 'c')
T.check(place == nil, 'a joining player already near them: stays')

-- the anchors message: every live player's anchor and place, keyed by the id as a string
local a = G.anchors(G.live())
T.check(a['1'] and a['1'].aLat == 36.2 and a['2'] and a['2'].lon == -118.7 and a['3'], 'anchors: every player')
T.check(a['1'].name == 'a' and a['1'].h == nil, 'anchors: the name, no heading without one')
G.onPose(1, { v = G.PROTO, aLat = 36.2, aLon = -118, lat = 36.2, lon = -118, h = 90.5 }, 'a')
T.check(G.anchors(G.live())['1'].h == 90.5, 'anchors: the heading (the map arrows)')
G.onPose(1, { v = G.PROTO, aLat = 36.2, aLon = -118, lat = 36.2, lon = -118, h = 'x' }, 'a')
T.check(G.anchors(G.live())['1'].h == nil, 'anchors: a bad heading left out')

-- config: the sharing port set at the top of main.lua, a bad one left out (each player's own default)
T.check(G.config().sharePort == 47621, 'config: the default sharing port')
G.SHARE_PORT = 50000
T.check(G.config().sharePort == 50000, 'config: an edited port')
for _, bad in ipairs({ 80, 70000, 'x' }) do
  G.SHARE_PORT = bad
  T.check(G.config().sharePort == nil, 'config: a bad port left out: ' .. tostring(bad))
end
G.SHARE_PORT = 47621

-- peers: the sharing players on one anchor, the same sorted list for each; a player on another anchor alone
G.players = {}
G.onPose(1, { v = G.PROTO, aLat = 40, aLon = -111.9, lat = 40, lon = -111.9, sp = 47621, sc = 'c1' }, 'u1')
G.onPose(2, { v = G.PROTO, aLat = 40, aLon = -111.9, lat = 40.01, lon = -111.9, sp = 47621, sc = 'c1' }, 'u2')
G.onPose(3, { v = G.PROTO, aLat = 45.46, aLon = 9.19, lat = 45.46, lon = 9.19, sp = 47621, sc = 'c1' }, 'it')
G.onPose(4, { v = G.PROTO, aLat = 40, aLon = -111.9, lat = 40, lon = -111.9 }, 'noshare')
G.onPose(5, { v = G.PROTO, aLat = 40, aLon = -111.9, lat = 40, lon = -111.9, sp = 99999, sc = 'c1' }, 'badport')
for pid, ip in pairs({ [1] = '10.0.0.1', [2] = '10.0.0.2', [3] = '10.0.0.3', [4] = '10.0.0.4', [5] = '10.0.0.5' }) do
  G.players[pid].ip = ip
end
local pe = G.peers(G.live())
T.check(pe[1] and #pe[1].members == 2 and pe[1].members[1].id == '1' and pe[1].members[2].host == '10.0.0.2'
  and pe[2] and G.peersKey(pe[1]) == G.peersKey(pe[2]), 'peers: Utah\'s two share one list')
T.check(pe[3] and #pe[3].members == 1, 'peers: Italy alone')
T.check(pe[4] == nil and pe[5] == nil, 'peers: no sharing port (or a bad one): left out')
G.players, G.sentPeers = {}, {}

-- bad input ignored; stale poses and players who left out
T.check(G.onPose(4, { v = 1, aLat = 1, aLon = 1, lat = 1, lon = 1 }, 'd') == nil and G.players[4] == nil,
  'another protocol: ignored')
T.check(G.onPose(5, { v = G.PROTO, aLat = 1, aLon = 1, lat = 95, lon = 1 }, 'e') == nil and G.players[5] == nil,
  'a bad latitude: ignored')
G.tick(G.STALE_S + 1)
T.check(next(G.tick(0)) == nil, 'no pose for STALE_S: left out')
G.onLeave(1)
T.check(G.players[1] == nil, 'left: forgotten')

-- the BeamNG.Earth server list: ServerConfig.toml read, what is announced, the background curl
local cfg = G.readConfig('[General]\nName = "Earth # one" # comment\nPort = 30814\nMaxPlayers = 8  # max\nPrivate = true\n')
T.check(cfg.name == 'Earth # one' and cfg.port == 30814 and cfg.maxPlayers == 8 and cfg.private == true,
  'readConfig: name, port, cap, private (comments left out)')
local zeros = '00000000-0000-0000-0000-000000000000'
local key = '3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b'
T.check(G.validKey(key) and not G.validKey(zeros) and not G.validKey('') and not G.validKey(nil) and not G.validKey('abc'),
  'validKey: a key, not the placeholder or junk')
T.check(G.readConfig('AuthKey = "' .. key .. '"').authKey == key, 'readConfig: the key')
T.check(G.listable({ private = false, authKey = key }), 'listable: public with a key')
T.check(not G.listable({ private = true, authKey = key }), 'listable: Private = true: direct connect only')
T.check(not G.listable({ private = false, authKey = zeros }), 'listable: no key: direct connect only')
G.LISTED = false
T.check(not G.listable({ private = false, authKey = key }), 'listable: G.LISTED = false')
G.LISTED = true
local a = G.announcement(cfg, { [1] = { lat = 36.61, lon = -118.17, joinedAt = 5 }, [2] = { lat = 50, lon = 8, joinedAt = 9 } })
T.check(a.port == 30814 and a.players == 2 and a.maxPlayers == 8 and a.place == '36.6, -118.2', 'announcement: from the longest-joined player')
T.check(G.announcement(cfg, {}).place == nil and G.announcement(cfg, {}).players == 0, 'announcement: no one on')
T.check(G.announceCmd('http://x/announce', 'f.json', false):match('^curl .* @f%.json "http://x/announce" >/dev/null 2>&1 &$') ~= nil,
  'announceCmd: Linux, in the background')
T.check(G.announceCmd('http://x/announce', 'f.json', true):match('^start "" /b curl .* >NUL 2>&1$') ~= nil, 'announceCmd: Windows, in the background')

T.done('test_beammp_plugin')
