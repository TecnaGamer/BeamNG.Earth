-- luajit mod/tests/test_beammp.lua  (BeamMP support, beammp.lua): session detection through BeamMP's MPCoreNetwork,
-- when the "install BeamNG.Earth" window is due (BeamMP session, our level, no world generator for RECOMMEND_WAIT_S,
-- not hidden this session, not turned off), the wait reset by a connection, and the install page opened through the
-- BeamMP launcher when it can.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local beammp = require('beamngEarth/beammp')

-- session detection
_G.MPCoreNetwork = nil
T.check(beammp.inSession() == false, 'no BeamMP: no session')
_G.MPCoreNetwork = { isMPSession = function() return true end }
T.check(beammp.inSession() == true, 'BeamMP session')
_G.MPCoreNetwork = { isMPSession = function() return false end }
T.check(beammp.inSession() == false, 'BeamMP loaded, single player: no session')
_G.MPCoreNetwork = { isMPSession = function() error('boom') end }
T.check(beammp.inSession() == false, 'an error in BeamMP: no session')

-- the decision
local function s(t)
  local v = { mp = true, inLevel = true, connected = false, waitedS = beammp.RECOMMEND_WAIT_S, hidden = false, off = false }
  for k, x in pairs(t or {}) do v[k] = x end
  return v
end
T.check(beammp.shouldRecommend(s()) == true, 'BeamMP, our level, no generator for the wait: recommend')
T.check(beammp.shouldRecommend(s({ mp = false })) == false, 'single player: never')
T.check(beammp.shouldRecommend(s({ inLevel = false })) == false, 'another level: never')
T.check(beammp.shouldRecommend(s({ connected = true })) == false, 'the generator is running: never')
T.check(beammp.shouldRecommend(s({ waitedS = beammp.RECOMMEND_WAIT_S - 1 })) == false, 'still starting up: not yet')
T.check(beammp.shouldRecommend(s({ hidden = true })) == false, '"Not now" this session: no more')
T.check(beammp.shouldRecommend(s({ off = true })) == false, '"Don\'t show again": never')

-- update: counts the wait only in a session in our level without the generator; a connection resets it; shown once
_G.MPCoreNetwork = { isMPSession = function() return true end }
local shown = 0
local realShow = beammp.show
beammp.show = function() shown = shown + 1 end
local store = {}
local settings = { get = function(k) return store[k] end, set = function(k, v) store[k] = v end }
beammp.reset()
beammp.update(beammp.RECOMMEND_WAIT_S - 1, true, false, settings)
T.check(shown == 0, 'not shown before the wait')
beammp.update(2, true, false, settings)
T.check(shown == 1, 'shown after the wait')
beammp.update(30, true, false, settings)
T.check(shown == 1, 'shown once a session')
beammp.reset()
beammp.update(beammp.RECOMMEND_WAIT_S - 1, true, false, settings)
beammp.update(1, true, true, settings)
beammp.update(2, true, false, settings)
T.check(shown == 0 + 1, 'a connection resets the wait')
beammp.reset()
store[beammp.SETTING] = 'true'
beammp.update(beammp.RECOMMEND_WAIT_S + 1, true, false, settings)
T.check(shown == 1, 'turned off in the settings: not shown')
store[beammp.SETTING] = nil
beammp.show = realShow

