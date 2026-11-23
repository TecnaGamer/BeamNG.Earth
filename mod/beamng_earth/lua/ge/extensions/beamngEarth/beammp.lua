-- BeamMP (multiplayer): whether the game is in a BeamMP session, and the recommendation to install BeamNG.Earth's world
-- generator for a player who joined a BeamNG.Earth server without it (the user, 2026-10-22: "Add a popup with players
-- without the mod that highly recommends them install the .earth server"). Players with the generator build the world
-- on their own PC; without it the world has to come from the server or other players, slower (the hybrid plan,
-- .claude/work/beammp_plan.md). BeamMP's rules ask for no spam: one window per session, and i18n.t('multiplayer.noRepeat', nil, 'Don\'t show again').
local i18n = require('beamngEarth/i18n')
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
-- disabled when you leave the server"; again 2026-11-15 on 1.10.1: "when i left the server it disabled the mod").
-- Joining, BeamMP's MPModManager.checkAllMods switches off every mod the server didn't send, ours too (the server's
-- copy, "multiplayerbeamng_earth", runs in the session, codeswap.lua); leaving, cleanUpSessionMods deletes the
-- server's copy (core_modmanager.deleteMod: the files go, the Lua already loaded stays) and leaves ours off, so the
-- BeamNG.Earth level and the servers tab were gone until the game restarted (the launcher turns it back on at its next
-- start: install.ensure_mod_active).
-- 1.10.0 switched it on from core's onUpdate once the session was over, but behind onUpdate's early return for the
-- menus: leaving through the level's end (Esc > Main menu: onClientEndMission -> MPCoreNetwork.leaveServer(false)) the
-- level state was already reset, so that never ran (the user's log 2026-11-15: no line of ours after "1 Mods cleaned
-- up"). And a server's copy is whatever version its host put there, so its code can't be the one that does it.
-- Now the player's own copy's code arms it when BeamMP switches that copy off in a session (onModDeactivated, before
-- codeswap.lua swaps its code out; only then: a copy already off at the join was the player's choice): a job in the
-- game's own job system (core_jobsystem), not one of our extensions or modules, so neither the code swap nor the
-- server copy's deletion takes it away. Each frame it asks BeamMP whether the session is over (one coroutine resume,
-- only while BeamMP has our copy off); then it switches our copy on at once (before BeamMP's Lua reload 0.5 s after a
-- "Leave" from its menu), again (up to RESTORE_TRIES times) only if BeamMP turns it off again in a session within
-- RESTORE_GAP_S; the player turning it off ends it. Our copy's
-- modScript then brings its own code back (codeswap.lua). In the session it waits: BeamMP's checkMod would switch it
-- straight off again ("This mod should not be running: beamng_earth"). A player without our app has no such mod:
-- nothing is armed. A mod the player turns off outside a session (before joining, too) is never switched back on.
M.LOCAL_MOD = 'beamng_earth'
M.RESTORE_TRIES = 3             -- switched on again this many times when something turned it off meanwhile
M.RESTORE_GAP_S = 1.0           -- on and still on this long: done
-- the pending restore ({ live, mod, tries, ... }): a global, so a copy loaded later (after a swap, from the server's
-- copy) sees a job already running and doesn't start a second one
M.RESTORE_GLOBAL = 'beamngEarthMPRestore'

local function modManager()
  local mm = rawget(_G, 'core_modmanager')
  if type(mm) ~= 'table' or type(mm.getModDB) ~= 'function' or type(mm.activateMod) ~= 'function' then return nil end
  return mm
end

local function localEntry()
  local mm = modManager()
  if not mm then return nil end
  local ok, entry = pcall(mm.getModDB, M.LOCAL_MOD)
  if not ok or type(entry) ~= 'table' then return nil end
  return entry, mm
end

-- BeamMP in a session or on its way into one (MPCoreNetwork; no BeamMP: never)
local function sessionOn()
  local mp = rawget(_G, 'MPCoreNetwork')
  if type(mp) ~= 'table' then return false end
  for _, f in ipairs({ mp.isMPSession, mp.isGoingMPSession }) do
    if type(f) == 'function' then
      local ok, v = pcall(f)
      if ok and v == true then return true end
    end
  end
  return false
end

local function clockS()
  local ht = rawget(_G, 'hptimer')
  if type(ht) == 'function' then
    local ok, t = pcall(ht)
    if ok and t then
      return function() return t:stop() / 1000 end
    end
  end
  local c0 = os.clock()
  return function() return os.clock() - c0 end
end

-- One frame of a pending restore. -> true when done (switched on and still on, on already, gave up, nothing to do).
function M.restoreStep(s)
  if sessionOn() then return false end
  local entry, mm = localEntry()
  if not entry then return true end                 -- no mod manager, or no local copy any more
  if entry.active then
    if s.tries == 0 then return true end            -- (switched on by someone else: nothing for us)
    return (s.clock() - s.since) >= M.RESTORE_GAP_S
  end
  if s.tries > 0 and not s.again then return true end   -- (off again, not by BeamMP in a session: the player's choice)
  if s.tries >= M.RESTORE_TRIES then
    log('W', 'beamngEarth', 'BeamMP: left the server: BeamNG.Earth (' .. s.mod .. ') was turned off again ' .. s.tries
      .. ' times: left off (turn it on in the Mods menu)')
    return true
  end
  s.tries = s.tries + 1
  s.since = s.clock()
  s.again = false
  log('I', 'beamngEarth', 'BeamMP: left the server: BeamNG.Earth (' .. s.mod .. ') switched back on')
  local ok, err = pcall(mm.activateMod, s.mod)
  if not ok then log('W', 'beamngEarth', 'BeamMP: switching BeamNG.Earth back on: ' .. tostring(err)) end
  return false
end

-- The job: a step a frame until done (an error in a step ends it). (The job system first runs it at its next update.)
local function restoreJob(job, s)
  while s.live do
    local ok, done = pcall(M.restoreStep, s)
    if not ok or done then break end
    job.sleep(0)
  end
  s.live = false
end
M._restoreJob = restoreJob

-- Arm the restore (no-op when one is pending). -> true when armed now.
function M.armRestore(why)
  local cur = rawget(_G, M.RESTORE_GLOBAL)
  if type(cur) == 'table' and cur.live then return false end
  local js = rawget(_G, 'core_jobsystem')
  if type(js) ~= 'table' or type(js.create) ~= 'function' then return false end
  local s = { live = true, mod = M.LOCAL_MOD, tries = 0, since = 0, clock = clockS() }
  local ok = pcall(js.create, restoreJob, 1, s)
  if not ok then return false end
  rawset(_G, M.RESTORE_GLOBAL, s)
  log('I', 'beamngEarth', 'BeamMP: BeamNG.Earth (' .. M.LOCAL_MOD .. ') is off for the session (' .. tostring(why)
    .. '): switched back on when it ends')
  return true
end

-- core.lua's onModDeactivated, before the code swap: BeamMP switching the player's own copy off in a session (a pending
-- restore: switched on again after this session too). Outside a session it is the player: a pending restore ends.
function M.onModDeactivated(mod)
  if type(mod) ~= 'table' or mod.modname ~= M.LOCAL_MOD then return false end
  local cur = rawget(_G, M.RESTORE_GLOBAL)
  local live = type(cur) == 'table' and cur.live
  if not sessionOn() then
    if live then cur.live = false end
    return false
  end
  if live then cur.again = true end
  return M.armRestore('turned off by BeamMP')
end

-- BeamMP's onBeamMPServerLeave (through core.lua): the layout rewrite off. It arms nothing: "our copy is off" alone
-- doesn't say BeamMP turned it off (the player may have, before joining: Codex review 2026-11-15), and only the
-- onModDeactivated above sees that; a player whose own copy is older than this is turned back on at the launcher's next
-- start (install.ensure_mod_active).
function M.onServerLeave()
  layoutDue = false
  disarm()
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
  return { title = i18n.t('multiplayer.recommendTitle', nil, M.TITLE), text = i18n.t('multiplayer.recommendText', nil, M.TEXT), buttons = {
    { label = i18n.t('multiplayer.noRepeat', nil, 'Don\'t show again'), luaCallback = string.format(CALL, 'off'), isCancel = false },
    { label = i18n.t('multiplayer.later', nil, 'Not now'), luaCallback = string.format(CALL, 'later'), isCancel = true },
    { label = i18n.t('multiplayer.get', nil, 'Get BeamNG.Earth (recommended)'), luaCallback = string.format(CALL, 'get'), default = true,
      isConfirm = true },
  } }
end

-- A button of the dialog: 'get' opens the install page and says how to go on, 'off' turns the dialog off for good.
function M.onChoice(choice)
  if choice == 'get' then
    M.openInstallPage()
    guihooks.trigger('showConfirmationDialog', { title = 'BeamNG.Earth', text = i18n.t('multiplayer.installInstructions', nil, M.AFTER_TEXT),
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
  waited, hidden, layoutDue = 0, false, false
  local r = rawget(_G, M.RESTORE_GLOBAL)
  if type(r) == 'table' then r.live = false end      -- (a pending restore's job ends at its next frame)
  rawset(_G, M.RESTORE_GLOBAL, nil)
end

return M
