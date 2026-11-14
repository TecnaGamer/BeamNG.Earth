"""Building parts judged as pieces of their building (streets.gl comparison 2026-11-14): siblings' material and colour
only from a majority, no sheds / houses / masts by a part's own size, tiny tall parts and thin parts kept, crowns in
metal, one spire per church on its tower part, the skyline's body under a dome or a tower, domes over any outline
(earthfast.dome_roof). Plus the landmarks the comparison measured, from a small fixture of their Overture rows
(release 2026-10-21.0: the Empire State Building, the Chrysler Building, Trinity Church, the US Capitol, One World
Trade Center) as regression checks: no network."""
import json
import math
import sys
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld  # noqa: E402
import geodesy  # noqa: E402
from polygon import Path as PolyPath  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "landmark_parts.json"
FLAT = staticmethod(lambda la, lo: np.zeros(np.shape(la)))
STR_KEYS = {"class", "subtype", "roof_shape", "name", "facade_material", "facade_color", "roof_material", "roof_color",
            "roof_orientation", "src_dataset", "src_record", "building_id"}


class Feats:
    """A buildings / building_parts layer as tile_buildings reads it: ids, kind, attrs, parts(i) (lon, lat rings)."""

    def __init__(self, ids, rings, attrs):
        self.ids = np.array(ids, object)
        self.kind = np.full(len(ids), 3, np.uint8)
        self.rings = [np.asarray(r, np.float64) for r in rings]
        self.attrs = {}
        for k, v in attrs.items():
            if k in STR_KEYS:
                self.attrs[k] = np.array(["" if x is None else str(x) for x in v], object)
            else:
                self.attrs[k] = np.array([np.nan if x is None else float(x) for x in v], np.float64)

    def parts(self, i):
        return [[self.rings[i]]]


def layers_of(rec):
    """(buildings, parts) layers of one fixture record."""
    b = rec["building"]
    bkeys = [k for k in b if k not in ("id", "ring")]
    blayer = Feats([b["id"]], [b["ring"]], {k: [b[k]] for k in bkeys})
    pkeys = sorted({k for p in rec["parts"] for k in p if k not in ("id", "ring")})
    players = Feats([p["id"] for p in rec["parts"]], [p["ring"] for p in rec["parts"]],
                    {k: [p.get(k) for p in rec["parts"]] for k in pkeys})
    return blayer, players


def frame_and_lonlat(lat=40.0, lon=-74.0):
    f = geodesy.EnuFrame(lat, lon)

    def ll(x, y, w, d):
        e = np.array([x, x + w, x + w, x, x], np.float64)
        n = np.array([y, y, y + d, y + d, y], np.float64)
        la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
        return np.stack([lo, la], 1)
    return f, ll


def city(fn):
    """A test in a city core (tile coverage: not a small town, dense), as the landmarks' tiles are."""
    def run(self, *a, **kw):
        with unittest.mock.patch.object(bld, "TOWN_COVERAGE", -1.0), \
                unittest.mock.patch.object(bld, "BRICK_WALK_COVERAGE", -1.0):
            return fn(self, *a, **kw)
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def tile(f, blayer, players, **kw):
    return bld.tile_buildings(f, -256, -256, 256, 256, [blayer], FLAT.__func__, part_layers=[players], **kw)


def base(look):
    return str(look).split("|")[0]


def top(spec):
    return float(spec.min_h or 0.0) + float(spec.height)


