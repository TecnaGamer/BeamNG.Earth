local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local B = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/trafficBoundary.lua')
local north, south = {x=0,y=1,z=0}, {x=0,y=-1,z=0}
local function road()
  local g = {graph={},positions={}}
  for i=-6,2 do g.graph[i]={};g.positions[i]={x=0,y=i*50,z=0} end
  for i=-6,1 do
    local e={inNode=i,outNode=i+1,oneWay=false,drivability=1}
    g.graph[i][i+1],g.graph[i+1][i]=e,e
  end
  return g
end
local function path(g,start,state,ends,minimum)
  return B.shortPath(g,start,g.positions[start],minimum or 150,state,ends or {[2]=true})
end
local g=road()
local p=path(g,0,north)
T.check(p and #p==3 and p[1]==0 and p[3]==2,'short forward road ends at the artificial cut instead of reversing')
T.check(path(g,-2,north)==nil,'enough forward distance keeps stock routing')
T.check(path(g,0,north,{})==nil,'real dead ends retain normal stock behavior')
T.check(path(g,0,south)==nil,'traffic travelling away from the cut keeps stock routing')
T.check(path(g,0,{-2,-1,0})[3]==2,'extending a forward path retains its short continuation')
T.check(path(g,2,{0,1,2})[1]==2,'an extension at the cut holds its endpoint')
T.check(#path(g,2,north)==1,'fresh planning facing out of the cut cannot select the road behind')
T.check(path(g,2,south)==nil,'a vehicle intentionally facing back into the road is free to leave')
T.check(path(g,0,{-2,-1})==nil,'unrelated previous path is left to the stock planner')
T.check(path(g,0,{x=1,y=0,z=0})==nil,'sideways recovery is not mistaken for a forward approach')
g.graph[1].side={inNode=1,outNode='side',drivability=1}
g.graph.side={[1]=g.graph[1].side};g.positions.side={x=50,y=50,z=0}
T.check(path(g,0,north)==nil,'an intervening junction keeps its stock route choices')
g=road();g.graph[1][2].oneWay=true;g.graph[1][2].inNode=2
T.check(path(g,0,north)==nil,'short routes never violate a one-way restriction')
g=road();g.graph[1][2].drivability=0
T.check(path(g,0,north)==nil,'blocked forward edges are not traversed')
g=road();g.positions[2].z=200
T.check(path(g,0,north)==nil,'look-ahead distance includes elevation like the stock planner')
g=road();g.graph[2][-6]={inNode=2,outNode=-6,drivability=1};g.graph[-6][2]=g.graph[2][-6]
T.check(path(g,0,north,{[2]=true},10000)==nil,'a stale cut on a loop cannot create an unbounded walk')
g=road();g.graph[2][3]={inNode=2,outNode=3,drivability=1};g.graph[3]={[2]=g.graph[2][3]};g.positions[3]={x=0,y=200,z=0}
T.check(path(g,0,north)==nil,'an extended road no longer treats its old cut as a terminal')

local calls,resets=0,0
local stockState
local function stock(self,start,origin,minimum,state) calls=calls+1;stockState=state;return {'stock'} end
local function fresh() local md=road();md.getPathTWithState=stock;return md end
local originalSetMap=function() mapmgr.mapData=fresh() end
mapmgr={mapData=fresh(),setMap=originalSetMap}
ai={mode='traffic',reset=function() resets=resets+1 end}
local mailbox
obj={getDirectionVector=function() return north end,getLastMailbox=function() return mailbox end}
lpack={decode=function(value) return value end}
B.onExtensionLoaded();B.setCuts({2})
local md=mapmgr.mapData
T.check(md:getPathTWithState(0,md.positions[0],150,north)[3]==2 and calls==0,
  'installed traffic planner uses the short forward route')
B.setCuts({2})
T.check(resets==1,'initial installation clears an already planned route only once')
T.check(md:getPathTWithState(2,md.positions[2],150,{2})[1]==2,
  'repeated planning at a held endpoint does not pass singleton history to stock')
ai.mode='manual'
T.check(md:getPathTWithState(0,md.positions[0],150,north)[1]=='stock','player Autodrive is unaffected')
ai.mode='traffic';mapmgr.setMap()
local nextMap=mapmgr.mapData
T.check(nextMap~=md and nextMap:getPathTWithState(0,nextMap.positions[0],150,north)[3]==2,
  'replacement map is protected before returning to the AI')
T.check(resets==1,'map replacements do not issue extra AI resets')
B.setCuts({'futureCut'})
T.check(nextMap:getPathTWithState(0,nextMap.positions[0],150,north)[3]==2,
  'future boundary metadata does not unprotect the still-active old map')
B.setCuts({})
T.check(nextMap:getPathTWithState(2,nextMap.positions[2],150,{2})[1]==2,
  'an empty future boundary cannot release an old endpoint before its map arrives')
nextMap.graph[2][3]={inNode=2,outNode=3,drivability=1}
nextMap.graph[3]={[2]=nextMap.graph[2][3]};nextMap.positions[3]={x=0,y=300,z=0}
T.check(nextMap:getPathTWithState(2,nextMap.positions[2],150,{2})[1]=='stock' and stockState==north,
  'extending the old cut returns to stock planning with a valid heading')
T.check(nextMap:getPathTWithState(0,nextMap.positions[0],150,north)[1]=='stock',
  'retained cut identities do not stop traffic on an extended road')
B.onExtensionUnloaded()
T.check(mapmgr.setMap==originalSetMap and nextMap.getPathTWithState==stock,'unloading restores owned public methods')
-- A recycled vehicle can receive a map before its GE acknowledgement delivers cut IDs.
-- Its first plan must use the boundaries published with that map, not the previous map's IDs.
mapmgr.mapData=nil
local nextId='newCut'
mapmgr.setMap=function()
  local md=fresh()
  md.graph[nextId]=md.graph[2];md.positions[nextId]=md.positions[2]
  md.graph[1][nextId]=md.graph[1][2];md.graph[1][2]=nil
  md.graph[2]=nil;md.positions[2]=nil
  mapmgr.mapData=md
end
mailbox={nextId}
B.onExtensionLoaded();mapmgr.setMap()
local firstMap=mapmgr.mapData
T.check(firstMap:getPathTWithState(0,firstMap.positions[0],150,north)[3]==nextId,
  'the first plan on a recycled vehicle map uses its mailbox boundaries before the GE acknowledgement')
B.onExtensionUnloaded()
local N=require('beamngEarth/navgraph')
function vec3(x,y,z) return {x=x,y=y,z=z} end
local _,_,_,clipCuts=N.toNodes({nodes={a={0,0,0,3},b={50,0,0,3},c={150,0,0,3}},
  edges={{'a','b',0,1,1,25,1,3,3},{'b','c',0,1,1,25,1,3,3}},signals={}}, {-100,-100,100,100},0,false)
T.check(#clipCuts==1 and clipCuts[1][3]=='b','clipping records the actual cut node identity')
local received
beamngEarth_trafficBoundary={setCuts=function(ids) received=ids end}
assert(loadstring(N.trafficBoundaryCommand({{0,100,'cut"quoted'},{0,200,'next'}})))()
T.check(received[1]=='cut"quoted' and received[2]=='next','GE hands off exact cut identities with Lua-safe quoting')
-- A fresh city spawn can start at a junction just before the server's window
-- ends. Stock's minimum look-ahead can then select the incoming one-way road.
do
  local md={graph={},positions={a={x=0,y=-50,z=0},b={x=0,y=0,z=0},
    c={x=0,y=30,z=0},side={x=-20,y=0,z=0}}}
  for id in pairs(md.positions)do md.graph[id]={}end
  local function link(a,b,oneWay)
    local e={inNode=a,outNode=b,oneWay=oneWay,drivability=1}
    md.graph[a][b],md.graph[b][a]=e,e
  end
  link('a','b',true);link('b','c',true);link('b','side',false)
  local chosen={'b','a'}
  md.getPathTWithState=function()return chosen end
  mapmgr={mapData=md,setMap=function()end};mailbox=nil
  B.onExtensionLoaded();B.setCuts({'c'})
  local safe=md:getPathTWithState('b',md.positions.b,80,north)
  T.check(#safe==2 and safe[1]=='b' and safe[2]=='c',
    'an illegal stock reversal at a starting junction keeps the unique forward approach to a stream cut')
  chosen={'b','side'}
  T.check(md:getPathTWithState('b',md.positions.b,80,north)==chosen,
    'a legal stock junction choice is preserved')
  chosen={'b','a'};md.positions.side={x=20,y=20,z=0}
  T.check(md:getPathTWithState('b',md.positions.b,80,north)==chosen,
    'multiple forward branches are not replaced by an invented junction choice')
  B.onExtensionUnloaded()
end
do
  local graph={nodes={a={0,0,0,3},b={50,0,0,3}},edges={{'a','b',0,1,1,25,1,3}},
    cuts={'b','absent'},signals={}}
  local _,_,_,sourceCuts=N.toNodes(graph,nil,0,false)
  T.check(#sourceCuts==1 and sourceCuts[1][3]=='b',
    'source-window boundaries survive inside the physical terrain box; absent IDs are ignored')
  graph.nodes.c={150,0,0,3};graph.edges[2]={'b','c',0,1,1,25,1,3}
  local _,_,_,bothCuts=N.toNodes(graph,{-100,-100,100,100},0,false)
  T.check(#bothCuts==1 and bothCuts[1][3]=='b','source and physical cuts share a single boundary record')
end
T.done('traffic stream boundary')
