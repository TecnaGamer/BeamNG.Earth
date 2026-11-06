local dir=arg[0]:match('^(.*)[/\\]')or'.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local C=require('beamngEarth/nl_context')
local circle={width_m=.6,height_m=.6,shape='circle'}
for _,test in ipairs({{30,.6},{50,.6},{60,.8},{80,.8},{100,1},{130,1}})do
 local p=C.dimensions(circle,nil,{speed_kmh=test[1],speed_source='mapped_max_speed'})
 assert(math.abs(p.width_m-test[2])<1e-9 and p.status=='road_context_nominal_inferred')
end
assert(C.dimensions(circle,nil,{road_class='motorway',speed_kmh=40}).width_m==1,'Motorway with temporary low speed stays type III')
assert(C.dimensions(circle,nil,{speed_kmh=100,ambiguous=true}).scale==1,'Do not guess the carriageway at crossings')
assert(C.dimensions(circle,nil,{}).status=='catalogue_fallback_not_surveyed')
local triangle={width_m=.7,height_m=.60621778,shape='triangle'}
assert(C.dimensions(triangle,nil,{speed_kmh=80}).width_m==.9)
assert(C.dimensions(triangle,nil,{speed_kmh=100}).width_m==1.1)
assert(C.dimensions({width_m=.6,height_m=.9},nil,{speed_kmh=100}).status=='special_shape_size_unresolved')
assert(math.abs(C.supplementary({width_m=.6,height_m=.2},5/3).height_m-.2*5/3)<1e-9)
local edges={{ax=-100,ay=0,bx=100,by=0,key='a',context={road_class='motorway',max_speed_kmh=100,speed_source='mapped_max_speed'},ai_speed_mps=8}}
local road=C.match(0,5,edges)
assert(road.speed_kmh==100,'Do not use AI bend speed when original road metadata is available')
assert(C.match(0,50,edges).status=='road_context_unavailable')
edges[2]={ax=0,ay=-100,bx=0,by=100,key='b'}
assert(C.match(0,0,edges).ambiguous,'Crossing is not a certain road assignment')
local planner=require('beamngEarth/nl_placement')
local oldNav=package.loaded['beamngEarth/navgraph']
package.loaded['beamngEarth/navgraph']={nlContextFor=function(a,b)return{road_class='motorway',max_speed_kmh=100}end}
local recovered=planner.fromMap({a={pos={x=0,y=0},radius=3,links={b={speedLimit=8}}},b={pos={x=100,y=0},radius=3,links={}}},10,5,180)
assert(recovered[1].context.max_speed_kmh==100,'Use applied side-index if map.reset strips custom edge fields')
package.loaded['beamngEarth/navgraph']=oldNav
local r=C.rotation(math.cos(math.rad(10)),math.sin(math.rad(10)),{ax=1,ay=0})
assert(r.status=='small_axis_alignment_inferred' and r.fx==1 and r.fy==0)
r=C.rotation(0,1,{ax=1,ay=0});assert(r.status=='large_axis_mismatch_review' and r.fy==1,'Never guess a 90-degree correction')
r=C.rotation(-1,0,{ax=1,ay=0});assert(r.fx==-1,'Never flip a source face 180 degrees merely to match an undirected road')
r=C.rotation(1,0,{ax=1,ay=0,flow_x=1,flow_y=0});assert(r.possible_reverse_facing and r.fx==1,'Flag possible wrong-way face without changing potentially intentional signs')
r=C.rotation(.99,.1,{ax=1,ay=0,ambiguous=true});assert(r.fx==.99 and r.status=='ambiguous_road_source_preserved')
local skew={ax=math.cos(math.rad(35)),ay=math.sin(math.rad(35))}
r=C.rotation(1,0,skew,90)
assert(r.status=='coarse_bearing_axis_alignment_inferred' and math.abs(r.fx-skew.ax)<1e-9 and math.abs(r.fy-skew.ay)<1e-9,'Coarse cardinal source follows an unambiguous diagonal road')
r=C.rotation(-1,0,skew,270);assert(r.fx<0 and r.fy<0,'Keep the source-facing half-plane')
r=C.rotation(1,0,skew,89);assert(r.fx==1 and r.status=='large_axis_mismatch_review','A precise noncardinal source keeps the conservative limit')
r=C.rotation(1,0,{ax=math.sqrt(.5),ay=math.sqrt(.5)},90);assert(r.fx==1,'A 45-degree quadrant tie is ambiguous')
r=C.rotation(1,0,{ax=0,ay=1},90);assert(r.fx==1,'Do not turn cardinal sources by 90 degrees')
r=C.rotation(1,0,{ax=skew.ax,ay=skew.ay,ambiguous=true},90);assert(r.fx==1,'Do not override ambiguous road assignment')
local G=require('beamngEarth/geodesy');local frame=G.newAnchor(52,5)
for _,test in ipairs({{0,0,1},{90,1,0},{180,0,-1},{270,-1,0}})do
 local x,y=C.sourceFront({northAt=function()return 0,1 end},0,0,test[1],'front_bearing')
 assert(math.abs(x-test[2])<1e-9 and math.abs(y-test[3])<1e-9,'Independent cardinal facing check')
 local a,b=C.sourceFront({northAt=function()return 0,1 end},0,0,test[1],'travel_bearing')
 assert(math.abs(a+test[2])<1e-9 and math.abs(b+test[3])<1e-9)
end
for _,bearing in ipairs({0,45,90,135,180,225,270,315})do for _,convention in ipairs({'travel_bearing','front_bearing'})do
 local fx,fy=C.sourceFront(frame,250,400,bearing,convention);local yaw=C.yawForFront(fx,fy);local h=math.rad(yaw+90)/2
 local x,y=G.beamngRotate(0,0,math.sin(h),math.cos(h),0,-1,0)
 assert(math.abs(x-fx)<1e-9 and math.abs(y-fy)<1e-9,'Actual BeamNG quaternion convention, including diagonals')
end end
print('PASS: road-category sizes, motorway low-speed exception, unknown/special-shape fallback, AI/legal speed separation, junction ambiguity, bounded axis alignment and all compass quaternion directions.')
