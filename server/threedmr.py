"""3dmr.eu model auto-fetch: buildings.road_tile_items finds nearby models by geo-search (search_ids, within a
road tile's bbox; model_info for each hit's own lat/lon), pulls the match's GLB from
https://3dmr.eu/api/model/<id>, bakes it into a .cdae + texture next to the tile that needs it, and registers
a material for it in the mod. (tag_3dmr/TAG_KEY are the earlier mechanism, a mapper's 3dmr=<id> tag on an
Overture feature's source_tags: buildings has no source_tags column, so they are unused now but kept, tests
still cover them.)

GLB decoding is a small, read-only glTF 2.0 subset (stdlib json/struct + numpy for the accessor views and the
node transforms): embedded buffer only (the BIN chunk), triangle-list primitives, one combined mesh per model
(every primitive's vertices concatenated, its triangles' indices shifted). A model with several materials/
textures gets one texture slot per distinct material (gltf_mesh's tri_materials indexes into its textures
list), written out as one .cdae material per slot (cdae.encode's own several-materials support); untextured
models/slots get a grey fallback material instead of failing. Normalized integer accessors (KHR_mesh_
quantization: u8/u16 positions, scaled back up by the node's own transform) are converted to float the way
the spec requires before any node matrix/scale is applied; a primitive whose accessors carry no bufferView
(KHR_draco_mesh_compression: we do not decode Draco) is skipped rather than raising, and a model whose every
primitive is unsupported this way is dropped as a whole (UnsupportedModelError). A primitive's own accessor
min/max bounds are not re-validated vertex by vertex (trusted); but a POSITION accessor's own vertices that
come out non-finite (NaN/inf, observed on a few upstream models) are dropped along with the triangles that
reference them, rather than poisoning the whole mesh's bounding box.

Downloads go straight through urllib (3dmr.eu is not one of netfetch's policy hosts: one file per model id,
search or info response, cached forever under <cache_dir>/threedmr_cache/). A GLB's own header (bytes 8:12)
gives its true length, so a stream cut short by a restrictive network (observed: ~16 KB then silence) is
detected instead of baked into a broken shape; retried once through curl (bound to a network interface when
the caller passes one: iface).
"""

import contextlib
import hashlib
import io
import json
import logging
import math
import threading
import time
import os
import re
import struct
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np
import dmath
import dnp
from PIL import Image

import cdae
import netfetch
import staging

log = logging.getLogger("earthd.threedmr")

TAG_KEY = "3dmr"
API_URL = "https://3dmr.eu/api/model/{}"
SEARCH_URL = "https://3dmr.eu/api/search/{lat:.6f}/{lon:.6f}/{range_m}"
INFO_URL = "https://3dmr.eu/api/info/{id}"
FEATURE_VERSION = 29  # what a baked model is made of: in the roads revisit-cache key and the model bridges' cached
# decks (bridge_<id>_v<n>.json); bumped whenever a model is baked differently (the sections, the OSM yaw, the bridge
# deck, the one-sided materials) or a tile with a model bridge is built differently round it (28: model_position's
# new order - a tagged OSM way's own centroid, then buildings._custom_tile_entries' nearest-footprint level - can
# move a model that used to sit at its stale api point; 29, GitHub #1071: a bridge by its 3dmr.eu tags or its
# turned bounding box, its deck traced along its own axis past its piers, laid onto its mapped bridge, the rail and
# walkway bridges under it left out too)
# The settings page's "3D models from 3dmr.eu" switch (cpupin.apply_player_settings sets BNE_3DMR=off when it is off;
# a BNE_ variable, so revisit.code_key keeps what was built with and without the models apart): off, search_ids
# finds no model (the player's own local models stay)
ENABLE_ENV = "BNE_3DMR"
NO_YAW_ENV = "BNGE_3DMR_NO_YAW"  # set: skip osm_yaw's OSM lookup entirely, model placed as downloaded
# (a fallback if OSM is unreachable from a session)
NO_REPOSITION_ENV = "BNGE_3DMR_NO_REPOSITION"  # set: skip model_position's OSM-polygon level and buildings.
# _custom_tile_entries' nearest-footprint level - a model sits at its POSITION_OVERRIDES/threedmr_position_fixes.json
# entry if it has one, else its own api point, exactly as before this feature (owner debugging switch)
MOD_MATERIALS_REL = (
    "mods",
    "unpacked",
    "beamng_earth",
    "levels",
    "beamng_earth",
    "art",
    "shapes",
    "beamngEarth",
)
MATERIALS_FILE = "three_dmr.materials.json"
# The translucency fields a material carries at its TOP level (BeamNG reads them there only, never off a
# Stage - example_glass_dmg): _material_stage's second return value's keys, and what update_materials'
# no-op check compares between an existing entry and the freshly built one. translucent/translucentBlendOp
# are NOT here: those are only valid on a "version": 1.5 (PBR) material - example_glass_dmg is one, but this
# mod's own materials (classic format: colorMap/diffuseColor Stages, no "version") are not, and putting them
# at the top level of a classic material gets the whole material rejected by the engine (every mesh using it
# shows as "NO MATERIAL", observed after the 2026-10-25 test build: every 3dmr-fetched material broke at
# once). sign_speed100kph (sign_speed.materials.json), a classic-format material confirmed working in-game,
# carries alphaTest/alphaRef at the top level and nothing else - that is the only translucency this format
# supports: alphaTest cuts a colorMap's alpha channel out pixel by pixel, no blending.
TRANSLUCENCY_KEYS = ("alphaTest", "alphaRef")
# A local, no-3dmr-base model catalog, next to (not inside) the 3dmr.eu GLB cache: <cache_dir>/threedmr_cache/
# local_models.json - the player's own .stl/.glb files, placed in that same folder by hand (the
# repository's local_models/ folder is an example), for a mesh that has no 3dmr.eu id/page to fetch from at all. local_models/
# load_local_model below are this catalog's own counterparts of search_ids/model_position/fetch_model/
# gltf_mesh - road_tile_items and buildings._custom_tile_entries fold its entries into the same custom list
# a 3dmr.eu geo-search hit lands in, told apart there only by a "local" key (never a numeric 3dmr id).
LOCAL_MODELS_FILE = "local_models.json"


class UnsupportedModelError(Exception):
    """gltf_mesh: every primitive of the model used geometry this decoder can't read (Draco-compressed, so
    far - we don't implement Draco). Raised instead of returning an empty mesh, so the caller treats the
    model as absent rather than placing a TSStatic with nothing in it."""


def tag_3dmr(tags):
    """The "3dmr" value of an Overture source_tags JSON string (or None: absent, not a model tag, or the
    string is not valid JSON - a mapper's tag is free text, not a schema)."""
    if not tags:
        return None
    try:
        d = json.loads(tags)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    v = d.get(TAG_KEY)
    return None if v is None else str(v)


# --- download ---------------------------------------------------------------------------
def _glb_complete(data):
    """Whether data holds a whole GLB file (its own header states the true byte length)."""
    if len(data) < 12 or data[:4] != b"glTF":
        return False
    total_len = struct.unpack_from("<I", data, 8)[0]
    return len(data) >= total_len


MAX_DOWNLOAD = 64 << 20         # bytes: a model (or a search/info reply) larger than this is refused
MAX_TRIANGLES = 400_000         # a model with more is left out (gltf_mesh): the pure-Python baking per triangle (id 8:
                                # ~1.1 M) would hold a road tile for a long time


def _urllib_fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": netfetch.USER_AGENT})
    # (Windows: the system's certificate check, as netfetch's own requests: OpenSSL's chain took an expired
    # intermediate for 3dmr.eu's Let's Encrypt YE1 certificate, "certificate has expired", 2026-11-03; GitHub #567)
    with urllib.request.urlopen(req, timeout=timeout, context=netfetch.windows_ssl_context()) as resp:
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise OSError(f"reply over {MAX_DOWNLOAD >> 20} MB")
    return data


