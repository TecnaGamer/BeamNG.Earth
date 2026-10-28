"""Render-only dynamic-LOD tiles: the far field, and in mesh mode the near ground too
(docs/V1_2_DESIGN.md §2, §3, §6; docs/V1_3_SPEC.md §A, §B; V1_1_SPEC 2).

A quadtree in the current anchor's ENU plane. Tile (L, i, j) is 256*2^L m wide and covers
[i*S, (i+1)*S) x [j*S, (j+1)*S); its key is "L_i_j" and depends on nothing else, so a terrain
commit never re-keys a tile (v1.1 had per-block apron variants). Levels: L0 (256 m) .. L9
(131 km), and in mesh near-visual mode (v1.3 §B) also L-1 (128 m, 4 m vertex spacing) and L-2
(64 m, 2 m spacing: exactly the terrain block's physics lattice).

Selection (select()):
  - a node of level L >= 1 is refined while the player is closer than K(L) * S; in mesh mode
    a node is also refined down to L-1 within 400 m and to L-2 within 150 m of the player
    (min-level radii NEAR_RADII[32]; with 64 cells: L0 within 400 m, L-1 within 150 m, and the
    K' table, so selection stays one level coarser at the same distance);
  - hysteresis: a node refined last time stays refined until the player is (1 + h) times its
    switch distance away (h = 0.25, so the band is 25 % of the switch distance);
  - terrain mode only: nodes overlapping a forced box (the committed, pending and predicted
    terrain blocks) are refined down to L0, so the 8 x 8 L0 tiles under every such block are
    always present: the mod hides the ones under the committed block and sinks the edge ring
    (§2.3). Mesh mode has no forced boxes: the TerrainBlock is invisible (physics only);
  - everything intersecting the square of half-size RADIUS (80 km) around the player is kept.
The selected tiles tile the radius square exactly once.

Horizon mode (docs/V1_4_SPEC.md §3.6 "Radius", milestone M1h; earthd --horizon, off by default):
roots at HORIZON_MAX_LEVEL (L10, 262 km), a disc instead of the square (radius from the line-of-sight
horizon, HorizonGrid.radius) and, beyond HORIZON_MIN (80 km), a per-node horizon cull (Horizon): a
node whose nearest point is farther than sqrt(2R'h_eye) + sqrt(2R'zmax_node) is dropped with its
subtree (a child is never nearer nor higher than its parent, so this is the per-tile test). The
selected tiles then tile the disc minus the culled nodes exactly once.

LOD changes are grouped into swap groups (swap_groups()): connected sets of added and removed
tiles with the same footprint (a parent and its descendants), which the mod exchanges with a
vertical crossover (§2.5 C2).

Each tile is C x C cells plus a skirt on all 4 edges: a vertical curtain facing outwards,
max(2 m, 0.02 S, 2.5 err + 1 m) deep, so neighbours at different levels and tiles moving during a
crossover never show cracks. C is the layout's cell count (Layout, docs/V1_4_SPEC.md §3.1 M1):
32 (v1.3.1, FORMAT 3, 2048 + 256 tris; the default) or 64 (FORMAT 4, 8192 + 512 tris, every level
keeps its size so its vertex spacing halves, and the exact level is L-1). Vertex x, y are plane
positions; z is the ENU z of the DEM surface point under it (curvature included). Tiles of level
<= 0 use the terrain block's DEM zoom and height quantum, so their vertices (every 4th / 2nd /
every lattice sample for L0 / L-1 / L-2 with 32 cells; every 2nd / every sample for L0 / L-1 with
64) equal the terrain's samples bit for bit. Cells are split along the diagonal the Torque
TerrainBlock uses for the same square (torque_split45()), so an exact-level tile (L-2 with 32
cells, L-1 with 64) is the TerrainBlock's surface exactly at its vertices. Per tile the builder also measures `err` (max deviation
from a 2x finer reference surface) and, for L0 in terrain mode, `sink` (how far the tile must go
down to be under the terrain block everywhere).

Colour: with imagery (v1.3), every tile has its own texture of its footprint (imagery.py) and
UV0 = (x - x0, y - y0) / S across the footprint, plus UV1 = (x - x0, y - y0) / DETAIL_M, a
world-locked detail-map coordinate (tile origins are multiples of 64 m and DETAIL_M divides 64,
so UV1 is continuous across tiles). Without imagery (--imagery off) the colour comes from one
small palette texture shared by every tile (shipped with the mod, see write_mod_assets()): u
encodes height (with water at the far left), v encodes slope. The palette is mirrored around
v = 0.5, so the result does not depend on the engine's v-flip. Below 1500 m the land colour is
the terrain material's dirt colour, blending into the height palette by 2500 m.
"""
import json
import functools
import math
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image

import cdae
import dae
import dem as demmod
import fast
import heightmap
import imagery

