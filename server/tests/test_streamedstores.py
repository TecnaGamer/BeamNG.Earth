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
from storeidentity import identity_for, source_key


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
    def test_previous_generated_packs_and_catalog_cache_keys(self):
        import json
        import streamedstores
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, BNE_STOREFRONTS="1"):
            cache = Path(tmp) / "threedmr_cache"
            cache.mkdir()
            manual = {"file": "manual.glb", "lat": 36., "lon": -86.}
            old = dict(manual, file="old.glb", branded_storefront=True, storefront_source="district.osm")
            current = dict(old, file="current.glb", fictional_identity="tandem_goods", storefront_edition=2)
            (cache / "local_models.json").write_text(json.dumps([manual, old, current]))
            self.assertEqual([e["file"] for e in threedmr.local_models(tmp)], ["manual.glb", "current.glb"])
            panel = np.array([[0., 0.], [.0001, 0.]])
            first = streamedstores.cached_entry(cache, "Target", panel, None, None)
            signature = threedmr.local_signature(tmp)
            with patch("storeidentity.CATALOG_DIGEST", "revised"), patch.object(streamedstores, "CATALOG_DIGEST", "revised"):
                second = streamedstores.cached_entry(cache, "Target", panel, None, None)
                self.assertNotEqual(first["file"], second["file"])
                self.assertNotEqual(signature, threedmr.local_signature(tmp))

    def test_carhop_row_needs_a_separate_footprint_and_access_lane(self):
        from streamedstores import businesses, cached_entry, carhop_approach
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "", ["Sonic Drive In"])
        points = list(businesses(md, [None]))
        ring = np.array([[27.,18.],[52.,18.],[52.,23.],[27.,23.]])
        lat, lon, _ = frame.enu_to_geodetic(ring[:,0], ring[:,1], np.zeros(4))
        coords = np.column_stack((lon, lat))
        lane = np.array([[26.,27.],[55.,27.]])
        self.assertIsNone(carhop_approach(points, coords, ring, lambda: []))
        self.assertIsNone(carhop_approach([], coords, ring, lambda: [lane]))
        self.assertIsNone(carhop_approach(points, coords, ring, lambda: [lane-[0,9]]))
        owner, approach = carhop_approach(points, coords, ring, lambda: [lane])
        self.assertEqual(owner, "Sonic Drive-In")
        np.testing.assert_allclose(approach, [0.,1.], atol=.001)
        with tempfile.TemporaryDirectory() as temp:
            entry = cached_entry(Path(temp)/"threedmr_cache", "Sonic Drive-In", coords, 90, "", carhop=True)
            self.assertTrue(entry["carhop_stalls"])
            vertices = threedmr.load_local_model(temp, entry)[0]
            self.assertGreater(np.ptp(vertices[:,0]), 24)
            self.assertLess(np.max(vertices[:,2]), 4)

    def test_double_carhop_requires_service_lanes_on_both_sides(self):
        from streamedstores import businesses, carhop_approach
        frame = geodesy.EnuFrame(40., -83.)
        places = list(businesses(tile(frame, "", ["Sonic Drive In"]), [None]))
        ring = np.array([[23.,12.],[53.,12.],[53.,24.],[23.,24.]])
        lat,lon,_ = frame.enu_to_geodetic(ring[:,0], ring[:,1], np.zeros(4))
        coords = np.column_stack((lon,lat))
        near = np.array([[20.,28.],[55.,28.]])
        self.assertIsNone(carhop_approach(places,coords,ring,lambda:[near]))
        np.testing.assert_allclose(carhop_approach(places,coords,ring,lambda:[near,near-[0,20]])[1],
                                   [0.,1.], atol=.001)

    def test_drive_through_follows_driver_side_and_rejects_ambiguous_walls(self):
        from streamedstores import drive_through_fixtures, cached_entry
        ring = np.array([[0.,0.],[20.,0.],[20.,12.],[0.,12.]])
        east = np.array([[0.,-3.],[20.,-3.]])
        west = east[::-1]
        wall, positions = drive_through_fixtures(ring, [east], False)
        self.assertEqual(wall, 0)
        self.assertLess(*positions)
        self.assertIsNone(drive_through_fixtures(ring, [west], False))
        self.assertIsNone(drive_through_fixtures(ring, [east, west+[0,18]], False))
        self.assertGreater(*drive_through_fixtures(ring, [west], True)[1])
        coords = ring/100000.
        fixtures = (coords[0]+positions[:,None]*(coords[1]-coords[0])).tolist()
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)/"threedmr_cache"
            for name in ("McDonald's", "Burger King"):
                plain = cached_entry(cache, name, coords, 90, "")
                fitted = cached_entry(cache, name, coords, 90, "", drive_through=fixtures)
                self.assertNotEqual(plain["file"], fitted["file"])
                self.assertGreater(len(threedmr.load_local_model(temp, fitted)[0]),
                                   len(threedmr.load_local_model(temp, plain)[0]))

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
            spans = sorted((points[t, 0].min(), points[t, 0].max()) for t in glass[::2])
            gaps = [right[0]-left[1] for left,right in zip(spans,spans[1:]) if right[0]-left[1] > .01]
            np.testing.assert_allclose(gaps, [.85,.85])
            # Only the central entry reaches the pavement.
            self.assertEqual(sum(points[t, 2].min() == 0 for t in glass), 2)
            np.testing.assert_allclose(np.ptp(points[glass].reshape(-1,3),axis=0), [28.8,0.,3.15])
            self.assertFalse(np.any(materials == buildings.MATERIALS.index(buildings.SHOP)))

    def test_optional_local_sign_uses_category_and_keeps_host(self):
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
                self.assertEqual(entry["local"]["title"], {"retail":"Bookshop","gas_station":"Fuel Station","smoke_shop":"Tobacco Shop","car_wash":"Car Wash"}[category])
                self.assertIsNone(entry["storefront_host"])
                model = threedmr.load_local_model(cache, entry["local"])
                self.assertEqual(len(model[2]), 4)
                with patch.dict(os.environ, BNE_LOCAL_BUSINESS_SIGNS="0"):
                    self.assertFalse(generate())

    @patch("storeidentity.MAPPED_NAMES", True)
    def test_named_retail_sign_spans_parapet_and_keeps_selected_wall(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "Olive Books", [], "retail")
        with (tempfile.TemporaryDirectory() as cache,
              patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="1"),
              patch("threedmr.region_ids", return_value=[]),
              patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])])):
            items, entries = buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                       cache_dir=cache)
            spec = items[0][0]
            self.assertEqual(entries[0]["local"]["title"], "Olive Books")
            front = spec.front
            self.assertIsNotNone(front)
            buildings.doors(items, [(np.array([[0.,75.],[256.,75.]]),3.)],0,0)
            self.assertEqual(spec.front, front)
            vertices, _, triangles, *_ = threedmr.load_local_model(cache, entries[0]["local"])
            self.assertEqual(len(triangles), 12)
            self.assertAlmostEqual(np.ptp(vertices[:,2]), spec.height+.65, places=2)

    def test_four_customer_doors_on_two_walls_of_unnamed_retail_host(self):
        import streamedstores
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "", [], "retail")
        md.cell(None).layers["buildings"].attrs["src_record"] = np.array(["w42@1"])
        lat,lon,_ = frame.enu_to_geodetic(np.array([36.,44.,56.,75.]),np.array([30.,30.,30.,48.]),np.zeros(4))
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ,BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids",return_value=[]),
              patch.object(streamedstores,"entrance_index",return_value={"w42": list(zip(lon,lat,[1]*4))}),
              patch("buildings.road_lines",return_value=[np.array([[0.,15.],[256.,15.]])])):
            items,_ = buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros(np.shape(a)),cache_dir=cache)
            spec = items[0][0]
            self.assertEqual(sorted(map(len,spec.retail_doors.values())),[1,3])
            for lite in (False,True):
                mesh = buildings.Mesh()
                buildings._walls(mesh,spec,spec.ring,-1.,spec.height,buildings._Looks(spec.seed),lite)
                points,_,triangles,materials,_ = mesh.arrays()
                glass = triangles[materials == buildings.MATERIALS.index(buildings.LOBBY)]
                self.assertEqual(sum(points[t,2].min() == 0 for t in glass),8)

    def test_exterior_shops_claim_distinct_bays_only(self):
        from streamedstores import exterior_frontages
        ring = np.array([[0.,0.],[30.,0.],[30.,12.],[0.,12.]])
        scale = [111320.,111132.]
        points = np.array([[5.,-1.],[24.,-2.],[15.,-6.],[0.,-.5]])/scale
        attrs = {"name": ["West","East","Across","Corner"], "confidence": [.76]*4,
                 "basic_category": ["sporting_goods_store"]*4}
        layers = [(SimpleNamespace(attrs=attrs),np.arange(4),points)]
        def names(neighbors=()):
            return [s.name for s,_,_,_ in exterior_frontages(layers,ring/scale,ring,"",neighbors)]
        self.assertEqual(names(),["West","East"])
        self.assertEqual(names([(np.array([20.,-4.]),np.array([28.,-1.]))]),["West"])
        points[2] = [5.5/scale[0],-1./scale[1]]
        self.assertEqual(names(),["East"])

    def test_corner_chain_needs_a_second_store_on_one_wall(self):
        from streamedstores import exterior_frontages
        ring = np.array([[0.,0.],[40.,0.],[40.,18.],[0.,18.]])
        scale = np.array([111320.,111132.])
        points = np.array([[-9.,-9.],[22.,-3.],[-3.,12.]])/scale
        attrs = dict(name=["Five Guys", "Jimmy John's", "Subway"], confidence=[.99]*3)
        def corner(n):
            layers = [(SimpleNamespace(attrs=attrs),np.arange(n),points[:n])]
            bounds = [(np.array(["host"]), np.array([[0.,0.]]), ring.max(0)[None]/scale)]
            return [s for s,*_ in exterior_frontages(layers,ring/scale,ring,"",(),chain_host_id="host",
                                                       get_bounds=lambda:bounds,retail_corner=True)]
        self.assertNotIn("Five Guys",corner(1))
        self.assertIn("Five Guys",corner(2))
        self.assertNotIn("Five Guys",corner(3))
        attrs["name"][1] = "Five Guys"
        self.assertEqual(corner(2), ["Five Guys"])  # duplicate pin, one direct sign only
        attrs["name"][1] = "Jimmy John's"
        points[2] = points[1]
        self.assertEqual(corner(3), [])  # two different tenants competing for the supporting bay

    def test_corner_tenants_use_adjacent_parking_face(self):
        from streamedstores import exterior_frontages
        ring = np.array([[0.,0.],[40.,0.],[40.,18.],[0.,18.]])
        scale = np.array([111320.,111132.])
        points = np.array([[-8.,-8.],[22.,-2.]])/scale
        attrs = dict(name=["Five Guys", "Jimmy John's"], confidence=[.99,.99])
        layers = [(SimpleNamespace(attrs=attrs), np.arange(2), points)]
        bounds = [(np.array(["host"]), ring.min(0)[None]/scale,ring.max(0)[None]/scale)]
        parking = [np.array([[-8.,4.],[-8.,15.]])]
        def walls(aisles):
            return [w for _,_,w,_ in exterior_frontages(layers,ring/scale,ring,"",(),chain_host_id="host",
                        get_bounds=lambda:bounds,retail_corner=True,aisles=aisles)]
        self.assertEqual(walls(parking), [3,3])
        self.assertEqual(walls(parking+[np.array([[10.,-10.],[15.,-10.]])]), [0,0])

    def test_pharmacy_entrance_geometry(self):
        from whole_store import pharmacy_entrance
        def door(ring, arrival, street):
            edges = np.roll(ring,-1,axis=0)-ring
            lengths = np.linalg.norm(edges,axis=1)
            normals = np.column_stack((edges[:,1],-edges[:,0]))/lengths[:,None]
            return pharmacy_entrance(0,lengths,normals,[[] for _ in ring],arrival,street)
        square = np.array([[0.,0.],[30.,0.],[30.,20.],[0.,20.]])
        self.assertEqual(door(square,[0,-1],[0,-1]), (0,15))
        self.assertEqual(door(square,[0,-1],[1,0]), (0,25))
        clipped = np.array([[0.,0.],[26.,0.],[30.,4.],[30.,20.],[0.,20.]])
        self.assertEqual(door(clipped,[.707,-.707],[0,-1])[0],1)

    def test_pharmacy_ownership_requires_an_interior_branch_identifier(self):
        from streamedstores import hosted_pharmacy
        coords = np.array([[0.,0.],[1.,0.],[1.,1.],[0.,1.]])
        attrs = dict(name=["Walgreens #123", "Independent Shoes"], confidence=[.99,.99],
                     basic_category=["money_transfer_service","shoe_store"])
        def owner(n):
            layers = [(SimpleNamespace(attrs=attrs),np.arange(n),np.full((n,2),.5))]
            return hosted_pharmacy(layers,coords,[("Walgreens",None)])
        self.assertIsNone(owner(0))
        self.assertEqual(owner(1),"Walgreens")
        self.assertIsNone(owner(2))

    def test_suite_directory_requires_one_address_and_distinct_tenants(self):
        from streamedstores import suite_directory, cached_entry
        center = np.array([-80.,35.])
        coords = center+np.array([[0.,0.],[.00010,0.],[.00012,0.]])
        attrs = {"street_address":["101 Oak Dr., Ste. B","101 Oak Drive Suite A","110 Oak Dr #C"],
                 "name":["Anchor","Oak Tea","Other"],"confidence":[.76]*3,"basic_category":["cafe"]*3}
        layers = [(SimpleNamespace(attrs=attrs),np.arange(3),coords)]
        directory = suite_directory(layers,center,"Anchor")
        self.assertEqual(directory,(("A","Oak Tea"),("B","Anchor")))
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)/"threedmr_cache"/"storefronts"
            entry = cached_entry(cache,"Anchor",coords[:2],None,None,local_panel=("shop",5.4,"raised",directory))
            model = threedmr.load_local_model(tmp,dict(entry,file="storefronts/"+entry["file"]))
            self.assertEqual(len(model[2]),14)
        attrs["street_address"][2] = "101 Oak Dr Unit A"
        self.assertFalse(suite_directory(layers,center,"Anchor"))
        del attrs["street_address"]
        self.assertFalse(suite_directory(layers,center,"Anchor"))

    def test_business_confidence_cutoff(self):
        from streamedstores import qualifies_business
        for score in (None, float("nan"), .74, .75, 1.01):
            self.assertFalse(qualifies_business(score))
        for score in (.75001, .76, .92, 1.0):
            self.assertTrue(qualifies_business(score))

    def test_catalog_matches_typed_cinemas_and_restaurant_aliases(self):
        from streamedstores import chain
        from store_match import brand
        self.assertEqual(chain("Regal Green Hills", cinema=True), "Regal Cinemas")
        self.assertIsNone(chain("Regal Nails"))
        self.assertEqual(brand({"amenity": "cinema", "name": "AMC CLASSIC Plaza 12"}), "AMC Theatres")
        self.assertIsNone(brand({"amenity": "cinema", "brand": "AMC", "drive_in": "yes"}))
        self.assertEqual(brand({"amenity": "fast_food", "name": "In-N-Out"}), "In-N-Out Burger")

    def test_commercial_frontage_between_two_streets(self):
        from streamedstores import street_frontage
        offsets = np.array([[54.,-2.],[-51.,-20.],[0.,-45.]])
        ranks = np.array([1,0,3])
        np.testing.assert_array_equal(street_frontage(offsets,ranks), offsets[1])
        offsets[1] *= 2
        np.testing.assert_array_equal(street_frontage(offsets,ranks), offsets[0])
        np.testing.assert_array_equal(street_frontage(offsets,None), offsets[2])

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

    def test_multipart_mall_keeps_geometry_and_places_a_visible_overlay_once(self):
        frame = geodesy.EnuFrame(40., -83.)
        mall_id = "main-shell"
        name = "Harbor Square Mall"

        def lonlat(xy):
            xy = np.asarray(xy, np.float64)
            lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            return np.column_stack((lon, lat))

        def rect(left, bottom, right, top):
            return lonlat([(left, bottom), (right, bottom), (right, top), (left, top), (left, bottom)])

        def polygons(layer, ids, rings, **attrs):
            rows = [(mapgeom.POLYGON, [[ring]]) for ring in rings]
            features = mapgeom.build(layer, np.array(ids), rows)
            features.attrs.update({k: np.array(v) for k, v in attrs.items()})
            return features

        shells = polygons("buildings", [mall_id, "different-shell"],
                          [rect(60, 70, 340, 200), rect(282, 48, 318, 58)],
                          name=[name, name], height=[10., 6.], has_parts=[1., 1.],
                          src_dataset=["OpenStreetMap", "OpenStreetMap"], src_record=["w100@1", "w101@1"])
        shells.attrs["class"] = np.array(["shopping_mall", "retail"])
        parts = polygons("building_parts", ["west", "east"],
                         [rect(60, 70, 190, 200), rect(190, 70, 340, 200)],
                         building_id=[mall_id, mall_id], height=[8., 11.], min_height=[0., 0.])
        points = lonlat([(200, 150), (110, 74), (300, 74), (150, 73)])
        places = mapgeom.build("places", np.array(["mall-poi", "west-tenant", "east-tenant", "indoor-tenant"]),
                               [(mapgeom.POINT, [[p[None]]]) for p in points])
        places.attrs.update(name=np.array([name, "Target", "Burlington", "Indoor Books"]),
                            basic_category=np.array(["shopping_mall", "department_store", "department_store", "book_store"]),
                            confidence=np.array([.99, .99, .99, .99]))
        layers = {"buildings": shells, "building_parts": parts, "places": places}
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers=layers))

        aisles = [[[106., 40.], [114., 40.]], [[296., 40.], [304., 40.]]]

        def roads(frame, layers, x0, y0, **options):
            if options.get("only_sub") == {"parking_aisle"}:
                return [np.array(aisle) - [x0, y0] for aisle in aisles]
            return []

        def ground(lat, lon):
            return np.zeros(np.shape(lat)) + 4.

        with (tempfile.TemporaryDirectory() as cache, patch("threedmr.region_ids", return_value=[]),
              patch("roads.cells_of_tile", return_value=[None, None]),
              patch("streamedstores.pylons", return_value=[]),
              patch("streamedstores.exterior_access_index", return_value={"w100": {"target", "burlington"}}),
              patch("buildings.road_lines", side_effect=roads),
              patch.dict(os.environ, BNE_LOCAL_BUSINESS_SIGNS="1")):
            def generate(enabled, tile_x=0):
                with patch.dict(os.environ, BNE_STOREFRONTS=enabled):
                    return buildings.road_tile_items(frame, tile_x, 0, md, ground, cache_dir=cache)

            off_items, off_signs = generate("0")
            on_items, signs = generate("1")
            self.assertFalse(off_signs)
            self.assertEqual(len(on_items), len(off_items))
            for mesh in (buildings.build, buildings.collision_mesh):
                for on, off in zip(mesh(on_items), mesh(off_items)):
                    if isinstance(on, np.ndarray):
                        np.testing.assert_array_equal(on, off)
                    else:
                        self.assertEqual(on, off)
            overlay, = signs
            self.assertEqual(overlay["local"]["title"], "Bullmark", "the detached shell blocks the other approach")
            self.assertTrue(overlay["local"]["storefront_overlay"])
            self.assertIsNone(overlay["storefront_host"])
            self.assertAlmostEqual(overlay["xy"][0], 110., delta=.2)
            self.assertAlmostEqual(overlay["xy"][1], 69.76, delta=.2)
            west = next(row for row in on_items if row[0].ring.mean(0)[0] < 190.)
            self.assertAlmostEqual(overlay["storefront_ground"], buildings.base_z(*west))
            verts = threedmr.load_local_model(cache, overlay["local"])[0]
            self.assertAlmostEqual(np.max(verts[:, 2]), 6.88, delta=.1)
            self.assertFalse(generate("1", tile_x=1)[1], "neighboring tile does not emit the owner's overlays")

            # A public parking aisle supports indoor tenant banners even without a mapped door.
            aisles.append([[135., 40.], [150., 40.], [162., 40.]])
            with patch("streamedstores.exterior_access_index", return_value={}):
                _, banners = generate("1")
                titles = [entry["local"]["title"] for entry in banners]
                self.assertEqual(titles.count("Bookshop"), 1)
                self.assertNotIn(source_key("Burlington"), [e["local"]["storefront_match"] for e in banners], "the detached building still blocks this approach")
                with patch.dict(os.environ, BNE_LOCAL_BUSINESS_SIGNS="0"):
                    self.assertFalse(generate("1")[1])

    def test_multipart_mall_sign_reconciles_only_contiguous_coplanar_walls(self):
        from streamedstores import multipart_mall_wall

        ring = np.array([[0., 0.], [30., 0.], [30., 20.], [0., 20.]])
        panel = np.array([[8., 0.], [18., 0.]])
        approach = np.array([[[8., -6.], [18., -6.]]])
        fit, ground, height = multipart_mall_wall(
            panel, [(ring, 8.5, 5.1), (ring, 4., 4.5)], [ring], approach)
        np.testing.assert_array_equal(fit, panel)
        self.assertAlmostEqual(height, 9.6)
        self.assertEqual(ground, 4.)
        for upper, z in [(ring, 8.8), (ring + [0., .1], 8.5), (ring + [0., 3.], 8.5),
                         (ring * [.5, 1.], 8.5)]:
            with self.subTest(upper=upper.tolist(), z=z):
                fit, ground, height = multipart_mall_wall(
                    panel, [(upper, z, 5.1), (ring, 4., 4.5)], [ring, upper], approach)
                np.testing.assert_array_equal(fit, panel)
                self.assertEqual((ground, height), (4., 4.5))

        # Thin bands, overlapping geometry and duplicate records can describe one flat wall.
        bands = [(ring, 4., 2.), (ring, 6., 2.), (ring, 6., 2.), (ring, 7.5, 3.)]
        for ordered in [bands, bands[::-1]]:
            _, ground, height = multipart_mall_wall(panel, ordered, [ring], approach)
            self.assertEqual((ground, height), (4., 6.5))
        self.assertIsNone(multipart_mall_wall(panel, bands[:1], [ring], approach))
        self.assertIsNone(multipart_mall_wall(
            panel, [(ring, 4., 4.5), (ring + [0., .1], 4., 4.5)], [ring], approach))

    def test_mall_sign_height_scales_with_the_supported_face(self):
        from streamedstores import mall_sign_top

        for height, expected in [(3.5, 3.05), (5.1, 4.65), (6., 5.55), (9.6, 7.95), (30., 9.)]:
            with self.subTest(height=height):
                self.assertEqual(mall_sign_top(height), expected)

    def test_multipart_mall_sign_requires_a_drawn_wall_and_clear_public_approach(self):
        from streamedstores import multipart_mall_wall

        shell = np.array([[0., 0.], [80., 0.], [80., 55.], [0., 55.]])
        panel = np.array([[30., -.1], [46., -.1]])
        setback = shell + [0., 3.]
        blocker = np.array([[20., -10.], [50., -10.], [50., -3.], [20., -3.]])
        aisle = np.array([[[22., -20.], [60., -20.]]])
        drawn = [(shell, 9., 11.)]
        fit, ground, height = multipart_mall_wall(panel, drawn, [shell], aisle)
        np.testing.assert_allclose(fit, [[30., 0.], [46., 0.]])
        self.assertEqual((ground, height), (9., 11.))
        self.assertIsNone(multipart_mall_wall(panel, drawn, [shell, blocker], aisle))
        self.assertIsNone(multipart_mall_wall(panel, [(setback, 9., 11.)], [shell, setback], aisle))
        self.assertIsNone(multipart_mall_wall(panel, drawn, [shell], aisle + [0., 35.]))

    def test_anchor_on_mall_exterior_requires_a_supported_wall(self):
        frame = geodesy.EnuFrame(40., -83.)
        for brand in ("Macy's", "Target", "Dick's Sporting Goods", "The Cheesecake Factory"):
            md = tile(frame, "", [brand], "shopping_mall")
            layers = md.cell(None).layers
            layers["buildings"].attrs["height"] = np.array([9.])
            aisle = np.array([[50.,15.],[50.,20.]])
            with (self.subTest(brand=brand), tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ,BNE_STOREFRONTS="1"), patch("threedmr.region_ids",return_value=[]),
                  patch("buildings.road_lines",return_value=[aisle])):
                def generate(y):
                    lat,lon,_ = frame.enu_to_geodetic(50.,y,0.)
                    layers["places"].lonlat[:] = [lon,lat]
                    return buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros(np.shape(a)),cache_dir=cache)[1]
                sign, = generate(33.)
                self.assertIsNone(sign["storefront_host"])
                self.assertEqual(sign["local"]["title"],identity_for(brand).label)
                mesh = threedmr.load_local_model(cache,sign["local"])
                self.assertAlmostEqual(np.ptp(mesh[0][:,2]),7.55,places=2)
                self.assertFalse(generate(45.))

    def test_sloping_mall_keeps_wall_signs_at_host_base(self):
        frame = geodesy.EnuFrame(40., -83.)
        def hillside(lat, lon):
            return (lon + 83.) * 17000
        aisle = np.array([[50.,15.],[50.,20.]])
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ,BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids",return_value=[]), patch("buildings.road_lines",return_value=[aisle])):
            for kind in ("shopping_mall", "supermarket"):
                with self.subTest(kind=kind):
                    md = tile(frame, "" if kind=="shopping_mall" else "Target", ["Target"], kind)
                    lat,lon,_ = frame.enu_to_geodetic(50.,33.,0.)
                    md.cell(None).layers["places"].lonlat[:] = [lon,lat]
                    items,models = buildings.road_tile_items(frame,0,0,md,hillside,cache_dir=cache)
                    spec,low,high = items[0]
                    self.assertGreater(high-low,3)
                    if kind=="shopping_mall":
                        panel, = models
                        self.assertTrue(panel["local"]["storefront_overlay"])
                        self.assertAlmostEqual(panel["storefront_ground"],buildings.base_z(spec,low,high))
                    else:
                        self.assertFalse(models)

    def test_mall_cladding_wraps_the_shell_at_world_scale(self):
        ring = np.array([[0.,0.],[65.,0.],[65.,30.],[0.,30.]])
        mesh = buildings.Mesh()
        buildings._mall_cladding(mesh,ring,-1.,0.,10.,buildings._Looks(.5),.5)
        positions,uv,_,materials,_ = mesh.arrays()
        self.assertEqual(set(materials), {buildings.MATERIALS.index(buildings.BRICK_WALL),
                                          buildings.MATERIALS.index(buildings.CONCRETE)})
        self.assertEqual(set(positions[:,2].round(2).tolist()),
                         set(np.array([-1.,1.6,8.4,9.65,10.],dtype=positions.dtype).tolist()))
        self.assertGreater(np.ptp(uv[:,0]),10.)

    def test_retail_mall_identity_uses_approach_and_matching_place(self):
        import streamedstores as stores
        frame = geodesy.EnuFrame(40.,-83.)
        md = tile(frame, "Lake Crossing", ["Lake Crossing Mall"], "retail")
        layer = md.cell(None).layers["places"]
        layer.attrs["basic_category"] = np.array(["shopping_mall"])
        coords = md.cell(None).layers["buildings"].lonlat[:-1]
        layers = list(stores.businesses(md,[None]))
        self.assertTrue(stores.mapped_mall(layers,coords,"Lake Crossing"))
        self.assertFalse(stores.mapped_mall(layers,coords,"River Crossing"))
        lat,lon,_ = frame.enu_to_geodetic(np.array([52.,52.]),np.array([30.,15.]),np.zeros(2))
        walk = np.column_stack((lon,lat))[None]
        panel, = stores.mall_entrance_panels(coords,walk,[])
        self.assertIsNotNone(panel)
        self.assertFalse(stores.mall_entrance_panels(coords,walk,[("Existing tenant",panel)]))
        self.assertFalse(stores.mall_entrance_panels(coords,[],[]))

    def test_unnamed_center_needs_one_inside_center_and_distinct_retail_tenants(self):
        import streamedstores as stores

        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "", ["Harbor Plaza", "West Books", "East Shoes"], "retail")
        features = md.cell(None).layers
        places = features["places"]
        places.attrs["basic_category"] = np.array(["shopping_center", "book_store", "shoe_store"])
        ring = features["buildings"].lonlat[:-1]

        def identify():
            # The same place may be supplied by more than one overlapping cell.
            return stores.unnamed_shopping_center(list(stores.businesses(md, [0, 1])), ring)

        self.assertEqual(identify(), "Harbor Plaza")
        with patch.dict(places.attrs, operating_status=np.array(["open", "closed", "open"])):
            self.assertIsNone(identify())
        with patch.dict(places.attrs, confidence=np.array([.6, .99, .99])):
            self.assertIsNone(identify())
        with patch.dict(places.attrs, name=np.array(["Harbor Plaza", "West Books", "WEST BOOKS"])):
            self.assertIsNone(identify())
        original = places.lonlat.copy()
        places.lonlat[1] = places.lonlat[2]
        self.assertIsNone(identify())
        places.lonlat[:] = original
        places.lonlat[0, 0] = ring[:, 0].max() + .0001
        self.assertIsNone(identify())
        other = tile(frame, "", ["Plaza A", "Plaza B", "West Books", "East Shoes"], "retail")
        other.cell(None).layers["places"].attrs["basic_category"] = np.array(
            ["shopping_center", "shopping_mall", "book_store", "shoe_store"])
        self.assertIsNone(stores.unnamed_shopping_center(list(stores.businesses(other, [0])), ring))

    def test_large_unnamed_plaza_signs_tenants_through_road_tile(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "", ["Harbor Plaza", "West Books", "East Shoes"], "retail")
        layers = md.cell(None).layers

        def lonlat(x, y):
            lat, lon, _ = frame.enu_to_geodetic(np.array(x), np.array(y), np.zeros(len(x)))
            return np.column_stack((lon, lat))

        layers["buildings"].lonlat[:] = lonlat([10., 310., 310., 10., 10.], [30., 30., 80., 80., 30.])
        places = layers["places"]
        places.lonlat[:] = lonlat([130., 55., 190.], [60., 33., 33.])
        places.attrs["basic_category"] = np.array(["shopping_center", "book_store", "shoe_store"])
        aisle = np.array([[x, 20.] for x in range(15, 310, 5)])
        with (tempfile.TemporaryDirectory() as cache, patch("threedmr.region_ids", return_value=[]),
              patch("buildings.road_lines", return_value=[aisle]),
              patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="1")):
            def signs():
                return buildings.road_tile_items(frame, 0, 0, md, lambda lat, lon: np.zeros(np.shape(lat)),
                                                 cache_dir=cache)[1]

            generated = signs()
            titles = {entry["local"]["title"] for entry in generated}
            self.assertTrue({"Bookshop", "Footwear"} <= titles, titles)
            self.assertTrue(all(entry["storefront_host"] is None for entry in generated))
            with patch.dict(places.attrs, basic_category=np.array(["office", "book_store", "shoe_store"])):
                self.assertFalse(signs())

    def test_mall_entrances_are_separate_and_directories_deduplicate(self):
        import streamedstores as stores
        frame = geodesy.EnuFrame(40., -83.)
        def coords(rows):
            xy = np.array(rows)
            lat,lon,_ = frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
            return np.column_stack((lon,lat))
        ring = coords([(0,0),(200,0),(200,100),(0,100)])
        walks = [coords([(x,0),(x,-12)]) for x in (20,22,150)]
        self.assertEqual(len(stores.mall_entrance_panels(ring,walks,[])), 2)
        tenants = [("Target",ring[0]),("Target",ring[1]),("Old Navy",ring[2]),(None,ring[3])]
        tenants.append((stores.LocalSign("Corner Books","shop"),ring[1]))
        candidates = [(n,ring[1],.95) for n in ("target","Market","Games","Cinema","Crafts","Mall")]
        self.assertEqual(stores.mall_directory(tenants,candidates,"Mall"),
                         ("Cinema","Corner Books","Crafts","Games","Market","Old Navy","Target"))

    def test_mall_tenant_exterior_evidence_requires_a_live_wall_match(self):
        import streamedstores as stores
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "Test Center", ["River Kitchen"], "retail")
        layer = md.cell(None).layers["places"]
        layer.attrs["basic_category"] = np.array(["restaurant"])
        coords = md.cell(None).layers["buildings"].lonlat[:-1]
        def panels(y, documented=False, entrance=False, *, x=50.):
            lat,lon,_ = frame.enu_to_geodetic(x,y,0.)
            layer.lonlat[:] = [lon,lat]
            lat,lon,_ = frame.enu_to_geodetic(50.,30.,0.)
            doors = ((lon,lat),) if entrance else ()
            return stores.confirmed_mall_panels(list(stores.businesses(md,[None])),coords,
                                                {"river kitchen"} if documented else (), doors)
        self.assertFalse(panels(29.))
        self.assertEqual(len(panels(29.,documented=True)),1)
        self.assertEqual(len(panels(29.,entrance=True)),1)
        self.assertFalse(panels(44.,documented=True))
        self.assertFalse(panels(31.,documented=True,x=31.), "documented identity does not disambiguate a corner pin")
        layer.attrs["operating_status"] = np.array(["closed"])
        self.assertFalse(panels(29.,documented=True,entrance=True))

    def test_mall_banners_rank_visible_tenants_and_respect_existing_signs(self):
        import streamedstores as stores
        scale = np.array([111320.,111132.])
        ring = np.array([[0.,0.],[200.,0.],[200.,100.],[0.,100.]])/scale
        parking = np.array([(x,-12.) for x in range(0,201,8)])/scale
        candidates = [(n,np.array([x,y])/scale,c) for n,x,y,c in (
            ("Lake Books",80,5,.99),("Lake Books",82,3,.99),("Older Listing",80,3,.82),
            ("Back Wall",90,95,.99),("Anchor",40,5,.99))]
        occupied = [("Anchor",np.array([[35.,0.],[45.,0.]])/scale)]
        extra = stores.mall_tenant_banners(ring,candidates,occupied,parking,[])
        self.assertEqual([n.name for n,_ in extra],["Lake Books"])
        self.assertTrue(np.allclose(extra[0][1][:,1],0))
        self.assertFalse(stores.mall_tenant_banners(ring,candidates,occupied,[],[]))

    def test_adjacent_high_confidence_mall_banners_share_a_long_wall(self):
        from streamedstores import mall_tenant_banners, local_sign
        scale = np.array([111320., 111132.])
        ring = np.array([[0.,0.],[200.,0.],[200.,80.],[0.,80.]])/scale
        parking = np.array([(x,-10.) for x in range(0,201,8)])/scale
        candidates = [(name,np.array([x,2.])/scale,.99) for name,x in (("PetSmart",80),("Burlington",93))]
        panels = mall_tenant_banners(ring,candidates,[],parking,[])
        self.assertEqual({name.name for name,_ in panels},{"PetSmart","Burlington"})
        left,right = sorted((panel[:,0].min(),panel[:,0].max()) for _,panel in panels)
        self.assertLess(left[1],right[0])
        self.assertEqual(local_sign("Main Street Music",{"musical_instrument_store"}).name,"Main Street Music")

    def test_mall_banner_needs_an_unobstructed_approach(self):
        from streamedstores import mall_tenant_banners
        scale = np.array([111320.,111132.])
        ring = np.array([[0,0],[100,0],[100,70],[0,70]])/scale
        annex = np.array([[-1,-5],[101,-5],[101,-12],[-1,-12]])/scale
        parking = np.array([(x,-20) for x in range(10,91,8)])/scale
        tenants = [("Lake Books",np.array([45.,2.])/scale,.99)]
        self.assertTrue(mall_tenant_banners(ring,tenants,[],parking,[]))
        self.assertFalse(mall_tenant_banners(ring,tenants,[],parking,[],[annex]))
        walk = np.array([[[43,-2],[48,-2]]])/scale
        self.assertTrue(mall_tenant_banners(ring,tenants,[],parking,walk,[annex]))

    def test_dense_frontage_requires_distinct_well_located_tenants(self):
        from streamedstores import mall_tenant_banners
        scale = np.array([111320., 111132.])
        ring = np.array([[0,0],[100,0],[100,80],[0,80]])/scale
        parking = np.array([(x,-10) for x in range(10,91,5)])/scale
        def count(xs, y, confidence):
            candidates = [(f"Tenant {i}", np.array([x,y])/scale, confidence)
                          for i,x in enumerate(xs)]
            return len(mall_tenant_banners(ring,candidates,[],parking,[]))
        xs = [19,44,69,81]
        self.assertEqual(count(xs,2,.98),4)
        self.assertEqual(count(xs,22,.98),2)
        self.assertEqual(count(xs,2,.81),2)
        self.assertLessEqual(count([44]*4,2,.98),2)

    def test_street_sign_avoids_shared_wall_in_either_winding(self):
        from streamedstores import approach_panel
        ring = np.array([[0.,0.],[10.,0.],[10.,25.],[0.,25.]])
        neighbor = ring - [10.,0.]
        for outline in (ring,ring[::-1]):
            panel = approach_panel(outline,outline,np.array([-1.,0.]),(),
                                   neighbors=[neighbor],street_offsets=[[0.,-18.]])
            self.assertTrue(np.allclose(panel[:,1],0))
            self.assertIsNone(approach_panel(outline,outline,np.array([-1.,0.]),(),neighbors=[neighbor]))

    def test_named_venue_with_offset_business_pin(self):
        import streamedstores
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "The Lantern", ["THE LANTERN"], "retail")
        layer = md.cell(None).layers["places"]
        layer.attrs["basic_category"] = np.array(["music_venue"])
        ring = np.array([[30,30], [75,30], [75,60], [30,60]])
        lat,lon,_ = frame.enu_to_geodetic(ring[:,0],ring[:,1],np.zeros(4))
        coords = np.column_stack((lon,lat))
        for y,label,expected in ((24,"The Lantern",True), (24,"The Lantern Annex",False),
                                 (18,"The Lantern",False)):
            lat,lon,_ = frame.enu_to_geodetic(50., y, 0.)
            layer.lonlat[:] = [lon,lat]
            layers = list(streamedstores.businesses(md,[None]))
            self.assertEqual(bool(streamedstores.named_frontage(layers,coords,label)),expected)
        lat,lon,_ = frame.enu_to_geodetic(50., 24., 0.)
        layer.lonlat[:] = [lon,lat]
        layers = list(streamedstores.businesses(md,[None]))
        layer.attrs["operating_status"] = np.array(["closed"])
        self.assertIsNone(streamedstores.named_frontage(layers,coords,"The Lantern"))

    def test_multistory_venue_sign_preserves_host_and_requires_one_identity(self):
        frame = geodesy.EnuFrame(40., -83.)
        with (tempfile.TemporaryDirectory() as cache,
              patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="1"),
              patch("threedmr.region_ids", return_value=[]),
              patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])])):
            for names in (["Lantern Hall"], ["Lantern Hall", "Side Stage"]):
                md = tile(frame, "", names, "retail")
                layers = md.cell(None).layers
                layers["buildings"].attrs.update(height=np.array([15.]), num_floors=np.array([4]))
                layers["places"].attrs["basic_category"] = np.array(["music_venue"] * len(names))
                entries = buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                    cache_dir=cache)[1]
                if len(names) == 1:
                    sign, = entries
                    self.assertIsNone(sign["storefront_host"])
                    self.assertEqual(sign["local"]["title"], "Music & Nightlife")
                    vertices = threedmr.load_local_model(cache, sign["local"])[0]
                    self.assertGreater(np.ptp(vertices[:, 1]), 1.)
                    self.assertLess(np.max(vertices[:, 2]), 5.)
                else:
                    self.assertFalse(entries)

    def test_building_tenant_requires_exclusive_evidence(self):
        from streamedstores import building_tenant, LocalSign, unclassified_building_tenant
        bank = LocalSign("Central Bank", "bank")
        chain = [("Target", None)]
        self.assertIsNone(unclassified_building_tenant(chain, "", False))
        self.assertEqual(unclassified_building_tenant(chain, "", True), "Target")
        self.assertEqual(unclassified_building_tenant([(bank, None)], "CENTRAL BANK", False), bank)
        self.assertIsNone(unclassified_building_tenant([(bank, None)], "Main Bank", False))
        self.assertIsNone(building_tenant(chain, label="", anchor=None, fallback=None, mall=True, generic_host=True))
        self.assertIsNone(building_tenant(chain + [(None, None)], label="Target", anchor="Target",
                                          fallback=None, mall=False, generic_host=True))

    def test_named_big_box_with_hosted_services_keeps_its_shell(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame,"Walmart Supercenter",["Walmart","Walmart Pharmacy","Coin Kiosk"],"retail")
        places = md.cell(None).layers["places"]
        aisle = np.array([[50.,15.],[50.,20.]])
        with (tempfile.TemporaryDirectory() as cache, patch.dict(os.environ,BNE_STOREFRONTS="1",
              BNE_LOCAL_BUSINESS_SIGNS="1"), patch("threedmr.region_ids",return_value=[]),
              patch("buildings.road_lines",return_value=[aisle])):
            for business,category,whole in (("Coin Kiosk","financial_service",True),
                                            ("Other Anchor","department_store",False),
                                            ("Best Buy","electronics_store",False),
                                            ("River Books","book_store",False)):
                with self.subTest(category=category):
                    places.attrs["name"] = np.array(["Walmart","Walmart Pharmacy",business])
                    places.attrs["basic_category"] = np.array(["superstore","pharmacy_and_drug_store",category])
                    _,models = buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros_like(a),cache_dir=cache)
                    walmart, = [m for m in models if m["local"]["title"]==identity_for("Walmart").label]
                    self.assertEqual(walmart["storefront_host"] is not None,whole)

    def test_chain_on_anonymous_flat_building(self):
        frame = geodesy.EnuFrame(40., -83.)
        for name, x, neighbor, expected in (
            ("Burger King", 50., False, ["Burger King"]),
            ("Sonic Drive In", 76.2, False, ["Sonic Drive-In"]),
            ("Sonic Drive In", 81., False, []),
            ("Sonic Drive In", 77., True, []),
        ):
            with (self.subTest(name=name, x=x, neighbor=neighbor), tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="0"),
                  patch("threedmr.region_ids", return_value=[]),
                  patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])])):
                md = tile(frame, "", [name], "", subtype="")
                layers = md.cell(None).layers
                host = layers["buildings"]
                if neighbor:
                    # Two metres to each wall. Neither building owns this store.
                    ring = host.parts(0)[0][0]
                    lat1, lon1, _ = frame.enu_to_geodetic(30., 30., 0.)
                    lat2, lon2, _ = frame.enu_to_geodetic(79., 30., 0.)
                    shifted = ring + [lon2-lon1, lat2-lat1]
                    host = mapgeom.build("buildings", np.array(["0", "1"]),
                                         [(mapgeom.POLYGON, [[r]]) for r in (ring, shifted)])
                    layers["buildings"] = host
                host.attrs.update(height=np.full(len(host), 4.7), roof_shape=np.full(len(host), "flat"))
                lat, lon, _ = frame.enu_to_geodetic(x, 45., 0.)
                layers["places"].lonlat[:] = [lon, lat]
                entries = buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)),
                                                    cache_dir=cache)[1]
                self.assertEqual([entry["local"]["title"] for entry in entries], [identity_for(n).label for n in expected])

    def test_cafe_label_requires_a_mapped_cafe_category(self):
        from unittest.mock import patch
        from streamedstores import local_sign
        from storeidentity import identity_for
        for category in ("cafe", "coffee_shop"):
            place = local_sign("Oak Tea", {category})
            with patch("storeidentity.MAPPED_NAMES", True):
                self.assertEqual(identity_for(place.name, place.style, mapped_name=True).label, "Oak Tea")
            with patch("storeidentity.MAPPED_NAMES", False):
                self.assertEqual(identity_for(place.name, place.style, mapped_name=True).label, "Café")
        shop = local_sign("Cafe Supply", {"hardware_store"})
        self.assertEqual(identity_for(shop.name, shop.style).label, "Hardware Store")
        self.assertEqual(identity_for(shop.name, "mall_banner").label, "Shop")

    def test_category_models_require_an_exclusive_named_host(self):
        from streamedstores import local_sign
        from storeidentity import category_family
        self.assertEqual(category_family({"pizza_restaurant", "restaurant"}), "pizzeria")
        self.assertIsNone(category_family({"electronics_store", "cafe"}))
        self.assertEqual(local_sign("Zed", {"cafe"}).style, "cafe")
        frame = geodesy.EnuFrame(40., -83.)
        for building, kind, tenants, categories, family, whole in [
                ("Oak Tea", "coffee_shop", [], [], "cafe", True),
                ("Oak Pie", "retail", ["Oak Pie"], ["pizza_restaurant"], "pizzeria", True),
                ("Oak Pie", "retail", ["Oak Pie", "Other"], ["pizza_restaurant", "retail"], "pizzeria", False),
                ("", "retail", ["Oak Pie"], ["pizza_restaurant"], "pizzeria", False)]:
            with (self.subTest(building=building, tenants=tenants), tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ, BNE_STOREFRONTS="1", BNE_LOCAL_BUSINESS_SIGNS="1"),
                  patch("threedmr.region_ids", return_value=[]),
                  patch("buildings.road_lines", return_value=[np.array([[0.,15.],[256.,15.]])])):
                md = tile(frame, building, tenants, kind)
                if tenants:
                    md.cell(None).layers["places"].attrs["basic_category"] = np.array(categories)
                for mapped in (True, False):
                    with patch("storeidentity.MAPPED_NAMES", mapped):
                        _, entries = buildings.road_tile_items(frame, 0, 0, md, lambda a,b: np.zeros(np.shape(a)), cache_dir=cache)
                    entry, = entries
                    local = entry["local"]
                    self.assertEqual(local["storefront_style"], family)
                    self.assertEqual(bool(entry["storefront_host"]), whole)
                    expected = building if building and mapped else {"cafe":"Café", "pizzeria":"Pizzeria"}[family]
                    self.assertEqual(local["title"], expected)
                    self.assertEqual("storefront_variant" in local, whole)

    def test_local_bank_requires_a_branch_category(self):
        from streamedstores import local_sign
        self.assertIsNone(local_sign("Central Bank ATM", {"bank_or_credit_union"}))
        self.assertIsNone(local_sign("North Insurance", {"financial_service","insurance_agency"}))
        self.assertEqual(local_sign("River Credit Union", {"financial_service","credit_union"}).style, "bank")

    def test_commons_places_both_rows_in_order_on_long_customer_paths(self):
        from streamedstores import commons_panels
        scale = np.array([111320.,111132.])
        ring = np.array([(0,0),(100,0),(100,20),(20,20),(20,100),(0,100)])/scale
        paths = np.array([[(24,30),(110,30)],[(30,24),(30,110)]])/scale
        tenants = [(name,np.array(point)/scale) for name,point in (
            ("Shoes",(70,14)),("Books",(45,14)),("Cafe",(14,65)),("Rear",(80,1)))]
        for outline in (ring, ring[::-1]):
            panels = dict(commons_panels(tenants, outline, paths, ()))
            self.assertEqual(set(panels), {"Shoes","Books","Cafe"})
            np.testing.assert_allclose(panels["Books"].mean(0)*scale, [45,20], atol=.01)
            np.testing.assert_allclose(panels["Shoes"].mean(0)*scale, [70,20], atol=.01)
            np.testing.assert_allclose(panels["Cafe"].mean(0)*scale, [20,65], atol=.01)
            self.assertFalse(list(commons_panels(tenants, outline, [], ())))

    def test_enclosed_mall_omits_interior_tenants_from_signs_and_directory(self):
        import streamedstores as stores
        scale = np.array([111320.,111132.])
        ring = np.array([(0,0),(100,0),(100,60),(0,60)])/scale
        points = np.array([(19,2),(43,22),(60,24)])/scale
        places = SimpleNamespace(ids=["a","b","c"], attrs={
            "name": ["Exterior Books","Interior Books","Target"], "confidence": [.99]*3,
            "basic_category": ["book_store","book_store","department_store"]})
        layers = [(places, np.arange(3), points)]
        candidates = []
        tenants = stores.mapped_tenants(layers, ring, local=True, retail_candidates=candidates, enclosed=True)
        parking = np.array([(x,-10) for x in range(0,101,5)])/scale
        panels = stores.mall_tenant_banners(ring, candidates, [], parking, [], enclosed=True)
        self.assertEqual([name.name for name,_ in panels], ["Exterior Books"])
        self.assertEqual(stores.mall_directory(tenants, candidates), ("Exterior Books",))
        self.assertEqual(len(stores.mapped_tenants(layers, ring, local=True)), 3)

    @patch("storeidentity.MAPPED_NAMES", True)
    def test_bank_canopy_uses_tagged_lane_and_atm_requires_exterior_door(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame,"River Bank",["River Bank","River Bank ATM"],"bank")
        layers = md.cell(None).layers
        layers["buildings"].attrs.update(height=np.array([5.]),src_record=np.array(["w44@1"]))
        places = layers["places"]
        places.attrs["basic_category"] = np.array(["bank","atm"])
        lat,lon,_ = frame.enu_to_geodetic(52.,60.,0.)
        places.lonlat[1] = [lon,lat]
        bank_lane = [np.array([[31.,27.],[74.,27.]])]
        def lines(*args, **kw):
            if kw.get("directed"):
                return bank_lane if "drive-through" in kw["only_sub"] else []
            if kw.get("only_sub"):
                return []
            return [np.array([[0.,80.],[110.,80.]])]
        with (tempfile.TemporaryDirectory() as cache,patch("threedmr.region_ids",return_value=[]),
              patch("buildings.road_lines",side_effect=lines),
              patch("streamedstores.entrance_index",return_value={"w44":[(lon,lat,1)]}),
              patch.dict(os.environ,BNE_STOREFRONTS="1",BNE_LOCAL_BUSINESS_SIGNS="1")):
            def generate():
                rows = buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros(np.shape(a)),cache_dir=cache)[1]
                return {r["local"]["title"]:threedmr.load_local_model(cache,r["local"])[0] for r in rows}
            fitted = generate()
            self.assertEqual(set(fitted), {"River Bank","ATM"})
            self.assertLess(np.ptp(fitted["ATM"][:,0]),1.2)
            self.assertGreater(np.ptp(fitted["River Bank"][:,1]),32)
            bank_lane.clear()
            places.lonlat[1] = places.lonlat[0]
            plain = generate()
            self.assertEqual(set(plain), {"River Bank"})
            self.assertLess(np.ptp(plain["River Bank"][:,1]),1.)

    @patch("storeidentity.MAPPED_NAMES", True)
    def test_identified_service_business_on_unclassified_building(self):
        frame = geodesy.EnuFrame(40.,-83.)
        for label,kind,category,taxonomy in (
                ("Hill Credit Union", "", "bank_or_credit_union","credit_union"),
                ("Main Bank", "office", "bank_or_credit_union","bank"),
                ("", "", "automotive_service","car_wash")):
            name = label or "River Wash"
            md = tile(frame,label,[name],kind,subtype="")
            layers = md.cell(None).layers
            layers["places"].attrs.update(basic_category=np.array([category]),taxonomy=np.array([taxonomy]))
            layers["buildings"].attrs.update(height=np.array([4.7]),roof_shape=np.array(["flat"]))
            with (self.subTest(name=name),tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ,BNE_STOREFRONTS="1",BNE_LOCAL_BUSINESS_SIGNS="1"),
                  patch("threedmr.region_ids",return_value=[]),
                  patch("buildings.road_lines",return_value=[np.array([[0.,15.],[256.,15.]])])):
                def generate():
                    return buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros(np.shape(a)),cache_dir=cache)[1]
                entry, = generate()
                self.assertEqual(entry["local"]["title"],label or "Car Wash")
                layers["places"].attrs.update(basic_category=np.array(["atm"]),taxonomy=np.array(["atm"]))
                self.assertFalse(generate())

    @patch("storeidentity.MAPPED_NAMES", True)
    def test_independent_mall_tenant_door_or_outdoor_promenade(self):
        import streamedstores
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "Station Outlets", ["Oak Kitchen"], "retail")
        layers = md.cell(None).layers
        layers["buildings"].attrs["src_record"] = np.array(["w44@1"])
        layers["buildings"].attrs["height"] = np.array([8.])
        layers["places"].attrs["basic_category"] = np.array(["restaurant"])
        lat,lon,_ = frame.enu_to_geodetic(50.,33.,0.)
        layers["places"].lonlat[:] = [lon,lat]
        def generate(walk, entrance, expected="Restaurant"):
            def lines(*args, **kwargs):
                if kwargs.get("only_sub"):
                    return []
                if kwargs.get("classes") == {"pedestrian","footway"}:
                    return [walk] if walk is not None else []
                return []  # An outdoor mall can be served entirely by pedestrian paths here.
            with (tempfile.TemporaryDirectory() as cache,
                  patch.dict(os.environ,BNE_STOREFRONTS="1",BNE_LOCAL_BUSINESS_SIGNS="1"),
                  patch("threedmr.region_ids",return_value=[]),
                  patch.object(streamedstores,"entrance_index",return_value={"w44":entrance}),
                  patch("buildings.road_lines",side_effect=lines)):
                entries = buildings.road_tile_items(frame,0,0,md,lambda a,b: np.zeros(np.shape(a)),cache_dir=cache)[1]
                for row in entries:
                    self.assertIsNone(row["storefront_host"])
                    self.assertEqual(row["local"]["title"],expected)
                    verts = threedmr.load_local_model(cache,row["local"])[0]
                    self.assertAlmostEqual(np.ptp(verts[:,2]),6.88,places=2)
                return len(entries)
        self.assertEqual(generate(None,[]),0)
        self.assertEqual(generate(np.array([[0.,34.],[100.,34.]]),[]),0)
        self.assertEqual(generate(np.array([[0.,25.],[100.,25.]]),[]),1)
        lat,lon,_ = frame.enu_to_geodetic(50.,30.,0.)
        self.assertEqual(generate(None,[(lon,lat,1)]),1)
        layers["places"].lonlat[:] = [lon,lat]
        self.assertEqual(generate(None,[(lon,lat,1)], "Oak Kitchen"),1)
        lat,lon,_ = frame.enu_to_geodetic(70.,30.,0.)
        self.assertEqual(generate(None,[(lon,lat,1)]),0)

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
                self.assertEqual([m["local"]["title"] for m in generated], [identity_for("Kroger").label])
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
            self.assertEqual(building["local"]["title"], "Patty Regent")
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
