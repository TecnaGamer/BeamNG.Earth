-- Only shipped inside the server's client ZIP. This unique path cannot be masked
-- by the older local app's /scripts/beamng_earth/modScript.lua during activation.
-- BeamMP disables non-server mods after mounting the downloads; disable our own
-- local copy first so all refreshed Lua modules resolve from the server's ZIP.
local mp, mm = rawget(_G,'MPCoreNetwork'), rawget(_G,'core_modmanager')
if mp and mp.isMPSession and mp.isMPSession() and mm and mm.getModDB and mm.deactivateMod then
  local entry = mm.getModDB('beamng_earth')
  if entry and entry.active then mm.deactivateMod('beamng_earth') end
end
dofile('/scripts/beamng_earth/modScript.lua')
