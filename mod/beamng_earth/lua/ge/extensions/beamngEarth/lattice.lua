-- The world-locked terrain lattice and height quantum (docs/V1_2_DESIGN.md 1.2-1.3, 12 "P1").
-- Pure math, runs under plain luajit.
--
-- The TerrainBlock has N = 1024 samples at squareSize exactly 2.0 m. Sample (gx, gy) sits at
-- (cx - 1024 + 2*gx, cy - 1024 + 2*gy): every sample is an even-metre lattice point of the anchor
-- frame, and a 256 m re-centre is exactly 128 samples, so overlapping samples of two blocks are the
-- same points. The server writes real DEM data into row/column 1023 as well.
--
-- P1 (in-game, 2026-10-10): physics (castRayStatic) hits up to x0 + 2045.x and misses at 2047, so
-- the collidable width is (N-1)*s = 2046 m: [cx-1024, cx+1022] per axis. The 2 m strip
-- [cx+1022, cx+1024] on the N and E sides is not ground; the far tiles under the block (the sunk
-- edge ring, farfield.lua) cover it.
-- The engine computes h = zBase + q16 * maxHeight / 65536 (P1: dz/q = H/65536), so the server sends
-- maxHeight = Q * 65536 with zBase a multiple of Q: the same world height then maps to the same
-- q16 in every block.
--
-- Block size (docs/V1_4_SPEC.md 3.5, M4): the server's hello.blockSize picks 1024 (v1.3.1, the
-- default) or 512 (collidable [c-512, c+510], centres on a 128 m grid, so the SW corner is still an
-- even lattice index). configure(size) sets N, HALF, VALID_W, COVER_W and GRID for the session; one
-- session per process, so nothing else changes them (the luajit tests call it in their setup).

local M = {}

local floor, abs = math.floor, math.abs

M.N = 1024                         -- samples per side (the shipped layer map has the same size)
M.SQUARE = 1.0                     -- m between samples (the server's hello.squareSize; 2 m before 2026-11-01)
M.FAR_SQUARE = 2.0                 -- m: the far field's near tiles' lattice (every second block sample)
M.HALF = M.N * M.SQUARE / 2        -- block SW corner = centre - HALF (both axes)
M.VALID_W = (M.N - 1) * M.SQUARE   -- collidable width (P1)
M.COVER_W = 2 * M.HALF             -- the L0 tiles the block sits on
M.L0 = 256                         -- far-field L0 tile size (= the 2 km block's centre grid)
M.GRID_1024 = 256                  -- block centre grid by the block's width: 2 km (named for 1024² at 2 m) ...
M.GRID_512 = 128                   -- ... and 1 km (512² at 2 m, 1024² at 1 m) (flat: modsim reads these)
M.GRID = M.GRID_512
M.WIDTH = M.N * M.SQUARE           -- m: the block's width (1 km or 2 km: follow / rebase tables)
M.Q_LADDER = { 1 / 256, 1 / 128, 1 / 64, 1 / 32, 1 / 16 } -- height quanta, m (design 1.3)
M.Q_SCALE = 65536                  -- maxHeight = Q * Q_SCALE (P1)

-- The session's block: `size` samples per side `square` m apart (hello.blockSize / squareSize), a block 1 or 2 km wide
-- (anything else: 1024 x 1 m, and false as the second value). Returns the size in use, whether it was valid and the
-- block's width (m).
function M.configure(size, square)
  square = tonumber(square) or 1.0
  local w = (tonumber(size) or 0) * square
  local ok = (size == 512 or size == 1024 or size == 2048) and (w == 1024 or w == 2048)
  local n = ok and size or 1024
  M.N = n
  M.SQUARE = ok and square or 1.0
  M.HALF = n * M.SQUARE / 2
  M.VALID_W = (n - 1) * M.SQUARE
  M.COVER_W = 2 * M.HALF
  M.WIDTH = n * M.SQUARE
  M.GRID = M.WIDTH >= 2048 and M.GRID_1024 or M.GRID_512
  return n, ok, M.WIDTH
end

-- TerrainBlock position (SW corner, x/y) for a block centred at (cx, cy).
function M.blockOrigin(cx, cy) return cx - M.HALF, cy - M.HALF end

-- Area the block occupies on the L0 grid: [cx-1024, cx+1024]^2 (8 x 8 L0 tiles when cx, cy are
-- multiples of 256). Far tiles fully inside it are "covered" (farfield.lua; terrain mode only, which
-- is always 1024²).
function M.coverBox(cx, cy) return cx - M.HALF, cy - M.HALF, cx + M.HALF, cy + M.HALF end

-- Collidable / rendered ground of the block: [cx-1024, cx+1022]^2 (512²: [cx-512, cx+510]^2).
function M.groundBox(cx, cy)
  local x0, y0 = cx - M.HALF, cy - M.HALF
  return x0, y0, x0 + M.VALID_W, y0 + M.VALID_W
end

-- World x of sample index k (integer lattice index x 2.0, exact in doubles).
function M.sampleX(cx, gx) return (floor((cx - M.HALF) / M.SQUARE) + gx) * M.SQUARE end

local function isLadder(q)
  for _, v in ipairs(M.Q_LADDER) do if abs(q - v) < 1e-12 then return true end end
  return false
end
M.isLadder = isLadder

-- maxHeight to hand to importMaps for a terrain message ({maxHeight, zBase, q?}), plus a warning
-- string (or nil) when the message does not follow the quantum rule. maxHeight from the message
-- wins (it is what the server quantised with); without one it is q * 65536.
function M.heightParams(tr)
  local q = tonumber(tr.q)
  local maxH = tonumber(tr.maxHeight)
  local zBase = tonumber(tr.zBase) or 0
  if not q or q <= 0 then return maxH, nil end -- no quantum sent: v2-style per-block scaling
  local want = q * M.Q_SCALE
  local warn
  if not maxH then
    maxH = want
  elseif abs(maxH - want) > 1e-9 * want then
    warn = string.format('maxHeight %.9g is not q*65536 = %.9g', maxH, want)
  end
  local k = zBase / q
  if abs(k - floor(k + 0.5)) > 1e-6 then
    warn = (warn and (warn .. '; ') or '') .. string.format('zBase %.9g is not a multiple of q %.9g', zBase, q)
  end
  if not isLadder(q) then
    warn = (warn and (warn .. '; ') or '') .. string.format('q %.9g is not on the ladder', q)
  end
  return maxH, warn
end

return M
