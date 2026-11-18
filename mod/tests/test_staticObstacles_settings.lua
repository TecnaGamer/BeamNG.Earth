local dir = arg[0]:match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local S = require('beamngEarth/settings')
local stored, sent = nil, 0
function jsonReadFile()return stored end
function jsonWriteFile(_, value)stored=value;return true end
FS={fileExists=function(_,path)return path==S.FILE and stored~=nil end}
local ve=setmetatable({mapmgr={setMap=function()end},ai={mode='disabled'}},{__index=_G})
ve.obj={getId=function()return 7 end}
local vehicle={queueLuaCommand=function(_,code)
  sent=sent+1;setfenv(assert(loadstring(code)),ve)()
end}
be={queueAllObjectLua=function(_,code)vehicle:queueLuaCommand(code)end,
  getObjectByID=function(_,id)if id==7 then return vehicle end end}
beamngEarth_settings=S
ve.obj.queueGameEngineLua=function(_,code)assert(loadstring(code))()end
local loader=assert(loadfile(dir..'/../beamng_earth/lua/vehicle/extensions/beamngEarth/staticObstacles.lua'))
local controller=setfenv(loader,ve)()
ve.beamngEarth_staticObstacles=controller
T.check(S.get('autodriveObstacles')==false,'the saved setting defaults off')
T.check(controller.enabled==false,'the optional behavior defaults off')
T.check(S.set('autodriveObstacles',true).ok and controller.enabled,'a settings change enables the existing controller')
local values=S.load()
T.check(values.autodriveObstacles==true,'the opt-in persists across settings reload')
controller.setEnabled(false)
controller.onExtensionLoaded()
T.check(controller.enabled==true,'a newly loaded controller requests its saved opt-in')
T.check(S.set('autodriveObstacles',false).ok and controller.enabled==false,'switching off reaches the controller')
T.check(sent>=3,'settings changes and late loading use the vehicle command bridge')
T.done('autodriveObstacles settings')
