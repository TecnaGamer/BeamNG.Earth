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
    def test_shared_retail_wall_requires_overlap_and_low_height(self):
        ring = np.array([[0.,0.],[12.,0.],[12.,10.],[0.,10.]])
        shop = {"_storefront": True, "class": "shop"}
        unit = {"_storefront": True, "height": 5.2}
        for shift, attrs, expected in [([12,0], unit, {0,1}), ([12,9], unit, {0}),
                                      ([13,0], unit, {0}), ([12,0], dict(unit,num_floors=2), {0})]:
            self.assertEqual(buildings._retail_frontages([ring,ring+shift],[shop,attrs]), expected)

    def test_retail_glass_is_one_ground_floor_band_at_both_detail_levels(self):
        ring = np.array([[0.,0.], [31.,0.], [31.,16.], [0.,16.]])
        spec = buildings.plan(ring, {"class": "retail", "_retail_front": True,
                                     "height": 5.2, "num_floors": 1, "roof_shape": "flat"},
                              0, 40., -83.)
        spec.retail_front, spec.front = True, 0
        for lite in (False, True):
            mesh = buildings.Mesh()
            buildings._walls(mesh, spec, ring, -1., spec.height, buildings._Looks(spec.seed), lite)
            points, uv, triangles, materials, _ = mesh.arrays()
            glass = triangles[materials == buildings.MATERIALS.index(buildings.LOBBY)]
            self.assertEqual(len(glass), 2)
            np.testing.assert_allclose(np.ptp(points[glass].reshape(-1,3),axis=0), [28.8,0.,3.15])
            self.assertFalse(np.any(materials == buildings.MATERIALS.index(buildings.SHOP)))

    def test_optional_local_sign_uses_mapped_name_and_keeps_host(self):
        frame = geodesy.EnuFrame(40., -83.)
        for category, name in (("car_wash", "River Wash"), ("smoke_shop", "Northside Smoke"),
                               ("gas_station", "East End Fuel"), ("retail", "Olive Books")):
            md = tile(frame, "", [name], "retail")
            md.cell(None).layers["places"].attrs["basic_category"] = np.array([category])
            with (self.subTest(category=category), tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="1"),
                  patch("threedmr.region_ids", return_value=[]),
                  patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])])):
                def generate():
                    return buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                     cache_dir=cache)[1]
                entry, = generate()
                self.assertTrue(entry["local"]["storefront_overlay"])
                self.assertEqual(entry["local"]["title"], name)
                self.assertIsNone(entry["storefront_host"])
                model = threedmr.load_local_model(cache, entry["local"])
                self.assertEqual(len(model[2]), 4)
                with patch.dict(os.environ, BNE_LOCAL_BUSINESS_SIGNS="0"):
                    self.assertFalse(generate())

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

    def test_mapped_doors_reach_the_export_and_change_its_cache_key(self):
        import streamedstores
        from build_stores import export_store
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "Walmart", [], "supermarket")
        layer = md.cell(None).layers["buildings"]
        layer.attrs["src_record"] = np.array(["w42@7"])
        # Customer doors on the far wall, a path at the road, an obsolete interior point.
        lat, lon, _ = frame.enu_to_geodetic(np.array([40.,65.,48.,50.]),
                                            np.array([60.,60.,30.,44.]), np.zeros(4))
        rows = list(zip(lon,lat,(1,1,0,1)))
        index = {"w42": rows}
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids", return_value=[]),
              patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])]),
              patch.object(streamedstores, "entrance_index", return_value=index),
              patch.object(streamedstores, "export_store", wraps=export_store) as export):
            def generate():
                return buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                 cache_dir=cache)[1][0]["local"]["file"]
            first = generate()
            match = export.call_args.args[1]
            self.assertLess(match[3][1], -.9)  # Road still faces south.
            np.testing.assert_allclose([p for p, dock in match[4]], sorted(zip(lon[:2],lat[:2])))
            index["w42"] = rows[2:]
            self.assertNotEqual(first, generate())
            np.testing.assert_allclose(export.call_args.args[1][4][0][0], [lon[2],lat[2]])
            layer.attrs["src_record"] = np.array(["w43@7"])
            generate()
            self.assertEqual(export.call_args.args[1][4], [])

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

    def test_named_retail_host_requires_tenant_frontage(self):
        frame = geodesy.EnuFrame(40., -83.)
        aisle = np.array([[50., 15.], [50., 20.]])
        for host, labels in (("Town Square", ["Kroger"]), ("Walmart", ["Kroger", "Walmart"]),
                             ("Town Square", ["Kroger", "Other shop"])):
            with (self.subTest(host=host, labels=labels), tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ, BNE_STOREFRONTS="1"), patch("threedmr.region_ids", return_value=[]),
                  patch("buildings.road_lines", return_value=[aisle])):
                md = tile(frame, host, labels, "retail")
                points = md.cell(None).layers["places"].lonlat
                lat, lon, _ = frame.enu_to_geodetic(np.array([50., 72.]), np.array([33., 55.]), np.zeros(2))
                points[:] = np.column_stack((lon, lat))[:len(points)]
                _, generated = buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                         cache_dir=cache)
                self.assertEqual([m["local"]["title"] for m in generated], ["Kroger"])
                self.assertTrue(generated[0]["local"]["storefront_overlay"])
                self.assertIsNone(generated[0]["storefront_host"])

    def test_mapped_pylon_owns_one_tile_and_survives_a_revisit(self):
        import streamedstores
        import storefront_totems
        frame = geodesy.EnuFrame(38., -90.)
        lat, lon, _ = frame.enu_to_geodetic(28., 20., 0.)
        row = (float(lat), float(lon), "12345", "Burger King", 12., None)
        md = tile(frame, "Burger King", [], "fast_food")
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids", return_value=[]),
              patch.object(storefront_totems, "rows", return_value=[row]),
              patch("buildings.road_lines", return_value=[np.array([[0., 15.], [256., 15.]])])):
            def generate(tx=0):
                return buildings.road_tile_items(frame, tx, 0, md, lambda a,b: np.zeros(np.shape(a)), cache_dir=cache)[1]
            with (patch.object(streamedstores, "export_store", side_effect=OSError("temporary disk failure")),
                  patch("revisit.fault") as fault):
                self.assertFalse(generate())
                self.assertEqual(fault.call_count, 2)
            sign, building = generate()
            self.assertEqual(sign["id"], "osm_storefront_totem_12345")
            self.assertTrue(sign["local"]["storefront_pylon"])
            self.assertEqual(building["local"]["title"], "Burger King")
            verts = threedmr.load_local_model(cache, sign["local"])[0]
            self.assertAlmostEqual(float(np.ptp(verts[:, 2])), 12.)
            self.assertGreater(np.ptp(verts[:, 1]), np.ptp(verts[:, 0]))
            root = Path(cache) / "threedmr_cache"
            (root / building["local"]["file"]).with_suffix(".json").write_text("{")
            (root / sign["local"]["file"]).write_bytes(b"glTF")
            regenerated = generate()
            self.assertEqual([m["local"] for m in regenerated], [sign["local"], building["local"]])
            self.assertGreater(len(threedmr.load_local_model(cache, sign["local"])[2]), 0)
            with patch.object(streamedstores, "export_store", side_effect=AssertionError("cache miss")):
                self.assertEqual(generate()[0]["local"], sign["local"])
            self.assertFalse(any(item["id"] == sign["id"] for item in generate(1)))
            with patch("threedmr.local_models", return_value=[dict(sign["local"], id="installed")]):
                self.assertEqual([item["id"] for item in generate() if item["local"].get("storefront_pylon")], ["installed"])
            with patch.dict(os.environ, BNE_STOREFRONTS="0"):
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
