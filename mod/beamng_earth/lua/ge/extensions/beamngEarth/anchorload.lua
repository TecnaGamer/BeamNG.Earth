-- Policy and user-facing text for the first ground/explicit teleport. Rebase/follow rules stay separate.
-- MAX_S: the whole first load's backstop (a first visit on a slow connection can take minutes: not 5, PR #831's own;
-- the server's 2 s heartbeat goes on through a stuck worker, so only a total bounds it); PREPARE_TRIES: server failures
-- before the map's message (the retries back off to TELEPORT_RETRY_MAX_S; #286: the start block failed once before)
local M = { MAX_S = 1800, PREPARE_TRIES = 10, IMPORT_TRIES = 3, IMPORT_RETRY_S = 2, DELAYED_S = 30 }
local labels = { preparing = 'Preparing the location', land_cover = 'Loading land cover',
  terrain = 'Building the ground', finalizing = 'Preparing the ground files' }

function M.expired(p, retry, now)
  local at = p and (p.firstAt or p.sentAt) or retry and retry.firstAt
  return at ~= nil and now - at >= M.MAX_S
end

function M.failure(kind)
  local why = kind == 'import' and 'The game could not open the ground files.'
    or kind == 'timeout' and 'The first ground did not finish loading.'
    or 'The server could not prepare the ground.'
  local logs = kind == 'import' and 'game and companion server logs' or 'companion server log'
  return why .. ' Check the ' .. logs .. ', then select the location again on the map.'
end

function M.text(now, p, retry)
  if retry then return string.format('Ground preparation failed; retrying in %d s', math.max(0, math.ceil(retry.at - now))) end
  if not p or p.purpose ~= 'teleport' then return nil end
  if p.importAt then return string.format('Opening the ground again in %d s', math.max(0, math.ceil(p.importAt - now))) end
  local elapsed = math.max(0, now - (p.firstAt or p.sentAt or now))
  local label = labels[p.stage] or 'Loading the ground'
  if now - (p.progressAt or p.sentAt or now) >= M.DELAYED_S then
    return string.format('%s (%d s; waiting for the server)', label, math.floor(elapsed))
  end
  return p.stage and string.format('%s (%d s)', label, math.floor(elapsed)) or label
end

return M
