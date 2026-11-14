"""Overture's PMTiles as the map cells' source (pmtiles.py + earthfast pmtiles.rs): the archive format (header,
Hilbert tile ids, directories with runs and leaves), the MVT decode, a cell's features whole from the tiles they cross
(lines and polygons stitched at tile edges, junction vertices put back), the columns and rule tables the parquet path
gives, the fetch rounds, the mirrors' order and failover, an archive that changed, and MapData's use of it (its own
cache folder and key, the parquet fallback).

No network: small archives written here (an MVT and PMTiles v3 writer of a few lines) behind a fake fetcher."""
import gzip
import itertools
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import mapgeom as mg  # noqa: E402
import netfetch  # noqa: E402
import pmtiles  # noqa: E402
import revisit  # noqa: E402

EXT = 4096
CELL = mapcells.CellKey(14, 4706, 6245)        # Fells Point, Baltimore
EAST = (14, 4707, 6245)


# ------------------------------------------------------------------------------------------- writers
def _varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num, wire, payload):
    key = _varint((num << 3) | wire)
    if wire == 0:
        return key + _varint(payload)
    if wire == 2:
        return key + _varint(len(payload)) + payload
    raise ValueError(wire)


def _zz(v):
    return (v << 1) ^ (v >> 31)


def _geom(kind, parts):
    """MVT geometry commands of parts (tile pixel rings / lines / points)."""
    out, cx, cy = [], 0, 0
    for ring in parts:
        x, y = ring[0]
        out += [(1 & 7) | (1 << 3), _zz(x - cx), _zz(y - cy)]
        cx, cy = x, y
        if len(ring) > 1:
            out.append((2 & 7) | ((len(ring) - 1) << 3))
            for x, y in ring[1:]:
                out += [_zz(x - cx), _zz(y - cy)]
                cx, cy = x, y
        if kind == 3:
            out.append(7 | (1 << 3))
    return out


def _value(v):
    if isinstance(v, bool):
        return _field(7, 0, int(v))
    if isinstance(v, int):
        return _field(6, 0, (v << 1) ^ (v >> 63))
    if isinstance(v, float):
        return b"\x19" + struct.pack("<d", v)        # field 3, fixed64
    return _field(1, 2, v.encode())


def mvt(layers):
    """layers: {name: [(kind, parts, props)]} -> MVT bytes (extent 4096)."""
    out = b""
    for name, feats in layers.items():
        keys, vals, body = [], [], _field(15, 0, 2) + _field(1, 2, name.encode())
        for n, (kind, parts, props) in enumerate(feats):
            tags = []
            for k, v in props.items():
                if v is None:
                    continue
                if k not in keys:
                    keys.append(k)
                vv = _value(json.dumps(v, separators=(",", ":")) if isinstance(v, (list, dict)) else v)
                if vv not in vals:
                    vals.append(vv)
                tags += [keys.index(k), vals.index(vv)]
            fb = _field(1, 0, n + 1) + _field(2, 2, b"".join(_varint(t) for t in tags)) + _field(3, 0, kind)
            fb += _field(4, 2, b"".join(_varint(c) for c in _geom(kind, parts)))
            body += _field(2, 2, fb)
        body += b"".join(_field(3, 2, k.encode()) for k in keys) + b"".join(_field(4, 2, v) for v in vals)
        body += _field(5, 0, EXT)
        out += _field(3, 2, body)
    return out


def tile_id(z, x, y):
    """The PMTiles spec's zxy -> tile id (an independent reference for the native one)."""
    acc = sum(4 ** t for t in range(z))
    n = 1 << z
    d, s = 0, n // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s //= 2
    return acc + d


def directory(entries):
    """entries: [(tile_id, offset, length, run)] sorted -> directory bytes (gzip)."""
    b = _varint(len(entries))
    last = 0
    for t, *_ in entries:
        b += _varint(t - last)
        last = t
    b += b"".join(_varint(e[3]) for e in entries)
    b += b"".join(_varint(e[2]) for e in entries)
    for k, e in enumerate(entries):
        contiguous = k > 0 and e[1] == entries[k - 1][1] + entries[k - 1][2]
        b += _varint(0 if contiguous else e[1] + 1)
    return gzip.compress(b, mtime=0)


