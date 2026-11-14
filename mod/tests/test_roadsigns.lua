-- Ordinary road signs (server roadsigns.py, field rsigns of a sign tile): the setting and the field the mod loads.
-- NOT RUN on the machine it was written on (no lua / luajit there, 2026-11-07): run it with the other mod tests.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local roads = require('beamngEarth/roads')
local settings = require('beamngEarth/settings')

T.check(settings.defaults().roadSigns == true, 'road signs are on in the settings (the server flag BNE_ROADSIGNS decides)')
T.check(settings.defaults().directionSigns == true and settings.defaults().highwaySigns == true, 'the other sign switches are as before')

local found = false
for _, what in ipairs(roads.SIGN_WHATS) do if what == 'rsigns' then found = true end end
T.check(found, 'a sign tile\'s rsigns field is one of the sign shapes')
local kept = {}
for _, what in ipairs(roads.SIGN_WHATS) do kept[what] = true end
T.check(kept.exits and kept.mileposts and kept.merges and kept.speeds and kept.dirs, 'and the older ones are still there')

T.done('test_roadsigns')
