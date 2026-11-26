"""build_osm_road_areas.py PLANET OUT_DIR: OpenStreetMap's mapped road areas (area:highway=* polygons: the actual paved
width beside the centre line; traffic islands, footways, junction areas, ...) cut into one blob per zoom-14 tile (the
server's map cells) and packed into PMTiles archive parts the server reads a tile of with one range request
(server/osmareas.py, earthfast roadareas.rs oah_decode), as build_osm_building_years.py does for the building years.

    python3 tools/build_osm_road_areas.py planet-latest.osm.pbf out/osmareas
    python3 tools/build_osm_road_areas.py https://.../planet-YYMMDD.osm.pbf out/osmareas     (streamed by curl: no disk)
    python3 tools/build_osm_road_areas.py berlin-latest.osm.pbf out/osmareas --bbox 13.3,52.4,13.5,52.6     (tests)

Why (the user, 2026-11-22, an OSM screenshot of a split slip road: "2 road layers: one the general road line and the
second the actual road width"): about 495k area:highway objects worldwide (taginfo 2026-11-22: traffic_island 158k,
footway 100k, residential 34k, service 31k, tertiary 22k, secondary 17k, emergency 13k, primary 12k ...; 462k ways,
33k multipolygon relations), dense in German, Dutch, Polish and Czech cities. The road system (native/roadsys
areas.rs) measures its generated surface against them (the M0 audit's edge distance, IoU, width error, splits) and,
behind BNE_ROADSYS_AREAS=on, takes their edges as soft targets for the carriageway edges.

Three passes (a planet's ways come after its nodes and its relations after its ways: one streaming pass cannot place
them): 1. ways and relations with area:highway (a closed way is a polygon; a relation of type multipolygon is its
outer and inner member ways); 2. those relations' member ways (their node refs), skipped when there is none; 3. the
nodes of all of them (their places, OSM's own 1e-7 degree integers: exact, no float rounding). A relation's rings are
its member ways joined end to end in member order (ways.join_rings); one that does not close, a way or relation with a
node missing, and rings under 4 points are left out (counted in the manifest's "dropped").

Output, in OUT_DIR (the building years' layout, build_osm_building_years.write_build):
- <key>/osmareas-000.pmtiles, ...: the tiles in tile-id order in parts of at most --part-bytes each, every part a
  whole PMTiles v3 archive (tile type 0: the blob below, raw deflate; gzip directories; zoom SHARD_ZOOM only;
  clustered). <key>: the build time and a digest of its parts; its files never change once published.
- manifest.json: {"format", "zoom", "built", "key", "source", "source_etag", "parts": [...], "bytes", "tiles",
  "rows", "license", "counts", "values"} (osmtagshards.parts_of reads the parts; "counts": objects read, polygons
  kept, dropped by reason; "values": the polygons per area:highway value, the 40 most common).

A tile holds every polygon whose float32 bbox touches it (a polygon on a tile edge is in every tile it touches), in
key order (2 x the OSM id, + 1 for a relation), each once.
Blob (OAH1, little endian, uv = LEB128 varint, zz = zig-zag): "OAH1"; uv ndict, ndict x (uv length, UTF-8) (the
tile's texts, sorted); uv n; per polygon: uv key delta (the first from 0, then >= 1: ascending), uv value (dict
index), uv name, ref, surface (dict index + 1, 0: none), uv zz(layer), u8 flags (1 bridge, 2 tunnel), uv nlinks,
nlinks x uv way id deltas (a relation's members with a street role: the highway way it is the area of, ascending),
uv nrings, per ring: u8 role (0 outer, 1 inner), uv count, count x 2 uv zz(delta of lon / lat x 1e7 from the ring's
previous point, the first from 0). Rings are closed (the last point equals the first) as OSM has them.

Deterministic: the passes' output sorted by id, rings in member order, the tiles in tile-id order, texts sorted; the
same planet makes the same bytes (the key's time aside). Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import os
import sys
import time
from collections import Counter

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_osm_tag_shards as ots      # noqa: E402  (the PMTiles writer, tile maths, varints)
import build_osm_building_years as by   # noqa: E402  (write_build: the parts, their digests, the manifest)

FORMAT = 1
SHARD_ZOOM = 14
PART_BYTES = ots.PART_BYTES
TILE_MAGIC = b"OAH1"
KEY = "area:highway"
TEXT_TAGS = ("name", "ref", "surface")
STREET_ROLES = {"street", "highway", "road", "way", "centre", "center", "centreline", "centerline"}
MAX_RING_POINTS = 20000         # a ring this long is left out (a mapping error; the decoder's own bound is higher)


# ------------------------------------------------------------------------------------------------ the passes
def _osmium():
    import osmium
    return osmium


def pass_objects(src):
    """(ways {id: (tags dict, refs int64)}, relations {id: (tags dict, [(member way id, role)])}) with area:highway:
    closed ways (first ref == last), multipolygon relations' way members."""
    osmium = _osmium()
    ways, rels, t0, k = {}, {}, time.time(), 0
    with _Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.WAY | osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter(KEY))
        for o in fp:
            k += 1
            if k % 100_000 == 0:
                print(f"  pass 1: {k:,} objects, {len(ways):,} ways, {len(rels):,} relations, "
                      f"{time.time() - t0:.0f} s", flush=True)
            tags = {t.k: t.v for t in o.tags}
            if o.is_way():
                refs = np.array([n.ref for n in o.nodes], np.int64)
                ways[o.id] = (tags, refs)
            elif o.is_relation() and tags.get("type") == "multipolygon":
                rels[o.id] = (tags, [(m.ref, m.role) for m in o.members if m.type == "w"])
    return ways, rels


