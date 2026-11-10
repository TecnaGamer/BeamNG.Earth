"""placenames: the status bar's town, state and country from our own copy of Overture's divisions (GitHub #602: no
outside geocoder). pick() on synthetic towns and areas; the cells and their files; the Overture layers through the
synthetic release (mapfixtures: no network); earthd's where() asks no outside service."""
import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geocode  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import overture  # noqa: E402
import placenames as pn  # noqa: E402
import rangeread  # noqa: E402

KM_LAT = 1000.0 / pn.M_PER_DEG          # degrees of latitude in a km
LAT, LON = 39.2904, -76.6122


def town(name, dkm_north, dkm_east=0.0, klass="town", pop=np.nan, country="US", region="US-MD", tid=None,
         subtype="locality", **names):
    """One town row dkm_north / dkm_east km from (LAT, LON)."""
    return dict(lat=LAT + dkm_north * KM_LAT, lon=LON + dkm_east * KM_LAT / np.cos(np.radians(LAT)), population=pop,
                id=tid or f"id-{name}", subtype=subtype, **{"class": klass}, country=country, region=region, name=name,
                **{f"name_{lang}": names.get(lang, "") for lang in overture.DIVISION_LANGS})


def area(name, subtype, country, region="", box=(-80.0, 37.0, -75.0, 40.0), did=None, **names):
    x0, y0, x1, y1 = box
    return dict(subtype=subtype, country=country, region=region, division_id=did or f"d-{name}", x0=x0, y0=y0,
                x1=x1, y1=y1, name=name, **{f"name_{lang}": names.get(lang, "") for lang in overture.DIVISION_LANGS})


def table(rows):
    if not rows:
        return {}
    out = {}
    for k in rows[0]:
        vals = [r[k] for r in rows]
        out[k] = np.array(vals, np.float64) if isinstance(vals[0], float) else np.array(vals).astype(str)
    return out


MD = area("Maryland", "region", "US", "US-MD")
PA = area("Pennsylvania", "region", "US", "US-PA", box=(-80.6, 39.7, -74.7, 42.3))
US = area("United States", "country", "US", box=(-180.0, 18.0, 180.0, 72.0), de="Vereinigte Staaten")


