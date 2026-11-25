"""The lidar road heights as a prebuilt input on the project's CDN ("lidarz", the user's go 2026-11-15; work plan
.claude/work/expo-7s-nyc-2026-11-15.md step 0a): what roadz.RoadZ._lidar measures in the USGS 3DEP point clouds for a
z14 map cell (its decks', elevated stretches' and approaches' lines, the stretches' answers: a Lines with .gaps, kept
on disk as roadz/v<VERSION>/lidar/<z>/<x>/<y>.npz + .gaps.json), built once by the project's own machine
(tools/build_lidarz.py, the server's own roadz code) and signed with the decision archives' key. A cold Manhattan read
698 MB of EPT nodes in 2,538 requests for ~2 MB of answers, and its first plans were made without them (the rule) and
made again once they came: with the product the answers are a few block reads.

Used exactly as a read of the lidar here would be: RoadZ._lidar asks this module first, once it knows what it would
measure along (the cell's bridge parts and gap candidates from the map, their half widths, the 3DEP datasets to try),
and before anything is read. A hit is the builder's Lines, bit for bit (earthfast lidarz.rs: lossless records), and
goes the same way as one measured here (lines(): on disk under the same name, the same gaps). Anything else is a miss
and the lidar is read as before: off, no manifest, a block not built, a network error, a block whose signature, name
hash, cell or version key do not check out, a record that does not decode, a cell whose inputs differ from the
builder's (another map: another inputs digest in the item's key), a DEM that gives other heights than the builder's
anywhere the builder's measuring read it (FORMAT 2: the record holds every DEM read of the measuring, call by call,
and the digest of what it read; this server reads its own DEM at the same points the same way and compares: the
datum's ground points beside the roads, the deck ends, the stretches' and approaches' stations; FORMAT 1 checked
points along the map's lines only, Codex review of 206afb6f).

A hit kept on disk keeps its identity inside the cell's own file (roadz.Lines.save: the record, its lkey and item
key as an array of the npz, written whole with the lines, so cache eviction can never keep the heights and drop what
they are checked against; Codex review of 866ebb97, a file of its own beside them could be evicted alone):
RoadZ.lines takes such a cell from disk again only while kept_ok holds (the product on, the same lkey, the
same inputs from this server's map, the same DEM heights at the record's points, the record's lines the file's);
else the cell's files are removed and it is made again (the CDN, or measured here). A cell measured here has none.
The per-process memory of RoadZ (_mem) needs no identity of its own: one process's map release, DEM and lkey do not
change while it runs, and a cell is checked when it is read from disk into it.

The key (staleness: nothing measured under other rules or another DEM may stand in). The version key (lkey: sha256
of lkey_doc) holds this product's layout (FORMAT), roadz.VERSION (the local cells' folder: any change of what the lidar
read gives bumps it), the roadz kernels' versions, every roadz constant the measuring reads (params), the ground
stretches' switch (roads.LIDAR_GAPS_ON) and the DEM's decoding (dem.DECODED_VERSION) and sampling zoom. Not the
Overture release, the weekly datasets or the server's version: each item's key holds the digest of the inputs this
server's own map gives the cell (lz_inputs_digest), so a new map release misses exactly the cells whose bridges or
candidates changed, and a server update keeps the product unless roadz.VERSION changes.

Layout under BASE (BNGE_LIDARZ_URL, else <CDN_BASE>/lz):
    lz<FORMAT>/<lkey>/manifest.bnlm          signed (domain "lidarz"): {"format", "vkey" (the lkey), "doc", "blocks"}
    lz<FORMAT>/<lkey>/12/<x>/<y>.<h>.bnlz    one z12 block's cell records, signed; items "c/<z>_<x>_<y>@<digest>"
Kept under <cache>/lidarz/lz<FORMAT>/<lkey>/ (decisions.Store's handling: verified whole when downloaded, a rebuilt
block a new name, another process's download waited for).

Off unless the switch file says {"lidarz": {"enabled": true}} (cdn.lidarz_on, read at the server's start: earthd sets
BNGE_LIDARZ for its processes) or BNGE_LIDARZ=on. The builder keys are the decision archives' (decisions.pubkeys:
earthfast's TRUSTED, or BNGE_DECISIONS_PUBKEY): no trust root of its own.
"""
import hashlib
import logging
import os

import numpy as np

import decisions
import fast
import mapcells

log = logging.getLogger("earthd.lidarz")

NATIVE = 303                    # earthfast: lz_* and the dec_* functions' domain
FORMAT = 2                      # the layout, lkey_doc and the cell records (earthfast LZ_CELL_FORMAT 2; 2: every
                                # DEM read of the measuring in the record, Codex review of 206afb6f)