class PartRulesTest(unittest.TestCase):
    def _building(self, parts, height=100.0, cls="office", **battrs):
        """A 40 x 40 m building with parts [(x, y, w, d, attrs)] (own attrs: height, min_height, ...)."""
        f, ll = frame_and_lonlat()
        b = Feats(["B"], [ll(-20, -20, 40, 40)], {"class": [cls], "height": [height], "min_height": [None],
                                                  "src_dataset": ["OpenStreetMap"],
                                                  **{k: [v] for k, v in battrs.items()}})
        keys = sorted({k for p in parts for k in p[4]} | {"height", "min_height", "num_floors", "min_floor"})
        pl = Feats([f"p{i}" for i in range(len(parts))], [ll(*p[:4]) for p in parts],
                   {"building_id": ["B"] * len(parts), **{k: [p[4].get(k) for p in parts] for k in keys}})
        return f, b, pl

    @city
    def test_siblings_material_only_from_a_majority(self):
        """One part of four mapped concrete (the Empire State Building's base): the others keep their own look; two of
        four mapped metal (New York by Gehry's parts): the bare ones take it."""
        parts = [(-20, -20, 40, 40, {"height": 17.0, "facade_material": "concrete"}),
                 (-15, -15, 30, 30, {"height": 60.0, "min_height": 17.0}),
                 (-10, -10, 20, 20, {"height": 90.0, "min_height": 60.0}),
                 (-5, -5, 10, 10, {"height": 100.0, "min_height": 90.0})]
        items = tile(*self._building(parts))
        concrete = [s for s, _, _ in items if base(s.facade) == "bne_bridge_concrete"]
        self.assertEqual(len(concrete), 1, "only the part mapped concrete")
        parts[1][4]["facade_material"] = "concrete"
        items = tile(*self._building(parts))
        self.assertEqual(sum(base(s.facade) == "bne_bridge_concrete" for s, _, _ in items), 4, "half of them: all")

    @city
    def test_siblings_stone_from_any_share(self):
        """The Palais de Justice, Paris: a sixth of its parts mapped stone, the rest bare: all stone (only the stones
        spread from a minority; the Empire State Building's concrete base does not, above)."""
        parts = [(-20, -20, 40, 40, {"height": 20.0, "facade_material": "stone"})] + \
            [(-20 + 8 * i, 20, 8, 10, {"height": 18.0}) for i in range(5)]
        items = tile(*self._building(parts, height=20.0, cls="civic"))
        self.assertTrue(all(base(s.facade) in (bld.STONE, bld.STONE_RUBBLE) for s, _, _ in items),
                        [s.facade for s, _, _ in items])

    @city
    def test_siblings_colour_only_from_a_majority(self):
        parts = [(-20, -20, 40, 40, {"height": 20.0}), (-15, -15, 30, 30, {"height": 40.0, "min_height": 20.0}),
                 (-2, -2, 4, 4, {"height": 50.0, "min_height": 40.0, "facade_color": "#40484A"})]
        items = tile(*self._building(parts, height=50.0, cls="government"))
        dyed = [s for s, _, _ in items if "0.251_0.282_0.290" in str(s.facade)]
        self.assertEqual(len(dyed), 1, "the statue alone")

    @city
    def test_a_small_part_is_no_shed_house_or_row(self):
        """A lantern ring of 30 m² at 67 m on a 90 m building: the building's walls, not a shed's siding or a
        house's."""
        parts = [(-20, -20, 40, 40, {"height": 60.0}),
                 (-2.7, -2.7, 5.5, 5.5, {"height": 70.0, "min_height": 67.0, "facade_material": "stone"}),
                 (-2.7, -2.7, 5.5, 5.5, {"height": 30.0})]
        items = tile(*self._building(parts, height=90.0, cls="government"))
        small = [s for s, _, _ in items if abs(bld._area2(s.ring)) / 2 < 40]
        self.assertEqual(len(small), 2)
        for s in small:
            self.assertNotIn(base(s.facade), (bld.SIDING, bld.STUCCO), s.facade)
            self.assertNotIn(s.cls, ("shed", "elevator"))
            self.assertFalse(s.porch)
        self.assertEqual(base(max(small, key=top).facade), bld.STONE, "its mapped stone")

    @city
    def test_a_slender_part_is_a_mast_only_at_the_building_top(self):
        """The Chrysler Building's 185 m wings on 32 m² are walls with windows; its needle to the top is a mast."""
        parts = [(-20, -20, 40, 40, {"height": 199.0}),
                 (20, -3, 5.6, 5.6, {"height": 185.0}),
                 (-1, -1, 2, 2, {"height": 319.0, "min_height": 272.0, "roof_shape": "pyramidal", "roof_height": 40.0}),
                 (-5, -5, 10, 10, {"height": 272.0, "min_height": 199.0})]
        items = tile(*self._building(parts, height=319.0, cls=""))
        wing = [s for s, _, _ in items if abs(top(s) - 185.0) < 0.5]
        self.assertEqual(len(wing), 1)
        self.assertNotEqual(wing[0].cls, "mast")
        self.assertNotEqual(wing[0].windows, False)
        needle = [s for s, _, _ in items if (s.min_h or 0) >= 271]
        self.assertEqual([s.cls for s in needle], ["mast"])

    def test_a_slender_part_mapped_steel_is_a_mast_anywhere(self):
        s = bld.plan(bld.clean_ring(np.array([(0, 0), (4, 0), (4, 4), (0, 4)], np.float64)),
                     {"class": "", "height": 60.0, "facade_material": "steel", "building_height": 200.0,
                      "_part_min": 0.0})
        self.assertEqual(s.cls, "mast")

    @city
    def test_tiny_parts_left_out_unless_tall(self):
        parts = [(-20, -20, 40, 40, {"height": 30.0}),
                 (-0.45, -0.45, 0.9, 0.9, {"height": 77.0, "min_height": 30.0}),     # 0.8 m², 47 m: a needle
                 (10, 10, 1.5, 1.5, {"height": 33.0, "min_height": 30.0})]           # 2.25 m², 3 m: a vent
        items = tile(*self._building(parts, height=77.0))
        tops = sorted(round(top(s)) for s, _, _ in items)
        self.assertEqual(tops, [30, 77], "the needle kept, the vent left out")

    def test_a_thin_part_keeps_its_thickness(self):
        a = {"height": np.array([1.0, 142.0, 5.0]), "min_height": np.array([np.nan, 141.0, 5.0]),
             "num_floors": np.array([np.nan] * 3), "min_floor": np.array([np.nan] * 3)}
        self.assertEqual(bld._part_heights(a, 0), (1.0, 0.0), "a 1 m terrace")
        self.assertEqual(bld._part_heights(a, 1), (1.0, 141.0), "a 1 m cornice band")
        self.assertIsNone(bld._part_heights(a, 2), "no thickness: no height of its own")

    def test_a_tall_buildings_crown_is_metal(self):
        ring = bld.clean_ring(np.array([(0, 0), (6, 0), (6, 12), (0, 12)], np.float64))
        s = bld.plan(ring, {"class": "", "height": 10.0, "roof_shape": "round", "roof_height": 4.0,
                            "building_height": 260.0, "_part_min": 250.0})
        self.assertEqual(base(s.roof_mat), bld.ROOF_METAL)
        house = bld.plan(ring, {"class": "house", "roof_shape": "gabled"}, lat=40.0, lon=-74.0)
        self.assertNotEqual(base(house.roof_mat), bld.ROOF_METAL, "(a house keeps the region's roofs)")

    @city
    def test_one_spire_on_the_church_tower(self):
        """A church in parts: no steeple on each part; a spire over its tower part (the tallest flat part, square)."""
        parts = [(-20, -8, 40, 16, {"height": 21.0, "roof_shape": "gabled", "roof_height": 4.0}),   # nave
                 (-20, -14, 40, 6, {"height": 12.0}), (-20, 8, 40, 6, {"height": 12.0}),             # aisles
                 (20, -5, 10, 10, {"height": 41.0}),                                                   # tower
                 (20, -5, 1.0, 1.0, {"height": 48.0, "min_height": 41.0, "roof_shape": "pyramidal"})]  # pinnacle
        f, b, pl = self._building(parts, height=None, cls="church")
        items = tile(f, b, pl)
        st = [s for s, _, _ in items if s.steeple]
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0].steeple, bld.STEEPLE_TOP)
        self.assertAlmostEqual(top(st[0]), 41.0, delta=0.01)
        P = bld.build([(st[0], 0.0, 0.0)])[0]
        self.assertAlmostEqual(P[:, 2].max(), 41.0 + bld.STEEPLE_TOP_RISE * 10.0, delta=0.6)

    @city
    def test_no_spire_of_ours_where_the_map_has_one(self):
        parts = [(-20, -8, 40, 16, {"height": 21.0}), (20, -5, 10, 10, {"height": 41.0}),
                 (20, -5, 10, 10, {"height": 86.0, "min_height": 41.0, "roof_shape": "pyramidal", "roof_height": 45.0})]
        items = tile(*self._building(parts, height=None, cls="church"))
        self.assertFalse([s for s, _, _ in items if s.steeple])

    @city
    def test_no_steeple_on_an_outline_kept_under_its_parts(self):
        """A church whose parts cover under half its outline (the outline drawn too, PARTS_COVER_MIN): the outline is
        no church of its own, its tower part has the one spire (Codex review of a92a4828)."""
        parts = [(-20, -8, 40, 16, {"height": 20.0}), (20, -5, 10, 10, {"height": 41.0})]
        items = tile(*self._building(parts, height=15.0, cls="church"))
        self.assertEqual(len(items), 3, "the outline and both parts")
        st = [s for s, _, _ in items if s.steeple]
        self.assertEqual([s.steeple for s in st], [bld.STEEPLE_TOP])
        self.assertAlmostEqual(top(st[0]), 41.0, delta=0.01)

    @city
    def test_no_steeple_on_a_skyline_body(self):
        parts = [(-20, -20, 40, 40, {"height": 20.0}), (20, -5, 10, 10, {"height": 60.0})]
        f, b, pl = self._building(parts, height=90.0, cls="church")
        sky = tile(f, b, pl, min_size=(bld.SKY_SLENDER_H, bld.SKY_MIN_AREA_M2), parts_min_top=bld.SKY_PARTS_MIN_M)
        body = [s for s, _, _ in sky if abs(top(s) - (1600 * 20.0 + 100 * 60.0) / 1700) < 0.05]
        self.assertEqual(len(body), 1)
        self.assertFalse(body[0].steeple)
        sky = tile(*self._building([(-20, -20, 40, 40, {"height": 20.0})], height=90.0, cls="church"),
                   min_size=(bld.SKY_SLENDER_H, bld.SKY_MIN_AREA_M2), parts_min_top=bld.SKY_PARTS_MIN_M)
        self.assertEqual([(round(top(s), 1), bool(s.steeple)) for s, _, _ in sky], [(20.0, False)],
                         "no part over it: the body alone, no steeple")

    def test_a_church_without_parts_keeps_its_steeple(self):
        s = bld.plan(bld.clean_ring(np.array([(0, 0), (30, 0), (30, 12), (0, 12)], np.float64)),
                     {"class": "church", "seed_id": "w1"}, lat=40.0, lon=-74.0)
        self.assertEqual(s.steeple, "spire")

    @city
    def test_skyline_body_under_its_dome(self):
        """Far away (the skyline, parts only from SKY_PARTS_MIN_M): a 21 m body at its parts' height with its 67 m
        dome on it, not one box to the 87.6 m statue."""
        parts = [(-60, -20, 120, 40, {"height": 21.0}),
                 (-15, -15, 30, 30, {"height": 51.8, "min_height": 21.0}),
                 (-15, -15, 30, 30, {"height": 67.3, "min_height": 51.8, "roof_shape": "dome", "roof_height": 12.0})]
        f, b, pl = self._building(parts, height=87.6, cls="government")
        b.rings = [frame_and_lonlat()[1](-60, -20, 120, 40)]
        sky = tile(f, b, pl, min_size=(bld.SKY_SLENDER_H, bld.SKY_MIN_AREA_M2), parts_min_top=bld.SKY_PARTS_MIN_M)
        tops = sorted(round(top(s), 1) for s, _, _ in sky)
        self.assertEqual(len(tops), 3)
        self.assertAlmostEqual(tops[0], (4800 * 21.0 + 900 * 51.8 + 900 * 67.3) / 6600, delta=0.05,
                               msg="the body at its parts' mean height")
        self.assertNotIn(87.6, tops)
        dome = [s for s, _, _ in sky if s.roof == "dome"]
        self.assertEqual(len(dome), 1)

    def test_skyline_body_rules(self):
        ring = lambda w: np.array([(0, 0), (w, 0), (w, w), (0, w)], np.float64)     # noqa: E731
        low = [(ring(50), (20.0, 0.0), {}), (ring(10), (30.0, 20.0), {})]        # a 50 m tower part on a 20 m body
        tall, body = bld.sky_body(low, 60.0)
        self.assertEqual(len(tall), 1)
        self.assertAlmostEqual(body, (2500 * 20 + 100 * 50) / 2600, places=9)
        self.assertEqual(bld.sky_body([(ring(50), (20.0, 0.0), {})], 22.0), ([], None), "as mapped: the outline")
        self.assertEqual(bld.sky_body([(ring(50), (20.0, 0.0), {})], 80.0), ([], 20.0), "a spire's height: the body")


