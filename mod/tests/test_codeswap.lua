-- luajit mod/tests/test_codeswap.lua  (codeswap.lua): another copy of BeamNG.Earth mounted under the running code (a
-- BeamMP server's copy replacing the player's) gets its code loaded: the running copy's extensions unloaded and its
-- modules forgotten, the mounted copy's extensions loaded; nothing while no copy is mounted, or the same one is, or the
-- rollback file is there. (The BeamMP 1-2 fps: a 1.9.1 server's copy under a newer app's code required a module only
-- the app's copy has on every frame, 1.4 s each on the Steam Deck.)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local cs = require('beamngEarth/codeswap')

-- the decision
T.check(cs.due(true, 'a', 'b') == true, 'another copy mounted: swap')
T.check(cs.due(true, 'a', 'a') == false, 'the same copy: no swap')
T.check(cs.due(true, nil, 'b') == true, 'the running copy is older than this module (no fingerprint): swap')
T.check(cs.due(true, 'a', nil) == false, 'no copy mounted (between BeamMP turning one off and the other on): no swap')
T.check(cs.due(false, nil, 'b') == false, 'our code not running (the game starting): nothing to swap')

-- own modules
T.check(cs.ownModule('beamngEarth/parking') and cs.ownModule('beamngEarth.parking'), 'our modules')
T.check(cs.ownModule('core/cameraModes/earthGlobe'), 'the globe camera mode')
T.check(not cs.ownModule('core/vehicles') and not cs.ownModule('beamngEarthish') and not cs.ownModule(nil),
  'not the game\'s or anyone else\'s')

-- a fake game: the mounted files (path -> size), the loaded extensions, package.loaded
local mounted, offFile = {}, false
_G.FS = {
  findFiles = function(_, d, pat, depth, rec, dirs)
    local out = {}
    for p in pairs(mounted) do if p:sub(1, #d) == d and p:match('%.lua$') then out[#out + 1] = p end end
    return out
  end,
  fileSize = function(_, p) return mounted[p] or -1 end,
  fileExists = function(_, p) return p == cs.OFF_FILE and offFile end,
}
local loaded, unloadedLog, loadedLog = {}, {}, {}
_G.extensions = {
  getLoadedExtensionsNames = function()
    local out = {}
    for n in pairs(loaded) do out[#out + 1] = n end
    table.sort(out)
    return out
  end,
  unload = function(n) loaded[n] = nil; rawset(_G, n, nil); unloadedLog[#unloadedLog + 1] = n end,
  load = function(names)
    for _, n in ipairs(type(names) == 'table' and names or { names }) do
      loaded[n] = true; rawset(_G, n, {}); loadedLog[#loadedLog + 1] = n
    end
  end,
}
local logs = {}
_G.log = function(level, tag, msg) logs[#logs + 1] = msg end
local function start(files)
  mounted = files
  loaded = { beamngEarth_core = true, beamngEarth_sky = true, core_vehicles = true }
  for n in pairs(loaded) do rawset(_G, n, {}) end
  unloadedLog, loadedLog, logs = {}, {}, {}
end

local copyA = { [cs.DIR .. 'core.lua'] = 1000, [cs.DIR .. 'parking.lua'] = 300, [cs.DIR .. 'busparked.lua'] = 50 }
local copyB = { [cs.DIR .. 'core.lua'] = 900, [cs.DIR .. 'parking.lua'] = 280 }
start(copyA)
cs.remember()
local fpA = cs.remembered()
T.check(type(fpA) == 'string' and fpA:find('busparked.lua:50', 1, true) ~= nil, 'fingerprint: the files and sizes')
mounted = { [cs.DIR .. 'busparked.lua'] = 50, [cs.DIR .. 'parking.lua'] = 300, [cs.DIR .. 'core.lua'] = 1000 }
T.check(cs.fingerprint() == fpA, 'fingerprint: independent of the listing order')
mounted[cs.DIR .. 'release.lua'] = 20
T.check(cs.fingerprint() == fpA, 'fingerprint: a packaged copy\'s release.lua is not code')
mounted[cs.DIR .. 'release.lua'] = nil

-- the same copy mounted again: nothing
T.check(cs.check('same', true) == false and #unloadedLog == 0, 'the same copy: nothing unloaded')
-- no copy mounted: nothing (the running code still turns the player's copy back on, beammp.lua)
mounted = {}
T.check(cs.check('none', true) == false and #unloadedLog == 0, 'no copy mounted: nothing unloaded')

-- another copy: ours unloaded (not the game's), our modules forgotten, the mounted copy's extensions loaded
package.loaded['beamngEarth/parking'] = { old = true }
package.loaded['beamngEarth/busparked'] = { old = true }
package.loaded['core/somethingElse'] = { keep = true }
mounted = copyB
T.check(cs.check('turned on: multiplayerbeamng_earth', true) == true, 'another copy: swapped')
table.sort(unloadedLog)
T.check(#unloadedLog == 2 and unloadedLog[1] == 'beamngEarth_core' and unloadedLog[2] == 'beamngEarth_sky',
  'our two extensions unloaded, the game\'s kept')
T.check(package.loaded['beamngEarth/parking'] == nil and package.loaded['beamngEarth/busparked'] == nil,
  'our modules forgotten (the new copy\'s files are required again)')
T.check(package.loaded['core/somethingElse'] ~= nil, 'other modules kept')
T.check(#loadedLog == 2 and loaded.beamngEarth_core and loaded.beamngEarth_sky, 'the mounted copy\'s extensions loaded')
T.check(cs.remembered() == nil, 'the fingerprint left to the new code (it remembers its own at load)')
T.check(#logs == 1 and logs[1]:find('another copy', 1, true) ~= nil, 'one log line')

-- a modScript's check (reload false): unloaded only, the mod manager loads after the modScripts
start(copyA)
rawset(_G, cs.GLOBAL, nil)            -- (the running copy is older than this module: no fingerprint)
T.check(cs.check('its modScript', false) == true and #unloadedLog == 2 and #loadedLog == 0,
  'modScript: an older running copy unloaded, nothing loaded here')

-- the rollback file
start(copyA)
cs.remember()
mounted = copyB
offFile = true
T.check(cs.check('off', true) == false and #unloadedLog == 0, 'settings/beamngEarth_nocodeswap: off')
offFile = false

-- our core not loaded (the game starting: the first modScripts run before any extension): nothing
start(copyB)
loaded.beamngEarth_core = nil
rawset(_G, 'beamngEarth_core', nil)
rawset(_G, cs.GLOBAL, 'x')
T.check(cs.check('startup', false) == false, 'startup: nothing')

-- no FS (a stub game): no fingerprint, never a swap
_G.FS = nil
T.check(cs.fingerprint() == nil and cs.check('nofs', true) == false, 'no FS: nothing')

T.done('test_codeswap')
