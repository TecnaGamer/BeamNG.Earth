"""airportbuildings: a control tower mapped as one footprint (named, or slender and tall by an apron) is drawn as a
concrete shaft, a glass cab and a dark cap; a terminal is told by the gate points at its outline (or its name) and takes
the terminal look at a height by its size; a building with parts of its own, and every building off an airport, is left
as it was."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import airportbuildings as ab  # noqa: E402
import buildings as bld  # noqa: E402
import geodesy  # noqa: E402
import mapgeom  # noqa: E402

FRAME = geodesy.EnuFrame(-27.39, 153.118)
BOX = (0.0, 0.0, 256.0, 256.0)
ELEV = lambda la, lo: np.zeros(np.shape(la))        # noqa: E731
_IDS = iter(range(10 ** 6))


def lonlat(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], np.float64),
                                      np.array([p[1] for p in pts], np.float64), 0.0)
    return np.stack([lo, la], 1)


def rect(x, y, w, d):
    return [(x, y), (x + w, y), (x + w, y + d), (x, y + d), (x, y)]


class Feats:
    """Buildings (or parts) layer: rings in metres east / north of the frame's origin, attributes by column."""

    def __init__(self, ids, rings, **attrs):
        self.ids = np.array(ids, object)
        self.kind = np.full(len(ids), 3, np.uint8)
        self.rings = [lonlat(r) for r in rings]
        self.attrs = {k: np.array(v, object if isinstance(v[0], str) else np.float64) for k, v in attrs.items()}

    def __len__(self):
        return len(self.ids)

    def parts(self, i):
        return [[self.rings[i]]]


def building(ring, **at):
    n = {"class": "", "height": np.nan, "num_floors": np.nan, "min_height": np.nan, "name": "", "has_parts": 0.0}
    n.update({k: v for k, v in at.items() if v is not None})
    return Feats(["b%d" % next(_IDS)], [ring], **{k: [v] for k, v in n.items()})


def infra(*rows):
    """Infrastructure Features of (kind, points, class[, subtype]) rows, subtype airport by default; points as lines / rings / one point."""
    base = next(_IDS)
    ids = np.array([f"{base:06d}{i:03d}" for i in range(len(rows))])
    geoms = [(r[0], [[lonlat(r[1])]]) for r in rows]
    f = mapgeom.build("infrastructure", ids, geoms)
    f.attrs = {"class": mapgeom.text_column([r[2] for r in rows]),
               "subtype": mapgeom.text_column([r[3] if len(r) > 3 else "airport" for r in rows]),
               "tags": mapgeom.text_column(["{}"] * len(rows)), "name": mapgeom.text_column([""] * len(rows)),
               "surface": mapgeom.text_column([None] * len(rows))}
    return f


def apron(x, y, w, d):
    return (mapgeom.POLYGON, rect(x, y, w, d), "apron")


def gate(x, y):
    return (mapgeom.POINT, [(x, y)], "airport_gate")


FIELD = (mapgeom.POLYGON, rect(-900, -900, 1800, 1800), "international_airport")


def items(layer, infra_layer=None, parts=(), airport=True):
    ctx = ab.context(FRAME, [infra_layer], *BOX[:2]) if airport and infra_layer is not None else None
    return bld.tile_buildings(FRAME, *BOX, [layer], ELEV, part_layers=list(parts), airport=ctx)


def tops(its):
    return sorted((round(s.min_h or 0.0, 2), round(s.height, 2)) for s, _, _ in its)


def material(spec):
    return spec.facade.split("|")[0]


