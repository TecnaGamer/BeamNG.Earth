"""Protocol 4 agreement between the server (earthd.py) and the mod (core.lua, farfield.lua,
lattice.lua, follow.lua, ...), checked from both sides against one table (docs/V1_3_SPEC.md E over
docs/V1_2_DESIGN.md §7 as amended by §12; server/README.md documents the same table):

  - every message the mod builds (net.send constructors in core.lua) has exactly the table's
    fields, and every field the server reads exists in the table;
  - every message the server sends in a real session (in-process server, synthetic DEM: teleport,
    far field, terrain-follow, errors, a second anchor with the previous one's far_removes) has
    the table's required fields with the table's types and nothing else, and every field the mod
    reads (msg.X / tr.X in its Lua) is in the table;
  - the constants both sides must share (protocol/format, lattice, quantum, terrain-follow rule,
    tile grid, material name and file, paths), and the material loading order in the mod;
  - the tile layouts (docs/V1_4_SPEC.md §3.1 M1): FORMAT 3 (32 cells) and 4 (64 cells) on both
    sides, hello.cells and far_tile.tex with FORMAT 4 only, crossover.defaultErr (under luajit) and
    the exact level for both cell counts, the governor kind of every texture size;
  - the terrain block sizes (docs/V1_4_SPEC.md §3.5 M4): both follow tables (1024 and 512, with the
    fast mode) and the staleness distances on both sides, hello.blockSize with --block 512 only,
    terrain.size the session's, the mod's layer / flat maps per size, setBlockSize first in hello;
  - the horizon (docs/V1_4_SPEC.md §3.6 M1h): hello.horizon and status.radius with --horizon only, the
    mod's visibleDistance margin and the sky's planet radius against the server's (horizon.lua), the
    mod reacting to both (core.lua);
  - protocol 4 (docs/V1_3_SPEC.md E): hello.nearVisual values; a textured far_tile's material
    name (bngEarthSat_<anchorId>_<key, '-' as 'm'>) is the one inside its .cdae (the mod's
    Material maps onto it by mapTo), its texture is a DXT1 DDS with a full mip chain next to the
    shape (same stem), UV0 spans the footprint with v flipped as the engine stores it (row 0 of
    the texture = north), UV1 is world-locked (x, y) / DETAIL_M (the mod's detail map runs on UV1
    at detailScale 1, so one repeat per DETAIL_M), and the mod's detail texture is a stock asset;
  - real weather (server/weather.py, docs/research/2026-10-10-real-weather.md): the 'weather'
    message (additive, no protocol bump: both sides ignore unknown types), sent in the session by an
    injected weather service; its nested cover / wind / aloft fields; the mod reads it in weather.lua;
  - real gravity (server/gravity.py Provider / Tracker): the 'gravity' message (additive the same
    way), sent in the session by a provider on the degree-36 EGM2008 fixture; weather.lua reads it;
  - data credits (server/credits.py, docs/DEVTOOLS.md "Data credits overlay"): the 'credits' message
    (additive the same way; --credits on, Server()'s default sends none), sent in the session with a
    Credits object; core.lua hands it to credits.lua, which reads near / far;
  - the download cache (server/cache.py, docs/ROADMAP.md "Caching"): 'cache' both ways (additive the
    same way; Server()'s default has no Cache and sends none): the mod's size cap / clear / usage request
    and the server's usage answer (clearedBytes after a clear), exercised in the session; the settings
    page's cap values are the settings schema's;
  - live retexture (--colormatch / --deshadow): the 'far_retex' message (additive the same way), sent
    in a session whose colour-match regions turn ready once the far field is shown; core.lua hands it
    to farfield.onRetex, which reads anchorId / key / texture.
"""
import json
import math
import os
import struct
import re
import shutil
import subprocess
import zipfile
import sys
import time
import types
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache as cachemod  # noqa: E402
import cdae  # noqa: E402
import credits as creditsmod  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import heightmap  # noqa: E402
import imagery  # noqa: E402
import modsim  # noqa: E402
import test_retex  # noqa: E402
import test_server  # noqa: E402
import colormatch as colormatchmod  # noqa: E402
import deshadow as deshadowmod  # noqa: E402
import gravity as gravitymod  # noqa: E402
import test_weather  # noqa: E402
import weather  # noqa: E402

NUM = (int, float)
OPT = "optional"            # may be absent
NULL = "nullable"           # may be JSON null


def F(types, *flags):
    return (types if isinstance(types, tuple) else (types,), set(flags))


# message type -> {field: F(types, flags)}; "type" itself is implied.
MOD_TO_SERVER = {
    # sessions: the session folders whose files the mod's objects still use (farfield.heldSessions; a
    # restarted server keeps an earlier run's folder named here, docs/ROADMAP.md "Robustness" 3)
    "hello": {"protocol": F(int), "format": F(int), "modVersion": F(str), "sessions": F((list, dict), OPT),
              # a reconnect: the pooled far-tile slots [[slot, serial], ...] the mod's objects hold for that session
              "slotSession": F(str, OPT), "slotsHeld": F((list, dict), OPT),
              # the mod reads a vegetation tile as one flat array (veg_tile's data: {groups, stride, flat}; trees.lua)
              "vegFlat": F(bool, OPT),
              # the mod places geotiles itself (geotile.lua's zoom: 17): the server sends their content once, kept
              # across rebases (geotiles.py, --geo-tiles on)
              "geoTiles": F(int, OPT),
              # ... the road tiles too (roads.lua ROAD_GEO: content kept, a skin per anchor; roads.GeoTracker)
              "roadGeo": F(int, OPT),
              # ... and the far buildings, skyline and far bridges (roads.lua FAR_GEO: blockworker.far_geo)
              "farGeo": F(int, OPT),
              # ... and the far field from level 3 up as global tiles (farfield.lua GLOBAL_FAR: select_global)
              "globalFar": F(int, OPT)},
    "prepare_anchor": {"requestId": F(int), "lat": F(NUM), "lon": F(NUM), "cx": F(NUM, OPT), "cy": F(NUM, OPT),
                       # 2026-10-22: the player's spot when not the origin (BeamMP: onto another player's anchor)
                       "px": F(NUM, OPT), "py": F(NUM, OPT),
                       # 2026-10-25: 'teleport' (a declared teleport: provisional first block, boost) or 'rebase'
                       "purpose": F(str, OPT)},
    "commit_anchor": {"anchorId": F(int)},
    "prepare_terrain": {"requestId": F(int), "anchorId": F(int), "cx": F(NUM), "cy": F(NUM)},
    "commit_terrain": {"anchorId": F(int), "cx": F(NUM), "cy": F(NUM)},
    "pose": {"anchorId": F(int), "x": F(NUM), "y": F(NUM), "z": F(NUM), "vx": F(NUM), "vy": F(NUM), "lat": F(NUM),
             "lon": F(NUM), "speed": F(NUM), "cam": F(dict, OPT),
             "follow": F(bool, OPT),        # false: the altitude rule holds the block (no prefetch)
             # previous anchors whose tile objects the mod still has (an empty Lua table may encode as {})
             "held": F((list, dict), OPT),
             # the session folders in use, as in hello: an earlier run's one no longer named is released
             "sessions": F((list, dict), OPT),
             # pooled far-tile slots whose far_tile's object is gone: [[slot, serial], ...] (matslots.py)
             "slotsGone": F((list, dict), OPT),
             # other vehicles' collision foci [{x, y, r, trees}] (collisionfoci.lua; earthd parse_foci)
             "foci": F((list, dict), OPT)},
    # the download cache (cache.py; additive): the page's size cap (GB, 0 = unlimited), a clear, or
    # neither (fresh usage numbers); always answered with a 'cache'
    "cache": {"capGb": F(NUM, OPT), "clear": F(bool, OPT)},
    # 2026-10-25: the settings page's cache folder / BeamMP launcher (apppaths.py): chosen in a window or typed
    "app_path": {"what": F(str, OPT), "pick": F(bool, OPT), "path": F(str, OPT)},
    # the ground's collision patch under another vehicle's collision island off the block (terrainpatch.lua / .py)
    "terrain_patch": {"anchorId": F(int), "key": F(str), "x0": F(NUM), "y0": F(NUM), "x1": F(NUM), "y1": F(NUM)},
    # the performance log (perflog.lua; additive): lines for the server's log (hitches, drops, minutes, the machine)
    "perf_log": {"lines": F((list, dict))},
    # BeamMP shared generation (peerwork.py, mpsession.lua): our world server serves our group; the players on our
    # anchor ({id, host, port, code, weight} each). (Commented out 2026-10-27: shared world building is off.)
    # "share": {"port": F(int, OPT)},       # (the BeamMP server's sharing port, its plugin's bngeConfig)
    # "peers": {"me": F(str), "members": F(list)},
    # roads (roads.py; additive): the mod removed its roads (switched off and on again): send them again
    "roads_reset": {},
    # gas stations (gasstations.py; additive): on, sent after every hello
    "gas_stations": {"on": F(bool)},
    # highway signs (signstream.py; additive): the settings page's switch, sent after every hello and when changed
    "highway_signs": {"on": F(bool)},
    # the settings page's graphics (Connection.set_graphics; additive): sent after every hello and on a change; the
    # far field's terrain detail (K x lodScale), the tree tiles' and far buildings' reach, the ground photo's px
    "graphics": {"lodScale": F(NUM, OPT), "treeScale": F(NUM, OPT), "buildingScale": F(NUM, OPT),
                 "photoPx": F(NUM, OPT), "viewM": F(NUM, OPT)},
    "map_tiles": {"tiles": F(list)},       # the world map's basemap tiles as files (settings.mapTiles)
    "geocode": {"id": F(int), "q": F(str), "lat": F(NUM, OPT), "lon": F(NUM, OPT)},   # the map's search box
    # the map's cache view (covermap.py; additive): the zoom-z tiles x0..x1, y0..y1; mode summary / elevation /
    # imagery / mapdata (world: not asked by the map yet)
    "cache_map": {"id": F(int), "z": F(int), "x0": F(NUM), "y0": F(NUM), "x1": F(NUM), "y1": F(NUM),
                  "mode": F(str, OPT)},
    # saved areas (areas.py; additive): a picked box's estimate, a new area, pause / resume / delete one; a saved
    # route: route "current" (the last planned GPS route) and half (its corridor's half width, m) instead of the box
    "area_plan": {"id": F(int), "s": F(NUM, OPT), "w": F(NUM, OPT), "n": F(NUM, OPT), "e": F(NUM, OPT),
                  "route": F(str, OPT), "half": F(NUM, OPT), "mode": F(str, OPT)},
    "area_add": {"id": F(int), "s": F(NUM, OPT), "w": F(NUM, OPT), "n": F(NUM, OPT), "e": F(NUM, OPT),
                 "route": F(str, OPT), "half": F(NUM, OPT), "name": F(str, OPT), "mode": F(str, OPT)},
    "area_cmd": {"id": F(int), "area": F(str), "op": F(str)},
    "route": {"id": F(int), "lat0": F(NUM), "lon0": F(NUM), "lat1": F(NUM), "lon1": F(NUM),
              "bearing": F(NUM, OPT),    # the GPS (routing.py); bearing: the car's heading, the route starts that way
              "download": F(str, OPT)},  # the "Download along route" setting: off / ahead / route (route_download)
    "where": {"id": F(int), "lat": F(NUM), "lon": F(NUM), "online": F(bool)},   # the status bar (geocode.py)
    "server_list": {},                  # the BeamNG.Earth servers for the tab in BeamMP's browser (serverlist.lua)
    # the update notice's button: the server opens the releases page (updates.open_page; additive)
    "open_page": {"url": F(str)},
    "update_now": {},                   # the update notice's "Update now" (selfupdate.py)
    # the level select's "Random location" (randomplace.py; additive)
    "random_place": {"id": F(int)},
    # the level select's place pictures (placepreview.py; additive): [{key, lat, lon}]
    "place_previews": {"places": F((list, dict))},
}
CAM = {"x", "y", "z", "fx", "fy", "fz", "fovDeg", "h"}     # h: the screen height px (screen-space LOD)
TERRAIN = {"cx": F(NUM), "cy": F(NUM), "heightmap": F(str), "size": F(int), "squareSize": F(NUM),
           "zBase": F(NUM), "maxHeight": F(NUM), "q": F(NUM),
           "layers": F(list, OPT), "materials": F(list, OPT),     # --surface on (surface.py)
           "base": F(str, OPT), "baseSize": F(NUM, OPT),           # --near-visual detail: the block's photo
           "ter": F(str, OPT)}                                     # the block as a .ter (swapped in, no import)
