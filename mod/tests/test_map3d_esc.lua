-- luajit mod/tests/test_map3d_esc.lua  (Esc on the 3D map: the game's back takes our screen to 'play' and fires
-- onMenuToggled(false); the map closes, not shown again; the user, 2026-11-08: "pressing esc dosnt exit map mode")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')
package.path = (arg[0]:match('^(.*)[/\\]') or '.') .. '/../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local m3 = require('beamngEarth/map3d')

local function setUp(name, value)
  for i = 1, 60 do
    local n = debug.getupvalue(m3.onMenuToggled, i)
    if not n then break end
    if n == name then debug.setupvalue(m3.onMenuToggled, i, value) return true end
  end
  return false
end
local exits = 0
local realExit = m3.exit
m3.exit = function() exits = exits + 1 end
T.check(setUp('active', true) and setUp('transitioning', false), 'the map open (test setup)')

m3.onUiChangedState(m3.ROUTE)
m3.onMenuToggled(false)
T.check(exits == 1, 'Esc from the map\'s screen closes the map')
m3.onUiChangedState('play')          -- (the back's switch reported before the hook)
m3.onMenuToggled(false)
T.check(exits == 2, 'Esc: the switch to play reported first, still closed')
exits = 0

m3.onUiChangedState('menu.mainmenu')       -- the pause menu over the map
m3.onMenuToggled(false)
T.check(exits == 0, 'Esc from the pause menu over the map: back to the map, not closed')

m3.onUiChangedState(m3.ROUTE)
m3.onMenuToggled(true)
T.check(exits == 0, 'the menu opening is no Esc back')

setUp('active', false)
m3.onMenuToggled(false)
T.check(exits == 0, 'the map closed already: nothing')

m3.exit = realExit
T.done('test_map3d_esc')
