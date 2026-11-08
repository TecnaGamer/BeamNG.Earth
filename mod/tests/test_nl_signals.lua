-- Real border/geodesy + pure planning; stubbed engine lifecycle (no in-game rendering/AI claim).
local dir = arg[0]:match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local G = require('beamngEarth/geodesy')
local P = require('beamngEarth/nl_signals')
local N = require('beamngEarth/navgraph')
local STOCK = N.SIGNAL_SHAPE
local frame = G.newAnchor(51.445, 4.929)
local function approach(lat, lon, dx, dy)
  local x,y,z = frame:geodeticToWorld(lat,lon,17)
  return {'a', x-4, y-3, z-.15, dx or 1, dy or 0, 0, x,y,z, 37}
end
local dutch = approach(51.445,4.929)
local foreign = approach(51.4416,4.9323)
local function same(a,b) for k,v in pairs(a) do if v~=b[k] then return false end end;return true end
local before = {}; for k,v in pairs(dutch) do before[k]=v end
local plan=P.plan(dutch,frame,STOCK,true)
T.check(plan.model=='groenpol_vehicle' and plan.shape==P.SHAPE,'NL prop chooses prepared genuine vehicle')
T.check(plan.x==dutch[8] and plan.z==dutch[10] and plan.stop[3]==dutch[4],'prop/deck/stop heights unchanged')
T.check(same(before,dutch),'planning does not mutate cached graph arrays')
for _,d in ipairs({{1,0},{-1,0},{0,1},{0,-1},{.6,.8}}) do
  local a=approach(51.445,4.929,d[1],d[2]);local p=P.plan(a,frame,STOCK,true)
  T.near(math.cos(math.rad(p.yaw)),-d[1],1e-10,'front faces approaching driver X')
  T.near(math.sin(math.rad(p.yaw)),-d[2],1e-10,'front faces approaching driver Y')
end
T.check(P.plan(foreign,frame,STOCK,true).shape==STOCK and P.plan(foreign,frame,STOCK,true).yaw==37,
  'Belgian enclave in a Dutch-anchored graph keeps exact stock shape/yaw')
for _,ll in ipairs({{51.3167,6.2833},{51.221,4.403},{50.776,6.083},{39.28,-76.56},{51.507,-.128}}) do
  local a=approach(ll[1],ll[2]);local p=P.plan(a,frame,STOCK,true)
  T.check(p.shape==STOCK and p.x==a[8] and p.y==a[9] and p.z==a[10] and p.yaw==a[11],
    'foreign placement/stock selection identical: '..ll[1]..','..ll[2])
end
T.check(P.plan(dutch,nil,STOCK,true).reason=='frame_unavailable','unknown frame falls back')
local bad=approach(51.445,4.929,0,0)
T.check(P.plan(bad,frame,STOCK,true).reason=='direction_unavailable','unknown direction falls back')
T.check(P.plan({'a',0,0,0,1,0,0},frame,STOCK,true).reason=='position_unavailable','no safe prop position invented')
for _,missing in ipairs(P.RESOURCES) do
  T.check(not P.available(function(p) return p~=P.ROOT..missing end),'missing resource: '..missing)
end
local loads=0
loadJsonMaterialsFile=function() loads=loads+1 end
P.reset();P.resolve(foreign,frame,STOCK)
T.check(loads==0,'foreign selection never loads Groenpol materials')
P.resolve(dutch,frame,STOCK);P.resolve(dutch,frame,STOCK)
T.check(loads==1,'one shared material load across heads')
P.reset();loadJsonMaterialsFile=function() error('fixture material failure') end
T.check(P.resolve(dutch,frame,STOCK).reason=='material_load_failed','material import failure keeps stock')
loadJsonMaterialsFile=function() loads=loads+1 end;P.reset()
local audit=P.audit({signals={{j='mixed',kind='signal',app={dutch,foreign}},
  {j='stop',kind='stop',app={dutch}}}},frame,STOCK,true)
T.check(audit.groenpol==0 and audit.stock==1 and audit.omitted==1 and audit.stops==1,'mixed-border audit distinguishes unverified Dutch geometry from foreign stock')

-- Exercise the real navgraph apply/setup order, resource fallback, head reuse and unloading.
table.clear=table.clear or function(t) for k in pairs(t) do t[k]=nil end end
local mapped={nodes={}}
map={getMap=function() return mapped end,reset=function(nodes) mapped={nodes=nodes} end}
path={getPathLevelInfo=function() return '/level/info.json' end}
writeFile=function() end;jsonEncode=function() return '{}' end;serialize=function() return '{}' end
lpack={encodeBin=function() return '' end}
be.queueAllObjectLua=function() end;be.sendToMailbox=function() end
MatrixF=function() return {setFromEuler=function(self,e) self.e=vec3(e) end,setPosition=function(self,p) self.p=vec3(p) end,
  setColumn=function(self,i,v) self.columns=self.columns or {};self.columns[i]=vec3(v) end} end
