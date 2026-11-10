-- Direction signs (dirsigns.lua; server dirsigns.py): a sign tile's board faces make their Materials before the shape,
-- one a name; the setting is off by default.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local dirsigns = require('beamngEarth/dirsigns')
local settings = require('beamngEarth/settings')

T.check(settings.defaults().directionSigns == true, 'direction signs are on by default (the user, 2026-11-05)')
T.check(dirsigns.valid({ bne_dir_a = '/t/a.dds' }), 'a name -> path table is valid')
T.check(not dirsigns.valid({ 'x' }) and not dirsigns.valid('x') and not dirsigns.valid({ a = 1 }), 'anything else is not')

T.check(dirsigns.register({ bne_dir_a = '/t/a.dds', bne_dir_b = '/t/b.dds' }) == 2, 'two names taken')
local a = scenetree.findObject('bne_dir_a')
T.check(a ~= nil and a.fields.mapTo == 'bne_dir_a', 'a Material with mapTo = the mesh\'s material name')
T.check(a.fields.colorMap == '/t/a.dds', 'its picture is the colour map')
T.check(a.canSave == false, 'never saved with the level')
local metal = scenetree.findObject('bne_dir_metal')
T.check(metal ~= nil and metal.fields.mapTo == 'bne_dir_metal', 'the metal Material of the backs and posts exists')

local made = dirsigns.count
T.check(dirsigns.register({ bne_dir_a = '/t/a.dds' }) == 0, 'a name seen again is not made again')
T.check(dirsigns.count == made, 'and no Material is created for it')

T.check(dirsigns.register({ bne_dir_a = '/t/new/a.dds' }) == 0, 'a restarted server\'s new file is not a new name')
T.check(scenetree.findObject('bne_dir_a').fields.colorMap == '/t/new/a.dds', 'but the Material takes the new picture')

T.check(dirsigns.register(nil) == 0 and dirsigns.register({ 1, 2 }) == 0, 'garbage is skipped')
T.check(dirsigns.size() == 2, 'two names are kept')
dirsigns.reset()
T.check(dirsigns.size() == 0, 'a new level forgets them (the Materials stay, adopted when seen again)')
T.check(dirsigns.register({ bne_dir_a = '/t/a.dds' }) == 1 and dirsigns.count == made, 'adopted, not made again')
T.done('test_dirsigns')