BASE = 256.0            # L0 tile size
MAX_LEVEL = 9           # L9 = 131 072 m
RADIUS = 80000.0
# Tile layouts by cell count (docs/V1_4_SPEC.md §3.1 M1; Layout below). Nothing reads these at call
# time through a module setting: a Layout picks its row, and the server makes one Layout.
CELL_COUNTS = (32, 64)
# K(L) for refining a node of level L >= 1; the last entry repeats for coarser levels.
# 32 (v1.3.1, §3 recommendation): K(1) = K(2) = 3, K(>=3) = 2.5. 64: K'(L) = K(L-1) / 2, so a level
# splits at the distance where the 32-cell layout split the level below it (K'(1) = 0.75: 384 m,
# the 400 m radius of L0 wins).
K_TABLE = {32: {1: 3.0, 2: 3.0, 3: 2.5}, 64: {1: 0.75, 2: 1.5, 3: 1.5, 4: 1.25}}
# Mesh mode: everything within NEAR_RADII[cells][m] of the player is level m or finer (v1.3 §B).
NEAR_RADII = {32: {-2: 150.0, -1: 400.0}, 64: {-1: 150.0, 0: 400.0}}
# Mesh near-visual mode's finest level: the one whose vertices are the 2 m physics lattice
# (cells * 2 m = its size): L-2 = 64 m with 32 cells, L-1 = 128 m with 64.
MESH_MIN_LEVEL = {32: -2, 64: -1}
CONTENT_FORMAT = {32: 3, 64: 4}     # the protocol's FORMAT (hello.format) of each layout
# Texture edge (px) per level from the finest level of the mode, the last value repeating. 32: the
# v1.3.1 rule (two finest levels tex_near, the rest tex_far; both 256). 64 (§3.1; P8 may change it):
# 256 to L1, 512 from L2 (keeps today's texel density at distance).
TEX_DEFAULT = {32: (256, 256, 256), 64: (256, 256, 256, 512)}
BLOCK_SIZES = (1024, 512)   # terrain block samples per edge (M4, docs/V1_4_SPEC.md §3.5: 512, mesh mode only)
# Triangles per tile shape: 8192 + 512 at 64 cells. The old 8192 was the ProceduralMesh rule;
# .cdae has no such cliff (§3.1): its limit is the engine's 16-bit vertex indices (VERTS_MAX), which the fine near
# tiles (NEAR_FINE_M, up to ~2x the grid's triangles at a cutting) keep under.
TRIS_MAX = 9000
VERTS_MAX = 65535
HYSTERESIS = 0.25           # a refined node stays refined while d < (1 + h) * its switch distance
HOLD_S = 2.0                # a node changes state at most once per HOLD_S seconds (§2.4)
DETAIL_M = 2.0              # UV1 (detail map) repeat in metres; must divide 64 (see module doc)
FAR_MAX_ZOOM = 14           # DEM zoom cap for L1+ (L0 uses the heightmap's zoom)
SINK_MARGIN = 0.3           # m below the terrain everywhere for a sunk L0 tile
# Adaptive far meshes (the user, 2026-10-22: "Only use the highest resolution available when needed ... Large flat
# areas can use lower resolutions ... and far away of course uses low res"): from ADAPT_MIN_LEVEL up a tile is the
# right-triangulated irregular network of its grid (earthfast.rtin_mesh) within ADAPT_EPS of its vertex spacing of
# every grid height: flat ground a few large triangles, cliffs the whole grid. 2026-10-22, 64 cells: 75-85 % fewer
# triangles on Baltimore's and Kansas's tiles, 50-70 % at Whitney Portal, 25-35 % in Yosemite, for ~0.06 spacings
# of error (under a pixel at a level's switch distance; the grid's own sampling error is 0.3-5 spacings). Levels <= 0
# stay the 2 m lattice the roads and the terrain block meet. BNGE_FAR_ADAPT=0 turns it off.
ADAPT_MIN_LEVEL = 1
ADAPT_EPS = 0.0 if os.environ.get("BNGE_FAR_ADAPT") == "0" else 0.04
# Fine near ground (prototype, BNGE_NEAR_FINE=1; the user, 2026-10-22: "up close sharp features (like walls) more
# detailed and sharper ... instead of the mesh being forced to a grid it can be more fluid"): in mesh mode (the tiles
# drawn, the terrain block physics only) the finest near level is sampled every NEAR_FINE_M of the graded ground and
# simplified like the far tiles (rtin_mesh within NEAR_FINE_EPS of the spacing, doubled until the mesh has at most NEAR_FINE_VERTS
# vertices: the engine's 16-bit indices): a cutting's wall is a 0.5 m step instead of a 2-4 m ramp of lattice teeth,
# flat ground a few triangles. The physics stays the 2 m lattice (the walls' own colliders hold the steps).
NEAR_FINE_M = 0.5 if os.environ.get("BNGE_NEAR_FINE") == "1" else 0.0
NEAR_FINE_NEXT_M = 1.0          # ... and the level after it (L0, to ~400 m) every this
# ... and their road masks (surface.road_mask: the mod's asphalt layer) this fine, by level past the exact one: 12.5 cm
# on the 128 m tiles, 50 cm on the 256 m ones
NEAR_MASK_PX = {0: 1024, 1: 512}
NEAR_FINE_EPS = 0.1             # of the sample spacing (5 cm at 0.5 m, 10 cm at 1 m)
NEAR_FINE_VERTS = 60000
LATTICE = int(BASE / heightmap.SQUARE) + 1      # 129 terrain samples per L0 tile edge
PALETTE_W, PALETTE_H = 256, 32
WATER_U = 0.02
MATERIAL = "bngEarthFar_v3"     # static material shipped with the mod (§6.2)
# Terrain material's macro colour (mean of levels/beamng_earth/art/terrains/bngEarth_base_b.png).
DIRT = (0.39, 0.35, 0.27)
DIRT_UNTIL, DIRT_BLEND_TO = 1500.0, 2500.0      # m of elevation
MOD_SHAPES = ("levels", "beamng_earth", "art", "shapes", "beamngEarth")   # in mod/beamng_earth
MOD_PALETTE = "far_palette.png"
FORMATS = ("cdae", "dae")

# --- horizon mode (M1h, docs/V1_4_SPEC.md §3.6 "Radius: the real line-of-sight horizon") ---------
# Heights are above sea level (the ellipsoid: the anchor frame's h0 is 0), never above the ground
# under the player: the bulge drops a point by d²/2R' whatever the local ground.
R_EARTH = 6371000.0             # m: the sphere the sky uses too (the mod sets ScatterSky.atmoPlanetRadius)
REFRACTION_K = 0.13             # standard terrestrial refraction; the cull's effective radius is R' below.
R_REFRACTED = R_EARTH / (1.0 - REFRACTION_K)    # 7323 km. The geometry (and the sky) have no refraction,
                                                # so using R' only makes the cull generous (~7 % farther).
HORIZON_MIN = RADIUS            # the disc is never smaller than today's radius, and nothing within it is culled
HORIZON_MAX = 1500000.0         # radius cap (spec: raised from 700 km so planes see far; eyes up to ~40 km)
HORIZON_MAX_LEVEL = 10          # L10 = 262 km, 4 km vertex spacing with 64 cells: a silhouette at 300+ km
# Texture px of the coarse levels in horizon mode (a 256 px L9 is 512 m/px; at 300 km one screen
# pixel is ~100 m, so the texture is never the limit); imagery.render picks the low source zoom.
HORIZON_TEX = {8: 256, 9: 256, 10: 128}
HORIZON_GRID_LEVEL = 6          # the zmax pyramid's base cell: one L6 node (16 384 m)
HORIZON_DEM_ZOOM = 6            # the coarse DEM pass (z6: ~1 km pixels at 36° N) ...
HORIZON_DEM_TILES = 64          # ... or coarser while the grid would need more DEM tiles than this
HORIZON_BLOCK = 8               # DEM pixels per block maximum (a block is ~8 km at z6)
Z_TOP = 8900.0                  # m: no terrain is higher (Everest 8849 m); sizes the grid an eye needs
VISIBLE_MARGIN = 1.10           # the mod's LevelInfo.visibleDistance = radius * this (horizon.lua)


@dataclass(frozen=True)
class Layout:
    """The tile layout of a server (docs/V1_4_SPEC.md §3.1, M1): cells per tile edge (32: v1.3.1,
    FORMAT 3; 64: FORMAT 4), the terrain block's samples per edge, the near-visual mode (mesh: tiles
    down to the exact level with the near radii; terrain: L0 floor, frozen at 32 cells) and the
    texture edge per level (from the mode's finest level, the last value repeating; None: the
    cells' default). Made once by earthd.Server and passed to select (K, radii), build_tile,
    zoom_for_level and heightmap, so nothing depends on a module setting."""
    cells: int = 32
    block: int = heightmap.SIZE
    mesh: bool = True
    tex: tuple = None

    def __post_init__(self):
        if self.cells not in CELL_COUNTS:
            raise ValueError(f"cells must be one of {CELL_COUNTS}, not {self.cells!r}")
        if self.block not in BLOCK_SIZES:
            raise ValueError(f"terrain block size {self.block!r} is not supported (one of {BLOCK_SIZES})")
        if not self.mesh and (self.cells != 32 or self.block != heightmap.SIZE):
            raise ValueError("terrain near-visual mode is frozen at the v1.3.1 layout (32 cells, 1024² block)")
        tex = TEX_DEFAULT[self.cells] if self.tex is None else tuple(int(v) for v in self.tex)
        if not tex:
            raise ValueError("no texture size")
        object.__setattr__(self, "tex", tex)

    @property
    def k_table(self):
        return K_TABLE[self.cells]

    def k(self, level):
        """K(L) of a node of level L >= 1 (the last table entry repeats)."""
        t = self.k_table
        return t[min(max(level, 1), max(t))]

    @property
    def near_radii(self):
        """The min-level radii of the mode (none in terrain mode: its floor is L0)."""
        return dict(NEAR_RADII[self.cells]) if self.mesh else {}

    @property
    def min_level(self):
        return MESH_MIN_LEVEL[self.cells] if self.mesh else 0

    @property
    def exact_level(self):
        """The level whose vertices are the physics lattice (mesh mode's finest)."""
        return MESH_MIN_LEVEL[self.cells]

    @property
    def format(self):
        return CONTENT_FORMAT[self.cells]

    @property
    def tris(self):
        """Triangles per tile: the grid plus a skirt quad per edge cell."""
        return 2 * self.cells * self.cells + 8 * self.cells

    def spacing(self, level):
        return level_size(level) / self.cells

    def dem_zoom(self, lat, level):
        return zoom_for_level(self, lat, level)

    def tex_size(self, level):
        return self.tex[min(max(level - self.min_level, 0), len(self.tex) - 1)]

    @property
    def grid(self):
        """The terrain block's centre grid (M4: 128 m for 512², 256 m for 1024²)."""
        return heightmap.grid(self.block)

    def block_at(self, cx, cy):
        """The Block of this layout's size centred at (cx, cy)."""
        return Block(cx, cy, self.block)


