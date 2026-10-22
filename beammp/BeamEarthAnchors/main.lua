-- BeamEarthAnchors: relays each player's current BeamNG.Earth anchor (lat, lon) to every other
-- player, so the client-side mpanchor.lua extension (mod/beamng_earth) can correct remote vehicle
-- positions across independent floating origins. Install: copy this folder (BeamEarthAnchors/) as
-- a whole into the server's Resources/Server/. No config; no other files needed.
--
-- Protocol (all client<->server, relayed as-is, never parsed by this plugin beyond pid/lat/lon):
--   client -> server  'bngEarthAnchor'       {"lat":n,"lon":n}
--   server -> clients 'bngEarthAnchorRelay'  {"pid":n,"lat":n,"lon":n}
-- A client without BeamNG.Earth (or without this build of it) never sends 'bngEarthAnchor' and
-- just ignores an unknown 'bngEarthAnchorRelay' event: no impact on a plain BeamMP session.

local anchors = {} -- [tostring(playerId)] = {lat, lon}: the latest per connected player

-- MP.RegisterEvent resolves its function-name argument as a GLOBAL in this plugin's Lua state
-- (BeamMP-Server TLuaEngine.cpp: "auto Fn = mStateView[Handler]"), so these must not be `local`;
-- named with the plugin's prefix since the lookup is a plain global table read.
function BeamEarthAnchors_onAnchor(playerId, data)
  local d = Util.JsonDecode(data) -- nil (logged by the engine) on malformed json, never throws
  if type(d) ~= 'table' or type(d.lat) ~= 'number' or type(d.lon) ~= 'number' then
    print('[BeamEarthAnchors] malformed anchor from player ' .. tostring(playerId) .. ': ' .. tostring(data))
    return
  end
  anchors[tostring(playerId)] = { lat = d.lat, lon = d.lon }
  local relay = Util.JsonEncode({ pid = playerId, lat = d.lat, lon = d.lon })
  MP.TriggerClientEvent(-1, 'bngEarthAnchorRelay', relay)
end

-- A late joiner needs the roster that was announced before they connected.
function BeamEarthAnchors_onPlayerJoin(playerId)
  for pidStr, a in pairs(anchors) do
    local pid = tonumber(pidStr)
    if pid and pid ~= playerId then
      MP.TriggerClientEvent(playerId, 'bngEarthAnchorRelay', Util.JsonEncode({ pid = pid, lat = a.lat, lon = a.lon }))
    end
  end
end

function BeamEarthAnchors_onPlayerDisconnect(playerId)
  anchors[tostring(playerId)] = nil
end

MP.RegisterEvent('bngEarthAnchor', 'BeamEarthAnchors_onAnchor')
MP.RegisterEvent('onPlayerJoin', 'BeamEarthAnchors_onPlayerJoin')
MP.RegisterEvent('onPlayerDisconnect', 'BeamEarthAnchors_onPlayerDisconnect')
