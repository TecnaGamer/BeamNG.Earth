-- Exercise the real navgraph apply with stubbed engine objects: select/reuse the handed shape and hide its static copy.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local N = require('beamngEarth/navgraph')
local shape = require('beamngEarth/signalshape')
N.MIN_GAP_S = 0
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end
local graph, current, made, refreshed = nil, { nodes = {} }, {}, {}
local trees = { refresh = function(group) refreshed[group] = true end }
package.loaded['beamngEarth/trees'] = trees
package.loaded['beamngEarth/gps'] = {}
readFile = function() return '{}' end
writeFile = function() end
jsonDecode = function() return graph end
jsonEncode = function() return '{}' end
serialize = function() return '{}' end
lpack = { encodeBin = function() return '' end }
be.queueAllObjectLua = function() end
be.sendToMailbox = function() end
path = { getPathLevelInfo = function() end }
map = { reset = function(nodes) current = { nodes = nodes } end, getMap = function() return current end }
MatrixF = function()
  return { setFromEuler = function(self, e) self.euler = e end, setPosition = function(self, p) self.pos = p end }
end
core_trafficSignals = {
  resetSignals = function() end, setupSignals = function() end, getMapNodeSignals = function() return {} end,
  newController = function(opts) return { id = opts.name, applyDefinition = function() end } end,
  newSequence = function(opts) return { id = opts.name } end,
  newSignal = function(opts)
    opts.createSignalObject = function(_, asset)
      local o = createObject('TSStatic')
      o:registerObject(opts.name)
      o.setTransform = function(self, m) self.transform = m end
      o.asset = asset
      made[#made + 1] = o
      return o
    end
    return opts
  end,
}

local function apply(left)
  graph = { nodes = { j = { 0, 0, 0, 4 }, s = { 0, -80, 0, 4 } },
    edges = { { 's', 'j', 0, 1, 1, 13, 1, 4 } },
    signals = { { j = 'j', kind = 'signal', app = { { 's', 2, -10, 0, 0, 1, 0, 8, -10, 0, 0, left and 1 or nil } } } },
    rules = { rightHandDrive = left } }
  T.check(N.onMessage({ anchorId = 1, serial = 1, path = '/graph' }, 1), 'graph accepted')
  local b = { -500, -500, 500, 500 }
  N.update(1, b)
  N.update(1, b)       -- signals apply on the next frame
end
apply(false)
T.check(#made == 1 and made[1].asset == N.SIGNAL_SHAPE, 'right-hand graph uses the stock active head')
apply(false)
T.check(#made == 1, 'unchanged head is reused')
apply(true)
T.check(#made == 2 and made[2].asset == shape.LEFT .. '.dae', 'left-hand graph uses the reflected active head')
T.check(scenetree.findObjectById(made[1].id) == nil, 'a changed handedness replaces the old head')
T.check(trees.suppress('signal', 8, -10) and trees.suppress('signal_left', 8, -10), 'both static variants suppressed at active heads')
T.check(not trees.suppress('stop', 8, -10) and not trees.suppress('signal_left', 1000, 1000), 'unrelated props and distant signals kept')
T.check(refreshed.signal and refreshed.signal_left, 'both static groups refreshed after graph apply')
apply(true)
T.check(#made == 2, 'reflected head reused too')
T.done('test_signal_heads')