def pass_member_ways(src, ids):
    """{way id: refs} of the member ways ids (a set)."""
    osmium = _osmium()
    out = {}
    if not ids:
        return out
    with _Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(sorted(ids)))
        for o in fp:
            out[o.id] = np.array([n.ref for n in o.nodes], np.int64)
    return out


def pass_nodes(src, ids):
    """(sorted node ids, x, y: OSM's own 1e-7 degree integers) of the node ids (np.int64, unique, sorted)."""
    osmium = _osmium()
    gi, gx, gy, t0 = [], [], [], time.time()
    with _Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(ids.tolist()))
        for o in fp:
            loc = o.location
            if loc.valid():
                gi.append(o.id)
                gx.append(loc.x)
                gy.append(loc.y)
            if len(gi) % 2_000_000 == 0 and gi:
                print(f"  pass 3: {len(gi):,} of {len(ids):,} nodes, {time.time() - t0:.0f} s", flush=True)
    i = np.asarray(gi, np.int64)
    o = np.argsort(i, kind="stable")
    return i[o], np.asarray(gx, np.int64)[o], np.asarray(gy, np.int64)[o]


class _Source:
    """A pass's input: a path or an http(s) URL streamed by curl (build_rail_tags._Source)."""

    def __init__(self, src):
        self.src, self.proc = src, None

    def __enter__(self):
        if self.src.startswith(("http://", "https://")):
            import subprocess
            osmium = _osmium()
            self.proc = subprocess.Popen(["curl", "-sfL", "--retry", "5", "-A", "BeamNG.Earth data build", self.src],
                                         stdout=subprocess.PIPE)
            return osmium.io.File(f"/proc/self/fd/{self.proc.stdout.fileno()}", "pbf")
        return self.src

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.stdout.close()
            rc = self.proc.wait()
            if rc not in (0, None) and exc[0] is None:
                raise SystemExit(f"curl {self.src}: exit {rc}")


# ------------------------------------------------------------------------------------------------ the polygons
def join_rings(members):
    """Closed rings (lists of node refs, first == last) of a relation's member ways [(refs list, role)] of one role,
    joined end to end in member order: each ring starts at the first unused way and takes, until it closes, the first
    unused way (in member order) that begins or ends where it ends (reversed when it ends there). None when a ring
    does not close."""
    left = [list(r) for r in members if len(r) >= 2]
    rings = []
    while left:
        ring = left.pop(0)
        while ring[0] != ring[-1]:
            end = ring[-1]
            for i, w in enumerate(left):
                if w[0] == end:
                    ring.extend(w[1:])
                    left.pop(i)
                    break
                if w[-1] == end:
                    ring.extend(w[-2::-1])
                    left.pop(i)
                    break
            else:
                return None
        rings.append(ring)
    return rings


