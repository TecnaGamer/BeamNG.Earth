-- Executed by core/modmanager.lua:704-711 (FS:findFiles('/scripts/', 'modScript.lua') + dofile)
-- once the mod is mounted. 'manual' keeps the extension loaded across level changes, and the mod manager loads
-- every 'manual' extension right after the modScripts (loadManualUnloadExtensions): an extensions.load here
-- logs a deprecation warning per call.
-- First: another copy of BeamNG.Earth already running (BeamMP turned the player's copy off for the server's, or back
-- on): its code unloaded, so the mod manager loads this copy's (codeswap.lua; dofile: a require would return the
-- running copy's module).
do
  local ok, cs = pcall(dofile, '/lua/ge/extensions/beamngEarth/codeswap.lua')
  if ok and type(cs) == 'table' and type(cs.check) == 'function' then pcall(cs.check, 'its modScript', false) end
end
setExtensionUnloadMode('beamngEarth_core', 'manual')
-- The in-vehicle satellite map (dashboard.lua: receives mission lifecycle callbacks).
setExtensionUnloadMode('beamngEarth_dashboard', 'manual')
-- The pause-menu settings page's Lua side (settings.lua) and the dev tools (dev.lua: everything off
-- until the player turns something on; its hooks return at their first line meanwhile).
setExtensionUnloadMode('beamngEarth_settings', 'manual')
setExtensionUnloadMode('beamngEarth_dev', 'manual')
-- The in-game reports (report.lua: Ctrl+Shift+E a report / GitHub issue, Ctrl+Shift+Y the 30 s performance profiler).
setExtensionUnloadMode('beamngEarth_report', 'manual')
-- The BeamNG.Earth servers list for the tab in BeamMP's server browser (serverlist.lua; fetches only when asked).
setExtensionUnloadMode('beamngEarth_serverlist', 'manual')
-- The 3D map mode (map3d.lua: the orbit camera core/cameraModes/earthGlobe.lua, its fly-up / fly-down paths).
setExtensionUnloadMode('beamngEarth_map3d', 'manual')
-- The modding API for other mods (api.lua: beamngEarth_api; docs/ADDONS.md).
setExtensionUnloadMode('beamngEarth_api', 'manual')
-- The station finder (finder.lua: Nearest station from the radial menu, a key or the map; the low fuel prompt).
setExtensionUnloadMode('beamngEarth_finder', 'manual')
