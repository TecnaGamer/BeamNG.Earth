local dir=arg[0]:match('^(.*)[/\\]')or'.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
math.atan2=math.atan2 or math.atan
math.sinh=math.sinh or function(x)return(math.exp(x)-math.exp(-x))/2 end
local geo=require('beamngEarth/geotile')
local tx,ty=geo.tileOf(51.36711,6.14178)
local key=geo.key(tx,ty)
local sent,provider,stops={},nil,0
package.loaded['beamngEarth/net']={send=function(msg)sent[#sent+1]=msg;return true end}
extensions={load=function()end,beamngEarth_nlndw={startRemote=function(p,c)provider=p;assert(c=='travel_bearing')end,stop=function()stops=stops+1 end}}
local st={active=true,anchorId=1,player={lat=51.36711,lon=6.14178}}
local M=require('beamngEarth/nlremote')
local policy={}
package.loaded['beamngEarth/roads']={setLegacySignsSuppressed=function(on)policy.roads=on end}
package.loaded['beamngEarth/trees']={setLegacyTrafficSignsSuppressed=function(on)policy.trees=on end}
M.update(1,st);assert(policy.roads and policy.trees and M.report().legacy_suppression_active,'RVV-only applies even without a dataset or server hello')
local function keys(x,y)local out={};for a=x-1,x+1 do for b=y-1,y+1 do out[#out+1]=geo.key(a,b)end end;return out end
local function request(x,y)provider.neighborhood(x,y,keys(x,y));return sent[#sent]end
local function reply(q)
  local out={v=1,type='nl_sign_tiles',id=q.id,revision='r1',assets={'B6','OB504'},tiles={}}
  for _,k in ipairs(q.keys)do out.tiles[#out.tiles+1]={k,k==key and {{'source-id',51.36711,6.14178,1,0,{2}}} or {}}end
  return out
end
M.session({nlSigns=1,nlSignsRevision='r1'});M.update(1,st)
local q=request(tx,ty);assert(#q.keys==9 and #sent==1)
request(tx,ty);assert(#sent==1,'Only one in-flight request')
assert(M.onMessage(reply(q)))
assert(M.report().cached_tiles==9 and provider.get(key).records[1].supplementary_assets[1]=='OB504')
request(tx,ty);assert(#sent==1,'Cache empty tiles too')
q=request(tx+1,ty);assert(#q.keys==3 and M.report().cached_tiles==6,'Evict old cells and fetch only new ones')
local old=q
request(tx+2,ty);M.update(1,st);q=request(tx+2,ty)
assert(q.id~=old.id and not M.onMessage(reply(old)),'Reject stale neighborhood reply')
local bad=reply(q);bad.tiles[1][1]='g17_0_0'
assert(not M.onMessage(bad) and M.report().cached_tiles<=9,'Reject invalid reply atomically')
M.update(5,st);q=request(tx+2,ty)
local waiting=q.id
M.update(11,st);request(tx+2,ty);assert(not M.onMessage(reply(q)),'Timed-out replies must not populate cache')
M.update(1,st);q=request(tx+2,ty);assert(q.id~=waiting,'Retry timeout with a fresh serial')
M.session({nlSigns=1,nlSignsRevision='r2'});M.update(1,st)
assert(M.report().cached_tiles==0 and not M.onMessage(reply(q)),'Reconnect invalidates requests and revision')
q=request(tx,ty);bad=reply(q);assert(not M.onMessage(bad),'Reject different dataset revision')
M.update(5,st);q=request(tx,ty)
st.pending={};M.update(.01,st);assert(not M.onMessage(reply(q)) and M.report().cached_tiles==0,'Teleport rejects in-flight response')
st.pending=nil;st.anchorId=2;M.update(.01,st)
local before=#sent;request(1,1);assert(#sent==before and M.report().outcome.status=='outside_netherlands')
M.session({});M.update(1,st);assert(not M.report().active,'Old server is compatible and leaves source signs disabled')
assert(stops>=2)
local starts=0;extensions.load=function()starts=starts+1;error('Missing optional renderer')end
st.player={lat=51.3167,lon=6.2833};M.update(1,st)
assert(not policy.roads and not policy.trees and not M.rvvOnly(),'Germany near Venlo keeps the ordinary signs despite lying inside the old NL bounding box')
st.player={lat=51.221,lon=4.403};M.update(1,st)
assert(not policy.roads and not policy.trees,'Belgium keeps the ordinary signs')
st.player=nil;M.update(1,st);assert(not M.rvvOnly(),'Unknown position does not suppress signs abroad')
st.player={lat=51.36711,lon=6.14178};M.update(1,st)
assert(policy.roads and policy.trees and M.rvvOnly(),'Returning to the Netherlands re-enables RVV-only')
M.session({nlSigns=1,nlSignsRevision='r1'});M.update(1,st)
assert(not M.report().active and M.report().outcome.status=='renderer_unavailable','Missing add-on must not break the world update')
M.update(1,st);assert(starts==1,'Back off failed optional startup')
package.loaded['beamngEarth/net'].isConnected=function()return true end
st.player={lat=51.221,lon=4.403};M.update(1,st)
assert(sent[#sent].type=='rvv_only'and sent[#sent].on==false,'Crossing out of NL restores the server legacy stream')
assert(not M.report().active,'Foreign positions stop the NL renderer')
st.player={lat=51.36711,lon=6.14178};M.update(1,st)
assert(sent[#sent].type=='rvv_only'and sent[#sent].on==true,'Crossing into NL stops the server legacy stream')
M.close()
st.active=false;M.update(1,st);assert(not policy.roads and not policy.trees,'Other maps restore their usual sign policy')
print('PASS: remote compact decoding, subplates, bounded positive/negative cache, overlap requests, single flight, timeouts, revision/reconnect/teleport cleanup, old-server fallback and no outside-NL requests.')
