local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local files, copies, warnings = {}, 0, 0
local stockAvailable = true
FS = {
  directoryCreate = function() end,
  fileExists = function(_, path) return files[path] ~= nil end,
  copyFile = function(_, source, target)
    copies = copies + 1
    if stockAvailable then files[target] = source end
  end,
  triggerFilesChanged = function() end,
}
log = function() warnings = warnings + 1 end
readFile = function(path) return files[path] end
writeFile = function(path, text) files[path] = text end
jsonEncode = function() return '{}' end
jsonReadFile = function() return {cacheKey='abcdef',models={test=true}} end
loadJsonMaterialsFile = function() end
local rocks = require('beamngEarth/clifftalus')
rocks.configure(nil)
T.check(not rocks.enabled and copies == 0, 'absent opt-ins do no stock preparation')
rocks.configure({natural=false, roadCuts=false})
T.check(not rocks.enabled and copies == 0, 'disabled categories do no stock preparation')
stockAvailable = false
rocks.configure({roadCuts=true})
T.check(not rocks.enabled and warnings == 1, 'missing stock maps skip rocks with diagnostic')
stockAvailable = true
rocks.configure({roadCuts=true})
T.check(rocks.enabled, 'road cuts work without natural cliffs')
local prepared = copies
rocks.configure({natural=true})
T.check(rocks.enabled and copies == prepared, 'natural opt-in reuses prepared stock DDS')
local group = 'talus_large_a08060'
T.check(rocks.kind(group) == 'outcrop' and rocks.kind('talus_large_BAD') == nil, 'strict wire group validation')
local _, scale = rocks.pick(group, {{'large',16.6}},10,.2)
T.check(scale < 1, 'large authored outcrops are scaled down')
local materials = rocks.materials(group)
local names = rocks.materialNames(group)
T.check(materials[names.rocks_desert_ca].Stages[1].baseColorMap:match('%.dds$'), 'no runtime PNG conversion')
T.check(materials[names.rocks_desert_ca].Stages[1].vertColor == false, 'stock desert vertex colors do not override tint')
local template = 'binary\0utah_rocks_b\0lod_vertcol\0tail'
local spec = {bytes=#template,slots={{8,'utah_rocks_b'},{21,'lod_vertcol'}}}
local remapped = rocks.remapModel(template,spec,group)
T.check(remapped == 'binary\0'..names.utah_rocks_b..'\0'..names.lod_vertcol..'\0tail', 'only verified material slots change')
T.check(#remapped == #template, 'binary geometry offsets are preserved')
T.check(rocks.remapModel(template..'x',spec,group)==nil, 'truncated or stale template rejected')
T.check(rocks.remapModel(template,{bytes=#template,slots={{7,'utah_rocks_b'}}},group)==nil, 'invalid slot rejected')
T.check(rocks.materialNames('talus_a08060').utah_rocks_b==names.utah_rocks_b, 'populations share identical tint materials')
T.check(rocks.catalog(group, true) == nil, 'catalog yields after material preparation')
T.check(rocks.catalog(group, true) == nil and not rocks.available(group), 'missing model disables group instead of retrying forever')
T.check(rocks.catalog(group, true) == nil, 'failed group stays skipped')
rocks.configure({})
T.check(not rocks.available(group), 'disconnecting opt-ins disables cached groups')
local classify, calls = rocks.kind, 0
rocks.kind = function(name) calls = calls + 1; return classify(name) end
T.check(rocks.isGroup('talus_abcdef') and rocks.isGroup('talus_abcdef'), 'cliff groups are recognized')
T.check(not rocks.isGroup('ordinary_tree') and not rocks.isGroup('ordinary_tree'), 'ordinary groups stay ordinary')
T.check(calls == 2, 'positive and negative group classifications are cached across repeated forest rows')
rocks.kind = classify
T.done('test_clifftalus')
