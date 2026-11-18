"""Offline checks for the public SR16 WMS adapter and placement integration."""
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image


SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import norway_sr16 as sr16
import norway_outline


def make_png():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    colors = [(55, 125, 40, 255), (138, 84, 44, 255), (220, 205, 40, 255),
              (170, 214, 118, 255), (225, 146, 52, 255), (160, 160, 160, 255)]
    for k, color in enumerate(colors):
        for x in range(k * 40, (k + 1) * 40):
            for y in range(256):
                im.putpixel((x, y), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def load_vegetation():
    import importlib.util

    fast = types.ModuleType("fast")
    fast.require = lambda *a: None
    fast._ef = types.SimpleNamespace()
    fast._ef.regional_groups = lambda group, *_a: group
    fakes = {"fast": fast, "dmath": types.ModuleType("dmath"),
             "dnp": types.ModuleType("dnp"), "beaches": types.ModuleType("beaches"),
             "mapcells": types.ModuleType("mapcells"), "pacer": types.ModuleType("pacer"),
             "staging": types.ModuleType("staging")}
    fakes["dmath"].cos = np.cos
    fakes["dmath"].radians = np.radians
    for name in ("log", "tan", "arctan", "exp", "cos", "hypot"):
        setattr(fakes["dnp"], name, getattr(np, name))
    fakes["dnp"].sq = np.square
    sp = importlib.util.spec_from_file_location("veg_sr16_test", SERVER / "vegetation.py")
    mod = importlib.util.module_from_spec(sp)
    with mock.patch.dict(sys.modules, fakes):
        sys.modules[sp.name] = mod
        try:
            sp.loader.exec_module(mod)
        finally:
            sys.modules.pop(sp.name, None)
    return mod


class SR16Test(unittest.TestCase):
    def test_nibio_map_palette_and_transparency(self):
        classes = sr16.SR16.decode(make_png())
        self.assertEqual([int(classes[128, i * 40 + 20]) for i in range(6)],
                         [sr16.SPRUCE, sr16.PINE, sr16.BROADLEAF, sr16.CONIFER_MIX,
                          sr16.MIXED, sr16.NOT_FOREST])
        self.assertEqual(int(classes[128, 252]), sr16.UNKNOWN)

    def test_mercator_bbox_url_and_sampling(self):
        # Pass a real west/south/east/north WebMercator bbox.
        x0, y0 = sr16.mercator(7.5, 62.2)
        x1, y1 = sr16.mercator(8.5, 62.8)
        url = sr16.SR16.url((x0, y0, x1, y1))
        self.assertIn("LAYERS=SRVTRESLAG", url)
        self.assertIn("CRS=EPSG%3A3857", url)
        tile = sr16.Tile(np.full((256, 256), sr16.PINE, np.uint8), (x0, y0, x1, y1))
        self.assertEqual(int(tile.sample(8.0, 62.5)), sr16.PINE)
        self.assertEqual(int(tile.sample(12.0, 62.5)), sr16.UNKNOWN)

    def test_real_http_getmap_path_is_cached_and_reused(self):
        calls = []
        payload = make_png()

        def fake_fetch(url):
            calls.append(url)
            return payload

        with tempfile.TemporaryDirectory() as tmp:
            p = sr16.SR16(tmp, fetch=fake_fetch)
            bounds = (7.5, 62.2, 8.5, 62.8)
            a = p.cell(8888, 9999, bounds)
            self.assertIsNotNone(a)
            self.assertEqual(len(calls), 1)
            b = p.cell(8888, 9999, bounds)
            self.assertEqual(len(calls), 1)
            self.assertEqual(b.classes.shape, (256, 256))
            offline = sr16.SR16(tmp, offline=True, fetch=lambda _url: self.fail("unexpected network"))
            c = offline.cell(8888, 9999, bounds)
            self.assertIsNotNone(c)

    def test_missing_network_keeps_canopy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = sr16.SR16(tmp, fetch=lambda _u: (_ for _ in ()).throw(OSError("offline")))
            bounds = (7.5, 62.2, 8.5, 62.8)
            self.assertIsNone(provider.cell(8888, 9999, bounds))
            self.assertIsNone(provider.cell(8888, 9999, bounds))  # cooldown, no exception

    def test_original_tree_groups_are_preserved(self):
        v = load_vegetation()
        self.assertEqual(v.GROUPS[:25], ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper",
                                               "desert_shrub", "desert_rock", "oak_west", "tropical", "mediterranean",
                                               "conifer_clump", "deciduous_clump", "oak_west_clump", "mediterranean_clump",
                                               "vine", "straw_n", "straw_s", "silage_n", "silage_s", "eucalyptus",
                                               "eucalyptus_clump", "poplar", "creosote"))
        self.assertEqual(v.VERSION, 34)
        self.assertEqual(v.GROUPS[-1], "norway_floorcover")
        self.assertEqual(v.SR16_GAP_CELL_M,15.0)
        self.assertAlmostEqual(v.SR16_GAP_P,.09)
        old = np.array([v.G_DECIDUOUS, v.G_CONIFER, v.G_SHRUB], np.uint8)
        types = np.array([sr16.SPRUCE, sr16.PINE, sr16.UNKNOWN], np.uint8)
        got = v._sr16_groups(old, types, np.array([0.1, 0.9, 0.5]))
        self.assertEqual(got.tolist(), [v.G_CONIFER, v.G_PINE_WEST, v.G_SHRUB])

    def test_saplings_respect_road_masks_and_are_deterministic(self):
        v = load_vegetation()
        lon0, lat0, lon1, lat1 = 7.5, 62.2, 7.506, 62.204
        xx0, yy0 = v._to3857(lon0, lat0)
        xx1, yy1 = v._to3857(lon1, lat1)
        # A synthetic canopy grid that covers this whole small forest patch.
        shape = (700, 700)
        chm = types.SimpleNamespace(x0=xx0 - 5, y0=yy1 + 5, dx=(xx1 - xx0 + 10) / shape[1],
                                    dy=(yy1 - yy0 + 10) / shape[0])
        def sample_class(lon, lat):
            return np.full(np.asarray(lon).shape, v.WC["tree"], np.uint8)
        lc = types.SimpleNamespace(sample=sample_class)
        cd = types.SimpleNamespace(bounds=(lon0, lat0, lon1, lat1))
        fake_sr = types.SimpleNamespace(sample=lambda lon, lat: np.full(np.asarray(lon).shape, sr16.SPRUCE, np.uint8))
        instance = object.__new__(v.Vegetation)
        heights = np.zeros(shape, np.float32)
        blocked = np.zeros(shape, bool)
        a, _ = instance._sr16_saplings(cd, fake_sr, lc, blocked, chm, heights)
        b, _ = instance._sr16_saplings(cd, fake_sr, lc, blocked, chm, heights)
        self.assertGreater(len(a), 0)
        np.testing.assert_array_equal(a.lon, b.lon)
        self.assertTrue(np.all(a.group == v.G_NORWAY_SPRUCE))
        none, _ = instance._sr16_saplings(cd, fake_sr, lc, np.ones(shape, bool), chm, heights)
        self.assertEqual(len(none), 0)

    def test_sr16_grows_roadside_bushes_only_in_mapped_forest(self):
        v = load_vegetation()
        bounds = (7.5, 62.2, 7.506, 62.204)
        x0, y0 = v._to3857(bounds[0], bounds[1])
        x1, y1 = v._to3857(bounds[2], bounds[3])
        shape = (256, 256)
        chm = types.SimpleNamespace(x0=x0 - 5, y0=y1 + 5, dx=(x1 - x0 + 10) / 256,
                                    dy=(y1 - y0 + 10) / 256)
        cd = types.SimpleNamespace(bounds=bounds)
        road = np.zeros(shape, bool)
        road[:, 128] = True
        blocked = road.copy()
        heights = np.zeros(shape, np.float32)
        lc = types.SimpleNamespace(sample=lambda lon, lat: np.full(np.asarray(lon).shape, v.WC["shrub"], np.uint8))
        mapped = types.SimpleNamespace(sample=lambda lon, lat: np.full(np.asarray(lon).shape, sr16.SPRUCE, np.uint8))
        instance = object.__new__(v.Vegetation)
        baseline = instance._edge(cd, lc, blocked, road, chm, heights)
        improved = instance._edge(cd, lc, blocked, road, chm, heights, mapped)
        self.assertEqual(len(baseline), 0)
        self.assertGreater(len(improved), 0)
        px, py = v._to3857(improved.lon, improved.lat)
        cc = np.floor((px - chm.x0) / chm.dx).astype(int)
        rr = np.floor((chm.y0 - py) / chm.dy).astype(int)
        self.assertFalse(np.any(blocked[rr, cc]))

    def test_real_country_boundary_and_cross_border_group_safety(self):
        v = load_vegetation()
        lat = np.array([59.9139,60.3913,69.6492,67.8557,59.3793,66.50])
        lon = np.array([10.7522,5.3221,18.9553,20.2253,13.5020,25.73])
        self.assertEqual(v._nordic(lat,lon).tolist(), [True,True,True,False,False,False])
        stock = np.array([v.G_CONIFER, v.G_PINE_WEST, v.G_DECIDUOUS,
                          v.G_CONIFER,v.G_CONIFER,v.G_DECIDUOUS],np.uint8)
        got = v._norway_tree_groups(stock,lat,lon)
        self.assertEqual(got.tolist(), [v.G_NORWAY_SPRUCE, v.G_NORWAY_PINE,v.G_NORWAY_BIRCH,
                                        v.G_CONIFER,v.G_CONIFER,v.G_DECIDUOUS])

    def test_sr16_class_goes_all_the_way_to_norway_species_group(self):
        v = load_vegetation()
        t = np.array([sr16.SPRUCE,sr16.PINE,sr16.BROADLEAF],np.uint8)
        baseline = np.full(3,v.G_DECIDUOUS,np.uint8)
        mapped = v._sr16_groups(baseline,t,np.full(3,.5))
        rendered = v._norway_tree_groups(mapped,np.full(3,62.5),np.full(3,7.5))
        self.assertEqual([v.GROUPS[x] for x in rendered], ["norway_spruce","norway_pine","norway_birch"])

    def test_roadside_3d_and_far_slope_backdrop(self):
        v=load_vegetation()
        x,y=v._to3857(7.5,62.5)
        chm=types.SimpleNamespace(x0=x,y0=y+200,dx=2.0,dy=2.0)
        road=np.zeros((200,200),bool)
        road[:,100]=True
        px=x+2*np.array([102,20,25,20])
        py=y+200-2*np.array([100,100,100,110])
        lon,lat=v._to4326(px,py)
        lon=np.append(lon,20.2253)  # Kiruna, Sweden: no distant-tree conversion
        lat=np.append(lat,67.8557)
        items=v.Items(lon,lat,np.full(5,16,np.float32),np.full(5,3,np.float32),
                      np.full(5,v.KIND_TREE,np.uint8),
                      np.array([v.G_NORWAY_SPRUCE,v.G_NORWAY_SPRUCE,
                                v.G_CONIFER_CLUMP,v.G_NORWAY_SPRUCE,v.G_CONIFER_CLUMP],np.uint8),
                      np.array([.2,.2,.2,.2,.2],np.float32),np.zeros(5,np.float32),
                      np.array([1,1,1,-1,-1],np.float32))
        result=v._roadside_backdrop(items,road,chm)
        self.assertEqual(result.group.tolist(),[v.G_NORWAY_SPRUCE,v.G_NORWAY_SPRUCE_FAR,
                                                v.G_NORWAY_CONIFER_BACKDROP,v.G_CONIFER_CLUMP])
        self.assertEqual(len(result),4)  # Norwegian member represented by clump removed

    def test_norwegian_understory_is_clustered_and_floorcover_replaces_bushes(self):
        v=load_vegetation()
        bounds=(11.434,62.412,11.440,62.416)
        x0,y0=v._to3857(bounds[0],bounds[1])
        x1,y1=v._to3857(bounds[2],bounds[3])
        shape=(256,256)
        chm=types.SimpleNamespace(x0=x0,y0=y1,dx=(x1-x0)/256,dy=(y1-y0)/256)
        road=np.zeros(shape,bool)
        road[:,128]=True
        cd=types.SimpleNamespace(bounds=bounds)
        lc=types.SimpleNamespace(sample=lambda lon,lat:
             np.full(np.asarray(lon).shape,v.WC["tree"],np.uint8))
        heights=np.zeros(shape,np.float32)
        instance=object.__new__(v.Vegetation)
        a=instance._scatter(cd,lc,road,chm,heights,road)
        b=instance._scatter(cd,lc,road,chm,heights,road)
        self.assertGreater(len(a),0)
        np.testing.assert_array_equal(a.group,b.group)
        self.assertTrue(np.any(a.group==v.G_NORWAY_FLOORCOVER))
        on_floor=a.group==v.G_NORWAY_FLOORCOVER
        self.assertTrue(np.all(v._near_road(
            a.lat[on_floor],a.lon[on_floor],road,chm,v.NORDIC_FLOORCOVER_NEAR_M)))
        # A floor-cover candidate is a converted old shrub, not an extra row.
        no_road=instance._scatter(cd,lc,road,chm,heights)
        self.assertEqual(len(a),len(no_road))
        self.assertFalse(np.any(no_road.group==v.G_NORWAY_FLOORCOVER))
        self.assertGreater(np.std(v._patch_field(
            np.linspace(x0,x1,75),np.full(75,(y0+y1)/2),
            v.NORDIC_PATCH_M,62.414,62)),0.01)

    def test_fallback_shapes_use_existing_beamng_assets(self):
        root = SERVER.parent
        lua = (root/"mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua").read_text()
        for group in ("norway_spruce","norway_pine","norway_birch","norway_spruce_far",
                      "norway_pine_far","norway_birch_far","norway_conifer_backdrop",
                      "norway_deciduous_backdrop","norway_floorcover"):
            self.assertIn(group+" =",lua)
        self.assertNotIn("bne_no_spruce_a",lua)
        self.assertNotIn("NO_TREES",lua)
        self.assertIn("tree_douglasfir_group_low",lua)
        pine_list=lua.split('norway_pine = {',1)[1].split('norway_birch = {',1)[0]
        self.assertIn('tree_douglasfir_large_d',pine_list)
        self.assertNotIn('pine_radiata',pine_list)
        self.assertNotIn('scots_pine',pine_list)
        self.assertIn("Clover_closetop",lua)
        self.assertIn("M.GROUP_MAX_M.norway_floorcover = 180",lua)
        self.assertIn("group == 'norway_birch'",lua)


if __name__ == "__main__":
    unittest.main()
