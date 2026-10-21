-- BeamMP co-op: makes remote players' vehicle positions correct across independent floating
-- origins (docs/ROADMAP.md "Multiplayer (BeamMP)"). Every player runs their own earthd and rebases
-- on their own schedule (core.lua, rebase.lua unchanged); this module only fixes what BeamMP's
-- position sync assumes and BeamNG.Earth breaks: that every client's Lua-space vec3 means the same
-- real-world point. It does not change collidable ground, so players sharing one physics
-- TerrainBlock (~1-4 km depending on hello.blockSize, docs/ARCHITECTURE.md "Multiple TerrainBlocks:
-- Not supported by physics") is still the usable range; this only keeps them correctly placed
-- relative to each other within it and while further apart (just without solid ground there).
--
-- Design (no change to BeamMP's wire format, no central authority):
-- - Each player's current anchor is just their current real-world origin (lat, lon). Any anchor can
--   be converted into any other with geodesy.Anchor:transformTo (exact, doubles; already used by
--   rebase.lua for the player's own frame changes).
-- - Whenever a player's own anchor changes (core.lua, both commitTeleport and finishRebase), they
--   announce (lat, lon) over a tiny custom BeamMP event, relayed to everyone by a server-side
--   plugin (beammp/BeamEarthAnchors/main.lua in this repo; copy it into the server's
--   Resources/Server/). A late joiner gets the current roster replayed by the plugin on connect.
-- - Every client keeps the last-announced anchor of every other player. When a position packet
--   for a remote vehicle arrives, if its owner's last-announced anchor differs from ours, the
--   packet (position, velocity, orientation, angular velocity) is transformed into our frame before
--   BeamMP's own code (prediction, smoothing, the teleport-distance correction) ever sees it - so
--   all of that keeps working unmodified, just on already-correct numbers.
-- - Between two anchor changes (the common case) the transform is cached per remote player and
--   reused; it is only rebuilt when either side's announced anchor changes.
--
-- Hook point: positionGE.handle(rawData), the single entry point for every incoming position
-- packet regardless of the position-smoother setting (positionGE.lua: handle() branches to either
-- smoothPosExec or applyPos, both fed from the same raw "p:<serverVehicleID>:{json}" string).
-- Wrapping it here, once, covers both branches and needs no change to BeamMP itself.

local geodesy = require('beamngEarth/geodesy')

local M = {}

local myLat, myLon = nil, nil            -- our own current anchor (announced by core.lua)
local remote = {}                        -- [playerId] = { lat, lon }, last announced
local xfCache = {}                       -- [playerId] = { myLat, myLon, rLat, rLon, xf } (lazy, see transformFor)
local installed = false

local function isMPSession()
  return MPCoreNetwork ~= nil and MPCoreNetwork.isMPSession ~= nil and MPCoreNetwork.isMPSession()
end

-- Called by core.lua right after st.anchor is set (commitTeleport, finishRebase): every anchor
-- change, including the player's very first one. A no-op outside a BeamMP session.
function M.announceOwn(lat, lon)
  myLat, myLon = lat, lon
  if not isMPSession() or TriggerServerEvent == nil then return end
  local ok, err = pcall(TriggerServerEvent, 'bngEarthAnchor', jsonEncode({ lat = lat, lon = lon }))
  if not ok then log('W', 'beamngEarth', 'mpanchor: announce failed: ' .. tostring(err)) end
end

-- beammp/BeamEarthAnchors/main.lua relays {pid, lat, lon} to everyone (and replays its roster to a
-- player who just joined), so this is the only inbound message this module needs to handle.
local function onRelay(dataStr)
  local ok, d = pcall(jsonDecode, dataStr)
  if not ok or type(d) ~= 'table' or not d.pid or not d.lat or not d.lon then return end
  local pid = tonumber(d.pid)
  if not pid then return end
  remote[pid] = { lat = d.lat, lon = d.lon }
end

-- The cached Transform from player `pid`'s last-announced frame into ours, or nil (unknown peer
-- anchor, or no local anchor yet: pass the packet through unmodified, same as today).
local function transformFor(pid)
  if not myLat then return nil end
  local r = remote[pid]
  if not r then return nil end
  local c = xfCache[pid]
  if c and c.myLat == myLat and c.myLon == myLon and c.rLat == r.lat and c.rLon == r.lon then
    return c.xf
  end
  local theirs, ours = geodesy.newAnchor(r.lat, r.lon), geodesy.newAnchor(myLat, myLon)
  local xf = theirs:transformTo(ours)
  xfCache[pid] = { myLat = myLat, myLon = myLon, rLat = r.lat, rLon = r.lon, xf = xf }
  return xf
end

-- Transform one decoded position packet's pos/vel/rot/rvel in place: pos is a world point
-- (xf:apply), vel and rvel are directions/rates (xf:rotate, no translation), rot is the vehicle's
-- orientation quaternion, composed with the frame's own rotation the same way rebase.lua's
-- updateResetState composes a vehicle's orientation across a rebase ("vehicle's rotation first,
-- then R"): newRot = quatMul(rot, xf:beamngQuat()).
local function applyTransform(decoded, xf)
  if decoded.pos then
    decoded.pos[1], decoded.pos[2], decoded.pos[3] = xf:apply(decoded.pos[1], decoded.pos[2], decoded.pos[3])
  end
  if decoded.vel then
    decoded.vel[1], decoded.vel[2], decoded.vel[3] = xf:rotate(decoded.vel[1], decoded.vel[2], decoded.vel[3])
  end
  if decoded.rvel then
    decoded.rvel[1], decoded.rvel[2], decoded.rvel[3] = xf:rotate(decoded.rvel[1], decoded.rvel[2], decoded.rvel[3])
  end
  if decoded.rot then
    local bx, by, bz, bw = xf:beamngQuat()
    decoded.rot[1], decoded.rot[2], decoded.rot[3], decoded.rot[4] =
      geodesy.quatMul(decoded.rot[1], decoded.rot[2], decoded.rot[3], decoded.rot[4], bx, by, bz, bw)
  end
end

-- Wraps positionGE.handle: corrects a 'p' (position) packet's embedded JSON before handing the
-- (possibly rewritten) raw string to the original handle, which then runs completely unmodified.
local function wrapHandle(original)
  return function(rawData)
    local code, serverVehicleID, data = string.match(rawData, '^(%a)%:(%d+%-%d+)%:({.*})')
    if code ~= 'p' or not serverVehicleID then return original(rawData) end
    local pid = tonumber(serverVehicleID:match('^(%d+)%-'))
    local xf = pid and transformFor(pid) or nil
    if not xf then return original(rawData) end
    local ok, decoded = pcall(jsonDecode, data)
    if not ok or type(decoded) ~= 'table' then return original(rawData) end
    applyTransform(decoded, xf)
    local ok2, reencoded = pcall(jsonEncode, decoded)
    if not ok2 then return original(rawData) end
    return original(code .. ':' .. serverVehicleID .. ':' .. reencoded)
  end
end

-- Installs the hooks once positionGE and the BeamMP event globals exist. BeamNG's extension loader
-- exposes every loaded GE extension as a bare global of its module name too (BeamMP's own code
-- calls it the same way, e.g. MPGameNetwork.lua: "positionGE.handle(params)"), so this patches the
-- same table everyone else calls through, not a local copy.
-- Safe to call repeatedly (every onUpdate, see core.lua): a no-op after the first success, and a
-- no-op entirely in singleplayer (positionGE never loads there).
function M.install()
  if installed then return end
  if AddEventHandler == nil or positionGE == nil then return end
  positionGE.handle = wrapHandle(positionGE.handle)
  AddEventHandler('bngEarthAnchorRelay', onRelay)
  installed = true
  log('I', 'beamngEarth', 'mpanchor: installed (BeamMP cross-anchor position fixups active)')
end

-- Console diagnostic (beamngEarth_core.mpStatus(), see core.lua): whether the hook installed, our
-- own last-announced anchor, and every peer anchor we have heard about so far. An empty peer list
-- after both players have spawned and moved means the announce/relay chain is broken somewhere
-- (not installed, not in an MP session, the server plugin not loaded, or not yet relayed).
function M.status()
  local parts = {
    'installed=' .. tostring(installed),
    'isMPSession=' .. tostring(isMPSession()),
    'myAnchor=' .. tostring(myLat) .. ',' .. tostring(myLon),
  }
  local n = 0
  for pid, r in pairs(remote) do
    n = n + 1
    parts[#parts + 1] = string.format('peer#%s=%.5f,%.5f', tostring(pid), r.lat, r.lon)
  end
  parts[#parts + 1] = 'knownPeers=' .. n
  return table.concat(parts, ' | ')
end

return M
