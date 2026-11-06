local dir=arg[0]:match('^(.*)[/\\]')or'.';package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local P=require('beamngEarth/nl_placement')
local edges={{ax=-100,ay=0,bx=100,by=0,half=5}}
local a=P.roadside(0,0,edges,90);assert(a and a.y<0 and P.clearOfRoad(a.x,a.y,edges))
local b=P.roadside(5,3,edges,90);assert(b and b.y>0 and b.shift_m<=15)
local c=P.roadside(5,12,edges,90);assert(c.shift_m==0)
assert(not P.roadside(0,0,{},0),'No road data means no unsafe spawn')
assert(not P.roadside(0,100,edges,0),'Do not move unrelated off-road objects toward a road')
local crossing={{ax=-100,ay=0,bx=100,by=0,half=5},{ax=0,ay=-100,bx=0,by=100,half=5}}
assert(not P.roadside(0,0,crossing,90),'Candidate must clear every intersecting carriageway')
local rows={{id='zone',x=1,y=1,asset='A2-30-ZE',bearing_deg=0},{id='yield',x=1.1,y=1,asset='B6',bearing_deg=0},{id='back',x=1,y=1,asset='A1-30-ZB',bearing_deg=180},{id='perpendicular',x=1,y=1,asset='C1',bearing_deg=90},{id='far',x=10,y=1,asset='C1',bearing_deg=0}}
local groups=P.group(rows);assert(#groups==3)
local shared;for _,g in ipairs(groups)do if #g.rows==3 then shared=g end end
assert(shared and shared.rows[1].id=='yield','Generator priority puts yield ahead of speed/zone')
local custom=P.group({{id='a',x=0,y=0,asset='B6',bearing_deg=0,mount_order=2},{id='b',x=0,y=0,asset='A1-30-ZB',bearing_deg=0,mount_order=1}});assert(custom[1].rows[1].id=='b')
print('PASS: road-corridor exclusion, preferred side, crossings, bounded shift, fail-closed missing data, shared/opposite faces, perpendicular separation and explicit mounting order.')
