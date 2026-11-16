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
--
-- Its wait is a sleep inside the game's luaPreRender, so the engine's frame metrics count it as "LuaPre" (#1170 and
-- two other players, 2026-11: LuaPre 0.1 -> 2.5-3 ms "whenever the car moves", 880 -> 250 fps on a 7950X3D; the
-- pacer is off while the car stands, governor loading mode, core.lua). That was MIN_MS used as a floor: every frame
-- faster than 4 ms was held to 4 ms (250 fps). Faster frames than that are not paced at all now (no game-speed
-- swing worth a frame there, and Windows' Sleep is whole milliseconds). A sleep that keeps overshooting its
-- request (a coarse system timer: every wait a ~15 ms Sleep) turns pacing off for the session: it only made
-- frames longer. M.stats() (core status 'pacer', the performance report) says what the pacer waits.

local M = {}

M.ENABLED = nil         -- nil: automatic (see above); true / false: forced (tests)
M.STATUS_FILE = '/temp/beamngEarth_dtsmooth.txt'   -- (tools/gcpatch/bngearth_dtsmooth.c, enginepatch.py)
M.RECHECK_S = 5
M.STALE_S = 30          -- an "applied" line older than this before our level's first paced frame: another session

M.WINDOW = 120          -- frames the target is taken over (~1-2 s)
M.EVERY = 30            -- frames between recomputes
M.PERCENTILE = 0.70     -- (0.80 held a city drive to 70.6 fps against 93.6 unpaced on a desktop, 2026-10-30)
M.TARGET_ALPHA = 0.25
M.MIN_MS, M.MAX_MS = 4.0, 33.4  -- paced only between: frames faster than 250 fps or slower than 30 fps are left alone
M.MIN_WAIT_MS = 0.3     -- a shorter wait is not done
M.OVERSHOOT_MS = 1.5    -- a sleep overshooting by more than this is a coarse one (M.slept)
M.OVERSHOOT_ALPHA = 0.05  -- (the average shown in stats)
M.OVERSHOOT_MIN = 60    -- the last this many sleeps judged: ...
M.OVERSHOOT_SHARE = 0.75  -- ... this share of them coarse: no pacing for the session (a coarse timer overshoots nearly
                          -- every sleep; one descheduled sleep, 30 ms late, must not end the pacing: Codex review)

local ring, n, head = {}, 0, 0
local target = nil
local sinceRecompute = 0
local waitAvg = 0       -- the recent frames' wait (ms, an average over paced frames: M.stats)
local overAvg, sleeps, coarse = 0, 0, false
local slRing, slBad = {}, 0    -- the last OVERSHOOT_MIN sleeps: coarse (true) or not, and how many of them are

function M.reset()
  if n == 0 and target == nil and waitAvg == 0 then return end     -- (called every frame while off: no table each time)
  ring, n, head, target, sinceRecompute, waitAvg = {}, 0, 0, nil, 0, 0
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
    if p > M.MAX_MS or p < M.MIN_MS or coarse then
      -- (slower than 30 fps, a Steam Deck: it would only cost frames; faster than 250 fps: it held every frame to
      -- MIN_MS, #1170; a coarse sleep: every wait overshoots)
      target = nil
    else
      target = target and (target + (p - target) * M.TARGET_ALPHA) or p
    end
  end
  local wait = 0
  if target then
    wait = target - frameMs
    if wait < M.MIN_WAIT_MS then wait = 0 end
  end
  waitAvg = waitAvg + (wait - waitAvg) * 0.05
  return wait
end

-- A wait done: asked `wantMs`, the sleep took `gotMs` (core.lua measures it). Sleeps that keep overshooting (a
-- system timer of ~15 ms: Windows' Sleep without a raised timer resolution) turn pacing off for this session.
function M.slept(wantMs, gotMs)
  if coarse or not wantMs or not gotMs then return end
  sleeps = sleeps + 1
  overAvg = overAvg + ((gotMs - wantMs) - overAvg) * M.OVERSHOOT_ALPHA
  local i = (sleeps - 1) % M.OVERSHOOT_MIN + 1
  local bad = (gotMs - wantMs) > M.OVERSHOOT_MS
  if slRing[i] then slBad = slBad - 1 end
  slRing[i] = bad
  if bad then slBad = slBad + 1 end
  if sleeps >= M.OVERSHOOT_MIN and slBad >= M.OVERSHOOT_SHARE * M.OVERSHOOT_MIN then
    coarse, target = true, nil
    local lg = rawget(_G, 'log')
    if lg then
      pcall(lg, 'W', 'beamngEarth', string.format('frame pacing off: its sleeps overshoot by %.1f ms on average', overAvg))
    end
  end
end

-- What the pacer does (core status 'pacer'): its target (ms, nil when not pacing), the recent frames' average wait
-- (ms: counted in the engine's LuaPre), the sleeps' average overshoot and whether that turned it off.
function M.stats()
  return { target = target, wait = waitAvg, overshoot = overAvg, coarse = coarse or nil }
end

-- (tests)
function M._resetSleeps() overAvg, sleeps, coarse, slRing, slBad = 0, 0, false, {}, 0 end

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
