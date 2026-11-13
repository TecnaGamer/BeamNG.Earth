"""Paint lists (paintlist.py, earthfast 295 paintlist.rs): a ground photo's whole paint in one native call from parts
made once per set of map cells is byte-identical to the per-square Python it replaced (paint_reference.py), in every
step (fields and their dirt roads, golf, lots, pads, roundabout islands, tapered carriageways, ramp gores, curbless
and curb corners, stand-ins, the road ghosts' segments); Pillow's polygon and wide-line rasterisers as ported; the parts
kept per cell set and not when something they read was short; the far-tile process's paint (blockworker.far_paint)."""
import hashlib
import math
import os
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import FRAME, LAT, LON, M_LAT, M_LON, features, rules  # noqa: E402
import fast  # noqa: E402
import paint_reference as ref  # noqa: E402
import paintlist  # noqa: E402
import revisit  # noqa: E402
import surface as sf  # noqa: E402


def ll(points):
    """(k, 2) lon / lat of east / north metres round the fixtures' origin."""
    p = np.asarray(points, np.float64)
    return np.c_[LON + p[:, 0] / M_LON, LAT + p[:, 1] / M_LAT]


def circle(cx, cy, r, n=24):
    a = np.linspace(0.0, 2 * math.pi, n + 1)
    return np.c_[cx + r * np.cos(a), cy + r * np.sin(a)]


class Areas:
    """Land-use / infrastructure polygons (mapgeom.Features' duck type the photo painters read)."""

    def __init__(self, rings, classes, subtypes=None, ids=None):
        self.rings = [ll(r) for r in rings]
        self.kind = np.full(len(rings), 3, np.uint8)
        self.attrs = {"class": np.array(classes, object),
                      "subtype": np.array(subtypes or [""] * len(rings), object)}
        self.ids = np.array(ids or [f"a{i}" for i in range(len(rings))], object)

    def __len__(self):
        return len(self.rings)

    def parts(self, i):
        return [[self.rings[i]]]


def scene():
    """(road Features, area layers, pads): a town grid half built up (curb returns east, curbless corners west), a
    roundabout with its approaches, a motorway with an exit ramp (a gore), a dirt track through a field, roads of
    two widths end to end (a taper), a golf course, a car park and two gas station pads."""
    lines, classes = [], []

    def road(pts, cls):
        lines.append([tuple(p) for p in pts])
        classes.append(cls)
    road([(-260, 0), (0, 0), (260, 0)], "residential")
    road([(0, -200), (0, 0), (0, 220)], "residential")
    road([(120, -200), (120, 0), (120, 220)], "tertiary")
    road([(-260, -90), (-60, -90)], "unclassified")
    road([(-60, -90), (260, -90)], "secondary")              # (another width end to end: a taper)
    ring = circle(-140.0, 110.0, 22.0, 24)
    quarters = [ring[k * 6:k * 6 + 7] for k in range(4)]
    for q in quarters:
        road(q, "tertiary")
    road([(-140, 132), (-140, 220)], "tertiary")
    road([(-140, 88), (-140, 0)], "tertiary")
    road([(-300, -250), (300, -250)], "motorway")
    rx = np.arange(-50.0, 251.0, 10.0)
    road(np.c_[rx, -250.0 - (rx + 50.0) / 10.0], "motorway")
    road([(160, 60), (300, 200)], "track")
    f = features(lines, classes)
    n = len(lines)
    ow = [(k, 1.0) for k in range(5, 9)] + [(n - 3, 1.0), (n - 2, 1.0)]
    f.rules["oneway"] = rules(ow, "float")
    f.rules["subclass"] = rules([(n - 2, "link")])
    f.rules["surface"] = rules([(n - 1, "dirt")])
    lots = Areas([[(30, 30), (90, 30), (90, 80), (30, 80), (30, 30)],                     # a car park
                  [(150, 40), (250, 40), (250, 190), (150, 190), (150, 40)],               # a field the track crosses
                  [(-250, 150), (-180, 150), (-180, 210), (-250, 210), (-250, 150)],       # golf: rough
                  [(-240, 160), (-200, 160), (-200, 200), (-240, 200), (-240, 160)],       # fairway
                  [(-230, 170), (-215, 170), (-215, 185), (-230, 185), (-230, 170)],       # green
                  [(-200, 150), (-190, 150), (-190, 158), (-200, 158), (-200, 150)]],      # bunker
                 ["parking", "farmland", "rough", "fairway", "green", "bunker"],
                 ["", "", "golf", "golf", "golf", "golf"], ["lot", "field", "g1", "g2", "g3", "g4"])
    pads = []
    for e0, n0 in ((40.0, -60.0), (180.0, -40.0)):
        e = np.array([e0, e0 + 25.0, e0 + 25.0, e0])
        nn = np.array([n0, n0, n0 + 18.0, n0 + 18.0])
        la, lo, _ = FRAME.enu_to_geodetic(e, nn, np.zeros(4))
        pads.append((np.asarray(la), np.asarray(lo)))
    return f, [lots], pads


