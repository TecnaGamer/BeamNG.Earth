"""snowcover (winter W2): the snow term on the ground photos, its band tables and its keys."""
import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy  # noqa: E402
import revisit  # noqa: E402
import snowcover  # noqa: E402


@contextmanager
def winter(snow=None, on=True):
    """snowcover as a server started with BNE_WINTER / BNE_SNOW would have it (module constants: reloaded)."""
    old = {k: os.environ.get(k) for k in ("BNE_WINTER", "BNE_SNOW")}
    try:
        for k in old:
            os.environ.pop(k, None)
        if on:
            os.environ["BNE_WINTER"] = "on"
        if snow is not None:
            os.environ["BNE_SNOW"] = snow
        importlib.reload(snowcover)
        yield snowcover
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        snowcover.set_provider(None)
        importlib.reload(snowcover)


ALPS = (46.55, 7.95)        # a polar / alpine Koppen class near (permanent snow possible)
PLAINS = (40.0, -89.0)      # Illinois: none


def ramp(lo, hi, n=65, axis=0):
    """A height grid rising from lo to hi to the north (axis 0) or the east (axis 1)."""
    v = np.linspace(lo, hi, n)
    return np.tile(v[:, None], (1, n)) if axis == 0 else np.tile(v[None, :], (n, 1))


class OffTest(unittest.TestCase):
    def test_off_reads_and_keys_nothing(self):
        with winter(on=False) as sc:
            self.assertFalse(sc.ON)
            self.assertEqual(sc.MODE, "none")
            self.assertIsNone(sc.spec_for_box(46.0, 7.0, 47.0, 8.0))
            self.assertIsNone(sc.spec_for_square(geodesy.EnuFrame(*ALPS), -500, -500, 1000))
            self.assertEqual(sc.key_parts(None), ())
            self.assertIsNone(sc.hello())

    def test_none_is_todays_key(self):
        """BNE_SNOW=none with winter on: no spec, no key part, and the switches stay out of the code key (every key
        that holds no snow part is today's)."""
        revisit._switches.clear()
        base = revisit.switches_key()
        with winter("none") as sc:
            revisit._switches.clear()
            self.assertEqual(revisit.switches_key(), base)
            self.assertIsNone(sc.spec_for_box(*ALPS, ALPS[0] + 0.01, ALPS[1] + 0.01))
            self.assertEqual(revisit.key("far", 1, 2, *sc.key_parts(None)), revisit.key("far", 1, 2))
        with winter("heavy") as sc:
            revisit._switches.clear()
            self.assertEqual(revisit.switches_key(), base, "BNE_WINTER / BNE_SNOW are keyed by the specs, not the code")
            sp = sc.spec_for_box(*PLAINS, PLAINS[0] + 0.01, PLAINS[1] + 0.01)
            self.assertIsNotNone(sp)
            self.assertNotEqual(revisit.key("far", 1, 2, *sc.key_parts(sp)), revisit.key("far", 1, 2))
        revisit._switches.clear()


