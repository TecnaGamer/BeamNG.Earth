-- Offline WKD built-up road sections and OSM lane-turn matching. No gameplay network.
local M={}
local PI,ORIGIN=math.pi,20037508.342789244
local function u16(s,i)local a,b=s:byte(i,i+1);return a+b*256 end
local function u24(s,i)local a,b,c=s:byte(i,i+2);return a+b*256+c*65536 end
local function u32(s,i)local a,b,c,d=s:byte(i,i+3);return a+b*256+c*65536+d*16777216 end
function M.mercator(lat,lon)return lon*ORIGIN/180,math.log(math.tan(PI/4+math.rad(lat)/2))*ORIGIN/PI end
local function split(s)local a={};for v in ((s or '')..'|'):gmatch('(.-)|')do a[#a+1]=v end;return a end

function M.new(index,data,decode)
  assert(data:sub(1,8)=='BNECON1\0','Invalid offline context pack')
  local cache,order={},{}
  local function tile(key)
    if cache[key] then return cache[key] end
    local slot=index.tiles[key];local rows={}
    if slot then
      for k=0,slot[2]-1 do
        local at=u32(data,slot[1]+k*4);local kind=data:byte(at);local id=u32(data,at+1)
        local n,len=u16(data,at+5),u16(data,at+7);at=at+9
        local meta=len>0 and decode(data:sub(at,at+len-1)) or nil;at=at+len
        local points={};for j=0,n-1 do points[#points+1]=u24(data,at+j*6)/2;points[#points+1]=u24(data,at+j*6+3)/2 end
        rows[#rows+1]={kind=kind,id=id,meta=meta,points=points}
      end
    end
    cache[key]=rows;order[#order+1]=key
    if #order>16 then cache[table.remove(order,1)]=nil end
    return rows
  end
  local function lookup(lat,lon,dx,dy,count)
    local x,y=M.mercator(lat,lon);local span=2*ORIGIN/2^index.zoom
    local key=math.floor((x+ORIGIN)/span)..'_'..math.floor((ORIGIN-y)/span)
    local scale=math.cos(math.rad(lat));local inside=math.huge;local insideId;local choices={};local road
    local dl=math.sqrt((dx or 0)^2+(dy or 0)^2)
    for _,row in ipairs(tile(key))do
      local best,along=math.huge,0;local p=row.points;local nearX,nearY
      for i=1,#p-2,2 do
        local ax,ay,bx,by=p[i],p[i+1],p[i+2],p[i+3]
        local vx,vy=bx-ax,by-ay;local l2=vx*vx+vy*vy
        if l2>.001 then
          local t=math.max(0,math.min(1,((x-ax)*vx+(y-ay)*vy)/l2))
          local d=((x-ax-t*vx)^2+(y-ay-t*vy)^2)*scale^2
          if d<best then best=d;nearX,nearY=ax+t*vx,ay+t*vy;along=dl>.1 and ((dx or 0)*vx+(dy or 0)*vy)/(dl*math.sqrt(l2)) or 0 end
        end
      end
      if row.kind==0 then
        if best<inside then inside,insideId=best,row.id end
      elseif row.kind==1 and best<=15^2 and math.abs(along)>.90 then
        local meta=row.meta;local raw
        if (meta[1]==0 or along*meta[1]>.90) and (not road or best<road.distance)then
          road={distance=best,x=nearX,y=nearY,meta=meta,along=along,id=row.id}
        end
        if meta[1]~=0 then if along*meta[1]>.90 then raw=meta[4] end
        else raw=along>0 and meta[2] or meta[3] end
        if raw and raw~='' then
          local turns=split(raw)
          if #turns==count or count==nil then choices[#choices+1]={distance=math.sqrt(best),turns=turns,raw=raw,id=row.id} end
        end
      end
    end
    table.sort(choices,function(a,b)return a.distance<b.distance end)
    local out={zone=inside<=20^2 and 'bibeko' or 'bubeko',zone_source='WKD_KOM_BORDEN_spatial_match',
      zone_distance_m=inside<math.huge and math.sqrt(inside) or nil,zone_wvk_id=insideId,
      turns_source='turn_lanes_unavailable'}
    local best=choices[1]
    if road then
      local m=road.meta
      out.road_class=m[5];out.road_x,out.road_y=road.x,road.y
      local total=tonumber(m[6]);local own=tonumber(road.along>0 and m[7] or m[8])
      local other=tonumber(road.along>0 and m[8] or m[7])
      out.road_lane_count=own or (m[1]~=0 and total or total and math.max(1,math.floor(total/2)))
      out.road_opposite_count=m[1]~=0 and 0 or other
    end
    if best then
      if choices[2] and choices[2].distance<best.distance+2.5 and choices[2].raw~=best.raw then
        out.turns_source='parallel_road_turns_ambiguous'
      else
        out.turns=best.turns;out.turns_source='OSM_turn_lanes_spatial_match';out.osm_way_id=best.id
        out.road_lane_count=#best.turns
      end
    end
    return out
  end
  local function signals(lat,lon,radius)
    local x,y=M.mercator(lat,lon);local span=2*ORIGIN/2^index.zoom
    local reach=math.min(radius or 1800,2500)/math.cos(math.rad(lat))
    local rows,seen={},{}
    for gx=math.floor((x-reach+ORIGIN)/span),math.floor((x+reach+ORIGIN)/span)do
      for gy=math.floor((ORIGIN-y-reach)/span),math.floor((ORIGIN-y+reach)/span)do
        for _,row in ipairs(tile(gx..'_'..gy))do if row.kind==2 then
          local id=row.meta.osm_node_id
          if not seen[id]then
            seen[id]=true;local px,py=row.points[1],row.points[2]
            rows[#rows+1]={id=id,lon=px*180/ORIGIN,lat=math.deg(2*math.atan(math.exp(py/ORIGIN*math.pi))-math.pi/2),road_class=row.meta.road_class}
          end
        end end
      end
    end
    table.sort(rows,function(a,b)return a.id<b.id end);return rows
  end
  return lookup,signals
end

local lookup,signalPoints,attempted
function M.readBinary(path)
  local file=io.open(path,'rb')
  if not file then return end
  local data=file:read('*all');file:close();return data
end
function M.lookup(lat,lon,dx,dy,count)
  if not attempted then
    attempted=true
    local ok,index=pcall(require,'beamngEarth/nl_groenpol_context_index')
    if ok and jsonDecode then
      local data=M.readBinary(index.data)
      if type(data)=='string' and #data==index.data_bytes and data:sub(1,8)=='BNECON1\0' then lookup,signalPoints=M.new(index,data,jsonDecode) end
    end
  end
  return lookup and lookup(lat,lon,dx,dy,count) or {zone='unknown',zone_source='context_pack_unavailable',turns_source='turn_lanes_unavailable'}
end
function M.signalPoints(lat,lon,radius)
  if not attempted then M.lookup(lat,lon,0,0,1)end
  return signalPoints and signalPoints(lat,lon,radius) or {}
end
function M.reset()lookup,signalPoints,attempted=nil,nil,nil end
return M
