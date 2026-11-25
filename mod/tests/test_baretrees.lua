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
  T.check(B.reportLine():find('^bare trees: amount 0%.%d%d at lat 39%.29, %d+ bare') ~= nil, 'the log line: ' .. B.reportLine())
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

T.done('test_baretrees')