ENV = "BNGE_LIDARZ"
URL_ENV = "BNGE_LIDARZ_URL"
SUBDIR = "lidarz"
LANE = "lz"
CELL_Z = 14
# s a planning process that would take the rule waits for a cell's record (roadz.gap_answers: the server's prefetch
# reads cells ahead, but a block may still be on its way when the first plans start)
WAIT_S = float(os.environ.get("BNGE_LIDARZ_WAIT", "6"))
# roadz's constants the measuring reads (RoadZ._lidar and the kernels' arguments): a change of any is another lkey
PARAMS = ("LIDAR_DENSITY", "LIDAR_MAX_DEPTH", "LIDAR_NODES_MAX", "LIDAR_STEP_M", "LIDAR_PAD_M", "LIDAR_MAX_GRADE",
          "LIDAR_DATASETS", "BOX_STEP_M", "MASK_M", "DATUM_MIN_PTS", "DATUM_MAX_MAD_M", "CLIP_PAD_M", "DEM_ZOOM",
          "LIDAR_GAP_RISE_M", "LIDAR_GAP_END_TOL_M", "LIDAR_GAP_MISS", "LIDAR_GAP_MIN_M", "APPROACH_END_TOL_M",
          "APPROACH_REJOIN_M", "APPROACH_MIN_DEV_M")


def enabled():
    return os.environ.get(ENV, "").strip().lower() == "on"


def _base():
    import imagery
    return (os.environ.get(URL_ENV) or f"{imagery.CDN_BASE}/lz").strip().rstrip("/")


def lkey_doc():
    """What every record's key holds besides its cell and inputs (see the module docstring)."""
    import dem
    import roads
    import roadz
    return {"lz": FORMAT, "roadz": roadz.VERSION, "native": [roadz.NATIVE, roadz.GAP_NATIVE],
            "params": {k: getattr(roadz, k) for k in PARAMS}, "gaps": bool(roads.LIDAR_GAPS_ON),
            "dem": [dem.DECODED_VERSION, roadz.DEM_ZOOM], "cell": int(fast._ef.LZ_CELL_FORMAT)}


def lkey():
    """(hex, 32 bytes) of this server's lidarz version key."""
    d = hashlib.sha256(decisions.canonical(lkey_doc())).digest()
    return d.hex()[:32], d


def item_key(cell, digest):
    """A record's key in its block: cell (z, x, y), digest its inputs' (32 bytes; the first 16 hex kept)."""
    return f"c/{int(cell[0])}_{int(cell[1])}_{int(cell[2])}@{digest.hex()[:decisions.HASH_HEX]}"


def base_key(key):
    return key.split("@", 1)[0]


def _rings(parts, cands):
    """(rings, halves, fids) of RoadZ._lidar's inputs: the bridge parts [(lonlat, half)] then the gap candidates
    [(fid, lonlat, half)], each ring an (k, 2) float64 lon / lat array."""
    rings = [np.ascontiguousarray(np.asarray(r, np.float64)[:, :2]) for r, _h in parts]
    rings += [np.ascontiguousarray(np.asarray(c[1], np.float64)[:, :2]) for c in cands]
    halves = [float(h) for _r, h in parts] + [float(c[2]) for c in cands]
    return rings, halves, [str(c[0]) for c in cands]


def inputs_digest(key, parts, cands, names):
    """earthfast lz_inputs_digest (32 bytes) of what cell key's read measures along (RoadZ._lidar's parts, cands and
    the datasets it tries, names)."""
    rings, halves, fids = _rings(parts, cands)
    return bytes(fast._ef.lz_inputs_digest(int(key[0]), int(key[1]), int(key[2]), rings, halves, len(parts), fids,
                                           [str(n) for n in names]))


def measure(rz, key):
    """The builder's: (Lines with .gaps, calls, samples (m, 2) lat / lon, dem digest) of cell key measured from the
    point clouds (rz._lidar), with every DEM read it made (roadz._tl.dem_log) and the digest of what it read."""
    import roadz
    log_ = roadz._tl.dem_log = []
    try:
        got = rz._lidar(key)
    finally:
        roadz._tl.dem_log = None
    calls = [len(la) for la, _lo, _v in log_]
    samples = (np.stack([np.concatenate([la for la, _lo, _v in log_]), np.concatenate([lo for _la, lo, _v in log_])], 1)
               if log_ else np.zeros((0, 2)))
    vals = np.concatenate([v for _la, _lo, v in log_]) if log_ else np.zeros(0)
    return got, calls, np.ascontiguousarray(samples, np.float64), _digest(vals)


def _digest(vals):
    return bytes(fast._ef.lz_dem_digest(np.ascontiguousarray(np.asarray(vals, np.float64).ravel())))


