"""The horizon ring's tall buildings (bldhz.py) from a local index (tools/build_tall_index.py's shards)."""
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import bldhz  # noqa: E402
import geotiles  # noqa: E402
import mapgeom  # noqa: E402

LAT, LON = 40.7484, -73.9857        # (a tall building's place: any)


def _ring(lat, lon, half_m=20.0):
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * np.cos(np.radians(lat)))
    r = np.array([[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat], [lon - dlon, lat + dlat]])
    return np.round(r * 1e7).astype(np.int32)


def _shard(n_b=2, parts=True):
    import build_tall_index as T
    b_ids = [f"b{i:03d}" for i in range(n_b)]
    rings = [_ring(LAT + 0.001 * i, LON) for i in range(n_b)]
    a = {"b_id": mapgeom.text_column(b_ids),
         "b_ring_off": np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64),
         "b_ll": np.concatenate(rings)}
    for c in T.B_SPEC["text"]:
        a["b_t_" + c] = mapgeom.text_column(["office" if c == "class" else None] * n_b)
    for c in T.B_SPEC["float"]:
        a["b_f_" + c] = mapgeom.float_column([200.0 if c == "height" else None] * n_b)
    for c in T.B_SPEC["bool"]:
        a["b_b_" + c] = mapgeom.bool_column([c == "has_parts" and parts] * n_b)
    if parts:
        pr = [_ring(LAT, LON, 15.0), _ring(LAT, LON, 8.0)]
        a["p_id"] = mapgeom.text_column(["p0", "p1"])
        a["p_ring_off"] = np.r_[0, np.cumsum([len(r) for r in pr])].astype(np.int64)
        a["p_ll"] = np.concatenate(pr)
        for c in T.P_SPEC["text"]:
            a["p_t_" + c] = mapgeom.text_column(["b000" if c == "building_id" else None] * 2)
        for c in T.P_SPEC["float"]:
            a["p_f_" + c] = mapgeom.float_column([{"height": h, "min_height": m}.get(c) for h, m in
                                                  ((200.0, None), (260.0, 200.0))])
    return a


def _index(d, arrays, sha=None):
    import build_tall_index as T
    gx, gy = geotiles.tile_of(LAT, LON, bldhz.Z)
    os.makedirs(os.path.join(d, "12"), exist_ok=True)
    p = os.path.join(d, "12", f"{gx}_{gy}.npz")
    T.write_npz(p, arrays)
    with open(p, "rb") as f:
        real = hashlib.sha1(f.read()).hexdigest()
    key = geotiles.key(gx, gy, bldhz.Z)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"format": bldhz.FORMAT, "release": "test", "z": bldhz.Z, "min_height": 60.0,
                   "tiles": {key: {"n": 2, "sha1": sha or real}}}, f)
    return gx, gy, real


class BldhzTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        bldhz._local.clear()

    def _with(self, arrays, sha=None):
        gx, gy, real = _index(self.d.name, arrays, sha)
        p = mock.patch.object(bldhz, "DIR", self.d.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(bldhz._local.clear)
        return gx, gy, real

    def test_pieces_per_skyline_square(self):
        gx, gy, _ = self._with(_shard())
        tile = geotiles.geobox(gx, gy, bldhz.Z)
        sq = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)))
        sx, sy = geotiles.tile_of(LAT, LON, geotiles.SKY_Z)
        self.assertIn(geotiles.key(sx, sy, geotiles.SKY_Z), sq)
        n = sum(len(v) for v in sq.values())
        self.assertGreaterEqual(n, 2)
        tops = [float(s.min_h or 0) + float(s.height or 0) for v in sq.values() for s, _a, _b in v]
        self.assertGreaterEqual(max(tops), 250.0, "the building drawn from its parts (the 260 m one)")
        again = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(sorted(sq), sorted(again))
        for k in sq:
            self.assertTrue(all(np.array_equal(a[0].ring, b[0].ring) for a, b in zip(sq[k], again[k])))

    def test_shard_checked(self):
        gx, gy, _ = self._with(_shard(), sha="0" * 40)
        with self.assertRaises(ValueError):
            bldhz.source().load(gx, gy)

    def test_malformed_shard(self):
        a = _shard(parts=False)
        a["b_ring_off"] = a["b_ring_off"].copy()
        a["b_ring_off"][1] = 99
        gx, gy, _ = self._with(a)
        with self.assertRaises(ValueError):
            bldhz.source().load(gx, gy)

    def test_cache_key_follows_the_shard(self):
        import blockworker
        gx, gy, real = self._with(_shard())
        with mock.patch.object(blockworker, "_s", mock.Mock(md=None)):
            k1 = blockworker._bldhz_key(gx, gy)
            self.assertIn(real, k1)
            bldhz._local.clear()
            _index(self.d.name, _shard(n_b=3))
            k2 = blockworker._bldhz_key(gx, gy)
        self.assertNotEqual(k1, k2)

    def test_far_geotile_pieces(self):
        """blockworker's bldhz kind: a piece a skyline square, each a shape at the tile's corner in its frame."""
        import blockworker
        gx, gy, _ = self._with(_shard())
        tile = geotiles.geobox(gx, gy, bldhz.Z)
        dem = mock.Mock(sample=lambda la, lo, z: np.zeros(np.shape(la)))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(blockworker, "_s", mock.Mock(md=None, dem=dem)), \
                mock.patch.object(blockworker, "ground", lambda _a, la, lo, h: h):
            data, shapes, n = blockworker._far_geo_build("bldhz", tile, 15, d, "/x", None)
            self.assertGreaterEqual(n, 2)
            sx, sy = geotiles.tile_of(LAT, LON, geotiles.SKY_Z)
            sq = geotiles.key(sx, sy, geotiles.SKY_Z)
            self.assertIn(sq, data["parts"])
            self.assertEqual(data["parts"][sq]["pos"], [tile.box[0], tile.box[1], 0.0])
            self.assertTrue(all(os.path.exists(os.path.join(d, f)) for f in shapes.values()))
            self.assertEqual(shapes[sq], blockworker.far_geo_piece(tile.key, sq, "bldhz"))

    def test_reach_by_setting(self):
        """near none, normal 20 km, far 35 km, max to the horizon (or the fixed view distance)."""
        self.assertEqual([bldhz.reach(s) for s in (0.6, 1.0, 1.5, 2.0)], [0.0, 20000.0, 35000.0, bldhz.HORIZON_M])
        self.assertEqual(bldhz.reach(2.0, 50000.0), 50000.0)
        self.assertEqual(bldhz.reach(1.0, 50000.0), 20000.0)

    def test_from_the_data_site(self):
        """The published layout (build_tall_index.write_pages: PMTiles parts in the build's folder, a manifest) read
        through a fake host as osmyears is: pieces as from the local index; the frozen build in the cache key."""
        import build_tall_index as T
        from tests.test_osm_building_years import FakeFetcher
        from types import SimpleNamespace
        gx, gy = geotiles.tile_of(LAT, LON, bldhz.Z)
        out = os.path.join(self.d.name, "pages")
        m = T.write_pages(out, {(gx, gy): T.npz_bytes(_shard())}, "test", 60.0)
        base = "https://tall.test/tallbld"
        files = {f"{base}/manifest.json": Path(out, "manifest.json").read_bytes()}
        for p in m["parts"]:
            files[f"{base}/{p['archive']}"] = Path(out, p["archive"]).read_bytes()
        md = SimpleNamespace(fetcher=FakeFetcher(files), cache_dir=os.path.join(self.d.name, "cache"))
        with mock.patch.object(bldhz, "DIR", None), mock.patch.object(bldhz, "BASE", base), \
                mock.patch.dict(bldhz._pages, clear=True):
            src = bldhz.source(md)
            self.assertIsInstance(src, bldhz.PagesIndex)
            src.version()                   # (pinned: MapData.warm_manifests)
            self.assertTrue(bldhz.on(md))
            tile = geotiles.geobox(gx, gy, bldhz.Z)
            sq = bldhz.squares(tile, lambda la, lo: np.zeros(np.shape(la)), md)
            self.assertTrue(sq and sum(len(v) for v in sq.values()) >= 2)
            self.assertEqual(src.sig(gx, gy)[:2], ("pages", m["built"]))
            empty = geotiles.geobox(gx + 5, gy, bldhz.Z)
            self.assertEqual(bldhz.squares(empty, lambda la, lo: np.zeros(np.shape(la)), md), {})

    def test_builder_columns_are_the_map_cells(self):
        """The standalone builder's column copies are overture.LAYERS' (the server reads its tiles as map cells), its
        tile maths geotiles', its WKB ring mapgeom's."""
        import build_tall_index as T
        import overture
        for mine, theirs in ((T.B_SPEC, overture.LAYERS["buildings"]), (T.P_SPEC, overture.LAYERS["building_parts"])):
            for k in ("cols", "text", "float", "bool"):
                self.assertEqual(mine.get(k, ()), theirs.get(k, ()), k)
        lat = np.array([LAT, -33.86, 51.5, 85.0, -85.0])
        lon = np.array([LON, 151.2, -0.12, 179.99, -179.99])
        x, y = T.tile_of(lat, lon)
        gx, gy = geotiles.tile_of(lat, lon, bldhz.Z)
        self.assertEqual((x.tolist(), y.tolist()), (np.asarray(gx).tolist(), np.asarray(gy).tolist()))
        import struct
        ring = [(1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 2.0)]
        poly = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", 4) + b"".join(struct.pack("<dd", *p) for p in ring)
        multi = struct.pack("<BI", 1, 6) + struct.pack("<I", 1) + poly
        for b in (poly, multi):
            self.assertEqual(T.first_ring_wkb(b).tolist(), mapgeom.parse_wkb(b)[1][0][0].tolist())

    def test_off_without_dir(self):
        with mock.patch.object(bldhz, "DIR", None):
            self.assertFalse(bldhz.on())
        importlib.reload(bldhz)


