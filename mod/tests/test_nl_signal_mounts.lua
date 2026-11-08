local dir=arg[0]:match('^(.*)[/\\]') or '.'
local T=dofile(dir..'/testlib.lua')
local M=require('beamngEarth/nl_signal_mounts')
local function heads(n)local a={};for i=1,n do a[i]={id='lane_'..i,lane_id='lane_'..i,distance_m=(n-i)*3.3+2.65} end;return a end
local function byKey(p,key)for _,part in ipairs(p.parts)do if part.key==key then return part end end end
local one=M.plan({{id='primary'}},{kind='straight',left_span_m=8.6})
T.check(one.ready and #one.parts==4 and one.total_heads==2,'single lane: right and left standing assembly')
local cap=M.catalog.supports.straight.mounts[1].position[3]
T.near(byKey(one,'head_primary').position[3],cap,1e-9,'standing cap height')
for n=1,10 do
  local reach=(n-1)*3.3+2.65;local span=reach+4
  local p=M.plan(heads(n),{arm_length_m=reach,left_span_m=span})
  T.check(p.ready and p.kind==(n<=3 and 'cantilever' or 'portal'),'construction for '..n..' overhead heads')
  T.check(p.head_count==n and p.total_heads==n+2,'requested overhead lamps plus two low repeaters')
  T.check(p.length_limit_configured and M.MAX_CANTILEVER_LENGTH_M==12,'explicit interim 12 m threshold')
  T.near(byKey(p,'head_right_repeat').position[3],cap,1e-5,'low right repeat at standing height')
  T.near(byKey(p,'head_left').position[3],cap,1e-5,'left repeat at standing height')
  T.near(byKey(p,'support_left').position[2],-span,1e-9,'left foot follows measured road span')
  local beam=byKey(p,'beam')
  T.near(beam.scale[2],p.kind=='portal' and span or reach,1e-9,'independent beam length')
  for _,mount in ipairs(p.mounts)do
    local head,hardware=byKey(p,'head_'..mount.head_id),byKey(p,'bracket_'..mount.head_id)
    T.check(head.position==hardware.position and head.basis==hardware.basis,'hanger and head share exact mounting frame')
    T.check(head.lane_id==mount.lane_id,'lane reference retained')
    local b=head.basis;for i=1,3 do for j=i,3 do
      T.near(b[i]*b[j]+b[i+3]*b[j+3]+b[i+6]*b[j+6],i==j and 1 or 0,1e-9,'rigid mount frame')
    end end
  end
end
T.check(M.supportKind(3,11.9,12)=='cantilever','under 12 m stays cantilever')
T.check(M.supportKind(3,12,12)=='portal','12 m reaches portal threshold')
T.check(M.supportKind(4,1,12)=='portal','four overhead heads always portal')
M.MAX_CANTILEVER_HEADS=99;T.check(M.supportKind(4,1,12)=='portal','hard overhead head limit');M.MAX_CANTILEVER_HEADS=3
T.check(M.plan(heads(1),{arm_length_m=11,left_span_m=15}).kind=='cantilever','11 m does not need obsolete fixed-span portal')
T.check(not M.plan(heads(1),{arm_length_m=100,left_span_m=20}).ready,'unsupported reach rejected')
T.check(M.plan(heads(6)).ready,'six overhead heads use the procedural portal')
T.check(M.plan(heads(10),{left_span_m=37}).ready,'ten overhead heads reuse source sockets within a verified span')
T.check(not M.plan(heads(12),{left_span_m=42}).ready,'procedural head count does not bypass the span bound')
local h=heads(3);h[2].distance_m=h[1].distance_m
T.check(M.plan(h).reason=='overlapping_heads','overlapping armatures rejected')
for _,n in ipairs({-1,math.huge,0/0})do T.check(not M.plan(heads(1),{arm_length_m=n}).ready,'invalid width rejected')end
local pose=M.worldPose({x=10,y=20,z=30,yaw=90},{position={1,2,3},basis={1,0,0,0,1,0,0,0,1}})
T.near(pose.x,8,1e-9,'world yaw X');T.near(pose.y,21,1e-9,'world yaw Y');T.near(pose.z,33,1e-9,'height')
local feet={right={position={0,0,0},edge_distance_m=1},left={position={4,-20,.6},edge_distance_m=2.5}}
local split=assert(M.alternatives(heads(5),feet)[1])
T.check(split.kind=='paired_cantilevers'and split.head_count==5,'five lanes can use independent mirrored masts')
local lanes,steel={},0
for _,p in ipairs(split.parts)do
  if p.role=='head'and not p.repeat_head then
    T.check(not lanes[p.lane_id],'exactly one main lantern per lane');lanes[p.lane_id]=true
    T.check(p.basis[1]>.99,'mirrored steel does not reverse the heads or arrows')
  elseif p.role=='beam'then
    steel=steel+1;T.check(p.scale[2]<12,'each independent beam stays under length limit')
    if p.key:find('left')then T.check(p.basis[5]==-1 and p.position[1]==4,'left steel points inward at its independently checked foot')end
  end
end
T.check(steel==2 and split.arms[1].heads<=3 and split.arms[2].heads<=3,'at most three overhead lamps per mast')
feet.left={position={2,-14,.3},edge_distance_m=1.45}
local hybrid=assert(M.alternatives(heads(4),feet)[1])
T.check(hybrid.kind=='cantilever_and_pole'and hybrid.overhead_count==3 and #hybrid.mounts==4,'four lanes can use three overhead and one standing head')
local space=require('beamngEarth/nl_signal_space')
local first=M.plan(heads(3),{arm_length_m=9.25,left_span_m=14})
first.x,first.y,first.z,first.yaw,first.model=0,0,0,0,'groenpol_vehicle'
local second=M.plan(heads(3),{arm_length_m=9.25,left_span_m=14})
second.x,second.y,second.z,second.yaw,second.model=0,.2,0,0,'groenpol_vehicle'
T.check(space.conflict(second,{first=first})=='first','overlapping separate assemblies are detected')
second.z=10;T.check(not space.conflict(second,{first=first}),'different road levels do not block each other')
T.done('test_nl_signal_mounts')