V131 = Layout(32)           # the v1.3.1 mesh layout (the server's default; terrain mode: mesh=False)


def k_default(level):
    """K of the v1.3.1 layout (32 cells)."""
    return V131.k(level)


def level_size(level):
    """Tile edge (m) of a level: 256 * 2^L (64 m for L-2)."""
    return BASE * 2.0 ** level


def switch_distance(level, kfun=None, radii=None, min_level=0, layout=V131):
    """Distance below which a node of `level` is refined (0: never): K(L) * S for L >= 1, and
    at least radii[m] for every allowed finer minimum level m < L. kfun / radii default to the
    layout's K table and NEAR_RADII row."""
    if level <= min_level:
        return 0.0
    d = (kfun or layout.k)(level) * level_size(level) if level >= 1 else 0.0
    for m, r in (NEAR_RADII[layout.cells] if radii is None else radii).items():
        if min_level <= m < level:
            d = max(d, r)
    return d


def torque_split45(gx, gy):
    """True where the Torque/BeamNG TerrainBlock splits square (gx, gy) (block-local indices,
    gx east, gy north from the SW corner) along the SW-NE diagonal (x - y = 0); False: along
    the SE-NW diagonal (x + y = 1).

    Torque3D TerrainFile::_buildGridMap (Engine/source/terrain/terrFile.cpp, development
    4c44642, ~line 213) sets for every level-0 (single) square
        bool shouldSplit45 = ((squareX ^ squareY) & 1) == 0;  if (i == 0) split45 = shouldSplit45;
    TerrainBlock::getHeight / getNormal (terrData.cpp ~721/772) and the collision convexes
    (terrCollision.cpp ~440, ~842) evaluate the two triangles of a Split45 square as
    x > y: (BL, BR, TR) / else (BL, TR, TL), and of other squares as x + y < 1: (BL, BR, TL) /
    else (BR, TR, TL). The renderer's index buffer (terrCell.cpp ~649, `xTess == yTess`) uses
    the same parity. Our blocks start at an even lattice index (cx - 1024 with cx a multiple
    of 256 m is a multiple of 4 m; M4's 512² block: cx - 512 with cx a multiple of 128 m), and
    L-2 tiles at multiples of 64 m, so the block-local
    parity equals the parity of the world lattice index (x / 2 + y / 2) and of a tile's local
    cell index (i + j): build_tile() and surface_at() split cell (i, j) on (i + j) even."""
    return ((np.asarray(gx) ^ np.asarray(gy)) & 1) == 0


@dataclass(frozen=True)
class Block:
    """Footprint of a terrain block: the 2048 m square of its samples (the valid, collidable
    area is [cx-1024, cx+1022]; the 2 m N/E strip is covered by sunk L0 tiles). size: samples per
    edge (M4: 512, a 1024 m square, collidable [cx-512, cx+510]; mesh mode only)."""
    cx: float
    cy: float
    size: int = heightmap.SIZE

    @property
    def bbox(self):
        h = heightmap.half_extent(self.size)
        return (self.cx - h, self.cy - h, self.cx + h, self.cy + h)


@dataclass(frozen=True)
class Tile:
    level: int
    i: int
    j: int

    @property
    def size(self):
        return level_size(self.level)

    @property
    def bbox(self):
        s = self.size
        return (self.i * s, self.j * s, (self.i + 1) * s, (self.j + 1) * s)

    @functools.cached_property
    def key(self):          # (cached: swap_groups and the build order ask for it on every pose)
        return f"{self.level}_{self.i}_{self.j}"

    @property
    def node(self):
        return (self.level, self.i, self.j)

    def distance(self, x, y):
        """Horizontal distance from (x, y) to the tile's footprint."""
        x0, y0, x1, y1 = self.bbox
        return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


@functools.lru_cache(maxsize=65536)
def tile_of(key):
    """The Tile of a key (cached: Tiles are immutable; the far field's bookkeeping parses the same keys on every pose)."""
    lvl, i, j = (int(v) for v in key.split("_"))
    return Tile(lvl, i, j)


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class View:
    """The camera for screen-space LOD (select's `view`, 2026-10-16): position (cx, cy) in the anchor frame and height
    above the ground under the player (dz), pixels per radian of the screen (ppr = height px / (2 tan(vfov / 2))),
    the pop size allowed (tau px) and err(node) -> the built tile's geometric error (m: how far its surface moves
    when it splits into its children, farfield.build_tile) or None when not known."""

    def __init__(self, cx, cy, dz, ppr, err, tau=None, reach=None, h=None, cells=64, cap=None):
        self.cx, self.cy, self.dz, self.ppr, self.err, self.cells = cx, cy, max(0.0, dz), ppr, err, cells
        self.cap = SSE_SPACING_CAP if cap is None else cap
        self.tau = SSE_TAU_PX if tau is None else tau
        self.reach = SSE_REACH if reach is None else reach
        self.h = SSE_HYSTERESIS if h is None else h

    def pixels(self, node, x0, y0, x1, y1):
        """The node's split pop on screen (px), or None (error not known). On steep ground the vertical error is
        mostly a sideways shift of about one grid spacing (a cliff's 300 m "error" is its face moving a few
        metres), so it counts at most SSE_SPACING_CAP spacings."""
        e = self.err(node)
        if e is None:
            return None
        e = min(e, self.cap * (x1 - x0) / self.cells)
        dxy = math.hypot(max(x0 - self.cx, 0.0, self.cx - x1), max(y0 - self.cy, 0.0, self.cy - y1))
        return e * self.ppr / max(math.hypot(dxy, self.dz), 1.0)


# Screen-space LOD (View): a node also splits while its tile's split would pop more than SSE_TAU_PX on screen, within
# SSE_REACH times its distance-rule switch distance (at most ~2 levels finer than the distance rule: a cliff 30 km
# away must not pull in thousands of tiles); a split node merges back once the pop is under SSE_TAU_PX / (1 + h).
SSE_TAU_PX = 0.0       # off by default: in Yosemite 4 px cost 17 % fps and +36 % tiles for 25 % smaller pops (2026-10-16)
SSE_REACH = 4.0
SSE_HYSTERESIS = 0.5
SSE_SPACING_CAP = 2.0


