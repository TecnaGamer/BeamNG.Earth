-- The trees' leaves in the season (the user, 2026-10-14: "it's currently fall right now and trees are changing
-- colour"). The ground's photo is graded by the server (server/season.py, hello.season); the 3D trees are stock
-- shapes, whose leaf materials take a tint in our level: each species its own fall colour (oak rust-orange, beech
-- bronze, birch / aspen gold; conifers stay green), so mixed woods vary. The tint grows with the season's amount
-- (the same curve as season.py: ~Oct 27 at 40 N). The materials are the stock maps' too: their original factors
-- come back when our level ends (M.restore).
local M = {}

M.CHECK_S = 5           -- leaf materials load with the first tree of their shape: looked for this often
-- species: the leaf materials (v1.5 baseColorFactor), the old-style merged billboards (diffuseColor) and the tint at
-- full fall (multiplies the material's own factor; above 1 on red: a green leaf texture cannot turn red otherwise)
M.SPECIES = {
  { mats = { 'oak_leaves_01', 'oak_leaves_02', 'oak_leaves_03', 'm_oak_leaves_distant' },
    merged = { 'm_oak_merged_foliage' }, folders = { 'trees_oak' }, tint = { 1.55, 0.72, 0.32 }, days = -12,
    winter = { 1.05, 0.68, 0.45 } },        -- oaks and beeches keep dead brown leaves through winter
  { mats = { 'beech_leaves', 'm_beech_leaves_distant' }, merged = { 'm_beech_merged_foliage' }, folders = { 'trees_beech' },
    tint = { 1.6, 1.0, 0.35 }, days = 0, winter = { 1.15, 0.8, 0.5 } },
  { mats = { 'Birch_Leaves_01', 'm_birch_leaves_distant' }, merged = {}, folders = { 'trees_birch', 'trees_aspen' },
    tint = { 1.7, 1.3, 0.3 }, days = 10,
    winter = { 1.0, 0.9, 0.7 } },           -- (a leafless birch is not possible with its shape: pale instead)
}
-- (oaks turn last; folders: the species' tree shapes, whose far imposters (the engine's flat pictures of a whole tree,
-- materials 'imposter_mat__<shape path>') take the same tint: they kept their summer green while the 3D trees in front
-- of them had turned, the user 2026-10-29)
M.SPRING = { 0.95, 1.1, 0.8 }   -- the fresh flush, every species
M.EASE = 1.6            -- tint = amount ^ EASE: a tree holds its green early in the turn

-- Pure: how far the leaves have turned (0..1) on day-of-year doy at latitude lat (server/season.py
-- season_amounts: none in the tropics, the southern hemisphere half a year later).
function M.fallAmount(doy, lat)
  if math.abs(lat) < 23 then return 0 end
  if lat < 0 then doy = (doy + 182) % 365 end
  local peak = 300 - (math.abs(lat) - 40) * 2.5
  return math.max(0, 1 - math.abs(doy - peak) / 28)
end

-- Pure: the winter and spring amounts (0..1) on day-of-year doy at latitude lat (server/season.py season_amounts).
function M.winterAmount(doy, lat)
  if math.abs(lat) < 23 then return 0 end
  if lat < 0 then doy = (doy + 182) % 365 end
  local shift = (math.abs(lat) - 40) * 2.5
  local w0, w1, r = 330 - shift, 365 + 60 + shift, 20
  local d = doy >= w0 - r and doy or doy + 365
  return math.min(math.max(0, (d - (w0 - r)) / r), math.max(0, (w1 + r - d) / r), 1)
end
function M.springAmount(doy, lat)
  if math.abs(lat) < 23 then return 0 end
  if lat < 0 then doy = (doy + 182) % 365 end
  local shift = (math.abs(lat) - 40) * 2.5
  return math.max(0, 1 - math.abs(doy - (112 + shift)) / 22)
end

-- Pure: a factor {r, g, b, a} times the tint blended by amount t.
function M.blend(orig, tint, t)
  return { orig[1] * (1 + (tint[1] - 1) * t), orig[2] * (1 + (tint[2] - 1) * t), orig[3] * (1 + (tint[3] - 1) * t),
           orig[4] or 1 }
end

