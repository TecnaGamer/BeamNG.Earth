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

-- the app layout: BeamMP's join hook, then the next update in our level sets the combined layout once
local states = {}
_G.core_gamestate = { setGameState = function(a, b, c) states[#states + 1] = a .. ' ' .. b .. ' ' .. c end }
beammp.reset()
beammp.onPostJoin()
beammp.update(0, false, true, settings)
T.check(#states == 0, 'not outside our level')
beammp.update(0, true, true, settings)
T.check(#states == 1 and states[1] == 'multiplayer ' .. beammp.APP_LAYOUT .. ' multiplayer', 'combined layout: ' .. tostring(states[1]))
beammp.update(0, true, true, settings)
T.check(#states == 1, 'set once')
_G.core_gamestate = nil

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

-- leaving a server: the player's own BeamNG.Earth (switched off by BeamMP at the join) back on
do
  local activated, db = {}, { beamng_earth = { active = false } }
  core_modmanager = { getModDB = function(n) return db[n] end, activateMod = function(n) activated[#activated + 1] = n end }
  T.check(beammp.onServerLeave() and activated[1] == 'beamng_earth', 'left: our own mod switched back on')
  db.beamng_earth.active = true
  T.check(not beammp.onServerLeave() and #activated == 1, 'already on: left alone')
  db.beamng_earth = nil
  T.check(not beammp.onServerLeave() and #activated == 1, 'no local copy (no app): nothing')
  core_modmanager = nil
  T.check(not beammp.onServerLeave(), 'no mod manager: nothing')
end

T.done('test_beammp')
