"""Arriving optional cached POIs invalidate generated decoration in both road-tile formats."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import development
import geodesy
import geotiles


class DecorationCacheTest(unittest.TestCase):
    LAT, LON = 41.13998, -104.8202

    def mapdata(self, directory):
        def path(key, layer):
            return str(Path(directory) / f"{key.z}_{key.x}_{key.y}_{layer}.npz")

        def forbidden(*args, **kwargs):
            raise AssertionError("cache-key checks must not request map data")

        return SimpleNamespace(path=path, versions=lambda: {"overture": "fixture"}, cell=forbidden, warm=forbidden)

    def test_anchor_road_key_changes_when_optional_pois_arrive_or_change(self):
        frame = geodesy.EnuFrame(self.LAT, self.LON)
        with tempfile.TemporaryDirectory() as directory:
            md = self.mapdata(directory)
            with mock.patch.object(blockworker, "_s", SimpleNamespace(md=md)), \
                    mock.patch.object(blockworker, "_revisit_tile", side_effect=lambda kind, key, *args: key):
                def key():
                    return blockworker._roads_tile_cached(1, self.LAT, self.LON, 16, 0, 0, directory, "/x",
                                                          True, 0., True, False, True, None, False, 0., "na:sparse", "US")
                before = key()
                self.assertEqual(before, key())
                path = Path(md.path(development._keys(frame, 0, 0)[0], "places"))
                path.write_bytes(b"first cached POIs")
                arrived = key()
                self.assertNotEqual(before, arrived)
                path.write_bytes(b"updated cached POIs with an extra shop")
                self.assertNotEqual(arrived, key())

    def test_server_and_worker_geotile_keys_share_optional_source_stamps(self):
        gx, gy = geotiles.tile_of(self.LAT, self.LON)
        tile = geotiles.geobox(gx, gy)
        with tempfile.TemporaryDirectory() as directory:
            md = self.mapdata(directory)
            with mock.patch.object(blockworker, "_s", SimpleNamespace(md=md)):
                args = (tile.key, 16, True, False, True, False, 0., "na:sparse", "US")
                worker = blockworker._geo_content_key(*args)
                parent = blockworker._geo_content_key(*args, md=md.versions(),
                                                     development_key=development.cache_key(md, tile.frame, tile, None))
                self.assertEqual(worker, parent)
                Path(md.path(development._keys(tile.frame, tile, None)[0], "places")).write_bytes(b"cached POIs")
                changed = blockworker._geo_content_key(*args)
                self.assertNotEqual(worker, changed)
                self.assertEqual(changed, blockworker._geo_content_key(
                    *args, md=md.versions(), development_key=development.cache_key(md, tile.frame, tile, None)))


if __name__ == "__main__":
    unittest.main()
