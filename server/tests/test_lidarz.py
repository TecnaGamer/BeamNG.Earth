"""lidarz.py / earthfast lidarz.rs / tools/build_lidarz.py: the lidar road heights as a signed CDN product. A cell
record gives a roadz.Lines (and its stretches' answers) back bit for bit; a block is taken only with a trusted key's
signature, its own name, block, product and version key; a player's server that reads a cell from the CDN measures,
matches and plans exactly what it would have from the point clouds (a fake 3DEP dataset here: the same deck heights
and bridge profiles, byte for byte, and not one point-cloud request), and reads the point clouds as before on every
miss: off, no manifest, a network error, a foreign signature, a damaged block, another map's inputs, another DEM."""
import hashlib
import http.server
import json
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import decisions  # noqa: E402
import fast  # noqa: E402
import lidarz  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
import roadz  # noqa: E402
from test_approaches import overpass, flat_elev, FLAT  # noqa: E402
from test_roads import FRAME  # noqa: E402

ef = fast._ef
HAVE = fast.AVAILABLE and getattr(ef, "VERSION", 0) >= lidarz.NATIVE
SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))
DATASET = "USGS_TEST_2020"


class FakeDem:
    def __init__(self, z):
        self.z = z

    def sample(self, la, lo, zoom):
        return np.full(np.shape(la), self.z, np.float64)


class OffDem(FakeDem):
    """FLAT within 4 m of the overpass's line (y = 0, FRAME), FLAT + rise beyond: a DEM revision away from the road
    centre line, where the lidar's ground points (the datum) are sampled but no point of the map's lines is."""

    def __init__(self, rise):
        super().__init__(FLAT)
        self.rise = rise

    def sample(self, la, lo, zoom):
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        y = np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[1], np.float64)
        return np.where(np.abs(y) > 4.0, FLAT + self.rise, FLAT).reshape(np.shape(la))


class MD:
    """The overpass's roads (test_approaches) on a flat DEM at dem (or a DEM object)."""

    def __init__(self, feats=None, dem=FLAT):
        self.feats = overpass() if feats is None else feats
        self.dem = dem if hasattr(dem, "sample") else FakeDem(dem)

    def cell(self, key, layers=None, kind=None):
        class C:
            pass
        c = C()
        c.layers = {"roads": self.feats}
        return c


# --- a fake 3DEP dataset (EPT: ept.json, a hierarchy, plain LAS nodes) round the overpass's deck --------------------
W = 6400.0          # the dataset's cube (m, EPSG:3857): depth-6 nodes 100 m across
DEPTH = 6


def _merc(x, y):
    la, lo, _ = FRAME.enu_to_geodetic(np.asarray(x, np.float64), np.asarray(y, np.float64), 0.0)
    return roadz.merc_xy(np.asarray(la), np.asarray(lo))


def _las(xyz, cls, off):
    hdr = bytearray(227)
    hdr[0:4] = b"LASF"
    hdr[24], hdr[25] = 1, 2
    struct.pack_into("<H", hdr, 94, 227)
    struct.pack_into("<I", hdr, 96, 227)
    hdr[104] = 0
    struct.pack_into("<H", hdr, 105, 20)
    struct.pack_into("<I", hdr, 107, len(xyz))
    struct.pack_into("<3d", hdr, 131, 0.01, 0.01, 0.01)
    struct.pack_into("<3d", hdr, 155, off[0], off[1], 0.0)
    out = bytes(hdr)
    for (x, y, z), c in zip(xyz, cls):
        rec = bytearray(20)
        struct.pack_into("<3i", rec, 0, int(round((x - off[0]) * 100)), int(round((y - off[1]) * 100)),
                         int(round(z * 100)))
        rec[15] = int(c)
        out += bytes(rec)
    return out