def _zip(members, method=None):
    """npz-like bytes of {name: raw member bytes}."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method or zipfile.ZIP_DEFLATED) as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def _npy_header(dtype, shape):
    import io
    from numpy.lib import format as npf
    b = io.BytesIO()
    npf.write_array_header_1_0(b, {"descr": npf.dtype_to_descr(np.dtype(dtype)), "fortran_order": False,
                                   "shape": shape})
    return npf.magic(1, 0) + b.getvalue()


class BuilderWantTest(unittest.TestCase):
    def test_empty_batches(self):
        """A region with no directly tall building, or a parts file adding no new tall building: an empty batch
        is skipped, not handed to DuckDB's executemany (which refuses it and aborted the whole build)."""
        import duckdb
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
        import build_tall_index as T
        con = duckdb.connect()
        con.execute("CREATE TEMP TABLE want (id VARCHAR)")
        T._want(con, [])
        T._want(con, (b for b in ()))
        T._want(con, ["b2", "b1"])
        self.assertEqual(sorted(r[0] for r in con.execute("SELECT id FROM want").fetchall()), ["b1", "b2"])


class BuilderPartsAcrossFilesTest(unittest.TestCase):
    def test_lower_part_in_an_earlier_file(self):
        """A building tall only through a part in Overture file B keeps its lower part from file A, read before B (one
        pass over the files missed it: the building was not wanted yet when A was read)."""
        import duckdb
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
        import build_tall_index as T

        def cols(spec, vals):
            out = {"id": vals["id"], "version": 1, "wkb": b"x", "cx": 0.5, "cy": 0.5}
            for k, typ in (("text", "VARCHAR"), ("float", "DOUBLE"), ("bool", "BOOLEAN")):
                for c in spec.get(k, ()):
                    out[c] = (vals.get(c), typ)
            return out

        class Meta:
            def __init__(self, files): self.files = files
            def select(self, box, hmin=None, fmin=None): return [(f, 0) for f in self.files]

        class Fake:
            requests, bytes = 0, 0
            def __init__(self, spec, data): self.spec, self.data = spec, data
            def fill(self, pairs, again=False): pass
            def load(self, con, table, proj, pairs):
                rows = [r for f, _ in pairs for r in self.data.get(f, ())]
                con.execute(f"DROP TABLE IF EXISTS {table}")
                if not rows:
                    return False
                sel = []
                for r in rows:
                    c = cols(self.spec, r)
                    parts = [f"'{c['id']}' AS id", "1 AS version", "'x'::BLOB AS wkb", "0.5::DOUBLE AS cx", "0.5::DOUBLE AS cy",
                             "{'xmin': 0.4::DOUBLE, 'xmax': 0.6::DOUBLE, 'ymin': 0.4::DOUBLE, 'ymax': 0.6::DOUBLE} AS bbox"]
                    for k, v in c.items():
                        if isinstance(v, tuple):
                            val = "NULL" if v[0] is None else (f"'{v[0]}'" if v[1] == "VARCHAR" else repr(v[0]))
                            parts.append(f"CAST({val} AS {v[1]}) AS {k}")
                    sel.append("SELECT " + ", ".join(parts))
                con.execute(f"CREATE TEMP TABLE {table} AS " + " UNION ALL ".join(sel))
                return True

        con = duckdb.connect()
        sb = Fake(T.B_SPEC, {"b": [{"id": "B1", "height": 20.0}]})
        sp = Fake(T.P_SPEC, {"a": [{"id": "P1", "building_id": "B1", "height": 30.0}],
                             "z": [{"id": "P2", "building_id": "B1", "height": 200.0}]})
        rows, names, prows, pnames = T.build_region(con, (Meta(["b"]), Meta(["a", "z"])), (sb, sp), (0, 0, 1, 1),
                                                    60.0, lambda *_: None)
        self.assertEqual([r[0] for r in rows], ["B1"])
        self.assertEqual(sorted(r[0] for r in prows), ["P1", "P2"])