local rawCreate=createObject
local failSupport=false
createObject=function(class)
  local o=rawCreate(class);function o:setTransform(m) self.m=m end
  local field=o.setField
  function o:setField(k,i,v) if failSupport and k=='shapeName' then error('fixture support failure') end;field(self,k,i,v) end
  return o
end
package.loaded['beamngEarth/gps']={}
local trees=require('beamngEarth/trees')
local function live() local a={};for _,o in ipairs(S.liveOf('TSStatic'))do if o.instance then a[#a+1]=o end end;table.sort(a,function(x,y)return x:getID()<y:getID()end);return a end
local function supports() local a={};for _,o in ipairs(S.liveOf('TSStatic'))do if o.fields.bneSignalRole=='support' then a[#a+1]=o end end;table.sort(a,function(x,y)return x:getID()<y:getID()end);return a end
local id=0
local data, definitions, events, failShape
core_trafficSignals={resetSignals=function() events[#events+1]='reset' end,getMapNodeSignals=function() return {} end}
function core_trafficSignals.newController(t)
  id=id+1;t.id=id;t.applyDefinition=function(self,def) self.type=def;definitions[#definitions+1]=def end
  t.autoSetTimings=function(self,speed,width) self.speed,self.width=speed,width end;return t
end
function core_trafficSignals.newSequence(t) id=id+1;t.id=id;return t end
function core_trafficSignals.newSignal(t)
  id=id+1;t.id=id
  function t:createSignalObject(shape,pos)
    if failShape==shape or failShape=='all' then return nil end
    local o=createObject('TSStatic');o.shape=shape;o.instance=self.name
    function o:setTransform(m) self.m=m end
    o:registerObject();return o
  end
  return t
end
function core_trafficSignals.setupSignals(t)
  events[#events+1]='setup';data=t
  -- Link by stable instance name, as setupSignalObjects does in the engine.
  for _,s in ipairs(data.instances) do
    s.linkedObjects={}
    for _,o in ipairs(live()) do if o.instance==s.name then s.linkedObjects[#s.linkedObjects+1]=o:getID() end end
  end
end
local graph
jsonDecode=function() return graph end;readFile=function() return '{}' end
-- These lifecycle fixtures already represent an OSM-verified client graph. The
-- raw-to-verified selection, including rejection of inferred VRIs, is tested separately.
local originalAugment=require('beamngEarth/nl_signal_osm').augment
require('beamngEarth/nl_signal_osm').augment=function(g)return g,{fixture='verified'}end
N.MIN_GAP_S=0;N.PREP_YIELD=10000;N.PREP_MS=math.huge
local function fixture(a,kind)
  local dx,dy=a[5],a[6];local cx,cy=a[8]-dy*4,a[9]+dx*4
  return {nodes={j={cx+dx*20,cy+dy*20,17,4},a={cx-dx*50,cy-dy*50,17,3}},edges={{'a','j',0,1,1,13,1,3}},
    signals={{j='j',kind=kind or 'signal',app={a}}},rules={rightHandDrive=false,turnOnRed=false,redYellow=false}}
end
local serial=0
local function apply(g,anchor,f,box)
  graph=g;definitions={};events={};serial=serial+1
  T.check(N.onMessage({anchorId=anchor,path='fixture',serial=serial},anchor),'graph accepted')
  N.update(anchor,box,nil,f);N.update(anchor,box,nil,f)
  T.check(events[1]=='reset' and events[2]=='setup','reset before graph, setup on next update')
end
N.reset()
apply(fixture(dutch),1,frame)
T.check(#live()==2 and #supports()==2 and live()[1].shape==P.SHAPE and supports()[1].fields.shapeName==P.SUPPORT_SHAPE,'right and left functional head on separate bare masts')
T.check(trees.suppress('signal',dutch[8],dutch[9]) and not trees.suppress('stop',dutch[8],dutch[9]),'suppresses existing static signal only')
local first=live()[1]:getID();local firstSupport=supports()[1]:getID()
T.check(data.instances[1].pos.z==dutch[4] and data.instances[1].dir.x==1,'native signal stop point/direction retained')
T.check(#definitions==2 and definitions[1]=='lightsBasic' and #data.sequences==1,'existing two approach phases/controllers retained')
apply(fixture(dutch),1,frame)
T.check(#live()==2 and live()[1]:getID()==first and supports()[1]:getID()==firstSupport and #data.instances[1].linkedObjects==2,'heads reused and relinked by instance name')
-- The installed game's real setLights method and material palettes are checked by tools/audit_groenpol.py.
T.check(live()[1].fields.instanceColor=='0 0 0 1' and live()[1].fields.instanceColor1=='0 0 0 1'
  and live()[1].fields.instanceColor2=='0 0 0 1','head initialisation is dark until native controller setup')
local raised=approach(51.445,4.929);raised[10]=raised[10]+7
apply(fixture(raised),1,frame)
T.check(live()[1]:getID()~=first and math.abs(live()[1].m.p.z-raised[10]-plan.layout.mounts[1].position[3])<1e-8 and supports()[1].m.p.z==raised[10],'height change replaces reused head (bridge deck)')
local turn=approach(51.445,4.929,0,1)
local raisedId=live()[1]:getID();apply(fixture(turn),1,frame)
T.check(live()[1]:getID()~=raisedId,'direction change updates head at same XY')
FS.fileExists=function(_,p) return p~=P.ROOT..'lens_channel_2_v2.color.png' end
apply(fixture(dutch),1,frame)
T.check(#live()==0 and #supports()==0,'missing Dutch asset omits visible assembly instead of reviving stock')
FS.fileExists=function() return true end
failShape=P.SHAPE;apply(fixture(dutch),1,frame)
T.check(#live()==0 and #supports()==0,'native head failure removes partial mast without old fallback')
failShape=nil;failSupport=true;apply(fixture(dutch),1,frame)
T.check(#live()==0 and #supports()==0,'mast setup failure cleans partial object without stock fallback')
failSupport=false
failShape='all';apply(fixture(dutch),1,frame)
T.check(#live()==0 and #supports()==0 and not trees.suppress('signal',dutch[8],dutch[9]),'both models failed: static tile fallback is visible')
failShape=nil
local nlEu=fixture(dutch);nlEu.rules.redYellow=true;apply(nlEu,1,frame)
T.check(definitions[1]=='lightsBasic','Dutch controllers omit red-yellow even when graph uses EU definition')
apply(fixture(foreign),1,frame)
T.check(#live()==1 and live()[1].shape==STOCK,'mixed-border graph uses exact stock outside NL')
local eu=fixture(foreign);eu.rules.redYellow=true;apply(eu,1,frame)
T.check(definitions[1]=='lightsBasicEU','non-NL controller definition still follows original graph rules')
local preRebaseId=live()[1]:getID()
local xf={apply=function(_,x,y,z) return -y+10,x+20,z+3 end}
T.check(N.transform(xf,2),'graph rebase')
local rebased=N.currentGraph().signals[1].app[1]
T.near(rebased[2],-foreign[3]+10,1e-9,'rebase stop X');T.near(rebased[8],-foreign[9]+10,1e-9,'rebase prop X')
T.near(rebased[6],1,1e-9,'rebase direction rotates');T.near(rebased[11],127,1e-8,'stock yaw rotates')
T.check(live()[1]:getID()==preRebaseId and live()[1].m.p.z==foreign[10]+3,'linked mesh moves immediately with rebase')
N.update(2,nil,nil,nil);N.update(2,nil,nil,nil)
T.check(#live()==1 and live()[1]:getID()==preRebaseId,'rebased head reused after native setup')
N.update(3,nil,nil,frame)
T.check(#live()==0 and #supports()==0 and not trees.suppress('signal',foreign[8],foreign[9]),'anchor switch cleans heads and suppression')
apply(fixture(dutch,'stop'),3,frame)
T.check(#live()==0 and #data.sequences==0 and #definitions==1 and definitions[1]=='signStop','stop controllers unchanged; no Groenpol head')
apply(fixture(dutch),3,frame)
local empty=fixture(dutch);empty.signals={};apply(empty,3,frame)
T.check(#live()==0 and #supports()==0 and not trees.suppress('signal',dutch[8],dutch[9]),'stream-out removes heads and restores fallback')
apply(fixture(dutch),3,frame)
local cap=plan.layout.mounts[1].position[3]
local hid,mid=live()[1]:getID(),supports()[1]:getID()
local headForward=live()[1].m.columns[0]
local forwardX,forwardY=headForward.x,headForward.y
T.check(N.transform(xf,4),'NL assembly rebase')
T.check(live()[1]:getID()==hid and supports()[1]:getID()==mid,'rebase keeps both piece identities')
T.near(supports()[1].m.p.z,dutch[10]+3,1e-8,'mast follows rebase')
T.near(live()[1].m.p.z-supports()[1].m.p.z,cap,1e-8,'cap attachment preserved after rebase')
T.near(live()[1].m.p.x,supports()[1].m.p.x,1e-8,'head stays centred on mast after rotation')
T.near(live()[1].m.columns[0].x,-forwardY,1e-8,'rebase rotates the independently aimed head forward X')
T.near(live()[1].m.columns[0].y,forwardX,1e-8,'rebase rotates the independently aimed head forward Y')
T.check(#data.instances[1].linkedObjects==2,'only the two heads linked to traffic controller')
N.reset();N.reset()
T.check(#live()==0 and #supports()==0 and not trees.suppress('signal',dutch[8],dutch[9]),'reset/unload idempotently clears all heads')
-- The live scene consumes the existing graph lane counts, including cached 11-field approaches.
for n=2,4 do
  local a=approach(51.445,4.929);local x,y,z=a[8],a[9],a[10]
  local radius=(n+1)*3.3/2;a[9]=y-radius-1
  local g=fixture(a);g.nodes={j={x+20,y,z,radius},a={x-50,y,z,radius}}
  g.edges={{'a','j',0,n,1,13,1,radius,radius}}
  apply(g,5,frame)
  T.check(#live()==n+2 and #supports()==2,'lane scene: overhead heads, low repeat and left repeat for '..n)
  T.check(#data.instances==1 and #data.instances[1].linkedObjects==n+2,'native approach phase controls lane heads and repeaters')
  T.check(#definitions==2 and #data.sequences==1,'lane scene preserves native approach controller timings')
  local ids={};for _,o in ipairs(live())do ids[o:getID()]=true end
  apply(g,5,frame)
  for _,o in ipairs(live())do T.check(ids[o:getID()],'lane objects reused by stable assembly signature') end
  g.signals={};apply(g,5,frame)
  T.check(#live()==0 and #supports()==0,'stream-out cleans complete lane assembly')
end
N.reset()
-- Visible junctions outside the clipped AI map follow the real sequence but add no AI roads/signals.
apply(fixture(dutch),6,frame,{-10,-10,10,10})
T.check(#data.instances==0 and #data.controllers==2 and #data.sequences==1,'outside AI clip: visual controller sequence only')
local c=data.controllers[1];c.states={{state='redState'},{state='greenState'}}
function c:getStateData(state)return {lights=state=='redState' and {'red','black','black'} or {'black','black','green'}}end
data.sequences[1].controllerStates={[c.name]={stateIdx=1}}
N.update(6,{-10,-10,10,10},nil,frame)
local visible=0
for _,o in ipairs(S.liveOf('TSStatic'))do if o.fields.bneSignalRole=='head' then
  visible=visible+1;T.check(not o.instance and o.fields.instanceColor=='1 1 1 1','visual heads read actual native phase without AI linkage')
end end
T.check(visible==2 and #live()==0,'both visual repeat heads exist outside AI graph')
data.sequences[1].controllerStates[c.name].stateIdx=2;N.update(6,{-10,-10,10,10},nil,frame)
for _,o in ipairs(S.liveOf('TSStatic'))do if o.fields.bneSignalRole=='head' then
  T.check(o.fields.instanceColor=='0 0 0 1' and o.fields.instanceColor2=='1 1 1 1','visual heads track green transition')
end end
N.reset();T.check(#S.liveOf('TSStatic')==0,'reset removes every distant visual part')
-- A busy governor must not starve an omitted approach forever. Never add heavy
-- work during a terrain-commit frame, but force one retry after bounded waiting.
local oldRoads,oldClock=package.loaded['beamngEarth/roads'],os.clock
package.loaded['beamngEarth/roads']={geoStats={tiles=1,skins=1,exacts=0}}
failShape='all';apply(fixture(dutch),7,frame)
T.check(#live()==0,'initially unavailable assembly retained as pending request')
failShape=nil
local clock=oldClock()+10;os.clock=function()return clock end
local heavy=true
local busy={roomFor=function()return false end,isHeavyFrame=function()return heavy end,noteFrame=function()end}
for i=1,65 do N.update(7,nil,busy,frame)end
T.check(#live()==0,'terrain-heavy frames never force a VRI rebuild')
heavy=false
for i=1,59 do N.update(7,nil,busy,frame)end
T.check(#live()==0,'bounded retry waits when governor has no room')
N.update(7,nil,busy,frame)
T.check(#live()==2,'VRI appears after bounded busy-frame wait without graph reload')
os.clock=oldClock;package.loaded['beamngEarth/roads']=oldRoads;N.reset()
T.done('test_nl_signals')