def polygons(ways, rels, member_refs, nid, nx, ny, counts=None):
    """[(key, tags, links, [(role, (k, 2) int64 x / y)])] sorted by key; counts (a Counter): dropped by reason."""
    counts = counts if counts is not None else Counter()

    def place(refs):
        j = np.searchsorted(nid, refs)
        j = np.minimum(j, max(len(nid) - 1, 0))
        if not len(nid) or not np.all(nid[j] == refs):
            return None
        return np.column_stack((nx[j], ny[j]))

    out = []
    for wid in sorted(ways):
        tags, refs = ways[wid]
        if len(refs) < 4 or refs[0] != refs[-1]:
            counts["open_way"] += 1
            continue
        if len(refs) > MAX_RING_POINTS:
            counts["too_long"] += 1
            continue
        xy = place(refs)
        if xy is None:
            counts["missing_node"] += 1
            continue
        out.append((2 * wid, tags, [], [(0, xy)]))
    for rid in sorted(rels):
        tags, members = rels[rid]
        links = sorted({w for w, role in members if role in STREET_ROLES})
        rings, ok = [], True
        for role_code, roles in ((0, ("outer", "")), (1, ("inner",))):
            ms = [member_refs.get(w) for w, role in members if role in roles]
            if any(m is None for m in ms):
                ok = False
                break
            got = join_rings([m.tolist() for m in ms])
            if got is None:
                ok = False
                break
            for r in got:
                if len(r) < 4 or len(r) > MAX_RING_POINTS:
                    ok = False
                    break
                xy = place(np.asarray(r, np.int64))
                if xy is None:
                    ok = False
                    break
                rings.append((role_code, xy))
            if not ok:
                break
        if not ok or not any(r[0] == 0 for r in rings):
            counts["bad_relation"] += 1
            continue
        out.append((2 * rid + 1, tags, links, rings))
    out.sort(key=lambda p: p[0])
    return out


def _layer(tags):
    try:
        return max(-8, min(8, int(str(tags.get("layer", "0")).strip())))
    except ValueError:
        return 0


def _flags(tags):
    yes = lambda v: v not in (None, "no", "0", "false")     # noqa: E731
    return (1 if yes(tags.get("bridge")) else 0) | (2 if yes(tags.get("tunnel")) else 0)


def encode_tile(keys, polys):
    """A tile's blob (the module's docstring) of its polygons (keys ascending, polys[i] the record of keys[i])."""
    texts = sorted({str(p[1].get(KEY, "")) for p in polys} |
                   {str(p[1][t]) for p in polys for t in TEXT_TAGS if t in p[1]})
    index = {s: i for i, s in enumerate(texts)}
    out = bytearray(TILE_MAGIC)
    out += ots.uv1(len(texts))
    for s in texts:
        out += ots.name(s)
    out += ots.uv1(len(polys))
    prev = 0
    for k, (key, tags, links, rings) in zip(keys, polys):
        out += ots.uv1(int(key) - prev)
        prev = int(key)
        out += ots.uv1(index[str(tags.get(KEY, ""))])
        for t in TEXT_TAGS:
            out += ots.uv1(index[str(tags[t])] + 1 if t in tags else 0)
        out += ots.uv1(ots.zz1(_layer(tags)))
        out.append(_flags(tags))
        out += ots.uv1(len(links))
        lp = 0
        for w in links:
            out += ots.uv1(int(w) - lp)
            lp = int(w)
        out += ots.uv1(len(rings))
        for role, xy in rings:
            out.append(role)
            out += ots.uv1(len(xy))
            d = np.diff(np.asarray(xy, np.int64), axis=0, prepend=np.zeros((1, 2), np.int64))
            out += ots.uv(ots.zz(d.ravel()))
    return bytes(out)


