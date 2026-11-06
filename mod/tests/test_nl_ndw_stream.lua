local dir=arg[0]:match('^(.*)[/\\]')or'.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local data={schema_version=1,kind='ndw_small_area_preview',records={{id='a',lat=51,lon=6,bearing_deg=0},{id='b',lat=51,lon=6.002,bearing_deg=90}}}
local st={active=true,anchorId=1,anchor={geodeticToWorld=function(_,lat,lon)return (lon-6)*100000,(lat-51)*100000 end}}
local count,spawns,clears=0,0,0
local r={clear=function()count=0;clears=clears+1 end,report=function()return{objects=count}end,previewNDW=function(s)count=#s.records*3;spawns=spawns+1;return count end}
extensions={beamngEarth_nltestset=r,beamngEarth_core={_state=function()return st end}}
jsonReadFile=function()return data end
local p={x=0,y=0,z=0};getPlayerVehicle=function()return{getPosition=function()return p end}end
local M=require('beamngEarth/nlndw')
M.start('index','travel_bearing');M.onUpdate(1);assert(count==3 and spawns==1)
M.onUpdate(1);assert(spawns==1,'Unchanged set must reuse objects')
p.x=100;M.onUpdate(1);assert(count==6 and spawns==2)
st.pending={};M.onUpdate(.01);assert(count==0,'Pending teleport clears immediately')
st.pending=nil;st.anchorId=2;M.onUpdate(1);assert(count==6 and spawns==3)
p.x=500;M.onUpdate(1);assert(count==0 and M.report().outcome.status=='outside_pilot_area')
p.x=0;r.previewNDW=function()error('terrain unavailable')end;M.onUpdate(1);assert(count==0 and M.report().outcome.status=='placement_failed')
M.stop();M.onUpdate(1);assert(not M.report().active)
-- A partially loaded road network must not freeze skipped placements forever.
local skipped=true
r.report=function()return{objects=count,outcome={locations={{source_ids={'a'}}},skipped=skipped and {{reason='no_roads'}} or {}}}end
r.previewNDW=function(s)count=3;spawns=spawns+1;return count end
M.start('index','travel_bearing');M.onUpdate(1);local initial=spawns
for _=1,4 do M.onUpdate(1)end;assert(spawns==initial,'Do not retry each frame')
M.onUpdate(1);assert(spawns==initial+1,'Retry blocked source positions after five seconds')
skipped=false;M.onUpdate(5);local settled=spawns;M.onUpdate(10);assert(spawns==settled,'Stop retries once placement succeeds')
package.loaded['beamngEarth/navgraph']={nlRevision=1}
M.onUpdate(1);assert(spawns==settled+1,'New applied road metadata triggers size/orientation replanning')
M.onUpdate(1);assert(spawns==settled+1,'Unchanged road metadata does not force repeated replanning')
M.stop()
print('PASS: bounded NDW preview loads/unloads on movement, reuses unchanged sets, clears pending teleport, rebuilds on new anchor, handles placement failure and stops.')
