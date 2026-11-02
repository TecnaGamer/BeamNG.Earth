-- Cliff quality must never construct or mutate a live native shape.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir..'/testlib.lua')
local S = dofile(dir..'/stubs.lua')
local make = createObject
function createObject(class)
  assert(class ~= 'TSShapeConstructor', 'live cliff shape editing can crash BeamNG')
  return make(class)
end
local settings = require('beamngEarth/settings')
local sent = {}
beamngEarth_core = {sendGraphics=function() sent[#sent+1]=settings.serverGraphics() end}
for p,want in pairs({low='low',medium='medium',high='normal',ultra='high',extreme='extreme'}) do
  settings.setQuality(p)
  T.check(settings.get('cliffDetail')==want,p..' controls cliff detail')
  T.check(sent[#sent].cliffDetail==want,p..' sends cliff detail to server')
end
settings.setQuality('high')
for _,level in ipairs({'low','extreme','medium','high','normal','low','extreme','normal'}) do
  settings.set('cliffDetail',level)
  T.check(sent[#sent].cliffDetail==level,'detail-only change sends '..level)
end
settings.set('cliffDetail','medium')
T.check(settings.get('quality')=='custom','manual cliff detail changes preset to Custom')
local count=#sent
settings.set('cliffDetail','bogus')
T.check(#sent==count and settings.get('cliffDetail')=='medium','invalid setting rejected')
T.check(package.loaded['beamngEarth/clifflod']==nil,'no native cliff-edit module loaded')
T.done('test_clifflod')
