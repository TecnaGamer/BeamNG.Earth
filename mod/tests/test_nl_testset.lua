local dir=arg[0]:match('^(.*)[/\\]') or '.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
math.atan2=math.atan2 or function(y,x) return math.atan(y,x) end
math.sinh=math.sinh or function(x) return (math.exp(x)-math.exp(-x))/2 end
local fixture=dofile(assert(arg[1],'Pass the generated fixture via tools/test_nl_testset.mjs'))
local geodesy=require('beamngEarth/geodesy')
local current={active=true,anchorId=7,anchor=geodesy.newAnchor(52.09,5.12)}
extensions={beamngEarth_core={_state=function() return current end}}
local objects,nextId,loaded={},0,false
local vehicle={getPosition=function() return {x=0,y=0,z=1} end}
getPlayerVehicle=function() return vehicle end
jsonReadFile=function() return fixture end
local saved
jsonWriteFile=function(p,r) saved={p,r} end
vec3=function(x,y,z) return {x=x,y=y,z=z} end
be={getSurfaceHeightBelow=function(_,p) return .01*p.x end}
scenetree={findObject=function(name) if name=='bngEarthTerrain' then return {getHeight=function(_,p)return .01*p.x end} end;return loaded and {} or nil end,findObjectById=function(id)return objects[id] end,MissionGroup={addObject=function()end}}
loadJsonMaterialsFile=function() loaded=true end
log=function()end
createObject=function(class)
 assert(class=='TSStatic' and loaded)
 local o={fields={}}
 function o:setField(k,_,v)self.fields[k]=v end
 function o:setScale(scale)self.scale=scale end
 function o:setPosRot(...)self.pos={...} end
 function o:registerObject()nextId=nextId+1;self.id=nextId;objects[self.id]=self end
 function o:getID()return self.id end
 function o:delete()objects[self.id]=nil end
 return o