-- the dialog: the game's showConfirmationDialog with three buttons; the recommended one is the default
local d = beammp.dialog()
T.check(d.title == beammp.TITLE and d.text == beammp.TEXT and #d.buttons == 3, 'dialog: title, text, three buttons')
local get = d.buttons[3]
T.check(get.default == true and get.luaCallback:find("onChoice('get')", 1, true), 'the install button is the default')
for _, b in ipairs(d.buttons) do T.check(loadstring(b.luaCallback) ~= nil, 'valid Lua callback: ' .. b.label) end

-- the buttons: "Don't show again" stores the setting; "Get" opens the page and shows the next step
local triggered = {}
_G.guihooks = { trigger = function(name, opts) triggered[#triggered + 1] = { name, opts } end }
beammp.update(0, true, false, settings)         -- (keeps the settings module for the callbacks)
beammp.onChoice('off')
T.check(store[beammp.SETTING] == 'true', '"Don\'t show again" stored')
local openedUrl
_G.MPCoreNetwork = { isMPSession = function() return true end, openURL = function(u) openedUrl = u end }
beammp.onChoice('get')
T.check(openedUrl == beammp.INSTALL_URL and triggered[#triggered][1] == 'showConfirmationDialog'
  and triggered[#triggered][2].text == beammp.AFTER_TEXT, '"Get" opens the page and shows the next step')

-- the app layout (#1302): BeamMP's runPostJoin calls its onBeamMPPostJoin hook (ours: onPostJoin), then sets its own
-- "beammp" layout. 1.10.0 then set ours on the next frame: two HUD builds in a row, which tore down and rebuilt BeamMP's
-- session app (its stale listener threw every second; the UI's crash recovery rebuilt the HUD over and over). Now
-- BeamMP's one request carries our layout: one HUD build, and nothing more from us.
do
  local LAYOUT_FILE = '/settings/ui_apps/originalLayouts/default/beamngEarthMP.uilayout.json'
  local files = { [LAYOUT_FILE] = true }
  local level = 'beamng_earth'
  local inMp = true
  _G.FS = { fileExists = function(_, p) return files[p] == true end }
  _G.getCurrentLevelIdentifier = function() return level end
  _G.MPCoreNetwork = { isMPSession = function() return inMp end }
  -- the game's core_gamestate.setGameState (lua/ge/extensions/core/gamestate.lua): nil keeps a field; every call
  -- sends the whole state to the UI (GameStateUpdate), which builds the layout it names
  local gs
  local function freshGameState(layout)
    gs = { state = { state = 'freeroam', appLayout = layout, menuItems = 'freeroam' }, sent = {} }
    gs.setGameState = function(st, layout2, menu)
      gs.state = { state = st or gs.state.state, appLayout = layout2 or gs.state.appLayout,
        menuItems = menu or gs.state.menuItems }
      gs.sent[#gs.sent + 1] = gs.state.state .. ' ' .. tostring(gs.state.appLayout) .. ' ' .. gs.state.menuItems
    end
    gs.original = gs.setGameState
    _G.core_gamestate = gs
  end
  -- BeamMP's MPCoreNetwork.runPostJoin: the hook (core.lua onBeamMPPostJoin -> beammp.onPostJoin), then its layout
  local function runPostJoin(withLayout)
    beammp.onPostJoin()
    if withLayout ~= false then core_gamestate.setGameState('multiplayer', 'beammp', 'multiplayer') end
  end
  local function frames(n, inLevel)
    for _ = 1, n do beammp.update(0.016, inLevel ~= false, true, settings) end
  end
  local OURS = 'multiplayer ' .. beammp.APP_LAYOUT .. ' multiplayer'
  local BEAMMP = 'multiplayer beammp multiplayer'

  -- a BeamNG.Earth server: one layout request reaches the game, ours (BeamMP's, rewritten); no second switch later
  beammp.reset()
  freshGameState('beamngEarth')                 -- (core.lua onClientPostStartMission in our level)
  runPostJoin()
  frames(30)
  T.check(#gs.sent == 1 and gs.sent[1] == OURS, 'Earth server: one HUD build, our layout: ' .. table.concat(gs.sent, ', '))
  T.check(core_gamestate.setGameState == gs.original, "the game's setGameState back as it was after the join")
  core_gamestate.setGameState('multiplayer', 'beammp', 'multiplayer')
  T.check(gs.sent[2] == BEAMMP, 'a later request is not touched')

  -- a stock map server (BeamMP switched our copy off; our Lua stays loaded): BeamMP's layout stays, nothing from us,
  -- and no request is left pending for later (1.10.0 kept it and switched the HUD at the next visit of our level, even
  -- in single player)
  beammp.reset()
  level = 'italy'
  freshGameState('freeroam')
  runPostJoin()
  frames(30, false)
  T.check(#gs.sent == 1 and gs.sent[1] == BEAMMP, "stock map: BeamMP's layout only: " .. table.concat(gs.sent, ', '))
  T.check(core_gamestate.setGameState == gs.original, 'stock map: setGameState never wrapped')
  inMp, level = false, 'beamng_earth'           -- (left the server; our level in single player)
  freshGameState('beamngEarth')
  frames(30)
  T.check(#gs.sent == 0, 'single player afterwards: no multiplayer layout: ' .. table.concat(gs.sent, ', '))
  inMp = true

  -- our layout file not mounted (our copy unmounted, a broken install): BeamMP's layout stays
  beammp.reset()
  files[LAYOUT_FILE] = nil
  freshGameState('beamngEarth')
  runPostJoin()
  frames(30)
  T.check(#gs.sent == 1 and gs.sent[1] == BEAMMP, "no layout file: BeamMP's layout: " .. table.concat(gs.sent, ', '))
  files[LAYOUT_FILE] = true

  -- not in a BeamMP session at the hook: nothing
  beammp.reset()
  inMp = false
  freshGameState('beamngEarth')
  runPostJoin()
  frames(30)
  T.check(#gs.sent == 1 and gs.sent[1] == BEAMMP, 'no session: nothing from us')
  inMp = true

  -- rejoining the same map (BeamMP's loadLevel calls runPostJoin directly): our layout already up: nothing re-sent
  beammp.reset()
  freshGameState(beammp.APP_LAYOUT)
  gs.state.state, gs.state.menuItems = 'multiplayer', 'multiplayer'
  runPostJoin()
  frames(30)
  T.check(#gs.sent == 0, 'already our layout: no rebuild: ' .. table.concat(gs.sent, ', '))

  -- a BeamMP without its own layout request in runPostJoin: ours once at the next frame (one build, no swap)
  beammp.reset()
  freshGameState('beamngEarth')
  runPostJoin(false)
  frames(30)
  T.check(#gs.sent == 1 and gs.sent[1] == OURS, 'no BeamMP layout: ours once: ' .. table.concat(gs.sent, ', '))
  -- ... but never in place of BeamMP's layout once it is up (that swap rebuilds BeamMP's session app)
  beammp.reset()
  freshGameState('beammp')
  runPostJoin(false)
  frames(30)
  T.check(#gs.sent == 0, "BeamMP's layout already up: kept: " .. table.concat(gs.sent, ', '))

  -- left the server between the hook and BeamMP's request / the next frame: nothing
  beammp.reset()
  freshGameState('beamngEarth')
  beammp.onPostJoin()
  beammp.onServerLeave()
  core_gamestate.setGameState('multiplayer', 'beammp', 'multiplayer')
  frames(30)
  T.check(#gs.sent == 1 and gs.sent[1] == BEAMMP, 'left: nothing from us: ' .. table.concat(gs.sent, ', '))
  T.check(core_gamestate.setGameState == gs.original, 'left: setGameState back as it was')

  -- another mod wraps setGameState over ours meanwhile: BeamMP's request through theirs, then ours; theirs stays in
  -- place; ours passes later requests through unchanged
  beammp.reset()
  freshGameState('beamngEarth')
  beammp.onPostJoin()
  local under = core_gamestate.setGameState
  local theirs = function(...) return under(...) end
  core_gamestate.setGameState = theirs
  core_gamestate.setGameState('multiplayer', 'beammp', 'multiplayer')
  frames(30)
  T.check(core_gamestate.setGameState == theirs, 'a wrapper on top of ours is left in place')
  core_gamestate.setGameState('multiplayer', 'beammp', 'multiplayer')
  T.check(#gs.sent == 2 and gs.sent[1] == OURS and gs.sent[2] == BEAMMP,
    'under another wrapper: rewritten once, then passes through: ' .. table.concat(gs.sent, ', '))

  _G.core_gamestate, _G.MPCoreNetwork = nil, nil
  _G.FS = { fileExists = function() return true end }
  _G.getCurrentLevelIdentifier = function() return 'beamng_earth' end
end

-- the install page: through the BeamMP launcher in a session, else the game's call
local opened
_G.MPCoreNetwork = { isMPSession = function() return true end, openURL = function(u) opened = 'mp ' .. u end }
beammp.openInstallPage()
T.check(opened == 'mp ' .. beammp.INSTALL_URL, 'opened through BeamMP: ' .. tostring(opened))
_G.MPCoreNetwork = nil
_G.openWebBrowser = function(u) opened = 'game ' .. u end
beammp.openInstallPage()
T.check(opened == 'game ' .. beammp.INSTALL_URL, 'opened by the game: ' .. tostring(opened))
_G.openWebBrowser = nil

-- leaving a server: the player's own BeamNG.Earth (switched off by BeamMP at the join) back on once the session is
-- over, by a job in the game's job system (it outlives our modules: the code swap, the server copy's deletion)
do
  local realLog = _G.log
  local logs = {}
  _G.log = function(_, _, msg) logs[#logs + 1] = msg end
  local now = 0                       -- (the job's clock: hptimer, ms)
  _G.hptimer = function()
    local t0 = now
    return { stop = function() return (now - t0) * 1000 end }
  end
  local jobs = {}
  _G.core_jobsystem = { create = function(fct, maxdt, ...)
    local job = { args = { ... } }
    job.sleep = function() coroutine.yield() end      -- (sleep(0): one frame)
    job.t = coroutine.create(fct)
    jobs[#jobs + 1] = job
    return job
  end }
  local function frames(n, dt)
    for _ = 1, n do
      now = now + (dt or 1 / 60)
      for _, j in ipairs(jobs) do
        if coroutine.status(j.t) ~= 'dead' then
          local ok, err = coroutine.resume(j.t, j, unpack(j.args))
          if not ok then error(err) end
        end
      end
    end
  end
  local function liveJobs()
    local n = 0
    for _, j in ipairs(jobs) do if coroutine.status(j.t) ~= 'dead' then n = n + 1 end end
    return n
  end
  local activated, db = {}, {}
  _G.core_modmanager = { getModDB = function(n) return db[n] end,
    activateMod = function(n) activated[#activated + 1] = n; db[n].active = true end }
  local inMp = true
  _G.MPCoreNetwork = { isMPSession = function() return inMp end, isGoingMPSession = function() return false end }
  local ours = { modname = 'beamng_earth' }

  -- BeamMP's checkMod switches ours off in the session: armed (once); nothing while the session lasts
  beammp.reset()
  db.beamng_earth = { active = false }
  T.check(beammp.onModDeactivated(ours) == true and liveJobs() == 1, 'switched off in a session: armed')
  T.check(beammp.onModDeactivated(ours) == false and liveJobs() == 1, 'already pending: no second job')
  frames(600)
  T.check(#activated == 0, 'in the session: left off (BeamMP would switch it straight off again)')
  -- the session over (MPCoreNetwork.leaveServer: the hook, then isMpSession false): on at the next frame
  inMp = false
  frames(1)
  T.check(#activated == 1 and activated[1] == 'beamng_earth', 'session over: on at the next frame')
  frames(math.ceil(beammp.RESTORE_GAP_S * 60) + 2)
  T.check(liveJobs() == 0 and not rawget(_G, beammp.RESTORE_GLOBAL).live and #activated == 1,
    'still on after the gap: the job ends')
  frames(100)
  T.check(#activated == 1, 'nothing more after it ended')

  -- turned off again soon after by BeamMP in a session (a quick join elsewhere): on again after it, at most
  -- RESTORE_TRIES times
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  inMp = false
  for _ = 1, 20 do
    frames(5)
    if liveJobs() == 0 then break end
    inMp = true; db.beamng_earth.active = false; beammp.onModDeactivated(ours); inMp = false
  end
  frames(5)
  T.check(#activated == beammp.RESTORE_TRIES and liveJobs() == 0, 'at most RESTORE_TRIES times a job: ' .. #activated)
  -- turned off again by something else (no BeamMP session): not switched on again, the job ends
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  inMp = false
  frames(3)
  db.beamng_earth.active = false
  frames(5)
  T.check(#activated == 1 and liveJobs() == 0, 'off again outside a session: left off')
  -- turned off by the player (the hook outside a session) while the job waits: it ends
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  inMp = false
  T.check(beammp.onModDeactivated(ours) == false, 'player turn-off: nothing armed')
  frames(5)
  T.check(#activated == 0 and liveJobs() == 0, 'player turn-off: the pending restore ends')

  -- not ours, or not in a session (the player turning it off in the Mods menu): nothing
  beammp.reset(); jobs, activated = {}, {}
  inMp = true
  T.check(beammp.onModDeactivated({ modname = 'someothermod' }) == false and liveJobs() == 0, 'another mod: nothing')
  inMp = false
  T.check(beammp.onModDeactivated(ours) == false and liveJobs() == 0, 'outside a session: never switched back on')
  T.check(beammp.onModDeactivated(nil) == false, 'no mod table: nothing')

  -- the leave hook and a load arm nothing: ours off there may be the player's own choice before joining (Codex review)
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onServerLeave()
  T.check(liveJobs() == 0 and rawget(_G, beammp.RESTORE_GLOBAL) == nil, 'leave hook, ours off before the join: nothing')
  inMp = false
  beammp.onServerLeave()
  frames(120)
  T.check(liveJobs() == 0 and #activated == 0, 'leave hook outside a session: nothing; stays off')
  T.check(beammp.onLoad == nil, 'no arming at load')
  inMp = true

  -- switched on by someone else first (an older copy's own restore): the job just ends
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  db.beamng_earth.active = true
  inMp = false
  frames(2)
  T.check(#activated == 0 and liveJobs() == 0, 'on already: nothing, the job ends')

  -- the code swap forgets our modules: the job keeps going; the new code's module sees it pending
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  package.loaded['beamngEarth/beammp'] = nil
  local fresh = require('beamngEarth/beammp')
  T.check(fresh ~= beammp and fresh.onModDeactivated(ours) == false and liveJobs() == 1,
    'new code: the pending job seen')
  inMp = false
  frames(1)
  T.check(#activated == 1, 'the old code\'s job switched it on after its module was forgotten')
  fresh.reset()

  -- an error in the mod manager: the job ends quietly; no job system or mod manager: nothing armed, no error
  beammp.reset(); jobs, activated, inMp = {}, {}, true
  db.beamng_earth = { active = false }
  beammp.onModDeactivated(ours)
  local armedState = rawget(_G, beammp.RESTORE_GLOBAL)
  _G.core_modmanager.getModDB = function() error('boom') end
  inMp = false
  frames(2)
  T.check(liveJobs() == 0 and not armedState.live, 'an error: the job ends')
  beammp.reset()
  local js = _G.core_jobsystem
  _G.core_jobsystem = nil
  inMp = true
  db.beamng_earth = { active = false }
  _G.core_modmanager = { getModDB = function(n) return db[n] end, activateMod = function() end }
  T.check(beammp.onModDeactivated(ours) == false, 'no job system: not armed')
  _G.core_jobsystem = js
  _G.core_modmanager = nil
  T.check(beammp.onModDeactivated(ours) == true, 'no mod manager: armed (the job ends at its first step)')
  beammp.reset()
  _G.MPCoreNetwork, _G.core_jobsystem, _G.hptimer = nil, nil, nil
  _G.log = realLog
end

T.done('test_beammp')
