-- luajit mod/tests/test_api_shapes.lua  (the modding API's shapes, 1.9: api.registerShapes / unregisterShapes over
-- trees.register / unregister; docs/ADDONS.md "Shapes")
-- A registration fills a group's catalog (a new group, or one the catalog has: replaced), its draw distance and
-- collision; a bad one changes nothing; two owners on one group: the latest wins and unregistering puts back the one
-- under it, then the catalog's own; a fresh vegetation module (a codeswap reload) gets the kept registrations and the
-- shapesCatalog event; M.pick draws the registered shapes.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

local hooks = {}
extensions = { hook = function(name, info) hooks[#hooks + 1] = { name, info } end }
local api = require('beamngEarth/api')
local trees = require('beamngEarth/trees')

T.check(api.has('shapes') and select(2, api.version()) >= 9, 'shapes: has() and minor version 9')

-- the first update sees the vegetation module: the event, for mods to register
api.onUpdate(0.016)
local function count(name)
  local n = 0
  for _, h in ipairs(hooks) do if h[1] == name then n = n + 1 end end
  return n
end
T.check(count('onBeamNGEarthShapesCatalog') == 1, 'the event when the vegetation module is first seen')
api.onUpdate(0.016)
T.check(count('onBeamNGEarthShapesCatalog') == 1, 'once: not every frame')

-- a group the catalog lacks (a newer server's species group)
T.check(trees.CATALOG.au_brushbox == nil, 'no such group before')
local ok = api.registerShapes('roadtrip_flora', {
  au_brushbox = { shapes = { { '/mods/x/brushbox_a', 12.0 }, { '/mods/x/brushbox_b', 18.0 } }, maxM = 900 },
  au_hedge = { shapes = { { '/mods/x/lillypilly', 2.0 } }, maxM = 250, noCollide = true },
})
T.check(ok == true, 'registered')
T.check(#trees.CATALOG.au_brushbox == 2 and trees.GROUP_MAX_M.au_brushbox == 900, 'the shapes and the draw distance')
T.check(trees.NO_COLLIDE.au_hedge == true and not trees.NO_COLLIDE.au_brushbox, 'hedges never collide, trees may')
local shape, sc = trees.pick('au_brushbox', 17.0, 0.3)
T.check(shape == '/mods/x/brushbox_b' and math.abs(sc - 17 / 18) < 1e-9, 'pick: the registered shape nearest its height')

-- a bad registration changes nothing
local bad, why = api.registerShapes('broken', { au_brushbox = { shapes = { { '', 3 } } } })
T.check(bad == false and type(why) == 'string', 'a malformed shape: refused, with why')
T.check(trees.CATALOG.au_brushbox[1][1] == '/mods/x/brushbox_a', 'and the catalog untouched')
T.check(api.registerShapes('broken', { au_brushbox = { shapes = { { '/a', 3 } }, maxM = -1 } }) == false, 'maxM > 0')
T.check(api.registerShapes('', { g = { shapes = { { '/a', 3 } } } }) == false, 'an owner is needed')

-- a group the catalog has: replaced, then put back
local before = trees.CATALOG.shrub
local beforeMax = trees.GROUP_MAX_M.shrub
T.check(api.registerShapes('second', { shrub = { shapes = { { '/mods/y/grevillea', 1.5 } }, maxM = 300 } }) == true,
  'an existing group')
T.check(trees.CATALOG.shrub[1][1] == '/mods/y/grevillea' and trees.GROUP_MAX_M.shrub == 300, 'replaced')
T.check(api.registerShapes('third', { shrub = { shapes = { { '/mods/z/banksia', 1.8 } } } }) == true, 'a second owner')
T.check(trees.CATALOG.shrub[1][1] == '/mods/z/banksia' and trees.GROUP_MAX_M.shrub == beforeMax,
  'the latest wins; no maxM: the catalog own distance')
api.unregisterShapes('third')
T.check(trees.CATALOG.shrub[1][1] == '/mods/y/grevillea' and trees.GROUP_MAX_M.shrub == 300,
  'unregistering puts back the one under it')
api.unregisterShapes('second')
T.check(trees.CATALOG.shrub == before and trees.GROUP_MAX_M.shrub == beforeMax, 'then the catalog own')

-- registering again under one owner replaces its earlier registration
api.registerShapes('roadtrip_flora', { au_jacaranda = { shapes = { { '/mods/x/jacaranda', 9.0 } } } })
T.check(trees.CATALOG.au_brushbox == nil and trees.CATALOG.au_hedge == nil and trees.CATALOG.au_jacaranda ~= nil,
  'the owner earlier groups gone with it')

-- a codeswap reload: a fresh vegetation module gets the kept registrations and the event again
package.loaded['beamngEarth/trees'] = nil
local fresh = require('beamngEarth/trees')
T.check(fresh ~= trees and fresh.CATALOG.au_jacaranda == nil, 'a fresh module starts from its own catalog')
api.onUpdate(0.016)
T.check(fresh.CATALOG.au_jacaranda and fresh.CATALOG.au_jacaranda[1][1] == '/mods/x/jacaranda',
  'the api applies the kept registration to it')
T.check(count('onBeamNGEarthShapesCatalog') == 2, 'and the event again')

api.unregisterShapes('roadtrip_flora')
T.check(fresh.CATALOG.au_jacaranda == nil, 'unregistered')

T.done('test_api_shapes')