class PickTest(unittest.TestCase):
    def pick(self, towns, areas=(MD, PA, US), lat=LAT, lon=LON):
        return pn.pick(table(list(towns)), table(list(areas)), lat, lon)

    def test_nearest_town(self):
        got = self.pick([town("Near", 0.3, pop=8000.0), town("Far", 2.5, pop=8000.0)])
        self.assertEqual(got, {"place": "Near", "region": "Maryland", "country": "United States"})

    def test_a_city_wins_over_a_nearer_hamlet(self):
        towns = [town("Hamlet", 1.0, klass="hamlet"), town("City", -4.0, klass="city", pop=585000.0)]
        self.assertEqual(self.pick(towns)["place"], "City")
        # ... but standing in the hamlet, far from the city's centre, it is the hamlet
        towns = [town("Hamlet", 0.1, klass="hamlet"), town("City", -14.0, klass="city", pop=585000.0)]
        self.assertEqual(self.pick(towns)["place"], "Hamlet")

    def test_population_over_class_default(self):
        # a "town" of 300 000 reaches farther than a "city" without population (100 000 assumed)
        towns = [town("BigTown", 6.0, pop=300000.0), town("City", -5.0, klass="city")]
        self.assertEqual(self.pick(towns)["place"], "BigTown")

    def test_districts_only_when_no_town_reaches(self):
        # Köln and its district Nippes (a locality without class in Overture)
        towns = [town("Köln", 5.0, klass="city", pop=1069000.0, country="DE", region="DE-NW", de="Köln"),
                 town("Nippes", 0.5, klass="", pop=117000.0, country="DE", region="DE-NW")]
        areas = [area("Nordrhein-Westfalen", "region", "DE", "DE-NW", box=(5.8, 50.3, 9.5, 52.6)),
                 area("Deutschland", "country", "DE", box=(5.8, 47.2, 15.1, 55.1), en="Germany", de="Deutschland")]
        self.assertEqual(self.pick(towns, areas)["place"], "Köln")
        self.assertEqual(self.pick(towns[1:], areas)["place"], "Nippes")

    def test_out_of_reach_no_place_but_region_and_country(self):
        got = self.pick([town("Hoisington", 4.3, pop=2832.0)])
        self.assertEqual(got, {"place": "", "region": "Maryland", "country": "United States"})

    def test_overlapping_boxes_take_the_nearest_towns_region(self):
        lat = 39.8          # inside both Maryland's and Pennsylvania's boxes
        towns = [town("PaTown", (lat - LAT) / KM_LAT + 30.0, region="US-PA", pop=500.0)]
        self.assertEqual(self.pick(towns, lat=lat)["region"], "Pennsylvania")
        towns = [town("MdTown", (lat - LAT) / KM_LAT - 30.0, pop=500.0)]
        self.assertEqual(self.pick(towns, lat=lat)["region"], "Maryland")

    def test_no_data_is_empty(self):
        self.assertEqual(pn.pick({}, {}, LAT, LON), pn.EMPTY)
        self.assertEqual(self.pick([]), pn.EMPTY)        # the open sea: boxes alone say nothing
        self.assertEqual(pn.pick(table([town("Near", 0.2)]), {}, LAT, LON),
                         {"place": "Near", "region": "", "country": ""})

    def test_names_in_the_countrys_language(self):
        self.assertEqual(geocode.lang_for("DE"), "de")
        towns = [town("Köln", 0.2, klass="city", pop=1069000.0, country="DE", region="DE-NW", en="Cologne",
                      de="Köln")]
        areas = [area("Nordrhein-Westfalen", "region", "DE", "DE-NW", en="North Rhine-Westphalia"),
                 area("Deutschland", "country", "DE", en="Germany", de="Deutschland")]
        self.assertEqual(self.pick(towns, areas), {"place": "Köln", "region": "Nordrhein-Westfalen",
                                                   "country": "Deutschland"})
        # English elsewhere where Overture has it, else the primary name
        towns = [town("東京都", 0.2, klass="city", pop=13600000.0, country="JP", region="JP-13", en="Tokyo")]
        areas = [area("東京都", "region", "JP", "JP-13", en="Tokyo"), area("日本", "country", "JP", en="Japan")]
        self.assertEqual(self.pick(towns, areas), {"place": "Tokyo", "region": "Tokyo", "country": "Japan"})
        towns = [town("Selfoss", 0.5, pop=11000.0, country="IS", region="IS-8")]
        areas = [area("Suðurland", "region", "IS", "IS-8"), area("Ísland", "country", "IS", en="Iceland")]
        self.assertEqual(self.pick(towns, areas), {"place": "Selfoss", "region": "Suðurland", "country": "Iceland"})

    def test_a_dependency_before_its_country(self):
        towns = [town("San Juan", 0.2, klass="city", pop=340000.0, country="PR", region="")]
        areas = [area("Puerto Rico", "dependency", "PR"), area("United States", "country", "US")]
        self.assertEqual(self.pick(towns, areas)["country"], "Puerto Rico")

    def test_deterministic(self):
        towns = [town(f"T{i}", float(i % 7) - 3.0, float(i % 5) - 2.0, pop=float(1000 + 37 * i)) for i in range(40)]
        towns.append(dict(towns[3], id="id-twin-a", name="TwinA"))      # two towns at one spot: by id
        towns.append(dict(towns[3], id="id-twin-b", name="TwinB"))
        rng = np.random.default_rng(5)
        want = [self.pick(towns, lat=LAT + k * 0.01, lon=LON + k * 0.013) for k in range(-4, 5)]
        for _ in range(5):
            order = rng.permutation(len(towns))
            got = [self.pick([towns[i] for i in order], [MD, PA, US][::-1], lat=LAT + k * 0.01, lon=LON + k * 0.013)
                   for k in range(-4, 5)]
            self.assertEqual(got, want)

    def test_reach(self):
        r = pn.reach_km([585708.0, np.nan, 0.0, 2e8], ["city", "hamlet", "village", "megacity"])
        self.assertAlmostEqual(r[0], 2 * 0.68 * 585.708 ** 0.4, places=6)       # Baltimore: ~17 km
        self.assertEqual((r[1], r[3]), (pn.REACH_MIN_KM, pn.REACH_MAX_KM))
        self.assertEqual(r[2], pn.REACH_MIN_KM)                                   # (0 = unknown: its class's)

    def test_across_the_antimeridian(self):
        towns = [town("East", 0.0, klass="city", pop=50000.0, country="FJ", region="")]
        towns[0]["lon"] = 179.99
        got = pn.pick(table(towns), table([area("Fiji", "country", "FJ", box=(-180.0, -21.0, 180.0, -12.0))]),
                      LAT, -179.99)
        self.assertEqual(got["place"], "East")


