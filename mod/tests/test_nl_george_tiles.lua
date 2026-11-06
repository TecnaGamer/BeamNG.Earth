local dir=arg[0]:match('^(.*)[/\\]')or'.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
math.atan2=math.atan2 or function(y,x)return math.atan(y,x)end
math.sinh=math.sinh or function(x)return(math.exp(x)-math.exp(-x))/2 end
local geo=require('beamngEarth/geodesy');local tiles=require('beamngEarth/geotile')
local frame=geo.newAnchor(51.36711,6.14178)
local st={active=true,anchorId=1,anchor=frame};local p={x=0,y=0,z=0}
local records={};for i=1,55 do records[i]={id=string.format('id_%03d',i),lat=51.36711,lon=6.14178+i*.000001,bearing_deg=0,asset='B6'}end
local tx,ty=tiles.tileOf(records[1].lat,records[1].lon);local key=tiles.key(tx,ty)
local data={schema_version=1,kind='george_v5_render_tile',key=key,records=records}
local fsReads,jsonReads,spawns,count=0,0,0,0
FS={fileExists=function(_,file)fsReads=fsReads+1;return file=='/george/render/'..key..'.json'end}
jsonReadFile=function(file)jsonReads=jsonReads+1;if file=='/george/manifest.json'then return{schema_version=1,kind='george_v5_tile_store',zoom=17,runtime_file_pattern='render/g17_{x}_{y}.json'}end;return data end
local r={clear=function()count=0 end,report=function()return{objects=count}end,previewNDW=function(subset)spawns=spawns+1;count=#subset.records*3;assert(#subset.records==40);assert(subset.records[1].id=='id_001' and subset.records[40].id=='id_040');return count end}
extensions={beamngEarth_nltestset=r,beamngEarth_core={_state=function()return st end}}
getPlayerVehicle=function()return{getPosition=function()return p end}end
local M=require('beamngEarth/nlndw')
M.startStore('/george/manifest.json','travel_bearing');M.onUpdate(1)
assert(count==120 and fsReads==9 and jsonReads==2 and spawns==1)
assert(M.report().outcome.candidates_in_radius==55)
M.onUpdate(1);assert(fsReads==9 and jsonReads==2 and spawns==1,'Unchanged neighborhood must not reread files or rebuild objects')
st.pending={};M.onUpdate(.01);assert(count==0)
st.pending=nil;st.anchorId=2;M.onUpdate(1);assert(spawns==2 and jsonReads==2,'Reanchor uses geographic cached data')
p.x=600;M.onUpdate(1);assert(count==0 and M.report().outcome.status=='outside_pilot_area');assert(M.report().outcome.cached_tiles==9)
M.onClientEndMission();assert(not M.report().active)
jsonReadFile=function(file)
 assert(file=='/levels/beamng_earth/data/george_nederland/manifest.json','National shortcut must select the complete country store')
 return{schema_version=1,kind='george_v5_tile_store',zoom=17,runtime_file_pattern='render/g17_{x}_{y}.json'}
end
M.startNetherlands();assert(M.report().active);M.stop()
-- The same selection/placement driver also works without any country files.
p.x=0
FS.fileExists=function()error('Remote mode must never inspect local country files')end
jsonReadFile=function()error('Remote mode must never read local country files')end
local requests=0
M.startRemote({neighborhood=function(x,y,keys)requests=requests+1;assert(#keys==9)end,get=function(k)return k==key and data or nil end,count=function()return 1 end},'travel_bearing')
M.onUpdate(1);assert(count==120 and requests==1 and M.report().outcome.transport=='companion')
M.stop();assert(count==0)
print('PASS: actual project geotiles/geodesy, 3x3 local tile reads, negative caching, nearest 40 limit, no unchanged-file reads, pending teleport cleanup, cache across rebase and neighborhood eviction.')
