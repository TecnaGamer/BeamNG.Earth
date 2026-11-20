"""Exercise geographic storefront discovery through the road-tile entry point."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings
import geodesy
import mapgeom
import threedmr


def tile(frame, name, tenants):
    def features(layer, kind, rows):
        geoms = []
        for xy, _ in rows:
            xy = np.asarray(xy)
            lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            geoms.append((kind, [[np.column_stack((lon, lat))]]))
        f = mapgeom.build(layer, np.array([str(i) for i in range(len(rows))]), geoms)
        for key in rows[0][1]:
            f.attrs[key] = np.array([r[1][key] for r in rows])
        return f

    ring = [(30, 30), (75, 30), (75, 60), (30, 60), (30, 30)]
    layers = {
        "buildings": features(
            "buildings",
            mapgeom.POLYGON,
            [(ring, {"class": "supermarket", "name": name})],
        )
    }
    if tenants:
        layers["places"] = features(
            "places",
            mapgeom.POINT,
            [
                ([(40 + i, 40)], {"name": v, "confidence": 0.99})
                for i, v in enumerate(tenants)
            ],
        )
    return SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers=layers))


class StreamedStoresTest(unittest.TestCase):
    def test_discovery_cache_and_ambiguous_tenants(self):
        for lat, lon, name, tenants, expected in (
            (36.1, -86.8, "Kroger", [], 1),
            (34.0, -118.2, "", ["Walmart"], 1),
            (13.7, 100.5, "", ["Walmart", "Pharmacy"], 0),
        ):
            with (
                self.subTest(lat=lat),
                tempfile.TemporaryDirectory() as temp,
                patch.dict(os.environ, BNE_STOREFRONTS="1"),
            ):
                frame = geodesy.EnuFrame(lat, lon)
                md = tile(frame, name, tenants)

                def load():
                    return buildings.road_tile_items(
                        frame,
                        0,
                        0,
                        md,
                        lambda la, lo: np.zeros(np.shape(la)),
                        cache_dir=temp,
                    )

                with (
                    patch("threedmr.region_ids", return_value=[]),
                    patch(
                        "buildings.road_lines",
                        return_value=[np.array([[0.0, 15.0], [256.0, 15.0]])],
                    ),
                ):
                    items, custom = load()
                    self.assertEqual(len(custom), expected)
                    if expected:
                        entry = custom[0]["local"]
                        self.assertGreater(
                            len(threedmr.load_local_model(temp, entry)[2]), 0
                        )
                        with patch(
                            "streamedstores.export_store",
                            side_effect=AssertionError("cache miss"),
                        ):
                            self.assertEqual(load()[1][0]["local"], entry)
                        with patch(
                            "threedmr.local_models",
                            return_value=[dict(entry, id="manual")],
                        ):
                            self.assertEqual([v["id"] for v in load()[1]], ["manual"])
                        with patch.dict(os.environ, BNE_STOREFRONTS="0"):
                            self.assertFalse(load()[1])
