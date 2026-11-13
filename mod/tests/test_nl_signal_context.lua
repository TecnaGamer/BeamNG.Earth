local dir=arg[0]:match('^(.*)[/\\]') or '.'
local T=dofile(dir..'/testlib.lua')
local C=require('beamngEarth/nl_signal_context')
local function bytes(n,k)local a={};for i=1,k do a[i]=string.char(n%256);n=math.floor(n/256)end;return table.concat(a)end
local lat,lon=51.35,6.15;local x,y=C.mercator(lat,lon)
local function record(kind,id,yy,meta)
  local p=bytes(kind,1)..bytes(id,4)..bytes(2,2)..bytes(#meta,2)..meta
  for _,xx in ipairs({x-100,x+100})do p=p..bytes(math.floor(xx*2+.5),3)..bytes(math.floor(yy*2+.5),3)end
  return p
end
local function pack(ambiguous,lazy)
  local data='BNECON1\0'..(lazy and string.rep('\0',C.BLOCK_BYTES-12)or'');local offsets={}
  for _,row in ipairs({record(0,100,y,''),record(1,200,y,'oneway')})do offsets[#offsets+1]=#data+1;data=data..row end
  if ambiguous then offsets[#offsets+1]=#data+1;data=data..record(1,201,y+1,'different')end
  local at=#data+1;for _,o in ipairs(offsets)do data=data..bytes(o,4)end
  local origin=20037508.342789244;local span=2*origin/2^14
  local key=math.floor((x+origin)/span)..'_'..math.floor((origin-y)/span)
  local index={zoom=14,tiles={[key]={at,#offsets}}}
  local decode=function(s)return s=='oneway' and {1,'','','left|through|right'} or {1,'','','through|through|right'}end
  if lazy then return index,data..string.rep('\0',2*C.BLOCK_BYTES),decode end
  return C.new(index,data,decode)
end
local match=pack(false)
local p=match(lat,lon,1,0,3)
T.check(p.zone=='bibeko' and p.zone_wvk_id==100,'near original inside-road section uses BIBEKO')
T.check(p.turns[1]=='left' and p.turns[3]=='right' and p.osm_way_id==200,'same direction and count matches turn lanes')
T.check(not match(lat,lon,-1,0,3).turns,'one-way opposite direction never gets its arrows')
T.check(not match(lat,lon,1,0,2).turns,'count mismatch never reassigns lanes')
T.check(not match(lat,lon,0,1,3).turns,'crossing road angle rejected')
local outsideLat=math.deg(2*math.atan(math.exp((y+100)/20037508.342789244*math.pi))-math.pi/2)
T.check(match(outsideLat,lon,1,0,3).zone=='bubeko','away from inside road sections uses BUBEKO')
T.check(pack(true)(lat,lon,1,0,3).turns_source=='parallel_road_turns_ambiguous','conflicting parallel source roads stay unresolved')
T.check(not pcall(C.new,{zoom=14,tiles={}},'broken pack',function()end),'invalid pack header rejected')
local index,data,decode=pack(false,true)
index.data='fake-context';index.data_bytes=#data
local closed,reads=false,0
local function opener(path,mode)
  T.check(path==index.data and mode=='rb','lazy context opens selected pack read-only')
  local cursor=0
  return {
    seek=function(_,whence,at)if whence=='end'then cursor=#data else cursor=at end;return cursor end,
    read=function(_,n)
      T.check(type(n)=='number'and n<=C.BLOCK_BYTES,'lazy reads are bounded; never read the whole pack')
      reads=reads+1;local s=data:sub(cursor+1,cursor+n);cursor=cursor+#s;return s
    end,
    close=function()closed=true end,
  }
end
local lazy,signals,close,stats=C.open(index,decode,opener)
T.check(type(lazy)=='function'and type(signals)=='function','lazy context exposes same lookup API')
local lazyResult=lazy(lat,lon,1,0,3)
T.check(lazyResult.zone==p.zone and lazyResult.osm_way_id==p.osm_way_id and lazyResult.turns[1]==p.turns[1],
  'record crossing a block boundary matches full-pack decoding')
local after=reads;lazy(lat,lon,1,0,3)
T.check(reads==after,'same tile lookup does not reread blocks')
T.check(stats.bytes_read<#data and stats.peak_resident_bytes<=C.BLOCK_LIMIT*C.BLOCK_BYTES,'payload reads and block memory stay bounded')
close();T.check(closed and stats.resident_bytes==0,'closing releases the file and cached blocks')
index.data_bytes=#data+1
T.check(C.open(index,decode,opener)==nil,'wrong-size pack is rejected and closed')
local filteredReads=0
index.signal_tiles={}
local _,filtered=C.new(index,function(at,n)
  filteredReads=filteredReads+1;return data:sub(at,at+n-1)
end,decode)
T.check(#filtered(lat,lon,250)==0 and filteredReads==1,'signal-only index avoids decoding unrelated road records')
T.done('test_nl_signal_context')