end
map={getMap=function()return {nodes={a={pos={x=-200,y=0},radius=3,links={b={inRadius=3,outRadius=3}}},b={pos={x=200,y=-20},radius=3,links={}}}}end}
local M=require('beamngEarth/nl_testset')
assert(require('beamngEarth/nltestset')==M, 'Engine-compatible extension entry point must expose the tested implementation')
assert(M.nearCar()==119)
local report=M.report();assert(report.outcome.geographic and #report.outcome.locations==39)
for _,p in ipairs(report.outcome.locations) do
 local x,y,z=current.anchor:geodeticToWorld(p.lat,p.lon,p.height_m)
 assert(x>=9.999 and x<=46.001 and y>=9.999 and y<=31.001)
 assert(math.abs(z-.01*x)<1e-5 and p.tile:match('^g17_'))
end
assert(M.nearCar()==119 and M.report().objects==119,'Repeated fixture must replace previous objects')
for _,o in pairs(objects) do assert(o.fields.collisionType=='None') end
-- Reject unreliable height before destroying the accepted previous fixture.
local oldFind=scenetree.findObject
scenetree.findObject=function(name)if name=='bngEarthTerrain' then return {getHeight=function()return 0/0 end} end;return oldFind(name)end
assert(not pcall(M.nearCar));assert(M.report().objects==119)
scenetree.findObject=function(name)if name=='bngEarthTerrain' then return {getHeight=function()return nil end} end;return oldFind(name)end
assert(not pcall(M.nearCar));assert(M.report().objects==119)
scenetree.findObject=oldFind
be.getSurfaceHeightBelow=function()return 99 end -- roofs must not influence Earth placement
assert(M.nearCar()==119)
M.benchmark();assert(M.report().objects==0)
for i=1,1700 do M.onUpdate(1/60) end
report=M.report();assert(report.outcome.measured and report.objects==119)
assert(math.abs(report.outcome.benchmark.baseline.average_fps-60)<1e-8)
M.saveReport();assert(saved and saved[1]:find('beamngEarth%-nl%-testset%-report.json'))
-- Real-source preview exercises cardinal orientation, original assets and transactional validation.
local preview={kind='ndw_small_area_preview',schema_version=1,records={}}
for i,bearing in ipairs({0,45,90,180,270,315})do
 local lat,lon=current.anchor:worldToGeodetic(i*10,10,0)
 preview.records[i]={id='ndw_'..i,lat=lat,lon=lon,asset='B6',bearing_deg=bearing}
end
assert(not pcall(M.previewNDW,preview),'Convention must be explicit')
assert(M.previewNDW(preview,'travel_bearing')==18)
local allocated=nextId
assert(M.previewNDW(preview,'travel_bearing')==18)
assert(nextId==allocated and M.report().render_delta.reused==18 and M.report().render_delta.created==0,'Stable assemblies reuse actual scene IDs')
local saved=table.remove(preview.records)
assert(M.previewNDW(preview,'travel_bearing')==15 and nextId==allocated,'Removing one source must not respawn retained assemblies')
assert(M.report().render_delta.deleted==3 and M.report().render_delta.reused==15)
preview.records[#preview.records+1]=saved
assert(M.previewNDW(preview,'travel_bearing')==18 and nextId==allocated+3,'Only returning assembly allocates new objects')
local rows=M.report().outcome.locations
for i,r in ipairs(rows)do
 local theta=math.rad(r.face_yaw_deg+90)
 local dx,dy=geodesy.beamngRotate(0,0,math.sin(theta/2),math.cos(theta/2),0,-1,0)
 local expected=math.rad(preview.records[i].bearing_deg+180)
 local ex,ey=math.sin(expected),math.cos(expected)
 assert(r.boards[1].source_bearing_deg==preview.records[i].bearing_deg,'Original source bearing is retained')
 if r.boards[1].rotation_status=='small_axis_alignment_inferred' or r.boards[1].rotation_status=='coarse_bearing_axis_alignment_inferred' then
  local length=math.sqrt(400^2+20^2);local ax,ay=400/length,-20/length
  local sign=ex*ax+ey*ay>=0 and 1 or -1;ex,ey=ax*sign,ay*sign
 end
 assert(math.abs(dx-ex)<.001 and math.abs(dy-ey)<.001,'Front normal respects source or bounded road-axis correction')
 assert(not r.synthetic and r.tile:match('^g17_'))
 local checked=0
 for _,o in pairs(objects)do
  if o.fields.shapeName:find('B6.dae',1,true) and math.abs(o.pos[1]-i*10)<.01 then
   local rx,ry=geodesy.beamngRotate(o.pos[4],o.pos[5],o.pos[6],o.pos[7],0,-1,0)
   assert(math.abs(rx-ex)<.001 and math.abs(ry-ey)<.001,'Actual engine quaternion must face the audited direction')
   for _,clamp in pairs(objects)do if clamp.fields.shapeName:find('clamps_',1,true) and math.abs(clamp.pos[1]-o.pos[1])<.01 and math.abs(clamp.pos[3]-o.pos[3])<.01 then
    for k=4,7 do assert(math.abs(clamp.pos[k]-o.pos[k])<1e-10,'Clamps and plate must have the same engine rotation')end
    checked=checked+1
   end end
  end
 end
 assert(checked==1,'One matching clamp pair per board')
end
preview.records[1].asset='A1-30-ZB'
preview.records[1].supplementary_assets={'OB504'}
assert(M.previewNDW(preview,'travel_bearing')==20)
local short=0
for _,o in pairs(objects)do if o.fields.shapeName:find('clamps_10cm.dae',1,true)then short=short+1 end end
assert(short==1,'Small supplementary plate needs the short clamp pair')
-- Two source records at one place share one post, including opposite faces.
local la,lo=current.anchor:worldToGeodetic(10,10,0)
local shared={kind='ndw_small_area_preview',schema_version=1,records={{id='zone',lat=la,lon=lo,asset='A2-30-ZE',bearing_deg=90},{id='yield',lat=la,lon=lo,asset='B6',bearing_deg=90}}}
assert(M.previewNDW(shared,'travel_bearing')==5)
local reportShared=M.report();assert(#reportShared.outcome.locations==1 and #reportShared.outcome.locations[1].source_ids==2)
shared.records[2].bearing_deg=270
assert(M.previewNDW(shared,'travel_bearing')==5)
local oldMap=map;map=nil
assert(M.previewNDW(shared,'travel_bearing')==0 and M.report().outcome.status=='placement_blocked')
map=oldMap
-- A valid neighborhood may contain no usable signs while roads/data arrive.
assert(M.previewNDW({kind='ndw_small_area_preview',schema_version=1,records={}},'travel_bearing')==0)
for i=1,3 do M.onUpdate(1/60)end
assert(M.report().objects==0,'An empty render plan remains safe across frames')
-- Bigger plates use the same assets, preserve plate depth and keep the pole below the top.
local originalGetter=map.getMap;local graph=map.getMap();map.getMap=function()return graph end
local edge=graph.nodes.a.links.b
edge.nlRoadContext={road_class='motorway',max_speed_kmh=40,speed_source='mapped_max_speed'}
local large={kind='ndw_small_area_preview',schema_version=1,records={{id='motorway_disc',lat=la,lon=lo,asset='C1',bearing_deg=0}}}
assert(M.previewNDW(large,'travel_bearing')==3)
local sized=M.report().outcome.locations[1]
assert(math.abs(sized.boards[1].width_m-1)<1e-8 and math.abs(sized.board_top_m-2.75)<1e-8)
assert(math.abs(sized.board_top_m-sized.pole_top_m-.05)<1e-8)
for _,o in pairs(objects)do
 if o.fields.shapeName:find('C1.dae',1,true)then assert(math.abs(o.scale.x-5/3)<1e-8 and o.scale.y==1 and o.scale.z==o.scale.x)
 else assert(o.scale.x==1 and o.scale.y==1 and o.scale.z==1,'Do not enlarge poles or clamps')end
end
edge.nlRoadContext=nil
map.getMap=originalGetter
assert(M.previewNDW(preview,'travel_bearing')==20)
preview.records[2].id=preview.records[1].id
assert(not pcall(M.previewNDW,preview,'travel_bearing'));assert(M.report().objects==20)
current.anchorId=8;M.onUpdate(1/60)
assert(M.report().objects==0 and M.report().outcome.status=='cleared_on_anchor_change')
assert(M.nearCar()==119)
vehicle.getPosition=function()return {x=500,y=500,z=1}end
M.onUpdate(1/60);assert(M.report().objects==0)
be.getSurfaceHeightBelow=function(_,p)return .01*p.x end
current=nil;vehicle.getPosition=function()return {x=0,y=0,z=1}end
assert(M.nearCar()==119 and not M.report().outcome.geographic,'Gridmap must remain explicitly non-geographic')
M.onClientEndMission();assert(M.report().objects==0)
print('PASS: 119 objects, terrain sampling, real geodesy round-trip/tile keys, replacement, bad-height rejection, benchmark state machine, report save, rebase/distance/mission cleanup and non-geographic Gridmap mode. Synthetic 60 FPS in this test is not a BeamNG measurement.')
