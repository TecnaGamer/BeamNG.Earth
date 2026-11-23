-- The engine patches' states (tools/gcpatch: the Linux preload library; enginepatch.py / enginepatch_terfast.py: the
-- Windows launcher), read from their status files a few seconds after our level started: never silent when one is not
-- applied (2026-11-22: BeamNG 0.40.6 dropped terfast and texkeep for every Linux player without a word: their status
-- files said "not applied: another game build" and nobody looked).
--
-- Each patch writes <user>/temp/beamngEarth_<name>.txt, one line "<unix time> ...: <state>", and writes it again when
-- our level starts (its level flag, groundcover.PATCH_FLAG), applied or not (bnge_report_sticky). So a line older than
-- the level's start is another game run's (a game started without the patches: "none").
--
-- state per patch (also the telemetry setup's enginePatches, TELEMETRY.md; perfsummary.setup):
--   on       applied in our level (or ready: it acts while our level runs)
--   off      switched off on purpose (its BNGEARTH_*_OFF switch, the launcher's --no-gcpatch, the Windows switches)
--   failed   not applied: another game build, half applied, a refusal; the player is told once a game run
--   none     no fresh status file: the patch is not there (started without the launcher, Windows for the
--            Linux-only gcpatch / texkeep)
-- Pure but for the file reads (M.read, replaced in tests); one read of four small files a level start, nothing per
-- frame (core calls M.tick from its once-a-second performance sample until M.done()).

local M = {}

M.NAMES = { 'gcpatch', 'texkeep', 'terfast', 'dtsmooth' }
M.WHAT = {
  gcpatch = 'grass kept across terrain block moves',
  texkeep = 'faster ground texture updates',
  terfast = 'faster terrain block swaps',
  dtsmooth = 'steady game speed',
}
M.CHECK_S = 4          -- s after the level start: every patch has written its line by then (it polls every 0.25 s)
M.SLACK_S = 3          -- a line this much older than the level start still counts (clock steps, the flag's poll)

local st = { started = nil, done = false, states = nil, why = {}, warned = false }

function M.read(name)
  local f = rawget(_G, 'readFile')
  if not f then return nil end
  local ok, text = pcall(f, '/temp/beamngEarth_' .. name .. '.txt')
  return ok and text or nil
end

-- (state, detail) of one status line; t0: the level start (unix s), nil = any age
function M.classify(text, t0)
  if type(text) ~= 'string' or text == '' then return 'none', nil end
  local line = text:match('^[^\r\n]*') or ''
  local t, msg = line:match('^(%d+)[^:]*:%s*(.*)$')
  t = tonumber(t)
  if not t or not msg then return 'none', nil end
  if t0 and t < t0 - M.SLACK_S then return 'none', 'old' end
  local low = msg:lower()
  if low:find('half applied', 1, true) or low:find('not applied', 1, true) or low:find('^stopped')
      or low:find('could not be patched', 1, true) or low:find('no thread', 1, true) then
    return 'failed', msg
  end
  if low:find('^off') then return 'off', msg end
  if low:find('^applied') or low:find('^ready') or low:find('^stock') or low:find('^installed')
      or low:find('^already applied') then
    return 'on', msg
  end
  return 'failed', msg
end

function M.levelStart(now)
  st.started, st.done, st.states, st.why = now, false, nil, {}
end

function M.levelEnd() st.started = nil end

-- Once a second from core (its performance sample): reads the files CHECK_S after the level started; true when done.
-- warn(title, text): told once a game run about the patches not applied.
function M.tick(now, warn)
  if st.done then return true end
  if not st.started or now < st.started + M.CHECK_S then return false end
  local states, failed = {}, {}
  for _, n in ipairs(M.NAMES) do
    local s, why = M.classify(M.read(n), st.started)
    states[n], st.why[n] = s, why
    if s == 'failed' then failed[#failed + 1] = n end
  end
  st.states, st.done = states, true
  local L = rawget(_G, 'log')
  for _, n in ipairs(failed) do
    if L then pcall(L, 'W', 'beamngEarth', 'engine patch ' .. n .. ' NOT APPLIED: ' .. tostring(st.why[n])) end
  end
  if #failed > 0 and not st.warned and warn then
    st.warned = true
    local parts = {}
    for _, n in ipairs(failed) do parts[#parts + 1] = n .. ' (' .. M.WHAT[n] .. ')' end
    pcall(warn, 'BeamNG.Earth: engine patch not applied',
      'This BeamNG.drive version is not one the engine patches know: ' .. table.concat(parts, ', ') ..
      ' did not apply, so the game runs without them (it still works; ground updates can be slower). '
      .. 'A BeamNG.Earth update brings them back. Details: temp/beamngEarth_<name>.txt in the game\'s user folder.')
  end
  return true
end

function M.done() return st.done end
-- a level started and its read is still to come
function M.pending() return st.started ~= nil and not st.done end
-- { gcpatch = 'on', ... } once read, else nil
function M.states() return st.states end
function M.why(name) return st.why[name] end
function M._reset() st = { started = nil, done = false, states = nil, why = {}, warned = false } end

return M