def select(px, py, forced=(), prev_refined=frozenset(), kfun=None, h=HYSTERESIS,
           radius=RADIUS, max_level=MAX_LEVEL, hold=frozenset(), min_level=0, radii=None, layout=V131,
           disc=False, horizon=None, forced_level=0, view=None):
    """Desired tiles for a player at (px, py).

    forced: bboxes (terrain blocks) under which everything is refined to `forced_level` (terrain mode:
    L0; detail mode: L-1, so tile edges meet the 128 m-grid edges of a 512² block).
    prev_refined: the nodes (L, i, j) refined last time (for the hysteresis).
    hold: nodes whose state must not change now (they changed less than HOLD_S ago).
    min_level: finest level (0; layout.min_level in mesh mode); kfun / radii: K(L) and the
    min-level radii (default: the layout's K table and NEAR_RADII row; only levels >= min_level
    count).
    disc (horizon mode): keep the nodes whose nearest point is closer than `radius` (a disc, not
    the square). horizon: a Horizon; beyond HORIZON_MIN it drops every node past the line-of-sight
    horizon, with its subtree (the server passes it with disc=True and max_level HORIZON_MAX_LEVEL).
    view: a View (screen-space LOD): nodes whose known geometric error would pop more than view.tau px on
    screen split too (within view.reach x the switch distance), distances from the camera.
    Returns ({key: Tile}, set of refined nodes)."""
    kfun = kfun or layout.k
    radii = NEAR_RADII[layout.cells] if radii is None else radii
    lo_x, lo_y, hi_x, hi_y = px - radius, py - radius, px + radius, py + radius
    s = level_size(max_level)
    stack = [(max_level, i, j)
             for j in range(math.floor(lo_y / s), math.ceil(hi_y / s))
             for i in range(math.floor(lo_x / s), math.ceil(hi_x / s))]
    out, refined = {}, set()
    while stack:
        node = stack.pop()
        lvl, i, j = node
        s = level_size(lvl)
        x0, y0, x1, y1 = i * s, j * s, (i + 1) * s, (j + 1) * s
        d = None
        if disc:
            d = math.hypot(max(x0 - px, 0.0, px - x1), max(y0 - py, 0.0, py - y1))
            if d >= radius or (horizon is not None and not horizon.keeps(lvl, i, j, d)):
                continue                                # outside the disc, or beyond the horizon
        elif not (x0 < hi_x and x1 > lo_x and y0 < hi_y and y1 > lo_y):
            continue                                    # outside the far radius
        if lvl > min_level:
            if lvl > forced_level and any(x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1] for b in forced):
                split = True
            elif node in hold:
                split = node in prev_refined
            else:
                if d is None:
                    d = math.hypot(max(x0 - px, 0.0, px - x1), max(y0 - py, 0.0, py - y1))
                lim = switch_distance(lvl, kfun, radii, min_level)
                split = d < lim or (node in prev_refined and d < (1.0 + h) * lim)
                if not split and view is not None and d < view.reach * lim:
                    px_ = view.pixels(node, x0, y0, x1, y1)
                    if px_ is not None:
                        split = px_ > view.tau or (node in prev_refined and px_ > view.tau / (1.0 + view.h))
            if split:
                refined.add(node)
                i2, j2 = 2 * i, 2 * j
                stack += [(lvl - 1, i2, j2), (lvl - 1, i2 + 1, j2), (lvl - 1, i2, j2 + 1), (lvl - 1, i2 + 1, j2 + 1)]
                continue
        t = Tile(lvl, i, j)
        out[t.key] = t
    return out, refined


# Load order (2026-10-16): tiles outside the camera's view cone count as VIEW_PENALTY times as far, so what is on
# screen fills first after a teleport; the cone is half the horizontal field of view plus VIEW_MARGIN_DEG (at most
# VIEW_MAX_DEG off the camera's heading).
VIEW_PENALTY = float(os.environ.get("BNGE_VIEW_PENALTY", "3.0"))     # (env: experiments)
VIEW_MARGIN_DEG = 15.0
VIEW_MAX_DEG = 80.0
ASPECT = 16 / 9


def view_cone(cam):
    """(x, y, unit forward fx, fy, cos of the cone's half angle) from a pose.cam dict, or None (no usable heading)."""
    try:
        x, y, fx, fy = float(cam["x"]), float(cam["y"]), float(cam["fx"]), float(cam["fy"])
        fov = float(cam.get("fovDeg") or 60)
    except (KeyError, TypeError, ValueError):
        return None
    n = math.hypot(fx, fy)
    if not (n > 0.2 and all(map(math.isfinite, (x, y, fx, fy, fov)))):
        return None                     # looking straight down / up: every direction counts
    half = math.degrees(math.atan(math.tan(math.radians(fov) / 2) * ASPECT)) + VIEW_MARGIN_DEG
    return x, y, fx / n, fy / n, math.cos(math.radians(min(half, VIEW_MAX_DEG)))


def view_distance(t, cone, x, y):
    """Distance of tile t from (x, y) for the load order: VIEW_PENALTY times as far outside the view cone (around the
    camera always in view): view_distances of the one tile."""
    return view_distances([t], cone, x, y)[0]


def view_distances(tiles, cone, x, y):
    """view_distance of every tile (a list), in Rust (earthfast.view_distances)."""
    if cone is None:                    # no camera: exactly the distances (and tie order) of before
        return [t.distance(x, y) for t in tiles]
    if not tiles:
        return []
    fast.require(1, "view_distances")
    bb = np.array([t.bbox for t in tiles], np.float64)
    return fast._ef.view_distances(bb, float(x), float(y), list(cone) if cone else [], float(VIEW_PENALTY)).tolist()


def nearest_first(tiles, x, y):
    return sorted(tiles, key=lambda t: (t.distance(x, y), t.level, t.key))


def diff(old, new):
    """Keys to add and to remove to go from tile set `old` to `new` (dicts or sets of keys)."""
    return [k for k in new if k not in old], [k for k in old if k not in new]


def swap_groups(old, new):
    """LOD changes from key set `old` to `new` as swap groups: [(adds, removes)], each a
    connected set of added and removed tiles that overlap (a parent and its descendants), so
    both sides have the same footprint. Tiles that enter or leave the far radius have no
    counterpart and form one-sided groups. Sorted lists, deterministic order."""
    adds, removes = diff(old, new)
    side = {k: 0 for k in adds}
    side.update({k: 1 for k in removes})
    by_node = {tile_of(k).node: k for k in side}
    parent = {k: k for k in side}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for k in side:
        lvl, i, j = tile_of(k).node
        while lvl < HORIZON_MAX_LEVEL:          # (no L10 node exists outside horizon mode)
            lvl, i, j = lvl + 1, i >> 1, j >> 1
            a = by_node.get((lvl, i, j))
            if a is not None and side[a] != side[k]:
                parent[find(k)] = find(a)
    comps = {}
    for k in side:
        comps.setdefault(find(k), []).append(k)
    groups = []
    for ks in comps.values():
        groups.append((sorted(k for k in ks if side[k] == 0), sorted(k for k in ks if side[k] == 1)))
    groups.sort(key=lambda g: (g[0] or g[1])[0])
    return groups