class FakeSurface:
    """The Surface the painters ask: its cells' roads and lots, the town (east of the origin built up), pads."""

    def __init__(self, roads, lots, pads):
        self._roads, self._lots, self._pads = roads, lots, pads
        self.gas_pads, self.bus_pads, self.extra_pads = True, False, None

    def roads(self, lat, lon):
        return list(self._roads), True

    def lots(self, lat, lon):
        return list(self._lots)

    def fuel_pads(self, lat, lon):
        return list(self._pads)

    def urban(self, lat, lon):
        return np.asarray(lon) > LON


def photo(px, seed):
    """A pseudo aerial: smooth greys and greens with speckle (the toning's samples, the islands' green test)."""
    g = np.random.default_rng(seed)
    out = np.zeros((px, px, 3), np.float32)
    for k in (4, 16):
        n = g.uniform(0, 1, (k + 1, k + 1, 3)).astype(np.float32)
        t = np.linspace(0, k, px)
        i0 = np.minimum(t.astype(int), k - 1)
        f = (t - i0)[:, None, None]
        rows = n[i0] * (1 - f) + n[i0 + 1] * f
        out += (rows[:, i0] * (1 - f.transpose(1, 0, 2)) + rows[:, i0 + 1] * f.transpose(1, 0, 2)) * 120.0
    green = out[..., 1] > np.percentile(out[..., 1], 55)
    out[green, 1] += 40
    out += g.normal(0, 6, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


SQUARES = [(-300.0, -300.0, 600.0, 512), (-64.0, -64.0, 128.0, 256), (-180.0, 60.0, 96.0, 256),
           (-120.0, -300.0, 256.0, 256), (100.0, 20.0, 230.0, 192), (-256.0, 128.0, 128.0, 128),
           (-40.0, -130.0, 160.0, 160), (150.0, 30.0, 64.0, 64), (-300.0, -300.0, 600.0, 300)]


class PaintParityTest(unittest.TestCase):
    """surface.paint_square from the paint lists against the per-square Python (paint_reference)."""

    @classmethod
    def setUpClass(cls):
        fast.require(paintlist.NATIVE, "paint lists")
        cls.roads, cls.lots, cls.pads = scene()
        cls.surf = FakeSurface([cls.roads], cls.lots, cls.pads)

    def setUp(self):
        paintlist.clear()

    def test_every_step_byte_identical(self):
        painted = {}
        for k, (x0, y0, size, px) in enumerate(SQUARES):
            rgb = photo(px, k)
            for simple in (False, True):
                want = ref.paint_square(rgb, FRAME, x0, y0, size, self.surf, None, simple=simple)
                got = sf.paint_square(rgb, FRAME, x0, y0, size, self.surf, None, simple=simple)
                np.testing.assert_array_equal(got, want, f"square {k} simple {simple}")
                painted[(k, simple)] = int(np.any(want != rgb, axis=2).sum())
            self.assertTrue(np.array_equal(rgb, photo(px, k)), "the input untouched")
        self.assertTrue(all(painted[(k, False)] > 0 for k in range(len(SQUARES))), painted)

    def test_each_step_was_exercised(self):
        """The scene has what each step paints, in the big square: fields, golf, lots, pads, islands, gores,
        tapers, both kinds of corners."""
        x0, y0, size, px = SQUARES[0]
        roads, (lots,), pads = self.roads, self.lots, self.pads
        self.assertTrue(ref._road_segments(FRAME, x0, y0, size, px, px, [roads], self.surf.urban) is not None)
        self.assertTrue(sf.gore_fills(FRAME, [roads], (x0, y0, x0 + size, y0 + size), self.surf.urban), "a gore")
        self.assertTrue(sf._roundabout_footprints(sf._roads_list_enu(FRAME, [roads], self.surf.urban)), "an island")
        pads_ = sf.corner_pads(FRAME, x0, y0, size, [roads], self.surf.urban)
        self.assertGreater(len(pads_), 4, "corners")
        rgb = photo(px, 0)
        for name, step in (("fields", lambda: ref.photo_fields(rgb, FRAME, x0, y0, size, [lots], [roads])),
                           ("golf", lambda: ref.photo_golf(rgb, FRAME, x0, y0, size, [lots])),
                           ("lots", lambda: ref.photo_lots(rgb, FRAME, x0, y0, size, [lots])),
                           ("pads", lambda: ref.photo_pads(rgb, FRAME, x0, y0, size, pads))):
            self.assertTrue(np.any(step() != rgb), name)
        specs = [s for s in __import__("roads").road_specs(roads, self.surf.urban) if s is not None]
        self.assertTrue(any(s.kind == "dirt" for s in specs), "a dirt track (the furrows stop at it)")

    def test_the_photo_helpers_byte_identical(self):
        x0, y0, size, px = SQUARES[0]
        rgb = photo(px, 3)
        roads, lots, pads, urban = [self.roads], self.lots, self.pads, self.surf.urban
        for name, a, b in (
                ("fields", ref.photo_fields(rgb, FRAME, x0, y0, size, lots, roads, urban),
                 sf.photo_fields(rgb, FRAME, x0, y0, size, lots, roads, urban)),
                ("golf", ref.photo_golf(rgb, FRAME, x0, y0, size, lots), sf.photo_golf(rgb, FRAME, x0, y0, size, lots)),
                ("lots", ref.photo_lots(rgb, FRAME, x0, y0, size, lots), sf.photo_lots(rgb, FRAME, x0, y0, size, lots)),
                ("pads", ref.photo_pads(rgb, FRAME, x0, y0, size, pads), sf.photo_pads(rgb, FRAME, x0, y0, size, pads)),
                ("roads", ref.photo_roads(rgb, FRAME, x0, y0, size, roads, urban),
                 sf.photo_roads(rgb, FRAME, x0, y0, size, roads, urban)),
                ("ghosts", ref.photo_ghosts(rgb, FRAME, x0, y0, size, roads, urban),
                 sf.photo_ghosts(rgb, FRAME, x0, y0, size, roads, urban))):
            np.testing.assert_array_equal(b, a, name)

    def test_segments_and_their_roads(self):
        """_road_segments (the ghosts' and the road mask's): the carriageways' segments, widths, kinds, roads and
        curbs as before, in the same order; then the gores that can reach the photo (the Python took those of the
        roads within 300 m: some beyond the photo, none it touches left out), painting the same pixels."""
        colors = np.array([(255.0, 255.0, 255.0, 1.0)] * len(sf.PHOTO_ROAD_RGBA), np.float32)
        for x0, y0, size, px in SQUARES:
            of_a, of_b = [], []
            a = ref._road_segments(FRAME, x0, y0, size, px, px, [self.roads], self.surf.urban, roads_of=of_a)
            b = sf._road_segments(FRAME, x0, y0, size, px, px, [self.roads], self.surf.urban, roads_of=of_b)
            gores = sf.gore_fills(FRAME, [self.roads], (x0 - 300, y0 - 300, x0 + size + 300, y0 + size + 300),
                                  self.surf.urban)
            n = len(a[0]) - sum(len(mid) - 1 for _, mid in gores)
            for u, v in zip(a, b):
                np.testing.assert_array_equal(v[:n], u[:n])
            ra, ga = np.concatenate([r for r, _ in of_a]), np.concatenate([g for _, g in of_a])
            rb, gb = np.concatenate([r for r, _ in of_b]), np.concatenate([g for _, g in of_b])
            np.testing.assert_array_equal(rb[:n], ra[:n])
            np.testing.assert_array_equal(gb[:n], ga[:n])
            self.assertFalse(gb[n:].any(), "gores have no curbs")
            self.assertTrue(len(set(rb[n:].tolist())) == len(set(rb[n:].tolist()) - set(rb[:n].tolist())),
                            "each gore its own road")
            blank = np.zeros((px, px, 3), np.uint8)
            np.testing.assert_array_equal(fast._ef.paint_lines(blank, *b, colors), fast._ef.paint_lines(blank, *a, colors))

    def test_island_fill_byte_identical(self):
        poly = [(20, 20), (80, 20), (80, 80), (20, 80)]
        grass = np.full((100, 100, 3), (170, 165, 155), np.uint8)
        grass[30:70, 30:70] = (70, 120, 50)
        for p in (poly, [(10.5, 3.2), (95.9, 40.1), (60.0, 99.7), (2.2, 70.0)]):
            np.testing.assert_array_equal(sf._fill_island_photos(grass, [p]), ref._fill_island_photos(grass, [p]))

    def test_parts_made_once_per_cell_set(self):
        made = []
        orig = paintlist._build_roads

        def counted(*a):
            made.append(1)
            return orig(*a)
        paintlist._build_roads = counted
        squares = [(-64.0, -64.0, 128.0, 256), (0.0, 0.0, 64.0, 128), (-40.0, -130.0, 160.0, 160),
                   (-100.0, -20.0, 150.0, 256)]
        self.assertEqual(len({paintlist.group(*q[:3]) for q in squares}), 1, "one group")
        try:
            for x0, y0, size, px in squares:
                sf.paint_square(photo(px, 1), FRAME, x0, y0, size, self.surf, None)
        finally:
            paintlist._build_roads = orig
        self.assertEqual(len(made), 1, "the carriageways' part made once for the group's four squares")

    def test_a_part_read_short_is_not_kept(self):
        """A part made while something it read was missing (revisit.fault) is not kept: used again for SHORT_KEEP_S
        (each build that paints with it told: not kept), then made again; one made whole is kept."""
        roads, lots, pads = scene()
        calls = []

        class Short(FakeSurface):
            def urban(self, lat, lon):
                calls.append(1)
                if len(calls) == 1:
                    revisit.fault()
                return np.asarray(lon) > LON
        s = Short([roads], lots, pads)
        x0, y0, size, px = SQUARES[1]
        made = []
        orig, keep = paintlist._build_roads, paintlist.SHORT_KEEP_S

        def counted(*a):
            made.append(1)
            return orig(*a)
        paintlist._build_roads = counted
        try:
            with revisit.scope() as b:
                first = sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertFalse(b.complete)
            with revisit.scope() as b:
                again = sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertFalse(b.complete, "the short part's faults told again")
            self.assertEqual(len(made), 1, "used again meanwhile")
            np.testing.assert_array_equal(again, first)
            paintlist.SHORT_KEEP_S = 0.0
            with revisit.scope() as b:
                again = sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertTrue(b.complete)
            self.assertEqual(len(made), 2, "made again")
            np.testing.assert_array_equal(again, first)
            sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertEqual(len(made), 2, "kept once made whole")
        finally:
            paintlist._build_roads, paintlist.SHORT_KEEP_S = orig, keep

    def test_an_orphan_fault_is_not_lost(self):
        """A fault of a thread no scope covers (a Surface's background fetch failing) while a part is made: the part is
        not kept, the photo's build is incomplete, and blockworker.far_paint tells the server at least one fault."""
        import threading
        import blockworker
        roads, lots, pads = scene()

        class Orphan(FakeSurface):
            def urban(self, lat, lon):
                t = threading.Thread(target=revisit.fault)      # (no scope in that thread: the process's fault)
                t.start()
                t.join()
                return np.asarray(lon) > LON
        x0, y0, size, px = SQUARES[1]
        saved = blockworker._s
        try:                    # (a fresh layer: its road specs are made, and the land cover read, in this call)
            blockworker._s = SimpleNamespace(surface=Orphan([roads], lots, pads), widths=None, md=None)
            _, n, lidar = blockworker.far_paint(LAT, LON, x0, y0, size, photo(px, 2), pads)
            self.assertGreaterEqual(n + lidar, 1, "the server's build told")
        finally:
            blockworker._s = saved
        roads, lots, pads = scene()
        s = Orphan([roads], lots, pads)
        made = []
        orig, keep = paintlist._build_roads, paintlist.SHORT_KEEP_S

        def counted(*a):
            made.append(1)
            return orig(*a)
        paintlist._build_roads = counted
        paintlist.SHORT_KEEP_S = 0.0
        try:
            with revisit.scope() as b:
                sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertFalse(b.complete)
            st = paintlist.cell_set(FRAME, [roads], (), s.urban, None)
            self.assertFalse(st.parts, "not kept")
            sf.paint_square(photo(px, 2), FRAME, x0, y0, size, s, None, simple=True)
            self.assertEqual(len(made), 2, "made again")
        finally:
            paintlist._build_roads, paintlist.SHORT_KEEP_S = orig, keep

    def test_short_parts_stay_bounded(self):
        """Parts made short over many groups: at most SHORTS_KEPT kept per cell set, the expired ones dropped, and a
        part made whole replaces its short one."""
        roads, lots, pads = scene()
        state = {"short": True}

        class Flaky(FakeSurface):
            def urban(self, lat, lon):
                if state["short"]:
                    revisit.fault()
                return np.asarray(lon) > LON
        s = Flaky([roads], lots, pads)
        keep = paintlist.SHORT_KEEP_S
        try:
            paintlist.SHORT_KEEP_S = 60.0
            squares = [(-2048.0 + 600.0 * k, -2048.0 + 600.0 * j, 64.0) for k in range(6) for j in range(6)]
            for x0, y0, size in squares:
                sf.paint_square(photo(64, 1), FRAME, x0, y0, size, s, None)
            st = paintlist.cell_set(FRAME, [roads], lots, s.urban, None)
            self.assertLessEqual(len(st.shorts), paintlist.SHORTS_KEPT)
            self.assertGreater(len(st.shorts), 0)
            paintlist.SHORT_KEEP_S = 0.0
            sf.paint_square(photo(64, 1), FRAME, *squares[0], s, None)
            self.assertLessEqual(len(st.shorts), 3, "the expired ones dropped")
            state["short"] = False
            x0, y0, size = squares[1]
            sf.paint_square(photo(64, 1), FRAME, x0, y0, size, s, None)
            g = paintlist.group(x0, y0, size)
            self.assertIn(("roads",) + g, st.parts)
            self.assertNotIn(("roads",) + g, st.shorts, "replaced by the whole one")
        finally:
            paintlist.SHORT_KEEP_S = keep

    def test_parts_shared_through_the_revisit_cache(self):
        """Map data layers (their cell, layer and versions known): a part made once is kept in the revisit cache, and
        another process (here: the memory dropped) takes it from there, painting the same pixels; a fixture's layers
        (no cell of their own) are never kept."""
        import tempfile
        roads, lots, pads = scene()
        roads.__dict__["_bnge_cell"] = ("14/0/0", "roads", "{}")
        lots[0].__dict__["_bnge_cell"] = ("14/0/0", "land_use", "{}")
        s = FakeSurface([roads], lots, pads)
        x0, y0, size, px = SQUARES[0]
        rgb = photo(px, 4)
        saved = revisit.CACHE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            revisit.CACHE_DIR = tmp
            try:
                want = sf.paint_square(rgb, FRAME, x0, y0, size, s, None)
                np.testing.assert_array_equal(want, ref.paint_square(rgb, FRAME, x0, y0, size, s, None))
                paintlist.clear()
                builders = ("_build_roads", "_build_islands", "_build_corners", "_build_lots", "_build_fields")
                orig = {b: getattr(paintlist, b) for b in builders}

                def boom(*a):
                    raise AssertionError("made again")
                for b in builders:
                    setattr(paintlist, b, boom)
                try:
                    got = sf.paint_square(rgb, FRAME, x0, y0, size, s, None)
                finally:
                    for b, fn in orig.items():
                        setattr(paintlist, b, fn)
                np.testing.assert_array_equal(got, want)
                self.assertFalse(os.listdir(os.path.join(tmp, "paintmaking")), "no marker left")
            finally:
                revisit.CACHE_DIR = saved
                paintlist.clear()
        self.assertIsNone(paintlist.cell_set(FRAME, [self.roads], self.lots).disk_key)

    def test_far_tile_process_paint(self):
        """blockworker.far_paint: the server's pads with the job, a Surface of its own when the process has none;
        the same pixels as the server's paint, and what it read short handed back."""
        import blockworker
        x0, y0, size, px = SQUARES[0]
        rgb = photo(px, 5)
        want = ref.paint_square(rgb, FRAME, x0, y0, size, self.surf, None)
        saved = blockworker._s
        try:
            blockworker._s = SimpleNamespace(surface=self.surf, widths=None, md=None)
            got, n, lidar = blockworker.far_paint(LAT, LON, x0, y0, size, rgb, self.pads)
            np.testing.assert_array_equal(got, want)
            self.assertEqual((n, lidar), (0, 0))
            blockworker._s = SimpleNamespace(surface=None, widths=None, md=None)
            got, n, lidar = blockworker.far_paint(LAT, LON, x0, y0, size, rgb, self.pads)
            self.assertIs(got, rgb, "no map data: as it was")
        finally:
            blockworker._s = saved


def pil_cases(n, seed=1):
    """Random polygons and wide lines (ties on half pixels, integer vertices, blobs, off-canvas parts)."""
    rng = np.random.default_rng(seed)
    for t in range(n):
        w, h = int(rng.integers(1, 48)), int(rng.integers(1, 48))
        k = int(rng.integers(2, 10))
        mode = t % 4
        if mode == 0:
            xy = rng.uniform(-20, 70, (k, 2))
        elif mode == 1:
            xy = np.round(rng.uniform(-10, 60, (k, 2)) * 2) / 2
        elif mode == 2:
            a = np.sort(rng.uniform(0, 2 * np.pi, k))
            r = rng.uniform(3, 25)
            xy = np.c_[w / 2 + r * np.cos(a), h / 2 + r * np.sin(a)]
        else:
            xy = rng.integers(-5, 55, (k, 2)).astype(float)
        width = 1 if t % 3 else int(rng.integers(2, 12))
        yield w, h, np.ascontiguousarray(xy), width


class PilRasterTest(unittest.TestCase):
    """The ported Pillow rasterisers (paintlist.rs pil): ImageDraw.polygon(fill) and ImageDraw.line(width > 1)."""


    def test_same_pixels_as_pillow(self):
        import PIL
        from PIL import Image, ImageDraw
        if not PIL.__version__.startswith("13."):
            self.skipTest(f"ported from Pillow 13 (installed {PIL.__version__}); the digest test still runs")
        for w, h, xy, width in pil_cases(3000):
            im = Image.new("L", (w, h), 0)
            pts = list(zip(xy[:, 0].tolist(), xy[:, 1].tolist()))
            if width > 1:
                ImageDraw.Draw(im).line(pts, fill=255, width=width)
            else:
                ImageDraw.Draw(im).polygon(pts, fill=255)
            np.testing.assert_array_equal(fast._ef.pil_raster(w, h, xy, width), np.asarray(im), (w, h, width, xy))

    def test_digest(self):
        """The 600 cases' rasters' sha256 (PIL_DIGEST, made with Pillow 13.0.0, which they matched pixel for pixel)."""
        h = hashlib.sha256()
        for w, hh, xy, width in pil_cases(600, seed=2):
            h.update(np.ascontiguousarray(fast._ef.pil_raster(w, hh, xy, width)).tobytes())
        self.assertEqual(h.hexdigest(), PIL_DIGEST)


PIL_DIGEST = "847a2a0b09ee294cf12855e3ebf23e469fc6991afe154582473aa0a65e8b2baa"


if __name__ == "__main__":
    unittest.main()
