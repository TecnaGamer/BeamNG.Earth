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
-- app layout with .Earth"). BeamMP sets its own layout ("beammp") right after its onBeamMPPostJoin hook
-- (MPCoreNetwork runPostJoin). 1.10.0 set ours on the next frame: two HUD builds in a row tore down and rebuilt BeamMP's
-- session app, whose stale listener then threw every second, and the UI's crash recovery rebuilt the HUD over and over
-- (#1302). Now the hook arms a one-shot rewrite of BeamMP's own request, so the HUD is built once, with ours; only in our
-- level, in a session, with our layout file mounted and not already up. Nothing is left pending past the next frame.
M.APP_LAYOUT = 'beamngEarthMP'
M.BEAMMP_LAYOUT = 'beammp'
M.LAYOUT_FILE = '/settings/ui_apps/originalLayouts/default/beamngEarthMP.uilayout.json'
M.LEVEL_ID = 'beamng_earth'

local waited = 0            -- seconds in our level in a BeamMP session without the generator (reset when connected)
local hidden = false        -- shown once this session (the player answered or the dialog is up)
local settingsRef = nil
local layoutDue = false     -- BeamMP joined (our level, all checks passed): the rewrite is disarmed at the next update
local armed = nil           -- { gs, orig, wrapper, live, used }: the one-shot rewrite of BeamMP's layout request

local function gameState()
  local gs = rawget(_G, 'core_gamestate')
  if type(gs) ~= 'table' or type(gs.setGameState) ~= 'function' then return nil end
  return gs
end

-- Our layout may go up now: our level, a BeamMP session, our layout file mounted (BeamMP unmounts a copy the server
-- did not send; its Lua stays loaded).
function M.layoutAllowed()
  local lvl = rawget(_G, 'getCurrentLevelIdentifier')
  if type(lvl) ~= 'function' then return false end
  local okL, id = pcall(lvl)
  if not okL or id ~= M.LEVEL_ID or not M.inSession() then return false end
  local fs = rawget(_G, 'FS')
  if type(fs) ~= 'table' or type(fs.fileExists) ~= 'function' then return false end
  local okF, exists = pcall(fs.fileExists, fs, M.LAYOUT_FILE)
  return okF and exists == true
end

-- The rewrite off: the game's function back unless another mod wrapped it meanwhile (then ours stays under theirs,
-- passing everything through). -> true when it rewrote a request.
local function disarm()
  local a = armed
  if not a then return false end
  armed = nil
  a.live = false
  if a.gs.setGameState == a.wrapper then a.gs.setGameState = a.orig end
  return a.used
end

local function arm(gs)
  local a = { gs = gs, orig = gs.setGameState, live = true, used = false }
  a.wrapper = function(state, appLayout, menuItems, options)
    if a.live and appLayout == M.BEAMMP_LAYOUT and M.layoutAllowed() then
      a.live, a.used = false, true
      if a.gs.setGameState == a.wrapper then a.gs.setGameState = a.orig end
      local cur = type(a.gs.state) == 'table' and a.gs.state or {}
      if cur.appLayout == M.APP_LAYOUT and (state == nil or state == cur.state)
          and (menuItems == nil or menuItems == cur.menuItems) and options == nil then
        log('I', 'beamngEarth', 'BeamMP joined: the .Earth multiplayer layout is already up (no HUD rebuild)')
        return
      end
      log('I', 'beamngEarth', 'BeamMP joined: the .Earth multiplayer layout in place of "' .. appLayout .. '" (one HUD build)')
      appLayout = M.APP_LAYOUT
    end
    return a.orig(state, appLayout, menuItems, options)
  end
  gs.setGameState = a.wrapper
  armed = a
end

-- BeamMP's onBeamMPPostJoin (through core.lua): called inside runPostJoin just before BeamMP's own setGameState.
function M.onPostJoin()
  disarm()
  layoutDue = false
  local gs = gameState()
  if not gs or not M.layoutAllowed() then return end   -- another map, no session, no layout file: never the HUD
  arm(gs)
  layoutDue = true
end

-- The player's own BeamNG.Earth back on when they leave a server (the user, 2026-10-24: "I think the earth mod gets
-- disabled when you leave the server"). Joining, BeamMP switches off every mod the server didn't send, ours too (the
-- server's copy, "multiplayerbeamng_earth", runs in the session); leaving, it deletes the server's copy and leaves ours
-- off, so the BeamNG.Earth level and the servers tab were gone until the game restarted. Called from the server's copy
-- in BeamMP's onBeamMPServerLeave, before BeamMP's cleanup removes it. A player without our app has no such mod:
-- nothing happens (core_modmanager.activateMod logs "mod not existing").
-- Switched on once BeamMP's session is over (M.restoreTick): in the leave hook BeamMP still counts the session, and its
-- MPModManager.checkMod turned our copy straight off again ("This mod should not be running: beamng_earth", Steam Deck
-- and a player's bug report, 2026-11-10/11), so the level stayed gone until the game restarted; with the code swap
-- (codeswap.lua) the player's copy's code had come in for that moment and then had no files at all.
M.LOCAL_MOD = 'beamng_earth'
M.RESTORE_TRIES = 3             -- switched on again this many times when something turned it off meanwhile
M.RESTORE_GAP_S = 1.0
local restoreDue, restoreTries, restoreWait = false, 0, 0

local function localEntry()
  local mm = rawget(_G, 'core_modmanager')
  if type(mm) ~= 'table' or type(mm.getModDB) ~= 'function' or type(mm.activateMod) ~= 'function' then return nil end
  local ok, entry = pcall(mm.getModDB, M.LOCAL_MOD)
  if not ok or type(entry) ~= 'table' then return nil end
  return entry, mm
end

function M.onServerLeave()
  layoutDue = false
  disarm()
  local entry = localEntry()
  if not entry or entry.active then return false end
  restoreDue, restoreTries, restoreWait = true, 0, 0
  return M.restoreTick(0)
end

-- Each frame (core.lua onUpdate, also outside a level): our copy switched on once BeamMP's session is over. -> true when
-- it was switched on now.
function M.restoreTick(dt)
  if not restoreDue then return false end
  restoreWait = restoreWait - (dt or 0)
  if restoreWait > 0 or M.inSession() then return false end
  local entry, mm = localEntry()
  if not entry or entry.active or restoreTries >= M.RESTORE_TRIES then
    restoreDue = false          -- (on and still on RESTORE_GAP_S later: done)
    return false
  end
  restoreTries = restoreTries + 1
  restoreWait = M.RESTORE_GAP_S
  log('I', 'beamngEarth', 'BeamMP: left the server: BeamNG.Earth (' .. M.LOCAL_MOD .. ') switched back on')
  return (pcall(mm.activateMod, M.LOCAL_MOD))
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
  if layoutDue then
    -- the frame after the join: the rewrite off. Only if BeamMP sent no layout of its own (another BeamMP version),
    -- ours now, once; never in place of BeamMP's once it is up (that swap is #1302's rebuild loop) or ours again.
    layoutDue = false
    local rewrote = disarm()
    local gs = gameState()
    local cur = gs and type(gs.state) == 'table' and gs.state.appLayout or nil
    if not rewrote and inLevel and gs and cur ~= M.APP_LAYOUT and cur ~= M.BEAMMP_LAYOUT and M.layoutAllowed() then
      pcall(gs.setGameState, 'multiplayer', M.APP_LAYOUT, 'multiplayer')
    end
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
function M.reset()
  disarm()
  waited, hidden, layoutDue, restoreDue, restoreTries, restoreWait = 0, false, false, false, 0, 0
end

return M