def transform_bbox(src, dst, bb):
    """Axis-aligned bbox (in dst's ENU plane) of a bbox given in src's ENU plane."""
    x0, y0, x1, y1 = bb
    xs = np.array([x0, x1, x0, x1, (x0 + x1) / 2])
    ys = np.array([y0, y0, y1, y1, (y0 + y1) / 2])
    e, n, _ = dst.ecef_to_enu(*src.enu_to_ecef(xs, ys, 0.0))
    return (float(e.min()), float(n.min()), float(e.max()), float(n.max()))


def zoom_for_level(layout, lat, level):
    """DEM zoom with pixels about half the layout's vertex spacing (L1+; L0 uses the heightmap zoom)."""
    return demmod.zoom_for(lat, layout.spacing(level) / 2, FAR_MAX_ZOOM)


def skirt_depth(size, err=0.0):
    return max(2.0, 0.02 * size, 2.5 * err + 1.0)


# --- horizon mode (M1h) ------------------------------------------------------------------
def horizon_distance(h, r=R_REFRACTED):
    """Distance (m) from a point h m above sea level to its sea-level horizon, sqrt(2 R' h) (0 at or
    below sea level). A point at height z is above the horizon of an eye at h within
    horizon_distance(h) + horizon_distance(z)."""
    return math.sqrt(2.0 * r * max(0.0, h))


def horizon_extent(h_eye, margin=0.0):
    """Half-size of the zmax grid an eye h_eye m above sea level needs: everything that can rise
    above its horizon (terrain up to Z_TOP) within the radius cap, plus `margin` (how far the player
    can be from the anchor), rounded up to whole L10 nodes (the pyramid's top level)."""
    e = min(HORIZON_MAX, horizon_distance(h_eye) + horizon_distance(Z_TOP)) + margin
    s = level_size(HORIZON_MAX_LEVEL)
    return max(1, math.ceil(e / s)) * s


class HorizonGrid:
    """The highest terrain (m above sea level) under every quadtree node of levels >= HORIZON_GRID_LEVEL
    over [-half, half]² of one anchor's plane (half: whole L10 nodes): base[j, i] for the L6 node
    (i - n/2, j - n/2), and its 2x2 maximum pyramid up to HORIZON_MAX_LEVEL. Built once per anchor by
    horizon_grid() (a coarse DEM pass), so a tile's zmax never depends on whether it was built."""

    def __init__(self, half, base, zoom=None):
        self.half, self.zoom = float(half), zoom
        base = np.asarray(base, np.float64)
        n = base.shape[0]
        if base.shape != (n, n) or n * level_size(HORIZON_GRID_LEVEL) != 2 * half or n % (
                1 << (HORIZON_MAX_LEVEL - HORIZON_GRID_LEVEL)):
            raise ValueError("the grid must cover [-half, half]² in whole L10 nodes")
        self.levels = {HORIZON_GRID_LEVEL: base}
        a = base
        for lvl in range(HORIZON_GRID_LEVEL + 1, HORIZON_MAX_LEVEL + 1):
            m = a.shape[0] // 2
            a = a.reshape(m, 2, m, 2).max(axis=(1, 3))
            self.levels[lvl] = a
        self._edges = (np.arange(n) - n // 2) * level_size(HORIZON_GRID_LEVEL)     # west / south cell edges

    @property
    def base(self):
        return self.levels[HORIZON_GRID_LEVEL]

    def zmax(self, level, i, j):
        """Highest terrain under node (level, i, j): a finer node takes its L6 ancestor's value; a node
        outside the grid gets +inf (never culled: the disc ends inside the grid)."""
        if level < HORIZON_GRID_LEVEL:
            k = HORIZON_GRID_LEVEL - level
            level, i, j = HORIZON_GRID_LEVEL, i >> k, j >> k
        a = self.levels.get(level)
        if a is None:
            return math.inf
        m = a.shape[0]
        ii, jj = i + m // 2, j + m // 2
        if 0 <= ii < m and 0 <= jj < m:
            return float(a[jj, ii])
        return math.inf

    def radius(self, px, py, h_eye):
        """(RADIUS, zmax_region) for an eye h_eye m above sea level at (px, py): zmax_region is the
        highest L6 cell that rises above the eye's horizon (its nearest point within
        sqrt(2R'h_eye) + sqrt(2R'z)), and RADIUS = clamp(sqrt(2R'h_eye) + sqrt(2R'zmax_region),
        HORIZON_MIN, HORIZON_MAX) (spec §3.6: Whitney summit ~480-510 km, the per-tile cull trims the
        rest; the Central Valley floor with the Sierra in sight ~290 km)."""
        s = level_size(HORIZON_GRID_LEVEL)
        e = self._edges
        dx = np.maximum(np.maximum(e - px, 0.0), px - (e + s))
        dy = np.maximum(np.maximum(e - py, 0.0), py - (e + s))
        d = np.hypot(dx[None, :], dy[:, None])
        reach = horizon_distance(h_eye)
        z = self.base
        vis = d <= reach + np.sqrt(2.0 * R_REFRACTED * np.maximum(z, 0.0))
        zr = float(z[vis].max()) if vis.any() else 0.0
        return min(max(reach + horizon_distance(zr), HORIZON_MIN), HORIZON_MAX), zr


class Horizon:
    """The per-node line-of-sight cull of one selection (M1h): an eye h_eye m above sea level and the
    anchor's HorizonGrid. Within HORIZON_MIN everything is kept (today's far field near the player)."""

    def __init__(self, eye, grid):
        self.eye, self.grid = float(eye), grid
        self.reach = horizon_distance(self.eye)

    def keeps(self, level, i, j, d):
        """Node (level, i, j) whose nearest point is d m from the eye (horizontally): kept while
        d <= sqrt(2R'h_eye) + sqrt(2R'zmax_node). The bulge only: terrain in between can still hide
        a kept node (the depth buffer handles that). Plane distances are a little longer than the arc
        (2.8 km at 700 km), which R' more than makes up for."""
        if d <= HORIZON_MIN:
            return True
        return d <= self.reach + horizon_distance(self.grid.zmax(level, i, j))


def _merc(lat, lon, zoom):
    """Global DEM pixel coordinates (x east, y south; pixel k covers [k, k + 1)) of lat/lon at a zoom,
    as dem.sample maps them; latitudes beyond the Web Mercator limit are clamped to it."""
    w = 2 ** zoom * demmod.TILE
    lat = np.clip(lat, -demmod.MAX_LAT, demmod.MAX_LAT)
    fx = (np.asarray(lon, np.float64) / 360.0 + 0.5) * w
    fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * w
    return fx, fy


def dem_block_max(dem, zoom, x, y, block=HORIZON_BLOCK):
    """Highest elevation in every block x block pixel square of DEM tile (zoom, x, y): a
    (TILE / block)² array, row 0 north (dem.Dem.block_max). A DEM without block_max (the tests'
    synthetic ones) is sampled twice per block and axis instead."""
    if hasattr(dem, "block_max"):
        return dem.block_max(zoom, x, y, block)
    n = demmod.TILE // block
    w = 2 ** zoom * demmod.TILE
    f = (np.arange(2 * n) + 0.5) * (block / 2.0)
    fx, fy = np.meshgrid(x * demmod.TILE + f, y * demmod.TILE + f)
    lon = fx / w * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * fy / w))))
    a = np.asarray(dem.sample(lat, lon, zoom), np.float64)
    return a.reshape(n, 2, n, 2).max(axis=(1, 3))


