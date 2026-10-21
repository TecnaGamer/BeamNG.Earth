-- Ordered main-thread message dispatch. Poll only when the previous batch is drained: a busy
-- handler applies socket backpressure rather than letting decoded batches accumulate forever.
-- One handler is atomic and may exceed the wall budget; always run one to make progress.
local stopwatch = require('beamngEarth/stopwatch')
local M = { DRIVING_MS = 3, LOADING_MS = 8 }
local Queue = {}
Queue.__index = Queue

function M.new(opts)
  return setmetatable({ clock = opts and opts.clock or stopwatch.clock(), items = {}, head = 1, tail = 0,
    stats = { queued = 0, maxQueued = 0, processed = 0, maxProcessed = 0, total = 0, errors = 0,
      ms = 0, maxMs = 0, maxHandlerMs = 0, maxHandlerType = nil, budgetMs = M.DRIVING_MS } }, Queue)
end

function Queue:clear()
  self.items, self.head, self.tail = {}, 1, 0
  self.stats.queued = 0
end

function Queue:update(poll, handler, budgetMs, onError)
  local s, clock = self.stats, self.clock
  local started = clock()
  s.processed, s.budgetMs = 0, budgetMs
  if self.head > self.tail then
    self.items = poll() or {}
    self.head, self.tail = 1, #self.items
    s.maxQueued = math.max(s.maxQueued, self.tail)
  end
  while self.head <= self.tail do
    local msg = self.items[self.head]
    self.items[self.head] = nil
    self.head = self.head + 1
    local before = clock()
    local ok, err = pcall(handler, msg)
    local after = clock()
    local elapsed = after - before
    s.processed, s.total = s.processed + 1, s.total + 1
    if elapsed > s.maxHandlerMs then s.maxHandlerMs, s.maxHandlerType = elapsed, msg.type end
    if not ok then
      s.errors = s.errors + 1
      if onError then onError(msg, err) end
    end
    if clock() - started >= budgetMs then break end
  end
  s.queued = self.tail - self.head + 1
  if s.queued == 0 then self:clear() end -- stable head index; no shifting the remaining messages
  s.ms = clock() - started
  s.maxMs, s.maxProcessed = math.max(s.maxMs, s.ms), math.max(s.maxProcessed, s.processed)
  return s
end

return M
