"""bnwt.py / earthfast bnwt.rs / tools/build_bnwt.py: our prebaked z14 input tiles. The codec gives a cell's layers
back bit for bit (ids, geometry with off-grid doubles, every column's dtype and bytes, rule tables, the sources'
counts), builds are deterministic and resumable, the reader answers from file:// and from an HTTP server with range
reads (and a region download), and every case it cannot answer (no archive, another release / format / content,
a cell outside the coverage, a damaged tile) raises Unavailable, which mapdata turns into the parquet path."""
import http.server
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bnwt  # noqa: E402
import fast  # noqa: E402
import mapcells  # noqa: E402
import mapgeom as mg  # noqa: E402
import netfetch  # noqa: E402
import overture  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= bnwt.NATIVE
_spec = importlib.util.spec_from_file_location(
    "build_bnwt", str(Path(__file__).resolve().parents[2] / "tools" / "build_bnwt.py"))
build_bnwt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_bnwt)

CX, CY = 8667, 5793             # (a z14 cell in the Alps)


def uuid_of(i):
    return f"{i:08x}-0000-4000-8000-{i * 7919:012x}"


def fake_layer(layer, key, n=None):
    """(Features, sources) shaped like overture.Reader.query's for a base layer: polygons with a clip's off-grid edge
    vertex, lines, points; text with unicode, empty and repeated values; NaN floats; a JSON tags column."""
    rng = np.random.default_rng(hash((layer, key.x, key.y)) % (1 << 32))
    lon0, lat0, lon1, lat1 = key.bounds()
    n = int(rng.integers(0, 6)) if n is None else n
    ids = mg.text_column(sorted(uuid_of(int(v)) for v in rng.choice(10 ** 6, n, replace=False)))
    geoms = []
    for i in range(n):
        k = (mg.POLYGON, mg.LINE, mg.POINT)[i % 3]
        pts = np.round(np.column_stack([rng.uniform(lon0, lon1, 5), rng.uniform(lat0, lat1, 5)]) * 1e7) / 1e7
        if k == mg.POLYGON:
            pts[0, 0] = lon0                    # (a cut point on the cell edge: lon0 is no 1e-7 grid value)
            pts[1, 1] = lat0 + 1e-9 * np.pi
            ring = np.vstack([pts, pts[:1]])
            geoms.append((k, [[ring, ring[::-1].copy()]]))
        elif k == mg.LINE:
            geoms.append((k, [[pts]]))
        else:
            geoms.append((k, [[pts[:1]]]))
    f = mg.build(layer, ids, geoms)
    f.clipped = np.array([i % 3 == 0 for i in range(n)], bool)
    f.attrs["cx"] = rng.uniform(lon0, lon1, n)
    f.attrs["cy"] = rng.uniform(lat0, lat1, n)
    f.attrs["subtype"] = mg.text_column(["river" if i % 2 else "" for i in range(n)])
    f.attrs["name"] = mg.text_column(["Großer Bach ✓" if i % 3 == 1 else None for i in range(n)])
    f.attrs["src_record"] = mg.text_column([f"w{1000 + i}@{i + 1}" for i in range(n)])
    f.attrs["tags"] = mg.text_column(['{"natural":"water","name:de":"Bach \\"x\\""}' if i % 2 else "" for i in range(n)])
    f.attrs["level"] = mg.float_column([None if i % 2 else 1.5 for i in range(n)])
    f.attrs["is_salt"] = mg.bool_column([i % 2 for i in range(n)])
    sources = {("OpenStreetMap", "ODbL"): n + 1, ("", ""): 1} if n or rng.integers(0, 2) else {}
    if not n and not sources:
        return overture.empty(layer), {}       # (what the parquet path gives a cell without rows)
    return f, sources


def road_like(key):
    """A layer with rule tables (text and float values), as the roads would be."""
    f, s = fake_layer("roads", key, 4)
    f.rules["surface"] = mg.rule_table([(0, "paved", None), (2, "gravel", [0.25, 0.5])], "text")
    f.rules["width"] = mg.rule_table([(1, 7.5, [0.0, 1.0 / 3.0])], "float")
    f.rules["connectors"] = mg.rule_table([(0, uuid_of(5), [0.0, 0.0]), (3, uuid_of(9), [1.0, 1.0])], "text")
    return f, s


