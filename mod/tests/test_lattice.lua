-- luajit mod/tests/test_lattice.lua  (2.0 m world-locked lattice + height quantum, V1_2_DESIGN 1.2, 1.3, 12)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local L = require('beamngEarth/lattice')

-- the default since 2026-11-01: 1024 samples at 1 m, a 1 km block on a 128 m grid (the stock maps' resolution)
T.check(L.N == 1024 and L.SQUARE == 1.0 and L.HALF == 512 and L.GRID == 128 and L.WIDTH == 1024
  and L.VALID_W == 1023, 'default: 1024 samples at 1 m, 1 km, 128 m grid')
T.check(L.GRID / L.SQUARE == 128 and (L.blockOrigin(128, 0) / L.SQUARE) % 2 == 0,
  'a 128 m re-centre is 128 samples; the SW corner on an even index')
local a0, b0, c0, d0 = L.groundBox(128, 0)
T.check(a0 == -384 and b0 == -512 and c0 == 639 and d0 == 511, '1 m: ground box = [cx-512, cx+511]')

-- the v1.3.1 block (hello: blockSize 1024, squareSize 2)
L.configure(1024, 2.0)
T.check(L.N == 1024 and L.SQUARE == 2.0 and L.HALF == 1024, 'N = 1024 samples at exactly 2.0 m')
T.check(L.VALID_W == 2046, 'collidable width (N-1)*s = 2046 m (probe P1)')
T.check(L.COVER_W == 2048 and L.COVER_W / L.L0 == 8, 'the block sits on 8 x 8 L0 tiles')
T.check(L.GRID / L.SQUARE == 128, 'a 256 m re-centre is exactly 128 samples (even)')

local x0, y0 = L.blockOrigin(512, -256)
T.check(x0 == -512 and y0 == -1280, 'block position = (cx-1024, cy-1024)')
local a, b, c, d = L.coverBox(256, 0)
T.check(a == -768 and b == -1024 and c == 1280 and d == 1024, 'cover box = centre +- 1024')
a, b, c, d = L.groundBox(256, 0)
T.check(a == -768 and c == 1278 and d == 1022, 'ground (collidable) box = [cx-1024, cx+1022]: 2 m N/E gap')

-- the same world point is the same sample index in every block (bit-identical x)
for _, cx in ipairs({ 0, 256, -768, 4096 }) do
  for _, gx in ipairs({ 0, 1, 511, 1023 }) do
    local x = L.sampleX(cx, gx)
    T.check(x % 2 == 0 and x == cx - 1024 + 2 * gx, 'sample x is an even-metre lattice point')
  end
end
T.check(L.sampleX(0, 128 + 5) == L.sampleX(256, 5), 'block (256,0) sample 5 = block (0,0) sample 133')

-- height quantum: maxHeight = Q * 65536 (the engine divides by 65536), zBase a multiple of Q
local mh, warn = L.heightParams({ q = 1 / 64, maxHeight = 1024, zBase = 1088 })
T.check(mh == 1024 and warn == nil, 'q = 1/64: maxHeight 1024 accepted')
mh, warn = L.heightParams({ q = 1 / 64, zBase = 64 })
T.check(mh == 1024 and warn == nil, 'missing maxHeight = q * 65536')
mh, warn = L.heightParams({ q = 1 / 64, maxHeight = 65535 / 64, zBase = 64 })
T.check(mh == 65535 / 64 and warn and warn:match('q%*65536') ~= nil, 'maxHeight = q*65535 is flagged (engine divides by 65536)')
mh, warn = L.heightParams({ q = 1 / 64, maxHeight = 1024, zBase = 100.001 })
T.check(warn and warn:match('multiple') ~= nil, 'zBase off the quantum is flagged')
mh, warn = L.heightParams({ q = 0.3, maxHeight = 0.3 * 65536, zBase = 0 })
T.check(warn and warn:match('ladder') ~= nil, 'q off the ladder is flagged')
mh, warn = L.heightParams({ maxHeight = 3500, zBase = 1100 })
T.check(mh == 3500 and warn == nil, 'no q (v2 per-block scale): maxHeight as sent')
for _, q in ipairs(L.Q_LADDER) do
  T.check(L.isLadder(q) and q * 65535 >= 255 and q * 65535 <= 4096, 'ladder range 256..4096 m')
end
-- stored height zBase + q16*Q is Q*round(u/Q) whatever the zBase (same q16 grid in every block)
local Q = 1 / 64
local u = 1234.56789
for _, zb in ipairs({ 1088, 1152, 0 }) do
  local q16 = math.floor((u - zb) / Q + 0.5)
  T.near(zb + q16 * Q, Q * math.floor(u / Q + 0.5), 1e-9, 'quantised height independent of zBase ' .. zb)
end

-- M4 (docs/V1_4_SPEC.md 3.5): the 512² block from hello.blockSize (configure), centres on a 128 m grid
local n, ok = L.configure(512, 2.0)
T.check(n == 512 and ok and L.N == 512 and L.HALF == 512 and L.SQUARE == 2.0, '512: N = 512 at 2.0 m, HALF 512')
T.check(L.VALID_W == 1022 and L.COVER_W == 1024 and L.GRID == 128 and L.L0 == 256, '512: collidable 1022 m, 128 m grid, L0 unchanged')
T.check(L.GRID / L.SQUARE == 64, 'a 128 m re-centre is exactly 64 samples')
x0, y0 = L.blockOrigin(128, -384)
T.check(x0 == -384 and y0 == -896, 'block position = (cx-512, cy-512)')
for _, cx in ipairs({ 0, 128, -384, 4224 }) do
  local sx = L.blockOrigin(cx, 0)
  T.check((sx / L.SQUARE) % 2 == 0, 'SW corner on an even lattice index (Torque diagonal parity), cx ' .. cx)
end
a, b, c, d = L.groundBox(128, 0)
T.check(a == -384 and b == -512 and c == 638 and d == 510, 'ground (collidable) box = [cx-512, cx+510]')
T.check(L.sampleX(0, 64 + 5) == L.sampleX(128, 5), 'block (128,0) sample 5 = block (0,0) sample 69')
T.check(L.sampleX(128, 0) == L.sampleX(0, 0) + 128 and L.sampleX(0, 0) == -512, '512: sample 0 at cx - 512')
n, ok = L.configure(2048, 2.0)
T.check(n == 1024 and ok == false and L.N == 1024 and L.SQUARE == 1.0 and L.HALF == 512 and L.GRID == 128,
  'an unsupported size falls back to 1024 x 1 m (and says so)')
n, ok = L.configure(1024, 2.0)
T.check(n == 1024 and ok and L.COVER_W == 2048 and L.GRID == 256, 'configure(1024, 2): the v1.3.1 block')
local w
n, ok, w = L.configure(1024, 1.0)
T.check(n == 1024 and ok and w == 1024 and L.GRID == 128 and L.HALF == 512, 'configure(1024, 1): 1 km')

T.done('test_lattice')
