-- The settings page's "Restart world server" (the user, 2026-11-15: "a button to restart the server without having to
-- close beamng"; server/srvrestart.py has the server's and the launcher's side). Pure state (runs under plain luajit):
-- core.lua feeds it the server's messages and the frame's facts and does what it answers (the car's hold: simhold).
--
-- Which settings wait for a restart: the server's hello names the start-time values of the settings it reads only when
-- it starts (startSettings: the Experimental switches, the ground grid, the cores, the priority, the 3dmr.eu models) and
-- the 'memory' answer says when Max RAM changed (its caches are sized at the start); pending() lists those that differ
-- from the player's settings now. The button is offered when the server says a supervisor starts it again
-- (hello.restartable: the app's launcher, start.py).
--
-- A restart's phases: asked (sent, waiting for the server's yes) -> stopping (yes: it closes) -> starting (the
-- connection gone: waiting for the new server's hello, a new session) -> loading (the resync and the world around the
-- car) -> done (the line shown DONE_S), or failed (the reason shown FAILED_S). From the yes until the world near the
-- car is there again the car is held (step's answer): the game paused, in BeamMP only our own car (simhold), let go for
-- the frames the resync needs to commit in single player (it moves vehicles only with physics running, core.lua). Each
-- phase has a time limit: nothing waits for ever, the car is never held for good.
local i18n = require('beamngEarth/i18n')
local M = {}

M.ASK_S = 10        -- s for the server's answer to the request
M.STOP_S = 30       -- s for its connection to go after its yes
M.DOWN_S = 180      -- s for the new server's hello (a slow PC's start; the launcher waits for the port, then starts it)
M.LOAD_S = 60       -- s the car is held after that hello for the resync and the roads round the car
M.DONE_S = 8        -- s the "restarted" line stays
M.FAILED_S = 30     -- s a failure's line stays
M.BUSY = { asked = true, stopping = true, starting = true, loading = true }

M.TEXT = {
  asked = 'Restarting the world server ...',
  stopping = 'Restarting the world server: the world reloads around you in a few seconds (the game stays open)',
  starting = 'Restarting the world server: starting it again with your new settings (the game stays open)',
  loading = 'World server restarted: loading the world around you',
  done = 'World server restarted: your new settings are in use',
}
M.HELD = ' - the car is held still meanwhile'

function M.new() return { phase = nil } end

-- The ids (in order) whose value in values differs from the server's start-time value (start: hello.startSettings),
-- but those set by hand in the server's environment (fixed: hello.startFixed; a restart would not change them), and
-- 'maxRamGb' when the server says Max RAM changed since its start (memRestart). Pure.
function M.pending(values, start, memRestart, order, fixed)
  local out = {}
  local skip = {}
  for _, k in pairs(type(fixed) == 'table' and fixed or {}) do skip[k] = true end
  if type(values) == 'table' and type(start) == 'table' then
    for _, k in ipairs(order or {}) do
      local s, v = start[k], values[k]
      if s ~= nil and v ~= nil and v ~= s and k ~= 'maxRamGb' and not skip[k] then out[#out + 1] = k end
    end
  end
  if memRestart then out[#out + 1] = 'maxRamGb' end
  return out
end

local function fail(r, clock, why)
  r.phase, r.at, r.error = 'failed', clock, tostring(why or 'the restart did not work')
end
M.fail = fail

-- The request was sent (session: the server's session id now).
function M.request(r, clock, session)
  r.phase, r.at, r.oldSession, r.newSession, r.error, r.letGo = 'asked', clock, session, nil, nil, false
end

-- The server's 'server_restart' answer.
function M.answer(r, clock, ok, err)
  if r.phase ~= 'asked' then return end
  if ok then r.phase, r.at = 'stopping', clock
  else fail(r, clock, err or 'the world server refused') end
end

-- The connection went (net.lua '_disconnected').
function M.disconnected(r, clock)
  local ph = r.phase
  if ph == 'asked' or ph == 'stopping' or ph == 'loading' then r.phase, r.at = 'starting', clock end
end

-- A server's hello (session: its session id): a new one is the restarted server.
function M.hello(r, clock, session)
  local ph = r.phase
  if (ph == 'starting' or ph == 'stopping') and session ~= nil and session ~= r.oldSession then
    r.phase, r.at, r.newSession = 'loading', clock, session
  end
end

-- Once a frame while a restart is under way (r.phase). c: { clock, inLevel (our level, a car: a hold means
-- something), resyncReady (the resync's anchor ready to commit), worldReady (the resync committed with the new
-- server, the road tiles round the car in), mp (a BeamMP session) }. Returns whether the car is to be held now.
function M.step(r, c)
  local ph = r.phase
  if not ph then return false end
  local t = c.clock - (r.at or c.clock)
  if ph == 'asked' and t > M.ASK_S then fail(r, c.clock, 'no answer from the world server')
  elseif ph == 'stopping' and t > M.STOP_S then fail(r, c.clock, 'the world server did not stop')
  elseif ph == 'starting' and t > M.DOWN_S then
    fail(r, c.clock, 'the world server did not come back: its BeamNG.Earth window says why (start BeamNG.Earth '
      .. 'again if it stays away)')
  elseif ph == 'loading' and (c.worldReady or t > M.LOAD_S) then r.phase, r.at = 'done', c.clock
  elseif (ph == 'done' and t > M.DONE_S) or (ph == 'failed' and t > M.FAILED_S) then r.phase = nil end
  ph = r.phase
  if not c.inLevel or r.letGo then return false end
  if ph == 'stopping' or ph == 'starting' then return true end
  if ph == 'loading' then return c.mp == true or not c.resyncReady end
  return false
end

-- The line for the banner and the settings page (nil when nothing is under way); held: the car is held now.
function M.text(r, held)
  local ph = r.phase
  if not ph then return nil end
  if ph == 'failed' then return i18n.t('restart.failedDetail', { error = tostring(r.error) }, 'World server restart: ' .. tostring(r.error)) end
  local key = ({
    asked = 'restart.phase.asked', stopping = 'restart.phase.stopping', starting = 'restart.phase.starting',
    loading = 'restart.phase.loading', done = 'restart.phase.done',
  })[ph]
  if not key then return nil end
  local t = i18n.t(key, nil, M.TEXT[ph])
  if held and M.BUSY[ph] then t = t .. i18n.t('restart.held', nil, M.HELD) end
  return t
end

-- The banner's level for it.
function M.level(r)
  if r.phase == 'failed' then return 'warn' end
  if r.phase == 'done' then return 'ok' end
  return 'info'
end

return M
