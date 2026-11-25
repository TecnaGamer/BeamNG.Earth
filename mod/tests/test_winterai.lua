-- luajit mod/tests/test_winterai.lua (vehicle extension winterAi.lua: stock ai.lua loaded again with getMinPlanLen's
-- 0.2 g floor at 0.05 as the vehicle's `ai` (the sandbox refuses debug.setupvalue), swapped back on unswap; refused
-- when the lines at the given place are not the stock ones)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
package.path = dir .. '/../beamng_earth/lua/vehicle/extensions/?.lua;' .. package.path

-- a stand-in for ai.lua: the stock function's text at line 5, a planner that calls it
local SRC = table.concat({
  'local M = {}',
  'local max, min = math.max, math.min',
  'local g = 9.81',
  'local ego = { speed = 20 }',
  'local function getMinPlanLen(limLow, speed, accelg)',
  '  -- given current speed, distance required to come to a stop if I can decelerate at 0.2g',
  '  limLow = limLow or 150',
  '  speed = speed or ego.speed',
  '  accelg = max(0.2, accelg or 0.2)',
  '  return min(550, max(limLow, 0.5 * speed * speed / (accelg * g)))',
  'end',
  'function M.updateGFX() return getMinPlanLen(20, nil, 0.2 * 0.119) end',
  'return M' }, '\r\n')
local file = SRC
function readFile(path) return path == 'lua/vehicle/ai.lua' and file or nil end
ai = assert(load(SRC, '@lua/vehicle/ai.lua'))()
local stock = ai
local reports = {}
obj = { getId = function() return 42 end, queueGameEngineLua = function(_, c) reports[#reports + 1] = c end }
local X = require('beamngEarth/winterAi')

T.near(ai.updateGFX(), 0.5 * 400 / (0.2 * 9.81), 1e-9, 'stock: the 0.2 g floor')
T.check(X.patchedSource(SRC, 4, 0.05) == nil, 'another line: no source')
local src = X.patchedSource(SRC, 5, 0.05)
T.check(src and src:find('accelg = max(0.050, accelg or 0.2)', 1, true) and not src:find('max(0.2,', 1, true),
  'the floor line changed, CRLF read')
T.check(X.swap(99, 0.05) == false and ai == stock, 'wrong line: not swapped')
T.check(X.swap(5, 0.05) == true and ai ~= stock and X.isSwapped(), 'swapped')
T.check(package.loaded.ai == ai, 'package.loaded too')
T.near(ai.updateGFX(), 0.5 * 400 / (0.05 * 9.81), 1e-9, 'patched: plans for 0.05 g (0.2 x 0.119 floored)')
X.unswap()
T.check(ai == stock and not X.isSwapped(), 'unswapped: the stock module back')
X.apply(5, 0.05)
T.check(X.isSwapped() and reports[#reports]:find('aiPatchResult(42, true, true)', 1, true), 'apply reports success')
X.apply(nil)
T.check(ai == stock and reports[#reports]:find('aiPatchResult(42, false, false)', 1, true), 'unswap reported')
file = SRC:gsub('max%(0%.2, accelg', 'max(0.25, accelg')
X.apply(5, 0.05)
T.check(ai == stock and reports[#reports]:find('aiPatchResult(42, false, true)', 1, true), 'changed stock: refused')

T.done('test_winterai')
