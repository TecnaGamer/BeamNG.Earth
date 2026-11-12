-- The status bar's loading line (the user, 2026-10-23: "the loading text isn't very precise and detailed. it takes a
-- few seconds before it shows up after teleporting and doesn't account for buildings or any other features than base
-- terrain"). A load starts the moment a teleport is asked for (or the session's first place) and lasts until the
-- ground, the roads and buildings, the trees and the distant land round the player are all in and have stayed so for
-- QUIET_S. While it lasts:
--   Loading 64%  |  downloading 14 files · 1.1 MB/s  ·  roads & buildings 34/52  ·  trees 10/40  ·  distant land 120/405
-- each part's tiles done of those wanted (the world server's load_progress) and still listed while the mod is still
-- placing what came; "ground" while the block under the car is still to be replaced (the graded first block, its full
-- photo with the roads painted in: the user saw the road texture change after the loading line had gone); before the
-- car is on the ground: "Loading the ground". Pure but for the clock the caller passes.
local M = {}

M.QUIET_S = 3           -- everything in this long: the load is over
M.MAX_S = 600           -- a load never shows longer than this (a tile that cannot be built)
M.SEP = '  ·  '

-- The parts in the order shown: key in the progress / the caller's info, the label.
M.PARTS = { { 'ground', 'ground' }, { 'roads', 'roads & buildings' }, { 'trees', 'trees' }, { 'far', 'distant land' } }

local load = nil        -- { since, quietSince }

-- A load begins (a teleport asked for, the first place of a session); a load already on keeps its start. kind
-- 'driving': the world fell behind a driving player (core: the server's catch-up boost or the edge guard's warning;
-- the user, 2026-11-11: "the loading bar never comes back after it hits 100% while driving"): it ends once the world
-- has caught up (info.behind false) for QUIET_S, not when every far tile is in (at speed they always are coming).
function M.start(now, kind)
  if not load then load = { since = now, kind = kind } end
end

function M.kind() return load and load.kind or nil end

-- The load is dropped (the level ended, the server went away).
function M.reset() load = nil end

function M.active() return load ~= nil end

-- The downloads the world waits on (the server's load_progress.net: { requests in flight, MB of this burst, MB/s }
-- over all its processes; the user, 2026-11-04: "add a downloading progress to the loading bar"): 'downloading
-- 14 files · 1.1 MB/s', or nil when nothing is in flight. (The MB count is not shown: the world downloads almost
-- all the time, photos and the map ahead, so a burst rarely ends and it only grew: "805.2 MB" on a first visit.)
function M.downloadText(net)
  if type(net) ~= 'table' then return nil end
  local act, rate = tonumber(net[1]) or 0, tonumber(net[3]) or 0
  if act <= 0 then return nil end
  local s = act == 1 and 'downloading 1 file' or string.format('downloading %d files', act)
  -- what most of them are, and whether the world's own request limit for that source holds them (the user,
  -- 2026-11-11: players with gigabit lines read "1 MB/s" as their connection; it is the photo services' request
  -- limits, many small tiles at a fixed number a second): 'downloading 14 files · satellite photos, rate-limited'
  local what = type(net[4]) == 'string' and net[4] ~= '' and net[4] or nil
  if what then s = s .. ' · ' .. what .. (net[5] == true and ', rate-limited' or '') end
  if rate >= 0.05 then s = s .. string.format(' · %.1f MB/s', rate) end
  return s
end

local function pair(p)
  if type(p) ~= 'table' then return nil end
  local d, n = tonumber(p[1]), tonumber(p[2])
  if not d or not n or n <= 0 then return nil end
  return math.min(d, n), n
end

-- The line now (nil: no load on, or it just ended). info: { placed = the car is on its ground (the teleport's
-- anchor in, not held for its ground), progress = the server's last load_progress for this anchor ({ roads = {d, n},
-- trees = ..., far = ... } or nil), pending = { roads = n, trees = n, far = n } what the mod still has to place }.
function M.line(now, info)
  if not load then return nil end
  info = info or {}
  if info.failure then return info.failure end
  if now - load.since > M.MAX_S then load = nil; return nil end
  if not info.placed then
    load.quietSince = nil
    return info.groundLine or 'Loading the ground'
  end
  local prog, pend = info.progress or {}, info.pending or {}
  local parts, sumD, sumN, busy = {}, 0, 0, false
  if not pair(prog.ground) then sumD, sumN = 1, 1 end   -- (no ground count from the server: the car is on it)
  for _, p in ipairs(M.PARTS) do
    local d, n = pair(prog[p[1]])
    local waiting = (tonumber(pend[p[1]]) or 0) > 0
    if d then
      sumD, sumN = sumD + d, sumN + n
      if d < n or waiting then
        busy = true
        parts[#parts + 1] = n == 1 and p[2] or string.format('%s %d/%d', p[2], d, n)   -- (the ground: one, no counts)
      end
    elseif waiting then
      busy = true
      parts[#parts + 1] = p[2]
    end
  end
  if not info.progress then busy = true end           -- (no counts from the server yet)
  if load.kind == 'driving' and not info.behind then busy = false end   -- (caught up: the line ends after QUIET_S)
  if not busy then
    load.quietSince = load.quietSince or now
    if now - load.quietSince >= M.QUIET_S then load = nil; return nil end
    return 'Loading 100%'
  end
  load.quietSince = nil
  local dl = M.downloadText(prog.net)
  if dl then table.insert(parts, 1, dl) end      -- (first, the user, 2026-11-04; it does not keep a load on by itself)
  local pct = math.min(99, math.floor(100 * sumD / sumN))
  local head = string.format('Loading %d%%', pct)
  if #parts == 0 then return head end
  return head .. '  |  ' .. table.concat(parts, M.SEP)
end

return M
