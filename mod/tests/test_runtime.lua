-- Reproduce the real deployment order: an old local mod is loaded first, then
-- BeamMP mounts a newer server ZIP without restarting the Lua VM.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
dofile(dir..'/stubs.lua')
scenetree.MissionGroup=nil -- main menu, when BeamMP mounts downloaded mods
local realDofile = dofile
local root = arg[1] or dir..'/../beamng_earth'
local serverScript = arg[2] or dir..'/../../beammp/Client/scripts/beamng_earth_server/modScript.lua'
package.path = root..'/lua/ge/extensions/?.lua;'..package.path
local runtime = realDofile(root..'/lua/ge/extensions/beamngEarth/runtime.lua')
local unloaded,manual,logs = {},{},{}
local localActive,deactivated = true,0
log=function(_,_,msg) logs[#logs+1]=msg end
extensions={}
function extensions.unload(name)
  local value=extensions[name]
  if value and value.onExtensionUnloaded then value.onExtensionUnloaded() end
  unloaded[#unloaded+1]=name
  extensions[name],_G[name]=nil,nil
  package.loaded[name:gsub('_','/')]=nil
end
setExtensionUnloadMode=function(name,mode) manual[name]=mode end
-- BeamNG resolves absolute paths through its VFS; map just our mounted mod for this test.
dofile=function(path)
  if path=='/scripts/beamng_earth/modScript.lua' then
    if localActive then setExtensionUnloadMode('beamngEarth_core','manual');return end -- old local file wins VFS lookup
    return realDofile(root..path)
  end
  if path:sub(1,5)=='/lua/' then return realDofile(root..path) end
  return realDofile(path)
end
local function mount() realDofile(serverScript) end
T.check(not runtime.refresh(),'cold startup has no older runtime to evict')
local oldSession={legacy=true}
local cleanupWithHelpers=false
local oldCore={RUNTIME_REVISION=runtime.REVISION-1,onExtensionUnloaded=function()
  cleanupWithHelpers=package.loaded['beamngEarth/mpsession']==oldSession
end}
beamngEarth_core,extensions.beamngEarth_core=oldCore,oldCore
local oldSettings={old=true}
beamngEarth_settings,extensions.beamngEarth_settings=oldSettings,oldSettings
package.loaded['beamngEarth/core']=oldCore
package.loaded['beamngEarth/mpsession']=oldSession
package.loaded['beamngEarth/settings']=oldSettings
package.loaded['beamngEarth/runtime']={REVISION=-1,refresh=function() error('cached bootstrap used') end}
local unrelated={isMPSession=function() return true end}
MPCoreNetwork=unrelated
core_modmanager={getModDB=function(name) return name=='beamng_earth' and {active=localActive} end,
  deactivateMod=function(name) assert(name=='beamng_earth');localActive=false;deactivated=deactivated+1 end}
extensions.MPCoreNetwork=unrelated
package.loaded.MPCoreNetwork=unrelated
package.loaded['beamngEarthExtra/other']=unrelated
local levelIdentifier=getCurrentLevelIdentifier
getCurrentLevelIdentifier=function() return nil end
scenetree.MissionGroup={} -- an empty main-menu container is not a running level
mount()
getCurrentLevelIdentifier=levelIdentifier
scenetree.MissionGroup=nil
T.check(deactivated==1 and not localActive,'unique server bootstrap removes older local VFS precedence before loading code')
T.check(cleanupWithHelpers and unloaded[1]=='beamngEarth_core','old core cleans up before helpers are evicted')
T.check(not beamngEarth_core and not beamngEarth_settings,'old manual extensions are unloaded')
T.check(not package.loaded['beamngEarth/mpsession'] and not package.loaded['beamngEarth/settings']
  and not package.loaded['beamngEarth/runtime'],'cached helpers and cached bootstrap are replaced together')
T.check(extensions.MPCoreNetwork==unrelated and package.loaded.MPCoreNetwork==unrelated
  and package.loaded['beamngEarthExtra/other']==unrelated,'unrelated game, BeamMP and other mod modules stay loaded')
T.check(manual.beamngEarth_core=='manual' and manual.beamngEarth_settings=='manual'
  and manual.beamngEarth_dev=='manual' and manual.beamngEarth_report=='manual'
  and manual.beamngEarth_serverlist=='manual','mod manager will reload all normal manual extensions')

-- The real current core now requires the new session module, rather than the old cached one.
local core=require('beamngEarth/core')
beamngEarth_core,extensions.beamngEarth_core=core,core
T.check(core.RUNTIME_REVISION==runtime.REVISION,'new core loads the mounted runtime revision')
local session=require('beamngEarth/mpsession')
T.check(session~=oldSession and type(session.update)=='function','new session module is actually used')
local count=#unloaded
mount()
T.check(#unloaded==count and beamngEarth_core==core and package.loaded['beamngEarth/mpsession']==session,
  'repeated activation of the same runtime does not reset a session')

local handlers,poses,files={},{},{}
AddEventHandler=function(name,fn) handlers[name]=fn end
TriggerServerEvent=function(name,value) if name=='bngePose' then poses[#poses+1]=value end end
jsonEncode=function(value) return value end
jsonDecode=function(value) return value end
jsonReadFile=function(path) return files[path] end
jsonWriteFile=function(path,value) files[path]=value;return true end
MPCoreNetwork={isMPSession=function() return true end,
  getCurrentServer=function() return {ip='test.example',port=30814} end}
local anchor=require('beamngEarth/geodesy').newAnchor(42,-85)
session.onPostJoin()
session.update(.1,anchor,{x=0,y=0,z=0})
T.check(handlers.bngeGuestIdentity and poses[#poses].guestResume==1 and poses[#poses].clientRuntime==runtime.REVISION,
  'mounting updated server code registers guest handler and advertises support despite old app startup')
local token=string.rep('a',32)
handlers.bngeGuestIdentity({token=token})
session.update(.1,anchor,{x=0,y=0,z=0})
T.check(poses[#poses].guestToken==token,'newly loaded handler persists and acknowledges guest identity')
session.reset();session.onPostJoin();session.update(.1,anchor,{x=0,y=0,z=0})
T.check(poses[#poses].joined and poses[#poses].guestToken==token,'guest identity survives the next reconnect')

-- A manual mid-drive mod activation must never delete terrain under the player.
core.RUNTIME_REVISION=runtime.REVISION-1
scenetree.MissionGroup={}
mount()
T.check(#unloaded==count and beamngEarth_core==core,'live mission blocks destructive runtime replacement')
T.check(logs[#logs]:find('fully restart BeamNG',1,true),'deferred live reload gives an explicit restart instruction')
dofile=realDofile
T.done('test_runtime')