class CellsTest(unittest.TestCase):
    def test_cells_round_the_player(self):
        cells = pn.cells_for(LAT, LON)
        self.assertTrue(1 <= len(cells) <= 4)
        self.assertIn(mapcells.cell_of(LAT, LON, pn.ZOOM), cells)
        self.assertTrue(all(c.z == pn.ZOOM for c in cells))
        # the antimeridian: columns on both sides
        xs = {c.x for c in pn.cells_for(0.0, 179.99)}
        self.assertIn(0, xs)
        self.assertIn((1 << pn.ZOOM) - 1, xs)
        # towards the poles coarser cells: still a few per lookup, ~35-80 km wide
        for lat in (63.93, 70.0, 78.0, 84.9, 89.9, -85.0):
            cells = pn.cells_for(lat, 10.0)
            self.assertLessEqual(len(cells), 4, lat)
            lon0, lat0, lon1, lat1 = cells[0].bounds()
            w_km = (lon1 - lon0) * pn.M_PER_DEG * np.cos(np.radians(min(abs(lat), mapcells.MAX_LAT))) / 1000
            self.assertTrue(30 < w_km < 100, (lat, w_km))
        self.assertEqual(pn.zoom_at(LAT), pn.ZOOM)


class FakeReader:
    """overture.Reader.query for the two divisions layers of one synthetic cell set."""

    def __init__(self):
        self.calls = []

    def query(self, layer, key, kind="demand"):
        import mapgeom as mg
        self.calls.append((layer, key, kind))
        if layer == "divisions":
            rows = [town("Baltimore", 0.0, klass="city", pop=585708.0)] if key == mapcells.cell_of(LAT, LON, pn.ZOOM) \
                else []
            f = mg.build("divisions", mg.text_column([r["id"] for r in rows]),
                         [(mg.POINT, [[np.array([[r["lon"], r["lat"]]])]]) for r in rows])
            f.attrs["cx"] = np.array([r["lon"] for r in rows], np.float64)
            f.attrs["cy"] = np.array([r["lat"] for r in rows], np.float64)
            f.attrs["population"] = mg.float_column([r["population"] for r in rows])
            for c in pn.TOWN_COLS:
                f.attrs[c] = mg.text_column([r[c] for r in rows])
            return f, {("OpenStreetMap", "ODbL-1.0"): len(rows)}
        if layer == "division_boundaries":
            return mg.build("division_boundaries", mg.text_column([]), []), {}
        rows = [MD, US]
        f = mg.build("division_areas", mg.text_column([r["division_id"] for r in rows]),
                     [(mg.POINT, [[np.array([[0.0, 0.0]])]]) for _ in rows])
        for c in ("x0", "y0", "x1", "y1"):
            f.attrs[c] = mg.float_column([r[c] for r in rows])
        for c in pn.AREA_COLS:
            f.attrs[c] = mg.text_column([r[c] for r in rows])
        return f, {}


class PlaceNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="placenames-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fetched_once_kept_on_disk(self):
        r = FakeReader()
        p = pn.PlaceNames(self.tmp, None, reader=r)
        self.assertFalse(p.ready(LAT, LON))
        want = {"place": "Baltimore", "region": "Maryland", "country": "United States"}
        self.assertEqual(p.lookup(LAT, LON), want)
        cells = pn.cells_for(LAT, LON)
        self.assertEqual(len(r.calls), 3 * len(cells))      # (towns, areas, borders)
        self.assertTrue(all(kind == "background" for _, _, kind in r.calls))
        self.assertTrue(p.ready(LAT, LON))
        for k in cells:
            self.assertTrue(os.path.exists(p.path(k)))
            self.assertIn(f"f{pn.PLACES_FORMAT}", p.path(k))
        p.lookup(LAT + 0.001, LON)
        self.assertEqual(len(r.calls), 3 * len(cells))          # (memory)
        r2 = FakeReader()
        p2 = pn.PlaceNames(self.tmp, None, reader=r2)
        self.assertTrue(p2.ready(LAT, LON))
        self.assertEqual(p2.lookup(LAT, LON), want)             # (disk)
        self.assertEqual(r2.calls, [])

    def test_fetch_errors_raised_and_nothing_written(self):
        class Busy(FakeReader):
            def query(self, layer, key, kind="demand"):
                raise netfetch.Deferred("busy", "u", "h")
        p = pn.PlaceNames(self.tmp, None, reader=Busy())
        with self.assertRaises(netfetch.Deferred):
            p.lookup(LAT, LON)
        self.assertFalse(p.ready(LAT, LON))

    def test_a_damaged_file_is_fetched_again(self):
        r = FakeReader()
        p = pn.PlaceNames(self.tmp, None, reader=r)
        k = mapcells.cell_of(LAT, LON, pn.ZOOM)
        os.makedirs(os.path.dirname(p.path(k)), exist_ok=True)
        with open(p.path(k), "wb") as f:
            f.write(b"not a zip")
        self.assertEqual(p.lookup(LAT, LON)["place"], "Baltimore")


try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = rangeread.NetfetchFS is not None
except ImportError:
    HAVE_DUCKDB = False


@unittest.skipUnless(HAVE_DUCKDB, "duckdb / fsspec not installed (server/requirements.txt)")
class OvertureDivisionsTest(unittest.TestCase):
    """The divisions layers through overture.Reader on the synthetic release: no geometry read, the towns' filter,
    names.common's languages, and PlaceNames end to end."""

    @classmethod
    def setUpClass(cls):
        import mapfixtures as mf
        cls.mf = mf
        cls.fix_dir = tempfile.mkdtemp(prefix="placenames-fix-")
        cls.files, cls.names = mf.release(cls.fix_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fix_dir, ignore_errors=True)

    def setUp(self):
        self._saved = (rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP)
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = 4096, 8192, 1024
        self.tmp = tempfile.mkdtemp(prefix="placenames-test-")

    def tearDown(self):
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_layers(self):
        mf = self.mf
        t = mf.Transport(self.files)
        r = overture.Reader(os.path.join(self.tmp, "raw"), mf.fetcher(t), mf.RELEASE)
        self.addCleanup(r.close)
        key = mapcells.cell_of(*mf.CELL.center(), pn.ZOOM)
        towns, src = r.query("divisions", key)
        self.assertEqual(list(towns.ids), ["div-baltimore"])            # (the neighbourhood left out)
        lat, lon = mf._cell_xy(0.5, 0.5)[::-1]
        self.assertAlmostEqual(float(towns.attrs["cy"][0]), lat, places=9)
        self.assertEqual(towns.lonlat.tolist(), [[lon, lat]])            # a point at its bbox centre
        self.assertEqual((towns.attrs["name"][0], towns.attrs["name_en"][0], towns.attrs["name_it"][0],
                          towns.attrs["name_de"][0], towns.attrs["class"][0], towns.attrs["region"][0]),
                         ("Baltimore", "Baltimore", "Baltimora", "", "city", "US-MD"))
        self.assertEqual(float(towns.attrs["population"][0]), 585708.0)
        areas, _ = r.query("division_areas", key)
        self.assertEqual(sorted(areas.attrs["name"].tolist()), ["Maryland", "United States"])   # (no county)
        self.assertEqual(sorted(areas.attrs["name_de"].tolist()), ["", "Vereinigte Staaten"])

    def test_no_geometry_read(self):
        """The geometry column chunks are never fetched for the divisions layers (division_area's are ~10 MB a
        row group)."""
        spec = overture.LAYERS["division_areas"]
        self.assertIs(spec["geometry"], False)
        self.assertEqual(overture._wkb(spec), "NULL::BLOB")
        self.assertEqual(overture._wkb(overture.LAYERS["roads"]), "ST_AsWKB(geometry)")
        mf = self.mf
        t = mf.Transport(self.files)
        r = overture.Reader(os.path.join(self.tmp, "raw"), mf.fetcher(t), mf.RELEASE)
        self.addCleanup(r.close)
        key = mapcells.cell_of(*mf.CELL.center(), pn.ZOOM)
        r.query("division_areas", key)
        import duckdb
        name = self.names[("divisions", "division_area")][0]
        path = os.path.join(self.fix_dir, "divisions-division_area.parquet")
        geo = duckdb.connect().execute(f"""SELECT min(coalesce(dictionary_page_offset, data_page_offset)),
            max(coalesce(dictionary_page_offset, data_page_offset) + total_compressed_size)
            FROM parquet_metadata('{path}') WHERE path_in_schema = 'geometry'""").fetchone()
        for u, rng in t.log:
            if name in u and rng:
                a, b = (int(v) for v in rng.split("=")[1].split("-"))
                self.assertFalse(a <= geo[0] and b >= geo[1] - 1, (a, b, geo))

    def test_place_names_end_to_end(self):
        mf = self.mf
        t = mf.Transport(self.files)
        p = pn.PlaceNames(os.path.join(self.tmp, "cache"), mf.fetcher(t), release=mf.RELEASE)
        lat, lon = mf.CELL.center()
        self.assertEqual(p.lookup(lat, lon), {"place": "Baltimore", "region": "Maryland",
                                              "country": "United States"})
        n = t.count()
        p2 = pn.PlaceNames(os.path.join(self.tmp, "cache"), mf.fetcher(t), release=mf.RELEASE)
        self.assertEqual(p2.lookup(lat, lon)["place"], "Baltimore")
        self.assertEqual(t.count(), n)                                   # (from disk)
        self.assertFalse(any("photon" in u for u, _ in t.log))


