-- Bare deciduous trees in winter (winter plan W4, the user's "white Christmas" build; server BNE_WINTER). Loaded only
-- when the server has winter on (seasonlook.setSnow: hello.snow): with winter off nothing here is read, written or
-- loaded and every tree is the stock shape it always was.
--
-- How (probe P8, 2026-11-24: the stock oak / beech / birch shapes have modelled branches under their leaf cards):
--   - each stock deciduous shape gets a copy in the user folder whose leaf materials are renamed to ours (the compiled
--     .cdae's material names patched in place, same length: its MessagePack strings keep their sizes, as clifftalus.lua
--     does for the cliff rocks), so its trunk, limbs and branch cards stay the stock ones and its leaf cards draw our
--     twig cards (tools/make_twig_textures.py: every leafy sprig of the stock atlas a bare twig spray of the same
--     footprint, our own procedural pixels). The crown keeps its volume as a grey-brown haze of fine twigs; oak and beech
--     cards keep some dead brown leaves (marcescence), the young beeches' most;
--   - the copies are new shapes: the engine bakes their far imposters once from the twig cards (cached for good
--     beside them), so the far tree lines go bare too and no imposter material is ever touched or reloaded (GitHub
--     #535). The stock materials are not touched either (other levels share them);
--   - which trees: per tree (its fixed rank, hashed), a share of the deciduous trees by the season's winter amount
--     (seasonlook.winterAmount: the date and latitude, or the server's --season), so the turn comes tree by tree; a few
--     oaks and young beeches keep their whole crown of dead leaves (seasonlook's winter tint on the stock leaves);
--   - when: the amount is taken once, when the first deciduous tree is placed (a weekly season does not need the forest
--     made again mid-session); a snow state that already says hard frost or deep snow then takes the leaves of the
--     trees still turning (FROST);
--   - nothing per frame: one lookup per tree row as it is first placed (trees.lua caches the shape per row).
local M = {}

M.VERSION = 1           -- the copies' folder: bump with any change of the copy or of the twig textures (the engine's far
                        -- imposters are baked from them once and kept in it); the materials' own signature is in it too
M.TEX = '/levels/beamng_earth/art/shapes/beamngEarth/bare/'
M.FACTOR = { 0.85, 0.85, 0.92, 1 }     -- the twig colour, a touch cooler (Windows shots: sunlit far crowns read pink)
M.ALPHA_REF_FAR = 50    -- the far LOD's cards (*_distant: also what the engine bakes the far imposter from): their
                        -- coarse mips' twigs kept as a haze (Windows shots: at 70 bare park trees 200-400 m off nearly
                        -- vanished; at 40 everywhere the near crowns read as a solid brown mass; 35 here: the far
                        -- forest right, but trees 30-60 m off, already on this LOD, dark solid crowns)
M.ALPHA_REF = 100       -- the near cards: a crown overlaps many of them, so each draws only its denser twigs (Windows
                        -- probe: at 70 street oaks 20-40 m off read as dark solid crowns, real bare crowns show the sky)

-- the stock deciduous shapes' folders and their species
M.FOLDERS = {
  { '/levels/east_coast_usa/art/shapes/trees/trees_oak/', 'oak' },
  { '/levels/east_coast_usa/art/shapes/trees/trees_beech/', 'beech' },
  { '/levels/east_coast_usa/art/shapes/trees/trees_aspen/', 'birch' },
  { '/levels/automation_test_track/art/shapes/trees/trees_poplar/', 'poplar' },
}
-- stock leaf material -> ours (the same length: patched in place in the compiled shape) and its twig texture
M.RENAME = {
  oak_leaves_01 = { 'bne_twig_oak1', 'oak' }, oak_leaves_02 = { 'bne_twig_oak2', 'oak' },
  oak_leaves_03 = { 'bne_twig_oak3', 'oak' }, m_oak_leaves_distant = { 'bne_twig_oak_distant', 'oak_distant' },
  beech_leaves = { 'bne_twig_bch', 'beech' }, m_beech_leaves_distant = { 'bne_twig_beech_distant', 'beech_distant' },
  beech_blocker = { 'bne_twig_blck', 'beech_blocker' },
  Birch_Leaves_01 = { 'bne_twig_birch1', 'birch' }, m_birch_leaves_distant = { 'bne_twig_birch_distant', 'birch_distant' },
  m_poplar_leaves_01 = { 'bne_twig_poplar_01', 'poplar_01' }, m_poplar_leaves_02 = { 'bne_twig_poplar_02', 'poplar_02' },
}
-- the young beeches (bushes, small trees) keep most of their dead leaves through winter
M.YOUNG = { pattern = 'tree_beech_[bs][um][sa]', rename = { beech_leaves = { 'bne_twig_bcy', 'beech_young' } } }
-- the share of a species' trees that keep their whole crown of dead leaves (the stock shape, seasonlook's winter tint)
M.KEEP = { oak = 0.08, beech = 0.08, beech_young = 0.35, birch = 0, poplar = 0 }
-- a snow state at the latch that takes the turning trees' leaves: air this cold (degC) or snow this deep (bin D3+)
M.FROST = { airC = -8, depthBin = 3, amount = 0.75 }

-- One season look per tree at every distance (the 2026-11-26 playtest near Phoenix: "trees loose their leaves as i get
-- closer"). seasonlook.lua tinted the stock leaf materials live, but a tree's far picture (the imposter the engine baked
-- once from the untinted leaves) stayed summer green while the 3D trees in front of it were rust brown, and the far
-- clumps likewise. So while winter is on each deciduous tree takes one look for all its distances: bare (its twig copy),
-- tinted (a copy whose leaf materials are tinted clones of the stock ones, at the season's tint quantised to
-- TINT_STEPS: the engine bakes its far imposter from them, once, as for the bare copies; no material is ever reloaded),
-- or the stock tree (no tint at all: seasonlook leaves these species alone meanwhile, M.tinting).
M.TINT_STEPS = 4
M.TINT_SPECIES = { oak = 1, beech = 2, birch = 3 }       -- (seasonlook.SPECIES; the poplars have no season tint)
M.TINT_VERSION = 1      -- the tinted copies' folder: bump with any change of the copy
-- (the climate's share of the leaf cycle: seasonlook.climateFactor, the anchor's Koppen-Geiger class)
function M.climateFactor(k) return require('beamngEarth/seasonlook').climateFactor(k) end

-- Pure: the folder's species of a stock shape path (no extension), or nil.
function M.speciesOf(shape)
  if type(shape) ~= 'string' then return nil end
  for _, f in ipairs(M.FOLDERS) do
    if shape:sub(1, #f[1]) == f[1] and not shape:find('/', #f[1] + 1, true) then
      if f[2] == 'beech' and shape:sub(#f[1] + 1):find('^' .. M.YOUNG.pattern) then return 'beech', true end
      return f[2], false
    end
  end
  return nil
end

-- Pure: the renames for a stock shape {old = {new, texture}} (the young beeches' own).
function M.renamesFor(shape)
  local sp, young = M.speciesOf(shape)
  if not sp then return nil end
  if not young then return M.RENAME end
  local r = {}
  for k, v in pairs(M.RENAME) do r[k] = v end
  for k, v in pairs(M.YOUNG.rename) do r[k] = v end
  return r
end

-- Pure: a compiled shape's bytes with its materials renamed: each MessagePack fixstr (one byte 0xa0 + length, then the
-- name) of an old name replaced by the new one of the same length. -> the new bytes and the count, or nil without any,
-- with a name found twice (the material list names each once: a second hit would be inside a binary buffer) or a rename
-- of another length (the file's offsets would move). A compressed shape names none: no copy, the stock tree.
function M.patchCdae(bytes, renames)
  if type(bytes) ~= 'string' or #bytes < 16 then return nil, 0 end
  local n = 0
  local keys = {}
  for k in pairs(renames) do keys[#keys + 1] = k end
  table.sort(keys)          -- (deterministic)
  for _, old in ipairs(keys) do
    local new = renames[old][1]
    if #new ~= #old or #old >= 32 then return nil, 0 end
    local pre = string.char(0xa0 + #old)
    local c
    bytes, c = bytes:gsub(pre .. old:gsub('%p', '%%%0'), function() return pre .. new end)
    if c > 1 then return nil, 0 end       -- (a material is named once, in the material list: more is not ours to touch)
    n = n + c
  end
  if n == 0 then return nil, 0 end
  return bytes, n
end

-- Pure: the twig materials (loadJsonMaterialsFile's table): every renamed leaf material on its twig texture.
function M.materials()
  local out, seen = {}, {}
  local all = {}
  for _, v in pairs(M.RENAME) do all[#all + 1] = v end
  for _, v in pairs(M.YOUNG.rename) do all[#all + 1] = v end
  for _, v in ipairs(all) do
    local name, tex = v[1], v[2]
    if not seen[name] then
      seen[name] = true
      out[name] = { name = name, mapTo = name, class = 'Material', version = 1.5, annotation = 'NATURE',
        Stages = { { baseColorMap = M.TEX .. 'bne_twig_' .. tex .. '_b.color.dds',
          opacityMap = M.TEX .. 'bne_twig_' .. tex .. '_o.data.dds', roughnessFactor = 0.85,
          baseColorFactor = M.FACTOR }, {}, {}, {} },
        alphaTest = true, alphaRef = name:find('distant', 1, true) and M.ALPHA_REF_FAR or M.ALPHA_REF,
        doubleSided = true, invertBackFaceNormals = true }
    end
  end
  return out
end

-- Pure: FNV-1a (32 bits) of a string, as 8 hex digits.
function M.fnv(s)
  local h = 2166136261
  for c in tostring(s):gmatch('.') do
    h = bit.bxor(h, c:byte()) % 4294967296
    local lo, hi = h % 65536, math.floor(h / 65536)        -- (exact 32-bit product: each part under 2^53)
    h = ((hi * 16777619) % 65536 * 65536 + lo * 16777619) % 4294967296
  end
  return string.format('%08x', h % 4294967296)
end

-- Pure: the materials' signature (8 hex digits, FNV-1a over their fields in a fixed order): a change of a factor, the
-- alphaRef or a texture name gives the copies a new folder, so their far imposters are baked again with it.
function M.signature()
  local parts = {}
  for name, m in pairs(M.materials()) do
    local st = m.Stages[1]
    parts[#parts + 1] = table.concat({ name, st.baseColorMap, st.opacityMap, tostring(st.roughnessFactor),
      table.concat(st.baseColorFactor or {}, ','), tostring(m.alphaRef), tostring(m.doubleSided) }, '|')
  end
  table.sort(parts)
  return M.fnv(table.concat(parts, ';'))
end

-- Pure: a stock leaf material's tinted clone's name at a tint step: as long as the stock one (the compiled shape is
-- patched in place), 'b' .. the tint's kind (f fall, w winter, s spring) .. step .. its name from the fourth character on
function M.tintName(old, step, kind) return 'b' .. (kind or 'f') .. tostring(step) .. old:sub(4) end

-- Pure: the stock leaf materials (RENAME's names) of a species (the folder's), sorted.
function M.leafNames(sp)
  local out = {}
  for old, v in pairs(M.RENAME) do
    if v[2]:match('^[a-z]+') == sp then out[#out + 1] = old end
  end
  table.sort(out)
  return out
end

-- Pure: per species the season's tint of its leaves (seasonlook.speciesTint at doy / lat in mode, times the climate's
-- factor) quantised to TINT_STEPS: { [species] = { tint = {r, g, b}, step = 1..TINT_STEPS, kind = 'f' fall / 'w' winter
-- / 's' spring } }; none at step 0.
function M.tintsFor(mode, doy, lat, factor)
  local S = require('beamngEarth/seasonlook')
  local out = {}
  for sp, i in pairs(M.TINT_SPECIES) do
    local spec = S.SPECIES[i]
    local tint, t = S.speciesTint(spec, mode, doy, lat)
    local q = math.min(M.TINT_STEPS, math.floor((t or 0) * (factor or 1) * M.TINT_STEPS + 0.5))
    if q > 0 then
      out[sp] = { tint = tint, step = q, kind = (tint == spec.winter and 'w') or (tint == S.SPRING and 's') or 'f' }
    end
  end
  return out
end

-- Pure: the tinted clones (loadJsonMaterialsFile's table) of the stock leaf materials of each tinted species, from
-- the stock folders' material files (stock[species] = that folder's decoded main.materials.json): the same textures and
-- fields, the base colour factor blended towards the tint by its step (seasonlook.blend). tints: { [species] = tint }
-- (one per species) or a list of tints, each with its species (tn.sp: the leaf cycle's every kind and step).
function M.tintMaterials(tints, stock)
  local S = require('beamngEarth/seasonlook')
  local out = {}
  local list = {}
  if tints and tints[1] then list = tints
  else for sp, tn in pairs(tints or {}) do list[#list + 1] = { sp = sp, tint = tn.tint, step = tn.step, kind = tn.kind } end end
  for _, tn in ipairs(list) do
    local sp = tn.sp
    local file = stock and stock[sp]
    for _, old in ipairs(M.leafNames(sp)) do
      local src = type(file) == 'table' and file[old]
      if type(src) == 'table' and type(src.Stages) == 'table' and type(src.Stages[1]) == 'table' then
        local function copy(v)
          if type(v) ~= 'table' then return v end
          local c = {}
          for k, x in pairs(v) do c[k] = copy(x) end
          return c
        end
        local m = copy(src)
        do
          local name = M.tintName(old, tn.step, tn.kind)
          m.name, m.mapTo, m.persistentId = name, name, nil
          local f = S.blend(m.Stages[1].baseColorFactor or { 1, 1, 1, 1 }, tn.tint, tn.step / M.TINT_STEPS)
          for k = 1, 4 do f[k] = math.floor((f[k] or 1) * 10000 + 0.5) / 10000 end
          m.Stages[1].baseColorFactor = f
          out[name] = m
        end
      end
    end
  end
  return out
end

-- Pure: the exact low 32 bits of a * b (a, b < 2^32: each partial product under 2^53).
local function mul32(a, b)
  local alo, ahi = a % 65536, math.floor(a / 65536)
  return ((ahi * b) % 65536 * 65536 + alo * b) % 4294967296
end
local function u32(x) return x % 4294967296 end

-- Pure: a tree's fixed draw (0..1) from its rank (the server's, 0..1, sent to 4 decimals): MurmurHash3's finaliser of
-- the rank's bits, so trees of close ranks (a street's row) draw apart and the thinning by rank at range does not pick
-- bare or leafy ones.
function M.draw(rank)
  local h = math.floor((tonumber(rank) or 0) * 16777216) % 16777216
  h = u32(bit.bxor(h, bit.rshift(h, 16)))
  h = mul32(h, 0x85ebca6b)
  h = u32(bit.bxor(h, bit.rshift(h, 13)))
  h = mul32(h, 0xc2b2ae35)
  h = u32(bit.bxor(h, bit.rshift(h, 16)))
  return h / 4294967296
end

-- Pure: does the tree of this species (young: a young beech) and rank stand bare at the amount (0..1)?
function M.isBare(species, young, rank, amount)
  local keep = M.KEEP[young and 'beech_young' or species] or 0
  return M.draw(rank) < (amount or 0) * (1 - keep)
end

-- Pure: the bare amount (0..1) for the season mode (hello.season), day of year, latitude and the snow (hello.snow /
-- the latest snowstate): the season's winter amount (times the climate's factor, M.climateFactor); a debug preset's snow
-- shows the full winter; FROST.
function M.amountFor(mode, doy, lat, snow, state, factor)
  local S = require('beamngEarth/seasonlook')
  local a
  if mode == 'winter' then a = 1
  elseif mode == 'auto' and lat then a = S.winterAmount(doy, lat) * (tonumber(factor) or 1)
  else a = 0 end
  if type(snow) == 'table' and snow.mode and snow.mode ~= 'real' and snow.mode ~= 'none' then a = 1 end
  if type(state) == 'table' and state.winter ~= false and mode == 'auto' and lat
      and (S.fallAmount(doy, lat) > 0 or a > 0) then
    local g = type(state.ground) == 'table' and state.ground or {}
    if (tonumber(state.airC) or 99) <= M.FROST.airC or (tonumber(g[2]) or 0) >= M.FROST.depthBin then
      a = math.max(a, M.FROST.amount)
    end
  end
  return math.max(0, math.min(1, a))
end

-- The leaf cycle (server BNE_LEAF_CYCLE, the Experimental page's "Leaf cycle" under Winter; the user, 2026-11-27: "trees
-- should gradually lose their leaves"): the place's leaf calendar (anchor_ready.leaf: server leafcycle.py, Rust
-- phenology.rs: per species bud break, full leaf, first colour, leaf fall, midpoint and bare, by latitude, elevation
-- and climate) and each tree its own dates about it (its rank's draws: a spread of SPREAD days, the same for every
-- player), so a wood colours and thins over weeks tree by tree, and leafs out in spring the same way, instead of one
-- share of bare trees for the whole season. A tree's look is still one of its shapes at every distance: the stock tree,
-- a tinted copy (its stage's colour: spring's fresh green, autumn's species colour in TINT_STEPS) or its bare copy.
-- Without the calendar (the switch off): the season's amount as before.
M.LEAF_SPECIES = { oak = 1, beech = 2, birch = 3, poplar = 4 }   -- (phenology.rs SPECIES)
M.LEAFY_AT = 0.5        -- a tree with this share of its leaves or more draws them, below it its bare copy
M.SPRING_TINT_DAYS = 14 -- the fresh green lasts this long after full leaf
M.RELEAF_DAYS = 3       -- a new anchor's calendar this many days off the one taken: taken for the trees placed from then on

-- Pure: a standard normal-like draw (Irwin-Hall of four MurmurHash3 draws: mean 0, sd 1, within +-3.5) of a tree's rank
-- and a salt (each salt its own independent draw)
function M.normal(rank, salt)
  local r, s = tonumber(rank) or 0, 0
  for i = 1, 4 do s = s + M.draw((r + (salt or 0) * 0.137 + i * 0.2113) % 1) end
  return (s - 2) * math.sqrt(3)
end

-- Pure: a tree's leaves on (northern-equivalent) day d from its species' calendar row {bud, full, colour, drop, half,
-- bare} shifted by its own spring and autumn days (ds, da): the share of its leaves (0..1), their tint's kind ('s' the
-- spring flush, 'f' the autumn colour, nil none) and step (0..TINT_STEPS).
function M.leafState(row, d, ds, da)
  local bud, full = row[1] + (ds or 0), row[2] + (ds or 0)
  local col, drop, half, bare = row[3] + (da or 0), row[4] + (da or 0), row[5] + (da or 0), row[6] + (da or 0)
  local L
  if d < bud or d >= bare then L = 0
  elseif d < full then L = (d - bud) / math.max(full - bud, 1)
  elseif d < drop then L = 1
  else L = (bare - d) / math.max(bare - drop, 1) end
  local kind, t = nil, 0
  if d >= bud and d < full + M.SPRING_TINT_DAYS then
    kind, t = 's', 1 - (d - bud) / math.max(full + M.SPRING_TINT_DAYS - bud, 1)
  elseif d >= col and d < bare then
    kind, t = 'f', math.min(1, (d - col) / math.max(half - col, 1))
  end
  local step = kind and math.floor(t * M.TINT_STEPS + 0.5) or 0
  if step <= 0 then kind, step = nil, 0 end
  return L, kind, step
end

-- Pure: the northern-equivalent day of year (the southern hemisphere's seasons half a year later: season.py)
function M.leafDay(doy, lat) return (lat or 0) < 0 and (doy + 182) % 365 or doy end

-- Pure: what a tree of species sp (young: a young beech) and rank draws on northern-equivalent day d under the place's
-- leaf calendar (anchor_ready.leaf {share, cal = 4 rows, spread = {spring, autumn}}): 'stock', 'bare' or 'tint' with
-- the tint's kind and step ('w' at full step: a marcescent crown, dead leaves from leaf fall to bud break).
function M.treeLeaf(leaf, sp, young, rank, d)
  local i = M.LEAF_SPECIES[sp]
  local row = i and type(leaf) == 'table' and type(leaf.cal) == 'table' and leaf.cal[i]
  if type(row) ~= 'table' or #row < 6 then return 'stock' end
  local r = tonumber(rank) or 0
  if M.draw((r + 0.5) % 1) >= (tonumber(leaf.share) or 1) then return 'stock' end      -- (not in the cycle: evergreen-ish)
  local spread = type(leaf.spread) == 'table' and leaf.spread or {}
  local ds = M.normal(r, 1) * (tonumber(spread[1]) or 6)
  local L, kind, step = M.leafState(row, d, ds, M.normal(r, 2) * (tonumber(spread[2]) or 9))
  if L >= M.LEAFY_AT then
    if kind then return 'tint', kind, step end
    return 'stock'
  end
  -- (leafless: a marcescent crown keeps its dead leaves until its own leaves come out)
  local keep = M.KEEP[young and 'beech_young' or sp] or 0
  if keep > 0 and M.draw((r + 0.25) % 1) < keep and not (d >= row[1] + ds and d < row[2] + ds) then
    return 'tint', 'w', M.TINT_STEPS
  end
  return 'bare'
end

-- Pure: every tint the leaf cycle can ask for (each tinted species' spring, autumn and winter tints at every step), as
-- tintMaterials' list
function M.leafTints()
  local S = require('beamngEarth/seasonlook')
  local out = {}
  local sps = {}
  for sp in pairs(M.TINT_SPECIES) do sps[#sps + 1] = sp end
  table.sort(sps)
  for _, sp in ipairs(sps) do
    local spec = S.SPECIES[M.TINT_SPECIES[sp]]
    for _, k in ipairs({ { 's', S.SPRING }, { 'f', spec.tint }, { 'w', spec.winter } }) do
      for step = 1, M.TINT_STEPS do out[#out + 1] = { sp = sp, tint = k[2], step = step, kind = k[1] } end
    end
  end
  return out
end

-- state
M.ROOT = nil            -- the copies' folder (set with the game's build: a game update recompiles its shapes)
local active = false    -- winter on and the textures there
local latched = nil     -- the amount taken at the first tree
local latchedAt, reported = nil, false      -- when (os.clock), and its log line sent
local inputs = {}       -- mode, lat, snow, state, koppen: the latch's
local copies = {}       -- stock shape -> its copy's path, or false (none possible)
local tints = nil       -- the latch's tints (M.tintsFor), nil before it
local tintRoot = nil    -- the tinted copies' folder for them (its materials loaded), false: none possible
local tcopies = {}      -- stock shape (| kind step) -> its tinted copy's path, or false
local leaf = nil        -- the leaf cycle's calendar taken (anchor_ready.leaf), nil: the season's amount
local leafD = nil       -- the (northern-equivalent) day its trees are drawn for
M.stats = { copied = 0, bare = 0, kept = 0, failed = 0, stock = {}, tinted = 0 }  -- (stock: shapes with no copy possible)

local function root()
  if not M.ROOT then
    local b = tostring(rawget(_G, 'beamng_versionb') or rawget(_G, 'beamng_version') or 'x'):gsub('[^%w%.]', '_')
    M.ROOT = '/temp/beamngEarth/shapes/bare_v' .. M.VERSION .. '_' .. M.signature() .. '_' .. b .. '/'
  end
  return M.ROOT
end

-- A binary file's bytes / written whole ('rb' / 'wb': the game's readFile and writeFile open text mode).
local function readBin(path)
  local f = io and io.open and io.open(path, 'rb')
  if not f then return readFile(path) end
  local d = f:read('*all')
  f:close()
  return d
end
local function writeBin(path, data)
  local f = io and io.open and io.open(path, 'wb')
  if not f then return writeFile(path, data) end
  f:write(data)
  f:close()
  return true
end

-- The copy of a stock shape (made once: kept in the user folder), or nil.
local function copyOf(shape)
  local c = copies[shape]
  if c ~= nil then return c or nil end
  copies[shape] = false
  local renames = M.renamesFor(shape)
  if not renames then return nil end
  local out = root() .. shape:match('[^/]+$') .. '.cdae'
  if not FS:fileExists(out) then
    local bytes, n = M.patchCdae(readBin(shape .. '.cdae'), renames)
    if not bytes then                       -- (no leaf cards: a dead tree, already bare; or not readable)
      M.stats.stock[#M.stats.stock + 1] = shape:match('[^/]+$')
      return nil
    end
    writeBin(out, bytes)
    if not FS:fileExists(out) then M.stats.failed = M.stats.failed + 1; return nil end
    M.stats.copied = M.stats.copied + 1
  end
  copies[shape] = out
  return out
end

-- trees.lua (M.bareMap, set only while active): the shape a tree of rank draws in winter: its bare copy or the stock one.
local kinds = {}         -- shape -> { species, young } or false (one string walk per shape, not per tree)
function M.map(shape, rank)
  if not active then return shape end
  local k = kinds[shape]
  if k == nil then
    local sp, young = M.speciesOf(shape)
    k = sp and { sp, young } or false
    kinds[shape] = k
  end
  if not k then return shape end
  local sp, young = k[1], k[2]
  if leaf then
    if leafD == nil then
      if not inputs.lat then return shape end
      leafD = M.leafDay(tonumber(os.date('*t').yday), inputs.lat)
      local cal = leaf.cal[2] or {}
      log('I', 'beamngEarth.baretrees', string.format('leaf cycle: day %d at lat %.2f, share %s, beech %s',
        leafD, inputs.lat, tostring(leaf.share), table.concat(cal, ' ')))
      latchedAt, reported = os.clock(), false
    end
    local what, kind, step = M.treeLeaf(leaf, sp, young, rank, leafD)
    if what == 'bare' then
      local c = copyOf(shape)
      if c then M.stats.bare = M.stats.bare + 1; return c end
    end
    M.stats.kept = M.stats.kept + 1
    if what == 'tint' then
      local t = M._tintCopyOf(shape, sp, kind, step)
      if t then M.stats.tinted = M.stats.tinted + 1; return t end
    end
    return shape
  end
  if latched == nil then
    if inputs.mode == 'auto' and not inputs.lat then return shape end      -- (no anchor yet: none placed anyway)
    local doy, f = require('beamngEarth/seasondate').doy(), M.climateFactor(inputs.koppen)     -- (the PC's or the
    -- game's date, settings Season date: as the leaves' tint and the grass)
    latched = M.amountFor(inputs.mode, doy, inputs.lat, inputs.snow, inputs.state, f)
    tints, tintRoot, tcopies = M.tintsFor(inputs.mode, doy, inputs.lat, f), nil, {}
    local tl = {}
    for s, tn in pairs(tints) do tl[#tl + 1] = s .. ' ' .. tn.step end
    table.sort(tl)
    log('I', 'beamngEarth.baretrees', string.format('bare amount %.2f (season %s, lat %s, climate %s x%.2f), tints %s',
      latched, tostring(inputs.mode), tostring(inputs.lat), tostring(inputs.koppen), f,
      #tl > 0 and table.concat(tl, ', ') or 'none'))
    latchedAt, reported = os.clock(), false
  end
  if not M.isBare(sp, young, rank, latched) then
    M.stats.kept = M.stats.kept + 1
    local t = tints and tints[sp] and M._tintCopyOf(shape, sp)
    if t then M.stats.tinted = M.stats.tinted + 1; return t end
    return shape
  end
  local c = copyOf(shape)
  if not c then return shape end
  M.stats.bare = M.stats.bare + 1
  return c
end

-- The tinted copies' folder for the latch's tints, its materials written and loaded (once per latch, before any copy
-- in it loads: a shape whose materials do not resolve bakes a no-texture imposter, kept for good); false: none possible.
local function tintFolder()
  if tintRoot ~= nil then return tintRoot end
  tintRoot = false
  local list = leaf and M.leafTints() or tints     -- (the leaf cycle: every kind and step, one folder for good)
  local stock = {}
  for sp in pairs(M.TINT_SPECIES) do
    for _, f in ipairs(M.FOLDERS) do
      if f[2] == sp and jsonReadFile then
        local ok, t = pcall(jsonReadFile, f[1] .. 'main.materials.json')
        if ok and type(t) == 'table' then stock[sp] = t end
      end
    end
  end
  local mats = M.tintMaterials(list, stock)
  if next(mats) == nil then return false end
  local text = jsonEncode(mats)
  local b = tostring(rawget(_G, 'beamng_versionb') or rawget(_G, 'beamng_version') or 'x'):gsub('[^%w%.]', '_')
  local dir = '/temp/beamngEarth/shapes/tint_v' .. M.TINT_VERSION .. '_' .. M.fnv(text) .. '_' .. b .. '/'
  FS:directoryCreate(dir)
  local mat = dir .. 'tints.materials.json'
  if readFile(mat) ~= text then writeFile(mat, text) end
  loadJsonMaterialsFile(mat)
  tintRoot = dir
  return dir
end

-- The tinted copy of a stock shape of a tinted species (made once per tint: kept in the user folder), or nil; kind and
-- step: the leaf cycle's tint, else the latch's for the species.
function M._tintCopyOf(shape, sp, kind, step)
  local key = kind and (shape .. '|' .. kind .. step) or shape
  local c = tcopies[key]
  if c ~= nil then return c or nil end
  tcopies[key] = false
  local tn = kind and M.TINT_SPECIES[sp] and { kind = kind, step = step } or (not kind and tints and tints[sp])
  local dir = tn and tintFolder()
  if not dir then return nil end
  local renames = {}
  for _, old in ipairs(M.leafNames(sp)) do renames[old] = { M.tintName(old, tn.step, tn.kind) } end
  local out = dir .. shape:match('[^/]+$') .. (kind and ('_' .. kind .. step) or '') .. '.cdae'
  if not FS:fileExists(out) then
    local bytes = M.patchCdae(readBin(shape .. '.cdae'), renames)
    if not bytes then return nil end        -- (no leaf cards: the stock shape, untinted as its far picture)
    writeBin(out, bytes)
    if not FS:fileExists(out) then M.stats.failed = M.stats.failed + 1; return nil end
    M.stats.copied = M.stats.copied + 1
  end
  tcopies[key] = out
  return out
end

-- seasonlook.lua: while this is on (winter, the twig textures there) every deciduous tree's season look is its shape's
-- (bare, tinted or stock): no live tint of the stock leaf materials, which the far pictures never had.
function M.tinting() return active end

-- seasonlook.lua: winter's snow on (hello.snow) and the season's mode as they come; nil snow: winter off.
local function activate()
  if active then return true end
  if not (FS and readFile and writeFile and loadJsonMaterialsFile) then return false end
  if not FS:fileExists(M.TEX .. 'bne_twig_oak_b.color.dds') then
    if not M.warned then
      log('W', 'beamngEarth.baretrees', 'twig textures not installed (run install.py): the trees stay leafy')
      M.warned = true
    end
    return false
  end
  FS:directoryCreate(root())
  local mat = root() .. 'twigs.materials.json'
  local text = jsonEncode(M.materials())
  if readFile(mat) ~= text then writeFile(mat, text) end
  -- (before any copy loads: a shape whose materials do not resolve bakes a no-texture imposter, kept for good)
  loadJsonMaterialsFile(mat)
  active = true
  local trees = require('beamngEarth/trees')
  trees.bareMap = M.map
  if trees.applySeason then trees.applySeason() end       -- (the mapped leaf-off forests: our bare trees, not snags)
  return true
end
function M.set(snow, mode)
  inputs.snow, inputs.mode = snow, mode
  if snow == nil then return M.restore() end
  return activate()
end
-- One line for the server's log (its perf_log: the file players send) REPORT_S after the amount was taken: what the
-- level's trees became and the far pictures the engine baked for the copies so far (the engine writes them under
-- /temp + the copy's own path).
M.REPORT_S = 60
function M.reportLine()
  local imp = 0
  if FS and FS.findFiles and M.ROOT then
    for _, dir in ipairs({ M.ROOT, '/temp' .. M.ROOT }) do
      local ok, files = pcall(FS.findFiles, FS, dir, '*imposter*', -1, true, false)
      if ok and type(files) == 'table' then imp = imp + #files end
    end
  end
  local tl = {}
  for s, tn in pairs(tints or {}) do tl[#tl + 1] = s .. ' ' .. tn.kind .. tn.step end
  table.sort(tl)
  if leaf then tl[#tl + 1] = 'leaf cycle day ' .. tostring(leafD) end
  return string.format('bare trees: amount %s at lat %s (climate %s), %d bare, %d kept leafy (%d tinted: %s), %d copies '
    .. 'made, %d failed, %d imposter files, no copy: %s', latched and string.format('%.2f', latched) or 'none',
    inputs.lat and string.format('%.2f', inputs.lat) or '-', tostring(inputs.koppen), M.stats.bare, M.stats.kept,
    M.stats.tinted, #tl > 0 and table.concat(tl, ', ') or 'none', M.stats.copied, M.stats.failed, imp,
    #M.stats.stock > 0 and table.concat(M.stats.stock, ' ', 1, math.min(#M.stats.stock, 6)) or '-')
end

-- seasonlook.lua's check (in our level): the season's mode, and winter back on in a level started again.
function M.resume(mode)
  inputs.mode = mode
  if inputs.snow ~= nil and not active then activate() end
  if active and latchedAt and not reported and os.clock() - latchedAt > M.REPORT_S then
    reported = true
    local line = M.reportLine()
    log('I', 'beamngEarth.baretrees', line)
    pcall(function()
      local net = require('beamngEarth/net')
      if net.isConnected() then net.send({ type = 'perf_log', lines = { line } }) end
    end)
  end
end
-- The anchor's latitude (seasonlook.lua, as it changes): a place this much farther north or south (a teleport; the
-- level starts at one place and the player is sent to another) takes its own amount from its next tree on.
M.RELATCH_DEG = 0.25
function M.setLat(lat)
  if not lat then return end
  if inputs.lat == nil or math.abs(lat - inputs.lat) > M.RELATCH_DEG then
    inputs.lat, latched, latchedAt, reported = lat, nil, nil, false
  end
end
-- The anchor's climate class (seasonlook.lua, with the latitude): taken at the next latch.
function M.setClimate(k) inputs.koppen = tonumber(k) end
function M.setState(state) if type(state) == 'table' then inputs.state = state end end
-- The anchor's leaf calendar (seasonlook.lua, anchor_ready.leaf; nil without BNE_LEAF_CYCLE): taken for the trees placed
-- from then on when there was none or its beech dates moved RELEAF_DAYS or more (a teleport; not a rebase nearby).
function M.setLeaf(l)
  if type(l) ~= 'table' or type(l.cal) ~= 'table' or type(l.cal[2]) ~= 'table' then return end
  local function off(a, b, i) return math.abs((tonumber(a.cal[2][i]) or 0) - (tonumber(b.cal[2][i]) or 0)) end
  if leaf == nil or off(l, leaf, 1) >= M.RELEAF_DAYS or off(l, leaf, 6) >= M.RELEAF_DAYS then
    if leaf == nil then tintRoot, tcopies = nil, {} end     -- (the leaf cycle's tint folder from now on)
    leaf, leafD = l, nil
  end
end

-- The level ended or winter went off: the trees placed after this are the stock ones (those placed stay until made
-- again); the next level takes its amount anew (winter's snow kept: resume).
function M.restore()
  if active then
    local trees = package.loaded['beamngEarth/trees']
    if trees then
      trees.bareMap = nil
      if trees.applySeason then trees.applySeason() end
    end
  end
  active, latched, inputs.lat, inputs.state, latchedAt = false, nil, nil, nil, nil
  tints, tintRoot, tcopies = nil, nil, {}
  leaf, leafD = nil, nil
end

function M.status()
  return { active = active, amount = latched, root = M.ROOT, copied = M.stats.copied, bare = M.stats.bare,
    kept = M.stats.kept, failed = M.stats.failed, tinted = M.stats.tinted, tints = tints, tintRoot = tintRoot,
    leaf = leaf, leafDay = leafD }
end

-- (tests and look checks: the leaf cycle's trees drawn for day d from now on; nil: today again)
function M._setDay(d) leafD = d end

function M._reset()      -- (tests)
  active, latched, inputs, copies = false, nil, {}, {}
  tints, tintRoot, tcopies = nil, nil, {}
  leaf, leafD = nil, nil
  M.ROOT = nil
end

return M