class DecodeBoundsTest(unittest.TestCase):
    """bldhz.decode refuses a hostile tile before allocating for it (Codex review of the ring: a 199-byte npz whose
    member's header declared 1e9 float64 made np.load ask for 8 GB)."""

    def peak(self, b):
        import tracemalloc
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                bldhz.decode(b)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_the_real_tile_decodes(self):
        import build_tall_index as T
        a = _shard()
        got = bldhz.decode(T.npz_bytes(a))
        self.assertEqual(sorted(got), sorted(a))
        for k in a:
            self.assertTrue(np.array_equal(got[k], a[k]) or (a[k].dtype.kind == "f" and
                                                           np.array_equal(got[k], a[k], equal_nan=True)), k)
        b, p = bldhz.features(got)
        self.assertEqual(len(b), 2)

    def test_header_only_bombs(self):
        for dt, shape in (("<f8", (1_000_000_000,)), ("<f4", (1_000_000_000,)), ("<f4", (bldhz.MAX_ROWS,)),
                          ("<i4", (3, 2)), ("<i4", (10 ** 9, 2))):
            name = "b_ll.npy" if dt == "<i4" else "b_f_height.npy"
            b = _zip({name: _npy_header(dt, shape)})
            self.assertLess(len(b), 400)
            self.assertLess(self.peak(b), 4 << 20, (dt, shape))

    def test_compressed_expansion_bombs(self):
        import zipfile
        big = _npy_header("<f4", (25_000_000,)) + bytes(100_000_000)        # (100 MB of zeros: ~100 KB deflated)
        b = _zip({"b_f_height.npy": big})
        self.assertLess(len(b), bldhz.MAX_TILE_BYTES)
        self.assertLess(self.peak(b), 8 << 20, "refused by its declared size, nothing inflated")
        part = _npy_header("<f4", (5_000_000,)) + bytes(20_000_000)         # (each within, together past the budget)
        b = _zip({f"b_f_{c}.npy": part for c in ("height", "min_height", "num_floors", "level")})
        self.assertLess(self.peak(b), 48 << 20)
        # (a member longer than its zip entry says: read no further than declared)
        raw = _npy_header("<f4", (4,)) + bytes(16)
        b = bytearray(_zip({"b_f_height.npy": raw}, zipfile.ZIP_STORED))
        i = b.rfind(b"PK\x01\x02")
        size = len(raw)
        b[i + 24:i + 28] = (size - 8).to_bytes(4, "little")                 # (the central entry's declared size)
        with self.assertRaises(Exception):
            bldhz.decode(bytes(b))

    def test_wrong_members(self):
        good = _npy_header("<f4", (2,)) + bytes(8)
        for members in ({"evil.npy": good}, {"b_f_height.npy": good, "../b_f_height.npy": good},
                        {"b_f_height.npy": _npy_header("<f8", (2,)) + bytes(16)},       # (not the column's dtype)
                        {"b_id.npy": _npy_header("|O", (2,))},                           # (objects)
                        {"b_t_name.npy": _npy_header(f"<U{bldhz.MAX_TEXT + 1}", (1,))},
                        {"b_f_height.npy": good[:-1]},                                   # (a short payload)
                        {"b_ll.npy": _npy_header("<i4", (2, 3)) + bytes(24)}):           # (not lon / lat pairs)
            with self.assertRaises(ValueError, msg=str(list(members))):
                bldhz.decode(_zip(members))
        with self.assertRaises(ValueError):
            bldhz.decode(b"not a zip")


