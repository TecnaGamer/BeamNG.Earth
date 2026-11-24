"""The per-cell data-quality record ("q"): the adaptive world-generation plan's section 2.2 (job J2).

How complete a z14 map cell's data is, read by the evidence model's ladders (a missing tag in a cell where most roads
carry it is "probably none", where few do it is "unknown") and by the gates (places split by data quality). The kernel
is earthfast quality.rs (its module doc has the layout and the rules); this module is its glue:

    counts(key, layers, lanes)      one cell's own sums from its map layers (LAYERS) and its t:lanes slice
    record(key, nine)               the record's data part from the 3 x 3 cells' counts (ring(key) order)
    meta(key)                       the source block (DEM, imagery, canopy) from this server's own tables
    Source(md).record(key)          what the server reads: the CIP cell's "q" section when it has one of this VERSION,
                                    else made here from the same layers (the 3 x 3 cells' map layers, which the server
                                    then reads or fetches), plus meta(key)
    fields(rec)                     {field: value} of a record (the kernel's reading of the layout)

The data part is what tools/build_cip.py stores as section "q" (its 3 x 3 sums made at build time: no halo reads for a
cell with CIP). The source block is never stored: it comes from this server's tables (a new imagery source or DEM area
needs no new build). The data part of a cell with nothing in its 3 x 3 cells is the same bytes everywhere (the archive
keeps identical cells once).

Identity: the builder's section and Source.record's own making give the same bytes for the same layers (the kernel's
integer sums, deterministic maths; tests/test_quality.py, ~/bnge-tests/j2quality). A consumer keys what it makes from
the record on digest(rec).
"""
import hashlib
import json
import logging
import threading
from collections import OrderedDict

import fast
import mapcells

log = logging.getLogger("earthd.quality")

NATIVE = 614
SECTION = "q"
# the map layers a cell's counts read (MapData layer names; a cell's file arrays as CIP holds them)
LAYERS = ("roads", "connectors", "buildings", "land_use", "infrastructure", "water")
MEMO = 512                  # cells' counts kept per Source (~9 kB each)


def _ef():
    fast.require(NATIVE, "quality record")
    return fast._ef


def version():
    return int(_ef().QUALITY_VERSION)


def ring(key):
    """The 3 x 3 cells of key, row-major from the north-west (index 4: key); None beyond the poles' rows (x wraps)."""
    key = mapcells.check(key)
    n = 1 << key.z
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            y = key.y + dy
            out.append(mapcells.CellKey(key.z, (key.x + dx) % n, y) if 0 <= y < n else None)
    return out


# The arrays quality.rs reads (tests/test_quality.py checks this against the kernel's source): only these are handed
# over (a dense cell's other columns, the infrastructure tags' JSON above all, were half of its counting time)
KEEP = frozenset((
    "kind", "part_off", "ring_off", "coord_off", "lonlat", "own",
    "attr.subtype", "attr.class", "attr.subclass", "attr.name", "attr.src_record", "attr.src_dataset",
    "attr.osm_lanes", "attr.osm_width", "attr.osm_maxspeed", "attr.osm_surface", "attr.osm_sidewalk", "attr.osm_lit",
    "attr.osm_smoothness", "attr.osm_oneway",
    "rule.width.feature", "rule.surface.feature", "rule.max_speed_kmh.feature", "rule.oneway.feature",
    "rule.connectors.feature", "rule.connectors.value", "rule.connectors.start",
    "attr.cx", "attr.cy", "attr.height", "attr.num_floors", "attr.roof_shape", "attr.roof_color", "attr.roof_material",
    "attr.facade_material", "attr.facade_color", "attr.has_parts"))
KEEP_IDS = ("connectors",)      # (the layers whose ids it reads)


def _arrays_list(layer, arrays):
    import cip
    keep = KEEP | ({"ids"} if layer in KEEP_IDS else set())
    return cip.arrays_section({k: v for k, v in arrays.items() if k in keep})