SERVER_TO_MOD = {
    "server_list": {"servers": F(list, OPT), "error": F(str, OPT)},   # earthd.server_list (the list service)
    "geocode_result": {"id": F(int), "results": F(list), "error": F(str, OPT)},   # geocode.py (Photon)
    # covermap.py: w x h levels (base64, north row first) for the asked box, clipped to the world; gen changes with
    # the index; ready: false while the index is being built
    "cache_map_result": {"id": F(int), "z": F(int), "x0": F(int), "y0": F(int), "w": F(int), "h": F(int),
                         "cells": F(str), "gen": F(int), "ready": F(bool, OPT), "error": F(str, OPT)},
    # areas.py: a picked box's estimate (sizes in bytes, the time in s, days of the hosts' daily limits)
    "area_plan_result": {"id": F(int), "ok": F(bool), "km2": F(NUM, OPT), "items": F(int, OPT),
                         "missing": F(int, OPT), "downloadBytes": F(int, OPT), "diskBytes": F(int, OPT),
                         "seconds": F(int, OPT), "days": F(NUM, OPT), "hosts": F(list, OPT),
                         "excluded": F(list, OPT), "km": F(NUM, OPT), "worldTiles": F(int, OPT),
                         "worldMissing": F(int, OPT), "error": F(str, OPT)},
    # the saved areas [{id, name, s, w, n, e, state, done, total, msg, mode, route?}] after hello, a change and while one downloads
    # (at most every areas.STATUS_S); result: the answer to an area_add / area_cmd
    "areas": {"areas": F(list), "pinnedBytes": F(int, OPT), "result": F(dict, OPT)},
    "route_result": {"id": F(int), "points": F(list), "dist": F(NUM), "dur": F(NUM), "error": F(str, OPT),
                      "elev": F((list, dict), OPT)},  # OSRM; elev: the map's profile (routing.resample, dem.sample)
    "route_download": {"total": F(int), "done": F(int), "active": F(bool)},  # routeahead.py: the route's corridor
    "update": {"current": F(str), "latest": F(str), "url": F(str)},   # a newer version on GitHub (updates.py)
    "update_status": {"state": F(str), "text": F(str), "done": F(int, OPT), "total": F(int, OPT),
                      "version": F(str, OPT)},      # the "Update now" download (selfupdate.py)
    "where_result": {"id": F(int), "street": F(str), "place": F(str, OPT), "region": F(str, OPT),
                     "country": F(str, OPT)},   # the status bar's location
    "random_place_result": {"id": F(int), "lat": F(NUM, OPT), "lon": F(NUM, OPT), "name": F(str, OPT)},
    "place_previews_ready": {"keys": F(list)},
    "map_tiles_ready": {"tiles": F(list)},  # [z, x, y, ok, layer] each: written to <user>/temp/beamngEarth/maptiles
    "hello": {"protocol": F(int), "format": F(int), "serverVersion": F(str), "appVersion": F(str), "sessionId": F(str),
              "farFormat": F(str), "material": F(str),
              "nearVisual": F(str), "attribution": F(str),          # protocol 4
              "cells": F(int, OPT),             # v1.4 M1: cells per tile edge, FORMAT 4 only
              "blockSize": F(int, OPT),         # v1.4 M4: terrain block size, only when not 1024 (--block 512)
              "horizon": F(bool, OPT),          # v1.4 M1h: true with --horizon only
              "geoTiles": F(int, OPT),          # geotiles: the zoom of the tiles it sends (hello.geoTiles, --geo-tiles on)
              "roadGeo": F(int, OPT),           # road tiles as geotiles (hello.roadGeo, the road processes on)
              "farGeo": F(int, OPT),            # far buildings / skyline / far bridges as geotiles (hello.farGeo)
              "globalFar": F(int, OPT),         # far levels >= 3 as global tiles (hello.globalFar, --global-far)
              "deshadow": F(str, OPT),          # --deshadow lambert|full only (the settings page shows it)
              "colormatch": F(str, OPT),        # --colormatch s2 only (likewise)
              "season": F(str, OPT),           # --season auto / fall (seasonlook.lua tints the leaves)
              "mapPort": F(int, OPT),           # the world map's tile endpoint on 127.0.0.1 (maptiles.py)
              "prewarm": F(str, OPT),           # the shape listing our shapes' materials (prewarm.py; with map data)
              # the world map's credits for its view: [[credit, w, s, e, n] or [credit]] per imagery source, and
              # the rest (weather, elevation); with imagery on only
              "imageryCredits": F(list, OPT), "otherCredits": F(str, OPT)},
    "status": {"text": F(str),
               "backlog": F(int),           # v1.4 M0: desired - shown far tiles of the current anchor
               "radius": F(NUM, OPT)},      # v1.4 M1h: the far-field radius (m), --horizon only
    "anchor_ready": {"requestId": F(int), "anchorId": F(int), "lat": F(NUM), "lon": F(NUM),
                     "groundZ": F(NUM, NULL), "terrain": F(dict),
                     "floorZ": F(NUM, NULL, OPT),          # 2026-10-17: in a tunnel, its floor (core.floorCheck)
                     "anchorKey": F(str, OPT),             # phase 5 (lattice anchors): not sent yet
                     "provisional": F(bool, OPT),          # 2026-10-22: a teleport's ungraded first block
                     "px": F(NUM, NULL, OPT), "py": F(NUM, NULL, OPT),     # the prepare_anchor's spot, echoed
                     "fuelShapes": F(list, OPT)},          # 2026-10-23: the region's gas station shapes (--gas-stations)
    "anchor_error": {"requestId": F(int, NULL), "error": F(str)},
    # "share": {"port": F(int, NULL), "code": F(str), "weight": F(NUM, OPT)},   # our group service (shared world
    # building, commented out 2026-10-27)
    "terrain_ready": {"requestId": F(int), "anchorId": F(int), **TERRAIN},
    "terrain_refresh": {"anchorId": F(int), **TERRAIN},     # the graded block in place of the provisional one
    "terrain_error": {"requestId": F(int, NULL), "anchorId": F(int, NULL), "error": F(str)},
    "far_tile": {"anchorId": F(int), "key": F(str), "level": F(int), "shape": F(str), "pos": F(list),
                 "err": F(NUM), "skirt": F(NUM), "sink": F(NUM, OPT), "state": F(str),   # sink: exactly on L0
                 # protocol 4, satellite imagery: the tile's own material (all three, or none)
                 "material": F(str, OPT), "texture": F(str, OPT), "detail": F(bool, OPT),
                 "tex": F(int, OPT),            # v1.4 M1: texture px (FORMAT 4, with the material)
                 "slotSerial": F(int, OPT),     # a pooled shape file's send serial (matslots.py; pose.slotsGone)
                 # geotiles phase 3b (hello.globalFar): a global tile's frame [lat, lon] (pos then {0, 0, pivot} in
                 # it), and a global tile's / a piece's footprint (flat x, y: its quad / polygon in the anchor's world)
                 "frame": F(list, OPT), "poly": F(list, OPT)},
    "far_swap": {"anchorId": F(int), "id": F(int), "add": F(list), "remove": F(list), "dIn": F(NUM),
                 "dOut": F(NUM)},
    "far_remove": {"anchorId": F(int), "key": F(str)},
    "far_done": {"anchorId": F(int), "count": F(int)},
    # --colormatch / --deshadow: a shown (or held) tile's texture rendered again under a new name
    # (additive, protocol 4; exercised by RetexMessagesTest, a session whose colour-match regions turn ready)
    "far_retex": {"anchorId": F(int), "key": F(str), "texture": F(str)},
    # real weather at the player (weather.py; additive, protocol 4)
    "weather": {"v": F(int), "t": F(int), "lat": F(NUM), "lon": F(NUM), "code": F(int), "isDay": F(bool),
                "cover": F(dict), "baseM": F(NUM), "baseAglM": F(NUM), "rainMmH": F(NUM), "snowCmH": F(NUM),
                "visM": F(NUM, NULL), "fog": F(bool), "thunder": F(bool), "wind": F(dict),
                "aloft": F(dict, OPT), "tempC": F(NUM), "dewC": F(NUM), "elevM": F(NUM), "slTempC": F(NUM),
                "pressureHpa": F(NUM), "utcOffsetS": F(int, OPT), "tz": F(str, OPT), "credit": F(str)},
    # real gravity at the player (gravity.py; additive, protocol 4): |g| in m/s^2 at (lat, lon, h m MSL)
    "gravity": {"v": F(int), "g": F(NUM), "lat": F(NUM), "lon": F(NUM), "h": F(NUM), "source": F(str)},
    # detail mode: a block's base photo rendered again once its NAIP tiles arrived (earthd base_refresh)
    "block_base": {"anchorId": F(int), "cx": F(NUM), "cy": F(NUM), "base": F(str), "baseSize": F(NUM)},
    # the highest ground near the player's path (groundahead.py; the mod's altitude rule), z in the anchor's frame
    "ground_ahead": {"anchorId": F(int), "z": F(NUM)},
    # the water near the player (--sea on): tiles [key, x, y, z, size, nx, ny] (seatiles.py), rivers
    # [key, kind, flow m/s, [[x, y, z, width, depth], ...]] (riverlines.py)
    "sea": {"anchorId": F(int), "tiles": F(list), "rivers": F(list)},
    # data credits for the tiles around the player (credits.py; additive, protocol 4, --credits on)
    "credits": {"v": F(int), "near": F(str), "far": F(str), "ids": F(list), "farIds": F(list)},
    # the loading line's counts (Connection.send_progress, the mod's loadprogress.lua): [done, wanted] each
    "load_progress": {"anchorId": F(int), "ground": F(list), "roads": F(list), "trees": F(list), "far": F(list)},
    # road tiles near the player (roads.py; additive, with map data only): a JSON file of DecalRoad chunks
    "roads_tile": {"anchorId": F(int), "key": F(str), "path": F(str), "n": F(int),
                   "data": F(dict, OPT),       # the file's JSON, so the game need not open it (staging.inline_json)
                   "slots": F(dict, OPT),      # {what: [pooled shape path, serial]} (matslots.py; pose.slotsGone)
                   # a road geotile (key "g17_x_y", hello.roadGeo): its content in the frame [lat, lon] of its centre,
                   # each part {shape, pos, pivot, relief} in data, h its content hash (kept across a server restart),
                   # exact {what: shape}: the parts this anchor shows as their exact variants (slots "x:<what>")
                   "frame": F(list, OPT), "h": F(str, OPT), "exact": F(dict, OPT)},
    # a road geotile's draped surfaces for an anchor (road mesh, plazas, sidewalks; their colliders), a JSON file
    "roads_skin": {"anchorId": F(int), "key": F(str), "path": F(str), "data": F(dict, OPT), "slots": F(dict, OPT)},
    # a road geotile's parts shown as their exact variant for an anchor ({what: shape}; false: the base shape again)
    "roads_exact": {"anchorId": F(int), "key": F(str), "parts": F(dict), "slots": F(dict, OPT)},
    "roads_drop": {"anchorId": F(int), "key": F(str)},
    # trees, bushes and rocks of a tile (vegetation.py, --trees on): a JSON file of items
    # (a geotile, key "g17_x_y": frame [lat, lon] of its centre and pivot, m in that frame: the mod places it,
    # geotile.lua, and keeps it across rebases)
    "veg_tile": {"anchorId": F(int), "key": F(str), "path": F(str), "n": F(int), "data": F(dict, OPT),
                 "frame": F(list, OPT), "pivot": F(NUM, OPT)},
    "veg_drop": {"anchorId": F(int), "key": F(str)},
    # far buildings (buildings.far_tile, --buildings on): a JSON file naming a render-only mesh
    # (a geotile, key "g17_x_y", hello.farGeo: frame [lat, lon] of its centre and pivot, m in that frame: the mod
    # places it and keeps it across rebases; the skyline's and far bridges' keys "g15_x_y")
    "bldfar_tile": {"anchorId": F(int), "key": F(str), "path": F(str), "n": F(int), "data": F(dict, OPT),
                    "slots": F(dict, OPT),     # {"shape": [pooled shape path, serial]}
                    "frame": F(list, OPT), "pivot": F(NUM, OPT)},
    "bldfar_drop": {"anchorId": F(int), "key": F(str)},
    # the skyline past them (buildings.skyline_tile): the same, one per SKY_UNIT x SKY_UNIT road tiles
    "bldsky_tile": {"anchorId": F(int), "key": F(str), "path": F(str), "n": F(int), "data": F(dict, OPT),
                    "slots": F(dict, OPT), "frame": F(list, OPT), "pivot": F(NUM, OPT)},
    "bldsky_drop": {"anchorId": F(int), "key": F(str)},
    # the bridges past the road tiles (buildings.far_bridge_tile): the same, a road tile's square of decks
    "brfar_tile": {"anchorId": F(int), "key": F(str), "path": F(str), "n": F(int), "data": F(dict, OPT),
                   "slots": F(dict, OPT),      # (a geotile's: {road geotile key: [pooled shape path, serial]})
                   "frame": F(list, OPT), "pivot": F(NUM, OPT)},
    "brfar_drop": {"anchorId": F(int), "key": F(str)},
    # the AI road graph around the player (navgraph.py, --traffic on): a JSON file of nodes and edges
    "navgraph": {"anchorId": F(int), "path": F(str), "cx": F(NUM), "cy": F(NUM), "nodes": F(int), "edges": F(int),
                 "serial": F(int)},
    # the download cache's usage (cache.py; additive, with a Cache only): after hello and every mod 'cache'
    # ... its answer: the patch's shape and centre, or an error (the mod asks again later)
    "terrain_patch": {"anchorId": F(int), "key": F(str), "parts": F(list, OPT),     # [{path, x, y}]: small meshes
                      "tris": F(int, OPT), "error": F(str, OPT)},
    "app_paths": {"cacheDir": F(str), "cacheNext": F(str), "beammp": F(str), "result": F(dict, OPT)},
    "cache": {"v": F(int), "usedBytes": F(int), "capBytes": F(int), "files": F(int), "ready": F(bool),
              "managed": F(bool), "clearedBytes": F(int, OPT),
              "pinnedBytes": F(int, OPT)},     # the saved areas' files (areas.py; kept by a clear)
}
WEATHER_NESTED = {"cover": {"total", "low", "mid", "high"}, "wind": {"ms", "fromDeg", "gustMs"},
                  "aloft": {"ms", "fromDeg"}}
