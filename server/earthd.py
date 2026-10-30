#!/usr/bin/env python3
"""BeamNG.Earth companion server (protocol 4). See docs/V1_3_SPEC.md, docs/V1_2_DESIGN.md
(and V1_1_SPEC.md).

    python3 server/earthd.py [--port 47613] [--user-dir DIR] [--cache-dir DIR] [--far-format cdae|dae]
                             [--imagery usgs,eox2023,eox2016|off] [--near-visual mesh|terrain]
                             [--cells 32|64] [--tex 256,256,256,512] [--block 1024|512] [--horizon]
                             [--deshadow off|lambert|full] [--colormatch off|s2]
                             [--weather on|off] [--gravity on|off] [--offline] [--credits on|off]
                             [--cache-cap GB]

Listens on 127.0.0.1 for the mod, builds terrain-block heightmaps (1024² on a world-locked
2 m lattice, 256 m-aligned centres; in mesh near-visual mode the block is physics only) and a
dynamic-LOD tile field (down to 64 m tiles on the physics lattice in mesh mode) with per-tile
satellite textures, streamed as swap groups, and writes them into
<user-dir>/temp/beamngEarth/<sessionId>/.

Tile layout (docs/V1_4_SPEC.md §3.1, M1): --cells 64 (the default in mesh mode since probe P8
passed) makes every tile 64 x 64 cells, FORMAT 4, with the exact level L-1, hello.cells and
far_tile.tex; --cells 32 (and terrain mode) is the v1.3.1 layout, FORMAT 3.

Terrain block (docs/V1_4_SPEC.md §3.5, M4): --block 512 (mesh mode only; the default there since the
in-game probes P2, P7 and P11 passed; Server() keeps 1024) makes the physics block 512² (collidable [c-512, c+510]) on
a 128 m centre grid, sends hello.blockSize and terrain.size 512, and predicts the mod's 512 follow
table (FOLLOW_RULES, with its fast mode above 150 m/s) for the prefetch.

Horizon (docs/V1_4_SPEC.md §3.6 "Radius", M1h): --horizon (mesh mode only; off by default until the
in-game probe P12 passes) replaces the 80 km square by a disc out to the line-of-sight horizon: the
eye's height above sea level from the pose (camera z in the anchor frame, whose h0 is sea level),
zmax from a coarse DEM pass per anchor (farfield.horizon_grid, in its own worker), RADIUS =
clamp(sqrt(2R'h_eye) + sqrt(2R'zmax_region), 80 km, 1500 km), the per-tile horizon cull with
refraction R' = R/(1-0.13), MAX_LEVEL 10, 256/128 px textures from L8, hello.horizon and
status.radius (the mod's visibleDistance follows it).

Network (docs/research/2026-10-10-rate-limits.md §5): every download goes through one
netfetch.Fetcher (per-host rate limits, priorities, retries, circuit breakers with failover along
the source chains, daily budgets in <cache-dir>/netstats/, one summary line a minute in the log).
--offline serves the disk caches only. While a host is unreachable, or tiles fell back to the
palette, every status text carries a note ("Offline - using cached data", "Degraded: ...").

Download cache (cache.py, docs/ROADMAP.md "Caching"): the raw downloads and region caches under
<cache-dir> are capped (--cache-cap GB, default the last cap the mod's settings page set, else 20; 0 =
unlimited); least recently used files go in a background thread, never what this session used within
the hour, never the gravity model or netstats/. The mod's page shows the usage ('cache' message) and
sets the cap or clears the cache ('cache' from the mod).

Sessions (docs/ROADMAP.md "Robustness" 3): a restarted server keeps the folders of earlier runs that
are less than a day old until the game says (hello / pose `sessions`) which ones its far tiles still
use: those are adopted and deleted (rate-limited) once no pose names them any more; the others go
SESSION_ORPHAN_S after nobody claimed them. The mod resyncs at its current origin meanwhile, so the
tiles on screen never lose their files.
"""
import os

# Heavy numpy work must not compete with the game's physics threads (§4.5): one BLAS/OpenMP
# thread per worker. Set before numpy is imported.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import glob  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict, deque  # noqa: E402
import concurrent.futures  # noqa: E402
import multiprocessing  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from dataclasses import dataclass, field, replace  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import blockworker  # noqa: E402
import budget as budgetmod  # noqa: E402
import cache as cachemod  # noqa: E402
import cdae  # noqa: E402
import colormatch as colormatchmod  # noqa: E402
import credits as creditsmod  # noqa: E402
import covermap as covermapmod  # noqa: E402
import areas as areasmod  # noqa: E402
import surface as surfacemod  # noqa: E402
import navgraph as navgraphmod  # noqa: E402
import prewarm as prewarmmod  # noqa: E402
import parking as parkingmod  # noqa: E402
import gasstations as gasstationsmod  # noqa: E402
import roads as roadsmod  # noqa: E402
import rails as railsmod  # noqa: E402
import randomplace as randomplacemod  # noqa: E402
import placepreview as placepreviewmod  # noqa: E402
import pools as poolsmod  # noqa: E402
import plancache as plancachemod  # noqa: E402
import grading as gradingmod  # noqa: E402
import roadmesh as roadmeshmod  # noqa: E402
import roadwidth as roadwidthmod  # noqa: E402
import revisit as revisitmod  # noqa: E402
import signstream  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import dem as demmod  # noqa: E402
import mapcells  # noqa: E402
import pacer  # noqa: E402
import peerwork as peerworkmod  # noqa: E402
import deshadow as deshadowmod  # noqa: E402
import farfield  # noqa: E402
import terraincache  # noqa: E402
import terrainpatch as terrainpatchmod  # noqa: E402
import geodesy  # noqa: E402
import gravity as gravitymod  # noqa: E402
import airportgrade as airportgrademod  # noqa: E402
import airports as airportsmod  # noqa: E402
import seacarve as seacarvemod  # noqa: E402
import seatiles as seatilesmod  # noqa: E402
import vegetation as vegetationmod
import buildings as buildingsmod  # noqa: E402
import rockface as rockfacemod  # noqa: E402
import geotiles as geotilesmod  # noqa: E402
import riverlines as riverlinesmod  # noqa: E402
import groundahead as groundaheadmod  # noqa: E402
import routeahead as routeaheadmod  # noqa: E402
import furniture as furnituremod  # noqa: E402
import heightmap  # noqa: E402
import staging  # noqa: E402
import imagery as imagerymod  # noqa: E402
import matslots as matslotsmod  # noqa: E402
import maptiles as maptilesmod  # noqa: E402
import geocode as geocodemod  # noqa: E402
import routing as routingmod  # noqa: E402
import updates as updatesmod  # noqa: E402
import cpupin  # noqa: E402
import netfetch  # noqa: E402
import protocol  # noqa: E402
import weather as weathermod  # noqa: E402

PROTOCOL = 4
# The BeamNG.Earth server list (native/serverlist behind Caddy on the project's mini PC): server_list()
SERVER_LIST_URL = "https://earth.tgforever.net/servers"
# Tile/heightmap content format (hello.format) is the layout's: 3 = v1.3.1 (2 m lattice, fixed Q,
# 32-cell tiles), 4 = 64-cell tiles down to L-1 (farfield.CONTENT_FORMAT). A mod's hello.format is
# the newest format it reads (it reads every older one from 3: core.lua FORMATS), so a mod older
# than the server's format gets the mismatch status.
# Server(cells=...) default: the v1.3.1 layout (library and tests; test_v131_default pins it).
# The command line defaults to 64 cells in mesh mode since the in-game gate P8 passed
# (2026-10-10: 64-cell textured imports 4.0 ms at 256 px, 4.2 ms at 512 px; at 90 m/s the exact
# level was under the car 100 % of frames, queue max 35, vs 26 % / 2442 with 32 cells;
# docs/research/m1_d*); terrain mode stays frozen at 32.
CELLS_DEFAULT = 32
CLI_CELLS_MESH = 64


def resolve_block(block, near_visual):
    """The --block value to use: the given one, else 512 in mesh mode and 1024 in terrain mode."""
    if block is not None:
        return block
    return CLI_BLOCK_MESH if near_visual in MESH_LAYOUT else heightmap.SIZE


def resolve_colormatch(mode, chain):
    """--colormatch, else s2 when the imagery chain has USGS (the only source it corrects) and off
    otherwise. Server() keeps "off" (the golden message streams)."""
    if mode is not None:
        return mode
    names = [getattr(s, "name", s) for s in (chain or ())] if chain not in (None, "off") else []
    return "s2" if "usgs" in names else "off"


def resolve_horizon(horizon, near_visual):
    """--horizon / --no-horizon, else on in mesh mode (the owner's choice, 2026-10-11) and off in
    terrain mode. Server() keeps horizon=False (test_v131_default / test_cli_default)."""
    if horizon is not None:
        return horizon
    return near_visual in MESH_LAYOUT


def resolve_cells(cells, near_visual):
    """The --cells value to use: the given one, else 64 in mesh mode and 32 in terrain mode."""
    if cells is not None:
        return cells
    return CLI_CELLS_MESH if near_visual in MESH_LAYOUT else 32
SERVER_VERSION = netfetch.SERVER_VERSION      # one place: the User-Agent carries it too
NEAR_VISUALS = ("mesh", "terrain", "detail")
# detail (2026-10-12, docs/research/2026-10-12-engine-limits-and-worldgen.md §6-7): the mesh layout
# (64 cells, the 512² block, the horizon) with the physics block drawn as the near ground: aerial photo
# as its base colour, the surface materials' own detail textures; far tiles under it are hidden or sunk
# as in terrain mode. MESH_LAYOUT: the modes on the mesh-mode tile layout; BLOCK_DRAWN: the block is seen.
MESH_LAYOUT = ("mesh", "detail")
BLOCK_DRAWN = ("terrain", "detail")
TUNNEL_FLOOR_UNDER_M = 2.0     # a tunnel floor this far under the ground at a teleport's origin: the car goes on the floor
SEA_CHECK_S = 15.0          # sea (--sea on): the water tiles planned again when the player enters another
                            # 512 m square, or after this long (cells built meanwhile; seatiles.py)
VIEW_AWAY_M = 50.0              # m: a camera this far from the car centres what is drawn (Conn.camera_away)
PATCH_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")   # a terrain patch key (terrainpatch.lua)
FOCI_MAX = 16                   # other vehicles' collision foci taken from a pose (the mod's collisionfoci.lua)
FOCI_RANGE_M = 50000.0          # ... within this of the anchor's origin
FOCI_R_MAX_M = 640.0            # ... and radii at most this


def parse_foci(raw):
    """The pose's "foci" ([{x, y, r, trees}], the mod's collisionfoci.lua: other players' cars, traffic) as a tuple of
    (x, y, r, trees), FOCI_MAX at most, bad entries left out."""
    out = []
    if not isinstance(raw, list):
        return ()
    for f in raw[:FOCI_MAX]:
        try:
            x, y, r = float(f["x"]), float(f["y"]), float(f["r"])
        except (TypeError, KeyError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x, y, r)) or max(abs(x), abs(y)) > FOCI_RANGE_M or r <= 0:
            continue
        out.append((x, y, min(r, FOCI_R_MAX_M), bool(f.get("trees"))))
    return tuple(out)
GROUND_RESEND_M, GROUND_RESEND_S = 1.0, 1.5   # ground_ahead: sent on a change, else this often (the mod's
                                               # follow.GROUND_STALE_S is 3 s)
DETAIL_RING_M = 128.0           # detail mode: L-1 tiles this far beyond the block (forced())
IMG_WARM_MARGIN = 200.0         # m beyond the block's half width that warm_imagery fetches ahead
# download_ahead: the raw data of the terrain blocks and road tiles ahead of a driving player, fetched into the
# disk caches before anything there is built (a block in a new area waited ~0.5-2 s on its elevation tiles,
# 2026-10-15). Points AHEAD_S s ahead on the straight path (at most AHEAD_MAX_M), at netfetch "prefetch"
# priority: only a host's spare tokens, so the builds' own downloads never wait for it
AHEAD_ON = True                 # --download-ahead
AHEAD_S = (10.0, 20.0, 30.0, 45.0, 60.0)
AHEAD_MAX_M = 4000.0
AHEAD_MIN_V = 8.0               # m/s: slower, the blocks around (and surface.prefetch's 3 km) cover it
AHEAD_MOVE_M = 150.0            # m the player moves before the next round
AHEAD_MARGIN_M = 250.0          # m around the block's half width at each point
# the road tiles' cells (landcover: surface.prefetch). With roads (surface.prefetch has them within 3 km, with the land
# cover on) and water, which a road tile otherwise fetched itself when it needed it (from KyzoFox's 1.5.4, PR #64)
AHEAD_LAYERS = ("roads", "buildings", "building_parts", "infrastructure", "water", "land_use")
# route_download: like download_ahead, but along the planned GPS route's own geometry (routeahead.py)
# instead of the player's current heading, since a road route can wander off that heading entirely.
ROUTE_AHEAD_ON = True            # --route-prefetch
ROUTE_AHEAD_MAX_M = 20000.0      # --route-prefetch-km * 1000 (15-25 km is sane; the user, 2026-10-22)
# the GPS's "Download along route" setting (the user, 2026-10-29: "options on pre downloading/generating along a gps
# route"): the route message's "download": off, ahead (ROUTE_AHEAD_MAX_M) or the whole route (at most this far)
ROUTE_DOWNLOAD_MODES = ("off", "ahead", "route")
ROUTE_WHOLE_MAX_M = 500000.0
ROUTE_RETRY_S = 2.0               # a deferred tile/cell backs off this long before the next one in the corridor
FAST_BASE_V = 70.0              # m/s (157 mph): faster, a block waits at most BASE_FAST_WAIT_S for its photo after its
BASE_FAST_WAIT_S = 0.15         # heights, then goes without it (block_base later): blocks took 0.6-3 s at 600 mph on a
                                # Steam Deck, mostly the photo (heights 0.1-0.4 s), and the game paused 11 times a minute
PROGRESS_S = 0.5            # load_progress at most this often (Connection.send_progress)
GROUND_WAIT_S = 90.0        # a block's photo still to be drawn again counts as loading this long at most
PERF_LOG_LINES, PERF_LOG_CHARS = 40, 2000   # a perf_log message's lines and characters a line, at most
SLOW_BASE_S = 3.0               # a base photo slower than this is logged with its stages
BASE_RENDER_PX = 1024           # detail mode: the block's base photo as the imagery renders it (the 1 km block: 1 m a
                                # texel; USGS is ~1.9 m, NAIP 0.6 m)
BASE_PX = 2048                  # ... and as the game gets it: enlarged (a plain resize), then the roads, pads and rock
                                # painted in at 0.5 m a texel (at 1 m the asphalt's edges blurred into the ground and
                                # the sand and the trees' green into the road: the user, 2026-10-28)
# The terrain shader samples a block's base colour half a sample on (block-local sample coordinates + 0.5): a photo
# over the block's own square showed every road painted into it a metre south-west of its lines and edge decals
# (painted dots against objects at the same points, in game 2026-10-28; the user: "the decals are still offset").
# The photo is rendered over the square moved back by that much.
BASE_SHIFT_M = heightmap.SQUARE / 2
BASE_AO_VFS = "/levels/beamng_earth/art/terrains/bngEarth_base_ao.png"   # every surface layer's aoBaseTex
# The ground photo sizes the mod may choose (its graphics setting groundPhoto, the 'graphics' message's photoPx; the
# user, 2026-10-28: "much more control in the graphics settings"): the level ships the flat base maps of each
# (tools/make_level_assets.py BASE_SIZES: BASE_PX's under the old names, the others with a _<px> suffix), and every
# layer's aoBaseTex is the one of the size in use, so the packed cache entry's key (terraincache.base_key) follows it.
# Measured 2026-10-28 (quality-presets notes): 4096 costs ~1-3 s a block on a PC, 6-12 s on a Steam Deck.
BASE_SIZES = (1024, 2048, 4096)


def base_ao_vfs(px):
    """The level's flat AO map of ground photo size px (every surface layer's aoBaseTex at that size)."""
    return BASE_AO_VFS if px == BASE_PX else f"{BASE_AO_VFS[:-4]}_{px}.png"
BASE_AO = 251                   # its flat value (tools/make_level_assets.py)
GRAPHICS_SCALE = (0.5, 3.0)     # the 'graphics' message's scales are clamped to this (Connection.set_graphics)
VIEW_CAP_MIN_M = 10000.0        # ... and its fixed view distance (viewM, the far field's radius cap) to at least this
DETAIL_ROAD_LIFT = 0.025        # detail mode: road surfaces over the drawn block, within roadmesh.FOLLOW_TOL of it (0.05
                                # before, over a coarser surface it still cut through on hills: the tyres sank into it,
                                # the user 2026-10-20)
TEX_NEAR, TEX_FAR = 256, 256    # 32 cells: texture edge (px) of the two finest levels / of the others (512
                                 # near was 15x finer than USGS: no visible gain for 2x build time and VRAM);
                                 # --tex gives one per level (64 cells: farfield.TEX_DEFAULT)
SESSION_MARKER = ".earthd-session"
# The engine caches every loaded .dae as a .cdae under <user-dir>/temp/<VFS path, lower-cased>
# (seen in-game: temp/beamngEarth/<sid>/far_*.dae -> temp/temp/beamngearth/<sid>/far_*.cdae,
# 1.8 GB / 24k files after one v1 session). Those caches are deleted along with their sources.
# Server-written .cdae tiles (the default) have no such duplicate.
CDAE_ROOT = ("temp", "temp", "beamngearth")
PLACE_PREVIEWS_MAX = 100     # place pictures made per request (a long list: the rest next time)

HM_CACHE = 6                # heightmaps kept beyond the protected ones
TERRAIN_STUCK_S = 90.0      # s: a terrain request still building this long is logged once (prepare_terrain)
HM_KEEP_COMMITTED = 3       # the current terrain and the last two
HM_KEEP_SENT = 2            # terrains handed to the mod but maybe not committed yet
# Generated far-tile files kept per server, by layout: a 64-cell tile is ~3.7x the bytes (348 KB)
# and a pose needs ~300 tiles instead of ~811 (docs/V1_4_SPEC.md §3.1).
TILE_CACHE = {32: 6000, 64: 2500}
HM_THREADS = 4
LAG_TICK_S, LAG_REPORT_S = 0.1, 0.25     # the event-loop stall report (-v)
ROAD_PROCESSES = 2              # road-tile processes (--block-process on, --roads on)
# s a road tile may take in its worker before it is given up and its workers restarted (build_roads_tile): a first
# visit's slowest tiles take ~1 min on a busy 6-core laptop and several times that on a Steam Deck, where a tile given
# up at 2 min would be built and given up again for good; BNGE_ROAD_TILE_TIMEOUT=<s> to change it
ROAD_TILE_TIMEOUT_S = float(os.environ.get("BNGE_ROAD_TILE_TIMEOUT", "") or 300.0)
WORKERS = 3                     # far-tile workers (--workers)
TRACE_S = 60.0                  # BNGE_TRACEMALLOC: seconds between the memory reports ...
TRACE_TOP = 15                  # ... and the allocation sites in each
LANE_REQUEST, LANE_PREFETCH = 0, 1     # Server.block_pools: the blocks the mod asks for / the prefetch
HM_BUSY_TILE_WORKERS = 1    # far tiles built at once while a physics block is building in the server process (an
                            # anchor's first, or every one with --block-process off; the block processes' blocks do
                            # not share its interpreter: far tiles one at a time behind them were most of a Steam
                            # Deck's far field time, blocks building nearly all the time while driving, 2026-10-20)
WARM_STEPS_S = (0.0, 10.0, 20.0, 30.0)   # s ahead on the player's straight path whose road cells are planned early
WARM_AROUND = 2000.0        # m: and every cell this close to the player (any heading)
PREFETCH_START = 200.0      # m (Chebyshev) from the block centre before prefetching
# The mod's terrain-follow rule (mod follow.lua): it asks once the player is more than
# FOLLOW_PREPARE (Chebyshev) from the block centre, for snap256(player + v * FOLLOW_LEAD),
# clamped so the player stays within FOLLOW_REACH of the new centre on both axes
# (1020 - 600 m margin, §1.2; with the 2046 m collidable width of §12 P1 the N/E edge is at
# +1022, so this keeps >= 602 m there). tests/test_protocol_agreement.py checks these
# against follow.lua.
FOLLOW_PREPARE = 300.0
FOLLOW_LEAD = 4.0
FOLLOW_REACH = 420.0
FRAME_S = 0.05              # the mod decides per frame, so it asks just past the threshold


@dataclass(frozen=True)
class FollowRule:
    """The mod's terrain-follow table for one block size (follow.lua, its flat *_1024 / *_512
    constants; tests/test_protocol_agreement.py checks both sides): centre grid, prepare distance
    (Chebyshev from the centre), target lead (s) and reach (max |centre - player| per axis), the
    prefetch start, and the fast mode (|v| > fast_v: lead fast_lead, reach fast_reach, the prepare
    test at player + v * fast_ahead). 1024² has no fast mode (fast_v infinite)."""
    grid: float
    prepare: float
    lead: float
    reach: float
    prefetch_start: float
    fast_v: float = math.inf
    fast_lead: float = 0.0
    fast_reach: float = 0.0
    fast_ahead: float = 0.0
    min_lead: float = 0.0           # the target at least this far ahead of a moving player (follow.lua MIN_LEAD)
    min_lead_v: float = 2.0         # (moving: faster than this, m/s; follow.lua MIN_LEAD_V)

    def mode(self, vx, vy):
        """(lead, reach, prepare-test ahead s) for this velocity."""
        if vx * vx + vy * vy > self.fast_v * self.fast_v:
            return self.fast_lead, self.fast_reach, self.fast_ahead
        return self.lead, self.reach, 0.0


# docs/V1_4_SPEC.md §3.5 (M4): the 512² block (collidable [c-512, c+510]) on a 128 m grid: prepare at
# 128 m, lead 5 s (was 2: 2026-10-15, fewer moves), reach 510 - 250 = 260 m; fast mode above 150 m/s: lead 3 s, reach 510 - 90 = 420 m,
# prepare test 0.75 s ahead; the prefetch starts 28 m before the prepare distance.
FOLLOW_RULES = {
    1024: FollowRule(heightmap.GRIDS[1024], FOLLOW_PREPARE, FOLLOW_LEAD, FOLLOW_REACH, PREFETCH_START),
    512: FollowRule(heightmap.GRIDS[512], 128.0, 5.0, 260.0, 100.0,
                    fast_v=150.0, fast_lead=3.0, fast_reach=420.0, fast_ahead=0.75, min_lead=256.0),
}
BLOCK_DEFAULT = 1024        # Server()'s block (test_v131_default.py pins the library default)
# The command line's block in mesh mode: M4's 512² block (in-game 2026-10-10: P2 steady import 16 ms vs
# 45 ms, P7, P11 and the 134 m/s drive passed; docs/research/2026-10-10-m4-m1h-ingame.md)
CLI_BLOCK_MESH = 512
# |cx|,|cy| of an anchor's first terrain. A rebase hint stays within ~512 m of the new origin, but
# a resync (same origin, after a server restart) asks for the block around the player, which can be
# up to the mod's maximum rebase distance (16 km) away, plus however far the player got past it.
# Clamping that to ±768 m made the mod reject every answer as stale and re-request forever.
ANCHOR_CENTRE_MAX = 21000.0
# A superseded anchor's files are deleted this long after the new anchor's far_done: the mod
# still shows (and deletes, time-sliced) old tiles after far_done, and the engine watches
# loaded files (ge/main.lua onFileChanged), so a shape must not vanish under a live TSStatic.
PRUNE_DELAY = 30.0
# A tile the server swapped out (retired) keeps its files this long: the mod shows it until the swap
# has run there, and with an import backlog (in-game 2026-10-10, ball drive at ~38 m/s: ~1000 swap
# groups waiting) that took minutes; evicting at 30 s deleted shapes under shown tiles, which then
# rendered as nothing (the world went invisible, collision stayed). v1.4 replaces this guess with
# the mod reporting its deletes.
RETIRED_KEEP = 600.0
PRUNE_RECHECK = 2.0          # s; a prune waiting for the mod to let go of an anchor's tiles re-checks this often
# The game watches /temp: deleting ~400 tile files in one burst stalled it for 200-340 ms
# (in-game 2026-10-10, ~30 s after every rebase). Old files are deleted gradually instead.
DELETE_INTERVAL = 0.2           # seconds between file deletions (5 files/s)
# Mesh mode with imagery writes ~5x as many files per km (64/128 m tiles and a texture per tile:
# ~400 tiles = 800 files per km; a 10 km anchor leaves ~11k), more than 5 files/s can ever delete.
# There the queue drains faster when it is long, still one file at a time and evenly spaced:
# rate = backlog / DELETE_DRAIN_S, between 5 and DELETE_MAX_RATE files/s.
DELETE_DRAIN_S = 60.0
DELETE_MAX_RATE = 40.0
# Horizon mode (M1h): the eye is quantised so a car on a slope does not re-select every pose: it moves
# when it changed by more than max(EYE_STEP, EYE_STEP_REL * eye). Without a camera in the pose the eye
# is EYE_ABOVE_PLAYER over the player's z. The radius is re-sent (status.radius) when it changed by
# more than RADIUS_REPORT_REL; a failed coarse DEM pass is retried after HORIZON_RETRY s.
EYE_STEP = 10.0
EYE_STEP_REL = 0.05
EYE_ABOVE_PLAYER = 1.5
RADIUS_REPORT_REL = 0.02
HORIZON_RETRY = 30.0
HORIZON_BLOCK_CACHE = 1024      # DEM tiles' block maxima kept between anchors (16 KB each)
# Anchor height quantum: relief survey of +-SURVEY_HALF around the anchor at DEM zoom SURVEY_ZOOM.
SURVEY_HALF = 15000.0
SURVEY_N = 61                       # 500 m apart
SURVEY_WINDOW = 1.5                 # blocks: the windows survey_q takes the relief of (an unaligned block, z10 smoothing)
BLOCK_SURVEY_M = 2048.0             # m: the largest block (1024² at 2 m); Server passes its own
SURVEY_ZOOM = 10
DELTA_MARGIN = 0.1          # m added to measured swap-group deltas (crossings between vertices)
NICE = 10                   # default niceness of the server process (§4.5)
NICE_ROADS = 3              # the road tile workers' niceness on top of it (blockworker.init: the terrain blocks first)
NICE_FAR = 6                # the far field's (the scenery far away last)
NICE_FAR_BUILDINGS = 9      # the far buildings': after the far field (on a Steam Deck's four cores a teleport's far
                            # buildings took ~23 s of CPU, next to the road tiles and the far field)
IMAGERY_PREFETCH = 40       # queued tiles whose imagery is fetched ahead of their build (only spare request tokens)
PREFETCH_AGAIN_S = 2.0      # s before a queued tile's imagery prefetch is asked for again
PREFETCHED_MAX = 8192       # tiles remembered as prefetched (a connection's record starts again past this)
# status.backlog (docs/V1_4_SPEC.md §3.0, §4): every status message carries the number of desired
# tiles of the current anchor not shown yet. Status messages only go out on events (v1.3.1); with
# --status-interval S > 0 the last status is also re-sent from on_pose at most every S seconds
# while the backlog changed, so tools/drive.py can sample it (M0 harness runs use 1).
STATUS_INTERVAL = 0.0
NET_TICK_S = 60.0           # the network summary line in the log and the netstats file
# Session folders of earlier runs (docs/ROADMAP.md "Robustness" 3). The game may still show a dead
# server's tiles (the engine watches loaded files: a deleted shape under a live TSStatic renders as
# nothing), so a restarted server deletes at start only folders whose marker is untouched for
# SESSION_STALE_S; younger ones wait for a game to claim them (hello / pose `sessions`). Unclaimed,
# one goes once SESSION_ORPHAN_S passed since the server found it AND since its marker was last
# touched (a live server touches its own, and its adopted ones, every SESSION_HEARTBEAT_S).
SESSION_STALE_S = 86400.0
SESSION_ORPHAN_S = 600.0
SESSION_HEARTBEAT_S = 60.0
SESSION_SWEEP_S = 30.0      # main_async: how often sweep_sessions runs
# Live retexture (--colormatch / --deshadow): when a region a shown tile was rendered without becomes
# ready, the tile's texture is rendered again under its next revision's name and the mod is told
# (far_retex) to point the tile's material at it. At most RETEX_INFLIGHT such renders on the tile pool
# at once (the other workers keep building), started at most RETEX_RATE per second, nearest the player
# first. A tile is re-rendered once per region of it that turns ready (a coarse L5 / L6 texture spans up
# to 3 x 3 / 4 x 4 z12 regions, as many de-shadow ones); the guard against a loop is RETEX_REPEAT re-renders
# in a row that came back still waiting for a region already ready: past that it waits to be re-sent.
# --retex-rate 0: no live swap (a stale tile is re-rendered when it is sent again, as before).
RETEX_RATE = 5.0
RETEX_INFLIGHT = 1
RETEX_REPEAT = 3
# Shown in the mod's UI (V1_3_SPEC §C), after the imagery credits.
ELEVATION_CREDIT = "Elevation: Mapterhorn (see mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen"

log = logging.getLogger("earthd")


@dataclass
class Anchor:
    id: int
    lat: float
    lon: float
    frame: geodesy.EnuFrame
    hm_zoom: int = 15
    zooms: dict = field(default_factory=dict)      # far level -> DEM zoom (L0 = hm_zoom)
    first_terrain: tuple = None                    # (cx, cy) of the anchor_ready terrain
    q: float = None                                # height quantum, from the relief survey
    q_lock: threading.Lock = field(default_factory=threading.Lock)
    grader: object = None                          # grading.Grader (--grading on), made on first use
    chunks: object = field(default_factory=heightmap.ChunkCache)   # the physics blocks' heights per 128 m
    bridges: object = None                         # roads.BridgeProfiles: deck heights per chain
    tunnels: object = None                         # roads.TunnelProfiles: floor heights per chain
    first_base: tuple = None                       # ((cx, cy), Future): a provisional first block's photo, reused
    zf: int = 14                                   # far level 3's Web-Mercator zoom here (farfield.global_zoom)


PIECE_PARTS = 24            # nodes whose geometry and texture the server keeps for their other pieces (Server.piece_parts)
# The far tiles' server key of a global tile (farfield.GTile, geotiles phase 3b): (GLOBAL_AID, key), the same under
# every anchor (anchor ids start at 1)
GLOBAL_AID = 0


@dataclass
class TileInfo:
    """A built far tile (server-wide cache entry)."""
    pos: list
    err: float
    sink: float | None
    skirt: float
    grid: np.ndarray            # (cells + 1)² absolute vertex heights (float32), for swap deltas
    bytes: int
    seconds: float
    material: str | None = None     # per-tile imagery material (None: the static palette material)
    texture: str | None = None      # file name of its texture (next to the shape)
    tex_bytes: int = 0
    tex_seconds: float = 0.0
    # --colormatch / --deshadow: regions whose correction was not ready when the texture was rendered
    # (left raw there; de-shadow ones as (imagery.DS_PENDING, rx, ry)), whether one of them has turned
    # out to change it (the texture is then rendered again under a new file name: live for a tile on
    # screen, far_retex, else the next time the tile is sent), and the texture's revision
    cm_pending: frozenset = frozenset()
    stale: bool = False
    rev: int = 0
    # live retexture: re-renders in a row that came back stale at once (still waiting for a region
    # whose status is ready: the loop guard, RETEX_REPEAT)
    retex_repeat: int = 0
    credits: frozenset = frozenset()    # data-credit source ids (credits.tile_sources; --credits on only)
    shape: str | None = None    # its shape file when not tile_name(): a copy under a new material name (reslot)
    tile: object = None         # the farfield tile it was built for (Tile, GTile, Piece)
    frame: tuple | None = None  # a global tile's frame (lat, lon): its geometry is in that frame's world


def predict_centre(px, py, vx, vy, lead, reach=FOLLOW_REACH, grid=heightmap.GRID, min_lead=0.0, min_lead_v=2.0):
    """snap256(p + v*lead) (M4: the block's grid), the lead at least min_lead metres while moving (follow.lua's
    MIN_LEAD), clamped so p stays within `reach` of the centre on both axes."""
    lx, ly = vx * lead, vy * lead
    v2 = vx * vx + vy * vy
    if min_lead > 0 and v2 > min_lead_v * min_lead_v and lx * lx + ly * ly < min_lead * min_lead:
        k = min_lead / math.sqrt(v2)
        lx, ly = vx * k, vy * k
    out = []
    for p, d in ((px, lx), (py, ly)):
        c = heightmap.snap(p + d, grid)
        lo = math.ceil((p - reach) / grid) * grid
        hi = math.floor((p + reach) / grid) * grid
        out.append(float(min(max(c, lo), hi)) + 0.0)
    return tuple(out)


def predict_request(px, py, vx, vy, cx, cy, rule=FOLLOW_RULES[1024]):
    """The centre the mod will ask for (V1_1_SPEC 4 prefetch): where a player moving
    straight at (vx, vy) leaves the FOLLOW_PREPARE box around the block centre, then the
    mod's own target rule there. (Spec 4 suggests snap256(p + v*6 s); predicting the mod's
    exact request instead avoids builds that are never asked for.) rule: the block size's
    follow table; in its fast mode the box test is on the point v * fast_ahead ahead."""
    lead, reach, ahead = rule.mode(vx, vy)
    v2 = vx * vx + vy * vy
    if (rule.min_lead > 0 and rule.min_lead_v ** 2 < v2 and v2 * lead * lead < rule.min_lead ** 2
            and (cx - px) * vx + (cy - py) * vy > 0
            and max(abs(px - cx), abs(py - cy)) <= rule.min_lead + rule.grid):
        # (follow.lua centreAhead: the centre just committed is ahead; the next request once the player is past it)
        t0 = ((cx - px) * vx + (cy - py) * vy) / v2 + FRAME_S
        px, py = px + vx * t0, py + vy * t0
    qx, qy = px + vx * ahead, py + vy * ahead
    t = 0.0
    if max(abs(qx - cx), abs(qy - cy)) <= rule.prepare:
        exits = [((c + math.copysign(rule.prepare, v)) - q) / v
                 for q, v, c in ((qx, vx, cx), (qy, vy, cy)) if abs(v) > 0.5]
        t = min(exits) + FRAME_S if exits else 0.0
    return predict_centre(px + vx * t, py + vy * t, vx, vy, lead, reach, rule.grid, rule.min_lead, rule.min_lead_v)


def _num(msg, name, default=None):
    v = msg.get(name, default)
    if v is None:
        return default
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite")
    return v


def survey_q(frame, dem, block_m=BLOCK_SURVEY_M):
    """Height quantum of an anchor (§1.3): the smallest ladder step whose block range covers the
    largest relief inside any window of SURVEY_WINDOW blocks (block_m m) of a +-15 km survey at DEM
    z10, plus 10 %. A block only has to hold its own relief (its zBase is its own; one that does not
    fit takes the next coarser step, heightmap.block_params). The whole survey's relief made every
    mountain anchor 1/16 m: 6 cm steps on the runways and roads (Courchevel, in-game 2026-10-13)."""
    xs = np.linspace(-SURVEY_HALF, SURVEY_HALF, SURVEY_N)
    u = heightmap.surface_u(frame, lambda la, lo: dem.sample(la, lo, SURVEY_ZOOM), xs, xs)
    k = min(len(xs), int(math.ceil(SURVEY_WINDOW * block_m / (xs[1] - xs[0]))) + 1)
    win = np.lib.stride_tricks.sliding_window_view(u, (k, k))
    return heightmap.choose_q(float((win.max(axis=(2, 3)) - win.min(axis=(2, 3))).max()))


def road_tracker(conn):
    """A connection's road tiles' tracker: geotiles (roads.GeoTracker) when its mod places them (Connection.road_geo),
    else the anchor's."""
    return roadsmod.GeoTracker() if getattr(conn, "road_geo", False) else roadsmod.Tracker()


class Server:
    def __init__(self, user_dir, cache_dir, tile_workers=3, kfun=None,
                 hysteresis=farfield.HYSTERESIS, radius=farfield.RADIUS, far_format="cdae",
                 imagery=imagerymod.DEFAULT_CHAIN, near_visual="mesh", tex_near=TEX_NEAR, tex_far=TEX_FAR,
                 tone=1.0, near_radii=None, status_interval=STATUS_INTERVAL, cells=CELLS_DEFAULT, tex=None,
                 block=BLOCK_DEFAULT, horizon=False, deshadow="off", weather=None, gravity=None, net=None,
                 colormatch="off", credits=None, cache=None, retex_rate=RETEX_RATE, surface=None,
                 mapdata=None, grading=False, road_widths=False, block_pools=(), sidewalks=True, buildings=False, traffic=False, far_pool=None,
                 road_pool=None, airports_db=None, sea=None, vegetation=None, leaf_on=True, season_mode="off",
                 far_bld_pool=None, gas_stations=False):
        """kfun: K(L) for every level (default: the layout's K table); near_radii: the min-level
        radii (default: the layout's); cells: 32 (v1.3.1) or 64 (M1, mesh mode only); tex: texture
        px per level from the finest (default: tex_near, tex_near, tex_far with 32 cells,
        farfield.TEX_DEFAULT[64] with 64); block: terrain block samples per edge, 1024 (v1.3.1) or
        512 (M4, mesh mode only: a 128 m centre grid and the 512 follow table); horizon: M1h, the
        far field out to the line-of-sight horizon (mesh mode only; `radius` is then unused); net:
        the netfetch.Fetcher every download goes through (main_async passes the command line's; None =
        one of its own, no statistics file); colormatch: --colormatch (off, or s2: the colour-seam
        feather on the USGS tiles); cache: the download cache's cache.Cache (last use per file, the
        size cap, the page's 'cache' messages; None = no bookkeeping and no 'cache' message, Server()'s
        default); retex_rate: --retex-rate (live retextures per second, 0 = none)."""
        self.sid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.user_dir = user_dir           # the game's user folder (detail mode fills its terrain texture cache)
        root = self.root = os.path.join(user_dir, "temp", "beamngEarth")
        self.cdae_root = os.path.join(user_dir, *CDAE_ROOT)
        # earlier runs' session folders a game may still show: sid -> monotonic time found / released
        # by a connection (docs/ROADMAP.md "Robustness" 3; claim_sessions, sweep_sessions)
        self.orphans = {}
        self.orphan_s = SESSION_ORPHAN_S
        self.heartbeat_at = time.monotonic()
        self._scan_sessions()
        self.out_dir = os.path.join(root, self.sid)
        self.cdae_dir = os.path.join(user_dir, *CDAE_ROOT, self.sid.lower())
        self.vfs_dir = f"/temp/beamngEarth/{self.sid}"
        self.prewarm_vfs = None             # the materials' prewarm shape (prewarm.py), written at the first hello
        self.matslots = matslotsmod.MatSlots(self.sid[-4:], held=self.slot_held)    # pooled names and shape files
        self.reslot_serial = itertools.count(1)
        self.slot_serial = itertools.count(1)      # far_tile slotSerial (matslots.py)
        self.base_px = BASE_PX              # detail mode: the ground photo's size (px), the mod's graphics.photoPx
        self.lod_px = farfield.SSE_TAU_PX   # screen-space LOD: the largest split pop allowed on screen (px; 0: off)
        self.update_check = False       # ask GitHub for a newer version after a hello (main_async turns it on; tests
                                        # never reach GitHub: its small daily budget ran out over a day of test runs)
        os.makedirs(self.out_dir, exist_ok=True)
        open(os.path.join(self.out_dir, SESSION_MARKER), "w").close()
        if far_format not in farfield.FORMATS:
            raise ValueError(f"unknown far-tile format {far_format!r}")
        self.far_format = far_format
        if near_visual not in NEAR_VISUALS:
            raise ValueError(f"unknown near-visual mode {near_visual!r}")
        # mesh: the TerrainBlock is physics only, the ground is drawn by the finest tiles (§B)
        self.near_visual = near_visual
        # the near tiles' asphalt layer (mesh mode: the tiles are the ground seen; surface.road_mask)
        self.road_masks = roadsmod.ROADS_IN_GROUND and near_visual not in BLOCK_DRAWN
        if tex is None and cells == 32:
            tex = (tex_near, tex_near, tex_far)         # the v1.3.1 rule: the two finest levels are near
        # Every layout-dependent rule reads self.layout (terrain mode: frozen at 32 cells)
        self.layout = farfield.Layout(cells, block, near_visual in MESH_LAYOUT, tex)
        self.follow = FOLLOW_RULES[self.layout.block]     # the mod's terrain-follow table for this block
        self.min_level = self.layout.min_level
        # the finest level under a drawn block's forced box (detail: L-1, whose 128 m edges meet the 512²
        # block's 128 m-grid edges; terrain: L0 under the 256 m-grid 1024² block)
        self.forced_level = max(self.min_level, -1) if near_visual == "detail" else 0
        self.near_radii = dict(self.layout.near_radii if near_radii is None else near_radii)
        self.tile_cache = TILE_CACHE[cells]
        self.net = net if net is not None else netfetch.Fetcher()
        self.dem = demmod.Dem(os.path.join(cache_dir, "dem"), fetcher=self.net)
        import railways as _railways    # (coarse elevation: rail decks raised over the roads under them)
        _railways.COARSE_AT = self.dem.coarse_at
        # runways graded in (--airports on: main() loads the OurAirports copy before the worker processes)
        self.airports = airportgrademod.AirportGrader(airports_db, self.dem) if airports_db is not None else None
        self.airports_db = airports_db  # (also the road rules' country: navgraph.country_near)
        self.sea = sea              # seacarve.SeaCarver (--sea on): the sea floor carved, the water tiles
        self.veg = vegetation       # vegetation.Vegetation (--trees on): trees, bushes, rocks per cell
        if sea is not None and sea.dem is None:
            sea.dem = self.dem      # the lakes' levels
        if vegetation is not None and vegetation.dem is None:
            vegetation.dem = self.dem   # rocks on bare ground by its slope (vegetation.ROCK_SLOPE)
        # Satellite imagery (§A); None = --imagery off (v3-style palette tiles).
        # De-shadowing of the USGS tiles (--deshadow lambert|full; off = the imagery as served).
        self.deshadow = None
        if deshadow != "off" and imagery and deshadowmod.SOURCE in [getattr(s, "name", s) for s in imagery]:
            self.deshadow = deshadowmod.Deshadow(cache_dir, self.dem, deshadow)
            self.deshadow.add_listener(self._ds_listener)
        # Colour seams of the USGS mosaic (--colormatch s2; off = no object, textures as before).
        self.tex_modes = (deshadow, colormatch)     # (the revisit cache's key of a far texture: tex_key)
        self.colormatch = None
        if colormatch != "off" and imagery and colormatchmod.SOURCE in [getattr(s, "name", s) for s in imagery]:
            self.colormatch = colormatchmod.ColorMatch(cache_dir, colormatch)
            self.colormatch.add_listener(self._cm_listener)
        self.loop = None                    # the event loop (set by tile()), for the colormatch listener
        self.cm_waiting = {}                # region -> {(anchorId, key)} rendered without its correction
        self.tex_rev = {}                   # (anchorId, key) -> texture revision of its next build
        # live retexture (far_retex): stale tiles to re-render while shown, the renders running, the
        # earliest start of the next one, its timer, and the counts for the log / tests
        self.retex_rate = retex_rate
        self.retex_inflight = RETEX_INFLIGHT
        self.retex_todo = set()
        self.retex_busy = 0
        self._retex_next = 0.0
        self._retex_timer = None
        self.retex_stats = {"started": 0, "sent": 0, "dropped": 0}
        self.tone = tone
        self.imagery = (imagerymod.Imagery(os.path.join(cache_dir, "imagery"), imagery, tone=tone,
                                           deshadow=self.deshadow, colormatch=self.colormatch, fetcher=self.net,
                                           leaf_on=leaf_on, season_mode=season_mode)
                        if imagery else None)
        self.imagery_failures = 0           # tiles that fell back to the palette (network trouble)
        self.imagery_worker = 0             # far textures the far-tile processes rendered (blockworker.far_tile)
        self.base_worker = 0                # base photos the block processes rendered (blockworker.base_photo)
        self.map_port = None                # the in-game map's tile endpoint (maptiles.py), hello.mapPort
        self.map_tiles = None               # its MapTiles: also written as files on request (map_tiles message)
        self.map_files = None               # the maptiles.TileFiles writing them (made by the first request)
        # (anchorId, key) of cached tiles built with the palette because the imagery failed; shown in
        # the status note (re-texturing them once the host is back is a ROADMAP follow-up)
        self.degraded = set()
        # Heightmap workers: with the block processes they mostly wait on a process, and with two a
        # request queued behind a prefetch and an earlier request (3.5 s at 90 m/s, 2026-10-12)
        self.hm_pool = ThreadPoolExecutor(HM_THREADS, thread_name_prefix="heightmap")
        self.hm_here, self.hm_here_lock = 0, threading.Lock()  # blocks building in this process (far tiles wait)
        # a block's base photo rendered beside its heights (they do not depend on each other): after them it added
        # 2.3 s to a 12 s Baltimore block on a Steam Deck (2026-10-20)
        self.base_pool = ThreadPoolExecutor(HM_THREADS, thread_name_prefix="base-photo")
        # the photos a player waits for (a teleport's first block, the request lane's): never queued behind the other
        # blocks' (each holds a base-photo thread until its block process renders it; a teleport's photo waited
        # ~20 s behind the old place's on a Deck, 2026-10-23)
        self.urgent_base_pool = ThreadPoolExecutor(2, thread_name_prefix="base-urgent")
        self.ground_pool = ThreadPoolExecutor(1, thread_name_prefix="ground-ahead")   # groundahead.py
        # the world map's requests (GPS route, address search, map tiles): their own threads, so a teleport's tile
        # builds on the default pool do not hold them back (a route took 30 s after one, 2026-10-14)
        self.ui_pool = ThreadPoolExecutor(2, thread_name_prefix="map-ui")
        # (a thread per helper slot too: a far thread waits for its tile's builder, here or on a helper)
        self.cache_dir = cache_dir
        self.picking = None          # the settings page's file window open (apppaths.pick): for which path
        revisitmod.CACHE_DIR = cache_dir
        self.refetched = 0              # far textures whose missing photo tiles were fetched here (make_tile)
        # self.share_lock, self.group_helper = threading.Lock(), None    # (share_start: shared world building, commented out)
        self.tile_pool = ThreadPoolExecutor(tile_workers + (peerworkmod.MAX_SLOTS if hasattr(far_pool, "remote_slots")
                                                            else 0), thread_name_prefix="far")
        # Road plans (grading) of the map cells ahead of the player, made before a block needs them: a
        # cell's first plans (and street widths) take 2-10 s, more than a block move allows at speed
        # One thread takes the most urgent cell of the latest pose's list each time (not a FIFO: cells
        # queued while parked must not hold up the ones on the path once the player drives off)
        self.warm_want = []             # (grader, cell key), most urgent first (replaced every pose)
        # The follow / prefetch blocks build in processes of their own (blockworker.py: cores of their own,
        # not this interpreter's lock): (ProcessPoolExecutor of one, its queue of the road cells ahead) per
        # lane, LANE_REQUEST and LANE_PREFETCH, so a block the mod asks for never waits behind a prefetch
        # (one shared process: answers up to 4 s at 90 m/s, 2026-10-12). Empty: built in hm_pool's threads.
        self.block_pools = list(block_pools)
        # Far-tile geometry in worker processes too (blockworker.far_geometry; the texture stays here): a
        # ProcessPoolExecutor of tile_workers, or None (built in tile_pool's threads)
        self.far_pool = far_pool
        self.road_pool = road_pool          # road tiles in processes (blockworker.roads_tile), else threads
        # the highway signs (signstream.py) in the same processes: no process of their own, their map cells warm there
        # (at most signstream.WORKERS a connection, only for road tiles sent)
        self.sign_pool = road_pool
        # far building tiles in a process of their own: in the road pool (first come, first served) a teleport's
        # hundreds of them queued ahead of the road tiles under the car, which drove on bare ground for minutes
        # (Sinaia 2026-10-15); falls back to the road pool
        self.far_bld_pool = far_bld_pool or road_pool
        self.warm_cv = threading.Condition()
        self.warm_thread = None
        self.tile_workers = tile_workers
        self.delta_pool = ThreadPoolExecutor(1, thread_name_prefix="deltas")   # swap group deltas (emit_groups)
        self.kfun, self.hysteresis, self.radius = kfun or self.layout.k, hysteresis, radius
        self.status_interval = status_interval
        # M1h: the line-of-sight far field. One worker builds the anchors' zmax grids (coarse DEM
        # passes) so neither a heightmap nor a tile ever waits behind one.
        if horizon and near_visual not in MESH_LAYOUT:
            raise ValueError("horizon mode needs the mesh (or detail) near-visual mode")
        self.horizon = bool(horizon)
        self.horizon_pool = ThreadPoolExecutor(1, thread_name_prefix="horizon") if self.horizon else None
        self.ahead_pool = ThreadPoolExecutor(1, thread_name_prefix="download-ahead")   # download_ahead
        self.ahead_busy = False
        self.ahead_last = None          # (anchor id, x, y) of the last round
        self.ahead_stats = {"dem": 0, "cells": 0, "deferred": 0}
        self.route_pool = ThreadPoolExecutor(1, thread_name_prefix="route-download")   # Connection.route_download
        self.horizon_grids = {}             # anchorId -> farfield.HorizonGrid
        self.horizon_inflight = {}          # anchorId -> (future, half-size)
        self.horizon_failed = {}            # anchorId -> monotonic time of the last failed build
        self.horizon_blocks = OrderedDict()     # (zoom, x, y, block) -> block maxima, LRU
        self.anchors = {}
        self._next_anchor = 1
        self.connections = set()
        # Heightmaps: (anchorId, cx, cy) -> info, LRU order.
        self.hm = OrderedDict()
        self.hm_inflight = {}
        self.hm_stage = {}              # terrain key -> what its build waits on now (the stuck log, prepare_terrain)
        self.ter_serial = itertools.count()
        self.base_stale = set()         # blocks whose base photo lacks lazy imagery (NAIP) still to come
        self.pads_rebuild = set()       # blocks built without gas station pads that lie on them (pads_stale)
        self.roads_rebuild = set()      # blocks built before every road cell under them was on disk (roads_refresh)
        self.pads_changed_at = 0.0      # (time.monotonic) the last new pad: Connection.pads_refresh waits a little
        self.fast = False               # the player faster than FAST_BASE_V (the latest pose): photos follow blocks
        self.base_pending = {}          # block -> the analysis regions its base photo was rendered without
        self.base_rev = itertools.count(1)
        self.hm_committed = deque(maxlen=HM_KEEP_COMMITTED)
        self.hm_sent = deque(maxlen=HM_KEEP_SENT)
        # Far tiles: (anchorId, key) -> TileInfo, LRU order; files are far_<anchorId>_<key>.<fmt>.
        self.tiles = OrderedDict()
        self.tile_inflight = {}
        # A node across the global tiles' region edge is a piece per global tile it overlaps (farfield.Piece), each
        # its node's geometry and texture cut: (anchorId, node key) -> {"built", "tex": (DDS bytes, bytes, sources,
        # pending regions)} of the last few, so the node's other pieces build neither again
        self.piece_parts = OrderedDict()
        self.build_stats = deque(maxlen=500)   # seconds per built tile, for the log
        self.level_stats = {}                  # level -> deque of (build s, texture s, shape B, texture B)
        self.prune_delay = PRUNE_DELAY
        self.prune_recheck = PRUNE_RECHECK
        self.retired_keep = RETIRED_KEEP
        self.delete_interval = DELETE_INTERVAL
        self.delete_max_rate = DELETE_MAX_RATE if near_visual in MESH_LAYOUT else 1.0 / DELETE_INTERVAL
        self.delete_queue = deque()     # (path, tile key or None) waiting for a rate-limited delete
        self._deleting = False
        self._prune_scheduled = set()
        # Real weather at the player (weather.py): a weathermod.Service, or None (--weather off)
        self.weather = weather
        # Real gravity at the player (gravity.py): a gravitymod.Provider, or None (--gravity off)
        self.gravity = gravity
        # On-screen data credits (credits.py): a creditsmod.Credits, or None (--credits off; Server()'s default)
        self.credits = credits
        self.surface = surface      # surface.Surface: ground models under the physics block (--surface on)
        if surface is not None and self.airports is not None:      # the runways painted in (GitHub #337)
            surface.extra_pads = self.airports.runway_pads
        self.mapdata = mapdata      # mapdata.MapData for the roads (--roads on), else None
        if mapdata is not None and getattr(mapdata, "dem", 0) is None:
            mapdata.dem = self.dem  # (gradesep's ground)
        self.grading = bool(grading) and mapdata is not None   # ground graded under roads (grading.py)
        # street widths measured between the buildings (roadwidth.py), decided once per road per run
        self.sidewalks = sidewalks  # raised sidewalk meshes with the road tiles (sidewalks.py)
        self.buildings = buildings  # building meshes with the road tiles (buildings.py)
        # gas stations with refuelling with the road tiles (gasstations.py; the user, 2026-10-23)
        # (the settings page's switch turns them off and on again: set_gas_stations; never on without the flag)
        self.gas_allowed = bool(gas_stations) and mapdata is not None
        self.gas_stations = self.gas_allowed
        if self.gas_stations and surface is not None and hasattr(surface, "gas_pads"):
            surface.gas_pads = True     # their forecourts painted asphalt (surface.Surface.fuel_pads)
        self.traffic = bool(traffic) and mapdata is not None   # the AI road graph around the player (navgraph.py)
        self.road_widths = (roadwidthmod.RoadWidths(
            lambda key: mapdata.cell(key, layers=("buildings",), kind="demand").layers.get("buildings"),
            roadwidthmod.store_dir(cache_dir, mapdata)).get
            if road_widths and mapdata is not None else None)
        if vegetation is not None:      # trees kept off the roads' drawn widths (vegetation._mask_roads)
            vegetation.surface, vegetation.widths = surface, self.road_widths
        self.grader_lock = threading.Lock()
        # The download cache's bookkeeping (cache.py): every producer reports its disk hits and writes
        self.cache = cache
        self.covermap = None            # the map's cache view (covermap.CoverMap; main_async, with a Cache)
        self.areas = None               # saved areas downloaded ahead (areas.Areas; main_async, with a Cache)
        if cache is not None:
            for producer in (self.dem, self.imagery, self.deshadow, self.colormatch):
                if producer is not None:
                    producer.disk = cache

    def _scan_sessions(self):
        """Session folders left by earlier runs (only ones we marked): one whose marker is untouched
        for SESSION_STALE_S goes now, with the engine's .cdae caches of its shapes; a younger one is
        kept as an orphan until a game claims it or sweep_sessions deletes it."""
        wall, now = time.time(), time.monotonic()
        for d in glob.glob(os.path.join(self.root, "*", SESSION_MARKER)):
            sid = os.path.basename(os.path.dirname(d))
            if sid == self.sid:     # a restart in the same second with the same suffix: ours now, never released
                continue
            try:
                age = wall - os.path.getmtime(d)
            except OSError:
                continue
            if age > SESSION_STALE_S:
                self._rmtree_session(sid)
            else:
                self.orphans[sid] = now
                log.info("session %s of an earlier run (%.0f min old) kept until the game says whether it still "
                         "shows its tiles", sid, age / 60)

    def _rmtree_session(self, sid):
        # detail mode's packed copies of its base photos in the game's terrain texture cache (terraincache.py) go
        # with it: blocks evicted during a run took theirs (prune), but whatever a run left behind stayed for good,
        # ~1.4 MB a block, 3.9 GB of them by 2026-10-18
        for path in glob.glob(os.path.join(self.root, sid, "*_base*.png")):
            for px in BASE_SIZES:           # (whichever size it was made at)
                key = terraincache.base_key(f"/temp/beamngEarth/{sid}/{os.path.basename(path)}", base_ao_vfs(px))
                try:
                    os.remove(os.path.join(terraincache.cache_dir(self.user_dir), key + ".dds"))
                except OSError:
                    pass
        shutil.rmtree(os.path.join(self.root, sid), ignore_errors=True)
        shutil.rmtree(os.path.join(self.cdae_root, sid.lower()), ignore_errors=True)

    def _touch(self, sid):
        try:
            os.utime(os.path.join(self.root, sid, SESSION_MARKER))
        except OSError:
            pass

    def claimed_sessions(self):
        """Earlier runs' sessions some connected game says its objects still use."""
        out = set()
        for c in self.connections:
            out |= c.sessions
        return out

    def claim_sessions(self, conn, sessions):
        """hello / pose `sessions` of a connection: the session folders whose files the game's objects
        still use. Earlier runs' folders among them are adopted (kept, their markers touched); one this
        game named before and names no longer is released (release_session) unless another game
        claims it. Our own session and unknown names are ignored."""
        if isinstance(sessions, dict):      # an empty Lua table may encode as {}
            sessions = list(sessions.values())
        if not isinstance(sessions, list):
            return
        mine = {sid for sid in sessions if isinstance(sid, str) and sid in self.orphans}
        for sid in sorted(mine - conn.sessions):
            log.info("adopting session %s: the game still shows its tiles, its files stay until it lets go", sid)
            self._touch(sid)
        released = conn.sessions - mine
        conn.sessions = mine
        others = self.claimed_sessions()
        for sid in sorted(released - others):
            self.release_session(sid)

    def drop_claims(self, conn):
        """A connection closed: the sessions it claimed are orphans again (the game may come back,
        perhaps to yet another server), their grace starting now. Called once the connection left
        self.connections."""
        others = self.claimed_sessions()
        now = time.monotonic()
        for sid in conn.sessions - others:
            if sid in self.orphans:
                self.orphans[sid] = now
        conn.sessions = set()

    def release_session(self, sid):
        """An adopted session the game no longer uses: its files go through the rate-limited delete
        queue (the game loaded them, P9), the marker and the folders last."""
        if self.orphans.pop(sid, None) is None:
            return
        d = os.path.join(self.root, sid)
        cd = os.path.join(self.cdae_root, sid.lower())
        files = sorted(glob.glob(os.path.join(d, "*")) + glob.glob(os.path.join(cd, "*")))
        log.info("session %s released by the game: deleting its %d files", sid, len(files))
        self.queue_delete(files + [os.path.join(d, SESSION_MARKER), d, cd])

    def sweep_sessions(self, remove=True):
        """Every SESSION_SWEEP_S (main_async, via sweep_sessions_async): touch our own marker and the
        claimed sessions' every SESSION_HEARTBEAT_S (a later server sees them in use), and delete the
        orphans nobody claimed for orphan_s whose markers are as old (one a live server touches is in
        use). Returns the deleted session ids; remove=False leaves their folders to the caller."""
        now, wall = time.monotonic(), time.time()
        claimed = self.claimed_sessions()
        if now - self.heartbeat_at >= SESSION_HEARTBEAT_S:
            self.heartbeat_at = now
            for sid in [self.sid, *sorted(claimed)]:
                self._touch(sid)
        gone = []
        for sid, since in sorted(self.orphans.items()):
            if sid in claimed or now - since < self.orphan_s:
                continue
            try:
                idle = wall - os.path.getmtime(os.path.join(self.root, sid, SESSION_MARKER))
            except OSError:
                idle = math.inf
            if idle < self.orphan_s:
                continue
            del self.orphans[sid]
            log.info("session %s of an earlier run: no game claimed it, deleted", sid)
            if remove:
                self._rmtree_session(sid)
            gone.append(sid)
        return gone

    async def sweep_sessions_async(self):
        """sweep_sessions on the event loop (its state is the loop's), the folders removed in a worker
        thread: a whole session is thousands of files, the streaming must not wait for the rmtree."""
        gone = self.sweep_sessions(remove=False)
        if gone:
            await asyncio.to_thread(lambda: [self._rmtree_session(sid) for sid in gone])
        return gone

    def snap(self, v):
        """Nearest block centre coordinate on the layout's grid (256 m; M4 512²: 128 m)."""
        return heightmap.snap(v, self.layout.grid)

    def new_anchor(self, lat, lon):
        a = Anchor(self._next_anchor, lat, lon, geodesy.EnuFrame(lat, lon))
        a.hm_zoom = demmod.zoom_for(lat, 1.25, demmod.FINE_ZOOM)    # ~1 m pixels where the data exists (dem.FINE_ZOOM)
        a.zooms = {lvl: self.layout.dem_zoom(lat, lvl) for lvl in range(1, farfield.HORIZON_MAX_LEVEL + 1)}
        a.zooms[0] = a.hm_zoom                      # L0 shares the terrain's DEM (§1.4)
        a.zf = farfield.global_zoom(lat)            # the global far tiles' finest zoom (geotiles phase 3b)
        if self.deshadow is not None:
            self.deshadow.prepare(lat, lon)         # the regions around it, in the background
        if self.colormatch is not None:
            self.colormatch.prepare(lat, lon)
        self._next_anchor += 1
        return a

    def anchor_q(self, anchor):
        """The anchor's height quantum, surveyed once (worker threads)."""
        with anchor.q_lock:
            if anchor.q is None:
                anchor.q = survey_q(anchor.frame, self.dem, self.layout.block * heightmap.SQUARE)
                log.info("anchor %d height quantum 1/%d m", anchor.id, round(1 / anchor.q))
            return anchor.q

    @staticmethod
    def _remove(path):
        for p in (path, path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def delete_gap(self):
        """Seconds until the next queued delete: delete_interval, or shorter for a long queue
        (up to delete_max_rate files/s, see DELETE_DRAIN_S)."""
        rate = min(self.delete_max_rate, len(self.delete_queue) / DELETE_DRAIN_S)
        return min(self.delete_interval, 1.0 / rate) if rate > 0 else self.delete_interval

    def _delete_one(self):
        path, ck = self.delete_queue.popleft()
        # an evicted tile built again since (same file name) keeps its new file
        if ck is None or (ck not in self.tiles and ck not in self.tile_inflight):
            if os.path.isdir(path):     # a released session's folder, queued after its files
                shutil.rmtree(path, ignore_errors=True)
            else:
                self._remove(path)

    def queue_delete(self, paths, ck=None):
        """Delete files gradually (one at a time, delete_gap() apart) so the game's file watcher
        never sees a burst. ck: the far tile (anchorId, key) the files belong to, if it may be
        built again meanwhile (LRU eviction): then they are left alone. Needs a running event
        loop; falls back to deleting at once."""
        self.delete_queue.extend((p, ck) for p in paths)
        if self._deleting:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            while self.delete_queue:
                self._delete_one()
            return
        self._deleting = True
        due = [loop.time() + self.delete_gap()]

        def step():
            if self.delete_queue:
                self._delete_one()
            if self.delete_queue:
                # a gap after the last one was due, not after it ran: Windows' loop wakes up to ~16 ms late at every
                # timer (a 25 ms gap took 33, 40 files/s became 30); behind by more than a gap (a stall): afresh,
                # no burst to catch up
                now = loop.time()
                due[0] += self.delete_gap()
                if due[0] < now:
                    due[0] = now + self.delete_gap()
                loop.call_later(due[0] - now, step)
            else:
                self._deleting = False
        loop.call_later(due[0] - loop.time(), step)

    # --- heightmaps ------------------------------------------------------
    def hm_name(self, key):
        aid, cx, cy = key
        return f"hm_{aid}_{int(cx)}_{int(cy)}.png"

    def ter_name(self, key):
        """A new .ter name for every build of key: the engine skips a terrainFile it already has, and a
        rebuilt block (an evicted one, or one with more of its land cover) can differ."""
        return f"{self.hm_name(key)[:-4]}_{next(self.ter_serial)}.ter"

    def hm_vfs(self, key):
        return f"{self.vfs_dir}/{self.hm_name(key)}"

    def build_veg_tile(self, aid, tx, ty, flat=False):
        """vegetation.py: one vegetation tile of anchor aid written to the session folder: (vfs path, n) or
        None; flat: as one flat array (a mod that reads it, hello.vegFlat). Worker threads (builds the map cells it
        lacks, ~3.5 s each the first time)."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.veg is None:
            return None
        zoom = anchor.zooms[0]
        g = self.grader_for(anchor)

        def elev(la, lo):
            z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))
            # graded under the roads as the block is: a rock beside a track cut into a slope floated 1.9 m over the cut
            # (Stelvio pass, the user's report 2026-10-17)
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        rows = vegetationmod.tile_items(anchor.frame, tx, ty, self.veg, lambda k: True, elev, self.sea, as_json=True)
        if rows is None:
            return None
        return vegetationmod.write_tile(self.out_dir, self.vfs_dir, aid, tx, ty, rows, flat=flat)

    global_far = True       # --global-far: far levels >= 3 as global tiles (geotiles phase 3b; the mod must place them)
    geo_tiles = True        # --geo-tiles: content in the global tiles' frames, kept by the mod across rebases (geotiles.py)

    def build_veg_geo(self, gx, gy, flat=False, aid=None):
        """vegetation.geo_tile_items: geotile (gx, gy)'s trees, bushes and rocks in its own frame, written to the
        session folder: (vfs path, n, frame (lat, lon), pivot) or None. No anchor in it: the ground is the graded one
        (the plans are the planning regions', whatever the anchor) at the active anchor's DEM zoom."""
        if self.veg is None:
            return None
        import geotiles
        anchor = self.anchors.get(aid) or (self.anchors[max(self.anchors)] if self.anchors else None)
        if anchor is None:
            return None
        zoom = anchor.zooms[0]
        g = self.grader_for(anchor)

        def elev(la, lo):
            z = self.ground(None, la, lo, self.dem.sample(la, lo, zoom))
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        got = vegetationmod.geo_tile_items(gx, gy, self.veg, lambda k: True, elev, self.sea, as_json=True)
        if got is None:
            return None
        rows, pivot = got
        frame = geotiles.centre(gx, gy)
        path, n = vegetationmod.write_tile(self.out_dir, self.vfs_dir, None, gx, gy, rows, flat=flat,
                                           geo={"key": geotiles.key(gx, gy), "frame": frame, "pivot": pivot})
        return path, n, frame, pivot

    def country_at(self, lat, lon):
        """The ISO country code at (lat, lon) (its nearest airport's: navgraph.country_near), or None."""
        try:
            return navgraphmod.country_near(self.airports_db, float(lat), float(lon))
        except (TypeError, ValueError):
            return None

    def left_hand(self, anchor):
        """Whether traffic drives on the left at the anchor (its country's road rules, navgraph.road_rules)."""
        lh = getattr(anchor, "_left_hand", None)
        if lh is None:
            lh = navgraphmod.road_rules(navgraphmod.country_near(self.airports_db, anchor.lat, anchor.lon))["rightHandDrive"]
            anchor._left_hand = lh
        return lh

    def buried(self, anchor):
        """The share of low-voltage lines underground in the anchor's country (furniture.buried_share: fewer utility
        poles)."""
        v = getattr(anchor, "_buried", None)
        if v is None:
            v = furnituremod.buried_share(navgraphmod.country_near(self.airports_db, anchor.lat, anchor.lon))
            anchor._buried = v
        return v

    def country(self, anchor):
        """The anchor's country (ISO alpha-2, its nearest airport's: navgraph.country_near), or None: the road
        markings' style (markings.style)."""
        v = getattr(anchor, "_country", "")
        if v == "":
            v = navgraphmod.country_near(self.airports_db, anchor.lat, anchor.lon) if self.airports_db else None
            anchor._country = v
        return v

    def lamp_region(self, anchor):
        """The anchor's street lamp rules (furniture.lamp_region: their spacing)."""
        v = getattr(anchor, "_lamp_region", "")
        if v == "":
            v = furnituremod.lamp_region(navgraphmod.country_near(self.airports_db, anchor.lat, anchor.lon),
                                         navgraphmod.region_near(self.airports_db, anchor.lat, anchor.lon))
            anchor._lamp_region = v
        return v

    def mph(self, anchor):
        """Whether speed limits are posted in mph at the anchor (its country, navgraph.road_rules)."""
        v = getattr(anchor, "_mph", None)
        if v is None:
            v = navgraphmod.road_rules(navgraphmod.country_near(self.airports_db, anchor.lat, anchor.lon))["mph"]
            anchor._mph = v
        return v

    def build_navgraph(self, aid, x, y, serial):
        """navgraph.py: the AI road graph around (x, y) of anchor aid, written to the session folder (the
        anchor's earlier graphs deleted): (vfs path, nodes, edges) or None. Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None:
            return None
        if self.road_pool is not None:      # in a road process, with its bridges (blockworker.navgraph)
            t0 = time.perf_counter()
            cla, clo, _ = anchor.frame.enu_to_geodetic(x, y, 0.0)
            rules = navgraphmod.road_rules(navgraphmod.country_near(self.airports_db, float(cla), float(clo)))
            path, n_nodes, n_edges = self.road_pool.submit(
                blockworker.navgraph, aid, anchor.lat, anchor.lon, anchor.zooms[0], x, y, self.out_dir,
                self.vfs_dir, serial, self.left_hand(anchor), self.mph(anchor), rules).result()
            log.debug("navgraph %d of anchor %d: %d nodes in %.2f s (queue + build)", serial, aid, n_nodes,
                     time.perf_counter() - t0)
            keep = os.path.join(self.out_dir, os.path.basename(path))
            for old in glob.glob(os.path.join(self.out_dir, f"navgraph_{aid}_*.json")):    # one file at a time
                if old != keep:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            return path, n_nodes, n_edges
        t0 = time.perf_counter()
        w = navgraphmod.WINDOW_M + 200.0
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([x - w, x + w]), np.array([y - w, y + w]), 0.0)
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        feats = [self.mapdata.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]
        zoom = anchor.zooms[0]
        urban = self.surface.urban if self.surface is not None else None
        g = self.grader_for(anchor)

        def elev(la_, lo_):         # the physics ground, lifted under roads (as the road tiles')
            z = self.ground(anchor.frame, la_, lo_, self.dem.sample(la_, lo_, zoom))
            return g.apply(la_, lo_, z, gradingmod.ROAD_LIFT) if g is not None else z
        decks = {}
        try:            # the bridges' deck heights (the road tiles' BridgeProfiles, one per anchor)
            with self.grader_lock:
                if anchor.bridges is None:
                    anchor.bridges = self.chain_view(anchor, "bridges", elev)
            for fid, _spec, xy, z, _lon in anchor.bridges.parts(feats, (x - w, y - w, x + w, y + w)):
                decks.setdefault(fid, []).append((np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)))
        except Exception as e:          # no decks: the graph follows the ground under bridges
            log.debug("navgraph decks: %s: %s", type(e).__name__, e)
        graph = navgraphmod.build(anchor.frame, feats, elev, (x, y), urban, decks=decks, widths=self.road_widths,
                                  left_hand=self.left_hand(anchor), mph=self.mph(anchor),
                                  signals=furnituremod.signals_for(self.mapdata, anchor.frame, keys))
        cla, clo, _ = anchor.frame.enu_to_geodetic(x, y, 0.0)
        graph["rules"] = navgraphmod.road_rules(navgraphmod.country_near(self.airports_db, float(cla), float(clo)))
        for old in glob.glob(os.path.join(self.out_dir, f"navgraph_{aid}_*.json")):    # one file at a time
            try:
                os.remove(old)
            except OSError:
                pass
        path = navgraphmod.write(self.out_dir, self.vfs_dir, aid, serial, graph)
        log.debug("navgraph %d of anchor %d: %d nodes in %.2f s", serial, aid, len(graph["nodes"]),
                 time.perf_counter() - t0)
        return path, len(graph["nodes"]), len(graph["edges"])

    boost_on = False
    boost_cpus = server_cpus = None

    def load_boost(self, on, why=""):
        """The loading boost (cpupin.boosted): the server's processes also on half of the game's cores while the
        player waits after a teleport, back on their own when it moves or the far field is done."""
        if on and not self.boost_cpus:
            log.debug("load boost (%s): no cores to lend (--pin / --load-boost off, or too few cores)", why)
        on = bool(on) and bool(self.boost_cpus)
        if on == self.boost_on:
            return
        cpus = self.boost_cpus if on else self.server_cpus
        if not cpus:
            return
        try:
            n = cpupin.pin_tree(cpus)
        except OSError as e:
            log.warning("load boost: %s", e)
            return
        self.boost_on = on
        log.info("load boost %s (%s): %d threads on CPUs %s", "on" if on else "off", why, n,
                 ",".join(map(str, sorted(cpus))))

    def build_far_bld_tile(self, aid, tx, ty):
        """buildings.far_tile: a far (render-only) building tile of anchor aid, or None. Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or not self.buildings:
            return None
        zoom = anchor.zooms[0]
        if self.far_bld_pool is not None:
            t0 = time.perf_counter()
            fut = self.far_bld_pool.submit(blockworker.far_buildings_tile, aid, anchor.lat, anchor.lon, zoom, tx, ty,
                                           self.out_dir, self.vfs_dir, self.lamp_region(anchor))
            res = fut.result()
            log.debug("far buildings tile %d_%d: %.2f s (queue + build)", tx, ty, time.perf_counter() - t0)
            return res
        return buildingsmod.far_tile(anchor.frame, tx, ty, self.mapdata,
                                     lambda la, lo: self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom)),
                                     self.out_dir, self.vfs_dir, aid, self.lamp_region(anchor))

    def build_signs_tile(self, aid, tx, ty):
        """signstream.build_tile: the highway signs of road tile (tx, ty) of anchor aid (its roads are with the mod
        already), or None. Worker threads (signstream.Tracker)."""
        anchor = self.anchors.get(aid)
        source = getattr(self.mapdata, "highway_signs", None)
        if anchor is None or source is None:
            return None
        source.version()            # (the sign data's build, learnt here off the event loop: Connection.on_pose)
        if self.sign_pool is not None:
            return self.sign_pool.submit(blockworker.signs_tile, aid, anchor.lat, anchor.lon, anchor.zooms[0], tx, ty,
                                         self.out_dir, self.vfs_dir, self.left_hand(anchor),
                                         self.mph(anchor), self.country(anchor)).result(timeout=ROAD_TILE_TIMEOUT_S)
        g = self.grader_for(anchor)
        zoom = anchor.zooms[0]

        def elev(la, lo):
            z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        urban = self.surface.urban if self.surface is not None and hasattr(self.surface, "urban") else None
        return signstream.build_tile(self.mapdata, anchor.frame, aid, tx, ty, elev, self.out_dir, self.vfs_dir,
                                     urban, self.road_widths, self.left_hand(anchor), self.mph(anchor),
                                     self.country(anchor))

    def build_sky_tile(self, aid, ux, uy):
        """buildings.skyline_tile: a skyline (render-only) tile of anchor aid, or None (its cells not at hand: asked
        again later). Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or not self.buildings:
            return None
        zoom = anchor.zooms[0]
        if self.far_bld_pool is not None:
            return self.far_bld_pool.submit(blockworker.skyline_tile, aid, anchor.lat, anchor.lon, zoom, ux, uy,
                                            self.out_dir, self.vfs_dir, self.lamp_region(anchor)).result()
        return buildingsmod.skyline_tile(anchor.frame, ux, uy, self.mapdata,
                                         lambda la, lo: self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom)),
                                         self.out_dir, self.vfs_dir, aid, self.lamp_region(anchor))

    def build_far_bridge_tile(self, aid, tx, ty):
        """buildings.far_bridge_tile: a far bridge (render-only) tile of anchor aid, or None (its road cells not at
        hand, or no worker processes: none then). Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or self.far_bld_pool is None:
            return None
        return self.far_bld_pool.submit(blockworker.far_bridge_tile, aid, anchor.lat, anchor.lon, anchor.zooms[0],
                                        tx, ty, self.out_dir, self.vfs_dir).result()

    def build_terrain_patch(self, aid, key, box):
        """terrainpatch.py: the collision patch of the ground under a collision island off the terrain block (the
        mod's terrainpatch.lua), written to the session folder in pieces. ([(vfs path, (cx, cy))], triangles) or None
        if the anchor is gone. Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None:
            return None
        box = terrainpatchmod.snap_box(*box)
        path = os.path.join(self.out_dir, f"patch_{aid}")      # (+ _<cell>.cdae a piece: shared by patches)
        if self.road_pool is not None:
            parts, n = self.road_pool.submit(blockworker.terrain_patch, aid, anchor.lat, anchor.lon,
                                             anchor.zooms[0], box, path).result()
        else:
            zoom, g = anchor.zooms[0], self.grader_for(anchor)

            def elev(la, lo):
                z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))
                return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
            parts, n = terrainpatchmod.build(anchor.frame, elev, box, path)
        return [(f"{self.vfs_dir}/{os.path.basename(p)}", c) for p, c in parts], n

    WARM_ORDER = {"roads": 0, "infrastructure": 1, "buildings": 2, "building_parts": 3, "water": 4}
    WARM_PLACE_LAYERS = ("roads", "infrastructure")

    def warm_place(self, anchor, x, y):
        """The roads and bridges (WARM_PLACE_LAYERS) of the road tiles the mod will want around (x, y) (roads.SEND_M),
        fetched at once, nearest first: the first block's grading and the road tiles wait for them, and on a fresh place
        they came one after another as each part of the work asked (GitHub #241). Returns how many were queued."""
        if self.mapdata is None:
            return 0
        water = 1 + seacarvemod.LINE_CELLS if self.sea is not None else 0
        needs = set()
        for tx, ty in roadsmod.tiles_near(x, y, roadsmod.SEND_M):
            needs.update(roadsmod.tile_needs(anchor.frame, tx, ty, self.buildings, self.road_widths is not None, water))

        def where(k):
            la, lo = k.center()
            e, n, _ = anchor.frame.geodetic_to_enu(la, lo, 0.0)
            return math.hypot(float(e) - x, float(n) - y)
        # (only what the first block's grading and the bridges wait for: each road tile asks for its own buildings and
        # water when it starts; all of it at once kept a Steam Deck's 4 cores from the road tiles themselves)
        order = sorted((kl for kl in needs if kl[1] in self.WARM_PLACE_LAYERS),
                       key=lambda kl: (self.WARM_ORDER.get(kl[1], 9), where(kl[0]), str(kl[0]), kl[1]))
        n = self.mapdata.warm(order)
        import roadz        # (the measured road heights of the same cells, kept on disk for the road processes)
        if roadz.PROVIDER is not None:
            roadz.PROVIDER.warm(sorted({kl[0] for kl in order if kl[1] == "roads"}, key=lambda k: (where(k), str(k))))
        return n

    def roads_geo_ok(self):
        """Road tiles as geotiles (--geo-tiles on, road tiles in worker processes): content kept across rebases."""
        return bool(self.geo_tiles) and self.road_pool is not None and self.mapdata is not None

    def geo_rules(self, gx, gy, z=None):
        """(left_hand, mph, buried share, lamp region, country, DEM zoom) at geotile (gx, gy)'s centre (zoom z, the
        road geotiles' by default): its content's own place's rules (an anchor's were the anchor's), kept per tile."""
        import geotiles
        z = geotiles.Z if z is None else int(z)
        cache = self.__dict__.setdefault("_geo_rules", {})
        got = cache.get((gx, gy, z))
        if got is None:
            la, lo = geotiles.centre(gx, gy, z)
            c = navgraphmod.country_near(self.airports_db, la, lo)
            got = (bool(navgraphmod.road_rules(c)["rightHandDrive"]), bool(navgraphmod.road_rules(c)["mph"]),
                   furnituremod.buried_share(c),
                   furnituremod.lamp_region(c, navgraphmod.region_near(self.airports_db, la, lo)),
                   c if self.airports_db else None, demmod.zoom_for(la, 1.25, demmod.FINE_ZOOM))
            if len(cache) > 20000:
                cache.clear()
            cache[(gx, gy, z)] = got
        return got

    def far_geo_ok(self):
        """Far buildings, the skyline and far bridges as geotiles (--geo-tiles on, their worker process): built once
        per tile, kept by the mod across rebases."""
        return bool(self.geo_tiles) and self.far_bld_pool is not None and self.mapdata is not None and self.buildings

    FAR_GEO_KEPT = 20000        # far geotiles of this session remembered (build_far_geo: sent again from the files)

    def build_far_geo(self, kind, gx, gy):
        """blockworker.far_geo in the far buildings' process: far geotile (gx, gy) of kind ("bldfar", "bldsky",
        "brfar"), anchor-free (its own place's rules and DEM zoom); one built before in this session comes from its
        files. (vfs path, n, frame, pivot, shapes) or None (its cells not at hand yet). Worker threads
        (vegetation.Tracker geo)."""
        if self.far_bld_pool is None or self.mapdata is None:
            return None
        import geotiles
        z = blockworker.far_geo_zoom(kind)
        memo = self.__dict__.setdefault("_far_geo_done", {})
        got = memo.get((kind, gx, gy))
        if got is not None and all(os.path.exists(os.path.join(self.out_dir, n))
                                   for n in [os.path.basename(got[0])] + list(got[4].values())):
            return got
        _lh, _mph, _buried, lamps, _country, gzoom = self.geo_rules(gx, gy, z)
        t0 = time.perf_counter()
        got = self.far_bld_pool.submit(blockworker.far_geo, kind, gx, gy, gzoom, self.out_dir, self.vfs_dir,
                                       lamps).result()
        log.debug("far geotile %s %s: %.2f s (queue + build)", kind, geotiles.key(gx, gy, z), time.perf_counter() - t0)
        if got is not None:
            if len(memo) > self.FAR_GEO_KEPT:
                memo.clear()
            memo[(kind, gx, gy)] = got
        return got


    def build_roads_geo(self, aid, gx, gy, content=True, skin=True, exact=None):
        """blockworker.roads_geo in a road process: geotile (gx, gy)'s content (once: kept by the mod across
        rebases), anchor aid's skin and exact variants. Its gas station pads kept for the ground's painters. None
        when the anchor is gone. Worker threads (roads.GeoTracker)."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or self.road_pool is None:
            return None
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        fut = self.road_pool.submit(
            blockworker.roads_geo, aid, anchor.lat, anchor.lon, anchor.zooms[0], gx, gy, gzoom, self.out_dir,
            self.vfs_dir, self.sidewalks, DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
            self.buildings, lh, mph, self.anchor_q(anchor) if skin else None, self.gas_stations, buried, lamps,
            country, content, skin, exact)
        try:
            res = fut.result(timeout=ROAD_TILE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            log.warning("road geotile %d_%d: no answer from its worker in %.0f s: built again", gx, gy,
                        ROAD_TILE_TIMEOUT_S)
            if hasattr(self.road_pool, "recycle"):
                self.road_pool.recycle(f"road geotile {gx}_{gy} hung for {ROAD_TILE_TIMEOUT_S:.0f} s")
            raise
        if res is not None and res.get("content") is not None:
            self.keep_fuel_pads(res["content"][3])
        return res

    def geo_content_key(self, gx, gy):
        """The revisit key geotile (gx, gy)'s road content has with this server's settings (blockworker._geo_content_key,
        its own place's rules), or None (no map data)."""
        if self.mapdata is None:
            return None
        import geotiles
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        return blockworker._geo_content_key(geotiles.geobox(gx, gy).key, gzoom, self.buildings, lh, mph,
                                            self.gas_stations, buried, lamps, country, md=self.mapdata.versions())

    def geo_prebuilt(self, gx, gy):
        """Is geotile (gx, gy)'s road content in the revisit cache (no worker, no network)? -> its key or None."""
        k = self.geo_content_key(gx, gy)
        return k if k is not None and os.path.exists(revisitmod._path(self.cache_dir, "roadsg", k)) else None

    def prebuild_geo(self, gx, gy):
        """Saved areas' world phase: geotile (gx, gy)'s road content built into the revisit cache in a road process
        (blockworker.prebuild_geo), the game sent nothing. -> its revisit key, or None (not complete: tried later)."""
        if self.mapdata is None or self.road_pool is None or not revisitmod.ON:
            return None
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        scratch = os.path.join(self.cache_dir, "areas", "scratch")
        return self.road_pool.submit(blockworker.prebuild_geo, gx, gy, gzoom, scratch, self.buildings, lh, mph,
                                     self.gas_stations, buried, lamps, country).result(timeout=ROAD_TILE_TIMEOUT_S)

    def build_roads_tile(self, aid, tx, ty):
        """roads.py: one road tile of anchor aid, written to the session folder. Returns (vfs path,
        chunks) or None if the anchor is gone. Worker threads (fetches the map cells it lacks)."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None:
            return None
        if self.road_pool is not None:
            t0 = time.perf_counter()
            fut = self.road_pool.submit(
                blockworker.roads_tile, aid, anchor.lat, anchor.lon, anchor.zooms[0], tx, ty, self.out_dir,
                self.vfs_dir, self.sidewalks, DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
                self.buildings, self.left_hand(anchor), self.mph(anchor), self.anchor_q(anchor),
                self.gas_stations, self.buried(anchor), self.lamp_region(anchor), self.country(anchor))
            try:
                # bounded: a worker that never answered held this thread and its roads.Tracker slot for good (4
                # slots: four hung tiles and no road tile ever again, the loading line at "roads & buildings 0/26")
                res = fut.result(timeout=ROAD_TILE_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                log.warning("road tile %d_%d of anchor %d: no answer from its worker in %.0f s: built again",
                            tx, ty, aid, ROAD_TILE_TIMEOUT_S)
                if hasattr(self.road_pool, "recycle"):
                    self.road_pool.recycle(f"road tile {tx}_{ty} hung for {ROAD_TILE_TIMEOUT_S:.0f} s")
                raise
            log.debug("road tile %d_%d: %.2f s (queue + build)", tx, ty, time.perf_counter() - t0)
            self.keep_fuel_pads(res[2])
            return res[:2]
        self.mapdata.warm(roadsmod.tile_needs(anchor.frame, tx, ty, self.buildings, self.road_widths is not None,
                                              1 + seacarvemod.LINE_CELLS if self.sea is not None else 0))
        cells = [self.mapdata.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                 for k in roadsmod.cells_of_tile(anchor.frame, tx, ty)]
        zoom = anchor.zooms[0]
        urban = self.surface.urban if self.surface is not None and hasattr(self.surface, "urban") else None
        g = self.grader_for(anchor)

        def elev(la, lo):           # the physics ground (lifted under roads: grading.ROAD_LIFT)
            z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        walks = []
        with self.grader_lock:
            if anchor.bridges is None:
                anchor.bridges = self.chain_view(anchor, "bridges", elev)
            if anchor.tunnels is None:
                anchor.tunnels = self.chain_view(anchor, "tunnels", elev, g)
        fuel, skip, fuel_cuts, fuel_pads = [], frozenset(), [], []
        if self.gas_stations:       # pumps and refuel areas; their canopy outlines left out of the buildings
            fuel, skip = gasstationsmod.tile_sites(anchor.frame, tx, ty, self.mapdata, elev, cuts=fuel_cuts,
                                                   pads=fuel_pads)
            self.keep_fuel_pads(fuel_pads)
        props = [] if self.buildings else None
        lines = [] if self.buildings else None
        wires = [] if self.buildings else None
        park = [] if self.buildings else None
        items = town = None
        if self.buildings:
            # (on the graded ground, as blockworker.roads_tile)
            items = buildingsmod.road_tile_items(anchor.frame, tx, ty, self.mapdata, elev,
                                                 decks=blockworker._tile_decks(anchor.bridges, cells, tx, ty), skip=skip)
            town = buildingsmod.coverage(items) < buildingsmod.POLES_COVERAGE
        rwalls = []
        ctx = []
        box = roadsmod.tile_bbox(tx, ty)
        cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")  # noqa: E731

        def drawn(la, lo):      # the block's ground: raised over the low arches as the terrain is (_make_heightmap_here)
            return roadsmod.block_arch_raise(anchor.tunnels, cell_fn, anchor.frame, la, lo, elev(la, lo),
                                             (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
        cover = []                      # the ground carriageways for the decks' parapets (roadmesh.deck ground=)
        marks = []                  # stall lines along parking aisles (parking.finish_markings adds the lot polygons')
        near_roads = []             # the ground carriageways near the tile (the median barriers': medians.tile_rows)
        chunks = roadsmod.build_tile(anchor.frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=anchor.bridges,
                                     walk_cuts=fuel_cuts,    # (the stations' made-up driveways: no sidewalk across them)
                                     widths=self.road_widths, props=props, lines=lines, wires=wires, town=town, park=park,
                                     left_hand=self.left_hand(anchor), mph=self.mph(anchor), buried=self.buried(anchor),
                                     lamp_region=self.lamp_region(anchor), country=self.country(anchor),
                                     structures=buildingsmod.centres(items, tx, ty) if items else (),
                                     tunnels=anchor.tunnels, walls=rwalls,
                                     ground=lambda x, y: roadmeshmod.drawn_ground(anchor.frame, drawn, np.c_[x, y],
                                                                                  self.anchor_q(anchor)),
                                     terrain_paint=self.surface is not None, cover_out=cover, marks=marks,
                                     roads_out=near_roads,
                                     signals=furnituremod.signals_for(
                                         self.mapdata, anchor.frame,
                                         roadsmod.cells_of_tile(anchor.frame, tx, ty, blockworker.SIGNALS_MARGIN_M)))
        chunks += railsmod.tile_chunks(anchor.frame, tx, ty, cells)     # railway and tram tracks on the ground
        if self.airports is not None:   # runway markings, drawn by the road mesh like the lane lines
            chunks += self.airports.tile_chunks(anchor.frame, tx, ty, elev, roadsmod.tile_bbox)
        x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
        if items is not None:           # front doors and shopfronts, driveways into the road mesh
            drives = []
            props.extend(buildingsmod.doors(items, lines, x0, y0, drives))
            props.extend(buildingsmod.toll_booth_rows(items, x0, y0))
            drives.extend(buildingsmod.garage_drives(items, lines, x0, y0))
            chunks += drives
            park = parkingmod.clear_of(park, props, drives)
            park = parkingmod.tile_lot_spots(anchor.frame, tx, ty, self.mapdata, elev, items, park)
            park = gasstationsmod.clear_park(park, fuel)
            buildingsmod.brick_walks(walks, items, anchor.lat, anchor.lon)
        chunks += parkingmod.finish_markings(anchor.frame, tx, ty, self.mapdata, elev, items, marks)

        # Share the bridge mesh's exact parapet geometry with the tile builder.
        # Mapped rails on a deck are replaced by guardrails.bridge_rows(); the parapet runs also feed the fallback matching.
        pads_near = []
        if anchor.bridges is not None:  # (the junction pads round the tile: roads.deck_pads)
            try:
                pad_parts = list(anchor.bridges.parts(cells, (box[0] - 40, box[1] - 40, box[2] + 40, box[3] + 40)))
                pads_near = roadsmod.deck_pads([(f, sp, xy, z) for f, sp, xy, z, _lon in pad_parts], near_roads, box,
                                               anchor.bridges._ground, own=False)
            except Exception as ex:
                log.warning("deck pads: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        pads = [p for p in pads_near if roadsmod.box_owns(box, float(p[1][0]), float(p[1][1]))]
        parapets = roadmeshmod.parapet_runs(chunks, ctx, ground=cover, pads=pads_near)
        import guardrails as guardrailsmod
        infra_fn = lambda key: self.mapdata.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        low_parapet_decks = set()
        deck_parts = []                 # the bridges' parts near the tile (the median barriers' on joined decks too)
        try:
            deck_parts = list(anchor.bridges.parts(cells, (box[0]-10, box[1]-10, box[2]+10, box[3]+10)))
            guardrail_decks = [{"n": np.column_stack((xy, z)), "w": spec.width}
                               for _fid, spec, xy, z, _lon in deck_parts]
            guardrail_rows = guardrailsmod.forest_rows(anchor.frame, infra_fn, box, elev,
                                                       decks=guardrail_decks, parapets=parapets,
                                                       bridge_rails=roadmeshmod.BRIDGE_RAILS,
                                                       supported_decks_out=low_parapet_decks)
        except Exception as ex:
            log.warning("mapped guardrails: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            # A failed mapped-data pass cannot establish that a bridge needs rails.
            low_parapet_decks.clear()
            guardrail_rows = []
        guardrail_rows = guardrail_rows + blockworker._median_rows(
            anchor.frame, near_roads, box, elev, self.left_hand(anchor), self.country(anchor), guardrail_rows, tx, ty,
            deck_parts)
        mesh = roadmeshmod.build(anchor.frame, chunks, elev, (x0, y0, 0.0),
                                 DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
                                 clip=box, follow=True, q=self.anchor_q(anchor), drawn=drawn)
        mesh = blockworker._with_plazas(mesh, anchor.frame, roadsmod.tile_bbox(tx, ty), elev, (x0, y0, 0.0),
                                        self.surface)
        dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0),
                                 anchor.bridges.piers_in(roadsmod.tile_bbox(tx, ty)) if anchor.bridges is not None else (),
                                 ctx,
                                 anchor.bridges.beams_in(roadsmod.tile_bbox(tx, ty)) if anchor.bridges is not None else (),
                                 ground=cover, parapets=parapets,
                                 low_parapet_decks=low_parapet_decks, pads=pads)
        tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
        import railways
        rmesh = railways.tile_mesh(anchor.frame, tx, ty,
                                   [cell_fn(k) for k in roadsmod.cells_of_tile(anchor.frame, tx, ty, railways.REACH_M)],
                                   railways.point_ground(anchor.frame, elev, (aid, anchor.lat, anchor.lon)),
                                   far_ground=railways.point_ground(
                                       anchor.frame,
                                       lambda la, lo: self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom)),
                                       ("raw", aid, anchor.lat, anchor.lon)))
        bld = None
        garage_sound = ()
        oil_wells = []
        if self.buildings:
            import oilwells
            bld = buildingsmod.road_tile(anchor.frame, tx, ty, self.mapdata, elev, None, None, items)
            garage_sound = buildingsmod.garage_sound_boxes(items, *roadsmod.tile_bbox(tx, ty)[:2],
                                                           roadsmod.SOUND_GARAGE)
            oil_wells = oilwells.tile_wells(anchor.frame, tx, ty, elev)     # (the mod's pump jacks)
            import powerlines
            powerlines.tile_lines(anchor.frame, tx, ty, self.mapdata, elev, props, wires)    # high-voltage lines
            import industry
            garage_sound = (garage_sound + industry.refinery_sound_rows(anchor.frame, tx, ty, self.mapdata, elev)
                            + industry.power_plant_sound_rows(anchor.frame, tx, ty, self.mapdata, elev))
        return roadsmod.write_tile(self.out_dir, self.vfs_dir, aid, tx, ty, chunks, walks if self.sidewalks else None,
                                   mesh, dmesh, bld, props, wires, park, tunnel_mesh=tmesh, fuel=fuel,
                                   rail_mesh=rmesh, guardrail_rows=guardrail_rows, garage_sound=garage_sound, wells=oil_wells)

    def set_gas_stations(self, on):
        """The settings page's "Gas stations" switch (the mod's gas_stations; the user, 2026-10-23: off saves their
        cost on a small machine): stations laid out, pads painted and the region's shapes named only while on, never
        on without --gas-stations on. Returns whether it changed (the road tiles are then sent again)."""
        on = bool(on) and self.gas_allowed
        if on == self.gas_stations:
            return False
        self.gas_stations = on
        if self.surface is not None and hasattr(self.surface, "gas_pads"):
            self.surface.gas_pads = on
            self.surface.clear_fuel_pads()
        self.pads_stale()               # (the blocks' photos again: with or without their forecourts)
        log.info("gas stations %s (settings)", "on" if on else "off")
        return True

    def live_busy(self):
        """The live world has work in flight: a worker pool's jobs (blocks, far tiles, road tiles, far buildings), or
        a player's loading line not full. The saved areas' downloads (areas.py) wait meanwhile."""
        pools = [self.road_pool, self.far_bld_pool, getattr(self.far_pool, "local", self.far_pool)]
        pools += [bp for bp, _ in self.block_pools]
        if any(getattr(p, "inflight", 0) for p in pools if p is not None):
            return True
        for c in list(self.connections):
            counts = getattr(c, "progress_sent", None)
            if counts and any(d < w for d, w in counts):
                return True
        return False

    def driving(self):
        """A player is moving (faster than a walk): the saved areas' downloads run at areas.DUTY_DRIVE."""
        return any(math.hypot(*getattr(c, "vel", (0.0, 0.0))) > 2.0 for c in list(self.connections))

    def areas_message(self, result=None):
        """The 'areas' message (areas.Areas.message; result: the answer to an area_add)."""
        m = {"type": "areas", **self.areas.message()}
        if result is not None:
            m["result"] = result
        return m

    def world_signature(self):
        """The settings the revisit cache's items are built with ({name: value}; the parts of the revisit keys that
        are the player's settings or the data's versions, not the place): a sidecar row (revisit.sig, written where
        the item is kept: blockworker, plancache) is "current" when its code_key is this server's and each setting
        it names has the value given here (the map's cache view, covermap.py). One function: a new setting that
        changes what is built goes into its revisit key, its store site's revisit.sig and here, under the same name.
        Per-anchor parts (the height quantum, country, lamp rules, driving side) are the place's, not settings."""
        im = self.imagery
        md = self.mapdata.versions() if self.mapdata is not None and hasattr(self.mapdata, "versions") else None
        return {
            # road tiles (blockworker._roads_tile_cached)
            "sidewalks": bool(self.sidewalks),
            "lift": float(DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0),
            "buildings": bool(self.buildings),
            "gas_stations": bool(self.gas_stations),
            # every kind: the map data's versions (Overture release, layer versions, the weekly rail / lane builds)
            "md": md,
            # far tiles' geometry and texture (blockworker._far_key, far_tile): the layout (quality: cells, texture px)
            "layout": repr(self.layout),
            "chain": tuple(s.name for s in im.chain) if im is not None else None,
            "tone": self.tone,
            "leaf_on": getattr(im, "leaf_on", None),
            "season_mode": getattr(im, "season_mode", None),
            # road plans (plancache.for_grader's signature)
            "plans": f"w{int(self.road_widths is not None)}s{int(self.sea is not None)}",
        }

    def set_base_px(self, px):
        """The ground photo's size (px) the mod's terrain texture set has (its graphics setting groundPhoto, applied at
        a level's start: the 'graphics' message's photoPx); one of BASE_SIZES, else nothing changes. The blocks built
        at another size get their photo again (base_stale: base_refresh sends it), the next ones are made at it.
        Returns whether it changed."""
        try:
            px = int(px)
        except (TypeError, ValueError):
            return False
        if px not in BASE_SIZES or px == self.base_px:
            return False
        self.base_px = px
        if self.near_visual == "detail":
            self.base_stale.update(k for k in self.hm if k[0] in self.anchors)
        log.info("ground photo %d px (settings)", px)
        return True

    def keep_fuel_pads(self, pads):
        """A road tile's gas station pads kept for the ground's painters (surface.Surface.add_fuel_pads); the blocks
        a new station's pad lies on get their photo rendered again (pads_stale)."""
        if pads and self.surface is not None and hasattr(self.surface, "add_fuel_pads"):
            new = self.surface.add_fuel_pads(pads)
            if new:
                self.pads_stale(new)

    def pads_stale(self, items=None):
        """The built blocks whose photo was painted without these pads [(id, rings)] (every built block: None)
        marked stale, so base_refresh renders it again and sends it (block_base): the ground under the car is built
        before its road tiles bring their stations' pads (a teleport to a station showed its forecourt bare: the
        user, 2026-10-24, "No asphalt at the station"); in detail mode (the block photo) only. The same blocks are
        marked for building again (pads_rebuild: their surface layers, the asphalt and no grass, come with the block;
        Connection.pads_refresh). A block still being built (hm_stage) is marked too: its build took the pads of its
        start (after a teleport, a station's pad came 0.1 s before the block under the car was done and its lot stayed
        dirt: the user, 2026-10-27, "the gasstation lot never gets painted")."""
        self.pads_changed_at = time.monotonic()
        h = self.layout.block * heightmap.SQUARE / 2
        for key in list(self.hm) + list(self.hm_stage):
            anchor = self.anchors.get(key[0])
            if anchor is None:
                continue
            _, cx, cy = key
            if items is None or any(self._ring_meets(anchor, ring, cx - h, cy - h, cx + h, cy + h)
                                    for _, rings in items for ring in rings):
                self.pads_rebuild.add(key)
                if self.near_visual == "detail":
                    self.base_stale.add(key)

    @staticmethod
    def _ring_meets(anchor, ring, x0, y0, x1, y1):
        la, lo = ring
        e, n, _ = anchor.frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(len(la)))
        e, n = np.asarray(e), np.asarray(n)
        return bool(e.max() >= x0 and e.min() <= x1 and n.max() >= y0 and n.min() <= y1)

    def block_fuel_pads(self, anchor, cx, cy, size):
        """The kept gas station pads around a block (its square and a margin) for its process's job: [(id, rings)]."""
        if self.surface is None or not getattr(self.surface, "gas_pads", False):
            return []
        h = size / 2 + 64.0
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([cx - h, cx + h]), np.array([cy - h, cy + h]), np.zeros(2))
        return self.surface.fuel_pad_items(np.asarray(la), np.asarray(lo))

    def ground_z_at(self, anchor, e, n):
        """The ground's height (the frame's up) at (e, n) of anchor's frame, from the elevation data (not graded: the
        mod sets the car down on the collision there)."""
        la, lo, _ = anchor.frame.enu_to_geodetic(e, n, 0.0)
        la, lo = float(la), float(lo)
        z = self.ground(anchor.frame, la, lo, float(self.dem.sample(la, lo, anchor.hm_zoom)))
        return round(float(anchor.frame.geodetic_to_enu(la, lo, z)[2]), 3)

    def ground(self, frame, la, lo, z):
        """blockworker.ground here: runways graded in, the sea floor carved."""
        if self.airports is not None:
            z = self.airports.apply(la, lo, z)
        if self.sea is not None:
            z = self.sea.carve(la, lo, z)
        return z

    def grader_for(self, anchor):
        """The anchor's grading.Grader (--grading on), else None."""
        if not self.grading:
            return None
        with self.grader_lock:
            if anchor.grader is None:
                # (every anchor's view of one store: the plans are the planning regions', whatever the anchor)
                if getattr(self, "plan_store", None) is None:
                    self.plan_store = gradingmod.PlanStore(
                        self.mapdata, lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM), self.road_widths)
                anchor.grader = gradingmod.Grader(anchor.frame, self.mapdata, self.plan_store.elev, self.road_widths,
                                                  store=self.plan_store)
                anchor.grader.wet = self.sea.depth if self.sea is not None else None    # (no dams: underwater tunnels)
                anchor.grader.dem = self.dem                # (its urgent lane: _plan_missing)
                anchor.grader.shared = plancachemod.for_grader(self.cache_dir, anchor.grader, self.road_widths,
                                                              anchor.grader.wet)
            return anchor.grader

    def warm_roads(self, anchor, x, y, vx, vy):
        """Queue the road plans of the cells the physics block will cover along the player's straight path
        (WARM_STEPS_S; the block's half width + the grading pad), then those within WARM_AROUND."""
        g = self.grader_for(anchor)
        if g is None:
            return
        reach = self.layout.block * heightmap.SQUARE / 2 + gradingmod.T_MAX + 40.0
        keys = []
        for t, r in [(t, reach) for t in WARM_STEPS_S] + [(0.0, WARM_AROUND)]:
            la, lo, _ = anchor.frame.enu_to_geodetic(x + vx * t, y + vy * t, 0.0)
            la, lo = float(la), float(lo)
            dlat = r / 111320.0
            dlon = r / (111320.0 * max(0.05, math.cos(math.radians(la))))
            for k in mapcells.cells_for_bbox(la - dlat, lo - dlon, la + dlat, lo + dlon):
                if k not in keys:
                    keys.append(k)
        for _, q in self.block_pools:
            try:
                q.put_nowait((anchor.id, anchor.lat, anchor.lon, keys))
            except Exception as e:      # a full / closed queue only costs the lookahead
                log.debug("road plans ahead: %s", e)
        if self.road_pool is not None:      # road tiles plan in their processes: this one needs none ahead
            return
        with self.warm_cv:
            self.warm_want = [(g, k) for k in keys]
            if self.warm_thread is None:
                self.warm_thread = threading.Thread(target=self._warm_loop, name="roads-ahead", daemon=True)
                self.warm_thread.start()
            self.warm_cv.notify()

    def download_ahead(self, anchor, x, y, vx, vy):
        """Fetch the elevation tiles (the blocks' zoom) and the road tiles' map cells along the player's straight
        path AHEAD_S s ahead into the disk caches (one round at a time, again every AHEAD_MOVE_M)."""
        v = math.hypot(vx, vy)
        if not AHEAD_ON or v < AHEAD_MIN_V or self.ahead_busy or self.dem is None:
            return
        last = self.ahead_last
        if last is not None and last[0] == anchor.id and math.hypot(x - last[1], y - last[2]) < AHEAD_MOVE_M:
            return
        self.ahead_last = (anchor.id, x, y)
        k = min(1.0, AHEAD_MAX_M / (v * AHEAD_S[-1]))
        pts = []
        for t in AHEAD_S:
            la, lo, _ = anchor.frame.enu_to_geodetic(x + vx * t * k, y + vy * t * k, 0.0)
            pts.append((float(la), float(lo)))
        reach = self.layout.block * heightmap.SQUARE / 2 + AHEAD_MARGIN_M
        zoom = anchor.hm_zoom
        layers = (AHEAD_LAYERS if self.buildings else ("roads",)) if self.mapdata is not None else ()
        self.ahead_busy = True

        def run():
            try:
                dem_n = cells_n = 0
                for la, lo in pts:
                    got, deferred = self.dem.prefetch(self.dem.keys_around(la, lo, reach, zoom))
                    dem_n += got
                    if deferred:
                        self.ahead_stats["deferred"] += 1
                        break
                for la, lo in pts if layers else ():
                    dlat = reach / 111320.0
                    dlon = reach / (111320.0 * max(0.05, math.cos(math.radians(la))))
                    for key in mapcells.cells_for_bbox(la - dlat, lo - dlon, la + dlat, lo + dlon):
                        todo = tuple(ly for ly in layers if not self.mapdata.has(key, ly))
                        if todo:
                            self.mapdata.cell(key, layers=todo, kind="prefetch")
                            cells_n += 1
                self.ahead_stats["dem"] += dem_n
                self.ahead_stats["cells"] += cells_n
                if dem_n or cells_n:
                    log.info("download ahead: %d elevation tiles, %d map cells (%.0f m/s; session: %s)", dem_n,
                             cells_n, v, self.ahead_stats)
            except Exception as e:      # a refused / failed fetch only costs the lookahead
                log.debug("download ahead: %s: %s", type(e).__name__, e)
            finally:
                self.ahead_busy = False
        self.ahead_pool.submit(run)

    def warm_imagery(self, anchor, x, y, vx, vy):
        """The lazy imagery sources (NAIP) along the player's straight path (WARM_STEPS_S), over the block's
        footprint and a margin, at the base photo's texel size: fetched before the blocks there are built."""
        if self.imagery is None:
            return
        reach = self.layout.block * heightmap.SQUARE / 2 + IMG_WARM_MARGIN
        pts = [(x + vx * t, y + vy * t) for t in WARM_STEPS_S]
        self.imagery.warm(anchor.frame, pts, reach, self.layout.block * heightmap.SQUARE / BASE_RENDER_PX)

    def _warm_loop(self):
        while True:
            pacer.hold()                # (a teleport's first block first)
            with self.warm_cv:
                job = None
                while job is None:
                    job = next(((g, k) for g, k in self.warm_want if not g.planned(k)), None)
                    if job is None:
                        self.warm_cv.wait()
            try:
                t0 = time.perf_counter()
                job[0].warm(job[1])
                log.debug("road plans ahead: %s in %.2f s (%d wanted)", job[1], time.perf_counter() - t0,
                          len(self.warm_want))
            except Exception:
                log.exception("road plans ahead for %s", job[1])
                with self.warm_cv:      # not again for this pose list
                    self.warm_want = [w for w in self.warm_want if w[1] != job[1]]

    def make_heightmap(self, anchor, cx, cy, progress, lane=LANE_REQUEST, queued_at=None):
        """Worker thread: build and write one 1024² terrain. The follow / prefetch blocks (no progress
        callback) build in the block process when there is one; the anchor's first block here, so its
        DEM download reports progress."""
        t0 = time.perf_counter()
        if queued_at is not None and t0 - queued_at > 0.2:
            log.debug("terrain %s waited %.2f s for a heightmap worker", (anchor.id, cx, cy), t0 - queued_at)
        q = self.anchor_q(anchor)
        fb, base = anchor.first_base, None
        if fb is not None and fb[0] == (cx, cy):        # the provisional first block's photo (make_provisional)
            anchor.first_base, base = None, fb[1]
        elif self.near_visual == "detail" and self.imagery is not None:
            base = (self.urgent_base_pool if lane == LANE_REQUEST else self.base_pool).submit(
                self._base_photo, anchor, cx, cy, self._base_process(lane), lane == LANE_REQUEST)
        pool = self.block_pools[lane][0] if self.block_pools and progress is None else None
        key = (anchor.id, cx, cy)
        try:
            if pool is not None:
                self.hm_stage[key] = "the block process"
                info = pool.submit(
                    blockworker.build, anchor.id, anchor.lat, anchor.lon, anchor.hm_zoom, q, cx, cy,
                    self.layout.block, self.out_dir, self.hm_name(key), self.ter_name(key),
                    fuel_pads=self.block_fuel_pads(anchor, cx, cy, self.layout.block)).result()
                self.hm_stage[key] = "its base photo"
                return self._finish_heightmap(anchor, cx, cy, info, t0, base)
            self.hm_stage[key] = "the build in the server's process"
            urgent = getattr(self.dem, "urgent", None)     # (test DEMs have no lane)
            with urgent() if urgent is not None else contextlib.nullcontext():     # the player waits for it (teleport,
                # resync): ahead of every other download
                return self._make_heightmap_here(anchor, cx, cy, progress, t0, q, base)
        finally:
            self.hm_stage.pop(key, None)

    def make_provisional(self, anchor, cx, cy, progress, ground_at=(0.0, 0.0)):
        """Worker thread: a teleport's first block before its road plans (the user, 2026-10-22: "place the car as
        soon as possible"): the DEM's ground with the runways (no road grading, tunnels, arches or sea floor, which
        all wait on the map data of the cells around: 15-60 s of a fresh place's first block, ~5 s without), the
        cached land cover and the photo. The graded block replaces it when it is built (Connection.refine_first:
        terrain_refresh). Not kept in hm (the graded one is the block of its key), nor in anchor.chunks. ground_at: the
        point its groundZ is for (the car's spot)."""
        t0 = time.perf_counter()
        q = self.anchor_q(anchor)
        base = None
        if self.near_visual == "detail" and self.imagery is not None:
            # (here, not in a block process: the other lane's process can still be busy with the old place's block,
            # and the photo waited 16 s behind it on a Deck, 2026-10-23)
            base = self.urgent_base_pool.submit(self._base_photo, anchor, cx, cy, None, True, True)
            anchor.first_base = ((cx, cy), base)
        urgent = getattr(self.dem, "urgent", None)     # (test DEMs have no lane)
        with urgent() if urgent is not None else contextlib.nullcontext():
            info = self._make_heightmap_here(anchor, cx, cy, progress, t0, q, base, provisional=True,
                                             ground_at=ground_at)
        log.info("provisional first block %s in %.2f s (%s)", (anchor.id, cx, cy), time.perf_counter() - t0,
                 " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()))
        return info

    def _make_heightmap_here(self, anchor, cx, cy, progress, t0, q, base=None, provisional=False, ground_at=(0.0, 0.0)):
        with self.hm_here_lock:
            self.hm_here += 1
        try:
            return self._make_heightmap_here_(anchor, cx, cy, progress, t0, q, base, provisional, ground_at)
        finally:
            with self.hm_here_lock:
                self.hm_here -= 1

    def _make_heightmap_here_(self, anchor, cx, cy, progress, t0, q, base=None, provisional=False, ground_at=(0.0, 0.0)):
        keep = {}
        g = None if provisional else self.grader_for(anchor)
        c0 = time.thread_time()

        grade_s = [0.0, 0.0]            # wall, this thread's CPU (the rest: waiting for map data, the interpreter)

        def elev(la, lo):
            z = self.dem.sample(la, lo, anchor.hm_zoom, progress)
            if provisional:             # the runways only (the sea floor waits on the water cells)
                return self.airports.apply(la, lo, z) if self.airports is not None else z
            z = self.ground(anchor.frame, la, lo, z)
            if g is None:
                return z
            t1, c1 = time.perf_counter(), time.thread_time()
            z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
            grade_s[0] += time.perf_counter() - t1
            grade_s[1] += time.thread_time() - c1
            return z
        gen = elev
        if self.mapdata is not None and not provisional:
            # (made here when not yet: an anchor's first block was built before any road tile made them, without
            # its arches' raise and flat mouths, 2026-10-17)
            tun = self.tunnels_of(anchor, elev, g)
            half = self.layout.block * heightmap.SQUARE / 2
            cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")  # noqa: E731

            def gen(la, lo):        # the ground over the low arches raised over their vaults (roads.arch_raise)
                return roadsmod.block_arch_raise(tun, cell_fn, anchor.frame, la, lo, elev(la, lo),
                                                 (cx - half, cy - half, cx + half, cy + half))
        info = heightmap.generate(anchor.frame, gen,
                                  os.path.join(self.out_dir, self.hm_name((anchor.id, cx, cy))), cx, cy, q=q,
                                  size=self.layout.block, keep=keep, chunks=None if provisional else anchor.chunks,
                                  ground_at=ground_at)
        stages = {"heights": time.perf_counter() - t0, "grading": grade_s[0], "gradingCpu": grade_s[1],
                  "heightsCpu": time.thread_time() - c0}
        idx = None
        if self.surface is not None:
            # ground models per 2 m sample from the cached land cover and the slope (surface.py);
            # rows flipped as the heightmap's (row 0 = north)
            t1 = time.perf_counter()
            classes, complete = self.surface.classes(keep["lat"], keep["lon"])
            idx = surfacemod.classify(classes, keep["u"], heightmap.SQUARE, keep["lat"], keep["lon"])
            if self.sea is not None and not provisional:    # under water: the bed (no grass through the surface)
                idx[self.sea.depth(keep["lat"], keep["lon"]) > blockworker.WATER_BED_M] = surfacemod.MUD
            road_feats, roads_complete = self.surface.roads(keep["lat"], keep["lon"])
            stages["landcover"] = time.perf_counter() - t1
            t1 = time.perf_counter()
            if road_feats:              # (provisional: the widths' guesses, not measured between the buildings)
                idx = surfacemod.paint_roads(idx, keep["lat"], keep["lon"], road_feats, heightmap.SQUARE,
                                             self.surface.urban, None if provisional else self.road_widths,
                                             blockworker._open_tunnels(g, keep))
            idx = (surfacemod.paint_lots(idx, keep["lat"], keep["lon"], self.surface.lots(keep["lat"], keep["lon"]))
                if hasattr(self.surface, "lots") else idx)
            if hasattr(self.surface, "fuel_pads"):      # gas station forecourts (--gas-stations on)
                idx = surfacemod.paint_pads(idx, keep["lat"], keep["lon"],
                                            self.surface.fuel_pads(keep["lat"], keep["lon"]))
            stages["roadPaint"] = time.perf_counter() - t1
            complete = complete and roads_complete
            info["layers"] = surfacemod.write_masks(idx[::-1], self.out_dir, self.hm_name((anchor.id, cx, cy))[:-4])
            info["surfaceComplete"] = complete
            info["roadsComplete"] = roads_complete
        if not provisional:
            idx = self._tunnel_holes(anchor, elev, keep, cx, cy, idx, info)
        info["ter"], mats = surfacemod.write_ter(self.out_dir, self.ter_name((anchor.id, cx, cy)), keep["qgrid"], idx)
        if mats is not None:          # the .ter's own material list (MATERIALS indices)
            info["mats"] = mats
        info["stages"] = stages
        return self._finish_heightmap(anchor, cx, cy, info, t0, base)

    def tunnels_of(self, anchor, elev, g):
        """anchor.tunnels (roads.TunnelProfiles), made now if not yet (elev: the caller's ground; g: the anchor's
        Grader or None, taken before: grader_for takes grader_lock)."""
        with self.grader_lock:
            if anchor.tunnels is None:
                anchor.tunnels = self.chain_view(anchor, "tunnels", elev, g)
            return anchor.tunnels

    def tunnel_floor(self, anchor):
        """The floor of a tunnel tube at the anchor's origin (roads.tunnel_floor_at), or None: a teleport into a park
        arch set the car down on the soil over it, where it hung on the arch's top (2026-10-17)."""
        if self.mapdata is None:
            return None
        g = self.grader_for(anchor)
        cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731
        try:
            def elev(la, lo):
                z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
                return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
            self.tunnels_of(anchor, elev, g)
            box = (-40.0, -40.0, 40.0, 40.0)
            la, lo, _ = anchor.frame.enu_to_geodetic(np.array([-40.0, 40.0]), np.array([-40.0, 40.0]), np.zeros(2))
            cells = [cell_fn(k) for k in mapcells.cells_for_bbox(float(la.min()), float(lo.min()),
                                                                float(la.max()), float(lo.max()))]
            return roadsmod.tunnel_floor_at(anchor.tunnels.parts(cells, box), 0.0, 0.0)
        except Exception:
            log.exception("tunnel floor at anchor %s", anchor.id)
            return None

    def chain_view(self, anchor, kind, elev, g=None):
        """anchor's roads.ChainView of the server's bridge ("bridges") or tunnel ("tunnels") chains: one
        roads.ChainStore each for every anchor (its ground elev's values are the same whatever the anchor; the tunnels'
        raw / ends at grading.PROFILE_ZOOM, as in the block workers; g: a Grader or None). Under grader_lock."""
        stores = self.__dict__.setdefault("chain_stores", {})
        cs = stores.get(kind)
        if cs is None:
            cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")  # noqa: E731
            if kind == "bridges":
                def setup(p):
                    p.wet = self.sea.depth if self.sea is not None else None    # (decks clear the water)
                    p.infra_fn = lambda key: self.mapdata.cell(
                        key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                cls = roadsmod.BridgeProfiles
            else:
                def setup(t):
                    t.wet = self.sea.depth if self.sea is not None else None
                    t.raw = lambda la, lo: self.ground(None, la, lo, self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
                    if g is not None:
                        t.ends = lambda la, lo: g.apply(la, lo, t.raw(la, lo), gradingmod.ROAD_LIFT, roads_only=True)
                cls = roadsmod.TunnelProfiles
            cs = stores[kind] = roadsmod.ChainStore(cls, cell_fn, elev, setup)
        return roadsmod.ChainView(cs, anchor.frame)

    def _tunnel_holes(self, anchor, elev, keep, cx, cy, idx, info):
        """idx with the block's tunnel portals as holes (roads.block_tunnel_holes; blockworker._tunnel_holes)."""
        g = self.grader_for(anchor)       # (before grader_lock: it takes it)
        if self.mapdata is None:
            return idx
        cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731
        try:
            self.tunnels_of(anchor, elev, g)
            holes = roadsmod.block_tunnel_holes(anchor.tunnels, cell_fn, anchor.frame, keep, cx, cy, heightmap.SQUARE)
        except Exception:
            log.exception("tunnel holes of block (%s, %s)", cx, cy)
            return idx
        if holes is None or not holes.any():
            return idx
        idx = np.zeros(holes.shape, np.uint8) if idx is None else idx
        idx[holes] = heightmap.TER_HOLE
        info["tunnelHoles"] = int(holes.sum())
        return idx

    def _base_process(self, lane):
        """The block process that renders the base photo of a block built in lane (blockworker.base_photo): the
        other lane's, usually idle while this one builds the heights; None (rendered here) with the imagery
        analyses on (they live in this process) or without block processes."""
        if not self.block_pools or self.imagery is None or self.deshadow is not None or self.colormatch is not None:
            return None
        return self.block_pools[LANE_PREFETCH if lane == LANE_REQUEST else LANE_REQUEST][0]

    def _base_photo(self, anchor, cx, cy, pool=None, urgent=False, quick=False):
        """base_pool: the block's base photo (write_base; in the block process pool when given and its imagery
        tiles are on disk) while its heights are built: (file name or None, [rgb] or [], seconds). urgent: the
        player waits on this block (a teleport's first, or one the mod asked for): its missing photo tiles are
        fetched ahead of the far field's (Imagery.urgent, netfetch "urgent"). quick: write_base's."""
        t1 = time.perf_counter()
        photo = []
        name = self._base_elsewhere(anchor, cx, cy, pool, photo) if pool is not None else None
        if name is None:
            with self.imagery.urgent() if urgent and hasattr(self.imagery, "urgent") else contextlib.nullcontext():
                name = self.write_base(anchor, cx, cy, photo=photo, quick=quick)
        return name, photo, time.perf_counter() - t1

    def _base_elsewhere(self, anchor, cx, cy, pool, photo):
        """write_base in a block process (blockworker.base_photo): the file name, or None when it lacked an imagery
        tile on disk or failed (then rendered here)."""
        size = self.layout.block * heightmap.SQUARE
        key = (anchor.id, cx, cy)
        name = self.base_name(key)
        im = self.imagery
        tex = {"chain": [s.name for s in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
               "season_mode": im.season_mode}
        try:
            got = pool.submit(blockworker.base_photo, anchor.lat, anchor.lon, cx - size / 2 - BASE_SHIFT_M,
                              cy - size / 2 - BASE_SHIFT_M, size,
                              BASE_RENDER_PX, tex, bool(self.buildings and self.mapdata is not None), anchor.zooms[0],
                              os.path.join(self.out_dir, name),
                              (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px), BASE_AO),
                              fuel_pads=self.block_fuel_pads(anchor, cx, cy, size), out_px=self.base_px).result()
        except Exception as e:
            log.info("base photo %s in the block process: %s", key, e)
            return None
        if got is None:
            return None
        rgb, _, pending = got
        self._base_kept(anchor, cx, cy, pending)
        photo.append(rgb)
        self.base_worker += 1
        return name

    def _base_kept(self, anchor, cx, cy, pending):
        """A base photo was rendered with these (imagery.render pending) still to come: base_refresh renders it
        again once they are here."""
        key = (anchor.id, cx, cy)
        self.base_pending[key] = frozenset(pending)
        if self.base_ready(anchor, cx, cy):
            self.base_stale.discard(key)
        else:
            self.base_stale.add(key)

    def _finish_heightmap(self, anchor, cx, cy, info, t0, base=None):
        """The base photo (detail mode; rendered here: the imagery corrections live in this process; base: its
        future from _base_photo, started with the heights), the timings."""
        if base is not None:
            t1 = time.perf_counter()
            if self.fast and not base.done():
                concurrent.futures.wait([base], timeout=BASE_FAST_WAIT_S)
            if self.fast and not base.done():
                # a fast player (FAST_BASE_V): the block goes now and its photo when done (block_base)
                info["base"] = None
                info["stages"]["baseWait"] = time.perf_counter() - t1
                info["stages"]["baseLater"] = 1.0
                base.add_done_callback(lambda f, key=(anchor.id, cx, cy): self._base_later(key, f))
                info["seconds"] = time.perf_counter() - t0
                info["zoom"] = anchor.hm_zoom
                return info
            info["base"], photo, info["stages"]["baseRender"] = base.result()
            info["stages"]["baseWait"] = time.perf_counter() - t1
            if photo and info.get("layers") and info.get("ter"):
                try:                # lawns only where the photo is green (before the game reads the block)
                    surfacemod.photo_lawns(self.out_dir, info["ter"], info["layers"], photo[0])
                except Exception as e:
                    log.info("photo lawns %s: %s", (anchor.id, cx, cy), e)
        info["seconds"] = time.perf_counter() - t0
        info["zoom"] = anchor.hm_zoom
        return info

    def _base_later(self, key, fut, tries=4):
        """A block sent without its photo (a fast player): the photo, once rendered, onto the block's entry (later
        terrain_ready answers) and to the mod (block_base: the block in use takes it now, a ready one at its commit)."""
        try:
            name = fut.result()[0]
        except Exception:
            return
        conns = [c for c in list(self.connections) if getattr(c, "loop", None) is not None]
        if not name or not conns:
            return
        size = self.layout.block * heightmap.SQUARE
        loop = conns[0].loop

        def apply(left=tries):
            info = self.hm.get(key)
            if info is None and left > 0:       # (the block's entry is stored just after it is built)
                loop.call_later(0.25, apply, left - 1)
                return
            if info is not None and not info.get("base"):
                info["base"] = name
            for c in list(self.connections):    # (the mod drops another anchor's)
                c.send({"type": "block_base", "anchorId": key[0], "cx": key[1], "cy": key[2],
                        "base": f"{self.vfs_dir}/{name}", "baseSize": size})
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:
            pass                                # (the loop has closed)

    def base_name(self, key, rev=0):
        return self.hm_name(key)[:-4] + ("_base.png" if not rev else f"_base_r{rev}.png")

    def write_base(self, anchor, cx, cy, pool=None, rev=0, photo=None, quick=False):
        """Detail mode: the block footprint's aerial photo (base_px² PNG, row 0 = north, the same imagery
        chain and corrections as the far tiles), the drawn block's base colour. The game would pack it into
        BC7 on its main thread (1.5 s for a new 1024² photo); terraincache writes that packed texture into
        the game's cache first (~0.4 s here), so the game takes it as it is. Returns the file name, or None
        when no imagery could be rendered (the block keeps the level's neutral base).
        quick (a teleport's provisional first block): without the roofs' mask and the painted roads when the place's
        buildings and roads are not on disk yet (their downloads, and the road widths measured from them, were 20 s
        of a fresh place's 22 s photo on a Deck, before the car could be placed; 2026-10-23); rendered again in full
        once the graded block is in (base_refresh), as for a lazy imagery source."""
        size = self.layout.block * heightmap.SQUARE
        x0, y0 = cx - size / 2 - BASE_SHIFT_M, cy - size / 2 - BASE_SHIFT_M
        quick = quick and not self._photo_data_cached(anchor, x0, y0, size)
        t0 = time.perf_counter()
        try:
            pending = set()
            with (self.deshadow.no_wait() if self.deshadow is not None else contextlib.nullcontext()):
                rgb, used = self.imagery.render(anchor.frame, x0, y0, size, BASE_RENDER_PX, pending)
            log.debug("base photo %s r%d: %s", (anchor.id, cx, cy), rev,
                      {k: round(v / BASE_RENDER_PX ** 2, 3) for k, v in used.items()})
        except Exception as e:      # an imagery failure must not cost the terrain
            log.info("base photo %s: %s", (anchor.id, cx, cy), e)
            return None
        name = self.base_name((anchor.id, cx, cy), rev)
        path = os.path.join(self.out_dir, name)
        rgb = np.asarray(rgb, np.uint8)
        stamps = [("imagery", time.perf_counter())]
        if self.buildings and self.mapdata is not None and not quick:   # no roofs beside the 3D buildings
            try:
                mask = buildingsmod.photo_mask(anchor.frame, x0, y0, size, BASE_RENDER_PX, self.mapdata)
                if mask is not None:
                    rgb = buildingsmod.fill_photo(rgb, mask, size, BASE_RENDER_PX)
            except Exception as e:      # the photo stays as it is
                log.info("base photo %s: building mask: %s", (anchor.id, cx, cy), e)
        if roadsmod.ROADS_IN_GROUND and not quick:      # the photo's own pavement beside the drawn roads
            try:
                rgb = surfacemod.unghost_square(rgb, anchor.frame, x0, y0, size, self.surface, self.road_widths)
            except Exception as e:      # the photo stays as it is
                log.info("base photo %s: road ghosts: %s", (anchor.id, cx, cy), e)
        rgb = surfacemod.enlarge(rgb, self.base_px)
        stamps.append(("buildings", time.perf_counter()))
        if roadsmod.ROADS_IN_GROUND and not quick:      # the carriageways in the ground (surface.photo_roads)
            try:
                rgb = surfacemod.paint_square(rgb, anchor.frame, x0, y0, size, self.surface, self.road_widths)
            except Exception as e:      # the photo stays as it is
                log.info("base photo %s: roads: %s", (anchor.id, cx, cy), e)
        stamps.append(("roads", time.perf_counter()))
        try:            # its cliffs as rock, like the far tiles' (rockface.py), from a 16 m grid of the ground
            n = 65
            xs = x0 + np.arange(n) * (size / (n - 1))
            ys = y0 + np.arange(n) * (size / (n - 1))
            zoom = anchor.zooms[0]
            _, U = heightmap.surface(anchor.frame, lambda la, lo: self.dem.sample(la, lo, zoom), xs, ys)
            rgb = rockfacemod.rock(rgb, anchor.frame, x0, y0, size, U, x0, y0, size / (n - 1))
        except Exception as e:      # the photo stays as it is
            log.debug("base photo %s: rock faces: %s", (anchor.id, cx, cy), e)
        # a lazy source (NAIP) not fetched yet for all of it: rendered again once it is (base_refresh)
        # ... and the analyses (de-shadow, colour match) it was rendered without (never waited for)
        key = (anchor.id, cx, cy)
        self._base_kept(anchor, cx, cy, pending)
        if quick:
            self.base_stale.add(key)
        if photo is not None:
            photo.append(rgb)
        t1 = time.perf_counter()
        tmp = staging.tmp_for(path)
        # stored, not deflated: the game reads the packed cache entry (terraincache), this file only if that is stale;
        # level 1 was 0.15 s of a block photo on a Steam Deck, level 0 0.08 (3 MB, a session keeps a few)
        with open(tmp, "wb") as f:
            f.write(terraincache.stored_png(rgb))
        staging.replace(tmp, path)
        t2 = time.perf_counter()
        try:            # after the photo: the cache entry must be the newer file
            args = (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px), rgb, BASE_AO)
            if pool is not None:            # the BC7 encode on the block process's core
                pool.submit(blockworker.base_entry, *args).result()
            else:
                terraincache.base_entry(*args)
        except OSError as e:
            log.info("terrain cache for %s: %s", name, e)
        stamps.append(("rock", t1))
        parts = " ".join(f"{k} {b - a:.2f}" for (_, a), (k, b) in zip([("", t0)] + stamps, stamps))
        log.log(logging.INFO if time.perf_counter() - t0 > SLOW_BASE_S else logging.DEBUG,
                "base photo %s r%d in %.2f s: %s, png %.2f, cache %.2f", key, rev, time.perf_counter() - t0, parts,
                t2 - t1, time.perf_counter() - t2)
        return name

    def _photo_data_cached(self, anchor, x0, y0, size):
        """Whether the buildings and roads of every map cell under the square are on disk (never fetches)."""
        if self.mapdata is None:
            return True
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([x0, x0 + size]), np.array([y0, y0 + size]), 0.0)
        keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        return all(self.mapdata.has(k, "buildings") and self.mapdata.has(k, "roads") for k in keys)

    def base_ready(self, anchor, cx, cy):
        """Rendering the block's base photo again would not change it: every lazy imagery tile is here and
        the regions it was rendered without (base_pending) are done."""
        size = self.layout.block * heightmap.SQUARE
        if not self.imagery.lazy_ready(anchor.frame, cx - size / 2 - BASE_SHIFT_M, cy - size / 2 - BASE_SHIFT_M, size,
                                       BASE_RENDER_PX):
            return False
        return all(self._region_status(rk) is not None for rk in self.base_pending.get((anchor.id, cx, cy), ()))

    def terrain_cached(self, key):
        return key in self.hm or key in self.hm_inflight

    async def terrain(self, anchor, cx, cy, progress=None, lane=LANE_REQUEST):
        """Info of the (anchorId, cx, cy) terrain, generating it once (deduplicated). lane: the block
        process that builds it (LANE_PREFETCH for the prefetch)."""
        key = (anchor.id, cx, cy)
        if key in self.hm:
            self.hm.move_to_end(key)
            return self.hm[key]
        fut = self.hm_inflight.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = self.hm_inflight[key] = loop.run_in_executor(
                self.hm_pool, self.make_heightmap, anchor, cx, cy, progress, lane, time.perf_counter())
            fut.add_done_callback(lambda f: self._hm_done(key, f))
        return await asyncio.shield(fut)

    def _hm_done(self, key, fut):
        self.hm_inflight.pop(key, None)
        if fut.cancelled() or fut.exception() is not None:
            return
        info = fut.result()
        log.info("terrain %s z%d in %.2f s (%s): zBase %.0f maxHeight %.0f (Q 1/%d)", key, info["zoom"],
                 info["seconds"], " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()),
                 info["zBase"], info["maxHeight"], round(1 / info["q"]))
        self.hm[key] = info
        # (its roads and land cover painted once their cells are here: Connection.roads_refresh. A first visit's blocks
        # came before the land cover and kept bare dirt for good, no grass, Harz 2026-10-27, GitHub #316)
        if info.get("roadsComplete") is False or info.get("surfaceComplete") is False:
            if key not in self.roads_rebuild:
                log.info("block %s built before its map cells were here (roads %s, surface %s): painted again later",
                         key, info.get("roadsComplete"), info.get("surfaceComplete"))
            self.roads_rebuild.add(key)
        else:
            self.roads_rebuild.discard(key)
        self.evict_heightmaps(fresh=key)

    def evict_heightmaps(self, fresh=None):
        """Keep at most HM_CACHE entries, never deleting the current terrain, the
        last two committed ones, the last two handed to the mod, or `fresh` (the terrain
        just built: a request about to be answered, or the prefetch the mod asks for next)."""
        keep = set(self.hm_committed) | set(self.hm_sent)
        if fresh is not None:
            keep.add(fresh)
        # The first terrain of an anchor no connection has committed yet: anchor_ready
        # handed it out and the rebase may commit it only after more terrain-follow requests
        # in the old frame. (Once committed it is in hm_committed while it matters; keeping
        # every anchor's first terrain made the protected set larger than HM_CACHE after a
        # rebase, so each new prefetch was deleted as soon as it was built, and rebuilt on
        # every pose.)
        newest = max((c.active for c in self.connections if c.active is not None), default=None)
        keep |= {(aid, *a.first_terrain) for aid, a in sorted(self.anchors.items())[-2:]
                 if a.first_terrain is not None and (newest is None or aid > newest)}
        for key in [k for k in self.hm if k not in keep][:max(0, len(self.hm) - HM_CACHE)]:
            info = self.hm.pop(key)
            self._remove(os.path.join(self.out_dir, self.hm_name(key)))
            self._remove(os.path.join(self.out_dir, self.base_name(key)))
            self.base_stale.discard(key)
            self.base_pending.pop(key, None)
            if info.get("base") and info["base"] != self.base_name(key):     # a refreshed photo
                self._remove(os.path.join(self.out_dir, info["base"]))
            if info.get("ter"):
                self._remove(os.path.join(self.out_dir, info["ter"]))
            if self.near_visual == "detail":     # its packed copies in the game's terrain texture cache
                for name in {self.base_name(key), info.get("base") or self.base_name(key)}:
                    for px in BASE_SIZES:       # (whichever size it was made at)
                        self._remove(os.path.join(terraincache.cache_dir(self.user_dir), terraincache.base_key(
                            f"{self.vfs_dir}/{name}", base_ao_vfs(px)) + ".dds"))
            if self.surface is not None:        # its surface masks (only read at import)
                for name in surfacemod.mask_names(self.hm_name(key)[:-4]):
                    self._remove(os.path.join(self.out_dir, name))

    # --- far tiles -------------------------------------------------------
    def tile_name(self, aid, key):
        return f"far_{aid}_{key}.{self.far_format}"

    def attribution_rest(self):
        """The credit line's sources other than the imagery (weather, elevation)."""
        parts = [weathermod.CREDIT] if self.weather is not None else []
        return "; ".join(parts + [ELEVATION_CREDIT])

    def attribution(self):
        """The credit line the mod shows: imagery (if on) and elevation sources."""
        parts = [self.imagery.attribution()] if self.imagery is not None else []
        if self.weather is not None:
            parts.append(weathermod.CREDIT)
        return "; ".join(parts + [ELEVATION_CREDIT])

    def net_note(self):
        """The network state for the status text: "" when all is well, "Offline - using cached data"
        (--offline, or every imagery / DEM host unreachable), else the unreachable hosts and the
        tiles drawn without imagery. Hosts the world builds without (gravity, weather, search, routing, the update
        check) are not named: a player in a country that blocks SourceForge saw "Degraded" for good (GitHub #23)."""
        if self.net.offline:
            return netfetch.OFFLINE_TEXT
        parts = []
        down = self.net.unavailable_hosts(optional=False)
        if down:            # each with what it affects (netfetch.ROLES: the user, 2026-10-27)
            parts.append("; ".join(f"{h} unreachable ({netfetch.role_of(h)})" if netfetch.role_of(h)
                                   else f"{h} unreachable" for h in down))
        if self.degraded:
            parts.append(f"{len(self.degraded)} tiles without imagery")
        if self.net.slow() is not None:     # (map data at a crawl: a new place's roads take minutes: GitHub #241;
            # no number in it: the note is re-sent whenever its text changes)
            parts.append("slow internet connection: new places take a few minutes to load")
        return "Degraded: " + "; ".join(parts) if parts else ""

    def level_summary(self):
        """'L<level> <geometry ms>/<texture ms> <shape KB>/<texture KB>' medians, for the log."""
        out = []
        for lvl, st in sorted(self.level_stats.items()):
            # list() copies the deque in one step: tile workers append to it meanwhile, and a
            # Python-level iteration over it could raise "deque mutated during iteration" here
            # (in the connection's streamer, which would stop streaming).
            st = list(st)
            med = [sorted(v[k] for v in st)[len(st) // 2] for k in range(4)]
            out.append(f"L{lvl} {1000 * med[0]:.1f}/{1000 * med[1]:.1f} {med[2] / 1024:.0f}/{med[3] / 1024:.0f}")
        return ", ".join(out)

    def texture_name(self, aid, key, rev=0):
        """A tile's texture file; one rendered again (--colormatch, rev > 0) gets a new name: the engine
        caches textures by path, and the mod points the tile's material at the new file."""
        return f"far_{aid}_{key}.dds" if rev == 0 else f"far_{aid}_{key}_r{rev}.dds"

    def tile_files(self, name, texture=None, rev=0):
        """A far tile's files: the shape, its texture (`texture`: a re-rendered one's name), every
        earlier revision's texture up to `rev` (a tile retextured live left them behind) and the
        engine's .cdae cache of it (DAE tiles)."""
        stem = os.path.splitext(name)[0]
        out = [os.path.join(self.out_dir, name), os.path.join(self.out_dir, stem + ".dds"),
               os.path.join(self.out_dir, stem + "_road.dds")]
        out += [os.path.join(self.out_dir, f"{stem}_r{r}.dds") for r in range(1, rev + 1)]
        if texture is not None and os.path.join(self.out_dir, texture) not in out:
            out.append(os.path.join(self.out_dir, texture))
        if name.endswith(".dae"):
            out.append(os.path.join(self.cdae_dir, os.path.splitext(name)[0].lower() + ".cdae"))
        return out

    def tex_size(self, level):
        """Texture edge of a level (the layout's per-level table: with 32 cells the two finest
        levels of the mode get tex_near, the others tex_far; horizon mode: farfield.HORIZON_TEX from L8)."""
        if self.horizon and level in farfield.HORIZON_TEX:
            return farfield.HORIZON_TEX[level]
        return self.layout.tex_size(level)

    @property
    def max_level(self):
        """The quadtree's root level: L9 (v1.3.1), L10 in horizon mode."""
        return farfield.HORIZON_MAX_LEVEL if self.horizon else farfield.MAX_LEVEL

    # --- horizon (M1h) -------------------------------------------------------
    def make_horizon_grid(self, anchor, half):
        """Worker thread: the anchor's zmax grid over [-half, half]² (a coarse DEM pass)."""
        t0 = time.perf_counter()
        cache = _BoundedLru(self.horizon_blocks, HORIZON_BLOCK_CACHE)
        grid = farfield.horizon_grid(anchor.frame, self.dem, half, cache=cache)
        log.info("anchor %d horizon grid: %.0f km half-size, DEM z%d, %d cells, highest %.0f m, in %.2f s",
                 anchor.id, half / 1000, grid.zoom, grid.base.size, float(grid.base.max()), time.perf_counter() - t0)
        return grid

    def horizon_grid(self, anchor, h_eye):
        """The anchor's zmax grid, or None before its first one is built. Starts a build (on the event
        loop's executor) when there is none, or when the one it has is smaller than an eye at h_eye
        needs (an aircraft climbing); a finished build re-selects every connection on the anchor."""
        half = farfield.horizon_extent(h_eye, ANCHOR_CENTRE_MAX)
        have = self.horizon_grids.get(anchor.id)
        busy = self.horizon_inflight.get(anchor.id)
        failed = self.horizon_failed.get(anchor.id)
        if ((have is None or have.half < half) and (busy is None or busy[1] < half)
                and (failed is None or time.monotonic() - failed > HORIZON_RETRY)):
            if busy is not None:
                busy[0].cancel()            # a smaller build that has not started yet
            fut = asyncio.get_running_loop().run_in_executor(self.horizon_pool, self.make_horizon_grid, anchor, half)
            self.horizon_inflight[anchor.id] = (fut, half)
            fut.add_done_callback(lambda f, aid=anchor.id: self._horizon_done(aid, f))
        return have

    def _horizon_done(self, aid, fut):
        cur = self.horizon_inflight.get(aid)
        if cur is not None and cur[0] is fut:
            del self.horizon_inflight[aid]
        if fut.cancelled():
            return
        if fut.exception() is not None:
            self.horizon_failed[aid] = time.monotonic()
            log.warning("anchor %d: horizon grid failed (%r), retrying in %.0f s", aid, fut.exception(), HORIZON_RETRY)
            return
        grid = fut.result()
        if aid not in self.anchors:
            return                          # pruned meanwhile
        have = self.horizon_grids.get(aid)
        if have is None or have.half < grid.half:
            self.horizon_grids[aid] = grid
            self.horizon_failed.pop(aid, None)
            for c in list(self.connections):
                if c.active == aid:
                    c.update_far()

    def prefetch_imagery(self, anchor, tile):
        """Start downloading a queued tile's imagery (fire and forget), so tile workers rarely
        wait on the network. Only the first source of the chain; errors surface at build time."""
        if self.imagery is not None:
            frame, x0, y0, size = self.tile_where(anchor, tile)
            px = self.tex_size(tile.level)
            self.imagery.prefetch(frame, *imagerymod.gutter_square(x0, y0, size, px), px, soon=True)

    # (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
    # def share_start(self, port=None):
    #     """Compute sharing in a BeamMP group: our GroupHelper listening (started once) on port (the BeamMP server's
    #     setting, its plugin's SHARE_PORT; BNGE_SHARE_PORT on this machine wins; else peerwork.SHARE_PORT). -> {port,
    #     code} (port None: not sharing)."""
    #     if not hasattr(self.far_pool, "set_group"):
    #         return {"port": None, "code": peerworkmod.code_id()}
    #     self.far_pool.sharing = True        # every tile built is kept from now on (a player arriving gets ours)
    #     with self.share_lock:
    #         if self.group_helper is None:
    #             self.group_helper = peerworkmod.GroupHelper(self.far_pool, self.anchor_id_for,
    #                                                         os.path.join(self.cache_dir, "peer-out"))
    #             self.group_helper.start(share_port(port))
    #     return {"port": self.group_helper.port, "code": peerworkmod.code_id(), "weight": peerworkmod.share_weight()}

    # def share_group(self, msg):
    #     """The mod's "peers": {me, members: [{id, host, port, code}]}: the players on our anchor with our code."""
    #     if not hasattr(self.far_pool, "set_group"):
    #         return
    #     code = peerworkmod.code_id()
    #     members, weights = [], {}
    #     for m in msg.get("members") or []:
    #         try:
    #             if m.get("code") == code and m.get("host") and 0 < int(m.get("port")) < 65536:
    #                 members.append((str(m["id"]), str(m["host"]), int(m["port"])))
    #                 w = float(m.get("weight") or 1.0)
    #                 weights[str(m["id"])] = w if 0.1 <= w <= 1000 else 1.0
    #         except (TypeError, ValueError, AttributeError):
    #             continue
    #     self.far_pool.set_group(str(msg.get("me")), members, weights)

    # def anchor_id_for(self, lat, lon):
    #     """Our anchor id at (lat, lon) when we have one there (its graders are made already), else the helper's."""
    #     for a in list(self.anchors.values()):
    #         if float(a.lat) == lat and float(a.lon) == lon:
    #             return a.id
    #     return peerworkmod._anchor_id(lat, lon)

    def remote_slots(self):
        """Far tiles compute sharing's helpers build at once (0 without helpers)."""
        f = getattr(self.far_pool, "remote_slots", None)
        return f() if f is not None else 0

    def far_ck(self, aid, key):
        """The server's key of far tile `key` shown under anchor aid: (GLOBAL_AID, key) for a global tile (the same
        for every anchor), else (aid, key)."""
        return (GLOBAL_AID, key) if farfield.is_global_key(key) else (aid, key)

    def tile_where(self, anchor, tile):
        """(frame, x0, y0, size) of the square a tile's texture is rendered over in that frame: the node's for a piece;
        a global tile's own frame and its (Mercator) square, about [-s/2, s/2]^2 there."""
        if isinstance(tile, farfield.GTile):
            return geotilesmod.frame(tile.x, tile.y, tile.z), -tile.size / 2, -tile.size / 2, tile.size
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        x0, y0, _, _ = node.bbox
        return anchor.frame, x0, y0, node.size

    def render_tile(self, anchor, tile, px, pending):
        """(rgb, used): a far tile's photo with its gutter (a global tile's: its Mercator square, render_merc)."""
        if isinstance(tile, farfield.GTile):
            return self.imagery.render_merc(tile.z, tile.x, tile.y, px, pending=pending)
        frame, x0, y0, size = self.tile_where(anchor, tile)
        return self.imagery.render(frame, *imagerymod.gutter_square(x0, y0, size, px), px, pending=pending)

    def make_tile(self, anchor, tile, prev=None):
        """Worker thread: build and write one far tile (and its texture). Returns its TileInfo.
        prev: the tile's stale TileInfo (--colormatch): only its texture is rendered again.
        A global tile (farfield.GTile) is built in its own frame, the same for every anchor (ck (GLOBAL_AID, key));
        a piece (farfield.Piece) is its node's tile cut to its global tile's quad."""
        if prev is not None:
            return self.retexture(anchor, tile, prev)
        t0 = time.perf_counter()
        lvl = tile.level
        is_global = isinstance(tile, farfield.GTile)
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        ck = self.far_ck(anchor.id, tile.key)
        path = os.path.join(self.out_dir, self.tile_name(*ck))
        done = None             # (texture file, bytes, sources used) when the worker rendered it (blockworker.far_tile)
        geo = None              # the geometry's future while this thread renders the texture
        if is_global:
            zoom = farfield.global_dem_zoom(tile.z, self.layout.cells)
            gref = farfield.global_dem_zoom(tile.z + 1, self.layout.cells)
            if self.far_pool is not None:
                geo = self.far_pool.submit(blockworker.far_global_geometry, tile.z, tile.x, tile.y, zoom, gref,
                                           self.layout)
                built = None
            else:
                base = self.airports.wrap(self.dem) if self.airports is not None else self.dem
                if self.sea is not None:
                    base = seacarvemod.SeaDem(base, None)
                built = farfield.build_global(base, tile.z, tile.x, tile.y, zoom, gref, self.layout)
        else:
            q = self.anchor_q(anchor) if lvl <= 0 else None
            ref_zoom = anchor.zooms[lvl - 1] if lvl > 0 else None
            zoom = anchor.zooms[max(lvl, 0)]
            part = self.piece_parts.get((anchor.id, node.key)) if node is not tile else None
            g = self.grader_for(anchor) if lvl <= 0 and self.far_pool is None else None   # graded like the block
            base = self.airports.wrap(self.dem) if self.airports is not None else self.dem
            if self.sea is not None:
                base = seacarvemod.SeaDem(base, self.sea if lvl <= seacarvemod.CARVE_MAX_LEVEL else None)
            src = gradingmod.GradedDem(base, g) if g is not None else base
            if self.far_pool is not None and self.imagery is not None and self.deshadow is None \
                    and self.colormatch is None and node is tile:
                # the texture in the far-tile process too, from the imagery on disk (no analyses to apply here)
                texture = self.texture_name(anchor.id, tile.key, self.tex_rev.get(ck, 0))
                im = self.imagery
                tex = {"path": os.path.join(self.out_dir, texture), "px": self.tex_size(lvl),
                       "mask": (os.path.join(self.out_dir, surfacemod.road_mask_name(texture)),
                                self.road_mask_px(lvl)) if self.road_masks else None,
                       "chain": [s.name for s in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
                       "season_mode": im.season_mode}
                args = (blockworker.far_tile, anchor.id, anchor.lat, anchor.lon, tile, zoom, q,
                        ref_zoom, self.near_visual in BLOCK_DRAWN, self.layout, tex)
                built, got = self.far_pool.submit(*args).result()
                if got is None:
                    # a photo tile was not on disk (its prefetch dropped: a host at its daily budget, GitHub #116, or a
                    # fast drive): fetched here, the render goes back to the workers (rendered in this process, one
                    # interpreter for every far thread, the far field waited on it); here only if that fails too
                    x0, y0, _, _ = tile.bbox
                    if im.fetch_needed(anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, tex["px"]),
                                       tex["px"]):
                        built, got = self.far_pool.submit(*args).result()
                        self.refetched += 1
                if got is not None:
                    done = (texture, got[0], got[1])
            elif part is not None:          # (another piece of this node built it)
                built = part["built"]
            elif self.far_pool is not None:
                # the geometry in a far-tile process while this thread warps the texture from the finished imagery (no
                # analysis waits there any more: imagery.Imagery.finished); the steps that need both after it
                geo = self.far_pool.submit(blockworker.far_geometry, anchor.id, anchor.lat, anchor.lon, node,
                                           zoom, q, ref_zoom, self.near_visual in BLOCK_DRAWN, self.layout)
                built = None
            else:
                built = farfield.build_tile(anchor.frame, src, node, zoom, q, ref_zoom,
                                            with_sink=self.near_visual in BLOCK_DRAWN, layout=self.layout)
        t1 = time.perf_counter()
        geo_wait = 0.0
        material = texture = used = None
        tex_bytes = 0
        pending = set() if self.colormatch is not None or self.deshadow is not None else None
        part = self.piece_parts.get((anchor.id, node.key)) if node is not tile else None
        if done is not None:
            texture, tex_bytes, used = done
            material = self.matslots.take(ck)
            self.degraded.discard(ck)
            self.imagery_worker += 1
        elif part is not None and part.get("tex") is not None:      # the node's texture, another piece's
            texture = self.texture_name(*ck, self.tex_rev.get(ck, 0))
            data, tex_bytes, used, was = part["tex"]
            self.write_file(os.path.join(self.out_dir, texture), data)
            if pending is not None:
                pending |= set(was)
            material = self.matslots.take(ck)
            self.degraded.discard(ck)
        elif self.imagery is not None:
            try:
                px = self.tex_size(lvl)
                texture = self.texture_name(*ck, self.tex_rev.get(ck, 0))
                tk = self.tex_key(anchor, tile, px)
                hit = revisitmod.load(self.cache_dir, "fartex", tk)
                if hit is not None:         # (rendered in this place before: the revisit cache)
                    blobs, extra = hit
                    if "mask.dds" in blobs:
                        self.write_file(os.path.join(self.out_dir, surfacemod.road_mask_name(texture)),
                                        blobs["mask.dds"])
                    self.write_file(os.path.join(self.out_dir, texture), blobs["tex.dds"])
                    tex_bytes, used = int(extra["tex_bytes"]), extra["used"]
                else:
                    f0 = revisitmod.faults()
                    rgb, used = self.render_tile(anchor, tile, px, pending)
                    if geo is not None:
                        w0 = time.perf_counter()
                        built, geo = geo.result(), None
                        geo_wait = time.perf_counter() - w0
                    rgb = self.rock_faces(anchor, tile, px, rgb, built.grid)
                    if not is_global:
                        rgb = self.road_faces(anchor, node, px, rgb)
                        self.write_road_mask(anchor, node, px, texture)
                    tex_bytes = imagerymod.write_dds(os.path.join(self.out_dir, texture), rgb)
                    self.keep_texture(tk, texture, tex_bytes, used, pending, f0)
                material = self.matslots.take(ck)
                self.degraded.discard(ck)
            except imagerymod.ImageryError as e:
                # Network trouble: this tile keeps the palette look (the mod's static material).
                self.degraded.add(ck)
                self.imagery_failures += 1
                if self.imagery_failures % 100 == 1:
                    log.warning("tile %s of anchor %d: no imagery (%s), palette material (%d tiles so far)",
                                tile.key, anchor.id, e, self.imagery_failures)
        if geo is not None:             # (a revisit hit, or no imagery)
            w0 = time.perf_counter()
            built = geo.result()
            geo_wait += time.perf_counter() - w0
        if node is not tile:            # a piece: its node cut to its global tile
            if part is None:
                tex = None
                if material is not None:
                    with open(os.path.join(self.out_dir, texture), "rb") as fh:
                        tex = (fh.read(), tex_bytes, used, frozenset(pending or ()))
                self.piece_parts[(anchor.id, node.key)] = {"built": built, "tex": tex}
                while len(self.piece_parts) > PIECE_PARTS:
                    self.piece_parts.popitem(last=False)
            built = farfield.clip_built(built, tile.quad)
        t2 = time.perf_counter()
        size = farfield.write_built(path, built, self.far_format, material or farfield.MATERIAL,
                                    textured=material is not None, tex_px=self.tex_size(lvl))
        dt = time.perf_counter() - t0
        self.build_stats.append(dt)
        tex_s = t2 - t1 - geo_wait
        self.level_stats.setdefault(lvl, deque(maxlen=200)).append((dt - tex_s, tex_s, size, tex_bytes))
        sources = frozenset()
        if self.credits is not None:        # the tile's imagery and DEM sources (data credits)
            frame, x0, y0, s = self.tile_where(anchor, tile)
            sources = creditsmod.tile_sources(frame, (x0, y0, x0 + s, y0 + s), self.dem, zoom,
                                              used if material is not None else None)
        info = TileInfo(built.pos, built.err, built.sink, built.skirt, built.grid, size, dt,
                        material, texture, tex_bytes, tex_s, credits=sources)
        info.tile = tile
        if is_global:
            info.frame = farfield.global_frame_ll(tile.z, tile.x, tile.y)
        if material is not None and self.far_format == "cdae":    # the pooled shape path (matslots.py)
            info.shape = matslotsmod.MatSlots.file(material)
            matslotsmod.place(path, os.path.join(self.out_dir, info.shape))
        if material is not None:
            info.rev = self.tex_rev.get(ck, 0)
            info.cm_pending = frozenset(pending or ())
        return info

    def tex_key(self, anchor, tile, px):
        """The revisit cache's key of a far tile's texture rendered here (revisit.py): a global tile's names no anchor
        (any anchor's, any session's); a piece's is its node's (the same texture)."""
        im = self.imagery
        if isinstance(tile, farfield.GTile):
            return revisitmod.key("fartex", "m", farfield.GLOBAL_GEO_VERSION, tile.key, px, [s.name for s in im.chain],
                                  self.tone, im.leaf_on, im.season_mode, self.tex_modes, self.layout.cells,
                                  farfield.global_dem_zoom(tile.z, self.layout.cells))
        if isinstance(tile, farfield.Piece):
            tile = tile.base
        return revisitmod.key("fartex", round(float(anchor.lat), 7), round(float(anchor.lon), 7), tile.key, px,
                              self.road_mask_px(tile.level) if self.road_masks else 0, [s.name for s in im.chain],
                              self.tone, im.leaf_on, im.season_mode, self.tex_modes, roadsmod.ROADS_IN_GROUND,
                              self.mapdata.versions() if self.mapdata is not None else None,
                              self.road_widths is not None, self.surface is not None, self.layout.cells,
                              anchor.zooms[max(tile.level, 0)])

    def keep_texture(self, tk, texture, tex_bytes, used, pending, f0):
        """A far texture rendered here kept in the revisit cache when it is final: no colour-match or de-shadow region
        still to come (pending), nothing it read failed meanwhile (f0: revisit.faults() before; None: not checked, a
        render again for a region that came)."""
        if not revisitmod.ON or pending or (f0 is not None and revisitmod.faults() != f0):
            return
        try:
            blobs = {}
            with open(os.path.join(self.out_dir, texture), "rb") as f:
                blobs["tex.dds"] = f.read()
            mp = os.path.join(self.out_dir, surfacemod.road_mask_name(texture))
            if os.path.exists(mp):
                with open(mp, "rb") as f:
                    blobs["mask.dds"] = f.read()
            revisitmod.store(self.cache_dir, "fartex", tk, blobs,
                             {"tex_bytes": int(tex_bytes), "used": {str(n): int(c) for n, c in (used or {}).items()}})
        except Exception as e:          # (not kept rather than no tile)
            log.debug("revisit: far texture %s not kept: %s: %s", texture, type(e).__name__, e)

    @staticmethod
    def write_file(path, data):
        """path written atomically (staging: the game sees only finished files)."""
        tmp = staging.tmp_for(path)
        with open(tmp, "wb") as f:
            f.write(data)
        staging.replace(tmp, path)

    def rock_faces(self, anchor, tile, px, rgb, grid):
        """The far tile's photo with its steep ground drawn as rock (rockface.py: cliffs were the photo's top
        stretched down the wall); as it was if that fails."""
        if grid is None:
            return rgb
        try:
            frame, x0, y0, size = self.tile_where(anchor, tile)
            g = np.asarray(grid)
            return rockfacemod.rock(rgb, frame, *imagerymod.gutter_square(x0, y0, size, px), g,
                                    x0, y0, size / (g.shape[1] - 1))
        except Exception as e:          # the photo as it is
            log.debug("rock faces of %s: %s", tile.key, e)
            return rgb

    def road_faces(self, anchor, tile, px, rgb):
        """The far tile's photo with the carriageways painted in (roads.ROADS_IN_GROUND, surface.paint_square: the
        near ring the drawn block does not cover); as it was otherwise or if that fails."""
        if not roadsmod.ROADS_IN_GROUND:
            return rgb
        try:
            x0, y0, _, _ = tile.bbox
            return surfacemod.paint_square(rgb, anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, px),
                                           self.surface, self.road_widths)
        except Exception as e:          # the photo as it is
            log.debug("road faces of %s: %s", tile.key, e)
            return rgb

    def road_mask_px(self, level):
        """The road mask's edge for a near level (farfield.NEAR_MASK_PX), or 0 (none)."""
        return farfield.NEAR_MASK_PX.get(level - self.layout.exact_level, 0)

    def write_road_mask(self, anchor, tile, px, texture):
        """The near tile's road mask beside its texture (self.road_masks: mesh mode; surface.road_mask, over the
        photo's square with its gutter, at road_mask_px), written before the texture (the mod looks for it when it
        sets the texture)."""
        mpx = self.road_mask_px(tile.level)
        if not self.road_masks or not mpx:
            return
        try:
            x0, y0, _, _ = tile.bbox
            mask = surfacemod.road_mask(anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, px), mpx,
                                        self.surface, self.road_widths)
            if mask is not None:
                imagerymod.write_dds(os.path.join(self.out_dir, surfacemod.road_mask_name(texture)),
                                     np.repeat(mask[..., None], 3, axis=2))
        except Exception as e:          # the tile without its asphalt layer
            log.debug("road mask of %s: %s", tile.key, e)

    def retexture(self, anchor, tile, prev):
        """Worker thread: render a stale tile's texture again (its colour-match regions are ready now)
        under the next revision's file name; the shape and its material name stay. Network trouble:
        the previous TileInfo, still stale."""
        t0 = time.perf_counter()
        px = self.tex_size(tile.level)
        pending = set()
        try:
            rgb, used = self.render_tile(anchor, tile, px, pending)
        except imagerymod.ImageryError as e:
            log.debug("tile %s of anchor %d: texture not rendered again (%s)", tile.key, anchor.id, e)
            return prev
        rgb = self.rock_faces(anchor, tile, px, rgb, prev.grid)
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        ck = self.far_ck(anchor.id, tile.key)
        if not isinstance(tile, farfield.GTile):
            rgb = self.road_faces(anchor, node, px, rgb)
        texture = self.texture_name(*ck, prev.rev + 1)
        if not isinstance(tile, farfield.GTile):
            self.write_road_mask(anchor, node, px, texture)
        tex_bytes = imagerymod.write_dds(os.path.join(self.out_dir, texture), rgb)
        self.keep_texture(self.tex_key(anchor, tile, px), texture, tex_bytes, used, pending, None)
        sources = prev.credits
        if self.credits is not None:        # the new texture's sources (the corrected one names S2 too)
            frame, x0, y0, s = self.tile_where(anchor, tile)
            zoom = (farfield.global_dem_zoom(tile.z, self.layout.cells) if isinstance(tile, farfield.GTile)
                    else anchor.zooms[max(tile.level, 0)])
            sources = creditsmod.tile_sources(frame, (x0, y0, x0 + s, y0 + s), self.dem, zoom, used)
        return replace(prev, texture=texture, tex_bytes=tex_bytes, tex_seconds=time.perf_counter() - t0,
                       cm_pending=frozenset(pending), stale=False, rev=prev.rev + 1, credits=sources)

    def tile(self, anchor, tile):
        """asyncio future for the tile's TileInfo; generated at most once per (anchorId, key) (a global tile's: once
        for every anchor, far_ck)."""
        ck = self.far_ck(anchor.id, tile.key)
        loop = self.loop = asyncio.get_running_loop()
        prev = None
        if ck in self.tiles:
            self.tiles.move_to_end(ck)
            if not self.tiles[ck].stale:
                f = loop.create_future()
                f.set_result(self.tiles[ck])
                return f
            prev = self.tiles[ck]           # --colormatch: its texture is rendered again
        fut = self.tile_inflight.get(ck)
        if fut is None:
            fut = self.tile_inflight[ck] = loop.run_in_executor(self.tile_pool, self.make_tile, anchor, tile, prev)
            fut.add_done_callback(lambda f: self._tile_done(ck, f))
        return fut

    def in_use(self):
        """(anchorId, key) of every tile a connection shows or holds, or let go of less than
        prune_delay ago: after a far_swap / far_remove the mod still shows the tile until the
        swap has run (its adds load within the governor budget, then a 0.5 s crossover) and
        deletes it time-sliced, and a shape file must not vanish under a live TSStatic."""
        out = set()
        now = time.monotonic()
        for c in self.connections:
            out |= set(c.shown)
            out |= {self.far_ck(c.active, k) for k in c.sent}
            # built for the active anchor but not sent yet: evicting these deleted the files of
            # tiles about to be sent (in-game 2026-10-10, after a 200 mph run had left thousands
            # of held previous-anchor tiles in the cache: 150 tiles failed with "No Mesh")
            out |= {self.far_ck(c.active, k) for k in c.desired}
            out |= set(c.inflight)
            out |= {ck for ck, t in c.retired.items() if now - t < self.retired_keep}
        held = self.held_anchors()
        if held:
            out |= {ck for ck in self.tiles if ck[0] in held}
        return out

    def names_in_use(self):
        """(anchorId, key) of the tiles a connection is about to send or build (desired, sent, in flight): they
        keep their names. Whether a game still draws a name is slot_held()."""
        out = set()
        for c in self.connections:
            out |= {self.far_ck(c.active, k) for k in c.sent}
            out |= {self.far_ck(c.active, k) for k in c.desired}
            out |= set(c.inflight)
        return out

    def slot_held(self, n):
        """Does some game still hold pooled slot n (a far_tile with it not reported gone)?"""
        return any(n in c.slot_holds for c in self.connections)

    def slots_let_go(self, slots):
        """Event loop: these slots' holds ended (reported gone, or a connection closed)."""
        for n in slots:
            if not self.slot_held(n):
                self.matslots.unheld(n)

    def held_anchors(self):
        """Previous anchors whose tile objects some mod still has (pose.held): their files stay."""
        out = set()
        for c in self.connections:
            out |= c.held
        out.discard(GLOBAL_AID)
        return out

    def _tile_done(self, ck, fut):
        self.tile_inflight.pop(ck, None)
        if fut.cancelled() or fut.exception() is not None:
            return
        before = self.tiles.get(ck)
        info = self.tiles[ck] = fut.result()
        if before is not None and info.texture != before.texture:
            self._send_retex(ck, info)      # a re-rendered texture: the mods showing the tile switch to it
        for rk in sorted(info.cm_pending, key=str):     # regions it was rendered without
            st = self._region_status(rk)
            if st is None:
                self.cm_waiting.setdefault(rk, set()).add(ck)
            else:                           # ready meanwhile (while the texture was being rendered)
                self._cm_settle(ck, rk, st, pump=False)
        if before is not None and info is not before:     # a re-render: stale at once again, or not
            info.retex_repeat = before.retex_repeat + 1 if info.stale else 0
        if info.stale:
            self._retex_pump()
        if self.matslots.short():
            self.reclaim_names()
        if len(self.tiles) > self.tile_cache:
            used = self.in_use()
            for old in [k for k in self.tiles if k not in used][:len(self.tiles) - self.tile_cache]:
                self._evict_tile(old)

    def reclaim_names(self):
        """Event loop: the pooled names (and shape files) of tiles about to be neither sent nor built back to the
        pool (matslots; one a game still holds waits for its report): names left by builds that never finished
        first, then idle cached tiles, least recently used first."""
        used = self.names_in_use()
        owners = self.matslots.holders()
        lost = [ck for ck in owners if ck not in self.tiles and ck not in self.tile_inflight and ck not in used]
        idle = (ck for ck in self.tiles if ck not in used and ck not in self.tile_inflight)
        n = self.matslots.reclaim(itertools.chain(lost, idle))
        log.debug("material names: %d reclaimed, %s", n, self.matslots.stats())

    def pool_shapes(self, conn, files):
        """Event loop: the road / building tile's shape files ({what: file name in the session folder}) as pooled ones
        (matslots.py; the far tiles' slots): each a hard link at a slot path, held by conn until the mod reports it
        gone (pose.slotsGone). Returns {what: [vfs path, serial]} for the message's `slots` (files that do not exist
        are left out; a slot that cannot be placed too: the mod then loads the tile's own file)."""
        out = {}
        if self.far_format != "cdae":
            return out
        for what, fname in files.items():
            src = os.path.join(self.out_dir, fname)
            if not os.path.exists(src):
                continue
            owner = ("shape", next(self.slot_serial))
            name = self.matslots.take(owner)
            slot = matslotsmod.MatSlots.file(name)
            try:
                matslotsmod.place(src, os.path.join(self.out_dir, slot))
            except OSError as e:
                log.debug("pooled shape %s: %s", fname, e)
                self.matslots.release(owner)
                continue
            serial = next(self.slot_serial)
            conn.slot_holds[matslotsmod.MatSlots.number(name)] = serial
            self.matslots.release(owner)        # no tile owns it: free again once the mod lets go
            out[what] = [f"{self.vfs_dir}/{slot}", serial]
        return out

    def reslot(self, ck, info):
        """Event loop: a cached tile whose material name went to another tile meanwhile (reclaim_names) is
        sent again: a copy of its shape with the new name in the new name's pooled file (slot_<n>; DAE tiles: a file
        _s<serial>, the old copy deleted), even when it got its own name back (its pooled file may hold another
        tile by now).
        On failure the tile keeps its name: the caller builds it again rather than send it with another tile's name."""
        new = self.matslots.take(ck)
        src = os.path.join(self.out_dir, self.tile_name(*ck))
        pooled = self.far_format == "cdae"
        name = matslotsmod.MatSlots.file(new) if pooled else f"{os.path.splitext(self.tile_name(*ck))[0]}_s{next(self.reslot_serial)}.{self.far_format}"
        try:
            with open(src, "rb") as fh:
                data = fh.read()
            # the name in the tile's own file: the one it was built with, not info.material once it was renamed before
            # (a global far tile kept across rebases is renamed again and again: restamping info.material failed from
            # its second rename on, and the tile was sent with a name another tile had, drawing that tile's texture:
            # Utah's red far tiles on the Pikes Peak ground, 2026-10-29)
            have = info.material if info.material.encode() in data else self.matslots.name_in(data)
            if have is None:
                raise ValueError(f"no pooled name in {os.path.basename(src)}")
            if new != have:
                data = matslotsmod.restamp(data, have, new)
            dst = os.path.join(self.out_dir, name)
            tmp = staging.tmp_for(dst)
            with open(tmp, "wb") as fh:
                fh.write(data)
            staging.replace(tmp, dst)
        except (OSError, ValueError) as e:
            log.warning("tile %s of anchor %d: material name not renewed (%s)", ck[1], ck[0], e)
            return info
        if not pooled and info.shape is not None:
            self.queue_delete([os.path.join(self.out_dir, info.shape)])
        info = self.tiles[ck] = replace(info, material=new, shape=name)
        return info

    def _evict_tile(self, ck):
        info = self.tiles.pop(ck)
        self.degraded.discard(ck)
        self.matslots.release(ck)
        # --colormatch: a later build must not reuse its texture's name (re-rendered, or rendered raw
        # for a region that may finish before the tile is built again: the listener misses it then)
        if info.stale or info.rev or info.cm_pending:
            self.tex_rev[ck] = info.rev + 1
        # through the rate-limited queue: the mod loaded most of these files once (P9)
        files = self.tile_files(self.tile_name(*ck), info.texture if info.rev else None, info.rev)
        if info.shape is not None and not info.shape.startswith("slot_"):   # pooled files stay (re-used)
            files.append(os.path.join(self.out_dir, info.shape))
        self.queue_delete((p for p in files if os.path.exists(p)), ck)

    def _cm_listener(self, rk, changed):
        """ColorMatch worker thread: a region's correction is ready."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._cm_ready, rk, changed)

    def _ds_listener(self, rk, changed):
        """Deshadow worker thread: a region's relight values are ready (changed: it relights)."""
        self._cm_listener((imagerymod.DS_PENDING,) + tuple(rk), changed)

    def _region_status(self, rk):
        """None (not ready), False (ready, changes nothing) or True of a pending region: a colour-match
        (rx, ry) or a de-shadow (DS_PENDING, rx, ry) one (event loop: memory and stat calls only)."""
        if len(rk) == 3 and rk[0] == imagerymod.DS_PENDING:
            return None if self.deshadow is None else self.deshadow.status(tuple(rk[1:]))
        return None if self.colormatch is None else self.colormatch.status(rk)

    def _cm_ready(self, rk, changed):
        """The far tiles whose textures were rendered without the region's correction (colour match or
        de-shadow): if it changes them they are stale. tile() renders the texture again (only the
        texture, under the next revision's name) whenever the tile is sent again, and a tile some mod
        shows (or holds, sent) is re-rendered live and swapped with far_retex (_retex_pump)."""
        for ck in sorted(self.cm_waiting.pop(rk, ())):
            self._cm_settle(ck, rk, changed, pump=False)
        self._retex_pump()                  # once all are queued: the nearest goes first

    def _cm_settle(self, ck, rk, changed, pump=True):
        info = self.tiles.get(ck)
        if info is None or rk not in info.cm_pending:
            return
        info.cm_pending = info.cm_pending - {rk}
        if changed:
            info.stale = True
            self.retex_request(ck, pump)

    # --- live retexture (far_retex) ---------------------------------------------------------
    def _retex_viewers(self, ck):
        """The connections that show the tile, or hold it (sent, not swapped in yet), under their
        active anchor (a previous anchor's tiles are on their way out)."""
        aid, key = ck
        return [c for c in self.connections if c.active is not None and self.far_ck(c.active, key) == ck
                and (ck in c.shown or key in c.sent)]

    def retex_request(self, ck, pump=True):
        """A stale tile to re-render live if some mod shows it (event loop)."""
        if self.retex_rate <= 0 or self.imagery is None:
            return
        self.retex_todo.add(ck)
        if pump:
            self._retex_pump()

    def _retex_pick(self):
        """The stale tile to re-render next: shown (or held) by a connection, nearest its player
        first. Drops the ones nobody shows (re-rendered when sent again), already re-rendering, or
        past RETEX_REPEAT re-renders in a row that came back stale at once."""
        best, bd = None, math.inf
        for ck in list(self.retex_todo):
            info = self.tiles.get(ck)
            viewers = self._retex_viewers(ck) if info is not None and info.stale else []
            if not viewers or ck in self.tile_inflight or (ck[0] not in self.anchors and ck[0] != GLOBAL_AID) \
                    or info.retex_repeat >= RETEX_REPEAT:
                self.retex_todo.discard(ck)
                if info is not None and info.stale and ck not in self.tile_inflight:
                    self.retex_stats["dropped"] += 1
                continue
            t = info.tile if info.tile is not None else farfield.tile_of(ck[1])
            d = min(t.distance(*c.view_focus) for c in viewers)
            if best is None or (d, ck) < (bd, best):
                best, bd = ck, d
        return best

    def _retex_pump(self):
        """Start live re-renders within the rate and in-flight bounds (event loop; never waits)."""
        if self._retex_timer is not None or self.loop is None or self.loop.is_closed():
            return
        while self.retex_todo and self.retex_busy < self.retex_inflight:
            wait = self._retex_next - time.monotonic()
            if wait > 0:
                self._retex_timer = self.loop.call_later(wait, self._retex_wake)
                return
            ck = self._retex_pick()
            if ck is None:
                return
            self.retex_todo.discard(ck)
            self._retex_next = time.monotonic() + 1.0 / self.retex_rate
            prev = self.tiles[ck]
            tile = prev.tile if prev.tile is not None else farfield.tile_of(ck[1])
            viewers = self._retex_viewers(ck)
            anchor = self.anchors.get(ck[0]) or self.anchors.get(viewers[0].active if viewers else None)
            if anchor is None:
                continue
            fut = self.tile_inflight[ck] = self.loop.run_in_executor(self.tile_pool, self.make_tile, anchor, tile, prev)
            fut.add_done_callback(lambda f, ck=ck: self._tile_done(ck, f))
            fut.add_done_callback(self._retex_done)
            self.retex_busy += 1
            self.retex_stats["started"] += 1

    def _retex_wake(self):
        self._retex_timer = None
        self._retex_pump()

    def _retex_done(self, fut):
        self.retex_busy -= 1
        if not fut.cancelled() and fut.exception() is not None:    # not the network (still stale): a bug, the disk
            self.retex_stats["failed"] = self.retex_stats.get("failed", 0) + 1
            if self.retex_stats["failed"] <= 3:
                log.warning("live retexture failed: %r", fut.exception())
        self._retex_pump()

    def _send_retex(self, ck, info):
        """far_retex to every connection showing (or holding) the tile: its new texture."""
        for c in self._retex_viewers(ck):
            c.send({"type": "far_retex", "anchorId": c.active if ck[0] == GLOBAL_AID else ck[0], "key": ck[1],
                    "texture": f"{self.vfs_dir}/{info.texture}"})
            self.retex_stats["sent"] += 1
            c.credits.poke(c)               # the new texture's sources (the corrected one names S2 too)

    def schedule_prune(self, keep_from):
        """prune(keep_from) after prune_delay seconds (once per keep_from)."""
        if keep_from in self._prune_scheduled or not any(a < keep_from for a in self.anchors):
            return
        self._prune_scheduled.add(keep_from)

        loop = asyncio.get_running_loop()

        def run():
            # the mod may still show a previous anchor's tiles long after far_done (it applies
            # far_removes only once every tile received before them is shown; a rebase reload
            # can take minutes at speed): a file deleted under a live TSStatic gets reloaded
            if any(a < keep_from for a in self.held_anchors()):
                loop.call_later(self.prune_recheck, run)
                return
            self._prune_scheduled.discard(keep_from)
            self.prune(keep_from)
        loop.call_later(self.prune_delay, run)

    def prune(self, keep_from):
        """Forget anchors older than keep_from and delete their files."""
        for aid in [a for a in self.anchors if a < keep_from]:
            del self.anchors[aid]
            self.horizon_grids.pop(aid, None)
            self.horizon_failed.pop(aid, None)
            for ck in [k for k in self.tiles if k[0] == aid]:
                del self.tiles[ck]
            for ck in [k for k in self.matslots.holders() if k[0] == aid]:
                self.matslots.release(ck)
            for ck in [ck for ck in list(self.degraded) if ck[0] == aid]:     # list(): workers add meanwhile
                self.degraded.discard(ck)
            for ck in [k for k in self.tex_rev if k[0] == aid]:
                del self.tex_rev[ck]
            self.retex_todo = {ck for ck in self.retex_todo if ck[0] != aid}
            for rk in list(self.cm_waiting):    # (a teleport: regions far from the new anchor never finish)
                self.cm_waiting[rk] = {ck for ck in self.cm_waiting[rk] if ck[0] != aid}
                if not self.cm_waiting[rk]:
                    del self.cm_waiting[rk]
            for fut in [f for k, f in self.tile_inflight.items() if k[0] == aid]:
                fut.cancel()
            for key in [k for k in self.hm if k[0] == aid]:
                if key not in self.hm_committed and key not in self.hm_sent:
                    del self.hm[key]
            keep = {self.hm_name(k) for k in self.hm} | {i["ter"] for i in self.hm.values() if i.get("ter")}
            doomed = []
            for p in glob.glob(os.path.join(self.out_dir, f"far_{aid}_*")):
                name = os.path.basename(p)
                doomed.append(p)
                if name.endswith(".dae"):
                    doomed.append(os.path.join(self.cdae_dir, os.path.splitext(name)[0].lower() + ".cdae"))
            doomed += glob.glob(os.path.join(self.cdae_dir, f"far_{aid}_*"))  # caches whose source went earlier
            doomed += [p for p in glob.glob(os.path.join(self.out_dir, f"hm_{aid}_*"))
                       if os.path.basename(p) not in keep]
            # the road / building / tree tiles too (2026-10-14: a night of teleports left 13k files in the
            # session folder, and the game's file watchers cost 4.5 ms a frame)
            for stem in ANCHOR_TILE_FILES:
                doomed += glob.glob(os.path.join(self.out_dir, f"{stem}_{aid}_*"))
            self.queue_delete(p for p in dict.fromkeys(doomed) if os.path.exists(p))


# Per-anchor tile files (besides far_ / hm_) that prune() deletes with their anchor
# A teleport's first block is built first without the road grading (make_provisional: the car is placed in ~5 s
# instead of 15-60 s on a fresh place) and replaced by the graded one when that is built (terrain_refresh)
PROVISIONAL_FIRST = os.environ.get("BNGE_PROVISIONAL", "1") != "0"
PROVISIONAL_KEEP_S = 60.0   # the provisional .ter is deleted this long after its replacement was sent
PADS_REFRESH_MS = 5.0       # the block in use built again with its gas station pads only below this speed (m/s) ...
PADS_SETTLE_S = 3.0         # ... and this long after the last new pad (Connection.pads_refresh)
NAV_WAIT_S = 30.0           # s an anchor's first AI graph waits at most for the road tiles round the car (nav_may_start)
TELEPORT_PAUSE_M = 20000.0  # an anchor asked this far from the player is a teleport: the old place's work pauses
TELEPORT_PAUSE_MAX_S = 120.0  # ... at most this long (the mod may never commit it)
BOOST_MOVE_MS = 3.0         # the loading boost ends when the player moves faster than this (m/s) ...
BOOST_MAX_S = 120.0         # ... or this long after the teleport
BOOST_GRACE_S = 5.0         # (the car dropped into place moves a little at first)

ANCHOR_TILE_FILES = ("roads", "road", "walks", "walkcol", "deck", "bld", "bldcol", "bldfar", "wire", "veg", "navgraph")


class _BoundedLru:
    """The dict interface farfield.horizon_grid() uses for its block-maximum cache, over the server's
    OrderedDict: LRU order, at most `size` entries (only the one horizon worker reads and writes it)."""

    def __init__(self, od, size):
        self.od, self.size = od, size

    def get(self, key):
        v = self.od.get(key)
        if v is not None:
            self.od.move_to_end(key)
        return v

    def __contains__(self, key):
        return key in self.od

    def __setitem__(self, key, value):
        self.od[key] = value
        while len(self.od) > self.size:
            self.od.popitem(last=False)


def route_profile_smooth(z):
    """A route's elevation samples (even steps) as the road climbs them: a running median of 5 (the ground under a
    bridge, a dip or bump of one or two samples where the road crosses a valley or an embankment, drew spikes: the
    user, 2026-10-27, "way to spikey even for a short drive"), then a running mean of 3. Pure."""
    z = np.asarray(z, np.float64)
    n = len(z)
    if n < 5:
        return z
    p = np.r_[z[:1], z[:1], z, z[-1:], z[-1:]]
    med = np.median(np.stack([p[k:k + n] for k in range(5)]), axis=0)
    q = np.r_[med[:1], med, med[-1:]]
    return (q[:-2] + q[1:-1] + q[2:]) / 3.0


class Connection:
    """One mod connection. Far-field state for the active anchor A:
        desired  D: the selected tiles (farfield.select)
        shown    C: tiles of executed swaps, (anchorId, key) -> bbox in A's frame; also holds
                    previous anchors' tiles after a rebase until they are covered
        sent     S: far_tile sent but not yet part of a far_swap (the mod keeps them hidden)
        groups:     farfield.swap_groups(C, D), recomputed whenever D changes
    A group is emitted (far_swap) once all its adds are sent; C then becomes C - remove + add.
    Sent tiles that no group needs any more are evicted with far_remove."""

    def __init__(self, server, reader, writer):
        self.s = server
        self.reader, self.writer = reader, writer
        self.loop = asyncio.get_running_loop()
        self.peer = writer.get_extra_info("peername")
        self.tasks = set()
        self.active = None              # committed anchor id
        self.block = None               # farfield.Block of the committed terrain
        self.pending_block = None       # the last prepare_terrain of the active anchor
        self.terrain_wait = 0           # prepare_terrain requests being built (roads.Tracker: the ground first)
        self.predicted_block = None     # the terrain the prefetch expects next
        self.focus = (0.0, 0.0)         # player x, y in the active frame
        self.view_at = None             # the camera's x, y there when it is VIEW_AWAY_M or more from the car, else None
        self.foci = ()                  # other vehicles' collision foci there ((x, y, r, trees), parse_foci)
        self.vel = (0.0, 0.0)
        self.cam = None                 # pose.cam (protocol 3), kept for diagnostics
        self.desired = {}               # key -> Tile (active anchor)
        self.refined = set()            # nodes refined by the last selection (hysteresis)
        self.node_changed = {}          # node -> monotonic time of its last state change
        self.shown = {}
        self.sent = {}                  # key -> Tile (active anchor)
        self.retired = {}               # (anchorId, key) -> monotonic time it left shown / sent
        self.held = set()               # pose.held: previous anchors whose tile objects the mod still has
        self.slot_holds = {}            # pooled slot -> serial of its latest far_tile the mod has not reported gone
        # a teleport's anchor is being built (prepare_anchor > TELEPORT_PAUSE_M away): no new work for the old place
        # (its far tiles, road / tree / building tiles, prefetch) until it commits or fails (2026-10-16: the anchor's
        # first block waited 30 s for the CPU after a level load, 3.6 s alone)
        self.teleport_pending = False
        self.sessions = set()           # hello / pose `sessions`: earlier runs' folders the mod still uses
        self.groups = []                # [(adds, removes)] still to emit
        # their swap deltas, computed off the event loop: (adds, removes) as tuples -> Future of (dIn, dOut)
        # (group_deltas on the loop stalled it for seconds at 90 m/s, and every message waited, 2026-10-12)
        self.deltas = {}
        self.queue = []                 # tiles to build, by priority
        self.qi = 0
        self.next_group = 1
        self.inflight = {}              # (anchorId, key) -> (asyncio future, Tile)
        self.prefetched = {}            # (anchorId, key) -> when its imagery prefetch was last asked for
        self.failed = set()
        self.dirty = False              # far_done owed once every group is emitted
        self.batch = [0.0, 0, 0]        # [start, tiles sent, swaps sent] of the current change, for the log
        self.prefetching = False
        self.wake = asyncio.Event()
        self.streamer = None
        self.last_status = ""           # text of the last status message (re-sent with a new backlog)
        self.note_sent = ""             # the network note (Server.net_note) the last status carried
        self.status_sent = (0.0, None)  # (monotonic time, backlog) of the last status message
        # M1h (horizon mode): the eye in m above sea level (quantised, see EYE_STEP; frame
        # independent, so it survives a rebase; None before the first pose), the far-field radius of
        # the last selection, the radius the last status carried, the region's highest visible terrain
        self.eye = None
        self.radius = farfield.HORIZON_MIN if server.horizon else server.radius
        self.radius_sent = None
        self.zmax_region = None
        self.vram_mb = None
        self.vram_profile = None
        self.weather = weathermod.Tracker()     # real weather: fetch rule, last message sent
        self.road_geo = False                   # road tiles as geotiles (hello.geoTiles, Server.roads_geo_ok)
        self.roads = roadsmod.Tracker()         # road tiles sent to the mod (--roads on)
        self.signs = signstream.Tracker()       # their highway signs (key "s<tx>_<ty>")
        self.signs_on = True                    # the mod's highwaySigns setting (message highway_signs)
        self.refining = set()                   # graded first blocks on their way (refine_first; send_progress)
        self.ground_wait = {}                   # block key -> when its photo was first seen waiting (ground_progress)
        self.veg = vegetationmod.Tracker()      # vegetation tiles sent to the mod (--trees on)
        self.veg_flat = False                   # ... as one flat array each (the mod's hello.vegFlat; trees.lua)
        # far buildings (--buildings on); none under the road tiles (they carry the buildings themselves)
        # the far (lite) buildings under the road tiles too, nearest first: they come in seconds after a teleport and the
        # mod hides each as its road tile's full buildings are made (roads.lua updateFar), the town round the car
        # drawn simply first and in detail after (the user, 2026-10-25: "load simpler versions first then higher detail
        # version later"); without them the far ones came first and the car's surroundings last
        # past them the skyline (buildings.skyline_tile): the far buildings then come as whole skyline squares
        # (group), the skyline tiles where none of theirs are (inner)
        self.far_geo = False                    # ... as geotiles (hello.farGeo, Server.far_geo_ok): kept across rebases
        self.global_far = False                 # far levels >= 3 as global tiles (hello.globalFar, --global-far)
        self.prev_horizon = None                # (frame, HorizonGrid) of the anchor before the active one (horizon_select)
        self.gpolys = {}                        # (anchorId, key) -> a shown global tile's quad in that anchor's world
        self.ppolys = {}                        # piece key -> its polygon (pieces sent / shown under the active anchor)
        self.building_scale = 1.0
        self.bldfar = self.bldsky = self.brfar = None
        self.make_far_trackers(False)
        self.nav = navgraphmod.Tracker()        # the AI road graph around the player (--traffic on)
        self.nav_wait = {}                      # anchor id -> monotonic time its first AI graph was wanted
        self.gravity = gravitymod.Tracker()     # real gravity: recompute rule, last message sent
        self.credits = creditsmod.Tracker()     # data credits: throttle, last message sent (--credits on)
        self.ground_busy = False                # a groundahead job is running (one at a time)
        self.route_dl_version = 0               # bumped by every route(): supersedes a running route_download job
        self.route_points = []                  # the last planned route's points ([lat, lon]: a saved route's)
        self.sea_busy = False                   # a water-tile plan is running (sea_tiles)
        self.sea_checked = None                 # (anchor id, 512 m square, monotonic, complete) of the last plan
        self.sea_sent = None                    # (anchor id, tiles) of the last "sea" message
        self.base_busy = False                  # a base photo refresh is running (base_refresh)
        self.pads_busy = False                  # a block is being built again with its new pads (pads_refresh)
        self.roads_busy = False                 # ... with the roads of map cells that came after it (roads_refresh)
        self.ground_sent = None                 # (anchor id, z, monotonic) of the last ground_ahead sent
        self.following = True                   # the mod moves its block (pose "follow"; false high up)
        # the mod's graphics settings (message 'graphics', after every hello and on a change; set_graphics): the far
        # field's detail (lodScale times the layout's K: the distance a level splits at), the tree tiles' reach
        # (treeScale) and the far buildings', skyline's and far bridges' (buildingScale); 1 = the defaults
        self.lod_scale = self.tree_scale = self.building_scale = 1.0
        self.view_cap = None                    # the far field's radius at most (m): the mod's fixed view distance

    async def map_tiles(self, msg):
        """The world map's tiles {"tiles": [[z, x, y, layer?], ...]} (layer "sat" by default, "map", "labels"; the
        most wanted first): each written once under <user>/temp/beamngEarth/maptiles/ (maptiles.tile_path; the
        game's UI loads it from there: its browser cancels every http request, even to 127.0.0.1) and said as soon
        as it is there, map_tiles_ready {"tiles": [[z, x, y, ok, layer]]}. Built maptiles.FILE_WORKERS at a time
        (one by one, a 48-tile view took ~10 s and was said only at the end, 2026-10-16); each request replaces
        the last one's queue (maptiles.TileFiles; an empty list cancels)."""
        mt = getattr(self.s, "map_tiles", None)
        if mt is None:
            return
        want = []
        tiles = msg.get("tiles")
        if not isinstance(tiles, list):         # (an empty Lua table may arrive as {})
            tiles = []
        for t in tiles[:maptilesmod.MAX_FILES_REQUEST]:
            try:
                z, x, y = maptilesmod.check_tile(*t[:3])
                layer = t[3] if len(t) > 3 else "sat"
            except (TypeError, ValueError, IndexError):
                continue
            if isinstance(layer, str) and layer in maptilesmod.LAYERS:
                want.append((layer, z, x, y))
        files = self.s.map_files
        if files is None:
            files = self.s.map_files = maptilesmod.TileFiles(mt, os.path.join(self.s.root, "maptiles"))
        loop, send = self.loop, self.send

        def ready(key, ok):                     # (a worker thread) the connection that asked last hears of it
            layer, z, x, y = key
            loop.call_soon_threadsafe(send, {"type": "map_tiles_ready", "tiles": [[z, x, y, ok, layer]]})
        files.on_ready = ready
        files.request(want)

    async def route(self, msg):
        """{"id", "lat0", "lon0", "lat1", "lon1", "download"?} -> route_result {"id", "points": [[lat, lon]...], "dist",
        "dur", "elev": [[dist_m, elev_m], ...]?, "error"?}. Also (re)starts this route's corridor prefetch
        (route_download, routeahead.py; download: "off", "ahead" (ROUTE_AHEAD_MAX_M, the default) or "route" (the
        whole route, up to ROUTE_WHOLE_MAX_M))."""
        self.route_dl_version += 1
        version = self.route_dl_version
        out = {"type": "route_result", "id": msg.get("id"), "points": [], "dist": 0.0, "dur": 0.0}
        try:
            r = await asyncio.get_running_loop().run_in_executor(
                self.s.ui_pool, routingmod.plan, float(msg["lat0"]), float(msg["lon0"]), float(msg["lat1"]),
                float(msg["lon1"]), self.s.net, msg.get("bearing"))
            out.update(r)
        except Exception as e:          # offline, no route, rate limited: the mod falls back to steering
            out["error"] = f"{type(e).__name__}: {e}"[:200]
        if "error" not in out and self.s.dem is not None and out["points"]:
            try:                        # the map's elevation profile: a nice-to-have, the route works without it
                out["elev"] = await asyncio.get_running_loop().run_in_executor(
                    self.s.ui_pool, self._route_elevation, out["points"])
            except Exception as e:
                log.debug("route elevation: %s: %s", type(e).__name__, e)
        log.info("route %d (%s, %s -> %s, %s): %d points, %.1f km%s", int(msg.get("id") or 0), msg.get("lat0"),
                 msg.get("lon0"), msg.get("lat1"), msg.get("lon1"), len(out["points"]), out["dist"] / 1000,
                 (" (" + out["error"] + ")") if "error" in out else "")
        self.send(out)
        self.route_points = out["points"] if "error" not in out else []
        mode = msg.get("download")
        self.route_download(version, out["points"], mode if mode in ROUTE_DOWNLOAD_MODES else "ahead")

    def _route_elevation(self, points):
        """The route's elevation profile for the map (routing.resample onto even steps, dem.sample): [[dist_m,
        elev_m], ...]. Blocking (run_in_executor): called from route() only."""
        aid = self.active
        zoom = self.s.anchors[aid].hm_zoom if aid in self.s.anchors else demmod.FINE_ZOOM
        pts = routingmod.resample([(p[0], p[1]) for p in points])
        lats = np.array([p[0] for p in pts])
        lons = np.array([p[1] for p in pts])
        # (tiles about 4 steps wide, no finer than the terrain's: at the terrain's zoom a long route's ~120 points
        # each fetched their own tile before the route reached the map; a 220 px chart needs no more)
        step = pts[-1][2] / max(len(pts) - 1, 1) if len(pts) > 1 else 0.0
        if step > 0:
            tile_m = 40075016.7 * max(math.cos(math.radians(float(np.mean(lats)))), 0.05)
            zoom = int(min(zoom, max(8, math.floor(math.log2(tile_m / (4 * step))))))
        elev = route_profile_smooth(self.s.dem.sample(lats, lons, zoom))
        return [[round(p[2], 1), round(float(e), 1)] for p, e in zip(pts, elev)]

    def route_download(self, version, points, mode="ahead"):
        """Prefetch the route's corridor (elevation tiles and road/building map cells) up to
        ROUTE_AHEAD_MAX_M ahead (mode "ahead"; "route": ROUTE_WHOLE_MAX_M, "off": none), at "prefetch" priority,
        pushing route_download {total, done, active} (throttled like Server._progress) until it finishes or a newer
        route() supersedes it (checked by version, since self.route_pool's single worker already serializes a newer
        job behind this one)."""
        empty = {"type": "route_download", "total": 0, "done": 0, "active": False}
        if not ROUTE_AHEAD_ON or mode == "off" or self.s.dem is None or len(points) < 2:
            self.send(empty)
            return
        aid = self.active
        zoom = self.s.anchors[aid].hm_zoom if aid in self.s.anchors else demmod.FINE_ZOOM
        reach = self.s.layout.block * heightmap.SQUARE / 2 + AHEAD_MARGIN_M
        layers = (AHEAD_LAYERS if self.s.buildings else ("roads",)) if self.s.mapdata is not None else ()
        pts = routeaheadmod.arc_length_points(points, ROUTE_WHOLE_MAX_M if mode == "route" else ROUTE_AHEAD_MAX_M)
        dem_keys, cell_layer_pairs = routeaheadmod.need(self.s.dem, self.s.mapdata, pts, reach, zoom, layers)
        total = len(dem_keys) + len(cell_layer_pairs)
        if total == 0:
            self.send(empty)
            return
        dem, mapdata = self.s.dem, self.s.mapdata

        def run():
            # counted once, up front: an already-cached key is skipped below (has() is re-checked only to
            # decide what to fetch), never re-counted, or a route with nothing left to do would inflate past
            # total and spam a push every iteration (push's own throttle only skips while done < total)
            done = routeaheadmod.done_count(dem, mapdata, dem_keys, cell_layer_pairs)
            last_push = [0.0]

            def push(active):
                now = time.monotonic()
                if active and done < total and now - last_push[0] < 0.25:
                    return
                last_push[0] = now
                self.loop.call_soon_threadsafe(
                    self.send, {"type": "route_download", "total": total, "done": done, "active": active})
            if done >= total:           # the whole corridor is already cached: nothing to show a bar for
                push(False)
                return
            push(True)
            for z, x, y in dem_keys:
                if self.route_dl_version != version:
                    return
                if dem.has(z, x, y):
                    continue
                try:
                    got, deferred = dem.prefetch([(z, x, y)])
                except Exception as e:
                    log.debug("route download: %s: %s", type(e).__name__, e)
                    continue
                if deferred:             # no spare tokens now: wait rather than give up on the corridor
                    time.sleep(ROUTE_RETRY_S)
                    continue
                done += got
                push(True)
            for key, layer in cell_layer_pairs:
                if self.route_dl_version != version:
                    return
                if mapdata.has(key, layer):
                    continue
                try:
                    mapdata.cell(key, layers=(layer,), kind="prefetch")
                    done += 1
                    push(True)
                except mapdatamod.DEFERRED:
                    time.sleep(ROUTE_RETRY_S)
                except Exception as e:
                    log.debug("route download: %s: %s", type(e).__name__, e)
            push(False)
        self.s.route_pool.submit(run)

    async def server_list(self):
        """The BeamNG.Earth server list (native/serverlist, SERVER_LIST_URL) for the mod's tab in BeamMP's server browser
        (serverlist.lua): fetched here, as the game's Lua may only open connections to local addresses ("connect
        restricted"). A 'server_list' message with the servers, or with an error."""
        def get():
            return json.loads(self.s.net.get(SERVER_LIST_URL, kind="demand").body.decode("utf-8", "replace"))
        try:
            servers = await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, get)
        except Exception as e:          # offline, the list down: said in the tab
            log.info("server list: %s: %s", type(e).__name__, e)
            self.send({"type": "server_list", "error": "the server list did not answer"})
            return
        self.send({"type": "server_list", "servers": servers if isinstance(servers, list) else []})

    async def check_update(self):
        """The update check after a hello: an 'update' message when GitHub has a newer version (updates.py); offline or
        up to date: nothing."""
        try:
            r = await asyncio.get_running_loop().run_in_executor(
                self.s.ui_pool, updatesmod.check, updatesmod.local_version(), self.s.net)
        except Exception as e:          # never a reason to drop the connection
            log.debug("update check: %s: %s", type(e).__name__, e)
            return
        if r:
            log.info("update available: %s -> %s (%s)", r["current"], r["latest"], r["url"])
            self.send({"type": "update", **r})

    async def update_now(self):
        """The update notice's "Update now": the latest release's file for this packaged app downloaded beside it and
        checked (selfupdate.download), with update_status {state: progress | ready | error, text, done, total}."""
        import selfupdate
        loop = asyncio.get_running_loop()
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                loop.call_soon_threadsafe(self.send, {"type": "update_status", "state": "progress", "done": done,
                                                      "total": total, "text": "Downloading the update..."})
        self.send({"type": "update_status", "state": "progress", "done": 0, "total": 0, "text": "Asking GitHub..."})
        try:
            version, path = await loop.run_in_executor(self.s.ui_pool, selfupdate.download, self.s.net,
                                                       updatesmod.local_version(), None, progress)
        except selfupdate.UpdateError as e:
            log.info("update now: %s", e)
            self.send({"type": "update_status", "state": "error", "text": str(e)})
            return
        except Exception as e:          # never a reason to drop the connection
            log.warning("update now: %s: %s", type(e).__name__, e)
            self.send({"type": "update_status", "state": "error", "text": f"{type(e).__name__}: {e}"[:200]})
            return
        log.info("update now: %s downloaded to %s", version, path)
        self.send({"type": "update_status", "state": "ready", "version": version,
                   "text": f"BeamNG.Earth {version} is downloaded. Close the game: the next time you start "
                           f"BeamNG.Earth it starts {os.path.basename(path)}."})

    async def place_previews(self, msg):
        """{"places": [{"key", "lat", "lon"}]} -> place_previews_ready {"keys"}: each place's picture for the level
        select, written into the game's user folder (placepreview.write) when the server has imagery."""
        items = [(p.get("key"), p.get("lat"), p.get("lon")) for p in (msg.get("places") or []) if isinstance(p, dict)]
        if not items or self.s.imagery is None:
            return
        try:
            keys = await asyncio.get_running_loop().run_in_executor(
                self.s.ui_pool, placepreviewmod.write, self.s.imagery, self.s.user_dir, items[:PLACE_PREVIEWS_MAX])
        except Exception as e:
            log.warning("place previews: %s: %s", type(e).__name__, e)
            return
        self.send({"type": "place_previews_ready", "keys": keys})

    async def random_place(self, msg):
        """{"id"} -> random_place_result {"id", "lat", "lon", "name"} on a road near a random town (randomplace.pick),
        or without lat / lon when none was found (the mod then starts where it would have)."""
        out = {"type": "random_place_result", "id": msg.get("id")}
        md = getattr(self.s, "mapdata", None)
        if md is not None:
            try:
                got = await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, randomplacemod.pick, md)
            except Exception as e:
                log.warning("random place: %s: %s", type(e).__name__, e)
                got = None
            if got is not None:
                out["lat"], out["lon"], out["name"] = got
                log.info("random place: %s (%.5f, %.5f)", got[2], got[0], got[1])
        self.send(out)

    async def where(self, msg):
        """The status bar's location {"id", "lat", "lon", "online"} -> where_result {"id", "street", "place", "region",
        "country"}: the street from the cached map cells (never waits for a download), the town, state and country from
        Photon's reverse geocoding with the position rounded to ~1 km, only when "online" (the mod's setting), and
        asked again only when that rounded position changed."""
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
        except (KeyError, TypeError, ValueError):
            return
        out = {"type": "where_result", "id": msg.get("id"), "street": ""}
        loop = asyncio.get_running_loop()
        if self.s.surface is not None:
            def street():
                layers, _ = self.s.surface.roads(lat, lon)
                return geocodemod.nearest_street(layers, lat, lon)
            try:
                out["street"] = await loop.run_in_executor(self.s.ui_pool, street)
            except Exception as e:      # a cell being written, an odd feature: no street this time
                log.debug("where: street: %s: %s", type(e).__name__, e)
        if msg.get("online"):
            key = (round(lat, geocodemod.REVERSE_ROUND), round(lon, geocodemod.REVERSE_ROUND))
            cached = getattr(self, "_where_place", None)
            if cached is None or cached[0] != key:
                try:
                    place = await loop.run_in_executor(self.s.ui_pool, geocodemod.reverse, lat, lon, self.s.net,
                                                       self.s.country_at(lat, lon))
                    self._where_place = (key, place)
                    cached = self._where_place
                except Exception as e:  # offline, rate limited: the bar shows the street alone
                    log.debug("where: place: %s: %s", type(e).__name__, e)
            if cached is not None and cached[0] == key:
                out.update(cached[1])
        self.send(out)

    async def geocode(self, msg):
        """The map's search {"id", "q", "lat"?, "lon"?} -> geocode_result {"id", "results": [...], "error"?}."""
        q = str(msg.get("q") or "")[:200]
        lat, lon = msg.get("lat"), msg.get("lon")
        out = {"type": "geocode_result", "id": msg.get("id"), "results": []}
        try:
            out["results"] = await asyncio.get_running_loop().run_in_executor(
                self.s.ui_pool, geocodemod.search, q, lat, lon, self.s.net,
                self.s.country_at(lat, lon) if lat is not None and lon is not None else None)
        except Exception as e:          # offline, rate limited, a bad answer: the map says so
            out["error"] = f"{type(e).__name__}: {e}"[:200]
        log.debug("geocode %r: %d results%s", q, len(out["results"]), (" (" + out["error"] + ")") if "error" in out else "")
        self.send(out)

    async def cache_map(self, msg):
        """The map's cache view {"id", "z", "x0", "y0", "x1", "y1", "mode"?} -> cache_map_result {"id", "z", "x0",
        "y0", "w", "h", "cells" (base64, a level per cell), "gen", "ready", "error"?} (covermap.CoverMap.query)."""
        out = {"type": "cache_map_result", "id": msg.get("id"), "z": 0, "x0": 0, "y0": 0, "w": 0, "h": 0, "cells": "",
               "gen": 0}
        cm = self.s.covermap
        if cm is None:
            out["error"] = "no cache view (no download cache)"
        else:
            try:
                out.update(await asyncio.get_running_loop().run_in_executor(
                    None, cm.query, msg.get("z"), msg.get("x0"), msg.get("y0"), msg.get("x1"),
                    msg.get("y1"), msg.get("mode") or "summary"))
            except ValueError as e:
                out["error"] = str(e)[:200]
        self.send(out)

    def _area_route(self, msg):
        """A saved route's points (area_plan / area_add with "route": "current": the last planned GPS route) as
        (lats, lons), or None (a box)."""
        if msg.get("route") != "current":
            return None
        pts = self.route_points or []
        return [p[0] for p in pts], [p[1] for p in pts]

    async def area_plan(self, msg):
        """{"id", "s", "w", "n", "e"} or {"id", "route": "current", "half"} -> area_plan_result {"id", "ok", km2, items,
        missing, downloadBytes, diskBytes, seconds, days, hosts, excluded, km? (a route's length), "error"?}
        (areas.Areas.plan / plan_route: no network)."""
        out = {"type": "area_plan_result", "id": msg.get("id"), "ok": False}
        if self.s.areas is None:
            out["error"] = "saved areas need the download cache"
        else:
            try:
                route = self._area_route(msg)
                mode = msg.get("mode") if isinstance(msg.get("mode"), str) else areasmod.DEFAULT_MODE
                if route is not None:
                    out.update(await asyncio.get_running_loop().run_in_executor(
                        None, self.s.areas.plan_route, route[0], route[1], msg.get("half"), mode))
                else:
                    out.update(await asyncio.get_running_loop().run_in_executor(
                        None, self.s.areas.plan, msg.get("s"), msg.get("w"), msg.get("n"), msg.get("e"), mode))
            except Exception as e:      # (the box stays without an estimate)
                out.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
        self.send(out)

    async def area_add(self, msg):
        """{"id", "s", "w", "n", "e", "name"?} or {"id", "route": "current", "half", "name"?} -> areas {..., "result":
        {"id", "ok", "area" or "error"}}."""
        if self.s.areas is None:
            self.send({"type": "areas", "areas": [], "result": {"id": msg.get("id"), "ok": False,
                                                                "error": "saved areas need the download cache"}})
            return
        name = msg.get("name")
        name = name if isinstance(name, str) else ""
        mode = msg.get("mode") if isinstance(msg.get("mode"), str) else areasmod.DEFAULT_MODE
        route = self._area_route(msg)
        if route is not None:
            ok, info = await asyncio.get_running_loop().run_in_executor(
                None, self.s.areas.add_route, route[0], route[1], msg.get("half"), name, mode)
        else:
            ok, info = await asyncio.get_running_loop().run_in_executor(
                None, self.s.areas.add, msg.get("s"), msg.get("w"), msg.get("n"), msg.get("e"), name, mode)
        res = {"id": msg.get("id"), "ok": ok, ("area" if ok else "error"): info}
        self.send(self.s.areas_message(res))

    async def area_cmd(self, msg):
        """{"id", "area", "op": pause | resume | delete} -> areas {..., "result": {"id", "ok"}}."""
        if self.s.areas is None:
            return
        area, op = msg.get("area"), msg.get("op")
        ok = isinstance(area, str) and isinstance(op, str) and await asyncio.get_running_loop().run_in_executor(
            None, self.s.areas.command, area, op)
        self.send(self.s.areas_message({"id": msg.get("id"), "ok": bool(ok)}))

    def send(self, msg):
        if not self.writer.is_closing():
            self.writer.write(protocol.encode(msg))

    def backlog(self):
        """status.backlog: desired tiles of the current anchor that are not shown yet (V1_4_SPEC §4)."""
        aid = self.active
        if aid is None:
            return 0
        return sum(1 for k in self.desired if self.ck(k) not in self.shown)

    def status(self, text):
        n = self.backlog()
        self.last_status, self.status_sent = text, (time.monotonic(), n)
        note = self.note_sent = self.s.net_note()
        if note:                        # offline / degraded: the mod shows the status text as it is
            text = f"{text} | {note}" if text else note
        m = {"type": "status", "text": text, "backlog": n}
        if self.s.horizon:              # M1h: the far-field radius (the mod's visibleDistance follows it)
            m["radius"] = round(self.radius)
            self.radius_sent = self.radius
        self.send(m)

    def send_progress(self, aid):
        """The loading line's counts (the mod's loadprogress.lua): (done, wanted) of the road tiles, the tree tiles and
        the far tiles round the player, sent when they change, at most every PROGRESS_S."""
        roads = self.roads.progress() if self.s.mapdata is not None else (0, 0)
        trees = self.veg.progress() if self.s.veg is not None else (0, 0)
        want = len(self.desired)
        far = (want - self.backlog(), want)
        counts = (self.ground_progress(aid), roads, trees, far)
        now = time.monotonic()
        if counts == getattr(self, "progress_sent", None) or now - getattr(self, "progress_at", 0.0) < PROGRESS_S:
            return
        self.progress_sent, self.progress_at = counts, now
        self.send({"type": "load_progress", "anchorId": aid, "ground": list(counts[0]), "roads": list(roads),
                   "trees": list(trees), "far": list(far)})

    def ground_progress(self, aid):
        """(1, 1) once the ground under the player is final, else (0, 1): a teleport's graded first block still on its
        way (refine_first), or the block in use waiting to be drawn again with its full photo (a quick one without the
        painted roads, or one without all its imagery: base_refresh), for GROUND_WAIT_S at most (an imagery source
        that does not come back must not hold the loading line)."""
        if any(k[0] == aid for k in self.refining):
            return 0, 1
        s = self.s
        key = next((k for k in reversed(s.hm_committed) if k[0] == aid), None)
        if key is None or s.near_visual != "detail" or not (key in s.base_stale or self.base_busy):
            return 1, 1
        since = self.ground_wait.setdefault(key, time.monotonic())
        return (1, 1) if time.monotonic() - since > GROUND_WAIT_S else (0, 1)

    def refresh_status(self):
        """Re-send the last status when the network note changed (a host went down or came back);
        --status-interval: also when the backlog changed, at most every status_interval seconds
        (off by default: v1.3.1 sends status on events only)."""
        if self.s.net_note() != self.note_sent:
            self.status(self.last_status)
            return
        iv = self.s.status_interval
        if iv <= 0 or time.monotonic() - self.status_sent[0] < iv:
            return
        if self.backlog() != self.status_sent[1]:
            self.status(self.last_status)

    async def run(self):
        log.info("client connected %s", self.peer)
        self.s.connections.add(self)
        self.streamer = asyncio.create_task(self.stream())
        try:
            while True:
                msg = await protocol.read_message(self.reader)
                if msg is None:
                    break
                try:
                    self.dispatch(msg)
                except (KeyError, TypeError, ValueError) as e:  # a malformed field must not drop the connection
                    log.warning("client %s: bad %r message: %s: %s", self.peer, msg.get("type"), type(e).__name__, e)
                except Exception:
                    log.exception("client %s: message %r failed", self.peer, msg.get("type"))
                await self.writer.drain()
        except (protocol.ProtocolError, ConnectionError) as e:
            log.warning("client %s: %s", self.peer, e)
        finally:
            self.route_dl_version += 1          # (a running route_download stops: no one to show its bar to)
            self.s.connections.discard(self)
            self.s.slots_let_go(list(self.slot_holds))
            self.slot_holds = {}
            for tr in (self.roads, self.signs, self.veg, self.bldfar, self.bldsky, self.brfar, self.nav):
                if tr is not None:
                    tr.close()
            self.s.drop_claims(self)
            self.streamer.cancel()
            self.credits.close()
            for t in list(self.tasks):
                t.cancel()
            self.writer.close()
            log.info("client disconnected %s", self.peer)

    def build_veg_tile(self, aid, tx, ty):
        """Server.build_veg_tile in this mod's format (veg_flat). The vegetation tracker's worker threads."""
        return self.s.build_veg_tile(aid, tx, ty, flat=self.veg_flat)

    def make_far_trackers(self, geo):
        """The far buildings', skyline's and far bridges' trackers (closing the old ones): the anchor's tiles, or
        geotiles (geo: far buildings in the road geotiles' grid sent as whole skyline squares, the skyline's and far
        bridges' squares at geotiles.SKY_Z / BRIDGE_Z), each at the building scale's reach (set_graphics)."""
        import geotiles
        for tr in (self.bldfar, self.bldsky, self.brfar):
            if tr is not None:
                tr.close()
        self.far_geo = bool(geo)
        bld = self.building_scale
        far_m = buildingsmod.FAR_SEND_M * bld
        sky_m = buildingsmod.SKY_SEND_M * bld
        br_m = buildingsmod.BRIDGE_FAR_SEND_M * bld
        far_drop = far_m + (buildingsmod.FAR_DROP_M - buildingsmod.FAR_SEND_M)
        sky_drop = sky_m + (buildingsmod.SKY_DROP_M - buildingsmod.SKY_SEND_M)
        br_drop = br_m + (buildingsmod.BRIDGE_FAR_DROP_M - buildingsmod.BRIDGE_FAR_SEND_M)
        # far buildings (--buildings on); none under the road tiles (they carry the buildings themselves)
        # the far (lite) buildings under the road tiles too, nearest first: they come in seconds after a teleport and
        # the mod hides each as its road tile's full buildings are made (roads.lua updateFar), the town round the car
        # drawn simply first and in detail after (the user, 2026-10-25: "load simpler versions first then higher
        # detail version later"); without them the far ones came first and the car's surroundings last
        # past them the skyline (buildings.skyline_tile): the far buildings then come as whole skyline squares
        # (group), the skyline tiles where none of theirs are (inner)
        sky = buildingsmod.SKY_SEND_M > buildingsmod.FAR_SEND_M
        bridges = buildingsmod.BRIDGE_FAR_SEND_M > roadsmod.SEND_M
        if geo:
            self.bldfar = vegetationmod.Tracker(far_m, far_drop, buildingsmod.FAR_PER_POSE, "bldfar", geo=True,
                                                z=geotiles.FAR_Z, group_z=geotiles.SKY_Z if sky else None)
            self.bldsky = (vegetationmod.Tracker(sky_m, sky_drop, buildingsmod.SKY_PER_POSE, "bldsky", geo=True,
                                                 z=geotiles.SKY_Z, inner=(far_m, buildingsmod.SKY_UNIT))
                           if sky else None)
            # (the far bridges' squares under the road tiles too: a square's pieces past their reach are wanted, the
            # mod hides each piece where its road geotile shows its own deck)
            self.brfar = (vegetationmod.Tracker(br_m, br_drop, buildingsmod.BRIDGE_FAR_PER_POSE, "brfar", geo=True,
                                                z=geotiles.BRIDGE_Z) if bridges else None)
            return
        self.bldfar = vegetationmod.Tracker(far_m, far_drop, buildingsmod.FAR_PER_POSE, "bldfar",
                                            group=buildingsmod.SKY_UNIT if sky else None)
        self.bldsky = (vegetationmod.Tracker(sky_m, sky_drop, buildingsmod.SKY_PER_POSE, "bldsky",
                                             unit=buildingsmod.SKY_UNIT, inner=(far_m, buildingsmod.SKY_UNIT))
                       if sky else None)
        # the bridges past the road tiles (buildings.far_bridge_tile): none built under the road tiles' own reach
        self.brfar = (vegetationmod.Tracker(br_m, br_drop, buildingsmod.BRIDGE_FAR_PER_POSE, "brfar",
                                            skip_m=roadsmod.SEND_M - roadsmod.TILE) if bridges else None)

    def build_far_geo(self, kind):
        """The far geotile tracker's build(gx, gy) of kind (Server.build_far_geo)."""
        return lambda gx, gy: self.s.build_far_geo(kind, gx, gy)

    def build_veg_geo(self, gx, gy):
        """Server.build_veg_geo in this mod's format (veg_flat): the geotile tracker's worker threads."""
        return self.s.build_veg_geo(gx, gy, flat=self.veg_flat, aid=self.active)

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def dispatch(self, msg):
        kind = msg["type"]
        if kind == "hello":
            lay = self.s.layout
            fmt = msg.get("format", lay.format)
            if msg.get("protocol") != PROTOCOL:
                self.status(f"protocol mismatch: mod {msg.get('protocol')}, server {PROTOCOL}")
            elif not isinstance(fmt, (int, float)) or fmt < lay.format:
                # the mod's format is the newest it reads (a FORMAT 4 mod reads 3 too)
                self.status(f"format mismatch: mod {msg.get('format')}, server {lay.format}")
            vram_mb = msg.get("vramMB")
            prof = budgetmod.vram_profile(vram_mb)
            self.vram_mb = vram_mb
            self.vram_profile = prof["name"]
            self.roads.send_m = prof["roads_send"]
            self.roads.drop_m = prof["roads_drop"]
            self.veg.send_m = prof["veg_send"]
            self.veg.drop_m = prof["veg_drop"]
            self.bldfar.send_m = prof["bldfar_send"]
            self.bldfar.drop_m = prof["bldfar_drop"]
            if self.bldsky is not None:
                self.bldsky.inner = (prof["bldfar_send"], buildingsmod.SKY_UNIT)
            if self.brfar is not None:
                self.brfar.skip_m = prof["roads_send"] - roadsmod.TILE
            log.info("client %s: VRAM %s MB -> profile %s (roads: %.0fm, veg: %.0fm, bldfar: %.0fm)",
                     self.peer, vram_mb, prof["name"], prof["roads_send"], prof["veg_send"], prof["bldfar_send"])
            m = {"type": "hello", "protocol": PROTOCOL, "format": lay.format, "serverVersion": SERVER_VERSION,
                 "appVersion": updatesmod.local_version() or "",     # the release (VERSION): the players' reports
                 "sessionId": self.s.sid, "farFormat": self.s.far_format, "material": farfield.MATERIAL,
                 "nearVisual": self.s.near_visual, "attribution": self.s.attribution(),
                 "vramProfile": prof["name"]}
            if self.s.imagery is not None:      # the world map credits the sources its view shows
                m["imageryCredits"] = self.s.imagery.credits_by_area()
                m["otherCredits"] = self.s.attribution_rest()
            if lay.format >= 4:         # M1: the tiles' cell count (crossover.defaultErr, the exact level)
                m["cells"] = lay.cells
            if lay.block != heightmap.SIZE:     # M4: the terrain block size (the mod's lattice / follow table)
                m["blockSize"] = lay.block
            if self.s.horizon:                  # M1h: status.radius follows; the mod sets its sky and view distance
                m["horizon"] = True
            if self.s.deshadow is not None:     # --deshadow lambert|full: shown on the mod's settings page
                m["deshadow"] = self.s.deshadow.mode
            if self.s.colormatch is not None:   # --colormatch s2: likewise
                m["colormatch"] = self.s.colormatch.mode
            season_mode = getattr(self.s.imagery, "season_mode", "off") if self.s.imagery is not None else "off"
            if season_mode not in (None, "off"):    # --season: the mod tints the trees' leaves to match (seasonlook.lua)
                m["season"] = season_mode
            if self.s.map_port:                 # the world map's tiles (maptiles.py; --map-tiles on)
                m["mapPort"] = self.s.map_port
            if self.s.mapdata is not None:      # our shapes' materials, set up by the mod now (prewarm.py)
                if self.s.prewarm_vfs is None:
                    try:
                        self.s.prewarm_vfs = prewarmmod.write(self.s.out_dir, self.s.vfs_dir)
                    except Exception as e:      # (nothing to prewarm: the first tiles set them up)
                        log.info("prewarm shape: %s", e)
                        self.s.prewarm_vfs = ""
                if self.s.prewarm_vfs:
                    m["prewarm"] = self.s.prewarm_vfs
            # a mod reconnecting to this server: the pooled slots its objects still hold (matslots.py)
            if msg.get("slotSession") == self.s.sid and isinstance(msg.get("slotsHeld"), list):
                for g in msg["slotsHeld"]:
                    if isinstance(g, list) and len(g) == 2 and isinstance(g[0], (int, float)):
                        self.slot_holds[int(g[0])] = g[1]
            # a game that outlived its server: keep the session folders its tiles still use
            self.s.claim_sessions(self, msg.get("sessions"))
            # a mod that reads a vegetation tile as one flat array of numbers (no table per item in its Lua: trees.lua);
            # an older one gets the rows
            self.veg_flat = msg.get("vegFlat") is True
            # a mod that places geotiles itself (hello.geoTiles: geotile.lua's zoom) gets the vegetation in them, kept
            # across rebases (--geo-tiles on); an older one, or --geo-tiles off, the anchor's tiles
            import geotiles
            geo = bool(self.s.geo_tiles) and msg.get("geoTiles") == geotiles.Z
            if geo != self.veg.geo:
                old = self.veg
                old.close()
                self.veg = vegetationmod.Tracker(old.send_m, old.drop_m, geo=geo)
            if geo:
                m["geoTiles"] = geotiles.Z
            # ... and the road tiles too when it places those (hello.roadGeo: roads.lua's ROAD_GEO; content kept across
            # rebases, a skin per anchor: roads.GeoTracker)
            road_geo = geo and msg.get("roadGeo") == roadsmod.ROAD_GEO and self.s.roads_geo_ok()
            if road_geo != self.road_geo:
                self.road_geo = road_geo
                self.roads.close()
                self.roads = road_tracker(self)
            if road_geo:
                m["roadGeo"] = roadsmod.ROAD_GEO
            # ... and the far buildings, skyline and far bridges when it places those too (hello.farGeo: roads.lua's
            # FAR_GEO; a far tile and the road geotile of the same key hold the same buildings: one shows them)
            far_geo = road_geo and msg.get("farGeo") == buildingsmod.FAR_GEO and self.s.far_geo_ok()
            if far_geo != self.far_geo:
                self.make_far_trackers(far_geo)
            if far_geo:
                m["farGeo"] = buildingsmod.FAR_GEO
            # ... and the far field's levels from 3 up as global tiles when it places those (hello.globalFar:
            # farfield.lua's GLOBAL_FAR; geotiles phase 3b, --global-far): built once, kept across rebases
            self.global_far = (geo and msg.get("globalFar") == farfield.GLOBAL_PROTO and bool(self.s.global_far)
                               and self.s.layout.mesh)
            if self.global_far:
                m["globalFar"] = farfield.GLOBAL_PROTO
            self.send(m)
            if self.weather.msg is not None:        # a reconnected mod gets the weather again
                self.send(self.weather.msg)
            if self.gravity.msg is not None:        # ... and its gravity
                self.send(self.gravity.msg)
            if self.credits.msg is not None:        # ... and the data credits
                self.send(self.credits.msg)
            if self.s.net_note():                   # --offline / hosts down: say so at once
                self.status(self.last_status)
            if self.s.cache is not None:            # the download cache's usage (the settings page)
                self.send(self.s.cache.message())
            if self.s.areas is not None:            # the saved areas (the map's Places panel)
                self.send(self.s.areas_message())
            if self.s.update_check:
                self.spawn(self.check_update())     # a newer version on GitHub: the mod says so (updates.py)
            if getattr(self.s, "game_cpus", None):  # the game off the server's cores (cpupin.py)
                n = cpupin.pin_game(self.s.game_cpus)
                log.info("the game's %d %s on CPUs %s", n, "processes" if sys.platform == "win32" else "threads",
                         ",".join(map(str, sorted(self.s.game_cpus))))
        elif kind == "prepare_anchor":
            self.spawn(self.prepare_anchor(msg))
        elif kind == "server_list":             # the BeamNG.Earth servers for the tab in BeamMP's browser
            self.spawn(self.server_list())
        # (shared world building, commented out 2026-10-27: the mod sends neither any more)
        # elif kind == "share":                   # a BeamMP session: serve our group (peerwork.GroupHelper)
        #     self.send(dict(self.s.share_start(msg.get("port")), type="share"))
        # elif kind == "peers":                   # the players on our anchor (the BeamMP plugin, through mpsession)
        #     self.s.share_group(msg)
        elif kind == "prepare_terrain":
            self.spawn(self.prepare_terrain(msg))
        elif kind == "commit_anchor":
            self.commit_anchor(msg.get("anchorId"))
        elif kind == "commit_terrain":
            self.commit_terrain(msg)
        elif kind == "pose":
            self.on_pose(msg)
        elif kind == "map_tiles":               # the in-game map's tiles, as files (the UI cannot fetch http)
            self.spawn(self.map_tiles(msg))
        elif kind == "geocode":                 # the map's search box: addresses and places (geocode.py)
            self.spawn(self.geocode(msg))
        elif kind == "cache_map":               # the map's cache view: what is cached where (covermap.py)
            self.spawn(self.cache_map(msg))
        elif kind == "area_plan":               # the map's picked area: the estimate for its confirm box (areas.py)
            self.spawn(self.area_plan(msg))
        elif kind == "area_add":                # ... downloaded ahead
            self.spawn(self.area_add(msg))
        elif kind == "area_cmd":                # a saved area paused, resumed or deleted
            self.spawn(self.area_cmd(msg))
        elif kind == "route":                   # the GPS: the whole road route (routing.py)
            self.spawn(self.route(msg))
        elif kind == "where":                   # the status bar's street, town and country (geocode.py)
            self.spawn(self.where(msg))
        elif kind == "random_place":            # the level select's "Random location" (randomplace.py)
            self.spawn(self.random_place(msg))
        elif kind == "place_previews":          # the level select's place pictures (placepreview.py)
            self.spawn(self.place_previews(msg))
        elif kind == "update_now":              # the update notice's "Update now" (selfupdate.py)
            self.spawn(self.update_now())
        elif kind == "open_page":               # the update notice's button: the releases page (updates.open_page)
            if not updatesmod.open_page(msg.get("url")):
                log.info("open_page: refused %r", msg.get("url"))
        elif kind == "terrain_patch":           # ground under another vehicle's collision island (terrainpatch.lua)
            self.spawn(self.terrain_patch(msg))
        elif kind == "cache":
            self.on_cache(msg)
        elif kind == "app_path":                # the settings page's folders: the cache's, the BeamMP launcher's
            self.spawn(self.app_path(msg))
        elif kind == "perf_log":                # the mod's performance log (perflog.lua): its hitches, frame-rate drops,
            # minute lines and the machine, into this log beside the blocks' own timings (the file players send)
            lines = msg.get("lines")
            for line in (lines if isinstance(lines, list) else [])[:PERF_LOG_LINES]:
                if isinstance(line, str):
                    log.info("game: %s", line[:PERF_LOG_CHARS].replace("\n", " "))
        elif kind == "roads_reset":             # the mod dropped its roads (settings): send them all again
            self.roads.close()
            self.roads = road_tracker(self)
            self.signs.close()
            self.signs = signstream.Tracker()
        elif kind == "highway_signs":           # the settings page's switch: no sign tiles built while it is off
            self.signs_on = bool(msg.get("on", True))
        elif kind == "graphics":                # the settings page's graphics knobs (set_graphics)
            self.set_graphics(msg)
        elif kind == "gas_stations":            # the settings page's switch: the road tiles again, with or without
            if self.s.set_gas_stations(msg.get("on")):
                self.roads.close()
                self.roads = road_tracker(self)
        else:
            log.debug("ignoring message type %r", kind)

    def set_graphics(self, msg):
        """The settings page's graphics (the user, 2026-10-28: "give player much more control in the graphics settings
        (view distance, LOD quality, ETC."), message 'graphics' {lodScale, treeScale, buildingScale, photoPx}, each
        optional (viewM: the fixed view distance in m, the far field's radius at most; 0 = the real horizon):
        lodScale times the K table (the far field's terrain detail: >1 splits each level farther out, finer
        ground to the horizon; Baltimore's first selection, 64 cells: 0.75 -> 0.78x the tiles, 1.3 -> ~1.3x, 2 ->
        2.1x), treeScale the tree tiles' reach (vegetation.SEND_M times it), buildingScale the far buildings', the
        skyline's and the far bridges' (buildings.FAR_SEND_M / SKY_SEND_M / BRIDGE_FAR_SEND_M times it), photoPx the
        ground photo's size (Server.set_base_px). Scales are clamped to GRAPHICS_SCALE; the far field is selected
        again at once, the trackers send and drop by their new reach at the next pose."""
        def scale(name, cur):
            v = msg.get(name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return cur
            return min(GRAPHICS_SCALE[1], max(GRAPHICS_SCALE[0], float(v)))
        lod, tree, bld = (scale("lodScale", self.lod_scale), scale("treeScale", self.tree_scale),
                          scale("buildingScale", self.building_scale))
        far = lod != self.lod_scale
        self.lod_scale = lod
        if tree != self.tree_scale:
            self.tree_scale = tree
            self.veg.send_m = vegetationmod.SEND_M * tree
            self.veg.drop_m = self.veg.send_m + (vegetationmod.DROP_M - vegetationmod.SEND_M)
        if bld != self.building_scale:
            self.building_scale = bld
            self.bldfar.send_m = buildingsmod.FAR_SEND_M * bld
            self.bldfar.drop_m = self.bldfar.send_m + (buildingsmod.FAR_DROP_M - buildingsmod.FAR_SEND_M)
            if self.bldsky is not None:
                self.bldsky.send_m = buildingsmod.SKY_SEND_M * bld
                self.bldsky.drop_m = self.bldsky.send_m + (buildingsmod.SKY_DROP_M - buildingsmod.SKY_SEND_M)
                self.bldsky.inner = (self.bldfar.send_m, buildingsmod.SKY_UNIT)
            if self.brfar is not None:
                self.brfar.send_m = buildingsmod.BRIDGE_FAR_SEND_M * bld
                self.brfar.drop_m = self.brfar.send_m + (buildingsmod.BRIDGE_FAR_DROP_M - buildingsmod.BRIDGE_FAR_SEND_M)
        if msg.get("photoPx") is not None:
            self.s.set_base_px(msg.get("photoPx"))
        v = msg.get("viewM")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            cap = max(VIEW_CAP_MIN_M, float(v)) if v > 0 else None
            far = far or cap != self.view_cap
            self.view_cap = cap
        log.info("graphics: terrain detail x%.2f, trees to %.0f m, far buildings to %.0f m, ground photo %d px, view %s",
                 self.lod_scale, self.veg.send_m, self.bldfar.send_m, self.s.base_px,
                 "to the horizon" if self.view_cap is None else f"{self.view_cap / 1000:.0f} km")
        if far and self.active is not None:
            self.update_far()

    def kfun(self):
        """K(L) of this connection's far field: the server's times the mod's terrain detail (lodScale)."""
        k, s = self.s.kfun, self.lod_scale
        return k if s == 1.0 else (lambda lvl: k(lvl) * s)

    async def terrain_patch(self, msg):
        """The mod's terrainpatch.lua: the ground's collision patch over box (x0, y0, x1, y1, the active anchor's
        frame) under a collision island off the terrain block (collisionfoci.lua); answered with its path and centre
        (or an error)."""
        aid, key = msg.get("anchorId"), msg.get("key")
        try:
            box = tuple(float(msg[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            return
        if (aid not in self.s.anchors or not isinstance(key, str) or not PATCH_KEY.fullmatch(key)
                or not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]
                or max(abs(v) for v in box) > FOCI_RANGE_M):
            return
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, self.s.build_terrain_patch, aid, key, box)
        except Exception as e:      # the ground not at hand (offline): the mod asks again later
            log.warning("terrain patch %s: %s: %s", key, type(e).__name__, e)
            self.send({"type": "terrain_patch", "anchorId": aid, "key": key, "error": f"{type(e).__name__}: {e}"[:200]})
            return
        if res is not None:
            parts, n = res
            self.send({"type": "terrain_patch", "anchorId": aid, "key": key, "tris": n,
                       "parts": [{"path": p, "x": c[0], "y": c[1]} for p, c in parts]})

    async def app_path(self, msg):
        """The settings page's "Cache folder" and "BeamMP launcher" (apppaths.py, the user 2026-10-25: "Open the
        explorer for that operating system but have a text input for the path as fallback"): what ('cache' or
        'beammp') chosen in the system's window (pick: true) or typed (path), kept for the next start of the app;
        always answered with app_paths (the paths now, and how the choice went). Without what: the paths only."""
        what = msg.get("what")
        pick, path = msg.get("pick") is True, msg.get("path")
        if what not in ("cache", "beammp") or not (pick or isinstance(path, str)):
            self.send(await self.loop.run_in_executor(None, app_paths_message, self.s.cache_dir, None))
            return
        if pick:
            if self.s.picking:
                self.send(await self.loop.run_in_executor(None, app_paths_message, self.s.cache_dir,
                                                          {"what": what, "ok": False, "text": "a window is already open"}))
                return
            self.s.picking = what
            try:
                self.send(await self.loop.run_in_executor(None, app_paths_message, self.s.cache_dir, None))
                state, path = await self.loop.run_in_executor(None, pick_app_path, what)
            finally:
                self.s.picking = None
            if state != "ok":
                result = {"what": what, "ok": False, "window": state != "none",
                          "text": "nothing chosen" if state == "cancel" else "no file window here: type the path"}
                self.send(await self.loop.run_in_executor(None, app_paths_message, self.s.cache_dir, result))
                return
        result = await self.loop.run_in_executor(None, set_app_path, what, path[:1000])
        log.info("app path %s: %s", what, result.get("text"))
        self.send(await self.loop.run_in_executor(None, app_paths_message, self.s.cache_dir, result))

    def on_cache(self, msg):
        """The settings page: capGb sets the cap (0 = unlimited), clear: true empties the cache (but this
        session's files and the gravity model) on an executor; every 'cache' gets the usage back (after a
        clear, with clearedBytes). Without a Cache (Server()'s default): ignored."""
        cache = self.s.cache
        if cache is None:
            return
        cap = msg.get("capGb")
        if cap is not None:
            if isinstance(cap, bool) or not isinstance(cap, (int, float)):
                raise ValueError(f"capGb {cap!r}: a number")
            if cache.set_cap(cap):
                log.info("cache: cap %s (from the mod)", f"{cap:g} GB" if cap else "unlimited")
        if msg.get("clear") is True:
            async def run():
                _, freed = await self.loop.run_in_executor(None, cache.clear)
                self.send(cache.message(cleared=freed))
            self.spawn(run())
        else:
            self.send(cache.message())

    # --- anchors and terrain ------------------------------------------------
    def _progress(self):
        last = [0.0]

        def progress(done, total):      # called from DEM worker threads
            now = time.monotonic()
            if done == total or now - last[0] > 0.25:
                last[0] = now
                self.loop.call_soon_threadsafe(self.status, f"Elevation tiles {done}/{total}")
        return progress

    def terrain_msg(self, key, info):
        out = {"cx": key[1], "cy": key[2], "heightmap": self.s.hm_vfs(key), "size": self.s.layout.block,
               "squareSize": heightmap.SQUARE, "zBase": info["zBase"], "maxHeight": info["maxHeight"],
               "q": info["q"]}
        if info.get("layers"):      # --surface on: one mask per material, MATERIALS order
            # only the .ter's own materials, in its order (surface.write_ter; all of them for an older block)
            use = info.get("mats") or range(len(surfacemod.MATERIALS))
            out["layers"] = [f"{self.s.vfs_dir}/{info['layers'][i]}" for i in use]
            out["materials"] = [list(surfacemod.MATERIALS[i]) for i in use]
        if info.get("base"):        # detail mode: the base photo over the block's world size
            out["base"] = f"{self.s.vfs_dir}/{info['base']}"
            out["baseSize"] = self.s.layout.block * heightmap.SQUARE
        if info.get("ter"):         # the same block as the engine's terrain file (swapped in, no import)
            out["ter"] = f"{self.s.vfs_dir}/{info['ter']}"
        return out

    async def prepare_anchor(self, msg):
        req = msg.get("requestId")
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90):
                raise ValueError
            lim = ANCHOR_CENTRE_MAX
            # the player's spot in the new frame when it is not the origin (BeamMP: a teleport onto another player's
            # anchor, mpsession.adoptAnchor): its ground height is sent instead of the origin's
            place = None
            if msg.get("px") is not None:
                place = (min(max(float(msg["px"]), -lim), lim), min(max(float(msg["py"]), -lim), lim))
            cx = min(max(self.s.snap(_num(msg, "cx", 0.0)), -lim), lim)
            cy = min(max(self.s.snap(_num(msg, "cy", 0.0)), -lim), lim)
        except (KeyError, TypeError, ValueError):
            self.send({"type": "anchor_error", "requestId": req, "error": "invalid lat/lon/cx/cy"})
            return
        lon = (lon + 180.0) % 360.0 - 180.0
        old = self.s.anchors.get(self.active)
        # (a session's first place, or a jump: the provisional first block; the mod says so since 1.6.3, a teleport
        # under TELEPORT_PAUSE_M away waited 26 s for a graded first block in Manhattan, 2026-10-25)
        declared = msg.get("purpose") == "teleport"
        teleport = old is None or declared
        if old is not None:
            la, lo, _ = old.frame.enu_to_geodetic(*self.focus, 0.0)
            if declared or geodesy.haversine_m(float(la), float(lo), lat, lon) > TELEPORT_PAUSE_M:
                teleport = True
                self.teleport_pending, self.teleport_t0 = True, time.monotonic()
                log.debug("teleport to %.5f, %.5f: the old place's work paused", lat, lon)
                # the cores lent from the request on, not from the first block's arrival: that block is most of
                # the wait (16.9 s in Baltimore on a Steam Deck, the server on its own 4 CPUs, 2026-10-20)
                self.boost_t0 = time.monotonic()
                self.s.load_boost(True, "teleport")
        anchor = self.s.new_anchor(lat, lon)
        # (the map data the first block's grading and the road tiles will ask for, fetched meanwhile)
        asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.warm_place, anchor, cx, cy)
        self.status(f"Building terrain at {lat:.5f}, {lon:.5f}...")
        t0 = time.perf_counter()
        # a teleport's first block without its road plans, the graded one sent when built (make_provisional)
        provisional = teleport and PROVISIONAL_FIRST and self.s.grading
        if self.s.surface is not None and not provisional:
            # the anchor's first block is built once and kept while the player stands still: give the
            # land cover around it a moment to arrive (blocks built while driving never wait)
            # (on the map's threads: the default pool is full of the old place's road / vegetation builds after a
            # teleport, and this 2 s wait queued behind them for ~25 s, 2026-10-14)
            tw = time.perf_counter()
            await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.surface.ready, lat, lon)
            log.info("anchor %s: land cover wait %.2f s", anchor.id, time.perf_counter() - tw)
        pacer.foreground_begin()        # the player waits for this block: the background loops hold (pacer.hold)
        try:
            if provisional:
                info = await asyncio.get_running_loop().run_in_executor(
                    self.s.hm_pool, self.s.make_provisional, anchor, cx, cy, self._progress(), place or (0.0, 0.0))
            else:
                info = await self.s.terrain(anchor, cx, cy, self._progress())
        except Exception as e:
            log.exception("terrain for anchor request %s failed", req)
            self.teleport_pending = False
            self.wake.set()
            self.send({"type": "anchor_error", "requestId": req, "error": f"{type(e).__name__}: {e}"})
            return
        finally:
            pacer.foreground_end()
        key = (anchor.id, cx, cy)
        anchor.first_terrain = (cx, cy)
        self.s.anchors[anchor.id] = anchor
        if self.s.horizon:
            # its zmax grid now, before the commit (a far field selected without it is the HORIZON_MIN disc, uncut)
            self.s.horizon_grid(anchor, self.eye if self.eye is not None else 0.0)
        self.s.hm_sent.append(key)
        # groundZ is None when (0, 0) lies outside the block (a resync around a distant player)
        gz = info["groundZ"]
        if place is not None and (not provisional or gz is None):
            # (a provisional block's groundZ is at the place already: ground_z_at carves the sea floor, which waits on
            # the water cells: 30 s of a fresh place's teleport, 2026-10-25)
            gz = await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.ground_z_at, anchor, *place)
        floor_z = None                  # in a tunnel: its floor under the ground (the mod moves the car down onto it
        if gz is not None and not provisional and place is None:   # once the tube's collision is there: set down in
                                                                   # it at once, it fell)
            fl = await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.tunnel_floor, anchor)
            if fl is not None and fl < gz - TUNNEL_FLOOR_UNDER_M:
                floor_z = round(fl + gradingmod.ROAD_LIFT, 3)
                log.info("anchor %d: in a tunnel, its floor %.1f under the ground %.1f", anchor.id, floor_z, gz)
        log.info("anchor %d (%.5f, %.5f) ready in %.2f s, terrain at (%.0f, %.0f), groundZ %s; dem %s",
                 anchor.id, lat, lon, time.perf_counter() - t0, cx, cy,
                 "n/a" if gz is None else f"{gz:.1f}",
                 self.s.dem.stats_snapshot())
        ready = {"type": "anchor_ready", "requestId": req, "anchorId": anchor.id, "lat": lat, "lon": lon,
                 "groundZ": gz, "floorZ": floor_z, "terrain": self.terrain_msg(key, info), "provisional": provisional}
        if place is not None:           # (the player's spot off the origin, echoed: core places the car there)
            ready["px"], ready["py"] = place
        if self.s.gas_stations:         # the shapes of the region's gas stations, preloaded (only that region's)
            ready["fuelShapes"] = gasstationsmod.style_shapes(gasstationsmod.style_of(lat, lon))
        self.send(ready)
        self.status(f"Terrain ready ({time.perf_counter() - t0:.1f} s)")
        if provisional:
            self.spawn(self.refine_first(anchor, cx, cy, info.get("ter"), t0))

    async def refine_first(self, anchor, cx, cy, provisional_ter, t0):
        """The graded first block (roads levelled, tunnels, the sea floor) in place of the provisional one: built as
        any block, sent as terrain_refresh (the mod swaps it in while that block is still its own). The provisional
        .ter is deleted a while later (the game may still be reading it)."""
        key = (anchor.id, cx, cy)
        self.refining.add(key)              # (the loading line's ground: send_progress)
        try:
            info = await self.s.terrain(anchor, cx, cy)
        except Exception:
            log.exception("graded first block %s failed (the provisional one stays)", key)
            return
        finally:
            self.refining.discard(key)
        self.s.hm_sent.append(key)
        log.info("anchor %d: graded first block in %.2f s after the request", anchor.id, time.perf_counter() - t0)
        self.send({"type": "terrain_refresh", "anchorId": anchor.id, **self.terrain_msg(key, info)})
        if provisional_ter and provisional_ter != info.get("ter"):
            await asyncio.sleep(PROVISIONAL_KEEP_S)
            self.s._remove(os.path.join(self.s.out_dir, provisional_ter))

    async def prepare_terrain(self, msg):
        req = msg.get("requestId")
        try:
            aid = msg["anchorId"]
            anchor = self.s.anchors[aid]
            cx, cy = self.s.snap(_num(msg, "cx")), self.s.snap(_num(msg, "cy"))
        except (KeyError, TypeError, ValueError) as e:
            self.send({"type": "terrain_error", "requestId": req, "anchorId": msg.get("anchorId"),
                       "error": f"invalid request: {type(e).__name__} {e}"})
            return
        if aid == self.active and self.s.layout.block_at(cx, cy) != self.block:
            # The pending block's L0 tiles stream first, so the commit never waits (§2.3).
            self.pending_block = self.s.layout.block_at(cx, cy)
            t1 = time.perf_counter()
            self.update_far()
            if time.perf_counter() - t1 > 0.1:
                log.debug("terrain request %s: update_far %.2f s", (aid, cx, cy), time.perf_counter() - t1)
        t0 = time.perf_counter()
        self.terrain_wait += 1
        try:
            job = asyncio.ensure_future(self.s.terrain(anchor, cx, cy))
            try:
                info = await asyncio.wait_for(asyncio.shield(job), TERRAIN_STUCK_S)
            except asyncio.TimeoutError:
                # (a game waited 5 min for one block with nothing in the log, GitHub #263)
                log.warning("terrain request %s still building after %.0f s (waiting on %s): it may be stuck",
                            (aid, cx, cy), time.perf_counter() - t0,
                            self.s.hm_stage.get((aid, cx, cy), "a heightmap worker"))
                info = await job
        except Exception as e:
            self.terrain_wait -= 1
            log.exception("terrain request %s failed", req)
            self.send({"type": "terrain_error", "requestId": req, "anchorId": aid,
                       "error": f"{type(e).__name__}: {e}"})
            if aid == self.active and self.pending_block == self.s.layout.block_at(cx, cy):
                self.pending_block = None   # the mod will not commit it: stop forcing L0 there
                self.update_far()
            return
        self.terrain_wait -= 1
        key = (aid, cx, cy)
        self.s.hm_sent.append(key)
        log.info("terrain_ready %s after %.0f ms", key, (time.perf_counter() - t0) * 1000)
        self.send({"type": "terrain_ready", "requestId": req, "anchorId": aid, **self.terrain_msg(key, info)})

    def commit_anchor(self, aid):
        if aid == self.active:
            return
        if self.teleport_pending:
            self.teleport_pending = False
            self.wake.set()
        anchor = self.s.anchors.get(aid)
        if anchor is None:
            log.warning("commit of unknown anchor %r ignored", aid)
            return
        old = self.s.anchors.get(self.active)
        if old is not None:
            # Tiles of the old anchor stay visible (the mod moves them); track their
            # footprints in the new frame so they are removed once covered.
            self.shown = {k: farfield.transform_bbox(old.frame, anchor.frame, bb) for k, bb in self.shown.items()}
            fx, fy, _ = anchor.frame.ecef_to_enu(*old.frame.enu_to_ecef(*self.focus, 0.0))
            self.focus = (float(fx), float(fy))
            if self.view_at is not None:
                # the camera away from the car too (the far field's centre until the next pose: left in the old
                # frame, the far field was selected that far off at the commit and global tiles went and came back,
                # Pikes Peak 2026-10-29)
                vx, vy, _ = anchor.frame.ecef_to_enu(*old.frame.enu_to_ecef(*self.view_at, 0.0))
                self.view_at = (float(vx), float(vy))
            log.debug("commit anchor %d: player at (%.0f, %.0f) in its frame, first terrain %s", aid, fx, fy,
                      anchor.first_terrain)
            if anchor.first_terrain is not None:
                bx0, by0, bx1, by1 = self.s.layout.block_at(*anchor.first_terrain).bbox
                if not (bx0 <= fx <= bx1 and by0 <= fy <= by1):
                    # Not a rebase but a teleport: the mod places the player on the new
                    # anchor's first terrain, and its next pose may come only after that.
                    self.focus, self.vel = (float(anchor.first_terrain[0]), float(anchor.first_terrain[1])), (0.0, 0.0)
                    self.view_at = None
                    self.boost_t0 = time.monotonic()
                    self.s.load_boost(True, "teleport")
        else:
            self.shown = {k: bb for k, bb in self.shown.items() if k[0] in self.s.anchors}
            # No previous frame to map the player from (first anchor, or a resync after a server
            # restart, whose terrain is around the player): start at the anchor's first terrain.
            self.focus = tuple(float(v) for v in anchor.first_terrain) if anchor.first_terrain else (0.0, 0.0)
            self.vel = (0.0, 0.0)
            self.view_at = None
        for ck, (fut, _) in self.inflight.items():
            if ck[0] != aid:
                fut.cancel()            # only stops jobs that have not started
        # Tiles sent but not swapped in are dropped by the mod at the anchor switch, but for the global ones (any
        # anchor's: the mod keeps them loading, placed into the new anchor; not after a resync, old is None)
        keep = {k: t for k, t in self.sent.items() if farfield.is_global_key(k)} if old is not None else {}
        self._retire(self.ck(k) for k in self.sent if k not in keep)
        self.active = aid
        self.ppolys = {}
        self.desired, self.sent, self.groups, self.queue, self.qi = {}, keep, [], [], 0
        old_grid = self.s.horizon_grids.get(old.id) if old is not None else None
        self.prev_horizon = (old.frame, old_grid) if old_grid is not None and old_grid.merc is not None else None
        # (the global far tiles' split state is any anchor's: kept, so their hysteresis holds across the rebase)
        self.refined = {n for n in self.refined if len(n) == 4}
        self.node_changed = {n: t for n, t in self.node_changed.items() if len(n) == 4}
        self.failed.clear()
        self.block = self.pending_block = self.predicted_block = None
        if anchor.first_terrain is not None:
            self._set_block(aid, *anchor.first_terrain)
        log.info("commit anchor %d", aid)
        self.update_far()

    def _set_block(self, aid, cx, cy):
        self.block = self.s.layout.block_at(cx, cy)
        if self.pending_block == self.block:
            self.pending_block = None
        key = (aid, cx, cy)
        if key in self.s.hm_committed:
            self.s.hm_committed.remove(key)
        self.s.hm_committed.append(key)
        self.s.evict_heightmaps()

    def commit_terrain(self, msg):
        aid = msg.get("anchorId")
        cx, cy = self.s.snap(_num(msg, "cx")), self.s.snap(_num(msg, "cy"))
        if aid != self.active:
            if aid in self.s.anchors and (self.active is None or aid > self.active):
                self.commit_anchor(aid)
            else:
                log.warning("commit_terrain for anchor %r (active %r) ignored", aid, self.active)
                return
        if self.block == self.s.layout.block_at(cx, cy):
            return
        # Far-tile keys do not depend on the block; only the forced L0 area moves.
        self._set_block(aid, cx, cy)
        self.update_far()

    def slots_gone(self, gone):
        """pose.slotsGone [[slot, serial], ...]: the mod's objects (or never-spawned instances) of those far_tile
        messages are gone; a slot whose latest send is among them is no longer held by this game."""
        if isinstance(gone, dict):      # an empty Lua table may encode as {}
            gone = list(gone.values())
        if not isinstance(gone, list):
            return
        let_go = []
        for g in gone:
            if isinstance(g, dict):
                g = [g.get("1"), g.get("2")]
            if not isinstance(g, (list, tuple)) or len(g) != 2:
                continue
            n, serial = g
            if isinstance(n, (int, float)) and self.slot_holds.get(int(n)) == serial:
                del self.slot_holds[int(n)]
                let_go.append(int(n))
        self.s.slots_let_go(let_go)

    def on_pose(self, msg):
        self.slots_gone(msg.get("slotsGone"))
        held = msg.get("held")
        if isinstance(held, dict):      # an empty Lua table may encode as {}
            held = list(held.values())
        if isinstance(held, list):
            self.held = {int(a) for a in held if isinstance(a, (int, float)) and not isinstance(a, bool)}
        self.s.claim_sessions(self, msg.get("sessions"))   # earlier runs' folders still in use (or released)
        self.weather.on_pose(self, msg.get("lat"), msg.get("lon"))
        if self.s.surface is not None and isinstance(msg.get("lat"), (int, float)) and isinstance(msg.get("lon"), (int, float)):
            self.s.surface.prefetch(float(msg["lat"]), float(msg["lon"]))
        aid = msg.get("anchorId")
        # The latest committed anchor is the one the mod reports in its pose
        # (ids only grow, so never switch back to an older one).
        if aid in self.s.anchors and (self.active is None or aid > self.active):
            self.commit_anchor(aid)
        x, y = _num(msg, "x", 0.0), _num(msg, "y", 0.0)
        vx, vy = _num(msg, "vx", 0.0), _num(msg, "vy", 0.0)
        if aid == self.active:
            self.focus = (x, y)
            self.pz = _num(msg, "z", 0.0)
        elif aid in self.s.anchors and self.active in self.s.anchors:
            # A late pose of the previous anchor: move it into the active frame.
            src, dst = self.s.anchors[aid].frame, self.s.anchors[self.active].frame
            fx, fy, _ = dst.ecef_to_enu(*src.enu_to_ecef(x, y, 0.0))
            self.focus = (float(fx), float(fy))
        else:
            return
        self.vel = (vx, vy)
        self.s.fast = math.hypot(vx, vy) > FAST_BASE_V
        if self.s.boost_on:
            since = time.monotonic() - getattr(self, "boost_t0", 0.0)
            moving = math.hypot(vx, vy) > BOOST_MOVE_MS and since > BOOST_GRACE_S     # (the car settling: not moving)
            if moving and self.s.mapdata is not None:
                # (not while the roads round the car are still being built: a 2-core server's boost ended as the
                # player drove off on bare ground, its road tiles minutes late, GitHub #412)
                done, want = self.roads.progress()
                moving = done >= want
            if moving or since > BOOST_MAX_S:
                self.s.load_boost(False, "moving" if moving else "timeout")
        if isinstance(msg.get("cam"), dict):
            self.cam, self.cam_aid = msg["cam"], aid
        self.view_at = self.camera_away(msg.get("cam")) if aid == self.active else None
        self.foci = parse_foci(msg.get("foci")) if aid == self.active else ()
        if self.s.horizon:
            self.update_eye(self.s.anchors[aid].frame, *(self.view_at or (x, y)), msg)
        self.gravity.on_pose(self, self.s.anchors[aid].frame, x, y, _num(msg, "z", 0.0))
        self.ground_ahead(aid, x, y, vx, vy)
        if self.s.sea is not None:
            self.sea_tiles(aid, x, y)
        # the mod's altitude rule: high above the ground it does not move its block, so no block is
        # built ahead (prefetch, road plans) until it comes down
        self.following = msg.get("follow") is not False
        self.predicted_block = None
        # The mod asks for its next terrain relative to the block it will have committed by
        # then: the pending one if it asked already (it commits that one first), else the
        # committed one. Predicting from the committed block while a request is pending built
        # terrains nobody asks for whenever the heading changed.
        base = self.pending_block or self.block
        if base is not None and self.following:
            px, py = self.focus
            rule = self.s.follow
            ahead = rule.mode(*self.vel)[2]         # M4 fast mode: the mod tests a point ahead
            qx, qy = px + self.vel[0] * ahead, py + self.vel[1] * ahead
            if max(abs(qx - base.cx), abs(qy - base.cy)) >= rule.prefetch_start:
                c = predict_request(px, py, *self.vel, base.cx, base.cy, rule)
                if c != (base.cx, base.cy) and self.s.layout.block_at(*c) != self.block:
                    self.predicted_block = self.s.layout.block_at(*c)
        self.update_far()
        if self.teleport_pending and time.monotonic() - self.teleport_t0 > TELEPORT_PAUSE_MAX_S:
            self.teleport_pending = False   # the mod never committed it (it stayed): the old place's work goes on
            self.wake.set()
        if self.teleport_pending:           # the old place's work waits: the new place has the CPU and the network
            self.refresh_status()
            return
        self.maybe_prefetch()
        if aid == self.active and self.following:
            self.s.warm_roads(self.s.anchors[aid], *self.focus, *self.vel)
            self.s.warm_imagery(self.s.anchors[aid], *self.focus, *self.vel)
            self.s.download_ahead(self.s.anchors[aid], *self.focus, *self.vel)
            self.base_refresh(aid)
            self.pads_refresh(aid)
            self.roads_refresh(aid)
        if self.s.mapdata is not None and aid == self.active:
            if getattr(self.roads, "geo", False):
                anchor = self.s.anchors.get(aid)
                self.roads.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_roads_geo,
                                   vel=self.vel, ground_busy=self.terrain_wait > 0, view=self.view_at,
                                   foci=self.foci, frame=anchor.frame if anchor is not None else None)
                # (the sign tiles stay the anchor's: each once the road geotiles under it are in)
                ready = self.roads.ready_tiles(anchor.frame, *self.focus, roadsmod.SEND_M) if anchor is not None \
                    else set()
            else:
                self.roads.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_roads_tile,
                                   vel=self.vel, ground_busy=self.terrain_wait > 0, view=self.view_at,
                                   foci=self.foci)
                ready = self.roads.sent
            source = getattr(self.s.mapdata, "highway_signs", None)
            if source is not None and self.signs_on:      # (after the roads: signs only for road tiles sent)
                self.signs.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_signs_tile,
                                   ready, version=source.tags.known())
        if self.s.veg is not None and aid == self.active:
            anchor = self.s.anchors.get(aid)
            if anchor is not None:                            # a teleport's cells built at once, ahead
                la, lo, _ = anchor.frame.enu_to_geodetic(*self.focus, 0.0)
                self.s.veg.warm_around(float(la), float(lo))
            self.veg.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                             self.build_veg_geo if self.veg.geo else self.build_veg_tile,
                             view=self.view_at, foci=[f for f in self.foci if f[3]],
                             frame=anchor.frame if anchor is not None else None)
        if self.s.buildings and aid == self.active:
            geo = self.far_geo
            anchor = self.s.anchors.get(aid)
            frame = anchor.frame if anchor is not None else None
            self.bldfar.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                                self.build_far_geo("bldfar") if geo else self.s.build_far_bld_tile,
                                view=self.view_at, frame=frame)
            if self.bldsky is not None and self.bldfar.progress()[0] >= self.bldfar.progress()[1]:
                # (only once the far buildings round the car are all there: the skyline is the least urgent)
                self.bldsky.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                                    self.build_far_geo("bldsky") if geo else self.s.build_sky_tile,
                                    view=self.view_at, frame=frame)
            if self.brfar is not None and self.bldfar.progress()[0] >= self.bldfar.progress()[1]:
                self.brfar.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                                   self.build_far_geo("brfar") if geo else self.s.build_far_bridge_tile,
                                   view=self.view_at, frame=frame)
        if self.s.traffic and aid == self.active and self.nav_may_start(aid):
            self.nav.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_navgraph)
        if aid == self.active:
            self.send_progress(aid)
        self.refresh_status()
        self.credits.poke(self)             # the player moved: the near / far credits may change

    def nav_may_start(self, aid):
        """May the AI graph of anchor aid be built now? Its first one waits until half the road tiles round the car are
        made (or NAV_WAIT_S): it takes a road process for its whole build, and on a teleport the road tiles under the
        car came after it (a Windows PC's two road processes, 2026-10-25)."""
        if self.nav.aid == aid:
            return True                     # (a rebuild as the car moves on: the tiles round it are there)
        t0 = self.nav_wait.get(aid)
        if t0 is None:
            t0 = self.nav_wait[aid] = time.monotonic()
            for k in [k for k in self.nav_wait if k != aid]:
                del self.nav_wait[k]
        done, wanted = self.roads.progress() if self.roads is not None else (0, 0)
        return (wanted > 0 and done * 2 >= wanted) or time.monotonic() - t0 >= NAV_WAIT_S

    def camera_away(self, cam):
        """The camera's (x, y) from a pose's cam when it is more than VIEW_AWAY_M from the car (a free camera, a far
        chase camera), else None. What is drawn follows it: the far field, and the road, tree and far building tiles
        round it as well as round the car; what collides stays round the car (the terrain block, the road and
        building colliders in the tiles round the car, the nav graph). The user, 2026-10-20: "All collision ...
        should be following the car while visual stuff should follow the camera"."""
        if not isinstance(cam, dict):
            return None
        cx, cy = cam.get("x"), cam.get("y")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (cx, cy)):
            return None
        if math.hypot(cx - self.focus[0], cy - self.focus[1]) <= VIEW_AWAY_M:
            return None
        return float(cx), float(cy)

    @property
    def view_focus(self):
        """Where the far field is centred: the camera when it is away from the car (camera_away), else the car."""
        return self.view_at or self.focus

    def base_refresh(self, aid):
        """Detail mode: the block in use and the one handed out last, if their base photo was rendered before
        all its NAIP tiles were fetched (Server.base_stale) and they are all here now: rendered again (a new
        file name) and sent as block_base. One at a time."""
        s = self.s
        if self.base_busy or not s.base_stale or s.imagery is None or s.near_visual != "detail":
            return
        anchor = s.anchors.get(aid)
        cands = [k for k in list(s.hm_committed)[-1:] + list(s.hm_sent)[-1:] if k in s.base_stale and k[0] == aid]
        size = s.layout.block * heightmap.SQUARE
        key = next((k for k in cands if k in s.hm and s.base_ready(anchor, k[1], k[2])), None)
        if key is None:
            return
        self.base_busy = True

        async def run():
            try:
                pool = s.block_pools[LANE_PREFETCH][0] if s.block_pools else None     # its BC7 encode
                name = await self.loop.run_in_executor(s.hm_pool, s.write_base, anchor, key[1], key[2], pool,
                                                       next(s.base_rev))
                info = s.hm.get(key)
                if name is None or info is None:
                    return
                old, info["base"] = info.get("base"), name
                if old and old != name and old != s.base_name(key):
                    s._remove(os.path.join(s.out_dir, old))
                log.info("base photo of %s rendered again with its NAIP (%s)", key, name)
                self.send({"type": "block_base", "anchorId": key[0], "cx": key[1], "cy": key[2],
                           "base": f"{s.vfs_dir}/{name}", "baseSize": size})
            except Exception:
                log.exception("base photo refresh %s", key)
                s.base_stale.discard(key)
            finally:
                self.base_busy = False
        self.spawn(run())

    def roads_refresh(self, aid):
        """The block in use built again (terrain_refresh) when it was built before every road cell under it was on disk
        (Server.roads_rebuild: surface.Surface.roads leaves a missing cell's roads out and fetches it; the block kept
        grass and gravel on those roads, and their grip, until it was built again: a fast first visit's mountain roads,
        Discord 2026-10-26), once those cells are here. One at a time, at any speed (the car is on it)."""
        s = self.s
        if self.roads_busy or not s.roads_rebuild or s.mapdata is None:
            return
        key = next((k for k in list(s.hm_committed)[-1:] if k in s.roads_rebuild and k[0] == aid), None)
        anchor = s.anchors.get(aid)
        if key is None or anchor is None:
            return
        half = s.layout.block * heightmap.SQUARE / 2
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([key[1] - half, key[1] + half]),
                                                 np.array([key[2] - half, key[2] + half]), 0.0)
        cells = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        if not all(s.mapdata.has(k, "roads") and (s.surface is None or s.mapdata.has(k, "landcover")) for k in cells):
            return                          # (not here yet: asked again at the next pose)
        self.roads_busy = True
        s.roads_rebuild.discard(key)

        async def run():
            old = s.hm.pop(key, None)
            try:
                info = await s.terrain(anchor, key[1], key[2])
            except Exception:
                log.exception("block %s again with its roads (the old one stays)", key)
                if old is not None:
                    s.hm[key] = old
                return
            finally:
                self.roads_busy = False
            s.hm_sent.append(key)
            log.info("block %s built again with its roads (their map cells came after it was built)", key)
            self.send({"type": "terrain_refresh", "anchorId": key[0], **self.terrain_msg(key, info)})
            old_ter = (old or {}).get("ter")
            if old_ter and old_ter != info.get("ter"):
                await asyncio.sleep(PROVISIONAL_KEEP_S)
                s._remove(os.path.join(s.out_dir, old_ter))
        self.spawn(run())

    def pads_refresh(self, aid, now=None):
        """The block in use built again (terrain_refresh, as refine_first) when gas station pads came in after it was
        built (Server.pads_rebuild: its surface layers then have the forecourt's asphalt, no grass on it; the user,
        2026-10-24: "looks like we painted it but it is still dirt"). Only with the car slower than PADS_REFRESH_MS
        (driving on, the next blocks are built with them) and PADS_SETTLE_S after the last new pad (a tile burst
        brings several); one at a time."""
        s = self.s
        now = time.monotonic() if now is None else now
        if (self.pads_busy or not s.pads_rebuild or math.hypot(*self.vel) > PADS_REFRESH_MS
                or now - s.pads_changed_at < PADS_SETTLE_S):
            return
        key = next((k for k in list(s.hm_committed)[-1:] if k in s.pads_rebuild and k[0] == aid), None)
        anchor = s.anchors.get(aid)
        if key is None or anchor is None:
            return
        self.pads_busy = True
        s.pads_rebuild.discard(key)

        async def run():
            old = s.hm.pop(key, None)
            try:
                info = await s.terrain(anchor, key[1], key[2])
            except Exception:
                log.exception("block %s again with its gas station pads (the old one stays)", key)
                if old is not None:
                    s.hm[key] = old
                return
            finally:
                self.pads_busy = False
            s.base_stale.discard(key)           # (its photo built with them too)
            s.hm_sent.append(key)
            log.info("block %s built again with its gas station pads", key)
            self.send({"type": "terrain_refresh", "anchorId": key[0], **self.terrain_msg(key, info)})
            old_ter = (old or {}).get("ter")
            if old_ter and old_ter != info.get("ter"):
                await asyncio.sleep(PROVISIONAL_KEEP_S)
                s._remove(os.path.join(s.out_dir, old_ter))
        self.spawn(run())

    def ground_ahead(self, aid, x, y, vx, vy):
        """Send the mod the highest ground near its straight path (groundahead.py; its altitude rule), one
        job at a time; a pose that arrives meanwhile is skipped (the next one comes in 0.5 s)."""
        if self.ground_busy:
            return
        self.ground_busy = True
        frame = self.s.anchors[aid].frame

        async def run():
            try:
                z = await self.loop.run_in_executor(self.s.ground_pool, groundaheadmod.highest, self.s.dem, frame,
                                                    x, y, vx, vy)
                now, last = time.monotonic(), self.ground_sent
                if math.isfinite(z) and (last is None or last[0] != aid or abs(z - last[1]) > GROUND_RESEND_M
                                         or now - last[2] >= GROUND_RESEND_S):
                    self.ground_sent = (aid, z, now)
                    self.send({"type": "ground_ahead", "anchorId": aid, "z": round(z, 1)})
            except Exception as e:      # offline, a DEM failure: the mod keeps following
                log.debug("ground ahead: %s: %s", type(e).__name__, e)
            finally:
                self.ground_busy = False
        self.spawn(run())

    def sea_tiles(self, aid, x, y):
        """Send the mod the water tiles around the player (seatiles.plan and plan_lakes; its sea.lua),
        planned again in another 512 m square or after SEA_CHECK_S, sent when the set changed."""
        last, now = self.sea_checked, time.monotonic()
        sq = seatilesmod.square_of(x, y)        # the plan is the same anywhere in this square
        # again after SEA_CHECK_S only when the last plan lacked map data (built meanwhile): a complete one stays
        # (re-planned every 15 s, the rivers' probes were a Steam Deck server's busiest thread, 64 s of a drive)
        if self.sea_busy or (last is not None and last[0] == aid and last[1] == sq
                             and (last[3] or now - last[2] < SEA_CHECK_S)):
            return
        self.sea_busy = True
        self.sea_checked = (aid, sq, now, False)
        missing = [False]               # a cell's map data not at hand for this plan
        frame = self.s.anchors[aid].frame
        sea, dem = self.s.sea, self.s.dem

        async def run():
            try:
                def cell_water(key):    # a cell's water lines if its map data is at hand (else built meanwhile)
                    md = sea.md
                    if not os.path.exists(md.path(mapdatamod._key(key), "water")):
                        missing[0] = True
                        sea.warm([(key.x, key.y)])
                        return None
                    return md.cell(key, layers=("water",)).layers.get("water")

                def tiled(la, lo):      # already water as tiles (the sea, lakes; not rivers)
                    d, lv, _ = sea.water(la, lo, wait=False)
                    return np.where(np.isfinite(lv) & ~sea.outline_at(la, lo), d, 0.0)

                def road_clear():   # the roads of the cells at hand around: the rivers keep off them
                    md = sea.md
                    r = riverlinesmod.RADIUS_M + riverlinesmod.PIECE_M
                    la0, lo0, _ = frame.enu_to_geodetic(x - r, y - r, 0.0)
                    la1, lo1, _ = frame.enu_to_geodetic(x + r, y + r, 0.0)
                    try:
                        every = mapcells.cells_for_bbox(float(la0), float(lo0), float(la1), float(lo1), max_cells=256)
                        keys = tuple(key for key in every if os.path.exists(md.path(mapdatamod._key(key), "roads")))
                        if len(keys) < len(every):
                            missing[0] = True
                        memo = getattr(self, "_river_clear", None)   # (anchor, cells) -> clearance: built once
                        if memo is not None and memo[0] == (aid, keys):
                            return memo[1]
                        feats = [md.cell(key, layers=("roads",)).layers.get("roads") for key in keys]
                        rc = riverlinesmod.RoadClearance.from_features(frame, feats)
                        self._river_clear = ((aid, keys), rc)
                        return rc
                    except Exception as e:      # no clearance: the widths as measured
                        log.debug("river road clearance: %s", e)
                        return None

                grader = self.s.grader_for(self.s.anchors[aid])

                def lake_ground(la, lo):
                    # the ground as graded under the roads (the terrain block's): a reservoir's tile kept over the
                    # raw DEM flooded Central Park's sunken transverse, graded 1.6 m under its water (2026-10-17)
                    z = dem.sample(la, lo, seatilesmod.LAKE_DEM_ZOOM)
                    return grader.apply(la, lo, z) if grader is not None else z

                def sea_ground(la, lo, size):
                    # the ground under a sea tile at about its samples' spacing (seatiles.plan: land under the sea,
                    # the polders, splits it)
                    n = min(max(size / 32.0, seatilesmod.SEA_SPLIT_SAMPLES[0]), seatilesmod.SEA_SPLIT_SAMPLES[1])
                    z = demmod.zoom_for(float(np.mean(la)), size / n)
                    z = min(max(z, getattr(demmod, "MIN_ZOOM", 0)), demmod.MAX_ZOOM)
                    return dem.sample(la, lo, z)

                def plan():
                    pacer.hold()        # (a teleport's first block first)
                    asked = sea.warm_asked
                    # with the cells at hand (the others are built in the background and used by the
                    # next plan, SEA_CHECK_S later): water appears at once at a new place
                    tiles = (seatilesmod.plan(frame, x, y, lambda la, lo, size: sea.sea_at(dem, la, lo, size, False),
                                              ground_at=sea_ground)
                             + seatilesmod.plan_lakes(frame, x, y, lambda la, lo: sea.lake_at(la, lo, False),
                                                      lake_ground))
                    def carved(la, lo):     # the river beds' level (seacarve), NaN elsewhere
                        lv = sea.water(la, lo, wait=False)[1]
                        return np.where(sea.outline_at(la, lo), lv, np.nan)
                    rivers = riverlinesmod.plan(frame, x, y, cell_water, tiled,
                                                lambda la, lo: dem.sample(la, lo, riverlinesmod.DEM_ZOOM),
                                                sea.outline_at, level_at=carved, road_clear=road_clear())
                    shores = seacarvemod.shore_plan(frame, x, y, sea)     # (the mod's shore sound)
                    return tiles, rivers, shores, not missing[0] and sea.warm_asked == asked
                tiles, rivers, shores, complete = await self.loop.run_in_executor(self.s.ground_pool, plan)
                if complete and self.sea_checked is not None and self.sea_checked[:2] == (aid, sq):
                    self.sea_checked = (aid, sq, self.sea_checked[2], True)
                msg = ([t.to_msg() for t in tiles], [r.to_msg() for r in rivers], shores)
                if self.sea_sent is None or self.sea_sent[0] != aid or self.sea_sent[1] != msg:
                    self.sea_sent = (aid, msg)
                    self.send({"type": "sea", "anchorId": aid, "tiles": msg[0], "rivers": msg[1], "shores": msg[2]})
            except Exception as e:      # offline, no map data: no water tiles
                log.debug("sea tiles: %s: %s", type(e).__name__, e)
            finally:
                self.sea_busy = False
        self.spawn(run())

    def update_eye(self, frame, x, y, msg):
        """M1h: the eye's height above sea level from a pose in `frame` (the pose's anchor): the camera's
        z, else EYE_ABOVE_PLAYER over the player's. The frame's z is the height above the anchor's
        tangent plane, whose origin is at sea level, so the exact geodetic height adds the d²/2R the
        plane lies above the ellipsoid at the player (17.7 m at 15 km). Quantised (EYE_STEP)."""
        cam = msg.get("cam")
        z = cam.get("z") if isinstance(cam, dict) else None
        if isinstance(z, (int, float)) and not isinstance(z, bool) and math.isfinite(z):
            z = float(z)
        else:
            z = _num(msg, "z", 0.0) + EYE_ABOVE_PLAYER
        h = float(frame.enu_to_geodetic(x, y, z)[2])
        if self.eye is None or abs(h - self.eye) > max(EYE_STEP, EYE_STEP_REL * abs(self.eye)):
            self.eye = h

    def horizon_select(self):
        """M1h: (radius, extra select() arguments) for the active anchor: the disc out to the
        line-of-sight horizon with the per-tile cull, L10 roots; until the anchor's zmax grid is
        built, the HORIZON_MIN disc without a cull. Reports a changed radius in a status."""
        anchor = self.s.anchors[self.active]
        eye = self.eye if self.eye is not None else 0.0
        grid = self.s.horizon_grid(anchor, eye)
        cull = None
        focus = self.view_focus
        if grid is None and self.global_far and self.prev_horizon is not None:
            # (a rebase whose grid is not built yet: the previous anchor's, the focus in its plane. Its Mercator
            # table culls the global tiles the same for any anchor; without it they went and came back once the new
            # grid was built. The anchor's own tiles lie within HORIZON_MIN: never culled)
            pframe, grid = self.prev_horizon
            fx, fy, _ = pframe.ecef_to_enu(*self.s.anchors[self.active].frame.enu_to_ecef(*focus, 0.0))
            focus = (float(fx), float(fy))
        if grid is None:
            radius = farfield.HORIZON_MIN
        else:
            radius, zr = grid.radius(*focus, eye)
            cull = farfield.Horizon(eye, grid)
            if zr != self.zmax_region or abs(radius - self.radius) > RADIUS_REPORT_REL * self.radius:
                log.info("horizon: eye %.0f m, highest visible terrain %.0f m, radius %.0f km",
                         eye, zr, radius / 1000)
            self.zmax_region = zr
        self.radius = radius
        if self.radius_sent is None or abs(radius - self.radius_sent) > RADIUS_REPORT_REL * self.radius_sent:
            self.status(self.last_status or f"Horizon {radius / 1000:.0f} km")
        return radius, {"max_level": self.s.max_level, "disc": True, "horizon": cull}

    def maybe_prefetch(self):
        """Speculatively build the terrain the mod is about to ask for."""
        anchor = self.s.anchors.get(self.active)
        if anchor is None or self.predicted_block is None or self.prefetching:
            return
        cx, cy = self.predicted_block.cx, self.predicted_block.cy
        if self.s.terrain_cached((anchor.id, cx, cy)):
            return
        self.prefetching = True

        async def run():
            try:
                await self.s.terrain(anchor, cx, cy, lane=LANE_PREFETCH)
                log.debug("prefetched terrain (%d, %.0f, %.0f)", anchor.id, cx, cy)
            except Exception:
                log.exception("terrain prefetch failed")
            finally:
                self.prefetching = False
        self.spawn(run())

    # --- far field ----------------------------------------------------------
    def forced(self):
        """Boxes refined to L0 (terrain mode: the mod covers L0 under the block; detail mode: to L-1).
        None in mesh mode: the block is invisible, so tiles never depend on it (V1_3_SPEC §B).
        Detail mode: each box grown by DETAIL_RING_M, so the block's neighbours are L-1 tiles too (2 m
        cells, the block's own lattice: their edge meets the block's; L1 neighbours, 8 m cells, left a
        step and their dark skirt along the block's edge, in-game 2026-10-13)."""
        if self.s.near_visual not in BLOCK_DRAWN:
            return []
        g = DETAIL_RING_M if self.s.near_visual == "detail" else 0.0
        return [(x0 - g, y0 - g, x1 + g, y1 + g) for x0, y0, x1, y1 in
                (b.bbox for b in (self.block, self.pending_block, self.predicted_block) if b is not None)]

    def _retire(self, cks):
        now = time.monotonic()
        for ck in cks:
            self.retired[ck] = now

    def update_far(self):
        """Recompute the desired set, the swap groups and the build queue; wake the streamer."""
        if self.active is None:
            return
        now = time.monotonic()
        self.retired = {ck: t for ck, t in self.retired.items() if now - t < self.s.retired_keep}
        self.node_changed = {n: t for n, t in self.node_changed.items() if now - t < farfield.HOLD_S}
        radius, extra = (self.horizon_select() if self.s.horizon else (self.s.radius, {}))
        if self.view_cap is not None:       # (a fixed view distance: nothing past it is drawn; a disc, as the horizon)
            radius, extra = min(radius, self.view_cap), dict(extra, disc=True)
        anchor = self.s.anchors[self.active]
        if self.global_on(anchor, radius):
            # far levels from 3 up as global tiles (geotiles phase 3b): the same for every anchor, kept at a rebase
            new, refined = farfield.select_global(anchor.frame, *self.view_focus, anchor.zf, self.forced(),
                                                  self.refined, self.kfun(), self.s.hysteresis, radius,
                                                  hold=set(self.node_changed), min_level=self.s.min_level,
                                                  radii=self.s.near_radii, layout=self.s.layout,
                                                  forced_level=self.s.forced_level, view=self.view(), **extra)
        else:
            new, refined = farfield.select(*self.view_focus, self.forced(), self.refined, self.kfun(),
                                           self.s.hysteresis, radius, hold=set(self.node_changed),
                                           min_level=self.s.min_level, radii=self.s.near_radii, layout=self.s.layout,
                                           forced_level=self.s.forced_level, view=self.view(), **extra)
        if self.desired:                # the first selection of an anchor is not a change
            for n in refined ^ self.refined:
                self.node_changed[n] = now
        self.refined = refined
        changed = new.keys() != self.desired.keys()
        self.desired = new
        self._regroup()
        if changed and not self.dirty:
            self.dirty = True
            self.batch = [time.perf_counter(), 0, 0]
        self.wake.set()

    def ck(self, key):
        """The server's key of far tile `key` under the active anchor (Server.far_ck: a global tile's names none)."""
        return (GLOBAL_AID, key) if farfield.is_global_key(key) else (self.active, key)

    def global_on(self, anchor, radius):
        """Global far tiles for this selection: the mod places them (hello.globalFar) and the radius stays within Web
        Mercator (farfield.GLOBAL_MAX_LAT; beyond it the anchor's quadtree as before)."""
        return bool(self.global_far) and abs(anchor.lat) + (radius + 20000.0) / 110574.0 < farfield.GLOBAL_MAX_LAT

    def poly_of(self, key):
        """A special tile's footprint polygon (flat) in the active anchor's world (swap groups): a desired tile's own,
        a shown global tile's quad, a piece's polygon as sent."""
        t = self.desired.get(key)
        if t is not None:
            return t.poly
        if farfield.is_global_key(key):
            ck = (self.active, key)
            q = self.gpolys.get(ck)
            if q is None:
                anchor = self.s.anchors.get(self.active)
                g = farfield.parse_global(key)
                if anchor is None or g is None:
                    return None
                q = self.gpolys[ck] = tuple(float(v) for v in
                                            geotilesmod.quad(anchor.frame, g[1], g[2], g[0]).ravel())
            return q
        return self.ppolys.get(key)

    def view(self):
        """The camera for screen-space LOD (farfield.View) from pose.cam (x, y, z, fovDeg, h: the screen's height px),
        or None: no camera yet, an older mod without h, a camera of another anchor's frame, or --lod-px 0. The
        tile errors are the built tiles' (a node never built splits by the distance rule only)."""
        c = self.cam
        if not isinstance(c, dict) or self.s.lod_px <= 0 or getattr(self, "cam_aid", None) != self.active:
            return None
        try:
            cx, cy, cz = float(c["x"]), float(c["y"]), float(c["z"])
            h, fov = float(c.get("h") or 0), float(c.get("fovDeg") or 60)
        except (KeyError, TypeError, ValueError):
            return None
        if not (h > 0 and 1 < fov < 170 and math.isfinite(cx) and math.isfinite(cy) and math.isfinite(cz)):
            return None
        ppr = h / (2 * math.tan(math.radians(fov) / 2))     # BeamNG's fov: vertical
        aid, tiles = self.active, self.s.tiles

        def err(node):
            if len(node) != 3:
                return None
            info = tiles.get((aid, farfield.Tile(*node).key))
            return None if info is None else info.err
        reach, cap = farfield.SSE_REACH, farfield.SSE_SPACING_CAP
        tune = os.environ.get("BNGE_LOD")      # experiments: "reach,cap"
        if tune:
            reach, cap = (float(v) for v in tune.split(","))
        return farfield.View(cx, cy, cz - getattr(self, "pz", cz), ppr, err, tau=self.s.lod_px, reach=reach,
                             cells=self.s.layout.cells, cap=cap)

    def _regroup(self):
        aid = self.active
        cur = {k for a, k in self.shown if a == aid or a == GLOBAL_AID}
        if len(self.gpolys) > 4 * len(self.desired) + 1000:
            self.gpolys = {ck: q for ck, q in self.gpolys.items() if ck[0] == aid}
        self.groups = farfield.swap_groups(cur, self.desired, self.poly_of)
        # Sent tiles no group needs any more: the mod drops them. (A global one kept from a previous anchor takes
        # the tile of this selection: its quad in this anchor's world.)
        needed = {k for adds, _ in self.groups for k in adds}
        for k in self.sent:
            t = self.desired.get(k)
            if t is not None and self.sent[k] is not t:
                self.sent[k] = t
        for k in [k for k in self.sent if k not in needed]:
            del self.sent[k]
            self._retire([self.ck(k)])
            self.send({"type": "far_remove", "anchorId": aid, "key": k})
        # Build order: L0 under the committed / pending block (the mod's cover and commit
        # gating need them), then groups that are partly sent, then the rest; nearest first.
        gate = [b.bbox for b in (self.block, self.pending_block) if b is not None and self.s.near_visual in BLOCK_DRAWN]
        started = {k for adds, _ in self.groups if any(a in self.sent for a in adds) for k in adds}
        fx, fy = self.view_focus
        todo = [self.desired[k] for k in needed if k not in self.sent]

        cone = (farfield.view_cone(self.cam)
                if isinstance(self.cam, dict) and getattr(self, "cam_aid", None) == aid else None)
        dist = farfield.view_distances(todo, cone, fx, fy)

        def prio(k):
            t = todo[k]
            cls = 2
            if t.level in (0, self.s.forced_level) and any(farfield.overlaps(t.bbox, b) for b in gate):
                cls = 0
            elif t.key in started:
                cls = 1
            return (cls, dist[k], t.level, t.key)
        self.queue = [todo[k] for k in sorted(range(len(todo)), key=prio)]
        self.qi = 0
        self.failed.clear()

    def _next_tile(self):
        aid = self.active
        while self.qi < len(self.queue):
            t = self.queue[self.qi]
            self.qi += 1
            ck = self.ck(t.key)
            if t.key not in self.sent and ck not in self.inflight and t.key in self.desired:
                return t
        return None

    def emit_groups(self):
        """far_swap every group whose adds are all sent."""
        aid = self.active
        rest = []
        info = self.s.tiles
        live = set()
        for adds, removes in self.groups:
            if not all(k in self.sent for k in adds):
                rest.append((adds, removes))
                continue
            gk = (aid, tuple(adds), tuple(removes))
            live.add(gk)
            fut = self.deltas.get(gk)
            if fut is None:
                special = any(farfield.is_special_key(k) for k in adds + removes)
                try:
                    if special:
                        # a global tile's grid is on its Mercator vertices, a piece's its node's: the mod's bound,
                        # 1.2 (err_new + err_old) + 0.2 (crossover.delta) on the group's largest errors
                        en = max([info[self.ck(k)].err for k in adds if self.ck(k) in info] or [0.0])
                        eo = max([info[self.ck(k)].err for k in removes if self.ck(k) in info] or [0.0])
                        raise LookupError(1.2 * (en + eo) + 0.2)
                    args = ([(farfield.tile_of(k), info[(aid, k)].grid) for k in adds],
                            [(farfield.tile_of(k), info[(aid, k)].grid) for k in removes])
                except LookupError as bound:    # a tile record was evicted: fall back to the err bound
                    e = [info[self.ck(k)].err for k in adds + removes if self.ck(k) in info]
                    d = 1.2 * sum(e) + 0.2 if e else 0.0
                    if special:
                        d = float(bound.args[0])
                    fut = concurrent.futures.Future()
                    fut.set_result((d - DELTA_MARGIN, d - DELTA_MARGIN))
                else:
                    fut = self.s.delta_pool.submit(farfield.group_deltas, args)
                    loop = asyncio.get_running_loop()
                    fut.add_done_callback(lambda _f: loop.call_soon_threadsafe(self.wake.set))
                self.deltas[gk] = fut
            if not fut.done():
                rest.append((adds, removes))
                continue
            del self.deltas[gk]
            if fut.cancelled() or fut.exception() is not None:
                log.warning("swap deltas of %s: %r", gk[1], None if fut.cancelled() else fut.exception())
                e = [info[self.ck(k)].err for k in adds + removes if self.ck(k) in info]
                d_in = d_out = (1.2 * sum(e) + 0.2 if e else 0.0) - DELTA_MARGIN
            else:
                d_in, d_out = fut.result()
            d_in, d_out = d_in + DELTA_MARGIN, d_out + DELTA_MARGIN
            gid = self.next_group
            self.next_group += 1
            self.send({"type": "far_swap", "anchorId": aid, "id": gid, "add": adds, "remove": removes,
                       "dIn": round(d_in, 2), "dOut": round(d_out, 2)})
            if log.isEnabledFor(logging.DEBUG) and adds and removes and \
                    not any(farfield.is_special_key(k) for k in adds + removes):    # the pop's size: amplitude / distance
                dist = min(farfield.tile_of(k).distance(*self.focus) for k in adds + removes)
                log.debug("far_swap %d L%d->L%d dIn %.1f dOut %.1f at %.0f m (%.2f mrad)", gid,
                          farfield.tile_of(removes[0]).level, farfield.tile_of(adds[0]).level, d_in, d_out, dist,
                          1000 * max(d_in, d_out) / max(dist, 1.0))
            for k in removes:
                self.shown.pop(self.ck(k), None)
            self._retire(self.ck(k) for k in removes)
            for k in adds:
                self.shown[self.ck(k)] = self.sent.pop(k).bbox
            self.batch[2] += 1
            self.credits.poke(self)         # other tiles on screen
        self.groups = rest
        for gk in [gk for gk in self.deltas if gk not in live]:     # groups re-planned meanwhile
            self.deltas.pop(gk).cancel()

    def flush_old(self):
        """far_remove every shown tile of a previous anchor once no desired tile that is not
        swapped in yet overlaps it (so nothing ever leaves a hole)."""
        aid = self.active
        cands = [ck for ck in self.shown if ck[0] != aid and ck[0] != GLOBAL_AID]
        if not cands:
            return
        pending = [t.bbox for k, t in self.desired.items() if self.ck(k) not in self.shown]
        if pending:
            c = np.array([self.shown[ck] for ck in cands])
            u = np.array(pending)
            blocked = ((c[:, None, 0] < u[None, :, 2]) & (u[None, :, 0] < c[:, None, 2]) &
                       (c[:, None, 1] < u[None, :, 3]) & (u[None, :, 1] < c[:, None, 3])).any(1)
        else:
            blocked = [False] * len(cands)
        for ck, b in zip(cands, blocked):
            if not b:
                del self.shown[ck]
                self._retire([ck])
                self.send({"type": "far_remove", "anchorId": ck[0], "key": ck[1]})
                self.credits.poke(self)

    def _blocked(self):
        """Groups that cannot complete now (an add failed to build)."""
        return [g for g in self.groups if any(k in self.failed for k in g[0])]

    async def stream(self):
        try:
            await self._stream()
        except ConnectionError:
            pass
        except asyncio.CancelledError:
            for fut, _ in self.inflight.values():
                fut.cancel()
            raise

    async def _stream(self):
        """Build the needed tiles by priority, send them, emit swap groups as they complete;
        one task per connection."""
        waker = None
        while True:
            # Clear before reading the state, so an update from here on wakes us again.
            self.wake.clear()
            anchor = self.s.anchors.get(self.active)
            # While a physics block builds in this process, one far tile at a time: they share the interpreter
            # (GIL), and three far workers made a block build 3-10x slower (landcover 1.4 s vs 0.1 s), too slow
            # to follow a car at 58 m/s (2026-10-12). The far tile finishing next refills the workers.
            workers = HM_BUSY_TILE_WORKERS if self.s.hm_here else self.s.tile_workers
            # plus the helpers' slots (compute sharing): their tiles cost this process only the files (26 ms a
            # tile); kept to the local workers, a Steam Deck sent 2 at a time to 12 idle helper workers (2026-10-22)
            workers += self.s.remote_slots()
            while anchor is not None and not self.teleport_pending and len(self.inflight) < workers:
                t = self._next_tile()
                if t is None:
                    break
                self.inflight[self.ck(t.key)] = (self.s.tile(anchor, t), t)
            if anchor is not None and self.s.imagery is not None and not self.teleport_pending:
                # a tile at most every PREFETCH_AGAIN_S (a prefetch skips sources short of request tokens): the
                # loop wakes on every tile done and every message from the game, and asking again for the same 40
                # tiles each time kept the event loop busy (~22 s of a teleport on a Steam Deck, 2026-10-20)
                now = time.monotonic()
                if len(self.prefetched) > PREFETCHED_MAX:
                    self.prefetched.clear()
                for t in self.queue[self.qi:self.qi + IMAGERY_PREFETCH]:
                    ck = self.ck(t.key)
                    if ck not in self.s.tiles and now - self.prefetched.get(ck, -1e9) >= PREFETCH_AGAIN_S:
                        self.prefetched[ck] = now
                        self.s.prefetch_imagery(anchor, t)
            if anchor is not None:
                self.emit_groups()
                self.flush_old()
            if (not self.inflight and self.dirty and anchor is not None
                    and len(self.groups) == len(self._blocked())):
                self.dirty = False
                n = sum(1 for k in self.desired if self.ck(k) in self.shown)
                self.send({"type": "far_done", "anchorId": anchor.id, "count": n})
                self.s.load_boost(False, "far field done")
                self.status(f"Far field ready ({n} tiles)")
                bs = sorted(self.s.build_stats)
                log.info("anchor %d far field done: %d tiles (%d sent, %d swaps in %.2f s; build median %.1f ms)",
                         anchor.id, n, self.batch[1], self.batch[2], time.perf_counter() - self.batch[0],
                         1000 * bs[len(bs) // 2] if bs else 0.0)
                log.info("tile build ms (geometry+write / texture) and KB (shape / texture) by level: %s",
                         self.s.level_summary())
                if getattr(self.s.far_pool, "stats", None) is not None and self.batch[1] >= 50:   # (a full one)
                    g = getattr(self.s.far_pool, "group", None)
                    log.info("compute sharing: %s; group %s", self.s.far_pool.stats,
                             g["ids"] if g else "none")
                if self.s.imagery is not None:
                    log.info("imagery: %s", self.s.imagery.summary())
                    if self.s.imagery_worker or self.s.base_worker:
                        log.info("imagery in the worker processes: %d far textures (%d after a fetch here), "
                                 "%d base photos", self.s.imagery_worker, self.s.refetched, self.s.base_worker)
                if self.s.retex_stats["started"]:
                    log.info("live retexture: %s, %d waiting", self.s.retex_stats, len(self.s.retex_todo))
                if not any(ck[0] != anchor.id and ck[0] != GLOBAL_AID for ck in self.shown):
                    self.s.schedule_prune(anchor.id)
            await self.writer.drain()
            if waker is None:
                waker = asyncio.ensure_future(self.wake.wait())
            futs = [f for f, _ in self.inflight.values()]
            done, _ = await asyncio.wait(futs + [waker], return_when=asyncio.FIRST_COMPLETED)
            if waker in done:
                waker = None
            for ck in [ck for ck, (f, _) in self.inflight.items() if f.done()]:
                fut, t = self.inflight.pop(ck)
                if fut.cancelled():
                    continue
                if fut.exception() is not None:
                    log.error("far tile %s of anchor %d failed: %r", t.key, ck[0], fut.exception(),
                              exc_info=fut.exception() if t.key not in self.failed else None)   # (its first)
                    self.failed.add(t.key)
                    continue
                if ck != self.ck(t.key) or t.key not in self.desired or ck in self.shown or t.key in self.sent:
                    continue            # superseded meanwhile; the file stays cached
                # the server's latest record: a live retexture may have finished since this build
                info = self.s.tiles.get(ck, fut.result())
                if info.material is not None and not self.s.matslots.owns(ck, info.material):
                    info = self.s.reslot(ck, info)      # its material name went to another tile meanwhile
                    if not self.s.matslots.owns(ck, info.material):
                        # not renamed: sent with that name it would draw the other tile's texture; built again
                        # (its files are written anew; the cached entry and its claim on the name go)
                        self.s.tiles.pop(ck, None)
                        self.s.matslots.release(ck)
                        self.queue.append(t)
                        continue
                self.sent[t.key] = t
                if info.stale:          # a region turned ready since: re-rendered live, then far_retex
                    self.s.retex_request(ck)
                self.batch[1] += 1
                m = {"type": "far_tile", "anchorId": self.active, "key": t.key, "level": t.level,
                     "shape": f"{self.s.vfs_dir}/{info.shape or self.s.tile_name(*ck)}",
                     "pos": [round(v, 3) for v in info.pos], "err": round(info.err, 3),
                     "skirt": round(info.skirt, 2), "state": "group"}
                if info.sink is not None:
                    m["sink"] = round(info.sink, 2)
                if isinstance(t, farfield.GTile):   # geotiles phase 3b: placed by the mod from its own frame
                    m["frame"] = [info.frame[0], info.frame[1]]
                    m["pos"] = [0, 0, round(info.pos[2], 3)]
                    m["poly"] = [round(v, 2) for v in t.quad]
                elif isinstance(t, farfield.Piece):
                    m["poly"] = [round(v, 2) for v in t.poly]
                    self.ppolys[t.key] = t.poly
                if info.shape is not None and info.shape.startswith("slot_"):  # pooled (matslots.py)
                    m["slotSerial"] = serial = next(self.s.slot_serial)
                    self.slot_holds[matslotsmod.MatSlots.number(info.material)] = serial
                if info.material is not None:   # protocol 4: the tile's own imagery material
                    m["material"] = info.material
                    m["texture"] = f"{self.s.vfs_dir}/{info.texture}"
                    m["detail"] = True          # the shape has UV1 (world-locked detail map)
                    if self.s.layout.format >= 4:   # M1: texture px, the mod's governor unit kind
                        m["tex"] = self.s.tex_size(t.level)
                self.send(m)
                if self.batch[1] % 50 == 0:
                    left = sum(1 for g in self.groups for k in g[0] if k not in self.sent)
                    # this change's tiles (sent / sent + still to send), not the whole far field
                    self.status(f"Far field loading: {self.batch[1]}/{left + self.batch[1]} tiles")


async def main_async(args):
    fmt = args.far_format
    if fmt == "cdae":
        v = cdae.engine_version(args.user_dir)
        if v is not None and v != cdae.VERSION_WORD:
            log.warning("the game writes shape version 0x%08x, this server 0x%08x: using DAE far tiles",
                        v, cdae.VERSION_WORD)
            fmt = "dae"
    kfun = None if args.lod_k is None else (lambda lvl: args.lod_k)
    radii = None
    if args.near_radii is not None:
        ml = farfield.MESH_MIN_LEVEL[args.cells]
        radii = {ml: args.near_radii[0], ml + 1: args.near_radii[1]}
    net = netfetch.Fetcher(stats_dir=os.path.join(args.cache_dir, "netstats"), offline=args.offline)
    disk = cachemod.Cache(args.cache_dir, args.cache_cap)
    threading.Thread(target=prune_own_caches, args=(args.cache_dir, time.time()), daemon=True,
                     name="cache-prune").start()
    # map data (mapdata.py): land cover for the surfaces, roads; one instance shares its cache locks
    mapdata = (mapdatamod.MapData(args.cache_dir, fetcher=net)
               if "on" in (args.surface, args.roads, args.sea, args.trees) else None)
    pacer.DUTY = args.background_duty
    global DETAIL_RING_M, AHEAD_ON, ROUTE_AHEAD_ON, ROUTE_AHEAD_MAX_M
    DETAIL_RING_M = args.detail_ring
    AHEAD_ON = args.download_ahead == "on"
    ROUTE_AHEAD_ON = args.route_prefetch == "on" and args.route_prefetch_km > 0
    ROUTE_AHEAD_MAX_M = args.route_prefetch_km * 1000.0
    block_pools, far_pool = [], None
    plancachemod.start_run(args.cache_dir)      # (before the workers: they inherit the run's id)
    if args.block_process == "on":
        ctx = multiprocessing.get_context("spawn")
        for _ in (LANE_REQUEST, LANE_PREFETCH):
            warm_q = ctx.Queue(maxsize=64)
            block_pools.append((poolsmod.RespawningPool(
                "block", 1, mp_context=ctx, initializer=blockworker.init,
                initargs=(args.cache_dir, args.offline, args.roads == "on",
                          args.grading == "on" and args.roads == "on",
                          args.road_widths == "on" and args.roads == "on", args.surface == "on", warm_q,
                          logging.getLogger().level, args.airports == "on", args.sea == "on", 0,
                          roadsmod.ROADS_IN_GROUND, args.gas_stations == "on" and args.roads == "on")), warm_q))
        far_pool = poolsmod.RespawningPool(
            "far tile", args.workers, mp_context=ctx, initializer=blockworker.init,
            initargs=(args.cache_dir, args.offline, args.roads == "on",
                      args.grading == "on" and args.roads == "on",
                      args.road_widths == "on" and args.roads == "on", False, None,
                      logging.getLogger().level, args.airports == "on", args.sea == "on", NICE_FAR,
                      roadsmod.ROADS_IN_GROUND))
    if far_pool is not None:
        # compute sharing: far tiles to helpers (--peers) and, in a BeamMP group, split with the players on our anchor,
        # built with the same settings as these workers (peerwork.py)
        far_pool = peerworkmod.SharedFarPool(
            far_pool, peerworkmod.parse_peers(args.peers),
            {"roads": args.roads == "on", "grading": args.grading == "on" and args.roads == "on",
             "road_widths": args.road_widths == "on" and args.roads == "on", "airports": args.airports == "on",
             "sea": args.sea == "on", "roads_in_ground": roadsmod.ROADS_IN_GROUND}, None,
            peerworkmod.ReplyCache(os.path.join(args.cache_dir, "peercache")))
    airports_db = None
    if args.airports == "on":       # before any worker process starts: they read this copy
        try:
            airports_db = airportsmod.Store(args.cache_dir, net).load()
            log.info("airports: %s", airports_db.summary())
        except airportsmod.AirportsUnavailable as e:
            log.warning("airports: no runway grading (%s)", e)
    road_pool = None
    if args.block_process == "on" and args.roads == "on" and args.road_processes > 0:
        road_pool = poolsmod.RespawningPool(      # road tiles (blockworker.roads_tile)
            "road tile", args.road_processes, mp_context=ctx, initializer=blockworker.init,
            initargs=(args.cache_dir, args.offline, True, args.grading == "on", args.road_widths == "on",
                      args.surface == "on", None, logging.getLogger().level,
                      args.airports == "on", args.sea == "on", NICE_ROADS, roadsmod.ROADS_IN_GROUND))
    far_bld_pool = None
    if road_pool is not None and args.buildings == "on":
        far_bld_pool = poolsmod.RespawningPool(   # far building tiles (blockworker.far_buildings_tile)
            "far building", 1, mp_context=ctx, initializer=blockworker.init,
            initargs=(args.cache_dir, args.offline, True, args.grading == "on", args.road_widths == "on",
                      args.surface == "on", None, logging.getLogger().level,
                      args.airports == "on", args.sea == "on", NICE_FAR_BUILDINGS, roadsmod.ROADS_IN_GROUND))
    server = Server(args.user_dir, args.cache_dir, args.workers, kfun, args.hysteresis, args.far_radius, fmt,
                    imagery=args.imagery, near_visual=args.near_visual, tex_near=args.tex_near,
                    tex_far=args.tex_far, tone=args.tone, near_radii=radii,
                    leaf_on=args.leaf_on == "on", season_mode=args.season,
                    status_interval=args.status_interval, cells=args.cells, tex=args.tex, block=args.block,
                    horizon=args.horizon, deshadow=args.deshadow, colormatch=args.colormatch,
                    weather=(weathermod.Service(fetch=lambda lat, lon: weathermod.fetch(lat, lon, net=net))
                             if args.weather == "on" else None),
                    gravity=(gravitymod.Provider(os.path.join(args.cache_dir, "gravity"), net=net)
                             if args.gravity == "on" else None), net=net,
                    credits=creditsmod.Credits() if args.credits == "on" else None, cache=disk,
                    retex_rate=args.retex_rate,
                    surface=(surfacemod.Surface(mapdata) if args.surface == "on" else None),
                    mapdata=mapdata if args.roads == "on" else None,
                    grading=args.grading == "on" and args.roads == "on",
                    road_widths=args.road_widths == "on" and args.roads == "on",
                    sidewalks=args.sidewalks == "on",
                    buildings=args.buildings == "on" and args.roads == "on",
                    gas_stations=args.gas_stations == "on" and args.roads == "on",
                    traffic=args.traffic == "on" and args.roads == "on",
                    block_pools=block_pools, far_pool=far_pool, road_pool=road_pool, airports_db=airports_db,
                    far_bld_pool=far_bld_pool,
                    sea=seacarvemod.SeaCarver(mapdata, args.cache_dir) if args.sea == "on" else None,
                    vegetation=(vegetationmod.Vegetation(mapdata, args.cache_dir) if args.trees == "on" else None))
    server.update_check = args.update_check == "on"
    server.geo_tiles = args.geo_tiles == "on"
    server.global_far = args.global_far == "on"
    server.lod_px = args.lod_px
    loop = asyncio.get_running_loop()

    def areas_changed():            # (any thread) every connection gets the saved areas' progress
        def send():
            if server.areas is not None:
                m = server.areas_message()
                for c in list(server.connections):
                    c.send(m)
        loop.call_soon_threadsafe(send)
    try:            # saved areas downloaded ahead (areas.py): the list, its pins, the download thread
        server.areas = areasmod.Areas(args.cache_dir, server, notify=areas_changed).start()
    except Exception as e:      # (no saved areas; never the server)
        log.warning("saved areas: %s", e)
    try:            # the map's cache view: its index built in the background from the cache's keys (covermap.py)
        server.covermap = covermapmod.CoverMap(disk, args.cache_dir, mapdata if args.roads == "on" else None,
                                               server.world_signature).start()
    except Exception as e:      # (the map's view stays empty; never the server)
        log.warning("cache map: %s", e)

    server.game_cpus = getattr(args, "game_cpus", None)    # (cpupin.py: the game pinned at each hello)
    server.server_cpus = getattr(args, "server_cpus", None)
    server.boost_cpus = (cpupin.boosted(server.server_cpus, server.game_cpus)
                         if getattr(args, "load_boost", "on") == "on" else None)

    async def net_log():
        while True:
            await asyncio.sleep(NET_TICK_S)
            line = await asyncio.get_running_loop().run_in_executor(None, net.tick)
            if line:
                rate = net.throughput()     # (a slow connection shows here: GitHub #241's stuck-looking roads)
                log.info("net: %s%s", line, "" if rate is None else f"; large downloads at {rate:.2f} MB/s")
            for c in list(server.connections):     # a host that came back while nobody posed
                c.refresh_status()
    ticker = asyncio.create_task(net_log())

    # Debug: event-loop stalls (every message the game sends waits for them). The loop beats; a thread
    # logs the main thread's stack when a beat is late, so the stall names its cause.
    beat = [time.perf_counter()]
    main_ident = threading.get_ident()

    async def loop_beat():
        while True:
            beat[0] = time.perf_counter()
            await asyncio.sleep(LAG_TICK_S)
    lagger = asyncio.create_task(loop_beat())

    def watch_lag():
        reported = None
        while True:
            time.sleep(LAG_TICK_S)
            b = beat[0]
            lag = time.perf_counter() - b - LAG_TICK_S
            if lag > LAG_REPORT_S and reported != b and log.isEnabledFor(logging.DEBUG):
                reported = b
                f = sys._current_frames().get(main_ident)
                stack = []
                while f is not None and len(stack) < 12:
                    stack.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}:{f.f_lineno}")
                    f = f.f_back
                log.debug("event loop stalled > %.2f s in %s", lag, " < ".join(stack))
    threading.Thread(target=watch_lag, daemon=True, name="lag-watch").start()

    # Debug: where this process's memory is (BNGE_TRACEMALLOC=1; numpy arrays included): the top allocation sites every
    # TRACE_S, for the small-machine budget (a Steam Deck: the main process at 2.3 GB, 2026-10-20)
    if os.environ.get("BNGE_TRACEMALLOC"):
        import tracemalloc

        def trace_mem():
            while True:
                time.sleep(TRACE_S)
                snap = tracemalloc.take_snapshot()
                top = snap.statistics("lineno")[:TRACE_TOP]
                total = sum(st.size for st in snap.statistics("filename"))
                log.info("memory: rss %.0f MB, %.0f MB traced; top: %s", blockworker._rss_mb(), total / 1e6, "; ".join(
                    f"{os.path.basename(st.traceback[0].filename)}:{st.traceback[0].lineno} {st.size / 1e6:.0f} MB"
                    for st in top))
        threading.Thread(target=trace_mem, daemon=True, name="trace-mem").start()

    async def sessions():
        while True:
            await asyncio.sleep(SESSION_SWEEP_S)
            try:
                await server.sweep_sessions_async()
            except Exception:           # a file-system error must not stop the sweeps (or the server)
                log.exception("session sweep failed")
    sweeper = asyncio.create_task(sessions())
    map_srv = None
    if getattr(args, "map_tiles", "on") == "on" and server.imagery is not None:
        try:                        # the world map's tiles, 127.0.0.1 only (a failure only loses the map)
            server.map_tiles = maptilesmod.MapTiles(server.imagery)
            map_srv, server.map_port = await server.map_tiles.start(getattr(args, "map_port", 0))
            log.info("map tiles on http://127.0.0.1:%d/tiles/{z}/{x}/{y}.jpg", server.map_port)
        except OSError as e:
            log.warning("map tiles off: %s", e)
    srv = await asyncio.start_server(
        lambda r, w: Connection(server, r, w).run(), "127.0.0.1", args.port)
    port = srv.sockets[0].getsockname()[1]
    print(f"earthd listening on 127.0.0.1:{port} session {server.sid} -> {server.out_dir} "
          f"(far tiles: {fmt}, near visual: {server.near_visual}, cells: {server.layout.cells} "
          f"(format {server.layout.format}), block: {server.layout.block}, "
          f"horizon: {'on' if server.horizon else 'off'}, "
          f"imagery: {','.join(args.imagery) if args.imagery else 'off'}"
          f"{'' if server.deshadow is None else ', deshadow: ' + server.deshadow.mode}"
          f"{'' if server.colormatch is None else ', colormatch: ' + server.colormatch.mode}, "
          f"weather: {args.weather}, gravity: {args.gravity}, credits: {args.credits}, "
          f"cache cap: {f'{disk.cap / cachemod.GB:g} GB' if disk.cap else 'unlimited'}"
          f"{', OFFLINE' if args.offline else ''})",
          flush=True)
    try:
        async with srv:
            await srv.serve_forever()
    finally:
        ticker.cancel()
        lagger.cancel()
        sweeper.cancel()
        if map_srv is not None:
            map_srv.close()
        if server.areas is not None:
            server.areas.close()
        net.close()                 # today's request counters
        disk.close()                # the cache's last uses


# (shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while))
# def share_port(asked=None, env=None):
#     """The port our BeamMP group service listens on: BNGE_SHARE_PORT on this machine, else the BeamMP server's (its
#     plugin's SHARE_PORT, through the mod's "share"), else peerwork.SHARE_PORT; an unusable one is skipped."""
#     env = os.environ.get("BNGE_SHARE_PORT") if env is None else env
#     for p in (env, asked):
#         try:
#             p = int(p)
#         except (TypeError, ValueError):
#             continue
#         if 1024 <= p <= 65535:
#             return p
#     return peerworkmod.SHARE_PORT


def parse_cache_cap(text):
    """--cache-cap: GB >= 0 (0 = unlimited)."""
    try:
        cachemod.cap_bytes(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return float(text)


def parse_tex_px(text):
    """--tex-near / --tex-far: a power of two from 16 to 2048 (render() maps the texture in
    MESH_CELLS equal cells, and the DXT1 mip chain halves it down to 1 x 1)."""
    px = int(text)
    if px < 16 or px > 2048 or px & (px - 1):
        raise argparse.ArgumentTypeError("texture size must be a power of two from 16 to 2048")
    return px


def parse_tex_list(text):
    """--tex: texture px per level from the finest, the last value repeating: '256,256,256,512'."""
    return tuple(parse_tex_px(v) for v in text.split(","))


def parse_radii(text):
    """'150,400' -> (150.0, 400.0): the radii of the exact level and the next one (32 cells: L-2,
    L-1; 64 cells: L-1, L0)."""
    r2, r1 = (float(v) for v in text.split(","))
    if not 0 <= r2 <= r1:
        raise argparse.ArgumentTypeError("need 0 <= exact-level radius <= next-level radius")
    return r2, r1


OPEN_FILES = 65536      # raise_open_files: the soft limit wanted


def raise_open_files():
    """Unix: the soft limit on open files raised to the hard one (at most OPEN_FILES), before any worker starts (they
    inherit it). Every decoded DEM tile a process keeps is a memory-mapped file (dem.py's mirror), and CPython's mmap
    keeps its own descriptor: a few hundred per worker soon after a start, and the default soft limit of 1024 ran out
    on a drive (GitHub #99: "Too many open files": far tiles and map cells failed)."""
    try:
        import resource
    except ImportError:             # (Windows: no such limit)
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = OPEN_FILES if hard == resource.RLIM_INFINITY else min(hard, OPEN_FILES)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            log.info("open files limit: %d -> %d", soft, want)
    except (ValueError, OSError) as e:
        log.warning("open files limit not raised: %s", e)


def prune_own_caches(cache_dir, started=None):
    """The caches outside the cap's MANAGED folders, each cut back to its own limit at start (cache.prune_dir): the map
    data and DEM mirrors, sea carving's raw rasters, compute sharing's kept replies, the road widths' walls. Each grew run after run past its
    limit (35 GB on a Steam Deck under a 20 GB cap, 2026-10-23: players' C: drives filled). Then the older versions'
    folders of the per-cell caches (cache.prune_versions: never read again, never deleted before: 8 GB of
    vegetation/v1..v17 here, 2026-10-28) and the revisit sidecar's compaction (revisit.compact: the files written
    before `started`, without the lines whose zip the prune removed)."""
    try:
        n = mapdatamod.prune_mirror(cache_dir)
        if n:
            log.info("cache: mapdata-mirror cut back: %d layer files' mirrors removed", n)
    except Exception as e:
        log.warning("cache: mapdata-mirror not cut back: %s", e)
    for sub, mb, match in (("seacarve", seacarvemod.RAW_MB, lambda n: n.endswith(".raw.npy")),
                           (os.path.join("dem", "decoded"), demmod.MIRROR_MB, lambda n: n.endswith(".npy")),
                           ("peercache", peerworkmod.CACHE_BYTES / 1e6, None),
                           ("roadwidth", roadwidthmod.STORE_MB, lambda n: n.endswith(".npy")),
                           (revisitmod.SUBDIR, revisitmod.MAX_MB,
                            lambda n, kept=areasmod.pinned_world(cache_dir): n.endswith(".zip") and n[:-4] not in kept)):
        try:
            n, left = cachemod.prune_dir(os.path.join(cache_dir, sub), mb * 1e6, match=match)
            if n:
                log.info("cache: %s cut back: %d files removed, %.2f GB left", sub, n, left / 1e9)
        except Exception as e:      # a cache's trim must never stop the server
            log.warning("cache: %s not cut back: %s", sub, e)
    for sub, version in (("vegetation", vegetationmod.VERSION), ("seacarve", seacarvemod.VERSION),
                         ("roadwidth", roadwidthmod.WALLS_VERSION)):
        try:
            n, freed = cachemod.prune_versions(os.path.join(cache_dir, sub), f"v{version}")
            if n:
                log.info("cache: %s: %d older version folders removed (%.2f GB)", sub, n, freed / 1e9)
        except Exception as e:
            log.warning("cache: %s's older versions not removed: %s", sub, e)
    try:
        kept = revisitmod.compact(cache_dir, started)
        if kept:
            log.info("cache: revisit sidecar compacted: %d items", kept)
    except Exception as e:
        log.warning("cache: revisit sidecar not compacted: %s", e)
    finally:
        revisitmod.COMPACTED.set()


def _app_modules():
    """apppaths and bmplaunch: beside launcher.py, one folder up (the packaged app: in the bundle)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.append(root)
    import apppaths
    import bmplaunch
    return apppaths, bmplaunch


def saved_cache_dir():
    """The cache folder chosen on the settings page or with the app's --cache-dir (apppaths), or None."""
    try:
        return _app_modules()[0].saved_cache_dir()
    except Exception:
        return None


def app_paths_message(cache_dir, result):
    """app_paths for the settings page: the cache folder in use and the one kept for the next start, the BeamMP
    launcher (the player's choice, else the one found), whether a window is open (picking), and result: the answer to
    the last choice ({what, ok, text})."""
    try:
        ap, bm = _app_modules()
        dd = ap.data_dir()
        saved = ap.saved_cache_dir(dd) or ap.default_cache_dir()
        launcher = bm.remembered(dd) or bm.find(dd) or ""
    except Exception as e:          # (a source copy without them: the page shows the folder in use only)
        log.debug("app paths: %s: %s", type(e).__name__, e)
        saved, launcher = cache_dir, ""
    m = {"type": "app_paths", "cacheDir": os.path.abspath(cache_dir), "cacheNext": os.path.abspath(saved),
         "beammp": launcher}
    if result:
        m["result"] = result
    return m


def pick_app_path(what):
    """The system's window for the cache folder or the BeamMP launcher: apppaths.pick's (state, path)."""
    ap, bm = _app_modules()
    if what == "cache":
        return ap.pick("folder", "BeamNG.Earth: the folder for the download cache",
                       start=ap.saved_cache_dir() or ap.default_cache_dir())
    cur = bm.remembered(ap.data_dir()) or bm.find(ap.data_dir())
    return ap.pick("file", f"BeamNG.Earth: your BeamMP launcher ({bm.EXE})",
                   start=os.path.dirname(cur) if cur else None, name=bm.EXE)


def set_app_path(what, path):
    """Keep a typed or chosen path for the next start: {what, ok, text}."""
    ap, bm = _app_modules()
    if what == "cache":
        d, err = ap.set_cache_dir(path)
        if err:
            return {"what": what, "ok": False, "text": err}
        return {"what": what, "ok": True,
                "text": f"{d or ap.default_cache_dir()}: used from the next start of BeamNG.Earth"}
    exe = bm.remember(ap.data_dir(), path)
    if not exe:
        return {"what": what, "ok": False, "text": f"no {bm.EXE} in {ap.clean(path)}"}
    return {"what": what, "ok": True, "text": f"{exe}: used from the next start of BeamNG.Earth"}


def main():
    # (this run's id, inherited by its worker processes: roadz keeps one lidar decision per map cell and run in it)
    os.environ.setdefault("BNGE_ROADZ_SESSION", f"{os.getpid()}.{int(time.time())}")
    home = os.path.expanduser("~")
    ap = argparse.ArgumentParser(description="BeamNG.Earth companion server")
    ap.add_argument("--port", type=int, default=47613, help="TCP port on 127.0.0.1 (0 = any free port)")
    ap.add_argument("--user-dir", default=(os.path.join(os.environ.get("LOCALAPPDATA", home), "BeamNG", "BeamNG.drive",
                                                     "current") if sys.platform == "win32"
                                        else os.path.join(home, ".local/share/BeamNG/BeamNG.drive/current")),
                    help="BeamNG user folder; output goes to <user-dir>/temp/beamngEarth/<sessionId>/")
    ap.add_argument("--cache-dir", default=saved_cache_dir() or os.path.join(home, ".cache/beamng-earth"),
                    help="download cache root (DEM tiles go to <cache-dir>/dem/; default: the folder chosen on the "
                         "settings page or with the app's --cache-dir, else .cache/beamng-earth in the home folder)")
    ap.add_argument("--peers", default=os.environ.get("BNGE_PEERS", ""),
                    help="compute sharing: helpers (HOST[:PORT] ...; peerwork.py serve on them) that build far tiles "
                         "when this server's own workers are busy (default: BNGE_PEERS)")
    ap.add_argument("--workers", type=int, default=WORKERS, help="far-tile worker threads (keep <= 3: the game needs the cores)")
    ap.add_argument("--far-format", choices=farfield.FORMATS, default="cdae",
                    help="far-tile shape format: binary .cdae (default, ~5x faster to load) or Collada .dae")
    ap.add_argument("--lod-k", type=float, default=None,
                    help="far-field LOD: one K for every level (default: K(L1,L2)=3, K(L3+)=2.5)")
    ap.add_argument("--hysteresis", type=float, default=farfield.HYSTERESIS,
                    help="a refined tile stays refined until the player is (1 + h) x its switch distance away")
    ap.add_argument("--far-radius", type=float, default=farfield.RADIUS, help="far-field radius (m)")
    ap.add_argument("--nice", type=int, default=NICE, help="process niceness increment (0 = leave as is)")
    ap.add_argument("--pin", choices=("on", "off"), default="on",
                    help="Linux and Windows: the server on the last quarter of the physical cores and the game on "
                         "the rest (cpupin.py; default on; BNGE_SERVER_CORES=<n> for n cores)")
    ap.add_argument("--load-boost", choices=("on", "off"), default="on",
                    help="Linux or Windows with --pin on: after a teleport, while the player waits for the world, the server may "
                         "also use half of the game's cores (at idle priority) until the far field is done or the "
                         "player moves (cpupin.boosted; default on)")
    ap.add_argument("--sched", choices=("auto", "idle", "normal"), default="auto",
                    help="Linux: idle scheduling (the server runs only on CPU time the game leaves) or normal; auto "
                         "(default): idle unless the server has cores of its own (--pin)")
    ap.add_argument("--imagery", type=imagerymod.parse_chain, default=imagerymod.DEFAULT_CHAIN,
                    help="satellite imagery source chain, first with data wins: any of usgs,eox2023,eox2016, "
                         "or 'off' for the v1.2 palette look (default: usgs,eox2023,eox2016)")
    ap.add_argument("--near-visual", choices=NEAR_VISUALS, default="mesh",
                    help="mesh: the ground near the player is drawn by 64/128 m tiles and the terrain block is "
                         "physics only (default); terrain: the v1.2 look (the terrain block is drawn); detail: the "
                         "mesh layout with the physics block drawn as the near ground (aerial photo base colour, "
                         "surface detail textures)")
    ap.add_argument("--cells", type=int, choices=farfield.CELL_COUNTS, default=None,
                    help="tile cells per edge: 64 = docs/V1_4_SPEC.md M1 (FORMAT 4, exact level L-1; the "
                         "default in mesh mode), 32 = the v1.3.1 layout (FORMAT 3; terrain mode always)")
    ap.add_argument("--block", type=int, choices=tuple(FOLLOW_RULES), default=None,
                    help="terrain block samples per edge: 512 = docs/V1_4_SPEC.md M4 (on a 128 m grid, "
                         "hello.blockSize; mesh mode only; the default in mesh mode), 1024 = the v1.3.1 block "
                         "(terrain mode always)")
    ap.add_argument("--horizon", action=argparse.BooleanOptionalAction, default=None,
                    help="docs/V1_4_SPEC.md M1h: the far field out to the line-of-sight horizon (a disc up to "
                         "1500 km from the eye's height above sea level and the terrain in sight, L10 tiles, "
                         "per-tile horizon cull; hello.horizon, status.radius; mesh mode only; on by default in "
                         "mesh mode since 2026-10-11, --no-horizon for the fixed far radius)")
    ap.add_argument("--tex", type=parse_tex_list, default=None,
                    help="texture size (px) per level from the finest, the last value repeating "
                         "(default: 64 cells 256,256,256,512; 32 cells from --tex-near / --tex-far)")
    ap.add_argument("--tex-near", type=parse_tex_px, default=TEX_NEAR,
                    help="32 cells: texture size (px) of the two finest levels")
    ap.add_argument("--tex-far", type=parse_tex_px, default=TEX_FAR, help="32 cells: texture size (px) of the other levels")
    ap.add_argument("--leaf-on", choices=("on", "off"), default="on",
                    help="leaf-off imagery (Maryland's six-inch photos: bare trees, dormant grass) takes the summer "
                         "mosaic's colour in its vegetation (season.py; default on)")
    ap.add_argument("--season", choices=("auto", "spring", "fall", "winter", "off"), default="auto",
                    help="the season in the imagery and the trees' leaves: auto = today's date at the place (fall "
                         "colours from about October at 40 N, bare winter canopy from late November, the spring "
                         "flush in April), spring / fall / winter = that season in full, off = the summer look "
                         "(season.py, the mod's seasonlook.lua)")
    ap.add_argument("--tone", type=float, default=1.0,
                    help="strength of the imagery tone curve (0 = the imagery as downloaded)")
    ap.add_argument("--deshadow", choices=deshadowmod.MODES, default="off",
                    help="undo the capture sun's shading in the USGS imagery (docs/research/"
                         "2026-10-10-deshadow-prototype.md): lambert = slopes only, full = slopes and cast "
                         "shadows of the terrain; off = the imagery as served (default)")
    ap.add_argument("--colormatch", choices=colormatchmod.MODES, default=None,
                    help="remove the colour seams between NAIP flights in the USGS imagery (docs/research/"
                         "2026-10-10-imagery-colormatch.md): s2 = the seam feather against Sentinel-2 (EOX), "
                         "only near detected survey seams (the default when the imagery chain has USGS, since "
                         "the in-game checks of 2026-10-11); off = the imagery as served")
    ap.add_argument("--retex-rate", type=float, default=RETEX_RATE,
                    help="--colormatch / --deshadow: textures of tiles on screen rendered again (and swapped in the "
                         "game with far_retex) per second at most once a region they were rendered without is ready, "
                         "nearest first, one at a time on the tile pool (0 = none: such a tile keeps its texture until "
                         "it is sent again)")
    ap.add_argument("--near-radii", type=parse_radii, default=None,
                    help="mesh mode: radii in m around the player of the exact level and the next one "
                         "(L-2, L-1 with 32 cells; L-1, L0 with 64; default 150,400)")
    ap.add_argument("--status-interval", type=float, default=STATUS_INTERVAL,
                    help="re-send the last status with the current far-tile backlog at most every S seconds "
                         "while it changes (0 = status on events only, as v1.3.1; tools/drive.py runs use 1)")
    ap.add_argument("--lod-px", type=float, default=farfield.SSE_TAU_PX,
                    help="screen-space LOD: far tiles also split while their split would pop more than this many "
                         "pixels on the player's screen (camera, field of view, resolution; 0: distance rule only)")
    ap.add_argument("--update-check", choices=("on", "off"), default="on",
                    help="ask GitHub for a newer version after a game connects (updates.py; off: tests)")
    ap.add_argument("--weather", choices=("on", "off"), default="on",
                    help="real weather at the player from Open-Meteo.com (keyless; every 10 min or 25 km)")
    ap.add_argument("--gravity", choices=("on", "off"), default="on",
                    help="real gravity at the player (gravity.py: WGS84 normal + EGM2008 from <cache-dir>/gravity/, "
                         "every 2 km or 200 m of height; normal gravity while EGM2008 downloads, once, 74 MB)")
    ap.add_argument("--bridge-profile", choices=("curve", "legacy"), default="curve",
                    help="bridge decks: curve (grades of 5 %% up to what they must clear, joined by vertical curves, "
                         "a chain solved as one line) or legacy (deck_heights' line, tents, smoothing)")
    ap.add_argument("--offline", action="store_true",
                    help="no network: serve elevation and imagery from the disk cache only (uncached tiles get "
                         "the palette, uncached terrain fails); the status says 'Offline - using cached data'")
    ap.add_argument("--roads", choices=("on", "off"), default="on",
                    help="Overture roads near the player (roads.py): DecalRoads in the mod (paved asphalt, "
                         "gravel / dirt tracks) within ~640 m, in 256 m tiles; off = none")
    ap.add_argument("--airports", choices=("on", "off"), default="off",
                    help="runways graded into the ground and the tiles (OurAirports, airportgrade.py); "
                         "the data is downloaded once (~5 MB) and refreshed weekly")
    ap.add_argument("--sea", choices=("on", "off"), default="off",
                    help="sea and lake water (seacarve.py, seatiles.py): the ground carved under Overture's ocean "
                         "and lake polygons and the mod's WaterBlock tiles that follow the Earth's curve")
    ap.add_argument("--geo-tiles", choices=("on", "off"), default="on",
                    help="content in the global tiles' own frames (geotiles.py: the vegetation, the road tiles' content, "
                         "the far buildings, skyline and far bridges), placed by the mod and kept across rebases "
                         "instead of built again for each anchor; off: per anchor (A/B)")
    ap.add_argument("--global-far", choices=("on", "off"), default="on",
                    help="far-field levels from 3 (2 km) up as global Web-Mercator tiles in their own frames "
                         "(farfield.select_global): built once, cached for any anchor, kept by the mod across rebases; "
                         "needs --geo-tiles on; off: the anchor's quadtree at every level (A/B)")
    ap.add_argument("--trees", choices=("on", "off"), default="off",
                    help="trees, bushes and rocks near the player (vegetation.py): from the canopy height model and "
                         "land cover, Forest items of stock shapes in the mod")
    ap.add_argument("--road-processes", type=int, default=ROAD_PROCESSES,
                    help="processes building the road tiles (--block-process on); 0 = threads in the server; left at "
                    "the default: 2-4 by the machine's threads and memory (budget.road_processes)")
    ap.add_argument("--download-ahead", choices=("on", "off"), default="on",
                    help="fetch the elevation tiles and map cells 10-60 s ahead of a driving player (AHEAD_S)")
    ap.add_argument("--route-prefetch", choices=("on", "off"), default="on",
                    help="prefetch a planned GPS route's corridor (elevation tiles and road/building map cells) up "
                         "to --route-prefetch-km ahead, at the same low priority as --download-ahead")
    ap.add_argument("--route-prefetch-km", type=float, default=ROUTE_AHEAD_MAX_M / 1000.0,
                    help="how far along a planned GPS route to prefetch (15-25 km is sane; 0 disables it)")
    ap.add_argument("--detail-ring", type=float, default=DETAIL_RING_M,
                    help="detail mode: metres of L-1 tiles around the drawn block (0 = none)")
    ap.add_argument("--background-duty", type=float, default=pacer.CLI_DUTY,
                    help="share of the time the background imagery analyses (colour seams, de-shadow) may run "
                         "(pacer.py): their memory traffic slowed the game's rendering up to 2x; 1 = unpaced")
    ap.add_argument("--sidewalks", choices=("on", "off"), default="on",
                    help="raised sidewalks (curbs and slabs, sidewalks.py) with the road tiles; off = the roads "
                         "without them")
    ap.add_argument("--buildings", choices=("on", "off"), default="off",
                    help="3D buildings from the Overture footprints with the road tiles (buildings.py; needs --roads "
                         "on): heights and roofs predicted where the data has none")
    ap.add_argument("--gas-stations", choices=("on", "off"), default="off",
                    help="gas stations with refuelling at real stations (gasstations.py; Overture places, needs "
                         "--roads on): West Coast USA's pumps and canopies, the stock refuel prompt")
    ap.add_argument("--traffic", choices=("on", "off"), default="on",
                    help="the AI road graph around the player for BeamNG's own traffic and AI (navgraph.py; needs "
                         "--roads on): the mod hands it to map.reset. On by default since 2026-10-14: whether cars "
                         "spawn is the player's choice in the game (level-start options, radial menu)")
    ap.add_argument("--road-widths", choices=("on", "off"), default="on",
                    help="street widths measured between the buildings that line them (roadwidth.py; Overture "
                         "buildings); off = class defaults")
    ap.add_argument("--grading", choices=("on", "off"), default="on",
                    help="grade the ground under roads (grading.py: smooth along, flat across, 1:2 slopes back "
                         "to the natural ground) in the physics block and the near mesh tiles; needs --roads on")
    ap.add_argument("--block-process", choices=("on", "off"), default="on",
                    help="build the follow / prefetch physics blocks in processes of their own (blockworker.py: one "
                         "for the mod's requests, one for the prefetch; their own cores, off this process's "
                         "interpreter lock); off = in this process's threads")
    ap.add_argument("--surface", choices=("on", "off"), default="on",
                    help="ground models under the physics block from ESA WorldCover land cover and the slope "
                         "(surface.py: asphalt, grass, dusty / loose dirt, rock, snow, mud, dirt), imported as 8 "
                         "terrain layers with each block (the mod's grass grows on them); off = dirt everywhere")
    ap.add_argument("--map-tiles", choices=("on", "off"), default="on",
                    help="serve the in-game world map's tiles (the imagery chain, z/x/y JPEG) over HTTP on "
                         "127.0.0.1; its port goes in hello.mapPort (needs imagery)")
    ap.add_argument("--map-port", type=int, default=0,
                    help="port of the map tile endpoint (default 0: any free port, sent in hello.mapPort)")
    ap.add_argument("--credits", choices=("on", "off"), default="on",
                    help="'credits' messages: the imagery / elevation / weather sources of the tiles within 2 km "
                         "of the player and of the far field, for the mod's on-screen data credits (on change, "
                         "at most once a second)")
    ap.add_argument("--cache-cap", type=parse_cache_cap, default=None, metavar="GB",
                    help="size cap of the download cache in GB (0 = unlimited; default: the last cap set on the "
                         f"mod's settings page, else {cachemod.DEFAULT_CAP_GB:g}); least recently used files go, "
                         "never what this session used within the hour, the gravity model or netstats/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    os.environ["BNE_BRIDGE_PROFILE"] = args.bridge_profile     # read by roads.py here and in the block workers
    args.cells = resolve_cells(args.cells, args.near_visual)
    args.block = resolve_block(args.block, args.near_visual)
    args.horizon = resolve_horizon(args.horizon, args.near_visual)
    args.colormatch = resolve_colormatch(args.colormatch, args.imagery)
    if args.cells != 32 and args.near_visual not in MESH_LAYOUT:
        ap.error("--cells 64 needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    if args.block != heightmap.SIZE and args.near_visual not in MESH_LAYOUT:
        ap.error("--block 512 needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    if args.horizon and args.near_visual not in MESH_LAYOUT:
        ap.error("--horizon needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise_open_files()
    import fast
    try:
        fast.require(fast.MIN_VERSION, "the world server")     # at start, not in the middle of a drive
    except fast.NativeMissing as ex:
        log.error("%s", ex)
        raise SystemExit(str(ex))
    import sampler
    sampler.maybe_start()           # BNGE_SAMPLE=<path>: a sampling profile of this process's threads (debugging)
    import budget
    if args.nice:
        try:
            # all threads: below the game's main and physics threads (Windows: the "below normal" class)
            how = budget.lower_priority(args.nice)
            if how:
                log.info("server priority: %s", how)
        except (OSError, AttributeError) as e:
            log.warning("could not lower the process priority: %s", e)
    if args.sched == "auto":
        # Idle scheduling keeps the server off the game's CPU time when they share cores; with cores of its own (the
        # split below) it only made every server process equal: the workers' niceness (the ground under the car
        # first, NICE_ROADS / NICE_FAR) counts for nothing between idle-class tasks, and on a Steam Deck a teleport's
        # block took 105-170 s behind the far field and the road tiles (2026-10-20; 1.6 s on a desktop)
        split = cpupin.split(budget.SMALL_SERVER_SHARE if budget.small() else cpupin.SERVER_SHARE) \
            if args.pin == "on" and (sys.platform.startswith("linux") or sys.platform == "win32") else (None, None)
        args.sched = "normal" if split[0] else "idle"
    if args.sched == "idle" and hasattr(os, "sched_setscheduler") and hasattr(os, "SCHED_IDLE"):
        # Linux: CPU time only when a core would idle otherwise (the worker processes and threads made after this
        # inherit it). A recorded session (2026-10-15): with the server over 3 cores the game's physics fell behind
        # real time in 22 % of seconds (5 % under 1 core) and frame spikes nearly tripled; niceness alone still
        # gives it slices of the game's cores
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
        except OSError as e:
            log.warning("could not set idle scheduling: %s", e)
    if os.environ.get("BNGE_TRACEMALLOC"):      # (before the caches are made: main_async's trace_mem reports them)
        import tracemalloc
        tracemalloc.start(1)
    game_cpus = server_cpus = None
    small = budget.apply()
    if small:
        # a small machine (a Steam Deck): fewer worker processes, each with smaller caches (budget.py), where the
        # player left the defaults
        if args.road_processes == ROAD_PROCESSES:
            args.road_processes = budget.SMALL_ROAD_PROCESSES
        if args.workers == WORKERS:
            args.workers = budget.SMALL_FAR_WORKERS
        if budget.SMALL_ANALYSES_OFF:
            args.deshadow, args.colormatch = "off", "off"
        log.info("small machine (%.1f GB): %d far-tile and %d road-tile processes, smaller caches, imagery analyses "
                 "off (budget.py; BNGE_BUDGET=desktop for the desktop sizes)", budget.total_gb() or 0, args.workers,
                 args.road_processes)
    elif args.road_processes == ROAD_PROCESSES:
        args.road_processes = budget.road_processes()
        log.info("road-tile processes: %d (%d threads, %.0f GB)", args.road_processes, os.cpu_count() or 0,
                 budget.total_gb() or 0)
    if args.pin == "on" and (sys.platform.startswith("linux") or sys.platform == "win32"):   # (Windows: KyzoFox, PR #112)
        try:
            server_cpus, game_cpus = cpupin.split(budget.SMALL_SERVER_SHARE if small else cpupin.SERVER_SHARE)
            cpupin.pin_self(server_cpus)
            if server_cpus:
                log.info("cores: the server on CPUs %s, the game on %s (cpupin.py; --pin off)",
                         ",".join(map(str, sorted(server_cpus))), ",".join(map(str, sorted(game_cpus))))
        except OSError as e:
            log.warning("could not split the cores: %s", e)
            game_cpus = None
    args.game_cpus = game_cpus
    args.server_cpus = server_cpus if game_cpus else None
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
