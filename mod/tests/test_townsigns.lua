-- luajit mod/tests/test_townsigns.lua  (the town entry and exit signs' faces: townsigns.lua, 2026-10-28)
-- A road tile's "townsigns" register each face once: its catalog entry (the shape without its extension, the model's
-- height), its Material (mapTo its name, the texture its colorMap; a missing file: a white plate), and the body every
-- town sign shares (an entry face's only: the exit face is on its back), which alone collides. A malformed entry is
-- skipped.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local townsigns = require('beamngEarth/townsigns')

local exists = { ['/temp/beamngEarth/s/townin_a1.png'] = true }
FS = { fileExists = function(_, p) return exists[p] == true end }
local trees = { CATALOG = {}, GROUP_MAX_M = {} }

local defs = {
  townin_a1 = { shape = '/temp/beamngEarth/s/townin_a1.dae', texture = '/temp/beamngEarth/s/townin_a1.png',
                material = 'bne_townin_a1' },
  townout_a1 = { shape = '/temp/beamngEarth/s/townout_a1.dae', texture = '/temp/beamngEarth/s/townout_a1.png',
                 material = 'bne_townout_a1' },
  broken = { texture = 'x.png' },
}
T.check(townsigns.register(trees, defs) == 2, 'two faces taken, the malformed entry skipped')
T.check(trees.CATALOG.townin_a1[1][1] == '/temp/beamngEarth/s/townin_a1', 'the shape as the catalog spells it')
T.check(trees.CATALOG.townin_a1[1][2] == townsigns.HEIGHT_M, 'the model\'s height: scale 1')
T.check(trees.GROUP_MAX_M.townin_a1 == townsigns.MAX_M, 'shown out to MAX_M')
T.check(trees.CATALOG.broken == nil, 'nothing for the malformed entry')

local m = scenetree.findObject('bne_townin_a1')
T.check(m ~= nil and m.fields.mapTo == 'bne_townin_a1', 'the face\'s Material, mapped to its name')
T.check(m.fields.colorMap == '/temp/beamngEarth/s/townin_a1.png', 'its texture')
local w = scenetree.findObject('bne_townout_a1')
T.check(w ~= nil and w.fields.colorMap == nil and w.fields.diffuseColor == townsigns.FALLBACK_COLOR,
  'a missing texture: a white plate')

local before = townsigns.count
townsigns.register(trees, { townin_a1 = defs.townin_a1 })     -- the next tile with the same commune
T.check(townsigns.count == before, 'a face\'s Material is made once')

-- A restarted server: the same face in its new session's folder (the old one deleted): the Material takes it
exists['/temp/beamngEarth/t/townin_a1.png'] = true
townsigns.register(trees, { townin_a1 = { shape = '/temp/beamngEarth/t/townin_a1.dae',
                                          texture = '/temp/beamngEarth/t/townin_a1.png', material = 'bne_townin_a1' } })
T.check(townsigns.count == before, 'still one Material')
T.check(m.fields.colorMap == '/temp/beamngEarth/t/townin_a1.png', 'pointed at the new session\'s texture')

T.check(townsigns.body('townin_a1') == townsigns.BODY, 'an entry face gets the shared body')
T.check(townsigns.body('townout_a1') == nil, 'an exit face none: it is on the entry sign\'s back')
townsigns.register(trees, { townin_be_b2 = { shape = '/t/townin_be_b2.dae', texture = '/t/x.png',
                                             material = 'bne_b2' } })
T.check(townsigns.body('townin_be_b2') == townsigns.BODY .. '_be', 'a Belgian face: the Belgian body')
T.check(townsigns.body('deciduous') == nil, 'a tree does not')
T.check(townsigns.faceSolid('townin_a1', true) == false, 'the face never collides (the body does)')
T.check(townsigns.faceSolid('rock', true) == true, 'other groups keep theirs')
T.check(townsigns.shapeOf('/a/b.dae') == '/a/b', 'shapeOf drops the extension')

-- The trees in front of a sign: hidden within CLEAR_M of it, in its anchor only; the tiles near it looked at again
local near = {}
trees.refreshNear = function(a, x, y, r) near[#near + 1] = { a, x, y, r } end
trees.hiders = {}
local groups = { 'streetlight', 'townin_a1' }
local items = { { 0, 5, 5, 0 }, { 1, 100, 200, 30 } }
T.check(townsigns.addSigns(trees, 7, items, groups) == 1, 'one sign among the props')
T.check(townsigns.addSigns(trees, 7, items, groups) == 0, 'the same tile again: nothing new')
T.check(#near == 1 and near[1][1] == 7 and near[1][2] == 100 and near[1][4] == townsigns.CLEAR_M,
  'the tiles near the new sign looked at again')
T.check(trees.hiders.townsigns == townsigns.hides, 'the hider installed')
T.check(townsigns.hides('deciduous', 103, 202, 7) == true, 'a tree 3.6 m from the sign: hidden')
T.check(townsigns.hides('conifer', 100, 200 + townsigns.CLEAR_M + 0.5, 7) == false, 'one past CLEAR_M stays')
T.check(townsigns.hides('deciduous', 103, 202, 8) == false, 'another anchor\'s tree stays')
T.check(townsigns.hides('streetlight', 101, 200, 7) == false, 'a lamp stays (not a tree)')
T.check(townsigns.hides('townin_a1', 100, 200, 7) == false, 'the sign itself stays')
for a = 8, 7 + townsigns.ANCHORS_KEPT do townsigns.addSigns(trees, a, items, groups) end
T.check(townsigns.hides('deciduous', 103, 202, 7) == false, 'an old anchor\'s signs are forgotten')
T.check(townsigns.hides('deciduous', 103, 202, 7 + townsigns.ANCHORS_KEPT) == true, 'the latest kept')

T.done('test_townsigns')
