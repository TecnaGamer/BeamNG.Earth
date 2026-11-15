-- Exercise the real navgraph apply with stubbed engine objects: select/reuse the handed shape and hide its static copy.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local N = require('beamngEarth/navgraph')
local shape = require('beamngEarth/signalshape')
N.MIN_GAP_S = 0
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end
local graph, current, made, refreshed = nil, { nodes = {} }, {}, {}
local refreshCount,moves=0,0
local trees = { refresh = function(group) refreshed[group] = true;refreshCount=refreshCount+1 end,
  setSignalFrame = function() error('stock mode must not re-fit Dutch signal frames') end }
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
      o.setTransform = function(self, m) self.transform = m;moves=moves+1 end
      o.asset = asset
      made[#made + 1] = o
      return o
    end
    return opts
  end,
}

local function apply(left,farNative)
  graph = { nodes = { j = { 0, 0, 0, 4 }, s = { 0, -80, 0, 4 } },
    edges = { { 's', 'j', 0, 1, 1, 13, 1, 4 } },
    signals = { { j = 'j', kind = 'signal', app = { { 's', 2, -10, 0, 0, 1, 0, 8, -10, 0, 0, left and 1 or nil } } } },
    rules = { rightHandDrive = left } }
  if farNative~=nil then
    graph.nlNative=true
    graph.nodes.fj={10000,0,0,4};graph.nodes.fs={10000,-80,0,4}
    graph.edges[#graph.edges+1]={'fs','fj',0,1,1,13,1,4}
    graph.signals[#graph.signals+1]={j='fj',kind='signal',nlNative=farNative,
      app={{'fs',10000,-10,0,0,1,0}}}
  end
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
for _,name in ipairs({'nl_signals','nl_signal_scene','nl_groenpol_catalog','nl_country','nl_signal_surfaces'})do
  T.check(package.loaded['beamngEarth/'..name]==nil,'stock mode never loads '..name)
end
package.loaded['beamngEarth/roads']={geoStats={tiles=1,skins=1,exact=1}}
local unchanged=refreshCount
for i=1,8 do N.update(1,{-500,-500,500,500}) end
T.check(refreshCount==unchanged,'stock update does not refill Dutch re-fit work or refresh signal tiles')
local budgetCalls=0
local gov={roomFor=function()budgetCalls=budgetCalls+1;return true end,
  noteFrame=function()budgetCalls=budgetCalls+1 end}
for i=1,8 do N.update(1,{-500,-500,500,500},gov) end
T.check(budgetCalls==0,'idle stock frames add no governor calls')
local signals=N.currentGraph().signals
local moved=moves
N.transform({apply=function(_,x,y,z)return x+100,y+200,z end},2)
T.check(N.currentGraph().signals==signals and moves==moved and refreshCount==unchanged,
  'stock rebase retains upstream signal data and does not move heads or refresh trees')
N.update(3,{-500,-500,500,500})
T.check(scenetree.findObjectById(made[#made].id)~=nil and refreshCount==unchanged,
  'stock anchor change adds no head release or tree refresh')
N.reset()
T.check(refreshCount==unchanged,'level teardown adds no stock tree refresh')
local registered=0
package.loaded['beamngEarth/net']={send=function()end}
package.loaded['beamngEarth/nl_signal_stream']={configure=function()end,graph=function()end,
  register=function()registered=registered+1 end,owns=function()return false end,spots=function()return{}end,
  clear=function()end,update=function()end}
N.dutchSignals=true
apply(false)
moved=moves;unchanged=refreshCount
signals=N.currentGraph().signals
N.transform({apply=function(_,x,y,z)return x+100,y+200,z end},2)
T.check(registered>0 and moves>moved and N.currentGraph().signals~=signals,
  'enabled Dutch path alone transforms approach positions and fallback heads')
N.reset();registered=0
apply(false,false)
T.check(registered==1,'native graph retains the normal AI window for distant stock controllers')
N.reset();registered=0
apply(false,true)
T.check(registered==2,'a complete native mount outside the AI window gets a visual controller')
N.reset();N.dutchSignals=false
T.done('test_signal_heads')