# message types the plain session cannot produce (each has its own session test below)
EXERCISED_ELSEWHERE = {"far_retex", "roads_tile", "roads_drop",   # roads: needs map data (roads tests)
                       "roads_skin", "roads_exact",                  # road geotiles (test_roadgeo)
                       "terrain_refresh",                            # a provisional first block: --grading (map data)
                       "update_status",                              # the player's "Update now" (test_selfupdate)
                       "where_result",                               # the status bar: asked by the mod while driving
                       "random_place_result", "place_previews_ready",  # the level select: map data / imagery
                                                                     # (test_randomplace)
                       "block_base",                                 # a lazy imagery source (test_detail)
                       "sea",                                        # --sea on: needs map data (test_seacarve)
                       "veg_tile", "veg_drop",                       # --trees on: needs map data
                       "bldfar_tile", "bldfar_drop",                 # --buildings on: needs map data
                       "bldsky_tile", "bldsky_drop", "brfar_tile", "brfar_drop",
                       "navgraph",                                   # --traffic on: needs map data
                       "map_tiles_ready",                            # only when the world map asks (map_tiles)
                       "update",                                     # only when GitHub has a newer version (online)
                       "geocode_result", "route_result",             # only when the map / GPS asks
                       "cache_map_result",                           # only when the map's cache view asks
                       "area_plan_result", "areas",                  # saved areas: a Server with a Cache (test_areas)
                       "route_download",                             # only when the GPS plans a route (routeahead.py)
                       "server_list"}                                # only when the BeamMP tab asks (online)
# events the mod's net.lua makes up itself (not on the wire)
MOD_INTERNAL = {"_connected": set(), "_disconnected": {"error"}}

LUA = modsim.MOD_DIR
GRAVITY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "egm2008_n36.npz"     # degree 36 (test_gravity)
# levels down to -2 (mesh mode); a textured tile's shape is its material name's pooled file (matslots.py)
# (geotiles phase 3b: a piece's key has a _c<z>_<x>_<y> suffix, a global tile's file is far_0_m<z>_<x>_<y>)
SHAPE_RE = r"^/temp/beamngEarth/[^/]+/(far_\d+_(-?\d+_-?\d+_-?\d+(_c\d+_\d+_\d+)?|m\d+_\d+_\d+)|slot_\d{5})\.cdae$"
HM_RE = r"^/temp/beamngEarth/[^/]+/hm_\d+_-?\d+_-?\d+\.png$"


