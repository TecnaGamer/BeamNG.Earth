local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua');dofile(dir..'/stubs.lua')
local B=require('beamngEarth/nl_signal_bgt')
local S=require('beamngEarth/nl_signal_surfaces')
local C=require('beamngEarth/nl_signal_clearance')
local L=require('beamngEarth/nl_signal_lanes')
local context=require('beamngEarth/nl_signal_context')
local originX,originY=context.mercator(51.37,6.14)
local scale=math.cos(math.rad(51.37));local ORIGIN=20037508.342789244
local frame={worldToGeodetic=function(_,x,y,z)
  local u,v=originX+x/scale,originY+y/scale
  return math.deg(2*math.atan(math.exp(v/ORIGIN*math.pi))-math.pi/2),u*180/ORIGIN,z
end,geodeticToWorld=function(_,lat,lon,z)
  local u,v=context.mercator(lat,lon);return(u-originX)*scale,(v-originY)*scale,z
end}
local function ring(x0,y0,x1,y1)
  return{{originX+x0/scale,originY+y0/scale},{originX+x1/scale,originY+y0/scale},
    {originX+x1/scale,originY+y1/scale},{originX+x0/scale,originY+y1/scale}}
end
local rows={}
local function rect(role,id,x0,y0,x1,y1,holes)
  local row={role=role,id=id,rings={ring(x0,y0,x1,y1)}}
  for _,hole in ipairs(holes or{})do row.rings[#row.rings+1]=ring(unpack(hole))end
  rows[#rows+1]=row;return row
end
B.lookup=function()return rows end
rect('pedestrian','walk',-20,-9,20,-6.17,{{-1,-8,1,-7}})
rect('carriageway','road',-20,-6.17,20,6)
rect('supporting','island',-20,6.15,20,7.1)
local q=assert(B.context(frame,0,0,10))
T.check(q.classify(0,-8.6,.22),'whole pole foot fits a sidewalk')
T.check(q.classify(0,-6.3,.22)==false,'foot overlapping BGT road is rejected even with centre on sidewalk')
T.check(q.classify(0,-7.5,.22)==nil,'polygon holes never count as safe sidewalk')
T.check(q.classify(0,-8.1,.22)==nil,'complete foot must clear a polygon hole')
T.check(q.classify(0,6.6,.22),'a narrow median supports a complete foot')
T.check(q.classify(0,6.2,.22)==false,'median tip still must clear carriageway')
T.check(q.classify(0,100,.22)==nil,'municipality coverage is not blanket permission')
for _,role in ipairs({'cycleway','parking','access','unknown'})do
  local row=rect(role,role,-2,-8.9,2,-8.3)
  T.check(q.classify(0,-8.6,.22)==false,role..' cannot support a pole despite a coincident sidewalk')
  table.remove(rows)
end
local candidates=q.candidates(0,-5,15)
T.check(#candidates>0 and #candidates<=96,'bounded candidates follow BGT edges')
local useful=false
for _,p in ipairs(candidates)do
  T.check(q.classify(p[1],p[2],.22),'suggested candidate clears holes and forbidden areas')
  if p[2]<-6.4 and p[2]>-6.7 then useful=true end
end
T.check(useful,'irregular curb produces a precise candidate outside fixed half metre offsets')
local function mesh(x0,y0,x1,y1,z)
  local triangles={{x0,y0,z,x1,y0,z,x1,y1,z},{x0,y0,z,x1,y1,z,x0,y1,z}};local grid={}
  for x=math.floor(x0/8),math.floor(x1/8)do for y=math.floor(y0/8),math.floor(y1/8)do grid[x..','..y]=triangles end end
  return{grid=grid}
end
local mixed={kind='road',mesh=mesh(-20,-9,20,9,10.2),bgt={object_ids={'walk','road','island'}}}
S.provider=function()return{mixed},true end
core_terrain={getTerrainHeight=function()return 10 end}
castRayStatic=function()return .3 end
local probe=S.begin(0,0,10,frame)
local ok,z,kind=probe(0,-8.6,10)
T.check(ok and kind=='walk','matching BGT sidewalk is usable inside batched road mesh')
T.near(z,10.185,1e-5,'height comes from actual drawn mesh, never from BGT plan geometry')
local shifted,grounded=S.begin(0,0,11.5,frame)(0,-8.6,11.5)
T.check(shifted and math.abs(grounded-10.185)<1e-5,'old floating source height cannot hide an otherwise valid BGT sidewalk')
local allowed,_,why=probe(0,0,10)
T.check(not allowed and why=='bgt_carriageway','BGT road veto survives generic native mesh/material')
T.check(not probe(0,-7.5,10),'a hole in the semantic sidewalk still cannot use the mixed road mesh')
mixed.bgt={object_ids={'different_source'}}
T.check(not S.begin(0,0,10,frame)(0,-8.6,10),'unmatched source IDs cannot turn native asphalt into sidewalk')
mixed.bgt=nil
T.check(not S.begin(0,0,10,frame)(0,-8.6,10),'non-BGT renderer retains original safety checks')
mixed.kind='walk';mixed.mesh=mesh(-20,-9,20,9,15)
T.check(S.begin(0,0,15,frame)(0,0,15),'ground BGT road does not veto a correctly measured elevated sidewalk')
mixed.kind='road';mixed.mesh=mesh(-20,-9,20,9,10.2);mixed.bgt={object_ids={'walk','road','island'}}
mixed.pending=true
T.check(not S.begin(0,0,10,frame)(0,-8.6,10),'BGT cannot bypass pending visible geometry')
mixed.pending=nil
castRayStatic=function()return 1 end
T.check(not S.begin(0,0,10,frame)(0,-8.6,10),'BGT cannot bypass physical support/height mismatch')
castRayStatic=function()return .3 end
-- Whole approach fit retains lane descriptions and uses the median before the
-- far sidewalk; geometry metadata alone never creates signal controllers.
rows={};rect('carriageway','road',-100,-6.17,100,.1);rect('pedestrian','walk',-100,-9,100,-6.17)
rect('supporting','island',-100,.35,100,1.5)
mixed.mesh=mesh(-100,-9,100,9,10.2)
local a={'a',0,0,10,1,0,0,0,-4.3,10.5,0}
local g={nodes={a={-60,0,10,3.3},j={30,0,10,3.3}},edges={{'a','j',0,1,1,13,1,3.3,3.3}}}
local lane=L.describe(g,'j',a)
local fit=assert(C.fit(g,a,lane,frame))
T.check(fit.foot_source=='BGT_pedestrian','BGT boundary candidates participate in the real placement search')
T.check(fit.y< -6.4 and fit.y> -6.9,'right mast follows precise sidewalk boundary')
T.check(fit.left_span_m and fit.feet[2][2]>.55 and fit.feet[2][2]<1.3,'left mast prefers actual BGT median')
T.check(#lane.heads==1,'BGT does not invent lanes or add primary lanterns')
B.lookup=function()return{}end
T.check(B.context(frame,0,0,10)==nil,'missing local BGT retains native fallback')
B.lookup=nil;B.reset();S.provider=nil;core_terrain=nil;castRayStatic=nil
-- Decode the actual shipped pack (not just hand-authored fixtures).
local idx=require('beamngEarth/nl_groenpol_bgt_index')
local root=dir..'/../beamng_earth'
local bytes=assert(context.readBinary(root..idx.data))
local read=B.new(idx,bytes);local mx,my=context.mercator(51.36791,6.16357)
local actual=read(mx,my,100)
T.check(#actual>0,'real Venlo BGT polygons cover the BGT test junction')
local roles={};for _,r in ipairs(actual)do roles[r.role]=true end
T.check(roles.carriageway and roles.pedestrian,'real pack distinguishes road from sidewalk')
T.check(not pcall(B.new,idx,'bad'),'invalid optional context is rejected')
T.done('BGT signal placement')