def archive(tiles, leaf=False, maxz=14):
    """tiles: {(z, x, y): mvt bytes} -> a PMTiles v3 archive (gzip tiles; leaf: the entries in a leaf directory)."""
    data, entries = b"", []
    for (z, x, y), t in sorted(tiles.items(), key=lambda kv: tile_id(*kv[0])):
        g = gzip.compress(t, mtime=0)
        entries.append((tile_id(z, x, y), len(data), len(g), 1))
        data += g
    meta = gzip.compress(b"{}", mtime=0)
    if leaf:
        leaf_dir = directory(entries)
        root = directory([(entries[0][0], 0, len(leaf_dir), 0)])
    else:
        leaf_dir, root = b"", directory(entries)
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaf_dir)
    h = b"PMTiles" + bytes([3])
    h += struct.pack("<QQQQQQQQ", root_off, len(root), meta_off, len(meta), leaf_off, len(leaf_dir), data_off,
                     len(data))
    h += struct.pack("<QQQ", len(tiles), len(entries), len(entries))
    h += bytes([1, 2, 2, 1, 0, maxz])
    h += struct.pack("<iiiiiiiiB", 0, 0, 0, 0, 0, 0, 0, 0, 0)  # bounds, centre (unused)
    h = h[:127].ljust(127, b"\0")
    return h + root + meta + leaf_dir + data


_ETAGS = itertools.count(1)


class FakeFetcher:
    """Range reads of in-memory archives by URL; per host: errors to raise, the requests seen."""

    def __init__(self, files, etag=None):
        # (an ETag of its own per archive: the native side keeps decompressed tiles by release, theme and ETag)
        self.files, self.etag = files, etag or f'"v{next(_ETAGS)}"'
        self.down = set()
        self.seen = []
        self.lock = threading.Lock()

    def get(self, url, *, kind="demand", headers=None, lane=None):
        host = netfetch.host_of(url)
        with self.lock:
            self.seen.append((host, url.rsplit("/", 1)[-1], headers.get("Range")))
        if host in self.down:
            raise netfetch.HostUnavailable(f"{host} down", url, host)
        body = self.files.get(url.rsplit("/", 1)[-1])
        if body is None:
            raise netfetch.NotFound(f"{url}: 404", url, host, status=404)
        a, b = headers["Range"].split("=")[1].split("-")
        a, b = int(a), min(int(b), len(body) - 1)
        return netfetch.Response(206, body[a:b + 1], {"etag": self.etag}, 0.05 if "tiles." in host else 0.2)


def world(t, px, py):
    return t[1] * EXT + px, t[2] * EXT + py


def lonlat(wx, wy, z=14):
    n = (1 << z) * EXT
    return wx / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy / n))))


