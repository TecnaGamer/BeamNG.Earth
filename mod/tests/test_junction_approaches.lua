local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
function vec3(x, y, z) return {x=x,y=y,z=z} end
local N = require('beamngEarth/navgraph')
local function captured(reverse)
  local g={nodes={p={-19.52,354.66,581.16,6.2},a={-20.34,349.41,581.17,6.2},
    j={-20.72,347.54,581.17,6.2},w={-38.05,345.53,581.09,4.65},s={-22.8,338.35,581.18,4.28}},
    edges={{'p','a',0,2,2,10,.9,6.2,6.2,13.9},{'a','j',0,2,2,9.93,.9,6.2,6.2,13.9},
      {'j','w',0,1,2,13.89,.9,4.65},{'j','s',0,1,2,13.89,.9,4.28}}}
  if reverse then for _,e in ipairs(g.edges)do e[1],e[2],e[4],e[5]=e[2],e[1],e[5],e[4]end end
  return g
end
for _,reverse in ipairs({false,true})do
  for _,left in ipairs({false,true})do
    local g=captured(reverse)
    local ns,nn,ne=N.toNodes(g,nil,0,left)
    T.check(not ns.a and nn==4 and ne==3 and ns.p.links.j==ns.j.links.p,'recorded approach becomes one connected edge')
    T.check(ns.j.links.w and ns.j.links.s,'junction and both available turns remain')
    local e=ns.p.links.j
    T.check(e.speedLimit==9.93 and e.posted==13.9 and e.inRadius==6.2 and e.outRadius==6.2,
      'joined approach preserves widths, posted limit and the more conservative speed')
    T.check(e.inNode==(reverse and 'j'or'p')and e.outNode==(reverse and'p'or'j')and e.lanes==N.lanes(2,2,left),
      'stored direction and lanes survive either driving side')
    T.check(g.nodes.a and g.edges[1][2]==(reverse and'p'or'a'),'cached server graph remains unchanged')
  end
end
local variants={
  {'lane change',function(g)g.edges[2][4]=1 end},
  {'width change',function(g)g.edges[2][8]=5 end},
  {'taper',function(g)g.edges[2][9]=5 end},
  {'one-way change',function(g)g.edges[2][3]=1 end},
  {'posted limit change',function(g)g.edges[2][10]=8 end},
  {'surface change',function(g)g.edges[2][11]=true end},
  {'rumble change',function(g)g.edges[2][12]=true end},
  {'drivability change',function(g)g.edges[2][7]=.4 end},
  {'real bend',function(g)g.nodes.a[1]=g.nodes.a[1]+.5 end},
  {'grade change',function(g)g.nodes.a[3]=g.nodes.a[3]+.5 end},
  {'long approach',function(g)g.nodes.a={-19.52,354.66,581.16,6.2};g.nodes.p={-17,370,581,6.2}end},
  {'stop/signal approach',function(g)g.signals={{j='j',app={{'a'}}}}end},
  {'junction at shape node',function(g)g.nodes.x={-40,350,581.17,3};g.edges[5]={'a','x',0,1,1,5,.9,3}end},
  {'shortcut already present',function(g)g.edges[5]={'p','j',0,2,2,10,.9,6.2}end},
}
for _,case in ipairs(variants)do
  local g=captured(false);case[2](g)
  local ns=N.toNodes(g,nil,0,false)
  T.check(ns.a and ns.p.links.a and ns.a.links.j,'preserve '..case[1])
end
local g=captured(false);g.signals={{j='j',app={}}}
local ns=N.toNodes(g,nil,0,false)
T.check(not ns.a and ns.j.links.w and ns.j.links.s,'retained signalled junction still accepts a redundant approach removal')
g=captured(false);g.edges[2][3]=1;g.edges[1][3]=1
ns=N.toNodes(g,nil,0,false)
T.check(not ns.a and ns.p.links.j.oneWay and ns.p.links.j.inNode=='p','continuous one-way restriction survives')

local function equal(a, b)
  if type(a) ~= type(b) then return false end
  if type(a) ~= 'table' then return a == b end
  for k, v in pairs(a) do if not equal(v, b[k]) then return false end end
  for k in pairs(b) do if a[k] == nil then return false end end
  return true
end

-- Exercise the production yield interval inside both compaction passes, not just graph construction.
local many = {nodes={}, edges={}}
for i = 1, 64 do
  local source = captured(i % 2 == 0)
  for id, pos in pairs(source.nodes) do many.nodes[i .. id] = pos end
  for _, e in ipairs(source.edges) do
    e[1], e[2] = i .. e[1], i .. e[2]
    many.edges[#many.edges + 1] = e
  end
end
local expected = {N.toNodes(many, nil, 0, false)}
local originalShort, originalJunction, originalSort = N.compactShortEdges, N.compactJunctionApproaches, table.sort
local phase, yielded, sorts = nil, {}, 0
N.compactShortEdges = function(...)
  phase = 'short'
  local removed, ids = originalShort(...)
  phase = nil
  return removed, ids
end
N.compactJunctionApproaches = function(...)
  phase = 'junction'
  local removed = originalJunction(...)
  phase = nil
  return removed
end
table.sort = function(...) sorts = sorts + 1; return originalSort(...) end
local co = coroutine.create(function() return N.toNodes(many, nil, 0, false, N.PREP_YIELD) end)
local result, ok
repeat
  result = {coroutine.resume(co)}
  ok = table.remove(result, 1)
  if coroutine.status(co) ~= 'dead' and phase then yielded[phase] = true end
until not ok or coroutine.status(co) == 'dead'
N.compactShortEdges, N.compactJunctionApproaches, table.sort = originalShort, originalJunction, originalSort
T.check(ok and equal(result, expected), 'budgeted conversion preserves the complete synchronous graph and metadata')
T.check(yielded.short and yielded.junction, 'both cleanup passes yield at the production interval')
T.check(sorts == 1, 'conversion sorts the node IDs once')

local function chain(xs)
  local graph = {nodes={p={-30,0,0,6.2}, j={0,0,0,6.2}, w={0,30,0,6.2}, s={0,-30,0,6.2}}, edges={}}
  local previous = 'p'
  for i, x in ipairs(xs) do
    local id = string.char(96 + i)
    graph.nodes[id] = {x,0,0,6.2}
    graph.edges[#graph.edges + 1] = {previous,id,0,2,2,10,.9,6.2,6.2,13.9}
    previous = id
  end
  for _, id in ipairs({'j', 'w', 's'}) do
    graph.edges[#graph.edges + 1] = {previous,id,0,2,2,10,.9,6.2,6.2,13.9}
    previous = 'j'
  end
  return graph
end
for _, xs in ipairs({{-2.5,-2}, {-5.5,-3.5,-2}}) do
  local graph = chain(xs)
  local nodes, count, edges = N.toNodes(graph, nil, 0, false)
  T.check(count == 4 and edges == 3 and nodes.p.links.j == nodes.j.links.p,
    'reuse skips deleted seam IDs and still revisits earlier junction neighbours')
  T.check(nodes.j.links.w and nodes.j.links.s and graph.nodes.a and graph.nodes.b,
    'repeated cleanup preserves the junction branches and raw graph')
end
T.done('test_junction_approaches')
