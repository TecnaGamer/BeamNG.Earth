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


def tile(frame, name, tenants, kind="supermarket", subtype="commercial"):
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
            [(ring, {"class": kind, "subtype": subtype, "name": name})],
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
    def test_catalog_matches_typed_cinemas_and_restaurant_aliases(self):
        from streamedstores import chain
        from store_match import brand
        self.assertEqual(chain("Regal Green Hills", cinema=True), "Regal Cinemas")
        self.assertIsNone(chain("Regal Nails"))
        self.assertEqual(brand({"amenity": "cinema", "name": "AMC CLASSIC Plaza 12"}), "AMC Theatres")
        self.assertIsNone(brand({"amenity": "cinema", "brand": "AMC", "drive_in": "yes"}))
        self.assertEqual(brand({"amenity": "fast_food", "name": "In-N-Out"}), "In-N-Out Burger")

    def test_parking_aisles_identify_customer_side(self):
        from streamedstores import parking_direction
        ring = np.array([[0., 0.], [80., 0.], [80., 60.], [0., 60.]])
        front = [np.array([[x, -8.], [x, -45.]]) for x in (20., 60.)]
        for footprint in (ring, ring[::-1]):
            np.testing.assert_allclose(parking_direction(footprint, front), [0., -1.])
            self.assertIsNone(parking_direction(footprint, front[:1]))
            self.assertIsNone(parking_direction(footprint, front + [line + [0, 100] for line in front]))

    def test_fuel_site_with_supermarket_brand(self):
        from streamedstores import businesses, mapped_tenants
        frame = geodesy.EnuFrame(0., 0.)
        md = tile(frame, "", ["Kroger"])
        place = md.cell(None).layers["places"]
        place.attrs["basic_category"] = np.array(["gas_station"])
        footprint = md.cell(None).layers["buildings"].lonlat
        self.assertIsNone(mapped_tenants(list(businesses(md, [None])), footprint)[0][0])
        place.attrs["basic_category"] = np.array(["supermarket"])
        self.assertEqual(mapped_tenants(list(businesses(md, [None])), footprint)[0][0], "Kroger")

    def test_shared_retail_panel_preserves_host_and_neighbour(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "", ["Kroger", "Other shop"], "retail")
        places = md.cell(None).layers["places"]
        lat, lon, _ = frame.enu_to_geodetic(np.array([50., 72.]), np.array([33., 55.]), np.zeros(2))
        places.lonlat[:] = np.column_stack((lon, lat))
        aisle = np.array([[50., 15.], [50., 20.]])
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids", return_value=[]), patch("buildings.road_lines", return_value=[aisle])):
            def generate():
                return buildings.road_tile_items(frame, 0, 0, md, lambda la, lo: np.zeros(np.shape(la)), cache_dir=cache)[1]
            panel, = generate()
            self.assertTrue(panel["local"]["storefront_overlay"])
            self.assertIsNone(panel["storefront_host"])
            places.lonlat[1] = places.lonlat[0]
            self.assertFalse(generate())

    def test_discovery_cache_and_ambiguous_tenants(self):
        for lat, lon, name, tenants, kind, subtype, expected in (
            (37., -120., "In-N-Out", [], "fast_food", "commercial", 1),
            (40., -83., "", ["AMC Theatres"], "cinema", "commercial", 1),
            (40., -83., "Best Buy", [], "retail", "commercial", 1),
            (36.1, -86.8, "Kroger", [], "supermarket", "commercial", 1),
            (39.0, -84.0, "Walmart Supercenter", ["Pharmacy", "ATM"], "retail", "commercial", 1),
            (41.9, -87.7, "", ["Wendy's"], "", "commercial", 1),
            (40.1, -83.1, "", ["Taco Bell"], "", "", 0),
            (34.0, -118.2, "", ["Walmart"], "retail", "commercial", 1),
            (13.7, 100.5, "", ["Walmart", "Pharmacy"], "", "commercial", 0),
        ):
            with (
                self.subTest(lat=lat),
                tempfile.TemporaryDirectory() as temp,
                patch.dict(os.environ, BNE_STOREFRONTS="1"),
            ):
                frame = geodesy.EnuFrame(lat, lon)
                md = tile(frame, name, tenants, kind, subtype)

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