# The cell's tile A and its east neighbour B. A road W0 -> W1 -> W2 across their edge, W1 a junction straight on the
# line (Planetiler's simplification drops it: A's piece has none); a building across the edge; its connectors.
A = (14, CELL.x, CELL.y)
W = [world(A, 3000, 2000), world(A, 4000, 2050), world(EAST, 904, 2100)]
SRC_REL = {"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "r1@1", "property": "/routes"}


def road_props():
    return {"id": "seg-a", "subtype": "road", "class": "motorway", "names": {"primary": "Harbor Rd"}, "version": 2,
            "connectors": [{"connector_id": "c0", "at": 0.0}, {"connector_id": "c1", "at": 0.5},
                           {"connector_id": "c2", "at": 1.0}],
            "road_flags": [{"values": ["is_bridge", "is_link"], "between": [0.25, 0.5]}],
            "rail_flags": [{"values": ["is_tunnel"]}],
            "road_surface": [{"value": "paved"}], "width_rules": [{"value": 7.5, "between": [0.0, 0.5]}],
            "level_rules": [{"value": 1, "between": [0.25, 0.5]}], "subclass_rules": [{"value": "link"}],
            "speed_limits": [{"max_speed": {"value": 55, "unit": "mph"}},
                             {"max_speed": {"value": 40, "unit": "km/h"}, "when": {"heading": "forward"}}],
            "access_restrictions": [{"access_type": "denied", "when": {"heading": "backward"}},
                                    {"access_type": "denied", "when": {"heading": "forward", "mode": ["foot"]}},
                                    {"access_type": "denied", "between": [0.25, 0.5], "when": {"vehicle": [
                                        {"dimension": "height", "comparison": "greater_than", "value": 3.3,
                                         "unit": "m"}]}},
                                    {"access_type": "denied", "between": [0.6, 0.7], "when": {"vehicle": [
                                        {"dimension": "weight", "comparison": "greater_than", "value": 7.5,
                                         "unit": "t"}, {"dimension": "height", "comparison": "greater_than",
                                                        "value": 12, "unit": "ft"}]}}],
            "routes": [{"network": "US:I", "ref": "95", "between": [0.0, 0.75]}, {"network": "x"}],
            "sources": [SRC_REL,
                        {"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w10@3", "property": "",
                         "between": [0.0, 0.3]},
                        {"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w11@1", "property": "",
                         "between": [0.3, 1.0]}]}


def local(t, w):
    return int(round(w[0] - t[1] * EXT)), int(round(w[1] - t[2] * EXT))


def transportation():
    p = road_props()
    other = {"id": "seg-b", "subtype": "road", "class": "residential", "sources": [
        {"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w12@1", "property": ""}],
        "routes": [{"network": "US:I", "ref": "95"}]}
    # A: W0 .. its buffer cut (x = 4096 + 64), W1 left out; B: from its buffer cut (x = -64) to W2
    cut_a = (4160, 2058)
    cut_b = (-64, 2052)
    a = mvt({"segment": [(2, [[local(A, W[0]), cut_a]], p),
                         (2, [[(100, 100), (200, 300)]], other)],
             "connector": [(1, [[local(A, W[0])]], {"id": "c0", "sources": [{"dataset": "OpenStreetMap"}]}),
                           (1, [[local(A, W[1])]], {"id": "c1", "sources": [{"dataset": "OpenStreetMap"}]})]})
    b = mvt({"segment": [(2, [[cut_b, local(EAST, W[2])]], p)],
             "connector": [(1, [[local(EAST, W[2])]], {"id": "c2", "sources": [{"dataset": "OpenStreetMap"}]})]})
    return {A: a, EAST: b}


BUILDING = [world(A, 4000, 100), world(EAST, 100, 100), world(EAST, 100, 200), world(A, 4000, 200)]


def buildings():
    # MVT: exterior clockwise on screen (positive area, y down); A's piece cut at x = 4160, B's at x = -64
    props = {"id": "bld-1", "height": 12.5, "num_floors": 3, "roof_shape": "gabled", "has_parts": False,
             "is_underground": False, "names": {"primary": "Pier"}, "sources": [
                 {"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w5@1"},
                 {"dataset": "USGS", "license": "PD", "property": "/properties/height"}]}
    a = mvt({"building": [(3, [[(4000, 100), (4160, 100), (4160, 200), (4000, 200)]], props),
                          (3, [[(10, 10), (60, 10), (60, 60), (10, 60)], [(20, 20), (20, 50), (50, 50), (50, 20)]],
                           {"id": "bld-0", "sources": [{"dataset": "Microsoft", "license": "ODbL"}]})]})
    b = mvt({"building": [(3, [[(-64, 100), (100, 100), (100, 200), (-64, 200)]], props)]})
    return {A: a, EAST: b}


class Base(unittest.TestCase):
    def setUp(self):
        fast.require(pmtiles.NATIVE, "pmtiles tests")
        self.tmp = tempfile.mkdtemp(prefix="pmt-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def reader(self, files, **kw):
        f = FakeFetcher(files, **kw)
        return pmtiles.Reader(os.path.join(self.tmp, "raw"), f), f


class ArchiveFormatTest(Base):
    def test_tile_ids(self):
        self.assertEqual([fast._ef.pmt_tile_id(1, x, y) for x, y in ((0, 0), (0, 1), (1, 1), (1, 0))], [1, 2, 3, 4])
        for z, x, y in ((0, 0, 0), (14, 4706, 6245), (14, 4824, 6157), (13, 2353, 3122), (5, 31, 0)):
            self.assertEqual(fast._ef.pmt_tile_id(z, x, y), tile_id(z, x, y))

    def test_directory_runs_and_leaves(self):
        d = directory([(10, 0, 5, 1), (11, 5, 7, 3), (20, 100, 4, 0)])
        self.assertEqual(fast._ef.pmt_find(d, 10), (0, 5, False))
        self.assertEqual(fast._ef.pmt_find(d, 13), (5, 7, False))      # a run of three tiles with the same bytes
        self.assertIsNone(fast._ef.pmt_find(d, 14))
        self.assertIsNone(fast._ef.pmt_find(d, 9))
        self.assertEqual(fast._ef.pmt_find(d, 25), (100, 4, True))     # past a leaf pointer: look in the leaf

    def test_header(self):
        a = archive({A: b"x"})
        h = fast._ef.pmt_header(a)
        self.assertEqual((h["root_offset"], h["tile_compression"], h["max_zoom"]), (127, 2, 14))
        with self.assertRaises(ValueError):
            fast._ef.pmt_header(b"not an archive" * 20)

    def test_leaf_directories_are_followed(self):
        r, f = self.reader({"transportation.pmtiles": archive(transportation(), leaf=True)})
        feats, _ = r.query("roads", CELL)
        self.assertEqual(list(feats.ids), ["seg-a", "seg-b"])


class RoadsTest(Base):
    def setUp(self):
        super().setUp()
        self.r, self.f = self.reader({"transportation.pmtiles": archive(transportation())})
        self.feats, self.sources = self.r.query("roads", CELL)

    def test_whole_line_from_both_tiles_with_its_junction(self):
        f = self.feats
        i = f.index("seg-a")
        pts = f.parts(i)
        self.assertEqual(len(pts), 1)
        line = pts[0][0]
        want = [lonlat(*W[0]), lonlat(*W[1]), None, lonlat(*W[2])]
        self.assertEqual(len(line), 4)      # W0, the junction W1 put back, the joint at the tile edge, W2
        for k in (0, 1, 3):
            np.testing.assert_allclose(line[k], want[k], atol=1e-9)
        # the joint: on the tile edge, within half a tile unit of the true line
        ex, ey = lonlat(*world(EAST, 0, 2054.8))
        self.assertAlmostEqual(line[2][0], ex, places=9)
        self.assertLess(abs(line[2][1] - ey) * 110540, 0.15)
        self.assertEqual(f.kind[i], mg.LINE)

    def test_columns_as_the_parquet_path(self):
        f, i = self.feats, self.feats.index("seg-a")
        a = f.attrs
        self.assertEqual((a["subtype"][i], a["class"][i], a["name"][i]), ("road", "motorway", "Harbor Rd"))
        # src_*: the feature's own source (not the route relation listed first), its record the way covering most
        self.assertEqual((a["src_dataset"][i], a["src_record"][i]), ("OpenStreetMap", "w11@1"))
        self.assertEqual(f.rules["src_ways"].for_feature(i), [("w10@3", 0.0, np.float32(0.3)),
                                                              ("w11@1", np.float32(0.3), 1.0)])
        self.assertEqual(f.rules["flags"].for_feature(i), [("is_bridge", 0.25, 0.5), ("is_link", 0.25, 0.5),
                                                           ("is_tunnel", 0.0, 1.0)])
        self.assertEqual(f.rules["surface"].for_feature(i), [("paved", 0.0, 1.0)])
        self.assertEqual(f.rules["width"].for_feature(i), [(7.5, 0.0, 0.5)])
        self.assertEqual(f.rules["level"].for_feature(i), [(1.0, 0.25, 0.5)])
        self.assertEqual(f.rules["subclass"].for_feature(i), [("link", 0.0, 1.0)])
        (v, s, e), = f.rules["max_speed_kmh"].for_feature(i)       # the unconditional limit only, mph to km/h
        self.assertAlmostEqual(v, 55 * 1.609344, places=4)
        self.assertEqual(f.rules["oneway"].for_feature(i), [(1.0, 0.0, 1.0)])     # backward denied; foot only: not
        (v1, s1, e1), (v2, s2, e2) = f.rules["max_height_m"].for_feature(i)        # a maxheight sign: the height limits
        self.assertAlmostEqual(v1, 3.3, places=5)
        self.assertEqual((s1, e1), (0.25, 0.5))
        self.assertAlmostEqual(v2, 12 * 0.3048, places=5)                           # (feet)
        self.assertEqual([c for c, _, _ in f.rules["connectors"].for_feature(i)], ["c0", "c1", "c2"])
        self.assertEqual(f.rules["connectors"].for_feature(i)[1][1:], (0.5, 0.5))
        self.assertEqual(self.sources[("OpenStreetMap", "ODbL-1.0")], 4)
        # a line is owned where its first vertex lies (every cell that lists it agrees, even one that cuts it short)
        np.testing.assert_allclose([a["cx"][i], a["cy"][i]], lonlat(*W[0]), atol=1e-9)
        # the same schema as the parquet path's empty layer
        import overture
        e = overture.empty("roads")
        self.assertEqual(sorted(e.attrs), sorted(f.attrs))
        self.assertEqual(sorted(e.rules), sorted(f.rules))
        for k in e.attrs:
            self.assertEqual(e.attrs[k].dtype.kind, f.attrs[k].dtype.kind, k)

    def test_fetch_rounds(self):
        # the cell's tile first, then the neighbour the road leaves it for; each once (on disk after)
        tiles = [rng for host, name, rng in self.f.seen if name == "transportation.pmtiles"]
        self.assertGreaterEqual(len(tiles), 3)
        n = len(self.f.seen)
        r2 = pmtiles.Reader(os.path.join(self.tmp, "raw"), self.f)
        f2, _ = r2.query("roads", CELL)
        self.assertEqual(len(self.f.seen), n)       # all from disk
        self.assertEqual(mapdata.digest_arrays(f2.to_arrays("")), mapdata.digest_arrays(self.feats.to_arrays("")))

    def test_connectors_and_routes(self):
        c, _ = self.r.query("connectors", CELL)
        self.assertEqual(list(c.ids), ["c0", "c1"])       # c2 lies in the east cell
        self.assertEqual(c.kind.tolist(), [mg.POINT, mg.POINT])
        rr, _ = self.r.query("road_routes", CELL)
        self.assertEqual(list(rr.ids), ["seg-a"])         # seg-b: residential, not a route the signs name
        self.assertEqual(rr.attrs["routes_txt"][0], "us:i|95|0.0|0.75")

    def test_the_east_cell_has_the_same_road(self):
        east, _ = self.r.query("roads", mapcells.CellKey(*EAST))
        i, j = east.index("seg-a"), self.feats.index("seg-a")
        np.testing.assert_array_equal(east.parts(i)[0][0], self.feats.parts(j)[0][0])
        own = mapcells.owner(np.r_[east.attrs["cx"][i]], np.r_[east.attrs["cy"][i]])
        self.assertIn((int(own[0][0]), int(own[1][0])), ((CELL.x, CELL.y), EAST[1:]))


class LongLineTest(Base):
    def test_owner_agrees_where_the_line_is_cut(self):
        # a rail across four tiles A B C D (west to east), each cell reading only its own 3x3 (cap 0): A sees it cut at
        # the B/C edge, D at the same edge from the other side; only A (its first vertex) owns it
        props = {"id": "rail-1", "subtype": "rail", "class": "standard_gauge",
                 "sources": [{"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w9@1"}]}
        xs = [CELL.x + k for k in range(4)]
        tiles = {}
        for k, x in enumerate(xs):
            a = (100 if k == 0 else -64, 2000)
            b = (4000 if k == 3 else 4160, 2000)
            tiles[(14, x, CELL.y)] = mvt({"segment": [(2, [[a, b]], props)]})
        r, _ = self.reader({"transportation.pmtiles": archive(tiles)})
        with mock.patch.dict(pmtiles.CAP, {14: 0}):
            fa, _ = r.query("roads", CELL)
            fd, _ = r.query("roads", mapcells.CellKey(14, xs[3], CELL.y))
        owners = []
        for f, x in ((fa, xs[0]), (fd, xs[3])):
            i = f.index("rail-1")
            ox, oy = mapcells.owner(f.attrs["cx"][i:i + 1], f.attrs["cy"][i:i + 1])
            owners.append(int(ox[0]) == x and int(oy[0]) == CELL.y)
        self.assertEqual(owners, [True, False])

    def cut_rail(self, cap):
        """A rail across five tiles (T0..T4, west to east, one horizontal line) read from the middle one's cell with
        the given cap, so that it comes cut: a bridge at 0.70-0.72 of the WHOLE line, a tunnel at 0.30-0.35, another
        bridge in T0 at 0.05-0.08, a connector at the middle of each tile (its `at` the arc fraction there).
        Returns (features, index of the line, (x of its start, x of its end) in world units)."""
        xs = [CELL.x + k for k in range(5)]
        x0, x1 = xs[0] * EXT + 100, xs[4] * EXT + 3996
        props = {"id": "rail-long", "subtype": "rail", "class": "standard_gauge",
                 "connectors": [{"connector_id": f"k{k}", "at": (xs[k] * EXT + 2048 - x0) / (x1 - x0)}
                                for k in range(5)],
                 "rail_flags": [{"values": ["is_bridge"], "between": [0.05, 0.08]},
                                {"values": ["is_tunnel"], "between": [0.30, 0.35]},
                                {"values": ["is_bridge"], "between": [0.70, 0.72]}],
                 "level_rules": [{"value": 1, "between": [0.05, 0.08]}, {"value": -1, "between": [0.30, 0.35]},
                                 {"value": 1, "between": [0.70, 0.72]}],
                 "sources": [{"dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w9@1"}]}
        tiles = {}
        for k, x in enumerate(xs):
            a = (100 if k == 0 else -64, 2048)
            b = (3996 if k == 4 else 4160, 2048)
            tiles[(14, x, CELL.y)] = mvt({
                "segment": [(2, [[a, b]], props)],
                "connector": [(1, [[(2048, 2048)]], {"id": f"k{k}", "sources": [{"dataset": "OpenStreetMap"}]})]})
        r, _ = self.reader({"transportation.pmtiles": archive(tiles)})
        with mock.patch.dict(pmtiles.CAP, {14: cap}):
            f, _ = r.query("roads", mapcells.CellKey(14, xs[2], CELL.y))
        return f, f.index("rail-long"), (x0, x1)

    @staticmethod
    def span(f, i):
        """x (world units) of the first and last vertex of the line as the cell has it."""
        ring = f.parts(i)[0][0]
        n = (1 << 14) * EXT
        return [(lon + 180) / 360 * n for lon, _ in (ring[0], ring[-1])]

    def test_a_cut_lines_ranges_are_put_on_the_line_we_have(self):
        f, i, (x0, x1) = self.cut_rail(1)
        a, b = self.span(f, i)
        self.assertAlmostEqual(b - a, 3 * EXT, delta=2.0)           # (cut: three of the five tiles)
        for name in ("level", "flags"):
            rows = f.rules[name].for_feature(i)
            # the T0 bridge (0.05-0.08) is off the line we have: dropped; the tunnel and the T3 bridge are where they
            # are along it (the fraction of OUR line at the true x of the range's ends)
            self.assertEqual(len(rows), 2, (name, rows))
            for (_, s, e), (u, v) in zip(rows, ((0.30, 0.35), (0.70, 0.72))):
                for got, frac in ((s, u), (e, v)):
                    self.assertAlmostEqual(float(got), (x0 + frac * (x1 - x0) - a) / (b - a), delta=1e-3, msg=name)
        c = f.rules["connectors"].for_feature(i)
        self.assertEqual([x[0] for x in c], ["k1", "k2", "k3"])       # the connectors on the line we have
        self.assertAlmostEqual(float(c[1][1]), (EXT + 2048) / (3 * EXT), delta=1e-3)

    def test_a_line_that_comes_whole_keeps_its_ranges(self):
        f, i, (x0, x1) = self.cut_rail(6)
        a, b = self.span(f, i)
        self.assertAlmostEqual(b - a, x1 - x0, delta=2.0)
        self.assertEqual([(float(s), float(e)) for _, s, e in f.rules["level"].for_feature(i)],
                         [(float(np.float32(s)), float(np.float32(e))) for s, e in ((.05, .08), (.30, .35), (.70, .72))])
        self.assertEqual(len(f.rules["connectors"].for_feature(i)), 5)


class BuildingsTest(Base):
    def test_polygon_across_the_edge_is_whole(self):
        r, _ = self.reader({"buildings.pmtiles": archive(buildings())})
        f, src = r.query("buildings", CELL)
        self.assertEqual(list(f.ids), ["bld-0", "bld-1"])
        i = f.index("bld-1")
        parts = f.parts(i)
        self.assertEqual((len(parts), len(parts[0])), (1, 1))
        ring = parts[0][0]
        np.testing.assert_array_equal(ring[0], ring[-1])
        lo, hi = ring.min(0), ring.max(0)
        want = np.array([lonlat(*p) for p in BUILDING])
        np.testing.assert_allclose(lo, want.min(0), atol=1e-9)
        np.testing.assert_allclose(hi, want.max(0), atol=1e-9)
        x, y = ring[:, 0], ring[:, 1]
        self.assertGreater(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]), 0)      # counter-clockwise (the parquet cells')
        a = f.attrs
        self.assertEqual((a["height"][i], a["num_floors"][i], a["roof_shape"][i], a["name"][i]),
                         (np.float32(12.5), np.float32(3), "gabled", "Pier"))
        self.assertEqual(a["height_src"][i], "USGS")
        self.assertTrue(np.isnan(a["min_height"][i]))
        # a hole stays a hole, clockwise
        k = f.index("bld-0")
        outer, hole = f.parts(k)[0]
        self.assertLess(np.sum(hole[:-1, 0] * hole[1:, 1] - hole[1:, 0] * hole[:-1, 1]), 0)
        self.assertEqual(src[("Microsoft", "ODbL")], 1)