class TowerTest(unittest.TestCase):
    def test_a_named_tower_with_no_parts(self):
        """Brisbane's "Air Traffic Control Tower": 75 m, 219 m2, one footprint, no parts: a shaft, a cab and a cap."""
        b = building(rect(100, 100, 15, 14.6), name="Brisbane Airport - Air Traffic Control Tower", height=75.0)
        its = items(b, infra(FIELD, apron(0, 0, 90, 90)))
        self.assertEqual(len(its), 3)
        self.assertEqual(tops(its), [(0.0, 66.0), (66.0, 5.0), (71.0, 4.0)], "shaft to h-9, cab to h-4, cap to h")
        shaft, cab, cap = sorted((s for s, _, _ in its), key=lambda s: s.min_h)
        areas = [abs(bld._area2(s.ring)) / 2 for s in (shaft, cab, cap)]
        self.assertAlmostEqual(areas[0] / areas[1], ab.SHAFT_SHARE ** 2, delta=0.02, msg="the footprint shrunk to 55 %")
        self.assertAlmostEqual(areas[2] / areas[1], ab.CAP_SHARE ** 2, delta=0.02, msg="the cap a little smaller")
        self.assertEqual(material(cab), bld.OFFICE, "a glass cab")
        self.assertEqual(material(shaft), bld.CONCRETE)
        self.assertNotEqual(shaft.cls, "mast", "(66 m2 and 66 m tall: a mast by the slenderness rule)")
        self.assertFalse(shaft.windows)
        self.assertEqual(len({s.seed for s, _, _ in its}), 1, "one look")
        P = bld.build(its)[0]
        self.assertAlmostEqual(float(P[:, 2].max()), 75.0, delta=0.3, msg="to the mapped height (a flat roof's lip), no plant above")
        self.assertGreater(len(bld.build(its, lite=True)[0]), 0)

    def test_a_name_without_an_airport_context_is_left(self):
        b = building(rect(100, 100, 15, 14.6), name="Air Traffic Control Tower", height=75.0)
        self.assertEqual(len(items(b, None)), 1)
        self.assertEqual(len(items(b, infra((mapgeom.POLYGON, rect(0, 0, 9, 9), "parking", "transportation")))), 1)

    def test_a_height_by_the_airport_when_missing(self):
        b = building(rect(100, 100, 15, 15), name="Control Tower")
        self.assertEqual(tops(items(b, infra(FIELD, apron(0, 0, 90, 90))))[-1], (36.0, 4.0), "international: 40 m")
        small = infra((mapgeom.POLYGON, rect(-900, -900, 1800, 1800), "regional_airport"), apron(0, 0, 90, 90))
        self.assertEqual(tops(items(building(rect(100, 100, 15, 15), name="Control Tower"), small))[-1], (21.0, 4.0),
                         "elsewhere: 25 m")

    def test_a_tower_word_needs_an_airport_word(self):
        pads = infra(FIELD, apron(0, 0, 90, 90))
        self.assertEqual(len(items(building(rect(100, 100, 15, 15), name="Clock Tower Airport Hotel"), pads)), 1)
        self.assertEqual(len(items(building(rect(100, 100, 15, 15), name="Landmark Tower"), pads)), 1)
        self.assertEqual(len(items(building(rect(100, 100, 15, 15), name="Airport Tower", height=40.0), pads)), 3)

    def test_a_slender_unnamed_tower_by_an_apron(self):
        pads = infra(FIELD, apron(0, 0, 90, 90))
        its = items(building(rect(120, 120, 12, 12), height=32.0), pads)
        self.assertEqual(tops(its), [(0.0, 23.0), (23.0, 5.0), (28.0, 4.0)])
        self.assertEqual(len(items(building(rect(120, 120, 12, 12), num_floors=8.0), pads)), 3, "or its floors")

    def test_a_small_footprint_gets_an_octagonal_cab(self):
        its = items(building(rect(120, 120, 7, 7), height=30.0), infra(FIELD, apron(0, 0, 90, 90)))
        shaft, cab, cap = sorted((s for s, _, _ in its), key=lambda s: s.min_h)
        self.assertEqual(len(cab.ring), 8, "a regular octagon")
        across = np.ptp(cab.ring[:, 0]), np.ptp(cab.ring[:, 1])
        self.assertGreaterEqual(min(across), ab.CAB_SMALL_M - 0.5, "at least 10 m across")
        self.assertGreaterEqual(min(np.ptp(shaft.ring[:, 0]), np.ptp(shaft.ring[:, 1])), ab.SHAFT_MIN_M - 0.1)

    def test_not_a_tower(self):
        pads = infra(FIELD, apron(0, 0, 90, 90))
        for what, b in (("low", building(rect(120, 120, 12, 12), height=12.0)),
                        ("wide", building(rect(120, 120, 40, 40), height=30.0)),
                        ("long", building(rect(120, 120, 60, 8), height=30.0)),
                        ("a hotel", building(rect(120, 120, 12, 12), height=32.0, **{"class": "hotel"})),
                        ("far from a pad", building(rect(20, 20, 12, 12), height=32.0)),
                        ("raised", building(rect(120, 120, 12, 12), height=32.0, min_height=10.0))):
            far = infra(FIELD, apron(-2500, -2500, 90, 90)) if what == "far from a pad" else pads
            self.assertEqual(len(items(b, far)), 1, what)

    def test_a_building_with_parts_is_left_alone(self):
        """Heathrow's tower: a stack of OSM parts (has_parts), drawn from them as before, whatever its name."""
        b = building(rect(100, 100, 15, 15), name="Heathrow Airport Control Tower", height=87.0, has_parts=1.0)
        key = b.ids[0]
        parts = Feats(["p1", "p2"], [rect(100, 100, 15, 15), rect(104, 104, 7, 7)], building_id=[key, key],
                      height=[20.0, 87.0], min_height=[np.nan, 40.0], num_floors=[np.nan, np.nan],
                      min_floor=[np.nan, np.nan])
        pads = infra(FIELD, apron(0, 0, 90, 90))
        with_ctx, without = items(b, pads, [parts]), items(b, pads, [parts], airport=False)
        self.assertEqual(len(with_ctx), 2)
        self.assertEqual(tops(with_ctx), tops(without))
        self.assertEqual([s.facade for s, _, _ in with_ctx], [s.facade for s, _, _ in without])
        flagged = building(rect(100, 100, 15, 15), name="Control Tower", height=60.0, has_parts=1.0)
        self.assertEqual(len(items(flagged, pads)), 1, "(has_parts and its parts not loaded: not synthesised over)")


