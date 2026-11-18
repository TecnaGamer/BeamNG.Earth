local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local H = require('beamngEarth/traffichorizon')
local now, reads, state = 10, 0, 'on'
os.clock = function() return now end
local data, objects = {}, {}
be = {getPlayerVehicleID = function(_, slot) return slot == 0 and 1 or slot == 2 and 2 or -1 end}
core_multiseat = {getPlayers = function() return {0, 2} end}
gameplay_traffic = {getState = function() return state end, getTrafficData = function() reads = reads + 1; return data end}
getObjectByID = function(id) return objects[id] end
local function car(id, isAi, phase, active, controlled)
  data[id] = {isAi=isAi, state=phase}
  objects[id] = {
    getActive = function() return active end,
    isPlayerControlled = function() return controlled end,
    getPosition = function() return {x=id, y=20} end,
    getVelocity = function() return {x=30, y=0} end,
  }
end
car(1,true,'active',true,false) -- primary player
car(2,true,'active',true,false) -- another local seat
car(3,true,'active',true,true)  -- seat transition before the roster updates
car(4,false,'active',true,false) -- remote human
car(5,true,'active',false,false) -- pooled car
car(6,true,'queued',true,false)
car(7,true,'fadeOut',true,false)
car(8,true,'active',true,false)
car(9,true,'fadeIn',true,false)
car(10,true,'locked',true,false) -- driving AI with respawning disabled
data[11] = {isAi=true,state='active'} -- removed object
data[12] = {} -- unclassified/parked entry
local rows = H.sample()
local ids = {}; for _, c in ipairs(rows) do ids[c.x] = true end
T.check(#rows == 3 and ids[8] and ids[9] and ids[10], 'only active, unoccupied AI enters the shared road horizon')
T.check(rows[1].vx == 30 and rows[1].sampleS == 0.25, 'read-only position/velocity snapshots include sampling allowance')
now = 10.1
T.check(H.sample() == rows and reads == 1, 'terrain and graph updates share one bounded scan')
data[8] = nil; now = 10.3
T.check(#H.sample() == 2 and reads == 2, 'expired snapshot drops removed traffic')
state = 'off'
T.check(#H.sample() == 0, 'disabling traffic immediately clears the snapshot')
state = 'on'
T.check(#H.sample() == 2 and reads == 3, 're-enabling traffic samples immediately')
H.reset(); data[9] = nil
T.check(#H.sample() == 1 and reads == 4, 'anchor/level reset invalidates cached world coordinates')
now = 1
T.check(#H.sample() == 1 and reads == 5, 'a rewound clock cannot retain a stale snapshot')
gameplay_traffic = nil
T.check(#H.sample() == 0, 'traffic extension absent: no extra streaming work')
T.done('test_traffichorizon')