class BaseThemeTest(Base):
    def test_clipped_layer_at_z13(self):
        t13 = (13, CELL.x >> 1, CELL.y >> 1)
        # a lake over the cell's tile quarter edge (the cell is the z13 tile's quarter at qx, qy)
        qx, qy = CELL.x & 1, CELL.y & 1
        x0, y0 = qx * 2048, qy * 2048
        lake = [(x0 + 1000, y0 + 1000), (x0 + 3000, y0 + 1000), (x0 + 3000, y0 + 1500), (x0 + 1000, y0 + 1500)]
        t = mvt({"water": [(3, [lake], {"id": "lake", "subtype": "lake", "class": "lake", "is_salt": False,
                                        "source_tags": {"natural": "water"},
                                        "sources": [{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}]})]})
        with mock.patch.object(pmtiles, "LAYERS", pmtiles.LAYERS + pmtiles.BASE_LAYERS):
            r, _ = self.reader({"base.pmtiles": archive({t13: t}, maxz=13)})
            f, _ = r.query("water", CELL)
        self.assertEqual(list(f.ids), ["lake"])
        self.assertTrue(f.clipped[0])
        ring = f.parts(0)[0][0]
        self.assertLessEqual(ring[:, 0].max(), CELL.bounds()[2] + 1e-12)       # cut at the cell's east edge
        self.assertEqual(f.attrs["tags"][0], '{"natural":"water"}')


