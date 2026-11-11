-- Debug trace of what the mod hands the engine that loads textures (the game-freeze hunt, 2026-11-11: the engine's
-- TextureCooker deadlocked on an unnamed texture after cold teleports). Off unless the file
-- /temp/beamngEarth/texdbg.flag exists (checked once a session); then each call is one log line.
local M = {}
local on = nil
function M.on()
  if on == nil then on = (FS and FS:fileExists('/temp/beamngEarth/texdbg.flag')) and true or false end
  return on
end
function M.log(kind, a, b)
  if not M.on() then return end
  local t = (os.clockhp or os.clock)()
  log('I', 'texdbg', string.format('%.3f %s %s %s', t, kind, tostring(a), tostring(b or '')))
end
return M