class KeyTest(unittest.TestCase):
    def box(self, at=PLAINS, d=0.05):
        return (at[0] - d, at[1] - d, at[0] + d, at[1] + d)

    def test_presets_differ(self):
        sigs = {}
        for name in snowcover.PRESETS:
            with winter(name) as sc:
                sigs[name] = sc.spec_for_box(*self.box()).sig
                self.assertEqual(sc.spec_for_box(*self.box()).sig, sigs[name], "a spec is a pure function")
        self.assertEqual(len(set(sigs.values())), len(sigs), sigs)

    def test_band_tables_never_share_a_key(self):
        """Any change of a table the box reads (a row's elevation, cover, depth, age, a road share) is another key;
        a change in a cell the box does not read is not."""
        T = snowcover.Table
        base = T(((200, 0, 0, 0), (600, 8, 3, 0)), (8, 8, 8, 8, 8))
        variants = [base, T(((200, 0, 0, 0), (601, 8, 3, 0)), base.road), T(((200, 0, 0, 0), (600, 7, 3, 0)), base.road),
                    T(((200, 0, 0, 0), (600, 8, 4, 0)), base.road), T(((200, 0, 0, 0), (600, 8, 3, 1)), base.road),
                    T(base.rows, (8, 8, 8, 8, 7)), T(((600, 8, 3, 0),), base.road)]
        with winter("real") as sc:
            sigs = []
            for t in variants:
                sc.set_provider(lambda s, w, n, e, t=t: sc.Field(0.1, uniform=t))
                sigs.append(sc.spec_for_box(*self.box()).sig)
            self.assertEqual(len(set(sigs)), len(sigs))
            # a per-cell field: the box's own cells and their padding are read, a cell far away is not
            ci, cj = int((PLAINS[1] + 180) / 0.1), int((90 - PLAINS[0]) / 0.1)
            cells = {(ci + di, cj + dj): base for di in range(-3, 4) for dj in range(-3, 4)}
            sc.set_provider(lambda s, w, n, e: sc.Field(0.1, cells))
            a = sc.spec_for_box(*self.box(d=0.01)).sig
            far = dict(cells)
            far[(ci + 3, cj + 3)] = variants[1]
            sc.set_provider(lambda s, w, n, e: sc.Field(0.1, far))
            self.assertEqual(sc.spec_for_box(*self.box(d=0.01)).sig, a, "a cell the box does not read")
            near = dict(cells)
            near[(ci, cj)] = variants[1]
            sc.set_provider(lambda s, w, n, e: sc.Field(0.1, near))
            self.assertNotEqual(sc.spec_for_box(*self.box(d=0.01)).sig, a)
            # no cover anywhere round the box and no permanent snow: no spec (today's keys)
            sc.set_provider(lambda s, w, n, e: sc.Field(0.1, uniform=T(((0, 0, 0, 0),), base.road)))
            self.assertIsNone(sc.spec_for_box(*self.box()))
            sc.set_provider(None)
            self.assertIsNone(sc.spec_for_box(*self.box()), "real without W1's state: none")

    def test_permanent_snow_only_in_polar_alpine_climates(self):
        with winter("real") as sc:
            self.assertIsNone(sc.spec_for_box(*self.box(PLAINS)))
            sp = sc.spec_for_box(*self.box(ALPS))
            self.assertTrue(sp is not None and sp.perm and sp.field is None)
            self.assertTrue(sc.perm_possible(71.9, -40.1, 72.0, -40.0), "Greenland")
            self.assertFalse(sc.perm_possible(-1.0, 20.0, -0.9, 20.1), "the Congo")

    def test_server_specs(self):
        """earthd's base photo key and render take one spec of write_base's square; the far tiles' of tile_where's."""
        import earthd
        import heightmap
        with winter("heavy") as sc:
            anchor = SimpleNamespace(frame=geodesy.EnuFrame(*PLAINS), lat=PLAINS[0], lon=PLAINS[1])
            srv = SimpleNamespace(layout=SimpleNamespace(block=1024))
            size = 1024 * heightmap.SQUARE
            got = earthd.Server.base_snow(srv, anchor, 0.0, 0.0)
            want = sc.spec_for_square(anchor.frame, -size / 2 - earthd.BASE_SHIFT_M, -size / 2 - earthd.BASE_SHIFT_M,
                                      size)
            self.assertEqual(got.sig, want.sig)
        with winter(on=False):
            self.assertIsNone(earthd.Server.base_snow(srv, anchor, 0.0, 0.0))


