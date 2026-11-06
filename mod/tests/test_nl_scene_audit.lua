local dir=arg[0]:match('^(.*)[/\\]') or '.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local base='/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/'
local fixture={objects=1,outcome={status='spawned',locations={{id='p',lat=1,lon=2,height_m=3}}}}
extensions={beamngEarth_nltestset={report=function()return fixture end},beamngEarth_core={_state=function()return {anchorId=4,anchor={geodeticToWorld=function()return 1,2,3 end}}end}}
jsonReadFile=function()return {assemblies={{id='p',pole_offset_m=-.1}},poles={{model='pole.dae'}}}end
vec3=function(x,y,z)return{x=x,y=y,z=z}end
be={getSurfaceHeightBelow=function()return 5 end}
local pole={getField=function()return base..'pole.dae'end,getPosition=function()return{x=1,y=2,z=2.9}end}
local plate={getField=function()return '/levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/cat_test.dae'end}
local names={'pole','orphanPlate'}
scenetree={findClassObjects=function()return names end,findObject=function(name)if name=='bngEarthTerrain'then return{getHeight=function()return 3 end}end;return name=='pole'and pole or plate end}
local audit=require('beamngEarth/nlcheck')
local r=audit.snapshot()
assert(r.actual_objects==2 and r.internal_objects==1 and not r.counts_agree,'Detect orphan scene objects independently')
assert(r.posts==1 and r.plates==1 and #r.ground_checks==1)
assert(r.max_placement_error_m<1e-12 and r.max_terrain_gap_m==0 and r.max_surface_drift_m==2)
names={};fixture.objects=0;fixture.outcome.locations={};r=audit.snapshot();assert(r.actual_objects==0 and r.counts_agree)
print('PASS: independent scene count detects orphan objects, validates ground diagnostics and empty scene.')
