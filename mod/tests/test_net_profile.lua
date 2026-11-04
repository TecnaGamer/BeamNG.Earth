local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local framing=require('beamngEarth/framing')
local frame=framing.encode('{}')
local chunks={}
local writes=0
local fake={settimeout=function() end,connect=function() return true end,setoption=function() end,close=function() end,
 send=function(_,data,pos) writes=writes+1;if writes==1 then return nil,'timeout',pos+2 end;return #data end,
 receive=function() if #chunks>0 then return nil,'timeout',table.remove(chunks,1) end;return nil,'timeout','' end}
package.loaded.socket={tcp=function() return fake end}
jsonEncode=function() return '{}' end
jsonDecode=function() return {type='notice'} end
local N=require('beamngEarth/net')
N.setProfiling(true);N.update(0)
N.send({type='test'})
T.check(N.profileState().sentBytes==3,'partial socket send counted once')
chunks={frame:sub(1,2),frame:sub(3)}
N.update(0)
T.check(N.profileState().sentBytes==#frame,'remaining socket send counted without double counting')
N.update(0)
T.check(N.profileState().receivedBytes==#frame,'partial receives include protocol frame bytes')
T.check(N.profileState().connectionAttempts==1,'connection attempts tracked')
N.setProfiling(false);N.send({type='test'});N.update(0)
T.check(N.profileState().sentBytes==0 and N.profileState().receivedBytes==0,'off stops transport counters')
N.close()
T.done('net_profile')
