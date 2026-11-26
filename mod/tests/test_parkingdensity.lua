local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local D = require('beamngEarth/parkingdensity')
local live, bus = {5,8,0,0,1}, {7,8,0,0,2}
local slots = {live,{120,0,0,90,3,'sedan',1.45,'retail',.5},bus,
  {120,10,0,90,3,'pickup',1.79,'dealer',.82}}
local normal, scenery = D.split(slots)
T.check(#normal == 2 and normal[1] == live and normal[2] == bus, 'preserves traffic and bus sites')
T.check(#scenery == 2 and scenery[1][5]==90 and scenery[2][1]==3, 'forest yaw and model')
T.check(D.split(normal)==normal,'old tile retained')
local Trees = require('beamngEarth/trees')
local tile = {f=Trees.flatten(scenery,9),n=2,s=9}
local clock = 18
package.loaded['beamngEarth/nightlights'] = {hour=function() return clock end}
local parking = require('beamngEarth/parking')
local player = {x=0,y=0,z=0}
local view = {pos=player,fwd={x=1,y=0,z=0},cosHalf=0.1}
T.check(D.refresh(tile,view,player) and tile.occupied[1] and tile.occupied[2], 'retail peak')
clock=2
T.check(not D.refresh(tile,view,player) and tile.occupied[1], 'time skip preserves visible shoppers')
view.fwd.x=-1
view.pos={x=110,y=0,z=0}
T.check(not D.refresh(tile,view,player) and tile.occupied[1], 'near cars stay even behind camera')
view.pos=player
T.check(D.refresh(tile,view,player) and not tile.occupied[1] and tile.occupied[2], 'after closing: retail out, inventory stays')
T.check(not D.refresh(tile,view,player),'stable on revisit')
T.check(D.visible(.5,.499,true,false) and not D.visible(.5,.501,false,false), 'occupancy hysteresis')
T.check(D.fraction(4,10) > D.fraction(4,20) and D.fraction(5,18) > D.fraction(5,15),'different premises')
T.done('test_parkingdensity')
