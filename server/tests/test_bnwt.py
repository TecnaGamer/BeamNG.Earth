"""bnwt.py / earthfast bnwt.rs / tools/build_bnwt.py / bnwtunit.py: our prebaked z14 input tiles. The codec gives a
cell's layers back bit for bit (ids, geometry with off-grid doubles, every column's dtype and bytes, rule tables, the
sources' counts); a cell made of its own edges only (open sea) is the same bytes everywhere, kept once and decoded with
each cell's edges; builds are deterministic, resumable, shardable and split into parts; the reader answers from
file:// and from an HTTP server with range reads (and a pinned region download), and every case it cannot answer (no
archive, another release / format / content / source, a cell outside the coverage, a damaged tile) raises Unavailable,
which mapdata turns into the source's own path. The block path's rows-to-cells kernel follows the parquet bbox test."""
import zlib
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
SEA = (CX + 1, CY + 1)          # (fake_layer makes this cell, and the one north of it, open sea)


def uuid_of(i):
    return f"{i:08x}-0000-4000-8000-{i * 7919:012x}"


def sea_layer(layer, key):
    """An open-sea cell as the parquet path gives it: one ocean piece cut to the cell (its edges only)."""
    lon0, lat0, lon1, lat1 = key.bounds()
    ring = np.array([[lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0], [lon1, lat0]])
    f = mg.build(layer, mg.text_column([uuid_of(424242)]), [(mg.POLYGON, [[ring]])])
    f.clipped = np.array([True])
    f.attrs["cx"] = np.array([-40.0])
    f.attrs["cy"] = np.array([30.5])
    f.attrs["subtype"] = mg.text_column(["ocean"])
    f.attrs["level"] = mg.float_column([None])
    return f, {("OpenStreetMap", "ODbL-1.0"): 1}