def lum(look):
    c = bld._sky_colour(look, 1.0)
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


class InGameCheckTest(unittest.TestCase):
    """The four looks the 2026-11-14 in-game check of the branch found wrong: a church's tower band (New York's measured
    massing) with no spire, the Empire State Building and 1 Wall Street as dark glass, the Capitol's dark dome."""
    _building = PartRulesTest._building

    @city
    def test_spire_on_a_tower_band_under_the_outline_height(self):
        """St. Nicholas of Myra, New York: an 18.1 m body and a 14 x 10 m tower band from 18.1 to 21.4 m (the city's
        massing: a band per roof level, none on the ground), the outline 31.4 m: one spire on the band, to 31.4 m."""
        parts = [(-17, -8, 34, 16, {"height": 18.1}), (5, -6, 14, 10, {"height": 21.4, "min_height": 18.1})]
        items = tile(*self._building(parts, height=31.4, cls="church"))
        st = [s for s, _, _ in items if s.steeple]
        self.assertEqual(len(st), 1)
        self.assertAlmostEqual(top(st[0]), 21.4, delta=0.01)
        self.assertTrue(bld._is_steeple_top(st[0].steeple), st[0].steeple)
        P = bld.build([(st[0], 0.0, 0.0)])[0]
        self.assertAlmostEqual(P[:, 2].max(), 31.4, delta=0.05, msg="the spire up to the building's height")
        self.assertLessEqual(np.ptp(P[P[:, 2] > 23.0][:, 0]), 2 * bld.STEEPLE_TOP_MAX_HW, "a spire, not a hip roof")
        items = tile(*self._building(parts, height=None, cls="church"))
        self.assertFalse([s for s, _, _ in items if s.steeple], "3.3 m over the body, no taller outline: no tower")

    @city
    def test_spire_on_a_massing_tower_column(self):
        """St. Mark's in-the-Bowery: a 6 x 6 m band 16.2-23.9 m over a 16.2 m church: the tower's spire."""
        parts = [(-20, -10, 40, 20, {"height": 5.6}), (-20, -10, 38, 20, {"height": 16.2, "min_height": 5.6}),
                 (18, -3, 6, 6, {"height": 23.9, "min_height": 16.2})]
        st = [s for s, _, _ in tile(*self._building(parts, height=16.4, cls="church")) if s.steeple]
        self.assertEqual([round(top(s), 1) for s in st], [23.9])

    @city
    def test_no_spire_where_the_map_tops_the_tower(self):
        """The Church of the Most Holy Redeemer: a dome mapped on its 48 m tower; St. Paul's Chapel: a pyramid."""
        for shape in ("dome", "pyramidal", "onion"):
            parts = [(-20, -8, 40, 16, {"height": 28.0}), (20, -5, 10, 10, {"height": 48.0}),
                     (21, -4, 8, 8, {"height": 60.0, "min_height": 48.0, "roof_shape": shape})]
            items = tile(*self._building(parts, height=None, cls="church"))
            self.assertFalse([s for s, _, _ in items if s.steeple], shape)

    @city
    def test_twin_towers_both_spired(self):
        parts = [(-20, -10, 30, 20, {"height": 20.5}), (10, -10, 6, 6, {"height": 35.0}),
                 (10, 4, 6, 6, {"height": 35.0})]
        st = [s for s, _, _ in tile(*self._building(parts, height=None, cls="church")) if s.steeple]
        self.assertEqual(len(st), 2)

    @city
    def test_a_domes_drum_band_is_no_tower(self):
        """St. Bartholomew's, New York: its dome's drum a 16 x 16 m band in the massing (over STEEPLE_TOWER_M2)."""
        parts = [(-20, -20, 40, 40, {"height": 26.7}), (-8, -8, 16, 16, {"height": 43.8, "min_height": 26.7})]
        self.assertFalse([s for s, _, _ in tile(*self._building(parts, height=41.6, cls="church")) if s.steeple])

    def test_a_supertall_in_a_stone_colour_is_masonry(self):
        ring = bld.clean_ring(np.array([(0, 0), (30, 0), (30, 30), (0, 30)], np.float64))
        for col, glass in (("#DFD4D0", False), ("#D7DBDE", False), ("#DCD5B9", False), ("#2F4F6F", True),
                           ("#C0C0C0", True), ("#46433A", True), (None, True)):
            s = bld.plan(ring, {"class": "office", "height": 200.0, "facade_color": col}, lat=40.7, lon=-74.0,
                         town=False, dense=True)
            self.assertEqual(base(s.facade) == bld.OFFICE, glass, (col, s.facade))
            if not glass:
                self.assertEqual((base(s.facade), s.band), (bld.FACADE, bld.BAND_TALL))
        s = bld.plan(ring, {"class": "office", "height": 200.0, "facade_color": "#DFD4D0", "facade_material": "glass"},
                     lat=40.7, lon=-74.0)
        self.assertEqual(base(s.facade), bld.OFFICE, "mapped glass stays glass")

    @city
    def test_bare_parts_follow_their_stone_coloured_siblings(self):
        """The Empire State Building: a concrete base part, a pale grey on its shaft, nothing on the setbacks: all
        masonry, the base its concrete; a part mapped glass keeps the bare ones glass."""
        parts = [(-20, -20, 40, 40, {"height": 17.0, "facade_material": "concrete"}),
                 (-15, -15, 30, 30, {"height": 300.0, "facade_color": "#D7DBDE"}),
                 (-10, -10, 20, 20, {"height": 330.0, "min_height": 300.0}),
                 (-8, -8, 16, 16, {"height": 350.0, "min_height": 330.0})]
        items = tile(*self._building(parts, height=350.0))
        self.assertFalse([s.facade for s, _, _ in items if base(s.facade) == bld.OFFICE])
        self.assertEqual(sum(base(s.facade) == bld.FACADE and s.band == bld.BAND_TALL for s, _, _ in items), 3)
        parts[2][4]["facade_material"] = "glass"
        parts.append((-4, -4, 8, 8, {"height": 360.0, "min_height": 350.0}))
        items = tile(*self._building(parts, height=360.0))
        self.assertEqual(sum(base(s.facade) == bld.OFFICE for s, _, _ in items), 3, "the glass one and the bare ones")

    @city
    def test_no_spire_under_a_mapped_roof_over_a_step(self):
        """A 48 m 10 x 10 m tower under a centred 8 x 8 m mapped dome (or spire) from 50 to 60 m: a 2 m gap, still
        the map's own top, no spire of ours through it (Codex review of 3be5f78e)."""
        for shape in ("dome", "pyramidal"):
            parts = [(-20, -8, 40, 16, {"height": 21.0}), (20, -5, 10, 10, {"height": 48.0}),
                     (21, -4, 8, 8, {"height": 60.0, "min_height": 50.0, "roof_shape": shape})]
            self.assertFalse([s for s, _, _ in tile(*self._building(parts, height=None, cls="church")) if s.steeple],
                             shape)

    @city
    def test_stone_colour_by_area_before_the_siblings_count(self):
        """A 900 m² pale part, two 16 m² dark ones, three bare ones on a 200 m tower: the bare ones take the pale
        part's stone colour (by area), not the dark one the most parts carry (Codex review of 3be5f78e)."""
        parts = [(-15, -15, 30, 30, {"height": 120.0, "facade_color": "#DFD4D0"}),
                 (15, -15, 4, 4, {"height": 60.0, "facade_color": "#2F4F6F"}),
                 (15, 11, 4, 4, {"height": 60.0, "facade_color": "#2F4F6F"}),
                 (-10, -10, 20, 20, {"height": 160.0, "min_height": 120.0}),
                 (-6, -6, 12, 12, {"height": 190.0, "min_height": 160.0}),
                 (-4, -4, 8, 8, {"height": 200.0, "min_height": 190.0})]
        items = tile(*self._building(parts, height=200.0))
        glass = sorted(round(abs(bld._area2(s.ring)) / 2) for s, _, _ in items if base(s.facade) == bld.OFFICE)
        self.assertEqual(glass, [16, 16], "the dark parts alone")
        self.assertEqual(sum(base(s.facade) == bld.FACADE and s.band == bld.BAND_TALL for s, _, _ in items), 4)

    def test_stone_colour_judged_as_mapped(self):
        """Overture's name hexes are drawn in muted shades (#D3D3D3 is lightgrey at 0.75): the masonry test reads the
        mapped value, so a hex, its neighbour, its shorthand and its name agree (Codex review of 3be5f78e)."""
        groups = ((True, ("#D3D3D3", "#d3d3d4", "#D2D3D3", "lightgrey", "lightgray", "Light Grey")),
                  (True, ("#F5F5DC", "#F5F5DD", "beige")),
                  (True, ("#DDDDDD", "#ddd", "#DDDDDDFF")),
                  (False, ("#C0C0C0", "#C0C0C1", "silver")),
                  (False, ("#808080", "grey", "gray", "#777")),
                  (False, ("#000", "black", "#000000", "#010101")))
        ring = bld.clean_ring(np.array([(0, 0), (30, 0), (30, 30), (0, 30)], np.float64))
        for stone, cols in groups:
            for c in cols:
                self.assertEqual(bld.masonry_colour(bld.source_rgb(c)), stone, c)
                s = bld.plan(ring, {"class": "office", "height": 200.0, "facade_color": c}, lat=40.7, lon=-74.0)
                self.assertEqual(base(s.facade) != bld.OFFICE, stone, (c, s.facade))

    def test_a_dome_takes_its_walls_light_look(self):
        ring = np.array([(10 * math.cos(a), 10 * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)])
        ring = bld.clean_ring(ring)
        for attrs in ({"facade_material": "stone", "roof_material": "slate"}, {"facade_material": "stone"},
                      {"facade_material": "brick"}, {}):
            s = bld.plan(ring, dict({"class": "government", "height": 30.0, "roof_shape": "dome", "roof_height": 8.0,
                                     "building_height": 90.0, "_part_min": 20.0}, **attrs), lat=38.9, lon=-77.0)
            self.assertEqual(s.roof, "dome")
            self.assertGreaterEqual(lum(s.roof_mat), 140, (attrs, s.roof_mat))
        s = bld.plan(ring, {"class": "government", "height": 30.0, "roof_shape": "dome", "roof_material": "copper"})
        self.assertEqual(s.roof_mat, bld.custom_look(bld.ROOF_METAL, bld.COLOR_NAMES["copper"]), "mapped: kept")
        s = bld.plan(ring, {"class": "church", "height": 30.0, "roof_shape": "dome", "facade_material": "brick",
                            "roof_material": "slate"})
        self.assertIn(base(s.roof_mat), (bld.ROOF_SLATE_DARK, bld.ROOF_SLATE_ROUND), "slate over brick: slate")


