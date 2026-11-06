-- Opt-in preview. George is precompiled offline; the game reads only nearby local tiles.
local M={}
local active,index,convention,signature,elapsed=false,nil,nil,nil,0
local store,tileCache=nil,{}
local lastObjects=0
local placementRetry=false
local sincePlacement=0
local outcome={status='not_started'}
local function renderer()return assert(extensions.beamngEarth_nltestset,'Load beamngEarth_nltestset first')end
local function coreState()local c=extensions.beamngEarth_core;return c and c._state and c._state()end
local function validRow(row)
  assert(type(row.id)=='string' and row.id~='','Missing source ID')
  assert(type(row.lat)=='number' and row.lat>=50 and row.lat<=54 and type(row.lon)=='number' and row.lon>=3 and row.lon<=8,'Invalid WGS84 point')
  assert(type(row.bearing_deg)=='number' and row.bearing_deg>=0 and row.bearing_deg<360,'Invalid source bearing')
end
local function validConvention(value)assert(value=='travel_bearing' or value=='front_bearing','Confirm bearing convention')end
function M.stop()
  if active then renderer().clear()end
  lastObjects=0;placementRetry=false;sincePlacement=0;active=false;index=nil;store=nil;tileCache={};signature=nil;elapsed=0;outcome={status='stopped'}
