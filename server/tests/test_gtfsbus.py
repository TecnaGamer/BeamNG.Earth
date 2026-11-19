"""gtfsbus: Västtrafik's bus lines from its GTFS Regional feed (the user, 2026-11-17: "yes do the västtrafik data")."""
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import buslines  # noqa: E402
import gtfsbus  # noqa: E402

LON = 11.97


def lat_at(m):
    return 57.70 + m / 111_320.0


def feed(path):
    """A small feed: bus line 25 north (three trips one way, one short turn), back south once; a tram and a communal
    taxi (not lines). Its path runs due north along LON; stop B stands 100 m off it."""
    files = {
        "agency.txt": "agency_id,agency_name\n1,Västtrafik Göteborgsområdet\n",
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type,route_desc\n"
                      "r25,1,25,,700,\nr1,1,1,,900,\nrt,1,T,,1501,\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id,shape_id\n"
                     "r25,1,t1,,,0,s1\nr25,1,t2,,,0,s1\nr25,1,t3,,,0,s1\nr25,1,t4,,,0,s1\nr25,1,t5,,,1,s2\n"
                     "r1,1,m1,,,0,s1\nrt,1,x1,,,0,s1\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,platform_code\n"
                     f"a,Vasaplatsen,{lat_at(0)},{LON + 0.0001},0,,A\n"
                     f"b,Kapellplatsen,{lat_at(400)},{LON + 100 / 111_320 / 0.5348},0,,B\n"
                     f"c1,Chalmers,{lat_at(800)},{LON + 0.0001},0,,A\n"
                     f"c2,Chalmers,{lat_at(820)},{LON - 0.0001},0,,B\n"
                     f"d,Wavrinskys Plats,{lat_at(1200)},{LON},0,,A\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,pickup_type,"
                          "drop_off_type,shape_dist_traveled,timepoint\n"
                          + "".join(f"{t},,,{s},{k + 1},Wavrinskys Plats,0,0,{d},1\n" for t in ("t1", "t2", "t3")
                                    for k, (s, d) in enumerate((("a", 0), ("b", 400), ("c1", 800), ("c2", 820),
                                                                ("d", 1200))))
                          + "".join(f"t4,,,{s},{k + 1},Chalmers,0,0,{d},1\n" for k, (s, d) in enumerate((("a", 0),
                                                                                                       ("c1", 800))))
                          + "".join(f"t5,,,{s},{k + 1},Vasaplatsen,0,0,{d},1\n" for k, (s, d) in
                                    enumerate((("d", 0), ("a", 1200))))
                          + "m1,,,a,1,X,0,0,0,1\nm1,,,d,2,X,0,0,1200,1\nx1,,,a,1,X,0,0,0,1\nx1,,,d,2,X,0,0,1200,1\n",
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\n"
                      + "".join(f"s1,{lat_at(m)},{LON},{k + 1},{m}\n" for k, m in enumerate(range(0, 1201, 100)))
                      + "".join(f"s2,{lat_at(1200 - m)},{LON},{k + 1},{m}\n" for k, m in enumerate(range(0, 1201, 100))),
        "feed_info.txt": "feed_id,feed_publisher_name,feed_publisher_url,feed_lang,feed_version\nSE-vt,S,u,sv,2026-11-16\n",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = gtfsbus.build_index(feed(Path(self.tmp.name) / "vt.zip"))

    def test_bus_lines_only_the_way_most_trips_run(self):
        lines = {(l["ref"], l["id"].rsplit("-", 1)[1]): l for l in self.data["lines"]}
        self.assertEqual(set(lines), {("25", "0"), ("25", "1")}, "no tram, no communal taxi")
        north = lines[("25", "0")]
        self.assertEqual([s[0] for s in north["stops"]], ["Vasaplatsen", "Kapellplatsen", "Chalmers", "Wavrinskys Plats"],
                         "three trips' pattern, not the short turn's; Chalmers's two platforms once")
        self.assertEqual((north["from"], north["to"], north["operator"]),
                         ("Vasaplatsen", "Wavrinskys Plats", "Västtrafik Göteborgsområdet"))
        self.assertEqual(self.data["feed"], "2026-11-16")

    def test_stops_stand_on_the_buss_path(self):
        north = [l for l in self.data["lines"] if l["id"].endswith("-0")][0]
        by = {s[0]: s for s in north["stops"]}
        self.assertTrue(by["Vasaplatsen"][3])
        self.assertAlmostEqual(by["Vasaplatsen"][2], LON, 6, "on the path, not on the platform 6 m beside it")
        self.assertFalse(by["Kapellplatsen"][3], "100 m off the path: its own place")

    def test_near_in_buslines_form(self):
        got = gtfsbus.near(lat_at(1150), LON, self.data, 700, 40, buslines._ref_key)
        self.assertEqual(len(got), 2)
        north = [l for l in got if l["to"] == "Wavrinskys Plats"][0]
        self.assertEqual(north["near_i"], 3)
        self.assertEqual(north["stops"][0].keys(), {"name", "lat", "lon", "on_road"})
        self.assertEqual(north["source"], "vt")
        self.assertEqual(gtfsbus.near(lat_at(5000), LON, self.data, 700, 40, buslines._ref_key), [])


class Net:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.urls = body, error, []

    def get(self, url, kind=None, **_kw):
        self.urls.append(url)
        if self.error:
            raise self.error(f"{url}: HTTP 403")
        return mock.Mock(body=self.body)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.zip = feed(self.cache / "feed.zip").read_bytes()
        gtfsbus._state.update(index=None, loaded=None, building=False, failed_at=0.0, error=None)
        self.addCleanup(gtfsbus._state.update, index=None, loaded=None, building=False, failed_at=0.0, error=None)
        os.environ.pop("TRAFIKLAB_KEY", None)

    def test_without_a_key_the_public_copy(self):
        net = Net(self.zip)
        data = gtfsbus.refresh(self.cache, net)
        self.assertEqual(net.urls, [gtfsbus.MIRROR_URL])
        self.assertEqual(len(data["lines"]), 2)
        self.assertTrue(gtfsbus.index_path(self.cache).exists())
        self.assertEqual([p.name for p in gtfsbus.index_path(self.cache).parent.iterdir()], ["vt.json.gz"], "no zip kept")

    def test_with_a_key_trafiklab_and_the_key_never_in_a_message(self):
        (self.cache / "trafiklab.key").write_text("s3cret-key\n")
        net = Net(error=RuntimeError)
        with self.assertRaises(RuntimeError) as raised:
            gtfsbus.refresh(self.cache, net)
        self.assertEqual(net.urls, [gtfsbus.TRAFIKLAB_URL.format(key="s3cret-key")])
        self.assertNotIn("s3cret-key", str(raised.exception))
        self.assertIn("<key>", str(raised.exception))

    def test_first_lookup_waits_for_the_download_then_reads_the_disk(self):
        net = Net(self.zip)
        self.assertEqual(len(gtfsbus.index(self.cache, net)["lines"]), 2)
        gtfsbus._state.update(index=None, loaded=None)        # (a server restart)
        self.assertEqual(len(gtfsbus.index(self.cache, Net(error=RuntimeError))["lines"]), 2, "from the disk")
        self.assertEqual(len(net.urls), 1)

    def test_a_slow_download_overpass_meanwhile(self):
        gate = threading.Event()

        class Slow(Net):
            def get(self, url, kind=None, **kw):
                gate.wait(10)
                return super().get(url, kind, **kw)
        with mock.patch.object(gtfsbus, "FIRST_WAIT_S", 0.2):
            with self.assertRaises(gtfsbus.Preparing):
                gtfsbus.index(self.cache, Slow(self.zip))
        gate.set()
        for t in threading.enumerate():
            if t.name == "vt-feed":
                t.join(30)

    def test_a_failed_download_says_why(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            gtfsbus.index(self.cache, Net(error=RuntimeError))


class BusLinesTest(unittest.TestCase):
    """buslines.near: Västtrafik's lines in its area; Overpass while they download."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = gtfsbus.build_index(feed(Path(tmp.name) / "vt.zip"))

    def test_in_the_area_its_lines(self):
        with mock.patch.object(gtfsbus, "index", return_value=self.data), \
                mock.patch.object(buslines, "_overpass") as overpass:
            got = buslines.near(lat_at(100), LON, mock.Mock(), cache_dir="x")
        overpass.assert_not_called()
        self.assertEqual(buslines.attribution(got), gtfsbus.ATTRIBUTION)
        self.assertEqual(got[0]["colour"], buslines.DEFAULT_COLOUR)

    def test_while_they_download_overpass(self):
        with mock.patch.object(gtfsbus, "index", side_effect=gtfsbus.Preparing("downloading")), \
                mock.patch.object(buslines, "_overpass", return_value=[{"ref": "1"}]) as overpass:
            self.assertEqual(buslines.near(lat_at(100), LON, mock.Mock(), cache_dir="x"), [{"ref": "1"}])
        overpass.assert_called_once()
        with mock.patch.object(gtfsbus, "index", side_effect=gtfsbus.Preparing("downloading")), \
                mock.patch.object(buslines, "_overpass", side_effect=buslines.Busy("busy")):
            with self.assertRaisesRegex(buslines.Busy, "downloading"):
                buslines.near(lat_at(100), LON, mock.Mock(), cache_dir="x")

    def test_outside_the_area_overpass(self):
        with mock.patch.object(gtfsbus, "index") as idx, mock.patch.object(buslines, "_overpass", return_value=[]):
            buslines.near(55.6, 13.0, mock.Mock(), cache_dir="x")        # (Malmö)
        idx.assert_not_called()
        self.assertEqual(buslines.attribution([]), buslines.ATTRIBUTION)

    def test_inside_rectangle_without_nearby_regional_service_uses_osm(self):
        with mock.patch.object(gtfsbus, "covers", return_value=True), \
                mock.patch.object(gtfsbus, "index", return_value={"lines": []}), \
                mock.patch.object(buslines, "_overpass", return_value=[{"ref": "Local"}]) as overpass:
            self.assertEqual(buslines.near(57.7, 11.97, mock.Mock(), cache_dir="x"), [{"ref": "Local"}])
        overpass.assert_called_once()


if __name__ == "__main__":
    unittest.main()
