-- BeamMP (multiplayer): whether the game is in a BeamMP session, and the recommendation to install BeamNG.Earth's world
-- generator for a player who joined a BeamNG.Earth server without it (the user, 2026-10-22: "Add a popup with players
-- without the mod that highly recommends them install the .earth server"). Players with the generator build the world
-- on their own PC; without it the world has to come from the server or other players, slower (the hybrid plan,
-- .claude/work/beammp_plan.md). BeamMP's rules ask for no spam: one window per session, and "Don't show again".
local M = {}

M.RECOMMEND_WAIT_S = 15     -- in our level this long in a BeamMP session with no world generator: the window
M.INSTALL_URL = 'https://github.com/TecnaGamer/BeamNG.Earth/releases/latest'
M.TITLE = 'This server uses BeamNG.Earth'
M.TEXT = 'BeamNG.Earth builds the real world around you.<br><br>For the best experience, install the free BeamNG.Earth app: '
  .. 'it builds the world on your own PC, so it loads much faster and in full detail. Without it, the world comes from '
  .. 'the server and other players, and can load slowly.'
M.AFTER_TEXT = 'Install BeamNG.Earth from the page that opened, start it, then rejoin this server.'
M.SETTING = 'beammpRecommendOff'
-- BeamMP's HUD apps (chat, session bar, player list) and BeamNG.Earth's in one app layout
-- (settings/ui_apps/originalLayouts/default/beamngEarthMP.uilayout.json; the user, 2026-10-22: "combine the BeamNG ui
-- app layout with .Earth"). BeamMP sets its own layout right after its onBeamMPPostJoin hook (MPCoreNetwork
-- runPostJoin), so ours goes in on the next frame.
M.APP_LAYOUT = 'beamngEarthMP'

local waited = 0            -- seconds in our level in a BeamMP session without the generator (reset when connected)
local hidden = false        -- shown once this session (the player answered or the dialog is up)
local settingsRef = nil
local layoutDue = false     -- BeamMP joined: our layout at the next update

-- BeamMP's onBeamMPPostJoin (through core.lua)
function M.onPostJoin() layoutDue = true end

-- The player's own BeamNG.Earth back on when they leave a server (the user, 2026-10-24: "I think the earth mod gets
-- disabled when you leave the server"). Joining, BeamMP switches off every mod the server didn't send, ours too (the
-- server's copy, "multiplayerbeamng_earth", runs in the session); leaving, it deletes the server's copy and leaves ours
-- off, so the BeamNG.Earth level and the servers tab were gone until the game restarted. Called from the server's copy
-- in BeamMP's onBeamMPServerLeave, before BeamMP's cleanup removes it. A player without our app has no such mod:
-- nothing happens (core_modmanager.activateMod logs "mod not existing").
M.LOCAL_MOD = 'beamng_earth'
function M.onServerLeave()
  local mm = rawget(_G, 'core_modmanager')
  if type(mm) ~= 'table' or type(mm.getModDB) ~= 'function' or type(mm.activateMod) ~= 'function' then return false end
  local ok, entry = pcall(mm.getModDB, M.LOCAL_MOD)
  if not ok or type(entry) ~= 'table' or entry.active then return false end
  log('I', 'beamngEarth', 'BeamMP: left the server: BeamNG.Earth (' .. M.LOCAL_MOD .. ') switched back on')
  return pcall(mm.activateMod, M.LOCAL_MOD)
end

-- Is the game in a BeamMP session? (BeamMP's own extension MPCoreNetwork; nil outside BeamMP.)
function M.inSession()
  local mp = rawget(_G, 'MPCoreNetwork')
  if type(mp) ~= 'table' or type(mp.isMPSession) ~= 'function' then return false end
  local ok, v = pcall(mp.isMPSession)
  return ok and v == true
end

-- Pure: show the recommendation now? s = { mp, inLevel, connected, waitedS, hidden, off }
function M.shouldRecommend(s)
  return s.mp == true and s.inLevel == true and not s.connected and not s.hidden and not s.off
    and (s.waitedS or 0) >= M.RECOMMEND_WAIT_S
end

-- The install page: through the BeamMP launcher in a session (the game's openWebBrowser does nothing in the native
-- Linux build), else the game's own call.
function M.openInstallPage()
  local mp = rawget(_G, 'MPCoreNetwork')
  if type(mp) == 'table' and type(mp.openURL) == 'function' then
    pcall(mp.openURL, M.INSTALL_URL)
  elseif openWebBrowser then
    pcall(openWebBrowser, M.INSTALL_URL)
  end
end

local CALL = "local b = package.loaded['beamngEarth/beammp'] if b then b.onChoice('%s') end"

-- Pure: the game's dialog (ui-vue popup.js "showConfirmationDialog") for the recommendation.
function M.dialog()
  return { title = M.TITLE, text = M.TEXT, buttons = {
    { label = "Don't show again", luaCallback = string.format(CALL, 'off'), isCancel = false },
    { label = 'Not now', luaCallback = string.format(CALL, 'later'), isCancel = true },
    { label = 'Get BeamNG.Earth (recommended)', luaCallback = string.format(CALL, 'get'), default = true,
      isConfirm = true },
  } }
end

-- A button of the dialog: 'get' opens the install page and says how to go on, 'off' turns the dialog off for good.
function M.onChoice(choice)
  if choice == 'get' then
    M.openInstallPage()
    guihooks.trigger('showConfirmationDialog', { title = 'BeamNG.Earth', text = M.AFTER_TEXT,
      buttons = { { label = 'OK', luaCallback = '', default = true, isConfirm = true } } })
  elseif choice == 'off' and settingsRef then
    settingsRef.set(M.SETTING, 'true')
  end
end

-- Each frame (core.lua drawUi): count the wait; open the game's dialog once when it is due. settings: the mod's
-- settings module.
function M.update(dt, inLevel, connected, settings)
  settingsRef = settings
  if layoutDue and inLevel then
    layoutDue = false
    local gs = rawget(_G, 'core_gamestate')
    if gs and gs.setGameState then pcall(gs.setGameState, 'multiplayer', M.APP_LAYOUT, 'multiplayer') end
  end
  local mp = M.inSession()
  if not mp or not inLevel or connected then
    waited = 0
    return
  end
  waited = waited + (dt or 0)
  local off = settings and settings.get(M.SETTING) == 'true'
  if not M.shouldRecommend({ mp = mp, inLevel = inLevel, connected = connected, waitedS = waited, hidden = hidden,
      off = off }) then
    return
  end
  hidden = true
  M.show()
end

function M.show() guihooks.trigger('showConfirmationDialog', M.dialog()) end

-- (tests) the session's state back to the start
function M.reset() waited, hidden, layoutDue = 0, false, false end

return M
