-- Companion transport only. No country files, image downloads or HTTP in game.
local M={}
M.RVV_ONLY=true
local legacyPolicy=false
function M.rvvOnly()return legacyPolicy end
local country=require('beamngEarth/nl_country')
local function inNetherlands(st)
  local p=st.player
  if not p or st.pending then return false end
  return country.contains(p.lat,p.lon)
end
local function applyLegacyPolicy(on)
  local roads=package.loaded['beamngEarth/roads'];local trees=package.loaded['beamngEarth/trees']
  if roads and roads.setLegacySignsSuppressed then roads.setLegacySignsSuppressed(on)end
  if trees and trees.setLegacyTrafficSignsSuppressed then trees.setLegacyTrafficSignsSuppressed(on)end
  if legacyPolicy~=on then
    legacyPolicy=on
    local net=package.loaded['beamngEarth/net']
    if net and net.isConnected and net.isConnected()then net.send({type='rvv_only',on=on})end
  end
end
local revision,active,driver=nil,false,nil
local cache,wanted={},{}
local pending,serial,clock,retryAt=nil,0,0,0
local startAt=0
local anchor,wasPending=nil,false
local outcome={status='unavailable'}
local function clearCache()cache={};wanted={};pending=nil;retryAt=0 end
local function stop()
  if active and driver then pcall(driver.stop)end
  active=false;clearCache();anchor=nil;wasPending=false
end
function M.session(msg)
  stop()
  startAt=0
  revision=msg and msg.nlSigns==1 and type(msg.nlSignsRevision)=='string' and msg.nlSignsRevision or nil
  outcome={status=revision and 'waiting_for_earth' or 'unavailable'}
end
local function integer(n)return type(n)=='number' and n==math.floor(n) and math.abs(n)<2^53 end
local function decode(msg)
  assert(msg.v==1 and msg.revision==revision and type(msg.assets)=='table' and #msg.assets<=1024,'Unsupported sign payload')
  local assets={}
  for i,a in ipairs(msg.assets)do assert(type(a)=='string' and #a>0 and #a<=128,'Invalid asset');assets[i]=a end
  local function asset(i)assert(integer(i) and assets[i],'Invalid asset reference');return assets[i]end
  assert(type(msg.tiles)=='table' and #msg.tiles==#pending.keys,'Incomplete tile response')
  local result,seen,total={}, {},0
  local geo=require('beamngEarth/geotile')
  for _,tile in ipairs(msg.tiles)do
    assert(type(tile)=='table' and #tile==2 and type(tile[1])=='string' and pending.want[tile[1]] and not result[tile[1]],'Unexpected tile')
    assert(type(tile[2])=='table','Invalid rows')
    local rows={}
    for _,row in ipairs(tile[2])do
      total=total+1;assert(total<=5000,'Response too large')
      assert(type(row)=='table' and (#row==5 or #row==6),'Invalid row')
      local id,lat,lon,bearing=row[1],row[2],row[3],row[5]
      assert(type(id)=='string' and #id>0 and #id<=256 and not seen[id],'Duplicate or invalid source ID');seen[id]=true
      assert(type(lat)=='number' and lat>=50 and lat<=54 and type(lon)=='number' and lon>=3 and lon<=8,'Invalid position')
      assert(type(bearing)=='number' and bearing>=0 and bearing<360,'Invalid bearing')
      local x,y=geo.tileOf(lat,lon);assert(geo.key(x,y)==tile[1],'Wrong geographic tile')
      local subs={};if row[6]then assert(type(row[6])=='table' and #row[6]<=2,'Invalid subplates');for _,i in ipairs(row[6])do subs[#subs+1]=asset(i)end end
      rows[#rows+1]={id=id,lat=lat,lon=lon,asset=asset(row[4]),bearing_deg=bearing,supplementary_assets=subs,source_version=5}
    end
    result[tile[1]]={schema_version=1,kind='george_v5_render_tile',key=tile[1],records=rows}
  end
  return result
end
function M.onMessage(msg)
  if not active or not pending or msg.id~=pending.id then return false end
  if msg.error then pending=nil;retryAt=clock+5;outcome.status='server_error';return false end
  local ok,tiles=pcall(decode,msg)
  if not ok then pending=nil;retryAt=clock+5;outcome.status='invalid_response';outcome.error=tostring(tiles);return false end
  -- Commit an entire validated reply; no partially accepted or late-neighborhood data.
  for key,tile in pairs(tiles)do if wanted[key]then cache[key]=tile end end
  pending=nil;outcome.status='streaming';outcome.error=nil
  return true
end
local provider={}
function provider.neighborhood(x,y,keys)
  local geo=require('beamngEarth/geotile')
  local west,north=geo.tileOf(54,3);local east,south=geo.tileOf(50,8)
  if x<west or x>east or y<north or y>south then clearCache();outcome.status='outside_netherlands';return end
  local nextWanted={};for _,key in ipairs(keys)do nextWanted[key]=true end
  for key in pairs(cache)do if not nextWanted[key]then cache[key]=nil end end
  wanted=nextWanted
  if pending then
    local same=pending.x==x and pending.y==y
    if not same or clock-pending.at>=10 then pending=nil;retryAt=clock+1 else return end
  end
  if clock<retryAt then return end
  local missing={};for _,key in ipairs(keys)do if not cache[key]then missing[#missing+1]=key end end
  if #missing==0 then return end
  local want={};for _,key in ipairs(missing)do want[key]=true end
  serial=serial+1
  local request={type='nl_sign_tiles',id=serial,x=x,y=y,keys=missing}
  pending={id=serial,x=x,y=y,keys=missing,want=want,at=clock}
  if not require('beamngEarth/net').send(request)then pending=nil;retryAt=clock+2;outcome.status='disconnected'
  else outcome.status='requesting' end
end
function provider.get(key)return cache[key]end
function provider.count()local n=0;for _ in pairs(cache)do n=n+1 end;return n end
function M.update(dt,st)
  local inNL=(st.active==true or st.inOurLevel==true)and inNetherlands(st)
  applyLegacyPolicy(M.RVV_ONLY==true and inNL)
  clock=clock+(type(dt)=='number' and dt>0 and dt or 0)
  if not revision or not st.active or not inNL then if active then stop()end;return end
  if not active then
    if clock<startAt then return end
    local ok,err=pcall(function()
      extensions.load('beamngEarth_nltestset')
      extensions.load('beamngEarth_nlndw')
      driver=assert(extensions.beamngEarth_nlndw,'NL sign streamer not loaded')
      driver.startRemote(provider,'travel_bearing')
    end)
    if not ok then startAt=clock+5;outcome.status='renderer_unavailable';outcome.error=tostring(err);return end
    active=true
  end
  if anchor~=st.anchorId or (st.pending and not wasPending)then clearCache();anchor=st.anchorId end
  wasPending=st.pending and true or false
end
function M.report()return {active=active,revision=revision,cached_tiles=provider.count(),pending_id=pending and pending.id,outcome=outcome,rvv_only=M.rvvOnly(),legacy_suppression_active=legacyPolicy}end
function M.close()stop();revision=nil;outcome.status='stopped'end
return M
