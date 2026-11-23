-- luajit mod/tests/test_enginepatches.lua  (the engine patches' states from their status files; a warning when not applied)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local E = require('beamngEarth/enginepatches')

local t0 = 1795300000
-- the lines the patches write (tools/gcpatch, enginepatch*.py)
T.check(E.classify(t0 .. ' pid 12: applied: grass kept across terrain block moves (BeamNG.Earth level)', t0) == 'on', 'applied')
T.check(E.classify(t0 .. ' pid 12: ready: applied while the BeamNG.Earth level runs (found by signature)', t0) == 'on', 'ready')
T.check(E.classify((t0 + 1) .. ' launcher pid 9: ready: applied while the BeamNG.Earth level runs (launcher patch, block at 0x1)', t0) == 'on',
  'the Windows launcher\'s line')
T.check(E.classify(t0 .. ' pid 12: not applied: another game build (tf_bounds: 0 matches of its code; build id ab); stock', t0) == 'failed',
  'another game build')
T.check(E.classify(t0 .. ' pid 12: applied (BeamNG.Earth level): cell scans fast 1 bounds (found by signature; HALF APPLIED: x)', t0)
  == 'failed', 'half applied')
T.check(E.classify(t0 .. ' pid 12: off (BNGEARTH_TERFAST_OFF set)', t0) == 'off', 'off')
T.check(E.classify(t0 .. ' pid 12: stopped: mprotect refused', t0) == 'failed', 'stopped')
T.check(E.classify((t0 - 600) .. ' pid 12: not applied: another game build', t0) == 'none', 'an old line: another game run')
T.check(E.classify((t0 - 2) .. ' pid 12: applied: x', t0) == 'on', 'a line just before the level start (the poll)')
T.check(E.classify(nil, t0) == 'none' and E.classify('', t0) == 'none' and E.classify('garbage', t0) == 'none', 'no file / junk')
T.check(E.classify(t0 .. ' pid 12: something new', t0) == 'failed', 'an unknown state is not taken as applied')

-- a level start: nothing read before CHECK_S, then all four once; one warning a game run
E._reset()
local files = {
  gcpatch = (t0 + 1) .. ' pid 5: applied: grass kept',
  texkeep = (t0 + 1) .. ' pid 5: not applied: another game build (tk_create: 0 matches of its code; build id x); stock',
  terfast = (t0 + 1) .. ' pid 5: not applied: another game build (tf_bounds: 2 matches of its code; build id x); stock',
  dtsmooth = (t0 + 1) .. ' pid 5: applied: steady game speed',
}
local reads = 0
E.read = function(n) reads = reads + 1 return files[n] end
local warned = {}
local function warn(title, text) warned[#warned + 1] = { title, text } end
E.levelStart(t0)
T.check(E.pending(), 'pending after the level start')
T.check(E.tick(t0 + 1, warn) == false and reads == 0, 'nothing read in the first seconds')
T.check(E.tick(t0 + E.CHECK_S, warn) == true, 'read at CHECK_S')
T.check(reads == 4 and not E.pending() and E.done(), 'four files read once')
local s = E.states()
T.check(s.gcpatch == 'on' and s.texkeep == 'failed' and s.terfast == 'failed' and s.dtsmooth == 'on', 'the states')
T.check(#warned == 1 and warned[1][2]:find('terfast', 1, true) and warned[1][2]:find('texkeep', 1, true), 'one warning naming both')
T.check(E.tick(t0 + 10, warn) == true and reads == 4, 'nothing more once read')
-- another level start in the same game run: read again, not warned again
E.levelStart(t0 + 100)
for k in pairs(files) do files[k] = files[k]:gsub('^%d+', tostring(t0 + 101)) end
E.tick(t0 + 100 + E.CHECK_S, warn)
T.check(reads == 8 and #warned == 1, 'read again at the next level start, warned once a game run')
-- a game without the patches (old files): none, no warning
E._reset()
for k in pairs(files) do files[k] = files[k]:gsub('^%d+', tostring(t0 - 3600)) end
E.levelStart(t0)
E.tick(t0 + E.CHECK_S, warn)
s = E.states()
T.check(s.terfast == 'none' and s.texkeep == 'none' and #warned == 1, 'old files: none, no warning')
-- outside our level: never pending
E._reset()
T.check(not E.pending() and E.tick(t0, warn) == false and E.states() == nil, 'no level: nothing')

-- the telemetry setup carries the states (perfsummary.setup), known names and states only
local P = require('beamngEarth/perfsummary')
local out = P.setup({}, nil, { gcpatch = 'on', terfast = 'failed', texkeep = 'bogus', other = 'on' })
T.check(out.enginePatches and out.enginePatches.gcpatch == 'on' and out.enginePatches.terfast == 'failed'
  and out.enginePatches.texkeep == nil and out.enginePatches.other == nil, 'setup: enginePatches')
T.check(P.setup({}, nil, nil).enginePatches == nil, 'setup without the states: no field')

T.done('test_enginepatches')
