local dir=arg[0]:match('^(.*)[/\\]')or'.';package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local R=require('beamngEarth/nl_replacements');local p={{x=0,y=0,fx=0,fy=-1,kind='control'}}
assert(R.hidden('giveway',0,1,0,p));assert(R.hidden('stop',0,1,0,p))
assert(not R.hidden('giveway',0,1,180,p),'Opposite approach remains')
assert(not R.hidden('giveway',20,0,0,p),'Distant sign remains')
assert(not R.hidden('deciduous',0,1,0,p),'Vegetation remains')
assert(not R.hidden('speedk30',0,1,0,p),'Different sign class remains')
assert(not R.hidden('giveway',0,1,0,{}),'Clearing restores inferred signs')
print('PASS: sourced-sign Forest suppression is local, direction/class matched, reversible and leaves vegetation alone.')