def _curl_fetch(url, iface, timeout):
    proc = subprocess.run(
        ["curl", "-s", "--interface", iface, "-m", str(int(timeout)), url],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout


def _fetch_retry(url, iface, timeout, complete=lambda data: True, what="request"):
    """GET url through urllib, retried once through curl bound to iface (the caller's iface, if any) when
    the first try raised or its result fails `complete` (a restrictive network cuts a stream short, observed
    ~16 KB then silence, for GLBs). b"" when both fail (or there is no interface to retry through)."""
    try:
        data = _urllib_fetch(url, timeout)
    except (urllib.error.URLError, OSError) as e:
        log.warning("3dmr %s: download failed (%s)", what, e)
        data = b""
    if not complete(data) and iface:
        data = _curl_fetch(url, iface, timeout)
    return data


def fetch_model(model_id, cache_dir, iface=None, timeout=120.0):
    """The GLB bytes of a 3dmr.eu model, from <cache_dir>/threedmr_cache/<id>.glb if already cached there (a
    file over 1000 bytes: a previous download that finished), else downloaded and cached. iface: a network
    interface name to retry a truncated download through curl (the caller's iface, if any)."""
    cache = os.path.join(cache_dir, "threedmr_cache")
    path = os.path.join(cache, f"{model_id}.glb")
    try:
        if os.path.getsize(path) > 1000:
            with open(path, "rb") as f:
                log.info("3dmr model %s: cached", model_id)
                return f.read()
    except OSError:
        pass

    url = API_URL.format(model_id)
    data = _fetch_retry(url, iface, timeout, _glb_complete, what=f"model {model_id}")

    if not _glb_complete(data):
        raise RuntimeError(
            f"3dmr model {model_id}: download incomplete ({len(data)} bytes) from {url}"
        )

    os.makedirs(cache, exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)
    log.info("3dmr model %s: downloaded %d bytes", model_id, len(data))
    return data


def _cached_json(path, url, iface, timeout, what):
    """The JSON at path, or fetched from url (through _fetch_retry) and cached there (atomically) first."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    data = json.loads(_fetch_retry(url, iface, timeout, what=what))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(data, f)
    staging.replace(tmp, path)
    return data


def enabled():
    """Are the 3dmr.eu models on (ENABLE_ENV not "off")?"""
    return os.environ.get(ENABLE_ENV, "on") != "off"


def search_ids(lat, lon, range_m, cache_dir, iface=None, timeout=30.0):
    """Model ids within range_m metres of (lat, lon) (https://3dmr.eu/api/search/<lat>/<lon>/<range>), cached
    per rounded (lat, lon, range) under <cache_dir>/threedmr_cache/search_<lat>_<lon>_<range>.json. [] on a
    network failure or a bad response (never raises through the tile build, and nothing is cached: the search
    is retried, and the tile rebuilt, next visit - revisit.fault()); [] without a request when the models are
    off (enabled)."""
    import revisit

    if not enabled():
        return []
    cache = os.path.join(cache_dir, "threedmr_cache")
    rlat, rlon, rrange = round(float(lat), 4), round(float(lon), 4), int(round(range_m))
    path = os.path.join(cache, f"search_{rlat}_{rlon}_{rrange}.json")
    url = SEARCH_URL.format(lat=lat, lon=lon, range_m=rrange)
    try:
        ids = _cached_json(path, url, iface, timeout, f"search {rlat},{rlon},{rrange}")
    except (ValueError, OSError) as e:
        log.warning("3dmr search %s,%s,%s: %s", rlat, rlon, rrange, e)
        revisit.fault()
        return []
    log.info("3dmr search %s,%s,%s: %d models", rlat, rlon, rrange, len(ids))
    return ids


# One search per REGION_DEG square (its centre, out to its corners plus SEARCH_CORRECT_R_M), not two per road tile: a
# tile's own centre made each tile's 40 km search a request of its own (a new key per tile), on the world-gen path,
# and the volunteer-run service a request per tile (2026-11-03). The library is sparse (one model within 80 km of
# central Paris), so a region's list is short. Kept REGION_TTL_S, then asked again (a failed refresh keeps the old).
REGION_DEG = 0.5
REGION_TTL_S = 14 * 86400.0
REGION_PAGES = 20               # (the search is paged: /<page>, from 1, until an empty page)


def region_ids(lat, lon, cache_dir, iface=None, timeout=30.0):
    """Model ids of 3dmr.eu's coordinate search for the REGION_DEG square holding (lat, lon), reaching
    SEARCH_CORRECT_R_M past its corners (a model whose api point is stale and far off, model_position corrects),
    cached under <cache_dir>/threedmr_cache/region_<i>_<j>.json. [] with the models off. A first search that fails
    is a revisit fault (the tile is rebuilt next visit); an old list stands in when its refresh fails."""
    import revisit

    if not enabled() or not cache_dir:
        return []
    i, j = math.floor(float(lat) / REGION_DEG), math.floor(float(lon) / REGION_DEG)
    clat, clon = (i + 0.5) * REGION_DEG, (j + 0.5) * REGION_DEG
    half = math.hypot(REGION_DEG / 2 * 111320.0 * dmath.cos(math.radians(min(abs(clat) + REGION_DEG / 2, 89.9))),
                      REGION_DEG / 2 * 110540.0)
    rng = int(math.ceil(half + SEARCH_CORRECT_R_M))
    path = os.path.join(cache_dir, "threedmr_cache", f"region_{i}_{j}.json")
    old = None
    try:
        with open(path) as f:
            old = json.load(f)
        if time.time() - os.path.getmtime(path) < REGION_TTL_S:
            return [int(v) for v in old]
    except (OSError, ValueError, TypeError):
        old = None
    ids = []
    try:
        for page in range(1, REGION_PAGES + 1):
            url = SEARCH_URL.format(lat=clat, lon=clon, range_m=rng) + f"/{page}"
            got = json.loads(_fetch_retry(url, iface, timeout, what=f"search {i},{j} page {page}") or b"null")
            if not isinstance(got, list):
                raise ValueError("not a list of model ids")
            if not got:
                break
            ids += [int(v) for v in got]
        ids = sorted(set(ids))
    except (ValueError, OSError, TypeError) as e:
        if old is not None:
            log.warning("3dmr search %s,%s: %s (the cached list kept)", i, j, e)
            return [int(v) for v in old]
        log.warning("3dmr search %s,%s: %s", i, j, e)
        revisit.fault()
        return []
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(ids, f)
    staging.replace(tmp, path)
    log.info("3dmr search %s,%s: %d models", i, j, len(ids))
    return ids


def model_info(model_id, cache_dir, iface=None, timeout=30.0):
    """The https://3dmr.eu/api/info/<id> record of a model ({"id", "lat", "lon", "title", "tags", ...}),
    cached per id under <cache_dir>/threedmr_cache/info_<id>.json."""
    cache = os.path.join(cache_dir, "threedmr_cache")
    path = os.path.join(cache, f"info_{model_id}.json")
    url = INFO_URL.format(id=model_id)
    return _cached_json(path, url, iface, timeout, f"info {model_id}")


def cached_info(model_id, cache_dir):
    """model_info's record of a model as it is on disk (model_position fetched it), or None: never a request."""
    try:
        with open(os.path.join(cache_dir, "threedmr_cache", f"info_{int(model_id)}.json")) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def info_says_bridge(info):
    """Does a model's 3dmr.eu record (model_info) call it a bridge: its OSM tags (man_made=bridge, building=bridge,
    a bridge=* other than no) or one of its categories ("Bridge", "Foot Bridge")? Perth's Elizabeth Quay Bridge (id 55,
    GitHub #1071) is an S-shaped footbridge whose bounding box is no bridge deck's shape at all."""
    if not isinstance(info, dict):
        return False
    tags = info.get("tags") if isinstance(info.get("tags"), dict) else {}
    if str(tags.get("man_made", "")).lower() == "bridge" or str(tags.get("building", "")).lower() == "bridge":
        return True
    if str(tags.get("bridge", "no")).lower() not in ("no", "", "0", "false"):
        return True
    cats = info.get("categories") if isinstance(info.get("categories"), (list, tuple)) else []
    return any("bridge" in str(c).lower() for c in cats)


# --- local models (no 3dmr.eu base) -------------------------------------------------------
def local_models(cache_dir):
    """The player's own local model catalog: <cache_dir>/threedmr_cache/local_models.json, a JSON list of
    {"file", "lat", "lon", "factor" (optional, sRGB diffuseColor RGB), "title" (optional)} entries - one per
    .stl/.glb the player has placed in that same threedmr_cache folder by hand, for a mesh with no 3dmr.eu
    id/page to search or fetch from at all (the repository's local_models/ folder is an example). Each entry comes back
    with its own "id" added: "local_<file's own name, without its extension>" - road_tile_items/
    _custom_tile_entries fold these into the very same custom list a 3dmr.eu geo-search hit lands in, a
    string id (never a numeric 3dmr one) and a "local" key telling the two apart there. [] if the file is
    missing or fails to parse as JSON (nothing configured, or a bad hand-edit - never raises through a tile
    build)."""
    path = os.path.join(cache_dir, "threedmr_cache", LOCAL_MODELS_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [
        {**entry, "id": f"local_{os.path.splitext(entry['file'])[0]}"} for entry in data
    ]


def local_signature(cache_dir):
    """What the player's local models are (local_models' entries with their files' sizes and times), for the road
    tiles' revisit keys: a model added, moved or replaced is built in places visited before. () without any."""
    out = []
    for entry in local_models(cache_dir) if cache_dir else ():
        try:
            st = os.stat(os.path.join(cache_dir, "threedmr_cache", str(entry.get("file"))))
            out.append((json.dumps(entry, sort_keys=True), st.st_size, st.st_mtime_ns))
        except (OSError, TypeError, ValueError):
            out.append((repr(entry), None, None))
    return tuple(out)


def _load_stl(path, factor):
    """(positions, uvs, tris, normals, textures, tri_materials) of a binary STL file (load_local_model's own
    .stl branch). STL carries no vertex indexing at all (every triangle owns its own 3 vertices, never
    shared with another's), so tris is simply arange(3 * n).reshape(-1, 3) over n = the file's own triangle
    count. uvs are all zero (STL carries none). normals are computed from the geometry itself - a numpy
    cross product per triangle, repeated over its own 3 vertices - rather than trusted from the file's own
    per-facet normal field: several STL exporters write that field as all-zero, which would otherwise shade
    the whole mesh black. textures is a single untextured slot, tinted by `factor` (load_local_model's own,
    sRGB) when given, else left to the engine's own default (materials._material_stage's grey fallback).

    An ASCII STL ("solid ... endsolid", free-form text, no fixed per-triangle byte layout) is not decoded:
    raised as UnsupportedModelError with a clear message, rather than silently returning an empty/garbled
    mesh - told apart from a binary STL by its header's own triangle count not matching the file's actual
    size (84 + 50 bytes per triangle: 80-byte header, 4-byte count, 50 bytes - 12 normal + 36 vertex + 2
    attribute - per triangle)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 84:
        raise UnsupportedModelError(
            f"local model {path}: not a binary STL (file too small)"
        )
    ntris = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + 50 * ntris:
        raise UnsupportedModelError(
            f"local model {path}: not a binary STL (ASCII STL is not supported)"
        )
    rec_dtype = np.dtype([("normal", "<f4", 3), ("verts", "<f4", 9), ("attr", "<u2")])
    records = np.frombuffer(data, dtype=rec_dtype, count=ntris, offset=84)
    positions = records["verts"].reshape(ntris * 3, 3).astype(np.float64)

    e0 = positions[1::3] - positions[0::3]
    e1 = positions[2::3] - positions[0::3]
    face_normals = np.cross(e0, e1)
    lengths = dnp.norm(face_normals, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    normals = np.repeat(face_normals / lengths, 3, axis=0)

    tris = np.arange(ntris * 3, dtype=np.int64).reshape(-1, 3)
    uvs = np.zeros((ntris * 3, 2), np.float64)
    textures = [(None, None, tuple(factor) if factor else None, False, None, True)]
    tri_materials = np.zeros(ntris, np.int64)
    return positions, uvs, tris, normals, textures, tri_materials


def load_local_model(cache_dir, entry):
    """(positions, uvs, tris, normals, textures, tri_materials) of a local_models() entry - the local
    catalog's own counterpart of gltf_mesh, for a mesh with no 3dmr.eu base to fetch/decode through
    fetch_model. entry["file"] (a .stl or .glb, in the very same <cache_dir>/threedmr_cache/ local_models()
    itself read from) decides the decoder: .glb is handed straight to gltf_mesh (its own materials carried
    through as-is - entry's own "factor", if any, is NOT applied on top: a glb already carries its own
    material colours, unlike a factor-less STL which has no colour of its own at all); .stl goes through
    _load_stl (entry's own "factor" tints its one slot there). Any other extension raises
    UnsupportedModelError (logged by the caller same as gltf_mesh's)."""
    path = os.path.join(cache_dir, "threedmr_cache", entry["file"])
    ext = os.path.splitext(entry["file"])[1].lower()
    if ext == ".glb":
        with open(path, "rb") as f:
            glb = f.read()
        return gltf_mesh(glb, model_id=entry.get("id"))
    if ext == ".stl":
        return _load_stl(path, entry.get("factor"))
    raise UnsupportedModelError(
        f"local model {entry.get('id', entry.get('file'))}: unsupported file type {ext!r}"
    )


# --- a model's api point vs. its own description -----------------------------------------
# info["lat"]/["lon"] is the position a mapper set when they first uploaded a model - it is never updated
# afterwards even if the mapper's own write-up (info["desc"]) later gets a more precise osmgo.org link added
# to it. Observed on id 22 (Dragon Bridge, Da Nang): the api point sits 26 km away in the mountains, while
# desc still carries "View: http://osmgo.org/?km=1&lat=16.05716901&lon=108.2318281" - the real bridge.
_DESC_LATLON_RE = re.compile(r"lat=(-?[\d.]+)&lon=(-?[\d.]+)")
POSITION_CORRECT_M = 1000.0
# buildings.road_tile_items' own second, wide search: id 22's api point sits 26.2 km from its real (desc-
# corrected) position, outside any tile's own small search_ids radius, so a tile near the real bridge never
# finds it there at all - model_position (which would fix the coordinates) is never even reached. 40 km
# comfortably covers that worst observed case with margin; a model whose desc link points farther than this
# from its own tile is not treated as belonging to it.
SEARCH_CORRECT_R_M = 40000.0
# Authoritative positions (and, where known, a forced yaw) pulled straight from OpenStreetMap for the rare
# model whose 3dmr.eu data is wrong at the source - both its api point AND its desc link, not just the one
# kind of staleness desc_position already corrects for. id 22 (Cau Rong / Dragon Bridge, Da Nang): the api
# point sits 25.9 km away in the mountains, and desc's own osmgo.org link (meant to correct it) is itself
# 614 m from the real bridge - model_position's usual fallback would still land on the wrong spot. Centre of
# the OSM way's own bbox (api/0.6/map, way "Cầu Rồng": lon 108.22434..108.23146, lat 16.06091..16.06150).
# (lat, lon, yaw_deg): yaw_deg a forced rotation for _custom_tile_entries (skips osm_yaw's own OSM lookup
# entirely when given), or None there to leave osm_yaw's usual auto-detection in place. id 22's yaw is forced
# to 0.0: a PCA over the OSM way's own nodes puts its bearing ~2.3 degrees off true west-east, and the
# model's mesh is already oriented west-east as downloaded - nothing to rotate.
POSITION_OVERRIDES = {
    22: (16.06120, 108.22790, 0.0),
    # id 24 (Al-Shaheed Monument, Baghdad): the api point sits ~20 m south-west of the island's own centre
    # (probe/position_audit.json's own OSM way 1089391780 centroid, 33.3434416,44.4461248, is itself 6.6 m off
    # that true centre) - the owner's own measured centre of the monument island, not the tagged way's centroid.
    24: (33.343561, 44.446220, 0.0),
}


def position_override(model_id):
    """A POSITION_OVERRIDES entry for model_id as (lat, lon, yaw_deg), or None - model_position's first
    check, ahead of its usual api/desc logic, and _custom_tile_entries' own check ahead of osm_yaw."""
    return POSITION_OVERRIDES.get(model_id)


# Offline backstop (probe/position_audit.json, OSM-measured): a few more models whose own 3dmr.eu api point
# sits tens to ~110 m from their real OSM way, collected by the owner's own audit run rather than at model_
# position's usual level 2 (osm_polygon_position) - so the fix still applies with no network at all (a
# session without internet, or one where OSM's own API is unreachable). Checked right after POSITION_OVERRIDES,
# still ahead of any network call. id 24 is already in POSITION_OVERRIDES above (level 1, reached first) -
# kept here too as a documented backstop in case that entry is ever removed on its own.
_POSITION_FIXES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "threedmr_position_fixes.json"
)
_position_fixes_cache = None


def _position_fixes():
    """{model_id: (lat, lon)} of _POSITION_FIXES_PATH, loaded once per process and cached - the file itself
    never changes while a server runs. {} (not a crash) if it is missing or malformed: this table is a
    backstop, not a required file."""
    global _position_fixes_cache
    if _position_fixes_cache is None:
        try:
            with open(_POSITION_FIXES_PATH) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        _position_fixes_cache = {
            int(k): (float(v["lat"]), float(v["lon"])) for k, v in raw.items()
        }
    return _position_fixes_cache


def position_fix(model_id):
    """A threedmr_position_fixes.json entry for model_id as (lat, lon), or None - model_position's second
    check (after POSITION_OVERRIDES, still no network)."""
    return _position_fixes().get(model_id)


def desc_position(info):
    """(lat, lon) parsed out of info["desc"]'s own osmgo.org link (...&lat=<num>&lon=<num>...), or None if
    desc carries no such link, or its numbers fail to parse as floats."""
    desc = info.get("desc") if isinstance(info, dict) else None
    if not desc:
        return None
    m = _DESC_LATLON_RE.search(desc)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def _latlon_dist_m(lat1, lon1, lat2, lon2):
    """Equirectangular approximation, metres - fine over the few km this is ever used to measure."""
    coslat = dmath.cos(math.radians((lat1 + lat2) / 2.0))
    de = (lon2 - lon1) * 111320.0 * coslat
    dn = (lat2 - lat1) * 111132.0
    return math.hypot(de, dn)


def model_position(model_id, cache_dir, iface=None, timeout=30.0, offline=False):
    """(lat, lon, corrected, source) of a model, in order: POSITION_OVERRIDES (source "override", level 1, no
    network - the model's own api AND desc data are both wrong, so neither is worth even fetching); else
    threedmr_position_fixes.json (source "fix", level 1.5, still no network - an offline backstop of a few
    more models audited the same way level 2 would find them, for a session with no OSM access at all); else
    the OSM way tagged 3dmr=<model_id> around the model's own api point (source "osm", level 2 -
    osm_polygon_position: probe/position_audit.json found 8 of 3dmr.eu's own models 10-109 m from this
    surveyed tag); else its desc's own osmgo.org link if it disagrees with the api point by more than
    POSITION_CORRECT_M metres (source "desc", level 3 - guards against a model's api point going stale after
    the mapper's own write-up moves on without the api point ever being updated to match); else the api point
    itself (source "api", corrected False, level 4).

    This moves the OSM tag ahead of desc (the pre-existing correction): a surveyed building outranks either
    of the model's own two self-reported points, api or desc, not just the staler of the two. BNGE_3DMR_NO_
    REPOSITION skips level 2 only (osm_polygon_position checks it itself) - override/fix predate any network
    lookup and stay on; buildings._custom_tile_entries' own nearest-footprint level (level "3" in the owner's
    own numbering, between osm and desc/api here) checks the same switch before it runs."""
    override = position_override(model_id)
    if override is not None:
        olat, olon, _oyaw = override
        return olat, olon, True, "override"
    fix = position_fix(model_id)
    if fix is not None:
        flat, flon = fix
        return flat, flon, True, "fix"
    info = model_info(model_id, cache_dir, iface=iface, timeout=timeout)
    lat, lon = float(info["lat"]), float(info["lon"])
    poly = osm_polygon_position(lat, lon, model_id, cache_dir, iface=iface, timeout=timeout, offline=offline)
    if poly is not None:
        plat, plon = poly
        log.info(
            "3dmr model %s: positioned by its tagged OSM way (api point was %.5f,%.5f)",
            model_id, lat, lon,
        )
        return plat, plon, True, "osm"
    desc_ll = desc_position(info)
    if desc_ll is not None:
        dlat, dlon = desc_ll
        dist = _latlon_dist_m(lat, lon, dlat, dlon)
        if dist > POSITION_CORRECT_M:
            log.warning(
                "3dmr model %s: desc coords %.5f,%.5f are %.0f m from the api point %.5f,%.5f - "
                "using the desc ones",
                model_id,
                dlat,
                dlon,
                dist,
                lat,
                lon,
            )
            return dlat, dlon, True, "desc"
    return lat, lon, False, "api"


_near_memo = {}  # (cache folder, info file name) -> (lat, lon) of its model (credited_near)


def credited_near(lat, lon, range_m, cache_dir):
    """Is a 3dmr.eu model within range_m metres of (lat, lon), as far as this cache knows (its info_<id>.json files,
    at model_position's place)? Never a request: credits.feature_ids asks it every second (the overlay names the
    library near a model; its CC BY 4.0 models ask for "3D Model Repository" as their attribution). False with the
    models off."""
    if not enabled() or not cache_dir:
        return False
    d = os.path.join(cache_dir, "threedmr_cache")
    try:
        names = os.listdir(d)
    except OSError:
        return False
    for name in names:
        if not (name.startswith("info_") and name.endswith(".json")):
            continue
        ll = _near_memo.get((d, name))
        if ll is None:
            try:
                with open(os.path.join(d, name)) as f:
                    json.load(f)            # (a whole file: model_position below reads it, never fetches)
                mlat, mlon, _corrected, _source = model_position(int(name[5:-5]), cache_dir, offline=True)
            except Exception:
                continue
            ll = _near_memo[(d, name)] = (float(mlat), float(mlon))
        if _latlon_dist_m(lat, lon, ll[0], ll[1]) <= range_m:
            return True
    return False


# --- auto yaw from the OSM footprint --------------------------------------------------------
# 3dmr.eu's own upload guide asks modellers to orient a model Z-towards-south, but not all of them do (the
# user's report: the Eiffel Tower, id 4, stood rotated ~45 degrees though its position was right) - and
# /api/info carries no field to read the rotation back from (it is baked into the vertices). A per-model yaw
# table was rejected in review ("too easy to miss one" as new models are added); this recovers the angle
# instead, by comparing the mod-90 edge-direction histogram of the model's own lowest tier (its footprint,
# from a kNN edge over its vertices) against the OSM building way's (its ring's own consecutive edges) -
# both histograms peak at a building's true orientation, whichever 90-degree-symmetric quadrant either one
# happens to be measured in.
OSM_MAP_URL = "https://api.openstreetmap.org/api/0.6/map"
OSM_BBOX_DEG = (
    0.002  # ~150-220 m: wide enough to hold the one building the model stands on
)
YAW_BIN_DEG = 5.0
YAW_BINS = int(round(90.0 / YAW_BIN_DEG))
YAW_KNN_CUTOFF_M = 5.0  # a model's own lower-tier vertices farther than this from any other have no edge
YAW_MAX_MODEL_POINTS = (
    2000  # subsampled above this (a big model's bottom tier can be tens of thousands)
)


def _angle_histogram(angles_deg, weights):
    """(bin centres (YAW_BINS,), summed weight per bin) of angles (any range) folded mod 90 degrees into
    YAW_BIN_DEG-wide bins - the shared binning both _edge_histogram (the OSM ring) and _knn_edge_histogram
    (the model) reduce to, so their dominant bins are directly comparable. Centres sit on the multiples of
    YAW_BIN_DEG (0, 5, ... 85) with each bin's edges running YAW_BIN_DEG/2 either side, not on top of them -
    real building edges (axis-aligned or 45-degree ones especially) atan2 to angles that cluster on those
    multiples, and float error would otherwise scatter them across a boundary landing right where they
    cluster, splitting one true peak into two neighbouring bins."""
    angles_deg = np.asarray(angles_deg, np.float64) % 90.0
    weights = np.asarray(weights, np.float64)
    bins = np.round(angles_deg / YAW_BIN_DEG).astype(np.int64) % YAW_BINS
    hist = np.zeros(YAW_BINS, np.float64)
    np.add.at(hist, bins, weights)
    centers = np.arange(YAW_BINS, dtype=np.float64) * YAW_BIN_DEG
    return centers, hist


def _edge_histogram(points_xy):
    """(bin centres, weights) of a closed ring's own edges (points_xy (n, 2), already local metres; the last
    point need not repeat the first - wraparound is implicit), each edge's angle mod 90 weighted by its
    length: the longer, straighter runs of a building's own walls outweigh a few short jogs."""
    p = np.asarray(points_xy, np.float64)
    d = np.roll(p, -1, axis=0) - p
    lengths = dnp.hypot(d[:, 0], d[:, 1])
    angles = np.degrees(dnp.arctan2(d[:, 1], d[:, 0]))
    return _angle_histogram(angles, lengths)


def _knn_edge_histogram(
    points_xy, cutoff=YAW_KNN_CUTOFF_M, max_points=YAW_MAX_MODEL_POINTS
):
    """(bin centres, weights) of a point cloud's own dominant edge direction: each point paired with its
    nearest neighbour (within cutoff), weighted by that distance. A model's vertices carry no edge list of
    their own at the footprint level (gltf_mesh's triangles mix roof and wall edges in) - kNN over just the
    lowest tier approximates the straight runs of its base instead. Subsampled to max_points first (evenly,
    not randomly: deterministic across calls)."""
    p = np.asarray(points_xy, np.float64)
    if len(p) > max_points:
        idx = np.linspace(0, len(p) - 1, max_points).astype(np.int64)
        p = p[idx]
    if len(p) < 2:
        return _angle_histogram([], [])
    diff = p[:, None, :] - p[None, :, :]
    dist = dnp.hypot(diff[:, :, 0], diff[:, :, 1])
    np.fill_diagonal(dist, np.inf)
    nearest = np.argmin(dist, axis=1)
    nearest_dist = dist[np.arange(len(p)), nearest]
    keep = nearest_dist <= cutoff
    if not keep.any():
        return _angle_histogram([], [])
    d = p[nearest[keep]] - p[keep]
    angles = np.degrees(dnp.arctan2(d[:, 1], d[:, 0]))
    return _angle_histogram(angles, nearest_dist[keep])


def _dominant_angle(centers, weights, near=None):
    """The centre of the histogram's dominant bin. With `near` given (an already-known angle, e.g. the OSM
    side's A_osm) and a second bin present, picks whichever of the two best bins lies closer to `near`
    (circular, mod 90) instead of always the single heaviest one - a 90-degree-symmetric footprint's edges
    split near-evenly between two adjacent (or wraparound-adjacent, 0/90) bins as often as they land cleanly
    in one (observed on the Eiffel Tower's own base: bins 0 and 85 nearly tied), and either could be "the"
    dominant one on weight alone. The second bin only counts as a real contender if it holds at least half
    the top bin's weight - otherwise it's noise (a stray near-duplicate point or two) that would otherwise
    override a clear single peak just for lying closer to `near`. None if every bin is empty (no ring, or no
    point closer than the cutoff)."""
    if not weights.any():
        return None
    order = np.argsort(weights)[::-1]
    top = float(centers[order[0]])
    if near is None or len(order) < 2 or weights[order[1]] < 0.5 * weights[order[0]]:
        return top
    second = float(centers[order[1]])

    def circ(a):
        dd = abs(a - near) % 90.0
        return min(dd, 90.0 - dd)

    return top if circ(top) <= circ(second) else second


def _yaw_from_angles(a_osm, a_model):
    """The yaw (-45..45 degrees) that rotates a_model (the model's own dominant edge angle, mod 90) onto
    a_osm (the OSM polygon's): normalized into (-45, 45] so a square-symmetric footprint's two equally valid
    90-degree-apart corrections land on the same value either way round."""
    return ((a_osm - a_model + 45.0) % 90.0) - 45.0


def _osm_building_ring(lat, lon, model_id, iface, timeout):
    """(ring, tagged) - the local-metre ring (n, 2), origin (lat, lon), of the OSM way nearest a 3dmr.eu
    model's own mapped position, and whether it is the exact one: preferably the single way tagged
    3dmr=<model_id> (a mapper's own cross-reference - the only way to tell which of several nearby buildings
    is the model's, and why this goes straight to the OSM API rather than Overpass (no index on an arbitrary
    tag, and prone to timeouts over a VPN) or Nominatim (does not index 3dmr tags at all)), tagged True; else
    the nearest building=* way (or, when more than one way happens to carry the tag - id 35's own data, 5
    different buildings all tagged 3dmr=35, apparently copy-pasted across a city block import - the nearest
    of THOSE instead, still untagged) inside a small bbox (OSM_BBOX_DEG) around it, tagged False - good
    enough for osm_yaw's own rotation (any nearby building is oriented about the same way as the real one),
    but never trusted for osm_polygon_position's placement (the wrong neighbour, tagged or not, would
    silently relocate the model, not just mis-rotate it). (None, False) if the map call fails, or no such way
    is found at all."""
    d = OSM_BBOX_DEG
    url = f"{OSM_MAP_URL}?bbox={lon - d:.6f},{lat - d:.6f},{lon + d:.6f},{lat + d:.6f}"
    data = _fetch_retry(url, iface, timeout, what=f"osm map (model {model_id})")
    if not data:
        return None, False
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None, False
    nodes = {
        n.get("id"): (float(n.get("lat")), float(n.get("lon")))
        for n in root.findall("node")
    }
    tagged_candidates, nearest, nearest_d = [], None, None
    for w in root.findall("way"):
        pts = [
            nodes[r] for r in (nd.get("ref") for nd in w.findall("nd")) if r in nodes
        ]
        if len(pts) < 4:
            continue
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        is_tag_match = tags.get(TAG_KEY) == str(model_id)
        if is_tag_match:
            tagged_candidates.append(pts)
        if is_tag_match or "building" in tags:
            clat = sum(p[0] for p in pts) / len(pts)
            clon = sum(p[1] for p in pts) / len(pts)
            dist = math.hypot(clat - lat, clon - lon)
            if nearest_d is None or dist < nearest_d:
                nearest, nearest_d = pts, dist
    if len(tagged_candidates) > 1:
        log.info(
            "3dmr model %s: %d ways tagged 3dmr=%s - ambiguous, not used for position",
            model_id, len(tagged_candidates), model_id,
        )
    is_tagged = len(tagged_candidates) == 1
    ring = tagged_candidates[0] if is_tagged else nearest
    if ring is None:
        return None, False
    if (
        len(ring) > 1 and ring[0] == ring[-1]
    ):  # a closed way repeats its first node: drop the duplicate
        ring = ring[:-1]
    if len(ring) < 4:
        return None, False
    coslat = dmath.cos(math.radians(lat))
    e = [(rlon - lon) * 111320.0 * coslat for _rlat, rlon in ring]
    n = [(rlat - lat) * 111132.0 for rlat, _rlon in ring]
    return np.array(list(zip(e, n)), np.float64), is_tagged


def _ring_cache_path(cache_dir, lat, lon):
    cache = os.path.join(cache_dir, "threedmr_cache")
    rlat, rlon = round(float(lat), 4), round(float(lon), 4)
    return cache, os.path.join(cache, f"yaw_{rlat}_{rlon}.json")


_osm_down_until = 0.0      # time.time() before which a failed OSM /map fetch is not tried again (_osm_ring_cached)
OSM_RETRY_S = 3600.0


def _osm_ring_cached(lat, lon, model_id, cache_dir, iface=None, timeout=30.0, offline=False):
    """(ring_xy or None, tagged) for (lat, lon, model_id): _osm_building_ring's own OSM /map fetch, cached
    forever (including a "no ring" miss) under its own "ring"/"tagged" fields in the SAME yaw_<lat>_<lon>.json
    file osm_yaw keeps its "yaw" answer in and osm_polygon_position its "pos" - between all three, a model's
    OSM lookup at a given (lat, lon) costs at most one network request, however many of them ask for it."""
    cache, path = _ring_cache_path(cache_dir, lat, lon)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if "ring" in data:
        ring = data["ring"]
        return (np.asarray(ring, np.float64) if ring is not None else None), bool(
            data.get("tagged", False)
        )
    # offline: the cache only (credits.feature_ids asks from the server's event loop every second: a request there
    # held every game message); and a failed fetch is not kept as "no ring" for good, only not asked again for a while
    global _osm_down_until
    if offline or time.time() < _osm_down_until:
        return None, False
    try:
        ring_xy, tagged = _osm_building_ring(lat, lon, model_id, iface, timeout)
    except (urllib.error.URLError, OSError) as e:
        log.info("3dmr model %s: OSM fetch failed (%s); asked again in %.0f min", model_id, e, OSM_RETRY_S / 60)
        _osm_down_until = time.time() + OSM_RETRY_S
        return None, False
    data["ring"] = ring_xy.tolist() if ring_xy is not None else None
    data["tagged"] = tagged
    os.makedirs(cache, exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(data, f)
    staging.replace(tmp, path)
    return ring_xy, tagged


def _seed_ring_cache(cache_dir, lat, lon, ring_xy, tagged):
    """Copies an already-fetched ring into the cache file for a SECOND (lat, lon) key - osm_polygon_position's
    own use, once it has turned a tagged ring into a corrected position: a later osm_yaw call for the same
    model, centred on that corrected position (not the api point the ring was originally fetched around),
    would otherwise round to a different cache file and re-fetch the same ring over the network. A no-op if
    that key already has a ring cached (e.g. the two points round to the same key)."""
    _cache, path = _ring_cache_path(cache_dir, lat, lon)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if "ring" in data:
        return
    data["ring"] = ring_xy.tolist() if ring_xy is not None else None
    data["tagged"] = tagged
    cache = os.path.dirname(path)
    os.makedirs(cache, exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(data, f)
    staging.replace(tmp, path)


OSM_POLYGON_GATE_M = 500.0  # a tagged OSM way's centroid farther than this from the model's own api point is
# not trusted as this model's own building (osm_polygon_position) - in practice always comfortably inside
# this (OSM_BBOX_DEG's own search box cannot turn up a tagged way farther than ~300 m out to begin with), but
# a centroid that somehow comes back implausibly far is logged and left at the usual desc/api fallback
# rather than silently moving the model there.


def osm_polygon_position(lat, lon, model_id, cache_dir, iface=None, timeout=30.0, offline=False):
    """(lat, lon) of the OSM way tagged 3dmr=<model_id> around a model's own api point (lat, lon here), its
    ring's centroid - model_position's level 2, ahead of its desc/api fallback: probe/position_audit.json
    found 8 of 3dmr.eu's own models placed by their own api point 10-109 m from this surveyed tag (Baghdad's
    Al-Shaheed Monument, id 24, 7 m - now in POSITION_OVERRIDES instead, measured more precisely by hand).
    None with BNGE_3DMR_NO_REPOSITION set, the OSM fetch failing, no way carrying the exact tag (_osm_
    building_ring's own looser "nearest building" fallback is good enough for osm_yaw's rotation but not
    trusted here - the wrong neighbouring building would silently relocate the model), or a centroid farther
    than OSM_POLYGON_GATE_M from (lat, lon) (logged as a warning: not this model's own way after all).

    Shares its OSM /map fetch and cache file with osm_yaw (_osm_ring_cached, keyed on (lat, lon) - the
    model's api point here) and seeds that same ring under its own corrected answer's key too (_seed_ring_
    cache), so a later osm_yaw call centred on THIS function's own (lat, lon) result also hits the cache."""
    if os.environ.get(NO_REPOSITION_ENV):
        return None
    ring_xy, tagged = _osm_ring_cached(lat, lon, model_id, cache_dir, iface, timeout, offline=offline)
    if ring_xy is None or not tagged or len(ring_xy) < 3:
        return None
    ce, cn = float(ring_xy[:, 0].mean()), float(ring_xy[:, 1].mean())
    dist = math.hypot(ce, cn)
    if dist > OSM_POLYGON_GATE_M:
        log.warning(
            "3dmr model %s: tagged OSM way centroid is %.0f m from the api point - not trusted, ignored",
            model_id,
            dist,
        )
        return None
    coslat = dmath.cos(math.radians(lat))
    clat = lat + cn / 111132.0
    clon = lon + ce / (111320.0 * coslat)
    _seed_ring_cache(cache_dir, clat, clon, ring_xy, tagged)
    return clat, clon


def osm_yaw(lat, lon, model_id, points_xy, cache_dir, iface=None, timeout=30.0):
    """The yaw (degrees, -45..45) to rotate a 3dmr.eu model so its own footprint's dominant edge direction
    (mod 90; points_xy: the model's lower tier, local XY, e.g. buildings._custom_tile_entries' pos[z <= 10%
    of its height range][:, :2]) matches its OSM building way's (_osm_building_ring, _edge_histogram). 0.0 -
    no rotation, the model placed as downloaded - when BNGE_3DMR_NO_YAW is set (a fallback if OSM
    is not reachable from a session), the OSM lookup fails or finds no way, or either side has fewer than 4
    points to measure an edge direction from; logged, never raises (a tile must still build without this).

    Cached forever per rounded (lat, lon) under <cache_dir>/threedmr_cache/yaw_<lat>_<lon>.json's own "yaw"
    field (_osm_ring_cached keeps the ring itself, shared with osm_polygon_position, in the same file), including
    a 0.0 ("no footprint") result - an OSM building's own footprint does not change day to day, so a repeat
    visit to the same model is not worth another network round trip either way. BNGE_3DMR_NO_YAW is checked
    ahead of the cache: the override must not depend on what an earlier, network-reachable run wrote
    there."""
    if os.environ.get(NO_YAW_ENV):
        return 0.0
    _cache, path = _ring_cache_path(cache_dir, lat, lon)
    try:
        with open(path) as f:
            cached = json.load(f)
        if "yaw" in cached:
            return float(cached["yaw"])
    except (OSError, ValueError):
        cached = {}

    ring_xy, _tagged = _osm_ring_cached(lat, lon, model_id, cache_dir, iface, timeout)

    yaw = 0.0
    points_xy = np.asarray(points_xy, np.float64)
    if ring_xy is not None and len(ring_xy) >= 4 and len(points_xy) >= 4:
        a_osm = _dominant_angle(*_edge_histogram(ring_xy))
        a_model = _dominant_angle(*_knn_edge_histogram(points_xy), near=a_osm)
        if a_osm is not None and a_model is not None:
            yaw = _yaw_from_angles(a_osm, a_model)
        else:
            log.info("3dmr model %s: no OSM footprint, yaw 0", model_id)
    else:
        log.info("3dmr model %s: no OSM footprint, yaw 0", model_id)

    cache = os.path.dirname(path)
    try:
        with open(path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = {}
    cached["yaw"] = yaw
    os.makedirs(cache, exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(cached, f)
    staging.replace(tmp, path)
    return yaw


def rotate_xz(positions, yaw_deg):
    """positions (n, 3) rotated yaw_deg degrees about the vertical axis (column 2; BeamNG Z-up, gltf_mesh's
    convention) - columns 0/1 only, before write_model centres and offsets them (rotating after centering
    would leave the mesh off its anchor; rotating the already-ground-biased Z would tilt it)."""
    if not yaw_deg:
        return np.asarray(positions, np.float64)
    rad = math.radians(yaw_deg)
    c, s = dmath.cos(rad), dmath.sin(rad)
    p = np.array(positions, np.float64, copy=True)
    x, y = p[:, 0].copy(), p[:, 1].copy()
    p[:, 0] = x * c - y * s
    p[:, 1] = x * s + y * c
    return p


# --- GLB / glTF decoding -----------------------------------------------------------------
_COMPONENT_DTYPE = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}
_TYPE_NCOMP = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def _parse_glb(glb):
    """(gltf dict, BIN chunk bytes) of a .glb file."""
    magic, _version, total_len = struct.unpack_from("<4sII", glb, 0)
    if magic != b"glTF":
        raise ValueError("not a GLB file")
    offset = 12
    gltf, bin_chunk = None, b""
    while offset < total_len:
        clen, ctype = struct.unpack_from("<I4s", glb, offset)
        offset += 8
        data = glb[offset : offset + clen]
        offset += clen
        if ctype == b"JSON":
            gltf = json.loads(data)
        elif ctype == b"BIN\x00":
            bin_chunk = data
    if gltf is None:
        raise ValueError("GLB has no JSON chunk")
    return gltf, bin_chunk


# KHR_mesh_quantization normalized integer -> float (glTF 2.0 spec 3.9.2): unsigned divides by its max value
# into [0, 1], signed the same into [-1, 1] (its most-negative value is one past -1.0, hence the max(..., -1.0)
# at use); accessors we may need this for (POSITION, NORMAL, TEXCOORD) are never the unsigned-int componentType
# 5125, so it is not listed here.
_NORMALIZE_DIV = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}


def _accessor(gltf, bin_chunk, idx):
    """A glTF accessor as an (count, ncomp) (or (count,) for SCALAR) numpy array, honouring its bufferView's
    byteStride (positions of some exporters are interleaved with other attributes). A KHR_mesh_quantization
    accessor (normalized integer components, typically u16 POSITION) is converted to float in [0, 1] or
    [-1, 1] here, before any node transform is applied to it - the real-world size comes from that transform
    (a node scale), not from the raw integer. Raises KeyError if the accessor has no bufferView (Draco-
    compressed primitives keep their attributes' accessors bufferView-less on purpose: the actual data lives
    in the primitive's KHR_draco_mesh_compression extension, which this module does not decode)."""
    acc = gltf["accessors"][idx]
    dtype = _COMPONENT_DTYPE[acc["componentType"]]
    ncomp = _TYPE_NCOMP[acc["type"]]
    count = acc["count"]
    bv = gltf["bufferViews"][acc["bufferView"]]
    itemsize = np.dtype(dtype).itemsize
    elem = itemsize * ncomp
    stride = bv.get("byteStride", elem)
    offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    shape = (count, ncomp) if ncomp > 1 else (count,)
    strides = (stride, itemsize) if ncomp > 1 else (stride,)
    view = np.ndarray(
        shape, dtype=dtype, buffer=bin_chunk, offset=offset, strides=strides
    )
    raw = np.array(
        view
    )  # copy: bin_chunk is read-only, and callers keep these past the GLB buffer
    if acc.get("normalized") and acc["componentType"] in _NORMALIZE_DIV:
        div = _NORMALIZE_DIV[acc["componentType"]]
        norm = raw.astype(np.float64) / div
        return (
            np.maximum(norm, -1.0) if np.issubdtype(dtype, np.signedinteger) else norm
        )
    return raw


def _quat_to_mat3(q):
    """3x3 rotation matrix of a glTF quaternion (x, y, z, w), for v' = v @ R.T on row vectors."""
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ],
        np.float64,
    )


def _node_matrix(node):
    """A glTF node's local 4x4 transform (row-major, v' = M @ v on column vectors)."""
    if "matrix" in node:
        return (
            np.array(node["matrix"], np.float64).reshape(4, 4).T
        )  # glTF matrices are column-major
    t = node.get("translation", (0.0, 0.0, 0.0))
    r = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    s = node.get("scale", (1.0, 1.0, 1.0))
    T, R, S = np.eye(4), np.eye(4), np.eye(4)
    T[:3, 3] = t
    R[:3, :3] = _quat_to_mat3(r)
    S[0, 0], S[1, 1], S[2, 2] = s
    return dnp.matmul(dnp.matmul(T, R), S)


def _walk(nodes, idx, parent_mat, out):
    """Depth-first node walk; out collects (mesh index, world matrix) of every node with a mesh."""
    node = nodes[idx]
    mat = dnp.matmul(parent_mat, _node_matrix(node))
    if "mesh" in node:
        out.append((node["mesh"], mat))
    for child in node.get("children", ()):
        _walk(nodes, child, mat, out)


def _is_white(rgb):
    """Whether an RGB triple is the glTF default baseColorFactor (1, 1, 1) within float rounding - not worth
    tinting a material away from its texture's/the engine's own default colour."""
    return all(abs(c - 1.0) < 1e-3 for c in rgb)


def _linear_to_srgb(c):
    """A linear-light colour channel converted to sRGB (the standard piecewise gamma curve). glTF's
    baseColorFactor is defined in linear space (spec), but BeamNG materials.json diffuseColor is read as
    sRGB like any other colour picker value - without this, a dark linear factor (e.g. the Eiffel Tower's
    bronze, 3dmr.eu id 4: linear ~0.06,0.04,0.03) bakes in almost black instead of its intended colour."""
    return 12.92 * c if c <= 0.0031308 else 1.055 * (dnp.power(c, 1 / 2.4)) - 0.055


def _texture_has_alpha(data):
    """Whether raw (not yet PNG-re-encoded) image bytes decode to a real, cutting alpha channel - some pixel's
    alpha below 250, not a channel that is merely present but reads as opaque everywhere."""
    try:
        img = Image.open(io.BytesIO(data))
    except OSError:
        return False
    if img.mode not in ("RGBA", "LA") and "transparency" not in img.info:
        return False
    return img.convert("RGBA").getchannel("A").getextrema()[0] < 250


def _trim_image(data):
    """An embedded image's bytes without what follows its end: a GLB bufferView is padded to 4 bytes and some
    exporters count the padding in the view, so 1-3 bytes trail the PNG's IEND chunk. Python's readers ignore them,
    the game's does not: those slots drew BeamNG's orange "NO TEXTURE" (GitHub #1072, Perth's State War Memorial and
    Central Park tower: 12 of 128 cached textures). PNG: up to the end of IEND; JPEG: up to its EOI marker."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        j = data.rfind(b"IEND")
        if j >= 4:
            return data[:j + 8]         # (type + CRC)
    elif data[:2] == b"\xff\xd8":
        j = data.rfind(b"\xff\xd9")
        if j > 0:
            return data[:j + 2]
    return data


def _resolve_texture(gltf, bin_chunk, mat_idx, cache):
    """(bytes, extension, factor, has_alpha, alpha_cutoff) of a material's baseColorTexture/baseColorFactor.
    bytes/extension are (None, None) if the material has no baseColorTexture (observed on 3dmr.eu ids 47/31:
    several materials carry only a baseColorFactor, no image at all - BeamNG has no concept of a textureless
    material with a colour, so one must be synthesized from this). factor is the material's baseColorFactor
    RGB (r, g, b) converted from glTF's linear space to the sRGB materials.json's diffuseColor expects
    (_linear_to_srgb), or None when the material has none or it is the glTF-default white (nothing to tint
    diffuseColor away from).

    has_alpha: whether this slot needs translucency/alphaTest in the engine material to read correctly -
    either the texture's own alpha channel actually cuts pixels out (_texture_has_alpha: some pixel's alpha
    below 250), or the glTF material declares alphaMode BLEND/MASK and this slot actually has a texture to
    cut (observed on 3dmr.eu id 4, the Eiffel Tower: its lattice textures carry alpha 0/255 and every one of
    its materials is alphaMode BLEND - without this, our materials.json painted the gaps opaque and the
    tower read in-game as a solid mass). A colourless slot (factor, no texture) is never alpha, even under
    BLEND/MASK: there is no image to cut anything out of. alpha_cutoff: the material's own alphaCutoff (glTF
    default 0.5) when alphaMode is MASK (a hard cutout), else None (BLEND reads the whole channel, no cutoff)."""
    if mat_idx is None:
        return None, None, None, False, None
    if mat_idx in cache:
        return cache[mat_idx]
    material = gltf.get("materials", [])[mat_idx]
    pbr = material.get("pbrMetallicRoughness", {})
    tex_ref = pbr.get("baseColorTexture")
    base_factor = pbr.get("baseColorFactor")
    factor = None
    if base_factor and not _is_white(base_factor[:3]):
        factor = tuple(_linear_to_srgb(float(c)) for c in base_factor[:3])
    alpha_mode = material.get("alphaMode", "OPAQUE")
    result = (None, None, factor, False, None)
    if tex_ref is not None:
        tex = gltf["textures"][tex_ref["index"]]
        img_idx = tex.get("source")
        if img_idx is not None:
            image = gltf["images"][img_idx]
            if "bufferView" in image:
                bv = gltf["bufferViews"][image["bufferView"]]
                off = bv.get("byteOffset", 0)
                data = bytes(bin_chunk[off : off + bv["byteLength"]])
            elif str(image.get("uri", "")).startswith("data:"):
                import base64

                data = base64.b64decode(image["uri"].split(",", 1)[1])
            else:
                data = None
            if data is not None:
                data = _trim_image(data)
                ext = {"image/jpeg": "jpeg", "image/png": "png"}.get(
                    image.get("mimeType")
                )
                if ext is None:
                    try:
                        ext = (
                            Image.open(io.BytesIO(data)).format or ""
                        ).lower() or None
                    except OSError:
                        ext = None
                has_alpha = _texture_has_alpha(data) or alpha_mode in ("BLEND", "MASK")
                alpha_cutoff = (
                    float(material.get("alphaCutoff", 0.5))
                    if alpha_mode == "MASK"
                    else None
                )
                result = (data, ext, factor, has_alpha, alpha_cutoff)
    cache[mat_idx] = result
    return result


def _prim_compression(prim):
    """The name of the geometry-compression extension a primitive uses (its attributes' accessors have no
    bufferView: the real data is inside the extension, which this decoder does not read), or None for an
    ordinary primitive."""
    exts = prim.get("extensions") or {}
    if "KHR_draco_mesh_compression" in exts:
        return "KHR_draco_mesh_compression"
    return next(iter(exts), None)


def _drop_nonfinite(pos, uv, norm, idx):
    """pos/uv/norm/idx (one primitive's, already world-transformed pos) with any non-finite (NaN/inf)
    position dropped, along with every triangle that referenced it (observed on a few upstream models: a
    bad vertex would otherwise poison the whole mesh's bounding box)."""
    finite = np.isfinite(pos).all(1)
    if finite.all():
        return pos, uv, norm, idx, 0
    remap = np.full(len(pos), -1, np.int64)
    remap[finite] = np.arange(int(finite.sum()))
    keep_tri = finite[idx].all(1)
    return (
        pos[finite],
        uv[finite],
        norm[finite],
        remap[idx[keep_tri]],
        int((~finite).sum()),
    )


WEDGE_KEEP_ENV = "BNGE_3DMR_KEEP_WEDGES"  # set: skip _wedge_mask's clean-up entirely (an escape
# hatch for a false positive on some future model)
_WEDGE_MAX_FLAT_M = 0.5
_WEDGE_MIN_AREA_M2 = 500.0
_WEDGE_MAX_ISOLATED_USES = 4
_WEDGE_BBOX_CORNER_TOL = 0.02
_WEDGE_NEIGHBOR_AREA_M2 = 10.0


def _wedge_mask(positions, tris):
    """Boolean (m,) mask over tris, True = drop: a triangle this decoder judges to be an author's stray giant
    flat "wedge" rather than real building geometry (observed on 3dmr.eu id 3, Petronas: two triangles,
    23967 sq m, all 4 distinct vertices sitting exactly on the model's own bbox corners at its lowest Z - a
    screenshot shows it as a white wedge slicing through the whole complex).

    A triangle only qualifies when it is flat (its own Z range under _WEDGE_MAX_FLAT_M), large
    (_WEDGE_MIN_AREA_M2) and isolated (none of its 3 vertex indices is reused more than
    _WEDGE_MAX_ISOLATED_USES times across the whole mesh - real building geometry shares vertices far more
    than that). Real ground-plate/mockup geometry (3dmr.eu id 6: a whole district mockup, hundreds of flat
    isolated plates well over this area) is just as flat, large and isolated by that alone, so one of two
    further signatures is required before actually dropping: either every one of the triangle's 3 vertices
    sits within _WEDGE_BBOX_CORNER_TOL of the mesh's own bbox on every axis (id 3's own signature), or - short
    of that - none of its vertices is shared with any other triangle of real size (_WEDGE_NEIGHBOR_AREA_M2): a
    plate that is part of a mockup touches (or is touched by) the rest of that mockup's geometry, a stray
    wedge touches nothing."""
    tris = np.asarray(tris, np.int64)
    if len(tris) == 0:
        return np.zeros(0, bool)
    positions = np.asarray(positions, np.float64)
    tri_pos = positions[tris]  # (m, 3, 3)

    flat = (tri_pos[:, :, 2].max(1) - tri_pos[:, :, 2].min(1)) < _WEDGE_MAX_FLAT_M
    e0 = tri_pos[:, 1] - tri_pos[:, 0]
    e1 = tri_pos[:, 2] - tri_pos[:, 0]
    area = 0.5 * dnp.norm(np.cross(e0, e1), axis=1)
    candidate = flat & (area > _WEDGE_MIN_AREA_M2)
    if not candidate.any():
        return candidate

    counts = np.bincount(tris.reshape(-1), minlength=int(tris.max()) + 1)
    candidate &= (counts[tris] <= _WEDGE_MAX_ISOLATED_USES).all(1)
    if not candidate.any():
        return candidate

    bbox_min, bbox_max = positions.min(0), positions.max(0)
    tol = _WEDGE_BBOX_CORNER_TOL * np.maximum(bbox_max - bbox_min, 1e-9)
    near_bound = (
        np.minimum(np.abs(tri_pos - bbox_min), np.abs(bbox_max - tri_pos)) <= tol
    )
    bbox_corners = near_bound.all(2).all(1)  # every vertex near the bbox on every axis

    vert_to_tris = defaultdict(list)
    for ti, tri in enumerate(tris):
        for v in tri:
            vert_to_tris[int(v)].append(ti)

    mask = np.zeros(len(tris), bool)
    for ti in np.nonzero(candidate)[0]:
        if bbox_corners[ti]:
            mask[ti] = True
            continue
        neighbors = set()
        for v in tris[ti]:
            neighbors.update(vert_to_tris[int(v)])
        neighbors.discard(ti)
        if not any(area[t] > _WEDGE_NEIGHBOR_AREA_M2 for t in neighbors):
            mask[ti] = True
    return mask


def carve_bridge_deck_mask(
    positions, tris, flat_ratio=0.85, min_area=20.0, height_share=0.55
):  # decorative-era, unused since FV=18 (buildings._custom_tile_entries no longer calls this - an
    # is_bridge model now stands whole, standard-bedded, over the stub road) - retained for tests

    """Boolean mask (m,) True for a bridge model's own ROAD-SURFACE triangles to carve out before
    write_model, so the model stands as pure decoration (arches/pylons) over the procedural road
    stub (which stays: correct height, clearance, drivable). A triangle is road surface when it is
    near-horizontal (|cross_z|/|cross| > flat_ratio), its horizontal projected area > min_area m2,
    and its centroid z is below zmin + height_share*(zmax-zmin) (a deck and its walkways; the
    arches above survive). Empty mask for a mesh with no such triangles."""
    P = np.asarray(positions, np.float64).reshape(-1, 3)
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if not len(T) or not len(P):
        return np.zeros(len(T), bool)
    v0, v1, v2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    mag = dnp.norm(cross, axis=1)
    flat = np.abs(cross[:, 2]) / np.maximum(mag, 1e-9) > flat_ratio
    e1 = v1[:, :2] - v0[:, :2]
    e2 = v2[:, :2] - v0[:, :2]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2.0
    zc = (v0[:, 2] + v1[:, 2] + v2[:, 2]) / 3.0
    zmin, zmax = float(P[:, 2].min()), float(P[:, 2].max())
    low = zc <= zmin + height_share * (zmax - zmin)
    return flat & (area > min_area) & low


DECK_BIN_M = 1.0  # write_bridge_deck's own z-histogram bin, scanning the mesh's lower half for its deck layer
# (the horizontal plate a car drives on - an arch/tower reaches higher but barely registers as horizontal area)


def deck_level_z(
    positions, tris, height_share=0.6, flat_ratio=0.85
):  # decorative-era, unused since FV=17 (deck_z_at is gone) - retained for tests
    """The z (in positions' own frame) of a bridge mesh's own road deck: the area-weighted median z of its
    near-horizontal triangles (|cross_z|/|cross| > flat_ratio, same criterion as carve_bridge_deck_mask's)
    restricted to the mesh's lowest height_share of its own z range. write_model's deck_z= beds this level on
    the procedural stub's own road height instead of carving the deck away (buildings._custom_tile_entries), so
    the model's mesh becomes the road itself rather than decoration floating beside it (Da Nang's Dragon
    Bridge, model 22: no fixed offset ever matched the stub's own ground-dependent height). None when nothing
    in that lower share has any horizontal extent at all (should not happen for an is_bridge model, but this
    must not bed on noise if it does)."""
    P = np.asarray(positions, np.float64).reshape(-1, 3)
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if not len(T) or not len(P):
        return None
    v0, v1, v2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    flat = (
        np.abs(cross[:, 2]) / np.maximum(dnp.norm(cross, axis=1), 1e-9)
        > flat_ratio
    )
    tz = (v0[:, 2] + v1[:, 2] + v2[:, 2]) / 3.0
    e1 = v1[:, :2] - v0[:, :2]
    e2 = v2[:, :2] - v0[:, :2]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2.0
    zmin, zmax = float(P[:, 2].min()), float(P[:, 2].max())
    sel = flat & (area > 1e-6) & (tz <= zmin + height_share * (zmax - zmin))
    if not sel.any():
        return None
    order = np.argsort(tz[sel])
    zs = tz[sel][order]
    cum = np.cumsum(area[sel][order])
    return float(zs[int(np.searchsorted(cum / cum[-1], 0.5))])


def deck_ends_z(positions, tris, band=0.05, flat_ratio=0.85, min_area=5.0):
    """(z_west, z_east): area-weighted medians of near-horizontal triangles in the leftmost and rightmost
    'band' (share of the x span) of the mesh — at a bridge's own ENDS only its roadway tier reaches (arches
    and upper decks stop short), so the ends name the deck the stub's road must meet. None, None when a band
    has no such triangles.

    Decorative-era, unused since FV=17: write_model's and write_bridge_deck's own calls to this are both
    behind an `if deck_z is not None` that no caller ever satisfies any more (deck_z_at is gone - every
    is_bridge model stands whole and standard-bedded now, FV=18). Kept rather than dug out of
    write_model/write_bridge_deck for a future caller that wants to bed a mesh on a named deck height again."""
    P = np.asarray(positions, np.float64).reshape(-1, 3)
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if not len(T) or not len(P):
        return None, None
    v0, v1, v2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    flat = (
        np.abs(cross[:, 2]) / np.maximum(dnp.norm(cross, axis=1), 1e-9)
        > flat_ratio
    )
    tx = (v0[:, 0] + v1[:, 0] + v2[:, 0]) / 3.0
    tz = (v0[:, 2] + v1[:, 2] + v2[:, 2]) / 3.0
    e1 = v1[:, :2] - v0[:, :2]
    e2 = v2[:, :2] - v0[:, :2]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2.0
    xmin, xmax = float(P[:, 0].min()), float(P[:, 0].max())
    span = xmax - xmin
    west = flat & (area > min_area) & (tx <= xmin + band * span)
    east = flat & (area > min_area) & (tx >= xmax - band * span)

    def _median_z(sel):
        if not sel.any():
            return None
        order = np.argsort(tz[sel])
        zs = tz[sel][order]
        cum = np.cumsum(area[sel][order])
        return float(zs[int(np.searchsorted(cum / cum[-1], 0.5))])

    return _median_z(west), _median_z(east)



# --- a model bridge carries the road (FV=24; a user's report: no stub under it, it is driven on, no friction) ---
# Its own drawn deck is a lattice of two-sided faces (every face doubled with the opposite normal, 2 m slab, a
# raised median under the dragon): the car does not drive on it. A smooth closed collision slab traced off the
# top of that deck (bridge_deck_surface) carries the car instead, the model is drawn without collision, both
# leaned along the bridge (bridge_tilt) so the deck's two ends land on the ground roads they continue, and the
# procedural bridge under it (deck chunks, piers, beams, guard rails) is left out of every tile its footprint
# (bridge_footprint) reaches - not only the one the model is baked in.
BRIDGE_CELL_U_M = 2.0  # m between the deck surface's samples along the bridge ...
BRIDGE_CELL_V_M = 1.0  # ... and across it
BRIDGE_UP_NZ = 0.3  # a face whose normal's z is over this is a floor (a road surface) seen from above
BRIDGE_HEADROOM_M = 2.5  # a floor with no face this far above it is open to drive on (the deck's top, not the
# top of the slab's lower skin 2 m under it, and not the dragon's back over the median)
BRIDGE_TIER_BELOW_M = 0.5  # a sample's floor is the highest within this far under its column's deck level ...
BRIDGE_TIER_ABOVE_M = 1.0  # ... and this far over it (a raised median / curb, not an arch or a pylon)
BRIDGE_STEP_M = 0.5  # a deck level this much off the next column's is no slope but the deck's end (id 22: a
# lower plinth reaches 6 m past the top of each end, ~1 m down)
BRIDGE_SMOOTH_CELLS = 2  # the deck surface averaged over +-this many samples along the bridge (a lattice step)
BRIDGE_END_EPS_M = 0.03  # m the deck's ends are put over the ground road they meet
BRIDGE_TILT_MAX_M = 15.0  # an end this far off its ground is not a road's end (a high footbridge): no lean
BRIDGE_RAISE_MAX_M = 150.0  # ... onto the mapped bridge's ends: a model is lifted / lowered at most this far
BRIDGE_APRON_M = 8.0  # m the collision slab goes on past each end of the deck, sinking into the road ...
BRIDGE_APRON_DROP_M = 0.6  # ... to this far under its end row's lowest: no lip where the road meets the bridge
BRIDGE_SLAB_M = 0.5  # m thick, the collision slab (closed: its top faces up whatever side the engine collides)
BRIDGE_SLAB_VERTS_MAX = 60000  # Torque's 16-bit index buffer: coarser across the bridge over this
BRIDGE_REACH_M = 2000.0  # m from a tile a bridge model's centre may stand and its deck still reach into it
BRIDGE_FOOT_MARGIN_M = 2.0  # m past the deck's own sides a procedural bridge part still counts as under it
BRIDGE_AXIS_STEP_DEG = 1.0  # deck_axis's directions (its smallest bounding rectangle's) ...
BRIDGE_AXIS_POINTS = 20000  # ... over at most this many of the mesh's points
BRIDGE_START_SHARE = 0.2  # _deck_levels seeks the deck's level first in the columns this share of its length round
# its middle


def _column_floors(A, B, C, upf, u, vs):
    """Per sample (u, vs[j]): (all face heights there sorted, the floor ones) over triangles (A, B, C) (k, 3) in
    (u, v, z); upf (k,) the floors."""
    e0 = C[:, :2] - A[:, :2]
    e1 = B[:, :2] - A[:, :2]
    d00 = (e0 * e0).sum(1)
    d01 = (e0 * e1).sum(1)
    d11 = (e1 * e1).sum(1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    px = (u - A[:, 0])[:, None]
    py = vs[None, :] - A[:, 1][:, None]
    d20 = px * e0[:, 0, None] + py * e0[:, 1, None]
    d21 = px * e1[:, 0, None] + py * e1[:, 1, None]
    a = (d11[:, None] * d20 - d01[:, None] * d21) / den[:, None]
    b = (d00[:, None] * d21 - d01[:, None] * d20) / den[:, None]
    inside = ok[:, None] & (a >= -1e-6) & (b >= -1e-6) & (a + b <= 1 + 1e-6)
    z = A[:, 2, None] + a * (C[:, 2] - A[:, 2])[:, None] + b * (B[:, 2] - A[:, 2])[:, None]
    out = []
    for j in range(len(vs)):
        m = inside[:, j]
        if not m.any():
            out.append((None, None))
            continue
        out.append((np.sort(z[m, j]), np.sort(z[m & upf, j])))
    return out


def _nan_filter(Z, size, how):
    """Z (nu, nv) filtered over a size (du, dv) window of its finite values (how: np.nanmedian / np.nanmean);
    NaN where Z is NaN."""
    nu, nv = Z.shape
    hu, hv = size
    pad = np.full((nu + 2 * hu, nv + 2 * hv), np.nan)
    pad[hu:hu + nu, hv:hv + nv] = Z
    stack = np.stack([pad[i:i + nu, j:j + nv] for i in range(2 * hu + 1) for j in range(2 * hv + 1)])
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            out = how(stack, axis=0)
    return np.where(np.isfinite(Z), out, np.nan)


def deck_axis(xy):
    """(ang, major, minor) of points xy (k, 2) seen from above: ang (radians, in (-pi/2, pi/2]) the direction of the
    long side of their smallest bounding rectangle (BRIDGE_AXIS_STEP_DEG steps), major / minor its sides (m). A bridge
    runs along it whatever way it was drawn: Perth's Mount Henry Bridge (3dmr id 53, GitHub #1071) crosses its
    model's frame at ~28 degrees, so its north-aligned bounding box is barely longer one way than the other."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return 0.0, 0.0, 0.0
    if len(xy) > BRIDGE_AXIS_POINTS:
        xy = xy[:: int(np.ceil(len(xy) / BRIDGE_AXIS_POINTS))]
    th = np.radians(np.arange(0.0, 90.0, BRIDGE_AXIS_STEP_DEG))
    a = dnp.matmul(xy, np.stack([dnp.cos(th), dnp.sin(th)]))          # (k, m): along each direction ...
    b = dnp.matmul(xy, np.stack([-dnp.sin(th), dnp.cos(th)]))         # ... and across it
    ea, eb = np.ptp(a, axis=0), np.ptp(b, axis=0)
    k = int(np.argmin(ea * eb))
    ang, major, minor = float(th[k]), float(ea[k]), float(eb[k])
    if minor > major:
        ang, major, minor = ang + np.pi / 2, minor, major
    if ang > np.pi / 2 + 1e-12:
        ang -= np.pi
    return ang, major, minor


def deck_uv(s, xy):
    """(u, v) of points xy (k, 2) in a deck surface's / footprint's frame s (bridge_deck_surface, bridge_footprint_of):
    u along the bridge (its "ang"), v across it; a frame without "ang" (an old cache, a test's) is axis "ax"'s."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    ang = s.get("ang")
    if ang is None:
        ax = int(s["ax"])
        return xy[:, ax], xy[:, 1 - ax]
    c, sn = dnp.cos(ang), dnp.sin(ang)
    return xy[:, 0] * c + xy[:, 1] * sn, -xy[:, 0] * sn + xy[:, 1] * c


def deck_xy(s, u, v):
    """deck_uv's inverse: (x, y) arrays of the points (u, v) of frame s."""
    u, v = np.asarray(u, np.float64), np.asarray(v, np.float64)
    ang = s.get("ang")
    if ang is None:
        return (u, v) if int(s["ax"]) == 0 else (v, u)
    c, sn = dnp.cos(ang), dnp.sin(ang)
    return u * c - v * sn, u * sn + v * c


def _floor_layers(z):
    """[(count, median)] of sorted heights z split where they part by more than BRIDGE_STEP_M: the floors a column's
    samples stand on, one per layer (a deck, a pier's cap under it, the ground)."""
    if not len(z):
        return []
    cut = np.nonzero(np.diff(z) > BRIDGE_STEP_M)[0] + 1
    return [(len(g), float(np.median(g))) for g in np.split(z, cut)]


def _deck_run(opens, i0, z0):
    """(level (nu,) NaN off it, the samples carrying it) of the floor at z0 in column i0 followed out both ways
    (_deck_levels)."""
    nu = len(opens)
    level = np.full(nu, np.nan)
    level[i0] = z0
    n = 0
    for step in (1, -1):
        last, i = i0, i0 + step
        while 0 <= i < nu and abs(i - last) <= 4:
            z = opens[i]
            if len(z) >= 3:
                tol = BRIDGE_STEP_M * abs(i - last)
                w = z[(z >= level[last] - tol) & (z <= level[last] + tol)]
                if len(w) < 3:
                    break           # (floors here, none carrying on: a step)
                level[i] = float(np.median(w))
                n += len(w)
                last = i
            i += step
    return level, n


def _deck_levels(opens):
    """The deck's level per column (nu,), NaN off the deck, from opens (per column, the sorted heights of its
    samples' floors open above), or None. Each floor layer (_floor_layers, 3 samples or more) of the columns round
    the middle (within BRIDGE_START_SHARE of the bridge's length) is followed out both ways (_deck_run) to the columns
    whose floors carry on from the last one's level: a level that many samples (at least 3) stand within
    BRIDGE_STEP_M (per column) of; the deck is the run that most samples carry (it spans the bridge's length and
    width: a pylon or an arch's rib a sample or two wide, a pier's cap or the ground under the deck a few metres
    long). Other floors in a column are left out, not followed: a pier's cap under the deck, the ground between its
    piers, a lower rail deck beside it (Perth's Narrows Bridge, 3dmr id 54, GitHub #1071: the median of the lowest
    floors fell onto its first pier caps, 44 m from the middle of a 370 m deck, and the deck ended there). A gap of a
    few columns is bridged; past it, or where a column has floors but none carrying on (a step: a plinth, a lower
    road under an end), the deck ends."""
    nu = len(opens)
    have = [i for i in range(nu) if len(opens[i]) >= 3]
    if not have:
        return None
    centre = (have[0] + have[-1]) / 2.0
    reach = max(2.0, BRIDGE_START_SHARE * (have[-1] - have[0]) / 2.0)
    starts = {}                 # (a layer's level to 0.5 m) -> (its column nearest the middle, its level)
    for i in sorted(have, key=lambda i: abs(i - centre)):
        if abs(i - centre) > reach:
            break
        for cnt, z in _floor_layers(opens[i]):
            if cnt >= 3:
                starts.setdefault(round(z * 2.0), (i, z))
    best = None
    for i0, z0 in starts.values():
        level, n = _deck_run(opens, i0, z0)
        if best is None or n > best[1]:
            best = (level, n)
    return None if best is None else best[0]


def bridge_deck_surface(positions, tris):
    """The top of a bridge model's own road deck, sampled on a grid along the bridge: {"ang" (radians: the direction
    the bridge runs in positions' frame, deck_axis; deck_uv / deck_xy go between the two), "ax" (the nearer of the
    frame's axes, informative), "u0", "du" (along), "v0", "dv" (across), "z" (nu, nv) heights, NaN off the deck}; None
    when the mesh has no deck to drive on. The floors open above (BRIDGE_HEADROOM_M: the deck's top, not its slab's
    lower skin, not the dragon's back) give each column across the bridge its deck level (_deck_levels: the one
    running on from the middle); per sample the highest floor within BRIDGE_TIER_BELOW_M / BRIDGE_TIER_ABOVE_M of it
    (a raised median stays, an arch's top does not); then a 3x3 median (a lamp base, a stray face) and a mean over
    +-BRIDGE_SMOOTH_CELLS along it (the lattice's own steps, ~0.2 m on id 22)."""
    P = np.asarray(positions, np.float64).reshape(-1, 3)
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if not len(P) or not len(T):
        return None
    ang = deck_axis(P[:, :2])[0]
    frame = {"ang": ang}
    u, v = deck_uv(frame, P[:, :2])
    Q = np.c_[u, v, P[:, 2]]
    A, B, C = Q[T[:, 0]], Q[T[:, 1]], Q[T[:, 2]]
    n = np.cross(B - A, C - A)          # (a rotation: a floor's normal still points up in z)
    nz = n[:, 2] / np.maximum(dnp.norm(n, axis=1), 1e-12)
    keep = np.abs(nz) > 0.05            # (walls: no height of their own seen from above)
    A, B, C, nz = A[keep], B[keep], C[keep], nz[keep]
    upf = nz > BRIDGE_UP_NZ
    du, dv = BRIDGE_CELL_U_M, BRIDGE_CELL_V_M
    u0, v0 = float(Q[:, 0].min()), float(Q[:, 1].min())
    nu = int(np.floor(np.ptp(Q[:, 0]) / du)) + 1
    nv = int(np.floor(np.ptp(Q[:, 1]) / dv)) + 1
    us = u0 + du * np.arange(nu)
    vs = v0 + dv * np.arange(nv)
    lo_u = np.minimum(np.minimum(A[:, 0], B[:, 0]), C[:, 0])
    hi_u = np.maximum(np.maximum(A[:, 0], B[:, 0]), C[:, 0])
    floors = []
    opens = []              # per column: every floor open above, one list entry per (sample, floor)
    for i, u in enumerate(us):
        sel = np.nonzero((lo_u <= u) & (hi_u >= u))[0]
        col = _column_floors(A[sel], B[sel], C[sel], upf[sel], u, vs) if len(sel) else [(None, None)] * nv
        floors.append(col)
        open_ = []
        for allz, upz in col:
            if upz is None:
                continue
            for z in upz:
                above = allz[allz > z + 0.05]
                if not len(above) or above[0] > z + BRIDGE_HEADROOM_M:
                    open_.append(z)
        opens.append(np.sort(np.asarray(open_, np.float64)))
    level = _deck_levels(opens)
    if level is None:
        return None
    Z = np.full((nu, nv), np.nan)
    for i in range(nu):
        if not np.isfinite(level[i]):
            continue
        for j, (_allz, upz) in enumerate(floors[i]):
            if upz is None:
                continue
            w = upz[(upz >= level[i] - BRIDGE_TIER_BELOW_M) & (upz <= level[i] + BRIDGE_TIER_ABOVE_M)]
            if len(w):
                Z[i, j] = float(w.max())
    Z = _nan_filter(Z, (1, 1), np.nanmedian)
    Z = _nan_filter(Z, (BRIDGE_SMOOTH_CELLS, 0), np.nanmean)
    cols = np.isfinite(Z).sum(1) >= 3
    Z[~cols] = np.nan
    if not cols.any():
        return None
    return {"ang": ang, "ax": 0 if abs(dnp.cos(ang)) >= abs(dnp.sin(ang)) else 1, "u0": u0, "du": du, "v0": v0,
            "dv": dv, "z": Z}


def _deck_ends(surf):
    """((u, v, z) of the deck's first column's middle, the same of its last)."""
    Z = surf["z"]
    rows = np.nonzero(np.isfinite(Z).any(1))[0]
    out = []
    for i in (rows[0], rows[-1]):
        js = np.nonzero(np.isfinite(Z[i]))[0]
        out.append((surf["u0"] + surf["du"] * i, surf["v0"] + surf["dv"] * float(js.mean()),
                    float(np.median(Z[i, js]))))
    return out


def _deck_level_at(surf, u):
    """(v, z) of the deck's middle at u (its column nearest u that has deck)."""
    Z = surf["z"]
    rows = np.nonzero(np.isfinite(Z).any(1))[0]
    i = int(rows[np.argmin(np.abs(rows - (u - surf["u0"]) / surf["du"]))])
    js = np.nonzero(np.isfinite(Z[i]))[0]
    return surf["v0"] + surf["dv"] * float(js.mean()), float(np.median(Z[i, js]))


def bridge_tilt(surf, ground_at, model_id=None, road_ends=None):
    """(ax, u_a, dz_a, slope, (u_a, u_b), ang): the lean (dz = dz_a + slope (u - u_a), u the coordinate along the
    bridge: deck_uv's in the surface's frame, ang its "ang"; ax its nearer axis) that lands the deck
    (bridge_deck_surface) BRIDGE_END_EPS_M over the road at both ends, and the span (u_a, u_b) the road runs on the
    deck. road_ends: the mapped bridge's own ends [(x, y, z)] in the surface's frame
    (the procedural bridge left out under the model: where the road network leaves the bridge, at the ground road's
    height - the model may reach past them, id 22 by ~37 m a side over the riverside streets, and is buried there);
    without them the deck's own ends onto ground_at(x, y). No lean (zeros) when an end is BRIDGE_TILT_MAX_M or more
    off - onto the mapped bridge's ends, when the two ends' moves part by that much, or one is BRIDGE_RAISE_MAX_M off
    (the model bedded on the ground under it is lifted onto its roads however high: a deck with no piers drawn,
    a river's bed deep under it)."""
    ax, ang = surf["ax"], surf.get("ang")
    if road_ends:
        e = np.asarray(road_ends, np.float64).reshape(-1, 3)
        u = deck_uv(surf, e[:, :2])[0]
        sides = []
        for pick in (u <= u.min() + 10.0, u >= u.max() - 10.0):
            sides.append((float(np.median(u[pick])), float(np.median(e[pick, 2]))))
        (ua, ga), (ub, gb) = sides
        za, zb = _deck_level_at(surf, ua)[1], _deck_level_at(surf, ub)[1]
        what = "the mapped bridge's ends"
    else:
        (ua, va, za), (ub, vb, zb) = _deck_ends(surf)

        def g(u, v):
            x, y = deck_xy(surf, u, v)
            return float(ground_at(float(x), float(y)))
        ga, gb = g(ua, va), g(ub, vb)
        what = "the ground roads"
    da = ga + BRIDGE_END_EPS_M - za
    db = gb + BRIDGE_END_EPS_M - zb
    off = (abs(da - db) >= BRIDGE_TILT_MAX_M or max(abs(da), abs(db)) >= BRIDGE_RAISE_MAX_M) if road_ends \
        else max(abs(da), abs(db)) >= BRIDGE_TILT_MAX_M
    if off or ub <= ua:
        log.warning("3dmr model %s: deck %.1f / %.1f m off %s: not leaned", model_id, da, db, what)
        return ax, ua, 0.0, 0.0, (ua, ub), ang
    slope = (db - da) / (ub - ua)
    log.info("3dmr model %s: deck at u %.1f / %.1f (z %.2f / %.2f) leaned onto %s (%+.2f / %+.2f m)",
             model_id, ua, ub, za, zb, what, da, db)
    return ax, ua, da, slope, (ua, ub), ang


def tilt_dz(tilt, xy):
    """bridge_tilt's height change at points xy (k, 2) (zeros without a tilt; a tilt without its 6th element, "ang",
    runs along axis ax)."""
    if tilt is None:
        return np.zeros(len(xy))
    ax, ua, da, slope = tilt[:4]
    ang = tilt[5] if len(tilt) > 5 else None
    u = deck_uv({"ax": ax, "ang": ang}, xy)[0]
    return da + slope * (u - ua)


def write_bridge_deck(out_dir, model_id, surf, tilt=None):
    """threedmr_<id>_deck.cdae: the deck surface (bridge_deck_surface, leaned by tilt and cut to its span) as a closed invisible
    collision slab (cdae collision_only: the mod spawns a "_deck" shape with collisionType "Collision Mesh"),
    BRIDGE_SLAB_M thick, going on BRIDGE_APRON_M past each end sinking BRIDGE_APRON_DROP_M (into the ground road
    it meets: no lip at the joint). Every face wound outwards (counter-clockwise seen from outside, cdae's).
    {"shape": file name, "verts", "tris", "ends": [(x, y, z) of each end's middle, after the lean]} or None."""
    if surf is None:
        return None
    Z = np.array(surf["z"], np.float64)
    du = surf["du"]
    if tilt is not None and len(tilt) > 4:      # (only where the road runs on the deck: bridge_tilt's span)
        us = surf["u0"] + du * np.arange(Z.shape[0])
        Z[(us < tilt[4][0] - du / 2) | (us > tilt[4][1] + du / 2)] = np.nan
    rows = np.nonzero(np.isfinite(Z).any(1))[0]
    if not len(rows):
        return None
    na = int(round(BRIDGE_APRON_M / du))
    ia, ib = int(rows[0]), int(rows[-1])
    Z = np.vstack([np.full((na, Z.shape[1]), np.nan), Z, np.full((na, Z.shape[1]), np.nan)])
    ia, ib = ia + na, ib + na
    u0 = surf["u0"] - na * du
    for k in range(1, na + 1):       # the aprons: each end's own row sinking to its lowest less the drop (a raised
        for i, j in ((ia, ia - k), (ib, ib + k)):          # median too: no ledge at its end)
            Z[j] = Z[i] + (np.nanmin(Z[i]) - BRIDGE_APRON_DROP_M - Z[i]) * k / na
    nu, nv = Z.shape
    stride = 1
    while 2 * np.isfinite(Z[:, ::stride]).sum() > BRIDGE_SLAB_VERTS_MAX:
        stride += 1
    Z = Z[:, ::stride]
    dv = surf["dv"] * stride
    nv = Z.shape[1]
    U, V = np.meshgrid(u0 + du * np.arange(nu), surf["v0"] + dv * np.arange(nv), indexing="ij")
    X, Y = deck_xy(surf, U, V)
    ok = np.isfinite(Z)
    Zt = np.where(ok, Z, 0.0) + tilt_dz(tilt, np.c_[X.ravel(), Y.ravel()]).reshape(Z.shape)
    idx = -np.ones(Z.shape, np.int64)
    idx[ok] = np.arange(int(ok.sum()))
    top = np.c_[X[ok], Y[ok], Zt[ok]]
    nt = len(top)
    verts = np.vstack([top, top - [0.0, 0.0, BRIDGE_SLAB_M]])
    cell = ok[:-1, :-1] & ok[1:, :-1] & ok[1:, 1:] & ok[:-1, 1:]
    out = []

    def face(a, b, c, outward):
        n = np.cross(verts[b] - verts[a], verts[c] - verts[a])
        out.append((a, b, c) if float(dnp.matmul(n, outward)) >= 0 else (a, c, b))
    up = np.array([0.0, 0.0, 1.0])
    for i, j in zip(*np.nonzero(cell)):
        q = (idx[i, j], idx[i + 1, j], idx[i + 1, j + 1], idx[i, j + 1])
        face(q[0], q[1], q[2], up)
        face(q[0], q[2], q[3], up)
        face(q[0] + nt, q[1] + nt, q[2] + nt, -up)
        face(q[0] + nt, q[2] + nt, q[3] + nt, -up)
        # its sides with no slab beyond them: walls down to the slab's bottom
        mid = verts[list(q)].mean(0)
        for (a, b), (ni, nj) in (((q[0], q[1]), (i, j - 1)), ((q[1], q[2]), (i + 1, j)),
                                 ((q[2], q[3]), (i, j + 1)), ((q[3], q[0]), (i - 1, j))):
            if 0 <= ni < cell.shape[0] and 0 <= nj < cell.shape[1] and cell[ni, nj]:
                continue
            o = (verts[a] + verts[b]) / 2 - mid
            o[2] = 0.0
            face(a, b, b + nt, o)
            face(a, b + nt, a + nt, o)
    if not out:
        return None
    T = np.array(out, np.int64)
    ends = []
    for i in (ia, ib):
        js = np.nonzero(ok[i])[0]
        ends.append((float(X[i, js].mean()), float(Y[i, js].mean()), float(np.median(Zt[i, js]))))
    name = f"threedmr_{model_id}_deck.cdae"
    cdae.write(os.path.join(out_dir, name), verts, T, collision_only=True, close=False)   # (a placed model, as made)
    return {"shape": name, "verts": verts, "tris": T, "ends": ends}


def bridge_footprint_of(surf):
    """The deck's footprint (bridge_deck_surface) for bridge_footprint's cache: {"ang", "ax", "u0", "du", "cols":
    [[v_min, v_max] or None per column]} in the surface's frame (deck_uv)."""
    Z = surf["z"]
    cols = []
    for i in range(Z.shape[0]):
        js = np.nonzero(np.isfinite(Z[i]))[0]
        cols.append([round(surf["v0"] + surf["dv"] * float(js.min()), 2),
                     round(surf["v0"] + surf["dv"] * float(js.max()), 2)] if len(js) else None)
    fp = {"ax": int(surf["ax"]), "u0": round(float(surf["u0"]), 3), "du": float(surf["du"]), "cols": cols}
    if surf.get("ang") is not None:
        fp["ang"] = round(float(surf["ang"]), 6)
    return fp


def bridge_footprint(model_id, cache_dir, mesh):
    """A 3dmr model's bridge footprint, cached per model under <cache_dir>/threedmr_cache/bridge_<id>_v<FV>.json:
    {"bridge": False} for a model that is no bridge, else bridge_footprint_of's dict plus "bridge": True - in the
    frame of the mesh as baked (mesh(): (positions, tris) after the yaw, centred on their bbox like write_model,
    called only when not cached). Every tile near the model reads the same answer (no race with the tile that
    bakes it, which is what the old bridge_boxes.json had: a neighbour built first kept its stub)."""
    path = os.path.join(cache_dir, "threedmr_cache", f"bridge_{model_id}_v{FEATURE_VERSION}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    pos, tris, is_bridge = mesh()
    fp = {"bridge": False}
    if is_bridge:
        surf = bridge_deck_surface(pos, tris)
        if surf is not None:
            fp = dict(bridge_footprint_of(surf), bridge=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(fp, f)
    staging.replace(tmp, path)
    return fp


def under_footprint(fp, centre, xy, margin=BRIDGE_FOOT_MARGIN_M):
    """Mask (k,) of points xy (k, 2) (a tile's frame) under bridge footprint fp whose model stands at centre (x, y)
    of the same frame."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    u, v = deck_uv(fp, xy - np.asarray(centre, np.float64))
    i = np.rint((u - fp["u0"]) / fp["du"]).astype(np.int64)
    cols = fp["cols"]
    out = np.zeros(len(xy), bool)
    for k in np.nonzero((i >= 0) & (i < len(cols)))[0]:
        c = cols[i[k]]
        out[k] = c is not None and c[0] - margin <= v[k] <= c[1] + margin
    return out


DEDUP_QUANT_MM = 1000.0  # positions (metres) * this -> integer millimetre quantization (the
# duplicate count on id 22 was measured with the same 1 mm bucket)


def _tri_sides(positions, tris):
    """(keys (m, 9) int64, side (m,) int8) per triangle: keys its 3 vertices quantized (DEDUP_QUANT_MM) and
    sorted as whole (x, y, z) rows (not per-axis - a per-axis sort would also equate two triangles that merely
    share an x/y/z multiset without sharing any actual point), so any two triangles on the same 3 points share
    a key whatever their winding; side +1/-1 which way the triangle faces relative to that sorted order (two
    triangles on one key with opposite sides are a surface's front and back), 0 for a degenerate one."""
    q = np.round(np.asarray(positions, np.float64) * DEDUP_QUANT_MM).astype(np.int64)
    tri_q = q[tris]  # (m, 3, 3): triangle, vertex, coord
    vert_dtype = np.dtype([("x", np.int64), ("y", np.int64), ("z", np.int64)])
    verts = np.ascontiguousarray(tri_q).view(vert_dtype).reshape(len(tris), 3)
    srt = np.sort(verts, axis=1)
    keys = srt.view(np.int64).reshape(len(tris), 9)
    S = keys.reshape(-1, 3, 3).astype(np.float64)
    T = tri_q.astype(np.float64)
    ns = np.cross(S[:, 1] - S[:, 0], S[:, 2] - S[:, 0])
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    side = np.sign(dnp.vecdot(n, ns)).astype(np.int8)
    return keys, side


def _backface_pairs(keys, side):
    """(m,) bool: the triangle shares its 3 points (_tri_sides' keys) with another facing the other way."""
    if len(keys) == 0:
        return np.zeros(0, bool)
    _, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = inv.ravel()
    up = np.bincount(inv, weights=(side > 0)) > 0
    down = np.bincount(inv, weights=(side < 0)) > 0
    return (up & down)[inv] & (side != 0)


BACKFACE_SHARE_MIN = 0.5  # share of a slot's triangles with a modelled back face (_backface_pairs) that makes it
# one-sided - measured over the whole 3dmr.eu base it is bimodal: models built two-sided (ids 9-19, 22) carry
# 67-100% per slot, every other model a stray few (id 8: 120 of 1,137,789; id 38: 2 of 50,000), whose own
# winding nothing else vouches for (no normals) - those few back faces are dropped instead (_dedup_tris)


def slot_sidedness(positions, tris, tri_materials, gltf_double_sided):
    """Per material slot, whether the engine material must be doubleSided (materials.json "doubleSided").
    A slot is drawn one-sided (back faces culled) when its glTF material says doubleSided false (the glTF
    default) AND the slot carries modelled back faces - at least BACKFACE_SHARE_MIN of its triangles coincide
    with another facing the other way (_backface_pairs): the author built that surface's back as its own geometry, so the engine
    must not draw each face from both sides too. Drawn two-sided, every such front/back pair sits in one
    plane twice: the depth test picks a random one per pixel per frame - z-fighting. Observed on 3dmr.eu id 22
    (Dragon Bridge): 10,488 pairs of different materials (the deck's dark top over its light grey underside,
    the yellow dragon's body over its grey spikes' backs) - the whole deck flipping dark/white and the
    dragon yellow/grey with the camera (a user's screenshots). Measured over the whole 3dmr.eu base, every
    model drawn this way that carries vertex normals (ids 11-14, 19, 22, 27) has its winding agreeing with them
    (99.8-100%), so culling shows the side the author meant (ids 9, 10, 16-18 carry none: the glTF winding is
    kept as is - a mirroring node flips it, gltf_mesh). Every other slot stays doubleSided as before
    (single open planes - lattices, signs - that the author did not close: culling would cut holes)."""
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    n = len(gltf_double_sided)
    if len(tris) == 0 or n == 0:
        return [True] * n
    keys, side = _tri_sides(positions, tris)
    paired = _backface_pairs(keys, side)
    tm = np.asarray(tri_materials, np.int64).reshape(-1)
    share = np.bincount(tm[paired], minlength=n) / np.maximum(np.bincount(tm, minlength=n), 1)
    return [bool(gltf_double_sided[k]) or not share[k] >= BACKFACE_SHARE_MIN for k in range(n)]


def _dedup_tris(positions, uvs, normals, tris, tri_materials, model_id=None, double_sided=None):
    """positions/uvs/normals unchanged; tris/tri_materials (filtered by a boolean mask, never reordered) without
    the triangles that would z-fight another in the engine:

    - a triangle on the same 3 quantized points (_tri_sides) facing the same way as an earlier one - whatever
      either's material: two coincident faces facing one way are drawn into one pixel twice, one randomly
      wins per frame. The first occurrence is kept.
    - a back face (the same 3 points facing the other way) when either of the pair's own slots is drawn
      doubleSided (double_sided, per slot; slot_sidedness): that slot draws its face from both sides
      already, so the partner is the same surface twice. One is kept, a doubleSided one first. When both
      slots are one-sided, the pair is a surface's front and back (a deck's top and its underside, a wall's
      two faces) and both stay - each is culled from the side it does not face.

    double_sided None: every slot doubleSided (each back face counts as a duplicate).

    Observed on 3dmr.eu id 22 (Dragon Bridge): 76,308 triangles of which 27,665 same-material and 10,488
    different-material front/back pairs - a closed, two-sided model; drawn with every material doubleSided,
    each pair z-fought (the bridge flickered on its own, no stub road anywhere in the picture)."""
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    if len(tris) == 0:
        return positions, uvs, normals, tris, tri_materials
    keys, side = _tri_sides(positions, tris)
    tm = (
        np.asarray(tri_materials, np.int64).reshape(-1)
        if tri_materials is not None
        else np.zeros(len(tris), np.int64)
    )
    ds_slot = (
        np.ones(int(tm.max()) + 1, bool)
        if double_sided is None
        else np.asarray(list(double_sided) + [True] * max(0, int(tm.max()) + 1 - len(double_sided)), bool)
    )
    keep = np.zeros(len(tris), bool)
    _, first = np.unique(np.concatenate([keys, side.reshape(-1, 1).astype(np.int64)], 1), axis=0, return_index=True)
    keep[first] = True
    n_same = len(tris) - int(keep.sum())
    # the survivors: at most one per (key, side) - a back-face pair is a key with both a +1 and a -1 survivor
    idx = np.nonzero(keep & (side != 0))[0]
    n_back = 0
    if len(idx):
        _, inv = np.unique(keys[idx], axis=0, return_inverse=True)
        inv = inv.ravel()
        order = np.lexsort((side[idx], inv))
        a, b = order[:-1], order[1:]
        pair = inv[a] == inv[b]  # sorted by key then side: a pair is two neighbours on one key (-1 then +1)
        ia, ib = idx[a[pair]], idx[b[pair]]
        ds_a, ds_b = ds_slot[tm[ia]], ds_slot[tm[ib]]
        either = ds_a | ds_b
        # keep a doubleSided one; both doubleSided: the earlier one
        drop_b = either & (ds_a & (~ds_b | (ia < ib)))
        drop_a = either & ~drop_b
        keep[ib[drop_b]] = False
        keep[ia[drop_a]] = False
        n_back = int(either.sum())
    if n_same or n_back:
        log.info(
            "3dmr model %s: dropped %d duplicate triangle(s) (%.0f%%): %d coincident same-facing, "
            "%d back faces of a doubleSided material",
            model_id,
            n_same + n_back,
            100.0 * (n_same + n_back) / len(tris),
            n_same,
            n_back,
        )
    return (
        positions,
        uvs,
        normals,
        tris[keep],
        (tri_materials[keep] if tri_materials is not None else tri_materials),
    )


def _gltf_double_sided(gltf, mat_idx):
    """The glTF material's own doubleSided (glTF default false; no material at all: false too)."""
    mats = gltf.get("materials", [])
    if mat_idx is None or not 0 <= mat_idx < len(mats):
        return False
    return bool(mats[mat_idx].get("doubleSided", False))


def gltf_mesh(glb, model_id=None):
    """Decode a .glb into one combined mesh: (positions (n,3) float64, uvs (n,2) float64, tris (m,3) int64,
    normals (n,3) float64 or None, textures, tri_materials (m,) int64). Y-up (glTF) is converted to Z-up
    (BeamNG) and v is flipped to the Collada convention (v_dae = 1 - v_gltf). textures is a list of (bytes,
    ext, factor, has_alpha, alpha_cutoff, double_sided) (bytes/ext (None, None) for an untextured material/slot; factor its
    baseColorFactor RGB (sRGB), or None if it has none or is white; has_alpha/alpha_cutoff whether/how this
    slot needs translucency in the engine material - _resolve_texture; double_sided whether its engine material
    is drawn from both sides - slot_sidedness), one per distinct material used by a
    primitive; tri_materials indexes each triangle into it (write_model's tri_materials). model_id: only used in log
    messages (the caller's, for a model whose primitives/extensions this decoder can't all read).

    A primitive using a geometry-compression extension this decoder does not implement (observed: Draco) is
    skipped with a warning rather than raising; if every primitive in the model is unsupported this way,
    UnsupportedModelError is raised (logged) instead of handing back an empty mesh.

    The combined mesh's own stray giant flat "wedge" triangles (_wedge_mask: observed on 3dmr.eu id 3,
    Petronas) are dropped before returning, unless BNGE_3DMR_KEEP_WEDGES is set; after that, any triangle
    that duplicates another's geometry under different vertex indices is dropped too (_dedup_tris: observed
    on 3dmr.eu id 22, Dragon Bridge - the cause of that model's own self-flicker, unrelated to the road
    stub)."""
    gltf, bin_chunk = _parse_glb(glb)
    nodes = gltf.get("nodes", [])
    scene = gltf.get("scenes", [{"nodes": list(range(len(nodes)))}])[
        gltf.get("scene", 0)
    ]
    instances = []
    for root in scene.get("nodes", ()):
        _walk(nodes, root, np.eye(4), instances)

    all_pos, all_uv, all_norm, all_tris, all_tri_mat = [], [], [], [], []
    have_normals = False
    textures = []
    tex_slot = {}
    tex_cache = {}
    slot_ds = []  # per slot: its glTF material's own doubleSided
    vert_off = 0
    n_prims, n_skipped, n_dropped_verts = 0, 0, 0
    for mesh_idx, mat in instances:
        rot = mat[:3, :3]
        for prim_no, prim in enumerate(gltf["meshes"][mesh_idx].get("primitives", ())):
            if prim.get("mode", 4) != 4:
                continue
            n_prims += 1
            comp = _prim_compression(prim)
            if comp is not None:
                n_skipped += 1
                log.warning(
                    "3dmr model %s: primitive %s uses %s compression, skipped",
                    model_id,
                    prim_no,
                    "DRACO" if comp == "KHR_draco_mesh_compression" else comp,
                )
                continue
            try:
                attrs = prim["attributes"]
                pos = _accessor(gltf, bin_chunk, attrs["POSITION"]).astype(np.float64)
                uv = (
                    _accessor(gltf, bin_chunk, attrs["TEXCOORD_0"]).astype(np.float64)
                    if "TEXCOORD_0" in attrs
                    else np.zeros((len(pos), 2))
                )
                norm_raw = (
                    _accessor(gltf, bin_chunk, attrs["NORMAL"]).astype(np.float64)
                    if "NORMAL" in attrs
                    else None
                )
                idx = (
                    _accessor(gltf, bin_chunk, prim["indices"])
                    .astype(np.int64)
                    .reshape(-1, 3)
                )
            except KeyError:
                n_skipped += 1
                log.warning(
                    "3dmr model %s: primitive %s uses DRACO compression, skipped",
                    model_id,
                    prim_no,
                )
                continue

            world_pos = dnp.matmul(pos, rot.T) + mat[:3, 3]
            if norm_raw is not None:
                norm = dnp.matmul(norm_raw, rot.T)
                have_normals = True
            else:
                norm = np.zeros((len(pos), 3))
            world_pos, uv, norm, idx, dropped = _drop_nonfinite(
                world_pos, uv, norm, idx
            )
            n_dropped_verts += dropped

            if dnp.det(rot) < 0:  # a mirroring node: glTF flips the front face's winding with it
                idx = idx[:, ::-1]

            mat_idx = prim.get("material")
            if mat_idx not in tex_slot:
                tex_slot[mat_idx] = len(textures)
                textures.append(_resolve_texture(gltf, bin_chunk, mat_idx, tex_cache))
                slot_ds.append(_gltf_double_sided(gltf, mat_idx))
            slot = tex_slot[mat_idx]

            all_pos.append(
                np.stack([world_pos[:, 0], -world_pos[:, 2], world_pos[:, 1]], 1)
            )
            all_norm.append(np.stack([norm[:, 0], -norm[:, 2], norm[:, 1]], 1))
            all_uv.append(np.stack([uv[:, 0], 1.0 - uv[:, 1]], 1))
            all_tris.append(idx + vert_off)
            all_tri_mat.append(np.full(len(idx), slot, np.int64))
            vert_off += len(world_pos)

    if n_dropped_verts:
        log.warning(
            "3dmr model %s: dropped %d non-finite vertex/vertices",
            model_id,
            n_dropped_verts,
        )
    if n_prims and n_skipped == n_prims:
        log.warning(
            "3dmr model %s: all primitives unsupported (DRACO), model skipped", model_id
        )
        raise UnsupportedModelError(
            f"3dmr model {model_id}: all primitives unsupported (DRACO)"
        )

    positions = np.concatenate(all_pos, 0) if all_pos else np.zeros((0, 3))
    uvs = np.concatenate(all_uv, 0) if all_uv else np.zeros((0, 2))
    tris = np.concatenate(all_tris, 0) if all_tris else np.zeros((0, 3), np.int64)
    normals = np.concatenate(all_norm, 0) if have_normals else None
    tri_materials = (
        np.concatenate(all_tri_mat, 0) if all_tri_mat else np.zeros((0,), np.int64)
    )

    if not os.environ.get(WEDGE_KEEP_ENV):
        wedge = _wedge_mask(positions, tris)
        if wedge.any():
            log.info(
                "3dmr model %s: dropped %d wedge triangle(s)",
                model_id,
                int(wedge.sum()),
            )
            tris = tris[~wedge]
            tri_materials = tri_materials[~wedge]

    double_sided = slot_sidedness(positions, tris, tri_materials, slot_ds)
    n_culled = sum(1 for ds in double_sided if not ds)
    if n_culled:
        log.info("3dmr model %s: %d of %d material(s) one-sided (modelled back faces)", model_id,
                 n_culled, len(double_sided))
    positions, uvs, normals, tris, tri_materials = _dedup_tris(
        positions, uvs, normals, tris, tri_materials, model_id=model_id, double_sided=double_sided
    )
    textures = [tex + (ds,) for tex, ds in zip(textures, double_sided)]

    if len(tris) > MAX_TRIANGLES:
        raise UnsupportedModelError(f"3dmr model {model_id}: {len(tris)} triangles (over {MAX_TRIANGLES})")
    return positions, uvs, tris, normals, textures, tri_materials


# --- writing the shape, texture and material --------------------------------------------
def _write_texture(out_dir, name, png_bytes):
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(png_bytes)
    staging.replace(tmp, path)


def _persistent_png_dir(cache_dir):
    """<cache_dir>/threedmr_cache - the same folder fetch_model's own GLB cache lives in. Unlike out_dir (a
    session's own <root>/<sid>, wiped whenever the anchor changes), this one is never removed by a session
    sweep, so a texture written here is still there for a tile that comes back from the revisit cache on a
    later visit - which restores only the .cdae (revisit.shapes_in matches a tile JSON's "shape" field, never
    the mod's own materials.json colorMap)."""
    return os.path.join(cache_dir, "threedmr_cache")


def write_persistent_png(cache_dir, name, png_bytes):
    """threedmr_cache/<name> (_persistent_png_dir) written atomically, unless a file already there holds the
    same bytes (several sessions baking the same 3dmr.eu model would otherwise rewrite byte-identical
    textures on every visit)."""
    path = os.path.join(_persistent_png_dir(cache_dir), name)
    try:
        if os.path.getsize(path) == len(png_bytes):
            with open(path, "rb") as f:
                existing = f.read()
            if hashlib.sha256(existing).digest() == hashlib.sha256(png_bytes).digest():
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(png_bytes)
    staging.replace(tmp, path)


SPLIT_VERTEX_TRIGGER = (
    65536  # Torque3D's IBO is 16-bit (TSShape::initVertexBuffers, tsShape.cpp ~624:
)
# mShapeVertexIndices.lock(..) hands back a U16*) and TSMesh::dumpPrimitives
# (tsMesh.cpp ~2395) bases a draw call's 64k index block on its own first
# triangle's vertex index (pInfo.startVertex = indices[draw.start] &
# 0xFFFF0000) - a mesh with more vertices than this draws every index beyond
# it mod 65536, i.e. garbled/stretched triangles (Torque's own warning,
# tsMesh.cpp ~2505, is exactly this: "split this mesh into pieces of no more
# than 65k unique verts"). Observed on several 3dmr.eu models a user
# reported as "glitchy" (ids 3, 4, 7, 8, 14, 25, 26, 30).
SPLIT_VERTEX_LIMIT = (
    60000  # split_mesh's own per-section cap: some headroom under the 65536 hard limit
)


def split_mesh(positions, tris, uvs, normals, tri_materials, limit=SPLIT_VERTEX_LIMIT):
    """A mesh too big for Torque's 16-bit IBO (SPLIT_VERTEX_TRIGGER) broken into sections of at most `limit`
    unique vertices each: [{"positions", "tris", "uvs", "normals", "tri_materials"}, ...], every section's
    own compact vertex set (sequentially repacked, not a slice of the original indices).

    Triangles are walked in order and packed greedily: a triangle joins the current section if its vertices
    (new ones plus those the section already carries) still fit under `limit`, else the current section is
    closed and a new one started for it - so every triangle lands in exactly one section, none are
    duplicated, and triangle order (tri_materials' own alignment) is preserved within a section. uvs/normals
    (normals may be None) are sliced the same way as positions; tri_materials is carried through unchanged
    (it indexes a material list shared by every section, not re-based per section)."""
    positions = np.asarray(positions, np.float64)
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    uvs = np.asarray(uvs, np.float64) if uvs is not None else None
    normals = np.asarray(normals, np.float64) if normals is not None else None
    tri_materials = np.asarray(tri_materials) if tri_materials is not None else None

    sections = []
    remap = {}
    local_verts = []
    sec_tris = []
    sec_tri_mats = []

    def flush():
        idx = np.array(local_verts, np.int64)
        sections.append(
            {
                "positions": positions[idx],
                "tris": np.array(sec_tris, np.int64).reshape(-1, 3),
                "uvs": uvs[idx] if uvs is not None else None,
                "normals": normals[idx] if normals is not None else None,
                "tri_materials": (
                    np.array(sec_tri_mats, tri_materials.dtype)
                    if tri_materials is not None
                    else None
                ),
            }
        )

    for ti, tri in enumerate(tris):
        tri = [int(v) for v in tri]
        new_verts = [v for v in tri if v not in remap]
        if local_verts and len(remap) + len(new_verts) > limit:
            flush()
            remap, local_verts, sec_tris, sec_tri_mats = {}, [], [], []
            new_verts = tri
        for v in new_verts:
            remap[v] = len(local_verts)
            local_verts.append(v)
        sec_tris.append([remap[v] for v in tri])
        if tri_materials is not None:
            sec_tri_mats.append(tri_materials[ti])
    if local_verts:
        flush()
    return sections


def _write_section(
    out_dir,
    name_id,
    P,
    tris,
    uvs,
    normals,
    png_bytes,
    tri_materials,
    has_alpha,
    alpha_cutoff,
    force_multi=False,
    tex_cache_dir=None,
):
    """One section's worth of write_model's own write: threedmr_<name_id>.cdae (+ its texture(s)), named
    exactly as write_model always has for an unsplit model (force_multi False, name_id the model_id itself -
    section 0 of a split model looks identical to a model that was never split). force_multi (set for every
    section beyond the first of a split model) always writes the indexed materials/textures form
    (threedmr_<name_id>_<i>...) even when png_bytes is a single value - a split section's own geometry no
    longer lines up with write_model's single-vs-several-materials distinction, only with tri_materials.

    tex_cache_dir: write_model's own (write_model's docstring) - when given, textures are written into
    _persistent_png_dir(tex_cache_dir) (write_persistent_png) instead of out_dir, under the same names as
    always; the .cdae always goes into out_dir either way (it is re-created by the revisit cache on a later
    visit, unlike a texture)."""
    shape_name = f"threedmr_{name_id}.cdae"

    def write_tex(tex_name, png):
        if tex_cache_dir is not None:
            write_persistent_png(tex_cache_dir, tex_name, png)
        else:
            _write_texture(out_dir, tex_name, png)

    multi = force_multi or isinstance(png_bytes, (list, tuple))
    if not multi:
        material = f"threedmr_{name_id}"
        cdae.write(
            os.path.join(out_dir, shape_name),
            P,
            tris,
            normals=normals,
            uvs=uvs,
            material=material,
            object_name=material,
            close=False,            # (a placed 3dmr model, as made: not Earth's geometry)
        )
        result = {"shape": shape_name, "texture": None, "material": material}
        if png_bytes:
            tex_name = f"threedmr_{name_id}.png"
            write_tex(tex_name, png_bytes)
            result["texture"] = tex_name
        result["has_alpha"] = (
            has_alpha
            if has_alpha is not None
            else bool(png_bytes and _texture_has_alpha(png_bytes))
        )
        result["alpha_cutoff"] = alpha_cutoff
        return result

    pngs = png_bytes if isinstance(png_bytes, (list, tuple)) else [png_bytes]
    materials = [f"threedmr_{name_id}_{i}" for i in range(len(pngs))]
    cdae.write(
        os.path.join(out_dir, shape_name),
        P,
        tris,
        normals=normals,
        uvs=uvs,
        material=materials,
        tri_materials=tri_materials,
        object_name=materials[0],
        close=False,                # (a placed 3dmr model, as made)
    )
    textures = [None] * len(pngs)
    written = {}  # sha256(png bytes) -> tex_name already written: several slots sharing the same source
    # image (observed on 3dmr.eu id 10: 234 materials, several byte-identical) write one
    # file and point at it, instead of one file per slot regardless of content
    for i, png in enumerate(pngs):
        if png:
            digest = hashlib.sha256(png).hexdigest()
            tex_name = written.get(digest)
            if tex_name is None:
                tex_name = f"threedmr_{name_id}_{i}.png"
                write_tex(tex_name, png)
                written[digest] = tex_name
            textures[i] = tex_name
    has_alphas = (
        has_alpha
        if isinstance(has_alpha, (list, tuple))
        else [bool(png and _texture_has_alpha(png)) for png in pngs]
        if has_alpha is None
        else [has_alpha] * len(pngs)
    )
    alpha_cutoffs = (
        list(alpha_cutoff)
        if isinstance(alpha_cutoff, (list, tuple))
        else [alpha_cutoff] * len(pngs)
    )
    return {
        "shape": shape_name,
        "textures": textures,
        "materials": materials,
        "has_alpha": list(has_alphas),
        "alpha_cutoff": alpha_cutoffs,
    }


SUPPORT_BIN_M = (
    0.5  # _support_z's own layer height when histogramming a mesh's Z values
)
SUPPORT_DZ_CAP = (
    40.0  # _support_z never lifts a mesh more than this above its own z.min()
)
SUPPORT_SHARE = 0.05  # _support_z's threshold: a layer needs at least this share of the mesh's own vertex
# count to count as a real floor/plate rather than noise


def _support_z(z):
    """The Z height write_model beds a mesh on, in the mesh's own local Z (before any ground offset):
    scanning upward from the mesh's own floor (z.min()) in SUPPORT_BIN_M layers, the first layer that holds
    at least SUPPORT_SHARE of the mesh's own vertices - a real floor/plate/deck, as opposed to a handful of
    stray low vertices (observed on several 3dmr.eu models: a mapper modeled a terrace/plaza plate around
    their building, but left 1-2 leftover vertices tens of metres below it - georeferencing debris that used
    to BE z.min() and get bedded on the ground itself, launching the whole building 16-20 m into the air;
    on id 22, a dragon bridge, the same pattern comes from its own piers reaching down into the riverbed
    below the actual deck, which left the bridge's ends floating over the water instead of resting on it).
    Plain z.min() (the old behaviour, unchanged) when no layer anywhere reaches that share - an ordinary
    building's own foundation/floor slab is normally dense enough on its own to win on the first layer, and a
    shape with no flat floor at all (a dome, a cylinder's rounded cap) has no single layer to prefer over
    z.min() either."""
    zmin = float(z.min())
    threshold = SUPPORT_SHARE * len(z)
    bins = np.floor(z / SUPPORT_BIN_M).astype(np.int64)
    order = np.argsort(bins, kind="stable")
    sorted_bins = bins[order]
    counts = np.bincount(sorted_bins - sorted_bins.min())
    csum = np.cumsum(counts)
    # the support plane sits at most SUPPORT_DZ_CAP above the bare minimum: enough to bury
    # georeferencing debris (id 31's stray vertices 16 m below its terrace plate) without ever
    # burying an openwork tower's own legs (id 4's mass - its first platform - sits ~60 m up,
    # which is the tower standing on its legs, not debris)
    k = int(np.searchsorted(csum, threshold))
    if k >= len(counts):
        return zmin
    support = float((sorted_bins.min() + k) * SUPPORT_BIN_M)
    return support if support - zmin <= SUPPORT_DZ_CAP else zmin


def write_model(
    out_dir,
    model_id,
    positions,
    tris,
    uvs,
    normals,
    png_bytes,
    ground_z,
    offset_xy=(0.0, 0.0),
    tri_materials=None,
    has_alpha=None,
    alpha_cutoff=None,
    tex_cache_dir=None,
    tilt=None,
):
    """Write threedmr_<id>.cdae (and its texture(s)) into out_dir, atomically. positions are centered on the
    mesh's own bbox center first (glTFs are not origin-centered: some carry their original georeferenced
    offset, which would otherwise land the mesh far from its TSStatic pos), then offset by offset_xy, then
    raised so the mesh's lowest point sits at ground_z - 0.2 (bedded into the ground, not floating on top of
    it). This centering/offset is computed once, on the whole mesh, before any splitting below.

    tex_cache_dir: None (the default - out_dir gets the texture(s) too, same as the .cdae) unless the caller
    wants textures kept past out_dir's own lifetime (buildings._custom_tile_entries' real path: out_dir is a
    session folder wiped on the next anchor change, but a tile that comes back from the revisit cache afterwards
    only restores the .cdae, never the texture, leaving the mod's own materials.json pointing at a dead file -
    the bug this parameter exists to avoid). When given, the texture(s) are written into
    _persistent_png_dir(tex_cache_dir) (write_persistent_png, deduplicated by content there too) under the
    same names as always; the .cdae is unaffected, always in out_dir.

    has_alpha/alpha_cutoff: the caller's own per-slot translucency flags (_resolve_texture's, combining the
    texture's real alpha channel with the glTF material's alphaMode/alphaCutoff - information this function
    does not have, since it only sees already-PNG-encoded bytes) - passed straight through into the result
    below for the caller to hand to update_materials. None (the default) computes has_alpha from png_bytes'
    own alpha channel alone (_texture_has_alpha), the only half of that logic this function can see by itself.

    png_bytes a single bytes-or-None: one material, Returns {"shape", "texture": <png file name> or None,
    "material": "threedmr_<id>", "has_alpha", "alpha_cutoff"}. png_bytes a list of bytes-or-None (gltf_mesh's
    textures, PNG-encoded; one per distinct source material) with tri_materials (gltf_mesh's, indexing tris
    into that list): one .cdae material per list entry (cdae.encode's own several-materials support; entries
    tri_materials never actually uses are dropped there), Returns {"shape", "textures": [<png file name> or
    None, ...], "materials": ["threedmr_<id>_0", ...], "has_alpha": [bool, ...], "alpha_cutoff": [float or
    None, ...]} (same order as png_bytes).

    A mesh with more than SPLIT_VERTEX_TRIGGER vertices (Torque's 16-bit IBO - see SPLIT_VERTEX_TRIGGER) is
    broken into sections (split_mesh) first, each written as its own threedmr_<id>.cdae (section 0, same
    names as above), threedmr_<id>_s1.cdae, _s2.cdae, ... - the result then gains a "sections" key, a list of
    every section's own result dict (in order; result itself mirrors section 0's, for a caller that only
    looks at the single-section keys). A model that does not need splitting returns exactly the single-
    section form above, with no "sections" key at all.

    The result also always carries "support_dz": _support_z's own support plane minus the mesh's bare
    z.min(), in metres - 0 for an ordinary model (its floor IS its lowest point), positive when the chosen
    support plane sits above stray low vertices (buildings._custom_tile_entries logs a large value as a
    diagnostic, not a hard failure - a legitimately elevated deck, e.g. a bridge or a viewing platform, is
    also a large support_dz).

    tilt: bridge_tilt's lean of a model bridge (its deck's ends onto the ground roads), added after the bedding."""
    P = np.array(positions, np.float64).reshape(-1, 3)
    P[:, 0] -= (
        P[:, 0].min() + P[:, 0].max()
    ) / 2  # glTFs are not origin-centered: some carry
    P[:, 1] -= (
        P[:, 1].min() + P[:, 1].max()
    ) / 2  # their original georeferenced offsets (m24's
    # own origin sits 286 m off; without this the
    # mesh renders ~300 m from its TSStatic pos)
    P[:, 0] += offset_xy[0]
    P[:, 1] += offset_xy[1]
    zmin = float(P[:, 2].min())
    support_z = _support_z(P[:, 2])
    support_dz = support_z - zmin
    P[:, 2] += (ground_z - 0.2) - support_z
    if tilt is not None:    # (a model bridge leaned onto its ground roads: bridge_tilt, in the centred frame)
        P[:, 2] += tilt_dz(tilt, P[:, :2] - np.asarray(offset_xy, np.float64))

    if len(P) <= SPLIT_VERTEX_TRIGGER:
        result = _write_section(
            out_dir,
            model_id,
            P,
            tris,
            uvs,
            normals,
            png_bytes,
            tri_materials,
            has_alpha,
            alpha_cutoff,
            tex_cache_dir=tex_cache_dir,
        )
        result["support_dz"] = support_dz
        return result

    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    full_tri_materials = (
        np.asarray(tri_materials)
        if tri_materials is not None
        else np.zeros(len(tris), np.int64)
    )
    sections = split_mesh(
        P, tris, uvs, normals, full_tri_materials, limit=SPLIT_VERTEX_LIMIT
    )
    results = [
        _write_section(
            out_dir,
            model_id if k == 0 else f"{model_id}_s{k}",
            sec["positions"],
            sec["tris"],
            sec["uvs"],
            sec["normals"],
            png_bytes,
            sec["tri_materials"],
            has_alpha,
            alpha_cutoff,
            force_multi=(k > 0),
            tex_cache_dir=tex_cache_dir,
        )
        for k, sec in enumerate(sections)
    ]
    if len(results) == 1:
        results[0]["support_dz"] = support_dz
        return results[0]
    result = dict(results[0])
    result["sections"] = results
    result["support_dz"] = support_dz
    return result


def _material_stage(texture_vfs, factor=None, has_alpha=False, alpha_cutoff=None):
    """(stage, translucency) for one materials.json material: stage is its Stage 0 - diffuseColor is factor
    (a material's baseColorFactor RGB, _resolve_texture) when given - tinting the texture if there is one,
    or standing in as a plain colour if there isn't (3dmr.eu ids 47/31: several materials carry only a
    baseColorFactor, no image; written without this they'd get the plain-grey fallback below and read
    in-game as "the material didn't load"). Without a factor: white (let the texture show through as-is)
    if textured, else the grey fallback.

    translucency is None, or the 2 fields (TRANSLUCENCY_KEYS) that cut that alpha out - BeamNG reads
    them ONLY off the material's top level, never off a Stage, so they travel separately from stage
    (update_materials merges them into the entry). has_alpha (_resolve_texture's): Torque3D/BeamNG does not
    draw a colorMap's alpha channel on its own - a material with neither field reads its PNG's transparent
    pixels as opaque, baking in a solid colour (observed on 3dmr.eu id 4, the Eiffel Tower: its lattice read
    as a solid mass in-game). translucent/translucentBlendOp are NOT used here even though BeamNG's own
    example_glass_dmg doc material carries them: that material is "version": 1.5 (PBR, baseColorMap), while
    this mod's materials are the classic format (colorMap/diffuseColor Stages, no "version") - on a classic
    material those two fields at the top level are invalid and get the whole material rejected by the engine
    ("NO MATERIAL" in-game, observed after the 2026-10-25 test build broke every 3dmr-fetched material at
    once). alphaTest alone is what this mod's own working classic-format material with alpha,
    sign_speed100kph (sign_speed.materials.json), actually uses - a hard cutout by alpha value, no blending.
    alphaRef mirrors BeamNG's example_glass_dmg (alphaRef 20): default 64 (~25%) cuts a soft edge correctly
    while still fully dropping a hard 0/255 lattice; alpha_cutoff (the glTF material's own alphaCutoff, MASK
    mode only) overrides it when given."""
    color = (
        [*factor, 1]
        if factor
        else ([1, 1, 1, 1] if texture_vfs else [0.6, 0.6, 0.6, 1])
    )
    stage = {
        "diffuseColor": color,
        "specular": [0.02, 0.02, 0.02, 1.0],
        "specularPower": 4,
        "pixelSpecular": False,
    }
    if texture_vfs:
        stage["colorMap"] = texture_vfs
    translucency = None
    if has_alpha:
        translucency = {
            "alphaTest": True,
            "alphaRef": round(alpha_cutoff * 255) if alpha_cutoff else 64,
        }
    return stage, translucency


def _dead_colormap(user_dir, stage0):
    """Whether stage0's own colorMap (a materials.json Stage, _material_stage) points at a file that is gone:
    a revisit session's /temp/beamngEarth/<sid>/... folder is wiped when the anchor changes (sid is per-
    session), but an entry referencing it stays in three_dmr.materials.json forever otherwise - the real disk
    path is <user_dir> + that game-path. False (alive) for a textureless Stage (no colorMap to check)."""
    color_map = stage0.get("colorMap")
    if not color_map:
        return False
    return not os.path.exists(os.path.join(user_dir, color_map.lstrip("/")))


def update_materials(
    user_dir,
    material_name,
    texture_vfs,
    factor=None,
    has_alpha=False,
    alpha_cutoff=None,
    double_sided=True,
):
    """Ensure <user_dir>/mods/unpacked/beamng_earth/.../beamngEarth/three_dmr.materials.json has a material
    named material_name using texture_vfs (its game-path, "/temp/beamngEarth/<sid>/threedmr_<id>.png") as its
    colour map, or a grey fallback if texture_vfs is None. factor: baseColorFactor RGB to use as diffuseColor
    instead of the white/grey default (_material_stage) - a textureless material would otherwise come out
    looking unloaded in-game. has_alpha/alpha_cutoff: passed straight through to _material_stage (translucency/
    alphaTest, _resolve_texture's). double_sided: the material's "doubleSided" (gltf_mesh's slot_sidedness - false
    for a slot whose back faces are modelled, else z-fighting). Existing materials in the file are kept; a no-op if the entry already matches
    and its own colorMap file is still on disk.

    Stale entries (every other threedmr_* material whose Stage 0 colorMap names a file that no longer exists -
    its /temp/beamngEarth/<sid>/... session folder was wiped on an anchor change, revisit() can still return a
    tile whose cdae references that dead material) are dropped from the file on every call, logged; the entry
    for material_name itself is never dropped this way (it is about to be written below instead). None (no mod
    installed, logged) if the mod isn't there to write into."""
    mod_root = os.path.join(user_dir, *MOD_MATERIALS_REL[:3])
    if not os.path.isdir(mod_root):
        log.warning(
            "beamng_earth mod not installed under %s: skipped material %s",
            user_dir,
            material_name,
        )
        return None

    materials_dir = os.path.join(user_dir, *MOD_MATERIALS_REL)
    os.makedirs(materials_dir, exist_ok=True)
    path = os.path.join(materials_dir, MATERIALS_FILE)
    with _materials_locked(path):
        return _update_materials_file(path, user_dir, material_name, texture_vfs, factor, has_alpha, alpha_cutoff,
                                      double_sided)


_MATERIALS_TLOCK = threading.Lock()


@contextlib.contextmanager
def _materials_locked(path):
    """The mod's materials file for this process alone while it is read, changed and written: the road processes
    each update it, and two at once lost one's entries ("NO MATERIAL" in game). The lock file is in the system's temp
    folder, not beside it in the game's (the game watches its own folders)."""
    import tempfile
    import xproc
    lock = os.path.join(tempfile.gettempdir(),
                        "bnge_3dmr_materials_%s.lock" % hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:16])
    with _MATERIALS_TLOCK:
        held, t0 = False, time.monotonic()
        while not held and time.monotonic() - t0 < 10.0:
            held = xproc.take(lock)
            if not held:
                time.sleep(0.02)
        try:
            yield
        finally:
            if held:
                xproc.release(lock)


def _update_materials_file(path, user_dir, material_name, texture_vfs, factor, has_alpha, alpha_cutoff, double_sided):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    stale = [
        name
        for name, entry in data.items()
        if name != material_name
        and name.startswith("threedmr_")
        and _dead_colormap(user_dir, (entry.get("Stages") or [{}])[0])
    ]
    for name in stale:
        del data[name]
    if stale:
        log.info("materials: dropped %d stale entries", len(stale))

    stage0, translucency = _material_stage(texture_vfs, factor, has_alpha, alpha_cutoff)
    entry = {
        "name": material_name,
        "mapTo": material_name,
        "class": "Material",
        "Stages": [stage0, {}, {}, {}],
        "doubleSided": bool(double_sided),
    }
    if translucency is not None:
        entry.update(translucency)
    existing = data.get(material_name)
    if (
        existing is not None
        and existing.get("Stages", [{}])[0] == stage0
        and not _dead_colormap(user_dir, stage0)
        and all(existing.get(k) == entry.get(k) for k in TRANSLUCENCY_KEYS + ("doubleSided",))
    ):
        return None

    data[material_name] = entry
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    staging.replace(tmp, path)
    return None
