-- Even frame pacing (the user, 2026-10-29: "make sure the game runs smoothly and that the game speed/time is consistent").
-- The engine moves the world by a smoothed frame time (a median of 3 and a slow average), not the real gap between
-- frames: while driving, our streaming made the frame times uneven over a quarter second or so, and game time ran
-- ahead of and behind the clock: 0.8-1.25x speed over quarter seconds in Baltimore, a stock map 0.99-1.01x (its frames
-- even). Correcting the time scale instead pitches every sound with it (measured: 316 Hz at 1.0x, 252 Hz at 0.8x), so
-- the frames are evened out: a frame shorter than the target waits the rest just before it is drawn (socket.sleep:
-- the thread sleeps, no core spins). The target follows the recent frames' PERCENTILE (slowly: TARGET_ALPHA a
-- recompute), so most frames come at the same spacing and a lone slow one is the engine's filter's to drop. Off while
-- loading, paused or in BeamMP (core.lua). Pure but for M.sleep.

local M = {}

M.WINDOW = 120          -- frames the target is taken over (~1-2 s)
M.EVERY = 30            -- frames between recomputes
M.PERCENTILE = 0.80
M.TARGET_ALPHA = 0.25
M.MIN_MS, M.MAX_MS = 4.0, 33.4  -- the target's range (no pacing at all when the frames run slower than 30 fps)
M.MIN_WAIT_MS = 0.3     -- a shorter wait is not done

local ring, n, head = {}, 0, 0
local target = nil
local sinceRecompute = 0

function M.reset()
  ring, n, head, target, sinceRecompute = {}, 0, 0, nil, 0
end

function M.target() return target end

local function percentile(p)
  local t = {}
  for i = 1, n do t[i] = ring[i] end
  table.sort(t)
  return t[math.max(1, math.min(n, math.floor(n * p + 0.5)))]
end

-- One frame's own time (ms, before any wait): the wait (ms) to add, 0 for none.
function M.step(frameMs)
  if not frameMs or frameMs <= 0 then return 0 end
  head = head % M.WINDOW + 1
  ring[head] = frameMs
  n = math.min(n + 1, M.WINDOW)
  sinceRecompute = sinceRecompute + 1
  if n >= M.EVERY and sinceRecompute >= M.EVERY then
    sinceRecompute = 0
    local p = percentile(M.PERCENTILE)
    if p > M.MAX_MS then
      target = nil          -- (slower than 30 fps, a Steam Deck: no pacing, it would only cost frames)
    else
      p = math.max(M.MIN_MS, p)
      target = target and (target + (p - target) * M.TARGET_ALPHA) or p
    end
  end
  if not target then return 0 end
  local wait = target - frameMs
  return wait >= M.MIN_WAIT_MS and wait or 0
end

local socket = nil
function M.sleep(ms)
  if socket == nil then
    local ok, s = pcall(require, 'socket')
    socket = ok and type(s) == 'table' and s.sleep and s or false
  end
  if socket then socket.sleep(ms / 1000) end
end

return M
