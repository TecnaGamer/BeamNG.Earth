local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local rv = require('beamngEarth/resumevehicle')
local geo = require('beamngEarth/geodesy')
local car = {getID=function() return 123 end}
local spawned, model, options, direction, camera, entered
core_vehicles={defaultVehicleModel='pickup',getModel=function(m) return m=='missing_mod' and {} or {model={}} end,
  spawnNewVehicle=function(m,opt) spawned=(spawned or 0)+1;model,options=m,opt;return car end}
quatFromDir=function(d) direction=d;return quat(0,0,0,1) end
commands={setGameCamera=function() camera=true end}
be.enterVehicle=function(_,player,veh) entered=veh end
local saved={lat=42,lon=-84,height=220,heading=123,vehicle={model='pickup',config={format=4,
  parts={engine='v8'},vars={boost=12},paints={{baseColor={.2,.3,.4,1}}},licenseName='EARTH',
  linkedPCFile='someone-elses-file.pc',vehicles={{model='another'}}}}}
local anchor=geo.newAnchor(42,-84)
local result=rv.spawn(saved,anchor,0,0,221)
T.check(result==car and model=='pickup' and options.autoEnterVehicle,'spawn saved model and enter it through normal API')
T.check(options.config.format==2 and not options.config.linkedPCFile and not options.config.vehicles,
  'only a single portable configuration reaches spawner')
T.check(options.config.parts.engine=='v8' and options.config.vars.boost==12
  and options.paint.baseColor[3]==.4 and options.licenseText=='EARTH','custom parts, tuning, paint and plate reach spawner')
T.near(options.pos.z,221,1e-6,'spawn is on the prepared road floor')
T.near(direction.x,math.sin(math.rad(123)),1e-6,'spawn heading follows saved true bearing')
rv.enter(car)
T.check(entered==car and camera,'restore explicitly enters car and exits free camera')
saved.vehicle.model='missing_mod'
local _,message=rv.spawn(saved,anchor,0,0,221)
T.check(model=='pickup' and options.config==nil and message,'missing model falls back to game default without foreign parts')
saved.vehicle=nil
rv.spawn(saved,anchor,0,0,221)
T.check(model=='pickup' and options.config==nil,'legacy position-only saves receive a default vehicle')
T.check(not rv.record({model='../invalid',config={parts={}}}),'invalid model path rejected')
MPVehicleGE={isOwn=function() return false end,getServerVehicleID=function() return '3-1' end}
T.check(rv.serverId(car)==nil,'remote vehicle never becomes saved vehicle')
MPVehicleGE.isOwn=function() return true end
T.check(rv.serverId(car)=='3-1','owned active vehicle identifies exact BeamMP server vehicle')
core_vehicles.spawnNewVehicle=function() return false end
result,message=rv.spawn(saved,anchor,0,0,221)
T.check(not result and message,'server rejection is surfaced without bypassing vehicle limits')
T.done('test_resumevehicle')