class DomeTest(unittest.TestCase):
    def _l_ring(self):
        # an L whose corners' mean lies outside it (the old dome's centre)
        return np.array([(0, 0), (20, 0), (20, 4), (4, 4), (4, 20), (0, 20)], np.float64)

    def test_a_dome_over_an_l_stays_inside_it(self):
        m = bld.Mesh()
        bld._dome(m, self._l_ring(), 10.0, 6.0, (0, (255, 255, 255, 255)))
        P, *_ = m.arrays()
        self.assertGreater(len(P), 0)
        ring = self._l_ring()
        path = PolyPath(ring)

        def on_edge(p):
            for a, b in zip(ring, np.roll(ring, -1, 0)):
                d = b - a
                t = min(max(float(np.dot(p - a, d) / np.dot(d, d)), 0.0), 1.0)
                if np.hypot(*(a + t * d - p)) < 1e-6:
                    return True
            return False
        inside = np.array([bool(path.contains_points(p[None, :])[0]) or on_edge(p) for p in P[:, :2]])
        self.assertTrue(inside.all())
        self.assertAlmostEqual(P[:, 2].max(), 16.0, delta=1e-9)

    def test_degenerate_outlines_no_dome_and_fast(self):
        import time
        for ring in ([(0, 0), (1, 1e-8), (2, 2e-8)],                                  # collinear
                     [(0, 0), (100, 0), (100, 1e-6), (1e-6, 1e-6), (1e-6, 100), (0, 100)],   # a 1e-8 sliver L
                     [(0, 0), (10, 0), (10, float("nan")), (0, 10)],
                     [(0, 0), (10, 0), (10, float("inf")), (0, 10)],
                     [(0, 0), (0, 0), (10, 0), (10, 10), (10, 10), (0, 10), (0, 0)]):      # repeated corners
            m = bld.Mesh()
            t0 = time.perf_counter()
            bld._dome(m, np.array(ring, np.float64), 0.0, 3.0, (0, (255, 255, 255, 255)))
            self.assertLess(time.perf_counter() - t0, 0.2, ring)
            P = m.arrays()[0]
            self.assertTrue(np.isfinite(P).all())
        m = bld.Mesh()
        bld._dome(m, np.array([(0, 0), (1, 1e-8), (2, 2e-8)], np.float64), 0.0, 3.0, (0, (255, 255, 255, 255)))
        self.assertEqual(len(m.arrays()[0]), 0, "collinear: no dome")
        m = bld.Mesh()
        bld._dome(m, np.array([(0, 0), (0, 0), (10, 0), (10, 10), (10, 10), (0, 10), (0, 0)], np.float64), 0.0, 3.0,
                  (0, (255, 255, 255, 255)))
        P = m.arrays()[0]
        self.assertEqual(int(np.sum(P[:, 2] == 0.0)), 8, "repeated corners dropped: the square's four eave quads")

    def test_a_cupola_is_a_dome(self):
        ring = np.array([(0.5 * math.cos(a), 0.5 * math.sin(a))
                         for a in np.linspace(0, 2 * math.pi, 12, endpoint=False)])
        s = bld.plan(bld.clean_ring(ring), {"class": "church", "height": 3.0, "roof_shape": "dome"})
        self.assertEqual(s.roof, "dome")

    def test_dome_faces_and_vertices(self):
        ring = np.array([(10 * math.cos(a), 10 * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)])
        m = bld.Mesh()
        bld._dome(m, ring, 0.0, 5.0, (0, (255, 255, 255, 255)))
        P, UV, T, M, C = m.arrays()
        k = len(bld.DOME_PROFILE["dome"])
        self.assertEqual(len(T), 16 * (k - 1) * 2, "a dome: quads up to its top ring (cos 90 deg is not quite 0)")
        self.assertEqual(len(P), 16 * (k - 1) * 4)
        mo = bld.Mesh()
        bld._dome(mo, ring, 0.0, 5.0, (0, (255, 255, 255, 255)), shape="onion")
        ko = len(bld.DOME_PROFILE["onion"])
        self.assertEqual(len(mo.arrays()[2]), 16 * (ko - 2) * 2 + 16, "an onion: quads, then apex triangles")
        np.testing.assert_allclose(UV, P[:, :2] / bld.ROOF_UV_M)
        m2 = bld.Mesh()
        bld._dome(m2, ring, 0.0, 5.0, (0, (255, 255, 255, 255)))
        self.assertEqual(m2.arrays()[0].tobytes(), P.tobytes(), "the same numbers every time")