def code_only(text, mark):
    """text without its whole-line comments (lines starting with mark: commented-out code is not part of the
    protocol, e.g. shared world building's messages, 2026-10-27)."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(mark))


def lua_tables(text, start):
    """Text of the Lua table constructor that opens at text[start] == '{' (balanced braces)."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unbalanced table")


def top_keys(table):
    """Keys `name = ...` of a constructor, and of the constructors nested one level down
    ({outer key: keys})."""
    keys, nested, depth, last = set(), {}, 0, None
    for m in re.finditer(r"\{|\}|(\w+)\s*=(?!=)", table):
        tok = m.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 1:
            keys.add(m.group(1))
            last = m.group(1)
        elif depth == 2 and last:
            nested.setdefault(last, set()).add(m.group(1))
    return keys, nested


def mod_sends():
    """{type: (keys, nested)} of every net.send({...}) in core.lua, places.lua (the level select's pictures) and
    mpsession.lua (require('beamngEarth/net').send), and every send({...}) in terrainpatch.lua (core hands it
    net.send)."""
    out = {}
    for names, pattern in ((("core.lua", "places.lua", "mpsession.lua", "serverlist.lua"), r"net(?:'\))?\.send\(\{"),
                           (("terrainpatch.lua",), r"\bsend\(\{")):
        text = code_only("\n".join((LUA / n).read_text(encoding="utf-8") for n in names), "--")
        for m in re.finditer(pattern, text):
            t = lua_tables(text, m.end() - 1)
            keys, nested = top_keys(t)
            kind = re.search(r"type\s*=\s*'(\w+)'", t).group(1)
            out.setdefault(kind, []).append((keys - {"type"}, nested))
    return out


def mod_reads():
    """Fields the mod reads from server messages: msg.X / p.msg.X / tr.X (terrain) in its Lua."""
    out = set()
    for name in ("core.lua", "farfield.lua", "lattice.lua", "weather.lua", "credits.lua", "roads.lua", "mpsession.lua"):
        text = code_only((LUA / name).read_text(encoding="utf-8"), "--")
        out |= set(re.findall(r"\bmsg\.(\w+)", text))
        out |= {"terrain." + f for f in re.findall(r"\btr\.(\w+)", text)}
    return out


def server_reads():
    text = code_only((Path(earthd.__file__)).read_text(encoding="utf-8"), "#")
    out = set(re.findall(r"msg\.get\(\"(\w+)\"", text)) | set(re.findall(r"msg\[\"(\w+)\"\]", text))
    out |= set(re.findall(r"_num\(msg, \"(\w+)\"", text))
    return out


def lua_function(text, name):
    """Body of `local function name(` / `function M.name(` / `name = function(` (a forward-declared
    local) up to the next top-level `end`; all of them if there are several (a wrapper and its body)."""
    ms = re.finditer(rf"^(?:(?:local )?function (?:M\.)?{name}\(|{name} = function\().*?^end", text, re.S | re.M)
    return "\n".join(m.group(0) for m in ms)


class AgreementTableTest(unittest.TestCase):
    def test_mod_sends_match_table(self):
        sends = mod_sends()
        self.assertEqual(set(sends), set(MOD_TO_SERVER), "message types the mod sends")
        for kind, variants in sends.items():
            want = MOD_TO_SERVER[kind]
            required = {f for f, (_, flags) in want.items() if OPT not in flags}
            for keys, nested in variants:
                self.assertLessEqual(required, keys, (kind, keys))
                self.assertLessEqual(keys, set(want), (kind, keys))
                if "cam" in nested:
                    self.assertEqual(nested["cam"], CAM)

    def test_server_reads_are_in_table(self):
        fields = {f for spec in MOD_TO_SERVER.values() for f in spec} | {"type"}
        self.assertLessEqual(server_reads(), fields)

    def test_mod_reads_are_in_table(self):
        sent = {f for spec in SERVER_TO_MOD.values() for f in spec} | {"type"}
        sent |= {f for spec in MOD_INTERNAL.values() for f in spec}
        sent |= {"terrain." + f for f in TERRAIN}
        unknown = mod_reads() - sent
        self.assertFalse(unknown, f"the mod reads fields no server message has: {sorted(unknown)}")


class SessionsAgreementTest(test_server.ServerHarness):
    """hello / pose `sessions` (docs/ROADMAP.md "Robustness" 3): the mod names the session folders its
    objects use, parsed from the server's paths by farfield.folderOf (the same pattern as the server's
    vfs_dir and modsim.folder_of); core.lua sends them in hello and every pose; the server adopts an
    earlier run's folder named in hello and releases it when a pose no longer names it."""

    def test_mod_side(self):
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        far = (LUA / "farfield.lua").read_text(encoding="utf-8")
        for kind in ("hello", "pose"):
            t = next(lua_tables(core, m.end() - 1) for m in re.finditer(r"net\.send\(\{", core)
                     if f"type = '{kind}'" in lua_tables(core, m.end() - 1))
            self.assertIn("sessions = farfield.heldSessions()", t, kind)
        self.assertIn("'^/temp/beamngEarth/([^/]+)/'", far)
        self.assertEqual(self.server.vfs_dir, f"/temp/beamngEarth/{self.server.sid}")
        self.assertEqual(modsim.folder_of(self.server.vfs_dir + "/far_1_0_0_0.cdae"), self.server.sid)
        self.assertEqual(modsim.folder_of(self.server.hm_vfs((1, 0, 0))), self.server.sid)
        self.assertIsNone(modsim.folder_of("/levels/beamng_earth/x.cdae"))
        self.assertIn("sessions", server_reads())

    def test_adopt_and_release(self):
        old = "20261010-120000-abcd"
        d = os.path.join(self.tmp.name, "temp", "beamngEarth", old)
        os.makedirs(d)
        for name in (earthd.SESSION_MARKER, "far_1_0_0_0.cdae"):
            open(os.path.join(d, name), "w").close()
        self.server.orphans[old] = 0.0                   # as found at start (Server._scan_sessions)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0",
                     "sessions": [old, self.server.sid]})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("sessions", m, "the server's hello is unchanged")
        self.assertEqual(self.conn().sessions, {old})
        self.c.send({"type": "pose", "anchorId": 99, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "lat": test_server.LAT,
                     "lon": test_server.LON, "speed": 0, "held": [], "sessions": [old]})
        time.sleep(0.2)
        self.assertTrue(os.path.isdir(d), "still named: kept")
        self.c.send({"type": "pose", "anchorId": 99, "x": 0, "y": 0, "z": 0, "vx": 0, "vy": 0, "lat": test_server.LAT,
                     "lon": test_server.LON, "speed": 0, "held": [], "sessions": {}})
        end = time.monotonic() + 10
        while os.path.isdir(d) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertFalse(os.path.isdir(d), "no longer named: released and deleted")
        self.assertNotIn(old, self.server.orphans)


class CreditsAgreementTest(unittest.TestCase):
    def test_credits_in_the_mod(self):
        """core.lua hands 'credits' to credits.lua and clears the previous session's on hello; credits.lua
        reads near / far (the lines the server builds); the overlay is on by default (CC BY)."""
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        on_msg = lua_function(core, "onMessage")
        self.assertIn("credits.onMessage(msg)", on_msg[on_msg.index("t == 'credits'"):])
        hello = on_msg[on_msg.index("t == 'hello'"):on_msg.index("t == 'status'")]
        self.assertIn("credits.clear()", hello)
        text = (LUA / "credits.lua").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"\bmsg\.(\w+)", text)), {"near", "far"})
        self.assertIn("M.enabled = true", text)
        settings = (LUA / "settings.lua").read_text(encoding="utf-8")
        self.assertRegex(settings, r"creditsVisible = \{ kind = 'bool', default = true")
        self.assertIn("M.onUiChangedState = onUiChangedState", core)


class CacheAgreementTest(unittest.TestCase):
    def test_cache_in_the_mod(self):
        """core.lua sends the page's cap on hello (settings.cacheCapGb) and keeps the server's 'cache'
        for the page (cleared on hello / disconnect); settings.lua's cap values are the page's options
        and the page's default is the server's."""
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        on_msg = lua_function(core, "onMessage")
        hello = on_msg[on_msg.index("t == 'hello'"):on_msg.index("t == 'status'")]
        self.assertIn("net.send({ type = 'cache', capGb = settings.cacheCapGb() })", hello)
        self.assertIn("cacheInfo = nil", hello)
        self.assertIn("cacheInfo = nil", on_msg[on_msg.index("t == '_disconnected'"):on_msg.index("t == 'hello'")])
        self.assertIn("cacheInfo = { usedBytes = tonumber(msg.usedBytes)", on_msg[on_msg.index("t == 'cache'"):])
        settings = (LUA / "settings.lua").read_text(encoding="utf-8")
        m = re.search(r"cacheCapGb = \{ kind = 'enum', default = '(\w+)', values = \{ ([^}]*) \}", settings)
        values = re.findall(r"'(\w+)'", m.group(2))
        self.assertEqual((m.group(1), values), ("20", ["5", "10", "20", "50", "unlimited"]))
        self.assertEqual(float(m.group(1)), cachemod.DEFAULT_CAP_GB, "the page's default is the server's")
        # (the settings pages' schema and state: earthSettingsStore.js, since the pause-menu tab, 2026-10-28)
        page = (LUA.parents[3] / "ui" / "ui-vue" / "mods" / "beamngEarth" / "earthSettingsStore.js").read_text(encoding="utf-8")
        row = re.search(r'\{ id: "cacheCapGb", type: "select", default: "(\w+)".*?options: \[(.*?)\] \}', page, re.S)
        self.assertEqual(row.group(1), m.group(1))
        self.assertEqual(re.findall(r'value: "(\w+)"', row.group(2)), values)
        self.assertIn("beamngEarth_settings.uiCacheClear()", page)