class MirrorsTest(Base):
    def test_fastest_first_and_failover(self):
        files = {"transportation.pmtiles": archive(transportation())}
        r, f = self.reader(files)
        r.query("roads", CELL)
        hosts = [h for h, _, _ in f.seen]
        self.assertEqual(set(hosts[:3]), set(pmtiles.HOSTS))      # each measured once, at once
        self.assertTrue(all(h == "tiles.overturemaps.org" for h in hosts[3:]))     # then the fastest only
        with open(os.path.join(self.tmp, "raw", "overture-pmtiles", "mirrors.json")) as fh:
            self.assertEqual(json.load(fh)["order"][0], 0)
        # the CDN down: the next mirror answers, a fresh cell's tiles still come
        shutil.rmtree(os.path.join(self.tmp, "raw", "overture-pmtiles", pmtiles.overture.RELEASE))
        f.down.add("tiles.overturemaps.org")
        r2 = pmtiles.Reader(os.path.join(self.tmp, "raw"), f)
        feats, _ = r2.query("roads", CELL)
        self.assertIn("seg-a", list(feats.ids))

    def test_a_changed_archive_drops_its_directories(self):
        files = {"transportation.pmtiles": archive(transportation())}
        r, f = self.reader(files)
        r.query("connectors", CELL)
        f.etag = '"changed"'
        with self.assertRaises(pmtiles.Changed):
            r.query("roads", mapcells.CellKey(*EAST))
        self.assertFalse(os.path.exists(os.path.join(r.archive("transportation").dir, "head.json")))