def dem_replay(rz, calls, samples):
    """The digest of this server's DEM at the record's points, read call by call as the builder read them."""
    out, i = [], 0
    for n in calls:
        la, lo = samples[i:i + n, 0], samples[i:i + n, 1]
        out.append(np.asarray(rz._dem_raw(np.ascontiguousarray(la), np.ascontiguousarray(lo)), np.float64).ravel())
        i += n
    return _digest(np.concatenate(out) if out else np.zeros(0))


def encode(lines, dem, calls, samples):
    """A cell record (earthfast lz_encode_cell) of a roadz.Lines (with its .gaps), the builder's dem digest and its DEM
    reads (calls: each read's points; samples (m, 2) lat / lon)."""
    n = len(lines)
    refs = [str(v) for v in lines.ref] if len(lines.ref) else [""] * n
    names = [str(v) for v in lines.name] if len(lines.name) else [""] * n
    gaps = [(str(f), (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))) for f, a, b in getattr(lines, "gaps", [])]
    return bytes(fast._ef.lz_encode_cell(np.ascontiguousarray(lines.lonlat, np.float64),
                                         np.ascontiguousarray(lines.z, np.float64),
                                         np.ascontiguousarray(lines.off, np.int64),
                                         np.ascontiguousarray(lines.level, np.int8), refs, names,
                                         bool(lines.corrected), gaps, dem, [int(c) for c in calls],
                                         np.ascontiguousarray(np.asarray(samples, np.float64).reshape(-1, 2))))


def decode(buf):
    """(roadz.Lines with .gaps, dem digest, calls, samples) of a cell record; ValueError when it is damaged."""
    import roadz
    lonlat, z, off, level, refs, names, corrected, gaps, dem, calls, samples = fast._ef.lz_decode_cell(buf)
    got = roadz.Lines(lonlat, z, off, level, refs, names, corrected)
    got.gaps = [(str(f), (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))) for f, a, b in gaps]
    return got, bytes(dem), [int(c) for c in calls], np.asarray(samples, np.float64)


def record_for(rz, key):
    """The builder's: (Lines, record, item key) of cell key, or None (nothing a server would read there)."""
    parts, cands, names = rz.lidar_inputs(key)
    if not parts or not names:
        return None
    ikey = item_key((int(key[0]), int(key[1]), int(key[2])), inputs_digest(key, parts, cands, names))
    got, calls, samples, dem = measure(rz, key)
    return got, encode(got, dem, calls, samples), ikey


def record_ok(rz, buf):
    """Does this server's DEM give the heights a record's measuring read (the builder: a record made before)?"""
    _got, dem, calls, samples = decode(buf)
    return dem_replay(rz, calls, samples) == dem


def same_lines(a, b):
    """Are two roadz.Lines (and their gaps) the same bit for bit?"""
    for f in ("lonlat", "z", "off", "level"):
        x, y = np.asarray(getattr(a, f)), np.asarray(getattr(b, f))
        if x.dtype != y.dtype or x.shape != y.shape or x.tobytes() != y.tobytes():
            return False
    n = len(a)
    ra = [str(v) for v in a.ref] if len(a.ref) else [""] * n
    rb = [str(v) for v in b.ref] if len(b.ref) else [""] * n
    na = [str(v) for v in a.name] if len(a.name) else [""] * n
    nb = [str(v) for v in b.name] if len(b.name) else [""] * n
    ga = [(str(f), tuple(map(float, p)), tuple(map(float, q))) for f, p, q in getattr(a, "gaps", [])]
    gb = [(str(f), tuple(map(float, p)), tuple(map(float, q))) for f, p, q in getattr(b, "gaps", [])]
    return ra == rb and na == nb and bool(a.corrected) == bool(b.corrected) and ga == gb


def identity_bytes(ident):
    """A CDN cell's identity (lkey hex, item key, record bytes) as bytes (kept inside its roadz file)."""
    vk, ikey, rec = ident
    return f"BNLK1 {vk} {ikey}\n".encode("ascii") + bytes(rec)


def identity_of(buf):
    """(lkey hex, item key, record bytes) of identity_bytes' bytes; ("", "", b"") when unreadable (fails kept_ok)."""
    try:
        head, rec = bytes(buf).split(b"\n", 1)
        magic, vk, ikey = head.decode("ascii").split(" ")
        return (vk, ikey, rec) if magic == "BNLK1" else ("", "", b"")
    except (ValueError, UnicodeDecodeError):
        return ("", "", b"")