class HorizonAgreementTest(unittest.TestCase):
    def test_horizon_constants_and_mod(self):
        """M1h (docs/V1_4_SPEC.md §3.6): the sky's planet radius is the tiles' sphere, the mod's
        visibleDistance margin is the server's (radius + 10 %), and core.lua reads hello.horizon first
        thing after the block size and status.radius on every status; horizon.lua re-applies
        LevelInfo with postApply (in-game: without it the engine keeps the level's value)."""
        H = modsim.lua_consts("horizon.lua")
        self.assertEqual(H["PLANET_RADIUS_KM"] * 1000.0, ff.R_EARTH)
        self.assertEqual(H["VISIBLE_MARGIN"], ff.VISIBLE_MARGIN)
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        on_msg = lua_function(core, "onMessage")
        hello = on_msg[on_msg.index("t == 'hello'"):on_msg.index("t == 'status'")]
        self.assertIn("M.setHorizon(msg.horizon == true)", hello)
        status = on_msg[on_msg.index("t == 'status'"):on_msg.index("t == 'anchor_ready'")]
        self.assertIn("M.setFarRadius(msg.radius)", status)
        text = (LUA / "horizon.lua").read_text(encoding="utf-8")
        setter = lua_function(text, "setVisibleDistance")
        self.assertLess(setter.index("li.visibleDistance = v"), setter.index("li:postApply()"))
        self.assertIn("s.atmoPlanetRadius = M.PLANET_RADIUS_KM", lua_function(text, "applySky"))
        self.assertIn("horizon.restoreSky()", lua_function(core, "onClientEndMission"))
        self.assertIn("horizon.restoreSky()", lua_function(core, "onExtensionUnloaded"))