class LandmarkRegressionTest(unittest.TestCase):
    """The comparison's landmarks from their Overture rows (fixtures/landmark_parts.json), drawn by today's rules."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text())

    def _items(self, name, **kw):
        rec = self.data[name]
        f = geodesy.EnuFrame(rec["lat"], rec["lon"])
        with unittest.mock.patch.object(bld, "TOWN_COVERAGE", -1.0):
            return bld.tile_buildings(f, -256, -256, 256, 256, [layers_of(rec)[0]], FLAT.__func__,
                                      part_layers=[layers_of(rec)[1]], **kw)

    def test_capitol(self):
        items = self._items("capitol")
        self.assertGreaterEqual(len(items), 20)
        self.assertFalse([s for s, _, _ in items if base(s.facade) in (bld.SIDING, bld.STUCCO)], "no siding")
        self.assertLessEqual(sum("0.251_0.282_0.290" in str(s.facade) for s, _, _ in items), 2, "bronze: its 2 parts")
        self.assertGreaterEqual(sum(base(s.facade) == bld.STONE for s, _, _ in items), 15, "marble: stone")
        self.assertTrue([s for s, _, _ in items if s.roof == "dome" and top(s) > 50], "the dome")
        self.assertTrue([s for s, _, _ in items if top(s) >= 87.0], "the Statue of Freedom (2.7 m²)")
        sky = self._items("capitol", min_size=(bld.SKY_SLENDER_H, bld.SKY_MIN_AREA_M2),
                          parts_min_top=bld.SKY_PARTS_MIN_M)
        self.assertFalse([s for s, _, _ in sky if abs(bld._area2(s.ring)) / 2 > 10000 and top(s) > 80],
                         "far away: not one 87.6 m box")
        self.assertTrue([s for s, _, _ in sky if s.roof == "dome"], "far away: its dome")
        dome = max((s for s, _, _ in items if s.roof == "dome"), key=lambda s: abs(bld._area2(s.ring)))
        self.assertGreaterEqual(lum(dome.roof_mat), 150, "its marble dome (Overture: slate) light, not dark slate")
        sky_dome = max((s for s, _, _ in sky if s.roof == "dome"), key=lambda s: abs(bld._area2(s.ring)))
        self.assertEqual(sky_dome.roof_mat, dome.roof_mat)
        tops = [top(s) for s, _, _ in sky]
        for s, _, _ in sky:     # (far away: nothing floats; the drum's lowest ring, not tall, left a gap under it)
            if float(s.min_h or 0) > 0.1:
                self.assertTrue(any(abs(t - float(s.min_h)) <= 1.0 for t in tops), (s.min_h, top(s)))

    def test_chrysler(self):
        items = self._items("chrysler")
        masts = [s for s, _, _ in items if s.cls == "mast"]
        self.assertTrue(all(s.min_h >= 270 for s in masts), [(s.min_h, s.height) for s in masts])
        self.assertTrue(masts, "the needle kept")
        crown = [s for s, _, _ in items if s.roof == "round"]
        self.assertTrue(crown and all(base(s.roof_mat) == bld.ROOF_METAL for s in crown), "a metal crown")
        self.assertFalse([s for s, _, _ in items if base(s.facade) in (bld.SIDING, bld.STUCCO)])

    def test_trinity(self):
        items = self._items("trinity")
        st = [s for s, _, _ in items if s.steeple]
        self.assertEqual(len(st), 1, "one spire, on the tower")
        self.assertAlmostEqual(top(st[0]), 41.0, delta=0.5)

    def test_empire_state(self):
        items = self._items("empire_state")
        self.assertGreaterEqual(len(items), 10)
        concrete = [s for s, _, _ in items if base(s.facade) == "bne_bridge_concrete"]
        self.assertLessEqual(len(concrete), 1, "only the base mapped concrete")
        self.assertGreater(max(top(s) for s, _, _ in items), 329.0)
        self.assertFalse([s.facade for s, _, _ in items if base(s.facade) == bld.OFFICE],
                         "no dark glass: its parts' only colours are a stone's (the 2026-11-14 in-game check)")
        sky = self._items("empire_state", min_size=(bld.SKY_SLENDER_H, bld.SKY_MIN_AREA_M2),
                          parts_min_top=bld.SKY_PARTS_MIN_M)
        shaft = max((s for s, _, _ in sky if s.cls != "mast"), key=lambda s: abs(bld._area2(s.ring)) * top(s))
        self.assertGreaterEqual(lum(shaft.facade), 130, "pale from the Hudson, as the old concrete was")

    def test_one_wall_street(self):
        """1 Wall Street (Overture's "Bank of New York Building"): 42 parts in a light limestone colour, no material;
        its slender corner piers came out as plain steel masts, then glass like the rest: all masonry."""
        items = self._items("one_wall_street")
        self.assertGreaterEqual(len(items), 40)
        self.assertFalse([s for s, _, _ in items if s.cls == "mast"])
        self.assertFalse([s.facade for s, _, _ in items if base(s.facade) == bld.OFFICE])
        self.assertTrue(all(base(s.facade) == bld.FACADE and s.band == bld.BAND_TALL for s, _, _ in items))

    def test_one_world_trade_center(self):
        items = self._items("one_wtc")
        self.assertEqual(sum(s.roof == "skillion" for s, _, _ in items), 4, "its four tapering corners")
        self.assertEqual(sum(s.cls == "mast" for s, _, _ in items), 1, "its mast alone")

    def test_landmarks_build_the_same_every_time(self):
        for name in ("capitol", "trinity"):
            a = bld.build(self._items(name))
            b = bld.build(self._items(name))
            self.assertEqual(a[0].tobytes(), b[0].tobytes())
            self.assertEqual(a[2].tobytes(), b[2].tobytes())


if __name__ == "__main__":
    unittest.main()
