-- The US road signs (tools/generate_us_signs.py; the user, 2026-10-25): speed limits (furniture.speed_signs' mph
-- groups 'speed15'..'speed85'), turn / curve warnings, advisory plaques and chevrons (roadsigns.py's groups). One model
-- each, face, back and post (a plaque hangs on its sign's post); their heights are the rows' (generate_us_signs.HEIGHTS),
-- so every item is drawn at scale 1. install merges them into trees.lua's CATALOG and GROUP_MAX_M.
local M = {}

local SIGNS = '/levels/beamng_earth/art/shapes/beamngEarth/'
M.SPEEDS = { 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85 }
M.ADVISORY = { 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80 }
M.HEIGHTS = { speed = 2.60, warn = 3.05, advisory = 1.90, chevron = 2.20 }
M.MAX_M = 300           -- the tile distance (m) beyond which a sign is not shown

M.CATALOG = {}
M.GROUP_MAX_M = {}
local function add(group, shape, h)
  M.CATALOG[group] = { { SIGNS .. shape, h } }
  M.GROUP_MAX_M[group] = M.MAX_M
end
for _, v in ipairs(M.SPEEDS) do add('speed' .. v, 'us_speed' .. v, M.HEIGHTS.speed) end
for _, v in ipairs(M.ADVISORY) do add('advisory' .. v, 'us_advisory' .. v, M.HEIGHTS.advisory) end
for _, kind in ipairs({ 'turn', 'curve' }) do
  for _, side in ipairs({ 'l', 'r' }) do add('warn_' .. kind .. '_' .. side, 'us_warn_' .. kind .. '_' .. side, M.HEIGHTS.warn) end
end
for _, side in ipairs({ 'l', 'r' }) do add('chevron_' .. side, 'us_chevron_' .. side, M.HEIGHTS.chevron) end

function M.install(trees)
  for g, v in pairs(M.CATALOG) do trees.CATALOG[g] = v end
  for g, m in pairs(M.GROUP_MAX_M) do trees.GROUP_MAX_M[g] = m end
end

return M
