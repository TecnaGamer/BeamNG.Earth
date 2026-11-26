-- The map data's release a level's world is built from (the Overture release, server overture.effective: the app's
-- supervisor picks it once per app session and every world server it starts reads it, overturepick.py).
--
-- The pin is set from the LIVE connection's hello only: at a level start the connected server's (its hello may have
-- come at the main menu), else the first hello in the level. A disconnect forgets the live connection's release, so
-- a level started while no server is connected is pinned by the next server's hello, never by an old one's. Once
-- pinned, a hello of another release, or of none (a server too old to say) after a known one, is a 'mismatch': the
-- caller stops streaming (no anchor, no resync, the block does not follow) and tells the player to load the level
-- again (core.lua st.releaseHold). A level pinned by a server that says none (an older server) takes only that same
-- server session back (its sessionId): another session that says none may be another build of another release.
-- Pure: the state is a table the caller keeps.
local M = {}

M.NONE = 'none'         -- a hello without a release id (an older server): known to be unknown

local function norm(rel)
  return (type(rel) == 'string' and rel:match('^%d%d%d%d%-%d%d%-%d%d%.%d%d?$')) and rel or M.NONE
end

function M.new() return { pin = nil, live = nil } end

-- The connection went (or a reconnect was asked): its release is no longer the live one.
function M.disconnected(s) s.live, s.liveSession = nil, nil end

-- A level starts: pinned to the live connection's release, or to none yet (the next hello in the level pins it).
function M.levelStart(s)
  s.pin, s.pinSession = s.live, s.liveSession
  return s.pin
end

-- A server's hello: 'ok' or 'mismatch' (inLevel: our level runs; outside it nothing is pinned). sessionId: the
-- server's session (only an older server's pin needs it).
function M.hello(s, helloRelease, inLevel, sessionId)
  s.live, s.liveSession = norm(helloRelease), sessionId
  if not inLevel then return 'ok' end
  if s.pin == nil then
    s.pin, s.pinSession = s.live, s.liveSession
    return 'ok'
  end
  if s.pin ~= s.live then return 'mismatch' end
  if s.pin == M.NONE and (sessionId == nil or sessionId ~= s.pinSession) then return 'mismatch' end
  return 'ok'
end

-- The level's pinned release (nil: not pinned yet; M.NONE: an older server's).
function M.pinned(s) return s.pin end

return M