class TerminalTest(unittest.TestCase):
    def terminal(self, w=80, d=40, gates=((20, 98), (40, 98), (60, 98)), **at):
        b = building(rect(100, 100, w, d), **at)
        return items(b, infra(FIELD, apron(0, 0, 90, 90), *[gate(*g) for g in gates]))

    def test_two_gates_at_the_outline_make_a_terminal(self):
        (spec, _, _), = self.terminal(name="Bozeman Yellowstone Intl. Airport (BZN)", gates=((120, 98), (150, 98)))
        self.assertEqual(spec.facade, bld.OFFICE, "the terminal look")
        self.assertAlmostEqual(spec.height, float(np.clip(8 + np.sqrt(3200) / 12, 12, 20)), places=3)
        self.assertEqual(spec.floors, 2)

    def test_the_height_by_its_size(self):
        (huge, _, _), = self.terminal(w=300, d=120, gates=((200, 98), (250, 98)))
        self.assertAlmostEqual(huge.height, 20.0, places=3, msg="clamped")
        self.assertEqual(huge.floors, 3)
        (mid, _, _), = self.terminal(w=100, d=40, gates=((120, 98), (150, 98)))
        self.assertTrue(12.0 < mid.height < 20.0)

    def test_one_gate_or_far_gates_or_a_small_building_are_not(self):
        (one, _, _), = self.terminal(gates=((120, 98),))
        self.assertNotEqual(one.facade, bld.OFFICE)
        (far, _, _), = self.terminal(gates=((120, 20), (150, 20)))
        self.assertNotEqual(far.facade, bld.OFFICE)
        (small, _, _), = self.terminal(w=30, d=30, gates=((110, 98), (120, 98)))
        self.assertNotEqual(small.facade, bld.OFFICE, "under 1500 m2")
        (hangar, _, _), = self.terminal(name="Hangar 4", gates=((120, 98), (150, 98)))
        self.assertNotEqual(hangar.facade, bld.OFFICE)

    def test_a_named_terminal_at_an_airport_takes_the_new_height_elsewhere_the_old(self):
        at_airport, = items(building(rect(100, 100, 60, 30), name="Terminal 1"), infra(FIELD, apron(0, 0, 90, 90)))
        self.assertAlmostEqual(at_airport[0].height, float(np.clip(8 + np.sqrt(1800) / 12, 12, 20)), places=3)
        elsewhere, = items(building(rect(100, 100, 60, 30), name="Terminal 1"), None)
        self.assertEqual((elsewhere[0].height, elsewhere[0].floors), (13.0, 2), "(not at an airport: as before)")

    def test_a_mapped_height_is_kept(self):
        (spec, _, _), = self.terminal(height=9.0, gates=((120, 98), (150, 98)))
        self.assertEqual(spec.facade, bld.OFFICE)
        self.assertAlmostEqual(spec.height, 9.0, places=3)

    def lifted(self, name, airport, one=True, **lines):
        """The spec of a 80 x 40 m building named `name`, a line through it, `airport` its context or None."""
        ctx = ab.context(FRAME, [infra(FIELD, apron(0, 0, 90, 90))], *BOX[:2]) if airport else None
        got = bld.tile_buildings(FRAME, *BOX, [building(rect(100, 100, 80, 40), name=name)], ELEV, airport=ctx,
                                 **{k: [np.array([(60.0, 120.0), (220.0, 120.0)])] for k in lines})
        if not one:                             # (no terminal: whatever the town's rules make of it; upstream's tunnel portals
            return [g[0] for g in got]          # cut it in pieces since 1.10)
        self.assertEqual(len(got), 1)
        return got[0][0]

    def test_a_terminal_over_a_tunnel_or_road_stands_on_the_ground(self):
        """Heathrow's Terminal 1 (62,000 m2, an Airside Road Tunnel mouth and forecourt roads at it) was a portal building
        / a structure over the roads: a 3 m slab 7.5 m or more over the apron, nothing under it."""
        for kind in ("tunnels_xy", "roads_xy"):
            other = self.lifted("Terminal 1", False, one=False, **{kind: 1})
            self.assertFalse(any(o.terminal for o in other), "(without an airport: no terminal, " + kind + ")")
            term = self.lifted("Terminal 1", True, **{kind: 1})
            self.assertEqual((term.min_h, term.portal, term.terminal), (0.0, False, True), kind)
            self.assertGreaterEqual(term.height, 12.0)
        shop = self.lifted("Shopping Hall", True, one=False, tunnels_xy=1)
        town = self.lifted("Shopping Hall", False, one=False, tunnels_xy=1)
        self.assertEqual([(o.min_h, o.portal) for o in shop], [(o.min_h, o.portal) for o in town],
                         "(a building that is no terminal at the airport: as it is elsewhere)")
        self.assertFalse(any(o.terminal for o in shop))


