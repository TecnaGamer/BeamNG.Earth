"""Road wear (roadwear.py, earthfast.wear_plan): deterministic pieces along a tile's paved roads, by tier, cut at the
other carriageways, the same pieces in two tiles that share a road, and the road mesh's fade for them."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
import roadmesh  # noqa: E402
import roadwear  # noqa: E402
from test_roads import FRAME  # noqa: E402


def spec(cls="primary", width=10.0, surf=None, urban=False, lanes=None, oneway=False):
    return roads.Spec(width, "paved", urban, cls, oneway, 2.5, 0, None, None, None, lanes, surf)


def line(x0, y0, x1, y1, step=10.0):
    n = max(2, int(np.hypot(x1 - x0, y1 - y0) / step) + 1)
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n), np.zeros(n)], axis=1)


BOX = (0.0, 0.0, 256.0, 256.0)


def plan(rds, tier=2, box=BOX, **kw):
    return roadwear.tile_chunks(rds, {}, box, tier, **kw)


def quads(chunks, m):
    """The quads (4, 3) of the spot chunks of material m (roadmesh "quads": 4 corners each)."""
    out = []
    for c in chunks:
        if c["m"] == m:
            self_check = len(c["poly"]) % 4 == 0 and len(c["uvq"]) == len(c["poly"]) and c.get("quads")
            assert self_check, c
            out.extend(np.asarray(c["poly"], np.float64).reshape(-1, 4, 3))
    return out


def kinds(chunks):
    out = {}
    for c in chunks:
        out[c["m"]] = out.get(c["m"], 0) + 1
    return out


class RoadWearTest(unittest.TestCase):
    def setUp(self):              # (the strips' engine: drawn only with BNE_WEAR_STRIPS=on for now)
        self._strips = roadwear.STRIPS_ON
        roadwear.STRIPS_ON = True

    def tearDown(self):
        roadwear.STRIPS_ON = self._strips

    def test_tier_off_and_unpaved(self):
        rd = [("a", spec(), line(-500, 128, 800, 128), 0.0)]
        self.assertEqual(plan(rd, tier=0), [])
        dirt = [("a", spec()._replace(kind="dirt"), line(-500, 128, 800, 128), 0.0)]
        self.assertEqual(plan(dirt), [])

    def test_basic_tier_is_polish_and_joints_only(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0),
              ("c", spec(surf="concrete", lanes=4, cls="motorway", width=16.0), line(128, -500, 128, 800), 0.0)]
        got = kinds(plan(rd, tier=1))
        self.assertTrue(set(got) <= {"bne_wear_tread", "bne_crack_line"}, got)
        self.assertIn("bne_wear_tread", got)
        self.assertIn("bne_crack_line", got)

    def test_strips_off_by_default(self):
        """The full-lane strips (tyre polish, crack networks) are left out unless BNE_WEAR_STRIPS=on (bright bands in
        game, 2026-11-11); the crack lines stay."""
        roadwear.STRIPS_ON = False
        rd = [("c", spec(surf="concrete", lanes=4, cls="motorway", width=16.0), line(128, -500, 128, 800), 0.0)]
        got = kinds(plan(rd, tier=2))
        self.assertFalse({"bne_wear_tread", "bne_wear_sealed", "bne_wear_cracks"} & set(got), got)
        self.assertIn("bne_crack_line", got)

    def test_concrete_joints_across_every_few_metres(self):
        rd = [("c", spec(surf="concrete", lanes=2, width=8.0), line(-500, 128, 800, 128), 0.0)]
        across = [c for c in plan(rd, tier=1) if c["m"] == "bne_crack_line" and len(c["n"]) == 2
                  and abs(c["n"][0][0] - c["n"][1][0]) < 1e-6]
        xs = sorted(c["n"][0][0] for c in across)
        gaps = np.diff(xs)
        self.assertGreater(len(xs), 30)
        self.assertTrue(np.all((gaps > 4.4) & (gaps < 6.3)), gaps)
        for c in across:            # the whole carriageway across, less 0.2 m each side
            self.assertAlmostEqual(abs(c["n"][0][1] - c["n"][1][1]), 8.0 - 0.4, places=3)

    def test_same_pieces_in_two_tiles(self):
        """Two tiles sharing a road lay the same pieces where their boxes overlap: same seeds, same distances."""
        rd = [("x", spec(lanes=2, cls="secondary"), line(-1000, 300, 1500, 300), 0.0)]
        a = plan(rd, box=(0, 172, 256, 428))
        b = plan(rd, box=(200, 172, 456, 428))

        def near(chunks, x0, x1):
            out = set()
            for c in chunks:
                for pts in (np.asarray(c["poly"]).reshape(-1, 4, 3) if c.get("quads") else [np.asarray(c["n"])]):
                    if pts[:, 0].min() >= x0 and pts[:, 0].max() <= x1:
                        out.add((c["m"], tuple(np.round(pts[0, :2], 2)), round(float(c.get("slen", -1)), 2)))
            return out
        self.assertTrue(a and b)
        self.assertEqual(near(a, 210, 250), near(b, 210, 250))

    def test_strip_fade_fields(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0)]
        for c in plan(rd, tier=1):
            if c["m"] == "bne_wear_tread":
                self.assertEqual(c["fends"], [1, 1])
                self.assertEqual(c["feather"], 0)
                self.assertGreater(c["slen"], 0)
                self.assertLessEqual(c["s1"], c["slen"] + 1e-6)

    def test_cut_at_crossing_carriageway(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0),
              ("b", spec(width=12.0), line(128, -500, 128, 800), 0.0)]
        for c in plan(rd, tier=1):
            n = np.asarray(c["n"])
            if c["m"] == "bne_wear_tread" and abs(n[0, 1] - 128) < 6:     # road a's: none across b's carriageway
                self.assertFalse(n[:, 0].min() < 128 < n[:, 0].max(), n)
                self.assertFalse(np.any(np.abs(n[:, 0] - 128) < 6.0 + 0.5 - 1e-6), n)
        # excluded (a straight continuation): not cut
        ex = plan(rd, tier=1, exclude=lambda f, o: True)
        self.assertTrue(any(c["m"] == "bne_wear_tread" and abs(c["n"][0][1] - 128) < 6
                            and np.asarray(c["n"])[:, 0].min() < 120 < 136 < np.asarray(c["n"])[:, 0].max() for c in ex))

    def test_town_spots_and_lot_oil(self):
        town = [("t", spec(cls="residential", width=8.0, urban=True), line(-800, 128, 1200, 128), 0.0)]
        got = kinds(plan(town))
        self.assertIn("bne_spot_manhole", got)
        aisle = [("lot", spec(cls="service", width=7.0), line(-50, 128, 300, 128), 0.0)]
        oil = quads(plan(aisle, aisles={"lot"}), "bne_spot_oil")
        self.assertTrue(oil)
        for q in oil:               # in the stalls beside the aisle, not on it
            self.assertAlmostEqual(abs(q[:, 1].mean() - 128), 3.5 + 2.0, places=3)

    def test_poor_roads_get_more_damage(self):
        def damage(cond):
            class F:
                attrs = {"osm_smoothness": np.array([cond])}
            rd = [("p", spec(cls="tertiary", width=7.0, lanes=2), line(-1500, 128, 1800, 128), 0.0)]
            ch = roadwear.tile_chunks(rd, {"p": (F, 0, rd[0][1])}, (-2000, -2000, 2000, 2000), 2)
            return sum(c["m"] in ("bne_wear_cracks", "bne_spot_pothole") for c in ch)
        self.assertGreater(damage("bad"), damage("excellent"))
        self.assertEqual(damage("excellent"), 0)

    def test_mesh_draws_quads(self):
        town = [("t", spec(cls="residential", width=8.0, urban=True), line(-800, 128, 1200, 128), 0.0)]
        ch = [c for c in plan(town) if c.get("quads")]
        n = sum(len(c["poly"]) // 4 for c in ch)
        mesh = roadmesh.build(FRAME, ch, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0))
        self.assertEqual(len(mesh[1]), 2 * n)       # two triangles a quad, facing up
        P, T = mesh[0], mesh[1]
        nz = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])[:, 2]
        self.assertTrue(np.all(nz > 0))

    def test_mesh_fades_wear_and_draws_it_first(self):
        rd = [("a", spec(lanes=2), line(0, 128, 256, 128), 0.0)]
        ch = [c for c in plan(rd, tier=1) if c["m"] == "bne_wear_tread"]
        ch.append({"m": "line_white", "w": 0.15, "tl": 6.4, "p": 2, "n": [[0, 128, 0], [256, 128, 0]]})
        mesh = roadmesh.build(FRAME, ch, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0), clip=BOX)
        self.assertEqual(len(mesh), 6)          # (the fade uv)
        self.assertEqual(mesh[3][0], "bne_wear_tread")
        fade = mesh[5]
        self.assertLess(fade[:, 1].min(), 0.05)     # (a run's open start)

    def test_grass_sides(self):
        """A rural lane through fields gets the grassy edge on its field side; a town street, a motorway and scrub
        keep the dirt edge."""
        def cover(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            return np.where(n > 128, 40, 20).astype(np.uint8), True      # cropland north of y = 128, scrub south
        rd = [("lane", spec(cls="tertiary", width=6.0), line(0, 128, 256, 128), 0.0),
              ("town", spec(cls="tertiary", width=6.0, urban=True), line(0, 200, 256, 200), 0.0),
              ("mw", spec(cls="motorway", width=12.0, oneway=True), line(0, 230, 256, 230), 0.0)]
        got, leaves = roadwear.verges(FRAME, rd, cover, roads.has_sidewalks)
        self.assertEqual(got, {("lane", 1)})
        self.assertEqual(leaves, {})
        self.assertEqual(roadwear.verges(FRAME, rd, lambda la, lo: 1 / 0, roads.has_sidewalks), (set(), {}))

    def test_fallen_leaves_under_trees_in_the_fall(self):
        def cover(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            return np.where(n > 128, 10, 30).astype(np.uint8), True      # woods north of y = 128, grass south
        rd = [("lane", spec(cls="tertiary", width=6.0), line(-400, 128, 600, 128), 0.0)]
        _, leaves = roadwear.verges(FRAME, rd, cover, roads.has_sidewalks, fall=1.0)
        self.assertAlmostEqual(leaves[("lane", 1)], 1.0)
        self.assertNotIn(("lane", -1), leaves)
        got = quads(roadwear.tile_chunks(rd, {}, BOX, 2, leaves=leaves), "bne_spot_leaves")
        self.assertTrue(got)
        self.assertTrue(all(q[:, 1].mean() > 128 for q in got))     # the wooded side only
        self.assertFalse([c for c in roadwear.tile_chunks(rd, {}, BOX, 1, leaves=leaves) if c["m"] == "bne_spot_leaves"])

    def test_fall_by_season_and_hemisphere(self):
        self.assertEqual(roadwear.fall_of({"tier": 2, "season": "off"}, 45.0), 0.0)
        self.assertEqual(roadwear.fall_of({"tier": 2, "season": "fall"}, -30.0), 1.0)
        self.assertEqual(roadwear.fall_of(2, 45.0), 0.0)
        import datetime
        import season
        a = season.amounts_for("auto", 48.0, datetime.date(2026, 10, 7))["fall"]
        b = season.amounts_for("auto", -48.0, datetime.date(2026, 10, 7))["fall"]
        self.assertGreater(a, 0.5)
        self.assertLess(b, 0.1)
        self.assertEqual(roadwear.wear_key({"tier": 1, "season": "fall"}, 45.0), (1, 0.0))

    def test_tier_of(self):
        self.assertEqual(roadwear.tier_of("off"), 0 if roadwear.ENV_TIER is None else roadwear.ENV_TIER)
        if roadwear.ENV_TIER is None:
            self.assertEqual(roadwear.tier_of("basic"), 1)
            self.assertEqual(roadwear.tier_of(None), roadwear.DEFAULT_TIER)
            self.assertEqual(roadwear.tier_of("nonsense"), roadwear.DEFAULT_TIER)
            self.assertEqual(roadwear.tier_of(7), 2)


if __name__ == "__main__":
    unittest.main()