class NearDropsTheRingTest(unittest.TestCase):
    """Building distance Near while horizon tiles show: every one dropped, and a build in flight lands unwanted (Codex
    review: the poses stopped at reach 0, so the drops in them never came)."""

    def test_near_drops_every_tile_and_ignores_a_late_build(self):
        import asyncio
        import concurrent.futures
        import earthd
        import vegetation

        class W:
            def write(self, data):
                pass

            def close(self):
                pass

            def get_extra_info(self, name):
                return ("127.0.0.1", 0)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=1024,
                          near_visual="detail")
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)

        async def go():
            c = earthd.Connection(s, None, W())
            sent = []
            c.send = sent.append
            tr = c.bldhz = vegetation.Tracker(20000.0, 24000.0, 1, "bldhz", geo=True, z=bldhz.Z)
            tr.aid = 1
            tr.sent = {(1206, 1539), (1205, 1539)}
            late = concurrent.futures.Future()
            late.set_running_or_notify_cancel()         # (running: its result still comes after the drop)
            e = tr.epoch
            tr.building.add((1207, 1539))
            tr.futs[(1207, 1539)] = late
            late.add_done_callback(lambda f: tr._built_geo(c, (1207, 1539), f, e))
            c.dispatch({"type": "graphics", "lodScale": 1, "treeScale": 1, "buildingScale": 0.6})
            self.assertEqual(tr.send_m, 0.0)
            drops = sorted(m["key"] for m in sent if m.get("type") == "bldhz_drop")
            self.assertEqual(drops, ["g12_1205_1539", "g12_1206_1539"])
            self.assertEqual((tr.sent, tr.building, tr.futs), (set(), set(), {}))
            n = len(sent)
            late.set_result(("/x/bh_g12_1207_1539.json", 3, (40.7, -74.0), 0.0, {}))
            self.assertEqual(len(sent), n, "the build started before Near sends nothing")
            self.assertEqual(tr.sent, set())
            c.dispatch({"type": "graphics", "lodScale": 1, "treeScale": 1, "buildingScale": 1.0})
            self.assertEqual(tr.send_m, 20000.0, "Normal again: the ring's reach back")
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
