local dir=arg[0]:match('^(.*)[/\\]') or '.'
local T=dofile(dir..'/testlib.lua')
local O=require('beamngEarth/nl_signal_osm')
local frame={geodeticToWorld=function(_,lat,lon,z)return (lon-6.14)*100000,(lat-51.37)*100000,z end,
  worldToGeodetic=function(_,x,y,z)return 51.37+y/100000,6.14+x/100000,z end}
local function point(x,y,id)return{id=id or '123',lat=51.37+y/100000,lon=6.14+x/100000,road_class='secondary'}end
local function junction()
  return {nodes={j={0,0,20,3},a={-60,0,20,3},b={60,0,20,3},c={0,-60,20,3},d={0,60,20,3}},
    edges={{'a','j',0,1,1,13,1,3,3},{'j','b',0,1,1,13,1,3,3},{'c','j',1,2,0,13,1,3,3},{'j','d',1,2,0,13,1,3,3}},signals={}}
end
local g=junction();local enriched,r=O.augment(g,frame,{point(-15,0)})
T.check(#enriched.signals==1 and r.added_junctions==1,'explicit OSM vehicle signal activates missing junction')
T.check(#enriched.signals[1].app==3,'only incoming car approaches; outgoing one-way omitted')
T.check(#g.signals==0 and #g.edges==4,'original graph remains unchanged')
local phases={};for _,a in ipairs(enriched.signals[1].app)do phases[a[7]]=true end
T.check(phases[0] and phases[1],'perpendicular approach phases use existing axis rule')
local again,rr=O.augment(enriched,frame,{point(-15,0),point(15,0,'456')})
T.check(#again.signals==1 and #again.signals[1].app==3 and rr.added_junctions==0,'multiple physical OSM nodes do not duplicate the intersection')
g=junction();g.signals={{j='j',kind='stop',app={}}}
enriched,r=O.augment(g,frame,{point(-15,0)})
T.check(enriched.signals[1].kind=='signal' and r.upgraded_stop_junctions==1,'explicit OSM signal replaces inferred stop control')
g={nodes={a={-100,0,20,3},b={100,0,22,3}},edges={{'a','b',0,1,1,13,1,3,3}},signals={}}
enriched,r=O.augment(g,frame,{point(0,0)})
T.check(#enriched.edges==2 and enriched.nodes.osm_signal_123~=nil,'midblock car signal splits existing road at mapped location')
T.near(enriched.nodes.osm_signal_123[3],21,1e-7,'split interpolates existing road height')
T.check(#enriched.signals==1 and #enriched.signals[1].app==2,'midblock vehicle crossing controls both incoming directions only')
T.check(#g.edges==1 and not g.nodes.osm_signal_123,'midblock split never alters cached source')
local reused,repeatReport=O.augment(enriched,frame,{point(5,0,'other_pole')})
T.check(#reused.edges==2 and #reused.signals==1 and repeatReport.added_junctions==0,'opposite pole at same midblock crossing reuses existing control')
enriched,r=O.augment(g,frame,{point(0,50)})
T.check(#enriched.signals==0 and r.unmatched==1,'point not associated with car graph stays unresolved')
g=junction();g.signals={{j='j',kind='signal',app={{'a'}}}}
enriched,r=O.augment(g,frame,{})
T.check(#enriched.signals==0 and r.removed_unconfirmed_junctions==1,'inferred VRI without an explicit OSM car signal is removed')
enriched,r=O.augment(g,frame,{point(-15,0)})
T.check(#enriched.signals==1 and enriched.signals[1].osm_confirmed,'explicit OSM point verifies existing VRI')
g=junction();g.signals={{j='j',kind='signal',app={{'a',-5,1,20,1,0,1}}}}
enriched,r=O.augment(g,frame,{point(-15,0)})
local fixed;for _,a in ipairs(enriched.signals[1].app)do if a[1]=='a'then fixed=a end end
T.check(fixed and type(fixed[8])=='number'and type(fixed[11])=='number'and r.completed_approaches==1,'native seven-field approach receives its missing visible placement')
T.check(fixed[2]==-5 and fixed[3]==1 and fixed[7]==1,'backfill retains native stop point and phase')
T.near(fixed[8]-fixed[2],-.5,1e-7,'backfill foot keeps its longitudinal offset relative to native stop')
T.check(g.signals[1].app[1][8]==nil,'cached source approach stays unchanged')
again,rr=O.augment(enriched,frame,{point(-15,0)})
T.check(rr.completed_approaches==0 and #again.signals[1].app==#enriched.signals[1].app,'completed approach is stable and does not duplicate on retry')
T.done('test_nl_signal_osm')