def horizon_grid(frame, dem, half, zoom=HORIZON_DEM_ZOOM, block=HORIZON_BLOCK, cache=None):
    """The HorizonGrid of an anchor frame over [-half, half]² (spec §3.6: zmax_region "from a coarse
    DEM pass, cached per anchor"). Each L6 cell gets the highest DEM block maximum (block x block
    pixels at `zoom`) touching the lat/lon box of its corners and edge midpoints, padded by one
    block, so it is never below the DEM's highest pixel in the cell. The zoom drops below `zoom`
    while the grid would need more than HORIZON_DEM_TILES DEM tiles. cache: {(zoom, x, y, block):
    block maxima} shared between anchors (a rebase reuses it). Runs in a worker thread: DEM tiles
    may be fetched."""
    s = level_size(HORIZON_GRID_LEVEL)
    n = int(round(2 * half / s))
    e = (np.arange(2 * n + 1) - n) * (s / 2.0)
    X, Y = np.meshgrid(e, e)                    # [j north, i east]: cell corners and edge midpoints
    lat, lon, _ = frame.enu_to_geodetic(X, Y, 0.0)
    nb = demmod.TILE // block
    while True:
        w = 2 ** zoom * demmod.TILE
        fx, fy = _merc(lat, lon, zoom)
        fx = fx - w * np.round((fx - fx[n, n]) / w)        # unwrapped around the anchor (antimeridian)
        bx = np.floor(fx / block).astype(np.int64)
        by = np.clip(np.floor(fy / block).astype(np.int64), 0, 2 ** zoom * nb - 1)
        tx0, tx1 = int(bx.min() - 1) // nb, int(bx.max() + 1) // nb
        ty0, ty1 = max(0, int(by.min() - 1) // nb), min(2 ** zoom - 1, int(by.max() + 1) // nb)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= HORIZON_DEM_TILES or zoom == 0:
            break
        zoom -= 1
    keys = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    todo = [(zoom, tx % 2 ** zoom, ty) for tx, ty in keys
            if cache is None or (zoom, tx % 2 ** zoom, ty, block) not in cache]
    if todo and hasattr(dem, "tiles"):
        dem.tiles(todo)                         # fetched concurrently, then read from its memory cache
    m = np.empty(((ty1 - ty0 + 1) * nb, (tx1 - tx0 + 1) * nb))
    for tx, ty in keys:
        k = (zoom, tx % 2 ** zoom, ty, block)
        a = cache.get(k) if cache is not None else None
        if a is None:
            a = dem_block_max(dem, *k)
            if cache is not None:
                cache[k] = a
        m[(ty - ty0) * nb:(ty - ty0 + 1) * nb, (tx - tx0) * nb:(tx - tx0 + 1) * nb] = a
    # per cell: the block range of its 3 x 3 points, padded by one block
    bx, by = bx - tx0 * nb, by - ty0 * nb
    views = [(bx[dj:dj + 2 * n:2, di:di + 2 * n:2], by[dj:dj + 2 * n:2, di:di + 2 * n:2])
             for dj in range(3) for di in range(3)]
    x0 = np.clip(np.minimum.reduce([v[0] for v in views]) - 1, 0, m.shape[1] - 1)
    x1 = np.clip(np.maximum.reduce([v[0] for v in views]) + 1, 0, m.shape[1] - 1)
    y0 = np.clip(np.minimum.reduce([v[1] for v in views]) - 1, 0, m.shape[0] - 1)
    y1 = np.clip(np.maximum.reduce([v[1] for v in views]) + 1, 0, m.shape[0] - 1)
    base = np.empty((n, n))
    for cj in range(n):
        for ci in range(n):
            base[cj, ci] = m[y0[cj, ci]:y1[cj, ci] + 1, x0[cj, ci]:x1[cj, ci] + 1].max()
    return HorizonGrid(half, base, zoom)


# --- colour ----------------------------------------------------------------------------
def palette_image():
    land = [  # (elevation m, rgb): the height palette
        (0, (0.36, 0.43, 0.24)), (800, (0.42, 0.44, 0.27)), (1800, (0.50, 0.45, 0.33)),
        (2800, (0.53, 0.50, 0.45)), (3800, (0.63, 0.62, 0.61)), (4800, (0.86, 0.87, 0.89)),
    ]
    u = (np.arange(PALETTE_W) + 0.5) / PALETTE_W
    elev = elev_from_u(u)
    base = np.stack([np.interp(elev, [e for e, _ in land], [c[k] for _, c in land]) for k in range(3)], -1)
    # Low land takes the terrain block's dirt colour (§2.3 colour continuity), blending into the
    # height palette between DIRT_UNTIL and DIRT_BLEND_TO; the slope tint fades in with it.
    w = np.clip((elev - DIRT_UNTIL) / (DIRT_BLEND_TO - DIRT_UNTIL), 0.0, 1.0)[:, None]
    base = np.array(DIRT) * (1 - w) + base * w
    base[u < 0.04] = (0.15, 0.30, 0.42)                         # water
    base[(u >= 0.04) & (u < 0.08)] = (0.72, 0.67, 0.50)         # shore
    t = np.abs((np.arange(PALETTE_H) + 0.5) / PALETTE_H - 0.5) * 2  # 0 at v=0.5 (flat) .. 1 at edges (steep)
    rock = np.array([0.44, 0.42, 0.40])
    rw = np.clip(t, 0, 1)[:, None, None] * 0.8 * w[None, :, :]
    img = base[None, :, :] * (1 - rw) + rock * rw
    return Image.fromarray(np.uint8(np.clip(img, 0, 1) * 255 + 0.5), "RGB")


def u_from_elev(elev):
    return 0.1 + 0.88 * np.clip(elev / 5000.0, 0.0, 1.0)


def elev_from_u(u):
    return (u - 0.1) / 0.88 * 5000.0


def material_json(palette_vfs):
    """The static far-tile material (§6.2): a v1.5 Material whose mapTo is the material name
    the tiles carry, so it applies to .cdae and .dae tiles alike (probe P2)."""
    return {MATERIAL: {
        "name": MATERIAL, "mapTo": MATERIAL, "class": "Material", "version": 1.5,
        "persistentId": "6f1c2a3e-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
        "Stages": [{"baseColorMap": palette_vfs, "metallicFactor": 0, "roughnessFactor": 1}, {}, {}, {}],
        "castShadows": False,       # (PR #41; the far tiles lie beyond the shadow distance: no change measured)
    }}


def write_mod_assets(mod_dir):
    """Write the palette and the material file into the mod (mod/beamng_earth). The level
    loader picks up every main.materials.json under the level folder."""
    d = os.path.join(mod_dir, *MOD_SHAPES)
    os.makedirs(d, exist_ok=True)
    palette_image().save(os.path.join(d, MOD_PALETTE))
    vfs = "/" + "/".join(MOD_SHAPES) + "/" + MOD_PALETTE
    with open(os.path.join(d, "main.materials.json"), "w") as f:
        json.dump(material_json(vfs), f, indent=2)
        f.write("\n")
    return d


# --- geometry --------------------------------------------------------------------------
def _skirt(nx, ny):
    """Edge vertex index runs, each ordered so that the skirt faces outwards
    (direction along the edge = up x outward normal)."""
    g = np.arange(nx * ny).reshape(ny, nx)          # [j, i], j = north
    return [g[0, :],            # south, west -> east
            g[:, -1],           # east, south -> north
            g[-1, ::-1],        # north, east -> west
            g[::-1, 0]]         # west, north -> south


def surface_at(grid, sp, fx, fy):
    """Height of a tile's triangulated surface at tile-local plane points (fx, fy in metres
    from the SW corner, inside the tile). grid: [j, i] vertex heights, spacing sp. Cells with
    i + j even are split along the SW-NE diagonal, the others along SE-NW, as build_tile
    triangulates them (the TerrainBlock's pattern, see torque_split45)."""
    n = grid.shape[0] - 1
    gx, gy = np.asarray(fx, np.float64) / sp, np.asarray(fy, np.float64) / sp
    i = np.clip(np.floor(gx).astype(np.int64), 0, n - 1)
    j = np.clip(np.floor(gy).astype(np.int64), 0, n - 1)
    a, b = gx - i, gy - j
    z00, z10 = grid[j, i], grid[j, i + 1]
    z01, z11 = grid[j + 1, i], grid[j + 1, i + 1]
    split45 = (i + j) % 2 == 0
    # SW-NE diagonal: triangles (00, 10, 11) where a >= b and (00, 11, 01) above it.
    z45 = np.where(a >= b, z00 + a * (z10 - z00) + b * (z11 - z10),
                   z00 + a * (z11 - z01) + b * (z01 - z00))
    # SE-NW diagonal: triangles (00, 10, 01) where a + b <= 1 and (10, 11, 01) above it.
    z135 = np.where(a + b <= 1.0, z00 + a * (z10 - z00) + b * (z01 - z00),
                    z11 + (1 - a) * (z01 - z11) + (1 - b) * (z10 - z11))
    return np.where(split45, z45, z135)


def grid_triangles(n):
    """Triangles (counter-clockwise from +Z) of an (n+1) x (n+1) vertex grid indexed [j, i]:
    cell (i, j) is split SW-NE when i + j is even, SE-NW otherwise (torque_split45)."""
    nx = n + 1
    jj, ii = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    a = (jj * nx + ii).ravel()
    b, c, d = a + 1, a + nx, a + nx + 1
    even = ((ii + jj) % 2 == 0).ravel()[:, None]
    t1 = np.where(even, np.stack([a, b, d], -1), np.stack([a, b, c], -1))
    t2 = np.where(even, np.stack([a, d, c], -1), np.stack([b, d, c], -1))
    return np.stack([t1, t2], 1).reshape(-1, 3)


@dataclass
class Built:
    pos: list               # [x, y, z] of the tile origin (SW corner, z = rounded mean height)
    positions: np.ndarray   # (n, 3) relative to pos
    normals: np.ndarray     # (n, 3)
    uvs: np.ndarray         # (n, 2) palette coordinates
    planar: np.ndarray      # (n, 2) tile-local xy / S: the footprint UV0 and the tangent frame
    tris: np.ndarray        # (m, 3) counter-clockwise from +Z
    grid: np.ndarray        # (cells + 1)² absolute vertex heights, float32 (for swap-group deltas)
    err: float              # max |reference - tile| (m)
    sink: float | None      # levels <= 0 with_sink (terrain / detail mode): drop that puts the tile under the terrain everywhere
    skirt: float            # skirt depth (m)
    size: float             # tile edge (m)

    @property
    def detail_uvs(self):
        """UV1: tile-local metres / DETAIL_M (world-locked: tile origins are multiples of DETAIL_M)."""
        return self.planar * (self.size / DETAIL_M)


def build_tile(frame, dem, tile, zoom, q=None, ref_zoom=None, with_sink=True, layout=V131):
    """Geometry of one tile with layout.cells cells per edge. zoom: DEM zoom of its vertices;
    ref_zoom: zoom of the 2x finer reference used for `err` (default zoom). For levels <= 0 pass q
    (the anchor's height quantum) and the heightmap zoom: vertices are then Q-rounded 2 m lattice
    samples, the same values the terrain block stores, and (L0, with_sink) `sink` is measured
    against the terrain's 2 m lattice."""
    cells = layout.cells
    x0, y0, x1, y1 = tile.bbox
    S = tile.size
    sp = S / cells
    elev_fn = lambda la, lo: dem.sample(la, lo, zoom)       # noqa: E731
    terrain = None
    if tile.level <= 0:
        # One evaluation of the terrain lattice over the tile (129² for L0, 65² for L-1, 33²
        # for L-2) gives the vertices (32 cells: every 4th / 2nd / every sample; 64 cells: every
        # 2nd / every sample for L0 / L-1), the error reference at half the vertex spacing and the
        # sink reference (all samples).
        n = int(S / heightmap.SQUARE) + 1
        step = (n - 1) // cells
        if step < 1 or (n - 1) % cells:
            raise ValueError(f"level {tile.level} is finer than the {cells}-cell layout's lattice level")
        k0x, k0y = int(x0 // heightmap.SQUARE), int(y0 // heightmap.SQUARE)
        lxs, lys = heightmap.lattice_coords(k0x, n), heightmap.lattice_coords(k0y, n)
        lelev, lu = heightmap.surface(frame, elev_fn, lxs, lys)
        lq = lu if q is None else heightmap.quantise(lu, q) * q
        xs, ys = lxs[::step], lys[::step]
        elev, U = lelev[::step, ::step], lq[::step, ::step]
        if step >= 2:
            ref = lu[::step // 2, ::step // 2]
        else:                           # the exact level: the vertices are the lattice; reference at 1 m
            r = x0 + np.arange(2 * cells + 1) * (sp / 2), y0 + np.arange(2 * cells + 1) * (sp / 2)
            ref = heightmap.surface_u(frame, elev_fn, *r)
        if with_sink:                   # L0 (terrain mode) or any near level (detail mode)
            terrain = lq
    else:
        xs = x0 + np.arange(cells + 1) * sp
        ys = y0 + np.arange(cells + 1) * sp
        elev, U = heightmap.surface(frame, elev_fn, xs, ys)
        if q is not None:
            U = heightmap.quantise(U, q) * q
        rz = zoom if ref_zoom is None else ref_zoom
        rxs = x0 + np.arange(2 * cells + 1) * (sp / 2)
        rys = y0 + np.arange(2 * cells + 1) * (sp / 2)
        ref = heightmap.surface_u(frame, lambda la, lo: dem.sample(la, lo, rz), rxs, rys)
    step_f = (NEAR_FINE_M if tile.level == layout.exact_level else
              NEAR_FINE_NEXT_M if tile.level == layout.exact_level + 1 else 0.0) if NEAR_FINE_M > 0 else 0.0
    fine = (step_f > 0 and not with_sink and tile.level <= 0
            and (int(round(S / step_f)) & (int(round(S / step_f)) - 1)) == 0)
    if fine:                            # (the two finest near levels, mesh mode: NEAR_FINE_M, NEAR_FINE_NEXT_M)
        nf = int(round(S / step_f)) + 1
        xs = x0 + np.arange(nf) * step_f
        ys = y0 + np.arange(nf) * step_f
        elev, U = heightmap.surface(frame, elev_fn, xs, ys)
        sp, cells = step_f, nf - 1
    U = np.asarray(U, np.float64)
    shade = U                           # the vertex normals: the whole grid's (its detail in the light)
    adaptive = None
    if fine:
        fast.require(51, "fine near ground")
        eps = NEAR_FINE_EPS * step_f
        while True:
            adaptive, surf, worst = fast._ef.rtin_mesh(np.ascontiguousarray(U), eps)
            if len(np.unique(adaptive)) + 4 * nf <= NEAR_FINE_VERTS:
                break
            eps *= 2
        U = surf
    elif tile.level >= ADAPT_MIN_LEVEL and ADAPT_EPS > 0 and layout.cells == 64:    # (the v1.3.1 layout: its grid)
        fast.require(51, "adaptive far tiles")
        adaptive, U, _ = fast._ef.rtin_mesh(np.ascontiguousarray(U), ADAPT_EPS * sp)     # U: the mesh's surface

    # Geometric error: the reference against the triangulated tile, on the 2x grid (fine: the samples themselves).
    if fine:
        err = float(worst)
    else:
        rf = np.arange(2 * cells + 1) * (sp / 2)
        RX, RY = np.meshgrid(rf, rf)
        err = float(np.abs(ref - surface_at(U, sp, RX, RY)).max())
    sink = None
    if terrain is not None:
        lf = np.arange(terrain.shape[0]) * heightmap.SQUARE
        LX, LY = np.meshgrid(lf, lf)
        sink = float(max(0.0, (surface_at(U, sp, LX, LY) - terrain).max()) + SINK_MARGIN)

    X, Y = np.meshgrid(xs, ys)          # [j, i], j = north
    # Colour: height along u, slope along v (mirrored palette, flat = 0.5).
    gy, gx = np.gradient(elev, ys, xs)
    slope_t = np.clip(np.hypot(gx, gy) / 1.0, 0.0, 1.0)
    water = (elev <= 0.5) & (slope_t < 0.02)
    uu = np.where(water, WATER_U, u_from_elev(elev))
    vv = 0.5 - 0.47 * slope_t

    # Vertex normals from the grid (central differences).
    Ps = np.stack([X, Y, shade], -1)
    di = np.gradient(Ps, axis=1)
    dj = np.gradient(Ps, axis=0)
    nrm = np.cross(di, dj)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    P = np.stack([X, Y, U], -1)

    ny, nx = X.shape
    P, nrm = P.reshape(-1, 3), nrm.reshape(-1, 3)
    uv = np.stack([uu, vv], -1).reshape(-1, 2)
    runs = _skirt(nx, ny)
    if adaptive is None:
        tris = [grid_triangles(nx - 1)]     # CCW from +Z, the TerrainBlock's diagonals
    else:                               # the mesh's vertices only; the skirts along its edge vertices
        used = np.unique(adaptive)
        new_of = np.full(nx * ny, -1, np.int64)
        new_of[used] = np.arange(len(used))
        tris = [new_of[adaptive]]
        P, nrm, uv = P[used], nrm[used], uv[used]
        runs = [new_of[r[new_of[r] >= 0]] for r in runs]

    # Skirts: copies of the edge vertices, lowered; same normal and uv as the edge.
    depth = skirt_depth(S, err)
    extra_p, extra_n, extra_uv, nv = [], [], [], len(P)
    for run in runs:
        low = P[run] - (0.0, 0.0, depth)
        t0, t1 = run[:-1], run[1:]
        lo0 = nv + np.arange(len(run) - 1)
        lo1 = lo0 + 1
        tris.append(np.stack([t0, lo0, t1, t1, lo0, lo1], -1).reshape(-1, 3))
        extra_p.append(low)
        extra_n.append(nrm[run])
        extra_uv.append(uv[run])
        nv += len(run)
    P = np.concatenate([P] + extra_p)
    nrm = np.concatenate([nrm] + extra_n)
    uv = np.concatenate([uv] + extra_uv)

    pos = [float(x0), float(y0), float(np.round(U.mean()))]
    P -= pos
    planar = (P[:, :2] / S)
    return Built(pos, P, nrm, uv, planar, np.concatenate(tris), U.astype(np.float32), err, sink, depth, S)


def write_built(path, built, fmt="cdae", material=MATERIAL, textured=False, tex_px=None):
    """Write a built tile as .cdae (default) or .dae; returns the file size. textured: the tile
    has its own imagery texture (UV0 = footprint, UV1 = world-locked detail); otherwise UV0 is
    the shared palette coordinate and there is no UV1. tex_px: the texture has a border of
    imagery.gutter(tex_px) texels around the footprint (UV0 is inset to match)."""
    assert len(built.positions) <= VERTS_MAX
    uvs = built.uvs
    if textured:
        uvs = built.planar if tex_px is None else imagery.gutter_uv(built.planar, tex_px)
    uvs2 = built.detail_uvs if textured else None
    if fmt == "cdae":
        return cdae.write(path, built.positions, built.tris, normals=built.normals, uvs=uvs, uvs2=uvs2,
                          material=material, tangent_uvs=built.planar)
    dae.write_grid_dae(path, built.positions.ravel(), uvs.ravel(), built.tris.ravel(),
                       material=material, normals=built.normals.ravel(),
                       uvs2=None if uvs2 is None else uvs2.ravel())
    return os.path.getsize(path)


def write_tile(path, frame, dem, tile, zoom, q=None, ref_zoom=None, fmt="cdae", cancelled=None, layout=V131):
    """Build and write one tile; returns the Built record, or None if cancelled meanwhile."""
    if cancelled is not None and cancelled.is_set():
        return None
    built = build_tile(frame, dem, tile, zoom, q, ref_zoom, layout=layout)
    write_built(path, built, fmt)
    return built


def group_deltas(group_tiles):
    """(dIn, dOut) of a swap group: how far the new surface rises above the old one at most,
    and how far the old one lies above the new one, evaluated on the finest vertex grid of the
    group (every vertex of both sides). group_tiles: (adds, removes), lists of (Tile, grid); a
    grid's shape gives its tile's cell count."""
    adds, removes = group_tiles
    if not adds or not removes:
        return 0.0, 0.0
    adds = [(t, np.asarray(g, np.float64)) for t, g in adds]
    removes = [(t, np.asarray(g, np.float64)) for t, g in removes]
    sp = min(t.size / (g.shape[0] - 1) for t, g in adds + removes)
    d_in = d_out = 0.0
    for ta, ga in adds:
        ab = ta.bbox
        for tr, gr in removes:
            rb = tr.bbox
            x0, y0, x1, y1 = max(ab[0], rb[0]), max(ab[1], rb[1]), min(ab[2], rb[2]), min(ab[3], rb[3])
            if x0 >= x1 or y0 >= y1:
                continue
            # The intersection of two quadtree tiles is the smaller one: aligned to sp.
            X, Y = np.meshgrid(x0 + np.arange(int(round((x1 - x0) / sp)) + 1) * sp,
                               y0 + np.arange(int(round((y1 - y0) / sp)) + 1) * sp)
            z_new = surface_at(ga, ta.size / (ga.shape[0] - 1), X - ab[0], Y - ab[1])
            z_old = surface_at(gr, tr.size / (gr.shape[0] - 1), X - rb[0], Y - rb[1])
            dz = z_new - z_old
            d_in = max(d_in, float(dz.max()))
            d_out = max(d_out, float(-dz.min()))
    return d_in, d_out


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "mod", "beamng_earth")
    print("wrote", write_mod_assets(target))
