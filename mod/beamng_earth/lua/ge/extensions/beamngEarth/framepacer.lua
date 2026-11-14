-- Even frame pacing (the user, 2026-10-29: "make sure the game runs smoothly and that the game speed/time is consistent").
-- The engine moves the world by a smoothed frame time (a median of 3 and a slow average), not the real gap between
-- frames: while driving, our streaming made the frame times uneven over a quarter second or so, and game time ran
-- ahead of and behind the clock: 0.8-1.25x speed over quarter seconds in Baltimore, a stock map 0.99-1.01x (its frames
-- even). Correcting the time scale instead pitches every sound with it (measured: 316 Hz at 1.0x, 252 Hz at 0.8x), so
-- the frames are evened out: a frame shorter than the target waits the rest just before it is drawn (socket.sleep:
-- the thread sleeps, no core spins). The target follows the recent frames' PERCENTILE (slowly: TARGET_ALPHA a
-- recompute), so most frames come at the same spacing and a lone slow one is the engine's filter's to drop. Off while
-- loading, paused or in BeamMP (core.lua). Pure but for M.sleep.
--
-- Not needed where dtsmooth runs (2026-11-11, the user: "Consistency is more important than max frame rate.
-- Ideally we shouldn't need that setting and make consistent performance the default"): the engine patch dtsmooth
-- (tools/gcpatch, enginepatch.py) now keeps game time on the clock, and on a Manhattan drive (tools/framerec.py, 20 s variants
-- interleaved) the pacer added nothing to it while costing frames: game speed over 0.25 s windows p95 / p99
-- 1.11 / 1.16 without it, 1.11 / 1.15 at the 70th percentile (fps 48 -> 46), 1.11 / 1.14 at the 50th; the 1.9
-- default (stock filter + this pacer) 1.23 / 1.37 at 38 fps against 49 without it. So it runs only where dtsmooth is
-- not applied (its status file, written by the Linux library or the Windows launcher when our level starts, does not
-- say "applied" for this session: a game started without the patch, a build the patch does not recognise).
-- No setting any more; M.ENABLED = true / false (beamngEarth_core._pacer.ENABLED from the console) forces it.

local M = {}

M.ENABLED = nil         -- nil: automatic (see above); true / false: forced (tests)
M.STATUS_FILE = '/temp/beamngEarth_dtsmooth.txt'   -- (tools/gcpatch/bngearth_dtsmooth.c, enginepatch.py)
M.RECHECK_S = 5
M.STALE_S = 30          -- an "applied" line older than this before our level's first paced frame: another session

M.WINDOW = 120          -- frames the target is taken over (~1-2 s)
M.EVERY = 30            -- frames between recomputes
M.PERCENTILE = 0.70     -- (0.80 held a city drive to 70.6 fps against 93.6 unpaced on a desktop, 2026-10-30)
M.TARGET_ALPHA = 0.25
M.MIN_MS, M.MAX_MS = 4.0, 33.4  -- the target's range (no pacing at all when the frames run slower than 30 fps)
M.MIN_WAIT_MS = 0.3     -- a shorter wait is not done

local ring, n, head = {}, 0, 0
local target = nil
local sinceRecompute = 0

function M.reset()
  if n == 0 and target == nil then return end     -- (called every frame while off: no table each time)
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

-- Pure: does the dtsmooth status text say it is applied in this session (since `since`, os.time())?
function M.dtsmoothApplied(text, since)
  if type(text) ~= 'string' or not text:find(': applied:', 1, true) then return false end
  local ts = tonumber(text:match('^(%d+) '))
  return ts ~= nil and (since == nil or ts >= since - M.STALE_S)
end

local autoOn, checkedAt, firstAt = true, nil, nil
-- Whether to pace now (core.lua asks once a frame in our level; `now` in seconds, any clock).
function M.active(now)
  if M.ENABLED ~= nil then return M.ENABLED end
  firstAt = firstAt or os.time()
  if not checkedAt or now - checkedAt >= M.RECHECK_S then
    checkedAt = now
    local read = rawget(_G, 'readFile')
    local ok, text = false, nil
    if read then ok, text = pcall(read, M.STATUS_FILE) end
    autoOn = not M.dtsmoothApplied(ok and text or nil, firstAt)
  end
  return autoOn
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