def counts(key, layers, lanes=None):
    """Cell key's counts blob from {layer: its cell file arrays} (LAYERS; a missing layer counts as empty) and its
    t:lanes slice (JSON bytes; None: lane tags unknown)."""
    key = mapcells.check(key)
    return _ef().quality_counts(key.z, key.x, key.y, {ly: _arrays_list(ly, a) for ly, a in layers.items()},
                                None if lanes is None else bytes(lanes))


def record(key, nine):
    """The record's data part of cell key from the ring(key) cells' counts blobs (None: not counted)."""
    key = mapcells.check(key)
    return _ef().quality_record(key.z, key.x, key.y, [None if c is None else bytes(c) for c in nine])


def fields(rec):
    """{field: value} of a record (data part, or data part + source block)."""
    return dict(_ef().quality_fields(bytes(rec)))


def digest(rec):
    """The content key of a record (what a consumer keys its product on)."""
    return hashlib.sha256(bytes(rec)).hexdigest()[:16]


def stored_ok(sec):
    """Is sec (a CIP "q" section's bytes) a data part this kernel reads?"""
    ef = _ef()
    return isinstance(sec, (bytes, bytearray)) and len(sec) == ef.QUALITY_DATA_LEN and sec[0] == ord("Q") \
        and sec[1] == ef.QUALITY_VERSION