def ept_files(deck_rise=6.0, seed=3):
    """{relative path: bytes} of the fake dataset: a deck x -35..35 along y = 0 (FRAME) deck_rise over the DEM, the
    road going on from its ends on embankments falling back to it over 80 m (elevated for the first ~40 m: a ground
    stretch on a structure, roads.lidar_gap_candidates), and ground (class 2) beside it at the DEM."""
    rng = np.random.default_rng(seed)
    pts, cls = [], []
    x = rng.uniform(-35, 35, 3000)
    pts.append(np.c_[x, rng.uniform(-4, 4, 3000), FLAT + deck_rise + 0.002 * x])
    cls.append(np.full(3000, 1))
    for sgn in (-1, 1):                     # (the approaches' embankments: the lidar's, not the DTM's)
        t = rng.uniform(0, 100, 1500)
        pts.append(np.c_[sgn * (35 + t), rng.uniform(-4, 4, 1500),
                         FLAT + (deck_rise + 0.002 * sgn * 35) * np.clip(1 - t / 80, 0, None)])
        cls.append(np.full(1500, 1))
    g = np.c_[rng.uniform(-200, 200, 4000), rng.choice([-1.0, 1.0], 4000) * rng.uniform(5, 12, 4000)]
    pts.append(np.c_[g, np.full(len(g), FLAT)])
    cls.append(np.full(len(g), 2))
    p = np.vstack(pts)
    mx, my = _merc(p[:, 0], p[:, 1])
    mxyz = np.c_[mx, my, p[:, 2]]
    c = np.concatenate(cls)
    x0, y0 = float(np.floor(mx.mean() - W / 2)), float(np.floor(my.mean() - W / 2))     # (the points in its middle)
    meta = {"bounds": [x0, y0, -100.0, x0 + W, y0 + W, W - 100.0], "srs": {"horizontal": "3857"}}
    w = W / 2 ** DEPTH
    ix = ((mxyz[:, 0] - x0) // w).astype(int)
    iy = ((mxyz[:, 1] - y0) // w).astype(int)
    files = {f"{DATASET}/ept.json": json.dumps(meta).encode()}
    hier = {}
    for nx, ny in sorted(set(zip(ix.tolist(), iy.tolist()))):
        sel = (ix == nx) & (iy == ny)
        key = f"{DEPTH}-{nx}-{ny}-0"
        hier[key] = 100000              # (a dense octree depth: depth_at stops here)
        for d in range(DEPTH):
            hier.setdefault(f"{d}-{nx >> (DEPTH - d)}-{ny >> (DEPTH - d)}-0", 1)
        files[f"{DATASET}/ept-data/{key}.laz"] = _las(mxyz[sel], c[sel], (x0 + nx * w, y0 + ny * w))
    files[f"{DATASET}/ept-hierarchy/0-0-0-0.json"] = json.dumps(hier).encode()
    return files


KEY = mapcells.cell_of(float(FRAME.lat0), float(FRAME.lon0))


def provider(cache, md=None, files=None, asked=None):
    """A RoadZ on cache whose point-cloud reads come from files (asked: the URLs read); None files: any read fails."""
    rz = roadz.RoadZ(cache, object(), md or MD())

    def fetch(url, headers=None, lane=None):
        rel = url[len(roadz.EPT_BASE):]
        if asked is not None:
            asked.append(rel)
        if files is None or rel not in files:
            import netfetch
            raise netfetch.FetchError(f"no {rel}")
        return files[rel]
    rz._fetch = fetch
    return rz


class Handler(http.server.BaseHTTPRequestHandler):
    root = None
    hits = []
    log_message = lambda *a: None  # noqa: E731

    def do_GET(self):
        Handler.hits.append(self.path)
        path = os.path.join(self.root, self.path.lstrip("/"))
        if not os.path.isfile(path):
            self.send_error(404)
            return
        body = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def same(a, b):
    import build_lidarz
    return build_lidarz.same_lines(a, b)


@unittest.skipUnless(HAVE, f"earthfast {lidarz.NATIVE} not built")
class RecordTest(unittest.TestCase):
    def lines(self):
        a = (np.c_[np.linspace(-76.6, -76.59, 7), np.linspace(39.28, 39.29, 7)], np.r_[1.0, np.nan, 3, 4, 5, 6, -0.0],
             1, "", "")
        b = (np.c_[np.linspace(-76.6, -76.6001, 3), np.full(3, 39.281)], np.array([7.25, 7.5, 7.75]), 0, "", "")
        ln = roadz.Lines.build([a, b], corrected=True)
        ln.gaps = [("w12@0", (-76.6, 39.28), (-76.5999, 39.2801))]
        return ln

    def test_round_trip_bit_for_bit(self):
        ln = self.lines()
        calls = [2, 0, 1]
        pts = np.array([[39.28, -76.6], [39.2801, -76.5999], [39.29, -76.59]])
        rec = lidarz.encode(ln, b"\x05" * 32, calls, pts)
        back, dem, calls2, pts2 = lidarz.decode(rec)
        self.assertEqual(calls2, calls)
        self.assertEqual(pts2.tobytes(), pts.tobytes(), "where the builder read the DEM, bit for bit")
        self.assertEqual(dem, b"\x05" * 32)
        self.assertTrue(same(ln, back))
        self.assertEqual(back.ref.dtype, ln.ref.dtype)
        self.assertEqual(lidarz.encode(back, dem, calls2, pts2), rec, "deterministic")
        with tempfile.TemporaryDirectory() as d:
            ln.save(os.path.join(d, "a.npz"))
            back.save(os.path.join(d, "b.npz"))
            a, b = roadz.Lines.load(os.path.join(d, "a.npz")), roadz.Lines.load(os.path.join(d, "b.npz"))
            a.gaps, b.gaps = ln.gaps, back.gaps
            self.assertTrue(same(a, b), "the same cell file on disk")
        empty = roadz.Lines.build([], corrected=True)
        self.assertTrue(same(empty, lidarz.decode(lidarz.encode(empty, bytes(32), [], np.zeros((0, 2))))[0]))

    def test_damaged_record_refused(self):
        rec = lidarz.encode(self.lines(), bytes(32), [1], np.array([[39.28, -76.6]]))
        for bad in (rec[:-1], rec + b"\0", b"BLZC" + bytes(60), rec[:4] + b"\x09" + rec[5:]):
            with self.assertRaises(ValueError):
                lidarz.decode(bad)

    def test_key_follows_the_rules_not_the_release(self):
        doc = lidarz.lkey_doc()
        self.assertEqual(doc["roadz"], roadz.VERSION)
        self.assertNotIn("overture", json.dumps(doc))
        k0 = lidarz.lkey()
        with mock.patch.object(roadz, "LIDAR_STEP_M", roadz.LIDAR_STEP_M + 1):
            self.assertNotEqual(lidarz.lkey(), k0, "a measuring constant is in the key")
        with mock.patch.object(roadz, "VERSION", roadz.VERSION + 1):
            self.assertNotEqual(lidarz.lkey(), k0, "the local cells' version")
        with mock.patch.object(roads, "LIDAR_GAPS_ON", not roads.LIDAR_GAPS_ON):
            self.assertNotEqual(lidarz.lkey(), k0, "the ground stretches' switch")
        self.assertEqual(lidarz.lkey(), k0)


@unittest.skipUnless(HAVE, f"earthfast {lidarz.NATIVE} not built")
class ProductTest(unittest.TestCase):
    """The builder's path (RoadZ._lidar on the fake dataset, then encode and sign) and a player's server reading the
    cell from a local HTTP server instead of the point clouds."""

    @classmethod
    def setUpClass(cls):
        cls.files = ept_files()
        cls.tmp = tempfile.mkdtemp()
        cls.web = os.path.join(cls.tmp, "web")
        os.makedirs(cls.web)
        Handler.root = cls.web
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"BNGE_LIDARZ": "on", "BNGE_LIDARZ_URL": self.base,
                                                "BNGE_DECISIONS_PUBKEY": ef.dec_public_key(SEED).hex(),
                                                "BNGE_ROADZ_SESSION": f"t{id(self)}"})
        self.env.start()
        self.ds = mock.patch.object(roadz, "lidar_datasets", lambda lat, lon: [DATASET])
        self.ds.start()
        self.old = roadz.PROVIDER, roadz.LIDAR_FETCH_THREADS, roadz.LIDAR_GAPS_WAIT_S
        roadz.LIDAR_FETCH_THREADS = 2
        lidarz._STORES.clear()
        Handler.hits = []
        shutil.rmtree(os.path.join(self.web, f"lz{lidarz.FORMAT}"), ignore_errors=True)
        self.n = 0

    def tearDown(self):
        roadz.PROVIDER, roadz.LIDAR_FETCH_THREADS, roadz.LIDAR_GAPS_WAIT_S = self.old
        self.env.stop()
        self.ds.stop()
        lidarz._STORES.clear()

    def cache(self):
        self.n += 1
        d = os.path.join(self.tmp, f"c{id(self)}_{self.n}")
        os.makedirs(d)
        return d

    def measured(self, md=None, files=None):
        """The builder's: (Lines, its record, its item key) measured from the point clouds (lidarz off)."""
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            rz = provider(self.cache(), md, files or self.files)
            got, rec, ikey = lidarz.record_for(rz, KEY)
            self.assertTrue(same(got, rz._lidar(KEY)), "what this server measures")
        return got, rec, ikey

    def publish(self, items, seed=SEED, domain="lidarz", lk=None):
        """items {item key: record} into their block and a manifest under the web root (the builder's layout)."""
        vk, vkb = lk or lidarz.lkey()
        bx, by = decisions.block_of((KEY.z, KEY.x, KEY.y))
        body = ef.dec_pack_block(12, bx, by, vkb, [(k, v, False) for k, v in items.items()], seed, domain=domain)
        h = hashlib.sha256(body).hexdigest()[:16]
        root = os.path.join(self.web, f"lz{lidarz.FORMAT}", vk)
        decisions._write(os.path.join(root, "12", str(bx), f"{by}.{h}.bnlz"), body)
        man = {"format": lidarz.FORMAT, "vkey": vk, "doc": lidarz.lkey_doc(), "blocks": [[bx, by, h]]}
        decisions._write(os.path.join(root, "manifest.bnlm"),
                         ef.dec_make_manifest(json.dumps(man).encode(), seed, domain=domain))
        return body

    def test_measured_deck(self):
        got, _rec, _k = self.measured()
        decks = [i for i in range(len(got)) if got.level[i] > 0]
        self.assertTrue(decks, "the fixture's deck is measured")
        z = got.z[got.off[decks[0]]:got.off[decks[0] + 1]]
        self.assertAlmostEqual(float(np.nanmedian(z)), FLAT + 6.0, delta=0.3)

    def test_cell_from_the_cdn_is_the_measured_one(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        asked = []
        rz = provider(self.cache(), files=None, asked=asked)
        ln = rz.lines("lidar", KEY)
        self.assertEqual(asked, [], "no point cloud read")
        self.assertTrue(same(got, ln), "the builder's lines, bit for bit")
        on_disk = roadz.Lines.load(rz._path("lidar", KEY))
        with open(rz._path("lidar", KEY) + ".gaps.json") as f:
            on_disk.gaps = [(str(a), tuple(b), tuple(c)) for a, b, c in json.load(f)]
        self.assertTrue(same(got, on_disk), "kept on disk as a cell measured here")
        self.assertTrue(rz.lidar_on_disk(KEY))
        self.assertEqual(lidarz.store_for(os.path.dirname(os.path.dirname(rz.dir))).stats["hits"], 1)

    def test_plans_byte_identical(self):
        """The bridge's profile (roads.BridgeProfiles over roadz.observed) and the measured deck heights a plan takes:
        the same bytes from the CDN's record as from the point clouds (a deck 1.8 m over the DEM: one the profile
        takes, OBS_MAX_GRADE)."""
        files = ept_files(deck_rise=1.8)
        got, rec, ikey = self.measured(files=files)
        f = overpass()
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]

        def plan(rz):
            roadz.PROVIDER = rz
            bp = roads.BridgeProfiles(FRAME, lambda key: f, flat_elev)
            parts = bp.parts([f], (-100, -100, 100, 100))
            obs = rz.observed(FRAME, line, 4.0, 1)
            return [np.asarray(p[3], np.float64) for p in parts], obs

        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            asked_a = []
            za, oa = plan(provider(self.cache(), files=files, asked=asked_a))
        self.assertTrue(asked_a, "measured from the point clouds")
        self.publish({ikey: rec})
        asked_b = []
        zb, ob = plan(provider(self.cache(), files=None, asked=asked_b))
        self.assertEqual(asked_b, [], "planned without a point-cloud read")
        self.assertIsNotNone(oa)
        self.assertEqual(oa.tobytes(), ob.tobytes(), "the measured deck heights")
        self.assertEqual(len(za), len(zb))
        for a, b in zip(za, zb):
            self.assertEqual(a.tobytes(), b.tobytes(), "the bridge profile")
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            zn, on = plan(provider(self.cache(), files=None))
        self.assertIsNone(on, "(and without either the inference: the profile differs, so the check means something)")
        self.assertNotEqual([z.tobytes() for z in zn], [z.tobytes() for z in za])

    def fallback(self, expect_reads=True, md=None, stats=None):
        asked = []
        rz = provider(self.cache(), md, self.files, asked)
        ln = rz.lines("lidar", KEY)
        self.assertEqual(bool(asked), expect_reads)
        if stats is not None:
            stats.update(lidarz.store_for(os.path.dirname(os.path.dirname(rz.dir))).stats)
        return ln

    def test_no_manifest_reads_the_lidar(self):
        got, _rec, _k = self.measured()
        self.assertTrue(same(got, self.fallback()))

    def test_off_reads_the_lidar(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            self.assertTrue(same(got, self.fallback()))
        self.assertEqual(Handler.hits, [], "nothing asked of the CDN")

    def test_network_error_reads_the_lidar(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ_URL": "http://127.0.0.1:9"}):
            lidarz._STORES.clear()
            self.assertTrue(same(got, self.fallback()))

    def test_foreign_signature_refused(self):
        got, rec, ikey = self.measured()
        bad = rec[:-8] + bytes(8)       # (a forged record: only a trusted key's block would carry it)
        self.publish({ikey: bad}, seed=OTHER_SEED)
        self.assertTrue(same(got, self.fallback()))

    def test_damaged_block_refused(self):
        got, rec, ikey = self.measured()
        body = self.publish({ikey: rec})
        vk = lidarz.lkey()[0]
        bx, by = decisions.block_of((KEY.z, KEY.x, KEY.y))
        h = hashlib.sha256(body).hexdigest()[:16]
        p = os.path.join(self.web, f"lz{lidarz.FORMAT}", vk, "12", str(bx), f"{by}.{h}.bnlz")
        b = bytearray(body)
        b[-1] ^= 1
        Path(p).write_bytes(bytes(b))
        self.assertTrue(same(got, self.fallback()))

    def test_a_decision_block_is_not_a_lidar_block(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec}, domain="dec")
        self.assertTrue(same(got, self.fallback()))

    def test_another_lkey_misses(self):
        got, rec, ikey = self.measured()
        other = hashlib.sha256(b"another version").digest()
        self.publish({ikey: rec}, lk=(lidarz.lkey()[0], other))     # (signed for another key, served at ours)
        self.assertTrue(same(got, self.fallback()))

    def test_another_map_misses(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        import test_roads
        moved = test_roads.features([[(-400, 0.5), (400, 0.5)], [(-12, -500), (-12, 500)], [(12, 500), (12, -500)]],
                                    ["secondary", "motorway", "motorway"],
                                    flags=[(0, "is_bridge", (400 - 35) / 800, (400 + 35) / 800)],
                                    ids=["cross", "i20n", "i20s"])
        md = MD(moved)
        mine, _r, _k = self.measured(md)
        st = {}
        ln = self.fallback(md=md, stats=st)
        self.assertTrue(same(mine, ln), "measured along this map's own line")
        self.assertFalse(same(mine, got))
        self.assertEqual(st.get("stale"), 1, "the cell is built, for another map")

    def test_another_dem_misses(self):
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        md = MD(dem=FLAT + 0.5)
        mine, _r, _k = self.measured(md)
        st = {}
        ln = self.fallback(md=md, stats=st)
        self.assertTrue(same(mine, ln), "measured on this server's own DEM")
        self.assertFalse(same(mine, got), "(the DEM matters: the datum and the stretches move with it)")
        self.assertEqual(st.get("dem"), 1)

    def test_dem_change_off_the_centre_line_misses(self):
        """Codex review of 206afb6f [high]: a DEM revision only where the datum's ground points are (beside the road,
        not on its line) changes what this server would measure; the record must not stand in for it."""
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        md = MD(dem=OffDem(0.6))
        mine, _r, _k = self.measured(md)
        self.assertFalse(same(mine, got), "(the off-centre DEM moves the measurement)")
        st = {}
        ln = self.fallback(md=md, stats=st)
        self.assertTrue(same(mine, ln), "measured on this server's own DEM, not the builder's record")
        self.assertEqual(st.get("dem"), 1)

    def test_builder_reuses_a_record_only_on_the_same_dem(self):
        """The builder's work records (build_lidarz: a cell made before is not read again) follow the same identity: a
        DEM revision beside the road makes the cell be measured again."""
        _got, rec, _ikey = self.measured()
        self.assertTrue(lidarz.record_ok(provider(self.cache(), files=None), rec))
        self.assertFalse(lidarz.record_ok(provider(self.cache(), MD(dem=OffDem(0.6)), files=None), rec))
        _lines, _dem, _calls, samples = lidarz.decode(rec)
        y = FRAME.geodetic_to_enu(samples[:, 0], samples[:, 1], 0.0)[1]
        self.assertGreater(float(np.max(np.abs(y))), 4.0, "(the record holds the datum's ground points beside the road)")

    def persisted(self):
        """A cache whose cell came from the CDN (on disk now), and its cache folder."""
        got, rec, ikey = self.measured()
        self.publish({ikey: rec})
        cache = self.cache()
        ln = provider(cache, files=None).lines("lidar", KEY)
        self.assertTrue(same(got, ln))
        kept = roadz.Lines.load(self.cell_file(cache))
        self.assertEqual(kept.lidarz[2], rec, "the record kept inside the cell's own file")
        self.assertEqual(kept.lidarz[:2], (lidarz.lkey()[0], ikey))
        return got, cache

    def reread(self, cache, md=None, expect_reads=True):
        """The cell asked again by a new process (another RoadZ) on the same cache."""
        lidarz._STORES.clear()
        asked = []
        rz = provider(cache, md, self.files, asked)
        ln = rz.lines("lidar", KEY)
        self.assertEqual(bool(asked), expect_reads)
        return ln

    def test_a_kept_cdn_cell_is_kept_while_its_inputs_hold(self):
        got, cache = self.persisted()
        self.assertTrue(same(got, self.reread(cache, expect_reads=False)))

    def test_a_kept_cdn_cell_is_dropped_when_the_map_changes(self):
        """Codex review of 206afb6f [high]: the CDN's lines kept on disk were taken on the next visit whatever the map
        said by then; they are checked again against this server's inputs, and measured here when they differ."""
        _got, cache = self.persisted()
        md = MD(self.moved())
        mine, _r, _k = self.measured(md)
        self.assertTrue(same(mine, self.reread(cache, md)), "measured along the new map's line")

    def test_a_kept_cdn_cell_is_dropped_when_the_dem_changes(self):
        _got, cache = self.persisted()
        md = MD(dem=OffDem(0.6))
        mine, _r, _k = self.measured(md)
        self.assertTrue(same(mine, self.reread(cache, md)))

    def test_a_kept_cdn_cell_is_dropped_when_the_product_is_off(self):
        got, cache = self.persisted()
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            self.assertTrue(same(got, self.reread(cache)), "measured here (the same lines: the same inputs)")
        self.assertIsNone(getattr(roadz.Lines.load(self.cell_file(cache)), "lidarz", None),
                          "a cell measured here is no CDN cell any more")

    def cell_file(self, cache):
        return os.path.join(cache, "roadz", f"v{roadz.VERSION}", "lidar", str(KEY.z), str(KEY.x), f"{KEY.y}.npz")

    def evict_all_but_the_cell(self, cache):
        """What cache eviction may leave of a kept CDN cell: its lines and gaps files, every other file of it gone
        (cache.py evicts the roadz files one by one)."""
        d = os.path.join(cache, "roadz", f"v{roadz.VERSION}", "lidar", str(KEY.z), str(KEY.x))
        keep = {f"{KEY.y}.npz", f"{KEY.y}.npz.gaps.json"}
        gone = [n for n in os.listdir(d) if n.startswith(f"{KEY.y}.") and n not in keep]
        for n in gone:
            os.remove(os.path.join(d, n))
        self.assertTrue(all(os.path.exists(os.path.join(d, n)) for n in keep))
        return gone

    def test_a_kept_cdn_cell_without_its_other_files_is_still_checked(self):
        """Codex review of 866ebb97 [high]: the identity in a file of its own could be evicted alone, and the CDN's
        lines were then taken as measured here, unchecked, after the DEM changed."""
        _got, cache = self.persisted()
        self.evict_all_but_the_cell(cache)
        md = MD(dem=OffDem(0.6))
        mine, _r, _k = self.measured(md)
        self.assertTrue(same(mine, self.reread(cache, md)), "the DEM changed: measured here")

    def test_a_kept_cdn_cell_without_its_other_files_is_dropped_when_off(self):
        got, cache = self.persisted()
        self.evict_all_but_the_cell(cache)
        with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off"}):
            self.assertTrue(same(got, self.reread(cache)))      # (measured: the reads happen, reread checks)

    def test_a_kept_cdn_cell_without_its_other_files_is_kept_while_its_inputs_hold(self):
        got, cache = self.persisted()
        self.evict_all_but_the_cell(cache)
        self.assertTrue(same(got, self.reread(cache, expect_reads=False)))

    def test_a_slow_check_of_a_kept_cell_is_bounded_by_the_planning_wait(self):
        """Codex review of 1b5ba25e [medium]: a planning process checked a kept CDN cell (its DEM reads may download
        tiles, a dropped cell asks the CDN again) on its own thread, unbounded by lidarz.WAIT_S; it takes the rule
        within the wait instead, and the check goes on in the background."""
        import time as _t
        _got, cache = self.persisted()
        real = lidarz.kept_ok

        def slow(*a, **k):
            _t.sleep(1.5)
            return real(*a, **k)
        with mock.patch.dict(os.environ, {roadz.PREFETCH_ENV: "1", "BNGE_ROADZ_SESSION": "slowcheck"}), \
                mock.patch.object(roadz, "PREFETCH_ON", True), mock.patch.object(lidarz, "WAIT_S", 0.2), \
                mock.patch.object(lidarz, "kept_ok", slow):
            rz = provider(cache, files=None)
            t0 = _t.monotonic()
            got = rz.gap_answers([KEY])[KEY]
            dt = _t.monotonic() - t0
        self.assertLess(dt, 1.0, "within the planning wait")
        self.assertIsNone(got, "the rule meanwhile")

    def test_a_quick_check_of_a_kept_cell_answers_the_planning_process(self):
        _got, cache = self.persisted()
        with mock.patch.dict(os.environ, {roadz.PREFETCH_ENV: "1", "BNGE_ROADZ_SESSION": "quickcheck"}), \
                mock.patch.object(roadz, "PREFETCH_ON", True):
            self.assertIsNotNone(provider(cache, files=None).gap_answers([KEY])[KEY])

    def test_a_dropped_kept_cell_is_no_read_in_a_planning_process(self):
        """With the product off, a planning process of a server run that finds a kept CDN cell on disk drops it (its
        check fails) and takes the rule, asking the server's prefetch for it; it never reads point clouds itself."""
        _got, cache = self.persisted()
        with mock.patch.dict(os.environ, {roadz.PREFETCH_ENV: "1", "BNGE_ROADZ_SESSION": "dropped",
                                          "BNGE_LIDARZ": "off"}), mock.patch.object(roadz, "PREFETCH_ON", True):
            asked = []
            rz = provider(cache, files=self.files, asked=asked)
            self.assertIsNone(rz.gap_answers([KEY])[KEY], "the rule")
            self.assertEqual(asked, [], "no point-cloud read in the planning process")
            self.assertFalse(os.path.exists(self.cell_file(cache)), "the kept cell dropped")
            want = os.listdir(os.path.join(rz._session_dir(), roadz.WANT_DIR))
            self.assertEqual(want, [f"lidar_{KEY.z}_{KEY.x}_{KEY.y}"], "asked of the server's prefetch")

    def moved(self):
        import test_roads
        return test_roads.features([[(-400, 0.5), (400, 0.5)], [(-12, -500), (-12, 500)], [(12, 500), (12, -500)]],
                                   ["secondary", "motorway", "motorway"],
                                   flags=[(0, "is_bridge", (400 - 35) / 800, (400 + 35) / 800)],
                                   ids=["cross", "i20n", "i20s"])

    def test_planning_process_takes_the_cdn_instead_of_the_rule(self):
        """A planning process of a server run (the prefetch reads the lidar; nothing on disk yet) takes the CDN's
        answer for the run instead of the rule (gap_answers), without a point-cloud read."""
        _got, rec, ikey = self.measured()
        with mock.patch.dict(os.environ, {roadz.PREFETCH_ENV: "1"}), mock.patch.object(roadz, "PREFETCH_ON", True):
            with mock.patch.dict(os.environ, {"BNGE_LIDARZ": "off", "BNGE_ROADZ_SESSION": "rule"}):
                rz = provider(self.cache(), files=self.files)
                self.assertIsNone(rz.gap_answers([KEY])[KEY], "without it: the rule for the run")
            self.publish({ikey: rec})
            asked = []
            rz = provider(self.cache(), files=self.files, asked=asked)
            got = rz.gap_answers([KEY])[KEY]
            self.assertIsNotNone(got)
            self.assertEqual(asked, [])
            self.assertTrue(rz.lidar_on_disk(KEY))


if __name__ == "__main__":
    unittest.main()