class AirsideTest(unittest.TestCase):
    """Brisbane's piers, satellites and walkways: many small footprints or parts, no height, drawn as brick houses."""
    PADS = infra(FIELD, apron(0, 0, 90, 90), apron(120, 0, 130, 90), gate(40, 40))

    def spec(self, ring, pads=None, **at):
        got = items(building(ring, **at), self.PADS if pads is None else pads)
        self.assertEqual(len(got), 1)
        return got[0][0]

    def test_a_small_pier_by_an_apron_takes_the_terminal_look(self):
        specs = [self.spec(rect(100 + 12 * k, 100, 10 + k % 3, 9 + k % 4)) for k in range(10)]
        lo, hi = ab.AIRSIDE_H_M
        for s in specs:
            self.assertFalse(s.porch or s.wall or s.plain or s.steeple, "no house, shed or plain wall")
            self.assertNotEqual(s.band, bld.BAND_BRICK)
            self.assertNotIn(s.facade, bld.BRICK_TINTS[1:])
            self.assertEqual(s.roof, "flat")
            self.assertTrue(s.windows)
            self.assertTrue(s.airside)
            self.assertTrue(lo <= s.height <= hi, s.height)      # (6-9 m)
        panels = [s for s in specs if s.band == bld.BAND_OFFICE2 and s.facade in bld.AIRSIDE_PANEL_TINTS]
        self.assertTrue(panels and len({s.facade for s in specs}) > 1, "glass and light panels, by the seed")
        for s in specs:
            self.assertEqual(material(s), bld.OFFICE, "the curtain wall's sheet, never FACADE's house windows")
            self.assertIn(s.band, (bld.BAND_OFFICE, bld.BAND_OFFICE2))

    def test_a_box_of_any_size_or_class_never_takes_the_house_facade(self):
        """Brisbane's jet bridge cabs and stair blocks: 4 to 60 m boxes (a retail or house class, a mapped brick wall) with
        the plaster window sheet's sash windows."""
        for w, d in ((4, 6), (4, 9), (6, 12), (10, 10), (30, 60)):
            for at in ({}, {"class": "retail"}, {"class": "house"}, {"class": "garage_shed"},
                       {"facade_material": "brick", "facade_color": "#a04030"}):
                s = self.spec(rect(110, 100, w, d), **at)
                self.assertTrue(s.airside, (w, d, at))
                self.assertEqual(material(s), bld.OFFICE, (w, d, at))
                self.assertFalse(s.shop or s.wall or s.plain or s.porch, (w, d, at))

    def test_a_mapped_pitched_tiled_roof_is_flat_membrane_or_metal(self):
        """Brisbane's domestic concourse: a mapped hipped roof in terracotta tiles; a terminal's, a pier's or a walkway's
        roof is flat and never tiles, whatever the map says, and has no house features."""
        pitched = dict(roof_shape="hipped", roof_material="tiles", roof_color="#b5532f", roof_height=4.0)
        for shape in ("gabled", "hipped", "pyramidal", "skillion", "mansard"):
            at = dict(pitched, roof_shape=shape)
            for ring, extra in ((rect(110, 100, 6, 5), {}), (rect(110, 100, 8, 60), {}), (rect(100, 100, 60, 30), {}),
                                (rect(100, 100, 60, 30), {"name": "Domestic Terminal"}),
                                (rect(100, 100, 8, 14), {"name": "Concourse B"})):
                s = self.spec(ring, **dict(at, **extra))
                self.assertEqual(s.roof, "flat", (shape, extra))
                self.assertIn(s.roof_mat.split("|")[0], (bld.ROOF_FLAT, bld.ROOF_METAL), (shape, extra, s.roof_mat))
                self.assertFalse(s.hf or s.skel or s.porch or s.steeple or s.wall, (shape, extra))

    def test_a_small_terminal_by_name_is_no_house(self):
        for name in ("Terminal 2", "Concourse C"):
            for w, d in ((6, 8), (10, 12), (14, 9)):
                s = self.spec(rect(100, 100, w, d), name=name)
                self.assertEqual((s.roof, s.facade), ("flat", bld.OFFICE), (name, w, d))
                self.assertFalse(s.wall or s.porch or s.plain or s.steeple, (name, w, d))
                self.assertFalse(s.roof_mat.startswith(bld.ROOF_TILES))

    def test_a_tower_stays_flat_under_a_mapped_pitch(self):
        got = items(building(rect(100, 100, 8, 8), name="Control Tower", roof_shape="hipped", roof_material="tiles"),
                    self.PADS)
        self.assertTrue(got)
        for s, _, _ in got:
            self.assertEqual(s.roof, "flat")
            self.assertNotEqual(material(s).split("|")[0], bld.ROOF_TILES)

    def test_the_same_building_off_an_airport_is_as_before(self):
        b = building(rect(100, 100, 12, 10))        # (its id seeds its look: the same one both ways)
        off = items(b, None, airport=False)[0][0]
        self.assertFalse(off.airside)
        far = infra(FIELD, apron(-2500, -2500, 90, 90), gate(-2500, -2500))
        got = items(b, far)[0][0]
        for k in ("facade", "band", "height", "roof", "wall", "windows"):
            self.assertEqual(getattr(got, k), getattr(off, k), k)
        self.assertFalse(got.airside)

    def test_within_30_m_of_a_pad_or_60_m_of_a_gate(self):
        self.assertTrue(self.spec(rect(110, 100, 10, 10)).airside, "20 m from the apron's corner")
        self.assertFalse(self.spec(rect(190, 190, 10, 10), pads=infra(FIELD, apron(0, 0, 90, 90))).airside, "100 m off")
        self.assertTrue(self.spec(rect(190, 190, 10, 10), pads=infra(FIELD, gate(150, 190))).airside, "40 m from a gate")
        self.assertFalse(self.spec(rect(230, 230, 10, 10), pads=infra(FIELD, gate(150, 190))).airside, "over 60 m")
        # (a long thin pier across a taxiway: its ends are far from the pavement, its middle is on it)
        way = (mapgeom.LINE, [(200, 100), (200, 250)], "taxiway")
        self.assertTrue(self.spec(rect(150, 170, 100, 6), pads=infra(FIELD, way, apron(0, 0, 90, 90))).airside)
        # (an airfield with no apron or gate mapped has no airportroles zone: a shed by its taxiway keeps its own look)
        self.assertFalse(self.spec(rect(150, 170, 100, 6), pads=infra(FIELD, way)).airside)

    def test_not_a_tower_a_hangar_or_a_garage(self):
        for at in (dict(name="Hangar 3"), {"class": "hangar"}, {"class": "parking_garage"}):
            self.assertFalse(self.spec(rect(110, 100, 20, 15), **at).airside, at)

    def test_a_mapped_height_is_kept_and_a_terminal_keeps_its_sizing(self):
        self.assertAlmostEqual(self.spec(rect(110, 100, 12, 10), height=14.0).height, 14.0, delta=0.7)
        term = self.spec(rect(100, 100, 60, 30), name="Terminal 1")
        self.assertFalse(term.airside)
        self.assertEqual(term.facade, bld.OFFICE)
        self.assertAlmostEqual(term.height, float(np.clip(8 + np.sqrt(1800) / 12, 12, 20)), places=3)

    def test_a_small_airside_roof_has_no_plant(self):
        small = self.spec(rect(110, 100, 12, 10))
        m = bld.Mesh()
        bld._clutter(m, small, small.ring, 8.0, None)
        self.assertEqual(m.n, 0, "(returns before it asks the kit for boxes)")

    def test_a_raised_airside_part_floats_on_pillars(self):
        b = building(rect(100, 100, 40, 8), name="Pier B walkway", has_parts=1.0)
        key = b.ids[0]
        walk = Feats(["w1"], [rect(100, 100, 40, 8)], building_id=[key], height=[8.0], min_height=[5.0],
                     num_floors=[np.nan], min_floor=[np.nan])
        got = items(b, self.PADS, [walk])
        deck = [s for s, _, _ in got if s.cls != "pillar"]
        pills = [s for s, _, _ in got if s.cls == "pillar"]
        self.assertEqual(len(deck), 1)
        self.assertAlmostEqual(deck[0].min_h, 5.0, places=3, msg="it still floats, as mapped")
        self.assertEqual(len(pills), 10, "5 stations (12 m apart or less) in a pair of rows")
        for p in pills:
            self.assertEqual((p.min_h, p.facade, p.windows), (0.0, "bne_precast", False))
            self.assertAlmostEqual(p.height, 5.0 - 0.02, places=3)
            self.assertAlmostEqual(abs(bld._area2(p.ring)) / 2, ab.PILLAR_M ** 2, places=3)
            c = p.ring.mean(0)
            self.assertTrue(100 <= c[0] <= 140 and 100 <= c[1] <= 108, "under the part")
        ys = sorted({round(float(p.ring.mean(0)[1]), 1) for p in pills})
        self.assertEqual(ys, [101.0, 107.0], "a row near each long edge")
        xs = sorted({round(float(p.ring.mean(0)[0]), 1) for p in pills})
        self.assertLessEqual(float(np.diff(xs).max()), ab.PILLAR_SPACING_M + 0.1)
        self.assertGreater(len(bld.build(got)[0]), 0)
        # (off the airport, or low: none)
        self.assertEqual(len(items(b, self.PADS, [walk], airport=False)), 1)
        low = Feats(["w2"], [rect(100, 100, 40, 8)], building_id=[key], height=[8.0], min_height=[1.5],
                    num_floors=[np.nan], min_floor=[np.nan])
        self.assertEqual(len(items(b, self.PADS, [low])), 1)

    def test_pillars_down_the_middle_of_a_narrow_part_and_inside_an_l(self):
        got = ab.pillars(np.array(rect(0, 0, 30, 4)[:-1], np.float64))
        self.assertEqual(len(got), 4)
        self.assertEqual({round(float(g.mean(0)[1]), 3) for g in got}, {2.0})
        for g in got:
            self.assertGreater(float(bld._area2(g)), 0.0, "counter-clockwise")
        ell = np.array([(0, 0), (30, 0), (30, 8), (8, 8), (8, 30), (0, 30)], np.float64)
        import polygon
        for g in ab.pillars(ell):
            self.assertTrue(bool(polygon.contains_points(ell, g.mean(0)[None])[0]))
        self.assertLessEqual(len(ab.pillars(np.array(rect(0, 0, 900, 20)[:-1], np.float64))), ab.PILLAR_MAX)