class FarTexKeyTest(unittest.TestCase):
    def test_fartex_key_holds_the_week_and_the_snow(self):
        """Server.tex_key ("fartex", the main process's far textures and the bulk restore's): the season's week
        (season_key, not "auto" alone: October's grade served in late November) and the snow spec's part."""
        import datetime
        import earthd
        import season
        im = SimpleNamespace(chain=[SimpleNamespace(name="eox2016")], leaf_on=True, season_mode="auto")
        srv = SimpleNamespace(imagery=im, tone="auto", tex_modes=(), road_masks=False, mapdata=None, road_widths=None,
                              surface=None, layout=SimpleNamespace(cells=64), road_mask_px=lambda lvl: 0)
        anchor = SimpleNamespace(lat=40.877, lon=-77.566, zooms={k: 16 - k for k in range(9)})
        tile = SimpleNamespace(key=(5, 0, 0), level=5, size=8192.0, bbox=(0.0, 0.0, 8192.0, 8192.0))
        real = season.utc_today
        try:
            season.utc_today = lambda: datetime.date(2026, 10, 20)
            october = earthd.Server.tex_key(srv, anchor, tile, 256)
            season.utc_today = lambda: datetime.date(2026, 11, 24)
            november = earthd.Server.tex_key(srv, anchor, tile, 256)
            self.assertNotEqual(october, november)
            with winter("heavy") as sc:
                sp = sc.spec_for_box(40.8, -77.6, 40.9, -77.5)
                self.assertNotEqual(earthd.Server.tex_key(srv, anchor, tile, 256, sp), november)
                self.assertEqual(earthd.Server.tex_key(srv, anchor, tile, 256, None), november)
        finally:
            season.utc_today = real


class LookTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(*PLAINS)

    def look(self, sc, U, x0=-500.0, size=1000.0, n=65):
        sp = sc.spec_for_square(self.frame, x0, x0, size)
        return sp, sc.look(sp, self.frame, x0, x0, size, U, x0, x0, size / (n - 1))

    def table(self, sc, rows):
        sc.set_provider(lambda s, w, n, e: sc.Field(0.25, uniform=sc.Table(rows, sc.NO_ROAD)))

    def test_line_cut_by_height_and_aspect(self):
        with winter("real") as sc:
            self.table(sc, ((50, 0, 0, 0), (150, 8, 3, 0)))
            ys = np.array([-490.0, 0.0, 490.0])
            xs = np.zeros(3)
            _, up = self.look(sc, ramp(0, 300))                     # rising north: the slope faces south
            c_up, _ = up.sample(xs, ys)
            self.assertLess(c_up[0], 3)
            self.assertGreater(c_up[2], 240)
            _, down = self.look(sc, ramp(300, 0))                   # the same heights falling north: facing the pole
            c_down, _ = down.sample(xs, -ys)
            self.assertGreater(int(c_down[1]), int(c_up[1]), "the snow line lower on the slope facing the pole")
            # a 60 degree wall holds none, whatever the table says
            _, wall = self.look(sc, ramp(0, 1700))
            self.assertLess(int(wall.sample(xs, ys)[0].max()), 3)

    def test_permanent_snow(self):
        frame = geodesy.EnuFrame(*ALPS)
        with winter("real") as sc:
            sp = sc.spec_for_square(frame, -500, -500, 1000)
            hi = sc.look(sp, frame, -500, -500, 1000, np.full((65, 65), 4200.0), -500, -500, 1000 / 64)
            lo = sc.look(sp, frame, -500, -500, 1000, np.full((65, 65), 1200.0), -500, -500, 1000 / 64)
            c, d = hi.sample(np.zeros(1), np.zeros(1))
            self.assertGreater(int(c[0]), 250)
            self.assertEqual(int(d[0]), 6)
            self.assertEqual(int(lo.sample(np.zeros(1), np.zeros(1))[0][0]), 0)

    def test_photo(self):
        with winter("heavy") as sc:
            sp, sl = self.look(sc, np.full((65, 65), 300.0))
            grass = np.zeros((256, 256, 3), np.uint8)
            grass[...] = (95, 120, 60)
            out = sl.apply(grass)
            self.assertGreater(int(out[..., 0].min()), 200, "open ground white under heavy snow")
            crowns = np.zeros_like(grass)
            crowns[...] = (24, 40, 30)
            # Illinois (hot-summer continental): its crowns mostly deciduous, a light bare grey-brown under snow; in the
            # taiga (subarctic) evergreen, near black (snowcover's climate fallback, snow_evergreen)
            bare = sl.apply(crowns).astype(int)
            self.assertTrue(90 < bare[..., 0].mean() < 200, f"deciduous crowns under snow: {bare[..., 0].mean():.0f}")
            taiga = geodesy.EnuFrame(62.0, 26.0)
            sp_t = sc.spec_for_square(taiga, -500, -500, 1000.0)
            sl_t = sc.look(sp_t, taiga, -500, -500, 1000.0, np.full((65, 65), 150.0), -500, -500, 1000 / 64)
            self.assertLess(int(sl_t.apply(crowns)[..., 0].mean()), 75, "evergreen crowns keep most of their colour")
            self.assertGreater(float(bare[..., 0].mean()), float(sl_t.apply(crowns)[..., 0].mean()) + 40)
            snowy = np.full_like(grass, 246)
            got = sl.apply(snowy)
            self.assertLessEqual(int(np.abs(got.astype(int) - 246).max()), 12,
                                 "imagery already snowy keeps its brightness (the snow's tint only)")
            firn = np.zeros_like(grass)
            firn[...] = (205, 195, 150)     # (summer firn in a photo: yellowish)
            out = sl.apply(firn).astype(int)
            self.assertLess(int(np.abs(out[..., 0] - out[..., 2]).max()), 25, "it takes the snow's colour")
            self.assertIsNone(sl.apply(np.full_like(grass, 255)), "blown-out white: left alone")
            # the paved ground (snowcover.road_classes: P2's share of the cover, a quarter under the heavy preset):
            # mostly the photo's asphalt, the yards white
            asphalt = grass.copy()          # (a street through a lawn)
            asphalt[:, 100:140] = (70, 72, 75)
            road = np.zeros(grass.shape[:2], np.uint8)
            road[:, 100:140] = sc.PHOTO_ROAD_CLASS
            out = sl.apply(asphalt, road).astype(int)
            self.assertLess(out[:, 110:130].mean(), 140, "a plowed street")
            self.assertGreater(out[:, :80].mean(), 200, "the white ground beside it")
            _, own = self.look(sc, np.full((65, 65), 300.0))
            sl2 = sc.look(sp, self.frame, -500, -500, 1000.0, np.full((65, 65), 300.0), -500, -500, 1000 / 64, road)
            self.assertTrue(np.array_equal(sl2.apply(asphalt), out), "the look's own road mask, its texture's size")
            self.assertTrue(sl.any(256))
        with winter("dusting") as sc:
            _, dust = self.look(sc, np.full((65, 65), 300.0))
            fine = np.zeros((1024, 1024, 3), np.uint8)     # (1 m texels: the patches show; coarser, an even share)
            fine[...] = grass[0, 0]
            out = dust.apply(fine).astype(int)
            white = (out[..., 0] > 160).mean()
            self.assertTrue(0.1 < white < 0.9, f"a dusting is patchy: {white:.2f} of the texels white")
        with winter("alpine") as sc:
            _, low = self.look(sc, np.full((65, 65), 300.0))
            self.assertIsNone(low.apply(grass), "below the preset's line: untouched")
            self.assertFalse(low.any(256))

    def test_water(self):
        """The map's water (snowcover.water_classes): a river and the sea stay the photo's open water; a lake freezes
        and whitens by the tables' ice share (heavy: frozen; dusting: open); the ice share is in the spec's key."""
        water_rgb = (40, 60, 90)
        img = np.zeros((256, 256, 3), np.uint8)
        img[...] = water_rgb
        mask = np.zeros((256, 256), np.uint8)
        mask[:, :80] = snowcover.WATER_MOVING
        mask[:, 80:170] = snowcover.WATER_STILL
        mask[:, 170:] = snowcover.WATER_SEA
        U = np.full((65, 65), 300.0)
        means = {}
        for preset in ("heavy", "dusting"):
            with winter(preset) as sc:
                sp = sc.spec_for_square(self.frame, -500, -500, 1000.0)
                sl = sc.look(sp, self.frame, -500, -500, 1000.0, U, -500, -500, 1000 / 64, None, mask)
                out = sl.apply(img)
                out = img if out is None else out
                means[preset] = [float(out[:, a:b].mean()) for a, b in ((0, 80), (80, 170), (170, 256))]
        river, lake, sea = means["heavy"]
        # (open water stays the photo's, darker under a winter sky: snowcover.PARAMS open_dark)
        self.assertLess(river, np.mean(water_rgb), "a river stays open, darker")
        self.assertLess(sea, np.mean(water_rgb), "the sea stays open, darker")
        self.assertGreater(lake, 200, "a lake under heavy snow: frozen and white")
        self.assertLess(means["dusting"][1], 100, "a dusting's lakes: open")
        T = snowcover.Table
        with winter("real") as sc:
            sigs = set()
            for ice in (0, 4, 8):
                sc.set_provider(lambda s_, w, n, e, ice=ice: sc.Field(0.1, uniform=T(((0, 8, 4, 0),), sc.NO_ROAD, ice)))
                sigs.add(sc.spec_for_box(40.0, -89.0, 40.1, -88.9).sig)
            self.assertEqual(len(sigs), 3, "the ice share is keyed")

    def test_winter_packs(self):
        """The winter packs (winterpacks.py) in place of the fallbacks: the leaf type's evergreen share decides a crown
        (a taiga cell the pack calls deciduous: light bare crowns), JRC's water share keeps a narrow water body open
        whatever its photo colour; their versions are in the spec's key and their credits named."""
        import winterpacks
        crowns = np.zeros((256, 256, 3), np.uint8)
        crowns[...] = (24, 40, 30)
        muddy = np.zeros_like(crowns)
        muddy[...] = (92, 85, 82)       # (a muddy river in the USGS mosaic: grey-brown, not blue)
        taiga = geodesy.EnuFrame(62.0, 26.0)

        class Packs:
            def __init__(self, ever, water, version="v1-0000000000000001"):
                self.ever, self.water_v, self.version = ever, water, version

            def versions(self):
                return {"leaftype": self.version, "watershare": self.version}

            def leaf_grid(self):
                return winterpacks.LeafGrid(np.full((3600, 7200), self.ever, np.uint8),
                                            np.full((3600, 7200), 200, np.uint8), 0.05, -180.0, 90.0, 250)

            def water_grid(self, south, west, north, east):
                j0, i0 = int((90.0 - north) * 250), int((west + 180.0) * 250)
                rows, cols = int((90.0 - south) * 250) - j0 + 1, int((east + 180.0) * 250) - i0 + 1
                v = np.zeros((rows, cols), np.uint8)
                v[:, cols // 2] = self.water_v       # (a river one cell wide, ~210 m here, down the middle)
                return winterpacks.WaterGrid(v, 90.0 - j0 / 250, i0 / 250 - 180.0, 1 / 250, 250, 255)
        with winter("heavy") as sc:
            sp0 = sc.spec_for_square(taiga, -500, -500, 1000.0)
            look0 = sc.look(sp0, taiga, -500, -500, 1000.0, np.full((65, 65), 150.0), -500, -500, 1000 / 64)
            dark = float(look0.apply(crowns)[..., 0].mean())
            try:
                sc.set_packs(Packs(0, 250))
                sp = sc.spec_for_square(taiga, -500, -500, 1000.0)
                self.assertEqual(set(sp.credits), {"land:esa-cci-lc", "land:jrc-gsw"})
                self.assertNotEqual(sp.sig, sp0.sig, "the packs are in the key")
                sl = sc.look(sp, taiga, -500, -500, 1000.0, np.full((65, 65), 150.0), -500, -500, 1000 / 64)
                light = float(sl.apply(crowns)[..., 0].mean())
                self.assertGreater(light, dark + 40, f"the pack's deciduous taiga: {light:.0f} against {dark:.0f}")
                out = sl.apply(muddy).astype(int)
                col = int(np.argmin(out[:, :, 0].mean(axis=0)))
                self.assertLess(out[:, col, 0].mean(), 92, "the river stays open (darker under a winter sky)")
                self.assertTrue(80 < col < 176, col)
                self.assertGreater(out[:, :40, 0].mean(), 180, "the ground beside it white")
                sc.set_packs(Packs(0, 250, "v2-0000000000000002"))
                self.assertNotEqual(sc.spec_for_square(taiga, -500, -500, 1000.0).sig, sp.sig, "another version")
            finally:
                sc.set_packs(None)

    def test_no_seam(self):
        """A block photo and a far tile over the same ground: their snow agrees where they overlap (one function of
        the world point; planar heights, which both grids hold exactly)."""
        with winter("real") as sc:
            self.table(sc, ((100, 0, 0, 0), (400, 8, 3, 0)))

            def plane(x0, size, n):
                xs = x0 + np.arange(n) * (size / (n - 1))
                return 150.0 + 0.1 * xs[None, :] + 0.15 * xs[:, None] + 0.0 * xs[:, None]
            # block: [-500, 500]^2 at 65 nodes; far tile: [-500, 3500]^2 at 33 nodes, the block its south-west corner
            sp_b = sc.spec_for_square(self.frame, -500, -500, 1000)
            sp_f = sc.spec_for_square(self.frame, -500, -500, 4000)
            b = sc.look(sp_b, self.frame, -500, -500, 1000, plane(-500, 1000, 65), -500, -500, 1000 / 64)
            f = sc.look(sp_f, self.frame, -500, -500, 4000, plane(-500, 4000, 33), -500, -500, 4000 / 32)
            pts = np.linspace(-490, 490, 15)
            xs, ys = np.meshgrid(pts, pts)
            cb, _ = b.sample(xs.ravel(), ys.ravel())
            cf, _ = f.sample(xs.ravel(), ys.ravel())
            self.assertLessEqual(int(np.abs(cb.astype(int) - cf.astype(int)).max()), 2)
            # the same texels (the far tile's 4 m texel grid over the block's corner): the same colours
            g = np.zeros((1000, 1000, 3), np.uint8)
            g[...] = (90, 110, 70)
            ob = b.apply(g[::4, ::4].copy())
            of = f.apply(np.ascontiguousarray(np.tile(g[:1, :1], (1000, 1000, 1))))
            self.assertIsNotNone(ob)
            self.assertLessEqual(int(np.abs(ob.astype(int) - of[750:, :250].astype(int)).max()), 3)


class LiveTest(unittest.TestCase):
    def test_state_change_textures_what_is_shown_again(self):
        """snow_mode: another mode live (winter on only), every far tile with a texture marked stale and queued for the
        live retexture, every block's photo for its refresh; the message's object is the protocol table's."""
        import json
        import earthd
        with winter(on=False) as sc:
            self.assertFalse(sc.set_mode("heavy"), "winter off: no snow mode")
        with winter("none") as sc:
            self.assertFalse(sc.set_mode("blizzard"))
            self.assertTrue(sc.set_mode("heavy"))
            self.assertEqual(sc.MODE, "heavy")
            self.assertFalse(sc.set_mode("heavy"), "no change")
            shown = SimpleNamespace(material="m", texture="t.dds", stale=False)
            bare = SimpleNamespace(material=None, texture=None, stale=False)
            queued = []
            srv = SimpleNamespace(tiles={(1, "a"): shown, (1, "b"): bare}, near_visual="detail",
                                  hm={(1, 0, 0): {}, (2, 0, 0): {}}, anchors={1: object()}, base_stale=set())
            srv.retex_request = lambda ck, pump=True: queued.append(ck)
            srv._retex_pump = lambda: None
            self.assertEqual(earthd.Server.snow_changed(srv), 1)
            self.assertEqual((queued, shown.stale, bare.stale), ([(1, "a")], True, False))
            self.assertEqual(srv.base_stale, {(1, 0, 0)})
            table = json.loads((Path(__file__).resolve().parents[1] / "protocol_table.json").read_text())
            self.assertLessEqual(set(sc.hello()), set(table["nested"]["snow.snow"]))
            self.assertLessEqual(set(sc.hello()), set(table["nested"]["hello.snow"]))


class W1ProviderTest(unittest.TestCase):
    def test_w1_service_drives_the_photos(self):
        """W1's snow.Service as snowcover's real provider (BNE_SNOW=real): no state, no spec (today's keys); its state
        committed, a spec whose key holds its table, white ground; Server.snow_changed and the 'snow' message on a
        change of table."""
        import snow
        from test_snow import FakeHourly

        class Srv:
            def __init__(self):
                self.changes, self.connections = 0, []

            def snow_changed(self):
                self.changes += 1

        class Conn:
            def __init__(self, s):
                self.s, self.sent = s, []

            def send(self, m):
                self.sent.append(m)
        with winter("real") as sc:
            svc = snow.Service(fetch=FakeHourly(), mode="light", cover=sc)
            srv = Srv()
            conn = Conn(srv)
            srv.connections.append(conn)
            frame = geodesy.EnuFrame(45.0, -93.0)
            self.assertIsNone(sc.spec_for_square(frame, -500, -500, 1000.0), "no state yet: today's keys")
            raw = {}
            svc.get(45.0, -93.0, 200, -1, raw)
            self.assertTrue(svc.commit(conn, raw))
            self.assertEqual(srv.changes, 1)
            self.assertTrue(any(m.get("type") == "snow" for m in conn.sent))
            sp = sc.spec_for_square(frame, -500, -500, 1000.0)
            self.assertIsNotNone(sp, "W1's state: snow on the photos")
            sl = sc.look(sp, frame, -500, -500, 1000.0, np.full((65, 65), 200.0), -500, -500, 1000 / 64)
            grass = np.zeros((256, 256, 3), np.uint8)
            grass[...] = (95, 120, 60)
            out = sl.apply(grass)
            self.assertIsNotNone(out)
            self.assertGreater(float(out[..., 0].mean()), 150.0, "W1's light snow whitens the grass")


class LayersTest(unittest.TestCase):
    def test_deep_snow_to_the_snow_layer(self):
        """A block's .ter lists the snow layer while winter is on; snow_layers moves its land samples under deep snow
        there and leaves the roads; off, the listing is today's."""
        import tempfile
        import surface as sf
        n = 64
        idx = np.full((n, n), sf.GRASS, np.uint8)
        idx[:, 20:24] = sf.ASPHALT
        idx[40:, :10] = sf.FOREST
        q = np.zeros((n, n), np.uint16)
        frame = geodesy.EnuFrame(*PLAINS)
        with tempfile.TemporaryDirectory() as d:
            with winter(on=False):
                _, used = sf.write_ter(d, "a.ter", q, idx)
                self.assertNotIn(sf.SNOW, used)
            for preset, want in (("heavy", True), ("light", False)):
                with winter(preset) as sc:
                    name, used = sf.write_ter(d, "b.ter", q, idx)
                    self.assertIn(sf.SNOW, used)
                    self.assertEqual(used[-1], sf.DIRT if sf.DIRT in used else used[-1])
                    masks = sf.write_masks(idx[::-1], d, "b")
                    sp = sc.spec_for_square(frame, 0.0, 0.0, float(n))
                    look = sc.look(sp, frame, 0.0, 0.0, float(n), np.full((5, 5), 200.0), 0.5, 0.5, 16.0)
                    _, _, _, was, _ = sf._read_ter(f"{d}/{name}")      # (its border ring: holes)
                    k = sf.snow_layers(d, name, masks, look, 0.0, 0.0, 1.0)
                    _, _, _, lay, _ = sf._read_ter(f"{d}/{name}")
                    if want:
                        land = np.isin(was, (sf.GRASS, sf.FOREST))
                        self.assertEqual(k, int(land.sum()))
                        self.assertTrue((lay[was == sf.ASPHALT] == sf.ASPHALT).all(), "the roads stay theirs")
                        self.assertTrue((lay[land] == sf.SNOW).all())
                    else:
                        self.assertEqual(k, 0, "D2: not deep, the land layers stay")


if __name__ == "__main__":
    unittest.main()
