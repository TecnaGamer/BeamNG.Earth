-- Executed by core/modmanager.lua:704-711 (FS:findFiles('/scripts/', 'modScript.lua') + dofile)
-- once the mod is mounted. 'manual' keeps the extension loaded across level changes, and the mod manager loads
-- every 'manual' extension right after the modScripts (loadManualUnloadExtensions): an extensions.load here
-- logs a deprecation warning per call.
setExtensionUnloadMode('beamngEarth_core', 'manual')
-- The pause-menu settings page's Lua side (settings.lua) and the dev tools (dev.lua: everything off
-- until the player turns something on; its hooks return at their first line meanwhile).
setExtensionUnloadMode('beamngEarth_settings', 'manual')
setExtensionUnloadMode('beamngEarth_dev', 'manual')
-- The in-game reports (report.lua: Ctrl+Shift+E a report / GitHub issue, Ctrl+Shift+Y the 30 s performance profiler).
setExtensionUnloadMode('beamngEarth_report', 'manual')
-- The BeamNG.Earth servers list for the tab in BeamMP's server browser (serverlist.lua; fetches only when asked).
setExtensionUnloadMode('beamngEarth_serverlist', 'manual')
