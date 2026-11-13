local dir=arg[0]:match('^(.*)[/\\]') or '.'
local T=dofile(dir..'/testlib.lua')
local C=require('beamngEarth/nl_signal_clearance')
local L=require('beamngEarth/nl_signal_lanes')
local P=require('beamngEarth/nl_signals')
local frame={worldToGeodetic=function()return 51.36351,6.12339,0 end}
local function fixture(n)
  local r=(n+1)*3.3/2;local a={'a',-5,0,0,1,0,0,-5,-r-1,0,0}
  return a,{nodes={a={-50,0,0,r},j={20,0,0,r}},edges={{'a','j',0,n,1,13,1,r,r}}}
end
for n=1,5 do
  local a,g=fixture(n);local lane=L.describe(g,'j',a);local p=assert(C.fit(g,a,lane))
  for _,foot in ipairs(p.feet)do T.check(C.clear(g,foot[1],foot[2]),'both roadside feet outside carriageway '..n) end
  T.check(p.left_span_m>(n+1)*3.3+1,'left foot outside full road in graph-only fixture '..n)
  local plan=P.plan(a,frame,'stock',true,g,'j')
  T.check(plan.placement and plan.layout.ready,'lane layout has a checked roadside pair '..n)
  if n<=3 and n>1 then T.check(plan.layout.kind=='cantilever','two/three heads do not force a fixed-span portal')
  elseif n>=4 then T.check(plan.layout.kind=='portal','four/five overhead heads use portal') end
  if plan.layout.kind=='cantilever' then
    local beam;for _,part in ipairs(plan.parts)do if part.role=='beam' then beam=part end end
    local longest=0;for _,m in ipairs(plan.layout.mounts)do longest=math.max(longest,-m.position[2]) end
    T.near(beam.scale[2],longest,1e-8,'beam stops at last lamp centre')
  end
end
local a,g=fixture(2)
g.nodes.c={-50,8,0,3};g.nodes.d={20,8,0,3};g.edges[#g.edges+1]={'c','d',1,2,0,13,1,3,3}
local fit=assert(C.fit(g,a,L.describe(g,'j',a)))
T.check(fit.left_span_m>15,'opposite carriageway moves left foot beyond its lanes')
for _,foot in ipairs(fit.feet)do T.check(C.clear(g,foot[1],foot[2]),'foot never stays in opposite carriageway') end
a,g=fixture(1);g.edges[1][10]={road_class='primary'}
T.check(P.plan(a,frame,'stock',true,g,'j').layout.kind=='cantilever','single-lane primary road gets overhead mast')
a[12]={turns={'through'}}
T.check(not P.plan(a,frame,'stock',true,g,'j').shape:find('arrow_straight'),'through always uses round lens')
local b={};for k,v in pairs(a)do b[k]=v end;b[2]=b[2]+2;b[8]=b[8]+2
g.signals={{j='first',kind='signal',app={a}},{j='duplicate',kind='signal',app={b}}}
local blocked=P.duplicates(g,frame);local total=0;for _ in pairs(blocked)do total=total+1 end
T.check(total==1,'coincident same-facing stop deduplicated')
b[3]=b[3]+4
T.check(next(P.duplicates(g,frame))==nil,'separate parallel lane approaches not merged')
T.done('test_nl_signal_clearance')
