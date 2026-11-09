-- Elapsed-milliseconds stopwatch for frame budgets and commit timings.
-- GE code measures with hptimer(): stopAndReset() returns the ms since the last reset
-- (core/cameraModes/external.lua:270-283, common/luaProfiler.lua:82-83; ge_utils.lua:428-430 uses
-- :stop()). Plain luajit (tests) falls back to socket.gettime, then os.clock. os.clock alone would
-- be the process CPU time summed over all of the game's threads, not wall time.

local M = {}

-- A monotonic wall clock in ms (arbitrary origin), backed by one hptimer.
function M.clock()
  local hp = rawget(_G, 'hptimer')
  if hp then
    local ok, t = pcall(hp)
    if ok and t and t.stopAndReset then
      local acc = 0
      t:stopAndReset()
      return function() acc = acc + t:stopAndReset(); return acc end
    end
  end
  local okS, socket = pcall(require, 'socket')
  local clock = (okS and type(socket) == 'table' and socket.gettime) or os.clock
  local t0 = clock()
  return function() return (clock() - t0) * 1000 end
end

-- Returns a function giving the ms elapsed since new() was called.
M.new = M.clock

return M