class ConstantsTest(unittest.TestCase):
    def test_protocol_and_format(self):
        """Protocol 4; the mod reads FORMAT 3 and 4 and says the newest in its hello; the server's
        format is its layout's (32 cells: 3, the default; 64: 4)."""
        self.assertEqual(modsim.CORE["PROTOCOL"], earthd.PROTOCOL)
        self.assertEqual(modsim.CORE["FORMAT"], ff.Layout(64).format)
        self.assertEqual(modsim.CORE["FORMAT"], max(modsim.CORE["FORMATS"]))
        self.assertEqual(set(modsim.CORE["FORMATS"]), set(ff.CONTENT_FORMAT.values()))
        self.assertEqual((ff.V131.format, ff.Layout(64).format), (3, 4))
        self.assertEqual(ff.Layout(earthd.CELLS_DEFAULT).format, 3)            # default: v1.3.1 until P8
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        on_msg = lua_function(core, "onMessage")
        self.assertIn("crossover.setCells(msg.cells)", on_msg)
        self.assertIn("farfield.setExactLevel(farfield.exactLevelFor(cells))", on_msg)
        self.assertIn("not formatKnown(msg.format)", on_msg)

    def test_lattice_and_quantum(self):
        L = modsim.LATTICE
        self.assertEqual((L["N"], L["SQUARE"], L["HALF"], L["GRID"]),
                         (heightmap.SIZE, heightmap.SQUARE, heightmap.HALF, heightmap.GRID))
        self.assertEqual(L["VALID_W"], heightmap.VALID)                     # 2046 m (§12 P1)
        self.assertEqual(tuple(L["Q_LADDER"]), heightmap.Q_LADDER)
        for q in heightmap.Q_LADDER:
            self.assertEqual(heightmap.block_params(1000.0, 1100.0, q)[2], q * L["Q_SCALE"])
        self.assertEqual(L["L0"], ff.BASE)
        self.assertEqual(2 * L["HALF"], 8 * ff.BASE)                        # a block sits on 8 x 8 L0 tiles
        # M4: lattice.configure(size) against the server's per-size geometry
        for size in ff.BLOCK_SIZES:
            t = modsim.lattice_table(size)
            self.assertEqual((t["N"], t["HALF"], t["VALID_W"], t["GRID"]),
                             (size, heightmap.half_extent(size), heightmap.valid(size), heightmap.grid(size)))
            self.assertEqual(ff.Layout(64, size).grid, t["GRID"])
            # the SW corner of a block on its grid is an even lattice index (Torque diagonal parity)
            self.assertEqual((t["GRID"] - t["HALF"]) / heightmap.SQUARE % 2, 0)
        self.assertEqual(modsim.XO["CELLS"], ff.V131.cells)             # without hello.cells (FORMAT 3)
        self.assertLess(modsim.FAR["EDGE_BAND"], ff.BASE)                   # the edge ring is one L0 tile deep

    def test_exact_level(self):
        """The mod's exactMiss metric (farfield.lua, V1_4_SPEC 3.0 M0): the exact level is the finest
        mesh level, whose vertices are the physics lattice, and the lookup reaches the coarsest
        level. (M1: the 64-cell layout makes L-1 exact; the mod's setExactLevel(-1) from hello.cells.)"""
        self.assertEqual(modsim.FAR["EXACT_LEVEL"], ff.MESH_MIN_LEVEL[32])
        self.assertEqual(modsim.FAR["MAX_LEVEL"], ff.MAX_LEVEL)
        for cells in ff.CELL_COUNTS:
            lay = ff.Layout(cells)
            self.assertEqual(ff.level_size(lay.exact_level), cells * heightmap.SQUARE)
            self.assertEqual(modsim.exact_level_for(cells), lay.exact_level)      # farfield.exactLevelFor
            self.assertEqual(lay.exact_level, lay.min_level)
        far = (LUA / "farfield.lua").read_text(encoding="utf-8")
        # the lookup key is the server's tile key (Tile.key: L_i_j, i = floor(x / S))
        self.assertIn("local k = l .. '_' .. floor(x / s) .. '_' .. floor(y / s)", lua_function(far, "shownAt"))
        self.assertEqual(ff.Tile(-2, 3, -1).key, "-2_3_-1")
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        self.assertIn("farfield.update(gov, (gov.w1 or dtReal * 1000) / 1000, st.cam, st.player)", core)

    @unittest.skipIf(shutil.which("luajit") is None, "luajit not installed")
    def test_default_err_and_exact_level_in_lua(self):
        """crossover.defaultErr after setCells (from hello.cells) and farfield.exactLevelFor agree
        with the server's layout for both cell counts: a quarter of the layout's vertex spacing,
        and the level on the physics lattice."""
        lua = (f"package.path = '{modsim.MOD_DIR.parent}/?.lua;' .. package.path\n"
               f"dofile('{modsim.MOD_ROOT.parent / 'tests' / 'stubs.lua'}')\n"
               "local X = require('beamngEarth/crossover')\n"
               "local F = require('beamngEarth/farfield')\n"
               "for _, c in ipairs({ 32, 64 }) do\n"
               "  X.setCells(c)\n"
               "  for l = -2, 9 do print(c, l, string.format('%.9f', X.defaultErr(l))) end\n"
               "  print(c, 'exact', F.exactLevelFor(c))\n"
               "end\n"
               "X.setCells(nil)\n"
               "print(0, 0, string.format('%.9f', X.defaultErr(0)))\n")
        out = subprocess.run(["luajit", "-e", lua], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = [ln.split("\t") for ln in out.stdout.splitlines()]
        for c, lvl, v in rows:
            c = int(c)
            if lvl == "exact":
                self.assertEqual(int(v), ff.Layout(c).exact_level)
            elif c == 0:                            # no hello.cells: the FORMAT 3 layout
                self.assertAlmostEqual(float(v), ff.V131.spacing(0) / 4, places=9)
            else:
                lay = ff.Layout(c)
                self.assertAlmostEqual(float(v), lay.spacing(int(lvl)) / 4, places=9)
                self.assertAlmostEqual(float(v), modsim.default_err(int(lvl), c), places=9)
        self.assertEqual(len(rows), 2 * 13 + 1)

    def test_terrain_follow_rule(self):
        F = modsim.FOLLOW
        self.assertEqual((F["PREPARE_DIST"], F["LOOKAHEAD_S"], F["REACH"]),
                         (earthd.FOLLOW_PREPARE, earthd.FOLLOW_LEAD, earthd.FOLLOW_REACH))
        # the player stays >= EDGE_MARGIN inside the collidable [cx-1024, cx+1022] on both sides
        self.assertLessEqual(F["REACH"], L_VALID_EDGE - F["EDGE_MARGIN"])
        self.assertLessEqual(earthd.ANCHOR_CENTRE_MAX, modsim.REBASE["MAX_DISTANCE"] + 5000)

    def test_follow_tables_per_block_size(self):
        """M4 (docs/V1_4_SPEC.md §3.5): the server's predicted follow rule per block size is the mod's
        table (flat *_1024 / *_512 constants) on both sides, including the 512 fast mode; each table
        keeps the player EDGE_MARGIN inside the collidable edge; the default stays 1024."""
        F = modsim.FOLLOW
        self.assertEqual(set(earthd.FOLLOW_RULES), set(ff.BLOCK_SIZES))
        self.assertEqual(earthd.BLOCK_DEFAULT, heightmap.SIZE)
        self.assertEqual(earthd.FOLLOW_RULES[1024], earthd.FollowRule(256.0, earthd.FOLLOW_PREPARE, earthd.FOLLOW_LEAD,
                                                                     earthd.FOLLOW_REACH, earthd.PREFETCH_START))
        for size, rule in earthd.FOLLOW_RULES.items():
            T = modsim.follow_table(size)
            self.assertEqual((rule.grid, rule.prepare, rule.lead, rule.reach),
                             (T["GRID"], T["PREPARE_DIST"], T["LOOKAHEAD_S"], T["REACH"]), size)
            self.assertEqual(rule.grid, heightmap.grid(size))
            self.assertEqual((rule.min_lead, rule.min_lead_v), (T["MIN_LEAD"], F["MIN_LEAD_V"]), size)
            edge = heightmap.valid(size) - heightmap.half_extent(size)            # +1022 / +510 m
            self.assertLessEqual(T["REACH"], edge - T["EDGE_MARGIN"])
            self.assertEqual(rule.fast_v, math.inf if T["FAST_MODE_V"] >= 1e9 else T["FAST_MODE_V"])
            self.assertLess(rule.prefetch_start, rule.prepare)
            self.assertLessEqual(T["PREPARE_DIST"], T["COMMIT_DIST"])
            self.assertLess(T["COMMIT_DIST"], T["HARD_COMMIT_DIST"])
        fast = earthd.FOLLOW_RULES[512]
        self.assertEqual((fast.fast_v, fast.fast_lead, fast.fast_reach, fast.fast_ahead),
                         (F["FAST_V"], F["LOOKAHEAD_FAST_S"], F["REACH_FAST"], F["PREP_AHEAD_FAST_S"]))
        self.assertLessEqual(F["REACH_FAST"], 510 - F["EDGE_MARGIN_FAST"])
        self.assertEqual(earthd.FOLLOW_RULES[512].prefetch_start, earthd.FOLLOW_RULES[512].prepare - 28)
        # the predicted request is the mod's target at the prepare point (both modes), 512 table
        for vx, vy, px, py in ((30.0, 0.0, 129.0, 0.0), (134.0, 0.0, 129.0, 0.0), (200.0, 0.0, 0.0, 0.0),
                               (-90.0, 60.0, -140.0, 40.0), (170.0, 120.0, 10.0, -20.0)):
            want = modsim.follow_decide(px, py, vx, vy, 0.0, 0.0, None, size=512)
            self.assertEqual(want[0], "prepare", (vx, vy))
            self.assertEqual(earthd.predict_centre(px, py, vx, vy, *fast.mode(vx, vy)[:2], fast.grid, fast.min_lead,
                                                   fast.min_lead_v), want[1:])

    def test_stale_dist_per_block_size(self):
        """rebase.lua STALE_DIST per block size equals that table's commit distance (a rebase never
        commits a block the follow rule would already replace), and the stale-anchor rule is 512² only."""
        for size in ff.BLOCK_SIZES:
            self.assertEqual(modsim.rebase_table(size)["STALE_DIST"], modsim.follow_table(size)["COMMIT_DIST"])
        self.assertEqual(modsim.REBASE["STALE_DIST"], modsim.REBASE["STALE_DIST_1024"])      # the default
        self.assertEqual((modsim.rebase_table(1024)["KEEP_EDGE"], modsim.rebase_table(512)["KEEP_EDGE"]), (0, 150))

    def test_block_size_in_the_mod(self):
        """core.lua sets the block size from hello.blockSize before anything reads the lattice, every
        module's configure is called, and the mod ships the layer / flat maps of each size."""
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        on_msg = lua_function(core, "onMessage")
        hello = on_msg[on_msg.index("t == 'hello'"):]
        self.assertLess(hello.index("M.setBlockSize(msg.blockSize or 1024)"), hello.index("requestAnchor("))
        setter = lua_function(core, "setBlockSize")
        for call in ("lattice.configure(", "follow.configure(n)", "terrain.configure(n)", "rebase.configure(n)"):
            self.assertIn(call, setter)
        terrain = (LUA / "terrain.lua").read_text(encoding="utf-8")
        for size in ff.BLOCK_SIZES:
            for kind, mode in (("layer", "RGB"), ("flat", "I;16")):
                name = f"beamngEarth_{kind}_{size}.png"
                self.assertIn(f"[{size}] = TERRAIN_DIR .. '{name}'", terrain)
                with Image.open(modsim.MOD_ROOT / "levels" / "beamng_earth" / "art" / "terrains" / name) as im:
                    self.assertEqual((im.size, im.mode), ((size, size), mode))

    def test_material(self):
        name = modsim.lua_string("farfield.lua", "MATERIAL")
        self.assertEqual(name, ff.MATERIAL)
        vfs = modsim.lua_string("farfield.lua", "MATERIALS_FILE")
        self.assertEqual(vfs, "/" + "/".join(ff.MOD_SHAPES) + "/main.materials.json")
        local = modsim.MOD_ROOT / vfs.lstrip("/")
        mats = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual(mats[name]["mapTo"], name)                        # P2: mapTo overrides the shape's own
        self.assertEqual(mats[name]["class"], "Material")
        palette = mats[name]["Stages"][0]["baseColorMap"]
        self.assertTrue((modsim.MOD_ROOT / palette.lstrip("/")).is_file(), palette)
        # the level loader picks up every *materials.json under the level folder, so the file must
        # live inside levels/beamng_earth
        self.assertTrue(vfs.startswith("/levels/beamng_earth/"))

    def test_material_loaded_before_first_tile(self):
        text = (LUA / "farfield.lua").read_text(encoding="utf-8")
        spawn = lua_function(text, "spawn")
        self.assertLess(spawn.index("ensureMaterial()"), spawn.index("createObject('TSStatic')"))
        self.assertIn("ensureMaterial()", lua_function(text, "switchAnchor"))
        ensure = lua_function(text, "ensureMaterial")
        self.assertIn("scenetree.findObject(M.MATERIAL)", ensure)          # loaded by the level already?
        self.assertIn("loadJsonMaterialsFile(M.MATERIALS_FILE)", ensure)
        terrain = (LUA / "terrain.lua").read_text(encoding="utf-8")
        create = lua_function(terrain, "getOrCreate")
        self.assertLess(create.index("ensureMaterials()"), create.index("TerrainBlock()"))

    def test_near_visual_values(self):
        """hello.nearVisual: the mod draws the ground with tiles for 'mesh', with tiles plus the drawn
        block for 'detail' (2026-10-12) and with the TerrainBlock for anything else (an older server
        sends none: the v1.2 look)."""
        self.assertEqual(set(earthd.NEAR_VISUALS), {"mesh", "terrain", "detail"})
        self.assertEqual(set(earthd.MESH_LAYOUT), {"mesh", "detail"})
        self.assertEqual(set(earthd.BLOCK_DRAWN), {"terrain", "detail"})
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        self.assertIn("nearVisual = (mode == 'mesh' or mode == 'detail') and mode or 'terrain'", lua_function(core, "setNearVisual"))
        far = (LUA / "farfield.lua").read_text(encoding="utf-8")
        self.assertIn("mode = (mode == 'mesh' or mode == 'detail') and mode or 'terrain'", lua_function(far, "setNearVisual"))

    def test_detail_base_photo(self):
        """Detail mode: terrain.base / baseSize (earthd.terrain_msg) reach terrain.load through core's
        loadBlock; the base texture slot of the shipped texture set is the server's photo size."""
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        load = lua_function(core, "loadBlock")
        self.assertIn("tr.base", load)
        self.assertIn("tonumber(tr.baseSize)", load)
        level = LUA.parents[3] / "levels" / "beamng_earth" / "art" / "terrains" / "main.materials.json"
        mats = json.loads(level.read_text(encoding="utf-8"))
        ts = mats["beamngEarthTerrainMaterialTextureSet"]
        self.assertEqual(ts["baseTexSize"], [earthd.BASE_PX, earthd.BASE_PX])

    def test_imagery_material(self):
        """satmat.lua builds the Material the way the server's shapes and textures need: mapTo =
        the far_tile's material (the name in the shape), colorMap = its texture, the detail map on
        UV1 at detailScale 0.5 (UV1 = metres / DETAIL_M, so one repeat per 2 * DETAIL_M); the
        detail texture ships with the mod (tools/make_detail_texture.py: mean 0.5 grey, fading
        mips)."""
        text = (LUA / "satmat.lua").read_text(encoding="utf-8")
        create = lua_function(text, "create")
        self.assertIn("m:setField('mapTo', 0, name)", create)
        self.assertIn("setTexture(m, texture, detail)", create)          # (the near tiles' asphalt layer with it)
        self.assertIn("m:setField('colorMap', 0, texture)", lua_function(text, "setTexture"))
        self.assertIn("m:registerObject(name)", create)
        self.assertIn("m:setField('detailMap', 0, M.DETAIL_MAP)", create)
        self.assertRegex(text, r"\{ 'detailMapUseUV', '1' \}")
        self.assertRegex(text, r"\{ 'detailScale', '0.5 0.5' \}")
        # farfield passes the far_tile's fields straight through
        far = (LUA / "farfield.lua").read_text(encoding="utf-8")
        self.assertIn("satmat.create, inst.material, inst.texture, inst.detail", far)
        detail = modsim.lua_string("satmat.lua", "DETAIL_MAP")
        mod_file = modsim.MOD_ROOT / detail.lstrip("/")      # mod root = the VFS root
        self.assertTrue(mod_file.is_file(), mod_file)
        data = mod_file.read_bytes()
        self.assertEqual(data[:4], b"DDS ")
        self.assertGreater(struct.unpack_from("<I", data, 28)[0], 1)   # has mips (the fade)

    def test_shape_kind_from_extension(self):
        text = (LUA / "farfield.lua").read_text(encoding="utf-8")
        self.assertIn("%.cdae$", lua_function(text, "kindOf"))
        self.assertLessEqual({"cdae", "dae"}, set(modsim.GOV["PRIOR"]))
        self.assertEqual(set(ff.FORMATS), {"cdae", "dae"})

    def test_texture_kinds(self):
        """M1: far_tile.tex picks the governor kind; every texture size a layout uses by default has
        its own prior (cdaeTex128/256/512, measured in-game by P8: the shape dominates, so the priors
        are close and grow with the texture), and a FORMAT 3 tile (no tex) stays 'cdaeTex'."""
        far = (LUA / "farfield.lua").read_text(encoding="utf-8")
        self.assertIn("inst.material and governor.texKind(inst.tex) or 'cdae'", lua_function(far, "kindOf"))
        self.assertIn("tex = tonumber(msg.tex)", far)
        prior = modsim.GOV["PRIOR"]
        for px in sorted({p for t in ff.TEX_DEFAULT.values() for p in t} | {128}):
            self.assertEqual(modsim.tex_kind(px), f"cdaeTex{px}")
            self.assertGreater(prior[f"cdaeTex{px}"], 0)
        self.assertLess(prior["cdaeTex128"], prior["cdaeTex256"])
        self.assertLess(prior["cdaeTex256"], prior["cdaeTex512"])
        self.assertEqual(modsim.tex_kind(None), "cdaeTex")


L_VALID_EDGE = modsim.LATTICE["VALID_W"] - modsim.LATTICE["HALF"]      # +1022 m from the centre


class ServerMessagesTest(test_server.ServerHarness):
    """Everything the server sends in a session, checked against SERVER_TO_MOD."""

    def setUp(self):
        super().setUp()
        self.server.weather = weather.Service(fetch=test_weather.FakeFetch())     # no network
        self.server.gravity = gravitymod.Provider(model=gravitymod.GravityModel.from_npz(GRAVITY_FIXTURE),
                                                  download=False)               # no network either
        self.server.credits = creditsmod.Credits(interval=0.1)
        # the synthetic DEM's tiles as if Mapterhorn served them (dem.Dem.served): tiles with sources,
        # so 'credits' goes out (the weather alone sends none)
        self.server.dem.served = types.SimpleNamespace(get=lambda key: creditsmod.demmod.MAPTERHORN["name"])
        self.server.cache = cachemod.Cache(os.path.join(self.tmp.name, "cache"), 20, start=False)
        self.server.cache.scan()

    def check(self, m):
        kind = m["type"]
        self.assertIn(kind, SERVER_TO_MOD, m)
        spec = SERVER_TO_MOD[kind]
        fields = set(m) - {"type"}
        self.assertLessEqual(fields, set(spec), m)
        for f, (types, flags) in spec.items():
            if f not in m:
                self.assertTrue(OPT in flags, (f, m))
                continue
            if m[f] is None:
                self.assertIn(NULL, flags, (f, m))
                continue
            self.assertIsInstance(m[f], types, (f, m))
            if isinstance(m[f], float):
                self.assertTrue(math.isfinite(m[f]), (f, m))
        if kind == "far_tile":
            self.assertRegex(m["shape"], SHAPE_RE)
            self.assertIn(m["state"], ("group", "prefetch"))
            self.assertEqual(len(m["pos"]), 3)
            # sink: on L0 in terrain near-visual mode only (mesh mode covers nothing)
            self.assertEqual(m["level"] == 0 and self.near_visual == "terrain", "sink" in m, m)
            # imagery: every tile carries its material, unless imagery is off
            self.assertEqual("material" in m, self.with_imagery, m)
            # M1: the texture size with FORMAT 4 (the governor kind), never with FORMAT 3
            self.assertEqual("tex" in m, self.with_imagery and self.cells == 64, m)
            if "tex" in m:
                self.assertEqual(m["tex"], self.server.tex_size(m["level"]))
                self.assertGreaterEqual(m["level"], ff.MESH_MIN_LEVEL[64])
            self.assertEqual(m["key"], ff.tile_of(m["key"]).key)
        if kind in ("anchor_ready", "terrain_ready"):
            t = m["terrain"] if kind == "anchor_ready" else {f: m[f] for f in TERRAIN if f in m}
            required = {f for f, (_, flags) in TERRAIN.items() if OPT not in flags}
            self.assertLessEqual(required, set(t))
            self.assertLessEqual(set(t), set(TERRAIN))
            for f, (types, _) in TERRAIN.items():
                if f in t:
                    self.assertIsInstance(t[f], types, (f, t))
            self.assertRegex(t["heightmap"], HM_RE)
            # M4: the session's block size (hello.blockSize), on its grid
            self.assertEqual((t["size"], t["squareSize"]), (self.server.layout.block, modsim.LATTICE["SQUARE"]))
            grid = modsim.lattice_table(t["size"])["GRID"]
            self.assertEqual((t["cx"] % grid, t["cy"] % grid), (0, 0))
            self.assertEqual(t["maxHeight"], t["q"] * modsim.LATTICE["Q_SCALE"])
            self.assertEqual(t["zBase"] % t["q"], 0)
        if kind == "far_swap":
            self.assertTrue(all(isinstance(k, str) for k in m["add"] + m["remove"]))
        if kind == "far_retex":                 # the next revision's file next to the shape
            self.assertRegex(m["texture"], rf"^/temp/beamngEarth/[^/]+/far_{m['anchorId']}_{m['key']}_r\d+\.dds$")
            self.assertEqual(m["key"], ff.tile_of(m["key"]).key)
        if kind == "status":                    # M1h: the radius with --horizon only, at least 80 km
            self.assertEqual("radius" in m, self.horizon, m)
            if self.horizon:
                self.assertTrue(ff.HORIZON_MIN <= m["radius"] <= ff.HORIZON_MAX, m)
        if kind == "far_tile":
            self.assertLessEqual(m["level"], ff.HORIZON_MAX_LEVEL if self.horizon else ff.MAX_LEVEL)
        if kind == "weather":
            for k, fields in WEATHER_NESTED.items():
                if k in m:
                    self.assertEqual(set(m[k]), fields, (k, m))
                    self.assertTrue(all(isinstance(v, NUM) for v in m[k].values()), (k, m))
            self.assertEqual((m["v"], m["credit"]), (weather.VERSION, weather.CREDIT))
        if kind == "gravity":
            self.assertEqual((m["v"], m["source"]), (gravitymod.MSG_VERSION, "egm2008"))
            self.assertTrue(9.75 < m["g"] < 9.85, m)
        if kind == "credits":
            self.assertEqual(m["v"], creditsmod.VERSION)
            self.assertTrue(m["near"] or m["far"], m)
            for f in ("ids", "farIds"):
                self.assertTrue(all(isinstance(i, str) and (i in creditsmod.ORDER or i.startswith("img:"))
                                    for i in m[f]), m)
            self.assertFalse(set(m["ids"]) & set(m["farIds"]), m)
            self.assertEqual(m["near"], creditsmod.line(m["ids"]))
            self.assertEqual(m["far"], creditsmod.line(m["farIds"], far=True))
        if kind == "cache":
            self.assertEqual(m["v"], cachemod.MSG_VERSION)
            self.assertTrue(m["ready"] and m["managed"], m)
            self.assertIn(m["capBytes"], (20_000_000_000, 10_000_000_000))
        if kind == "far_tile" and "material" in m:
            self.assertTrue("texture" in m and "detail" in m, m)
            self.assertRegex(m["texture"], r"^/temp/beamngEarth/[^/]+/[^/]+\.dds$")
            # the name the shape's material slot carries: a pooled one (matslots.py; satmat.pooled)
            self.assertRegex(m["material"], r"^bngEarthFar_[0-9a-f]{4}_\d{5}$")
            # in the shape's folder; the shape is the name's pooled file (slot_<n>.cdae, matslots.py)
            self.assertEqual(m["texture"].rsplit("/", 1)[0], m["shape"].rsplit("/", 1)[0])
            self.assertEqual(m["shape"].rsplit("/", 1)[1], f"slot_{m['material'].rsplit('_', 1)[1]}.cdae")
            self.assertIs(m["detail"], True)
        if kind == "hello":
            self.assertIn(m["nearVisual"], ("mesh", "terrain"))
            self.assertEqual(m["format"], self.server.layout.format)
            self.assertIn(m["format"], modsim.CORE["FORMATS"])
            self.assertEqual(m.get("cells"), 64 if self.cells == 64 else None)
            self.assertEqual(m.get("blockSize"), None if self.block == 1024 else self.block)       # M4
            self.assertEqual(m.get("horizon"), True if self.horizon else None)                    # M1h
            self.assertEqual(m["material"], modsim.lua_string("farfield.lua", "MATERIAL"))
            self.assertIn(m["farFormat"], ff.FORMATS)

    def test_session_messages(self):
        c = self.c
        seen = []

        def until(pred, timeout=60):
            m, before = c.until(pred, timeout)
            seen.extend(before + [m])
            return m

        c.send({"type": "hello", "protocol": modsim.CORE["PROTOCOL"], "format": modsim.CORE["FORMAT"],
                "modVersion": "0.4.0"})
        until(lambda m: m["type"] == "hello")
        until(lambda m: m["type"] == "cache")           # the download cache's usage follows hello
        c.send({"type": "cache", "capGb": 10})          # the page's size cap ...
        self.assertEqual(until(lambda m: m["type"] == "cache")["capBytes"], 10_000_000_000)
        c.send({"type": "cache", "clear": True})        # ... and "Clear cache"
        self.assertIn("clearedBytes", until(lambda m: m["type"] == "cache"))
        c.send({"type": "app_path"})                    # the page's cache folder and BeamMP launcher (no choice made)
        self.assertTrue(until(lambda m: m["type"] == "app_paths")["cacheDir"])
        # (shared world building, commented out 2026-10-27)
        # c.send({"type": "share"})                       # a BeamMP session: our group service's port and code
        # self.assertTrue(until(lambda m: m["type"] == "share")["code"])
        # c.send({"type": "peers", "me": "1", "members": []})     # (alone: no group; nothing answered)
        c.send({"type": "prepare_anchor", "requestId": 1, "lat": test_server.LAT, "lon": test_server.LON,
                "cx": 0, "cy": 0})
        a = until(lambda m: m["type"] == "anchor_ready")
        aid = a["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0, 0)
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == aid, 120)
        self.pose(aid, 600, 0, vx=30)                   # LOD swaps (both sides) + the predicted block
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == aid, 120)
        c.send({"type": "prepare_terrain", "requestId": 2, "anchorId": aid, "cx": 512, "cy": 0})
        until(lambda m: m["type"] == "terrain_ready")
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 512, "cy": 0})
        seen.extend(c.drain(0.3))                       # a commit changes no far tile: nothing to wait for
        c.send({"type": "prepare_terrain", "requestId": 3, "anchorId": 999, "cx": 0, "cy": 0})
        until(lambda m: m["type"] == "terrain_error")
        # the ground's collision patch under another vehicle's island off the block (terrainpatch.lua / .py)
        c.send({"type": "terrain_patch", "anchorId": aid, "key": "t1", "x0": 900, "y0": -100, "x1": 1100, "y1": 100})
        patch = until(lambda m: m["type"] == "terrain_patch")
        self.assertTrue(patch.get("parts") and patch["tris"] > 0, patch)
        self.assertTrue(all(os.path.exists(self.fs(p["path"])) for p in patch["parts"]), patch)
        c.send({"type": "prepare_anchor", "requestId": 4, "lat": 123.0, "lon": 0})
        until(lambda m: m["type"] == "anchor_error")
        # a second anchor 2 km east (a rebase): the previous anchor's tiles get far_removes
        lat, lon, _ = self.server.anchors[aid].frame.enu_to_geodetic(2000.0, 0.0, 0.0)
        c.send({"type": "prepare_anchor", "requestId": 5, "lat": float(lat), "lon": float(lon), "cx": 0, "cy": 0})
        b = until(lambda m: m["type"] == "anchor_ready")
        c.send({"type": "commit_anchor", "anchorId": b["anchorId"]})
        c.send({"type": "commit_terrain", "anchorId": b["anchorId"], "cx": 0, "cy": 0})
        self.pose(b["anchorId"], 0, 0)
        until(lambda m: m["type"] == "far_done" and m["anchorId"] == b["anchorId"], 120)
        seen += c.drain(0.3)
        kinds = {m["type"] for m in seen}
        self.assertEqual(kinds, set(SERVER_TO_MOD) - EXERCISED_ELSEWHERE,
                         "a message type was not exercised or is not in the table")
        for m in seen:
            self.check(m)
        old_removes = [m for m in seen if m["type"] == "far_remove" and m["anchorId"] == aid]
        self.assertTrue(old_removes)
        textured = [m for m in seen if m["type"] == "far_tile" and "material" in m]
        self.assertEqual(bool(textured), self.with_imagery)
        for m in textured[::max(1, len(textured) // 12)]:
            self.check_textured(m)

    def check_textured(self, m):
        """The files a textured far_tile names, as the mod and the engine use them."""
        shape = cdae.read(self.fs(m["shape"]))
        self.assertEqual([x["name"] for x in shape["materials"]], [m["material"]])   # mapTo target
        mesh = cdae.mesh_arrays(shape["meshes"][0])
        v, uv0, uv1 = mesh["verts"], mesh["tverts"], mesh["tverts2"]
        size = ff.tile_of(m["key"]).size
        self.assertEqual(len(uv1), len(v))
        with open(self.fs(m["texture"]), "rb") as f:
            px = imagery.read_dds_info(f.read())[0]
        # UV0: the footprint inside the texture's gutter, v stored flipped (the engine's
        # convention): u = gutter_uv(x / S), 1 - v = gutter_uv(y / S), so texture row 0 is north
        self.assertTrue(np.allclose(uv0[:, 0], imagery.gutter_uv(v[:, 0] / size, px), atol=1e-6))
        self.assertTrue(np.allclose(uv0[:, 1], 1 - imagery.gutter_uv(v[:, 1] / size, px), atol=1e-6))
        # UV1: tile-local metres / DETAIL_M, flipped the same way; world-locked because the tile
        # origin is a multiple of DETAIL_M
        self.assertTrue(np.allclose(uv1[:, 0], v[:, 0] / ff.DETAIL_M, atol=1e-4))
        self.assertTrue(np.allclose(uv1[:, 1], 1 - v[:, 1] / ff.DETAIL_M, atol=1e-4))
        self.assertEqual(m["pos"][0] % ff.DETAIL_M, 0)
        self.assertEqual(m["pos"][1] % ff.DETAIL_M, 0)
        with open(self.fs(m["texture"]), "rb") as f:
            data = f.read()
        w, h, mips, fourcc, sizes = imagery.read_dds_info(data)
        self.assertEqual((w, h, fourcc), (self.server.tex_size(m["level"]),) * 2 + (b"DXT1",))
        self.assertEqual(mips, w.bit_length())                 # full chain down to 1 x 1
        self.assertEqual(len(data), imagery.DDS_HEADER + sum(sizes))


class DeshadowHelloTest(test_server.ServerHarness):
    """hello.deshadow: absent with --deshadow off (the default hello is unchanged), the mode otherwise."""

    def test_hello_deshadow(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("deshadow", m)
        self.server.deshadow = types.SimpleNamespace(mode="full", prepare=lambda lat, lon: None)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual(m["deshadow"], "full")
        self.assertIn("deshadow", SERVER_TO_MOD["hello"])
        self.assertIn(m["deshadow"], deshadowmod.MODES)


class ColormatchHelloTest(test_server.ServerHarness):
    """hello.colormatch: absent with --colormatch off (the default hello is unchanged), the mode otherwise."""

    def test_hello_colormatch(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("colormatch", m)
        self.server.colormatch = types.SimpleNamespace(mode="s2", prepare=lambda lat, lon: None)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual(m["colormatch"], "s2")
        self.assertIn("colormatch", SERVER_TO_MOD["hello"])
        self.assertIn(m["colormatch"], colormatchmod.MODES)


class MapPortHelloTest(test_server.ServerHarness):
    """hello.mapPort: absent without the map tile endpoint (the default hello is unchanged), its port
    otherwise; the mod keeps it for the world map (core.lua st.mapPort)."""

    def test_hello_map_port(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertNotIn("mapPort", m)
        self.server.map_port = 47999
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual(m["mapPort"], 47999)
        self.assertIn("mapPort", SERVER_TO_MOD["hello"])
        self.assertIn("msg.mapPort", (LUA / "core.lua").read_text(encoding="utf-8"))


class RetexMessagesTest(ServerMessagesTest):
    """far_retex (--colormatch / --deshadow): a session whose colour-match regions turn ready after the
    far field is shown (test_retex.FakeColorMatch); every message checked against the table, the
    re-rendered textures exist, and the mod routes far_retex to farfield.onRetex."""
    near_visual = "mesh"
    with_imagery = True
    cells = 64

    def test_session_messages(self):
        cm = test_retex.install_colormatch(self)
        self.server.retex_rate = 200.0
        c = self.c
        c.send({"type": "hello", "protocol": modsim.CORE["PROTOCOL"], "format": modsim.CORE["FORMAT"],
                "modVersion": "0.4.0"})
        seen = [c.until(lambda m: m["type"] == "hello")[0]]
        c.send({"type": "prepare_anchor", "requestId": 1, "lat": test_server.LAT,
                "lon": test_retex.MeshHarness.LON, "cx": 0, "cy": 0})
        a, before = c.until(lambda m: m["type"] == "anchor_ready")
        seen += before + [a]
        aid = a["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0, 0)
        m, before = c.until(lambda m: m["type"] == "far_done", 120)
        seen += before + [m]
        regions = {rk for t in self.server.tiles.values() for rk in t.cm_pending}
        self.assertTrue(regions)
        for rk in sorted(regions):
            cm.make_ready(rk)
        m, before = c.until(lambda m: m["type"] == "far_retex", 60)
        seen += before + [m] + c.drain(1.0)
        retex = [m for m in seen if m["type"] == "far_retex"]
        self.assertTrue(retex)
        for m in seen:
            self.check(m)
        for m in retex:
            self.assertTrue(os.path.exists(self.fs(m["texture"])), m)
        core = (LUA / "core.lua").read_text(encoding="utf-8")
        self.assertRegex(core, r"t == 'far_retex' then\s+farfield\.onRetex\(msg\)")
        self.assertIn("function M.onRetex(msg)", (LUA / "farfield.lua").read_text(encoding="utf-8"))


class MeshImageryMessagesTest(ServerMessagesTest):
    """The same session with the v1.3 defaults: --near-visual mesh, imagery on (synthetic)."""
    near_visual = "mesh"
    with_imagery = True


class MeshImagery64MessagesTest(MeshImageryMessagesTest):
    """The same with --cells 64 (docs/V1_4_SPEC.md §3.1 M1): FORMAT 4, hello.cells, far_tile.tex."""
    cells = 64


class MeshImagery64Block512MessagesTest(MeshImagery64MessagesTest):
    """The same with --block 512 (docs/V1_4_SPEC.md §3.5 M4): hello.blockSize, terrain.size 512 on the
    128 m grid."""
    block = 512



class MeshImagery64HorizonMessagesTest(MeshImagery64MessagesTest):
    """The same with --horizon (docs/V1_4_SPEC.md §3.6 M1h): hello.horizon, status.radius, tiles up to
    L10 with the coarse textures."""
    horizon = True


if __name__ == "__main__":
    unittest.main()