def fake_layer(layer, key, n=None):
    """(Features, sources) shaped like a source's for a layer: polygons with a clip's off-grid edge vertex, lines,
    points; text with unicode, empty and repeated values; NaN floats; a JSON tags column."""
    if n is None and (key.x, key.y) in (SEA, (SEA[0], SEA[1] - 1)):
        return sea_layer(layer, key) if layer == "water" else (overture.empty(layer), {})
    rng = np.random.default_rng(zlib.crc32(f"{layer}/{key.x}/{key.y}".encode()))   # (str hash() is salted per process)
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
            tile, independent = bnwt.encode_tile(key, layers)
            self.assertFalse(independent)
            self.assertEqual(fast._ef.bnwt_tile_info(tile)[0], bnwt.FORMAT)
            for ly, got in layers.items():
                self.assertEqual(bnwt.differences(got, bnwt.decode_layer(tile, ly, key)), [], (n, ly))

    def test_open_sea_is_the_same_bytes_in_every_cell(self):
        a, b = mapcells.CellKey(14, *SEA), mapcells.CellKey(14, SEA[0] + 7, SEA[1] - 3)
        ta, ia = bnwt.encode_tile(a, {"water": sea_layer("water", a)})
        tb, ib = bnwt.encode_tile(b, {"water": sea_layer("water", b)})
        self.assertTrue(ia and ib)
        self.assertEqual(ta, tb)
        for key in (a, b):                      # (one tile, each cell's own edges back)
            self.assertEqual(bnwt.differences(sea_layer("water", key), bnwt.decode_layer(ta, "water", key)), [])

    def test_zero_rows_keep_their_dtypes(self):
        key = mapcells.CellKey(14, CX, CY)
        f, _ = fake_layer("water", key, 3)
        keep = np.zeros(3, bool)                 # (every row clipped away, as overture.Reader._run can leave it)
        g = mg.build("water", f.ids[keep], [])
        g.clipped = f.clipped[keep]
        g.attrs = {k: v[keep] for k, v in f.attrs.items()}
        tile, _ = bnwt.encode_tile(key, {"water": (g, {("OpenStreetMap", "ODbL"): 3})})
        back = bnwt.decode_layer(tile, "water", key)
        self.assertEqual(back[0].ids.dtype, np.dtype("<U36"))
        self.assertEqual(bnwt.differences((g, {("OpenStreetMap", "ODbL"): 3}), back), [])

    def test_missing_section_and_damage(self):
        key = mapcells.CellKey(14, CX, CY)
        tile, _ = bnwt.encode_tile(key, {"water": fake_layer("water", key, 3)})
        self.assertIsNone(bnwt.decode_layer(tile, "land_use", key))
        with self.assertRaises(ValueError):
            bnwt.decode_layer(tile[:len(tile) // 2], "water", key)
        with self.assertRaises(ValueError):
            bnwt.decode_layer(b"BNWT" + (99).to_bytes(2, "little") + tile[6:], "water", key)     # another format
        with self.assertRaises(ValueError):
            bnwt.decode_layer(b"junk" * 10, "water", key)

    def test_hilbert_runs(self):
        for zr, x, y in ((10, 541, 362), (12, 2166, 1448), (14, CX, CY)):
            a, b = fast._ef.bnwt_square_range(zr, x, y, 14)
            sh = 14 - zr
            ids = sorted(fast._ef.pmt_tile_id(14, (x << sh) + i, (y << sh) + j) for i in range(1 << sh)
                         for j in range(1 << sh))
            self.assertEqual((ids[0], ids[-1] + 1, len(ids)), (a, b, b - a))
        for x, y in ((0, 0), (CX, CY), (16383, 1)):
            self.assertEqual(bnwt._zxy(fast._ef.pmt_tile_id(14, x, y)), (14, x, y))
        self.assertEqual(bnwt._squares(4, 4, 7, 7), [(12, 1, 1)])
        self.assertEqual(len(bnwt._squares(3, 4, 7, 7)), 5)

    def test_assign_follows_the_bbox_test(self):
        rng = np.random.default_rng(3)
        lon = [mapcells.CellKey(14, CX + i, CY).bounds()[0] for i in range(4)] + [mapcells.CellKey(14, CX + 3, CY).bounds()[2]]
        lat = [mapcells.CellKey(14, CX, CY + j).bounds()[3] for j in range(4)] + [mapcells.CellKey(14, CX, CY + 3).bounds()[1]]
        edges = np.array(lon + lat)
        n = 400
        x0 = rng.choice(lon, n) + rng.choice([-1e-12, 0.0, 1e-12, -1e-3, 1e-3], n)     # (on, by and off the edges)
        y0 = rng.choice(lat, n) + rng.choice([-1e-12, 0.0, 1e-12, -1e-3, 1e-3], n)
        x1, y1 = x0 + rng.choice([0.0, 0.01, 0.05], n), y0 + rng.choice([0.0, 0.01, 0.05], n)
        for f32 in (False, True):
            offs, rows = fast._ef.bnwt_assign(x0, x1, y0, y1, lon, lat, f32)
            q = (lambda v: np.float32(v).astype(np.float64)) if f32 else (lambda v: np.float64(v))
            for ix in range(4):
                for iy in range(4):
                    w, e, nn, s = q(lon[ix]), q(lon[ix + 1]), q(lat[iy]), q(lat[iy + 1])
                    want = np.flatnonzero((q(x0) <= e) & (q(x1) >= w) & (q(y0) <= nn) & (q(y1) >= s))
                    i = ix * 4 + iy
                    self.assertEqual(list(rows[offs[i]:offs[i + 1]]), list(want))
        self.assertTrue(len(edges))

    def test_blanked_places_record(self):
        key = mapcells.CellKey(14, CX, CY)
        f, s = fake_layer("places", key, 3)
        f2, _ = bnwt.blanked("places", "places", (f, s))
        self.assertEqual(f2.attrs["src_record"].tolist(), ["", "", ""])
        self.assertEqual(list(f2.attrs), list(fake_layer("places", key, 3)[0].attrs))


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


class FakeCells:
    """build_bnwt.cell_layers for the tests: fake_layer per (layer, cell), counting the cells."""

    def __init__(self):
        self.calls = 0

    def __call__(self, archive, unit, cells, raw_dir, release, check=0):
        self.calls += len(cells)
        pmt = bnwt.ARCHIVES[archive]["source"] != "parquet"
        return {(x, y): {ly: bnwt.empty(archive, ly) if pmt and (x, y) == SEA else fake_layer(ly, mapcells.CellKey(14, x, y))
                         for ly in bnwt.ARCHIVES[archive]["layers"]} for _t, x, y in cells}


@unittest.skipUnless(HAVE, "native module older than bnwt.NATIVE")
class Archives(unittest.TestCase):
    BBOX = "10.4151,46.5136,10.4809,46.5588"         # (3 x 3 cells round CX, CY)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bnwt_test_")
        self.fake = FakeCells()
        self._orig = build_bnwt.cell_layers
        build_bnwt.cell_layers = self.fake

    def tearDown(self):
        build_bnwt.cell_layers = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name, *extra, archive="base"):
        out = os.path.join(self.tmp, name)
        build_bnwt.main(["--archive", archive, "--bbox", self.BBOX, "--out", out, "--jobs", "1", "--cache-dir",
                         self.tmp, *extra])
        d = Path(out, f"bnwt{bnwt.FORMAT}", overture.RELEASE)
        return out, d

    @staticmethod
    def blob(d):
        return b"".join(p.read_bytes() for p in sorted(d.glob("*.pmtiles"))) + \
            b"".join(p.read_bytes() for p in sorted(d.glob("*.index.json")))

    def reader(self, base, release=overture.RELEASE):
        return bnwt.Reader(os.path.join(self.tmp, "cache" + str(id(base)), "mapdata", "raw"), netfetch.Fetcher(), release,
                           base)

    def test_deterministic_resumable_dedup(self):
        _, a = self.build("a")
        _, b = self.build("b")
        self.assertEqual(self.blob(a), self.blob(b))
        idx = sorted(Path(self.tmp, "b", ".work", overture.RELEASE, "base", "parts").glob("*.idx"))
        calls = self.fake.calls
        self.build("b")                         # (all done: nothing made again)
        self.assertEqual(self.fake.calls, calls)
        idx[0].unlink()
        self.build("b")
        self.assertGreater(self.fake.calls, calls)
        self.assertEqual(self.blob(a), self.blob(b))
        import json
        man = json.loads(Path(b, "base.c1.manifest.json").read_text())
        self.assertEqual(man["release"], overture.RELEASE)
        self.assertEqual(man["licence"]["licence"], "ODbL-1.0")
        self.assertLess(man["contents"], man["tiles"])          # (the two open-sea cells: one tile kept)
        self.assertTrue(Path(b, "ATTRIBUTION-overture-odbl.txt").read_text().startswith("BeamNG.Earth"))

    def test_shards_and_parts(self):
        _, a = self.build("a")
        out = os.path.join(self.tmp, "s")
        common = ["--archive", "base", "--bbox", self.BBOX, "--out", out, "--jobs", "1", "--cache-dir", self.tmp,
                  "--unit-zoom", "13"]
        with self.assertRaises(SystemExit):     # (nothing built yet)
            build_bnwt.main(common + ["--assemble"])
        for i in range(3):
            build_bnwt.main(common + ["--shard", f"{i}/3"])
        build_bnwt.main(common + ["--assemble", "--part-mb", "0.003"])     # (parts of ~3 KB: several)
        d = Path(out, f"bnwt{bnwt.FORMAT}", overture.RELEASE)
        self.assertGreater(len(list(d.glob("*.pmtiles"))), 2)
        self.check_reads(self.reader("file://" + out))

    def test_shard_parts_assembled_from_records(self):
        """Each shard (its own --work, as separate CI jobs) writes its parts and a record; the index comes from the
        records alone; the shards are contiguous runs of the units (an empty shard: no parts, still a record)."""
        import json
        out = os.path.join(self.tmp, "sp")
        common = ["--archive", "base", "--bbox", self.BBOX, "--out", out, "--jobs", "1", "--cache-dir", self.tmp,
                  "--unit-zoom", "13", "--part-mb", "0.003"]
        sn = 5                                  # (4 zoom-13 units: shard 0 gets none)
        work = lambda i: ["--work", os.path.join(self.tmp, f"w{i}")]  # noqa: E731
        for i in range(sn - 1):
            build_bnwt.main(common + ["--shard", f"{i}/{sn}", "--shard-parts"] + work(i))
        with self.assertRaises(SystemExit):     # (shard 4 not done)
            build_bnwt.main(common + ["--assemble", "--from-shards", str(sn)] + work("a"))
        build_bnwt.main(common + ["--shard", f"{sn - 1}/{sn}", "--shard-parts"] + work(sn - 1))
        calls = self.fake.calls
        build_bnwt.main(common + ["--shard", f"1/{sn}", "--shard-parts"] + work("z"))
        self.assertEqual(self.fake.calls, calls)        # (a shard with a record: done)
        for i in range(sn):                             # (no unit parts needed to assemble)
            shutil.rmtree(os.path.join(self.tmp, f"w{i}"))
        build_bnwt.main(common + ["--assemble", "--from-shards", str(sn)] + work("a"))
        d = Path(out, f"bnwt{bnwt.FORMAT}", overture.RELEASE)
        recs = [json.loads(Path(d, "_build", "base.c1", str(sn), f"{i}.json").read_text()) for i in range(sn)]
        self.assertEqual([r["units"] for r in recs], [0, 1, 1, 1, 1])
        self.assertEqual(recs[0]["parts"], [])
        man = json.loads(Path(d, "base.c1.manifest.json").read_text())
        self.assertEqual(man["shards"], sn)
        self.assertEqual([p["file"] for p in man["parts"]], [p["file"] for r in recs for p in r["parts"]])
        self.assertGreaterEqual(len(man["parts"]), 4)          # (a part or more per shard with tiles)
        self.assertTrue(all(Path(d, p["file"]).exists() for p in man["parts"]))
        self.check_reads(self.reader("file://" + out))
        with self.assertRaises(SystemExit):     # (the records of another build)
            build_bnwt.main(common + ["--level", "3", "--assemble", "--from-shards", str(sn)] + work("b"))

    def check_reads(self, rd):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                key = mapcells.CellKey(14, CX + dx, CY + dy)
                for ly in bnwt.ARCHIVES["base"]["layers"]:
                    self.assertEqual(bnwt.differences(fake_layer(ly, key), rd.query(ly, key)), [], (key, ly))

    def test_file_reader_and_fallbacks(self):
        out, _ = self.build("a")
        rd = self.reader("file://" + out)
        self.check_reads(rd)
        self.check_reads(self.reader(out))      # (a plain folder too)
        key = mapcells.CellKey(14, CX, CY)
        with self.assertRaises(bnwt.Unavailable):            # outside the coverage
            rd.query("water", mapcells.CellKey(14, CX + 5, CY))
        with self.assertRaises(bnwt.Unavailable):            # a layer no archive holds
            rd.query("divisions", key)
        with self.assertRaises(bnwt.Unavailable):            # another source than the archive's
            rd.query("water", key, source=bnwt.PMT)
        with self.assertRaises(bnwt.Unavailable):            # no archive of this theme there
            rd.query("roads", key, source=bnwt.PMT)
        with self.assertRaises(bnwt.Unavailable):            # another release: the archive is not read
            self.reader("file://" + out, "2099-01-01.0").query("water", key)
        with self.assertRaises(bnwt.Unavailable):            # no archive there
            self.reader("file://" + self.tmp + "/none").query("water", key)
        if len(fake_layer("water", key)[0]) or len(fake_layer("land_use", key)[0]):
            p = rd.archive("base")._tile_path(14, CX, CY)
            Path(p).write_bytes(Path(p).read_bytes()[:20])
            rd._mem.clear()                     # (as a fresh process finds it)
            with self.assertRaises(bnwt.Unavailable):
                rd.query("water", key)
            self.assertFalse(os.path.exists(p))
            self.check_reads(rd)

    def test_content_mismatch(self):
        out, d = self.build("a")
        old = bnwt.CONTENT["base"]
        try:
            bnwt.CONTENT["base"] = old + 1      # (a newer reader: its own index name; the old index renamed to it)
            shutil.copy(d / f"base.c{old}.index.json", d / f"base.c{old + 1}.index.json")
            with self.assertRaises(bnwt.Unavailable):
                self.reader("file://" + out).query("water", mapcells.CellKey(14, CX, CY))
        finally:
            bnwt.CONTENT["base"] = old

    def test_pmtiles_source_archive_and_empty(self):
        out, d = self.build("p", archive="places")
        rd = self.reader("file://" + out)
        key = mapcells.CellKey(14, CX, CY)
        self.assertEqual(bnwt.differences(fake_layer("places", key), rd.query("places", key, source=bnwt.PMT)), [])
        with self.assertRaises(bnwt.Unavailable):
            rd.query("places", key, source="parquet")
        self.assertTrue(Path(d, "ATTRIBUTION-overture-places.txt").exists())
        # (a cell without a tile reads as Overture's tiles' own empty layer)
        self.assertEqual(bnwt.differences(bnwt.empty("places", "places"),
                                          rd.query("places", mapcells.CellKey(14, *SEA), source=bnwt.PMT)), [])

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

    def test_http_reader_region_pins(self):
        out, _ = self.build("a", "--part-mb", "0.004")
        RangeHandler.root = out
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            rd = self.reader(base)
            lon0, lat0, lon1, lat1 = (float(v) for v in self.BBOX.split(","))
            s = rd.download_region((lon0, lat0, lon1, lat1), kind="demand")
            self.assertEqual(s["fetched"], s["tiles"])
            self.assertLessEqual(s["requests"], 8)          # (a 3 x 3 block in a few parts: a few runs)
            again = rd.download_region((lon0, lat0, lon1, lat1), kind="demand")
            self.assertEqual((again["fetched"], again["requests"]), (0, 0))     # (resumed: all on disk)
            rd2 = self.reader(base + "/missing")           # (a 404: Unavailable, not asked again for a while)
            for _ in range(2):
                with self.assertRaises(bnwt.Unavailable):
                    rd2.query("water", mapcells.CellKey(14, CX, CY))
            # (connect and read timeouts apart: the transport takes (connect, read))
            st, _h, body = netfetch.HttpsTransport().request(base + "/missing", {}, (1.0, 5.0))
            self.assertEqual(st, 404)
            srv.shutdown()                                  # (the region on disk: no server needed)
            self.check_reads(rd)
            # pinned: its tiles and directories are kept by the cache cap, other cells' tiles are not
            cache_root = os.path.dirname(os.path.dirname(os.path.dirname(rd.root)))
            keys = [f"mapdata/raw/bnwt/{overture.RELEASE}/base/tiles/14/{CX}/{CY}.bnwt",
                    f"mapdata/raw/bnwt/{overture.RELEASE}/base/tiles/14/{CX + 9}/{CY}.bnwt",
                    f"mapdata/raw/bnwt/{overture.RELEASE}/base/index.json", "dem/x/1/2/3.png"]
            self.assertEqual(bnwt.pinned_keys(cache_root, keys), [True, False, True, False])
        finally:
            srv.server_close()


class ContentGuard(unittest.TestCase):
    """The archives hold what their source's code gave when they were built: a change of that code (the layers'
    columns, the query, the clipping, the WKB decode, the column builders; Overture's tiles' reader and its FORMAT; the
    block path) must bump that archive's bnwt.CONTENT (the old archives are then not read) or show that the output is
    unchanged; then the fingerprint here is updated."""
    FINGERPRINT = {("base", 1): "925b2e6e87dc4a6c", ("transportation", 1): "98802c4b9213d440",
                   ("buildings", 1): "0c37ee4249d499ca", ("places", 1): "af34ebc49196615b"}

    @staticmethod
    def fingerprint(name):
        import hashlib
        import inspect
        import bnwtunit
        import pmtiles
        spec = bnwt.ARCHIVES[name]
        parts = [repr(sorted((k, overture.LAYERS[k]) for k in spec["layers"])), repr(sorted(spec.items()))]
        if spec["source"] == "parquet":
            fns = (overture.Reader.query, overture.Reader._run, overture.Reader._plan, overture._number_rows,
                   overture.tolerant, overture.empty, overture.intersects, mg.build, mg.clip_polygon_parts,
                   mg.clip_ring, mg._clip_edge, mg.parse_wkb, mg._geom, mg._coords, mg.text_column, mg.float_column,
                   mg.bool_column, bnwtunit.unit_rows, bnwtunit.cell_layer, bnwtunit.assign)
        else:
            parts.append(repr((pmtiles.FORMAT, pmtiles.NATIVE, pmtiles.CAP, pmtiles.POINT_LAYERS, pmtiles.MAX_ROUNDS,
                               pmtiles.THEME_ZOOM, pmtiles.LAYERS)))
            fns = (pmtiles.Reader.query, pmtiles.Reader._features, pmtiles.Reader.tiles_for, mg.text_column,
                   bnwt.blanked)
        parts += [inspect.getsource(f) for f in fns]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def test_sources_unchanged(self):
        for name in bnwt.ARCHIVES:
            self.assertEqual(self.fingerprint(name), self.FINGERPRINT.get((name, bnwt.CONTENT[name])),
                             f"{name}: the code its archives hold the output of changed: bump bnwt.CONTENT['{name}'] "
                             "(and rebuild them), or show the cells are unchanged, then put the new fingerprint here")


if __name__ == "__main__":
    unittest.main()
