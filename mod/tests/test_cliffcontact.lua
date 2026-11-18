local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
local reads, loads, warnings = 0, 0, 0
local missing, failLoad
FS = { fileExists = function(_, path) reads = reads + 1; return path ~= missing end }
log = function() warnings = warnings + 1 end
loadJsonMaterialsFile = function()
  loads = loads + 1
  if failLoad then error('unavailable material') end
end
local contact = require('beamngEarth/cliffcontact')
contact.configure(nil)
contact.configure({natural=false, roadCuts=false})
T.check(not contact.ready and reads == 0 and loads == 0, 'disabled does not access or load assets')
contact.configure({roadCuts=true})
T.check(contact.ready and loads == 1, 'road cuts independently load private material')
contact.configure({natural=true})
T.check(contact.ready and loads == 2, 'natural cliffs independently load private material')
missing = '/temp/beamngEarth/shapes/cliff_contact_v1/normal.dds'
contact.configure({natural=true})
T.check(not contact.ready and loads == 2 and warnings == 1, 'missing map prevents material load')
missing, failLoad = nil, true
contact.configure({natural=true})
T.check(not contact.ready and warnings == 2, 'failed material load disables strips')
failLoad = false
contact.configure({natural=true, roadCuts=true})
T.check(contact.ready, 'reconnection can recover after cache repair')
local before = loads
contact.onMissionStart()
T.check(contact.ready and loads == before + 1, 'mission reload recreates the private material after scene teardown')
contact.configure({})
T.check(not contact.ready, 'disabled session does not reuse previous readiness')
before = reads
contact.onMissionStart()
T.check(not contact.ready and reads == before, 'disabled mission reload does not inspect assets')
T.done('test_cliffcontact')