class RolesTest(unittest.TestCase):
    """buildings.tile_buildings draws each footprint and part by its airportroles role."""
    PADS = infra(FIELD, apron(0, 0, 300, 100), gate(121, 78))
    TERM = rect(100, 100, 60, 30)

    def parts(self, rows):
        """A parts layer: (id, ring, height, min_height) rows of building 'term' (the terminal's key is the caller's)."""
        return lambda key: Feats([r[0] for r in rows], [r[1] for r in rows], building_id=[key] * len(rows),
                                 height=[r[2] for r in rows], min_height=[r[3] for r in rows],
                                 num_floors=[np.nan] * len(rows), min_floor=[np.nan] * len(rows))

    def test_a_raised_block_of_a_terminal_stands_on_the_ground_a_storey_stays_up(self):
        b = building(self.TERM, name="Terminal 1", has_parts=1.0)
        # a part over a drive (raised, thick, nothing under it): on the ground; a storey on a lower part of it: raised
        p = self.parts([("low", rect(100, 100, 30, 30), 8.0, 0.0), ("storey", rect(104, 104, 22, 22), 16.0, 8.0),
                        ("over", rect(135, 100, 25, 30), 14.0, 6.0)])(b.ids[0])
        got = items(b, self.PADS, [p])
        self.assertEqual(sorted((round(s.min_h, 2), round(s.height, 2)) for s, _, _ in got),
                         [(0.0, 8.0), (0.0, 14.0), (8.0, 8.0)], "the part over the drive is 14 m from the ground now")
        self.assertTrue(all(s.terminal or s.airside for s, _, _ in got), "the terminal look on every part")
        self.assertEqual(sum(s.terminal for s, _, _ in got), 3, "(the block over the drive is inside the terminal's outline)")
        off = items(b, self.PADS, [p], airport=False)
        self.assertIn((6.0, 8.0), sorted((round(s.min_h, 2), round(s.height, 2)) for s, _, _ in off), "(off an airport: as mapped)")

    def sparse(self, extra=(), **at):
        """The terminal (has parts, 60 x 30 m) with parts over a third of it: a 20 x 30 m part, 14 m tall, and `extra` rows
        (id, ring, height, min_height). -> the items"""
        b = building(self.TERM, name="Terminal 1", has_parts=1.0, **at)
        rows = [("wing", rect(100, 100, 20, 30), 14.0, 0.0)] + list(extra)
        return items(b, self.PADS, [self.parts(rows)(b.ids[0])])

    @staticmethod
    def wide(got, w):
        """The specs whose footprint is w m wide (east-west)."""
        return [s for s, _, _ in got if round(float(np.ptp(s.ring[:, 0]))) == w]

    def test_a_terminal_with_sparse_parts_is_drawn_as_well(self):
        """Brisbane's curved domestic terminal: parts over a fraction of a big footprint: the rest was never drawn."""
        got = self.sparse()
        mass = self.wide(got, 60)
        self.assertEqual(len(mass), 1, "the whole footprint too")
        self.assertTrue(mass[0].terminal)
        self.assertEqual(mass[0].min_h, 0.0)
        self.assertAlmostEqual(mass[0].height, 14.0, places=3, msg="no height mapped: its parts' top")
        self.assertEqual(self.wide(got, 20), [], "a part as tall as the mass, standing in it: not drawn twice (z-fighting)")
        got = self.sparse([("tall", rect(130, 105, 20, 20), 30.0, 0.0)])
        self.assertEqual(sorted(round(s.height, 2) for s, _, _ in got), [14.0, 30.0], "a taller part is drawn on the mass")
        got = self.sparse(height=11.0)
        self.assertAlmostEqual(self.wide(got, 60)[0].height, 11.0, places=3, msg="its mapped height when it has one")
        got = self.sparse([("roof", rect(105, 131, 48, 6), 9.0, 6.0)])
        self.assertAlmostEqual(self.wide(got, 60)[0].height, 14.0, places=3, msg="a roof part's top is no terminal height")
        b = building(self.TERM, name="Terminal 1", has_parts=1.0)
        rows = [("wing", rect(100, 100, 20, 30), 14.0, 0.0)]
        off = items(b, self.PADS, [self.parts(rows)(b.ids[0])], airport=False)
        self.assertEqual(len(off), 1, "(off an airport: its one part, as it was)")

    def test_parts_covering_most_of_a_terminal_leave_it_to_them(self):
        b = building(self.TERM, name="Terminal 1", has_parts=1.0)
        rows = [("a", rect(100, 100, 40, 30), 12.0, 0.0), ("b", rect(140, 100, 20, 30), 15.0, 0.0)]
        got = items(b, self.PADS, [self.parts(rows)(b.ids[0])])
        self.assertEqual(sorted(round(s.height, 2) for s, _, _ in got), [12.0, 15.0], "just the two parts")

    def test_a_roof_part_keeps_its_own_heights(self):
        got = self.sparse([("roof", rect(105, 131, 48, 6), 9.0, 6.0)])
        roof = self.wide(got, 48)
        self.assertEqual(len(roof), 1)
        self.assertEqual((round(roof[0].min_h, 2), round(roof[0].height, 2)), (6.0, 3.0), "its mapped 6 to 9 m, not the terminal's")
        self.assertFalse(roof[0].terminal)

    def test_a_block_inside_a_terminal_has_the_terminal_look(self):
        feats = Feats(["t", "blk", "far"], [self.TERM, rect(110, 105, 20, 15), rect(200, 20, 20, 15)], **{
            "class": ["", "", ""], "height": [np.nan, 8.0, 8.0], "num_floors": [np.nan] * 3, "min_height": [np.nan] * 3,
            "name": ["Terminal 1", "", ""], "has_parts": [0.0] * 3})
        got = {round(float(s.ring[:, 0].min())): s for s, _, _ in items(feats, self.PADS)}
        self.assertTrue(got[110].terminal, "mostly inside a terminal's outline")
        self.assertFalse(got[200].terminal)
        self.assertEqual(got[110].roof, "flat")

    def test_a_jet_bridge_stays_raised_on_pillars(self):
        bridge = Feats(["jb", "t"], [rect(121, 80, 3.5, 22), self.TERM], **{
            "class": ["", ""], "height": [7.0, np.nan], "num_floors": [np.nan] * 2, "min_height": [4.0, np.nan],
            "name": ["", "Terminal 1"], "has_parts": [0.0, 0.0]})
        got = items(bridge, self.PADS)
        deck = [s for s, _, _ in got if s.cls != "pillar" and s.min_h > 0]
        pills = [s for s, _, _ in got if s.cls == "pillar"]
        self.assertEqual(len(deck), 1)
        self.assertAlmostEqual(deck[0].min_h, 4.0, places=3, msg="it floats, as mapped")
        self.assertTrue(deck[0].airside)
        self.assertGreaterEqual(len(pills), 2)
        for p in pills:
            self.assertAlmostEqual(p.height, 4.0 - 0.02, places=3)
        self.assertEqual(len(items(bridge, self.PADS, airport=False)), 2, "(off an airport: no pillars)")

    def test_a_roof_on_a_terminals_front_keeps_its_own_heights_or_a_canopys(self):
        """Brisbane's strips along its domestic terminal: class-roof footprints, drawn at their own mapped heights (a canopy's
        6 m underside and 1 m thickness where none is mapped), never the terminal's height."""
        feats = Feats(["t", "front", "mapped", "far"], [self.TERM, rect(110, 131, 40, 7), rect(110, 140, 40, 7),
                                                         rect(110, 160, 40, 7)], **{
            "class": ["", "roof", "roof", "roof"], "height": [np.nan, np.nan, 9.0, np.nan], "num_floors": [np.nan] * 4,
            "min_height": [np.nan, np.nan, 5.0, np.nan], "name": ["Terminal 1", "", "", ""], "has_parts": [0.0] * 4})
        got = {round(float(s.ring[:, 1].min())): s for s, _, _ in items(feats, self.PADS)}
        self.assertTrue(got[100].terminal)
        self.assertFalse(got[131].terminal, "a canopy, no terminal")
        self.assertEqual((got[131].min_h, got[131].height), (bld.CANOPY_UNDERSIDE_M, 2.5), "6 m up (a slab at least 2.5 m)")
        self.assertEqual(got[140].min_h, 5.0, "its mapped underside")
        self.assertAlmostEqual(got[140].height, 4.0, places=3, msg="9 m top over 5 m: 4 m thick")
        self.assertNotAlmostEqual(got[131].height, ab.terminal_height(1800.0), places=1)

    def test_a_hangar_and_a_car_park_have_no_airside_look(self):
        feats = Feats(["h", "c"], [rect(200, 20, 30, 20), rect(200, 60, 30, 20)], **{
            "class": ["hangar", "parking_garage"], "height": [np.nan] * 2, "num_floors": [np.nan] * 2,
            "min_height": [np.nan] * 2, "name": ["", ""], "has_parts": [0.0] * 2})
        for s, _, _ in items(feats, self.PADS):
            self.assertFalse(s.airside)
            self.assertFalse(s.terminal)