end
function M.start(filename,bearingConvention)
  validConvention(bearingConvention)
  local data=assert(jsonReadFile(filename),'NDW index not found')
  assert(data.schema_version==1 and data.kind=='ndw_small_area_preview' and type(data.records)=='table' and #data.records<=40,'Use a bounded pilot index')
  local seen={};for _,row in ipairs(data.records)do validRow(row);assert(not seen[row.id],'Duplicate ID');seen[row.id]=true end
  renderer();M.stop();renderer().clear();index=data;convention=bearingConvention;active=true;elapsed=1
  outcome={status='waiting_for_earth',source_records=#data.records,stream_radius_m=120}
end
function M.startStore(filename,bearingConvention)
  validConvention(bearingConvention)
  local manifest=assert(jsonReadFile(filename),'Missing completed George manifest')
  assert(manifest.schema_version==1 and manifest.kind=='george_v5_tile_store' and manifest.zoom==17 and manifest.runtime_file_pattern=='render/g17_{x}_{y}.json','Unsupported George tile store')
  local root=assert(filename:match('^(.*[/])[^/]+$'),'Use a full VFS manifest path')
  local geotile=require('beamngEarth/geotile')
  renderer();M.stop();renderer().clear()
  store={root=root,geotile=geotile};index={kind='ndw_small_area_preview',records={}}
  convention=bearingConvention;active=true;elapsed=1
  outcome={status='waiting_for_earth',source_version=5,stream_radius_m=120,max_visible_signs=40,tile_cache_limit=9}
end
function M.startNetherlands(bearingConvention)
  return M.startStore('/levels/beamng_earth/data/george_nederland/manifest.json',bearingConvention or 'travel_bearing')
end
function M.startRemote(provider,bearingConvention)
  validConvention(bearingConvention)
  assert(type(provider)=='table' and type(provider.neighborhood)=='function' and type(provider.get)=='function','Invalid remote tile provider')
  renderer();M.stop();renderer().clear()
  store={provider=provider,geotile=require('beamngEarth/geotile')};index={kind='ndw_small_area_preview',records={}}
  convention=bearingConvention;active=true;elapsed=1
  outcome={status='waiting_for_server_tiles',source_version=5,transport='companion',stream_radius_m=120,max_visible_signs=40,tile_cache_limit=9}
end
local function nearby(st,p)
  local lat,lon=st.anchor:worldToGeodetic(p.x,p.y,p.z)
  local tx,ty=store.geotile.tileOf(lat,lon);local keep,records={},{}
  if store.provider then
    local keys={};for x=tx-1,tx+1 do for y=ty-1,ty+1 do keys[#keys+1]=store.geotile.key(x,y)end end
    store.provider.neighborhood(tx,ty,keys)
    for _,key in ipairs(keys)do local data=store.provider.get(key);if data then for _,r in ipairs(data.records)do records[#records+1]=r end end end
    outcome.cached_tiles=store.provider.count and store.provider.count() or 0
    return records
  end
  for x=tx-1,tx+1 do for y=ty-1,ty+1 do
    local key=store.geotile.key(x,y);local data=tileCache[key]
    if data==nil then
      local file=store.root..'render/'..key..'.json'
      if FS:fileExists(file)then
        data=assert(jsonReadFile(file),'Unreadable George render tile')
        assert(data.schema_version==1 and data.kind=='george_v5_render_tile' and data.key==key and type(data.records)=='table' and #data.records<=20000,'Invalid George render tile')
        for _,r in ipairs(data.records)do validRow(r);local rx,ry=store.geotile.tileOf(r.lat,r.lon);assert(store.geotile.key(rx,ry)==key,'Record in wrong geographic tile')end
      else data=false end
    end
    keep[key]=data
    if data then for _,r in ipairs(data.records)do records[#records+1]=r end end
  end end
  tileCache=keep;outcome.cached_tiles=9
  return records
end
function M.onUpdate(dt)
  if not active then return end
  local st=coreState();local r=renderer()
  if not(st and st.active and st.anchor)or st.pending then
    if signature then r.clear()end;signature=nil;outcome.status='waiting_for_earth';return
  end
  sincePlacement=sincePlacement+(type(dt)=='number' and dt>0 and dt or 0)
  elapsed=elapsed+(type(dt)=='number' and dt>0 and dt or 0)
  if elapsed<1 then return end;elapsed=0
  local car=getPlayerVehicle and getPlayerVehicle(0)
  if not car then r.clear();signature=nil;outcome.status='waiting_for_vehicle';return end
  local p=car:getPosition()
  if store then
    local ok,rows=pcall(nearby,st,p)
    if not ok then r.clear();signature=nil;outcome.status='tile_read_failed';outcome.error=tostring(rows);return end
    index.records=rows
  end
  local subset={schema_version=1,kind=index.kind,records={}};local distances={}
  local seen={}
  for _,row in ipairs(index.records)do
    if seen[row.id]then r.clear();signature=nil;outcome.status='duplicate_tile_id';return end;seen[row.id]=true
    local x,y=st.anchor:geodeticToWorld(row.lat,row.lon,0);local d=(x-p.x)^2+(y-p.y)^2
    if d<=120^2 then subset.records[#subset.records+1]=row;distances[row.id]=d end
  end
  table.sort(subset.records,function(a,b)local ad,bd=distances[a.id],distances[b.id];return ad<bd or(ad==bd and a.id<b.id)end)
  outcome.candidates_in_radius=#subset.records;while #subset.records>40 do table.remove(subset.records)end
  table.sort(subset.records,function(a,b)return a.id<b.id end)
  local expectedObjects=0;for _,row in ipairs(subset.records)do expectedObjects=expectedObjects+3+2*#(row.supplementary_assets or {}) end
  local nav=package.loaded['beamngEarth/navgraph']
  local parts={tostring(st.anchorId),tostring(nav and nav.nlRevision or 0)};for _,row in ipairs(subset.records)do parts[#parts+1]=row.id end
  local nextSignature=table.concat(parts,'|')
  if #subset.records==0 then
    if signature then r.clear()end;signature=nil;outcome.status='outside_pilot_area';outcome.loaded_records=0;return
  end
  if nextSignature==signature and r.report().objects==lastObjects and not(placementRetry and sincePlacement>=5)then return end
  sincePlacement=0
  local ok,result=pcall(r.previewNDW,subset,convention)
  if ok then lastObjects=result;signature=result>0 and nextSignature or nil;outcome.status=result>0 and 'streaming_preview' or 'placement_blocked';local rr=r.report().outcome;placementRetry=rr and rr.skipped and #rr.skipped>0 or false;local loaded=0;if rr and rr.locations then for _,loc in ipairs(rr.locations)do loaded=loaded+(loc.source_ids and #loc.source_ids or 1)end else loaded=#subset.records end;outcome.loaded_records=result>0 and loaded or 0;outcome.error=nil
  else r.clear();signature=nil;outcome.status='placement_failed';outcome.error=tostring(result)end
end
function M.report()return {active=active,outcome=outcome,render=renderer().report()}end
function M.onClientEndMission()M.stop()end
function M.onExtensionUnloaded()M.stop()end
return M
