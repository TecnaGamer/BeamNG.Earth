-- luajit mod/tests/test_baretrees.lua  (winter's bare deciduous trees, baretrees.lua): the stock shapes' compiled copies
-- with their leaf materials renamed in place to our twig cards, the trees bare by the season tree by tree, nothing
-- loaded or changed while winter is off.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

local seasonlook = require('beamngEarth/seasonlook')
seasonlook.setSnow(nil)
seasonlook.update(true, 39.29)
T.check(package.loaded['beamngEarth/baretrees'] == nil, 'winter off: baretrees is never loaded')

local B = require('beamngEarth/baretrees')
local ECA = '/levels/east_coast_usa/art/shapes/trees/'

-- the renames keep their length (the compiled shape's MessagePack strings are patched in place) and are fixstrs
do
  local ok, seen = true, {}
  for old, v in pairs(B.RENAME) do
    ok = ok and #old == #v[1] and #old < 32
    seen[v[1]] = true
  end
  for old, v in pairs(B.YOUNG.rename) do
    ok = ok and #old == #v[1] and B.RENAME[old] ~= nil and not seen[v[1]]
  end
  T.check(ok, 'every rename keeps its length and stays a fixstr; the young beeches\' name is their own')
end

-- species by the stock folder
T.check(B.speciesOf(ECA .. 'trees_oak/tree_oak_large_a') == 'oak', 'an oak')
local sp, young = B.speciesOf(ECA .. 'trees_beech/tree_beech_bush_b')
T.check(sp == 'beech' and young, 'a beech bush: young')
sp, young = B.speciesOf(ECA .. 'trees_beech/tree_beech_small_c')
T.check(sp == 'beech' and young, 'a small beech: young')
sp, young = B.speciesOf(ECA .. 'trees_beech/tree_beech_large_b')
T.check(sp == 'beech' and not young, 'a large beech: not')
T.check(B.speciesOf(ECA .. 'trees_aspen/tree_aspen_forest_b') == 'birch', 'an aspen: the birch cards')
T.check(B.speciesOf('/levels/automation_test_track/art/shapes/trees/trees_poplar/s_poplar_large_01') == 'poplar', 'a poplar')
T.check(B.speciesOf(ECA .. 'trees_douglasfir/tree_douglasfir_large_a') == nil, 'a conifer: none')
T.check(B.speciesOf('/levels/west_coast_usa/art/shapes/trees/trees_oak/oak_a') == nil, 'the West Coast live oaks: none')
T.check(B.speciesOf(ECA .. 'trees_oak/sub/x') == nil and B.speciesOf(nil) == nil, 'not in the folder itself: none')
T.check(B.renamesFor(ECA .. 'trees_beech/tree_beech_bush_b').beech_leaves[1] == 'bne_twig_bcy'
  and B.renamesFor(ECA .. 'trees_beech/tree_beech_large_b').beech_leaves[1] == 'bne_twig_bch',
  'the young beeches keep most of their dead leaves')

-- the in-place patch of a compiled shape
local function fix(s) return string.char(0xa0 + #s) .. s end
local cdae = 'HEAD' .. fix('Oak_bark_01') .. '\1\2' .. fix('oak_leaves_01') .. '\3' .. fix('m_oak_leaves_distant')
  .. 'xx_oak_leaves_01_' .. fix('Oak_branch_01') .. 'TAIL'
local out, n = B.patchCdae(cdae, B.RENAME)
T.check(n == 2 and #out == #cdae, 'two leaf materials renamed, the size kept: ' .. tostring(n))
T.check(out:find(fix('bne_twig_oak1'), 1, true) and out:find(fix('bne_twig_oak_distant'), 1, true)
  and out:find(fix('Oak_bark_01'), 1, true) and out:find(fix('Oak_branch_01'), 1, true), 'bark and branches kept')
T.check(out:find('xx_oak_leaves_01_', 1, true) ~= nil, 'a name inside another string (no fixstr length): kept')
T.check(B.patchCdae('HEAD' .. fix('tree_oak_dead_a') .. fix('Oak_bark_01') .. 'TAIL', B.RENAME) == nil,
  'no leaf cards (a dead tree): no copy')
T.check(B.patchCdae(cdae, { oak_leaves_01 = { 'too_long_a_name', 'x' } }) == nil, 'a rename of another length: refused')
T.check(B.patchCdae(nil, B.RENAME) == nil, 'nothing read: none')
T.check(B.patchCdae(cdae .. fix('oak_leaves_01'), B.RENAME) == nil, 'a name found twice: no copy')

-- the twig materials: each on its own textures (made by tools/make_twig_textures.py: the same names)
do
  local mats = B.materials()
  local tool = io.open(dir .. '/../../tools/make_twig_textures.py'):read('*a')
  local ok, count = true, 0
  for name, m in pairs(mats) do
    count = count + 1
    local st = m.Stages[1]
    local tex = st.baseColorMap:match('bne_twig_(.-)_b%.color%.dds$')
    ok = ok and m.name == name and m.mapTo == name and m.alphaTest and m.doubleSided
      and m.alphaRef == (name:find('distant', 1, true) and B.ALPHA_REF_FAR or B.ALPHA_REF)
      and tex and st.opacityMap == B.TEX .. 'bne_twig_' .. tex .. '_o.data.dds'
      and tool:find('\n    "' .. tex .. '": (', 1, true) ~= nil
  end
  T.check(ok and count == 12, 'twelve twig materials, each on a texture the tool makes: ' .. count)
end

-- the copies' folder carries the materials' signature: a factor change makes new copies (and new far imposters)
do
  local s0 = B.signature()
  T.check(s0:match('^%x%x%x%x%x%x%x%x$') ~= nil and s0 == B.signature(), 'a fixed 8-hex signature: ' .. s0)
  local f = B.FACTOR
  B.FACTOR = { 0.9, 0.9, 0.9, 1 }
  T.check(B.signature() ~= s0, 'another colour: another signature')
  B.FACTOR = f
  T.check(B.signature() == s0, 'back: the same')
end

-- which trees: a fixed draw per tree, the season's share bare, a few with their dead leaves
do
  local n, bare0, bare1, kept1, bare5, low, lowBare = 2000, 0, 0, 0, 0, 0, 0
  for i = 1, n do
    local rank = (i * 0.6180339887) % 1
    local d = B.draw(rank)
    T.check(d >= 0 and d < 1 and d == B.draw(rank), 'a fixed draw in 0..1')
    if B.isBare('oak', false, rank, 0) then bare0 = bare0 + 1 end
    if B.isBare('oak', false, rank, 1) then bare1 = bare1 + 1 else kept1 = kept1 + 1 end
    if B.isBare('birch', false, rank, 0.5) then bare5 = bare5 + 1 end
    if B.isBare('oak', false, rank, 0.5) and not B.isBare('oak', false, rank, 0.9) then low = low + 1 end
    if rank < 0.2 then lowBare = lowBare + (B.isBare('birch', false, rank, 0.5) and 1 or 0) end
  end
  T.check(bare0 == 0, 'amount 0: every tree leafy')
  T.check(math.abs(kept1 / n - B.KEEP.oak) < 0.03, 'deep winter: about KEEP of the oaks keep their dead leaves')
  T.check(math.abs(bare5 / n - 0.5) < 0.05, 'half the birches at amount 0.5: ' .. bare5)
  T.check(low == 0, 'a tree bare at 0.5 is bare at 0.9 too (tree by tree, never back)')
  T.check(lowBare > 0.35 * 400 and lowBare < 0.65 * 400, 'the trees kept at range (low ranks): mixed too')
end

-- ranks sent to 4 decimals, close together (a street's row): draws spread, no long runs of one kind
do
  local below, run, maxRun, prev = 0, 0, 0, nil
  for k = 1000, 2999 do
    local b = B.draw(k / 10000) < 0.5
    if b then below = below + 1 end
    run = (b == prev) and run + 1 or 1
    maxRun, prev = math.max(maxRun, run), b
  end
  T.check(math.abs(below / 2000 - 0.5) < 0.04 and maxRun < 16, 'close ranks: spread, longest run ' .. maxRun)
  T.check(math.abs(B.draw(0.1234) - 0.99797579087317) < 1e-12, 'the MurmurHash3 finaliser, exact (as Python\'s)')
end

-- the amount: the season's winter amount, a preset, the frost
do
  local nov25, jan15, jul1 = 329, 15, 182
  local a = B.amountFor('auto', nov25, 39.29)
  T.check(a > 0.7 and a < 1, 'Baltimore late November: most of the way: ' .. a)
  T.check(B.amountFor('auto', jan15, 39.29) == 1 and B.amountFor('auto', jul1, 39.29) == 0, 'January bare, July leafy')
  T.check(B.amountFor('auto', jan15, -35) == 0 and B.amountFor('auto', jul1, -40) == 1, 'the south half a year later')
  T.check(B.amountFor(nil, jan15, 39.29) == 0 and B.amountFor('fall', jan15, 39.29) == 0, 'season off / fall: leafy')
  T.check(B.amountFor('winter', jul1, 39.29) == 1, '--season winter: bare')
  T.check(B.amountFor('auto', jul1, 39.29, { mode = 'heavy' }) == 1, 'a snow preset: full winter')
  T.check(B.amountFor('auto', jul1, 39.29, { mode = 'real' }) == 0, 'real snow in July: leafy')
  local frost = { winter = true, airC = -12, ground = { 0, 0, 0 } }
  T.check(B.amountFor('auto', 300, 39.29, nil, frost) == B.FROST.amount, 'hard frost in the turn: most leaves gone')
  T.check(B.amountFor('auto', jul1, 39.29, nil, frost) == 0, 'frost outside the turn (a model glitch): none')
  T.check(B.amountFor('auto', 300, 39.29, nil, { airC = 2, ground = { 300, 4, 8 } }) == B.FROST.amount, 'deep snow too')
  T.check(B.amountFor('auto', 300, 39.29, nil, { airC = 2, ground = { 10, 1, 4 } }) < B.FROST.amount, 'a dusting: not')
end

-- in the game: winter on, the copies made once, the trees mapped, everything back at the level's end
do
  local files, reads, writes = {}, 0, 0
  local stock = ECA .. 'trees_oak/tree_oak_large_a'
  files[stock .. '.cdae'] = cdae
  files[B.TEX .. 'bne_twig_oak_b.color.dds'] = 'dds'
  FS = { fileExists = function(_, p) return files[p] ~= nil end, directoryCreate = function() end }
  readFile = function(p) reads = reads + 1; return files[p] end
  writeFile = function(p, d) writes = writes + 1; files[p] = d; return true end
  jsonEncode = function() return '{}' end
  local trees = { applied = 0 }
  function trees.applySeason() trees.applied = trees.applied + 1 end
  package.loaded['beamngEarth/trees'] = trees
  beamng_versionb = '0.40.6.0'

  seasonlook.setMode('auto')
  seasonlook.setSnow({ mode = 'real' })
  T.check(trees.bareMap == B.map and trees.applied == 1, 'winter on: the trees take the bare map')
  T.check(B.map(stock, 0.5) == stock, 'no anchor yet: the stock tree, nothing decided')
  T.check(B.status().amount == nil, '... and no amount taken')
  seasonlook.update(true, 39.29)
  local bareRank, leafyRank
  for i = 1, 200 do
    local r = (i * 0.6180339887) % 1
    if B.isBare('oak', false, r, B.amountFor('auto', tonumber(os.date('*t').yday), 39.29)) then bareRank = bareRank or r
    else leafyRank = leafyRank or r end
  end
  if bareRank then
    local c = B.map(stock, bareRank)
    T.check(c == B.ROOT .. 'tree_oak_large_a.cdae' and B.ROOT:find('0%.40%.6%.0') ~= nil, 'a bare oak: its copy ' .. c)
    T.check(files[c] and #files[c] == #cdae and files[c]:find('bne_twig_oak1', 1, true), 'the copy patched')
    local r0, w0 = reads, writes
    T.check(B.map(stock, bareRank) == c and reads == r0 and writes == w0, 'made once')
  end
  if leafyRank then T.check(B.map(stock, leafyRank) == stock, 'a leafy tree: the stock shape') end
  T.check(B.map(ECA .. 'trees_douglasfir/tree_douglasfir_large_a', 0.01) == ECA .. 'trees_douglasfir/tree_douglasfir_large_a',
    'a conifer: itself')
  T.check(B.reportLine():find('^bare trees: amount [01]%.%d%d at lat 39%.29 %(climate nil%), %d+ bare') ~= nil, 'the log line: ' .. B.reportLine())
  local a0 = B.status().amount
  seasonlook.update(true, 39.30)
  T.check(B.status().amount == a0, 'a rebase nearby: the amount kept')
  seasonlook.update(true, 46.5)
  T.check(B.status().amount == nil, 'a teleport far north: the amount taken anew')
  B.map(stock, 0.3)
  T.check(B.status().amount == B.amountFor('auto', tonumber(os.date('*t').yday), 46.5), '... at the new place')
  seasonlook.update(true, 39.29)
  B.map(stock, 0.3)
  seasonlook.restore()
  T.check(trees.bareMap == nil and B.status().amount == nil and not B.status().active, 'the level ended: stock trees')
  seasonlook.update(true, 39.29)
  T.check(trees.bareMap == B.map, 'the next level of the session: winter again')
  seasonlook.setSnow(nil)
  T.check(trees.bareMap == nil and not B.status().active, 'winter off: stock trees')
  seasonlook.update(true, 39.29)
  T.check(trees.bareMap == nil, '... and not resumed')

  -- textures not installed (install.py without BNE_WINTER): nothing
  B._reset()
  files[B.TEX .. 'bne_twig_oak_b.color.dds'] = nil
  seasonlook.setSnow({ mode = 'real' })
  T.check(trees.bareMap == nil and not B.status().active, 'no twig textures: the trees stay leafy')
  seasonlook.setSnow(nil)
  package.loaded['beamngEarth/trees'] = nil
end

-- one season look per tree at every distance (the 2026-11-26 playtest near Phoenix): tinted copies, the climate
do
  -- the tinted clones' names: as long as the stock ones, unique over species, steps and kinds
  local seen, ok = {}, true
  for _, sp in ipairs({ 'oak', 'beech', 'birch', 'poplar' }) do
    for _, old in ipairs(B.leafNames(sp)) do
      for step = 1, B.TINT_STEPS do
        for _, kind in ipairs({ 'f', 'w', 's' }) do
          local n = B.tintName(old, step, kind)
          ok = ok and #n == #old and not seen[n:lower()] and B.RENAME[n] == nil
          seen[n:lower()] = true
        end
      end
    end
  end
  T.check(ok, 'tinted names keep their length and are unique (case-blind)')
  T.check(table.concat(B.leafNames('oak'), ' ') == 'm_oak_leaves_distant oak_leaves_01 oak_leaves_02 oak_leaves_03'
    and #B.leafNames('beech') == 3 and #B.leafNames('birch') == 2, 'the leaf materials per species')
  -- the climate: Phoenix (BWh) a quarter of the cycle, the tropics none, elsewhere all
  T.check(seasonlook.climateFactor(4) == 0.25 and seasonlook.climateFactor(6) == 0.25 and seasonlook.climateFactor(2) == 0
    and seasonlook.climateFactor(15) == 1 and seasonlook.climateFactor(nil) == 1 and seasonlook.climateFactor(0) == 1,
    'the climate factors')
  local jan15, nov30 = 15, 334
  T.check(B.amountFor('auto', jan15, 33.51, nil, nil, seasonlook.climateFactor(4)) == 0.25,
    'Phoenix in January: a quarter of its broadleaves bare')
  T.check(B.amountFor('auto', jan15, 39.29, nil, nil, 1) == 1, 'Baltimore: all')
  T.check(B.amountFor('auto', jan15, 33.51, { mode = 'heavy' }, nil, 0.25) == 1, 'a snow preset: full winter anyway')
  -- the tints: quantised steps, by kind; the climate scales them
  local t1 = B.tintsFor('auto', 318, 33.51, 1)
  T.check(t1.oak and t1.oak.kind == 'f' and t1.oak.step >= 2, 'Phoenix mid November, full cycle: oaks turned (fall)')
  local t4 = B.tintsFor('auto', 318, 33.51, 0.25)
  T.check((t4.oak == nil or t4.oak.step <= 1) and t4.birch == nil, 'with its desert climate: barely or not at all')
  local tw = B.tintsFor('auto', jan15, 39.29, 1)
  T.check(tw.oak and tw.oak.kind == 'w' and tw.oak.step == B.TINT_STEPS, 'January in Baltimore: the winter look')
  T.check(next(B.tintsFor('auto', 182, 39.29, 1)) == nil and next(B.tintsFor(nil, jan15, 39.29, 1)) == nil,
    'July / season off: no tints')
  -- the clones: the stock fields, renamed, the factor blended by the step, no persistent id
  local stock = { oak = { oak_leaves_01 = { name = 'oak_leaves_01', mapTo = 'oak_leaves_01', persistentId = 'x',
    class = 'Material', alphaRef = 90, Stages = { { baseColorMap = '/a/b.color.png' }, {}, {}, {} } } } }
  local mats = B.tintMaterials({ oak = { tint = { 2, 0.5, 0.5 }, step = 2, kind = 'f' } }, stock)
  local m = mats['bf2_leaves_01']
  T.check(m and m.name == 'bf2_leaves_01' and m.mapTo == m.name and m.persistentId == nil and m.alphaRef == 90
    and m.Stages[1].baseColorMap == '/a/b.color.png', 'a clone: renamed, the stock fields kept')
  T.check(m and m.Stages[1].baseColorFactor[1] == 1.5 and m.Stages[1].baseColorFactor[2] == 0.75
    and m.Stages[1].baseColorFactor[4] == 1, 'its factor halfway to the tint (step 2 of 4)')
  T.check(stock.oak.oak_leaves_01.name == 'oak_leaves_01' and stock.oak.oak_leaves_01.Stages[1].baseColorFactor == nil,
    'the stock table untouched')
  T.check(next(B.tintMaterials({}, stock)) == nil, 'no tints: no clones')

  -- in the game: a leafy oak in a turned season draws its tinted copy (its imposter baked from it), and seasonlook
  -- leaves the stock leaf materials alone meanwhile
  B._reset()
  local files = {}
  local shape = ECA .. 'trees_oak/tree_oak_large_a'
  files[shape .. '.cdae'] = 'HEAD' .. fix('oak_leaves_01') .. fix('m_oak_leaves_distant') .. 'TAIL'
  files[B.TEX .. 'bne_twig_oak_b.color.dds'] = 'dds'
  FS = { fileExists = function(_, p) return files[p] ~= nil end, directoryCreate = function() end }
  readFile = function(p) return files[p] end
  writeFile = function(p, d) files[p] = d; return true end
  jsonEncode = function(t) local k = {} for n in pairs(t) do k[#k + 1] = n end table.sort(k) return table.concat(k, ',') end
  local loaded = {}
  loadJsonMaterialsFile = function(p) loaded[#loaded + 1] = p end
  jsonReadFile = function(p) return p == ECA .. 'trees_oak/main.materials.json' and stock.oak or nil end
  local trees = { applySeason = function() end }
  package.loaded['beamngEarth/trees'] = trees
  local leaf = scenetree.findObject('oak_leaves_01') or createObject('Material')
  if not scenetree.findObject('oak_leaves_01') then leaf:registerObject('oak_leaves_01') end
  leaf:setField('baseColorFactor', 0, '1 1 1 1')
  seasonlook.setMode('winter')            -- (every tree's amount 1: the KEEP share stays leafy, in its winter tint)
  seasonlook.setSnow({ mode = 'real' })
  seasonlook.update(true, 39.29, 15)
  local leafy
  for i = 1, 400 do
    local r = (i * 0.6180339887) % 1
    if not B.isBare('oak', false, r, 1) then leafy = r; break end
  end
  local c = leafy and B.map(shape, leafy)
  local st = B.status()
  T.check(c and st.tintRoot and c == st.tintRoot .. 'tree_oak_large_a.cdae' and st.tintRoot:find('^/temp/beamngEarth/shapes/tint_v1_') ~= nil,
    'a leafy oak in winter: its tinted copy ' .. tostring(c))
  local w = st.tints and st.tints.oak
  T.check(c and files[c] and files[c]:find(fix(B.tintName('oak_leaves_01', w.step, w.kind)), 1, true)
    and #files[c] == #files[shape .. '.cdae'], 'the copy names the tinted clone, its size kept')
  T.check(#loaded >= 2 and loaded[#loaded]:find('tints%.materials%.json$') ~= nil, 'the clones loaded before the copy')
  T.check(B.map(shape, leafy) == c, 'made once')
  T.check(B.status().tinted >= 2, 'counted')
  -- seasonlook: no live tint of the stock leaves while the copies carry the season
  lastCheckReset = true
  seasonlook.restore()
  seasonlook.setSnow({ mode = 'real' })
  seasonlook.update(true, 39.29, 15)
  T.check(leaf:getField('baseColorFactor', 0) == '1 1 1 1', 'the stock leaf material not tinted')
  seasonlook.setSnow(nil)
  seasonlook.restore()
  seasonlook.setMode('winter')
  seasonlook.update(true, 39.29, 15)
  T.check(leaf:getField('baseColorFactor', 0) ~= '1 1 1 1', 'winter off: the live tint as before')
  seasonlook.restore()
  T.check(leaf:getField('baseColorFactor', 0) == '1.000 1.000 1.000 1.000', '... and back at the end')
  package.loaded['beamngEarth/trees'] = nil
end

-- the leaf cycle (BNE_LEAF_CYCLE): each tree its own dates about the place's calendar (phenology.rs leaf_calendar at
-- Baltimore: 39.29 N, 50 m, Cfa; rows oak, beech, birch, poplar: bud, full, colour, drop, half, bare)
local BALT = { v = 1, share = 1, spread = { 6, 9 },
  cal = { { 107.3, 135.3, 285.0, 299.0, 310.0, 332.0 }, { 101.3, 123.3, 277.0, 289.0, 300.0, 320.0 },
          { 95.3, 113.3, 267.0, 275.0, 287.0, 303.0 }, { 98.3, 118.3, 272.0, 282.0, 293.0, 310.0 } } }
do
  local row = BALT.cal[2]
  local function L(d) return (B.leafState(row, d, 0, 0)) end
  T.check(L(60) == 0 and L(101.3) == 0 and L(200) == 1 and L(330) == 0, 'bare in winter, full in summer')
  T.check(L(112) > 0.4 and L(112) < 0.6, 'half way through the leaf-out')
  T.check(L(289) == 1 and L(304.5) > 0.45 and L(304.5) < 0.55 and L(319) < 0.1, 'thinning from leaf fall to bare')
  local _, k, s = B.leafState(row, 278, 0, 0)
  T.check(k == nil and s == 0, 'first colour: no tint yet')
  _, k, s = B.leafState(row, 300, 0, 0)
  T.check(k == 'f' and s == B.TINT_STEPS, 'the midpoint: full autumn colour')
  _, k, s = B.leafState(row, 104, 0, 0)
  T.check(k == 's' and s >= 3, 'bud break: the fresh flush')
  _, k = B.leafState(row, 200, 0, 0)
  T.check(k == nil, 'summer: no tint')
  _, k, s = B.leafState(row, 296, 0, 10)
  T.check(L(296) < 1 and k == 'f' and s < B.TINT_STEPS and B.leafState(row, 296, 0, 10) == 1,
    'a tree 10 days late: still full and less coloured')
  T.check(B.leafDay(15, -35) == 197 and B.leafDay(15, 39.3) == 15, 'the south: half a year later')
  -- per-tree draws: about normal, independent of the bare draw and of each other
  local n, s1, s2, sx = 4000, 0, 0, 0
  for i = 1, n do
    local r = i / n
    local a, b = B.normal(r, 1), B.normal(r, 2)
    s1, s2, sx = s1 + a, s2 + a * a, sx + a * b
  end
  T.check(math.abs(s1 / n) < 0.06 and math.abs(s2 / n - 1) < 0.08 and math.abs(sx / n) < 0.06,
    string.format('the dates\' draws: mean %.3f var %.3f cov %.3f', s1 / n, s2 / n, sx / n))
  T.check(B.normal(0.1234, 1) == B.normal(0.1234, 1), 'deterministic')
end

-- through the autumn the wood colours and thins over weeks, tree by tree, never back
do
  local n = 1500
  local prevBare, firstDay, lastDay = {}, nil, nil
  local back = 0
  local series = {}
  for d = 260, 345, 1 do
    local bare, tinted = 0, 0
    for i = 1, n do
      local r = ((i * 0.6180339887) % 1)
      local what = B.treeLeaf(BALT, 'beech', false, r, d)
      if what ~= 'stock' and not (what == 'tint' and select(2, B.treeLeaf(BALT, 'beech', false, r, d)) ~= 'w') then
        if what == 'bare' then bare = bare + 1 end
        prevBare[i] = true                -- (leafless: bare, or a marcescent crown of dead leaves)
      elseif prevBare[i] then
        back = back + 1
      end
      if what == 'tint' then tinted = tinted + 1 end
    end
    series[d] = { bare / n, tinted / n }
    if bare > 0 and not firstDay then firstDay = d end
    if bare >= n * (1 - B.KEEP.beech - 0.03) and not lastDay then lastDay = d end
  end
  T.check(back == 0, 'a leafless beech stays leafless through the autumn')
  T.check(firstDay and firstDay < 300, 'the first beeches bare by late October: ' .. tostring(firstDay))
  T.check(lastDay == nil or lastDay - firstDay >= 21, 'the wood thins over weeks: ' .. tostring(firstDay) .. '-'
    .. tostring(lastDay))
  T.check(series[304][1] > 0.25 and series[304][1] < 0.75, 'the midpoint: about half bare: ' .. series[304][1])
  T.check(series[285][2] > 0.5 and series[285][1] < 0.05, 'mid October: most coloured, hardly any bare')
  T.check(series[270][2] < series[285][2], 'the colour comes over the weeks')
  -- marcescent oaks: about KEEP of them keep their dead leaves through winter
  local keep = 0
  for i = 1, n do
    local w, k, s = B.treeLeaf(BALT, 'oak', false, (i * 0.6180339887) % 1, 15)
    if w == 'tint' and k == 'w' and s == B.TINT_STEPS then keep = keep + 1 end
  end
  T.check(math.abs(keep / n - B.KEEP.oak) < 0.03, 'January: the marcescent oaks ' .. keep / n)
  -- spring: leaves out tree by tree
  local early, late = 0, 0
  for i = 1, n do
    local r = (i * 0.6180339887) % 1
    if B.treeLeaf(BALT, 'beech', false, r, 108) ~= 'bare' then early = early + 1 end
    if B.treeLeaf(BALT, 'beech', false, r, 130) ~= 'bare' then late = late + 1 end
  end
  T.check(early > 0.2 * n and early < 0.8 * n and late > 0.97 * n, 'spring: some leafed by mid April, all by May')
  -- a place outside the cycle (a share of none): the stock trees
  T.check(B.treeLeaf({ share = 0, cal = BALT.cal }, 'beech', false, 0.3, 15) == 'stock', 'the tropics: stock trees')
  T.check(B.treeLeaf(BALT, 'beech', false, 0.3, 200) == 'stock', 'summer: the stock tree')
end

-- in the game: the calendar taken from the anchor, the trees mapped to their own copies, one tint folder for good
do
  B._reset()
  local files = {}
  local shape = ECA .. 'trees_oak/tree_oak_large_a'
  files[shape .. '.cdae'] = 'HEAD' .. fix('oak_leaves_01') .. fix('m_oak_leaves_distant') .. 'TAIL'
  files[B.TEX .. 'bne_twig_oak_b.color.dds'] = 'dds'
  FS = { fileExists = function(_, p) return files[p] ~= nil end, directoryCreate = function() end }
  readFile = function(p) return files[p] end
  writeFile = function(p, d) files[p] = d; return true end
  jsonEncode = function(t) local k = {} for n in pairs(t) do k[#k + 1] = n end table.sort(k) return table.concat(k, ',') end
  local loaded = {}
  loadJsonMaterialsFile = function(p) loaded[#loaded + 1] = p end
  local stockMats = { oak_leaves_01 = { name = 'oak_leaves_01', Stages = { { baseColorFactor = { 1, 1, 1, 1 } } } },
    m_oak_leaves_distant = { name = 'm_oak_leaves_distant', Stages = { {} } } }
  jsonReadFile = function(p) return p == ECA .. 'trees_oak/main.materials.json' and stockMats or nil end
  package.loaded['beamngEarth/trees'] = { applySeason = function() end }
  seasonlook.setMode('auto')
  seasonlook.setSnow({ mode = 'real' })
  seasonlook.update(true, 39.29, 14, BALT)
  T.check(B.status().leaf == BALT, 'the anchor\'s calendar taken')
  local d = tonumber(os.date('*t').yday)
  local kinds = {}
  for i = 1, 300 do
    local r = (i * 0.6180339887) % 1
    local c = B.map(shape, r)
    local what, k, s = B.treeLeaf(BALT, 'oak', false, r, d)
    if what == 'bare' then T.check(c:find('/bare_v', 1, true) ~= nil, 'a bare oak: its twig copy')
    elseif what == 'tint' then
      T.check(c:find('_' .. k .. s .. '%.cdae$') ~= nil and files[c]:find(fix(B.tintName('oak_leaves_01', s, k)), 1, true),
        'a coloured oak: its tint\'s copy ' .. c)
    else T.check(c == shape, 'a green oak: the stock tree') end
    kinds[what] = true
  end
  T.check(B.status().leafDay == d, 'today\'s trees')
  local mats = B.tintMaterials(B.leafTints(), { oak = stockMats })
  local nm = 0
  for _ in pairs(mats) do nm = nm + 1 end
  T.check(nm == 3 * B.TINT_STEPS * 2, 'the oak\'s every kind and step: one clone each of its two leaf materials ' .. nm)
  T.check(#B.leafTints() == 3 * 3 * B.TINT_STEPS, 'three species, three kinds, every step')
  -- a rebase nearby (the same calendar within RELEAF_DAYS): kept; a teleport far away: the new one
  local near = { share = 1, spread = { 6, 9 }, cal = { BALT.cal[1], { 102, 124, 278, 290, 301, 321 }, BALT.cal[3], BALT.cal[4] } }
  seasonlook.update(true, 39.29, 14, near)
  T.check(B.status().leaf == BALT, 'a rebase nearby: the calendar kept')
  local far = { share = 1, spread = { 6, 9 }, cal = { BALT.cal[1], { 130, 150, 250, 262, 273, 293 }, BALT.cal[3], BALT.cal[4] } }
  seasonlook.update(true, 39.29, 14, far)
  T.check(B.status().leaf == far and B.status().leafDay == nil, 'a teleport: the new place\'s calendar')
  seasonlook.restore()
  T.check(B.status().leaf == nil, 'the level ended: no calendar')
  seasonlook.setSnow(nil)
  package.loaded['beamngEarth/trees'] = nil
end

T.done('test_baretrees')