def lanes_slice(md, key, roads_arrays):
    """Cell key's t:lanes slice as tools/build_cip.py stores it (railtags.cell_slice of the lane shard for the cell's
    roads' way ids, JSON bytes), or None (no lane build known: lane tags unknown). LookupError: the shard of a known
    build could not be had."""
    t = getattr(md, "lane_tags", None)
    if t is None or t.known() is None:
        return None
    sh = t.of_cell(key)
    if sh is None:
        raise LookupError(f"{key}: the lane shard could not be had")
    import decisions
    import mapgeom
    import railtags
    roads = mapgeom.Features.from_arrays("roads", {k: v for k, v in roads_arrays.items() if k != "provenance"}, "")
    return json.dumps(railtags.cell_slice(sh, key, decisions.ids_of(roads)), separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# --- the source block ----------------------------------------------------------------------------------------------------
_META = OrderedDict()
_META_LOCK = threading.Lock()


def _mt_depth(x6, y6):
    """Mapterhorn's deepest zoom under z6 tile (x6, y6) from the file list this process has read (dem's), or None."""
    import dem
    idx = dem._coverage.get("index")
    if idx is None:
        return None
    return int(idx.get((x6, y6), dem.GLOBAL_ZOOM))


def meta_inputs(key):
    """The source block's inputs for cell key from this server's tables: (dem_src, dem_zoom, dem_bits, roadz_bits,
    imagery [(name, url, finest zoom, leaf_off)], canopy)."""
    import dem
    import hkdtm
    import hklidar
    import hrdem
    import imagery
    import maprasters
    import roadz
    key = mapcells.check(key)
    lat, lon = key.center()
    bits = 0
    if abs(lat) >= 84.9:
        src, zoom = 4, 0
    elif hkdtm.ON and _meets(hrdem.tile_bounds(key.z, key.x, key.y), hklidar.BOUNDS):
        src, zoom = 3, 0
    else:
        d = _mt_depth(key.x >> (key.z - dem.COVERAGE_ZOOM), key.y >> (key.z - dem.COVERAGE_ZOOM))
        if d is None:
            src, zoom, bits = 1, dem.GLOBAL_ZOOM, 8
        elif d > dem.GLOBAL_ZOOM:
            src, zoom = 2, d
        else:
            src, zoom = 1, d
    if src in (1, 4):
        bits |= 1
    if dem.HRDEM and hrdem.in_bounds(key.z, key.x, key.y):
        bits |= 2
    if dem.HRDEM_NEAR and hrdem.covers(key.z, key.x, key.y):
        bits |= 4
    rz = 0
    for s in roadz.sources_at(lat, lon, True):
        rz |= {"lidar": 1, "hk": 2, "ign": 4, "nvdb": 8}.get(s, 0)
    img = []
    for name in imagery.DEFAULT_CHAIN:
        s = imagery.SOURCES[name]
        if s.bounds:
            cov = imagery.in_bounds(s, key.z, key.x, key.y)
        elif name in imagery.US_CREDIT_BOXES:
            cov = imagery.in_bounds(imagery.dc_replace(s, bounds=imagery.US_CREDIT_BOXES[name]), key.z, key.x, key.y)
        else:
            cov = True
        if cov:
            img.append((name, s.url, int(imagery.zoom_caps(s, lat)[0]), bool(s.leaf_off)))
    canopy = 1 if maprasters.CHM_RELEASE else 0
    return src, zoom, bits, rz, img, canopy


def _meets(a, b):
    """Do boxes a and b (south, west, north, east) overlap?"""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def meta(key):
    """Cell key's source block (bytes)."""
    key = mapcells.check(key)
    k = str(key)
    with _META_LOCK:
        got = _META.get(k)
    if got is not None:
        return got
    src, zoom, bits, rz, img, canopy = meta_inputs(key)
    got = _ef().quality_meta(key.z, key.x, key.y, src, zoom, bits, rz, img, canopy)
    with _META_LOCK:
        _META[k] = got
        while len(_META) > 4096:
            _META.popitem(last=False)
    return got


# --- what the server reads -------------------------------------------------------------------------------------------------
class Source:
    """The records of a MapData's cells: the CIP cell's section, or made from the 3 x 3 cells' layers."""

    def __init__(self, md):
        self.md = md
        self._memo = OrderedDict()          # str(cell) -> counts blob (None: its layers could not be had)
        self._lock = threading.Lock()
        self.stats = dict(stored=0, made=0, short=0)

    def cell_counts(self, key, kind="demand"):
        """Cell key's counts blob from its layers (as the server reads them: the CIP cell's sections, the cache, the
        sources), or None (a layer could not be had, or only its parquet stand-in)."""
        import mapdata
        k = str(key)
        with self._lock:
            if k in self._memo:
                self._memo.move_to_end(k)
                return self._memo[k]
        layers = {}
        try:
            for ly in LAYERS:
                got, _hit = self.md.layer_arrays(key, ly, kind)
                if isinstance(got, mapdata.StandIn):
                    raise ValueError(f"{key} {ly}: a parquet stand-in")
                layers[ly] = got
            # (the roads with their OSM tags, the run's build, as the decode joins them: the file is Overture's alone)
            layers["roads"], ok = self.md.roads_joined_arrays(key, layers["roads"], kind)
            if not ok:
                raise ValueError(f"{key}: OSM road tags not of the run's build")
            lanes = lanes_slice(self.md, key, layers["roads"])
            blob = counts(key, layers, lanes)
        except Exception as e:      # (a source failed: no record this time)
            log.info("quality %s: %s: %s", key, type(e).__name__, e)
            return None
        with self._lock:
            self._memo[k] = blob
            while len(self._memo) > MEMO:
                self._memo.popitem(last=False)
        return blob

    def data(self, key, kind="demand"):
        """Cell key's data part: the CIP cell's section (this VERSION), else made here; None when a 3 x 3 cell's
        layers could not be had."""
        key = mapcells.check(key)
        r = getattr(self.md, "cip", None)
        if r is not None and r.data_builds().get("osmtags") == self.md.osm_tags_build():
            # (counted with the OSM road tags of the run's build only: the roads' tags are joined at the decode)
            sec = r.one(key, SECTION, kind)
            if sec is not None and stored_ok(sec):
                self.stats["stored"] += 1
                return bytes(sec)
        nine = []
        for k in ring(key):
            if k is None:
                nine.append(None)
                continue
            c = self.cell_counts(k, kind)
            if c is None:
                self.stats["short"] += 1
                return None
            nine.append(c)
        self.stats["made"] += 1
        return record(key, nine)

    def record(self, key, kind="demand"):
        """Cell key's whole record (data part + source block), or None."""
        d = self.data(key, kind)
        return None if d is None else d + meta(key)
