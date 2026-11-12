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
import errno  # noqa: E402
import glob  # noqa: E402
import functools  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402
import shutil  # noqa: E402
import collections  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict, deque  # noqa: E402
import concurrent.futures  # noqa: E402
import multiprocessing  # noqa: E402
from runtimeprofiler import ThreadPoolExecutor  # noqa: E402
from dataclasses import dataclass, field, replace  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import blockworker  # noqa: E402
import terraintex as terraintexmod  # noqa: E402
import cache as cachemod  # noqa: E402
import cdae  # noqa: E402
import colormatch as colormatchmod  # noqa: E402
import credits as creditsmod  # noqa: E402
import secrossings  # noqa: E402
import covermap as covermapmod  # noqa: E402
import areas as areasmod  # noqa: E402
import surface as surfacemod  # noqa: E402
import navgraph as navgraphmod  # noqa: E402
import distanttraffic          # noqa: E402
import prewarm as prewarmmod  # noqa: E402
import parking as parkingmod  # noqa: E402
import gasstations as gasstationsmod  # noqa: E402
import roads as roadsmod  # noqa: E402
import roadsurf as roadsurfmod  # noqa: E402
import reach as reachmod  # noqa: E402
import privacyregion as privacyregionmod
import perfstats  # noqa: E402
import rails as railsmod  # noqa: E402
import randomplace as randomplacemod  # noqa: E402
import urgency as urgencymod  # noqa: E402
import placepreview as placepreviewmod  # noqa: E402
import placenames as placenamesmod  # noqa: E402
import maplabels as maplabelsmod  # noqa: E402
import places as placesmod  # noqa: E402
import runtimeprofiler
import streamleases
import pools as poolsmod  # noqa: E402
import memwatch as memwatchmod  # noqa: E402
import plancache as plancachemod  # noqa: E402
import grading as gradingmod  # noqa: E402
import roadmesh as roadmeshmod  # noqa: E402
import roadwear  # noqa: E402
import roadwidth as roadwidthmod  # noqa: E402
import revisit as revisitmod  # noqa: E402
import signstream  # noqa: E402
import roadsigns as roadsignsmod  # noqa: E402
import designs as designsmod  # noqa: E402
import dirsigns as dirsignsmod  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import dem as demmod  # noqa: E402
import mapcells  # noqa: E402
import pacer  # noqa: E402
import peerwork as peerworkmod  # noqa: E402
import deshadow as deshadowmod  # noqa: E402
import farfield  # noqa: E402
import farpvs as farpvsmod  # noqa: E402
import bc7  # noqa: E402
import terraincache  # noqa: E402
import terrainpatch as terrainpatchmod  # noqa: E402
import geodesy  # noqa: E402
from types import SimpleNamespace  # noqa: E402
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
import development as developmentmod  # noqa: E402
import townentry as townentrymod  # noqa: E402     (PR #597's town entry and exit signs)
import heightmap  # noqa: E402
import l0roads  # noqa: E402
import staging  # noqa: E402
import imagery as imagerymod  # noqa: E402
import matslots as matslotsmod  # noqa: E402
import maptiles as maptilesmod  # noqa: E402
import geocode as geocodemod  # noqa: E402
import stationindex as stationindexmod  # noqa: E402
import routing as routingmod  # noqa: E402
import updates as updatesmod  # noqa: E402
import cpupin  # noqa: E402
import netstate as netstatemod  # noqa: E402
import netfetch  # noqa: E402
import protocol  # noqa: E402
import weather as weathermod  # noqa: E402

PROTOCOL = 4
SEND_BUFFER_MAX = 32 * 1024 * 1024  # pending JSON control bytes per transport; reconcile on reconnect (PR #817 had 8 MiB:
                                    # more room for a teleport's burst before a reconnect)
WATERGUARD_VERSION = 1
TELEMETRY_CONSENT = 3           # telemetry.CONSENT_VERSION (the hello tells the welcome page; kept equal by a test)
_NO_PERF = perfstats.Collector()    # (a server object without one: tests' stand-ins)


def _borders(server):
    """Whether the road rules' country (navgraph.country_at) asks the real borders first: where the regional scenery is
    on (--regional-poles, on by default), whose border data the server fetches at its start anyway; off, the nearest
    airport alone as before (no border download)."""
    return getattr(server, "regional_poles", "off") != "off"


def _perf(server):
    return getattr(server, "perf", None) or _NO_PERF


def _catchup_note(conn, on):
    """A connection's catch-up boost came on / went off: counted for the anonymous performance data (perfstats) and told
    to the mod ('catchup'), which tags its slow frames with it (perfsummary.lua: does lending cores cost the game frames?)."""
    _perf(conn.s).catchup(on)
    send = getattr(conn, "send", None)
    if send is not None:
        send({"type": "catchup", "on": bool(on)})


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
    """The --block value to use: the given one, else CLI_BLOCK_MESH (1 km) in mesh / detail mode and TERRAIN_BLOCK
    (its frozen 2 km block) in terrain mode."""
    if block is not None:
        return block
    return CLI_BLOCK_MESH if near_visual in MESH_LAYOUT else TERRAIN_BLOCK


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
TUNNEL_NEAR_M = 40.0          # m round a teleport's spot searched for a tunnel's tube (tunnel_floor)
TUNNEL_FLOOR_UNDER_M = 2.0     # a tunnel floor this far under the ground at a teleport's origin: the car goes on the floor
SEA_CHECK_S = 15.0          # sea (--sea on): the water tiles planned again when the player enters another
LOW_ROAD_CELLS = 64             # map cells whose road points (seatiles.road_points) the sea plan keeps per connection
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
AHEAD_CELLS = 4                 # map cells download_ahead fetches at once
AHEAD_FRONTIER_MAX = 4          # the time-to-reach tree's frontier points (its likely branches' data ends) fetched too
AHEAD_BATCH = os.environ.get("BNGE_AHEAD_BATCH", "on") != "off"     # a round's map tiles fetched in batches first
# Streaming by time to reach along the road graph (reach.py; the retro optimization study's section J, 2026-11-11): a
# fast car's road geotiles, prefetched terrain blocks, map cells, road plans and photos ahead follow the roads it is
# likely to take, in order of when it gets there, instead of a straight line by distance. BNGE_REACH=off: as before.
REACH_ON = os.environ.get("BNGE_REACH", "on") != "off"
AHEAD_LAYERS = ("roads", "buildings", "building_parts", "infrastructure", "water", "land_use")
# route_download: like download_ahead, but along the planned GPS route's own geometry (routeahead.py)
# instead of the player's current heading, since a road route can wander off that heading entirely.
ROUTE_AHEAD_ON = True            # --route-prefetch
ROUTE_AHEAD_MAX_M = 20000.0      # --route-prefetch-km * 1000 (15-25 km is sane; the user, 2026-10-22)
# the GPS's "Download along route" setting (the user, 2026-10-29: "options on pre downloading/generating along a gps
# route"): the route message's "download": off, ahead (ROUTE_AHEAD_MAX_M) or the whole route (at most this far)
ROUTE_DOWNLOAD_MODES = ("off", "ahead", "route")
ROUTE_429_TRIES = 3              # a route answered 429 (routing.openstreetmap.de's fair use) is asked again while it is
ROUTE_429_WAIT_S = 3.0           # the latest destination, after the host's Retry-After or this x the try ...
ROUTE_429_MAX_WAIT_S = 20.0      # ... at most this
ROUTE_SAME_DEST_DEG = 1e-5       # destinations this close (about a metre) are the same route planned again: the mod
                                 # re-sends its destination unchanged and a re-plan moves only the start
ROUTE_WHOLE_MAX_M = 500000.0
ROUTE_IMG_TRIES = 5               # a route download's ground-photo tile the limiter deferred: asked this often
ROUTE_RETRY_S = 2.0               # a deferred tile/cell backs off this long before the next one in the corridor
# (70 m/s until 2026-10-31: at 130-157 mph each block waited 2.5 s for its photo, then its stand-in, the edge guard
# holding the car; the user: "lower it or do whatever you need to do to get high speed driving viable". The stand-in
# has the roads painted in and the last photos' overlap now, so the swap to the photo shows less)
PHOTO_SKIP_V = 55.0             # m/s (123 mph): faster, a requested block goes with its stand-in photo too (make_heightmap)
FAST_BASE_V = 40.0              # m/s (89 mph): faster, a block waits at most BASE_FAST_WAIT_S for its photo after its
SMALL_FAST_BASE_V = 35.0        # m/s (78 mph): FAST_BASE_V on a small machine (budget.small: a Steam Deck)
BASE_FAST_WAIT_S = 0.15         # heights, then goes without it (block_base later): blocks took 0.6-3 s at 600 mph on a
                                # Steam Deck, mostly the photo (heights 0.1-0.4 s), and the game paused 11 times a minute
# A block the game asked for has its photo rendered in the other lane's block process, its own core (_base_process),
# with the imagery analyses on too (de-shadow, colour match: they live in this process; the photo is rendered again
# with them, base_refresh): here, beside the far tiles' and the vegetation's work under this interpreter's lock, its
# road paint (0.3 s alone) took 6-10 s and the blocks were ready 6-12 s after they were asked for, the edge guard
# holding the car at 105-189 mph (2026-10-31, tools/perfmon.py). BNE_URGENT_ELSEWHERE=off: here, as before.
URGENT_ELSEWHERE = os.environ.get("BNE_URGENT_ELSEWHERE", "on") != "off"
# A process of its own for the requested blocks' photos and stand-ins (detail mode) on a machine with this many CPUs:
# in the other lane's block process they queued behind its prefetch heights and photos, a stand-in made beside a
# block 1.6-4 s late at 100-200 mph down Manhattan (nyc_speed.py, 2026-11-02); not on a small machine (memory)
PHOTO_PROCESS_MIN_CPUS = 12


def photo_process(cpus=None, small=None):
    """Whether the requested blocks' photos get their process (PHOTO_PROCESS_MIN_CPUS, not on a small machine: the
    comment above said so, the code only counted CPUs until 2026-11-11, and a 16-thread 16 GB PC ran it at ~0.8-0.9 GB
    beside a ~9.7 GB server; BNGE_PHOTO_PROCESS=on / off overrides)."""
    force = os.environ.get("BNGE_PHOTO_PROCESS", "").lower()
    if force in ("on", "off"):
        return force == "on"
    import budget
    cpus = (os.cpu_count() or 0) if cpus is None else cpus
    small = budget.small() if small is None else small
    return cpus >= PHOTO_PROCESS_MIN_CPUS and not small
# A block waits at most this long for its stand-in photo started beside it (cold: the photo process queued behind
# imagery downloads and a block built in 1.4 s waited 99.5 s for it, nyc_speed.py --cold 2026-11-02)
STANDIN_WAIT_S = 1.0
QUICK_SYNC_MAX_V = 150.0        # m/s (335 mph): slower, a block built ahead gets its stand-in photo before it is ready (224 mph left a player at 230 mph with brown flashes)
RECENT_FULL = 2                 # full ground photos kept for the stand-ins' overlap (write_quick_base)
QUICK_MIN_COVER = 0.8          # share of a stand-in's texels real imagery must cover (else no stand-in)
QUICK_SHARE_WAIT_S = 30.0       # Server._quick_once: a caller waits this long for the stand-in being made
QUICK_MADE_KEPT = 256           # blocks whose stand-in file names _quick_once remembers
QUICK_BASE_PX = 512             # a block's stand-in photo (write_quick_base): the imagery alone, 2 m texels
BASE_NO_STANDIN_MAX_S = 8.0     # a block with no stand-in possible waits this long at most for its photo
# A teleport's provisional first block waits this long at most for its photo after its heights, then goes with its
# stand-in (made beside it), the photo swapped in when done (_base_later): the car placed ~1-2 s sooner on a fresh place
PROVISIONAL_BASE_WAIT_S = 0.3
# Queued work the car has left behind while driving is dropped, not built (Server.left_behind; the user, 2026-11-11:
# "use this to improve loading during driving as well"): a spot this far from every player on its anchor and behind
# each (against their velocity), or LEFT_FAR_M from all of them whatever the way. The mod drops road tiles past
# roads.DROP_M (960 m) anyway.
LEFT_BEHIND_M = 2000.0
LEFT_FAR_M = 8000.0
LEFT_MIN_V = 3.0                # m/s: slower, "behind" means nothing (only LEFT_FAR_M counts)
PADS_REFRESH_IN_USE = os.environ.get("BNGE_PADS_REFRESH", "on") != "off"     # (Connection.pads_refresh; A/B runs)
# Full priority loading (the settings page's teleportPriority) squeezes the game onto two cores only with this on
FULL_PIN_GAME = os.environ.get("BNGE_FULL_PIN_GAME", "off") == "on"
BASE_WAIT_S = 2.5               # ... and slower, this long: a slow photo held the heightmap thread and the blocks behind
                                # it (a city's first visit at 127 mph: 4.75 s for one photo, the next block 7.8 s and the
                                # car at the edge of its ground; a 4-core PC's photos took ~26 s, GitHub #496); it then
                                # goes with its stand-in, the photo after it (block_base)
FAST_KEEP_S = 20.0              # ... and this long after it last was: a car the game holds for its ground (the drop
                                # hold, the edge guard) reports no speed, and its next block waited for its photo
                                # again (5-10 s at 500 mph on a first visit, Indianapolis 2026-10-30: held half the time)
TRASH_DIR = "trash"         # in the cache: folders of the game's user folder moved out to be deleted (_rmtree_outside)
PROGRESS_S = 0.5            # load_progress at most this often (Connection.send_progress)
ANCHOR_PROGRESS_S = 2.0     # first ground: phase heartbeat, independent of the worker's optional tile counts
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
# Screen-space LOD (the mod's lodPx setting, the 'graphics' message; Connection.view): tau is clamped to LOD_PX_MAX; the
# pixels per radian are the most of the last LOD_PPR_WINDOW_S seconds (a zoom out does not merge the ground at once)
# and at most LOD_PPR_ZOOM times those of a LOD_BASE_FOV_DEG camera (a zoomed-in camera does not pull in the world)
LOD_PX_MAX = 8.0
LOD_PPR_WINDOW_S = 5.0
LOD_BASE_FOV_DEG = 60.0
LOD_PPR_ZOOM = 2.0
LOD_ERRS_MAX = 400000           # known tile errors kept (Server.lod_errs / glod_errs, oldest dropped first)
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
# the status bar's place names of a region not on disk yet (where(), placenames.py): fetched as a background job,
# asked again this often while Overture's hosts are busy with the world's downloads, this many times at most
WHERE_PLACE_RETRY_S, WHERE_PLACE_TRIES = 5.0, 60
# the modding API's places (Connection.places): incomplete requests waiting for their cells at a time, and how long
# each waits (tries x WHERE_PLACE_RETRY_S: 2 min, then its last answer says it is still incomplete)
PLACES_FOLLOWUPS, PLACES_TRIES = 4, 24

HM_CACHE = 16               # heightmaps kept beyond the protected ones (6 before 2026-10-30: a fast player's prefetched
                            # chain (PREFETCH_CHAIN) was evicted before the mod asked for it and built again)
TERRAIN_STUCK_S = 90.0      # s: a terrain request still building this long is logged once (prepare_terrain)
HM_KEEP_COMMITTED = 3       # the current terrain and the last two
HM_KEEP_SENT = 2            # terrains handed to the mod but maybe not committed yet
# Generated far-tile files kept per server, by layout: a 64-cell tile is ~3.7x the bytes (348 KB)
# and a pose needs ~300 tiles instead of ~811 (docs/V1_4_SPEC.md §3.1).
TILE_CACHE = {32: 6000, 64: 2500}
HM_THREADS = 4
HM_REQUEST_THREADS = 2          # Server.hm_req_pool: the request lane's (they wait on its block process)
LAG_TICK_S, LAG_REPORT_S = 0.1, 0.25     # the event-loop stall report (-v)
FIN_WORKER_WINDOW, FIN_WORKER_MIN_TRIES, FIN_WORKER_SHARE, FIN_WORKER_PROBE = 40, 10, 0.3, 5   # (fin_worker_worth)
FIN_WORKER = os.environ.get("BNGE_FIN_WORKER", "1") != "0"     # (A/B: 0 renders them all here, as before)
NAV_LEAD_S, NAV_LEAD_MAX_M = 12.0, 500.0     # the nav graph's centre ahead of a moving car (navgraph.WINDOW_M 1100)
LAG_STALL_S = 2.0                     # s: a stall this long is logged without -v (its stack, then its length)
# The event loop's thread a step above the server's other threads (budget.raise_thread, Windows): a block built 17 s
# earlier held the car at the edge until the loop answered its request (A13 at 200 mph, 2 server cores, 2026-11-04;
# GitHub #725). BNGE_LOOP_RAISE=0: as the others, as before
LOOP_RAISE = os.environ.get("BNGE_LOOP_RAISE", "1") != "0"
# ms the interpreter lock is held before a waiting thread is let in (Python's own: 5): the event loop waits up to this
# long behind each busy thread. BNGE_SWITCH_MS=0: Python's own, as before
SWITCH_MS = float(os.environ.get("BNGE_SWITCH_MS", "2") or 0)
ROAD_PROCESSES = 2              # road-tile processes (--block-process on, --roads on)
# s a road tile may take in its worker before it is given up and its workers restarted (build_roads_tile): a first
# visit's slowest tiles take ~1 min on a busy 6-core laptop and several times that on a Steam Deck, where a tile given
# up at 2 min would be built and given up again for good; BNGE_ROAD_TILE_TIMEOUT=<s> to change it
ROAD_GEO_SLOW_S = 3.0           # a road geotile slower than this is logged (queue and build)
VEG_GEO_BATCH = 32              # trees geotiles one road-process job builds at most (_veg_geo_job): its nearest wait
                                # for the rest of it
VEG_GEO_PER_POSE = 64           # trees geotiles asked for at once when the road processes build them: two jobs' worth
SLOW_SELECT_S = 0.25            # a far-tile selection (update_far, on the event loop) slower than this is logged
WORLD_WARM_Z = 7                # the rough earth's tiles (Server.start_world_warm): 16,384, ~260 km at the equator
WORLD_WARM_CONC = 2
EYE_LOD_MIN_M = 3000.0          # an eye above this: the far tiles' detail by the 3D distance (eye_clearance)
MAP_HIGH_EYE_M = 40000.0        # an eye above this (the 3D map): the finest global tiles are leaves (farfield.select_global)
MAP_RAMP_START = 0.125           # the 3D map at a new anchor: K x this first (Conn.commit_anchor) ...
MAP_RAMP_STEP = 2.0              # ... times this each time that far field is done, up to 1
MAP_OUT_OF_VIEW_K = 3.0          # the 3D map: tiles off the screen chosen as if this many times as far (map_cone)
# A teleport's far field coarse first (the user's goal 2026-11-11, "sub-second world gen even with a clear cache": the
# whole horizon within seconds, then refined). A cold Missoula took 20-35 s to its first far tile (the nearest tiles
# are graded like the block and waited for the road plans of a place never seen, every worker on them; the next ones,
# L1-L2, carve the sea floor and waited for the water's map data) and 45-90 s to the horizon, built last (nearest
# first). Now the first selection is the coarse one: K x FIRST_RAMP_K and nothing finer than the global far tiles
# (farfield.select_global's anchor_nodes off; FIRST_RAMP_MIN_LEVEL without them): no map data waited for, no forced
# block ring, no screen-space split; ~80 tiles about two levels coarser than the final ones at each distance, the same
# for every anchor, built while the first block is (Connection.prebuild_coarse). Once they are all shown (or after
# FIRST_RAMP_MAX_S) the full selection, its swap groups each a coarse tile and the finer ones under it. The block
# (1 km) covers the ground round the car meanwhile (the coarse tile under it sinks: farfield.lua 'partial'). Cold
# Missoula / Rock Springs / Laramie / Cody / Sheridan (tools/farprobe.py): half the horizon shown 1.8-3.4 s after
# the request, 90 % 3.3-5.9 s (31-62 s and 42-77 s before); a revisit 1.3 s / 1.6 s (5.8 / 8.0 s); far_done and CPU
# about the same (the full far field's photos: USGS's ~1900 requests at our 50 a second).
FIRST_RAMP = os.environ.get("BNGE_FIRST_RAMP", "1") != "0"
FIRST_RAMP_K = 0.25
FIRST_RAMP_MIN_LEVEL = 3        # (= farfield.GLOBAL_MIN_LEVEL: the anchor's own levels, sea-carved and graded, below)
# ... their textures at most this many px (FIRST_RAMP_NEAR_PX at level 3, the nearest; their photos two zooms coarser
# than at 512 px: a sixteenth of the requests). EOX allows ~40 requests a second (eox-rate-limit): at 256 px the
# coarse cover's ~300 photo tiles were most of its 7 s at a cold Powell, 2026-11-11. A coarse tile the full selection
# keeps is rendered again at its own size (Connection._stream: stale when sent after the coarse step).
FIRST_RAMP_TEX_PX = int(os.environ.get("BNGE_FIRST_RAMP_PX", "128"))
FIRST_RAMP_NEAR_PX = 256
# ... from these photos alone (Server.coarse_texture): the project's own Sentinel-2 packs first where the CDN switch has
# them in the chain (one request for 8 x 8 tiles; the full texture takes them at those zooms anyway, so the coarse step
# now looks like what replaces it), EOX for the finer zooms (z13+) and without them. EOX's own throttling hits every
# player (the user, 2026-11-11): its coarse-cover tiles (~60-100 a cold French teleport, z7-z12) were fetched only for
# a step the full texture replaces. BNGE_COARSE_EOX=1: EOX alone, as before.
FIRST_RAMP_CHAIN = (("eox2023", "eox2016") if os.environ.get("BNGE_COARSE_EOX") == "1"
                    else ("bngs2", "eox2023", "eox2016"))
FIRST_RAMP_MAX_S = 12.0
# Far tiles that wait for the place's map data (Connection.graded: graded under the roads like the block at levels
# <= 0, the sea floor carved to L2) in flight at most this share of the far workers (at least one): at a place never
# seen their road plans and water take 6-25 s (the map data's first download) and the nearest ten held every worker
# meanwhile, nothing else built (Missoula, Twin Falls 2026-11-11). Once the data is in they take ~100 ms; with nothing
# else left to build they take every worker (the cap orders, it never leaves a worker idle).
GRADED_SHARE = float(os.environ.get("BNGE_GRADED_SHARE", "0.5"))     # (1: no cap, A/B runs)
MAP_ZOOM_RAMP_K = 8.0            # the 3D map's eye this many times lower than before: coarse first again (on_pose)
MAP_FOCUS_MAX_M = 1200000.0      # a map_focus farther from the anchor than this is ignored (mod map3d REBASE_M)
FAR_TRACE = os.environ.get("BNGE_FARTRACE") == "1"     # (tools: every far tile sent, logged)
MAP_TEX_PX = 512                # the 3D map's far textures at least this (Server.tex_size) ...
MAP_TEX_MAX_LEVEL = 2           # ... up to this level (coarser ones: 4x the photo tiles each, and EOX answered 403 to
                                # ~135 requests/s with every level at 512 px and no cap, 2026-11-07)
MAP_TEX_OFF_PX = 128            # the 3D map: a far tile's texture off the screen (Server.tile_tex_px) ...
MAP_TEX_MIN_PX = 128            # ... never under this on it ...
MAP_TEX_OVERSAMPLE = 1.5        # ... and at most this many texels per screen pixel of its edge
MAP_SCREEN_H = 1440.0           # ... for a screen this many pixels high
FAR_LAZY_EVERY_S = 2.0          # Connection.far_lazy_tick: the larger far tiles' best photos queued this often at most
FLY_TEX_EYE_M = 300.0           # flying higher than this over the ground: textures as fine as on screen too
MAP_TEX_UPGRADE_S = 1.0         # shown tiles checked this often for a larger texture as the camera turns
MAP_LOD_SCALE = 1.25            # ... and their detail reaching this much farther (Connection.kfun)
MAP_DETAIL_EYE_M = 3000.0       # the 3D map's view frame: roads, trees round its target only below this eye ...
MAP_BUILDINGS_EYE_M = 10000.0   # ... the buildings below this one (the user, 2026-11-07: "make buildings start rendering
MAP_BLD_FAR_EYE = 1.0           # further out"), the far ones to this many times the eye's height ...
MAP_BLD_FAR_MAX_M = 6000.0      # ... at most this far,
MAP_BLD_SKY_EYE = 3.0           # the skyline to this many times it ...
MAP_BLD_SKY_MAX_M = 20000.0     # ... at most this far (Connection.building_reach)
VIEW_ANCHOR_Q = 1 / 32          # the 3D map's view-only anchors' height quantum (m): no survey, no block driven on
ROAD_TILE_TIMEOUT_S = float(os.environ.get("BNGE_ROAD_TILE_TIMEOUT", "") or 300.0)
# s past ROAD_TILE_TIMEOUT_S a road process's job may run before its pool's watchdog restarts the processes (pools
# hung_s): the road tiles' own wait recycles first; this catches jobs whose waiters have no timeout (GitHub #1058)
ROAD_HUNG_EXTRA_S = 60.0
WORKERS = 3                     # far-tile workers (--workers): while driving (FAR_DRIVE)
FAR_DRIVE = 3
FAR_MAX_DESKTOP = 10            # ... up to this many with the 3D map open / the loading boost (desktops, budget.py)
# The lean memory tiers (budget.tier: an 8 GB machine, a 4-6 GB one): their pools by LEAN_PROCS (main_async), the far
# buildings in the road-tile process, the process counts machine_profile sets. BNGE_LEAN_POOLS=off: the small
# profile's pools (a process per block lane, the far tiles' and the far buildings' own) with the lean caches and counts
LEAN_POOLS = os.environ.get("BNGE_LEAN_POOLS", "on") != "off"
# How many processes the lean tiers' server runs in all (BNGE_LEAN_PROCS): 4 = the server, one block process for both
# lanes, one road-tile process (the far buildings in it), one far-tile process; 2 = the server and one road-tile
# process (the blocks and the far tiles in the server's threads: one copy of the map data instead of three); 1 =
# everything in the server's process (--block-process off). Measured 2026-11-12 (drive_mem: a cold Baltimore at 30 mph
# then I-95 at 60 mph, the Low preset's graphics, the server in a 2.5 GB scope with 2 GB of swap): 4 processes peaked
# at 4.2 GB (swap full), 2 at 3.1 GB (swap ~1 GB), both with every terrain block in time (p90 1.1-1.2 s) and ~400 road
# tiles; 1 peaked at 3.1 GB too but had 10 of 17 blocks late (p50 5.8 s, p90 20 s), a quarter of the road tiles and a
# tenth of the far tiles (everything under one interpreter lock). The small profile's eight processes were killed by
# the scope's memory limit in a minute. The emergency tier (a 0.9 GB scope, 2 GB of swap, its extras off: EMERGENCY_OFF)
# with 2: 6 of 32 blocks late (p50 1.6 s, p90 4.5 s), ~300 road tiles; with 1: 24 of 29 late (p50 7.6 s), 209.
LEAN_PROCS = {"lean": 2, "emergency": 2}
# The small tier (a Steam Deck, a 16 GB PC: a RAM limit of ~4.8-6.5 GB by Auto) in the compact layout too: one block
# process for both lanes, the far buildings in the road-tile processes (SMALL_ROAD_PROCESSES of them, each replaced
# every budget.tasks_per_child() jobs), one far-tile process; 5 processes instead of 8-9. Measured 2026-11-12: the
# small layout's server held 8-9.7 GB on a Steam Deck (swap full, the edge guard holding the car 18-23 % of a 40 mph
# drive) and 11.6 GB private on a 16 GB Windows PC (the game's working set trimmed to 1.2 GB); with its RAM limit
# (memwatch.py) a game-less cold Baltimore stayed at 6.4-8.1 GB against 5 GB in eight processes: a fresh worker alone
# holds ~0.4-0.5 GB and the server's own ~2 GB. BNGE_SMALL_COMPACT=off: the small layout.
SMALL_COMPACT = os.environ.get("BNGE_SMALL_COMPACT", "on").lower() not in ("off", "0", "false", "no")
# The emergency tier's server arguments turned off (machine_profile): the trees, the gas stations, the airports' runway
# grading. BNGE_EMERGENCY_TRIM=off keeps them.
EMERGENCY_TRIM = os.environ.get("BNGE_EMERGENCY_TRIM", "on") != "off"
EMERGENCY_OFF = ("trees", "gas_stations", "airports")


def lean_processes(tier):
    """The lean tier's process count (LEAN_PROCS; BNGE_LEAN_PROCS 4, 2 or 1 overrides)."""
    v = os.environ.get("BNGE_LEAN_PROCS", "")
    if v in ("1", "2", "4"):
        return int(v)
    return LEAN_PROCS.get(tier, 4)


FAR_ELASTIC = os.environ.get("BNGE_FAR_ELASTIC", "on") != "off"     # ... those past FAR_DRIVE only while used (pools.py)


class DynGate:
    """A semaphore whose limit is read at each acquire (limit(): an int >= 1)."""

    def __init__(self, limit):
        self.limit = limit
        self.busy = 0
        self.cv = threading.Condition()

    def __enter__(self):
        with self.cv:
            while self.busy >= max(1, int(self.limit())):
                self.cv.wait(0.25)          # (the limit can rise meanwhile: the map opened)
            self.busy += 1
        return self

    def __exit__(self, *exc):
        with self.cv:
            self.busy -= 1
            self.cv.notify_all()
        return False
TRACE_S = 60.0                  # BNGE_TRACEMALLOC: seconds between the memory reports ...
TRACE_TOP = 15                  # ... and the allocation sites in each
PROVISIONAL_ELSEWHERE = os.environ.get("BNGE_PROV_HERE") != "1"   # a teleport's first block in a block process
# The road plans this process's own work asks for (its far tiles' ground, lakes, tunnels) are made by the prefetch
# lane's block process, not on this process's threads (Server.plan_elsewhere; BNE_PLAN_ELSEWHERE=off: here, as before)
PLAN_ELSEWHERE = os.environ.get("BNE_PLAN_ELSEWHERE", "on") != "off"
WARM_SPLIT = os.environ.get("BNE_WARM_SPLIT", "on") != "off"     # (Server.warm_roads; off: both lanes plan ahead)
PLAN_ELSEWHERE_S = 120.0    # s a cell's plans are waited for from there before they are made here after all
LANE_REQUEST, LANE_PREFETCH = 0, 1     # Server.block_pools: the blocks the mod asks for / the prefetch
HM_BUSY_TILE_WORKERS = 1    # far tiles built at once while a physics block is building in the server process (an
                            # anchor's first, or every one with --block-process off; the block processes' blocks do
                            # not share its interpreter: far tiles one at a time behind them were most of a Steam
                            # Deck's far field time, blocks building nearly all the time while driving, 2026-10-20)
WARM_STEPS_S = (0.0, 10.0, 20.0, 30.0)   # s ahead on the player's straight path whose road cells are planned early
WARM_AROUND = 2000.0        # m: and every cell this close to the player (any heading)
PREFETCH_START = 200.0      # m (Chebyshev) from the block centre before prefetching
# A fast player (FAST_BASE_V) gets the chain of its next PREFETCH_CHAIN blocks prefetched, PREFETCH_PARALLEL at once: at
# 500 mph the next block was asked for ~1.3 s before it was needed and took 0.5-3 s, so the game held its car for its
# ground half the time (Indianapolis 2026-10-30); one predicted block was not enough
PREFETCH_CHAIN = 4
READY_STEPS = 2             # grid steps a built block may be from the mod's target and still be taken (follow.lua)
PREFETCH_PARALLEL = 2
# The prefetch lane's block processes: two on a machine with the cores and memory for them (PREFETCH_PARALLEL's two
# blocks then build at once; with one process they took turns and a third of the blocks asked at 140-165 mph were not
# built yet, 4.4 s each, while the server used ~3 of 16 cores, 2026-11-08), one on a small one (a Steam Deck's memory).
# BNE_PREFETCH_PROCS sets it.
PREFETCH_PROCS_CPUS = 12
PREFETCH_PROCS_GB = 24.0


def prefetch_procs():
    """Block processes for the prefetch lane on this machine (PREFETCH_PROCS_*; BNE_PREFETCH_PROCS overrides)."""
    if os.environ.get("BNE_PREFETCH_PROCS"):
        try:
            return max(1, min(4, int(os.environ["BNE_PREFETCH_PROCS"])))
        except ValueError:
            pass
    import budget
    gb = budget.total_gb() or 0.0
    return 2 if (os.cpu_count() or 0) >= PREFETCH_PROCS_CPUS and gb >= PREFETCH_PROCS_GB and not budget.small() else 1
READY_SET_M = 3000.0        # m: the built blocks round the player the mod is told of (terrain_ready_set)
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


# By the block's width in metres (heightmap.grid): docs/V1_4_SPEC.md §3.5 (M4): the 1 km block (collidable
# [c-512, c+510] at 2 m, c+511 at 1 m) on a 128 m grid: prepare at 128 m, lead 5 s (was 2: 2026-10-15, fewer moves),
# reach 510 - 250 = 260 m; fast mode above 150 m/s: lead 3 s, reach 510 - 90 = 420 m, prepare test 0.75 s ahead; the
# prefetch starts 28 m before the prepare distance. 2 km: the v1.3.1 block.
FOLLOW_RULES = {
    2048.0: FollowRule(heightmap.GRID_BY_M[2048.0], FOLLOW_PREPARE, FOLLOW_LEAD, FOLLOW_REACH, PREFETCH_START),
    1024.0: FollowRule(heightmap.GRID_BY_M[1024.0], 128.0, 5.0, 260.0, 100.0,
                       fast_v=150.0, fast_lead=3.0, fast_reach=420.0, fast_ahead=0.75, min_lead=256.0),
}
BLOCK_SIZES = tuple(int(m / heightmap.SQUARE) for m in sorted(FOLLOW_RULES))    # samples per edge (1024, 2048)
TERRAIN_BLOCK = farfield.TERRAIN_BLOCK     # terrain mode's frozen 2 km block
BLOCK_DEFAULT = heightmap.SIZE  # Server()'s block (1 km) (test_v131_default.py pins the library default)
# The command line's block in mesh mode: 1 km (M4: in-game 2026-10-10, the 512² block at 2 m; since 2026-11-01 the
# same kilometre at 1 m, 1024² samples: the stock maps' resolution, a .ter swap of ~29 ms on the Stelvio)
CLI_BLOCK_MESH = heightmap.SIZE     # (512² at 2 m: BNGE_GROUND_SQUARE, the lighter ground)
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
SURVEY_N = 241                      # 125 m apart (the same z10 tiles as 500 m: ~58k samples)
SURVEY_WINDOW = 1.0                 # blocks: the windows survey_q takes the relief of (at most: whole survey steps)
SURVEY_PERCENTILE = 95.0            # of the windows' relief the anchor's quantum must hold (the rest: their own, coarser)
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
# ... and the tiles after those, with a host's spare request tokens alone (netfetch "prefetch": dropped while its bucket is
# below half), asked again less often: the next 40 of a cold French teleport were IGN's (the near rings, z16-17), its
# 50/s full for a minute while EOX's tiles further down the queue were not asked yet; EOX then did its ~1,300 after IGN
# (Lille, Caen, Brest, 2026-11-11: EOX ~5-15/s for the first 60 s, then 30-50/s). That was with IGN's tiles
# prefetched too (imagery BNGE_PREFETCH_PAST_REGIONAL=1), which is off by default; BNGE_PREFETCH_SPARE=200 with it
IMAGERY_PREFETCH_SPARE = int(os.environ.get("BNGE_PREFETCH_SPARE", "0"))
PREFETCH_SPARE_AGAIN_S = 5.0
PREFETCH_SPARE_PER_PASS = 40    # (at most this many of them a pass of the loop: its time on the event loop)
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
# A geotile every game dropped (roads_drop, bldfar_drop, ...): its session files deleted this long after, unless one
# is sent it again meanwhile (geo_dropped). Before (GitHub #599, 2026-10-31) they stayed until the server stopped: the
# geotiles are anchor-free, so prune never took them, and two days' driving left 9 GB in the session folder.
GEO_FILES_GRACE_S = 120.0
GEO_LIST_BATCH_S = 1.0          # geo_dropped: tiles whose grace ends within this share one listing of the folder
GEO_TRACKERS = {"roads": "roads", "bldfar": "bldfar", "bldsky": "bldsky", "brfar": "brfar"}  # kind -> Connection attr
SESSION_HEARTBEAT_S = 60.0
SESSION_SWEEP_S = 30.0      # main_async: how often sweep_sessions runs
LEASE_GRACE_S = 900.0       # a disconnected game's stream lease (PR #817) kept this long for it to reconnect
# Live retexture (--colormatch / --deshadow): when a region a shown tile was rendered without becomes
# ready, the tile's texture is rendered again under its next revision's name and the mod is told
# (far_retex) to point the tile's material at it. At most RETEX_INFLIGHT such renders on the tile pool
# at once (the other workers keep building), started at most RETEX_RATE per second, nearest the player
# first. A tile is re-rendered once per region of it that turns ready (a coarse L5 / L6 texture spans up
# to 3 x 3 / 4 x 4 z12 regions, as many de-shadow ones); the guard against a loop is RETEX_REPEAT re-renders
# in a row that came back still waiting for a region already ready: past that it waits to be re-sent.
# --retex-rate 0: no live swap (a stale tile is re-rendered when it is sent again, as before).
RETEX_RATE = 8.0
MAP_RETEX_BOOST = 4         # the 3D map open: RETEX_INFLIGHT and RETEX_RATE times this (retexture: in workers)
RETEX_INFLIGHT = 2         # (2: the 3D map's quick textures sharpen as their photos come, 2026-11-07)
RETEX_REPEAT = 3
# Shown in the mod's UI (V1_3_SPEC §C), after the imagery credits.
ELEVATION_CREDIT = "Elevation: Mapterhorn (https://mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen"

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
    ramp: bool = False                             # a teleport's: its far field coarse first (FIRST_RAMP)


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
    tex_px: int = 0                     # its texture's size (Server.tile_tex_px; 0: tex_size of its level)
    shape: str | None = None    # its shape file when not tile_name(): a copy under a new material name (reslot)
    tile: object = None         # the farfield tile it was built for (Tile, GTile, Piece)
    coarse: bool = False        # a coarse cover's texture (Server.coarse_texture: rendered again when sent after it)
    frame: tuple | None = None  # a global tile's frame (lat, lon): its geometry is in that frame's world
    err_n: float | None = None  # its error along the normal (farfield.Built.lod_err: the screen-space LOD's)


def ahead_points(x, y, vx, vy, tree=None):
    """download_ahead's points (anchor frame): AHEAD_S s ahead on the straight path (AHEAD_MAX_M at most); with the
    car's time-to-reach tree (reach.Tree) at those times along its most probable path (straight on past its end), then
    where the map data of its likely branches (reach.P_WANT) ends (its frontier: the next data the tree needs), the
    soonest first."""
    v = math.hypot(vx, vy)
    if v <= 0.0:
        return []
    k = min(1.0, AHEAD_MAX_M / (v * AHEAD_S[-1]))
    times = [t * k for t in AHEAD_S]
    if tree is None:
        return [(x + vx * t, y + vy * t) for t in times]
    import reach as reachmod
    pts = tree.points_at(times)
    fr = tree.frontier
    if len(fr):
        fr = fr[(fr[:, 3] >= reachmod.P_WANT) & (fr[:, 2] <= times[-1])]
        for fx, fy, _e, _p in sorted(fr.tolist(), key=lambda r: r[2])[:AHEAD_FRONTIER_MAX]:
            if all(math.hypot(fx - px, fy - py) > AHEAD_MARGIN_M for px, py in pts):
                pts.append((fx, fy))
    return pts


def predict_centre(px, py, vx, vy, lead, reach=FOLLOW_REACH, grid=heightmap.GRID_BY_M[2048.0], min_lead=0.0,
                   min_lead_v=2.0):
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


def predict_request(px, py, vx, vy, cx, cy, rule=FOLLOW_RULES[2048.0], at=None):
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
    if at is not None:          # (the player where it asks: READY_STEPS, Connection.maybe_prefetch)
        at.append((px + vx * t, py + vy * t))
    return predict_centre(px + vx * t, py + vy * t, vx, vy, lead, reach, rule.grid, rule.min_lead, rule.min_lead_v)


def predict_chain(px, py, vx, vy, cx, cy, rule, n, at=None):
    """The next n centres the mod will ask for (predict_request, chained): after each, the player where its path passes
    nearest that centre, the next request from there. Fewer when the chain stops (no move, a repeat). at: a dict
    filled with each centre's request point (the player where it asks)."""
    out = []
    v2 = vx * vx + vy * vy
    if v2 < 1.0:
        return out
    for _ in range(n):
        where = []
        c = predict_request(px, py, vx, vy, cx, cy, rule, where)
        if c == (cx, cy) or c in out:
            break
        out.append(c)
        if at is not None:
            at[c] = where[0]
        t = max(0.0, ((c[0] - px) * vx + (c[1] - py) * vy) / v2)
        px, py, (cx, cy) = px + vx * t, py + vy * t, c
    return out


# A fast player on their GPS route: the blocks ahead predicted along it, not along a straight line (predict_chain): at
# each turn of a route through Manhattan at 150 mph the straight chain's blocks were never asked for, ~40 % of the
# blocks built, and the ones asked for after the turn queued behind them (nyc_speed.py 2026-11-02).
ROUTE_NEAR_M = 40.0         # m: the player this near the route's line ...
ROUTE_ALIGN = 0.7           # ... and heading along it (cos of the angle) is on it
ROUTE_STEP_M = 16.0         # m between the route points the mod's follow rule is tried at
ROUTE_AHEAD_S = 20.0        # s of driving along the route looked at, at most ...
ROUTE_AHEAD_MAX_M = 3000.0  # ... and this far


def predict_route_chain(xy, px, py, vx, vy, cx, cy, rule, n, at=None):
    """The next n centres the mod will ask for while the player at (px, py) moving (vx, vy) drives along the route
    polyline xy ((k, 2) in the anchor frame), or None when the player is not on it (ROUTE_NEAR_M, ROUTE_ALIGN). The
    mod's rule (predict_request) tried every ROUTE_STEP_M along the route with the route's own heading there."""
    sp = math.hypot(vx, vy)
    if sp < 1.0 or xy is None or len(xy) < 2:
        return None
    a, b = xy[:-1], xy[1:]
    d = b - a
    ln2 = np.maximum((d * d).sum(1), 1e-9)
    t = np.clip(((px - a[:, 0]) * d[:, 0] + (py - a[:, 1]) * d[:, 1]) / ln2, 0.0, 1.0)
    q = a + d * t[:, None]
    dist = np.hypot(q[:, 0] - px, q[:, 1] - py)
    j = int(np.argmin(dist))
    seg = float(np.sqrt(ln2[j]))
    if dist[j] > ROUTE_NEAR_M or ((d[j, 0] * vx + d[j, 1] * vy) / (seg * sp)) < ROUTE_ALIGN:
        return None
    reach = min(ROUTE_AHEAD_MAX_M, sp * ROUTE_AHEAD_S)
    out, gone, x, y, k = [], 0.0, float(q[j, 0]), float(q[j, 1]), j
    while gone < reach and k < len(d) and len(out) < n:
        ux, uy = d[k, 0] / math.sqrt(ln2[k]), d[k, 1] / math.sqrt(ln2[k])
        bx, by = float(b[k, 0]), float(b[k, 1])
        left = math.hypot(bx - x, by - y)
        step = min(ROUTE_STEP_M, left)
        if left <= 1e-6:
            k += 1
            continue
        x, y, gone = x + ux * step, y + uy * step, gone + step
        lvx, lvy = ux * sp, uy * sp
        _lead, _reach, ahead = rule.mode(lvx, lvy)
        if max(abs(x + lvx * ahead - cx), abs(y + lvy * ahead - cy)) > rule.prepare:
            where = []
            c = predict_request(x, y, lvx, lvy, cx, cy, rule, where)
            if c != (cx, cy) and c not in out:
                out.append(c)
                if at is not None:
                    at[c] = where[0]
                cx, cy = c
    return out


def _num(msg, name, default=None):
    v = msg.get(name, default)
    if v is None:
        return default
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite")
    return v


def survey_q(frame, dem, block_m=BLOCK_SURVEY_M):
    """Height quantum of an anchor (§1.3): the smallest ladder step whose block range holds the SURVEY_PERCENTILE
    relief of the block-sized windows (SURVEY_WINDOW blocks of block_m m, at most) of a +-15 km survey at DEM z10, by
    the blocks' own rule (heightmap.block_q). A block only has to hold its own relief (its zBase is its own; one that
    does not fit takes the next coarser step, heightmap.block_params). The whole survey's relief made every mountain
    anchor 1/16 m: 6 cm steps on the runways and roads (Courchevel, in-game 2026-10-13); so did its one worst window of
    1.5 blocks (the Stelvio's and Courchevel's, whose 2 km blocks within 6 km all hold 1/32 m): its 2 m samples rounded
    to 6 cm steps were bumps on every steep road, twice the time over 0.2 g of 1/32 m in a quarter car (the user,
    2026-11-01: "Fix the bumps"). On the 1 m ground the rounding is 4x the curvature again (it goes as Q / square²):
    windows of a block and a 500 m survey step, plus 10 %, still made the Stelvio, Tyrol and Zermatt 1/32 m where 97-99 %
    of their real 1 km blocks hold 1/64, and the Brazilian and Japanese hills 1/64 where 95-100 % hold 1/128 (bumpscan
    2026-11-12: the Stelvio's roads IRI 13.5 m/km at 1/32 m against 6.8 unrounded)."""
    xs = np.linspace(-SURVEY_HALF, SURVEY_HALF, SURVEY_N)
    u = heightmap.surface_u(frame, lambda la, lo: dem.sample(la, lo, SURVEY_ZOOM), xs, xs)
    k = max(2, min(len(xs), int(math.floor(SURVEY_WINDOW * block_m / (xs[1] - xs[0]) + 1e-9)) + 1))
    win = np.lib.stride_tricks.sliding_window_view(u, (k, k))
    return heightmap.block_q(float(np.percentile(win.max(axis=(2, 3)) - win.min(axis=(2, 3)), SURVEY_PERCENTILE)))


def game_running():
    """Was BeamNG.drive running when this server started? The launcher says (BNGEARTH_GAME_AT_START: it starts the game
    right after the server, a new game that holds none of the earlier sessions); else whether one runs now: its Linux
    build (cpupin.game_pids), its Windows build on Windows or under Wine / Proton (a process whose command line names
    BeamNG.drive.x64.exe). Unsure (an error): True, the careful answer (_scan_sessions then keeps earlier sessions for
    the game to claim)."""
    said = os.environ.get("BNGEARTH_GAME_AT_START")
    if said in ("0", "1"):
        return said == "1"
    try:
        import cpupin
        if cpupin.game_pids():
            return True
        if os.name == "nt" or not os.path.isdir("/proc"):
            return False
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    if b"beamng.drive.x64.exe" in f.read().lower():
                        return True
            except OSError:
                continue
        return False
    except Exception:
        return True


def road_tracker(conn):
    """A connection's road tiles' tracker: geotiles (roads.GeoTracker) when its mod places them (Connection.road_geo),
    else the anchor's."""
    return roadsmod.GeoTracker() if getattr(conn, "road_geo", False) else roadsmod.Tracker()


OSM_FUEL_THREADS = 2     # OpenStreetMap fuel cells fetched at once (Server.osm_fuel_want)
OSM_FUEL_WARM_TRIES = 10    # the start's index read tried this often (Server._osm_fuel_warm) ...
OSM_FUEL_WARM_WAIT_S = 30.0  # ... this far apart


def osm_fuel_cells(frame, tile, geo=False):
    """The map cells whose OpenStreetMap fuel stations a road tile's layouts read (gasstations.station_cells): tile
    (tx, ty) of anchor frame, or geotile (gx, gy) with geo (its own frame; frame unused)."""
    if geo:
        import geotiles
        return gasstationsmod.station_cells(geotiles.frame(*tile), geotiles.geobox(*tile), None)
    return gasstationsmod.station_cells(frame, tile[0], tile[1])


def osm_fuel_tiles(frame, key, tiles, geo=False):
    """The road tiles among tiles whose stations reach map cell key (osm_fuel_cells)."""
    k = str(key)
    return {t for t in tiles if any(str(c) == k for c in osm_fuel_cells(frame, t, geo))}


TURBINE_GEO = 1
TURBINE_Z = 15             # about 1 km cells at mid-latitudes; existing far-content tracker
TURBINE_SEND_M = 4000.0
TURBINE_DROP_M = 5000.0
TURBINE_PER_POSE = 2       # bounded batches: only populated cells request terrain elevations


class Server:
    def __init__(self, user_dir, cache_dir, tile_workers=3, kfun=None,
                 hysteresis=farfield.HYSTERESIS, radius=farfield.RADIUS, far_format="cdae",
                 imagery=imagerymod.DEFAULT_CHAIN, near_visual="mesh", tex_near=TEX_NEAR, tex_far=TEX_FAR,
                 tone=1.0, near_radii=None, status_interval=STATUS_INTERVAL, cells=CELLS_DEFAULT, tex=None,
                 block=None, horizon=False, deshadow="off", weather=None, gravity=None, net=None,
                 colormatch="off", credits=None, cache=None, retex_rate=RETEX_RATE, surface=None,
                 mapdata=None, grading=False, road_widths=False, block_pools=(), sidewalks=True, buildings=False, traffic=False, far_pool=None,
                 road_pool=None, airports_db=None, sea=None, vegetation=None, leaf_on=True, season_mode="off",
                 far_bld_pool=None, gas_stations=False, regional_poles="off"):
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
        if os.environ.get("BNGE_WORLD_BUNDLE") and not (grading and mapdata is not None):
            from worldbundle import BundleError
            raise BundleError("Pinned road plans require roads and grading enabled")
        self.sid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.user_dir = user_dir           # the game's user folder (detail mode fills its terrain texture cache)
        self.cache_dir = cache_dir         # (here already: _scan_sessions moves old sessions into its trash)
        root = self.root = os.path.join(user_dir, "temp", "beamngEarth")
        self.cdae_root = os.path.join(user_dir, *CDAE_ROOT)
        # earlier runs' session folders a game may still show: sid -> monotonic time found / released
        # by a connection (docs/ROADMAP.md "Robustness" 3; claim_sessions, sweep_sessions)
        self.orphans = {}
        self.orphan_s = SESSION_ORPHAN_S
        self.heartbeat_at = time.monotonic()
        self._scan_sessions()
        # the squares' layer textures as PNG (the terrain packing reads no DDS: terraintex.py), off the start's path
        threading.Thread(target=terraintexmod.ensure, args=(user_dir, os.environ.get("BNGEARTH_GAME_DIR")),
                         name="terraintex", daemon=True).start()
        self.out_dir = os.path.join(root, self.sid)
        self.cdae_dir = os.path.join(user_dir, *CDAE_ROOT, self.sid.lower())
        self.vfs_dir = f"/temp/beamngEarth/{self.sid}"
        self.prewarm_vfs = None             # the materials' prewarm shape (prewarm.py), written at the first hello
        self.matslots = matslotsmod.MatSlots(self.sid[-4:], held=self.slot_held)    # pooled names and shape files
        self.reslot_serial = itertools.count(1)
        self.slot_serial = itertools.count(1)      # far_tile slotSerial (matslots.py)
        self.base_px = BASE_PX              # detail mode: the ground photo's size (px), the mod's graphics.photoPx
        self.lod_px = farfield.SSE_TAU_PX   # screen-space LOD: the largest split pop allowed on screen (px; 0: off;
                                            # --lod-px; a mod's lodPx setting overrides it for its connection)
        # the built far tiles' errors along the normal (m), what the screen-space LOD knows (Connection.view): anchor
        # nodes {(anchorId, (L, i, j)): e} (a piece's: its node's) and global tiles {(z, x, y): e}; kept past the tile
        # cache (a node's error is the same each time it is built), the oldest dropped past LOD_ERRS_MAX
        self.lod_errs = {}
        self.glod_errs = {}
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
        if block is None:               # BLOCK_DEFAULT; terrain mode: its frozen 2 km block
            block = BLOCK_DEFAULT if near_visual in MESH_LAYOUT else TERRAIN_BLOCK
        self.layout = farfield.Layout(cells, block, near_visual in MESH_LAYOUT, tex)
        self.follow = FOLLOW_RULES[self.layout.block * heightmap.SQUARE]     # the mod's terrain-follow table for its block
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
        # (and no separation in the ground under a rail bridge: railways.flat_rail_crossing)
        _railways.RAW_ELEV = lambda la, lo, _d=self.dem: _d.sample(la, lo, gradingmod.PROFILE_ZOOM)
        # runways graded in (--airports on: main() loads the OurAirports copy before the worker processes)
        self.airports = airportgrademod.AirportGrader(airports_db, self.dem) if airports_db is not None else None
        self.airports_db = airports_db  # (also the road rules' country where no border says: navgraph.country_at)
        self.regional_poles = regional_poles
        if regional_poles != "off":
            import countrybounds
            if isinstance(countrybounds.bundled(), countrybounds.Absent):    # (fetched from BeamNG.Earth-data once)
                log.warning("regional poles: no border data reachable yet: the stock scenery until it is")
            if regional_poles in ("fr-varied", "europe"):
                import townsigns
                import directionsigns
                townsigns.bundled()
                directionsigns.bundled()    # fail visibly before generating tiles if the bundle is missing/corrupt
            log.info("regional poles: %s (country polygons at each pole; data %s; other/unknown countries keep stock)",
                     regional_poles, countrybounds.signature())
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
        if self.imagery is not None:        # (a quick texture's photos downloaded: its live re-render, _cm_ready)
            self.imagery.add_img_listener(self._cm_listener)
        self.imagery_failures = 0           # tiles that fell back to the palette (network trouble)
        self.imagery_worker = 0             # far textures the far-tile processes rendered (blockworker.far_tile)
        self.fin_worker_hits = deque(maxlen=FIN_WORKER_WINDOW)     # (fin_worker_worth: recent tries found them?)
        self.fin_worker_tries = 0
        self.base_worker = 0                # base photos the block processes rendered (blockworker.base_photo)
        self.map_port = None                # the in-game map's tile endpoint (maptiles.py), hello.mapPort
        self.feed = None                    # the companion feed (companionfeed.py), when on
        self.map_tiles = None               # its MapTiles: also written as files on request (map_tiles message)
        self.map_files = None               # the maptiles.TileFiles writing them (made by the first request)
        # (anchorId, key) of cached tiles built with the palette because the imagery failed; shown in
        # the status note (re-texturing them once the host is back is a ROADMAP follow-up)
        self.degraded = set()
        # Heightmap workers: with the block processes they mostly wait on a process, and with two a
        # request queued behind a prefetch and an earlier request (3.5 s at 90 m/s, 2026-10-12)
        self.hm_pool = ThreadPoolExecutor(HM_THREADS, thread_name_prefix="heightmap")
        self.hm_proc = {}               # (aid, cx, cy) -> its build's future in a block process (provisional_follow)
        # the blocks the mod asks for (the request lane): their own threads, never behind the prefetch's and the
        # rebuilds' (a first visit's requested blocks waited 20-38 s for a heightmap thread, nyc_speed.py --cold
        # down Uptown at 100 mph, 2026-11-02)
        self.hm_req_pool = ThreadPoolExecutor(HM_REQUEST_THREADS, thread_name_prefix="heightmap-req")
        self.hm_here, self.hm_here_lock = 0, threading.Lock()  # blocks building in this process (far tiles wait)
        # a block's base photo rendered beside its heights (they do not depend on each other): after them it added
        # 2.3 s to a 12 s Baltimore block on a Steam Deck (2026-10-20)
        self.base_pool = ThreadPoolExecutor(HM_THREADS, thread_name_prefix="base-photo")
        # the photos a player waits for (a teleport's first block, the request lane's): never queued behind the other
        # blocks' (each holds a base-photo thread until its block process renders it; a teleport's photo waited
        # ~20 s behind the old place's on a Deck, 2026-10-23)
        self.urgent_base_pool = ThreadPoolExecutor(2, thread_name_prefix="base-urgent")
        self.geo_list_pool = None           # (geo_dropped: the session folder listed off the event loop)
        self.prov_pool = ThreadPoolExecutor(2, thread_name_prefix="provisional")    # follow blocks sent provisional
        self.prov_info = {}             # (aid, cx, cy) -> a provisional follow block's info (until the graded one)
        self.base_shown = OrderedDict()  # (aid, cx, cy) -> the base photo name last sent for that block (note_base)
        self.ground_bases = {}          # (aid, cx, cy) -> its ground stand-in photos (write_ground_base), until graded
        self.ground_pool = ThreadPoolExecutor(1, thread_name_prefix="ground-ahead")   # groundahead.py
        # the water plans (Connection.sea_tiles) apart: a plan takes seconds and holds while a teleport's block is
        # built (pacer.hold), and the ground ahead (a few ms) queued behind them: 2.2 s at 275 km/h (2026-11-07)
        self.sea_pool = ThreadPoolExecutor(1, thread_name_prefix="sea-plan")
        # the world map's requests (GPS route, address search, map tiles): their own threads, so a teleport's tile
        # builds on the default pool do not hold them back (a route took 30 s after one, 2026-10-14)
        self.ui_pool = ThreadPoolExecutor(2, thread_name_prefix="map-ui")
        # the search box's own: a search can take seconds, and must not hold up a route or the map's tiles
        self.search_pool = ThreadPoolExecutor(4, thread_name_prefix="map-search")
        # the status bar's place names (placenames.py): a thread of their own, a region's first fetch takes seconds
        self.place_pool = ThreadPoolExecutor(1, thread_name_prefix="place-names")
        # the modding API's places (places.py): their own thread, so an add-on asking often never holds up the status
        # bar's names or the 3D map's labels on place_pool
        self.api_pool = ThreadPoolExecutor(1, thread_name_prefix="api-places")
        self._place_names_lock = threading.Lock()
        # (a thread per helper slot too: a far thread waits for its tile's builder, here or on a helper)
        self.cache_dir = cache_dir
        self.picking = None          # the settings page's file window open (apppaths.pick): for which path
        revisitmod.CACHE_DIR = cache_dir
        self.refetched = 0              # far textures whose missing photo tiles were fetched here (make_tile)
        # self.share_lock, self.group_helper = threading.Lock(), None    # (share_start: shared world building, commented out)
        try:                            # (BeamMP sharing v2: a session's mark left by an earlier run; share_hub sets it)
            os.remove(os.path.join(cache_dir, "share", "active"))
        except (OSError, TypeError):
            pass
        self.tile_pool = ThreadPoolExecutor(tile_workers + (peerworkmod.MAX_SLOTS if hasattr(far_pool, "remote_slots")
                                                            else 0), thread_name_prefix="far")
        # (the 3D map's big leap, the user 2026-11-07: "big leaps in world gen speed": every far worker while the map
        # is open, FAR_DRIVE of them while driving: tile_workers is the pool's whole size, main() sizes it)
        self.far_max = tile_workers
        self.far_drive = min(tile_workers, FAR_DRIVE)
        self.far_gate = DynGate(lambda: self.far_limit())
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
        self._veg_jobs, self._veg_job_lock = {}, threading.Lock()      # (_veg_geo_job)
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
        self.reach_pool = ThreadPoolExecutor(1, thread_name_prefix="reach-cells")    # Connection.reach_tree's cells
        self.ahead_busy = False
        self.ahead_last = None          # (anchor id, x, y) of the last round
        self.ahead_stats = {"dem": 0, "cells": 0, "deferred": 0}
        self.route_pool = ThreadPoolExecutor(1, thread_name_prefix="route-download")   # Connection.route_download
        self.horizon_grids = {}             # anchorId -> farfield.HorizonGrid
        # the far field's potentially visible set (farpvs.py): far tiles hidden behind terrain from where the player
        # can be soon get a small texture; per anchor its elevation rings and viewpoints (one worker), per connection
        # its set (Connection.pvs_tick)
        self.pvs_on = farpvsmod.available() and self.horizon      # (the camera's height: horizon mode's update_eye)
        self.pvs_pool = ThreadPoolExecutor(1, thread_name_prefix="far-pvs") if self.pvs_on else None
        self.pvs_fields = {}                # anchorId -> farpvs.Field
        self.pvs_building = {}              # anchorId -> (future, radius)
        self.pvs_failed = {}                # anchorId -> monotonic time of a failed ring build
        self.pvs_stats = {"hidden": 0, "screen": 0, "texels_saved_M": 0.0, "revealed": 0, "sets": 0, "viewpoints": 0,
                          "set_s": 0.0, "rings_s": 0.0}
        self.anchors_preparing = {}     # id -> Anchor whose first block is building (prepare_anchor: its grid early)
        self.coarse_keys = set()        # global far tiles of a teleport's coarse cover: FIRST_RAMP_TEX_PX (tile_tex_px)
        self.horizon_inflight = {}          # anchorId -> (future, half-size)
        self.horizon_failed = {}            # anchorId -> monotonic time of the last failed build
        self.horizon_blocks = OrderedDict()     # (zoom, x, y, block) -> block maxima, LRU
        self.anchors = {}
        self._next_anchor = 1
        self.connections = set()
        self.stream_leases = streamleases.Registry()
        # Heightmaps: (anchorId, cx, cy) -> info, LRU order.
        self.hm = OrderedDict()
        self.hm_inflight = {}
        self.hm_stage = {}              # terrain key -> what its build waits on now (the stuck log, prepare_terrain)
        self.ter_serial = itertools.count()
        self.base_stale = set()         # blocks whose base photo lacks lazy imagery (NAIP) still to come
        self.base_skipped = set()      # blocks built without their photo (a fast player's prefetch: base_refresh)
        self.quick_pool = ThreadPoolExecutor(1, thread_name_prefix="quick-base")   # stand-in photos (_quick_later)
        self.quick_early = ThreadPoolExecutor(2, thread_name_prefix="quick-early")   # ... started with a block (fast)
        self.quick_lock = threading.Lock()  # (_quick_once: a block's stand-in made once)
        self.quick_made, self.quick_running = {}, {}
        self.quick_futs = {}            # (aid, cx, cy) -> a requested block's stand-in photo started with it (fast)
        self.photo_pool = None          # requested blocks' photos and stand-ins (main(): PHOTO_PROCESS_MIN_CPUS)
        self.recent_full = collections.deque(maxlen=RECENT_FULL)    # (anchorId, cx, cy, rgb) of the last full photos
        self.base_lock = threading.Lock()
        self.pads_rebuild = set()       # blocks built without gas station pads that lie on them (pads_stale)
        self.roads_rebuild = set()      # blocks built before every road cell under them was on disk (roads_refresh)
        self.roads_need = {}            # ... and what each waits for: "roads" (its roads painted) or "landcover"
        self.pads_changed_at = 0.0      # (time.monotonic) the last new pad: Connection.pads_refresh waits a little
        self.fast = False               # the player faster than FAST_BASE_V (the latest pose): photos follow blocks
        self.base_pending = {}          # block -> the analysis regions its base photo was rendered without
        self.base_rev = itertools.count(1)
        self.hm_committed = deque(maxlen=HM_KEEP_COMMITTED)
        # anchors a teleport left (Connection._prepare_anchor): their queued road / sign tiles and terrain blocks are
        # dropped, not built (the user, 2026-11-11: "if the server is still building one city but initiates a
        # teleport, stop generating the old city"; a fresh Albuquerque waited behind the Newark it left, its graded
        # first block and road tiles still building 46 s later); back when the teleport is never committed
        self.abandoned = set()
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
        if surface is not None and hasattr(surface, "bus_pads"):    # the bus stations' yards painted in as well (task 020)
            import busstations
            surface.bus_pads = busstations.ON           # (the road tiles keep the yards' pads)
        self.mapdata = mapdata      # mapdata.MapData for the roads (--roads on), else None
        if mapdata is not None and getattr(mapdata, "dem", 0) is None:
            mapdata.dem = self.dem  # (gradesep's ground)
        self.grading = bool(grading) and mapdata is not None   # ground graded under roads (grading.py)
        # the lids a bare-earth DEM leaves out over a tunnel under a ground road (tunnellids.py, GitHub #1083)
        import tunnellids
        self.lids = (tunnellids.Lids(mapdata, lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
                     if self.grading and tunnellids.ON else None)
        # street widths measured between the buildings (roadwidth.py), decided once per road per run
        self.sidewalks = sidewalks  # raised sidewalk meshes with the road tiles (sidewalks.py)
        self.buildings = buildings  # building meshes with the road tiles (buildings.py)
        # gas stations with refuelling with the road tiles (gasstations.py; the user, 2026-10-23)
        # (the settings page's switch turns them off and on again: set_gas_stations; never on without the flag)
        self.gas_allowed = bool(gas_stations) and mapdata is not None
        self.gas_stations = self.gas_allowed
        # the station finder's index (stationindex.py: the map's gas station / EV charger layers, Nearest, the low fuel
        # prompt; the user, 2026-11-09), beside the map data's cache; None without map data
        raw = getattr(mapdata, "raw_dir", None)
        self.stations = stationindexmod.StationIndex(os.path.join(raw, "stationindex"), mapdata) if raw else None
        # OpenStreetMap's fuel stations (osmfuel.py, the OSM test: the user, 2026-10-28, "lets test OSM if it is too laggy
        # we will revert"): fetched in the background per cell around the road tiles the car wants, never waited for; a
        # cell with stations that came after its tiles were sent sends them again (osm_fuel_want)
        if hasattr(mapdata, "osm_tags_warm"):       # (the OSM road tag shards' manifest and head: osmtagshards.py)
            threading.Thread(target=self._osm_tags_warm, name="osmtags-warm", daemon=True).start()
        self.osm_fuel = None
        if self.gas_allowed and mapdatamod.OSM_FUEL and hasattr(mapdata, "osm_fuel_fetch"):
            self.osm_fuel = ThreadPoolExecutor(OSM_FUEL_THREADS, thread_name_prefix="osm-fuel")
            self.osm_fuel_done, self.osm_fuel_wait = set(), {}
            self.osm_fuel.submit(self._osm_fuel_warm)
        self.rsigns_on = True       # the settings page's Road signs (roadsigns.enabled, with BNE_ROADSIGNS=on)
        self.dej_on = False         # the settings page's German junction signs (designs.enabled; BNE_DEJUNCTIONS forces it;
                                    # off by default until its in-game performance check)
        self.dirs_on = False        # the settings page's Direction signs (dirsigns.enabled; BNE_DIRSIGNS=on forces it)
        self.towns_on = True        # the settings page's Town signs (townentry.enabled; BNE_TOWNSIGNS=on / off forces it;
                                    # on by default since 2026-11-10, the user's call)
        self.road_wear = None       # the settings page's Road detail (roadwear.tier_of: 'off' / 'basic' / 'full', the
                                    # quality presets'; None: roadwear.DEFAULT_TIER; BNE_ROAD_WEAR forces it)
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
        self.perf = perfstats.Collector()   # the anonymous performance data of the reports (telemetry.py, perfstats.py)
        self.areas = None               # saved areas downloaded ahead (areas.Areas; main_async, with a Cache)
        self.fast_pool = None           # the saved areas' fast mode: its own road processes (fast_precache)
        self.make_fast_pool = None      # n -> such a pool (main_async: the road pool's initializer), or None
        self._fast_lock = threading.Lock()
        if cache is not None:
            for producer in (self.dem, self.imagery, self.deshadow, self.colormatch):
                if producer is not None:
                    producer.disk = cache

    def _empty_trash(self):
        """What earlier runs left in the cache's trash (_rmtree_outside): a server ended with the game while it was
        deleting a session moved there (the game closing ends the server's processes) left the rest of it for good, GBs
        of tiles that no later run looked at (GitHub #599: 9 GB of old sessions). Deleted in a background thread (a
        whole session is thousands of files; the start does not wait). An entry whose server still runs (its pid in its
        name, xproc.alive) is that server's own delete under way and is left to it (a rename keeps the folder's old
        time, so its age says nothing). Returns the thread, or None (nothing there)."""
        cache = getattr(self, "cache_dir", None)
        trash = os.path.join(cache, TRASH_DIR) if cache else None
        try:
            names = os.listdir(trash) if trash else []
        except OSError:
            return None
        import xproc
        old = []
        for n in names:
            parts = n.rsplit("_", 2)
            pid = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None
            if pid is not None and pid != os.getpid() and xproc.alive(pid):
                continue
            old.append(os.path.join(trash, n))
        if not old:
            return None
        log.info("trash: deleting %d folders earlier runs left in %s", len(old), trash)

        def run():
            for p in old:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    self._remove(p)
        t = threading.Thread(target=run, name="trash", daemon=True)
        t.start()
        return t

    def _scan_sessions(self):
        """Session folders left by earlier runs (only ones we marked): one whose marker is untouched
        for SESSION_STALE_S goes now, with the engine's .cdae caches of its shapes; a younger one is
        kept as an orphan until a game claims it or sweep_sessions deletes it. With no game running (the usual start:
        the launcher starts the server first) every earlier session goes now: no game can claim it, and no file
        watcher sees it go, where the sweep's later move of four sessions out of the user folder froze the game for
        2 x 8.6 s ("FileWatchers", 2026-11-02). Then the trash earlier runs left and these moves (_empty_trash)."""
        wall, now = time.time(), time.monotonic()
        no_game = not game_running()
        for d in glob.glob(os.path.join(self.root, "*", SESSION_MARKER)):
            sid = os.path.basename(os.path.dirname(d))
            if sid == self.sid:     # a restart in the same second with the same suffix: ours now, never released
                continue
            try:
                age = wall - os.path.getmtime(d)
            except OSError:
                continue
            if age > SESSION_STALE_S or no_game:
                self._rmtree_session(sid, later=True)
            else:
                self.orphans[sid] = now
                log.info("session %s of an earlier run (%.0f min old) kept until the game says whether it still "
                         "shows its tiles", sid, age / 60)
        if no_game:
            self._wipe_leaked_packs_once()
        self.trash_thread = self._empty_trash()

    def _base_cache_files(self, sid):
        """Detail mode's packed copies of session sid's base photos in the game's terrain texture cache (terraincache.py):
        they go with it (blocks evicted during a run took theirs, prune; whatever a run left behind stayed for good,
        ~1.4 MB a block, 3.9 GB of them by 2026-10-18)."""
        out = []
        for path in glob.glob(os.path.join(self.root, sid, "*_base*.png")) + \
                glob.glob(os.path.join(self.root, sid, "*_base*.jpg")):       # (.png: before 2026-11-04)
            for px in BASE_SIZES:           # (whichever size it was made at)
                key = terraincache.base_key(f"/temp/beamngEarth/{sid}/{os.path.basename(path)}", base_ao_vfs(px))
                p = os.path.join(terraincache.cache_dir(self.user_dir), key + ".dds")
                if os.path.exists(p):
                    out.append(p)
        return out

    def _remove_base(self, name):
        """A base photo name in out_dir gone with its packed copies in the game's terrain texture cache (whichever size it
        was made at). A replaced stand-in or earlier refresh lost only its photo, its 5.6-22 MB pack stayed for good:
        one a block at speed, 165 GB in the cache of a test PC by 2026-11-03."""
        self._remove(os.path.join(self.out_dir, name))
        for px in BASE_SIZES:
            self._remove(os.path.join(terraincache.cache_dir(self.user_dir),
                                      terraincache.base_key(f"{self.vfs_dir}/{name}", base_ao_vfs(px)) + ".dds"))

    PACK_WIPE_MARK = "terrainpacks_wiped_v1"   # in the cache dir: the one wipe of the leaked packs done
    PACK_WIPE_MIN_BYTES = 5 * 1024 ** 3

    def _wipe_leaked_packs_once(self):
        """Once (no game running): the game's terrain texture cache moved to the trash when it holds over
        PACK_WIPE_MIN_BYTES, the base photo packs earlier builds leaked (_remove_base). It is a cache: the game packs
        what it needs again (a few seconds at the next level load, stock maps' materials at their next use)."""
        mark = os.path.join(self.cache_dir, self.PACK_WIPE_MARK)
        if os.path.exists(mark):
            return
        d = terraincache.cache_dir(self.user_dir)
        total = 0
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        total += e.stat().st_size
                    except OSError:
                        pass
                    if total > self.PACK_WIPE_MIN_BYTES:
                        break
        except OSError:
            pass
        if total > self.PACK_WIPE_MIN_BYTES:
            log.info("terrain texture cache over %.0f GB (leaked base photo packs): moved out, deleted in the background",
                     self.PACK_WIPE_MIN_BYTES / 1024 ** 3)
            self._rmtree_outside(d, later=True)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            open(mark, "w").close()
        except OSError:
            pass

    def _rmtree_session(self, sid, later=False):
        """Session sid's folders (and its base photos' terrain cache copies) removed at once: moved out of the user
        folder and deleted (later: only moved, the trash thread deletes them). For when no game watches the folder
        (the start); a running game's sessions go file by file (_session_paths, queue_delete)."""
        for path in self._base_cache_files(sid):
            self._remove(path)
        for d in (os.path.join(self.root, sid), os.path.join(self.cdae_root, sid.lower())):
            self._rmtree_outside(d, later=later)

    def _session_paths(self, sid):
        """Session sid's files for the rate-limited delete queue, in order: its base photos' terrain cache copies, the
        session's and its .cdae cache's files, the marker, the folders. (Blocking: a whole session is ~11k files.)"""
        d = os.path.join(self.root, sid)
        cd = os.path.join(self.cdae_root, sid.lower())
        marker = os.path.join(d, SESSION_MARKER)
        files = sorted(p for p in glob.glob(os.path.join(d, "*")) + glob.glob(os.path.join(cd, "*")) if p != marker)
        return self._base_cache_files(sid) + files + [marker, d, cd]

    def _rmtree_outside(self, d, later=False):
        """Remove folder d of the game's user folder: moved out of it first (into the cache's trash, one rename) and
        deleted there (later: left there for _empty_trash). Deleted in place, a whole session's thousands of files were
        as many notifications for the game's file watcher, on its main thread: a 4 s frame at Utrecht 2026-10-30
        ("FileWatchers 1501.7"); the move alone of four sessions froze it too (2 x 8.6 s, 2026-11-02), so a running
        game's sessions go file by file instead (_session_paths). In place when the move cannot be made (the cache on
        another drive)."""
        if not os.path.isdir(d):
            return
        cache = getattr(self, "cache_dir", None)
        if cache:
            trash = os.path.join(cache, TRASH_DIR, f"{os.path.basename(d)}_{os.getpid()}_{time.monotonic_ns()}")
            try:
                os.makedirs(os.path.dirname(trash), exist_ok=True)
                os.replace(d, trash)
                if later:
                    return
                d = trash
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)

    def _touch(self, sid):
        try:
            os.utime(os.path.join(self.root, sid, SESSION_MARKER))
        except OSError:
            pass

    def claimed_sessions(self):
        """Earlier runs' sessions still owned by connected or reconnecting games."""
        out = self.stream_leases.sessions()
        for c in self.connections:
            if getattr(c, "lease", None) is None:
                out |= c.sessions
        return out

    def check_user_dir(self, conn, game_path):
        """The game's own user folder (hello.userPath) against the one this server writes into: a game whose user
        folder is elsewhere (moved in BeamNG's launcher, its own -userpath, a folder name the launcher misread) never
        finds our tiles: the car on a flat plane, "loading terrain" for good (GitHub #770). Told to the player, and
        saved for the launcher's next start (apppaths.set_user_dir)."""
        if not isinstance(game_path, str) or not game_path.strip():
            return
        game = os.path.normpath(game_path.strip())
        if not os.path.isdir(game):     # (a path we cannot see: Proton's C:/users/steamuser/..., a Flatpak sandbox's
            return                      # home: not ours to judge)
        try:
            apppaths, _ = _app_modules()
            same = apppaths.same_dir(game, self.user_dir)
        except Exception:           # (no apppaths: compared as text)
            apppaths, same = None, os.path.normcase(game) == os.path.normcase(os.path.normpath(self.user_dir))
        if same:
            return
        log.error("the game's user folder is %s, but this server writes into %s: the game cannot find the world's "
                  "files (GitHub #770); the next start uses the game's folder", game, self.user_dir)
        if apppaths is not None:
            try:
                apppaths.set_user_dir(game)
            except OSError as e:
                log.warning("could not save the game's user folder: %s", e)
        conn.send({"type": "user_dir_mismatch", "game": game, "server": self.user_dir})

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
        if getattr(conn, "lease", None) is not None:
            return                      # TCP EOF does not mean that the game's objects disappeared
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
            log.info("session %s of an earlier run: no game claimed it, deleting it file by file", sid)
            if remove:
                self.queue_delete(self._session_paths(sid))
            gone.append(sid)
        return gone

    async def sweep_sessions_async(self):
        """sweep_sessions on the event loop (its state is the loop's): each session's files listed in a worker thread
        (a whole session is ~11k files, the streaming must not wait) and deleted through the rate-limited queue, as a
        released session's are: a game is running, and its file watcher took a whole session moved out of the user
        folder at once as an 8.6 s frame (2026-11-02)."""
        self.expire_leases()
        gone = self.sweep_sessions(remove=False)
        for sid in gone:
            self.queue_delete(await asyncio.to_thread(self._session_paths, sid))
        return gone

    def expire_leases(self, now=None):
        """Release the stream leases of games gone longer than LEASE_GRACE_S (event loop, sweep_sessions_async):
        their pooled slots go back to the pool, their anchors' files and their sessions' folders follow the usual
        rules (the sessions' grace starts now, as drop_claims's)."""
        now = time.monotonic() if now is None else now
        gone = self.stream_leases.expire(now, LEASE_GRACE_S)
        if not gone:
            return 0
        others = self.claimed_sessions()
        for lease in gone:
            log.info("stream lease %s: its game gone %.0f s: released (%d slots, %d anchors, %d sessions)",
                     lease.token[:8], now - lease.detached_at, len(lease.slots), len(lease.anchors), len(lease.sessions))
            self.slots_let_go(set(lease.slots))
            for sid in lease.sessions - others:
                if sid in self.orphans:
                    self.orphans[sid] = now
        return len(gone)

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

    def _delete_pick(self):
        """The next queued delete as a call doing the file work (None: the file stays). The decision is the event loop's
        (the tile caches); the file work need not be: _remove alone held the loop 2.8 and 5.0 s after a teleport
        (2026-11-09, os.remove under a busy disk), every game message waiting."""
        path, ck = self.delete_queue.popleft()
        # an evicted tile built again since (same file name) keeps its new file; ck callable: kept while it says so
        # (asked again in the worker right before the file goes: a delete held up by a slow disk must not take the file
        # of a tile built again meanwhile under the same name; the checks are membership reads)
        if callable(ck):
            if ck():
                return None

            def work():
                if not ck():
                    self._remove(path)
            return work
        if ck is None or (ck not in self.tiles and ck not in self.tile_inflight):
            def work():
                if ck is not None and (ck in self.tiles or ck in self.tile_inflight):
                    return
                if os.path.isdir(path):     # a released session's folder, queued after its files
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    self._remove(path)
            return work
        return None

    def _delete_one(self):
        work = self._delete_pick()
        if work is not None:
            work()

    def geo_held(self, kind, k):
        """Does some connection's tracker of kind (GEO_TRACKERS) have geotile k (gx, gy) sent or building?"""
        attr = GEO_TRACKERS.get(kind)
        for c in list(self.connections):
            t = getattr(c, attr, None) if attr else None
            if t is not None and getattr(t, "geo", False) and (k in t.sent or k in t.building):
                return True
        return False

    def geo_files_many(self, wanted):
        """geo_files of several (kind, key) at once: the session folder listed once. [files] in their order."""
        try:
            names = os.listdir(self.out_dir)
        except OSError:
            return [[] for _ in wanted]
        return [self.geo_files(kind, key, names) for kind, key in wanted]

    def geo_files(self, kind, key, names=None):
        """The session files of geotile key (geotiles.key) of kind: a road geotile's content (rg_), every anchor's skin
        (rs_) and exact variants (rx_); a far geotile's json, shape and pieces (blockworker.FAR_GEO prefixes)."""
        if kind == "roads":
            pat = re.compile(rf"(rg_{re.escape(key)}([._].*)?|r[sx]_\d+_{re.escape(key)}([._].*)?)")
            stems = ("rg_", "rs_", "rx_")
        else:
            pre = blockworker.FAR_GEO.get(kind)
            if pre is None:
                return []
            pat = re.compile(rf"{pre}_{re.escape(key)}([._].*)?")
            stems = (f"{pre}_",)
        # (one listing, filtered by the key first: glob listed the session folder once per stem in Python, ~52 ms a
        # call over 16k files, 89 s of the event loop in a 10 min first visit, 2026-11-04; this ~10 ms, off the loop)
        if names is None:
            try:
                names = os.listdir(self.out_dir)
            except OSError:
                return []
        return sorted(os.path.join(self.out_dir, n) for n in names
                      if key in n and n.startswith(stems) and pat.fullmatch(n))

    def geo_dropped(self, kind, k, key, grace=None):
        """A connection dropped geotile k (gx, gy; key its geotiles.key) of kind: its session files are deleted after
        GEO_FILES_GRACE_S unless some connection has it (sent or building) by then or when each file's turn comes
        (geo_held: one built again writes the same names). Event loop (the trackers' on_pose)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def run():
            if self.geo_held(kind, k):
                return
            # (the tiles whose grace ran out together share one listing of the session folder, on a thread of its
            # own: 32 road tiles expiring at once listed ~27k entries 32 times; GitHub #758 measured the old
            # per-tile globs on the event loop at 8.5 s for 32 tiles)
            batch = self.__dict__.setdefault("geo_expired", [])
            batch.append((kind, k, key))
            if len(batch) > 1:
                return
            loop.call_later(GEO_LIST_BATCH_S, flush)

        def flush():
            items, self.geo_expired = self.geo_expired, []
            items = [it for it in items if not self.geo_held(it[0], it[1])]
            if not items:
                return
            if self.geo_list_pool is None:
                self.geo_list_pool = ThreadPoolExecutor(1, thread_name_prefix="geo-files")

            def listed(fut):
                try:
                    found = fut.result()
                except Exception as e:
                    log.debug("geotile files: %s", e)
                    return
                for (kd, kk, _key), files in zip(items, found):
                    if files and not self.geo_held(kd, kk):
                        self.queue_delete(files, (lambda kd=kd, kk=kk: self.geo_held(kd, kk)))
            loop.run_in_executor(self.geo_list_pool, self.geo_files_many, [(kd, ky) for kd, _kk, ky in items]
                                 ).add_done_callback(listed)
        loop.call_later(GEO_FILES_GRACE_S if grace is None else grace, run)

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
        if getattr(self, "_delete_pool", None) is None:
            # (one thread: the deletes stay in order and paced, off the event loop)
            self._delete_pool = ThreadPoolExecutor(1, thread_name_prefix="delete")

        def step():
            work = self._delete_pick() if self.delete_queue else None
            if work is None:
                after()
                return
            try:
                fut = loop.run_in_executor(self._delete_pool, work)
            except RuntimeError:            # (the pool shut down: at exit)
                work()
                after()
                return
            fut.add_done_callback(lambda _f: after())

        def after():
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

        def raw(la, lo):
            return self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))

        def elev(la, lo):
            z = raw(la, lo)
            # graded under the roads as the block is: a rock beside a track cut into a slope floated 1.9 m over the cut
            # (Stelvio pass, the user's report 2026-10-17)
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        rows = vegetationmod.tile_items(anchor.frame, tx, ty, self.veg, lambda k: True, elev, self.sea, as_json=True,
                                        raw=raw if g is not None else None)
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
        urgencymod.wait_turn()          # (a block the game waits for first: urgency.py)
        import geotiles
        anchor = self.anchors.get(aid) or (self.anchors[max(self.anchors)] if self.anchors else None)
        if anchor is None:
            return None
        zoom = anchor.zooms[0]
        if self.road_pool is not None and self.mapdata is not None:
            # (in a road tile process: here they took the server's one interpreter from the far textures and plans)
            t0, waited = time.perf_counter(), []
            fut, i = self._veg_geo_job(anchor, zoom, gx, gy, flat)
            got = poolsmod.result_running(fut, None, waited)
            n = len(got)
            got = got[i]
            if time.perf_counter() - t0 > ROAD_GEO_SLOW_S:
                log.info("trees geotile %d_%d in %.1f s (queue %.1f, %d in its job)", gx, gy,
                         time.perf_counter() - t0, waited[0] if waited else 0.0, n)
            if isinstance(got, Exception):
                raise got
            return got
        g = self.grader_for(anchor)

        def raw(la, lo):
            return self.ground(None, la, lo, self.dem.sample(la, lo, zoom))

        def elev(la, lo):
            z = raw(la, lo)
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        got = vegetationmod.geo_tile_items(gx, gy, self.veg, lambda k: True, elev, self.sea, as_json=True,
                                           raw=raw if g is not None else None)
        if got is None:
            return None
        rows, pivot = got
        frame = geotiles.centre(gx, gy)
        path, n = vegetationmod.write_tile(self.out_dir, self.vfs_dir, None, gx, gy, rows, flat=flat,
                                           geo={"key": geotiles.key(gx, gy), "frame": frame, "pivot": pivot})
        return path, n, frame, pivot

    def _veg_geo_job(self, anchor, zoom, gx, gy, flat):
        """(future, index): the road processes' job (blockworker.veg_geo_many) that builds geotile (gx, gy), its result
        the index'th of the job's list. The tiles asked for while a job waits for its turn join it (VEG_GEO_BATCH at
        most): one at a time, after the road tiles (RespawningPool.submit_low), each waited out the whole queue of
        road tiles for its ~10 ms of work, and the trees fell behind the car (533 tree tiles pending, 22 s each,
        2026-11-08)."""
        submit = getattr(self.road_pool, "submit_low_lazy", None)
        if submit is None:                  # (a plain executor: one tile a job)
            return self.road_pool.submit(blockworker.veg_geo_many, anchor.id, anchor.lat, anchor.lon, zoom,
                                         [(gx, gy)], self.out_dir, self.vfs_dir, flat), 0
        key = (anchor.id, zoom, bool(flat))
        with self._veg_job_lock:
            job = self._veg_jobs.get(key)
            if job is None or len(job[1]) >= VEG_GEO_BATCH or job[0].done():
                tiles = []

                def make_args(key=key, tiles=tiles, a=anchor):
                    with self._veg_job_lock:            # (no more joins: the job is on its way)
                        if self._veg_jobs.get(key, (None, None))[1] is tiles:
                            del self._veg_jobs[key]
                        return (a.id, a.lat, a.lon, zoom, list(tiles), self.out_dir, self.vfs_dir, flat), {}
                job = self._veg_jobs[key] = (submit(blockworker.veg_geo_many, make_args), tiles)
            job[1].append((gx, gy))
            return job[0], len(job[1]) - 1

    def place_names(self):
        """The status bar's town / state / country lookup (placenames.PlaceNames), made at its first use: it shares the
        map data's Overture reader (footers and row groups read once) when there is one."""
        with self._place_names_lock:
            pn = getattr(self, "_place_names", None)
            if pn is None:
                md = getattr(self, "mapdata", None)
                pn = self._place_names = placenamesmod.PlaceNames(
                    md.cache_dir if md is not None else self.cache_dir, self.net,
                    reader=md.overture if md is not None else None)
            return pn

    def build_turbines_geo(self, gx, gy):
        """Mapped wind turbines through the existing geotile tracker, without building distant roads.

        Empty cells do no DEM work. Populated cells sample their exact mapped points in one batch, with the
        existing airport/sea ground rules; nearby road-owned records provide the graded installation height.
        The tracker limits simultaneous batches, and this session memo keeps delivered heights stable.
        """
        import geotiles
        import windturbines
        memo = self.__dict__.setdefault("_turbines_geo_done", {})
        key = geotiles.key(gx, gy, TURBINE_Z)
        filename = "turbines_" + key + ".json"
        path = os.path.join(self.out_dir, filename)
        cached = memo.get(key)
        if cached is not None and os.path.exists(path):
            return cached
        clat, clon, south, west, north, east = geotiles.info(gx, gy, TURBINE_Z)
        frame = geotiles.frame(gx, gy, TURBINE_Z)
        # (the drawn ones only: rows_for_features leaves a small one out, and a count short of the features below was
        # taken for missing heights, so a tile holding one was never kept and asked again each time)
        features = tuple(windturbines.keep(
            [f for f in windturbines.near(south, west, north, east)
             if geotiles.tile_of(f.lat, f.lon, TURBINE_Z) == (gx, gy)],
            windturbines.worldcover_built_fn(getattr(self, "mapdata", None))
            if getattr(self, "mapdata", None) is not None else None))
        rows = []
        if features:
            zoom = demmod.zoom_for(clat, 1.25, demmod.FINE_ZOOM)

            def elev(la, lo):
                return self.ground(frame, la, lo, self.dem.sample(la, lo, zoom))
            rows = windturbines.rows_for_features(frame, features, elev, collide=False)
            if len(rows) != len(features):     # unavailable heights retry; never memoize a partial farm
                return None
        pivot = float(np.mean([r["z"] for r in rows])) if rows else 0.0
        data = {"key": key, "sid": self.sid, "frame": [clat, clon], "pivot": pivot, "turbines": rows}
        tmp = staging.tmp_for(path)
        with open(tmp, "w", encoding="utf-8") as target:
            json.dump(data, target, separators=(",", ":"))
        staging.replace(tmp, path)
        result = (f"{self.vfs_dir}/{filename}", len(rows), (clat, clon), pivot)
        if len(memo) >= 4096:
            memo.clear()
        memo[key] = result
        return result

    def country_at(self, lat, lon):
        """The ISO country code at (lat, lon) (its border's, else its nearest airport's: navgraph.country_at), or None."""
        try:
            return navgraphmod.country_at(self.airports_db, float(lat), float(lon), _borders(self))
        except (TypeError, ValueError):
            return None

    def left_hand(self, anchor):
        """Whether traffic drives on the left at the anchor (its country's road rules, navgraph.road_rules)."""
        lh = getattr(anchor, "_left_hand", None)
        if lh is None:
            lh = navgraphmod.road_rules(navgraphmod.country_at(self.airports_db, anchor.lat, anchor.lon, _borders(self)))["rightHandDrive"]
            anchor._left_hand = lh
        return lh

    def buried(self, anchor):
        """The share of low-voltage lines underground in the anchor's country (furniture.buried_share: fewer utility
        poles)."""
        v = getattr(anchor, "_buried", None)
        if v is None:
            v = furnituremod.buried_share(navgraphmod.country_at(self.airports_db, anchor.lat, anchor.lon, _borders(self)))
            anchor._buried = v
        return v

    def country(self, anchor):
        """The anchor's country (ISO alpha-2, its border's, else its nearest airport's: navgraph.country_at), or None: the road
        markings' style (markings.style)."""
        v = getattr(anchor, "_country", "")
        if v == "":
            v = navgraphmod.country_at(self.airports_db, anchor.lat, anchor.lon, _borders(self))
            anchor._country = v
        return v

    def lamp_region(self, anchor):
        """The anchor's street lamp rules (furniture.lamp_region: their spacing)."""
        v = getattr(anchor, "_lamp_region", "")
        if v == "":
            v = furnituremod.lamp_region(navgraphmod.country_at(self.airports_db, anchor.lat, anchor.lon, _borders(self)),
                                         navgraphmod.region_at(self.airports_db, anchor.lat, anchor.lon, _borders(self)))
            anchor._lamp_region = v
        return v

    def pole_profile(self, anchor=None):
        """Mode/data token only; roads resolve country at each pole, not at the anchor."""
        import poleprofiles
        return poleprofiles.geographic_profile(getattr(self, "regional_poles", "off"))

    def mph(self, anchor):
        """Whether speed limits are posted in mph at the anchor (its country, navgraph.road_rules)."""
        v = getattr(anchor, "_mph", None)
        if v is None:
            v = navgraphmod.road_rules(navgraphmod.country_at(self.airports_db, anchor.lat, anchor.lon, _borders(self)))["mph"]
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
            rules = navgraphmod.road_rules(navgraphmod.country_at(self.airports_db, float(cla), float(clo), _borders(self)))
            rules["de_junctions"] = designsmod.enabled(self.dej_on)
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
        feats = [cd.layers.get("roads") for cd in mapcells.load_cells(self.mapdata, keys, ("roads",))]
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
                decks.setdefault(fid, []).append((np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64),
                                                  anchor.bridges.depth_of(fid)))
        except Exception as e:          # no decks: the graph follows the ground under bridges
            log.debug("navgraph decks: %s: %s", type(e).__name__, e)
        # (Germany's mapped nodes too: its junction signs on, the graph's window in Germany)
        de_nodes = designsmod.reads_here(designsmod.enabled(self.dej_on), anchor.frame, (x - w, y - w, x + w, y + w))
        nodes = secrossings.read(self.mapdata, anchor.frame, keys) \
            if secrossings.wanted(self.country(anchor), de_nodes) else None
        # (and the cells' road_routes for the German junctions' ref ranks, as blockworker.navgraph reads them)
        de_routes = blockworker._de_routes(self.mapdata, keys) if de_nodes else ()
        tun, open_tun = {}, {}
        try:
            with self.grader_lock:
                if anchor.tunnels is None:
                    anchor.tunnels = self.chain_view(anchor, "tunnels", elev, g)
            tun, open_tun = navgraphmod.tunnel_floors(anchor.tunnels, feats, (x - w, y - w, x + w, y + w))
        except Exception as e:
            log.debug("navgraph tunnels: %s: %s", type(e).__name__, e)
        graph = navgraphmod.build(anchor.frame, feats, elev, (x, y), urban, decks=decks, tunnels=tun, open_tunnels=open_tun,
                                  widths=self.road_widths,
                                  left_hand=self.left_hand(anchor), mph=self.mph(anchor),
                                  signals=furnituremod.signals_for(self.mapdata, anchor.frame, keys),
                                  country=self.country(anchor), priority=None if nodes is None else nodes["priority"],
                                  de_on=designsmod.enabled(self.dej_on), de_routes=de_routes)
        cla, clo, _ = anchor.frame.enu_to_geodetic(x, y, 0.0)
        graph["rules"] = navgraphmod.road_rules(navgraphmod.country_at(self.airports_db, float(cla), float(clo), _borders(self)))
        graph["rules"]["de_junctions"] = designsmod.enabled(self.dej_on)
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

    boost_full = False

    def load_boost(self, on, why="", full_only=False, half_only=False):
        """The loading boost (cpupin.boosted): the server's processes also on half of the game's cores while the
        player waits after a teleport, back on their own when it moves or the far field is done. With the settings
        page's teleportPriority (cpupin.full_split): the server on every core and the game on two, until the far field
        and the roads round the car are done (moving does not end it); full_only: that alone (joining the world)."""
        if on and not self.boost_on:
            full = cpupin.full_split(self.server_cpus, getattr(self, "game_cpus", None)) \
                if not half_only and cpupin.player_settings(self.user_dir).get("teleport_full") else None
            if full:
                try:
                    n = cpupin.pin_tree(full[0])
                    # (the game left on its own cores unless BNGE_FULL_PIN_GAME=on: its threads squeezed onto two and
                    # spread back at the end, three of six cold teleports froze the game for good in its
                    # TextureCooker within a second of the spread, a texture import under way; 2026-11-11)
                    g = cpupin.pin_game(full[1]) if FULL_PIN_GAME else 0
                except OSError as e:
                    log.warning("full priority loading: %s", e)
                else:
                    self.boost_on = self.boost_full = True
                    log.info("full priority loading on (%s): %d server threads on CPUs %s, the game's %d on %s", why, n,
                             ",".join(map(str, sorted(full[0]))), g, ",".join(map(str, sorted(full[1]))))
                    return
            if full_only:
                return                  # (the half boost only after a teleport)
        if not on and self.boost_full:
            try:
                cpupin.pin_tree(self.server_cpus)
                if FULL_PIN_GAME:
                    cpupin.pin_game(getattr(self, "game_cpus", None))
            except OSError as e:
                log.warning("full priority loading: %s", e)
            self.boost_on = self.boost_full = False
            log.info("full priority loading off (%s)", why)
            return
        if on and not self.boost_cpus:
            log.debug("load boost (%s): no cores to lend (--pin / --load-boost off, or too few cores)", why)
        on = bool(on) and bool(self.boost_cpus)
        if on == self.boost_on:
            return
        if getattr(self, "split_on", False):
            self.boost_on = on          # (an edge hold's split in force: its end pins by this, split_boost)
            log.info("load boost %s (%s) after the edge hold", "on" if on else "off", why)
            return
        cpus = ((getattr(self, "catchup_cpus", None) or self.boost_cpus) if half_only else self.boost_cpus) if on \
            else self.server_cpus
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

    def split_boost(self, on, why):
        """The full priority split (cpupin.full_split: the server on every CPU, the game on its first cores) on, or off
        back to the boost in force before (Server.load_boost's state is left as it was). True when on. Not over full
        priority loading (boost_full: that is the same split already)."""
        if on:
            if self.boost_full or getattr(self, "split_on", False):
                return False
            full = cpupin.full_split(self.server_cpus, getattr(self, "game_cpus", None))
            if not full:
                return False
            try:
                n = cpupin.pin_tree(full[0])
                g = cpupin.pin_game(full[1]) if FULL_PIN_GAME else 0      # (load_boost's FULL_PIN_GAME)
            except OSError as e:
                log.warning("%s boost: %s", why, e)
                return False
            self.split_on = True
            log.info("%s: every core (%d server threads on CPUs %s, the game's %d on %s)", why, n,
                     ",".join(map(str, sorted(full[0]))), g, ",".join(map(str, sorted(full[1]))))
            return True
        if not getattr(self, "split_on", False):
            return False
        self.split_on = False
        if self.boost_full:
            return False                # (full priority loading came meanwhile: its split stays)
        try:
            cpupin.pin_tree(self.boost_cpus if self.boost_on and self.boost_cpus else self.server_cpus)
            if FULL_PIN_GAME:
                cpupin.pin_game(getattr(self, "game_cpus", None))
        except OSError as e:
            log.warning("%s: %s", why, e)
        log.info("%s: the cores back as before", why)
        return False

    def build_far_bld_tile(self, aid, tx, ty):
        """buildings.far_tile: a far (render-only) building tile of anchor aid, or None. Worker threads."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or not self.buildings:
            return None
        zoom = anchor.zooms[0]
        if self.far_bld_pool is not None:
            t0 = time.perf_counter()
            fut = self.far_bld_pool.submit(blockworker.far_buildings_tile, aid, anchor.lat, anchor.lon, zoom, tx, ty,
                                           self.out_dir, self.vfs_dir, self.lamp_region(anchor), self.left_hand(anchor))
            res = fut.result()
            log.debug("far buildings tile %d_%d: %.2f s (queue + build)", tx, ty, time.perf_counter() - t0)
            return res
        return buildingsmod.far_tile(anchor.frame, tx, ty, self.mapdata,
                                     lambda la, lo: self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom)),
                                     self.out_dir, self.vfs_dir, aid, self.lamp_region(anchor),
                                     urban_fn=self.surface.urban if getattr(self.surface, "urban", None) else None,
                                     widths=self.road_widths, left_hand=self.left_hand(anchor))

    def build_signs_tile(self, aid, tx, ty):
        """signstream.build_tile: the highway signs of road tile (tx, ty) of anchor aid (its roads are with the mod
        already), or None. Worker threads (signstream.Tracker)."""
        anchor = self.anchors.get(aid)
        source = getattr(self.mapdata, "highway_signs", None)
        if anchor is None or source is None:
            return None
        source.version()            # (the sign data's build, learnt here off the event loop: Connection.on_pose)
        if Server.is_abandoned(self, aid):      # (through the class: the tests' stand-in servers)
            return None
        if self.sign_pool is not None:
            try:
                return Server._signs_in_pool(self, aid, anchor, tx, ty)
            except poolsmod.Abandoned:
                return None                     # (a teleport left its anchor)
        return Server._signs_here(self, aid, anchor, tx, ty)

    def _signs_in_pool(self, aid, anchor, tx, ty):
        """build_signs_tile in the sign process (pools.Abandoned when a teleport leaves aid while it is queued)."""
        return poolsmod.result_running(self.sign_pool.submit(
            blockworker.signs_tile, aid, anchor.lat, anchor.lon, anchor.zooms[0], tx, ty, self.out_dir,
            self.vfs_dir, self.left_hand(anchor), self.mph(anchor), self.country(anchor), dirsignsmod.enabled(self),
            roadsignsmod.enabled(self), designsmod.enabled(self.dej_on), Server.towns_enabled(self)),
            ROAD_TILE_TIMEOUT_S, abandon=lambda: Server.is_abandoned(self, aid))

    def _signs_here(self, aid, anchor, tx, ty):
        """build_signs_tile in this process (no sign process)."""
        g = self.grader_for(anchor)
        zoom = anchor.zooms[0]

        def elev(la, lo):
            z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom))
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        urban = self.surface.urban if self.surface is not None and hasattr(self.surface, "urban") else None
        towns = None
        if Server.towns_enabled(self) and self.mapdata is not None and self.surface is not None:
            if getattr(self, "towns", None) is None:
                self.towns = townentrymod.Towns(self.mapdata, self.surface)
            towns = self.towns.town_state(anchor.frame, roadsmod.cells_of_tile(anchor.frame, tx, ty,
                                                                               blockworker.TOWNS_MARGIN_M))
        return signstream.build_tile(self.mapdata, anchor.frame, aid, tx, ty, elev, self.out_dir, self.vfs_dir,
                                     urban, self.road_widths, self.left_hand(anchor), self.mph(anchor),
                                     self.country(anchor), dirsignsmod.enabled(self), roadsignsmod.enabled(self),
                                     designsmod.enabled(self.dej_on), towns)

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

    WARM_ORDER = {"roads": 0, "infrastructure": 1, "town_areas": 1, "buildings": 2, "building_parts": 3, "water": 4}
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
        # (and the town signs' communes of the cells under the car: the first road tile waited ~2-5 s for their
        # division areas after the block, ~40 MB of a French place's row groups that its neighbours' cells share;
        # fetched now they come beside the block's heights. The same cells the tile asks for, no more)
        towns = {(k, "town_areas") for tx, ty in roadsmod.tiles_near(x, y, 1.0)
                 for k in roadsmod.cells_of_tile(anchor.frame, tx, ty)}
        order = sorted([kl for kl in needs if kl[1] in self.WARM_PLACE_LAYERS] + sorted(towns, key=str),
                       key=lambda kl: (self.WARM_ORDER.get(kl[1], 9), where(kl[0]), str(kl[0]), kl[1]))
        n = self.mapdata.warm(order)
        if getattr(self.sea, "prefetch_full", None) is not None:   # (the coast's sea floors: NOAA's slow requests
                                                                   # from now, not when a deck asks)
            own = {(k.x, k.y): where(k) for tx, ty in roadsmod.tiles_near(x, y, roadsmod.SEND_M)
                   for k in roadsmod.cells_of_tile(anchor.frame, tx, ty)}
            self.sea.prefetch_full(sorted(own, key=lambda k: (own[k], k)))
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
            c = navgraphmod.country_at(self.airports_db, la, lo, _borders(self))
            got = (bool(navgraphmod.road_rules(c)["rightHandDrive"]), bool(navgraphmod.road_rules(c)["mph"]),
                   furnituremod.buried_share(c),
                   furnituremod.lamp_region(c, navgraphmod.region_at(self.airports_db, la, lo, _borders(self))),
                   c, demmod.zoom_for(la, 1.25, demmod.FINE_ZOOM))
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


    def build_traffic_geo(self, gx, gy, far=False):
        """One visual-road geotile, on the existing worker pool; session files survive anchor changes."""
        if self.far_bld_pool is None or self.mapdata is None:
            return None
        memo = self.__dict__.setdefault("_traffic_geo_done", {})
        got = memo.get((gx, gy, far))
        if got is not None and os.path.exists(os.path.join(self.out_dir, os.path.basename(got[0]))):
            return got
        lh, mph, _buried, _lamps, _country, gzoom = self.geo_rules(gx, gy, distanttraffic.Z)
        got = self.far_bld_pool.submit(blockworker.traffic_geo, gx, gy, gzoom, self.out_dir, self.vfs_dir, lh, mph, far).result()
        if got is not None:
            if len(memo) >= 512:
                memo.clear()
            memo[(gx, gy, far)] = got
        return got

    def build_roads_geo(self, aid, gx, gy, content=True, skin=True, exact=None):
        """blockworker.roads_geo in a road process: geotile (gx, gy)'s content (once: kept by the mod across
        rebases), anchor aid's skin and exact variants. Its gas station pads kept for the ground's painters. None
        when the anchor is gone. Worker threads (roads.GeoTracker)."""
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None or self.road_pool is None or Server.is_abandoned(self, aid):
            return None
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        fut = self.road_pool.submit(
            blockworker.roads_geo, aid, anchor.lat, anchor.lon, anchor.zooms[0], gx, gy, gzoom, self.out_dir,
            self.vfs_dir, self.sidewalks, DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
            self.buildings, lh, mph, self.anchor_q(anchor) if skin else None, self.gas_stations, buried, lamps,
            country, content, skin, exact, Server.pole_profile(self), designsmod.enabled(self.dej_on),
            Server.towns_enabled(self), Server.wear_arg(self))
        t0, waited = time.perf_counter(), []
        try:
            res = poolsmod.result_running(fut, ROAD_TILE_TIMEOUT_S, waited, abandon=lambda: Server.is_abandoned(self, aid)
                                          or Server.geo_left_behind(self, aid, gx, gy) or Server.geo_unwanted(self, aid, gx, gy))
            took = time.perf_counter() - t0
            if took > ROAD_GEO_SLOW_S:      # (nyc_speed.py's road lead: queue against build)
                st = {}
                for part in ((res or {}).get("stages") or {}).values():
                    for k, v in (part or {}).items():
                        st[k] = st.get(k, 0.0) + v
                top = " ".join(f"{k} {v:.1f}" for k, v in sorted(st.items(), key=lambda kv: -kv[1])[:5])
                tm = (res or {}).get("time") or {}
                log.info("road geotile %d_%d in %.1f s (queue %.1f; content %.1f s %.1f cpu, skin %.1f s %.1f cpu, "
                         "exact %.1f s): %s",
                         gx, gy, took, waited[0] if waited else 0.0, tm.get("content_s", 0.0),
                         tm.get("content_cpu", 0.0), tm.get("skin_s", 0.0), tm.get("skin_cpu", 0.0),
                         tm.get("exact_s", 0.0), top)
        except poolsmod.Abandoned:
            return None                         # (a teleport left its anchor: not built)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            log.warning("road geotile %d_%d: no answer from its worker in %.0f s: built again", gx, gy,
                        ROAD_TILE_TIMEOUT_S)
            if hasattr(self.road_pool, "recycle"):
                self.road_pool.recycle(f"road geotile {gx}_{gy} hung for {ROAD_TILE_TIMEOUT_S:.0f} s")
            raise
        if res is not None and res.get("content") is not None:
            self.keep_fuel_pads(res["content"][3])
            Server.keep_fuel_tried(self, (res["content"][2] or {}).get("fuel_tried"))
        return res

    def geo_content_key(self, gx, gy):
        """The revisit key geotile (gx, gy)'s road content has with this server's settings (blockworker._geo_content_key,
        its own place's rules), or None (no map data). Near Germany with its junction signs on the key names the border
        data in use (designs.cache_parts): the first such key fetches it here once (regionaldata, then on disk)."""
        if self.mapdata is None:
            return None
        import geotiles
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        return blockworker._geo_content_key(geotiles.geobox(gx, gy).key, gzoom, self.buildings, lh, mph,
                                            self.gas_stations, buried, lamps, country, md=self.mapdata.versions(),
                                            models_dir=self.root, pole_profile=Server.pole_profile(self),
                                            de_junctions=designsmod.enabled(self.dej_on),
                                            towns_on=Server.towns_enabled(self), wear=Server.wear_arg(self))

    def geo_prebuilt(self, gx, gy):
        """Is geotile (gx, gy)'s road content in the revisit cache (no worker; no network but Germany's border data
        once, geo_content_key)? -> its key or None."""
        k = self.geo_content_key(gx, gy)
        return k if k is not None and revisitmod.has(self.cache_dir, "roadsg", k) else None

    def fast_precache(self, on):
        """The saved areas' fast mode (areas.py; the mod pauses the game meanwhile): road processes of its own for the
        world phase (FAST_POOL_*) and the full priority split (every core for the server, the game on its first
        ones), or both back. Any thread."""
        with self._fast_lock:
            if on:
                if self.fast_pool is None and self.make_fast_pool is not None:
                    n = fast_pool_size()
                    try:
                        self.fast_pool = self.make_fast_pool(n)
                        log.info("fast pre-cache: %d road processes of its own", n)
                    except Exception as e:      # (no processes: the road pool builds the world phase)
                        log.warning("fast pre-cache: no road processes of its own: %s: %s", type(e).__name__, e)
                self.split_boost(True, "fast pre-cache")
                return
            pool, self.fast_pool = self.fast_pool, None
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                log.info("fast pre-cache: its road processes closed")
            self.split_boost(False, "fast pre-cache")

    def prebuild_geo(self, gx, gy):
        """Saved areas' world phase: geotile (gx, gy)'s road content built into the revisit cache in a road process
        (blockworker.prebuild_geo), the game sent nothing. -> its revisit key, or None (not complete: tried later)."""
        if self.mapdata is None or self.road_pool is None or not revisitmod.ON:
            return None
        import roadz
        lh, mph, buried, lamps, country, gzoom = self.geo_rules(gx, gy)
        # (beside the session folders, as a session's: a 3dmr.eu model baked there finds the models' cache, its textures'
        # game path and the mod's materials file where the game's own tiles do: buildings._custom_tile_entries, PR #626)
        scratch = os.path.join(self.root, "areas_scratch")
        fast = self.fast_pool           # (fast mode: its own processes, the game paused: no need to stand back)
        submit = fast.submit if fast is not None else getattr(self.road_pool, "submit_low", self.road_pool.submit)
        # (the fast mode's own processes may wait for a cell's lidar; the live pool keeps its short wait)
        timeout = (max(ROAD_TILE_TIMEOUT_S, roadz.PRECACHE_LIDAR_WAIT_S + 120.0) if fast is not None
                   else ROAD_TILE_TIMEOUT_S)
        return poolsmod.result_running(submit(
            blockworker.prebuild_geo, gx, gy, gzoom, scratch, self.buildings, lh, mph, self.gas_stations, buried,
            lamps, country, "/temp/beamngEarth/areas_scratch", pole_profile=Server.pole_profile(self),
            wait_gaps=fast is not None, de_junctions=designsmod.enabled(self.dej_on),
            towns_on=Server.towns_enabled(self), wear=Server.wear_arg(self)), timeout)

    def build_roads_tile(self, aid, tx, ty):
        """roads.py: one road tile of anchor aid, written to the session folder. Returns (vfs path,
        chunks) or None if the anchor is gone. Worker threads (fetches the map cells it lacks)."""
        import poleprofiles
        anchor = self.anchors.get(aid)
        if anchor is None or self.mapdata is None:
            return None
        if self.road_pool is not None:
            t0 = time.perf_counter()
            fut = self.road_pool.submit(
                blockworker.roads_tile, aid, anchor.lat, anchor.lon, anchor.zooms[0], tx, ty, self.out_dir,
                self.vfs_dir, self.sidewalks, DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
                self.buildings, self.left_hand(anchor), self.mph(anchor), self.anchor_q(anchor),
                self.gas_stations, self.buried(anchor), self.lamp_region(anchor), self.country(anchor), Server.pole_profile(self, anchor),
                designsmod.enabled(self.dej_on), Server.towns_enabled(self), Server.wear_arg(self))
            try:
                # bounded: a worker that never answered held this thread and its roads.Tracker slot for good (4
                # slots: four hung tiles and no road tile ever again, the loading line at "roads & buildings 0/26")
                res = poolsmod.result_running(fut, ROAD_TILE_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                log.warning("road tile %d_%d of anchor %d: no answer from its worker in %.0f s: built again",
                            tx, ty, aid, ROAD_TILE_TIMEOUT_S)
                if hasattr(self.road_pool, "recycle"):
                    self.road_pool.recycle(f"road tile {tx}_{ty} hung for {ROAD_TILE_TIMEOUT_S:.0f} s")
                raise
            log.debug("road tile %d_%d: %.2f s (queue + build)", tx, ty, time.perf_counter() - t0)
            self.keep_fuel_pads(res[2])
            if len(res) > 3:            # (a revisit cache hit has no report: nothing marked)
                Server.keep_fuel_tried(self, res[3])
            return res[:2]
        self.mapdata.warm(roadsmod.tile_needs(anchor.frame, tx, ty, self.buildings, self.road_widths is not None,
                                              1 + seacarvemod.LINE_CELLS if self.sea is not None else 0))
        cells = [self.mapdata.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                 for k in roadsmod.cells_of_tile(anchor.frame, tx, ty, 200 if poleprofiles.kit_enabled(Server.pole_profile(self, anchor)) else roadsmod.OVERLAP)]
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
        fuel, skip, fuel_cuts, fuel_pads, fuel_tried = [], frozenset(), [], [], []
        if self.gas_stations:       # pumps and refuel areas; their canopy outlines left out of the buildings
            fuel, skip = gasstationsmod.tile_sites(anchor.frame, tx, ty, self.mapdata, elev, cuts=fuel_cuts,
                                                   pads=fuel_pads, tried=fuel_tried)
            Server.keep_fuel_tried(self, fuel_tried)
        if self.buildings:          # bus stations draw their own roofs: the sheds the map data makes of them are left out
            import busstations
            import busyard
            skip = frozenset(skip) | busstations.tile_skip(anchor.frame, tx, ty, self.mapdata)
            import railstations
            skip = skip | railstations.tile_skip(anchor.frame, tx, ty, self.mapdata)
            busyard.tile_pads(anchor.frame, tx, ty, self.mapdata, fuel_pads)      # ... and their yards are painted in
        self.keep_fuel_pads(fuel_pads)
        props = [] if self.buildings else None
        lines = [] if self.buildings else None
        wires = [] if self.buildings else None
        park = [] if self.buildings else None
        signs = [] if self.buildings and poleprofiles.kit_enabled(Server.pole_profile(self, anchor)) else None
        items = town = None
        wrows, mapped_lamps = [], None
        _bridge_fps, _bridge_ends = [], {}      # 3dmr model bridges over the tile (as blockworker._tile_parts)
        if self.buildings:
            # stock towers, containers, billboards and barns; a replaced barn's id is left out of the buildings
            try:
                import worldprops
                mapped_lamps = [] if worldprops.MAPPED_LAMPS else None     # (furniture.place draws them)
                wrows, barn_ids = worldprops.tile_props(anchor.frame, tx, ty, self.mapdata, elev, lamps=mapped_lamps)
                skip = frozenset(skip) | frozenset(barn_ids)
            except Exception as ex:
                log.warning("world props: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
                wrows, mapped_lamps = [], None
            # (on the graded ground, as blockworker.roads_tile)
            items, _custom = buildingsmod.road_tile_items(anchor.frame, tx, ty, self.mapdata, elev,
                                                 decks=blockworker._tile_decks(anchor.bridges, cells, tx, ty), skip=skip,
                                                 cache_dir=os.path.dirname(self.out_dir), urban_fn=urban,
                                                 widths=self.road_widths, left_hand=self.left_hand(anchor))
            _bridge_fps = buildingsmod.bridge_footprints(     # (each laid onto its mapped bridge, baked there)
                anchor.frame, tx, ty, os.path.dirname(self.out_dir),
                cell_fn=lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads"))
            _custom = buildingsmod.place_bridge_models(_custom, _bridge_fps)
            if anchor.bridges is not None and any(m in {c["id"] for c in _custom} for _fp, _c, m in _bridge_fps):
                try:
                    _bridge_ends = buildingsmod.bridge_road_ends(
                        _bridge_fps, anchor.bridges, cells, anchor.frame,
                        lambda x, y: elev(*anchor.frame.enu_to_geodetic(x, y, np.zeros(len(x)))[:2]))
                except Exception as ex:
                    log.warning("3dmr: mapped bridge ends: %s: %s", type(ex).__name__, ex)
            if wrows:
                props.extend(wrows)
            town = buildingsmod.coverage(items) < buildingsmod.POLES_COVERAGE
        rwalls = []
        ctx = []
        box = roadsmod.tile_bbox(tx, ty)
        de_routes = ()      # (the German junction signs' ref ranks near Germany: blockworker._tile_parts' cells, gate)
        if designsmod.enabled(self.dej_on) and designsmod.near(anchor.frame, box):
            reach = 200 if poleprofiles.kit_enabled(Server.pole_profile(self, anchor)) else roadsmod.OVERLAP
            de_routes = blockworker._de_routes(self.mapdata, roadsmod.cells_of_tile(anchor.frame, tx, ty, reach))
        if props is not None:                       # (the props' side sources, read while the tile is built)
            self.mapdata.side_prefetch(roadsmod.cells_of_tile(anchor.frame, tx, ty))
        import streetfront
        frontage_country = streetfront.country_at_tile(anchor.frame, box) if items is not None else None
        if frontage_country == "FR" and signs is None:
            signs = []
        cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")  # noqa: E731

        def drawn(la, lo):      # the block's ground: raised over the low arches as the terrain is (_make_heightmap_here)
            return roadsmod.block_arch_raise(anchor.tunnels, cell_fn, anchor.frame, la, lo, elev(la, lo),
                                             (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
        cover = []                      # the ground carriageways for the decks' parapets (roadmesh.deck ground=)
        marks = []                  # stall lines along parking aisles (parking.finish_markings adds the lot polygons')
        near_roads = []             # the ground carriageways near the tile (the median barriers': medians.tile_rows)
        nkeys = roadsmod.cells_of_tile(anchor.frame, tx, ty, blockworker.SIGNALS_MARGIN_M)
        # (Germany's mapped nodes too: its junction signs on, the junctions it decides reach into Germany)
        de_nodes = designsmod.reads_here(designsmod.enabled(self.dej_on), anchor.frame, designsmod.tile_reach(box))
        sign_nodes = secrossings.read(self.mapdata, anchor.frame, nkeys) \
            if secrossings.wanted(self.country(anchor), de_nodes) else None
        clearance = [] if self.buildings else None     # (build_tile's RoadClearance, PR #629: the powerlines' too)
        roadside_jobs = []
        drives = []
        mapped_drives = [] if frontage_country else None
        import euguardrails
        sign_obstacles=None
        if self.buildings and euguardrails.enabled():
            import signplacement
            sign_obstacles=signplacement.for_tile(anchor.frame,tx,ty,self.mapdata,items,skip)
        town_faces = {}                 # the town signs' faces the tile's props use (townentry.enabled)
        towns = None
        if (props is not None and Server.towns_enabled(self) and self.mapdata is not None
                and self.surface is not None):
            if getattr(self, "towns", None) is None:
                self.towns = townentrymod.Towns(self.mapdata, self.surface)
            towns = self.towns.maker(anchor.frame, roadsmod.cells_of_tile(anchor.frame, tx, ty,
                                                                          blockworker.TOWNS_MARGIN_M),
                                     self.out_dir, self.vfs_dir, town_faces)
        chunks = roadsmod.build_tile(anchor.frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=anchor.bridges,
                                     sign_obstacles=sign_obstacles,
                                     walk_cuts=fuel_cuts,    # (the stations' made-up driveways: no sidewalk across them)
                                     widths=self.road_widths, props=props, lines=lines, wires=wires, town=town, park=park,
                                     left_hand=self.left_hand(anchor), mph=self.mph(anchor), buried=self.buried(anchor),
                                     lamp_region=self.lamp_region(anchor), country=self.country(anchor), pole_profile=Server.pole_profile(self, anchor), signs=signs,
                                     structures=buildingsmod.centres(items, tx, ty) if items else (),
                                     tunnels=anchor.tunnels, walls=rwalls,
                                     ground=lambda x, y: roadmeshmod.drawn_ground(anchor.frame, drawn, np.c_[x, y],
                                                                                  self.anchor_q(anchor)),
                                     terrain_paint=self.surface is not None, cover_out=cover, marks=marks,
                                     roads_out=near_roads, roadside_jobs=roadside_jobs, signals=furnituremod.signals_for(self.mapdata, anchor.frame, nkeys),
                                     driveways_out=mapped_drives,
                                     nodes=sign_nodes, clearance_out=clearance,
                                     development=developmentmod.for_tile(self.mapdata, anchor.frame, tx, ty), towns=towns,
                                     de_on=designsmod.enabled(self.dej_on), de_routes=de_routes, mapped_lamps=mapped_lamps,
                                     wear=Server.wear_arg(self),
                                     cover_fn=getattr(self.surface, "classes", None))
        # (railway and tram tracks on the ground)
        chunks += railsmod.tile_chunks(anchor.frame, tx, ty, cells, self.country(anchor), urban)
        if self.airports is not None:   # runway markings, drawn by the road mesh like the lane lines
            chunks += self.airports.tile_chunks(anchor.frame, tx, ty, elev, roadsmod.tile_bbox)
        x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
        if items is not None:           # front doors and shopfronts, driveways into the road mesh
            # (no street tree in a house: its own or the next tile's)
            margin = streetfront.BUILT_FRONT_M + 2. if frontage_country else 2.
            rings = buildingsmod.near_rings(anchor.frame, tx, ty, self.mapdata, margin=margin, skip=skip)
            props[:] = furnituremod.off_buildings(props, rings)
            props.extend(buildingsmod.doors(items, lines, x0, y0, drives, infer_fences=frontage_country != "FR"))
            props.extend(buildingsmod.toll_booth_rows(items, x0, y0))
            drives.extend(buildingsmod.garage_drives(items, lines, x0, y0))
            chunks += drives
            park = parkingmod.clear_of(park, props, drives)
            park = parkingmod.tile_lot_spots(anchor.frame, tx, ty, self.mapdata, elev, items, park,
                                             separate_bays=bool(frontage_country))
            front_grass = [] if self.surface is not None else None
            paving, spots = streetfront.finish(anchor.frame, tx, ty, self.mapdata, walks, rings, items, near_roads,
                                               mapped_drives, drives, lambda x, y: roadmeshmod.drawn_ground(
                                                   anchor.frame, drawn, np.c_[x, y], self.anchor_q(anchor)),
                                               frontage_country, self.left_hand(anchor), redrape=True, props=props,
                                               grass=front_grass)
            chunks += paving
            if frontage_country and front_grass is not None:
                key = f"{anchor.frame.lat0:.8f}:{anchor.frame.lon0:.8f}:{tx}:{ty}"
                self.keep_fuel_pads(streetfront.pad_items(anchor.frame, box, key, paving, front_grass))
            park += spots
            park = gasstationsmod.clear_park(park, fuel)
            if park is not None:
                import busstationpark
                park = park + busstationpark.rows(anchor.frame, self.mapdata, box, elev)
            buildingsmod.brick_walks(walks, items, anchor.lat, anchor.lon)
        chunks += parkingmod.finish_markings(anchor.frame, tx, ty, self.mapdata, elev, items, marks,
                                             separate_bays=bool(frontage_country))
        chunks += blockworker._bay_lines(anchor.frame, tx, ty, self.mapdata)
        if _bridge_fps:                 # (the model bridge is the road there: its deck slab)
            chunks = buildingsmod.bridge_stub_chunks(_bridge_fps, chunks)
            ctx[:] = buildingsmod.bridge_stub_chunks(_bridge_fps, ctx)
            if props is not None:
                props[:] = buildingsmod.bridge_stub_props(_bridge_fps, props)

        # Share the bridge mesh's exact parapet geometry with the tile builder.
        # Mapped rails on a deck are replaced by guardrails.bridge_rows(); the parapet runs also feed the fallback matching.
        pads_near = []
        if anchor.bridges is not None:  # (the junction pads round the tile: roads.deck_pads)
            try:
                pad_parts = list(anchor.bridges.parts(cells, (box[0] - 40, box[1] - 40, box[2] + 40, box[3] + 40)))
                pad_parts = roadsmod.shifted_parts(pad_parts, roadsmod.deck_shift_fn(
                    anchor.frame, cells, urban, self.road_widths, self.left_hand(anchor)))
                pad_parts = buildingsmod.bridge_stub_parts(_bridge_fps, pad_parts)
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
        guardrail_decks = []
        rail_decks = []                 # railways.tile_mesh fills these in below (fences.py stands walls on them)
        try:
            deck_parts = list(anchor.bridges.parts(cells, (box[0]-10, box[1]-10, box[2]+10, box[3]+10)))
            deck_parts = buildingsmod.bridge_stub_parts(_bridge_fps, deck_parts)
            guardrail_decks = [{"n": np.column_stack((xy, z)), "w": spec.width}
                               for _fid, spec, xy, z, _lon in deck_parts]
            guardrail_rows = guardrailsmod.forest_rows(anchor.frame, infra_fn, box, elev,
                                                       decks=guardrail_decks, parapets=parapets,
                                                       bridge_rails=roadmeshmod.BRIDGE_RAILS,
                                                       supported_decks_out=low_parapet_decks, roads=near_roads)
        except Exception as ex:
            log.warning("mapped guardrails: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            # A failed mapped-data pass cannot establish that a bridge needs rails.
            low_parapet_decks.clear()
            guardrail_rows = []
        import medianbarriers
        barrier_mesh, barrier_chunks = medianbarriers.tile(
            self.mapdata, anchor.frame, tx, ty, elev, urban, self.road_widths, self.left_hand(anchor))
        guardrail_rows = medianbarriers.mapped_rows(guardrail_rows, barrier_chunks)
        guardrail_rows = guardrail_rows + blockworker._median_rows(
            anchor.frame, near_roads, box, elev, self.left_hand(anchor), self.country(anchor), guardrail_rows, tx, ty,
            deck_parts, barrier_chunks)
        guardrail_rows = guardrail_rows + blockworker._drop_rows(
            anchor.frame, near_roads, box, elev, guardrail_rows, tx, ty, guardrail_decks)
        wide = {}                       # (the wider cells' ways, both of these want the same)
        guardrail_rows = guardrail_rows + blockworker._motesfri_rows(
            anchor.frame, cell_fn, near_roads, box, elev, urban, self.road_widths, self.left_hand(anchor),
            self.country(anchor), guardrail_rows, tx, ty, chunks, wide)
        guardrail_rows = guardrail_rows + blockworker._kantstolpe_rows(
            anchor.frame, near_roads, box, elev, self.country(anchor), guardrail_rows, tx, ty)
        guardrail_rows = guardrail_rows + blockworker._sign_rows(
            anchor.frame, cell_fn, near_roads, sign_nodes, box, elev, urban, self.road_widths, self.left_hand(anchor),
            self.country(anchor), tx, ty, wide)
        for finish in roadside_jobs:
            finish(guardrail_rows, [(np.asarray(ch['n']), ch['w'] / 2) for ch in drives if 'n' in ch and 'w' in ch])
        mesh = roadmeshmod.build(anchor.frame, chunks, elev, (x0, y0, 0.0),
                                 DETAIL_ROAD_LIFT if self.near_visual == "detail" else 0.0,
                                 clip=box, follow=True, q=self.anchor_q(anchor), drawn=drawn)
        if self.near_visual != "detail":        # (detail mode: the drawn block paints the squares)
            mesh = blockworker._with_plazas(mesh, anchor.frame, roadsmod.tile_bbox(tx, ty), elev, (x0, y0, 0.0),
                                            self.surface)
        _piers, _beams = buildingsmod.bridge_stub_piers(
            _bridge_fps,
            anchor.bridges.piers_in(roadsmod.tile_bbox(tx, ty)) if anchor.bridges is not None else (),
            anchor.bridges.beams_in(roadsmod.tile_bbox(tx, ty)) if anchor.bridges is not None else ())
        dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0), _piers, ctx, _beams,
                                 ground=cover, parapets=parapets,
                                 low_parapet_decks=low_parapet_decks, pads=pads)
        import fences
        try:                        # the mapped stone walls (GitHub #938), in the retaining walls' stone
            rwalls = rwalls + fences.stone_walls(anchor.frame, infra_fn, box, elev, roads=near_roads)
        except Exception as ex:
            log.warning("stone walls: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
        import railways
        rail_props = []                 # (buffer stops and the walk signs at footway crossings: railways.tile_mesh's furniture)
        rmesh, rail_decks = railways.tile_mesh(anchor.frame, tx, ty,
                                   [cell_fn(k) for k in roadsmod.cells_of_tile(anchor.frame, tx, ty, railways.REACH_M)],
                                   railways.point_ground(anchor.frame, elev, (aid, anchor.lat, anchor.lon)),
                                   far_ground=railways.point_ground(
                                       anchor.frame,
                                       lambda la, lo: self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, zoom)),
                                       ("raw", aid, anchor.lat, anchor.lon)),
                                   return_decks=True, load=cell_fn,
                                   bed_elev=getattr(getattr(self, "plan_store", None), "elev", None), furniture=rail_props,
                                   under=((lambda xy: buildingsmod._under_bridges(_bridge_fps, xy))
                                          if _bridge_fps else None))
        if rail_props:
            props = (props or []) + rail_props
        bld = None
        garage_sound = ()
        oil_wells = []
        custom_entries = []             # the tile's baked 3dmr.eu / local models (roads.write_tile's "b3dmr")
        if self.buildings:
            import oilwells
            _baked_entries, _baked_radii = [], []
            if _custom and self.out_dir and self.vfs_dir:
                _baked_entries, _baked_radii, _boxes = buildingsmod.custom_models(
                    anchor.frame, _custom, elev, self.out_dir, self.vfs_dir, road_ends=_bridge_ends,
                    items=items, origin=(x0, y0))
                custom_entries = _baked_entries
            bld, _ = buildingsmod.road_tile(anchor.frame, tx, ty, self.mapdata, elev, None, None,
                                            items, entries=_baked_entries, radii=_baked_radii)
            garage_sound = buildingsmod.garage_sound_boxes(items, *roadsmod.tile_bbox(tx, ty)[:2],
                                                           roadsmod.SOUND_GARAGE)
            oil_wells = oilwells.tile_wells(anchor.frame, tx, ty, elev)     # (the mod's pump jacks)
            import powerlines
            powerlines.tile_lines(anchor.frame, tx, ty, self.mapdata, elev, props, wires,
                                  clearance=clearance[0] if clearance else None)    # high-voltage lines
            import fences
            try:
                props.extend(fences.rows(anchor.frame, infra_fn, box, elev, decks=[
                    {"n": np.column_stack((xy, z)), "w": spec.width} for _f, spec, xy, z, _l in deck_parts] + rail_decks,
                    roads=near_roads, french=frontage_country == "FR"))
            except Exception as ex:
                log.warning("fences: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            import picnic
            land_fn = lambda key: self.mapdata.cell(key, layers=("land_use",), kind="demand").layers.get("land_use")
            bld_fn = lambda key: self.mapdata.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")
            osm = getattr(self.mapdata, "picnic_osm", None)
            try:
                props.extend(picnic.rows(anchor.frame, infra_fn, land_fn, box, elev,
                                         (lambda keys: osm.cells(keys, wait=False)) if osm is not None else None,
                                         bld_fn))
            except Exception as ex:
                log.warning("picnic: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            import busfurniture
            import shelters
            try:                        # (not under a bus station's roof, nor in its bays: it has its own)
                props.extend(busfurniture.clear_of_stations(
                    shelters.rows(anchor.frame, infra_fn, box, elev, near_roads, lambda k: self.mapdata.cell(
                        k, layers=("roads",), kind="demand").layers.get("roads"), country=self.country(anchor)),
                    anchor.frame, self.mapdata, box))
            except Exception as ex:
                log.warning("shelters: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            import fast
            import areaprops as places
            try:       # (mapped benches, bins, hydrants ...: furniture.place's guessed ones give way to them)
                mapped = {}
                srows = places.street_rows(anchor.frame, box, self.mapdata, elev, near_roads, self.country(anchor),
                                           mapped_out=mapped)
                props[:] = places.drop_inferred(props, mapped)
                props.extend(srows)
            except fast.NativeMissing:
                raise
            except Exception as ex:
                log.warning("street furniture: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            try:       # (tall buildings' obstruction lights, long tunnels' jet fans)
                props.extend(places.warning_rows(items, x0, y0))
                props.extend(places.tunnel_fan_rows(chunks))
            except fast.NativeMissing:
                raise
            except Exception as ex:
                log.warning("lights and fans: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            try:       # (solar farms, container terminals, marinas, trailer parks, construction sites)
                props.extend(places.tile_rows(anchor.frame, box, self.mapdata, elev, near_roads, self.country(anchor)))
            except fast.NativeMissing:
                raise
            except Exception as ex:
                log.warning("places: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            try:
                props[:] = busfurniture.drop_stock_lamps(props, anchor.frame, self.mapdata, box, wires)
                props.extend(busfurniture.rows(anchor.frame, self.mapdata, box, elev, poleprofiles.lamp_country_selector(
                    Server.pole_profile(self, anchor), anchor.frame), props))   # benches, bins, lamps at bus stations
            except Exception as ex:
                log.warning("bus station furniture: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            try:
                import railstationprops
                props.extend(railstationprops.rows(anchor.frame, self.mapdata, box, elev, poleprofiles.lamp_country_selector(
                    Server.pole_profile(self, anchor), anchor.frame)))
            except Exception as ex:
                log.warning("rail station furniture: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            import industry
            garage_sound = (garage_sound + industry.refinery_sound_rows(anchor.frame, tx, ty, self.mapdata, elev)
                            + industry.power_plant_sound_rows(anchor.frame, tx, ty, self.mapdata, elev))
        import windturbines
        turbine_rows = windturbines.tile_turbines(
            anchor.frame, tx, ty, elev,
            windturbines.worldcover_built_fn(self.mapdata) if self.mapdata is not None else None)
        if self.mapdata is not None:
            import golf
            try:
                land_fn = lambda key: self.mapdata.cell(key, layers=("land_use",), kind="demand").layers.get("land_use")
                flag_rows = golf.rows(anchor.frame, land_fn, box, elev)
                if flag_rows:
                    props = (props or []) + flag_rows
            except Exception as ex:
                log.warning("golf: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        import radars
        radar_props, speed_traps = radars.tile_rows(anchor.frame, box, self.mapdata, elev, self.road_widths, urban)
        if radar_props:
            props = (props or []) + radar_props
        return roadsmod.write_tile(self.out_dir, self.vfs_dir, aid, tx, ty, chunks, walks if self.sidewalks else None,
                                   mesh, dmesh, bld, props, wires, park, tunnel_mesh=tmesh, fuel=fuel,
                                   rail_mesh=rmesh, guardrail_rows=guardrail_rows, garage_sound=garage_sound,
                                   custom_entries=custom_entries, signs=signs, wells=oil_wells, bld_none=bool(self.buildings),
                                   barrier_mesh=barrier_mesh, barrier_chunks=barrier_chunks,
                                   bus_col=blockworker._station_collision(anchor.frame, box, elev, self.mapdata)
                                   if self.buildings else None,
                                   turbines=turbine_rows, towns=town_faces,
                                   walk_ground=lambda x, y: roadmeshmod.drawn_ground(
                                       anchor.frame, drawn, np.c_[x, y], self.anchor_q(anchor)),
                                   speed_traps=speed_traps)

    def set_dej(self, on):
        """The settings page's German junction signs switch (message german_junctions): True when it changed (the road
        tiles and the sign tiles are then sent again, as the gas stations' and the direction signs' are)."""
        on = bool(on)
        changed = on != self.dej_on
        self.dej_on = on
        if changed:
            log.info("German junction signs %s (settings)", "on" if on else "off")
        return changed

    def towns_enabled(self):
        """Whether the town signs are built (townentry.enabled: the settings page's Town signs, BNE_TOWNSIGNS), and
        with them the speed-limit signs without Germany's general limits: only with the buildings, the road tiles'
        props that carry the signs (Codex on PR #892: with --buildings off neither the Ortstafel nor the 50 stood).
        Called as Server.towns_enabled(self), as pole_profile (the tests' servers are namespaces)."""
        return townentrymod.enabled(self.towns_on) and bool(self.buildings)

    def wear_tier(self):
        """The road wear's tier the road tiles are built with (roadwear.tier_of of the settings page's Road detail).
        Called as Server.wear_tier(self) (the tests' servers are namespaces)."""
        return roadwear.tier_of(getattr(self, "road_wear", None))

    def wear_arg(self):
        """The road tiles' wear argument (roadwear.py): the tier and the season mode of the imagery (the fallen leaves:
        roadwear.fall_of). Called as Server.wear_arg(self)."""
        im = getattr(self, "imagery", None)
        return {"tier": Server.wear_tier(self), "season": getattr(im, "season_mode", None) or "off"}

    def set_road_wear(self, value):
        """The settings page's Road detail (message road_wear {value: 'off' / 'basic' / 'full'}): True when the tier
        changed (the road tiles are then sent again)."""
        before = Server.wear_tier(self)
        self.road_wear = value
        changed = Server.wear_tier(self) != before
        if changed:
            log.info("road wear tier %d (settings)", Server.wear_tier(self))
        return changed

    def set_towns(self, on):
        """The settings page's Town signs switch (message town_signs): True when it changed (the road tiles and the
        sign tiles are then sent again, as the gas stations' and the direction signs' are)."""
        on = bool(on)
        changed = on != self.towns_on
        self.towns_on = on
        if changed:
            log.info("town signs %s (settings)", "on" if on else "off")
        return changed

    def set_dirs(self, on):
        """The settings page's Direction signs switch (message direction_signs): True when it changed."""
        on = bool(on)
        changed = on != self.dirs_on
        self.dirs_on = on
        return changed

    def set_rsigns(self, on):
        """The settings page's Road signs switch (message road_signs): True when it changed."""
        on = bool(on)
        changed = on != self.rsigns_on
        self.rsigns_on = on
        return changed

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
            # (the (done, wanted) pairs only: send_progress's counts end with the downloads [in flight, MB, MB/s]
            # since the loading line shows them, and unpacking that as a pair raised ValueError here, every minute,
            # so the saved areas never downloaded)
            if counts and any(len(p) == 2 and p[0] < p[1] for p in counts):
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
            "pole_profile": Server.pole_profile(self),
            "de_junctions": designsmod.enabled(self.dej_on),
            "towns_on": Server.towns_enabled(self),
            "road_wear": Server.wear_tier(self),
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

    def _osm_tags_warm(self):
        """Thread: the OSM road tag shards' manifest and archive head read at the start (MapData.osm_tags_warm)."""
        try:
            self.mapdata.osm_tags_warm()
        except Exception as e:      # (the first cell reads them, or the Layercake file stands in)
            log.info("osm tags: the shards' head: %s: %s", type(e).__name__, e)

    def _osm_fuel_warm(self, sleep=time.sleep):
        """Worker thread: the OSM fuel file's index read at the start, tried again every OSM_FUEL_WARM_WAIT_S when the
        host turns it away (in game, 2026-10-29: "busy, prefetch dropped", "bucket low" while the start's road tags
        took both slots), up to OSM_FUEL_WARM_TRIES times (then the first cell reads it)."""
        for i in range(OSM_FUEL_WARM_TRIES):
            try:
                t0 = time.perf_counter()
                self.mapdata.osm_fuel_warm()
                log.info("osm fuel: the file's index read in %.1f s", time.perf_counter() - t0)
                return
            except Exception as e:
                log.info("osm fuel: the file's index (try %d): %s: %s", i + 1, type(e).__name__, e)
                if i + 1 < OSM_FUEL_WARM_TRIES:
                    sleep(OSM_FUEL_WARM_WAIT_S)

    def osm_fuel_want(self, conn, aid, tiles):
        """The OpenStreetMap fuel cells of road tiles of anchor aid (tx, ty), or of road geotiles (gx, gy) when conn's
        tracker is roads.GeoTracker (their stations' reach: osm_fuel_cells), fetched in the background, each once; one
        with stations that comes after conn sent a tile it reaches has that tile built again (osm_fuel_tiles,
        roads.Tracker.again / GeoTracker.again)."""
        anchor = self.anchors.get(aid)
        if self.osm_fuel is None or anchor is None or not self.gas_stations:
            return
        geo = getattr(getattr(conn, "roads", None), "geo", False)
        loop = asyncio.get_running_loop()
        for t in tiles:
            for key in osm_fuel_cells(anchor.frame, t, geo):
                k = str(key)
                if k in self.osm_fuel_done:
                    continue
                waiting = self.osm_fuel_wait.get(k)
                if waiting is not None:
                    waiting.add((conn, aid))
                    continue
                self.osm_fuel_wait[k] = {(conn, aid)}
                fut = self.osm_fuel.submit(self._osm_fuel_cell, key)
                fut.add_done_callback(lambda f, k=k, key=key: loop.call_soon_threadsafe(self._osm_fuel_got, k, key, f))

    def _osm_fuel_cell(self, key):
        """Worker thread: the cell's OSM fuel stations, from the disk when fetched before."""
        rows = self.mapdata.osm_fuel_cached(key)
        if rows is not None:
            return rows, False
        t0 = time.perf_counter()
        rows = self.mapdata.osm_fuel_fetch(key)
        log.debug("osm fuel: cell %s, %d stations in %.2f s", key, len(rows), time.perf_counter() - t0)
        return rows, True

    def _osm_fuel_got(self, k, key, fut):
        waiting = self.osm_fuel_wait.pop(k, set())
        try:
            rows, fetched = fut.result()
        except Exception as e:          # (asked again with the next pose that wants it)
            log.debug("osm fuel: cell %s: %s: %s", k, type(e).__name__, e)
            return
        self.osm_fuel_done.add(k)
        if not rows or not fetched:     # (a cell from the disk was there when its tiles were built)
            return
        for conn, aid in waiting:
            anchor = self.anchors.get(aid)
            roads = getattr(conn, "roads", None)
            geo = getattr(roads, "geo", False)
            # (a geotile's content is the connection's, whatever the anchor: dev's geotiles, kept by the mod across
            # anchors; an anchor's road tiles only while it is the tracker's)
            if anchor is not None and roads is not None and (geo or roads.aid == aid):
                again = osm_fuel_tiles(anchor.frame, key, roads.sent, geo)
                if again:
                    log.info("osm fuel: cell %s brought %d stations: road tiles %s again", k, len(rows), sorted(again))
                    roads.again(again)

    def keep_fuel_pads(self, pads):
        """Keep the road tile's pads and refresh the material layers of the affected blocks."""
        if pads and self.surface is not None and hasattr(self.surface, "add_fuel_pads"):
            new = self.surface.add_fuel_pads(pads)
            if new:
                self.pads_stale(new)

    def keep_fuel_tried(self, tried):
        """A road tile's report of which stations it laid out (gasstations.tile_sites tried) into the station finder's
        index: a listing the world built nothing for leaves the map (the user, 2026-11-09). Worker threads."""
        idx = getattr(self, "stations", None)
        if tried and idx is not None:
            try:
                idx.mark(tried)
            except Exception as e:      # (the finder's bookkeeping never costs a tile)
                log.debug("station index: %s: %s", type(e).__name__, e)

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
            affected = None if items is None else [
                sid for sid, rings in items
                if any(self._ring_meets(anchor, ring, cx - h, cy - h, cx + h, cy + h) for ring in rings)]
            if affected is None or affected:
                self.pads_rebuild.add(key)
                if self.near_visual == "detail" and (affected is None or any(
                        not sid.startswith(surfacemod.FRONTAGE_PAD_PREFIX) for sid in affected)):
                    self.base_stale.add(key)

    @staticmethod
    def _ring_meets(anchor, ring, x0, y0, x1, y1):
        la, lo = ring
        e, n, _ = anchor.frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(len(la)))
        e, n = np.asarray(e), np.asarray(n)
        return bool(e.max() >= x0 and e.min() <= x1 and n.max() >= y0 and n.min() <= y1)

    def block_fuel_pads(self, anchor, cx, cy, size):
        """Fuel and frontage footprints around a block, for the worker's material layers."""
        if self.surface is None or not hasattr(self.surface, "frontage_pad_items"):
            return []
        h = size / 2 + 64.0
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([cx - h, cx + h]), np.array([cy - h, cy + h]), np.zeros(2))
        return self.surface.fuel_pad_items(la, lo) + self.surface.frontage_pad_items(la, lo)

    def ground_z_at(self, anchor, e, n):
        """The ground's height (the frame's up) at (e, n) of anchor's frame, from the elevation data (not graded: the
        mod sets the car down on the collision there)."""
        la, lo, _ = anchor.frame.enu_to_geodetic(e, n, 0.0)
        la, lo = float(la), float(lo)
        z = self.ground(anchor.frame, la, lo, float(self.dem.sample(la, lo, anchor.hm_zoom)))
        return round(float(anchor.frame.geodetic_to_enu(la, lo, z)[2]), 3)

    def _plan_elev(self, la, lo):
        """The road plans' ground: the DEM at grading.PROFILE_ZOOM with the tunnels' lids (tunnellids)."""
        z = self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM)
        return self.lids.apply(la, lo, z) if getattr(self, "lids", None) is not None else z

    def ground(self, frame, la, lo, z):
        """blockworker.ground here: runways graded in, the sea floor carved, the land off the roads roughened."""
        z = np.asarray(z, np.float64) + gradingmod.terrain_bumps(np.asarray(la, np.float64), np.asarray(lo, np.float64))
        if self.airports is not None:
            z = self.airports.apply(la, lo, z)
        if getattr(self, "lids", None) is not None:
            z = self.lids.apply(la, lo, z)
        z = blockworker._level_yards_of(self, z, la, lo)
        if self.sea is not None:
            z = self.sea.carve(la, lo, z)
        return z

    def grader_for(self, anchor):
        """The anchor's grading.Grader (--grading on), else None."""
        if not self.grading:
            if os.environ.get("BNGE_WORLD_BUNDLE"):
                from worldbundle import BundleError
                raise BundleError("Pinned road plans require roads and grading enabled")
            return None
        with self.grader_lock:
            if anchor.grader is None:
                # (every anchor's view of one store: the plans are the planning regions', whatever the anchor)
                if getattr(self, "plan_store", None) is None:
                    self.plan_store = gradingmod.PlanStore(
                        self.mapdata, self._plan_elev if getattr(self, "lids", None) is not None
                        else (lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM)), self.road_widths)
                    if PLAN_ELSEWHERE and getattr(self, "plan_elsewhere", None) is not None:
                        self.plan_store.delegate = self.plan_elsewhere
                # (plan_elsewhere: an anchor for the worker's store settings)
                self._plan_anchor = tuple(getattr(anchor, n, None) for n in ("id", "lat", "lon"))
                grader = gradingmod.Grader(anchor.frame, self.mapdata, self.plan_store.elev, self.road_widths,
                                          store=self.plan_store)
                grader.wet = self.sea.depth if self.sea is not None else None    # (no dams: underwater tunnels)
                grader.water = self.sea.water if self.sea is not None else None
                grader.tunnel_raw = lambda la, lo: self.ground(None, la, lo,
                    self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
                grader.dem = self.dem                # (its urgent lane: _plan_missing)
                grader.shared = plancachemod.for_grader(self.cache_dir, grader, self.road_widths, grader.wet)
                anchor.grader = grader       # publish only after a pinned bundle passed compatibility checks
            return anchor.grader

    def plan_elsewhere(self, key):
        """grading.PlanStore.delegate of this process: cell key's plans made by the prefetch lane's block process
        (blockworker.plan_cell; its own interpreter, the plans read back from the run's plan cache), waited for here.
        False when there is no such process (a lean tier's one block process builds the requested blocks: the cell is
        planned here as before) or it could not."""
        bp = getattr(self, "block_pools", None) or ()
        at = getattr(self, "_plan_anchor", None)
        if at is None or at[0] is None or len(bp) <= LANE_PREFETCH or bp[LANE_PREFETCH][0] is bp[LANE_REQUEST][0]:
            return False
        fut = bp[LANE_PREFETCH][0].submit(blockworker.plan_cell, at[0], at[1], at[2],
                                          (int(key[0]), int(key[1]), int(key[2])))
        return bool(fut.result(timeout=PLAN_ELSEWHERE_S))

    def warm_roads(self, anchor, x, y, vx, vy, tree=None):
        """Queue the road plans of the cells the physics block will cover along the player's straight path
        (WARM_STEPS_S; the block's half width + the grading pad), then those within WARM_AROUND. tree: the car's
        time-to-reach tree (reach.Tree): along its most probable path instead."""
        g = self.grader_for(anchor)
        if g is None:
            return
        reach = self.layout.block * heightmap.SQUARE / 2 + gradingmod.T_MAX + 40.0
        keys = []
        ahead = tree.points_at(WARM_STEPS_S) if tree is not None else \
            [(x + vx * t, y + vy * t) for t in WARM_STEPS_S]
        for (px, py), r in [(p, reach) for p in ahead] + [((x, y), WARM_AROUND)]:
            la, lo, _ = anchor.frame.enu_to_geodetic(px, py, 0.0)
            la, lo = float(la), float(lo)
            dlat = r / 111320.0
            dlon = r / (111320.0 * max(0.05, math.cos(math.radians(la))))
            for k in mapcells.cells_for_bbox(la - dlat, lo - dlon, la + dlat, lo + dlon):
                if k not in keys:
                    keys.append(k)
        # (the prefetch lane's process plans them; the requested blocks' process, while it builds a block, only reads
        # the planned ones into its memory, WARM_SPLIT: its own warm thread planned every cell round the player beside a first visit's
        # graded block, under one interpreter lock: 21 cells' plans, ~30 CPU-s, in the process whose block and whose
        # road tiles waited for 4 of them, the prefetch lane's process beside it planning none; York PA, 2026-11-12)
        bp = self.block_pools
        split = WARM_SPLIT and len(bp) > LANE_PREFETCH and bp[LANE_PREFETCH][0] is not bp[LANE_REQUEST][0]
        for lane, (_, q) in {id(q_): (n_, (p_, q_)) for n_, (p_, q_) in enumerate(self.block_pools)}.values():
            try:
                q.put_nowait((anchor.id, anchor.lat, anchor.lon, keys, split and lane == LANE_REQUEST))
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

    def bake_sync(self, anchor, x, y):
        """The data repo's baked packs near the player (bakepack.sync: road plans and cell facts made offline) fetched
        in a thread of their own, as the player moves (at most every bakepack.CHECK_S)."""
        if not BAKE_FETCH or self.mapdata is None or not self.grading or self.net is None:
            return
        import bakepack
        now = time.monotonic()
        if bakepack._sync["busy"] or now - bakepack._sync["at"] < bakepack.CHECK_S:
            return
        la, lo, _ = anchor.frame.enu_to_geodetic(x, y, 0.0)
        sig = f"w{int(self.road_widths is not None)}s{int(self.sea is not None)}"
        pool = self.__dict__.setdefault("bake_pool", ThreadPoolExecutor(1, thread_name_prefix="bake-packs"))
        pool.submit(bakepack.sync, self.cache_dir, self.net, self.mapdata, float(la), float(lo), sig)

    def download_ahead(self, anchor, x, y, vx, vy, tree=None):
        """Fetch the elevation tiles (the blocks' zoom) and the road tiles' map cells along the player's straight
        path AHEAD_S s ahead into the disk caches (one round at a time, again every AHEAD_MOVE_M); with the car's
        time-to-reach tree (reach.Tree) along its most probable path and where its likely branches' data ends
        (ahead_points)."""
        v = math.hypot(vx, vy)
        if not AHEAD_ON or v < AHEAD_MIN_V or self.ahead_busy or self.dem is None:
            return
        last = self.ahead_last
        if last is not None and last[0] == anchor.id and math.hypot(x - last[1], y - last[2]) < AHEAD_MOVE_M:
            return
        self.ahead_last = (anchor.id, x, y)
        pts = []
        for px, py in ahead_points(x, y, vx, vy, tree):
            la, lo, _ = anchor.frame.enu_to_geodetic(px, py, 0.0)
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
                jobs = []
                for la, lo in pts if layers else ():
                    dlat = reach / 111320.0
                    dlon = reach / (111320.0 * max(0.05, math.cos(math.radians(la))))
                    for key in mapcells.cells_for_bbox(la - dlat, lo - dlon, la + dlat, lo + dlon):
                        todo = tuple(ly for ly in layers if not self.mapdata.has(key, ly))
                        if todo and all(key != j[0] for j in jobs):
                            jobs.append((key, todo))
                # (AHEAD_CELLS cells at once, nearest first: one after another, a slow connection's round fell behind
                # the car, 48 cells in a 20 km first visit while its road tiles waited for theirs, 2026-11-04)
                if jobs and AHEAD_BATCH:
                    # (their map tiles first, in batches: neighbours' in one range read; mapdata.tiles_prefetch)
                    try:
                        self.mapdata.tiles_prefetch([(k, ly) for k, todo in jobs for ly in todo], kind="prefetch")
                    except Exception as e:      # (the cells' own reads go on without it)
                        log.debug("download ahead: tiles batch: %s: %s", type(e).__name__, e)
                if jobs:
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(AHEAD_CELLS, thread_name_prefix="download-ahead-cell") as pool:
                        for _ in pool.map(lambda j: self.mapdata.cell(j[0], layers=j[1], kind="prefetch"), jobs):
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

    def warm_imagery(self, anchor, x, y, vx, vy, tree=None):
        """The lazy imagery sources (NAIP) along the player's straight path (WARM_STEPS_S), over the block's
        footprint and a margin, at the base photo's texel size: fetched before the blocks there are built. tree: the
        car's time-to-reach tree: along its most probable path instead."""
        if self.imagery is None:
            return
        reach = self.layout.block * heightmap.SQUARE / 2 + IMG_WARM_MARGIN
        pts = tree.points_at(WARM_STEPS_S) if tree is not None else [(x + vx * t, y + vy * t) for t in WARM_STEPS_S]
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

    def write_abandoned(self):
        """The abandoned anchors for the worker processes (blockworker.ABANDONED_FILE in out_dir: their running blocks
        and road tiles of those anchors give up at their next road-plan wait or stage)."""
        try:
            p = os.path.join(self.out_dir, blockworker.ABANDONED_FILE)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(int(a) for a in self.abandoned), f)
            os.replace(tmp, p)
        except OSError as e:
            log.debug("abandoned anchors file: %s", e)

    def is_abandoned(self, aid):
        """Did a teleport leave anchor aid (abandoned: its queued work is not wanted)?"""
        return aid in getattr(self, "abandoned", ())

    def left_behind(self, aid, x, y, margin=0.0):
        """Is (x, y) of anchor aid's frame left behind by every player on it (LEFT_BEHIND_M + margin away and behind
        each, or LEFT_FAR_M + margin away)? False when nobody is on aid (a teleport's own: is_abandoned) or a player's
        place is unknown."""
        on = [c for c in list(getattr(self, "connections", ())) if getattr(c, "active", None) == aid]
        if not on:
            return False
        for c in on:
            f, v = getattr(c, "focus", None), getattr(c, "vel", None) or (0.0, 0.0)
            if f is None or getattr(c, "view_at", None) is not None or getattr(c, "map_open", False) \
                    or getattr(c, "map_view", False):
                return False            # (the 3D map's view, or a camera away from the car: its work follows that)
            # (the other cars it streams round: collisionfoci.lua's foci)
            if any(math.hypot(x - fx, y - fy) < LEFT_BEHIND_M + margin + fr for fx, fy, fr, *_ in getattr(c, "foci", ())):
                return False
            dx, dy = x - f[0], y - f[1]
            d = math.hypot(dx, dy)
            if d > LEFT_FAR_M + margin:
                continue
            if d > LEFT_BEHIND_M + margin and math.hypot(*v) >= LEFT_MIN_V and dx * v[0] + dy * v[1] < 0:
                continue
            return False
        return True

    def geo_unwanted(self, aid, gx, gy):
        """Is road geotile (gx, gy), queued for anchor aid, wanted by no player on it any more (roads.GeoTracker.unwanted:
        off every fast car's time-to-reach tree and out of reach round it, for a while)? False when nobody is on aid."""
        on = [c for c in list(getattr(self, "connections", ())) if getattr(c, "active", None) == aid]
        if not on:
            return False
        for c in on:
            tr = getattr(c, "roads", None)
            if tr is None or not callable(getattr(tr, "unwanted", None)) or not tr.unwanted((gx, gy)):
                return False
        return True

    def block_unwanted(self, aid, cx, cy):
        """Is the prefetched block (aid, cx, cy) predicted by no player on aid any more (each one's prediction led by its
        time-to-reach tree, the block neither its own nor the one it asked for, and not among its predicted blocks for
        reach.UNWANTED_GRACE_S: the car took another way)? False when nobody is on aid."""
        on = [c for c in list(getattr(self, "connections", ())) if getattr(c, "active", None) == aid]
        if not on:
            return False
        now = time.monotonic()
        for c in on:
            if not getattr(c, "tree_led", False):
                return False
            if any(b is not None and (b.cx, b.cy) == (cx, cy) for b in (getattr(c, "block", None),
                                                                        getattr(c, "pending_block", None))):
                return False
            if now - getattr(c, "chain_wanted_at", {}).get((cx, cy), -1e9) <= reachmod.UNWANTED_GRACE_S:
                return False
        return True

    def geo_left_behind(self, aid, gx, gy):
        """left_behind of road geotile (gx, gy)'s middle in anchor aid's frame (half its size of margin)."""
        anchor = getattr(self, "anchors", {}).get(aid)
        frame = getattr(anchor, "frame", None)
        if frame is None:
            return False
        import geotiles
        la, lo = geotiles.centre(gx, gy)
        x, y, _ = frame.geodetic_to_enu(la, lo, 0.0)
        return Server.left_behind(self, aid, float(x), float(y), 150.0)

    def make_heightmap(self, anchor, cx, cy, progress, lane=LANE_REQUEST, queued_at=None):
        """Worker thread: build and write one 1024² terrain. The follow / prefetch blocks (no progress
        callback) build in the block process when there is one; the anchor's first block here, so its
        DEM download reports progress."""
        t0 = time.perf_counter()
        if queued_at is not None and t0 - queued_at > 0.5:
            log.info("terrain %s waited %.2f s for a heightmap worker", (anchor.id, cx, cy), t0 - queued_at)
        half = self.layout.block * heightmap.SQUARE / 2
        # (a teleport left its place while it waited: is_abandoned; or a prefetched block the car has passed: left_behind,
        # or one its time-to-reach tree no longer leads to: block_unwanted)
        gone = lambda: Server.is_abandoned(self, anchor.id) or (     # noqa: E731
            lane == LANE_PREFETCH and progress is None and (Server.left_behind(self, anchor.id, cx, cy, half)
                                                            or Server.block_unwanted(self, anchor.id, cx, cy)))
        if gone():
            raise poolsmod.Abandoned()
        q = self.anchor_q(anchor)
        fb, base = anchor.first_base, None
        if fb is not None and fb[0] == (cx, cy):        # the provisional first block's photo (make_provisional)
            anchor.first_base, base = None, fb[1]
        elif self.near_visual == "detail" and self.imagery is not None and self.fast \
                and (lane == LANE_PREFETCH or (progress is None and getattr(self, "speed", 0.0) >= PHOTO_SKIP_V)) \
                and (anchor.id, cx, cy) not in self.hm_committed:     # (a rebuild of the block in use keeps its photo)
            # (and faster than PHOTO_SKIP_V a requested one too: in use 2-3 s at 150 mph, its 3-7 s photo came after
            # the car had left it, 36 photos for 28 blocks of a 3 mile drive, two of the server's four CPUs;
            # base_refresh draws the block in use's, one at a time, and the stand-ins after it take its overlap)
            # a fast player's prefetched block: its photo only once the mod has it (base_refresh: a stale base); a
            # 3-8 s photo for every guessed block, half of them never asked for, took the CPU the road tiles needed
            # (500 mph, 2026-10-30)
            self.base_stale.add((anchor.id, cx, cy))
            self.base_skipped.add((anchor.id, cx, cy))
            if getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V:
                # (its stand-in beside the heights too: made after them it was 2-8 s more of every prefetched block,
                # which a request for it then waited out; nyc_speed.py 2026-11-02)
                self.quick_futs[(anchor.id, cx, cy)] = self.quick_early.submit(self._stand_in, anchor, cx, cy)
        elif self.near_visual == "detail" and self.imagery is not None:
            base = (self.urgent_base_pool if lane == LANE_REQUEST else self.base_pool).submit(
                self._base_photo, anchor, cx, cy, self._base_process(lane), lane == LANE_REQUEST)
            if lane == LANE_REQUEST and self.fast and getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V:
                # (its stand-in photo now, beside the heights, in the other lane's process: made after them it took
                # 1.3-5 s more of every block at 100-200 mph down Manhattan, nyc_speed.py 2026-11-02)
                self.quick_futs[(anchor.id, cx, cy)] = self.quick_early.submit(self._stand_in, anchor, cx, cy)
        pool = self.block_pools[lane][0] if self.block_pools and progress is None else None
        key = (anchor.id, cx, cy)
        try:
            if pool is not None:
                self.hm_stage[key] = "the block process"
                submitted, submitted_pc = time.time(), time.perf_counter()
                # (one block process for both lanes, a lean tier: a prefetched block waits for the requested ones)
                submit = pool.submit_low if lane == LANE_PREFETCH and self.block_shared() else pool.submit
                pfut = self.hm_proc[key] = submit(
                    blockworker.build, anchor.id, anchor.lat, anchor.lon, anchor.hm_zoom, q, cx, cy,
                    self.layout.block, self.out_dir, self.hm_name(key), self.ter_name(key),
                    fuel_pads=self.block_fuel_pads(anchor, cx, cy, self.layout.block * heightmap.SQUARE))
                try:
                    info = poolsmod.result_running(pfut, None, abandon=gone)
                finally:
                    self.hm_proc.pop(key, None)
                if "started" in info:   # (waiting for the lane's process: a block before it, its road plans ahead)
                    started, finished = info.pop("started"), info.pop("finished", None)
                    info["stages"] = {"pre": submitted_pc - t0, "queue": max(0.0, started - submitted),
                                      **({"work": finished - started, "back": max(0.0, time.time() - finished)}
                                         if finished else {}),
                                      **info.get("stages", {})}
                self.hm_stage[key] = "its base photo"
                return self._finish_heightmap(anchor, cx, cy, info, t0, base, urgent=lane == LANE_REQUEST, lane=lane)
            self.hm_stage[key] = "the build in the server's process"
            urgent = getattr(self.dem, "urgent", None)     # (test DEMs have no lane)
            with urgent() if urgent is not None else contextlib.nullcontext():     # the player waits for it (teleport,
                # resync): ahead of every other download
                return self._make_heightmap_here(anchor, cx, cy, progress, t0, q, base)
        finally:
            self.hm_stage.pop(key, None)

    def block_shared(self):
        """Do both block lanes share one process (a lean tier: main_async)?"""
        bp = self.block_pools
        return len(bp) > LANE_PREFETCH and bp[LANE_PREFETCH][0] is bp[LANE_REQUEST][0]

    def block_planned(self, anchor, cx, cy):
        """Are the road plans of every map cell under block (cx, cy) made (here, or on disk: this run's plan cache, the
        revisit cache)? Then its graded build is quick and a provisional block only doubles the work."""
        g = self.grader_for(anchor)
        if g is None or not hasattr(g, "store"):
            return True
        half = self.layout.block * heightmap.SQUARE / 2
        X, Y = np.meshgrid(cx + np.linspace(-half, half, 5), cy + np.linspace(-half, half, 5))
        la, lo, _ = anchor.frame.enu_to_geodetic(X.ravel(), Y.ravel(), 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        for pl, idx in g.store.planners_of(la, lo):
            for k in pl._keys(la[idx], lo[idx]):
                sh = getattr(pl, "shared", None)
                if not (pl.planned(k) or (sh is not None and sh.has(k))):
                    return False
        return True

    def note_base(self, key, name):
        """The base photo name last sent to the mod for block key (terrain messages, block_base): a provisional block
        built again (Connection.prov_refresh) is sent with it, not without one (the level's flat base)."""
        if not name:
            return
        self.base_shown[key] = name
        self.base_shown.move_to_end(key)
        while len(self.base_shown) > 64:
            self.base_shown.popitem(last=False)

    def remake_provisional(self, anchor, cx, cy):
        """A provisional block built again (Connection.prov_refresh: its road cells came after it): blockworker.build
        provisional in the photo process (the lane make_provisional uses), no photo of its own (the one shown stays);
        its info. Without block processes, here."""
        key = (anchor.id, cx, cy)
        t0 = time.perf_counter()
        pool = self.photo_pool or (self.block_pools[LANE_PREFETCH][0] if self.block_pools else None)
        if pool is not None:
            info = pool.submit(blockworker.build, anchor.id, anchor.lat, anchor.lon, anchor.hm_zoom,
                               self.anchor_q(anchor), cx, cy, self.layout.block, self.out_dir, self.hm_name(key),
                               self.ter_name(key), provisional=True).result()
            info.pop("started", None)
            info.pop("finished", None)
        else:
            info = self._make_heightmap_here(anchor, cx, cy, None, t0, self.anchor_q(anchor), None, provisional=True)
        info.pop("base", None)
        info["seconds"] = time.perf_counter() - t0
        info["zoom"] = anchor.hm_zoom
        log.info("provisional block %s built again with its roads in %.2f s (%s)", key, info["seconds"],
                 " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()))
        return info

    def make_provisional_follow(self, anchor, cx, cy):
        """A follow block's provisional stand-in (Connection.provisional_follow) in a worker process (blockworker.build
        provisional: the DEM's ground, no road plans, sea floor or tunnels, the cached roads painted), its stand-in
        photo started beside it (at most STANDIN_WAIT_S; else none, the photo later). Built here (make_provisional) it
        took 11-33 s of a first visit beside the rest of this process's work (nyc_speed.py --cold, 2026-11-02)."""
        pool = self.photo_pool or (self.block_pools[LANE_PREFETCH][0] if self.block_pools else None)
        if pool is None:
            return self.make_provisional(anchor, cx, cy, None)
        t0 = time.perf_counter()
        key = (anchor.id, cx, cy)
        early = (self.quick_early.submit(self._stand_in, anchor, cx, cy)
                 if self.near_visual == "detail" and self.imagery is not None else None)
        info = pool.submit(blockworker.build, anchor.id, anchor.lat, anchor.lon, anchor.hm_zoom, self.anchor_q(anchor),
                           cx, cy, self.layout.block, self.out_dir, self.hm_name(key), self.ter_name(key),
                           provisional=True).result()
        info.pop("started", None)
        info.pop("finished", None)
        if early is not None:
            try:
                info["base"] = early.result(timeout=STANDIN_WAIT_S)
            except Exception:
                info["base"] = None
            if not info.get("base"):
                self.quick_pool.submit(self._quick_later, anchor, cx, cy)
        info["seconds"] = time.perf_counter() - t0
        info["zoom"] = anchor.hm_zoom
        log.info("provisional block %s in %.2f s in a worker (%s)", key, info["seconds"],
                 " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()))
        return info

    def make_provisional(self, anchor, cx, cy, progress, ground_at=(0.0, 0.0)):
        """Worker thread: a teleport's first block before its road plans (the user, 2026-10-22: "place the car as
        soon as possible"): the DEM's ground with the runways (no road grading, tunnels, arches or sea floor, which
        all wait on the map data of the cells around: 15-60 s of a fresh place's first block, ~5 s without), the
        cached land cover and the photo. The graded block replaces it when it is built (Connection.refine_first:
        terrain_refresh). Not kept in hm (the graded one is the block of its key), nor in anchor.chunks. ground_at: the
        point its groundZ is for (the car's spot)."""
        if os.environ.get("BNGE_WORLD_BUNDLE"):
            from worldbundle import BundleError
            raise BundleError("Ungraded provisional terrain is disabled for pinned road plans")
        t0 = time.perf_counter()
        # (the anchor's height quantum, its survey's DEM reads at the urgent priority as the block's own: queued behind
        # the teleport's other downloads, the survey (+-15 km of DEM z10, 0.1-0.6 s alone cold) was 3.6 s of a fresh
        # Spokane's 5.2 s first block, 2026-11-11. A quantum of the block's own instead (block_params) made the far
        # tiles over it two steps off the graded block, test_integration_drive's l2 check)
        urgent = getattr(self.dem, "urgent", None)     # (test DEMs have no lane)
        with urgent() if urgent is not None else contextlib.nullcontext():
            q = self.anchor_q(anchor)
        base = None
        if self.near_visual == "detail" and self.imagery is not None:
            # (here, not in a block process: the other lane's process can still be busy with the old place's block,
            # and the photo waited 16 s behind it on a Deck, 2026-10-23)
            base = self.urgent_base_pool.submit(self._base_photo, anchor, cx, cy, None, True, True)
            anchor.first_base = ((cx, cy), base)
        pool = self.photo_pool or (self.block_pools[LANE_PREFETCH][0] if self.block_pools else None)
        if pool is not None and PROVISIONAL_ELSEWHERE:
            # in a block process: here it took 13-21 s at fresh dense places beside this interpreter's other work (its
            # roadPaint above all), 3.9 s for the same block in a fresh process (.claude/work/rust_port_plan.md 1a,
            # 2026-11-07); the follow blocks' stand-ins went there before (make_provisional_follow)
            key = (anchor.id, cx, cy)
            if base is not None and getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V:
                # (its stand-in photo too, made here beside the heights (the photo process builds them): the block goes
                # with it when its photo is not done PROVISIONAL_BASE_WAIT_S after the heights, the photo swapped in
                # when done; it waited for the photo, 2 of a 2.9 s first block in Lower Manhattan, 2026-11-11)
                self.quick_futs[key] = self.quick_early.submit(
                    self._quick_once, anchor, cx, cy, lambda: self.write_quick_base(anchor, cx, cy))
            ts = time.time()
            info = pool.submit(blockworker.build, anchor.id, anchor.lat, anchor.lon, anchor.hm_zoom, q, cx, cy,
                               self.layout.block, self.out_dir, self.hm_name(key), self.ter_name(key),
                               provisional=True, ground_at=tuple(ground_at)).result()
            started, finished = info.pop("started", None), info.pop("finished", None)
            if started is not None and finished is not None:    # (where a first block's seconds go)
                info.setdefault("stages", {}).update(queue=max(0.0, started - ts), work=max(0.0, finished - started),
                                                     back=max(0.0, time.time() - finished))
            info = self._finish_heightmap(anchor, cx, cy, info, t0, base, urgent=True,
                                          base_wait=PROVISIONAL_BASE_WAIT_S, ground=True)
            log.info("provisional block %s in %.2f s in a worker (%s)", key, time.perf_counter() - t0,
                     " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()))
            return info
        urgent = getattr(self.dem, "urgent", None)     # (test DEMs have no lane)
        with urgent() if urgent is not None else contextlib.nullcontext():
            info = self._make_heightmap_here(anchor, cx, cy, progress, t0, q, base, provisional=True,
                                             ground_at=ground_at, ground=True)
        log.info("provisional block %s in %.2f s (%s)", (anchor.id, cx, cy), time.perf_counter() - t0,
                 " ".join(f"{k} {v:.2f}" for k, v in info.get("stages", {}).items()))
        return info

    def _make_heightmap_here(self, anchor, cx, cy, progress, t0, q, base=None, provisional=False, ground_at=(0.0, 0.0),
                             ground=False):
        with self.hm_here_lock:
            self.hm_here += 1
        try:
            return self._make_heightmap_here_(anchor, cx, cy, progress, t0, q, base, provisional, ground_at, ground)
        finally:
            with self.hm_here_lock:
                self.hm_here -= 1

    def _make_heightmap_here_(self, anchor, cx, cy, progress, t0, q, base=None, provisional=False, ground_at=(0.0, 0.0),
                              ground=False):
        if provisional and os.environ.get("BNGE_WORLD_BUNDLE"):
            from worldbundle import BundleError
            raise BundleError("Ungraded provisional terrain is disabled for pinned road plans")
        keep = {}
        g = None if provisional else self.grader_for(anchor)
        c0 = time.thread_time()

        grade_s = [0.0, 0.0]            # wall, this thread's CPU (the rest: waiting for map data, the interpreter)
        part_s = {"dem": 0.0, "sea": 0.0, "tunnels": 0.0}  # (the rest of "heights": the stages' log, 2026-10-30)

        def elev(la, lo):
            t1 = time.perf_counter()
            z = self.dem.sample(la, lo, anchor.hm_zoom, progress)
            part_s["dem"] += time.perf_counter() - t1
            if provisional:             # the runways only (the sea floor waits on the water cells)
                return self.airports.apply(la, lo, z) if self.airports is not None else z
            t1 = time.perf_counter()
            z = self.ground(anchor.frame, la, lo, z)
            part_s["sea"] += time.perf_counter() - t1
            if g is None:
                return z
            t1, c1 = time.perf_counter(), time.thread_time()
            z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
            grade_s[0] += time.perf_counter() - t1
            grade_s[1] += time.thread_time() - c1
            return z
        gen = elev
        if not provisional:
            blockworker.plan_ahead(g, anchor.frame, cx, cy, self.layout.block * heightmap.SQUARE / 2)
        if self.mapdata is not None and not provisional:
            # (made here when not yet: an anchor's first block was built before any road tile made them, without
            # its arches' raise and flat mouths, 2026-10-17)
            t1 = time.perf_counter()
            tun = self.tunnels_of(anchor, elev, g)
            part_s["tunnels"] += time.perf_counter() - t1
            half = self.layout.block * heightmap.SQUARE / 2
            cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")  # noqa: E731

            def gen(la, lo):        # the ground over the low arches raised over their vaults (roads.arch_raise)
                return roadsmod.block_arch_raise(tun, cell_fn, anchor.frame, la, lo, elev(la, lo),
                                                 (cx - half, cy - half, cx + half, cy + half))
        l0_st = {}              # (a provisional block's L0 roads: its painted roads' corridors flattened, l0roads.py)
        info = heightmap.generate(anchor.frame, gen,
                                  os.path.join(self.out_dir, self.hm_name((anchor.id, cx, cy))), cx, cy, q=q,
                                  size=self.layout.block, keep=keep, chunks=None if provisional else anchor.chunks,
                                  ground_at=ground_at,
                                  post=l0roads.post_for(anchor.frame, self.surface, cx, cy, self.layout.block, l0_st)
                                  if provisional else None)
        stages = {"heights": time.perf_counter() - t0, "grading": grade_s[0], "gradingCpu": grade_s[1],
                  "heightsCpu": time.thread_time() - c0, **part_s, **l0_st}
        idx = None
        if self.surface is not None:
            # ground models per 2 m sample from the cached land cover and the slope (surface.py);
            # rows flipped as the heightmap's (row 0 = north)
            t1 = time.perf_counter()
            classes, complete = self.surface.block_classes(anchor.frame, cx, cy, keep["lat"], keep["lon"])
            stages["classes"] = time.perf_counter() - t1
            idx = surfacemod.classify(classes, keep["u"], heightmap.SQUARE, keep["lat"], keep["lon"])
            if self.sea is not None and not provisional:    # under water: the bed (no grass through the surface)
                idx[self.sea.depth(keep["lat"], keep["lon"]) > blockworker.WATER_BED_M] = surfacemod.MUD
            land = idx.copy()           # (the land cover before the paint: surface.fold_layers)
            road_feats, roads_complete = self.surface.roads(keep["lat"], keep["lon"])
            stages["landcover"] = time.perf_counter() - t1
            t1 = time.perf_counter()
            lots = (self.surface.lots(keep["lat"], keep["lon"]) if hasattr(self.surface, "lots") else None)
            if lots:                    # farmland under the carriageways (a field does not cover the asphalt)
                idx = surfacemod.paint_fields(idx, keep["lat"], keep["lon"], lots)
                idx = surfacemod.paint_grass(idx, keep["lat"], keep["lon"], lots)  # (PR #505's mapped lawns)
                idx = surfacemod.paint_golf(idx, keep["lat"], keep["lon"], lots)   # (golf over it: courses win)
            if road_feats:              # (provisional: the widths' guesses, not measured between the buildings)
                idx = surfacemod.paint_roads(idx, keep["lat"], keep["lon"], road_feats, heightmap.SQUARE,
                                             self.surface.urban, None if provisional else self.road_widths,
                                             blockworker._open_tunnels(g, keep), quick=provisional)
            idx = surfacemod.paint_lots(idx, keep["lat"], keep["lon"], lots,
                                        blockworker._us_square_looks(anchor.frame)) if lots else idx
            if hasattr(self.surface, "paint_ground_pads"):
                idx = self.surface.paint_ground_pads(idx, keep["lat"], keep["lon"])
            stages["roadPaint"] = time.perf_counter() - t1
            idx = surfacemod.fold_layers(idx, land, heightmap.SQUARE, stages)   # (fewer layers: a cheaper swap)
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
        return self._finish_heightmap(anchor, cx, cy, info, t0, base, urgent=True,       # (a teleport's, a first block:
                                      ground=ground)                                    # waited for)

    def tunnels_of(self, anchor, elev, g):
        """anchor.tunnels (roads.TunnelProfiles), made now if not yet (elev: the caller's ground; g: the anchor's
        Grader or None, taken before: grader_for takes grader_lock)."""
        with self.grader_lock:
            if anchor.tunnels is None:
                anchor.tunnels = self.chain_view(anchor, "tunnels", elev, g)
            return anchor.tunnels

    def tunnel_floor(self, anchor, e=0.0, n=0.0):
        """The floor of a tunnel tube at (e, n) of the anchor's frame (roads.tunnel_floor_at), or None: a teleport into a
        park arch set the car down on the soil over it, where it hung on the arch's top (2026-10-17). No tunnel in the
        map data within TUNNEL_NEAR_M: None at once (no tunnel profiles made)."""
        if self.mapdata is None:
            return None
        cell_fn = lambda key: self.mapdata.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731
        try:
            r = TUNNEL_NEAR_M
            la, lo, _ = anchor.frame.enu_to_geodetic(np.array([e - r, e + r]), np.array([n - r, n + r]), np.zeros(2))
            cells = [cell_fn(k) for k in mapcells.cells_for_bbox(float(la.min()), float(lo.min()),
                                                                float(la.max()), float(lo.max()))]
            if not any(c is not None and any(s is not None for s in roadsmod.tunnel_specs(c)) for c in cells):
                return None
            g = self.grader_for(anchor)

            def elev(la, lo):
                z = self.ground(anchor.frame, la, lo, self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
                return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
            self.tunnels_of(anchor, elev, g)
            box = (e - r, n - r, e + r, n + r)
            return roadsmod.tunnel_floor_at(anchor.tunnels.parts(cells, box), e, n)
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
                    t.water = self.sea.water if self.sea is not None else None
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
            import railways
            holes = roadsmod.block_tunnel_holes(
                anchor.tunnels, cell_fn, anchor.frame, keep, cx, cy, heightmap.SQUARE,
                rail_parts=((lambda box: railways.tunnel_hole_parts(anchor.frame, cell_fn, self.plan_store.elev, box))
                            if railways.TUNNELS_ON and getattr(self, "plan_store", None) is not None else None))
        except Exception:
            log.exception("tunnel holes of block (%s, %s)", cx, cy)
            return idx
        import railunderpass            # (the railway stations' underpasses: their passages and pits are open where the ground was)
        under = railunderpass.holes(self.mapdata, anchor.frame, keep, cx, cy, heightmap.SQUARE, elev)
        if under is not None:
            holes = under if holes is None else (holes | under)
        if holes is None or not holes.any():
            return idx
        idx = np.zeros(holes.shape, np.uint8) if idx is None else idx
        idx[holes] = heightmap.TER_HOLE
        info["tunnelHoles"] = int(holes.sum())
        return idx

    def _base_process(self, lane):
        """The block process that renders the base photo of a block built in lane (blockworker.base_photo): the
        other lane's, usually idle while this one builds the heights; None (rendered here) without block processes.
        With the imagery analyses on (they live in this process) it renders from this process's finished tiles on
        disk (_base_elsewhere), else it is rendered here."""
        if not self.block_pools or self.imagery is None:
            return None
        photo = getattr(self, "photo_pool", None)
        if photo is not None and (lane != LANE_REQUEST or not getattr(self, "fast", False)):
            # (fast: the photo process makes a requested block's stand-in first, _stand_in.) A prefetched block's photo
            # there too: in the request lane's process it held the next block the game asked for, 0.2-1.8 s each
            # (a cold Chicago drive at 100 mph: requested blocks queued 2-4 s, the edge warning on, 2026-11-05)
            return photo
        return self.block_pools[LANE_PREFETCH if lane == LANE_REQUEST else LANE_REQUEST][0]

    def _base_photo(self, anchor, cx, cy, pool=None, urgent=False, quick=False):
        """base_pool: the block's base photo (write_base; in the block process pool when given and its imagery
        tiles are on disk) while its heights are built: (file name or None, [rgb] or [], seconds). urgent: the
        player waits on this block (a teleport's first, or one the mod asked for): its missing photo tiles are
        fetched ahead of the far field's (Imagery.urgent, netfetch "urgent"). quick: write_base's."""
        t1 = time.perf_counter()
        photo = []
        plain = URGENT_ELSEWHERE and urgent       # (analyses on: its photo without them at once, then refreshed)
        # (a block in use built again (gas station pads, a refresh): its photo under a new name, as base_refresh's. The
        # engine watches the photo it shows: written over in place, it reloaded it and froze for good in its
        # TextureCooker (Billings and Fargo, 2026-11-11, both right after "built again with its gas station pads")
        aid = getattr(anchor, "id", None)
        rev = next(self.base_rev) if aid is not None and (aid, cx, cy) in getattr(self, "hm_committed", ()) else 0
        kw = {"rev": rev} if rev else {}
        name = self._base_elsewhere(anchor, cx, cy, pool, photo, plain, **kw) if pool is not None else None
        if name is None:
            with self.imagery.urgent() if urgent and hasattr(self.imagery, "urgent") else contextlib.nullcontext():
                name = self.write_base(anchor, cx, cy, photo=photo, quick=quick, **kw)
        return name, photo, time.perf_counter() - t1

    def _base_elsewhere(self, anchor, cx, cy, pool, photo, plain=False, rev=0):
        """write_base in a block process (blockworker.base_photo): the file name, or None when it lacked an imagery
        tile on disk or failed (then rendered here). With the imagery analyses on, from this process's finished tiles
        on disk (tex["fin"], Imagery.finished_only: as write_base here would render it); one not finished yet fails
        it, and then plain (a block the player waits for) renders it there without them, refreshed later
        (base_stale). Every block's photo was rendered twice before (there without them, here with them: 2 x 12 MB
        of PNG and 2 x 5.6 MB of the game's packed copy a block, 2026-11-04)."""
        im = self.imagery
        if self.deshadow is not None or self.colormatch is not None:
            name = self._base_in(anchor, cx, cy, pool, photo,
                                 {s.name: im._fin_sigs.setdefault(s.name, im._fin_sig(s)) for s in im.chain}, rev)
            if name is not None or not plain:
                return name
        return self._base_in(anchor, cx, cy, pool, photo, None, rev)

    def _base_in(self, anchor, cx, cy, pool, photo, fin, rev=0):
        size = self.layout.block * heightmap.SQUARE
        key = (anchor.id, cx, cy)
        name = self.base_name(key, rev)
        im = self.imagery
        tex = {"chain": [s.name for s in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
               "season_mode": im.season_mode}
        if fin:
            tex["fin"] = fin
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
        if not fin and (self.deshadow is not None or self.colormatch is not None):
            # (rendered without the imagery analyses, which live in this process: rendered again here with them,
            # base_refresh, as a photo rendered before they were done)
            self.base_stale.add(key)
        photo.append(rgb)
        self.base_worker += 1
        return name

    def _base_kept(self, anchor, cx, cy, pending):
        """A base photo was rendered with these (imagery.render pending) still to come: base_refresh renders it
        again once they are here."""
        key = (anchor.id, cx, cy)
        self.base_pending[key] = frozenset(pending)
        if surfacemod.PHOTO_ROADS_PENDING not in pending and self.base_ready(anchor, cx, cy):
            self.base_stale.discard(key)
        else:
            self.base_stale.add(key)
        area = getattr(self, "_roadsurf_area", None)     # (tests hand in a stand-in server)
        if area is not None:
            area(anchor, cx, cy)

    ROADSURF_RESEND_S = 60.0        # s before a cell is handed to roadsurf again (another photo of it: more tiles here)

    def _roadsurf_area(self, anchor, cx, cy):
        """A block's photo was rendered (its z17 tiles fetched, or some of them): the untagged minor roads of the cells
        under it judged from the tiles on the disk (roadsurf.py) in a road process, nothing waiting on it (their
        verdicts are used from the next start). A cell is handed over again after ROADSURF_RESEND_S with a later photo
        of it (the road process measures it once its tiles are there, roadsurf.measure_area)."""
        if not roadsurfmod.ON or self.road_pool is None or self.mapdata is None:
            return
        size = self.layout.block * heightmap.SQUARE
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([cx - size / 2, cx + size / 2]),
                                                 np.array([cy - size / 2, cy + size / 2]), np.zeros(2))
        lat0, lat1 = float(min(la)), float(max(la))
        lon0, lon1 = float(min(lo)), float(max(lo))
        try:
            cells = {str(mapcells.cell_of(a, b)) for a in (lat0, lat1) for b in (lon0, lon1)}
        except ValueError:
            return
        sent = self.__dict__.setdefault("_roadsurf_sent", {})
        now = time.monotonic()
        if all(now - sent.get(c, -1e9) < self.ROADSURF_RESEND_S for c in cells):
            return
        for c in cells:
            sent[c] = now
        try:
            self.road_pool.submit(blockworker.roadsurf_area, lat0, lon0, lat1, lon1)
        except Exception as e:      # (a refinement for the next start: never this one's failure)
            log.debug("road surfaces of the block %s: %s", (anchor.id, cx, cy), e)

    def _finish_heightmap(self, anchor, cx, cy, info, t0, base=None, urgent=False, lane=None, base_wait=None, ground=False):
        """_finish_heightmap_now; a block the game asked for (urgent) waiting for its photo: urgent work (urgency.py),
        the background jobs in this process wait to start meanwhile (its stand-in photo, 0.2 s of work, took 5 s and
        more beside the far tiles' and the vegetation's, the edge guard holding the car at 130-175 mph, 2026-10-31)."""
        if urgent and base is not None and not base.done():
            with urgencymod.urgent():
                return self._finish_heightmap_now(anchor, cx, cy, info, t0, base, lane, base_wait, ground)
        return self._finish_heightmap_now(anchor, cx, cy, info, t0, base, lane, base_wait, ground)

    def _finish_heightmap_now(self, anchor, cx, cy, info, t0, base=None, lane=None, base_wait=None, ground=False):
        """The base photo (detail mode; rendered here: the imagery corrections live in this process; base: its
        future from _base_photo, started with the heights), the timings. base_wait: s it waits for the photo at most
        (default by speed: BASE_FAST_WAIT_S / BASE_WAIT_S)."""
        if base is not None:
            t1 = time.perf_counter()
            if not base.done():
                # below FAST_BASE_V it waits for its own photo (BASE_WAIT_S at most): sent before it, each block's
                # ground swapped twice (stand-in, then the photo) and flashed at each (the user at 57-124 mph, 2026-10-30)
                concurrent.futures.wait([base], timeout=base_wait if base_wait is not None
                                        else BASE_FAST_WAIT_S if self.fast else BASE_WAIT_S)
            quick = None
            early = self.quick_futs.pop((anchor.id, cx, cy), None)
            if not base.done() and early is not None:
                try:
                    quick = early.result(timeout=STANDIN_WAIT_S)
                except concurrent.futures.TimeoutError:
                    quick = None        # (the block goes without it: _quick_later / the photo when done)
                except Exception as e:
                    log.info("stand-in photo %s: %s", (anchor.id, cx, cy), e)
                info["stages"]["baseQuick"] = time.perf_counter() - t1
            if not base.done() and not quick and early is None and getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V \
                    and self.imagery is not None:     # (an early one tried: not again here, _quick_later)
                quick = self._quick_once(anchor, cx, cy, lambda: (
                    self._quick_elsewhere(anchor, cx, cy, lane) if lane == LANE_REQUEST else None)
                    or self.write_quick_base(anchor, cx, cy))
                info["stages"]["baseQuick"] = time.perf_counter() - t1
            if not quick and ground and not base.done():
                # (a teleport's first block, no imagery stand-in (its imagery not on disk, or the early one late): the
                # stand-in from its own ground layers, l0roads.py; the car is placed now and the photo comes as
                # block_base)
                quick = self.write_ground_base(anchor, cx, cy, info)
                info["stages"]["baseGround"] = time.perf_counter() - t1
            if not base.done() and not quick and early is None and getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V \
                    and self.imagery is not None:
                if not quick and not self.fast:
                    # (no stand-in to show: slower, the photo is waited for, BASE_NO_STANDIN_MAX_S at most: without a
                    # limit an imagery host that kept failing held the block and the car at the edge for good,
                    # "paused until the server answers terrain requests", a player in Nova Scotia, 2026-11-03)
                    concurrent.futures.wait([base], timeout=BASE_NO_STANDIN_MAX_S)
            if not base.done():
                # the block goes now with a stand-in photo (below QUICK_SYNC_MAX_V made first: sent without one, the
                # ground the last blocks had already shown went to the flat base until it came, "it flashes already
                # painted ground", the user at 230 mph, 2026-10-30; faster, right after it) and its own when done
                info["base"] = quick
                if not quick:
                    self.quick_pool.submit(self._quick_later, anchor, cx, cy)
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
        elif (anchor.id, cx, cy) in self.base_skipped and self.near_visual == "detail" and self.imagery is not None:
            # a block built ahead (a fast player's prefetch, not waited on yet) gets its stand-in before it is ready:
            # made after it, the mod could take the block first and show the level's green base until it came (a
            # player at 170 mph: road, brown, green, road again; 2026-10-30). Its own photo: base_refresh. Faster than
            # QUICK_SYNC_MAX_V: after it (on its own thread): made before it, the prefetch fell behind at 500 mph (the car
            # held 40 s of 90 against none); at 170 mph no hold, roads 1.2 km ahead.
            early = self.quick_futs.pop((anchor.id, cx, cy), None)
            if early is not None:
                t1 = time.perf_counter()
                try:
                    info["base"] = early.result(timeout=STANDIN_WAIT_S)
                except concurrent.futures.TimeoutError:
                    self.quick_pool.submit(self._quick_later, anchor, cx, cy)
                except Exception as e:
                    log.info("stand-in photo %s: %s", (anchor.id, cx, cy), e)
                info.setdefault("stages", {})["standIn"] = time.perf_counter() - t1
            elif getattr(self, "speed", 0.0) < QUICK_SYNC_MAX_V:
                # (in the block's own process when it can: just done with its heights; here it took 2-6 s, 2026-10-31)
                info["base"] = self._quick_once(anchor, cx, cy, lambda: (
                    self._quick_elsewhere(anchor, cx, cy, lane) if lane is not None else None)
                    or self.write_quick_base(anchor, cx, cy))
            else:
                self.quick_pool.submit(self._quick_later, anchor, cx, cy)
        info["seconds"] = time.perf_counter() - t0
        info["zoom"] = anchor.hm_zoom
        return info

    def _photo_lawns(self, key, ter, layers, rgb):
        if key in self.hm_committed:            # (in use since it was asked for: its .ter is the engine's, _base_later)
            return
        try:
            surfacemod.photo_lawns(self.out_dir, ter, layers, rgb)
        except Exception as e:
            log.info("photo lawns %s: %s", key, e)

    def _quick_later(self, anchor, cx, cy):
        """quick_pool: a block sent without its photo gets its stand-in (write_quick_base) onto its entry and to the mod
        (block_base) unless its own photo came first. Off the block's thread: made there it held each block ~1.5 s,
        and at 500 mph the car waited 12.5 s a run for ground instead of none (2026-10-30)."""
        key = (anchor.id, cx, cy)
        info = self.hm.get(key)
        if info is not None and info.get("base"):
            return                              # (its own photo came while this waited its turn)
        name = self._quick_once(anchor, cx, cy, lambda: self._quick_elsewhere(anchor, cx, cy, LANE_PREFETCH)
                                or self.write_quick_base(anchor, cx, cy))
        conns = [c for c in list(self.connections) if getattr(c, "loop", None) is not None]
        if not name or not conns:
            return
        size = self.layout.block * heightmap.SQUARE

        def apply(left=8):
            info = self.hm.get(key)
            if info is None and left > 0:       # (the block's entry is stored just after it is built)
                conns[0].loop.call_later(0.25, apply, left - 1)
                return
            if info is None or info.get("base"):
                return                          # (gone, or its own photo is here already)
            info["base"] = name
            self.note_base(key, name)
            for c in list(self.connections):
                c.send({"type": "block_base", "anchorId": key[0], "cx": key[1], "cy": key[2],
                        "base": f"{self.vfs_dir}/{name}", "baseSize": size})
        try:
            conns[0].loop.call_soon_threadsafe(apply)
        except RuntimeError:
            pass                                # (the loop has closed)

    def _base_later(self, key, fut, tries=4):
        """A block sent without its photo (not done BASE_WAIT_S after its heights): the photo, once rendered, onto the block's entry (later
        terrain_ready answers) and to the mod (block_base: the block in use takes it now, a ready one at its commit)."""
        try:
            name, photo, _ = fut.result()
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
            self.note_base(key, name)
            if info is not None and (not info.get("base") or info["base"] == self.quick_base_name(key)):
                info["base"] = name
                if photo and info.get("layers") and info.get("ter") and key not in self.hm_committed:
                    # lawns only where the photo is green (_finish_heightmap), in time for a block not committed
                    # yet (the mod reads its .ter at the commit); off the event loop (7.6 s of a Steam Deck's loop in
                    # a drive). Never a block in use: the engine watches its .ter, reloaded it when patched here and
                    # froze in its TextureCooker (a pads refresh's block whose photo came a second after it, Billings
                    # 2026-11-11: "File changed [hm_2_0_384_5.ter]", the game hung)
                    self.quick_pool.submit(self._photo_lawns, key, info["ter"], info["layers"], photo[0])
            for c in list(self.connections):    # (the mod drops another anchor's)
                c.send({"type": "block_base", "anchorId": key[0], "cx": key[1], "cy": key[2],
                        "base": f"{self.vfs_dir}/{name}", "baseSize": size})
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:
            pass                                # (the loop has closed)

    def base_name(self, key, rev=0):
        ext = terraincache.PHOTO_EXT
        return self.hm_name(key)[:-4] + (f"_base{ext}" if not rev else f"_base_r{rev}{ext}")

    def quick_base_name(self, key):
        return self.hm_name(key)[:-4] + "_base_q" + terraincache.PHOTO_EXT

    def _stand_in(self, anchor, cx, cy):
        """A requested block's stand-in photo made beside its heights (fast): in the prefetch lane's process, else
        here; its file name or None."""
        return self._quick_once(anchor, cx, cy, lambda: self._quick_elsewhere(anchor, cx, cy, LANE_PREFETCH,
                                                                              self.photo_pool)
                                or self.write_quick_base(anchor, cx, cy))

    def _quick_once(self, anchor, cx, cy, make):
        """make() (a block's stand-in photo: its file name or None) once per block: a caller while it is being made
        waits for it (QUICK_SHARE_WAIT_S at most), one after takes the file made. The early stand-in, the provisional
        block's, the finished block's and _quick_later each made their own: up to 5 of one block, two at once, ~100
        stand-ins at 2-9 s for 26 blocks of a 6 min drive (nyc_speed.py --cold, 2026-11-02)."""
        key = (anchor.id, cx, cy)
        with self.quick_lock:
            name = self.quick_made.get(key)
            if name and os.path.exists(os.path.join(self.out_dir, name)):
                return name
            fut = self.quick_running.get(key)
            mine = fut is None
            if mine:
                fut = self.quick_running[key] = concurrent.futures.Future()
        if not mine:
            try:
                return fut.result(timeout=QUICK_SHARE_WAIT_S)
            except Exception:
                return None
        try:
            name = make()
        except BaseException as e:
            fut.set_exception(e)
            raise
        else:
            fut.set_result(name)
            return name
        finally:
            with self.quick_lock:
                self.quick_running.pop(key, None)
                if fut.done() and not fut.exception() and fut.result():
                    if len(self.quick_made) >= QUICK_MADE_KEPT:
                        self.quick_made.clear()
                    self.quick_made[key] = fut.result()

    def _quick_elsewhere(self, anchor, cx, cy, lane, pool=None):
        """write_quick_base in the block's own lane process (idle once its heights are built: blockworker.base_photo
        quick), at FAST_BASE_V and over or when no recent full photo overlaps it (those pasted in need this process;
        at speed every block overlaps the last and the stand-in here took 5-7 s): the file name, or None
        (then here). Its road paint, 0.1 s alone, took 3.7 s here at 185 mph beside the rest (2026-10-31)."""
        if not URGENT_ELSEWHERE or not self.block_pools or self.imagery is None:
            return None
        size = self.layout.block * heightmap.SQUARE
        with self.base_lock:
            recent = list(self.recent_full)
        if getattr(self, "speed", 0.0) < FAST_BASE_V and any(
                aid == anchor.id and abs(ocx - cx) < size and abs(ocy - cy) < size for aid, ocx, ocy, _ in recent):
            return None                 # (slower: the overlap pasted in here; faster, every block overlaps the last)
        key = (anchor.id, cx, cy)
        name = self.quick_base_name(key)
        im = self.imagery
        tex = {"chain": [s_.name for s_ in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
               "season_mode": im.season_mode}
        t0 = time.perf_counter()
        try:
            got = (pool or self.block_pools[lane][0]).submit(
                blockworker.base_photo, anchor.lat, anchor.lon, cx - size / 2 - BASE_SHIFT_M,
                cy - size / 2 - BASE_SHIFT_M, size, QUICK_BASE_PX, tex, False, anchor.zooms[0],
                os.path.join(self.out_dir, name), (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px),
                                                   BASE_AO), out_px=self.base_px, quick=True).result()
        except Exception as e:
            log.info("quick base photo %s in the block process: %s", key, e)
            return None
        if got is None:
            log.info("quick base photo %s: not in the block process (an imagery tile not on disk)", key)
            return None
        log.info("quick base photo %s in the block process in %.2f s", key, time.perf_counter() - t0)
        return name

    def write_ground_base(self, anchor, cx, cy, info):
        """L0 ground: a block's stand-in photo from its own ground layers (l0roads.ground_photo: the materials' colours,
        slope-shaded), packed for the game like the photo, under a new name each time (a file the game has read is
        never written again); the file name, or None. For a teleport's first block on a cold place: its imagery
        stand-in could not be made (not on disk), and the car waited up to BASE_NO_STANDIN_MAX_S for the photo (11-13 s
        teleports in Pittsburgh, Genoa, Woodland Park, 2026-11-11). Its own photo follows (block_base)."""
        key = (anchor.id, cx, cy)
        ter = info.get("ter")
        if not l0roads.GROUND_ON or not ter or self.near_visual != "detail":
            return None
        t0 = time.perf_counter()
        try:
            rgb = l0roads.ground_photo(os.path.join(self.out_dir, ter), info["q"], anchor.lat, anchor.lon,
                                       info.get("surfaceComplete") is not False, self.base_px, heightmap.SQUARE)
            name = f"{self.hm_name(key)[:-4]}_base_g{next(self.ter_serial)}{terraincache.PHOTO_EXT}"
            path = os.path.join(self.out_dir, name)
            tmp = staging.tmp_for(path)
            with open(tmp, "wb") as f:
                f.write(terraincache.stored_photo(rgb))
            staging.replace(tmp, path)
            args = (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px), rgb, BASE_AO)
            pool = self.block_pools[LANE_PREFETCH][0] if (URGENT_ELSEWHERE and self.block_pools and
                                                          not bc7.gpu_ok()) else None
            if pool is not None:        # (the CPU's BC7 encode on a block process's core, as write_quick_base)
                pool.submit(blockworker.base_entry, *args).result()
            else:
                terraincache.base_entry(*args)
        except Exception as e:          # (waited for the photo then, as before)
            log.info("ground stand-in %s: %s: %s", key, type(e).__name__, e)
            return None
        with self.base_lock:
            self.ground_bases.setdefault(key, []).append(name)
        log.info("ground stand-in photo %s in %.2f s (its imagery not on disk)", key, time.perf_counter() - t0)
        return name

    def write_quick_base(self, anchor, cx, cy):
        """A block's stand-in photo while its own is drawn: the aerial imagery alone at QUICK_BASE_PX (no roofs, painted
        roads or rock), packed for the game like the photo. A block sent without its photo showed the level's flat
        green base until the photo came (GitHub #548: at ~155 mph the outback flashed between red desert and green).
        The file name, or None."""
        size = self.layout.block * heightmap.SQUARE
        x0, y0 = cx - size / 2 - BASE_SHIFT_M, cy - size / 2 - BASE_SHIFT_M
        key = (anchor.id, cx, cy)
        try:
            # the last full photos' overlap first: the block moves on every 128 m, and ground already shown with its
            # photo turned to the stand-in (or the flat base) with each new block (the user, 2026-10-30: "it flashes
            # already painted/loaded terrain instead of new terrain")
            n = self.base_px
            sc = n / size
            tq = [("start", time.perf_counter())]
            with self.base_lock:
                recent = list(self.recent_full)
            parts = []                  # (the oldest first: the newer is pasted over it)
            have = np.zeros((n, n), bool)
            for aid, ocx, ocy, orgb in recent:
                if aid != anchor.id or orgb.shape[:2] != (n, n):
                    continue
                dx, dy = int(round((cx - ocx) * sc)), int(round((ocy - cy) * sc))
                i0, i1, j0, j1 = max(0, -dy), min(n, n - dy), max(0, -dx), min(n, n - dx)
                if i0 < i1 and j0 < j1:
                    parts.append((i0, i1, j0, j1, dy, dx, orgb))
                    have[i0:i1, j0:j1] = True
            if have.all():
                rgb = np.empty((n, n, 3), np.uint8)
            else:                       # the rest (the new ground at the block's edge): the imagery alone
                tq.append(("overlap", time.perf_counter()))
                with (self.deshadow.no_wait() if self.deshadow is not None else contextlib.nullcontext()):
                    small, used = self.imagery.render(anchor.frame, x0, y0, size, QUICK_BASE_PX, set())
                tq.append(("imagery", time.perf_counter()))
                covered = sum(v for k, v in (used or {}).items() if k != "colormatch")
                if covered < QUICK_MIN_COVER * QUICK_BASE_PX * QUICK_BASE_PX and have.mean() < 0.5:
                    # its imagery not here yet and little of it shown before: the renderer's dirt-brown fill over the
                    # block ("the entire ground now turns brown"); none then, as before
                    return None
                small = np.asarray(small, np.uint8)
                if roadsmod.ROADS_IN_GROUND:    # the carriageways painted in at its own size (cached roads only)
                    try:
                        small = surfacemod.paint_square(small, anchor.frame, x0, y0, size, self.surface,
                                                        self.road_widths, simple=True)
                    except Exception as e:
                        log.debug("quick base photo %s: roads: %s", key, e)
                tq.append(("roads", time.perf_counter()))
                # (at the photo's own size: the engine refuses another, "dont have required size of 4096-4096")
                rgb = np.array(surfacemod.enlarge(small, n), np.uint8)
                tq.append(("enlarge", time.perf_counter()))
            # plain slices, not masks: masked copies took 0.9 s of a 4096² stand-in's 1.4 s, and at 230 mph (a block
            # every 1.2 s) the stand-ins fell behind and the flat base showed between them (the user, 2026-10-30)
            for i0, i1, j0, j1, dy, dx, orgb in parts:
                rgb[i0:i1, j0:j1] = orgb[i0 + dy:i1 + dy, j0 + dx:j1 + dx]
            tq.append(("paste", time.perf_counter()))
            name = self.quick_base_name(key)
            path = os.path.join(self.out_dir, name)
            tmp = staging.tmp_for(path)
            with open(tmp, "wb") as f:
                f.write(terraincache.stored_photo(rgb))
            staging.replace(tmp, path)
            tq.append(("png", time.perf_counter()))
            args = (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px), rgb, BASE_AO)
            pool = self.block_pools[LANE_PREFETCH][0] if (URGENT_ELSEWHERE and self.block_pools and
                                                          not bc7.gpu_ok()) else None
            if pool is not None:        # (the CPU's BC7 encode on a block process's core: ~1 s here beside the rest)
                pool.submit(blockworker.base_entry, *args).result()
            else:
                terraincache.base_entry(*args)
            tq.append(("cache", time.perf_counter()))
            if tq[-1][1] - tq[0][1] > 1.0:
                log.info("quick base photo %s in %.2f s: %s (%d px)", key, tq[-1][1] - tq[0][1],
                         " ".join(f"{k} {b - a:.2f}" for (_, a), (k, b) in zip(tq, tq[1:])), n)
            return name
        except Exception as e:      # the flat base then, as before
            log.info("quick base photo %s: %s", key, e)
            return None

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
                rgb = surfacemod.paint_square(rgb, anchor.frame, x0, y0, size, self.surface, self.road_widths,
                                              pending=pending)
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
        with self.base_lock:    # (the stand-ins of the blocks after it take its overlap: write_quick_base)
            self.recent_full.append((anchor.id, cx, cy, rgb))
        t1 = time.perf_counter()
        tmp = staging.tmp_for(path)
        # (terraincache.stored_photo: the game reads the packed cache entry, this file only if that is stale)
        with open(tmp, "wb") as f:
            f.write(terraincache.stored_photo(rgb))
        staging.replace(tmp, path)
        t2 = time.perf_counter()
        try:            # after the photo: the cache entry must be the newer file
            args = (self.user_dir, f"{self.vfs_dir}/{name}", base_ao_vfs(self.base_px), rgb, BASE_AO)
            if pool is not None and not bc7.gpu_ok():   # the CPU's BC7 encode on the block process's core
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
        x0, y0 = cx - size / 2 - BASE_SHIFT_M, cy - size / 2 - BASE_SHIFT_M
        if not self.imagery.lazy_ready(anchor.frame, x0, y0, size, BASE_RENDER_PX):
            return False
        pending = self.base_pending.get((anchor.id, cx, cy), ())
        road_data = surfacemod.PHOTO_ROADS_PENDING
        if road_data in pending and self.mapdata is not None:
            la, lo = surfacemod._square_pts(anchor.frame, x0, y0, size)
            if not all(self.mapdata.has(k, "roads") for k in mapcells.cells_for_points(la, lo)):
                return False
        return all(rk == road_data or self._region_status(rk) is not None for rk in pending)

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
            pool = self.hm_req_pool if lane == LANE_REQUEST and progress is None and self.block_pools else self.hm_pool
            fut = self.hm_inflight[key] = loop.run_in_executor(
                pool, self.make_heightmap, anchor, cx, cy, progress, lane, time.perf_counter())
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
            # (a block without its roads is painted again once they are here, not once the land cover is too: a cell
            # whose land cover was slow or never came kept the roads of the block grass and dirt under their markings
            # for good, Discord 2026-11-09, the Tomei at Kaisei; the land cover then repaints it again when it comes)
            self.roads_need[key] = "roads" if info.get("roadsComplete") is False else "landcover"
        else:
            self.roads_rebuild.discard(key)
            self.roads_need.pop(key, None)
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
            self.base_stale.discard(key)
            self.base_pending.pop(key, None)
            # its photos (the base, a refreshed one, the stand-in) with their packed copies in the game's terrain
            # texture cache
            for name in {self.base_name(key), info.get("base") or self.base_name(key), self.quick_base_name(key),
                         *info.get("old_bases", ())}:
                self._remove_base(name)
            if info.get("ter"):
                self._remove(os.path.join(self.out_dir, info["ter"]))
            if self.surface is not None:        # its surface masks (only read at import)
                for name in surfacemod.mask_names(self.hm_name(key)[:-4]):
                    self._remove(os.path.join(self.out_dir, name))

    # --- far tiles -------------------------------------------------------
    def tile_name(self, aid, key):
        return f"far_{aid}_{key}.{self.far_format}"

    def fin_worker_worth(self):
        """With the imagery analyses on: try this far tile's texture in its far-tile process (from the finished tiles
        on disk)? While most recent tries found them (a place seen before) yes; on a first visit (none finished yet:
        the try fails and the texture is rendered here after the geometry, not beside it) only every
        FIN_WORKER_PROBE-th tile, to notice when they are there."""
        if not FIN_WORKER:
            return False
        h = self.fin_worker_hits
        self.fin_worker_tries += 1
        if len(h) < FIN_WORKER_MIN_TRIES:
            return True
        return sum(h) >= FIN_WORKER_SHARE * len(h) or self.fin_worker_tries % FIN_WORKER_PROBE == 0

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
        disk = getattr(self, "disk_cache", None)
        if disk is not None and disk.managed and disk.disk_short() > 0:
            # (a full drive fails far tiles, blocks and caches with "No space left on device": the first week's most
            # frequent errors; the cache is cut back to its reserve, but other files may fill the drive)
            parts.append("your drive is nearly full: free some space (the download cache is cut back)")
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
        levels of the mode get tex_near, the others tex_far; horizon mode: farfield.HORIZON_TEX from L8). With the 3D
        map open, at least MAP_TEX_PX (the user, 2026-11-07: "i want the ground to look high res at every zoom level":
        256 px tiles were 4-8 m texels under ~2.4 m screen pixels from 3 mi up)."""
        if self.horizon and level in farfield.HORIZON_TEX:
            px = farfield.HORIZON_TEX[level]
        else:
            px = self.layout.tex_size(level)
        if level <= MAP_TEX_MAX_LEVEL and any(getattr(c, "map_view", False) or getattr(c, "map_open", False)
                                              for c in self.connections):
            px = max(px, MAP_TEX_PX)
        return px

    def tile_tex_px(self, anchor, tile):
        """A far tile's texture size: tex_size(level), but with the 3D map open as fine as the tile is on screen (the
        user, 2026-11-07, "photo detail matched to the screen": the photo zoom goes by it, each halving 4x fewer photo
        tiles): one off the screen (behind the camera, outside its view cone) MAP_TEX_OFF_PX, else at most
        MAP_TEX_OVERSAMPLE x its edge in screen pixels (at MAP_SCREEN_H px for the field of view), never under
        MAP_TEX_MIN_PX. Retextured as the camera turns (a texture's size may change, its UVs go by its own)."""
        orig = tile
        if isinstance(tile, farfield.Piece):
            tile = tile.base                # (a piece takes its node's texture: the same size)
        px = self.tex_size(tile.level)
        if isinstance(tile, farfield.GTile) and tile.key in self.coarse_keys:
            # (a teleport's coarse cover: a few photos, sharpened later)
            px = min(px, FIRST_RAMP_NEAR_PX if tile.level <= farfield.GLOBAL_MIN_LEVEL else FIRST_RAMP_TEX_PX)
        # (the far PVS, farpvs.py: behind terrain from everywhere the player can be soon, a few photos until it is not;
        # in sight, what the screen resolves of it from there)
        px = self.pvs_px(anchor, orig, px)
        conn = None
        for c in self.connections:
            if c.active != anchor.id or not isinstance(getattr(c, "cam", None), dict):
                continue
            if (getattr(c, "map_view", False) or getattr(c, "map_open", False)) and c.cam.get("tx") is not None:
                conn = c
                break
            # (flying high too, a plane or a free camera: the same rule; driving keeps every texture its level's, the
            # camera turns round there in a moment)
            if c.cam.get("fz") is not None and c.eye_clearance() > FLY_TEX_EYE_M:
                conn = c
                break
        if conn is None:
            return px
        cam = conn.cam
        try:
            x, y, z = float(cam["x"]), float(cam["y"]), float(cam["z"])
            fx, fy, fz = float(cam["fx"]), float(cam["fy"]), float(cam["fz"])
            gz = float(cam["tz"]) if cam.get("tz") is not None else z - conn.eye_clearance()
            fov = float(cam.get("fovDeg") or 60.0)
        except (KeyError, TypeError, ValueError):
            return px
        x0, y0, x1, y1 = tile.bbox
        s = max(x1 - x0, y1 - y0)
        dx, dy, dz = (x0 + x1) / 2 - x, (y0 + y1) / 2 - y, gz - z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < s:
            return px                       # (under the camera)
        n = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
        tv = math.tan(math.radians(fov) / 2)
        half = math.atan(math.hypot(tv, tv * farfield.ASPECT))
        slack = math.asin(min(1.0, s * 0.7071 / d))
        ang = math.acos(max(-1.0, min(1.0, (dx * fx + dy * fy + dz * fz) / (d * n))))
        if ang - slack > half:
            return min(px, MAP_TEX_OFF_PX)
        on_screen = s * (MAP_SCREEN_H / (2 * tv)) / d
        want = 2 ** math.ceil(math.log2(max(1.0, on_screen * MAP_TEX_OVERSAMPLE)))
        return int(max(MAP_TEX_MIN_PX, min(px, want)))

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
        if aid not in self.anchors and aid not in self.anchors_preparing:
            return                          # pruned meanwhile
        have = self.horizon_grids.get(aid)
        if have is None or have.half < grid.half:
            self.horizon_grids[aid] = grid
            self.horizon_failed.pop(aid, None)
            for c in list(self.connections):
                if c.active == aid:
                    c.update_far()
                elif getattr(self.anchors_preparing.get(aid), "prep_req", (None,))[0] == id(c):
                    c.prebuild_coarse(self.anchors_preparing[aid])  # (its first block still building: to the horizon)

    # --- the far field's potentially visible set (farpvs.py) -----------------------------------------------------------
    def pvs_field(self, anchor, radius):
        """The anchor's PVS rings and viewpoints (farpvs.Field) when built and reaching `radius`, else None; starts a
        build (its worker: the coarse DEM's rings) when there is none, or the one there is too small."""
        if not self.pvs_on or self.dem is None:
            return None
        have = self.pvs_fields.get(anchor.id)
        if have is not None and have.rings.radius >= radius:
            return have
        busy = self.pvs_building.get(anchor.id)
        failed = self.pvs_failed.get(anchor.id)
        if (busy is None or busy[1] < radius) and (failed is None or time.monotonic() - failed > HORIZON_RETRY):
            # (with room: the radius follows the eye's height in steps, the rings are not made again for each)
            r = max(radius * farpvsmod.RADIUS_ROOM, have.rings.radius * 1.25 if have is not None else 0.0)

            def build():
                t0 = time.perf_counter()
                rings = farpvsmod.Rings(anchor.frame, self.dem, r)
                self.pvs_stats["rings_s"] += time.perf_counter() - t0
                log.info("anchor %d far PVS rings: radius %.0f km in %.2f s", anchor.id, r / 1000,
                         time.perf_counter() - t0)
                return farpvsmod.Field(anchor.id, anchor.frame, rings)
            fut = asyncio.get_running_loop().run_in_executor(self.pvs_pool, build)
            self.pvs_building[anchor.id] = (fut, r)
            fut.add_done_callback(lambda f, aid=anchor.id: self._pvs_field_done(aid, f))
        return have

    def _pvs_field_done(self, aid, fut):
        cur = self.pvs_building.get(aid)
        if cur is not None and cur[0] is fut:
            del self.pvs_building[aid]
        if fut.cancelled():
            return
        if fut.exception() is not None:
            self.pvs_failed[aid] = time.monotonic()
            log.warning("anchor %d: far PVS rings failed (%r)", aid, fut.exception())
            return
        if aid not in self.anchors and aid not in self.anchors_preparing:
            return                          # pruned meanwhile
        field = fut.result()
        have = self.pvs_fields.get(aid)
        if have is None or have.rings.radius < field.rings.radius:
            self.pvs_fields[aid] = field
            self.pvs_failed.pop(aid, None)
            for c in list(self.connections):
                if c.active == aid:
                    c.pvs_tick()

    def pvs_px(self, anchor, tile, full):
        """The texture px far tile `tile` (shown under `anchor`) needs for the connections that want it (each one's far
        PVS, farpvs.Set.tex_px: hidden behind terrain, or what its screen resolves of the tile from where its player can
        be soon; the most any of them needs), `full` at most; `full` when one of them has no set. A piece goes by its
        node."""
        if not self.pvs_on or not self.pvs_fields:
            return full
        key = tile.key
        glob = isinstance(tile, farfield.GTile)
        best = None
        up = self.up_px(anchor, tile, full)
        for c in self.connections:
            if c.active is None or (not glob and c.active != anchor.id):
                continue
            t = c.desired.get(key)
            if t is None:
                continue                    # (not this connection's: no say)
            s = getattr(c, "pvs", None)
            if s is None or s.aid != c.active:
                return full
            node = t.base if isinstance(t, farfield.Piece) else t
            px = s.tex_px(key, node.bbox, t.level, node.size, full, up)
            best = px if best is None else max(best, px)
            if best >= up:
                return up
        return full if best is None else best

    def up_px(self, anchor, tile, full):
        """The most texture px far tile `tile` may get where the screen resolves more of it than its level's `full`
        (farpvs.UP_ON, pvs_px): farpvs.UP_PX_MAX at most, and texels no finer than the sharpest photos at its middle
        (Imagery.sharp_zoom: USGS's z16 in the US, EOX's z14 elsewhere: larger would only enlarge them)."""
        if not farpvsmod.UP_ON or self.imagery is None or getattr(anchor, "frame", None) is None:
            return full
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        x0, y0, x1, y1 = node.bbox
        try:
            lat, lon, _ = anchor.frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
            reach = imagerymod.zoom_for(float(lat), node.size / farpvsmod.UP_PX_MAX)
            texel = imagerymod.ground_res(float(lat), self.imagery.sharp_zoom(float(lat), float(lon), reach))
        except Exception as e:                  # (no imagery chain to ask: the level's own)
            log.debug("far texture cap %s: %s", tile.key, e)
            return full
        px = full
        while px * 2 <= farpvsmod.UP_PX_MAX and node.size / (px * 2) >= texel * 0.9:   # (a z14 tile at 1024 px
            # over USGS's z16 is 1.885 m against 1.88: the same photos, not enlarged)
            px *= 2
        return px

    def pvs_hidden(self, anchor, tile):
        """Is far tile `tile` (shown under `anchor`) hidden behind terrain for every connection that wants it (each
        one's PVS, farpvs.Set, valid and hiding it; at least one)? A piece goes by its node."""
        if not self.pvs_on or not self.pvs_fields:
            return False
        key = tile.key
        glob = isinstance(tile, farfield.GTile)
        judged = False
        for c in self.connections:
            if c.active is None or (not glob and c.active != anchor.id):
                continue
            t = c.desired.get(key)
            if t is None:
                continue                    # (not this connection's: no say)
            s = getattr(c, "pvs", None)
            if s is None or s.aid != c.active:
                return False
            node = t.base if isinstance(t, farfield.Piece) else t
            if not s.hidden(key, node.bbox, t.level):
                return False
            judged = True
        return judged

    def prefetch_imagery(self, anchor, tile, soon=True):
        """Start downloading a queued tile's imagery (fire and forget), so tile workers rarely
        wait on the network. Only the first source of the chain; errors surface at build time. soon: the next tiles
        (a host that takes no prefetch now still gets them, as demand); else spare request tokens only."""
        if self.imagery is not None:
            frame, x0, y0, size = self.tile_where(anchor, tile)
            px = self.tile_tex_px(anchor, tile)
            self.imagery.prefetch(frame, *imagerymod.gutter_square(x0, y0, size, px), px, soon=soon, gutter=True)

    # --- BeamMP shared world generation v2 (mpshare.py, .claude/work/mp_share_v2.md) --------------------------------
    def share_parts(self):
        """What the shared items' keys are made of (plans: their format, the signature's widths / sea; cell facts: theirs;
        the build switches; the map data's versions): equal for two apps, their items have the same keys."""
        md = self.mapdata.versions() if self.mapdata is not None and hasattr(self.mapdata, "versions") else None
        return {"plans": plancachemod.plans_format(), "facts": roadsmod.FACTS_FORMAT,
                "sw": revisitmod.switches_key(), "md": md, "widths": self.road_widths is not None,
                "sea": self.sea is not None}

    def share_store(self):
        """A PlanCache of this server's plans signature and map data (the hub's has / _kept / _revisit_key / expect: the
        road processes' own, plancache.for_grader)."""
        sig = f"w{int(self.road_widths is not None)}s{int(self.sea is not None)}"
        md = self.mapdata.versions() if self.mapdata is not None and hasattr(self.mapdata, "versions") else None
        pc = plancachemod.PlanCache(os.path.join(self.cache_dir, "plans",
                                                 os.environ.get(plancachemod.RUN_ENV) or "share"), sig)
        pc.persist = (self.cache_dir, (plancachemod.FRAMES, sig, md))
        return pc

    def addons(self):
        """The Add-ons page's service (addons.py: the registry, GitHub's pages for it, the installed add-ons), made at
        its first use."""
        if getattr(self, "_addons", None) is None:
            import addons as addonsmod
            feed = getattr(self, "feed", None)
            try:
                data_dir = _app_modules()[0].data_dir()
            except Exception:           # (no apppaths: beside the cache)
                data_dir = None
            self._addons = addonsmod.Addons(self.user_dir, self.cache_dir, self.net,
                                            version=updatesmod.local_version() or "", data_dir=data_dir,
                                            feed_url=f"http://127.0.0.1:{feed.port}" if feed is not None and
                                            getattr(feed, "port", None) else "",
                                            game_dir=os.environ.get("BNGEARTH_GAME_DIR") or None)
        return self._addons

    def share_hub(self, send):
        """The BeamMP sharing hub (one per server: the session's mod connection sends through it; a new connection takes
        it over). -> (hub, created now)."""
        import mpshare
        hub = getattr(self, "_share_hub", None)
        if hub is None:
            hub = self._share_hub = mpshare.Hub(send, self.cache_dir, self.share_store)
            log.info("share: hub up (BeamMP session: plans shared through the server)")
            return hub, True
        hub.send_mod = send
        return hub, False

    # (shared world building v1, commented out 2026-10-27 (off since 2026-10-23); replaced by share_hub above)
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
            f = geotilesmod.frame(tile.x, tile.y, tile.z)
            f.merc = (tile.z, tile.x, tile.y)   # (imagery: its exact Mercator square for a gutter's fetches, Imagery.square)
            return f, -tile.size / 2, -tile.size / 2, tile.size
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        x0, y0, _, _ = node.bbox
        return anchor.frame, x0, y0, node.size

    def render_tile(self, anchor, tile, px, pending):
        """(rgb, used): a far tile's photo with its gutter (a global tile's: its Mercator square, render_merc)."""
        if isinstance(tile, farfield.GTile):
            return self.imagery.render_merc(tile.z, tile.x, tile.y, px, pending=pending)
        frame, x0, y0, size = self.tile_where(anchor, tile)
        return self.imagery.render(frame, *imagerymod.gutter_square(x0, y0, size, px), px, pending=pending, gutter=True)

    def make_tile(self, anchor, tile, prev=None):
        urgencymod.wait_turn()          # (a block the game waits for first: urgency.py)
        with self.far_gate:
            return self._make_tile(anchor, tile, prev)

    def map_on(self):
        """Some connection has the 3D map open (its view frame or near the car)."""
        return any(getattr(c, "map_view", False) or getattr(c, "map_open", False) for c in self.connections)

    def worker_retex(self):
        """Re-render textures in the far-tile processes (raw photos): the 3D map open, or a camera flying high (the
        textures there upgraded as the view turns: tile_tex_px)."""
        return self.map_on() or any(c.eye_clearance() > FLY_TEX_EYE_M for c in self.connections
                                    if hasattr(c, "eye_clearance"))

    def far_limit(self):
        """Far tiles built at once (far_gate): far_drive while driving (the game needs its cores), every worker with
        the 3D map open (the game paused) or while the loading boost is on (the player waits)."""
        if any(getattr(c, "map_view", False) or getattr(c, "map_open", False) for c in self.connections) \
                or getattr(self, "boost_on", False):
            return self.far_max
        return self.far_drive

    def coarse_imagery(self):
        """The coarse cover's photos (FIRST_RAMP_CHAIN alone: one host, the whole world, raw), or None (not in this
        server's chain). Shares the imagery cache's files with the server's Imagery."""
        im = getattr(self, "_coarse_im", None)
        if im is None:
            have = {s_.name for s_ in self.imagery.chain}
            names = [n for n in FIRST_RAMP_CHAIN if n in have]
            if not names:
                self._coarse_im = False
                return None
            im = imagerymod.Imagery(os.path.join(self.cache_dir, "imagery"), names, tone=self.tone, fetcher=self.net,
                                    leaf_on=self.imagery.leaf_on, season_mode=self.imagery.season_mode)
            # (its own registration replaced the server's failover chain: put that back, the offline test reads it)
            self.net.register_chain("imagery", [s_.url for s_ in self.imagery.chain])
            self._coarse_im = im
        return im or None

    def coarse_texture(self, anchor, tile, ck, zoom, gref, px):
        """Worker thread: a coarse cover's global tile (FIRST_RAMP) with its texture from the coarse imagery alone
        (coarse_imagery: its few satellite tiles fetched here while the far-tile process builds the geometry, then
        rendered there, raw): (Built, (texture file, bytes, sources used)), or None (the usual way then). The
        server's whole chain (aerial photos first: their coverage probes, a de-shadow region's analysis in this
        interpreter, a host deferring at its rate) held the coarse step 10-15 s at a cold Elko (2026-11-11); the
        full chain's texture comes with the full selection (TileInfo.coarse: stale there)."""
        im = self.coarse_imagery()
        if im is None:
            return None
        texture = self.texture_name(*ck, self.tex_rev.get(ck, 0))
        frame_q, x0q, y0q, size_q = self.tile_where(anchor, tile)
        sq = imagerymod.gutter_square(x0q, y0q, size_q, px)
        tex = {"path": os.path.join(self.out_dir, texture), "px": px, "mask": None,
               "chain": [s_.name for s_ in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
               "season_mode": im.season_mode, "quick": False}
        args = (blockworker.far_global_tile, tile.z, tile.x, tile.y, zoom, gref, self.layout, tex)
        fut = self.far_pool.submit(*args)           # (its geometry meanwhile: kept, a second call reads it back)
        try:
            ok = im.fetch_needed(frame_q, *sq, px, gutter=True)
        except Exception as e:      # (the geometry's call still ends; the usual way after it)
            log.debug("coarse texture %s: %s: %s", tile.key, type(e).__name__, e)
            ok = False
        built, got = fut.result()
        if got is None and ok:
            built, got = self.far_pool.submit(*args).result()
        if got is None:
            return None
        return built, (texture, got[0], got[1])

    def _make_tile(self, anchor, tile, prev=None):
        """Worker thread: build and write one far tile (and its texture). Returns its TileInfo.
        prev: the tile's stale TileInfo (--colormatch): only its texture is rendered again.
        A global tile (farfield.GTile) is built in its own frame, the same for every anchor (ck (GLOBAL_AID, key));
        a piece (farfield.Piece) is its node's tile cut to its global tile's quad."""
        # (the 3D map open: quick textures, what is on disk at once and the slow hosts' photos later: Imagery.quick)
        quick = self.imagery is not None and any(getattr(c, "map_view", False) or getattr(c, "map_open", False)
                                                 for c in self.connections)
        later = []
        if prev is not None:
            return self.retexture(anchor, tile, prev)
        t0 = time.perf_counter()
        lvl = tile.level
        px_t = self.tile_tex_px(anchor, tile)       # (the 3D map: as fine as it is on screen)
        full_px = self.tex_size(lvl)
        if self.pvs_fields and px_t < full_px and tile.key not in self.coarse_keys and not self.map_on():
            # (a smaller texture by the far PVS, farpvs.py: behind terrain, or all the screen resolves of it)
            self.pvs_stats["hidden" if self.pvs_hidden(anchor, tile) else "screen"] += 1
            self.pvs_stats["texels_saved_M"] += (full_px * full_px - px_t * px_t) / 1e6
        is_global = isinstance(tile, farfield.GTile)
        node = tile.base if isinstance(tile, farfield.Piece) else tile
        ck = self.far_ck(anchor.id, tile.key)
        path = os.path.join(self.out_dir, self.tile_name(*ck))
        done = None             # (texture file, bytes, sources used) when the worker rendered it (blockworker.far_tile)
        geo = None              # the geometry's future while this thread renders the texture
        coarse = None           # (Built, done) of a coarse cover's tile (coarse_texture)
        if is_global:
            zoom = farfield.global_dem_zoom(tile.z, self.layout.cells)
            gref = farfield.global_dem_zoom(tile.z + 1, self.layout.cells)
            if self.far_pool is not None and self.imagery is not None and tile.key in self.coarse_keys and \
                    not quick:
                coarse = self.coarse_texture(anchor, tile, ck, zoom, gref, px_t)
            if coarse is not None:
                built, done = coarse
            elif self.far_pool is not None and self.imagery is not None:
                # the 3D map: the quick texture in the far-tile process too (raw, what is on disk; the rest downloads
                # meanwhile and re-renders it): the global tiles are most of a map view (2026-11-07, Spokane: 26 s each
                # here, waiting on their photos one interpreter at a time)
                im = self.imagery
                px = px_t
                texture = self.texture_name(*ck, self.tex_rev.get(ck, 0))
                frame_q, x0q, y0q, size_q = self.tile_where(anchor, tile)
                later = im.fetch_later(frame_q, *imagerymod.gutter_square(x0q, y0q, size_q, px), px,
                                       gutter=True) if quick else []
                if not later and (self.deshadow is not None or self.colormatch is not None):
                    later = [im.done_token()]
                tex = {"path": os.path.join(self.out_dir, texture), "px": px, "mask": None,
                       "chain": [s_.name for s_ in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
                       "season_mode": im.season_mode, "quick": quick and bool(later)}
                args = (blockworker.far_global_tile, tile.z, tile.x, tile.y, zoom, gref, self.layout, tex)
                built, got = self.far_pool.submit(*args).result()
                if got is None and im.fetch_needed(frame_q, *imagerymod.gutter_square(x0q, y0q, size_q, px), px,
                                                   last_only=bool(tex["quick"]), gutter=True):
                    built, got = self.far_pool.submit(*args).result()
                if got is not None:
                    done = (texture, got[0], got[1])
                else:
                    later = []
            elif self.far_pool is not None:
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
            g = self.grader_for(anchor) if self.far_pool is None else None   # graded like the block (levels <= 0)
            base = self.airports.wrap(self.dem) if self.airports is not None else self.dem
            if self.sea is not None:
                base = seacarvemod.SeaDem(base, self.sea if lvl <= seacarvemod.CARVE_MAX_LEVEL else None)
            # (past them: the bridges the elevation holds under decks taken out where already found, DeckGroundDem)
            src = (gradingmod.GradedDem(base, g) if lvl <= 0 else gradingmod.DeckGroundDem(base, g)) \
                if g is not None else base
            analyses = self.deshadow is not None or self.colormatch is not None
            if quick and analyses and self.far_pool is not None and self.imagery is not None and node is tile:
                # the 3D map at a place not seen yet (photos still to come): the quick texture from the raw photos in
                # the far-tile process (all cores), not the finished pyramid (de-shadow, colour match) in this one
                # interpreter, which held a fresh city's first tiles 12 s (2026-11-07); the background download's
                # token re-renders it finished here once the photos are in (far_retex)
                x0q, y0q, _, _ = tile.bbox
                later = self.imagery.fetch_later(anchor.frame, *imagerymod.gutter_square(
                    x0q, y0q, tile.size, px_t), px_t, gutter=True)
            use_fin = analyses and self.far_pool is not None and node is tile and self.fin_worker_worth()
            if analyses and not later and not use_fin and self.far_pool is not None and node is tile:
                # (no finished tiles here yet: a first visit; raw now in the worker, finished re-rendered right after.
                # Driving too, not only the 3D map (the user, 2026-11-07: "apply what we learned and optimized for the 3d
                # map into driving and flying"): this interpreter rendered them one at a time beside the event loop)
                later = [self.imagery.done_token()]
            raw_quick = bool(later) and analyses
            if FAR_TRACE:
                log.info("fartrace path L%d %s quick %s analyses %s later %d use_fin %s raw %s pool %s", lvl, tile.key,
                         quick, analyses, len(later), use_fin, raw_quick, self.far_pool is not None)
            if self.far_pool is not None and self.imagery is not None and node is tile and \
                    (not analyses or raw_quick or use_fin):
                # the texture in the far-tile process too, from the imagery on disk; with the analyses on (de-shadow,
                # colour match) from this process's finished tiles on disk alone (tex["fin"]: their directories), a
                # tile not finished yet (or with a correction to come) rendered here. They were all rendered here:
                # 81 s of one interpreter's time in a 10 min first visit beside the event loop (2026-11-04)
                texture = self.texture_name(anchor.id, tile.key, self.tex_rev.get(ck, 0))
                im = self.imagery
                tex = {"path": os.path.join(self.out_dir, texture), "px": px_t,
                       "mask": (os.path.join(self.out_dir, surfacemod.road_mask_name(texture)),
                                self.road_mask_px(lvl)) if self.road_masks else None,
                       "chain": [s.name for s in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
                       "season_mode": im.season_mode}
                if analyses and not raw_quick:
                    tex["fin"] = {s.name: im._fin_sigs.setdefault(s.name, im._fin_sig(s)) for s in im.chain}
                if raw_quick:
                    tex["quick"] = quick        # (driving: every photo, fetched first if missing; the map: what is there)
                elif quick:     # (the 3D map: what is on disk now, the slow hosts' photos downloaded meanwhile)
                    x0q, y0q, _, _ = tile.bbox
                    later = im.fetch_later(anchor.frame, *imagerymod.gutter_square(x0q, y0q, tile.size, tex["px"]),
                                           tex["px"], gutter=True)
                    if later:
                        tex["quick"] = True
                args = (blockworker.far_tile, anchor.id, anchor.lat, anchor.lon, tile, zoom, q,
                        ref_zoom, self.near_visual in BLOCK_DRAWN, self.layout, tex)
                built, got = self.far_pool.submit(*args).result()
                if analyses and not raw_quick:
                    self.fin_worker_hits.append(got is not None)
                if got is None and analyses and not raw_quick:
                    # the 3D map, the finished tiles not on disk (a first visit whose photos the prefetch brought
                    # already): raw in the worker too, finished right after it is shown, not in this interpreter
                    # (Tulsa 2026-11-07: ten first tiles 7 s each here, all at once)
                    later = later or [im.done_token()]
                    raw_quick = True
                    tex.pop("fin", None)
                    tex["quick"] = quick
                    args = args[:-1] + (tex,)
                    built, got = self.far_pool.submit(*args).result()
                if got is None and (not analyses or raw_quick):
                    # a photo tile was not on disk (its prefetch dropped: a host at its daily budget, GitHub #116, or a
                    # fast drive): fetched here, the render goes back to the workers (rendered in this process, one
                    # interpreter for every far thread, the far field waited on it); here only if that fails too
                    x0, y0, _, _ = tile.bbox
                    if im.fetch_needed(anchor.frame, *imagerymod.gutter_square(x0, y0, tile.size, tex["px"]),
                                       tex["px"], last_only=bool(tex.get("quick")), gutter=True):
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
            if later:
                pending = set(pending or ()) | set(later)
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
                px = px_t
                texture = self.texture_name(*ck, self.tex_rev.get(ck, 0))
                tk = self.tex_key(anchor, tile, px)
                hit = revisitmod.load(self.cache_dir, "fartex", tk)
                if hit is not None:         # (rendered in this place before: the revisit cache)
                    blobs, extra = hit
                    if "mask.dds" in blobs:         # (kept files: hard-linked, nothing written)
                        revisitmod.place(blobs["mask.dds"],
                                         os.path.join(self.out_dir, surfacemod.road_mask_name(texture)))
                    revisitmod.place(blobs["tex.dds"], os.path.join(self.out_dir, texture))
                    tex_bytes, used = int(extra["tex_bytes"]), extra["used"]
                else:
                    f0 = revisitmod.faults()
                    if quick and not is_global and not later:
                        frame_q, x0q, y0q, size_q = self.tile_where(anchor, tile)
                        later = self.imagery.fetch_later(frame_q, *imagerymod.gutter_square(x0q, y0q, size_q, px), px,
                                                         gutter=True)
                    if later:
                        pending = set(pending or ()) | set(later)
                        with self.imagery.quick():
                            rgb, used = self.render_tile(anchor, tile, px, pending)
                    else:
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
                                    textured=material is not None, tex_px=px_t)
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
                        material, texture, tex_bytes, tex_s, credits=sources, err_n=built.lod_err)
        info.tile = tile
        info.tex_px = px_t
        info.coarse = coarse is not None
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
            blobs = {"tex.dds": revisitmod.Side(os.path.join(self.out_dir, texture))}     # (linked in, not copied)
            mp = os.path.join(self.out_dir, surfacemod.road_mask_name(texture))
            if os.path.exists(mp):
                blobs["mask.dds"] = revisitmod.Side(mp)
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
        px = self.tile_tex_px(anchor, tile)
        pending = set()
        # (a global tile sharpened from a small texture, the coarse cover's or a hidden one's (farpvs.py): in a far-tile
        # process like its first build, not in this interpreter)
        upgrade = isinstance(tile, farfield.GTile) and (prev.coarse or 0 < (prev.tex_px or 0) < px)
        if (self.worker_retex() or upgrade) and self.far_pool is not None and self.imagery is not None and \
                isinstance(tile, farfield.GTile):
            ck = self.far_ck(anchor.id, tile.key)
            texture = self.texture_name(*ck, prev.rev + 1)
            im = self.imagery
            tex = {"path": os.path.join(self.out_dir, texture), "px": px, "mask": None,
                   "chain": [s_.name for s_ in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
                   "season_mode": im.season_mode}
            args = (blockworker.far_global_tile, tile.z, tile.x, tile.y,
                    farfield.global_dem_zoom(tile.z, self.layout.cells),
                    farfield.global_dem_zoom(tile.z + 1, self.layout.cells), self.layout, tex)
            got = self.far_pool.submit(*args).result()[1]
            if got is None and upgrade and not self.worker_retex():
                # (its photos not on disk yet: fetched here, rendered there, as its first build: _make_tile)
                frame_q, x0q, y0q, size_q = self.tile_where(anchor, tile)
                if im.fetch_needed(frame_q, *imagerymod.gutter_square(x0q, y0q, size_q, px), px, gutter=True):
                    got = self.far_pool.submit(*args).result()[1]
            if got is not None:
                return replace(prev, texture=texture, tex_bytes=got[0], tex_seconds=time.perf_counter() - t0,
                               cm_pending=frozenset(), stale=False, rev=prev.rev + 1, tex_px=px, coarse=False)
        if self.worker_retex() and self.far_pool is not None and self.imagery is not None and \
                type(tile) is farfield.Tile and tile.level > 0:
            # the 3D map: the sharper texture (its photos in now) from the raw photos in a far-tile process, many at
            # once, not the finished pyramid in this one interpreter (2026-11-07: a landed city's ground stayed blurry
            # for half a minute, two re-renders at a time here; its corrections changed nothing there anyway)
            ck = self.far_ck(anchor.id, tile.key)
            texture = self.texture_name(*ck, prev.rev + 1)
            im = self.imagery
            tex = {"path": os.path.join(self.out_dir, texture), "px": px,
                   "mask": (os.path.join(self.out_dir, surfacemod.road_mask_name(texture)),
                            self.road_mask_px(tile.level)) if self.road_masks else None,
                   "chain": [s_.name for s_ in im.chain], "tone": self.tone, "leaf_on": im.leaf_on,
                   "season_mode": im.season_mode}
            lvl = tile.level
            got = self.far_pool.submit(blockworker.far_tile, anchor.id, anchor.lat, anchor.lon, tile,
                                       anchor.zooms[lvl], None, anchor.zooms[lvl - 1], self.near_visual in BLOCK_DRAWN,
                                       self.layout, tex).result()[1]
            if got is not None:
                return replace(prev, texture=texture, tex_bytes=got[0], tex_seconds=time.perf_counter() - t0,
                               cm_pending=frozenset(), stale=False, rev=prev.rev + 1, tex_px=px, coarse=False)
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
                       cm_pending=frozenset(pending), stale=False, rev=prev.rev + 1, credits=sources, tex_px=px,
                       coarse=False)

    def start_world_warm(self, anchor):
        """The rough earth (the user, 2026-11-07: "keep a very rough version of earth that we can easaly load at all times
        ready in storage"): once per server, the first time the 3D map's camera is high (Conn.update_far), every
        WORLD_WARM_Z global far tile (L10 for the usual anchor zoom: ~260 km, a 128 px photo) built into the revisit
        cache, nearest the view first, WORLD_WARM_CONC at a time and only while no far tile of a view waits (a tile
        already in the cache is skipped: from then on a far-out view loads from disk)."""
        if getattr(self, "_world_warm", None) is not None or not revisitmod.ON:
            return
        self._world_warm = asyncio.get_running_loop().create_task(self._warm_world(anchor))

    async def _warm_world(self, anchor):
        z = WORLD_WARM_Z
        n = 1 << z
        lat0, lon0 = float(anchor.lat), float(anchor.lon)
        cands = []
        for y in range(n):
            la, _ = farfield.global_frame_ll(z, 0, y)
            if abs(la) > farfield.GLOBAL_MAX_LAT:
                continue
            for x in range(n):
                la, lo = farfield.global_frame_ll(z, x, y)
                d = math.hypot((la - lat0), (((lo - lon0 + 180.0) % 360.0) - 180.0) * math.cos(math.radians(lat0)))
                cands.append((d, x, y))
        cands.sort()
        level = farfield.GLOBAL_MIN_LEVEL + int(anchor.zf) - z
        px = self.tex_size(level)
        done = skipped = 0
        t0 = time.monotonic()
        sem = asyncio.Semaphore(WORLD_WARM_CONC)

        async def one(x, y):
            nonlocal done, skipped
            async with sem:
                while len(self.tile_inflight) > WORLD_WARM_CONC:      # (a view's tiles first)
                    await asyncio.sleep(0.5)
                q = tuple(float(v) for v in geotilesmod.quad(anchor.frame, x, y, z).ravel())
                edge = sum(math.hypot(q[(2 * k + 2) % 8] - q[2 * k], q[(2 * k + 3) % 8] - q[2 * k + 1])
                           for k in range(4)) / 4
                t = farfield.GTile(z, x, y, level, q, float(edge))
                if revisitmod.has(self.cache_dir, "fartex", self.tex_key(anchor, t, px)):
                    skipped += 1
                    return
                try:
                    await self.tile(anchor, t)
                    done += 1
                except Exception as e:          # (offline, a source down: the next start tries again)
                    log.debug("world warm %s/%s/%s: %s", z, x, y, e)
                if (done + skipped) % 500 == 0:
                    log.info("world warm: %d built, %d cached, of %d (%.0f s)", done, skipped, len(cands),
                             time.monotonic() - t0)

        await asyncio.gather(*(one(x, y) for _, x, y in cands))
        log.info("world warm: done, %d built, %d already cached, in %.0f s", done, skipped, time.monotonic() - t0)

    def tile(self, anchor, tile, shield=True):
        """asyncio future for the tile's TileInfo; generated at most once per (anchorId, key) (a global tile's: once
        for every anchor, far_ck). shield False: the build's own future (a build started ahead, nobody waits on it:
        Connection.prebuild_coarse)."""
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
        if not shield:
            return fut
        # A connection may stop waiting at a rebase or disconnect. Cancelling the
        # executor Future does not stop a running thread: it can still write its
        # pooled shape. Keep that producer tracked until its writes have finished,
        # so reclaim_names cannot give its slot to another tile in the meantime.
        return asyncio.shield(fut)

    def in_use(self):
        """(anchorId, key) of every tile a connection shows or holds, or let go of less than
        prune_delay ago: after a far_swap / far_remove the mod still shows the tile until the
        swap has run (its adds load within the governor budget, then a 0.5 s crossover) and
        deletes it time-sliced, and a shape file must not vanish under a live TSStatic."""
        out = self.matslots.held_owners()
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
        # Allocation can query this from a tile worker while the event loop
        # connects/disconnects clients; do not iterate the mutable set directly.
        return self.stream_leases.held(n) or any(n in c.slot_holds for c in tuple(self.connections)
                                                 if getattr(c, "lease", None) is None)

    def attach_stream(self, conn, msg):
        """Reconcile a game's complete resource snapshot before sending it new pooled files."""
        resumable = msg.get("streamLease") == 1
        token = msg.get("leaseId") if msg.get("leaseSession") == self.sid else None
        if not isinstance(token, str):
            token = None
        same_session = msg.get("slotSession") == self.sid
        try:
            if resumable and token in self.stream_leases.entries and not same_session:
                raise ValueError("resuming a stream requires its complete slot snapshot")
            slots = streamleases.snapshot(msg.get("slotsHeld"), self.matslots.made) if same_session else None
            held = msg.get("held", []) if same_session else []
            if held == {}:
                held = []
            if not isinstance(held, list) or any(not streamleases.integer(n) for n in held):
                raise ValueError("held anchors must be an array of integers")
            anchors = {int(n) for n in held if n in self.anchors}
        except ValueError as e:
            # (lenient: a bad snapshot starts a fresh lease rather than failing the hello; the old lease, if any,
            # stays detached and expires: expire_leases)
            log.warning("client %s: stream snapshot not resumed (%s): a new lease", getattr(conn, "peer", None), e)
            token, slots, anchors = None, None, set()
        lease, released, previous = self.stream_leases.attach(conn, token, slots, anchors, resumable)
        if previous is not None:
            previous.abort_stream("replaced by this game's reconnect")
        self.slots_let_go(released)
        return lease.token

    def slots_let_go(self, slots):
        """Event loop: these slots were reported gone or excluded by a complete reconnect snapshot."""
        for n in slots:
            if not self.slot_held(n):
                self.matslots.unheld(n)

    def held_anchors(self):
        """Previous anchors whose tile objects some mod still has (pose.held): their files stay."""
        out = self.stream_leases.anchors()
        for c in self.connections:
            lease = getattr(c, "lease", None)
            if lease is None or lease.connection is c:
                out |= c.held
        out.discard(GLOBAL_AID)
        return out

    def note_lod_err(self, ck, info):
        """Keep a built far tile's normal error for the screen-space LOD (lod_errs / glod_errs)."""
        e = getattr(info, "err_n", None)
        if e is None:
            e = info.err
        if e is None:
            return
        g = farfield.parse_global(ck[1])
        if g is not None:
            d, k = self.glod_errs, g
        else:
            t = getattr(info, "tile", None)
            node = t.node if t is not None else farfield.tile_of(ck[1]).node if not farfield.is_piece_key(ck[1]) \
                else None
            if node is None:
                return
            d, k = self.lod_errs, (ck[0], node)
        d.pop(k, None)
        d[k] = float(e)
        if len(d) > LOD_ERRS_MAX:
            for old in list(itertools.islice(d, len(d) - LOD_ERRS_MAX * 3 // 4)):
                del d[old]

    def _tile_done(self, ck, fut):
        self.tile_inflight.pop(ck, None)
        if fut.cancelled() or fut.exception() is not None:
            return
        before = self.tiles.get(ck)
        info = self.tiles[ck] = fut.result()
        self.note_lod_err(ck, info)
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
        lease = getattr(conn, "lease", None)
        if getattr(conn, "closing", False) or (lease is not None and lease.connection is not conn):
            return out
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
            packed = pooled and cdae.is_packed(data)
            if packed:                  # (a compressed shape: the names are inside its body; written back so)
                data = cdae.plain(data)
            # the name in the tile's own file: the one it was built with, not info.material once it was renamed before
            # (a global far tile kept across rebases is renamed again and again: restamping info.material failed from
            # its second rename on, and the tile was sent with a name another tile had, drawing that tile's texture:
            # Utah's red far tiles on the Pikes Peak ground, 2026-10-29)
            have = info.material if info.material.encode() in data else self.matslots.name_in(data)
            if have is None:
                raise ValueError(f"no pooled name in {os.path.basename(src)}")
            if new != have:
                data = matslotsmod.restamp(data, have, new)
            if packed:
                data = cdae.pack(data)
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
        if len(rk) == 2 and rk[0] == imagerymod.IMG_PENDING:      # (a quick texture's photos: Imagery.fetch_later)
            return None if self.imagery is None else self.imagery.img_status(rk)
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

    def far_lazy_done(self, ck):
        """(event loop) A far tile's best photos are in (Connection.far_lazy_tick): rendered again with them."""
        info = self.tiles.get(ck)
        if info is None or info.material is None or info.stale:
            return
        info.stale = True
        self.far_lazy_stats = getattr(self, "far_lazy_stats", 0) + 1
        self.retex_request(ck)

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
        if any(getattr(c, "lod_ramp", 1.0) < 1.0 and (c.map_view or c.map_open) for c in self.connections):
            return          # (the 3D map's coarse steps: their tiles go at the next step; sharpened once at full detail)
        boost = MAP_RETEX_BOOST if self.map_on() else 1      # (the 3D map: re-renders in the far-tile processes)
        while self.retex_todo and self.retex_busy < self.retex_inflight * boost:
            # (the 3D map: only into far-tile slots no build of a tile not shown yet needs; Tucson 2026-11-07: eight
            # re-renders held the gate and the far field built 110 tiles a minute; Conn._stream pumps again)
            if boost > 1 and self.retex_busy >= self.retex_inflight and \
                    sum(1 for c in self.connections for f, _ in c.inflight.values() if not f.done()) + \
                    self.retex_busy >= self.far_limit():
                return
            wait = self._retex_next - time.monotonic()
            if wait > 0:
                self._retex_timer = self.loop.call_later(wait, self._retex_wake)
                return
            ck = self._retex_pick()
            if ck is None:
                return
            self.retex_todo.discard(ck)
            self._retex_next = time.monotonic() + 1.0 / (self.retex_rate * boost)
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
        busy = {k[0] for k in self.tile_inflight}
        retry = False
        # (the session folders listed once: a glob per anchor and file kind, ~10 each, scanned folders of thousands of
        # files on the event loop, 2 s stalls when the 3D map's view moved the origin often, 2026-11-07)
        # (and indexed once by "<kind>_<anchor id>_": a scan of every name for each anchor and kind was ~10 x anchors x
        # thousands of names, still 2-3 s on the loop after many origin moves, 2026-11-07; every kind is one word)
        listing = {}

        def names(d):
            if d not in listing:
                try:
                    found = os.listdir(d)
                except OSError:
                    found = []
                idx = {}
                for n in found:
                    parts = n.split("_", 2)
                    if len(parts) == 3:
                        idx.setdefault((parts[0], parts[1]), []).append(n)
                listing[d] = (set(found), idx)
            return listing[d]

        def starting(d, kind, aid):
            return [os.path.join(d, n) for n in names(d)[1].get((kind, str(aid)), ())]

        def listed(p):
            return os.path.basename(p) in names(os.path.dirname(p))[0]
        for aid in [a for a in self.anchors if a < keep_from]:
            if aid in busy:
                # A running worker still owns its files and pooled shape slot.
                # Its asyncio Future must not be cancelled to hurry pruning.
                retry = True
                continue
            del self.anchors[aid]
            self.horizon_grids.pop(aid, None)
            self.horizon_failed.pop(aid, None)
            self.pvs_fields.pop(aid, None)      # (its rings and viewpoints: ~15 MB)
            self.pvs_failed.pop(aid, None)
            for ck in [k for k in self.tiles if k[0] == aid]:
                del self.tiles[ck]
            for k in [k for k in self.lod_errs if k[0] == aid]:
                del self.lod_errs[k]
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
            for key in [k for k in self.hm if k[0] == aid]:
                if key not in self.hm_committed and key not in self.hm_sent:
                    del self.hm[key]
            keep = {self.hm_name(k) for k in self.hm} | {i["ter"] for i in self.hm.values() if i.get("ter")}
            doomed = []
            for p in starting(self.out_dir, "far", aid):
                name = os.path.basename(p)
                doomed.append(p)
                if name.endswith(".dae"):
                    doomed.append(os.path.join(self.cdae_dir, os.path.splitext(name)[0].lower() + ".cdae"))
            doomed += starting(self.cdae_dir, "far", aid)  # caches whose source went earlier
            doomed += [p for p in starting(self.out_dir, "hm", aid) if os.path.basename(p) not in keep]
            # the road / building / tree tiles too (2026-10-14: a night of teleports left 13k files in the
            # session folder, and the game's file watchers cost 4.5 ms a frame)
            for stem in ANCHOR_TILE_FILES:
                doomed += starting(self.out_dir, stem, aid)
            self.queue_delete(p for p in dict.fromkeys(doomed) if listed(p))     # (there when listed: no stat each)
        if retry:
            self.schedule_prune(keep_from)


# Per-anchor tile files (besides far_ / hm_) that prune() deletes with their anchor
# A teleport's first block is built first without the road grading (make_provisional: the car is placed in ~5 s
# instead of 15-60 s on a fresh place) and replaced by the graded one when that is built (terrain_refresh)
PROVISIONAL_FIRST = os.environ.get("BNGE_PROVISIONAL", "1") != "0"
PROVISIONAL_KEEP_S = 60.0   # the provisional .ter is deleted this long after its replacement was sent
# A follow block the mod asked for whose road plans are not made yet (a first visit's cells, 15-120 s: requests queued
# 72-80 s in Chicago, the edge guard holding the car, 2026-11-01): sent provisional at once (make_provisional, its own
# threads) and the graded one as terrain_refresh when built (refine_follow); BNGE_PROVISIONAL_FOLLOW_S=0: never
PROVISIONAL_FOLLOW_S = float(os.environ.get("BNGE_PROVISIONAL_FOLLOW_S", "2.5"))
PROVISIONAL_RUNNING_S = 7.0     # s: a planned block already building in its process gets this long before a provisional
# A provisional block sent before its road map cells were on disk (a cold teleport's first block: the roads layer of a
# fresh place takes 10-60 s, its road plans 20-120 s more) is built again, still provisional, once they are: the roads
# painted in and their corridors flattened (l0roads.py), sent as terrain_refresh unless the graded block went first
# (Connection.prov_refresh; the user, 2026-11-11: "Fake it until it loads"). BNGE_L0_REFRESH=0: never
L0_REFRESH = os.environ.get("BNGE_L0_REFRESH", "1") != "0"
L0_REFRESH_MAX_S = 600.0        # s after its provisional block a refresh is still made (the graded one is long due then)

PADS_REFRESH_MS = 5.0       # the block in use built again with its gas station pads only below this speed (m/s) ...
PADS_SETTLE_S = 3.0         # ... and this long after the last new pad (Connection.pads_refresh)
NAV_WAIT_S = 30.0           # s an anchor's first AI graph waits at most for the road tiles round the car (nav_may_start)
TELEPORT_PAUSE_M = 20000.0  # an anchor asked this far from the player is a teleport: the old place's work pauses
TELEPORT_PAUSE_MAX_S = 120.0  # ... at most this long (the mod may never commit it)
BOOST_MOVE_MS = 3.0         # the loading boost ends when the player moves faster than this (m/s) ...
BOOST_MAX_S = 120.0         # ... or this long after the teleport
BOOST_GRACE_S = 5.0         # (the car dropped into place moves a little at first)
# While driving: the loading boost again when the world falls behind (the user's Manhattan drive 2026-11-01: the server
# saturated its 2 cores (4 CPUs) with the PC half idle, blocks queued 5-16 s and the edge guard held the car): the next
# block asked this long ago and not committed yet (still building, or its far tiles not live: the mod's gate), or the
# roads round the car not built; off once caught up for CATCHUP_CALM_S
CATCHUP_AFTER_S = 1.5
# ... the road tiles counted: those within this of the car (all of SEND_M's 640 m were never done while driving into
# new streets: the boost on all drive long at 20-60 mph, Fells Point 2026-11-07)
CATCHUP_ROADS_M = 300.0
CATCHUP_CALM_S = 3.0
BAKE_FETCH = os.environ.get("BNGE_BAKE_FETCH", "off") == "on"   # the data repo's packs (Server.bake_sync): off until
# there are any (the user, 2026-11-02: no cloud baking while world gen changes daily; local packs, BeamMP servers, later)
CATCHUP_MORE_S = 10.0       # s behind all along before the catch-up boost lends half the game's cores
HOLD_BOOST_CALM_S = 2.0     # s after an edge hold ends before its boost does (Connection.hold_boost)
CATCHUP_LEND_SHARE = float(os.environ.get("BNGE_CATCHUP_LEND", "0.25"))     # of the game's cores (cpupin.boosted)
CATCHUP_LEND_AT_NORMAL = os.environ.get("BNGE_CATCHUP_LEND_NORMAL", "0") == "1"   # ... also at normal priority
BELOW_GAME = True               # (main: the server below the game's priority, --nice > 0; tests: as by default)
CATCHUP_MIN_MS = 8.0        # m/s: only while driving (a parked player's work waits as before)
BOOST_FULL_MAX_S = 300.0    # full priority loading (Server.load_boost's teleportPriority) ends this long after at most

# (rs / rx: the road geotiles' skins and exact variants of the anchor; rail / railcol / tunnel: its road tiles' rail and
# tunnel shapes. Left out until 2026-11-02: kept until the server stopped, GitHub #599's 9 GB session folder)
ANCHOR_TILE_FILES = ("roads", "road", "walks", "walkcol", "deck", "bld", "bldcol", "bldfar", "wire", "veg", "navgraph",
                     "rs", "rx", "rail", "railcol", "tunnel", "buscol")


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


def same_route_dest(dest, msg):
    """Is route request msg bound for dest ((lat, lon): the saved route's destination, or None)? Within
    ROUTE_SAME_DEST_DEG: a re-plan of the saved route (off route, a retried 429) moves only its start. Pure."""
    if dest is None:
        return False
    try:
        lat, lon = float(msg["lat1"]), float(msg["lon1"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(lat - dest[0]) <= ROUTE_SAME_DEST_DEG and abs(lon - dest[1]) <= ROUTE_SAME_DEST_DEG


class Connection:
    """One mod connection. Far-field state for the active anchor A:
        desired  D: the selected tiles (farfield.select)
        shown    C: tiles of executed swaps, (anchorId, key) -> bbox in A's frame; also holds
                    previous anchors' tiles after a rebase until they are covered
        sent     S: far_tile sent but not yet part of a far_swap (the mod keeps them hidden)
        groups:     farfield.swap_groups(C, D), recomputed whenever D changes
    A group is emitted (far_swap) once all its adds are sent; C then becomes C - remove + add.
    Sent tiles that no group needs any more are evicted with far_remove."""

    first_ramp = None               # (the coarse cover's start, set per connection: __init__ / commit_anchor)
    teleport_pending = False
    pvs = None                      # the far PVS in effect (farpvs.Set), or None: every far tile visible (pvs_tick)
    pvs_key = None                  # ... what it was made for (anchor, centre, half-size, eye class, ground step)
    pvs_job = None                  # ... the next one being made (its future)

    def __init__(self, server, reader, writer):
        self.s = server
        self.waterguard_ok = getattr(server, "sea", None) is None
        self.reader, self.writer = reader, writer
        self.loop = asyncio.get_running_loop()
        self.peer = writer.get_extra_info("peername")
        self.tasks = set()
        self.lease = None
        self.closing = False
        self.active = None              # committed anchor id
        self.block = None               # farfield.Block of the committed terrain
        self.pending_block = None       # the last prepare_terrain of the active anchor
        self.terrain_wait = 0           # prepare_terrain requests being built (roads.Tracker: the ground first)
        self.pending_at = None          # when the pending block was asked (on_pose: the catch-up boost)
        self.pending_answered = False   # ... and whether its terrain_ready went (then the wait is the mod's, not ours)
        self.catchup = False            # the loading boost is on to catch up while driving (CATCHUP_*)
        self.caught_up_at = None
        self.predicted_block = None     # the terrain the prefetch expects next
        self.predicted_chain = []       # ... and after it, a fast player's (PREFETCH_CHAIN; blocks)
        self.focus = (0.0, 0.0)         # player x, y in the active frame
        self.view_at = None             # the camera's x, y there when it is VIEW_AWAY_M or more from the car, else None
        self.foci = ()                  # other vehicles' collision foci there ((x, y, r, trees), parse_foci)
        self.vel = (0.0, 0.0)
        self.cam = None                 # pose.cam (protocol 3), kept for diagnostics
        self.desired = {}               # key -> Tile (active anchor)
        self.refined = set()            # nodes refined by the last selection (hysteresis)
        self.first_ramp = None          # monotonic start of a teleport's coarse first selection (FIRST_RAMP), or None
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
        self.graded_wait = []           # graded tiles passed over at the GRADED_SHARE cap, by priority (_next_tile)
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
        self.turbine_geo = False               # separate capability; no road/building radius increase
        self.turbines = vegetationmod.Tracker(TURBINE_SEND_M, TURBINE_DROP_M, TURBINE_PER_POSE, "turbines",
                                              geo=True, z=TURBINE_Z)
        self.nav = navgraphmod.Tracker()        # the AI road graph around the player (--traffic on)
        self.traffic_visuals = False            # negotiated visual routes, requested only at night with traffic on
        self.traffic_on = False
        self.distant_traffic = vegetationmod.Tracker(distanttraffic.SEND_M, distanttraffic.DROP_M, 1, "traffic",
                                                     geo=True, z=distanttraffic.Z, rebuild_history=False)
        self.distant_traffic_far = vegetationmod.Tracker(distanttraffic.FAR_SEND_M, distanttraffic.FAR_DROP_M, 1,
                                                         "traffic_far", inner=(distanttraffic.FAR_SKIP_M, 1),
                                                         geo=True, z=distanttraffic.Z, rebuild_history=False)
        self.nav_wait = {}                      # anchor id -> monotonic time its first AI graph was wanted
        self.gravity = gravitymod.Tracker()     # real gravity: recompute rule, last message sent
        self.credits = creditsmod.Tracker()     # data credits: throttle, last message sent (--credits on)
        self.ground_busy = False                # a groundahead job is running (one at a time)
        self.route_dl_version = 0               # bumped by every route(): supersedes a running route_download job
        self.route_points = []                  # the last planned route's points ([lat, lon]: a saved route's)
        self.route_dest = None                  # that route's asked destination (lat, lon): a failed re-plan toward
                                                # it keeps route_points (the mod keeps drawing the route it has)
        self.sea_busy = False                   # a water-tile plan is running (sea_tiles)
        self.sea_checked = None                 # (anchor id, 512 m square, monotonic, complete) of the last plan
        self.sea_sent = None                    # (anchor id, tiles) of the last "sea" message
        self.base_busy = False                  # a base photo refresh is running (base_refresh)
        self.pads_busy = False                  # a block is being built again with its new pads (pads_refresh)
        self.roads_busy = False                 # ... with the roads of map cells that came after it (roads_refresh)
        self.prov_live = {}                     # key -> a provisional block sent before its road cells (prov_refresh)
        self.prov_busy = False                  # ... one being built again
        self.prov_last = None                   # ... the last provisional block sent (the mod's next commit)
        self.ground_sent = None                 # (anchor id, z, monotonic) of the last ground_ahead sent
        self.following = True                   # the mod moves its block (pose "follow"; false high up)
        self.map_view = False                   # the 3D map's view frame (pose "view": on_pose)
        self.map_open = False                   # the 3D map open (pose "map")
        # the mod's graphics settings (message 'graphics', after every hello and on a change; set_graphics): the far
        # field's detail (lodScale times the layout's K: the distance a level splits at), the tree tiles' reach
        # (treeScale) and the far buildings', skyline's and far bridges' (buildingScale); 1 = the defaults
        self.lod_scale = self.tree_scale = self.building_scale = 1.0
        self.view_cap = None                    # the far field's radius at most (m): the mod's fixed view distance
        # the mod's screen-space LOD (lodPx: the largest pop allowed on screen, px; 0: the distance rule alone; None:
        # not sent, the server's --lod-px) and the recent pixels per radian [(time, ppr)] (view)
        self.lod_px = None
        self.ppr_hist = deque()

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
        """{"id", "lat0", "lon0", "lat1", "lon1", "via"?, "download"?} -> route_result {"id", "points": [[lat, lon]...],
        "dist", "dur", "legs": [[dist_m, dur_s], ...]?, "steps": [maneuver, ...]?, "elev": [[dist_m, elev_m], ...]?, "error"?} (via: [[lat, lon],
        ...] the route passes through in order, routing.MAX_VIA at most; legs: with via, each leg's). Also (re)starts this route's corridor prefetch
        (route_download, routeahead.py; download: "off", "ahead" (ROUTE_AHEAD_MAX_M, the default) or "route" (the
        whole route, up to ROUTE_WHOLE_MAX_M)). A failed plan toward the saved route's destination (a re-plan)
        keeps the saved points; toward a new destination it drops them (same_route_dest)."""
        self.route_dl_version += 1
        version = self.route_dl_version
        out = {"type": "route_result", "id": msg.get("id"), "points": [], "dist": 0.0, "dur": 0.0}
        for attempt in range(ROUTE_429_TRIES):
            try:
                r = await asyncio.get_running_loop().run_in_executor(
                    self.s.ui_pool, routingmod.plan, float(msg["lat0"]), float(msg["lon0"]), float(msg["lat1"]),
                    float(msg["lon1"]), self.s.net, msg.get("bearing"), routingmod.via_points(msg.get("via")))
                out.update(r)
                out.pop("error", None)
                break
            except Exception as e:      # offline, no route, rate limited: the mod falls back to steering
                out["error"] = f"{type(e).__name__}: {e}"[:200]
                if not isinstance(e, netfetch.RateLimited) or attempt + 1 >= ROUTE_429_TRIES:
                    break
                # (rate limited: the latest destination asked again after the host's wait; a destination changed
                # twice in 2 s got a 429 and no route until it was set again, GitHub #1027)
                wait = getattr(e, "retry_after", None) or ROUTE_429_WAIT_S * (attempt + 1)
                log.info("route %s: rate limited, again in %.0f s", msg.get("id"), wait)
                await asyncio.sleep(min(float(wait), ROUTE_429_MAX_WAIT_S))
                if version != self.route_dl_version:
                    log.info("route %s: superseded while waiting", msg.get("id"))
                    return              # (a newer destination's request answers)
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
        if "error" not in out:
            self.route_points, self.route_dest = out["points"], (float(msg["lat1"]), float(msg["lon1"]))
        elif not same_route_dest(self.route_dest, msg):
            # a failed plan toward a NEW destination: the mod draws no route (gps.lua setDestination dropped its
            # own), so pre-cache must see none either; a failed RE-plan of the saved destination keeps the points
            # instead (the mod keeps drawing the route it has, and "Pre-cache route" answered "no route: plan one
            # with the GPS first" while the map drew one, every rate-limited off-route re-plan having wiped them)
            self.route_points, self.route_dest = [], None
        mode = msg.get("download")
        self.route_download(version, self.route_points, mode if mode in ROUTE_DOWNLOAD_MODES else "ahead")

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
        imagery = getattr(self.s, "imagery", None)
        if len(dem_keys) + len(cell_layer_pairs) == 0 and imagery is None:
            self.send(empty)
            return
        dem, mapdata = self.s.dem, self.s.mapdata
        texel = self.s.layout.block * heightmap.SQUARE / BASE_RENDER_PX

        def run():
            # the ground photo's high-resolution tiles along the corridor too (Imagery.route_keys, the blocks' base
            # photo zoom): the pre-cache fetched only heights and map cells, and a pre-cached long drive still
            # stopped to download every few km at 40-55 mph (GitHub #755); listed here, off the event loop
            img_keys = []
            if imagery is not None and pts:
                try:
                    img_keys = imagery.route_keys(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]), reach,
                                                  texel)
                except Exception as e:
                    log.debug("route download: imagery keys: %s: %s", type(e).__name__, e)
            total = len(dem_keys) + len(cell_layer_pairs) + len(img_keys)
            if total == 0:
                self.loop.call_soon_threadsafe(self.send, empty)
                return
            # counted once, up front: an already-cached key is skipped below (has() is re-checked only to
            # decide what to fetch), never re-counted, or a route with nothing left to do would inflate past
            # total and spam a push every iteration (push's own throttle only skips while done < total)
            done = routeaheadmod.done_count(dem, mapdata, dem_keys, cell_layer_pairs)
            done += sum(1 for k in img_keys if imagery.key_done(k))
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
            for key in img_keys:
                for _try in range(ROUTE_IMG_TRIES):     # (a deferred one asked again: an overnight pre-cache left no holes)
                    if self.route_dl_version != version:
                        return
                    try:
                        if imagery.fetch_key(key):
                            done += 1
                            push(True)
                        break
                    except imagerymod.ImageryError as e:
                        if not e.deferred:
                            break
                        time.sleep(ROUTE_RETRY_S)
                    except Exception as e:
                        log.debug("route download: imagery: %s: %s", type(e).__name__, e)
                        break
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
            import selfupdate
            b = selfupdate.blocked()
            if b and not updatesmod.newer(r["latest"], b["version"]):
                # (Windows would not start that version: offered again, it would be downloaded and blocked again)
                log.info("update %s not offered: Windows blocked it (%s)", r["latest"], b["by"] or "?")
                return
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

    async def addons_list(self, refresh=False):
        """The Add-ons page's list: 'addons' {addons: [card...], error?} (addons.Addons.cards)."""
        try:
            # (the loop's own pool, not ui_pool: GitHub can take seconds, the map's tiles must not wait behind it)
            cards = await asyncio.get_running_loop().run_in_executor(None, self.s.addons().cards, refresh)
            self.send({"type": "addons", "addons": cards})
        except Exception as e:          # never a reason to drop the connection
            log.warning("add-ons: list: %s: %s", type(e).__name__, e)
            self.send({"type": "addons", "addons": [], "error": f"{type(e).__name__}: {e}"[:200]})

    async def addon_detail(self, aid, refresh=False):
        """One add-on's page: 'addon_detail' {id, card, readme, readmeLinks, releases, error} (addons.Addons.detail)."""
        if not isinstance(aid, str):
            return
        try:
            d = await asyncio.get_running_loop().run_in_executor(None, self.s.addons().detail, aid, refresh)
        except Exception as e:
            log.info("add-ons: detail %r: %s: %s", aid, type(e).__name__, e)
            d = {"id": aid, "readme": "", "readmeLinks": [], "releases": [], "error": str(e)[:200]}
        self.send({"type": "addon_detail", **d})

    async def addon_install(self, aid, consent, mp):
        """Install / Update: the release downloaded into the mods folder and checked (addons.Addons.stage), with
        addon_status {id, state: progress | ready | error, text, done, total}; then addon_apply for the mod, which hands
        it to the game's mod manager."""
        if not isinstance(aid, str):
            return
        loop = asyncio.get_running_loop()
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                loop.call_soon_threadsafe(self.send, {"type": "addon_status", "id": aid, "state": "progress",
                                                      "done": done, "total": total, "text": "Downloading..."})
        self.send({"type": "addon_status", "id": aid, "state": "progress", "done": 0, "total": 0,
                   "text": "Asking GitHub..."})
        import addons as addonsmod
        try:
            apply = await loop.run_in_executor(None, lambda: self.s.addons().stage(aid, consent, mp, progress))
        except addonsmod.AddonError as e:
            log.info("add-ons: install %s: %s", aid, e)
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": str(e)})
            return
        except Exception as e:
            log.warning("add-ons: install %s: %s: %s", aid, type(e).__name__, e)
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": f"{type(e).__name__}: {e}"[:200]})
            return
        if apply is None:               # (a program beside the game: installed by the server itself)
            self.send({"type": "addon_status", "id": aid, "state": "done", "text": "Installed. Press Start to run it."})
            await self.addons_list()
            return
        self.send({"type": "addon_status", "id": aid, "state": "ready", "text": "Downloaded and checked: installing..."})
        self.send(apply)

    async def addon_add(self, text):
        """Add from GitHub: the pasted repository put on the local list (addons.Addons.add_repo), with
        addon_added {ok, id | error}, then the list again."""
        import addons as addonsmod
        try:
            aid = await asyncio.get_running_loop().run_in_executor(None, self.s.addons().add_repo, text)
            self.send({"type": "addon_added", "ok": True, "id": aid})
        except addonsmod.AddonError as e:
            self.send({"type": "addon_added", "ok": False, "error": str(e)})
        except Exception as e:
            log.warning("add-ons: add %r: %s: %s", text, type(e).__name__, e)
            self.send({"type": "addon_added", "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
        await self.addons_list()

    async def addon_options(self, aid, values):
        """An add-on's Options tab saved (addons.Addons.set_options: checked, kept; a running program restarted with
        them), with addon_status, then the list again."""
        import addons as addonsmod
        if not isinstance(aid, str):
            return
        try:
            again = await asyncio.get_running_loop().run_in_executor(None, self.s.addons().set_options, aid, values)
            self.send({"type": "addon_status", "id": aid, "state": "done",
                       "text": "Saved; restarted with them." if again else "Saved."})
        except addonsmod.AddonError as e:
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": str(e)})
        except Exception as e:
            log.warning("add-ons: options %s: %s: %s", aid, type(e).__name__, e)
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": f"{type(e).__name__}: {e}"[:200]})
        await self.addons_list()

    async def addon_forget(self, aid):
        """An add-on the player added taken off the list (addons.Addons.forget)."""
        import addons as addonsmod
        if not isinstance(aid, str):
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.s.addons().forget, aid)
        except addonsmod.AddonError as e:
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": str(e)})
        await self.addons_list()

    async def addon_run(self, aid, op):
        """A program beside the game (kind companion): Start / Stop / Remove / autostart_on / autostart_off
        (addons.Addons.run_op), with addon_status, then the list again."""
        if not isinstance(aid, str) or op not in ("start", "stop", "uninstall", "autostart_on", "autostart_off"):
            return
        import addons as addonsmod
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.s.addons().run_op, aid, op)
            text = {"start": "Running.", "stop": "Stopped.", "uninstall": "Removed.", "autostart_on":
                    "Starts with BeamNG.Earth.", "autostart_off": "Starts only when you press Start."}[op]
            self.send({"type": "addon_status", "id": aid, "state": "done", "text": text})
        except addonsmod.AddonError as e:
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": str(e)})
        except Exception as e:
            log.warning("add-ons: %s %s: %s: %s", op, aid, type(e).__name__, e)
            self.send({"type": "addon_status", "id": aid, "state": "error", "text": f"{type(e).__name__}: {e}"[:200]})
        await self.addons_list()

    async def addon_applied(self, aid, op, ok, error):
        """The mod's answer after the game's mod manager installed / updated / enabled / disabled / removed the add-on:
        logged (addons.Addons.applied), then the list again."""
        if not isinstance(aid, str) or op not in ("install", "update", "uninstall", "enable", "disable"):
            return
        try:
            self.s.addons().applied(aid, op, ok, error if isinstance(error, str) else "")
        except Exception as e:
            log.info("add-ons: applied %r: %s", aid, e)
        await self.addons_list()

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
        our own copy of Overture's divisions (placenames.py; GitHub #602: no outside geocoder is asked), only when
        "online" (the mod's setting, and the place due: moved ~1.5 km or 2 min). A region not on disk yet is fetched at
        low priority after the answer: its place parts come in a second where_result once they are there."""
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
        except (KeyError, TypeError, ValueError):
            return
        out = {"type": "where_result", "id": msg.get("id"), "street": ""}
        loop = asyncio.get_running_loop()
        if self.s.surface is not None:
            def street():
                layers, _ = self.s.surface.roads(lat, lon)
                return geocodemod.nearest_street(layers, lat, lon, routes=self.s.surface.road_routes(lat, lon))
            try:
                out["street"] = await loop.run_in_executor(self.s.ui_pool, street)
            except Exception as e:      # a cell being written, an odd feature: no street this time
                log.debug("where: street: %s: %s", type(e).__name__, e)
        if msg.get("online"):
            self._where_seq = seq = getattr(self, "_where_seq", 0) + 1
            pn = self.s.place_names()
            try:
                if await loop.run_in_executor(self.s.place_pool, pn.ready, lat, lon):
                    out.update(await loop.run_in_executor(self.s.place_pool, pn.lookup, lat, lon))
                else:
                    self.spawn(self._where_place_later(dict(out), lat, lon, seq))
            except Exception as e:      # a damaged cell file, odd data: the bar shows the street alone
                log.debug("where: place: %s: %s", type(e).__name__, e)
        self.send(out)

    def on_map_focus(self, msg):
        """map_focus {anchorId, x, y, eye, seconds}: the 3D map's flight ends at (x, y) with the eye that high; until
        then the far field is built there (on_pose) and at once now."""
        try:
            aid, x, y = int(msg["anchorId"]), float(msg["x"]), float(msg["y"])
            eye, secs = float(msg.get("eye") or 2000.0), min(10.0, float(msg.get("seconds") or 5.0))
        except (KeyError, TypeError, ValueError):
            return
        if not all(math.isfinite(v) for v in (x, y, eye, secs)) or math.hypot(x, y) > MAP_FOCUS_MAX_M:
            return          # (far from the origin: a rebase there is on its way, its own anchor's hint follows)
        self.focus_hint = (aid, x, y, max(0.0, eye), time.monotonic() + max(0.5, secs))
        if aid == self.active:
            self.view_at, self.eye = (x, y), max(0.0, eye)
            self.update_far()

    def _map_label_rows(self, lat, lon, zoom, radius, fetch):
        """maplabels' rows for the view (worker thread): (rows, complete). Without fetch only the cells on disk;
        complete is False when some were missing (fetch=True then asks for them as background jobs)."""
        rows = maplabelsmod.world_labels(lat, lon, zoom, radius)
        complete = True
        if zoom >= 7.5:
            towns = self.s.place_names().labels(lat, lon, min(radius, placenamesmod.LABELS_RADIUS_MAX_M),
                                                fetch=fetch)
            rows += maplabelsmod.town_labels(towns, rows)
        md = self.s.mapdata
        for layer, z0, r in (("hoods", maplabelsmod.HOOD_MIN_Z, maplabelsmod.HOOD_RADIUS_M),
                             ("roads", maplabelsmod.STREET_MIN_Z, maplabelsmod.STREET_RADIUS_M),
                             ("places", maplabelsmod.POI_MIN_Z, maplabelsmod.POI_RADIUS_M)):
            if md is None or zoom < z0:
                continue
            cells = []
            for k in maplabelsmod.near_cells(lat, lon, min(r, radius)):
                if md.has(k, layer) or fetch:
                    try:
                        cells.append(md.cell(k, layers=(layer,), kind="background").layers.get(layer))
                    except mapdatamod.DEFERRED:
                        complete = False
                else:
                    complete = False
            r = min(r, radius)
            rows += (maplabelsmod.street_labels(cells, lat, lon, zoom, r) if layer == "roads"
                     else maplabelsmod.hood_labels(cells, lat, lon, r) if layer == "hoods"
                     else maplabelsmod.poi_labels(cells, lat, lon, r))
        return rows, complete

    async def map_labels(self, msg):
        """The 3D map's labels {"id", "lat", "lon", "zoom", "radius"} -> map_labels_result {"id", "rows": [[kind, text,
        lat, lon, min zoom, max zoom, rank, extra, bearing], ...]} (maplabels.py). The cells on disk answer at once;
        when some are missing they are fetched as background jobs and a second answer follows, unless a newer request
        came meanwhile."""
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
            zoom, radius = float(msg.get("zoom") or 10.0), float(msg.get("radius") or 20000.0)
        except (KeyError, TypeError, ValueError):
            return
        if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(zoom) and math.isfinite(radius)):
            return
        self._map_labels_seq = seq = getattr(self, "_map_labels_seq", 0) + 1
        loop = asyncio.get_running_loop()
        try:
            rows, complete = await loop.run_in_executor(self.s.place_pool, self._map_label_rows, lat, lon, zoom,
                                                        radius, False)
        except Exception as e:
            log.info("map labels: %s: %s", type(e).__name__, e)
            return
        self.send({"type": "map_labels_result", "id": msg.get("id"), "rows": rows})
        for _ in range(WHERE_PLACE_TRIES):
            if complete or getattr(self, "_map_labels_seq", 0) != seq:
                return
            try:
                rows, complete = await loop.run_in_executor(self.s.place_pool, self._map_label_rows, lat, lon, zoom,
                                                            radius, True)
            except mapdatamod.DEFERRED:
                await asyncio.sleep(WHERE_PLACE_RETRY_S)
                continue
            except Exception as e:
                (log.debug if isinstance(e, netfetch.FetchError) else log.info)("map labels: %s: %s",
                                                                                  type(e).__name__, e)
                return
            if getattr(self, "_map_labels_seq", 0) == seq:
                self.send({"type": "map_labels_result", "id": msg.get("id"), "rows": rows})
            if not complete:
                await asyncio.sleep(WHERE_PLACE_RETRY_S)

    def _place_rows(self, req, fetch):
        """places.py's rows for an API request (worker thread): (places keyed, buildings, complete). Without fetch only
        the cells on disk; complete is False when some were missing (fetch=True then asks for them as background jobs,
        as _map_label_rows does)."""
        md = self.s.mapdata
        lat, lon = req["lat"], req["lon"]
        complete = True
        layers = (("places", req["radius"]),)
        if req["buildings"]:
            layers += (("buildings", min(req["radius"], placesmod.BUILDINGS_RADIUS_MAX_M)),)
        got = {}
        for layer, r in layers:
            cells = []
            for k in placesmod.near_cells(lat, lon, r):
                if md.has(k, layer) or fetch:
                    try:
                        cells.append(md.cell(k, layers=(layer,), kind="background").layers.get(layer))
                    except mapdatamod.DEFERRED:
                        complete = False
                else:
                    complete = False
            got[layer] = cells
        keyed = placesmod.place_rows(got["places"], lat, lon, req["radius"], placesmod.matcher(req["categories"]),
                                     req["min_conf"])
        blds = placesmod.building_rows(got["buildings"], lat, lon, req["radius"]) if req["buildings"] else None
        return keyed, blds, complete

    async def places(self, msg):
        """The modding API's places {"id", "lat", "lon", "radius", "categories", "limit", "cursor", "buildings",
        "minConfidence"} -> places_result {"id", "places", "buildings", "complete", "pending", "next", "error"}
        (places.py; api.places in the mod). The cells on disk answer at once; when some are missing they are fetched
        as background jobs (never ahead of the world's own downloads) and one more answer follows once they came
        (or after PLACES_TRIES, still incomplete); "pending" says whether another answer is still to come. Its own
        count of follow-ups, never _map_labels_seq (an add-on's request must not cancel the map's labels, nor the
        other way round), and no request cancels another's (two add-ons may ask at once); at most PLACES_FOLLOWUPS
        at a time, past them an incomplete answer is the last."""
        rid = msg.get("id")
        req, why = placesmod.request(msg)
        if req is None:
            self.send({"type": "places_result", "id": rid, "places": [], "complete": False, "pending": False,
                       "error": why})
            return
        if self.s.mapdata is None:
            self.send({"type": "places_result", "id": rid, "places": [], "complete": False, "pending": False,
                       "error": "no map data (BeamNG.Earth's roads are off)"})
            return
        loop = asyncio.get_running_loop()
        pool = self.s.api_pool

        def answer(keyed, blds, complete, pending, error=None):
            rows, nxt = placesmod.page(keyed, req["cursor"], req["limit"])
            out = {"type": "places_result", "id": rid, "places": rows, "complete": complete, "pending": pending}
            if blds is not None:
                out["buildings"] = blds
            if nxt is not None:
                out["next"] = nxt
            if error:
                out["error"] = error
            self.send(out)

        try:
            keyed, blds, complete = await loop.run_in_executor(pool, self._place_rows, req, False)
        except Exception as e:
            log.info("places: %s: %s", type(e).__name__, e)
            self.send({"type": "places_result", "id": rid, "places": [], "complete": False, "pending": False,
                       "error": f"{type(e).__name__}"})
            return
        busy = getattr(self, "_places_followups", 0)
        follow = not complete and busy < PLACES_FOLLOWUPS
        answer(keyed, blds, complete, follow)
        if not follow:
            return
        self._places_followups = busy + 1
        try:
            for attempt in range(PLACES_TRIES):
                last = attempt == PLACES_TRIES - 1
                try:
                    keyed, blds, complete = await loop.run_in_executor(pool, self._place_rows, req, True)
                except mapdatamod.DEFERRED:
                    if last:
                        answer(keyed, blds, False, False)
                        return
                    await asyncio.sleep(WHERE_PLACE_RETRY_S)
                    continue
                except Exception as e:
                    (log.debug if isinstance(e, netfetch.FetchError) else log.info)("places: %s: %s",
                                                                                    type(e).__name__, e)
                    answer(keyed, blds, False, False, type(e).__name__)
                    return
                if complete or last:
                    answer(keyed, blds, complete, False)
                    return
                await asyncio.sleep(WHERE_PLACE_RETRY_S)
        finally:
            self._places_followups = getattr(self, "_places_followups", 1) - 1

    async def _where_place_later(self, out, lat, lon, seq):
        """where()'s place parts for a region not on disk yet: fetched as a background job (netfetch "background":
        never ahead of the world's own downloads), asked again every WHERE_PLACE_RETRY_S while Overture's hosts are
        busy, sent in a where_result of their own unless the player asked for another place meanwhile."""
        loop = asyncio.get_running_loop()
        pn = self.s.place_names()
        for _ in range(WHERE_PLACE_TRIES):
            if getattr(self, "_where_seq", 0) != seq:
                return
            try:
                place = await loop.run_in_executor(self.s.place_pool, pn.lookup, lat, lon, "background")
            except mapdatamod.DEFERRED as e:     # busy (the world's downloads first), rate limited: later
                log.debug("where: place names deferred: %s: %s", type(e).__name__, e)
                await asyncio.sleep(WHERE_PLACE_RETRY_S)
                continue
            except Exception as e:      # offline, the data unreadable: the bar shows the street alone
                (log.debug if isinstance(e, netfetch.FetchError) else log.info)(
                    "where: place names: %s: %s", type(e).__name__, e)
                return
            if getattr(self, "_where_seq", 0) == seq:
                out.update(place)
                self.send(out)
            return

    async def geocode(self, msg):
        """The map's search {"id", "q", "lat"?, "lon"?, "full"?} -> geocode_result {"id", "results": [...], "error"?}."""
        q = str(msg.get("q") or "")[:200]
        lat, lon = msg.get("lat"), msg.get("lon")
        out = {"type": "geocode_result", "id": msg.get("id"), "results": []}
        try:
            out["results"] = await asyncio.get_running_loop().run_in_executor(
                self.s.search_pool, geocodemod.search, q, lat, lon, self.s.net,
                self.s.country_at(lat, lon) if lat is not None and lon is not None else None, bool(msg.get("full")))
        except Exception as e:          # offline, rate limited, refused, a bad answer: the map says which (GitHub #602)
            out["error"] = f"{type(e).__name__}: {e}"[:200]
            out["text"] = geocodemod.error_text(e)
            if out["text"] != getattr(self.s, "_geocode_said", None):     # once per change, not per keystroke
                self.s._geocode_said = out["text"]
                log.warning("address search: %s (%s)", out["text"], out["error"])
        else:
            self.s._geocode_said = None
        log.debug("geocode %r: %d results%s", q, len(out["results"]), (" (" + out["error"] + ")") if "error" in out else "")
        self.send(out)

    async def stations(self, msg):
        """The station finder {"id", "lat", "lon", "r_km", "kinds", "mode"} -> stations_result {"id", "items": [{"lat",
        "lon", "name", "brand", "kind", "km"}], "partial", "error"?} (stationindex.StationIndex.query, in the search
        pool: it reads, builds and may download cells)."""
        out = {"type": "stations_result", "id": msg.get("id"), "items": [], "partial": False}
        idx = getattr(self.s, "stations", None)
        if idx is None or not self.s.gas_stations:
            out["error"] = "no stations"
        else:
            try:
                kinds = [k for k in (msg.get("kinds") or ()) if k in ("fuel", "ev")]
                out["items"], out["partial"] = await asyncio.get_running_loop().run_in_executor(
                    self.s.search_pool, idx.query, float(msg["lat"]), float(msg["lon"]),
                    float(msg.get("r_km") or stationindexmod.R_KM_MAX), kinds,
                    "near" if msg.get("mode") == "near" else "map")
            except Exception as e:      # a bad ask, an unreadable cell: an empty answer that says why
                out["error"] = f"{type(e).__name__}: {e}"[:200]
                log.debug("stations: %s", out["error"])
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
        (areas.Areas.plan / plan_route, in an executor thread: no network, but a box near Germany fetches Germany's
        border data once, geo_content_key)."""
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
        fast = msg.get("fast") is True      # (fast mode: areas.py; the mod pauses the game while it runs)
        route = self._area_route(msg)
        if route is not None:
            ok, info = await asyncio.get_running_loop().run_in_executor(
                None, self.s.areas.add_route, route[0], route[1], msg.get("half"), name, mode, fast)
        else:
            ok, info = await asyncio.get_running_loop().run_in_executor(
                None, self.s.areas.add, msg.get("s"), msg.get("w"), msg.get("n"), msg.get("e"), name, mode, fast)
        res = {"id": msg.get("id"), "ok": ok, ("area" if ok else "error"): info}
        self.send(self.s.areas_message(res))

    async def area_cmd(self, msg):
        """{"id", "area", "op": pause | resume | delete | fast | slow} -> areas {..., "result": {"id", "ok"}}."""
        if self.s.areas is None:
            return
        area, op = msg.get("area"), msg.get("op")
        ok = isinstance(area, str) and isinstance(op, str) and await asyncio.get_running_loop().run_in_executor(
            None, self.s.areas.command, area, op)
        self.send(self.s.areas_message({"id": msg.get("id"), "ok": bool(ok)}))

    def send(self, msg):
        lease = getattr(self, "lease", None)
        if not getattr(self, "closing", False) and (lease is None or lease.connection is self) and not self.writer.is_closing():
            data = protocol.encode(msg)
            transport = getattr(self.writer, "transport", None)
            pending = transport.get_write_buffer_size() if transport is not None else 0
            if pending + len(data) > SEND_BUFFER_MAX:
                self.abort_stream("outgoing buffer limit exceeded")
                return False
            self.writer.write(data)
            return True
        return False

    def abort_stream(self, reason):
        """Close this transport, preserving ownership until the game's next complete snapshot."""
        if self.closing:
            return
        self.closing = True
        log.info("client %s: %s", self.peer, reason)
        self.writer.close()
        transport = getattr(self.writer, "transport", None)
        if transport is not None:
            transport.abort()            # a blocked peer must not keep a closing connection alive indefinitely
        if self.streamer is not None:
            self.streamer.cancel()
        for task in tuple(self.tasks):
            task.cancel()

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
        meter = self.__dict__.setdefault("net_meter", netstatemod.Meter())
        act, mb, rate = meter.read()
        # (the downloads the world waits on, across the server's processes: the loading line's and the edge hold's
        # "downloading" part; the user, 2026-11-04)
        # ... plus what most of them are and whether our own per-host limit holds them (the user, 2026-11-11: players
        # with gigabit lines read "1 MB/s" as their connection; it is the photo hosts' request limits)
        top = getattr(meter, "top", None)
        what = (netfetch.ROLES.get(top) or "").split(";")[0].strip() if top else ""
        net = [int(act), round(mb, 1), round(rate, 2), what, bool(getattr(meter, "waiting", 0))]
        counts = (self.ground_progress(aid), roads, trees, far, tuple(net))
        now = time.monotonic()
        if counts == getattr(self, "progress_sent", None) or now - getattr(self, "progress_at", 0.0) < PROGRESS_S:
            return
        self.progress_sent, self.progress_at = counts, now
        self.send({"type": "load_progress", "anchorId": aid, "ground": list(counts[0]), "roads": list(roads),
                   "trees": list(trees), "far": list(far), "net": net})

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
            self.closing = True
            self.route_dl_version += 1          # (a running route_download stops: no one to show its bar to)
            self.s.connections.discard(self)
            if not self.s.stream_leases.detach(self):
                self.s.slots_let_go(list(self.slot_holds))
            self.slot_holds = {}
            for tr in (self.roads, self.signs, self.veg, self.bldfar, self.bldsky, self.brfar, getattr(self, "turbines", None), self.nav,
                       self.distant_traffic, self.distant_traffic_far):
                if tr is not None:
                    tr.close()
            self.s.drop_claims(self)
            self.streamer.cancel()
            self.credits.close()
            pending_tasks = list(self.tasks)
            for t in pending_tasks:
                t.cancel()
            self.writer.close()
            # The streamer owns a separate Event.wait task; let its finally
            # finish before this connection (including a readiness probe) goes.
            await asyncio.gather(self.streamer, return_exceptions=True)
            await asyncio.gather(*pending_tasks, return_exceptions=True)
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

    SHARE_TICK_S = 2.0

    async def share_pump(self, hub):
        """The sharing hub's paced sending (mpshare.Hub.pump) and, every SHARE_TICK_S, its hello / uploads / prefetch
        around our player (Hub.tick, in the UI pool: it stats files), for the server's life."""
        tick_at = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                now = time.monotonic()
                if now >= tick_at:
                    tick_at = now + self.SHARE_TICK_S
                    ll = getattr(self, "pose_ll", None)
                    vx, vy = getattr(self, "vel", (0.0, 0.0))
                    await loop.run_in_executor(self.s.ui_pool, hub.tick, ll[0] if ll else None,
                                               ll[1] if ll else None, vx, vy)
                wait = hub.pump()
            except Exception as e:          # (never the connection's loop)
                log.warning("share: %s: %s", type(e).__name__, e)
                wait = 1.0
            await asyncio.sleep(min(wait if wait is not None else 0.05, max(0.005, tick_at - time.monotonic())))

    def spawn(self, coro):
        if self.closing:
            coro.close()
            return None
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def version_note(self, msg):
        """Tell the player when the mod in their game is not this app's own (a BeamMP server sends its own copy of the
        mod and turns the player's off): an older one has none of the faster tile paths the hello negotiates (before
        1.7.2: no geotiles, every rebase rebuilt the world round the car: 'severe performance issues', players'
        reports 2026-11-07). hello.release (release.lua, packaged with the mod) names its release when it has one."""
        if not isinstance(msg.get("modVersion"), str) or "vegFlat" not in msg:
            return                      # (not a BeamNG.Earth mod's hello: every one since 1.7.0 names these)
        mine = updatesmod.local_version() or ""
        theirs = msg.get("release") if isinstance(msg.get("release"), str) else ""
        import geotiles
        old_paths = msg.get("geoTiles") != geotiles.Z or msg.get("roadGeo") != roadsmod.ROAD_GEO
        if theirs and mine and theirs != mine:
            log.warning("hello: the mod is BeamNG.Earth %s, this app %s", theirs, mine)
            self.status(f"The BeamNG.Earth mod in your game is {theirs}, your app is {mine}. On a BeamMP server its owner "
                        "should run the same version as the players (the server's mod replaces yours): mismatched "
                        "versions load the world slower.")
        elif old_paths:
            log.warning("hello: the mod is BeamNG.Earth 1.7.1 or older (no geotiles): the slow tile paths")
            self.status("The BeamNG.Earth mod in your game is much older than your app (1.7.1 or older), so the world "
                        "loads the slow way. On a BeamMP server: ask its owner to update the server's BeamNG.Earth mod.")
        elif not getattr(self, "waterguard_ok", True):
            self.status("The BeamNG.Earth mod in your game is older than your app: the world loads, but its underwater "
                        "tunnels may flood. On a BeamMP server: ask its owner to update the server's BeamNG.Earth mod.")

    def dispatch(self, msg):
        if getattr(self, "closing", False):
            return
        kind = msg["type"]
        if kind == "hello":
            # (an older mod without the water guard: its world still loads, only an underwater tunnel's water may come
            # in. Refusing every world job left a BeamMP server's players, whose mod is the server's own copy, in an
            # empty world whenever that copy was older than their app, 2026-11-07)
            self.waterguard_ok = self.s.sea is None or msg.get("waterguard") == WATERGUARD_VERSION
            if not self.waterguard_ok:
                log.warning("hello: the mod has no water guard (older than this server): underwater tunnels may flood")
        if getattr(self, "lease", None) is None and kind in ("prepare_anchor", "commit_anchor", "prepare_terrain", "commit_terrain", "pose"):
            # (a client that skipped hello: a fresh lease, not resumable, and the request goes on as before PR #817;
            # the mod always says hello first on its connection, so its reconnect still resumes before any request)
            self.s.attach_stream(self, {})
        if kind == "traffic_visuals":
            self.traffic_on = self.traffic_visuals and msg.get("enabled") is True
            if self.traffic_on and msg.get("reset") is True:
                # The connection outlives levels; a reloaded client discarded its route cache.
                self.distant_traffic.sent.clear()
                self.distant_traffic_far.sent.clear()
        elif kind == "hello":
            lease_id = self.s.attach_stream(self, msg)
            self.s.check_user_dir(self, msg.get("userPath"))
            lay = self.s.layout
            fmt = msg.get("format", lay.format)
            if msg.get("protocol") != PROTOCOL:
                self.status(f"protocol mismatch: mod {msg.get('protocol')}, server {PROTOCOL}")
            elif not isinstance(fmt, (int, float)) or fmt < lay.format:
                # the mod's format is the newest it reads (a FORMAT 4 mod reads 3 too)
                self.status(f"format mismatch: mod {msg.get('format')}, server {lay.format}")
            m = {"type": "hello", "protocol": PROTOCOL, "format": lay.format, "serverVersion": SERVER_VERSION,
                 # the welcome page's telemetry defaults for this computer's region (privacyregion.py: its own time
                 # zone, nothing asked of a server) and the consent version it asks for (telemetry.CONSENT_VERSION)
                 "privacyRegion": privacyregionmod.region()[0], "telemetryConsent": TELEMETRY_CONSENT,
                 "runtimeProfiler": 2, "appVersion": updatesmod.local_version() or "",     # the release (VERSION): the players' reports
                 "sessionId": self.s.sid, "streamLease": 1, "leaseId": lease_id,
                 "farFormat": self.s.far_format, "material": farfield.MATERIAL,
                 "nearVisual": self.s.near_visual, "attribution": self.s.attribution()}
            import budget
            if budget.TIER in ("lean", "emergency"):    # (a low-memory machine: the mod warns on the emergency tier)
                m["memoryTier"] = budget.TIER
                note = budget.memory_notice(tier_=budget.TIER)
                if note:
                    m["memoryNote"] = note[1]
            if self.s.imagery is not None:      # the world map credits the sources its view shows
                m["imageryCredits"] = self.s.imagery.credits_by_area()
                m["otherCredits"] = self.s.attribution_rest()
            if msg.get("waterguard") == WATERGUARD_VERSION:
                m["waterguard"] = WATERGUARD_VERSION
            if lay.format >= 4:         # M1: the tiles' cell count (crossover.defaultErr, the exact level)
                m["cells"] = lay.cells
            # the terrain block: samples per edge and their spacing (the mod's lattice / follow table, by its width in
            # metres: 1024 x 1 m since 2026-11-01)
            m["blockSize"], m["squareSize"] = lay.block, heightmap.SQUARE
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
                # (in the road processes the waiting tiles share a job, so more may wait: _veg_geo_job)
                per = VEG_GEO_PER_POSE if geo and self.s.road_pool is not None else vegetationmod.BUILD_PER_POSE
                self.veg = vegetationmod.Tracker(old.send_m, old.drop_m, per_pose=per, geo=geo)
            if geo:
                m["geoTiles"] = geotiles.Z
            self.traffic_visuals = (geo and msg.get("trafficVisuals") == distanttraffic.PROTO and self.s.traffic
                                    and self.s.mapdata is not None and self.s.far_bld_pool is not None)
            self.traffic_on = False
            if self.traffic_visuals:
                m["trafficVisuals"] = distanttraffic.PROTO
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
            self.turbine_geo = msg.get("turbineGeo") == TURBINE_GEO
            if self.turbine_geo:
                m["turbineGeo"] = TURBINE_GEO
            # ... and the far field's levels from 3 up as global tiles when it places those (hello.globalFar:
            # farfield.lua's GLOBAL_FAR; geotiles phase 3b, --global-far): built once, kept across rebases
            self.ready_set = msg.get("readySet") == 1      # (the mod takes 'terrain_ready_set': follow.preferReady)
            self.global_far = (geo and msg.get("globalFar") == farfield.GLOBAL_PROTO and bool(self.s.global_far)
                               and self.s.layout.mesh)
            if self.global_far:
                m["globalFar"] = farfield.GLOBAL_PROTO
            self.send(m)
            self.version_note(msg)          # (after the hello and its protocol / format notes)
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
        elif kind == "share_hello":             # a BeamMP session (mpsession): our share code and weight for the plugin
            import mpshare
            hub, new = self.s.share_hub(self.send)
            self.send({"type": "share_info", "code": mpshare.share_code(self.s.share_parts()),
                       "weight": peerworkmod.share_weight()})
            if new:
                self.spawn(self.share_pump(hub))
        elif kind == "share_group":             # the plugin's bngeGroup: the players near us that share our code
            hub = getattr(self.s, "_share_hub", None)
            if hub is not None:
                try:
                    hub.set_group(json.loads(msg.get("d") or "{}"))
                except ValueError:
                    pass
        elif kind == "share_in":                # a member's message through the plugin ("<from>|<json>")
            hub = getattr(self.s, "_share_hub", None)
            if hub is not None:
                hub.on_data(msg.get("d"))
        # (shared world building v1, commented out 2026-10-27: the mod sends neither any more)
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
        elif kind == "preload_place":           # a destination picked on the map: loaded before the teleport
            self.preload_place(msg)
        elif kind == "stations":                # the station finder: the map's layers, Nearest (stationindex.py)
            self.spawn(self.stations(msg))
        elif kind == "route":                   # the GPS: the whole road route (routing.py)
            self.spawn(self.route(msg))
        elif kind == "where":                   # the status bar's street, town and country (geocode.py)
            self.spawn(self.where(msg))
        elif kind == "map_labels":              # the 3D map's labels, every kind by zoom (maplabels.py)
            self.spawn(self.map_labels(msg))
        elif kind == "places":                  # the modding API's places round a point (places.py; api.places)
            self.spawn(self.places(msg))
        elif kind == "map_focus":               # the 3D map flying: where the view will end (map3d.flyHint)
            self.on_map_focus(msg)
        elif kind == "random_place":            # the level select's "Random location" (randomplace.py)
            self.spawn(self.random_place(msg))
        elif kind == "place_previews":          # the level select's place pictures (placepreview.py)
            self.spawn(self.place_previews(msg))
        elif kind == "update_now":              # the update notice's "Update now" (selfupdate.py)
            self.spawn(self.update_now())
        elif kind == "open_page":               # the update notice's button: the releases page (updates.open_page)
            # (and the Add-ons page's links: an add-on's own text or repo, addons.Addons.openable)
            allow = self.s._addons.openable if getattr(self.s, "_addons", None) is not None else None
            if not updatesmod.open_page(msg.get("url"), allow=allow):
                log.info("open_page: refused %r", msg.get("url"))
        elif kind == "globe_req":               # the 3D map's whole-earth globe for an anchor (globe.py)
            self.spawn(self.globe(msg))
        elif kind == "space_clouds_req":        # the camera above the clouds: today's clouds round it (spaceclouds.py)
            self.spawn(self.space_clouds(msg))
        elif kind == "addons_list":             # the Add-ons page: the list (addons.py)
            self.spawn(self.addons_list(msg.get("refresh") is True))
        elif kind == "addon_detail":            # ... one add-on's README and changelog
            self.spawn(self.addon_detail(msg.get("id"), msg.get("refresh") is True))
        elif kind == "addon_install":           # ... Install / Update: downloaded and checked, then addon_apply
            self.spawn(self.addon_install(msg.get("id"), msg.get("consent") is True, msg.get("mp") is True))
        elif kind == "addon_add":               # ... Add from GitHub: a pasted repository put on the local list
            self.spawn(self.addon_add(msg.get("repo")))
        elif kind == "addon_options":           # ... its Options tab saved
            self.spawn(self.addon_options(msg.get("id"), msg.get("values")))
        elif kind == "addon_forget":            # ... one the player added taken off the list
            self.spawn(self.addon_forget(msg.get("id")))
        elif kind == "addon_run":               # ... a program beside the game: start / stop / remove / autostart
            self.spawn(self.addon_run(msg.get("id"), msg.get("op")))
        elif kind == "addon_applied":           # ... the game's mod manager did it (or not): logged, the list again
            self.spawn(self.addon_applied(msg.get("id"), msg.get("op"), msg.get("ok") is True, msg.get("error")))
        elif kind == "terrain_patch":           # ground under another vehicle's collision island (terrainpatch.lua)
            self.spawn(self.terrain_patch(msg))
        elif kind == "cache":
            self.on_cache(msg)
        elif kind == "memory":                  # the settings page's Max RAM: the limit (limitGb, 0 Auto) and the usage
            if "limitGb" in msg:
                memwatchmod.set_setting(msg.get("limitGb"))
            self.send(dict(memwatchmod.status(), type="memory"))
        elif kind == "app_path":                # the settings page's folders: the cache's, the BeamMP launcher's
            self.spawn(self.app_path(msg))
        elif kind == "runtime_profiler":
            mode = msg.get("mode", "full")
            if mode not in ("essential", "full"):
                raise ValueError("invalid profiler mode")
            self.profile_mode = mode
            self.profile_until = time.monotonic() + 3 if msg.get("enabled") is True else 0
            probe = msg.get("probe")
            if msg.get("enabled") is True and type(probe) is int and 0 <= probe <= 2147483647:
                self.send({"type": "runtime_profiler_pong", "probe": probe})
        elif kind == "perf_log":                # the mod's performance log (perflog.lua): its hitches, frame-rate drops,
            # minute lines and the machine, into this log beside the blocks' own timings (the file players send)
            lines = msg.get("lines")
            for line in (lines if isinstance(lines, list) else [])[:PERF_LOG_LINES]:
                if isinstance(line, str):
                    log.info("game: %s", line[:PERF_LOG_CHARS].replace("\n", " "))
        elif kind == "perf_setup":              # the anonymous performance data (perfsummary.lua; perfstats.py checks it):
            _perf(self.s).on_setup(msg.get("setup"))      # the machine and settings, once a connection
        elif kind == "perf_summary":            # ... and a 5-minute window of frame times, scene and world generation
            _perf(self.s).on_summary(msg.get("summary"))
        elif kind == "roads_reset":             # the mod dropped its roads (settings): send them all again
            self.roads.close()
            self.roads = road_tracker(self)
            self.signs.close()
            self.signs = signstream.Tracker()
        elif kind == "highway_signs":           # the settings page's switch: no sign tiles built while it is off
            self.signs_on = bool(msg.get("on", True))
        elif kind == "direction_signs":         # the settings page's switch: the sign tiles again, with or without
            if self.s.set_dirs(msg.get("on")):
                self.signs.refresh()
        elif kind == "road_signs":              # the settings page's switch: the sign tiles again, with or without
            if self.s.set_rsigns(msg.get("on")):
                self.signs.refresh()
        elif kind == "town_signs":              # the settings page's switch: the road and sign tiles again, with or without
            if self.s.set_towns(msg.get("on")):
                self.roads.close()
                self.roads = road_tracker(self)
                self.signs.refresh()
        elif kind == "road_wear":               # the settings page's Road detail: the road tiles again, worn or not
            if self.s.set_road_wear(msg.get("value")):
                self.roads.close()
                self.roads = road_tracker(self)
        elif kind == "graphics":                # the settings page's graphics knobs (set_graphics)
            self.set_graphics(msg)
        elif kind == "german_junctions":        # the settings page's switch: the road and sign tiles again, with or without
            if self.s.set_dej(msg.get("on")):   # (and the AI's graph: its stops, give-ways and zone speeds)
                self.roads.close()
                self.roads = road_tracker(self)
                self.signs.refresh()
                self.nav.refresh()
        elif kind == "gas_stations":            # the settings page's switch: the road tiles again, with or without
            if self.s.set_gas_stations(msg.get("on")):
                self.roads.close()
                self.roads = road_tracker(self)
        else:
            log.debug("ignoring message type %r", kind)

    def set_graphics(self, msg):
        """The settings page's graphics (the user, 2026-10-28: "give player much more control in the graphics settings
        (view distance, LOD quality, ETC."), message 'graphics' {lodScale, treeScale, buildingScale, photoPx}, each
        optional (viewM: the fixed view distance in m, the far field's radius at most; 0 = the real horizon; lodPx:
        the screen-space LOD's largest pop on screen, px, 0 = the distance rule alone, clamped to LOD_PX_MAX):
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
        v = msg.get("lodPx")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            v = min(LOD_PX_MAX, max(0.0, float(v)))
            far = far or v != self.lod_px_now()
            self.lod_px = v
        v = msg.get("viewM")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            cap = max(VIEW_CAP_MIN_M, float(v)) if v > 0 else None
            far = far or cap != self.view_cap
            self.view_cap = cap
        log.info("graphics: terrain detail x%.2f, trees to %.0f m, far buildings to %.0f m, ground photo %d px, view %s, "
                 "pixel error %s", self.lod_scale, self.veg.send_m, self.bldfar.send_m, self.s.base_px,
                 "to the horizon" if self.view_cap is None else f"{self.view_cap / 1000:.0f} km",
                 f"{self.lod_px_now():g} px" if self.lod_px_now() > 0 else "off")
        if far and self.active is not None:
            self.update_far()

    def kfun(self):
        """K(L) of this connection's far field: the server's times the mod's terrain detail (lodScale), times
        MAP_LOD_SCALE with the 3D map's view frame (finer tiles farther out: high res at every zoom)."""
        m = self.map_view or self.map_open
        k, s = self.s.kfun, self.lod_scale * (MAP_LOD_SCALE * getattr(self, "lod_ramp", 1.0) if m else 1.0)
        return k if s == 1.0 else (lambda lvl: k(lvl) * s)

    async def globe(self, msg):
        """The mod's globe.lua (the 3D map far out): the whole earth as one sphere in anchorId's frame, its world texture
        made once and cached (globe.py); answered with its files, or nothing (no world texture could be had)."""
        aid = msg.get("anchorId")
        if aid not in self.s.anchors:
            return
        import globe as globemod
        frame = self.s.anchors[aid].frame
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                None, globemod.build, self.s.out_dir, self.s.vfs_dir, frame, aid, self.s.cache_dir, self.s.net)
        except Exception as e:
            log.warning("globe: %s: %s", type(e).__name__, e)
            return
        if res is not None:
            self.send(dict(res, type="globe", anchorId=aid))

    async def space_clouds(self, msg):
        """The mod's spaceclouds.lua: the cloud shell round the point under a camera far above the clouds (lat, lon,
        alt m above sea level), in anchorId's frame; answered with its files (or nothing: no tile could be had)."""
        aid = msg.get("anchorId")
        try:
            lat, lon, alt = float(msg["lat"]), float(msg["lon"]), float(msg["alt"])
        except (KeyError, TypeError, ValueError):
            return
        if (aid not in self.s.anchors or not all(math.isfinite(v) for v in (lat, lon, alt))
                or abs(lat) > 90 or abs(lon) > 180 or not 0 <= alt <= 2_000_000):
            return
        import spaceclouds as spacecloudsmod
        frame = self.s.anchors[aid].frame
        sun = None                              # (the game's sun seen from there: its night side's lights follow it)
        try:
            az, el = float(msg["sunAz"]), float(msg["sunEl"])
            if math.isfinite(az) and math.isfinite(el) and -90 <= el <= 90:
                sun = spacecloudsmod.subsolar_from(lat, lon, az, el)
        except (KeyError, TypeError, ValueError):
            pass
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                None, spacecloudsmod.build, self.s.out_dir, self.s.vfs_dir, frame, lat, lon, alt, self.s.net, sun)
        except Exception as e:
            log.warning("space clouds: %s: %s", type(e).__name__, e)
            return
        if res is not None:
            self.send(dict(res, type="space_clouds", anchorId=aid))

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
            self.s.note_base(key, info["base"])
            out["baseSize"] = self.s.layout.block * heightmap.SQUARE
        if info.get("ter"):         # the same block as the engine's terrain file (swapped in, no import)
            out["ter"] = f"{self.s.vfs_dir}/{info['ter']}"
        return out

    def preload_place(self, msg):
        """A destination picked on the world map (its "Teleport here?" box, a search result; mod core.preloadPlace):
        its first block's elevation tiles and the road tiles' map cells (Server.warm_place's, in a frame at the place)
        fetched in the background before the teleport is asked for (the fastest world generators load the destination
        ahead; the user, 2026-11-11: "sub second world gen, even with clear cashe"). One at a time; a newer pick
        replaces a waiting one. Never waits, never builds."""
        try:
            lat, lon = float(msg["lat"]), float(msg["lon"])
        except (KeyError, TypeError, ValueError):
            return
        if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0):
            return
        lon = (lon + 180.0) % 360.0 - 180.0
        st = self.__dict__.setdefault("_preload", {"next": None, "busy": False, "last": None})
        st["next"] = (lat, lon)
        if st["busy"]:
            return
        st["busy"] = True
        s = self.s

        def run():
            try:
                while st["next"] is not None:
                    la, lo = st["next"]
                    st["next"] = None
                    last = st["last"]
                    if last is not None and abs(last[0] - la) < 1e-3 and abs(last[1] - lo) < 1e-3:
                        continue
                    st["last"] = (la, lo)
                    t0 = time.monotonic()
                    dem_n = cells_n = 0
                    if s.dem is not None:
                        zoom = demmod.zoom_for(la, 1.25, demmod.FINE_ZOOM)
                        reach = s.layout.block * heightmap.SQUARE / 2 + AHEAD_MARGIN_M
                        dem_n, _ = s.dem.prefetch(s.dem.keys_around(la, lo, reach, zoom))
                    spot = SimpleNamespace(frame=geodesy.EnuFrame(la, lo))
                    if s.mapdata is not None:
                        cells_n = s.warm_place(spot, 0.0, 0.0)
                    photo = 0
                    if s.imagery is not None and s.near_visual == "detail":
                        # (the first block's photos: the stand-in's, then its own, so their aerial tiles are on disk
                        # when the teleport asks: a cold Paducah block waited 2.7 s for its stand-in, 2026-11-11)
                        size = s.layout.block * heightmap.SQUARE
                        for px in (QUICK_BASE_PX, BASE_PX):
                            if st["next"] is not None:
                                break           # (a newer pick: that one now)
                            s.imagery.render(spot.frame, -size / 2, -size / 2, size, px, set())
                            photo += 1
                    log.info("preload %.5f, %.5f: %d elevation tiles, %d map cells queued, %d photos (%.1f s)", la,
                             lo, dem_n, cells_n, photo, time.monotonic() - t0)
            except Exception as e:          # (only the head start is lost)
                log.info("preload: %s: %s", type(e).__name__, e)
            finally:
                st["busy"] = False
        threading.Thread(target=run, name="preload-place", daemon=True).start()

    def abandon_old(self, aid):
        """A teleport leaves anchor aid: its queued work is dropped (Server.abandoned), unless another connection is
        on it (BeamMP: a player still there); the road plans warmed ahead for it are dropped too."""
        if any(c is not self and getattr(c, "active", None) == aid for c in list(self.s.connections)):
            return
        self.s.abandoned.add(aid)
        self.s.write_abandoned()
        self.abandoned_from = (getattr(self, "abandoned_from", None) or []) + [aid]
        with self.s.warm_cv:
            self.s.warm_want = []
        log.info("teleport: anchor %d's queued work dropped", aid)

    def unabandon_old(self):
        """The teleport did not happen (never committed, failed): the old place's work goes on."""
        aids = getattr(self, "abandoned_from", None)
        if aids:
            for aid in aids:
                self.s.abandoned.discard(aid)
            self.s.write_abandoned()
            self.abandoned_from = None

    async def prepare_anchor(self, msg):
        """Every first-ground preparation has progress and a terminal reply, including setup/finalization errors."""
        req = msg.get("requestId")
        progress = {"stage": "preparing", "done": False}
        started = time.monotonic()

        def report():
            self.send({"type": "anchor_progress", "requestId": req, "stage": progress["stage"],
                       "elapsed": round(time.monotonic() - started, 1)})

        async def heartbeat():
            while True:
                await asyncio.sleep(ANCHOR_PROGRESS_S)
                report()

        report()
        tick = asyncio.create_task(heartbeat())
        try:
            await self._prepare_anchor(msg, progress)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("anchor request %s failed during %s", req, progress["stage"])
            if not progress["done"]:
                self.teleport_pending = False
                self.unabandon_old()
                self.wake.set()
                self.send({"type": "anchor_error", "requestId": req, "stage": progress["stage"],
                           "error": f"{type(e).__name__}: {e}"})
        finally:
            tick.cancel()
            try:
                await tick
            except asyncio.CancelledError:
                pass
            for aid in [aid for aid, a in self.s.anchors_preparing.items() if aid not in self.s.anchors
                        and getattr(a, "prep_req", None) == (id(self), req)]:
                self.s.anchors_preparing.pop(aid, None)     # (a failed one: its grid and tiles are not kept for it)

    async def _prepare_anchor(self, msg, progress):
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
        if msg.get("purpose") == "view" and old is not None:
            # the 3D map's origin at the view (mod map3d.lua; the user, 2026-11-07: zooming into a spot far from the
            # car "the screen stays black"): the frame only. The car and its block stay in their own frame, hidden,
            # until the map closes; a first block here was built at the clamped car spot, neither the car's nor the
            # view's ground, and took 13 s of a fresh place (Milan) before anything could be drawn
            t0 = time.perf_counter()
            anchor = self.s.new_anchor(lat, lon)
            # (its height quantum without the survey: no block is ever driven on in this frame (the car goes back to
            # its own, 'carback'; Drive here is a teleport, a new anchor), and every far tile waited on the survey's
            # DEM, queued behind the last view's road and tree work: Turin's far field started 35 s late, 2026-11-07)
            anchor.q = VIEW_ANCHOR_Q
            self.s.anchors[anchor.id] = anchor
            if self.s.horizon:
                self.s.horizon_grid(anchor, self.eye if self.eye is not None else 0.0)
            log.info("anchor %d (%.5f, %.5f) for the map's view in %.2f s (no block)", anchor.id, lat, lon,
                     time.perf_counter() - t0)
            progress["done"] = True
            self.send({"type": "anchor_ready", "requestId": req, "anchorId": anchor.id, "lat": lat, "lon": lon,
                       "groundZ": None, "floorZ": None, "terrain": None, "provisional": False, "view": True})
            return
        # (a session's first place, or a jump: the provisional first block; the mod says so since 1.6.3, a teleport
        # under TELEPORT_PAUSE_M away waited 26 s for a graded first block in Manhattan, 2026-10-25)
        declared = msg.get("purpose") == "teleport"
        teleport = old is None or declared
        if old is None and not self.s.boost_on:
            # (joining the world: full priority loading as after a teleport, when the player chose it; the user,
            # 2026-10-31: "setting should trigger on joining the world too")
            self.boost_t0 = time.monotonic()
            self.s.load_boost(True, "joining", full_only=True)
        if old is not None:
            la, lo, _ = old.frame.enu_to_geodetic(*self.focus, 0.0)
            if declared or geodesy.haversine_m(float(la), float(lo), lat, lon) > TELEPORT_PAUSE_M:
                teleport = True
                self.teleport_pending, self.teleport_t0 = True, time.monotonic()
                log.debug("teleport to %.5f, %.5f: the old place's work paused", lat, lon)
                self.abandon_old(old.id)
                # the cores lent from the request on, not from the first block's arrival: that block is most of
                # the wait (16.9 s in Baltimore on a Steam Deck, the server on its own 4 CPUs, 2026-10-20)
                self.boost_t0 = time.monotonic()
                self.s.load_boost(True, "teleport")
        # (and a place this connection was still preparing, never committed: a teleport on before the last one came
        # left Wichita Falls's graded first block building 67 s on, Abilene's waiting 61 s behind it, 2026-11-11)
        prev = getattr(self, "preparing_aid", None)
        if prev is not None and prev != self.active and teleport:
            self.abandon_old(prev)
        anchor = self.s.new_anchor(lat, lon)
        anchor.ramp = teleport
        self.preparing_aid = anchor.id
        if teleport and FIRST_RAMP and not (self.map_view or self.map_open):
            # the coarse cover to the horizon built while the first block is (FIRST_RAMP: the global tiles, the same for
            # any anchor; they were only asked for at the commit, 4-11 s later), its horizon grid now too (it was asked
            # for after the block: the far field's first 4 s were the 80 km disc)
            anchor.prebuild_focus = tuple(float(v) for v in (place or (cx, cy)))
            anchor.prep_req = (id(self), msg.get("requestId"))
            self.s.anchors_preparing[anchor.id] = anchor
            try:
                if self.s.horizon:
                    self.s.horizon_grid(anchor, self.eye if self.eye is not None else 0.0)
                self.prebuild_coarse(anchor)
            except Exception as e:      # (the commit selects and builds them as before)
                log.warning("anchor %d: coarse far field ahead: %s: %s", anchor.id, type(e).__name__, e)
        # (the map data the first block's grading and the road tiles will ask for, fetched meanwhile)
        asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.warm_place, anchor, cx, cy)
        self.status(f"Building terrain at {lat:.5f}, {lon:.5f}...")
        t0 = time.perf_counter()
        # a teleport's first block without its road plans, the graded one sent when built (make_provisional)
        provisional = teleport and PROVISIONAL_FIRST and self.s.grading and not os.environ.get("BNGE_WORLD_BUNDLE")
        if self.s.surface is not None and not provisional:
            # the anchor's first block is built once and kept while the player stands still: give the
            # land cover around it a moment to arrive (blocks built while driving never wait)
            # (on the map's threads: the default pool is full of the old place's road / vegetation builds after a
            # teleport, and this 2 s wait queued behind them for ~25 s, 2026-10-14)
            tw = time.perf_counter()
            progress["stage"] = "land_cover"
            await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.surface.ready, lat, lon)
            log.info("anchor %s: land cover wait %.2f s", anchor.id, time.perf_counter() - tw)
        progress["stage"] = "terrain"
        pacer.foreground_begin()        # the player waits for this block: the background loops hold (pacer.hold)
        try:
            if provisional:
                info = await asyncio.get_running_loop().run_in_executor(
                    self.s.hm_pool, self.s.make_provisional, anchor, cx, cy, self._progress(), place or (0.0, 0.0))
            else:
                info = await self.s.terrain(anchor, cx, cy, self._progress())
        finally:
            pacer.foreground_end()
        progress["stage"] = "finalizing"
        key = (anchor.id, cx, cy)
        anchor.first_terrain = (cx, cy)
        self.s.anchors[anchor.id] = anchor
        self.s.anchors_preparing.pop(anchor.id, None)
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
        # in a tunnel: its floor under the ground, sent after (tunnel_floor_late: the mod moves the car down onto it once
        # the tube's collision is there). It was asked only at the origin of a full block, and since snapped anchors and
        # provisional first blocks (2026-10-25) a teleport into a tunnel left the car on the hill over it.
        floor_z = None
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
        progress["done"] = True
        self.send(ready)
        self.status(f"Terrain ready ({time.perf_counter() - t0:.1f} s)")
        if gz is not None and declared:     # (a player's teleport: not a rebase while driving, nor the level start)
            self.spawn(self.tunnel_floor_late(anchor, place or (0.0, 0.0), gz))
        if provisional:
            self.note_provisional(anchor, key, info)
            self.spawn(self.refine_first(anchor, cx, cy, info.get("ter"), t0))

    async def tunnel_floor_late(self, anchor, place, gz):
        """A teleport's spot in a tunnel: its floor (more than TUNNEL_FLOOR_UNDER_M under the ground gz there) sent as
        'tunnel_floor' (core.floorCheck sets the car down on it once the tube collides); nothing when none."""
        e, n = float(place[0]), float(place[1])
        fl = await asyncio.get_running_loop().run_in_executor(self.s.ui_pool, self.s.tunnel_floor, anchor, e, n)
        if fl is None or fl >= gz - TUNNEL_FLOOR_UNDER_M:
            return
        z = round(fl + gradingmod.ROAD_LIFT, 3)
        log.info("anchor %d: in a tunnel at (%.0f, %.0f), its floor %.1f under the ground %.1f", anchor.id, e, n, z, gz)
        self.send({"type": "tunnel_floor", "anchorId": anchor.id, "floorZ": z, "x": round(e, 3), "y": round(n, 3)})

    async def refine_first(self, anchor, cx, cy, provisional_ter, t0):
        """The graded first block (roads levelled, tunnels, the sea floor) in place of the provisional one: built as
        any block, sent as terrain_refresh (the mod swaps it in while that block is still its own). The provisional
        .ter is deleted a while later (the game may still be reading it)."""
        key = (anchor.id, cx, cy)
        self.refining.add(key)              # (the loading line's ground: send_progress)
        try:
            info = await self.s.terrain(anchor, cx, cy)
        except poolsmod.Abandoned:
            log.info("graded first block %s dropped: a teleport left it", key)
            return
        except Exception:
            log.exception("graded first block %s failed (the provisional one stays)", key)
            return
        finally:
            self.refining.discard(key)
        self.s.hm_sent.append(key)
        log.info("anchor %d: graded first block in %.2f s after the request", anchor.id, time.perf_counter() - t0)
        self.send({"type": "terrain_refresh", "anchorId": anchor.id, **self.terrain_msg(key, info)})
        olds = [provisional_ter] + self.graded_sent(key)
        grounds = self.s.ground_bases.pop(key, [])
        await asyncio.sleep(PROVISIONAL_KEEP_S)
        for t in olds:
            if t and t != info.get("ter"):
                self.s._remove(os.path.join(self.s.out_dir, t))
        for g in grounds:               # (its ground stand-in photos with their packs; kept if the graded one shows one)
            if g != info.get("base"):
                self.s._remove_base(g)

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
            self.pending_at = time.monotonic()
            self.pending_answered = False
            t1 = time.perf_counter()
            self.update_far()
            dt = time.perf_counter() - t1
            if dt > 0.1:
                (log.info if dt > 0.5 else log.debug)("terrain request %s: update_far %.2f s", (aid, cx, cy), dt)
        t0 = time.perf_counter()
        self.terrain_wait += 1
        try:
            job = asyncio.ensure_future(self.s.terrain(anchor, cx, cy))
            prov = await self.provisional_follow(anchor, cx, cy, job)
            if prov is not None:
                self.terrain_wait -= 1
                key = (aid, cx, cy)
                log.info("terrain_ready %s after %.0f ms (provisional: the graded block follows)", key,
                         (time.perf_counter() - t0) * 1000)
                self._answered(aid, cx, cy)
                self.send({"type": "terrain_ready", "requestId": req, "anchorId": aid, **self.terrain_msg(key, prov)})
                self.note_provisional(anchor, key, prov)
                return
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
        self._answered(aid, cx, cy)
        self.send({"type": "terrain_ready", "requestId": req, "anchorId": aid, **self.terrain_msg(key, info)})

    def _answered(self, aid, cx, cy):
        """The pending block's terrain_ready went: from now on its commit is the mod's to make (the player reaching
        its commit distance, its far tiles going live), which more server cores do not hasten (behind)."""
        if aid == self.active and self.pending_block == self.s.layout.block_at(cx, cy):
            self.pending_answered = True

    async def provisional_follow(self, anchor, cx, cy, job):
        """A follow block's provisional stand-in (PROVISIONAL_FOLLOW_S): None when the graded block came in time (or
        provisional blocks are off), else the provisional block's info, the graded one sent as terrain_refresh when
        built (refine_follow). One provisional build per block (Server.prov_info)."""
        s = self.s
        if PROVISIONAL_FOLLOW_S <= 0 or not PROVISIONAL_FIRST or not s.grading or anchor.id != self.active:
            return None
        key = (anchor.id, cx, cy)
        info = s.prov_info.get(key)     # (asked again: its provisional block at once)
        wait, planned, t_check = 0.0, False, time.perf_counter()
        if info is None:
            # at once where the block's road plans are not made yet (a first visit's cells: 15-120 s of planning);
            # planned, after PROVISIONAL_FOLLOW_S: the graded block is usually quick then, but a first visit's map data
            # (its roads round the block, read on demand) still held one 84 s (nyc_speed.py --cold, 2026-11-02).
            # Raced with the block itself: one built already (the prefetch) goes at once. The check runs on
            # prov_pool and may wait for a planner's lock while a cell is planned: blocks built 15-34 s before their
            # request were answered 2.6-8.8 s after it, each an edge hold at 68 mph (GitHub #725; 11 and 65 s here
            # under load, 2026-11-04). The check gets PROVISIONAL_FOLLOW_S at most: past that it counts as planned,
            # the wait spent.
            pf = asyncio.get_running_loop().run_in_executor(s.prov_pool, s.block_planned, anchor, cx, cy)
            done, _ = await asyncio.wait({job, pf}, timeout=PROVISIONAL_FOLLOW_S, return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                return None
            planned = True
            if pf in done:
                try:
                    planned = pf.result()
                except Exception:
                    pass
            else:
                def slow_check(f, key=key, t=t_check):
                    log.info("block %s: its plan check took %.1f s (a planner busy)", key, time.perf_counter() - t)
                pf.add_done_callback(slow_check)
            if planned:
                wait = max(0.0, PROVISIONAL_FOLLOW_S - (time.perf_counter() - t_check))
        done, _ = await asyncio.wait({job}, timeout=wait)
        if done:
            return None
        pf = s.hm_proc.get(key) if info is None and planned else None
        left = PROVISIONAL_RUNNING_S - (time.perf_counter() - t_check)
        if pf is not None and pf.running() and left > 0:
            # (planned and building already: a provisional block beside it took the CPU it needed, finished after it,
            # 150 mph down Midtown on a warm cache 2026-11-02; only if it runs long, a first visit's map data)
            done, _ = await asyncio.wait({job}, timeout=left)
            if done:
                return None
        if info is None:
            prov = asyncio.get_running_loop().run_in_executor(s.prov_pool, s.make_provisional_follow, anchor, cx, cy)
            done, _ = await asyncio.wait({job, prov}, return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                return None
            try:
                info = prov.result()
            except Exception:
                log.exception("provisional block %s failed (the graded one is waited for)", key)
                return None
            s.prov_info[key] = info
            self.spawn(self.refine_follow(anchor, cx, cy, job, info.get("ter")))
        return info

    async def refine_follow(self, anchor, cx, cy, job, provisional_ter):
        """The graded follow block in place of its provisional one (as refine_first): sent as terrain_refresh, which the
        mod takes for its committed block or the one it is about to commit."""
        key = (anchor.id, cx, cy)
        try:
            info = await job
        except Exception:
            log.exception("graded block %s failed (the provisional one stays)", key)
            return
        finally:
            self.s.prov_info.pop(key, None)
        self.s.hm_sent.append(key)
        log.info("graded block %s sent in place of its provisional one", key)
        self.send({"type": "terrain_refresh", "anchorId": anchor.id, **self.terrain_msg(key, info)})
        olds = [provisional_ter] + self.graded_sent(key)
        grounds = self.s.ground_bases.pop(key, [])
        await asyncio.sleep(PROVISIONAL_KEEP_S)
        for t in olds:
            if t and t != info.get("ter"):
                self.s._remove(os.path.join(self.s.out_dir, t))
        for g in grounds:               # (its ground stand-in photos with their packs; kept if the graded one shows one)
            if g != info.get("base"):
                self.s._remove_base(g)

    def commit_anchor(self, aid):
        if aid == self.active:
            return
        if self.teleport_pending:
            self.teleport_pending = False
            self.wake.set()
        self.abandoned_from = None          # (committed: the places left stay left)
        if getattr(self, "preparing_aid", None) == aid:
            self.preparing_aid = None
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
                fut.cancel()            # this connection's waiter; the producer keeps its slot until it finishes
        # Tiles sent but not swapped in are dropped by the mod at the anchor switch, but for the global ones (any
        # anchor's: the mod keeps them loading, placed into the new anchor; not after a resync, old is None)
        keep = {k: t for k, t in self.sent.items() if farfield.is_global_key(k)} if old is not None else {}
        self._retire(self.ck(k) for k in self.sent if k not in keep)
        self.active = aid
        self.ppolys = {}
        self.desired, self.sent, self.groups, self.queue, self.qi = {}, keep, [], [], 0
        self.graded_wait = []
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
        if self.map_view or self.map_open:
            # the 3D map at a new place: coarse first, then finer in steps as each is done (Conn._stream), as map apps
            # load; the finest first left the screen empty for seconds round the first tiles (2026-11-07)
            self.lod_ramp = MAP_RAMP_START
            self.zoom_ref = self.eye        # (the zoom rule's reference: this anchor's own ramp, not a second one)
            self.first_ramp = None
        else:
            # a teleport (driving, not the map's own ramp): the coarse cover of the whole horizon first (FIRST_RAMP)
            # (with global far tiles only: the cover is made of them; an older mod, past Web Mercator or terrain mode
            # load as before)
            self.first_ramp = time.monotonic() if FIRST_RAMP and anchor.ramp and self.global_far else None
        if self.first_ramp is None and not any(getattr(c, "first_ramp", None) is not None for c in self.s.connections
                                               if c is not self) and not self.s.anchors_preparing:
            self.s.coarse_keys.clear()      # (no coarse cover under way: every global tile at its own size)
        anchor.ramp = False                 # (once: another connection's commit of it, a reconnect, builds as usual)
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

    def behind(self):
        """Is the world behind the driving player? The next block asked CATCHUP_AFTER_S ago and not answered yet (still
        building here), or the road tiles round the car not all built. A block answered but not committed is not:
        the mod commits it when the player reaches its commit distance (and its far tiles are live), which more cores
        here do not hasten. Counting it lent half the game's cores to the server again and again in town: blocks
        answered 0-4 s after their request, the boost on 13-22 s after it, at 20-60 mph (Fells Point, 2026-11-07)."""
        if self.pending_block is not None and self.pending_block != self.block and self.pending_at is not None \
                and not getattr(self, "pending_answered", False) \
                and time.monotonic() - self.pending_at > CATCHUP_AFTER_S:
            return True
        if self.s.mapdata is not None:
            try:
                done, want = self.roads.progress(near_m=CATCHUP_ROADS_M)
            except TypeError:           # (a tracker without the radius)
                done, want = self.roads.progress()
            return done < want
        return False

    def hold_boost(self, holding):
        """While the edge guard holds the car for its ground (pose.guardHold), the server on every core and the game on
        its few (Server.split_boost, the full priority split): the game is paused then, and the block it waits for
        came 36 s after a first visit's start at 100 mph with the server's own cores, ~8 s with every core
        (nyc_speed.py --cold 2026-11-02). Back as before HOLD_BOOST_CALM_S after the hold ends."""
        now = time.monotonic()
        if holding:
            self.hold_seen = now
            if not getattr(self, "hold_split", False):
                self.hold_split = self.s.split_boost(True, "edge hold")
        elif getattr(self, "hold_split", False) and now - getattr(self, "hold_seen", 0.0) > HOLD_BOOST_CALM_S:
            self.s.split_boost(False, "hold over")
            self.hold_split = False

    def catch_up(self, speed):
        """The loading boost (Server.load_boost: half of the game's cores lent) while the world is behind a driving
        player, off CATCHUP_CALM_S after it caught up. Not over a teleport's own boost."""
        if CATCHUP_LEND_SHARE <= 0:         # (BNGE_CATCHUP_LEND=0: none)
            return
        if not getattr(self.s, "below_game", BELOW_GAME) and not CATCHUP_LEND_AT_NORMAL:
            # (the server at the game's own priority, "World server priority: normal": its workers on lent game cores
            # took the game's own time; a 6-core player's logs, 1% lows 30-38 fps, 37-44 with "low", Discord
            # 2026-11-06; the user, 2026-11-11: consistency over speed)
            return
        if self.catchup and not self.s.boost_on:
            self.catchup = False            # (ended elsewhere: free to start again)
            _catchup_note(self, False)
        if self.s.boost_on and not self.catchup:
            return
        behind = speed > CATCHUP_MIN_MS and self.behind()
        now = time.monotonic()
        if behind:
            self.caught_up_at = None
            if not self.catchup:
                self.s.load_boost(True, "catching up", half_only=True)
                self.catchup = self.s.boost_on
                self.catchup_since, self.catchup_more = now, False
                if self.catchup:
                    _catchup_note(self, True)
                    log.info("catch-up boost on (%.0f mph; next block asked %.1f s ago)", speed * 2.237,
                             now - self.pending_at if self.pending_at else -1)
            elif not getattr(self, "catchup_more", False) and now - getattr(self, "catchup_since", now) > CATCHUP_MORE_S \
                    and self.s.boost_cpus and not getattr(self.s, "split_on", False):
                # (behind all along: the larger lend, cpupin.boosted's half. A first visit at 100 mph on one core more:
                # the road tiles were never ahead of the car, 216 of 232 polls, nyc_speed.py 2026-11-02)
                try:
                    cpupin.pin_tree(self.s.boost_cpus)
                    self.catchup_more = True
                    log.info("catch-up boost: more cores (behind for %.0f s): CPUs %s", now - self.catchup_since,
                             ",".join(map(str, sorted(self.s.boost_cpus))))
                except OSError as e:
                    log.warning("catch-up boost: %s", e)
                    self.catchup_more = True
        elif self.catchup:
            if self.caught_up_at is None:
                self.caught_up_at = now
            elif now - self.caught_up_at > CATCHUP_CALM_S:
                self.s.load_boost(False, "caught up")
                self.catchup, self.catchup_more = False, False
                _catchup_note(self, False)
                log.info("catch-up boost off (caught up)")

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
        lease = getattr(self, "lease", None)
        if lease is not None and lease.connection is not self:
            return
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
            if streamleases.integer(n) and streamleases.integer(serial) and self.slot_holds.get(int(n)) == serial:
                del self.slot_holds[int(n)]
                let_go.append(int(n))
        self.s.slots_let_go(let_go)

    def far_loading(self):
        """Is the far field round the active anchor still being built or sent (far_done not owed yet)?"""
        return bool(self.dirty or self.inflight)

    def on_pose(self, msg):
        self.slots_gone(msg.get("slotsGone"))
        held = msg.get("held")
        if isinstance(held, dict):      # an empty Lua table may encode as {}
            held = list(held.values())
        if isinstance(held, list):
            self.held = {int(a) for a in held if isinstance(a, (int, float)) and not isinstance(a, bool)}
        self.s.claim_sessions(self, msg.get("sessions"))   # earlier runs' folders still in use (or released)
        self.weather.on_pose(self, msg.get("lat"), msg.get("lon"))
        feed = getattr(self.s, "feed", None)
        if feed is not None:            # the companion feed (companionfeed.py)
            feed.publish(msg.get("lat"), msg.get("lon"), msg.get("speed"), msg.get("vx"), msg.get("vy"),
                         msg.get("anchorId"))
        if isinstance(msg.get("lat"), (int, float)) and isinstance(msg.get("lon"), (int, float)):
            self.pose_ll = (float(msg["lat"]), float(msg["lon"]))     # (the BeamMP share hub's prefetch)
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
        now = time.monotonic()
        self.s.speed = math.hypot(vx, vy)
        if math.hypot(vx, vy) > FAST_BASE_V:
            self.s.fast_at = now
        self.s.fast = now - getattr(self.s, "fast_at", -1e9) < FAST_KEEP_S
        self.hold_boost(msg.get("guardHold") is True)
        self.catch_up(math.hypot(vx, vy))
        if self.s.boost_on and not self.catchup:
            since = time.monotonic() - getattr(self, "boost_t0", 0.0)
            moving = math.hypot(vx, vy) > BOOST_MOVE_MS and since > BOOST_GRACE_S     # (the car settling: not moving)
            if moving and self.s.mapdata is not None:
                # (not while the roads round the car are still being built: a 2-core server's boost ended as the
                # player drove off on bare ground, its road tiles minutes late, GitHub #412)
                done, want = self.roads.progress()
                moving = done >= want
            if self.s.boost_full:
                # (full priority: until the far field and the roads round the car are done, moving or not)
                done, want = self.roads.progress() if self.s.mapdata is not None else (1, 1)
                # (its own far field done since the teleport: right after it nothing was loading yet, ended in 0.7 s)
                loaded = getattr(self, "far_done_at", 0.0) > getattr(self, "boost_t0", 0.0) and not self.far_loading()
                if (loaded and done >= want) or since > BOOST_FULL_MAX_S:
                    self.s.load_boost(False, "loaded" if since <= BOOST_FULL_MAX_S else "timeout")
            elif moving or since > BOOST_MAX_S:
                self.s.load_boost(False, "moving" if moving else "timeout")
        if isinstance(msg.get("cam"), dict):
            self.cam, self.cam_aid = msg["cam"], aid
        self.view_at = self.camera_away(msg.get("cam")) if aid == self.active else None
        self.foci = parse_foci(msg.get("foci")) if aid == self.active else ()
        if self.s.horizon:
            self.update_eye(self.s.anchors[aid].frame, *(self.view_at or (x, y)), msg)
        hint = getattr(self, "focus_hint", None)
        if hint is not None:
            if hint[0] == aid == self.active and time.monotonic() < hint[4]:
                # (the 3D map flying there: the far field built round where the view will end, at its detail)
                self.view_at, self.eye = (hint[1], hint[2]), hint[3]
            elif time.monotonic() >= hint[4]:
                self.focus_hint = None
        self.gravity.on_pose(self, self.s.anchors[aid].frame, x, y, _num(msg, "z", 0.0))
        self.ground_ahead(aid, x, y, vx, vy)
        if self.s.sea is not None:
            self.sea_tiles(aid, x, y)
        # the mod's altitude rule: high above the ground it does not move its block, so no block is
        # built ahead (prefetch, road plans) until it comes down
        self.following = msg.get("follow") is not False
        self.map_view = msg.get("view") is True     # (the 3D map's view frame: mod core st.viewFrame)
        self.map_open = msg.get("map") is True      # (the 3D map open, near the car or not: map3d.isActive)
        # the 3D map open: every core for the server (cpupin.full_split, the game on its first cores: it draws the map
        # and runs no physics there), so the ten far-tile processes are not squeezed onto the server's four CPUs
        # (2026-11-07: a fresh city's first far tile came 19 s after the flight began)
        # (asked again on every pose while it is not ours: full priority loading or an edge hold has the split, and
        # theirs ends while the map is still open)
        want = self.map_view or self.map_open
        if want and not getattr(self, "map_split", False) and time.monotonic() >= getattr(self, "map_split_try", 0.0):
            self.map_split_try = time.monotonic() + 2.0
            self.map_split = self.s.split_boost(True, "3D map")
        elif not want and getattr(self, "map_split", False):
            self.map_split = False
            self.s.split_boost(False, "3D map closed")
        self.predicted_block = None
        tree = self.tree = self.reach_tree(aid) if aid == self.active else None
        self.tree_led = False           # (the blocks predicted along the tree: Server.block_unwanted)
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
            at = self.predicted_at = {}     # (centre -> the player where the mod asks for it: maybe_prefetch)
            if max(abs(qx - base.cx), abs(qy - base.cy)) >= rule.prefetch_start:
                where = []
                c = predict_request(px, py, *self.vel, base.cx, base.cy, rule, where)
                if c != (base.cx, base.cy) and self.s.layout.block_at(*c) != self.block:
                    self.predicted_block = self.s.layout.block_at(*c)
                    at[c] = where[0]
            self.predicted_chain = []
            if math.hypot(*self.vel) > FAST_BASE_V:
                route_at = {}
                cs = predict_route_chain(self.route_xy(aid), px, py, *self.vel, base.cx, base.cy, rule,
                                         PREFETCH_CHAIN + 1, route_at)
                if not cs and tree is not None and len(tree.mpp) >= 2:
                    # (no GPS route: along the time-to-reach tree's most probable path, the road's curves and the
                    # junctions' likeliest ways, not a straight line: block_unwanted drops what it no longer leads to)
                    route_at = {}
                    cs = predict_route_chain(tree.mpp_xy(), px, py, *self.vel, base.cx, base.cy, rule,
                                             PREFETCH_CHAIN + 1, route_at)
                    self.tree_led = bool(cs)
                if cs:              # (on the GPS route: along it; its first is the next block, as predicted_block)
                    at.update(route_at)
                    if self.predicted_block is not None:
                        self.predicted_block = self.s.layout.block_at(*cs[0])
                    cs = cs[1:]
                else:
                    cs = predict_chain(px, py, *self.vel, base.cx, base.cy, rule, PREFETCH_CHAIN, at)
                self.predicted_chain = [self.s.layout.block_at(*c) for c in cs]
        if (self.map_view or self.map_open) and self.eye:
            # the 3D map zoomed far in (the user, 2026-11-07: "no matter where i am it should only take a couple seconds
            # to see detailed terrain"): coarse first there too, as at a new anchor (commit_anchor), not the finest of
            # ~360 tiles first and the screen filling in 15 s (Minneapolis, no origin move)
            ref = getattr(self, "zoom_ref", None)
            if ref is None or self.eye > ref:
                self.zoom_ref = self.eye
            elif self.eye < ref / MAP_ZOOM_RAMP_K and getattr(self, "lod_ramp", 1.0) >= 1.0:
                self.lod_ramp, self.zoom_ref = MAP_RAMP_START, self.eye
                log.info("3D map: zoomed in x%.0f, coarse first", ref / self.eye)
        self.update_far()
        self.pvs_tick()                     # (the far PVS for where the player is now; after the selection: its radius)
        self.far_lazy_tick()
        if self.teleport_pending and time.monotonic() - self.teleport_t0 > TELEPORT_PAUSE_MAX_S:
            self.teleport_pending = False   # the mod never committed it (it stayed): the old place's work goes on
            self.unabandon_old()
            self.wake.set()
        if self.teleport_pending:           # the old place's work waits: the new place has the CPU and the network
            self.refresh_status()
            return
        self.maybe_prefetch()
        self.send_ready_set()
        if aid == self.active and self.following:
            self.s.warm_roads(self.s.anchors[aid], *self.focus, *self.vel, tree=tree)
            self.s.warm_imagery(self.s.anchors[aid], *self.focus, *self.vel, tree=tree)
            self.s.download_ahead(self.s.anchors[aid], *self.focus, *self.vel, tree=tree)
            self.s.bake_sync(self.s.anchors[aid], *self.focus)
            self.base_refresh(aid)
            self.pads_refresh(aid)
            self.roads_refresh(aid)
            self.prov_refresh(aid)
        # (the 3D map's view frame high up, pose.view: no roads, trees, buildings, turbines or traffic round its target,
        # built as round a car: none of them shows from there, and their builds held the far tiles back and stalled the
        # event loop 2.1 s over Turin, 2026-11-07; nearer the ground they come, as what the view shows)
        detail = not (self.map_view and self.eye_clearance() > MAP_DETAIL_EYE_M)
        if self.s.mapdata is not None and aid == self.active and detail:
            if getattr(self.roads, "geo", False):
                anchor = self.s.anchors.get(aid)
                self.roads.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_roads_geo,
                                   vel=self.vel, ground_busy=self.terrain_wait > 0, view=self.view_at,
                                   foci=self.foci, frame=anchor.frame if anchor is not None else None, tree=tree)
                # (the sign tiles stay the anchor's: each once the road geotiles under it are in)
                ready = self.roads.ready_tiles(anchor.frame, *self.focus, roadsmod.SEND_M) if anchor is not None \
                    else set()
            else:
                self.roads.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_roads_tile,
                                   vel=self.vel, ground_busy=self.terrain_wait > 0, view=self.view_at,
                                   foci=self.foci)
                ready = self.roads.sent
            self.s.osm_fuel_want(self, aid, getattr(self.roads, "car", ()))   # (OpenStreetMap's fuel stations)
            source = getattr(self.s.mapdata, "highway_signs", None)
            if source is not None and self.signs_on:      # (after the roads: signs only for road tiles sent)
                self.signs.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_signs_tile,
                                   ready, version=(source.tags.known(),
                                       self.s.mapdata.nl_rvv.known() if getattr(self.s.mapdata, 'nl_rvv', None) else None))
        if self.turbine_geo and aid == self.active and detail:
            anchor = self.s.anchors.get(aid)
            if anchor is not None:
                self.turbines.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_turbines_geo,
                                      view=self.view_at, frame=anchor.frame)
        if self.s.veg is not None and aid == self.active and detail:
            anchor = self.s.anchors.get(aid)
            if anchor is not None:                            # a teleport's cells built at once, ahead
                la, lo, _ = anchor.frame.enu_to_geodetic(*self.focus, 0.0)
                self.s.veg.warm_around(float(la), float(lo))
            self.veg.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                             self.build_veg_geo if self.veg.geo else self.build_veg_tile,
                             view=self.view_at, foci=[f for f in self.foci if f[3]],
                             frame=anchor.frame if anchor is not None else None)
        bld_on = not (self.map_view and self.eye_clearance() > MAP_BUILDINGS_EYE_M)
        if self.s.buildings and aid == self.active and bld_on:
            self.building_reach()
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
        if self.s.traffic and aid == self.active and not self.map_view and self.nav_may_start(aid):
            # (centred ahead of a moving car: the minimap's roads, the AI's and the GPS's graph, ran out in front at
            # speed, a graph built round where the car was when it started; the user, 2026-11-04)
            vx, vy = getattr(self, "vel", (0.0, 0.0))
            lead = min(NAV_LEAD_S * math.hypot(vx, vy), NAV_LEAD_MAX_M)
            sp = max(math.hypot(vx, vy), 1e-6)
            cx, cy = self.focus[0] + vx / sp * lead, self.focus[1] + vy / sp * lead
            self.nav.on_pose(self, aid, cx, cy, asyncio.get_running_loop(), self.s.build_navgraph)
        if self.traffic_on and aid == self.active and not self.map_view and self.nav_may_start(aid):
            self.distant_traffic.on_pose(self, aid, *self.focus, asyncio.get_running_loop(), self.s.build_traffic_geo,
                                         view=self.view_at, frame=self.s.anchors[aid].frame)
            done, want = self.distant_traffic.progress()
            if done >= want / 2:
                self.distant_traffic_far.on_pose(self, aid, *self.focus, asyncio.get_running_loop(),
                                                functools.partial(self.s.build_traffic_geo, far=True),
                                                view=self.view_at, frame=self.s.anchors[aid].frame)
        if aid == self.active:
            self.send_progress(aid)
        self.refresh_status()
        self.credits.poke(self)             # the player moved: the near / far credits may change

    def reach_tree(self, aid):
        """The car's time-to-reach tree for this pose (reach.Tree), or None: the switch off (REACH_ON), no map data or
        native kernel, slower than reach.MIN_V, the block not following the car, the 3D map open, or no road under the
        car. The cells of the graph round the car come in the reach thread (one round at a time)."""
        sp = math.hypot(*self.vel)
        if not REACH_ON or self.s.mapdata is None or sp < reachmod.MIN_V or not self.following or self.map_view \
                or self.map_open or not reachmod.available():
            return None
        anchor = self.s.anchors.get(aid)
        if anchor is None:
            return None
        r = getattr(self, "reach", None)
        if r is None or r.aid != aid:
            r = self.reach = reachmod.Reach(self.s.mapdata, anchor.frame, aid)
        if not r.busy:
            r.busy = True
            x, y, (vx, vy) = self.focus[0], self.focus[1], self.vel

            def run():
                try:
                    r.update_cells(x, y, vx, vy)
                except Exception:
                    log.exception("reach: the road graph's cells")
                finally:
                    r.busy = False
            self.s.reach_pool.submit(run)
        try:
            return r.grow(*self.focus, *self.vel, route_xy=self.route_xy(aid))
        except Exception:
            log.exception("reach: the time-to-reach tree")
            return None

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

    def route_xy(self, aid):
        """The last planned GPS route's line in anchor aid's frame ((k, 2), kept per route and anchor), or None."""
        pts = self.route_points
        anchor = self.s.anchors.get(aid)
        if not pts or anchor is None or len(pts) < 2:
            return None
        got = getattr(self, "_route_xy", None)
        if got is not None and got[0] == aid and got[1] is pts:
            return got[2]
        ll = np.asarray(pts, np.float64)
        e, n_, _ = anchor.frame.geodetic_to_enu(ll[:, 0], ll[:, 1], 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n_, np.float64)], 1)
        self._route_xy = (aid, pts, xy)
        return xy

    def camera_away(self, cam):
        """The point a pose's cam is about when it is more than VIEW_AWAY_M from the car, else None: the 3D map's
        orbit target (tx / ty) when the pose carries one, otherwise the camera's own (x, y) (a free camera, a far
        chase camera). What is drawn follows it: the far field, and the road, tree and far building tiles
        round it as well as round the car; what collides stays round the car (the terrain block, the road and
        building colliders in the tiles round the car, the nav graph). The user, 2026-10-20: "All collision ...
        should be following the car while visual stuff should follow the camera"."""
        if not isinstance(cam, dict):
            return None
        cx, cy = cam.get("x"), cam.get("y")
        if cam.get("tx") is not None and cam.get("ty") is not None:
            cx, cy = cam.get("tx"), cam.get("ty")       # (the 3D map's target on the ground: mod map3d.lua)
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
        # (a skipped photo (a fast player's prefetched block): at once, its imagery is fetched as it renders)
        key = next((k for k in cands if k in s.hm and (k in s.base_skipped or s.base_ready(anchor, k[1], k[2]))), None)
        if key is None:
            return
        self.base_busy = True

        async def run():
            try:
                # its BC7 encode: in the photo process when there is one (in the prefetch lane's, a block ahead waited
                # behind it and came late, 2026-11-05)
                pool = getattr(s, "photo_pool", None) or (s.block_pools[LANE_PREFETCH][0] if s.block_pools else None)
                rev = next(s.base_rev)
                name = None
                if pool is not None:
                    # (in the block process: from the finished tiles with the analyses on; they were all rendered
                    # here, 121 of a warm 8 min drive at 150 mph, beside the event loop, 2026-11-04)
                    name = await self.loop.run_in_executor(s.hm_pool, s._base_elsewhere, anchor, key[1], key[2],
                                                           pool, [], False, rev)
                if name is None:
                    name = await self.loop.run_in_executor(s.hm_pool, s.write_base, anchor, key[1], key[2], pool,
                                                           rev)
                info = s.hm.get(key)
                if name is None or info is None:
                    return
                old, info["base"] = info.get("base"), name
                if old and old != name and old != s.base_name(key):
                    # (kept until the block goes, prune_hm, with its pack: the mod may still show it a while, its
                    # refresh deferred at speed (core BASE_LATER_MS), and a material rebuild then needs the pack;
                    # deleted here, the game packed the photo again on its main thread, ~0.75 s frames, 2026-11-03)
                    info.setdefault("old_bases", []).append(old)
                log.info("base photo of %s rendered again with its NAIP (%s)", key, name)
                s.note_base(key, name)
                self.send({"type": "block_base", "anchorId": key[0], "cx": key[1], "cy": key[2],
                           "base": f"{s.vfs_dir}/{name}", "baseSize": size})
                s.base_skipped.discard(key)
            except Exception:
                log.exception("base photo refresh %s", key)
                s.base_stale.discard(key)
                s.base_skipped.discard(key)
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
        need = s.roads_need.get(key, "landcover")
        if not all(s.mapdata.has(k, "roads") and (need == "roads" or s.surface is None or s.mapdata.has(k, "landcover"))
                   for k in cells):
            return                          # (not here yet: asked again at the next pose)
        self.roads_busy = True
        s.roads_rebuild.discard(key)

        async def run():
            old = s.hm.pop(key, None)
            try:
                # (the prefetch lane's process: a correction of the block in use must not hold up the next one the
                # mod asks for, a first visit's rebuilds queued the follow requests, 2026-11-01)
                info = await s.terrain(anchor, key[1], key[2], lane=LANE_PREFETCH)
            except poolsmod.Abandoned:          # (a teleport left it: the old one stays, quietly)
                if old is not None:
                    s.hm[key] = old
                return
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

    def note_provisional(self, anchor, key, info):
        """A provisional block sent (the first after a teleport, or a follow block): kept for prov_refresh when it was
        built before its road or land cover cells were on disk."""
        if not L0_REFRESH or self.s.mapdata is None or not info:
            return
        if info.get("roadsComplete") is False or info.get("surfaceComplete") is False:
            self.prov_live[key] = {"anchor": anchor, "ter": info.get("ter"), "since": time.monotonic(), "olds": []}
            self.prov_last = key
            log.info("provisional block %s sent before its map cells (roads %s, surface %s): built again when they "
                     "come", key, info.get("roadsComplete"), info.get("surfaceComplete"))

    def graded_sent(self, key):
        """The graded block went for key: no provisional refresh after it (one being built is dropped); the .ter names
        its refreshes wrote (deleted with the provisional one's)."""
        e = self.prov_live.pop(key, None)
        if e is None:
            return []
        e["graded"] = True
        return list(e["olds"]) + ([e["ter"]] if e.get("refreshed") else [])

    def prov_refresh(self, aid, now=None):
        """A provisional block sent before its road / land cover cells were on disk (note_provisional), the graded
        block not sent yet: built again, provisional (blockworker.build: the cached roads painted in and laid flat,
        l0roads.py), once those cells are here, and sent as terrain_refresh (the mod swaps it in while that block is
        its own or pending). Once per block, one at a time; a cold teleport's first block had bare ground under its
        roads until the graded block 60-120 s later (Pittsburgh, 2026-11-11)."""
        s = self.s
        if self.prov_busy or not self.prov_live or s.mapdata is None:
            return
        now = time.monotonic() if now is None else now
        for key, e in list(self.prov_live.items()):
            if key[0] != aid or now - e["since"] > L0_REFRESH_MAX_S:
                self.prov_live.pop(key, None)       # (another anchor's, or long due its graded block)
                continue
            if e.get("refreshed"):
                continue
            if key not in list(s.hm_committed)[-1:] and key != self.prov_last:
                continue                            # (a block the car has left: the mod would drop it)
            anchor = s.anchors.get(aid)
            if anchor is None:
                return
            half = s.layout.block * heightmap.SQUARE / 2
            la, lo, _ = anchor.frame.enu_to_geodetic(np.array([key[1] - half, key[1] + half]),
                                                     np.array([key[2] - half, key[2] + half]), 0.0)
            cells = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)),
                                            float(np.max(lo)))
            if not all(s.mapdata.has(k, "roads") and (s.surface is None or s.mapdata.has(k, "landcover"))
                       for k in cells):
                continue                            # (not here yet: asked again at the next pose)
            self.prov_busy = True
            e["refreshed"] = True
            self.spawn(self._prov_refresh_run(anchor, key, e))
            return

    async def _prov_refresh_run(self, anchor, key, e):
        s = self.s
        try:
            info = await self.loop.run_in_executor(s.prov_pool, s.remake_provisional, anchor, key[1], key[2])
        except Exception:
            log.exception("provisional block %s again with its roads (the old one stays)", key)
            return
        finally:
            self.prov_busy = False
        base = s.base_shown.get(key)
        if base and base in s.ground_bases.get(key, ()) and not e.get("graded"):
            # (the ground stand-in shown: made again with the roads and land cover now in its layers)
            base = await self.loop.run_in_executor(s.prov_pool, s.write_ground_base, anchor, key[1], key[2],
                                                   info) or base
        if e.get("graded") or (base and not os.path.exists(os.path.join(s.out_dir, base))):
            # (the graded block went meanwhile: it is the better one; or the photo shown is gone: not without one)
            s._remove(os.path.join(s.out_dir, info.get("ter") or ""))
            log.info("provisional block %s built again but not sent (%s)", key,
                     "the graded one went first" if e.get("graded") else "its photo is gone")
            return
        if base:
            info["base"] = base
        old = e["ter"]
        e["olds"].append(old)
        e["ter"] = info.get("ter")
        if key in s.prov_info:          # (a follow block asked again gets this one)
            s.prov_info[key] = info
        log.info("provisional block %s sent again with its roads, %.1f s after it was first sent (L0 roads: %s "
                 "samples in %.0f ms)", key, time.monotonic() - e["since"], int(info.get("stages", {}).get("l0Samples", 0)),
                 1000 * info.get("stages", {}).get("l0roads", 0.0))
        self.send({"type": "terrain_refresh", "anchorId": key[0], **self.terrain_msg(key, info)})

    def pads_refresh(self, aid, now=None):
        """The block in use built again (terrain_refresh, as refine_first) when gas station pads came in after it was
        built (Server.pads_rebuild: its surface layers then have the forecourt's asphalt, no grass on it; the user,
        2026-10-24: "looks like we painted it but it is still dirt"). Only with the car slower than PADS_REFRESH_MS
        (driving on, the next blocks are built with them) and PADS_SETTLE_S after the last new pad (a tile burst
        brings several); one at a time."""
        s = self.s
        now = time.monotonic() if now is None else now
        if not PADS_REFRESH_IN_USE:
            return
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
                # (the prefetch lane's process: a correction of the block in use must not hold up the next one the
                # mod asks for, a first visit's rebuilds queued the follow requests, 2026-11-01)
                info = await s.terrain(anchor, key[1], key[2], lane=LANE_PREFETCH)
            except poolsmod.Abandoned:          # (a teleport left it: the old one stays, quietly)
                if old is not None:
                    s.hm[key] = old
                return
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

                def low_roads():    # the roads under the sea round the player (seatiles.low_points): their tiles split
                    md = sea.md
                    r = seatilesmod.LOW_ROAD_RADIUS_M
                    la0, lo0, _ = frame.enu_to_geodetic(x - r, y - r, 0.0)
                    la1, lo1, _ = frame.enu_to_geodetic(x + r, y + r, 0.0)
                    try:
                        every = mapcells.cells_for_bbox(float(la0), float(lo0), float(la1), float(lo1), max_cells=256)
                        keys = tuple(key for key in every if os.path.exists(md.path(mapdatamod._key(key), "roads")))
                        if len(keys) < len(every):
                            missing[0] = True
                        # a cell's road points every LOW_ROAD_STEP_M and the raw DEM there, made once per cell for the
                        # anchor (lower Manhattan's 9 cells: 15,000 lines, 162,000 points, ~3.5 s to read and sample)
                        memo = getattr(self, "_sea_low_cells", None)
                        if memo is None or memo[0] != aid:
                            memo = self._sea_low_cells = (aid, {})
                        cells = memo[1]
                        for key in keys:
                            if key in cells:
                                continue
                            lines = []
                            f = md.cell(key, layers=("roads",)).layers.get("roads")
                            sub = None if f is None else f.attrs.get("subtype")
                            for i in range(0 if f is None else len(f)):
                                if sub is not None and str(sub[i]) != "road":
                                    continue
                                lines += roadsmod.ground_parts(f, i) + roadsmod.tunnel_parts(f, i)
                            pts = seatilesmod.road_points(frame, lines)
                            la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
                            raw = np.asarray(dem.sample(np.asarray(la, np.float64).reshape(-1),
                                                        np.asarray(lo, np.float64).reshape(-1),
                                                        seatilesmod.LAKE_DEM_ZOOM), np.float64).reshape(-1)
                            if np.isfinite(raw).all():      # (a DEM tile missing: read again next time)
                                cells[key] = (pts, raw)
                        for key in [k for k in cells if k not in keys][:max(0, len(cells) - LOW_ROAD_CELLS)]:
                            del cells[key]
                        got = [cells[key] for key in keys if key in cells]
                        pts = np.concatenate([p for p, _ in got]) if got else np.zeros((0, 2))
                        raw = np.concatenate([r for _, r in got]) if got else np.zeros(0)

                        def sea_at(la, lo):
                            return sea.sea_at(dem, la, lo, seatilesmod.SEA_TILE_FLOOR, False)

                        def low_ground(la, lo):
                            # graded under the roads where the road processes have planned already (planned_only:
                            # this thread plans no cells itself: lower Manhattan's 2 km round the player, every cell
                            # planned for it, held it 46 s on a first visit)
                            z = dem.sample(la, lo, seatilesmod.LAKE_DEM_ZOOM)
                            return grader.apply(la, lo, z, planned_only=True) if grader is not None else z
                        return seatilesmod.low_points(frame, x, y, pts, low_ground, sea_at, raw)
                    except Exception as e:      # none: the tiles as before
                        log.debug("sea tiles' low roads: %s", e)
                        return None

                def plan():
                    pacer.hold()        # (a teleport's first block first)
                    asked = sea.warm_asked
                    # with the cells at hand (the others are built in the background and used by the
                    # next plan, SEA_CHECK_S later): water appears at once at a new place
                    tiles = (seatilesmod.plan(frame, x, y, lambda la, lo, size: sea.sea_at(dem, la, lo, size, False),
                                              ground_at=sea_ground,
                                              low_roads_xy=low_roads if seatilesmod.LOW_ROADS_ON else None)
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
                tiles, rivers, shores, complete = await self.loop.run_in_executor(self.s.sea_pool, plan)
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
        self.eye_now = h                    # (exact: the far PVS's eye check, pvs_tick)
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
            self._lod_grid = grid
            if zr != self.zmax_region or abs(radius - self.radius) > RADIUS_REPORT_REL * self.radius:
                log.info("horizon: eye %.0f m, highest visible terrain %.0f m, radius %.0f km",
                         eye, zr, radius / 1000)
            self.zmax_region = zr
        self.radius = radius
        if self.radius_sent is None or abs(radius - self.radius_sent) > RADIUS_REPORT_REL * self.radius_sent:
            self.status(self.last_status or f"Horizon {radius / 1000:.0f} km")
        return radius, {"max_level": self.s.max_level, "disc": True, "horizon": cull}

    def send_ready_set(self):
        """'terrain_ready_set' {anchorId, centres}: the blocks of the active anchor built already, within READY_SET_M of
        the player (the mod asks for one of them instead of its own target when it is close: follow.preferReady), when
        that set changed."""
        aid = self.active
        if aid is None or not self.s.fast or not getattr(self, "ready_set", False):   # (at speed: there the guesses
            return                                                                  # miss; a mod that takes it)
        px, py = self.focus
        cs = sorted((cx, cy) for (a, cx, cy) in list(self.s.hm) if a == aid
                    and max(abs(cx - px), abs(cy - py)) <= READY_SET_M)
        if cs != getattr(self, "ready_sent", None):
            self.ready_sent = cs
            self.send({"type": "terrain_ready_set", "anchorId": aid, "centres": [[float(cx), float(cy)] for cx, cy in cs]})

    def maybe_prefetch(self):
        """Speculatively build the terrain the mod is about to ask for: the predicted block, and a fast player's chain
        after it (PREFETCH_CHAIN), in order, PREFETCH_PARALLEL at once."""
        anchor = self.s.anchors.get(self.active)
        if anchor is None:
            return
        want = [b for b in [self.predicted_block] + list(getattr(self, "predicted_chain", ())) if b is not None]
        busy = getattr(self, "prefetch_busy", None)
        if busy is None:
            busy = self.prefetch_busy = set()
        at = getattr(self, "predicted_at", {})
        now = time.monotonic()
        wat = self.__dict__.setdefault("chain_wanted_at", {})
        for b in want:                  # (Server.block_unwanted)
            wat[(b.cx, b.cy)] = now
        if len(wat) > 256:
            self.chain_wanted_at = {k: t for k, t in wat.items() if now - t <= 60.0}
        for b in want:
            if len(busy) >= PREFETCH_PARALLEL:
                return
            key = (anchor.id, b.cx, b.cy)
            if key in busy or self.s.terrain_cached(key):
                continue
            if self.ready_will_do(anchor, b, at.get((b.cx, b.cy)), busy):
                continue
            busy.add(key)
            self.prefetching = True

            async def run(cx=b.cx, cy=b.cy, key=key):
                try:
                    await self.s.terrain(anchor, cx, cy, lane=LANE_PREFETCH)
                    log.debug("prefetched terrain (%d, %.0f, %.0f)", anchor.id, cx, cy)
                except poolsmod.Abandoned:
                    pass                        # (a teleport left it, or the car went on: not wanted any more)
                except Exception:
                    log.exception("terrain prefetch failed")
                finally:
                    busy.discard(key)
                    self.prefetching = bool(busy)
            self.spawn(run())

    def ready_will_do(self, anchor, b, where, busy):
        """Would the mod take a block built (or being built) already instead of predicted block b (follow.lua
        preferReady: within READY_STEPS grid steps of its target, the player within reach of it where it asks)? Then b
        is not built: each pose's prediction a grid step off the last built another block, 55 built for 24 asked for
        at 150 mph down Midtown (nyc_speed.py 2026-11-02)."""
        if where is None:
            return False
        rule = self.s.follow
        _lead, reach, _ahead = rule.mode(*self.vel)
        lim = READY_STEPS * rule.grid
        cur = (self.block.cx, self.block.cy) if self.block is not None else None
        keys = [k for k in list(self.s.hm) + list(self.s.hm_inflight) + list(busy) if k[0] == anchor.id]
        for _a, kx, ky in keys:
            if (kx, ky) == (b.cx, b.cy) or (kx, ky) == cur:
                continue
            if max(abs(kx - b.cx), abs(ky - b.cy)) <= lim and max(abs(where[0] - kx), abs(where[1] - ky)) <= reach:
                return True
        return False

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

    def prebuild_coarse(self, anchor):
        """A teleport's coarse cover (FIRST_RAMP: the global far tiles round anchor.prebuild_focus, as the commit's
        first selection will choose them) built ahead, while its first block is (prepare_anchor), nearest first; a
        second call (its horizon grid in: the radius to the horizon) adds the rest. Returns the builds started."""
        focus = getattr(anchor, "prebuild_focus", None)
        if focus is None or not FIRST_RAMP or not self.global_far or self.map_view or self.map_open:
            return 0
        extra = {}
        radius = self.s.radius
        if self.s.horizon:
            grid = self.s.horizon_grids.get(anchor.id)
            eye = self.eye if self.eye is not None else 0.0
            radius = farfield.HORIZON_MIN if grid is None else grid.radius(*focus, eye)[0]
            extra = {"max_level": self.s.max_level, "disc": True,
                     "horizon": None if grid is None else farfield.Horizon(eye, grid)}
        if self.view_cap is not None:
            radius, extra = min(radius, self.view_cap), dict(extra, disc=True)
        # (the far PVS's rings are made at the commit, not here: beside the first block they held it 2.3 -> 7 s at a
        # cold Zermatt, their DEM fetches and this interpreter, 2026-11-11)
        if not self.global_on(anchor, radius):
            return 0
        k0 = self.kfun()
        new, _ = farfield.select_global(anchor.frame, *focus, anchor.zf, [], set(), lambda lvl: k0(lvl) * FIRST_RAMP_K,
                                        self.s.hysteresis, radius, min_level=self.s.min_level, radii=self.s.near_radii,
                                        layout=self.s.layout, forced_level=self.s.forced_level, view=None,
                                        anchor_nodes=False, **extra)
        self.s.coarse_keys.update(new)
        n = 0
        for t in farfield.nearest_first(list(new.values()), *focus):
            ck = self.s.far_ck(anchor.id, t.key)
            if ck not in self.s.tiles and ck not in self.s.tile_inflight:
                self.s.tile(anchor, t, shield=False)
                n += 1
        log.info("anchor %d: %d coarse far tiles built ahead (%d chosen, radius %.0f km)", anchor.id, n, len(new),
                 radius / 1000)
        return n

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
        t_sel = time.perf_counter()
        forced, kfun, min_level, view = self.forced(), self.kfun(), self.s.min_level, self.view()
        if self.first_ramp is not None:     # a teleport's coarse cover first (FIRST_RAMP)
            k0 = kfun
            forced, kfun, view = [], (lambda lvl: k0(lvl) * FIRST_RAMP_K), None
            min_level = max(min_level, FIRST_RAMP_MIN_LEVEL)
        if self.global_on(anchor, radius):
            # far levels from 3 up as global tiles (geotiles phase 3b): the same for every anchor, kept at a rebase
            high = self.eye is not None and self.eye > MAP_HIGH_EYE_M
            eye_h = self.eye_clearance()
            if high:
                self.s.start_world_warm(anchor)
            new, refined = farfield.select_global(anchor.frame, *self.view_focus, anchor.zf, forced,
                                                  self.refined, kfun, self.s.hysteresis, radius,
                                                  hold=set(self.node_changed), min_level=min_level,
                                                  radii=self.s.near_radii, layout=self.s.layout,
                                                  forced_level=self.s.forced_level, view=view,
                                                  anchor_nodes=not high and self.first_ramp is None, eye_h=eye_h,
                                                  cone=self.map_cone(aid=anchor.id),
                                                  **extra)
        else:
            new, refined = farfield.select(*self.view_focus, forced, self.refined, kfun,
                                           self.s.hysteresis, radius, hold=set(self.node_changed),
                                           min_level=min_level, radii=self.s.near_radii, layout=self.s.layout,
                                           forced_level=self.s.forced_level, view=view, **extra)
        t_sel = time.perf_counter() - t_sel
        if t_sel > SLOW_SELECT_S:
            log.warning("far selection took %.2f s (radius %.0f km, eye %.0f m, %d tiles, %d refined nodes; global "
                        "part, anchor part s, regions, global tiles: %s)", t_sel, radius / 1000, self.eye or 0.0,
                        len(new), len(refined), farfield.LAST_GLOBAL_SELECT)
        if self.desired:                # the first selection of an anchor is not a change
            for n in refined ^ self.refined:
                self.node_changed[n] = now
        if FAR_TRACE and time.monotonic() - getattr(self, "_sel_logged", 0.0) > 0.5:
            self._sel_logged = time.monotonic()
            lv = {}
            for t in new.values():
                lv[t.level] = lv.get(t.level, 0) + 1
            log.info("fartrace select eye %.0f radius %.0f km focus %.0f,%.0f held %d levels %s", self.eye or -1,
                     radius / 1000, *self.view_focus, len(self.node_changed), dict(sorted(lv.items())))
        self.refined = refined
        if self.first_ramp is not None:
            self.s.coarse_keys.update(k for k in new if farfield.is_global_key(k))
        changed = new.keys() != self.desired.keys()
        self.desired = new
        self._regroup()
        if changed and not self.dirty:
            self.dirty = True
            self.batch = [time.perf_counter(), 0, 0]
        self.wake.set()

    def building_reach(self):
        """The far buildings' and the skyline's send distances: the graphics setting's, farther with the 3D map open as
        the eye rises (the user, 2026-11-07: "make buildings start rendering further out"), at most MAP_BLD_*_MAX_M."""
        bld = getattr(self, "building_scale", None) or 1.0
        far = buildingsmod.FAR_SEND_M * bld
        sky = buildingsmod.SKY_SEND_M * bld
        if self.map_view or self.map_open:
            e = self.eye_clearance()
            far = max(far, min(e * MAP_BLD_FAR_EYE, MAP_BLD_FAR_MAX_M))
            sky = max(sky, min(e * MAP_BLD_SKY_EYE, MAP_BLD_SKY_MAX_M))
        if abs(self.bldfar.send_m - far) > 50.0:
            self.bldfar.send_m = far
            self.bldfar.drop_m = far + (buildingsmod.FAR_DROP_M - buildingsmod.FAR_SEND_M)
        if self.bldsky is not None and abs(self.bldsky.send_m - sky) > 50.0:
            self.bldsky.send_m = sky
            self.bldsky.drop_m = sky + (buildingsmod.SKY_DROP_M - buildingsmod.SKY_SEND_M)
        if self.bldsky is not None:
            self.bldsky.inner = (self.bldfar.send_m, buildingsmod.SKY_UNIT)

    def map_cone(self, aid):
        """The 3D map's view cone for the selection (farfield.out_of_view: off-screen nodes MAP_OUT_OF_VIEW_K times as
        far), or None (driving: every direction counts; no 3D camera yet)."""
        if not (self.map_view or self.map_open) or not isinstance(self.cam, dict) or \
                getattr(self, "cam_aid", None) != aid:
            return None
        c = farfield.view_cone(self.cam)
        return list(c) + [MAP_OUT_OF_VIEW_K] if c is not None and len(c) == 8 else None

    # --- the far PVS (farpvs.py) ------------------------------------------------------------------------------------
    def tex_short(self, info, t):
        """A built far tile's texture is less than it should have now: smaller than tile_tex_px, or the coarse cover's
        few photos (FIRST_RAMP) where the tile is not hidden behind terrain (pvs_hidden: those may keep them)."""
        anchor = self.s.anchors.get(self.active)
        if anchor is None or not info.tex_px:
            return bool(info.coarse)
        need = self.s.tile_tex_px(anchor, t)
        return info.tex_px < need or (bool(info.coarse) and not self.s.pvs_hidden(anchor, t))

    def far_lazy_tick(self):
        """The far tiles shown larger than their level's texture (farpvs.UP_ON: in sight, the screen resolving more):
        their best photos (the lazy sources: NAIP in the US, a regional service) fetched after the near ones, nearest
        first (Imagery.far_warm), each tile rendered again when they are in (the user, 2026-11-11: "sharpen with the
        best source available in the region"). Every FAR_LAZY_EVERY_S at most."""
        s = self.s
        im = s.imagery
        anchor = s.anchors.get(self.active) if self.active is not None else None
        if im is None or anchor is None or not imagerymod.FAR_LAZY or not farpvsmod.UP_ON or self.map_view \
                or self.map_open:
            return
        now = time.monotonic()
        if now - getattr(self, "_far_lazy_at", 0.0) < FAR_LAZY_EVERY_S:
            return
        self._far_lazy_at = now
        queued = self.__dict__.setdefault("far_lazy", {})      # ck -> px queued
        x, y = self.view_focus
        loop = asyncio.get_running_loop()
        for key, t in list(self.desired.items()):
            ck = self.ck(key)
            info = s.tiles.get(ck)
            if info is None or info.material is None or not info.tex_px or info.tile is None or info.coarse:
                continue
            tile = info.tile
            node = tile.base if isinstance(tile, farfield.Piece) else tile
            if info.tex_px <= s.tex_size(node.level) or queued.get(ck) == info.tex_px:
                continue
            queued[ck] = info.tex_px
            frame, x0, y0, size = s.tile_where(anchor, tile)
            bx0, by0, bx1, by1 = node.bbox
            d = math.hypot(max(bx0 - x, x - bx1, 0.0), max(by0 - y, y - by1, 0.0))

            def done(ck=ck):
                loop.call_soon_threadsafe(s.far_lazy_done, ck)
            im.far_warm(ck, d, frame, *imagerymod.gutter_square(x0, y0, size, info.tex_px), info.tex_px, done)

    def pvs_tick(self):
        """Keep this connection's far PVS (farpvs.Set) current (every pose): drop it when it no longer holds (the
        player out of its square, the camera too high, the 3D map, another anchor), and make the next one in its worker
        when the player's square, eye class or ground step changed. Every change that un-hides tiles re-renders theirs
        (pvs_reveal)."""
        s = self.s
        anchor = s.anchors.get(self.active) if self.active is not None else None
        if not s.pvs_on or anchor is None or self.map_view or self.map_open or s.imagery is None \
                or getattr(self, "eye_now", None) is None:
            self._pvs_drop()
            return
        if s.horizon and anchor.id not in s.horizon_grids and anchor.id not in s.pvs_fields:
            return                          # (its far radius not known yet: rings for the 80 km disc made twice)
        if self.first_ramp is not None and anchor.id not in s.pvs_fields:
            return                          # (a teleport's coarse cover first: the rings' DEM and CPU after it)
        field = s.pvs_field(anchor, self.radius or s.radius)
        if field is None:
            self._pvs_drop()
            return
        x, y = self.view_focus              # (the camera's place when it is away from the car)
        g = field.rings.ground_hi(x, y)
        eye = self.eye_now
        cls = farpvsmod.eye_class_for(eye - g) if g is not None else None
        if cls is None:
            self._pvs_drop()                # (flying, or past the rings: everything visible)
            return
        p = self.pvs
        if p is not None and p.aid == anchor.id and p.eye_class > cls and \
                eye - g > farpvsmod.EYE_CLASSES[cls] / 2:
            cls = p.eye_class               # (down a class only well under it: a camera bobbing at the line)
        if self.pvs is not None and not self.pvs.valid_at(anchor.id, x, y, eye):
            self._pvs_drop()
        cx, cy, half = farpvsmod.region(x, y, *self.vel)
        ppr = self.pvs_ppr()
        key = (anchor.id, cx, cy, half, cls, int(round(g / farpvsmod.GROUND_STEP_M)), ppr)
        if key == self.pvs_key or self.pvs_job is not None:
            return
        self.pvs_key = key

        def make():
            t0 = time.perf_counter()
            n0 = field.computed
            got = farpvsmod.Set(field, cx, cy, half, cls, x, y, g, ppr=ppr)
            s.pvs_stats["sets"] += 1
            s.pvs_stats["viewpoints"] += field.computed - n0
            s.pvs_stats["set_s"] += time.perf_counter() - t0
            return got
        fut = self.pvs_job = asyncio.get_running_loop().run_in_executor(s.pvs_pool, make)

        def done(f):
            if self.pvs_job is f:
                self.pvs_job = None
            if f.cancelled() or f.exception() is not None:
                if not f.cancelled():
                    log.warning("far PVS failed: %r", f.exception())
                self.pvs_key = None
                return
            new = f.result()
            if self.active != new.aid or self.map_view or self.map_open:
                self.pvs_key = None
                return
            old, self.pvs = self.pvs, new
            if FAR_TRACE or old is None:
                hid = sum(1 for k, t in self.desired.items() if s.pvs_hidden(anchor, t))
                log.info("far PVS: square %.0f, %.0f +- %.0f m, eye class %d, %d viewpoints; %d of %d far tiles hidden",
                         new.cx, new.cy, new.half, new.eye_class, new.viewpoints, hid, len(self.desired))
            self.pvs_reveal()
            self.pvs_tick()                 # (the player may have moved on meanwhile)
        fut.add_done_callback(done)

    def pvs_ppr(self):
        """The screen's pixels per radian for the far PVS's texture sizes (pose.cam h / fovDeg), up to the next half
        power of two (a set is made again when it grows past it: a zoomed-in camera), at least farpvs.PPR_MIN."""
        c = self.cam if isinstance(self.cam, dict) else {}
        try:
            h, fov = float(c.get("h") or 0.0), float(c.get("fovDeg") or 60.0)
        except (TypeError, ValueError):
            h, fov = 0.0, 60.0
        ppr = h / (2 * math.tan(math.radians(fov) / 2)) if h > 0 and 1.0 < fov < 170.0 else 0.0
        ppr = max(ppr, farpvsmod.PPR_MIN)
        return 2.0 ** (math.ceil(math.log2(ppr) * 2) / 2)

    def _pvs_drop(self):
        if self.pvs is not None:
            self.pvs = None
            self.pvs_reveal()
        self.pvs_key = None

    def pvs_reveal(self):
        """Far tiles shown (or sent) with less texture than they should have now (a PVS that no longer hides them):
        stale, re-rendered live nearest first (far_retex)."""
        anchor = self.s.anchors.get(self.active) if self.active is not None else None
        if anchor is None or self.first_ramp is not None or not self.s.pvs_on:
            return                          # (a teleport's coarse cover: sharpened as the full selection replaces it)
        n = 0
        for key, t in list(self.desired.items()):
            ck = self.ck(key)
            if ck not in self.shown and key not in self.sent:
                continue
            info = self.s.tiles.get(ck)
            if info is None or info.material is None or info.stale or not self.tex_short(info, t):
                continue
            info.stale = True
            self.s.retex_request(ck, pump=False)
            n += 1
        if n:
            self.s.pvs_stats["revealed"] += n
            log.info("far PVS: %d far tiles in sight now, sharpened", n)
            self.s._retex_pump()

    def ck(self, key):
        """The server's key of far tile `key` under the active anchor (Server.far_ck: a global tile's names none)."""
        return (GLOBAL_AID, key) if farfield.is_global_key(key) else (self.active, key)

    def eye_clearance(self):
        """The camera's height over the ground under the view (the far tiles' detail goes by the 3D distance:
        farfield.select_global eye_h): 0 below EYE_LOD_MIN_M (driving, the car's camera: as before), else the eye over
        the highest terrain of the horizon grid's cell there (an under-estimate on a mountain: finer, never coarser)."""
        eye = self.eye
        if eye is None or eye < EYE_LOD_MIN_M:
            return 0.0
        zg = 0.0
        grid = getattr(self, "_lod_grid", None)
        if grid is not None:
            s = farfield.level_size(farfield.HORIZON_GRID_LEVEL)
            fx, fy = self.view_focus
            z = grid.zmax(farfield.HORIZON_GRID_LEVEL, int(math.floor(fx / s)), int(math.floor(fy / s)))
            if math.isfinite(z):
                zg = max(0.0, z)
        return max(0.0, float(eye) - zg)

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

    def lod_px_now(self):
        """This connection's screen-space LOD tau (px; 0: off): the mod's lodPx, else the server's --lod-px."""
        v = getattr(self, "lod_px", None)
        return self.s.lod_px if v is None else v

    def lod_ppr(self, h, fov, now=None):
        """The pixels per radian the screen-space LOD goes by: this pose's (h / (2 tan(vfov / 2))), at most
        LOD_PPR_ZOOM times a LOD_BASE_FOV_DEG camera's, the most of the last LOD_PPR_WINDOW_S seconds."""
        now = time.monotonic() if now is None else now
        ppr = h / (2 * math.tan(math.radians(fov) / 2))     # BeamNG's fov: vertical
        ppr = min(ppr, LOD_PPR_ZOOM * h / (2 * math.tan(math.radians(LOD_BASE_FOV_DEG) / 2)))
        hist = self.__dict__.setdefault("ppr_hist", deque())     # (a sliding maximum: decreasing pprs)
        while hist and hist[-1][1] <= ppr:
            hist.pop()
        hist.append((now, ppr))
        while now - hist[0][0] > LOD_PPR_WINDOW_S:
            hist.popleft()
        return hist[0][1]

    def view(self):
        """The camera for screen-space LOD (farfield.View) from pose.cam (x, y, z, fovDeg, h: the screen's height px),
        or None: no camera yet, an older mod without h, a camera of another anchor's frame, or tau 0 (lod_px_now). The
        tile errors are the built tiles' along the normal (Server.lod_errs / glod_errs; a node never built goes by the
        distance rule only)."""
        c = self.cam
        tau = self.lod_px_now()
        if not isinstance(c, dict) or not tau > 0 or getattr(self, "cam_aid", None) != self.active:
            return None
        try:
            cx, cy, cz = float(c["x"]), float(c["y"]), float(c["z"])
            h, fov = float(c.get("h") or 0), float(c.get("fovDeg") or 60)
        except (KeyError, TypeError, ValueError):
            return None
        if not (h > 0 and 1 < fov < 170 and math.isfinite(cx) and math.isfinite(cy) and math.isfinite(cz)):
            return None
        ppr = self.lod_ppr(h, fov)
        aid, errs = self.active, self.s.lod_errs

        def err(node):
            return errs.get((aid, node)) if len(node) == 3 else None
        reach, cap = farfield.SSE_REACH, farfield.SSE_SPACING_CAP
        tune = os.environ.get("BNGE_LOD")      # experiments: "reach,cap"
        if tune:
            reach, cap = (float(v) for v in tune.split(","))
        return farfield.View(cx, cy, cz - getattr(self, "pz", cz), ppr, err, tau=tau, reach=reach,
                             cells=self.s.layout.cells, cap=cap, gerr=self.s.glod_errs)

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
        self.graded_wait = []
        self.failed.clear()

    def graded(self, t):
        """Does far tile t wait for the place's map data (blockworker._far_geometry: graded under the roads at levels
        <= 0, the sea floor carved to seacarve.CARVE_MAX_LEVEL with --sea on)?"""
        top = seacarvemod.CARVE_MAX_LEVEL if getattr(self.s, "sea", None) is not None else 0
        return not isinstance(t, farfield.GTile) and t.level <= top

    def _next_tile(self, graded_ok=True):
        """The next tile to build by priority, None when none is left; graded_ok False: passing over the graded
        ones (GRADED_SHARE: they go first again once a graded slot is free)."""
        def wanted(t):
            return t.key not in self.sent and self.ck(t.key) not in self.inflight and t.key in self.desired
        if graded_ok:
            while self.graded_wait:
                t = self.graded_wait.pop(0)
                if wanted(t):
                    return t
        while self.qi < len(self.queue):
            t = self.queue[self.qi]
            self.qi += 1
            if not wanted(t):
                continue
            if not graded_ok and self.graded(t):
                self.graded_wait.append(t)
                continue
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
                    fut.add_done_callback(lambda _f: loop.is_closed() or loop.call_soon_threadsafe(self.wake.set))
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
        self._stream_waker = None
        try:
            await self._stream()
        except ConnectionError:
            pass
        except asyncio.CancelledError:
            for fut, _ in self.inflight.values():
                fut.cancel()
            raise
        finally:
            if self._stream_waker is not None:
                self._stream_waker.cancel()
                await asyncio.gather(self._stream_waker, return_exceptions=True)
                self._stream_waker = None

    async def _stream(self):
        """Build the needed tiles by priority, send them, emit swap groups as they complete;
        one task per connection."""
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
            # (graded tiles: at most GRADED_SHARE of the workers, the rest go on to the ungraded ones meanwhile)
            g_cap = max(1, int(workers * GRADED_SHARE)) if getattr(self.s, "grading", False) else workers
            g_n = sum(1 for _, t in self.inflight.values() if self.graded(t))
            while anchor is not None and not self.teleport_pending and len(self.inflight) < workers:
                t = self._next_tile(g_n < g_cap)
                if t is None and g_n >= g_cap:
                    # (nothing else to build: the cap would only leave workers idle; Laramie 2026-11-11, the near
                    # 300 tiles once the map data was in: 17 s at half the workers)
                    t = self._next_tile(True)
                if t is None:
                    break
                g_n += self.graded(t)
                self.inflight[self.ck(t.key)] = (self.s.tile(anchor, t), t)
            if anchor is not None and self.s.imagery is not None and not self.teleport_pending and \
                    self.first_ramp is None:    # (the coarse cover fetches its few satellite photos itself)
                # a tile at most every PREFETCH_AGAIN_S (a prefetch skips sources short of request tokens): the
                # loop wakes on every tile done and every message from the game, and asking again for the same 40
                # tiles each time kept the event loop busy (~22 s of a teleport on a Steam Deck, 2026-10-20)
                now = time.monotonic()
                if len(self.prefetched) > PREFETCHED_MAX:
                    self.prefetched.clear()
                spare = PREFETCH_SPARE_PER_PASS
                for i, t in enumerate(self.queue[self.qi:self.qi + IMAGERY_PREFETCH + IMAGERY_PREFETCH_SPARE]):
                    ck = self.ck(t.key)
                    soon = i < IMAGERY_PREFETCH
                    if not soon and spare <= 0:
                        break
                    if ck not in self.s.tiles and \
                            now - self.prefetched.get(ck, -1e9) >= (PREFETCH_AGAIN_S if soon else PREFETCH_SPARE_AGAIN_S):
                        self.prefetched[ck] = now
                        spare -= not soon
                        self.s.prefetch_imagery(anchor, t, soon)
            if anchor is not None:
                self.emit_groups()
                self.flush_old()
            if (anchor is not None and getattr(self, "lod_ramp", 1.0) < 1.0 and (self.map_view or self.map_open)
                    and not self.inflight and self.dirty and len(self.groups) == len(self._blocked())):
                # (only once a step is whole: stepping on a timer instead left the coarse tiles not built yet out of
                # the next selection, holes in the view for 15 s, Bozeman 2026-11-07)
                self.lod_ramp = min(1.0, self.lod_ramp * MAP_RAMP_STEP)     # (the next, finer step: commit_anchor)
                self.zoom_ref = self.eye    # (each step: what is shown is this eye's; a zoom from here starts over)
                log.info("anchor %d: 3D map detail step x%.2f after %.2f s", anchor.id, self.lod_ramp,
                         time.perf_counter() - self.batch[0])
                self.update_far()
                continue
            # (the full selection does not wait for the first far PVS: built nearest first, its far tiles come after the
            # set is there; held for it, a cold Zermatt's whole horizon was 10 s later, 2026-11-11)
            if anchor is not None and self.first_ramp is not None and (
                    (not self.inflight and self.dirty and len(self.groups) == len(self._blocked()))
                    or time.monotonic() - self.first_ramp > FIRST_RAMP_MAX_S):
                # (the coarse cover is shown, or it took too long: the full selection now, refined group by group)
                n = sum(1 for k in self.desired if self.ck(k) in self.shown)
                log.info("anchor %d: coarse far field %s in %.2f s (%d of %d tiles shown), the full one next",
                         anchor.id, "whole" if n == len(self.desired) else "given up",
                         time.monotonic() - self.first_ramp, n, len(self.desired))
                self.first_ramp = None
                # (the full selection as an anchor's first: neither the coarse step's changes nor its own hold
                # anything for HOLD_S; held, the next pose's selection kept some nodes of this one, its far_done came
                # before the tiles of the selection that followed, and a rebase sent global tiles again)
                self.s.coarse_keys.clear()      # (their own size from here on: those kept are rendered again)
                self.node_changed, self.desired = {}, {}
                self.update_far()
                self.pvs_reveal()               # (coarse tiles the full selection keeps, in sight: sharpened)
                continue
            if (not self.inflight and self.dirty and anchor is not None
                    and len(self.groups) == len(self._blocked())):
                self.dirty = False
                n = sum(1 for k in self.desired if self.ck(k) in self.shown)
                self.send({"type": "far_done", "anchorId": anchor.id, "count": n})
                self.far_done_at = time.monotonic()
                if self.map_view or self.map_open:
                    self.zoom_ref = self.eye    # (all shown at this eye's detail: the zoom rule's reference)
                # (full priority: the roads too, on_pose; a catch-up boost is ended by catch_up only: ended here, it
                # stayed off for the rest of a 150 mph drive, its flag still on, 2026-11-02)
                if not self.s.boost_full and not self.catchup:
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
                if self.s.pvs_stats["sets"]:
                    log.info("far PVS: %s", {k: round(v, 2) if isinstance(v, float) else v
                                             for k, v in self.s.pvs_stats.items()})
                if not any(ck[0] != anchor.id and ck[0] != GLOBAL_AID for ck in self.shown):
                    self.s.schedule_prune(anchor.id)
            await self.writer.drain()
            if self._stream_waker is None:
                self._stream_waker = asyncio.ensure_future(self.wake.wait())
            futs = [f for f, _ in self.inflight.values()]
            done, _ = await asyncio.wait(futs + [self._stream_waker], return_when=asyncio.FIRST_COMPLETED)
            if self._stream_waker in done:
                self._stream_waker = None
            if (self.map_view or self.map_open or self.eye_clearance() > FLY_TEX_EYE_M) and anchor is not None and \
                    time.monotonic() - getattr(self, "_texup_at", 0.0) > MAP_TEX_UPGRADE_S:
                # (the camera turned: tiles shown with a smaller texture than they now need on screen, sharper)
                self._texup_at = time.monotonic()
                for ck in [ck for ck in self.shown if ck[0] == anchor.id or ck[0] == GLOBAL_AID]:
                    info = self.s.tiles.get(ck)
                    if info is None or info.material is None or not info.tex_px or info.tile is None:
                        continue
                    if self.s.tile_tex_px(anchor, info.tile) > info.tex_px and not info.stale:
                        info.stale = True
                        self.s.retex_request(ck, pump=False)
            if self.s.retex_todo and (self.map_view or self.map_open):
                self.s._retex_pump()        # (re-renders into the far slots builds left free)
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
                if info.material is not None and not info.stale and self.first_ramp is None and self.tex_short(info, t):
                    info.stale = True   # (a coarse cover's texture: rendered again, the whole chain at its own size)
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
                        m["tex"] = info.tex_px or self.s.tex_size(t.level)
                self.send(m)
                if FAR_TRACE:
                    log.info("fartrace %.2f s L%d %s build %.0f ms tex %.0f ms quick %s", time.perf_counter() -
                             self.batch[0], t.level, t.key, 1000 * info.seconds, 1000 * info.tex_seconds,
                             any(isinstance(p, tuple) and p[:1] == (imagerymod.IMG_PENDING,) for p in info.cm_pending))
                if self.batch[1] % 50 == 0:
                    left = sum(1 for g in self.groups for k in g[0] if k not in self.sent)
                    # this change's tiles (sent / sent + still to send), not the whole far field
                    self.status(f"Far field loading: {self.batch[1]}/{left + self.batch[1]} tiles")


async def main_async(args):
    log.info("startup: initializing data services")
    if os.environ.get("BNGE_WORLD_BUNDLE") and (args.roads != "on" or args.grading != "on"):
        from worldbundle import BundleError
        raise BundleError("BNGE_WORLD_BUNDLE requires --roads on --grading on")
    if os.environ.get("BNGE_WORLD_BUNDLE") and getattr(args, "peers", ""):
        from worldbundle import BundleError
        raise BundleError("Legacy --peers helpers do not negotiate the pinned road-plan identity")
    if os.environ.get("BNGE_WORLD_BUNDLE"):
        from worldbundle import validate_bundle
        # Reject malformed/missing data before allocating services or starting cache maintenance.
        validate_bundle(os.environ["BNGE_WORLD_BUNDLE"])
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
    # map data (mapdata.py): land cover for the surfaces, roads; one instance shares its cache locks
    mapdata = None
    try:
        mapdata = (mapdatamod.MapData(args.cache_dir, fetcher=net)
                   if "on" in (args.surface, args.roads, args.sea, args.trees) else None)
        if os.environ.get("BNGE_WORLD_BUNDLE"):
            from worldbundle import PinnedPlanCache
            PinnedPlanCache(os.environ["BNGE_WORLD_BUNDLE"],
                            f"w{int(args.road_widths == 'on')}s{int(args.sea == 'on')}", mapdata.bundle_versions())
    except BaseException:
        try:
            if mapdata is not None:
                mapdata.close()
        finally:
            net.close()
        raise
    log.info("startup: opening the download cache")
    disk = cachemod.Cache(args.cache_dir, args.cache_cap)
    threading.Thread(target=prune_own_caches, args=(args.cache_dir, time.time()), daemon=True,
                     name="cache-prune").start()
    pacer.DUTY = args.background_duty
    global DETAIL_RING_M, AHEAD_ON, ROUTE_AHEAD_ON, ROUTE_AHEAD_MAX_M
    DETAIL_RING_M = args.detail_ring
    AHEAD_ON = args.download_ahead == "on"
    ROUTE_AHEAD_ON = args.route_prefetch == "on" and args.route_prefetch_km > 0
    ROUTE_AHEAD_MAX_M = args.route_prefetch_km * 1000.0
    # (before any worker process starts: they find the downloads' shared counters by name, netstate.ENV)
    if netstatemod.init():
        import atexit
        atexit.register(netstatemod.close)
    import budget
    # (the whole server's memory table and the watchdog's requests: budget.TREE_MB, memwatch.py; workers inherit it)
    if (budget.TREE_MB or budget.LIMIT_MB) and budget.tree_init():
        import atexit
        atexit.register(budget.tree_close)
    if budget.TREE_MB or budget.LOW_FREE_GB:    # (this process's own caches too: budget.tree_publisher)
        budget.tree_publisher()
    log.info("memory: a worker's ceiling %s, caches dropped below %.1f GB free, the whole server's budget %s",
             f"{budget.SHED_MB} MB" if budget.SHED_MB else "none", budget.LOW_FREE_GB,
             f"{budget.TREE_MB} MB" if budget.TREE_MB else "off")
    # (telemetry: every process's per-host download counts, summed by telemetry.Reporter; set before the workers start
    # so they inherit it)
    telemetry_on = args.telemetry == "on" and not args.offline and not os.environ.get("CI") \
        and os.environ.get("BNE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no")
    if telemetry_on:
        proc_dir = os.path.join(args.cache_dir, "netstats", "proc")
        os.environ["BNE_NETSTATS_PROC"] = proc_dir
        netfetch._PROC_DIR = proc_dir
    log.info("startup: preparing worker processes")
    block_pools, far_pool, photo_pool = [], None, None
    plancachemod.start_run(args.cache_dir)      # (before the workers: they inherit the run's id)
    roadsurfmod.mark_run_start()                # (likewise: the photo's road surfaces made before it are this run's)
    lean = getattr(args, "tier", None) in ("lean", "emergency") and LEAN_POOLS
    # (the compact layout: the lean tiers', and the small tier's (SMALL_COMPACT): a block process for both lanes, the far
    # buildings in the road processes, those replaced every budget.tasks_per_child() jobs)
    compact = lean or (getattr(args, "tier", None) == "small" and SMALL_COMPACT)
    procs = lean_processes(getattr(args, "tier", None)) if lean else 4
    if lean:
        log.info("worker processes: %s (a low-memory machine; BNGE_LEAN_PROCS 4, 2 or 1)",
                 {4: "one each for the blocks, the road tiles and the far tiles",
                  2: "one for the road tiles; the blocks and the far tiles in the server's own"}.get(
                      procs, "none: everything in the server's own process"))
    ctx = multiprocessing.get_context("spawn") if args.block_process == "on" and procs > 1 else None
    if ctx is not None and procs > 2:
        for lane in (LANE_REQUEST, LANE_PREFETCH):
            if compact and block_pools:
                # (a lean tier: the prefetch lane in the request lane's process, its blocks after the requested ones:
                # Server.block_shared, submit_low)
                block_pools.append(block_pools[0])
                continue
            warm_q = ctx.Queue(maxsize=64)
            # its feeder thread started now, not at the first warm_roads on the event loop: Thread.start waits for the
            # new thread to run, and a loaded machine stalled the loop 3.4 s there at a first visit's first pose
            # (2026-11-04; blockworker._read_warm skips the empty list)
            warm_q.put_nowait((None, 0.0, 0.0, []))
            block_pools.append((poolsmod.RespawningPool(
                "block", prefetch_procs() if lane == LANE_PREFETCH else 1, mp_context=ctx, initializer=blockworker.init,
                initargs=(args.cache_dir, args.offline, args.roads == "on",
                          args.grading == "on" and args.roads == "on",
                          args.road_widths == "on" and args.roads == "on", args.surface == "on", warm_q,
                          logging.getLogger().level, args.airports == "on", args.sea == "on", 0,
                          roadsmod.ROADS_IN_GROUND, args.gas_stations == "on" and args.roads == "on")), warm_q))
        if args.near_visual == "detail" and photo_process():
            # requested blocks' photos and stand-ins in a process of their own (Server._base_process / _stand_in)
            photo_pool = poolsmod.RespawningPool(
                "photo", 1, mp_context=ctx, initializer=blockworker.init,
                initargs=(args.cache_dir, args.offline, args.roads == "on",
                          args.grading == "on" and args.roads == "on",
                          args.road_widths == "on" and args.roads == "on", args.surface == "on", None,
                          logging.getLogger().level, args.airports == "on", args.sea == "on", 0,
                          roadsmod.ROADS_IN_GROUND, args.gas_stations == "on" and args.roads == "on"))
        far_n = max(args.workers, getattr(args, "far_max", args.workers))
        far_base = min(args.workers, FAR_DRIVE)
        far_kw = dict(mp_context=ctx, initializer=blockworker.init,
                      initargs=(args.cache_dir, args.offline, args.roads == "on",
                                args.grading == "on" and args.roads == "on",
                                args.road_widths == "on" and args.roads == "on", False, None,
                                logging.getLogger().level, args.airports == "on", args.sea == "on", NICE_FAR,
                                roadsmod.ROADS_IN_GROUND))
        # the processes beyond the driving ones (the 3D map, the loading boost: Server.far_limit) only while used
        # (pools.ElasticPool; BNGE_FAR_ELASTIC=off: all of them for the session, as from 1.9.0 to 1.9.1)
        far_pool = (poolsmod.ElasticPool("far tile", far_base, far_n - far_base, **far_kw)
                    if FAR_ELASTIC and far_n > far_base else poolsmod.RespawningPool("far tile", far_n, **far_kw))
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
    if ctx is not None and args.roads == "on" and args.road_processes > 0:
        road_pool = poolsmod.RespawningPool(      # road tiles (blockworker.roads_tile)
            "road tile", args.road_processes, mp_context=ctx, initializer=blockworker.init,
            hung_s=ROAD_TILE_TIMEOUT_S + ROAD_HUNG_EXTRA_S,
            initargs=(args.cache_dir, args.offline, True, args.grading == "on", args.road_widths == "on",
                      args.surface == "on", None, logging.getLogger().level,
                      args.airports == "on", args.sea == "on", NICE_ROADS, roadsmod.ROADS_IN_GROUND),
            **poolsmod.tasks_per_child_kw(budget.tasks_per_child() if compact else 0))
    make_fast_pool = None
    if road_pool is not None:
        def make_fast_pool(n):          # (the saved areas' fast mode: Server.fast_precache)
            return poolsmod.RespawningPool(
                "fast pre-cache", n, mp_context=ctx, initializer=blockworker.init,
                initargs=(args.cache_dir, args.offline, True, args.grading == "on", args.road_widths == "on",
                          args.surface == "on", None, logging.getLogger().level,
                          args.airports == "on", args.sea == "on", NICE_ROADS, roadsmod.ROADS_IN_GROUND))
    far_bld_pool = None
    if road_pool is not None and args.buildings == "on" and not compact:     # (compact: in the road processes)
        far_bld_pool = poolsmod.RespawningPool(   # far building tiles (blockworker.far_buildings_tile)
            "far building", 1, mp_context=ctx, initializer=blockworker.init,
            initargs=(args.cache_dir, args.offline, True, args.grading == "on", args.road_widths == "on",
                      args.surface == "on", None, logging.getLogger().level,
                      args.airports == "on", args.sea == "on", NICE_FAR_BUILDINGS, roadsmod.ROADS_IN_GROUND))
    log.info("startup: preparing world folders and services")
    server = Server(args.user_dir, args.cache_dir, max(args.workers, getattr(args, "far_max", args.workers)), kfun, args.hysteresis,
                    args.far_radius, fmt,
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
                    regional_poles=args.regional_poles,
                    traffic=args.traffic == "on" and args.roads == "on",
                    block_pools=block_pools, far_pool=far_pool, road_pool=road_pool, airports_db=airports_db,
                    far_bld_pool=far_bld_pool,
                    sea=seacarvemod.SeaCarver(mapdata, args.cache_dir) if args.sea == "on" else None,
                    vegetation=(vegetationmod.Vegetation(mapdata, args.cache_dir) if args.trees == "on" else None))
    server.make_fast_pool = make_fast_pool
    if budget.TREE_MB or budget.LOW_FREE_GB:    # (this process's own caches: dropped by the free memory / the budget)
        import rangeread
        budget.register_shed(server.dem.clear_memory)
        if mapdata is not None:
            budget.register_shed(mapdata.clear_memory)
        budget.register_shed(rangeread.clear_memory)
    if hasattr(getattr(server.far_pool, "local", server.far_pool), "want"):
        getattr(server.far_pool, "local", server.far_pool).want = server.far_limit     # (ElasticPool: extras on a boost)
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
        server.disk_cache = disk            # (net_note: a drive short of its free reserve, cache.disk_short)
        server.covermap = covermapmod.CoverMap(disk, args.cache_dir, mapdata if args.roads == "on" else None,
                                               server.world_signature).start()
    except Exception as e:      # (the map's view stays empty; never the server)
        log.warning("cache map: %s", e)

    server.game_cpus = getattr(args, "game_cpus", None)    # (cpupin.py: the game pinned at each hello)
    server.server_cpus = getattr(args, "server_cpus", None)
    server.photo_pool = photo_pool
    # the whole server held to its RAM limit (memwatch.py: caches dropped, a pool's processes replaced)
    memwatchmod.register(*[p for p, _q in block_pools], photo_pool, far_pool, road_pool, far_bld_pool)
    memwatchmod.start()
    server.boost_cpus = (cpupin.boosted(server.server_cpus, server.game_cpus)
                         if getattr(args, "load_boost", "on") == "on" else None)
    # (a driving player's catch-up lends less: half the game's cores for the whole of a 150 mph drive took its frame
    # rate from ~52 to ~40 fps, nyc_speed.py 2026-11-02)
    server.catchup_cpus = (cpupin.boosted(server.server_cpus, server.game_cpus, CATCHUP_LEND_SHARE)
                           if server.boost_cpus else None) or server.boost_cpus

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
    profile_task = asyncio.create_task(runtimeprofiler.stream(server))

    # Debug: event-loop stalls (every message the game sends waits for them). The loop beats; a thread
    # logs the main thread's stack when a beat is late, so the stall names its cause.
    beat = [time.perf_counter()]
    main_ident = threading.get_ident()
    if LOOP_RAISE:          # (this thread: the event loop's)
        import budget
        try:
            how = budget.raise_thread()
            if how:
                log.info("event loop priority: %s", how)
        except (OSError, AttributeError) as e:
            log.warning("could not raise the event loop's priority: %s", e)
    if SWITCH_MS:
        sys.setswitchinterval(SWITCH_MS / 1000.0)

    async def loop_beat():
        while True:
            beat[0] = time.perf_counter()
            await asyncio.sleep(LAG_TICK_S)
    lagger = asyncio.create_task(loop_beat())

    def main_stack():
        f = sys._current_frames().get(main_ident)
        stack = []
        while f is not None and len(stack) < 12:
            stack.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}:{f.f_lineno}")
            f = f.f_back
        return " < ".join(stack)

    def watch_lag():
        # (long stalls always: GitHub #725's server.log had 19 s with no line right after a block was built, the
        # block's terrain_ready 25 s late and the car held at the edge; nothing said what the loop was doing)
        reported, long_at = None, None
        while True:
            time.sleep(LAG_TICK_S)
            b = beat[0]
            lag = time.perf_counter() - b - LAG_TICK_S
            if long_at is not None and long_at != b:
                log.warning("event loop: the stall above lasted %.1f s", beat[0] - long_at - LAG_TICK_S)
                long_at = None
            if lag > LAG_STALL_S and long_at is None:
                long_at = b
                log.warning("event loop stalled > %.1f s (every game message waits) in %s", lag, main_stack())
            elif lag > LAG_REPORT_S and reported != b and log.isEnabledFor(logging.DEBUG):
                reported = b
                log.debug("event loop stalled > %.2f s in %s", lag, main_stack())
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
    if os.environ.get("BNGE_MEMATTR_S"):        # debug: which of the Server's attributes hold this process's arrays
        blockworker._memattr_loop(float(os.environ["BNGE_MEMATTR_S"]),
                                  lambda: [(f"server.{k}", v) for k, v in list(vars(server).items())],
                                  exclude=(server,))

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
    if telemetry_on:            # anonymous player count and data-source load (telemetry.py; BNE_TELEMETRY=off)
        import telemetry
        server.telemetry = telemetry.Reporter(
            args.cache_dir, updatesmod.local_version() or SERVER_VERSION,
            lambda: (bool(server.connections), getattr(server, "_share_hub", None) is not None),
            proc_dir=os.environ.get("BNE_NETSTATS_PROC"),
            # (the welcome page's choice and the settings page's switches: nothing before the choice; TELEMETRY.md)
            allowed=lambda: telemetry.choices(cpupin.player_settings(args.user_dir)),
            keep_id=not privacyregionmod.region()[1], perf=server.perf.take).start()
        logging.getLogger().addHandler(server.perf.errors)    # (error fingerprints: perfstats.ErrorPrints)
        log.info("telemetry: on, as the player chose on the welcome page (TELEMETRY.md; nothing before the choice; "
                 "BNE_TELEMETRY=off turns it off)")
    feed_srv = None
    if getattr(args, "companion_feed", "off") != "off":
        # the player's position for programs beside the game (companionfeed.py; docs/ADDONS.md), read-only
        import addons as addonsmod
        import companionfeed
        server.feed = companionfeed.Feed(updatesmod.local_version() or "", addonsmod.feed_origins(), args.companion_feed)
        try:
            feed_srv, fport = await server.feed.start(args.companion_port)
            log.info("companion feed on %s:%d/v1/ (read-only)",
                     "0.0.0.0" if args.companion_feed == "lan" else "127.0.0.1", fport)
        except OSError as e:
            server.feed = None
            # (its port taken: another BeamNG.Earth server runs, a test's beside the player's own, or a second app; the
            # feed is optional, the first one keeps it)
            if getattr(e, "errno", None) in (errno.EADDRINUSE, 10048):
                log.info("companion feed off: port %s is taken (another BeamNG.Earth server has it)", args.companion_port)
            else:
                log.warning("companion feed off: %s", e)
    # the programs beside the game set to start with BeamNG.Earth (addons.py, addonrun.py); ended with the server
    import atexit
    atexit.register(lambda: getattr(server, "_addons", None) is not None and server._addons.runner.stop_all())
    try:
        # (only a server the launcher started: a test's or a tool's own server never starts the player's add-ons; the
        # 2026-11-08 drive test started the real Live GPS add-on from the player's folder)
        if os.environ.get("BNGEARTH_PARENT"):
            await asyncio.get_running_loop().run_in_executor(None, server.addons().autostart)
    except Exception as e:      # (an add-on that does not start never stops the server)
        log.warning("add-ons: autostart: %s: %s", type(e).__name__, e)
    log.info("startup: opening the local connection")
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
        profile_task.cancel()
        try:
            await profile_task
        except asyncio.CancelledError:
            pass
        runtimeprofiler.collector.close()
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
    before `started`, without the lines whose item the prune removed)."""
    try:
        n = mapdatamod.prune_mirror(cache_dir)
        if n:
            log.info("cache: mapdata-mirror cut back: %d layer files' mirrors removed", n)
    except Exception as e:
        log.warning("cache: mapdata-mirror not cut back: %s", e)
    for sub, mb, match in (("seacarve", seacarvemod.RAW_MB, lambda n: n.endswith(".rawb.npy")),
                           ("seacarve", 0, lambda n: n.endswith(".raw.npy")),  # (whole rasters, before 2026-11-05)
                           (os.path.join("dem", "decoded"), demmod.MIRROR_MB,
                            lambda n: n.endswith((".npy", ".f32z"))),
                           ("peercache", peerworkmod.CACHE_BYTES / 1e6, None),
                           ("roadwidth", roadwidthmod.STORE_MB, lambda n: n.endswith(".npy"))):
        try:
            n, left = cachemod.prune_dir(os.path.join(cache_dir, sub), mb * 1e6, match=match)
            if n:
                log.info("cache: %s cut back: %d files removed, %.2f GB left", sub, n, left / 1e9)
        except Exception as e:      # a cache's trim must never stop the server
            log.warning("cache: %s not cut back: %s", sub, e)
    try:        # (its packs whole, the zips of before and the files kept beside the items; saved areas' items kept)
        n, merged, left = revisitmod.prune(cache_dir, areasmod.pinned_world(cache_dir), started)
        if n or merged:
            log.info("cache: revisit cut back: %d packs / files removed, %d small packs merged, %.2f GB left", n,
                     merged, left / 1e9)
    except Exception as e:
        log.warning("cache: revisit not cut back: %s", e)
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



def machine_profile(args, small):
    """The worker processes and analyses for the machine (budget.small): a small one (a Steam Deck, a 16 GB PC) fewer
    worker processes, each with smaller caches (budget.py), the imagery analyses off, where the player left the
    defaults; a desktop its road-tile processes by threads and memory. From 1.7.3 to 1.8.1 the small branch sat under
    the BNGE_FAST_BASE_V override (an A/B switch put in the middle of it), so small machines ran a desktop's processes
    and analyses: a Steam Deck's server at ~7 GB with 6 GB of swap (2026-11-04)."""
    import budget
    global FAST_BASE_V
    if small:
        # ... and "fast" (FAST_BASE_V: prefetched blocks without their photo until used, the chain ahead) from a lower
        # speed: at 87 mph a Steam Deck's server drew a full ground photo for every block (several CPU-s each) and the
        # road collision fell behind the car (2026-10-30)
        FAST_BASE_V = SMALL_FAST_BASE_V
        if args.road_processes == ROAD_PROCESSES:
            args.road_processes = budget.SMALL_ROAD_PROCESSES
        if args.workers == WORKERS:
            args.workers = budget.SMALL_FAR_WORKERS
        if budget.SMALL_ANALYSES_OFF:
            args.deshadow, args.colormatch = "off", "off"
        args.tier = budget.tier()
        if args.tier == "small" and SMALL_COMPACT and args.workers in (WORKERS, budget.SMALL_FAR_WORKERS):
            args.workers = budget.LEAN_FAR_WORKERS      # (the compact layout's one far-tile process: main_async)
        if args.tier in ("lean", "emergency"):
            # (the lean tiers: one road-tile process and, with LEAN_PROCS 4, one far-tile process; main_async)
            if args.road_processes in (ROAD_PROCESSES, budget.SMALL_ROAD_PROCESSES):
                args.road_processes = budget.LEAN_ROAD_PROCESSES
            if args.workers in (WORKERS, budget.SMALL_FAR_WORKERS):
                args.workers = budget.LEAN_FAR_WORKERS
        if args.tier == "emergency" and EMERGENCY_TRIM:
            # (4-6 GB, below what the game itself needs: the world's extras off, its ground and roads kept. In a 0.9 GB
            # scope with 2 GB of swap the untrimmed server was killed after 14.5 minutes of a cold Baltimore and I-95,
            # 2.9 GB at its peak; trimmed it lasted the drive at 2.6 GB, its teleports 40 s instead of 90: drive_mem
            # 2026-11-12)
            off = [n for n in EMERGENCY_OFF if getattr(args, n, "off") == "on"]
            for n in off:
                setattr(args, n, "off")
            if off:
                log.info("very low memory: %s off (BNGE_EMERGENCY_TRIM=off keeps them)",
                         ", ".join(n.replace("_", " ") for n in off))
        log.info("%s machine (%.1f GB): %d far-tile and %d road-tile processes, smaller caches, imagery analyses "
                 "off (budget.py; BNGE_BUDGET=desktop for the desktop sizes)",
                 {"lean": "low-memory", "emergency": "very low memory"}.get(args.tier, "small"),
                 budget.total_gb() or 0, args.workers, args.road_processes)
    elif args.road_processes == ROAD_PROCESSES:
        # (by the threads, and at most what the RAM limit holds: budget.desktop_sizes, ~1.2 GB a road process)
        sizes = budget.desktop_sizes()
        args.road_processes = min(budget.road_processes(), sizes[4]) if sizes else budget.road_processes()
        log.info("road-tile processes: %d (%d threads, %.0f GB, the server's limit %.1f GB)", args.road_processes,
                 os.cpu_count() or 0, budget.total_gb() or 0, budget.LIMIT_MB / 1024)
    # more far-tile processes on a big machine, used while the 3D map is open (Server.far_limit), at most what the RAM
    # limit holds (budget.desktop_sizes: ~0.8 GB a far-tile process)
    args.far_max = args.workers
    if not small and (budget.total_gb() or 0) >= 24:
        sizes = budget.desktop_sizes()
        args.far_max = max(args.workers, min((os.cpu_count() or 4) - 4, FAR_MAX_DESKTOP, sizes[5] if sizes else 99))
        log.info("far-tile processes: %d (%d at once while driving)", args.far_max, min(args.workers, FAR_DRIVE))

FAST_POOL_KEEP = 2          # the saved areas' fast mode: its road processes, one per CPU thread but these ...
FAST_POOL_GB = 2.0          # ... and at most one per this much memory (a road process holds ~1-1.5 GB at work) ...
FAST_POOL_MAX = 24          # ... and at most this many
FAST_POOL_FREE_GB = 1.5     # ... and one per this much of the memory free when it starts (the game, paused, keeps its
                            # own; sized from the total alone, a 32 GB PC with the game and the normal road pool running
                            # could be pushed into swap: PR #773's merge review, 2026-11-06)


def fast_pool_size(cpus=None, gb=None, free=None):
    """The road processes of the saved areas' fast mode on this computer (FAST_POOL_*): by its CPU threads, its memory
    and the memory free now. Pure given its arguments."""
    import budget
    cpus = (os.cpu_count() or 2) if cpus is None else cpus
    gb = (budget.total_gb() or 8.0) if gb is None else gb
    free = budget.free_gb() if free is None else free
    by_free = int(free / FAST_POOL_FREE_GB) if free is not None else FAST_POOL_MAX
    return max(2, min(FAST_POOL_MAX, cpus - FAST_POOL_KEEP, int(gb / FAST_POOL_GB), by_free))


def main():
    # (the GPU kernels' Vulkan instance without gamescope's WSI layer: the server draws nothing, and in a Steam Deck's
    # Game Mode the layer failed an assert in vkCreateInstance and aborted the whole server, the game with it (Mesa
    # 26.2-devel, 2026-11-07). Its implicit layer's own off switch; this process and its workers only, not the game)
    if sys.platform.startswith("linux"):
        os.environ.setdefault("DISABLE_GAMESCOPE_WSI", "1")
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
    ap.add_argument("--max-ram-gb", type=float, default=None, metavar="GB",
                    help="the most memory the whole server may hold, in GB (0: Auto, a third of the RAM, 60 %% from 22 GB, 1.5 to 24 GB; "
                         "default: the settings page's Max RAM for BeamNG.Earth, else Auto; budget.py, memwatch.py)")
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
    ap.add_argument("--block", type=int, choices=BLOCK_SIZES, default=None,
                    help="terrain block samples per edge (heightmap.SQUARE apart, 1 m): 1024 = 1 km on a 128 m grid "
                         "(the default), 2048 = 2 km on a 256 m grid (mesh mode only)")
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
                    help="screen-space LOD: far tiles split while their split would pop more than this many pixels "
                         "on the player's screen and merge while it would pop less (camera, field of view, "
                         "resolution; 0: distance rule only; a mod's lodPx setting overrides it)")
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
    ap.add_argument("--telemetry", choices=("on", "off"),
                    default="off",      # (the launcher turns it on for players: tests and direct runs never report)
                    help="anonymous usage reports (telemetry.py: players online, data-source load; default on, "
                         "BNE_TELEMETRY=off; never in --offline)")
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
    ap.add_argument("--regional-poles", choices=("off", "poles", "fr-demo", "fr-all", "fr-varied", "europe"),
                    default=os.environ.get("BNE_REGIONAL_POLES", "europe"),
                    help="regional furniture: off=stock, fr-demo=50/50 poles, fr-all=concrete poles, "
                         "fr-varied=French kit; europe=country-specific European scenery and Quebec/Turkish STOP lettering; "
                         "worldwide country polygons at each object, French assets in FR only; poles=only the country's own poles "
                         "(Europe, Turkey...: concrete and wood by country, everything else stock). Default europe "
                         "since 2026-11-10 (the user's call); BNE_REGIONAL_POLES=off for stock")
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
    ap.add_argument("--companion-feed", choices=("off", "local", "lan"), default="local",
                    help="the player's position, read-only, for programs beside the game (companionfeed.py, "
                         "docs/ADDONS.md): local = this computer only (default), lan = also the local network")
    ap.add_argument("--companion-port", type=int, default=47615, help="the companion feed's port")
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
    try:
        bad = _app_modules()[0].drive_relative_problem(args.cache_dir)
    except Exception:
        bad = None
    if bad:                     # (GitHub #895: c:\games\x with its backslashes dropped went to the current folder;
        ap.error(f"--cache-dir: {bad}")     # checked before anything writes there: cdn.chain's cdn.json)
    args.cache_dir = os.path.abspath(os.path.expanduser(args.cache_dir))
    os.environ["BNE_BRIDGE_PROFILE"] = args.bridge_profile     # read by roads.py here and in the block workers
    args.cells = resolve_cells(args.cells, args.near_visual)
    args.block = resolve_block(args.block, args.near_visual)
    args.horizon = resolve_horizon(args.horizon, args.near_visual)
    args.colormatch = resolve_colormatch(args.colormatch, args.imagery)
    if args.imagery:                # (the project's tile host where its switch turns it on: cdn.py)
        import cdn as cdnmod
        try:
            args.imagery = cdnmod.chain(args.imagery, netfetch.Fetcher(offline=args.offline), args.cache_dir)
        except Exception as e:
            log.warning("cdn: %s: %s", type(e).__name__, e)
    if "BNGE_DEM_CDN" not in os.environ:     # (Mapterhorn's z0-12 from the tile host where the switch says: dem.py)
        import cdn as cdnmod
        try:
            rel = cdnmod.dem_release(netfetch.Fetcher(offline=args.offline), args.cache_dir)
        except Exception as e:
            log.warning("cdn: %s: %s", type(e).__name__, e)
            rel = None
        os.environ["BNGE_DEM_CDN"] = rel or "off"   # (the block workers read it at their import of dem)
        demmod.MT_CDN_RELEASE = rel
    if args.cells != 32 and args.near_visual not in MESH_LAYOUT:
        ap.error("--cells 64 needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    if args.block != TERRAIN_BLOCK and args.near_visual not in MESH_LAYOUT:
        ap.error(f"--block {args.block} needs --near-visual mesh (terrain mode is frozen at its layout)")
    if args.horizon and args.near_visual not in MESH_LAYOUT:
        ap.error("--horizon needs --near-visual mesh (terrain mode is frozen at the v1.3.1 layout)")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("download cache: %s", args.cache_dir)
    if args.max_ram_gb is not None:     # (the settings page's choice otherwise: cpupin.apply_player_settings)
        import budget as _b
        os.environ[_b.SETTING_ENV] = str(args.max_ram_gb) if args.max_ram_gb > 0 else "auto"
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
    try:                            # the settings page's Performance choices (GitHub #529): before the priority and
        args.nice, chosen = cpupin.apply_player_settings(args.user_dir, args.nice, NICE)    # the cores' split below
        if chosen:
            log.info("the settings page: the server on %s", ", ".join(chosen))
    except Exception as e:          # (the defaults then)
        log.warning("could not read the settings page's server choices: %s", e)
    global BELOW_GAME
    BELOW_GAME = bool(args.nice and args.nice > 0)     # (Connection.catch_up: no game cores lent at the game's priority)
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
        split = cpupin.split(budget.SMALL_SERVER_SHARE if budget.small_machine() else cpupin.SERVER_SHARE) \
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
    machine_profile(args, small)
    if os.environ.get("BNGE_FAST_BASE_V"):     # (A/B: FAST_BASE_V in m/s)
        globals()["FAST_BASE_V"] = float(os.environ["BNGE_FAST_BASE_V"])
    if args.pin == "on" and (sys.platform.startswith("linux") or sys.platform == "win32"):   # (Windows: KyzoFox, PR #112)
        try:
            server_cpus, game_cpus = cpupin.split(budget.SMALL_SERVER_SHARE if budget.small_machine()
                                                  else cpupin.SERVER_SHARE)
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
