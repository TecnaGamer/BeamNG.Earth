-- luajit mod/tests/test_winterai.lua (vehicle extension winterAi.lua: stock ai.lua's getMinPlanLen floor swapped in
-- place for winter, the AI's state kept, unpatch restoring it; refused on another function or line)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
package.path = dir .. '/../beamng_earth/lua/vehicle/extensions/?.lua;' .. package.path

-- a stand-in for ai.lua: the stock function's text, a planner that calls it, the module table
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
  'local function trafficPlan() return getMinPlanLen(20, nil, 0.2 * 0.119) end',
  'local state = { mode = "traffic" }',
  'function M.updateGFX() return trafficPlan() end',
  'function M.mode() return state.mode end',
  'function M.setEgoSpeed(v) ego.speed = v end',
  'return M' }, '\n')
ai = assert(load(SRC, '@/lua/vehicle/ai.lua'))()
local reports = {}
obj = { getId = function() return 42 end, queueGameEngineLua = function(_, c) reports[#reports + 1] = c end }
local X = require('beamngEarth/winterAi')

local stock = ai.updateGFX()
T.near(stock, 0.5 * 400 / (0.2 * 9.81), 1e-9, 'stock: the 0.2 g floor')
T.check(X.patch(99, 0.05) == false and not X.isPatched(), 'another line: refused')
T.check(X.patch(5, 0.05) == true and X.isPatched(), 'the stock function at its line: patched')
T.near(ai.updateGFX(), 0.5 * 400 / (0.05 * 9.81), 1e-9, 'patched: the planner plans for 0.05 g (0.2 x 0.119 floored)')
ai.setEgoSpeed(10)
T.near(ai.updateGFX(), 0.5 * 100 / (0.05 * 9.81), 1e-9, 'the AI\'s own ego seen (shared upvalue)')
T.check(ai.mode() == 'traffic', 'the AI state untouched')
X.unpatch()
T.near(ai.updateGFX(), math.max(20, 0.5 * 100 / (0.2 * 9.81)), 1e-9, 'unpatched: stock again')
X.apply(5, 0.05)
T.check(X.isPatched() and reports[#reports]:find('aiPatchResult(42, true, true)', 1, true), 'apply reports success')
X.apply(nil)
T.check(not X.isPatched() and reports[#reports]:find('aiPatchResult(42, false, false)', 1, true), 'unpatch reported')
local saved = debug
debug = nil
X.apply(5, 0.05)
T.check(not X.isPatched() and reports[#reports]:find('aiPatchResult(42, false, true)', 1, true), 'no debug library: false')
debug = saved

T.done('test_winterai')