class UnchangedTest(unittest.TestCase):
    def snapshot(self, its):
        return [(s.ring.tolist(), s.height, s.min_h, s.floors, s.facade, s.roof, s.roof_mat, s.windows) for s, _, _ in its]

    def test_buildings_off_an_airport_come_out_the_same(self):
        rows = [rect(20, 20, 12, 12), rect(60, 20, 90, 40), rect(20, 80, 10, 10), rect(200, 200, 15, 15)]
        ats = [dict(height=32.0), dict(), dict(), dict(name="Bus Terminal")]
        layer = Feats([f"b{k}" for k in range(len(rows))], rows, **{k: [a.get(k, d) for a in ats] for k, d in (
            ("class", ""), ("height", np.nan), ("num_floors", np.nan), ("min_height", np.nan), ("name", ""),
            ("has_parts", 0.0))})
        base = self.snapshot(bld.tile_buildings(FRAME, *BOX, [layer], ELEV))
        # (no airport in the cells: no context, nothing asked of the buildings)
        self.assertIsNone(ab.context(FRAME, [infra((mapgeom.POLYGON, rect(0, 0, 9, 9), "parking", "transportation")), None], 0, 0))
        self.assertEqual(base, self.snapshot(bld.tile_buildings(FRAME, *BOX, [layer], ELEV, airport=None)))
        # (an airport 2 km off, its pads and gates there: a slender tower, a hall and a house away from them are as
        # they were; a name's terminal is a terminal wherever it stands in an airport's cells)
        far = ab.context(FRAME, [infra(FIELD, apron(-2500, -2500, 90, 90), gate(-2500, -2500))], 0, 0)
        got = self.snapshot(bld.tile_buildings(FRAME, *BOX, [layer], ELEV, airport=far))
        self.assertEqual(base[:3], got[:3])
        self.assertEqual(len(got), len(base))


if __name__ == "__main__":
    unittest.main()