class MapDataTest(Base):
    def md(self, fetcher):
        md = mapdata.MapData(cache_dir=self.tmp, fetcher=fetcher)
        self.addCleanup(md.close)
        md._osm_cell = lambda key, kind: ({}, [])
        md.osmtags = mock.Mock(highway_geometry=lambda *a, **k: {})
        return md

    def test_own_folder_key_and_fallback(self):
        self.enterContext(mock.patch.object(mapdata, "MAP_SOURCE", "pmtiles"))
        far = (14, CELL.x + 3, CELL.y)
        f = FakeFetcher({"transportation.pmtiles": archive({**transportation(), far: mvt({"segment": []})})})
        md = self.md(f)
        self.assertTrue(md.versions()["mapsrc"].startswith("pmtiles"))
        p = md.path(CELL, "roads")
        self.assertIn(f"overture-pmt{pmtiles.FORMAT}", p)
        self.assertNotIn(f"overture-pmt{pmtiles.FORMAT}", md.path(CELL, "roads", parquet=True))
        self.assertNotIn("overture-pmt", md.path(CELL, "water"))       # (base: parquet)
        cd = md.cell(CELL, ("roads",))
        self.assertTrue(os.path.exists(p))
        self.assertIn("seg-a", list(cd.layers["roads"].ids))
        self.assertEqual(cd.provenance[0]["format"], "pmtiles")
        # the tiles fail: the parquet path's layer, stored with the parquet cells, a revisit fault
        f.down.update(pmtiles.HOSTS)
        key = mapcells.CellKey(*far)
        import overture
        before = revisit.faults()
        with mock.patch.object(md.overture, "query", return_value=(overture.empty("roads"), {})) as q:
            md.cell(key, ("roads",))
        self.assertEqual(q.call_count, 1)
        self.assertGreater(revisit.faults(), before)
        self.assertFalse(os.path.exists(md.path(key, "roads")))
        self.assertTrue(os.path.exists(md.path(key, "roads", parquet=True)))

    def test_parquet_source_keeps_the_old_keys(self):
        with mock.patch.object(mapdata, "MAP_SOURCE", "parquet"):
            md = self.md(FakeFetcher({}))
        self.assertNotIn("mapsrc", md.versions())
        self.assertNotIn("overture-pmt", md.path(CELL, "roads"))


if __name__ == "__main__":
    unittest.main()