class WhereTest(unittest.TestCase):
    """earthd's 'where': the place parts from placenames (a second where_result once a missing region is fetched);
    nothing asked of an outside geocoder."""

    def run_where(self, msg, places, ready=True):
        import earthd
        sent = []

        class FakePlaces:
            def __init__(self):
                self.asked = []

            def ready(self, lat, lon):
                return ready

            def lookup(self, lat, lon, kind="background"):
                self.asked.append((lat, lon, kind))
                return places

        fp = FakePlaces()

        class NoNet:
            def get(self, url, **kw):
                raise AssertionError(f"no request expected: {url}")

        async def run():
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(1)
            s = types.SimpleNamespace(surface=None, ui_pool=pool, place_pool=pool, net=NoNet(),
                                      place_names=lambda: fp)
            tasks = []
            conn = types.SimpleNamespace(s=s, send=sent.append,
                                         spawn=lambda c: tasks.append(asyncio.ensure_future(c)))
            conn._where_place_later = lambda *a: earthd.Connection._where_place_later(conn, *a)
            await earthd.Connection.where(conn, msg)
            for t in tasks:
                await t
            pool.shutdown()
        asyncio.run(run())
        return sent, fp.asked

    def test_place_from_local_data(self):
        place = {"place": "Baltimore", "region": "Maryland", "country": "United States"}
        sent, asked = self.run_where({"id": 3, "lat": LAT, "lon": LON, "online": True}, place)
        self.assertEqual(sent, [dict(type="where_result", id=3, street="", **place)])
        self.assertEqual(len(asked), 1)

    def test_not_due_no_place(self):
        sent, asked = self.run_where({"id": 4, "lat": LAT, "lon": LON, "online": False}, {})
        self.assertEqual(sent, [{"type": "where_result", "id": 4, "street": ""}])
        self.assertEqual(asked, [])

    def test_missing_region_comes_after(self):
        place = {"place": "Selfoss", "region": "Southern Region", "country": "Iceland"}
        sent, asked = self.run_where({"id": 5, "lat": 63.933, "lon": -20.9976, "online": True}, place, ready=False)
        self.assertEqual(sent, [{"type": "where_result", "id": 5, "street": ""},
                                dict(type="where_result", id=5, street="", **place)])
        self.assertEqual(asked, [(63.933, -20.9976, "background")])

    def test_no_photon_reverse_left(self):
        for name in ("reverse", "reverse_url", "REVERSE_URL", "parse_place"):
            self.assertFalse(hasattr(geocode, name), name)


if __name__ == "__main__":
    unittest.main()
