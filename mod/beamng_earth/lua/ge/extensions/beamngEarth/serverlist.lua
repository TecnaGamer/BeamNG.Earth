-- BeamNG.Earth servers for BeamMP's server browser (the user, 2026-10-24: "show a list of all the .earth servers ...
-- make it so players without .earth cant see .earth server in the server list"). A public BeamNG.Earth server keeps
-- itself off BeamMP's public list and announces itself to our list service (native/serverlist, earth.tgforever.net)
-- instead; the UI's "BeamNG.Earth" tab in BeamMP's page (ui/ui-vue/mods/beamngEarth/earthServers.js) shows that list.
-- The game's Lua may only open connections to local addresses ("connect restricted"), so our world server fetches it
-- (earthd server_list) and this asks for it and keeps the answer. The UI polls state().
local M = {}

M.TIMEOUT_S = 12

local net = require('beamngEarth/net')
local waiting = nil         -- seconds since the request, while one is out
local result = { state = 'idle', servers = {}, error = nil, at = nil }

-- Pure: the list as the service sent it, each entry checked and its fields made safe for the page.
function M.toServers(list)
  if type(list) ~= 'table' then return nil end
  local out = {}
  for _, s in ipairs(list) do
    if type(s) == 'table' and type(s.ip) == 'string' and tonumber(s.port) then
      out[#out + 1] = { ip = s.ip, port = math.floor(tonumber(s.port)), name = tostring(s.name or ''),
        players = tonumber(s.players) or 0, maxPlayers = tonumber(s.maxPlayers) or 0,
        version = tostring(s.version or ''), place = tostring(s.place or '') }
    end
  end
  return out
end

local function finish(servers, err)
  waiting = nil
  if servers then result.servers = servers end
  result.state, result.error, result.at = err and 'error' or 'done', err, os.time()
end

-- Ask for the list (one already asked for carries on).
function M.refresh()
  if waiting then return true end
  if not net.isConnected() then
    finish(nil, "BeamNG.Earth's world server is not running (start the BeamNG.Earth app)")
    return false
  end
  net.send({ type = 'server_list' })
  waiting, result.state, result.error = 0, 'loading', nil
  return true
end

-- The world server's 'server_list' answer (core.lua's message dispatch).
function M.onList(msg)
  if type(msg) ~= 'table' then return end
  if msg.error then finish(nil, tostring(msg.error)) return end
  local servers = M.toServers(msg.servers)
  finish(servers or {}, servers == nil and 'bad list' or nil)
end

function M.onUpdate(dtReal)
  if not waiting then return end
  waiting = waiting + (dtReal or 0)
  if waiting > M.TIMEOUT_S then finish(nil, 'the server list did not answer') end
end

-- For the UI: { state = 'idle' | 'loading' | 'done' | 'error', servers, error, at }.
function M.state() return result end

return M