local function parse(s)
  if type(s) ~= 'string' or s == '' then return { 1, 1, 1, 1 } end
  if s == 'White' then return { 1, 1, 1, 1 } end
  local v = {}
  for x in s:gmatch('[-%d%.eE]+') do v[#v + 1] = tonumber(x) end
  return { v[1] or 1, v[2] or 1, v[3] or 1, v[4] or 1 }
end

local mode = nil        -- the server's hello.season ('auto', 'fall'), nil = off
local orig = {}         -- material name -> { field, factor } as found
local applied = {}      -- material name -> the amount applied
local lastCheck = -math.huge

function M.setMode(m) mode = m end

-- Pure: the tint of a species {tint, days, winter} on day doy at lat for mode: the strongest of its fall (its own
-- timing, eased), winter and spring tints, as {r, g, b} multipliers and that amount.
function M.speciesTint(sp, mode_, doy, lat)
  local f, w, s
  if mode_ == 'fall' then f, w, s = 1, 0, 0
  elseif mode_ == 'winter' then f, w, s = 0, 1, 0
  elseif mode_ == 'spring' then f, w, s = 0, 0, 1
  elseif mode_ == 'auto' and lat then
    f = M.fallAmount(doy + (sp.days or 0), lat) ^ M.EASE
    w, s = M.winterAmount(doy, lat), M.springAmount(doy, lat)
  else
    return { 1, 1, 1 }, 0
  end
  if w >= f and w >= s and w > 0 then return sp.winter or { 1, 1, 1 }, w end
  if s > f and s > 0 then return M.SPRING, s end
  return sp.tint, f
end

local function tintOne(name, field, tint, t)
  local m = scenetree.findObject(name)
  if not m then return end
  local k = name .. '|' .. field
  if not orig[k] then orig[k] = { field, parse(m:getField(field, 0)), name } end
  local key = string.format('%.2f %.2f %.2f %.2f', tint[1], tint[2], tint[3], t)
  if applied[k] == key then return end
  local c = M.blend(orig[k][2], tint, t)
  m:setField(field, 0, string.format('%.3f %.3f %.3f %.3f', c[1], c[2], c[3], c[4]))
  m:flush()
  m:reload()
  applied[k] = key
end

-- Pure: the species (an M.SPECIES entry) of an imposter material by its name (the shape's path in it), or nil.
function M.imposterSpecies(name)
  name = tostring(name or '')
  if name:sub(1, 14) ~= 'imposter_mat__' then return nil end
  local l = name:lower()
  for _, sp in ipairs(M.SPECIES) do
    for _, f in ipairs(sp.folders or {}) do
      if l:find(f:lower(), 1, true) then return sp end
    end
  end
  return nil
end

-- The imposter materials of the species' shapes in the scene now (they load with their shapes: looked for again at
-- every check), as {name, species}
local function imposters()
  local out = {}
  for _, name in ipairs(scenetree.findClassObjects and scenetree.findClassObjects('Material') or {}) do
    local sp = M.imposterSpecies(name)
    if sp then out[#out + 1] = { name, sp } end
  end
  return out
end

-- Every frame in our level (lat: the anchor's latitude): the leaf materials tinted to the season, checked every CHECK_S.
function M.update(inOurLevel, lat)
  if not inOurLevel or not scenetree then return end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  local doy = tonumber(os.date('*t').yday)
  for _, sp in ipairs(M.SPECIES) do
    local tint, t = M.speciesTint(sp, mode, doy, lat)
    for _, n in ipairs(sp.mats) do pcall(tintOne, n, 'baseColorFactor', tint, t) end
    for _, n in ipairs(sp.merged) do pcall(tintOne, n, 'diffuseColor', tint, t) end
  end
  -- (one field: on these version 1 materials baseColorFactor and diffuseColor are the same colour, so tinting both
  -- tinted the far trees twice, brighter and redder than the 3D trees in front of them, the user 2026-10-29)
  for _, e in ipairs(imposters()) do
    local tint, t = M.speciesTint(e[2], mode, doy, lat)
    pcall(tintOne, e[1], 'baseColorFactor', tint, t)
  end
end

-- Leaving our level: the stock factors back (other levels share the materials while the game runs).
function M.restore()
  for k, o in pairs(orig) do
    local m = scenetree and scenetree.findObject(o[3] or k)
    if m then
      pcall(function()
        m:setField(o[1], 0, string.format('%.3f %.3f %.3f %.3f', o[2][1], o[2][2], o[2][3], o[2][4]))
        m:flush()
        m:reload()
      end)
    end
  end
  orig, applied = {}, {}
end

return M
