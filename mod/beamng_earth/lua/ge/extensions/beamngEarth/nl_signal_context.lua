-- Offline WKD built-up road sections and OSM lane-turn matching. No gameplay network.
local M={BLOCK_BYTES=65536,BLOCK_LIMIT=8}
local PI,ORIGIN=math.pi,20037508.342789244
local function u16(s,i)local a,b=s:byte(i,i+1);return a+b*256 end
local function u24(s,i)local a,b,c=s:byte(i,i+2);return a+b*256+c*65536 end
local function u32(s,i)local a,b,c,d=s:byte(i,i+3);return a+b*256+c*65536+d*16777216 end
function M.mercator(lat,lon)return lon*ORIGIN/180,math.log(math.tan(PI/4+math.rad(lat)/2))*ORIGIN/PI end
local function split(s)local a={};for v in ((s or '')..'|'):gmatch('(.-)|')do a[#a+1]=v end;return a end

function M.new(index,data,decode)
  local read=type(data)=='function'and data or function(at,n)return data:sub(at,at+n-1)end
  assert(read(1,8)=='BNECON1\0','Invalid offline context pack')
  local cache,order={},{}
  local function tile(key,signalsOnly)
    local slots=signalsOnly and index.signal_tiles or index.tiles
    local slot=slots[key]
    if signalsOnly and index.signal_tiles then key='s:'..key end
    if cache[key] then return cache[key] end
    local rows={}
    if slot then
      local offsets=read(slot[1],slot[2]*4)
      for k=0,slot[2]-1 do
        local at=u32(offsets,1+k*4);local header=read(at,9)
        local kind,id=header:byte(1),u32(header,2)
        local n,len=u16(header,6),u16(header,8)
        local body=read(at+9,len+n*6)
        local meta=len>0 and decode(body:sub(1,len))or nil
        local points={};for j=0,n-1 do points[#points+1]=u24(body,len+1+j*6)/2;points[#points+1]=u24(body,len+4+j*6)/2 end
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
        for _,row in ipairs(tile(gx..'_'..gy,true))do if row.kind==2 then
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

-- Seek the existing pack in small blocks. The national payload never becomes
-- a Lua string; decoded rows still have the same bounded 16-tile cache.
function M.open(index,decode,opener)
  local file=(opener or io.open)(index.data,'rb')
  if not file then return end
  local blocks,order={},{}
  local stats={bytes_read=0,blocks_read=0,resident_bytes=0,peak_resident_bytes=0}
  local closed=false
  local function close()
    if not closed then file:close();closed=true end
    blocks={};order={};stats.resident_bytes=0
  end
  local function read(at,n)
    assert(not closed and at>=1 and n>=0 and n<=1048576 and at+n-1<=index.data_bytes,'Invalid context read')
    local pieces={}
    while n>0 do
      local key=math.floor((at-1)/M.BLOCK_BYTES)
      local block=blocks[key]
      if not block then
        assert(file:seek('set',key*M.BLOCK_BYTES),'Context seek failed')
        block=assert(file:read(math.min(M.BLOCK_BYTES,index.data_bytes-key*M.BLOCK_BYTES)),'Context read failed')
        blocks[key]=block;order[#order+1]=key
        stats.bytes_read=stats.bytes_read+#block;stats.blocks_read=stats.blocks_read+1
        stats.resident_bytes=stats.resident_bytes+#block
        if #order>M.BLOCK_LIMIT then
          local old=table.remove(order,1);stats.resident_bytes=stats.resident_bytes-#blocks[old];blocks[old]=nil
        end
        stats.peak_resident_bytes=math.max(stats.peak_resident_bytes,stats.resident_bytes)
      end
      local offset=(at-1)%M.BLOCK_BYTES;local take=math.min(n,#block-offset)
      assert(take>0,'Truncated context block')
      pieces[#pieces+1]=block:sub(offset+1,offset+take);at=at+take;n=n-take
    end
    return table.concat(pieces)
  end
  local ok,a,b=pcall(function()
    assert(file:seek('end')==index.data_bytes,'Context size mismatch')
    return M.new(index,read,decode)
  end)
  if not ok then close();return nil,a end
  return a,b,close,stats
end

local lookup,signalPoints,attempted,closePack
function M.lookup(lat,lon,dx,dy,count)
  if not attempted then
    attempted=true
    local ok,index=pcall(require,'beamngEarth/nl_groenpol_context_index')
    if ok and jsonDecode then
      lookup,signalPoints,closePack,M.stats=M.open(index,jsonDecode)
      if not lookup then signalPoints=nil end
    end
  end
  return lookup and lookup(lat,lon,dx,dy,count) or {zone='unknown',zone_source='context_pack_unavailable',turns_source='turn_lanes_unavailable'}
end
function M.signalPoints(lat,lon,radius)
  if not attempted then M.lookup(lat,lon,0,0,1)end
  return signalPoints and signalPoints(lat,lon,radius) or {}
end
function M.reset()
  if closePack then closePack()end
  lookup,signalPoints,attempted,closePack=nil,nil,nil,nil;M.stats=nil
end
return M
