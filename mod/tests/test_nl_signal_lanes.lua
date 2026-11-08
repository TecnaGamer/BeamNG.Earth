local dir=arg[0]:match('^(.*)[/\\]') or '.'
local T=dofile(dir..'/testlib.lua')
local L=require('beamngEarth/nl_signal_lanes')
local P=require('beamngEarth/nl_signals')
local function fixture(n,reverse)
  local radius=(n+1)*3.3/2
  local a={'a',-5,1.65,0,1,0,0,0,-radius-1,0,0}
  local e=reverse and {'j','a',0,1,n,13,1,radius,radius} or {'a','j',0,n,1,13,1,radius,radius}
  return a,{nodes={a={-50,0,0},j={20,0,0}},edges={e},signals={{j='j',kind='signal',app={a}}}}
end
local frame={worldToGeodetic=function()return 51.36351,6.12339,0 end}
for n=1,5 do
  for _,reverse in ipairs({false,true})do
    local a,g=fixture(n,reverse);local l=L.describe(g,'j',a)
    T.check(l.count==n and l.source=='graph_incoming_lanes','directional lane count '..n)
    T.near(l.lane_width_m,3.3,1e-9,'graph lane width')
    local p=P.plan(a,frame,'stock',true,g,'j')
    T.check(p.model=='groenpol_vehicle' and p.layout.head_count==n,'one physical head per incoming lane '..n)
    T.check(p.layout.kind==(n==1 and 'straight' or n<=3 and 'cantilever' or 'portal'),'automatic support '..n)
    if n>1 then for k=2,n do
      T.near(p.layout.mounts[k-1].position[2]-p.layout.mounts[k].position[2],-3.3,1e-9,'heads over adjacent lane centres')
    end end
  end
end
local a,g=fixture(3)
a[12]={turns={'left','through','right'}}
local p=P.plan(a,frame,'stock',true,g,'j');local shapes={}
for _,part in ipairs(p.parts)do if part.role=='head' then shapes[#shapes+1]=part.shape end end
T.check(shapes[1]:find('arrow_left') and not shapes[2]:find('arrow') and shapes[3]:find('arrow_right'),'mapped left/right arrows and full through lens')
T.check(L.variant('left;through')=='vehicle' and L.variant('merge_to_right')=='vehicle','mixed and unsupported arrows stay circular')
a[12]={turns={'left'}}
T.check(L.describe(g,'j',a).heads[1].variant=='vehicle','mismatched metadata cannot label the wrong lane')
g.edges[1][3]=1
T.check(L.describe(g,'j',a).count==3,'one-way incoming uses all forward lanes')
g.edges[1][1],g.edges[1][2]='j','a'
T.check(L.describe(g,'j',a).source=='incoming_edge_unavailable','one-way outgoing is not an incoming lane')
a,g=fixture(2);g.edges[2]=g.edges[1]
T.check(L.describe(g,'j',a).source=='ambiguous_graph_edge','ambiguous connection is reported')
a,g=fixture(2);g.edges[1][4]=0/0
T.check(L.describe(g,'j',a).source=='incoming_edge_unavailable','invalid count never creates extra heads')
for _,zone in ipairs({'bibeko','bubeko'})do
  local a,g=fixture(3);a[12]={zone=zone,turns={'left','through','right'}}
  local p=P.plan(a,frame,'stock',true,g,'j');local diameter=zone=='bibeko' and 200 or 300
  T.check(p.lane_plan.lens_diameter_mm==diameter,'zone selects nominal lens diameter '..zone)
  for _,part in ipairs(p.parts)do if part.role=='head' then
    T.check((part.shape:find('vehicle_300')~=nil)==(diameter==300),'correct source housing for '..zone)
  end end
  a,g=fixture(1);a[12]={zone=zone,turns={'right'}}
  p=P.plan(a,frame,'stock',true,g,'j')
  T.check(p.layout.head_count==1 and p.shape:find('arrow_right'),'single mast uses correct arrow too')
  T.check((p.shape:find('vehicle_300')~=nil)==(diameter==300),'single mast uses correct diameter')
end
T.done('test_nl_signal_lanes')