def cut(polys, z=SHARD_ZOOM, clip=None):
    """{(x, y): (keys ascending, records)} of the polygons: each in every zoom-z tile its float32 bbox touches
    (ots.tile_ranges, as the other shards test a tile; clip (x0, x1, y0, y1): only those tiles)."""
    if not polys:
        return {}
    outer = [np.concatenate([xy for role, xy in p[3] if role == 0]) for p in polys]
    bb = np.array([[o[:, 0].min(), o[:, 1].min(), o[:, 0].max(), o[:, 1].max()] for o in outer], np.float64) / 1e7
    bb = bb.astype(np.float32)
    tx0, tx1, ty0, ty1 = ots.tile_ranges(bb[:, 0], bb[:, 1], bb[:, 2], bb[:, 3], z)
    if clip is not None:
        tx0, tx1 = np.maximum(tx0, clip[0]), np.minimum(tx1, clip[1])
        ty0, ty1 = np.maximum(ty0, clip[2]), np.minimum(ty1, clip[3])
    out = {}
    for i, p in enumerate(polys):          # (polys sorted by key: each tile's list ascends)
        for x in range(int(tx0[i]), int(tx1[i]) + 1):
            for y in range(int(ty0[i]), int(ty1[i]) + 1):
                out.setdefault((x, y), []).append(i)
    return {t: (np.array([polys[i][0] for i in v], np.uint64), [polys[i] for i in v]) for t, v in out.items()}


def outer_bbox_ok(p, bbox):
    """Whether polygon p's outer ring's bbox meets bbox (lon0, lat0, lon1, lat1)."""
    xy = np.concatenate([xy for role, xy in p[3] if role == 0]) / 1e7
    return xy[:, 0].max() >= bbox[0] and xy[:, 0].min() <= bbox[2] and xy[:, 1].max() >= bbox[1] \
        and xy[:, 1].min() <= bbox[3]


def build(src, out, bbox=None, log=print, z=SHARD_ZOOM, part_bytes=PART_BYTES, second=None, third=None):
    """The archive's parts and the manifest in out from src (a .osm.pbf path or URL; bbox: only that region;
    second / third: the source again for passes 2 and 3, when a stream cannot be read twice)."""
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    etag0 = ots.etag_of(src) if src.startswith(("http://", "https://")) else None
    ways, rels = pass_objects(src)
    log(f"pass 1: {len(ways):,} ways, {len(rels):,} multipolygon relations, {time.time() - t0:.0f} s")
    want = {w for _tags, ms in rels.values() for w, _role in ms}
    member = {w: ways[w][1] for w in want if w in ways}
    rest = want - set(member)
    if rest:
        member.update(pass_member_ways(second or src, rest))
    log(f"pass 2: {len(member):,} member ways, {time.time() - t0:.0f} s")
    refs = [r for _t, r in ways.values()] + list(member.values())
    ids = np.unique(np.concatenate(refs)) if refs else np.zeros(0, np.int64)
    nid, nx, ny = pass_nodes(third or second or src, ids)
    log(f"pass 3: {len(nid):,} of {len(ids):,} nodes, {time.time() - t0:.0f} s")
    counts = Counter()
    polys = polygons(ways, rels, member, nid, nx, ny, counts)
    if bbox is not None:
        polys = [p for p in polys if outer_bbox_ok(p, bbox)]
    if etag0 is not None and ots.etag_of(src) != etag0:
        raise SystemExit(f"{src} changed during the build ({etag0} -> {ots.etag_of(src)})")
    values = Counter(str(p[1].get(KEY, "")) for p in polys)
    clip = None
    if bbox is not None:
        clip = tuple(int(v[0]) for v in ots.tile_ranges(*(np.array([v], np.float32) for v in bbox), z))
    tiles = cut(polys, z, clip)
    log(f"{len(polys):,} polygons in {len(tiles):,} tiles, {time.time() - t0:.0f} s")
    extra = {"counts": {"ways": len(ways), "relations": len(rels), "polygons": len(polys), **dict(sorted(counts.items()))},
             "values": dict(values.most_common(40))}
    return by.write_build(tiles, encode_tile, out, z=z, part_bytes=part_bytes, bbox=bbox, prefix="osmareas",
                          title="BeamNG.Earth OSM road areas", fmt=f"oah{FORMAT}", fmt_num=FORMAT,
                          source=src if src.startswith("http") else os.path.basename(src), source_etag=etag0,
                          log=log, t0=t0, extra=extra)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path or an http(s) URL (streamed)")
    ap.add_argument("out")
    ap.add_argument("--bbox", help="lon0,lat0,lon1,lat1: only this region (tests)")
    ap.add_argument("--part-bytes", type=int, default=PART_BYTES, help="an archive part at most (bytes)")
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(",")) if a.bbox else None
    build(a.planet, a.out, bbox, log=lambda s: print(s, flush=True), part_bytes=a.part_bytes)


if __name__ == "__main__":
    sys.exit(main())