@unittest.skipUnless(HAVE, "native module older than bnwt.NATIVE")
class Codec(unittest.TestCase):
    def test_round_trip_is_bit_identical(self):
        key = mapcells.CellKey(14, CX, CY)
        for n in (0, 1, 7, 40):
            layers = {"water": fake_layer("water", key, n), "land_use": fake_layer("land_use", key, n + 1),
                      "roads": road_like(key)}
            tile = bnwt.encode_tile(key, layers)
            self.assertEqual(fast._ef.bnwt_tile_info(tile)[:4], (bnwt.FORMAT, 14, CX, CY))
            for ly, got in layers.items():
                back = bnwt.decode_layer(tile, ly)
                if back is None:
                    self.assertFalse(len(got[0]) or got[1])
                    continue
                self.assertEqual(bnwt.differences(got, back), [], (n, ly))

    def test_zero_rows_keep_their_dtypes(self):
        key = mapcells.CellKey(14, CX, CY)
        f, _ = fake_layer("water", key, 3)
        keep = np.zeros(3, bool)                 # (every row clipped away, as overture.Reader._run can leave it)
        g = mg.build("water", f.ids[keep], [])
        g.clipped = f.clipped[keep]
        g.attrs = {k: v[keep] for k, v in f.attrs.items()}
        tile = bnwt.encode_tile(key, {"water": (g, {("OpenStreetMap", "ODbL"): 3})})
        back = bnwt.decode_layer(tile, "water")
        self.assertEqual(back[0].ids.dtype, np.dtype("<U36"))
        self.assertEqual(bnwt.differences((g, {("OpenStreetMap", "ODbL"): 3}), back), [])

    def test_missing_section_and_damage(self):
        key = mapcells.CellKey(14, CX, CY)
        tile = bnwt.encode_tile(key, {"water": fake_layer("water", key, 3)})
        self.assertIsNone(bnwt.decode_layer(tile, "land_use"))
        with self.assertRaises(ValueError):
            bnwt.decode_layer(tile[:len(tile) // 2], "water")
        with self.assertRaises(ValueError):
            bnwt.decode_layer(b"BNWT" + (99).to_bytes(2, "little") + tile[6:], "water")     # another format
        with self.assertRaises(ValueError):
            bnwt.decode_layer(b"junk" * 10, "water")

    def test_hilbert_runs(self):
        for zr, x, y in ((10, 541, 362), (12, 2166, 1448), (14, CX, CY)):
            a, b = fast._ef.bnwt_square_range(zr, x, y, 14)
            sh = 14 - zr
            ids = sorted(fast._ef.pmt_tile_id(14, (x << sh) + i, (y << sh) + j) for i in range(1 << sh)
                         for j in range(1 << sh))
            self.assertEqual((ids[0], ids[-1] + 1, len(ids)), (a, b, b - a))
            self.assertEqual(bnwt._zxy(ids[3 % len(ids)])[0], 14)
        for x, y in ((0, 0), (CX, CY), (16383, 1)):
            self.assertEqual(bnwt._zxy(fast._ef.pmt_tile_id(14, x, y)), (14, x, y))
        self.assertEqual(bnwt._squares(4, 4, 7, 7), [(12, 1, 1)])
        self.assertEqual(len(bnwt._squares(3, 4, 7, 7)), 5)


class FakeReader:
    """overture.Reader's query for the builder: fake_layer per (layer, cell), counting calls."""

    def __init__(self):
        self.calls = 0

    def query(self, layer, key, kind="demand"):
        self.calls += 1
        return fake_layer(layer, key)


class RangeHandler(http.server.BaseHTTPRequestHandler):
    root = None
    log_message = lambda *a: None  # noqa: E731

    def do_GET(self):
        path = os.path.join(self.root, self.path.lstrip("/"))
        if not os.path.isfile(path):
            self.send_error(404)
            return
        data = Path(path).read_bytes()
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.split("=", 1)[1].split("-")
            a, b = int(a), min(int(b), len(data) - 1)
            body = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(HAVE, "native module older than bnwt.NATIVE")
class Archives(unittest.TestCase):
    BBOX = "10.4151,46.5136,10.4809,46.5588"         # (3 x 3 cells round CX, CY)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bnwt_test_")
        self.fake = FakeReader()
        build_bnwt._W["rd"] = self.fake

    def tearDown(self):
        build_bnwt._W.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name, *extra):
        out = os.path.join(self.tmp, name)
        build_bnwt.main(["--bbox", self.BBOX, "--out", out, "--jobs", "1", "--cache-dir", self.tmp, *extra])
        return out, Path(out, f"bnwt{bnwt.FORMAT}", overture.RELEASE, f"base.c{bnwt.CONTENT}.pmtiles")

    def reader(self, base, release=overture.RELEASE):
        return bnwt.Reader(os.path.join(self.tmp, "cache" + str(id(base)), "raw"), netfetch.Fetcher(), release, base)

    def test_deterministic_and_resumable(self):
        _, a = self.build("a")
        _, b = self.build("b")
        self.assertEqual(a.read_bytes(), b.read_bytes())
        parts = Path(self.tmp, "b", ".work", overture.RELEASE, "base", "parts")
        idx = sorted(parts.glob("*.idx"))
        self.assertTrue(idx)
        calls = self.fake.calls
        self.build("b")                         # (all done: nothing queried again)
        self.assertEqual(self.fake.calls, calls)
        idx[0].unlink()
        self.build("b")                         # (the unit again, the same bytes)
        self.assertGreater(self.fake.calls, calls)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        man = Path(b.parent, f"base.c{bnwt.CONTENT}.manifest.json").read_text()
        self.assertIn('"release": "%s"' % overture.RELEASE, man)
        self.assertIn("ODbL-1.0", man)
        self.assertTrue(Path(b.parent, "ATTRIBUTION.txt").read_text().startswith("BeamNG.Earth"))

    def test_shards_assemble_to_the_same_bytes(self):
        _, a = self.build("a", "--unit-zoom", "13")      # (3 x 3 cells: 4 units of 2 x 2)
        out = os.path.join(self.tmp, "s")
        common = ["--bbox", self.BBOX, "--out", out, "--jobs", "1", "--cache-dir", self.tmp, "--unit-zoom", "13"]
        with self.assertRaises(SystemExit):     # (nothing built yet)
            build_bnwt.main(common + ["--assemble"])
        for i in range(3):
            build_bnwt.main(common + ["--shard", f"{i}/3"])
        build_bnwt.main(common + ["--assemble"])
        self.assertEqual(a.read_bytes(), Path(out, *a.parts[-3:]).read_bytes())

    def check_reads(self, rd):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                key = mapcells.CellKey(14, CX + dx, CY + dy)
                for ly in bnwt.LAYERS:
                    want = fake_layer(ly, key)
                    self.assertEqual(bnwt.differences(want, rd.query(ly, key)), [], (key, ly))

    def test_file_reader_and_fallbacks(self):
        out, _ = self.build("a")
        rd = self.reader("file://" + out)
        self.check_reads(rd)
        self.check_reads(self.reader(out))      # (a plain folder too)
        with self.assertRaises(bnwt.Unavailable):            # outside the coverage
            rd.query("water", mapcells.CellKey(14, CX + 5, CY))
        with self.assertRaises(bnwt.Unavailable):            # a layer no archive holds
            rd.query("roads", mapcells.CellKey(14, CX, CY))
        with self.assertRaises(bnwt.Unavailable):            # another release: the archive is not read
            self.reader("file://" + out, "2099-01-01.0").query("water", mapcells.CellKey(14, CX, CY))
        with self.assertRaises(bnwt.Unavailable):            # no archive there
            self.reader("file://" + self.tmp + "/none").query("water", mapcells.CellKey(14, CX, CY))
        # a damaged tile on disk: Unavailable, the file dropped, read again next time
        key = mapcells.CellKey(14, CX, CY)
        if len(fake_layer("water", key)[0]) or len(fake_layer("land_use", key)[0]):
            p = rd.archive("base")._tile_path(14, CX, CY)
            Path(p).write_bytes(Path(p).read_bytes()[:20])
            rd._mem.clear()                     # (as a fresh process finds it)
            with self.assertRaises(bnwt.Unavailable):
                rd.query("water", key)
            self.assertFalse(os.path.exists(p))
            self.check_reads(rd)

    def test_format_mismatch(self):
        out, _ = self.build("a")
        old = bnwt.CONTENT
        try:
            bnwt.CONTENT = old + 1              # (a newer reader: its own file name; the old archive renamed to it)
            src = Path(out, f"bnwt{bnwt.FORMAT}", overture.RELEASE, f"base.c{old}.pmtiles")
            shutil.copy(src, src.with_name(f"base.c{old + 1}.pmtiles"))
            with self.assertRaises(bnwt.Unavailable):
                self.reader("file://" + out).query("water", mapcells.CellKey(14, CX, CY))
        finally:
            bnwt.CONTENT = old

    def test_mapdata_falls_back(self):
        import mapdata

        class Stub:
            def __init__(self, err):
                self.err, self.stats = err, {"fallbacks": 0}

            def query(self, *a):
                raise self.err
        for err in (bnwt.Unavailable("x"), ValueError("damaged"), netfetch.HostUnavailable("down", "u", "h")):
            st = type("M", (), {})()
            st.bnwt = Stub(err)
            self.assertEqual(mapdata.MapData._bnwt_query(st, "water", mapcells.CellKey(14, CX, CY), "demand"),
                             (None, None))
            self.assertEqual(st.bnwt.stats["fallbacks"], 1)
        st.bnwt = Stub(netfetch.Deferred("busy", "u", "h"))
        with self.assertRaises(netfetch.Deferred):          # (a prefetch: as the parquet path's)
            mapdata.MapData._bnwt_query(st, "water", mapcells.CellKey(14, CX, CY), "prefetch")

    def test_http_reader_and_region(self):
        out, _ = self.build("a")
        RangeHandler.root = out
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            rd = self.reader(base)
            lon0, lat0, lon1, lat1 = (float(v) for v in self.BBOX.split(","))
            s = rd.download_region((lon0, lat0, lon1, lat1), kind="demand")
            self.assertEqual(s["fetched"], s["tiles"])
            self.assertLessEqual(s["requests"], 4)          # (a 3 x 3 block: a few contiguous runs)
            again = rd.download_region((lon0, lat0, lon1, lat1), kind="demand")
            self.assertEqual((again["fetched"], again["requests"]), (0, 0))     # (resumed: all on disk)
            rd2 = self.reader(base + "/missing")           # (a 404: Unavailable, not asked again for a while)
            for _ in range(2):
                with self.assertRaises(bnwt.Unavailable):
                    rd2.query("water", mapcells.CellKey(14, CX, CY))
            srv.shutdown()                                  # (the region on disk: no server needed)
            self.check_reads(rd)
        finally:
            srv.server_close()


class ContentGuard(unittest.TestCase):
    """The archives hold what the parquet path's code gave when they were built: a change of that code (the layers'
    columns, the query, the clipping, the WKB decode, the column builders) must bump bnwt.CONTENT (the old archives are
    then not read) or show that the output is unchanged; then the fingerprint here is updated."""
    FINGERPRINT = {1: "882e6e6b7ff6e02b"}

    def test_parquet_path_unchanged(self):
        import hashlib
        import inspect
        parts = [repr(sorted((k, overture.LAYERS[k]) for k in bnwt.LAYERS))]
        for f in (overture.Reader.query, overture.Reader._run, overture.Reader._plan, overture._number_rows,
                  overture.tolerant, overture.empty, mg.build, mg.clip_polygon_parts, mg.clip_ring, mg._clip_edge,
                  mg.parse_wkb, mg._geom, mg._coords, mg.text_column, mg.float_column, mg.bool_column):
            parts.append(inspect.getsource(f))
        got = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
        self.assertEqual(got, self.FINGERPRINT.get(bnwt.CONTENT),
                         "the parquet path's code for the BNWT layers changed: bump bnwt.CONTENT (and rebuild the "
                         "archives), or show the cells are unchanged, then put the new fingerprint here")


if __name__ == "__main__":
    unittest.main()