def kept_ok(rz, key, lines):
    """May a CDN cell kept on disk (its lines as loaded, with their identity lines.lidarz) stand for cell key now? The
    product on, the same lkey, the same inputs from this server's map, the same DEM heights where the builder read
    them, and the record's lines those on disk. Never raises."""
    try:
        if not enabled():
            return False
        vk, ikey, rec = lines.lidarz
        if vk != lkey()[0]:
            return False
        parts, cands, names = rz.lidar_inputs(key)
        if not parts or not names:
            return False
        k = (int(key[0]), int(key[1]), int(key[2]))
        if ikey != item_key(k, inputs_digest(key, parts, cands, names)):
            return False
        got, dem, calls, samples = decode(rec)
        return same_lines(got, lines) and dem_replay(rz, calls, samples) == dem
    except Exception as e:
        log.info("lidarz: kept cell %s: %s: %s", key, type(e).__name__, e)
        return False


class Store(decisions.Store):
    """The lidarz blocks of one cache folder in this process (decisions.Store's manifests, downloads and checks)."""
    DOMAIN = "lidarz"
    LAYOUT = FORMAT
    PREFIX = "lz"
    SUBDIR = SUBDIR
    EXT = "bnlz"
    MANIFEST = "manifest.bnlm"
    LANE = LANE
    NATIVE = NATIVE
    WHAT = "lidarz"

    @staticmethod
    def default_base():
        return _base()

    def _unbuilt(self, vk, bx, by):
        pass                        # (no on-demand builds of this product)

    def record(self, cell, ikey, fetch=True):
        """The record bytes of item ikey of cell (z, x, y), or None (no block, not in it)."""
        vk, vkb = lkey()
        bx, by = decisions.block_of(cell)
        got = self.block(vk, vkb, bx, by, fetch)
        if got is None:
            self._count("misses")
            return None
        ks = self.keys(vk, bx, by, got)
        if ikey not in ks:
            if any(base_key(k) == base_key(ikey) for k in ks):
                self._count("stale")        # (built from other inputs: another map's bridges or candidates)
            self._count("misses")
            return None
        try:
            return fast._ef.dec_block_item(got[1], ikey, domain=self.DOMAIN)
        except ValueError as e:         # (a damaged file on disk: dropped, read here)
            log.warning("lidarz: block %s/%s: %s", bx, by, e)
            self._drop(vk, bx, by, got[0])
            return None


_STORES = {}


def store_for(cache_dir):
    with decisions._STORES_LOCK:
        s = _STORES.get(cache_dir)
        if s is None:
            s = _STORES[cache_dir] = Store(cache_dir)
        return s


def cell(rz, key, parts, cands, names, fetch=True):
    """The builder's Lines (with .gaps) of cell key for these inputs (RoadZ._lidar's parts, cands and the datasets
    names it would try), or None: read the lidar as before. Never raises."""
    if not enabled() or rz.dir is None:
        return None
    try:
        cache_dir = os.path.dirname(os.path.dirname(rz.dir))     # (<cache>/roadz/v<VERSION>)
        k = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        if k.z != CELL_Z:
            return None
        s = store_for(cache_dir)
        ikey = item_key((k.z, k.x, k.y), inputs_digest(k, parts, cands, names))
        data = s.record((k.z, k.x, k.y), ikey, fetch)
        if data is None:
            return None
        got, dem, calls, samples = decode(data)
        if dem != dem_replay(rz, calls, samples):
            s._count("dem")             # (our DEM is not the builder's there: measured here)
            log.info("lidarz: cell %s: the DEM differs from the builder's: the lidar read here", k)
            return None
        s._count("hits")
        got.lidarz = (lkey()[0], ikey, bytes(data))     # (its identity, kept with it on disk: keep)
        log.debug("lidarz: cell %s from the CDN: %d lines, %d stretches", k, len(got), len(got.gaps))
        return got
    except Exception as e:              # (never in the way: the lidar read here)
        log.info("lidarz: cell %s: %s: %s", key, type(e).__name__, e)
        return None


def prune(cache_dir, keep_s=7 * 86400.0):
    """Other version keys' folders untouched for keep_s removed (the server's start). Count removed."""
    import shutil
    import time
    root = os.path.join(cache_dir, SUBDIR, f"lz{FORMAT}")
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    cur = lkey()[0]
    n = 0
    for vk in names:
        p = os.path.join(root, vk)
        try:
            if vk == cur or time.time() - os.path.getmtime(p) < keep_s:
                continue
            shutil.rmtree(p)
            n += 1
        except OSError:
            pass
    return n


def record_info(buf):
    """{"lines", "vertices", "gaps", "bytes"} of a cell record (the builder's stats)."""
    got, _dem, calls, _s = decode(buf)
    return {"lines": len(got), "vertices": int(len(got.z)), "gaps": len(got.gaps), "bytes": len(buf),
            "dem_reads": int(sum(calls))}


def manifest_doc(vk, blocks, built_by, pub):
    """The manifest's JSON object (the builder's)."""
    import time
    return {"format": FORMAT, "vkey": vk, "doc": lkey_doc(), "blocks": blocks,
            "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "builder": built_by, "key": pub.hex()[:16]}

