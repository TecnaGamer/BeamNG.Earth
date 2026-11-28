"""airportroles: every building (and part) at an airport has exactly one role (terminal, pier, jet bridge, walkway, raised
block, tower, hangar, canopy, landside, other), and each gate point gets its jet bridge (its id, outer end, direction).
Small synthetic cases, metres east / north of a frame, one airport (an international aerodrome polygon, an apron, gates)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import airportroles as ar  # noqa: E402
import geodesy  # noqa: E402
import mapgeom  # noqa: E402

FRAME = geodesy.EnuFrame(-27.39, 153.118)
NAN = float("nan")


def lonlat(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], np.float64),
                                      np.array([p[1] for p in pts], np.float64), 0.0)
    return np.stack([lo, la], 1)


def rect(x, y, w, d):
    return [(x, y), (x + w, y), (x + w, y + d), (x, y + d), (x, y)]


class Feats:
    """A buildings (or parts) layer: rows of rings in metres, columns by name (the Features' own API as far as roles reads it)."""

    def __init__(self, rows, parts=False):
        self.ids = np.array([r["id"] for r in rows], object)
        self.kind = np.full(len(rows), 3, np.uint8)
        self.rings = [lonlat(r["ring"]) for r in rows]
        cols = {"name": "", "class": "", "height": NAN, "min_height": NAN}
        if parts:
            cols["building_id"] = ""
        else:
            cols["has_parts"] = 0.0
        self.attrs = {}
        for k, dflt in cols.items():
            vals = [r.get(k, dflt) for r in rows]
            self.attrs[k] = np.array(vals, object if isinstance(dflt, str) else np.float64)

    def __len__(self):
        return len(self.ids)

    def parts(self, i):
        return [[self.rings[i]]]


def B(id, ring, **kw):
    return dict(id=id, ring=ring, **kw)


def infra(*rows):
    """Infrastructure Features of (kind, points, class[, subtype]) rows, subtype airport by default."""
    ids = np.array([f"i{i:03d}" for i in range(len(rows))])
    f = mapgeom.build("infrastructure", ids, [(r[0], [[lonlat(r[1])]]) for r in rows])
    f.attrs = {"class": mapgeom.text_column([r[2] for r in rows]),
               "subtype": mapgeom.text_column([r[3] if len(r) > 3 else "airport" for r in rows]),
               "tags": mapgeom.text_column(["{}"] * len(rows)), "name": mapgeom.text_column([""] * len(rows)),
               "surface": mapgeom.text_column([None] * len(rows))}
    return f


FIELD = (mapgeom.POLYGON, rect(-900, -900, 1800, 1800), "international_airport")
APRON = (mapgeom.POLYGON, rect(0, 0, 300, 200), "apron")
TERMINAL = rect(0, 200, 200, 40)                    # 8000 m2, along the apron's north edge
GATES = [(mapgeom.POINT, [(40, 190)], "airport_gate"), (mapgeom.POINT, [(100, 185)], "airport_gate"),
         (mapgeom.POINT, [(160, 190)], "airport_gate")]


def run(buildings=(), parts=(), extra=(), gates=GATES, apron=True):
    rows = [FIELD] + ([APRON] if apron else []) + list(gates) + list(extra)
    inf = infra(*rows)
    b = Feats(list(buildings)) if buildings else None
    p = Feats(list(parts), parts=True) if parts else None
    return ar.classify([b] if b else [], [p] if p else [], [inf], FRAME, 0.0, 0.0)


TERM = B("term", TERMINAL, name="Terminal 1", height=12.0)


class RoleTest(unittest.TestCase):
    def test_terminal_by_name_by_gates_and_by_size(self):
        r = run([TERM])
        self.assertEqual(r["term"], ar.TERMINAL)
        r = run([B("unnamed", TERMINAL)])
        self.assertEqual(r["unnamed"], ar.TERMINAL, "8000 m2 with three gates along it")
        r = run([B("shed", rect(0, 200, 200, 40))], gates=[])
        self.assertNotEqual(r["shed"], ar.TERMINAL, "no name, no gates, not big: not a terminal")
        r = run([B("kiosk", rect(10, 205, 6, 6), name="Terminal kiosk")])
        self.assertNotEqual(r["kiosk"], ar.TERMINAL, "a named kiosk is not a terminal")
        r = run([B("huge", rect(0, 200, 120, 100))], gates=GATES[:1])
        self.assertEqual(r["huge"], ar.TERMINAL, "12000 m2 with a gate near it")

    def test_a_pier_is_long_narrow_gated_and_joined_to_a_terminal(self):
        pier = rect(60, 100, 25, 100)                      # 100 x 25, from the terminal's south edge out over the apron
        gates = [(mapgeom.POINT, [(50, 190)], "airport_gate"), (mapgeom.POINT, [(95, 150)], "airport_gate"),
                 (mapgeom.POINT, [(50, 110)], "airport_gate")]
        r = run([B("term", TERMINAL, name="Terminal 1"), B("pier", pier)], gates=gates)
        self.assertEqual(r["term"], ar.TERMINAL)
        self.assertEqual(r["pier"], ar.PIER)
        far = rect(2000, 2000, 20, 100)
        r2 = run([B("lone", pier)], gates=gates)
        self.assertEqual(r2["lone"], ar.TERMINAL, "a gated linear building with no terminal to join: the terminal itself")
        r3 = run([B("far", far), TERM])
        self.assertNotIn("far", r3, "two km off: no part of the airport")

    def test_a_pier_named_so(self):
        r = run([B("e", rect(0, 100, 400, 50), name="E-pier"), B("t", rect(0, 200, 200, 40))],
                gates=[(mapgeom.POINT, [(100, 95)], "airport_gate")])
        self.assertEqual(r["e"], ar.PIER)

    def test_a_jet_bridge_from_the_terminal_to_a_gate(self):
        bridge = B("jb", rect(95, 175, 3.5, 25))           # root in the terminal's south edge, the other end 10 m from a gate
        r = run([TERM, bridge])
        self.assertEqual(r["jb"], ar.JET_BRIDGE)
        gate = r.gates[next(g for g, p in r.gate_xy.items() if p == (100.0, 185.0) or
                            (abs(p[0] - 100.0) < 0.05 and abs(p[1] - 185.0) < 0.05))]
        self.assertEqual(gate["bridge"], "jb")
        self.assertAlmostEqual(gate["end"][0], 96.75, delta=0.1)
        self.assertAlmostEqual(gate["end"][1], 175.0, delta=0.1, msg="the outer end, away from the terminal")
        self.assertAlmostEqual(gate["dir"][0], 0.0, delta=0.05)
        self.assertAlmostEqual(gate["dir"][1], -1.0, delta=0.05, msg="out of the terminal (south)")
        others = [g for g in r.gates.values() if g["bridge"] is None]
        self.assertEqual(len(others), 2, "the other two gates have none")

    def test_a_bridge_when_the_gate_is_mapped_at_the_terminal_end(self):
        gates = [(mapgeom.POINT, [(97, 197)], "airport_gate"), (mapgeom.POINT, [(40, 195)], "airport_gate")]
        r = run([TERM, B("jb", rect(95, 175, 3.5, 25))], gates=gates)
        self.assertEqual(r["jb"], ar.JET_BRIDGE)
        g = [v for v in r.gates.values() if v["bridge"]]
        self.assertEqual(len(g), 1)
        self.assertAlmostEqual(g[0]["end"][1], 175.0, delta=0.1, msg="its outer end is still the one away from the terminal")

    def test_a_bridge_in_two_pieces_ends_at_the_cab(self):
        r = run([TERM, B("jb", rect(95, 180, 3.5, 20)), B("cab", rect(95, 170, 3.5, 10))])
        self.assertEqual(r["jb"], ar.JET_BRIDGE)
        self.assertEqual(r["cab"], ar.JET_BRIDGE)
        g = [v for v in r.gates.values() if v["bridge"] == "jb"][0]
        self.assertAlmostEqual(g["end"][1], 170.0, delta=0.1)

    def test_a_bridge_mapped_twice_one_over_the_other_both_get_the_cab(self):
        r = run([TERM, B("jb", rect(95, 180, 3.5, 20)), B("dup", rect(95, 180, 3.5, 20)), B("cab", rect(95, 170, 3.5, 10))])
        self.assertEqual(r["cab"], ar.JET_BRIDGE)
        self.assertEqual({b for b in r.bridges}, {"jb", "dup"})
        for bid in ("jb", "dup"):
            self.assertAlmostEqual(r.bridges[bid][2][1], 170.0, delta=0.1, msg=bid + " ends at the cab")
        self.assertEqual(r.tails["cab"], {"jb", "dup"})

    def test_a_narrow_building_far_from_a_gate_is_no_bridge(self):
        r = run([TERM, B("strip", rect(250, 170, 3.5, 25))], gates=GATES)
        self.assertNotEqual(r["strip"], ar.JET_BRIDGE)

    def test_a_raised_bridge_wider_than_a_tunnel(self):
        r = run([TERM, B("rb", rect(95, 175, 9, 25), min_height=3.0, height=6.0)])
        self.assertEqual(r["rb"], ar.JET_BRIDGE)

    def test_a_walkway_joins_two_buildings(self):
        park = B("park", rect(0, 270, 200, 40), name="Car park")
        walk = B("walk", rect(90, 240, 5, 30), min_height=6.0, height=9.0)
        r = run([TERM, park, walk])
        self.assertEqual(r["walk"], ar.WALKWAY)
        r2 = run([TERM, B("hang", rect(90, 240, 5, 30), min_height=6.0, height=9.0)])
        self.assertNotEqual(r2["hang"], ar.WALKWAY, "joins one building only")

    def test_a_wide_raised_part_is_a_raised_block_and_a_thin_slab_a_canopy(self):
        r = run([TERM, B("block", rect(220, 100, 30, 30), min_height=6.0, height=20.0),
                 B("slab", rect(220, 140, 30, 30), min_height=6.0, height=8.0)])
        self.assertEqual(r["block"], ar.RAISED_BLOCK)
        self.assertEqual(r["slab"], ar.CANOPY)

    def test_a_storey_on_a_lower_part_is_no_raised_block(self):
        base = B("base", rect(0, 270, 60, 60), height=10.0, **{"building_id": "bld"})
        top = B("top", rect(10, 280, 40, 40), min_height=10.0, height=30.0, **{"building_id": "bld"})
        r = run([TERM], parts=[base, top])
        self.assertNotEqual(r["top"], ar.RAISED_BLOCK)

    def test_tower_hangar_canopy(self):
        r = run([TERM, B("twr", rect(250, 210, 15, 15), name="Airport Control Tower", height=40.0),
                 B("hng", rect(250, 100, 40, 30), **{"class": "hangar"}),
                 B("can", rect(250, 20, 20, 20), **{"class": "roof"})])
        self.assertEqual(r["twr"], ar.TOWER)
        self.assertEqual(r["hng"], ar.HANGAR)
        self.assertEqual(r["can"], ar.CANOPY)

    def test_landside_and_other(self):
        r = run([TERM, B("shed", rect(220, 60, 20, 20)), B("hotel", rect(0, 450, 50, 50), name="Airport Hotel"),
                 B("gone", rect(0, 1200, 50, 50))])
        self.assertEqual(r["shed"], ar.OTHER, "on the apron's edge")
        self.assertEqual(r["hotel"], ar.LANDSIDE, "within reach of the apron and the gates, off the airside")
        self.assertNotIn("gone", r)

    def test_parts_take_their_buildings_name(self):
        parent = B("big", TERMINAL, name="Terminal 1")
        parent["has_parts"] = 1.0
        part = B("wing", rect(0, 200, 70, 40), **{"building_id": "big"})
        r = run([parent], parts=[part])
        self.assertEqual(r["big"], ar.TERMINAL)
        self.assertEqual(r["wing"], ar.TERMINAL)

    def test_a_lot_inside_an_apron_is_airside_parking(self):
        lot_in = (mapgeom.POLYGON, rect(220, 20, 40, 40), "parking")
        lot_out = (mapgeom.POLYGON, rect(400, 400, 40, 40), "parking")
        r = run([TERM], extra=[lot_in, lot_out])
        self.assertEqual(len(r.airside_parking), 1)
        self.assertEqual(r.airside_parking[0]["id"], "i005", "the lot in the apron (the sixth row)")

    def test_every_footprint_has_exactly_one_role_and_the_same_every_time(self):
        rows = [TERM, B("jb", rect(95, 175, 3.5, 25)), B("twr", rect(250, 210, 15, 15), name="Control Tower", height=40.0),
                B("shed", rect(220, 60, 20, 20))]
        a, b = run(rows), run(rows)
        self.assertEqual(dict(a), dict(b))
        self.assertEqual(set(a), {"term", "jb", "twr", "shed"})
        self.assertTrue(all(v in ar.ROLES for v in a.values()))
        self.assertEqual(sum(a.counts().values()), len(a))

    def test_roofs_along_a_terminals_front_stay_canopies(self):
        """Brisbane's domestic terminal: roofs (class roof, 6 x 48 m) along its front, touching its outline: canopies, as
        mapped (they were given the terminal's height, tall roofs on pillars)."""
        strip = B("strip", rect(60, 241, 48, 7), **{"class": "roof"})
        r = run([TERM, strip])
        self.assertEqual(r["strip"], ar.CANOPY)
        self.assertFalse(hasattr(r, "fronts"))
        r = run([B("term", TERMINAL, name="Terminal 1", height=30.0, has_parts=1.0)],
                parts=[B("w1", rect(0, 200, 100, 40), height=14.0, building_id="term"),
                       B("sp", rect(60, 241, 48, 7), min_height=4.0, height=6.0, building_id="other")])
        self.assertEqual(r["sp"], ar.CANOPY, "a raised thin part is a roof too")
        self.assertFalse(r.stacked("sp"))

    def test_what_lies_mostly_inside_a_terminal_has_the_terminal_look(self):
        """Brisbane's domestic terminal: blocks (footprints, parts of other buildings) between its roofs came out brick."""
        inside = B("in", rect(60, 205, 30, 20), height=9.0)                        # wholly inside the terminal's outline
        half = B("half", rect(180, 210, 40, 20), height=9.0)                       # 20 of its 40 m in, 20 out: not mostly
        mostly = B("mostly", rect(170, 215, 40, 10), height=9.0)                   # 30 of 40 m in: mostly
        part = B("pt", rect(100, 205, 20, 20), height=9.0, building_id="other")    # a part of another building, inside
        raised = B("rb", rect(120, 205, 20, 20), min_height=6.0, height=24.0, building_id="other")
        r = run([TERM, inside, half, mostly, B("other", rect(250, 400, 10, 10))], parts=[part, raised])
        self.assertEqual(r.inside, {"in", "mostly", "pt", "rb"})
        self.assertEqual(r["rb"], ar.RAISED_BLOCK, "its role is as it was: only its look is the terminal's")
        self.assertEqual(run([TERM, B("out", rect(230, 100, 20, 20), height=9.0)]).inside, set(), "outside it: none")
        r = run([B("t2", TERMINAL, name="Terminal 1", height=12.0), B("hng", rect(60, 205, 30, 20), **{"class": "hangar"})])
        self.assertEqual(r.inside, set(), "a hangar has a look of its own")

    def test_stacked_says_a_part_stands_on_a_lower_one(self):
        base = B("base", rect(0, 270, 60, 60), height=10.0, **{"building_id": "bld"})
        top = B("top", rect(10, 280, 40, 40), min_height=10.0, height=30.0, **{"building_id": "bld"})
        r = run([TERM], parts=[base, top])
        self.assertTrue(r.stacked("top"))
        self.assertFalse(r.stacked("base"))
        self.assertFalse(r.stacked("nothing"))

    def test_airside_rings_are_the_aprons_and_the_lots_in_them(self):
        lot_in = (mapgeom.POLYGON, rect(220, 20, 40, 40), "parking")
        lot_out = (mapgeom.POLYGON, rect(400, 400, 40, 40), "parking")
        inf = infra(FIELD, APRON, *GATES, lot_in, lot_out)
        got = ar.airside_rings([inf, None], FRAME)
        self.assertEqual(len(got), 2, "the apron and the lot inside it")
        self.assertEqual(sorted(float(round(g[:, 0].max() - g[:, 0].min())) for g in got), [40.0, 300.0])
        off = infra((mapgeom.POLYGON, rect(0, 0, 9, 9), "parking", "transportation"))
        self.assertEqual(ar.airside_rings([off], FRAME), [])

    def test_no_airport_no_roles(self):
        f = Feats([B("x", rect(0, 0, 20, 20))])
        self.assertEqual(len(ar.classify([f], [], [infra((mapgeom.POLYGON, rect(0, 0, 9, 9), "parking", "transportation"))],
                                         FRAME, 0.0, 0.0)), 0)


if __name__ == "__main__":
    unittest.main()
