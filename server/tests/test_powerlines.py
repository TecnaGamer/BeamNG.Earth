"""High-voltage lines and substations (earthfast.power_line_layout, earthfast.substation_yard; powerlines.py): a tower
at every vertex outside substations, none inside; a line end where a line reaches a substation, its conductors on the
strain insulators of the kit's gantries there, lines ending close by sharing it; towers shared where lines meet; the
kit at a line end facing the line, every busbar over its column of supports (West Coast USA's yard); the rest of a
yard filled with the largest kit that fits, inside it and clear of the anchored kits; a small yard the smaller kit;
the fence along the whole edge."""
import sys
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # (power_reference: the original NumPy test oracle)
import fast  # noqa: E402
import powerlines  # noqa: E402
from power_reference import margin as road_margin  # noqa: E402

BOX = [-1e4, -1e4, 1e4, 1e4]
# (the yards kept to their fences: BNE_POWER_YARDS; its tests run with it on)
yards_on = mock.patch.object(powerlines, "YARDS_ON", True)


def layout(lines, stop=None, yards=(), ground=None, box=BOX, full=False, yard_id=None):
    """power_line_layout's (towers, ends [x, y, z, heading], wire points, offsets); full: its own five outputs."""
    pts = np.ascontiguousarray(np.concatenate(lines), np.float64)
    off = np.cumsum([0] + [len(r) for r in lines]).astype(np.int64)
    stop = np.zeros(len(pts), bool) if stop is None else np.asarray(stop, bool)
    yp = np.ascontiguousarray(np.concatenate(yards) if len(yards) else np.zeros((0, 2)), np.float64)
    yo = np.cumsum([0] + [len(r) for r in yards]).astype(np.int64)
    got = fast._ef.power_line_layout(pts, off, stop, powerlines.ARMS, powerlines.SAG, powerlines.SEG_M,
                                     powerlines.MIN_SPAN_M, powerlines.MAX_SPAN_M, powerlines.DEDUPE_M,
                                     powerlines.END_MERGE_M, powerlines.GANTRY_FRONT, box, yp, yo,
                                     np.asarray(range(len(yards)) if yard_id is None else yard_id, np.int64),
                                     list(powerlines.ANCHOR_AT), powerlines.GANTRIES, ground)
    return got if full else (got[0], got[1][:, :4], got[2], got[3])


def yard_full(rings, anchors=np.zeros((0, 3))):
    pts = np.ascontiguousarray(np.concatenate(rings), np.float64)
    ro = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
    groups = sorted(powerlines.PARTS)
    kits = np.array([k[0] for k in powerlines.KITS], np.float64)
    parts = np.array([[k, groups.index(g), x, y] for k, (_, ps) in enumerate(powerlines.KITS) for g, x, y in ps])
    a = np.asarray(anchors, np.float64)
    got, fence = fast._ef.substation_yard(pts, ro, kits, parts, powerlines.YARD_GAP,
                                          np.ascontiguousarray(a.reshape(-1, a.shape[-1] if a.size else 3)), 0,
                                          list(powerlines.ANCHOR_AT), powerlines.YARD_MAX_KITS, powerlines.FENCE_LEN,
                                          BOX)
    return [(groups[int(g)], x, y, yaw, z) for g, x, y, yaw, z in got], fence


def yard(rings, anchors=np.zeros((0, 3))):
    got, fence = yard_full(rings, anchors)
    return [q[:4] for q in got], fence


def square(x0, y0, w, h):
    return np.array([[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]], np.float64)


class LineTest(unittest.TestCase):
    def test_towers_and_conductors(self):
        towers, gantries, wp, wo = layout([np.array([[0, 0, 0], [300, 0, 0], [600, 0, 0]], np.float64)])
        self.assertEqual(len(towers), 3)
        self.assertEqual(len(gantries), 0)
        self.assertEqual(len(wo) - 1, 2 * len(powerlines.ARMS))           # two spans, six conductors each
        self.assertTrue(np.allclose(towers[:, 3], 0.0))                   # turned along the line
        w = wp[wo[0]:wo[1]]
        self.assertAlmostEqual(w[0][2], powerlines.ARMS[0][1])           # from the insulator ...
        self.assertLess(w[len(w) // 2][2], w[0][2] - 5.0)                # ... sagging between towers

    def test_into_a_substation(self):
        # the line's last two vertices in a substation: no towers there, a line end at the first, no span on
        line = np.array([[0, 0, 0], [200, 0, 0], [260, 0, 0], [290, 0, 0]], np.float64)
        towers, ends, wp, wo = layout([line], [False, False, True, True])
        self.assertEqual(sorted(towers[:, 0].tolist()), [0.0, 200.0])
        self.assertEqual(len(ends), 1)
        self.assertTrue(np.allclose(ends[0], [260.0, 0.0, 0.0, 0.0]))     # heading into the substation: +x
        last = np.array([wp[b - 1] for b in wo[1:]])
        last = last[last[:, 0] > 250]
        self.assertEqual(len(last), len(powerlines.ARMS))                # every conductor ends on the gantries ...
        self.assertTrue(np.allclose(last[:, 2], powerlines.GANTRY_H))
        self.assertTrue(np.allclose(last[:, 0], 260.0 - powerlines.GANTRY_FRONT))   # ... in front, toward the line
        # each side of the line to its own gantry's three insulators
        lat = np.sort(last[:, 1])
        g = powerlines.GANTRY_LAT
        self.assertTrue(np.allclose(lat, [-g - 1.75, -g, -g + 1.75, g - 1.75, g, g + 1.75]))
        # a line starting in the substation: the same sides in the world
        towers, ends, wp, wo = layout([line[::-1].copy()], [True, True, False, False])
        self.assertTrue(np.allclose(ends[0][:2], [260.0, 0.0]))
        first = np.array([wp[a] for a in wo[:-1]])
        first = first[first[:, 0] > 250]
        self.assertTrue(np.allclose(np.sort(first[:, 1]), lat))

    def test_gantries_inside_a_yard_near_its_fence(self):
        # Codex's review of 1bc9f8cb (report 95): a line end 3 m inside a side fence put a gantry 2.8 m past it; the
        # end moves with its gantries until both stand inside, its conductors with it
        yard = square(0, -30, 60, 33)                   # its north fence at y = 3
        line = np.array([[-200, 0, 0], [-100, 0, 0], [20, 0, 0], [40, 0, 0]], np.float64)
        stop = [False, False, True, True]
        _, ends0, _, _ = layout([line], stop)
        self.assertTrue(np.allclose(ends0[0][:2], [20.0, 0.0]))     # (no yard given: where it was)
        towers, ends, wp, wo = layout([line], stop, yards=[yard])
        self.assertEqual(len(ends), 1)
        ex, ey, _, w = ends[0]
        g = powerlines.GANTRY_LAT
        for side in (-1.0, 1.0):                        # both gantries (across the line) 1 m inside every fence
            gx, gy = ex - np.sin(w) * g * side, ey + np.cos(w) * g * side
            self.assertTrue(1.0 <= gx <= 59.0 and -29.0 <= gy <= 2.0, (gx, gy))
        # (the nearest end that fits: the north gantry's 8 m truss 1 m inside the fence, 7 m in)
        self.assertTrue(np.allclose([ex, ey], [20.0, -7.0]), (ex, ey))
        last = np.array([wp[b - 1] for b in wo[1:]])
        last = last[last[:, 0] > 0.0]                   # (the spans into the yard)
        self.assertEqual(len(last), len(powerlines.ARMS))
        self.assertTrue(np.allclose(last[:, 0], ex - powerlines.GANTRY_FRONT))    # the conductors on the moved gantries
        self.assertTrue(np.allclose(np.sort(last[:, 1]) - ey,
                                    [-g - 1.75, -g, -g + 1.75, g - 1.75, g, g + 1.75]))
        # a yard with room: the end stays where it is
        _, ends, _, _ = layout([line], stop, yards=[square(0, -30, 60, 60)])
        self.assertTrue(np.allclose(ends[0][:2], [20.0, 0.0]))

    def test_a_span_goes_with_the_next_tiles_tower(self):
        # Codex's review of 7bafc807: a span is drawn by its middle's tile, the tower at its far end by its own tile; a
        # racetrack's band there culls that tower while missing its outer conductors' attachments (4.12 m out), so the
        # span's tile kept them hanging. The span carries its towers' rows now and goes with the culled tower
        import racedig
        line = np.array([[0, 0, 0], [100, 0, 0], [200, 0, 0]], np.float64)
        band = [(np.array([[95.0, -8.0], [200.0, -8.0]]), 7.0)]            # to y = 1: the tower at (100, 0) on it
        flat = lambda x, y: np.zeros(len(x))
        box1, box2 = [-50.0, -50.0, 90.0, 50.0], [90.0, -50.0, 250.0, 50.0]
        towers, _, wp, wo, sup = layout([line], box=box1, full=True)
        rows = [powerlines.tower_row(powerlines.G_PYLON, powerlines.PYLON_H, *t[:4]) for t in towers]
        self.assertEqual([r[1] for r in rows], [0.0])
        wires = powerlines.span_wires(wp, wo, sup, powerlines.G_PYLON, powerlines.PYLON_H, powerlines.WIRE_R)
        self.assertEqual(len(wires), len(powerlines.ARMS))                # (the span 0 -> 100: its middle in box 1)
        plain = [(np.asarray(w[0]).copy(), w[1]) for w in wires]
        racedig.clear_props([list(r) for r in rows], plain, band, flat)
        self.assertGreater(len(plain), 0, "(without its towers' rows the outer conductors stayed)")
        racedig.clear_props(rows, wires, band, flat)
        self.assertEqual(wires, [])
        self.assertEqual([r[1] for r in rows], [0.0])
        # the tower's own tile culls it
        towers, _, _, _, _ = layout([line], box=box2, full=True)
        rows2 = [powerlines.tower_row(powerlines.G_PYLON, powerlines.PYLON_H, *t[:4]) for t in towers]
        racedig.clear_props(rows2, [], band, flat)
        self.assertNotIn(100.0, [r[1] for r in rows2])

    @yards_on
    def test_a_span_goes_with_its_culled_gantry(self):
        # Codex's review of 119bb4d7: a conductor's end at a gantry carried no support row, so a raceway band that culls
        # the yard's gantry (in the yard's tile) but misses the conductor's end left it hanging in the span's tile
        import furniture
        import racedig
        yard_ring = square(0, -30, 60, 60)
        line = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        _, ends, wp, wo, sup = layout([line], [False, False, True], yards=[yard_ring], full=True)
        wires = powerlines.span_wires(wp, wo, sup, powerlines.G_PYLON, powerlines.PYLON_H, powerlines.WIRE_R)
        gantry = [r for w in wires for r in furniture.wire_ends(w) if r is not None and r[0] == "gantry"]
        self.assertEqual(len(gantry), len(powerlines.ARMS))
        # (the very rows the yard stands)
        from test_roads import FRAME
        yard_props = []
        powerlines._yards(FRAME, lambda la, lo: np.zeros(np.shape(la)), [("y", [yard_ring])], ends,
                          (-1e4, -1e4, 1e4, 1e4), yard_props)
        self.assertEqual({tuple(r) for r in gantry}, {tuple(r) for r in yard_props if r[0] == "gantry"})
        band = [(np.array([[38.5, -100.0], [38.5, 100.0]]), 7.0)]          # to x = 29.5: the gantries on it
        flat = lambda x, y: np.zeros(len(x))
        plain = [(np.asarray(w[0]).copy(), w[1]) for w in wires]
        racedig.clear_props([], plain, band, flat)
        self.assertEqual(len(plain), len(wires), "(the conductors' own ends are off the band)")
        racedig.clear_props([], wires, band, flat)
        self.assertEqual(len(wires), len(powerlines.ARMS), "the span into the yard goes, the one before stays")
        self.assertTrue(all(np.asarray(w[0])[:, 0].max() < -99.0 for w in wires))

    def test_a_line_end_does_not_hang_on_where_its_run_starts(self):
        # Codex's review of 58788443: the vertices closer than MIN_SPAN_M were dropped greedily from the run's start, so
        # a tile whose run of a feeder began at its last approach vertex (the yard lookup's run) dropped the vertex in
        # the yard (12 m on) and made no line end, while the yard's tile (its run from farther back) made one: the two
        # merged other lines onto different gantries. The vertices either side of a yard's edge stay now
        yard_ring = square(0, -30, 60, 60)
        full = np.array([[-300, 0, 0], [-23, 0, 0], [-12, 0, 0], [0.5, 0, 0], [30, 0, 0]], np.float64)
        st = [False, False, False, True, True]
        _, ends_full, _, _ = layout([full], st, yards=[yard_ring])
        _, ends_cut, _, _ = layout([full[2:]], st[2:], yards=[yard_ring])
        self.assertEqual(len(ends_full), 1)
        np.testing.assert_array_equal(ends_full, ends_cut)

    def test_no_gantries_where_none_fit(self):
        # Codex's review of 7bafc807: a yard too small for the gantries anywhere: no line end, no span into it (the
        # line ends at its last tower), and so no gantries in the yard
        tiny = square(95, -3, 8, 6)
        line = np.array([[-100, 0, 0], [0, 0, 0], [100, 0, 0]], np.float64)
        towers, ends, wp, wo, sup = layout([line], [False, False, True], yards=[tiny], full=True)
        self.assertEqual(len(ends), 0)
        self.assertLess(max(wp[b - 1][0] for b in wo[1:]), 1.0)          # every conductor ends at the last tower
        got, _ = yard([tiny], anchors=ends[:, [0, 1, 3, 4, 5]] if len(ends) else np.zeros((0, 3)))
        self.assertNotIn("gantry", [g for g, *_ in got])

    def test_a_rejected_end_is_no_end_to_share(self):
        # Codex's review of 12b731e1: a line whose gantries fit nowhere in a 13 m wide yard (it comes in along it) and
        # a second line ending 11 m from it, coming in across the yard, where they fit: the second shared the first's
        # rejected end and lost its span; now it tries its own
        narrow = square(0, -6.5, 60, 13)
        along = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        across = np.array([[40, 200, 0], [40, 100, 0], [40, 5, 0]], np.float64)
        _, ends, wp, wo = layout([along, across], [False, False, True, False, False, True], yards=[narrow])
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][0], 40.0)
        self.assertTrue(-6.5 < ends[0][1] <= 5.0, ends)                       # (in along its line: the legs inside)
        last = np.array([wp[b - 1] for b in wo[1:]])
        self.assertEqual(int((np.abs(last[:, 1] - (ends[0][1] + powerlines.GANTRY_FRONT)) < 1e-6).sum()),
                         len(powerlines.ARMS))                               # its conductors on its gantries
        # (the first line: no span into the yard, it ends at its last tower)
        self.assertFalse(any(abs(p[0] - (30.0 - powerlines.GANTRY_FRONT)) < 0.5 for p in last))

    @yards_on
    def test_overlapping_yards_anchor_an_end_once(self):
        # Codex's review of 9618c426: two overlapping yard polygons both holding an end anchored its kit in each (two
        # coincident gantry pairs; the second's gantries past its own fence). The end carries its yard: anchored once
        from test_roads import FRAME
        big, small = square(0, -30, 60, 60), square(10, -4, 40, 8)     # (the end in both; the gantries only in big)
        line = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        _, ends, _, _, _ = layout([line], [False, False, True], yards=[big, small], yard_id=[0, 1], full=True)
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][6], 0.0)
        props = []
        powerlines._yards(FRAME, lambda la, lo: np.zeros(np.shape(la)), [("big", [big]), ("small", [small])], ends,
                          (-1e4, -1e4, 1e4, 1e4), props)
        self.assertEqual(sum(1 for r in props if r[0] == "gantry"), 2)

    def test_an_end_moves_only_within_its_own_yard(self):
        # Codex's review of 117bdde7: the search tested the gantries against every ring together, so a moved end could
        # sit in a hole of its yard (the yard's kit skips an anchor there) or straddle two adjacent yards
        line = np.array([[-200, 3, 0], [-100, 3, 0], [30, 3, 0]], np.float64)
        stop = [False, False, True]
        # a yard 24 m across with a strip hole 3 m wide along its middle: the gantries' trusses (8 m) fit only with the
        # end in the hole
        outer, hole = square(0, -12, 60, 24), square(5, -1.5, 50, 3)
        _, ends, wp, wo = layout([line], stop, yards=[outer, hole], yard_id=[0, 0])
        self.assertEqual(len(ends), 0, ends)
        self.assertFalse(any(p[0] > 0.0 for p in (wp[b - 1] for b in wo[1:])))
        # two adjacent yards, the end in a 10 m one: its gantries never fit in it (they did across both)
        a, b = square(0, -5, 60, 10), square(0, 5, 60, 30)
        line0 = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        _, ends, _, _ = layout([line0], stop, yards=[a, b], yard_id=[0, 1])
        self.assertEqual(len(ends), 0, ends)

    def test_a_gantrys_whole_truss_inside_a_narrow_yard(self):
        # Codex's review of 0cf9fd9b: the fit tested each gantry's centre and four points 1 m off, but its truss is 8 m
        # across: in an 18 m wide yard both centres (5.8 m either side of the end) passed while the trusses stood out
        narrow = square(0, -9, 60, 18)
        line = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        _, ends, _, _ = layout([line], [False, False, True], yards=[narrow])
        self.assertEqual(len(ends), 0, ends)                                   # (5.8 + 4 > 9: nowhere)
        _, ends, _, _ = layout([line], [False, False, True], yards=[square(0, -11, 60, 22)])
        self.assertEqual(len(ends), 1)                                         # (22 m: it fits)

    def test_no_hole_under_a_gantry(self):
        # Codex's review of 1ac6e583: a small hole wholly inside a gantry's footprint crossed none of its edges and left
        # its corners inside: the end was taken with the gantry over the hole. No ring vertex may lie under it now
        yard_ring = square(0, -30, 60, 60)
        line = np.array([[-200, 0, 0], [-100, 0, 0], [30, 0, 0]], np.float64)
        _, ends0, _, _ = layout([line], [False, False, True], yards=[yard_ring])
        gl = powerlines.GANTRIES[0]
        hx = 30.0 + (powerlines.ANCHOR_AT[1] - gl[1])                       # (the left gantry's place, heading +x:
        hy = gl[0] - powerlines.ANCHOR_AT[0]                                 # its kit x across the line, to +y)
        hole = square(hx - 0.5, hy - 0.5, 1.0, 1.0)
        self.assertTrue(np.allclose(ends0[0][:2], [30.0, 0.0]))
        _, ends, _, _ = layout([line], [False, False, True], yards=[yard_ring, hole], yard_id=[0, 0])
        self.assertEqual(len(ends), 1)
        ex, ey, _, w = ends[0]
        got, _ = yard_full([yard_ring, hole], anchors=[[ex, ey, w, 0.0, 0.0]])
        gantries = [(x, y, yaw) for g, x, y, yaw, z in got if g == "gantry"]
        self.assertEqual(len(gantries), 2)
        for x, y, yaw in gantries:                      # (the hole's middle off every gantry's footprint)
            a = np.radians(yaw)
            lx = (hx - x) * np.cos(a) + (hy - y) * np.sin(a)
            ly = -(hx - x) * np.sin(a) + (hy - y) * np.cos(a)
            self.assertTrue(abs(lx) > 4.0 or abs(ly) > 2.87, (x, y, lx, ly))

    def test_a_yard_across_the_antimeridian(self):
        # Codex's review of 0cf9fd9b: a yard straddling +-180 deg was unwrapped round its mean longitude (near 0): a
        # box round the world, cells_for_bbox's ValueError uncaught. Unwrapped round one of its vertices now; a box
        # that is still refused is logged and its cells left out
        import geodesy
        frame = geodesy.EnuFrame(10.0, 179.9997)
        ring = square(-20, -20, 80, 40)                 # (to about 40 m east of the antimeridian)
        la, lo, _ = frame.enu_to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
        self.assertTrue(np.any(np.asarray(lo) < 0) and np.any(np.asarray(lo) > 0))
        cells = powerlines._yard_cells(frame, [ring])
        self.assertTrue(0 < len(cells) <= 4, cells)
        self.assertEqual({k.x for k in cells}, {0, (1 << cells[0].z) - 1})
        self.assertEqual(powerlines._yard_cells(frame, [square(-4e5, -4e5, 8e5, 8e5)]), [])   # (refused: logged)

    def test_moved_gantries_on_sloping_ground(self):
        # Codex's review of 7bafc807: a moved end's conductors hung at the old end's ground while the yard stood its
        # gantries on the ground under them: on a slope a gap. Now both read the ground under each gantry, once
        yard_ring = square(0, -30, 60, 33)
        line = np.array([[-200, 0, 0], [-100, 0, 0], [20, 0, 0], [40, 0, 0]], np.float64)
        asked = []

        def ground(x, y):
            asked.extend(zip(np.asarray(x).tolist(), np.asarray(y).tolist()))
            return 100.0 + 0.2 * np.asarray(x) + 0.1 * np.asarray(y)
        _, ends, wp, wo, _ = layout([line], [False, False, True, True], yards=[yard_ring], ground=ground, full=True)
        self.assertEqual(len(ends), 1)
        ex, ey, ez, w, zl, zr = ends[0][:6]
        self.assertTrue(np.allclose([ex, ey], [20.0, -7.0]))               # (moved: the north gantry inside)
        self.assertEqual(len(asked), 2)
        got, _ = yard_full([yard_ring], anchors=ends[:, [0, 1, 3, 4, 5]])
        gantries = sorted((x, y, z) for g, x, y, yaw, z in got if g == "gantry" and np.isfinite(z))
        self.assertEqual(len(gantries), 2)
        for x, y, z in gantries:                        # the yard's gantries where the ground was read, at that ground
            self.assertIn((x, y), asked)
            self.assertEqual(z, 100.0 + 0.2 * x + 0.1 * y)
        self.assertEqual(sorted([zl, zr]), sorted(z for *_, z in gantries))
        last = np.array([wp[b - 1] for b in wo[1:]])
        last = last[last[:, 0] > 0.0]
        side = (last[:, 0] - ex) * -np.sin(w) + (last[:, 1] - ey) * np.cos(w)
        self.assertTrue(np.allclose(last[:, 2], np.where(side > 0, zr, zl) + powerlines.GANTRY_H))
        self.assertNotAlmostEqual(zl, ez)                                  # (not the old end's ground)

    def test_ends_merge(self):
        # two lines ending 12 m apart: one end, both lines' conductors on it
        a = np.array([[0, 0, 0], [200, 0, 0], [260, 0, 0]], np.float64)
        b = np.array([[0, 12, 0], [200, 12, 0], [260, 12, 0]], np.float64)
        _, ends, wp, wo = layout([a, b], [False, False, True, False, False, True])
        self.assertEqual(len(ends), 1)
        last = np.array([wp[q - 1] for q in wo[1:]])
        self.assertTrue(np.all(np.abs(last[last[:, 0] > 250][:, 1]) < powerlines.GANTRY_LAT + 2.0))

    def test_shared_tower(self):
        a = np.array([[0, 0, 0], [300, 0, 0]], np.float64)
        b = np.array([[300, 2, 0], [300, 300, 0]], np.float64)
        towers, _, _, _ = layout([a, b])
        self.assertEqual(len(towers), 3)                                  # the lines' meeting: one tower


class YardTest(unittest.TestCase):
    def test_big_yard(self):
        ring = square(0, 0, 120, 60)
        got, fence = yard([ring])
        self.assertIn("sub_breaker", [g for g, *_ in got])
        kits = sum(g == "sub_breaker" for g, *_ in got)
        self.assertGreaterEqual(kits, 4)
        for g, x, y, yaw in got:
            self.assertTrue(0 < x < 120 and 0 < y < 60, (g, x, y))
        self.assertTrue(all(abs(yaw % 180) < 1e-6 or abs(yaw % 180 - 180) < 1e-6 for *_, yaw in got))
        # the fence: the whole edge (360 m of 2.04 m panels), on it
        self.assertGreaterEqual(len(fence), int(360 / powerlines.FENCE_LEN))
        on = [min(abs(x), abs(x - 120), abs(y), abs(y - 60)) < 1e-6 for x, y, _ in fence]
        self.assertTrue(all(on))

    def test_small_yard(self):
        got, _ = yard([square(0, 0, 26, 24)])
        self.assertEqual(sorted(g for g, *_ in got), ["sub_mast", "sub_transformer"])

    def test_line_end_kit(self):
        # a line coming in from the west (heading +x) to (20, 50) in a 120 x 100 yard: the big kit there, its gantries
        # 5.8 m either side of the end, facing the line (their +y west: yaw 90 degrees, x north), every busbar (29.5 m
        # along the gantry's -y: east, into the yard) over its column of six supports; the rest filled clear of it
        got, _ = yard([square(0, 0, 120, 100)], anchors=[[20.0, 50.0, 0.0]])
        gantries = sorted((round(y, 2), round(x, 2), yaw) for g, x, y, yaw in got if g == "gantry")
        self.assertGreaterEqual(len(gantries), 2)
        g = powerlines.GANTRY_LAT
        near = [q for q in gantries if abs(q[1] - 20.0) < 1.0 and abs(q[0] - 50.0) < g + 0.5]
        self.assertEqual([round(q[0] - 50.0, 1) for q in near], [round(-g + 0.045, 1), round(g + 0.045, 1)])
        self.assertTrue(all(abs(q[2] - 90.0) < 1e-6 for q in near))
        supports = [(x, y) for g_, x, y, _ in got if g_ == "sub_support"]
        for y, x, _ in near:
            under = [s for s in supports if abs(s[1] - y) < 1.2 and x - 4.0 < s[0] < x + 29.5]
            self.assertEqual(len(under), 6, (x, y))
        # the other kits clear of it: no part of another kit within its footprint
        breakers = [(x, y) for g_, x, y, _ in got if g_ == "sub_breaker"]
        self.assertGreater(len(breakers), 1)
        self.assertEqual(sum(1 for x, y in breakers if abs(x - 20.0) < 25 and abs(y - 50.0) < 13), 1)

    def test_line_end_kit_stays_in_a_narrow_yard(self):
        # the user's report 95 (a Tennessee substation, 2026-11-17): a 52 x 25 m yard, the line in along its short side
        # just inside it, heading -y: the 38 m kit ran out past the fence; only its parts inside stay, and the gantries
        # the conductors end on
        ring = square(0, 0, 52, 25)
        got, _ = yard([ring], anchors=[[6.0, 21.0, -np.pi / 2, np.nan, np.nan]])     # (BNE_POWER_YARDS' anchors)
        gantries = [(x, y) for g, x, y, _ in got if g == "gantry"]
        near = [q for q in gantries if abs(q[1] - 21.0) < 1.0]
        self.assertEqual(len(near), 2, gantries)
        others = [(g, round(x, 1), round(y, 1)) for g, x, y, _ in got if g != "gantry"]
        self.assertTrue(others)
        self.assertEqual([q for q in others if not (0 < q[1] < 52 and 0 < q[2] < 25)], [])

    def test_turned_yard(self):
        # a yard along a diagonal: the kits turned with it
        c, s = np.cos(np.radians(30)), np.sin(np.radians(30))
        ring = square(-60, -30, 120, 60) @ np.array([[c, s], [-s, c]])
        got, _ = yard([ring])
        yaws = {round(yaw % 180, 3) for *_, yaw in got}
        self.assertEqual(yaws, {30.0})



class VoltageTest(unittest.TestCase):
    """Lines under HV_MIN_V go on poles (GitHub #787: a 66 kV line on street poles in Shepparton drawn as lattice pylons)."""

    def test_low_voltage(self):
        import powerlines as pl
        self.assertTrue(pl.low_voltage('{"power":"line","voltage":"66000"}'))
        self.assertTrue(pl.low_voltage('{"voltage":"33000;11000"}'))
        self.assertFalse(pl.low_voltage('{"voltage":"132000;66000"}'), "a circuit at 132 kV: the towers")
        self.assertFalse(pl.low_voltage('{"voltage":"220000"}'))
        self.assertFalse(pl.low_voltage('{"power":"line"}'), "no voltage mapped: the towers")
        self.assertFalse(pl.low_voltage('{"voltage":"high"}'))
        self.assertFalse(pl.low_voltage(""))

class OffRoadsTest(unittest.TestCase):
    """TGForever Discord 2026-10-28: a pylon on a highway's shoulder (its mapped tower node a few metres off)."""

    def test_tower_moved_off_the_carriageway(self):
        import gasstations
        import geodesy
        frame = geodesy.EnuFrame(40.0, -105.0)
        road = (np.array([[-100.0, 0.0], [100.0, 0.0]]), 5.0)       # a centreline along x, 10 m carriageway
        old = gasstations.drive_lines
        gasstations.drive_lines = lambda frame, layers, x, y, r: [road]
        try:
            class Md:
                def cell(self, key, layers, kind):
                    raise OSError("not here")
            e, n = np.array([10.0, 20.0, 30.0]), np.array([2.0, -1.0, 40.0])
            la, lo, _ = frame.enu_to_geodetic(e, n, np.zeros(3))
            e2, n2 = powerlines._off_roads(frame, Md(), e, n, np.stack([lo, la], -1))
        finally:
            gasstations.drive_lines = old
        want = 5.0 + powerlines.TOWER_ROAD_CLEAR_M
        self.assertAlmostEqual(n2[0], want)            # (straight away from the road, on its own side)
        self.assertAlmostEqual(n2[1], -want)
        self.assertAlmostEqual(e2[0], 10.0)
        self.assertAlmostEqual(e2[1], 20.0)
        self.assertEqual((e2[2], n2[2]), (30.0, 40.0))  # (far off it: where it is mapped)

    def moved(self, roads, e, n):
        import gasstations
        import geodesy
        frame = geodesy.EnuFrame(49.4, 17.45)
        old = gasstations.drive_lines
        gasstations.drive_lines = lambda frame, layers, x, y, r: roads
        try:
            class Md:
                def cell(self, key, layers, kind):
                    raise OSError("not here")
            e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
            la, lo, _ = frame.enu_to_geodetic(e, n, np.zeros(len(e)))
            return powerlines._off_roads(frame, Md(), e, n, np.stack([lo, la], -1))
        finally:
            gasstations.drive_lines = old

    def test_between_dual_carriageways(self):
        # GitHub #550: a tower 0.8 m off one of the D1's carriageways, the other 13 m away: pushed off the one onto
        # the other and back, it stood on the motorway. Now clear of both.
        roads = [(np.array([[-300.0, 0.0], [300.0, 0.0]]), 4.0), (np.array([[-300.0, 13.0], [300.0, 13.0]]), 4.0)]
        e2, n2 = self.moved(roads, [0.0], [0.8])
        m = road_margin(np.array([[e2[0], n2[0]]]), roads, powerlines.TOWER_ROAD_CLEAR_M)[0]
        self.assertGreaterEqual(m, -1e-6)
        self.assertLess(np.hypot(e2[0], n2[0] - 0.8), 15.0)       # the nearest clear spot: just off the one road

    def test_in_a_junction_corner(self):
        # a tower in the corner of a crossroads: clear of both roads
        roads = [(np.array([[-300.0, 0.0], [300.0, 0.0]]), 5.0), (np.array([[0.0, -300.0], [0.0, 300.0]]), 5.0)]
        e2, n2 = self.moved(roads, [3.0], [4.0])
        self.assertGreaterEqual(road_margin(np.array([[e2[0], n2[0]]]), roads, powerlines.TOWER_ROAD_CLEAR_M)[0], -1e-6)

class TowerFootprintTest(unittest.TestCase):
    """The native layout decides yaw; check its emitted rows against the tile's authoritative solved roads."""

    def place(self, lines=(), decks=(), clearance=None, tags=None, pts=((100., 100.), (200., 100.)), elev=None):
        import furniture
        import mapgeom
        from test_roads import FRAME
        pts = np.asarray(pts, np.float64)
        la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        infra = mapgeom.Features(
            layer="infrastructure", ids=np.array(["line"]), kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, len(pts)], np.int64), lonlat=np.column_stack([lo, la]),
            attrs={"class": np.array(["power_line"]), **({"tags": np.array([tags], object)} if tags else {})},
            rules={})

        class Md:
            def cell(self, key, layers, kind):
                from types import SimpleNamespace
                return SimpleNamespace(layers={"infrastructure": infra} if "infrastructure" in layers else {"roads": None})

        props, wires = [], []
        if clearance is None:
            clearance = furniture.RoadClearance(lines, decks, bounds=(0., 0., 256., 256.))
        powerlines.tile_lines(FRAME, 0, 0, Md(), elev or (lambda la, lo: np.zeros(np.shape(la))), props, wires,
                              clearance=clearance)
        return props, wires

    def test_a_tower_two_ways_share_has_one_scale(self):
        """Two ways sharing a tower, one on flat ground, the other over a ridge (Codex's review): the shared tower's
        scale is the most either way's spans ask, and both ways' conductors hang on its arms at that scale, in the tile
        drawing the tower and in the tiles drawing either way's span."""
        def ground(x, y):
            x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
            return np.maximum(0.0, 30.0 * (1.0 - np.hypot(x - 120.0, y - 165.0) / 25.0))
        flat_way = np.array([[20.0, 100.0, 0.0], [120.0, 100.0, 0.0]])
        ridge_way = np.array([[120.0, 100.0, 0.0], [120.0, 230.0, 0.0]])
        pts = np.ascontiguousarray(np.concatenate([flat_way, ridge_way]), np.float64)
        off = np.array([0, 2, 4], np.int64)
        tops = []
        for box in ([-1e4, -1e4, 1e4, 1e4], [100.0, 90.0, 140.0, 110.0], [0.0, 90.0, 100.0, 110.0],
                    [100.0, 150.0, 140.0, 180.0]):
            towers, _, wp, wo, _ = fast._ef.power_line_layout(
                pts, off, np.zeros(4, bool), powerlines.ARMS, powerlines.SAG, powerlines.SEG_M, powerlines.MIN_SPAN_M,
                powerlines.MAX_SPAN_M, powerlines.DEDUPE_M, powerlines.END_MERGE_M, powerlines.GANTRY_FRONT, box,
                np.zeros((0, 2)), np.zeros(1, np.int64), np.zeros(0, np.int64), list(powerlines.ANCHOR_AT),
                powerlines.GANTRIES, None, ground, powerlines.SPAN_CLEAR_M)
            at_t = [np.asarray(wp[a:b]) for a, b in zip(wo[:-1], wo[1:])]
            ends = [q for w in at_t for q in (w[0], w[-1]) if np.hypot(q[0] - 120.0, q[1] - 100.0) < 6.0]
            for t in towers:
                if np.hypot(t[0] - 120.0, t[1] - 100.0) < 1e-6:
                    tops.append(t[4])
            # every conductor end at the shared tower: one of its arms' heights at a single scale
            heights = sorted({round(float(q[2]), 4) for q in ends})
            if ends:
                s = heights[0] / powerlines.ARMS[:, 1].min()
                self.assertTrue(all(any(abs(z - h * s) < 1e-3 for h in powerlines.ARMS[:, 1]) for z in heights),
                                (box, heights, s))
                tops.append(s)
        self.assertGreater(max(tops), 1.0, "the ridge way lifts the shared tower")
        self.assertTrue(all(abs(t - tops[0]) < 1e-6 for t in tops), tops)

    def test_a_span_over_a_crest_clears_the_ground(self):
        """A span climbing over a ridge between its towers (Breno, the player's report 0XESWVG2: its conductors through
        the hill): every conductor at least SPAN_CLEAR_M over the ground along it, its towers scaled up where the chord
        alone is under that; a span over flat ground keeps its towers' size and its whole sag."""
        from test_roads import FRAME

        def ridge(la, lo):                     # (a 30 m ridge across the middle of the span, x 125 +- 25 m)
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            return np.maximum(0.0, 30.0 * (1.0 - np.abs(np.asarray(e) - 125.0) / 25.0))
        pts = ((20., 100.), (230., 100.))
        for elev, scaled in ((ridge, True), (None, False)):
            props, wires = self.place(tags='{"power":"line","voltage":"220000"}', pts=pts, elev=elev)
            towers = [r for r in props if r[0] == powerlines.G_PYLON]
            self.assertEqual(len(towers), 2)
            self.assertEqual(all(r[6] > powerlines.PYLON_H for r in towers), scaled, towers)
            self.assertTrue(all(r[6] <= 1.75 * powerlines.PYLON_H + 0.01 for r in towers))
            ground = elev or (lambda la, lo: np.zeros(np.shape(la)))
            for w, _ in wires:
                w = np.asarray(w)
                la, lo, _ = FRAME.enu_to_geodetic(w[:, 0], w[:, 1], np.zeros(len(w)))
                clear = w[:, 2] - np.asarray(ground(la, lo))
                self.assertGreaterEqual(float(clear.min()), powerlines.SPAN_CLEAR_M - 0.05)
            if not scaled:                  # (flat: the whole sag, SAG x the span, at the middle)
                w = np.asarray(wires[0][0])
                chord = (w[0, 2] + w[-1, 2]) / 2
                self.assertAlmostEqual(chord - float(w[:, 2].min()), powerlines.SAG * 210.0, delta=0.05)

    def test_service_road_rejects_collider_but_keeps_conductors(self):
        service = (4., np.array([[100., 0., 0.], [100., 256., 0.]]))
        before, old_wires = self.place()
        after, wires = self.place([service])
        self.assertEqual(len(before), 2)
        self.assertEqual(len(after), 1)
        self.assertGreater(after[0][1], 190., "only the tower clear of the service carriageway stays")
        self.assertEqual(len(wires), 6)
        self.assertEqual(len(wires), len(old_wires))
        for (a, ar), (b, br) in zip(wires, old_wires):
            np.testing.assert_array_equal(a, b)
            self.assertEqual(ar, br)

    def test_low_voltage_on_poles(self):
        """A 66 kV line: the wooden poles with three conductors, not left out (GitHub #797) nor pylons (#787)."""
        props, wires = self.place(tags='{"power":"line","voltage":"66000"}')
        self.assertEqual({r[0] for r in props}, {powerlines.G_POLE})
        self.assertEqual(len(props), 2)
        self.assertEqual(props[0][6], powerlines.POLE_H)
        self.assertEqual(len(wires), 3)
        self.assertTrue(all(r == powerlines.POLE_WIRE_R for _, r in wires))
        top = max(float(np.asarray(w)[:, 2].max()) for w, _ in wires)
        self.assertAlmostEqual(top, 9.21 * powerlines.POLE_H / powerlines.POLE_SHAPE_H, delta=0.05)
        hv, hw = self.place(tags='{"power":"line","voltage":"220000"}')
        self.assertEqual({r[0] for r in hv}, {powerlines.G_PYLON})
        self.assertEqual(len(hw), 6)

    def test_a_pole_faces_its_conductors_at_a_corner_and_on_a_straight(self):
        """The wooden pole's cross-arm and insulators lie along its model's x (electric_pole_wood_old_01's anchor
        points): each conductor's end at a pole is that far along the pole's x, turned by its row's yaw, at a corner
        (the bisector: the player's report BTZMCHC4, Drumheller) and on a straight run alike."""
        props, wires = self.place(tags='{"power":"line","voltage":"72000"}',
                                  pts=((40., 60.), (120., 60.), (120., 200.), (180., 200.)))
        poles = [r for r in props if r[0] == powerlines.G_POLE]
        self.assertEqual(len(poles), 4)
        lateral = sorted(powerlines.POLE_ARMS[:, 0].tolist())
        for r in poles:
            a = np.radians(r[4])
            x_axis = np.array([np.cos(a), np.sin(a)])        # (trees.lua: a row's yaw turns the model's +x there)
            ends = []
            for w, _ in wires:
                w = np.asarray(w)
                for q in (w[0], w[-1]):
                    if np.hypot(q[0] - r[1], q[1] - r[2]) < 3.0:
                        ends.append(q[:2] - np.array([r[1], r[2]]))
            self.assertGreaterEqual(len(ends), 3, r)
            for d in ends:
                along = float(d @ x_axis)
                across = float(d @ np.array([-x_axis[1], x_axis[0]]))
                self.assertLess(abs(across), 0.02, (r, d))
                self.assertTrue(any(abs(along - v) < 0.02 for v in lateral), (r, d))
        corner = min(poles, key=lambda r: np.hypot(r[1] - 120., r[2] - 60.))
        # the corner's cross-arm bisects the turn: across the mean of its spans' directions (east, then north)
        a = np.radians(corner[4])
        self.assertAlmostEqual(abs(float(np.array([np.cos(a), np.sin(a)]) @ (np.array([1., 1.]) / np.sqrt(2.)))), 0.0,
                               places=2)

    def test_mapped_pole_supports_put_a_high_voltage_line_on_poles(self):
        """(PR #919 by wyo-backroads) A 115 kV line whose mapped supports (its way's nodes) are power=pole points: the
        wooden poles; mostly power=tower points, or none mapped: the pylons."""
        import mapgeom
        from test_roads import FRAME
        la, lo, _ = FRAME.enu_to_geodetic(np.array([100., 200.]), np.array([100., 100.]), 0.)

        def run(classes):
            n = 1 + len(classes)
            infra = mapgeom.Features(
                layer="infrastructure", ids=np.array(["line"] + [f"p{i}" for i in range(len(classes))]),
                kind=np.array([mapgeom.LINE] + [mapgeom.POINT] * len(classes), np.uint8),
                part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
                coord_off=np.r_[0, 2 + np.arange(n)].astype(np.int64),
                lonlat=np.vstack([np.column_stack([lo, la]), np.column_stack([lo, la])[:len(classes)]]),
                attrs={"class": np.array(["power_line"] + classes),
                       "tags": np.array(['{"power":"line","voltage":"115000"}'] + ["{}"] * len(classes), object)},
                rules={})

            class Md:
                def cell(self, key, layers, kind):
                    from types import SimpleNamespace
                    return SimpleNamespace(layers={"infrastructure": infra} if "infrastructure" in layers
                                           else {"roads": None})
            props, wires = [], []
            powerlines.tile_lines(FRAME, 0, 0, Md(), lambda la, lo: np.zeros(np.shape(la)), props, wires)
            return {r[0] for r in props}, len(wires)
        self.assertEqual(run(["power_pole", "power_pole"]), ({powerlines.G_POLE}, 3))
        self.assertEqual(run(["power_tower", "power_tower"]), ({powerlines.G_PYLON}, 6))
        self.assertEqual(run([]), ({powerlines.G_PYLON}, 6))
        self.assertEqual(run(["power_pole", "power_tower"]), ({powerlines.G_PYLON}, 6), "no majority: the towers")

    def far_yard_world(self, feeder=False, feeder_end=-5.0, feeder_from=(-300.0,), poles_at_yard=False):
        """A tile whose span runs 1.9 km to a 60 m yard in a map cell beyond its lookup: (tx, the yard tile's tx, far x,
        Md); feeder: a second line ("a", before the span's "b") ending in the yard 9 m from it, mapped only in the
        yard's cell (Codex's review of 119bb4d7)."""
        import mapcells
        import mapgeom
        import roads
        from test_roads import FRAME
        from types import SimpleNamespace
        for tx in range(-12, 13):
            x0, y0, x1, y1 = roads.tile_bbox(tx, 0)
            mid = (x0 + x1) / 2
            far, near = mid - 950.0, mid + 950.0
            la, lo, _ = FRAME.enu_to_geodetic(np.array([far, near]), np.array([100.0, 100.0]), 0.0)
            far_cell = mapcells.cell_of(float(la[0]), float(lo[0]))
            if far_cell not in roads.cells_of_tile(FRAME, tx, 0, margin=powerlines.MAX_SPAN_M / 4):
                break
        else:
            self.skipTest("no such tile near the test frame")
        ll = lambda xy: np.column_stack(FRAME.enu_to_geodetic(np.asarray(xy)[:, 0], np.asarray(xy)[:, 1], 0.0)[1::-1])
        ring = [[far - 30, 70], [far + 30, 70], [far + 30, 130], [far - 30, 130], [far - 30, 70]]
        hv = '{"power":"line","voltage":"220000"}'
        b = ("b", mapgeom.LINE, "power_line", hv, ll([[far, 100.0], [near, 100.0]]))
        a_xy = [[far + d, 108.0] for d in feeder_from] + [[far + feeder_end, 108.0]]
        # (poles_at_yard: no voltage mapped, its power=pole points only in the yard's cell, and the line in every cell)
        a = ("a", mapgeom.LINE, "power_line", "{}" if poles_at_yard else hv, ll(a_xy))
        yard = ("yard", mapgeom.POLYGON, "substation", "{}", ll(ring))

        def features(items):
            n = len(items)
            return mapgeom.Features(
                layer="infrastructure", ids=np.array([q[0] for q in items]), kind=np.array([q[1] for q in items], np.uint8),
                part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
                coord_off=np.r_[0, np.cumsum([len(q[4]) for q in items])].astype(np.int64),
                lonlat=np.vstack([q[4] for q in items]), attrs={"class": np.array([q[2] for q in items]),
                                                              "tags": np.array([q[3] for q in items], object)}, rules={})
        pts = [(f"p{k}", mapgeom.POINT, "power_pole", "{}", ll([q])) for k, q in enumerate(a_xy)] if poles_at_yard else []
        at_yard = features(([a] if feeder else []) + [b, yard] + pts)
        elsewhere = features(([a] if feeder and poles_at_yard else []) + [b])

        class Md:
            def cell(self, key, layers, kind):
                return SimpleNamespace(layers={"infrastructure": at_yard if key == far_cell else elsewhere}
                                       if "infrastructure" in layers else {"roads": None})
        return tx, int(np.floor(far / roads.TILE)), far, Md()

    @yards_on
    def test_a_long_feeders_vertices_stand_alike_in_every_tile(self):
        """Codex's review of 1ac6e583: the stations' keepout was read for the box round all the vertices a call placed;
        a long feeder's whole run (the feeder search) made a box the station lookup gave up on (none: its end stayed
        outside the yard) while the yard tile's shorter run moved it in. Each vertex reads its own cell's now."""
        import busstationkeep
        import furniture
        from test_roads import FRAME
        tx, yard_tx, far, md = self.far_yard_world(feeder=True, feeder_end=-35.0,
                                                   feeder_from=(-8000.0, -6000.0, -4000.0, -2000.0))

        class Keep:
            def push_out(self, xy):
                xy = np.array(xy, np.float64)
                hit = (xy[:, 0] > far - 40.0) & (xy[:, 0] < far - 31.0) & (np.abs(xy[:, 1] - 108.0) < 5.0)
                xy[hit, 0] += 23.0
                return xy
        # (the lookup's cap: no stations for a box over 6 km)
        capped = lambda md_, frame_, box: Keep() if max(box[2] - box[0], box[3] - box[1]) < 6000.0 else None
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(busstationkeep, "keepout", capped):
            props, wires = [], []
            powerlines.tile_lines(FRAME, tx, 0, md, flat, props, wires)
            yard_props = []
            powerlines.tile_lines(FRAME, yard_tx, 0, md, flat, yard_props, [])
        hung = {tuple(r) for w in wires if furniture.wire_ends(w) for r in furniture.wire_ends(w)
                if r is not None and r[0] == "gantry"}
        built = {tuple(r) for r in yard_props if r[0] == "gantry"}
        self.assertEqual(len(built), 2)
        self.assertEqual(hung, built)

    @yards_on
    def test_a_long_feeder_reads_only_round_the_yard(self):
        """Codex's review of 08f31f7c: the feeder search placed every vertex of a candidate way (its roads' and
        stations' cells read along all of it, a 100 km way's for one tile). Only the vertices within FEED_REACH_M of a
        yard are placed now."""
        import mapcells
        from test_roads import FRAME
        tx, _, far, md = self.far_yard_world(feeder=True, feeder_end=-5.0,
                                             feeder_from=(-8000.0, -6000.0, -4000.0, -2000.0))
        asked = []
        real = md.cell

        def cell(key, layers, kind):
            if "roads" in layers:
                asked.append(key)
            return real(key, layers, kind)
        md.cell = cell
        powerlines.tile_lines(FRAME, tx, 0, md, lambda la_, lo_: np.zeros(np.shape(la_)), [], [])
        la, lo, _ = FRAME.enu_to_geodetic(np.array([far - 8000.0]), np.array([108.0]), 0.0)
        remote = mapcells.cell_of(float(la[0]), float(lo[0]))
        self.assertTrue(asked)
        self.assertNotIn(remote, asked)

    @yards_on
    def test_a_feeder_is_one_class_in_every_tile(self):
        """Codex's review of 732f777f: a feeder with no voltage mapped, its power=pole points only in the yard's cell:
        the span tile (the line in its own cells, the poles not) laid it out as a pylon line, the yard tile as a pole
        line, and the span merged onto different gantries in each. Its class is read from the cells at its two ends
        now, whichever tile asks."""
        import furniture
        from test_roads import FRAME
        tx, yard_tx, far, md = self.far_yard_world(feeder=True, poles_at_yard=True)
        groups = {}
        real = powerlines._lay

        def spy(frame, md_, elev, subs, lines, group, *a, **k):
            for fid, _ in lines:
                groups.setdefault(fid, set()).add((spy.tile, group))
            return real(frame, md_, elev, subs, lines, group, *a, **k)
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(powerlines, "_lay", spy):
            spy.tile = "span"
            props, wires = [], []
            powerlines.tile_lines(FRAME, tx, 0, md, flat, props, wires)
            spy.tile = "yard"
            yard_props = []
            powerlines.tile_lines(FRAME, yard_tx, 0, md, flat, yard_props, [])
        self.assertEqual(groups["a"], {("span", powerlines.G_POLE), ("yard", powerlines.G_POLE)})
        hung = {tuple(r) for w in wires if furniture.wire_ends(w) for r in furniture.wire_ends(w)
                if r is not None and r[0] == "gantry"}
        built = {tuple(r) for r in yard_props if r[0] == "gantry"}
        self.assertTrue(hung and hung <= built, (hung, built))

    def line_world(self, xs, pole_at=(), fail=()):
        """(frame, layer, i, md): an untagged line along x (xs, at y 100) and power=pole points at the x in pole_at;
        md.cell gives the layer for every cell (the points only in their own cells) and raises for the cells of fail."""
        import mapcells
        import mapgeom
        from test_roads import FRAME
        from types import SimpleNamespace
        ll = lambda xy: np.column_stack(FRAME.enu_to_geodetic(np.asarray(xy)[:, 0], np.asarray(xy)[:, 1], 0.0)[1::-1])
        def cell_at(x):
            la, lo, _ = FRAME.enu_to_geodetic(np.array([x]), np.array([100.0]), 0.0)
            return mapcells.cell_of(float(np.ravel(la)[0]), float(np.ravel(lo)[0]))
        line = ll([[x, 100.0] for x in xs])

        def features(items):
            n = len(items)
            return mapgeom.Features(
                layer="infrastructure", ids=np.array([q[0] for q in items]), kind=np.array([q[1] for q in items], np.uint8),
                part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
                coord_off=np.r_[0, np.cumsum([len(q[4]) for q in items])].astype(np.int64),
                lonlat=np.vstack([q[4] for q in items]), attrs={"class": np.array([q[2] for q in items]),
                                                              "tags": np.array([q[3] for q in items], object)}, rules={})
        per_cell = {}
        for x in pole_at:
            per_cell.setdefault(cell_at(x), []).append((f"p{x}", mapgeom.POINT, "power_pole", "{}", ll([[x, 100.0]])))
        bad = {cell_at(x) for x in fail}
        a = ("a", mapgeom.LINE, "power_line", "{}", line)

        class Md:
            def cell(self, key, layers, kind):
                if key in bad:
                    raise OSError("unreadable")
                return SimpleNamespace(layers={"infrastructure": features([a] + per_cell.get(key, []))})
        return FRAME, features([a]), 0, Md()

    def test_a_line_class_reads_along_the_line(self):
        # Codex's reviews of 8ac4b974 and 07eab0c0: the class read only the cells at a line's two ends, then five
        # sampled vertices' cells, so poles mapped between them (no voltage, no supports at its ends) made a pylon line;
        # every cell along it now
        xs = [0.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0]
        frame, f, i, md = self.line_world(xs, pole_at=(2000.0, 3000.0, 4000.0))
        self.assertTrue(powerlines._line_class(md, f, i, "{}", {}))
        # (the only pole in a cell no five-vertex sample reads: index 3 of six, at 4000 m)
        _, f, i, md = self.line_world([0.0, 500.0, 1000.0, 4000.0, 6000.0, 6500.0], pole_at=(4000.0,))
        self.assertTrue(powerlines._line_class(md, f, i, "{}", {}))
        _, f, i, md = self.line_world(xs)
        self.assertFalse(powerlines._line_class(md, f, i, "{}", {}))

    def test_a_line_class_survives_an_unreadable_cell(self):
        import revisit
        xs = [0.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0]
        _, f, i, md = self.line_world(xs, pole_at=(3000.0,), fail=(6000.0,))
        before = revisit.faults()
        self.assertTrue(powerlines._line_class(md, f, i, "{}", {}))         # (the readable cells' poles)
        self.assertGreater(revisit.faults(), before)

    @yards_on
    def test_a_line_laid_out_nowhere_here_is_not_classed(self):
        # Codex's review of 8ac4b974: the tile's scan classed every line its cells hold, a long one with no vertex
        # near the tile too (its far cells read, an unreadable one losing the tile's power lines); only lines with a
        # run here are classed now
        from test_roads import FRAME
        _, f, i, md = self.line_world([30000.0, 31000.0, 32000.0])
        asked = []
        real = powerlines._line_class
        with mock.patch.object(powerlines, "_line_class", lambda *a: asked.append(a) or real(*a)):
            powerlines.tile_lines(FRAME, 0, 0, md, lambda la_, lo_: np.zeros(np.shape(la_)), [], [])
        self.assertEqual(asked, [])

    def test_a_clipped_run_does_not_hide_the_yard_approach(self):
        # Codex's reviews of 732f777f and 07eab0c0: a line laid out round the tile (its run clipped there) was skipped by
        # the feeder search, then its yard approach added as a run of its own: the layout thinned the join differently
        # from a tile that saw it whole. The runs and approaches of a ring are merged where they overlap, share a
        # vertex or follow on
        self.assertEqual(powerlines._merged([(10, 14), (0, 11)]), [(0, 14)])
        self.assertEqual(powerlines._merged([(0, 5), (5, 9)]), [(0, 9)])            # (sharing a vertex)
        self.assertEqual(powerlines._merged([(0, 5), (6, 9)]), [(0, 9)])            # (following on: the span 5 -> 6)
        self.assertEqual(powerlines._merged([(0, 4), (7, 9)]), [(0, 4), (7, 9)])

    def test_a_join_at_a_clipped_run_lays_out_as_one_run(self):
        # (the reviewer's case: A - B 12 m at the clipped run's end, C in the yard: as one run the layout keeps B, the
        # yard's edge vertex, and heads the end B -> C, as a tile that saw the line whole)
        yard_ring = square(0, -30, 60, 60)
        whole = np.array([[-300, 0, 0], [-24, 0, 0], [-12, 0, 0], [5, 0, 0]], np.float64)
        st = [False, False, False, True]
        _, ends_whole, wp_w, wo_w = layout([whole], st, yards=[yard_ring])
        merged = powerlines._merged([(0, 2), (2, 3)])
        self.assertEqual(merged, [(0, 3)])
        _, ends_join, wp_j, wo_j = layout([whole[merged[0][0]:merged[0][1] + 1]], st, yards=[yard_ring])
        np.testing.assert_array_equal(ends_whole, ends_join)
        np.testing.assert_array_equal(wp_w, wp_j)

    @yards_on
    def test_feeders_read_where_the_layout_stands_them(self):
        """Codex's review of 0cf9fd9b: the feeder search tested the mapped points, the layout the points moved off the
        roads and out of the bus stations' ground: a feeder mapped 5 m outside the yard, moved 18 m into it, ended in
        the yard for the yard tile only. Both read the moved places now: the span lands on the yard tile's gantries."""
        import busstationkeep
        import furniture
        from test_roads import FRAME
        tx, yard_tx, far, md = self.far_yard_world(feeder=True, feeder_end=-35.0)

        class Keep:                     # (a bus station's ground just west of the yard pushes 10 m east)
            def push_out(self, xy):
                xy = np.array(xy, np.float64)
                hit = (xy[:, 0] > far - 40.0) & (xy[:, 0] < far - 31.0) & (np.abs(xy[:, 1] - 108.0) < 5.0)
                xy[hit, 0] += 23.0         # (to 12 m in: 14 m from the span's end, merged)
                return xy
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        with mock.patch.object(busstationkeep, "keepout", lambda md_, frame_, box: Keep()):
            props, wires = [], []
            powerlines.tile_lines(FRAME, tx, 0, md, flat, props, wires)
            yard_props = []
            powerlines.tile_lines(FRAME, yard_tx, 0, md, flat, yard_props, [])
        hung = {tuple(r) for w in wires if furniture.wire_ends(w) for r in furniture.wire_ends(w)
                if r is not None and r[0] == "gantry"}
        built = {tuple(r) for r in yard_props if r[0] == "gantry"}
        self.assertEqual(len(built), 2)
        self.assertEqual(hung, built)

    @yards_on
    def test_a_span_into_a_yard_beyond_the_tiles_cells(self):
        """Codex's review of 9618c426: a span drawn here (its middle in the tile) ends 1 km off in a small yard whose map
        cell is beyond the tile's 500 m lookup: the yard was not loaded, its vertex taken for a tower, the conductors
        hung on a tower its own tile never draws. The yard of every laid-out vertex's cell is read now."""
        from test_roads import FRAME
        tx, _, far, md = self.far_yard_world()
        props, wires = [], []
        powerlines.tile_lines(FRAME, tx, 0, md, lambda la_, lo_: np.zeros(np.shape(la_)), props, wires)
        self.assertEqual(len(wires), len(powerlines.ARMS))
        ends = [np.asarray(w[0])[0] if abs(np.asarray(w[0])[0][0] - far) < 10 else np.asarray(w[0])[-1] for w in wires]
        self.assertTrue(all(abs(p[0] - far) < 10 for p in ends))
        # (on the gantries, GANTRY_H over the frame's ground there; a tower's arms hang 15.65 m and more up)
        self.assertTrue(all(abs(p[2] - powerlines.GANTRY_H) < 1.0 for p in ends), [p[2] for p in ends])

    @yards_on
    def test_the_span_lands_on_the_gantries_its_yard_builds(self):
        """Codex's review of 119bb4d7: an earlier line ending in the same yard, mapped only in the yard's cell: the yard
        tile merged the span's end into that line's gantries while the span tile, which knew only its own line, hung
        the conductors on gantries of its own. The lines ending in every yard read are laid out too now; each conductor
        carries the gantry it hangs on, the very row the yard tile stands."""
        import furniture
        from test_roads import FRAME
        tx, yard_tx, far, md = self.far_yard_world(feeder=True)
        flat = lambda la_, lo_: np.zeros(np.shape(la_))
        props, wires = [], []
        powerlines.tile_lines(FRAME, tx, 0, md, flat, props, wires)
        span = [w for w in wires if furniture.wire_ends(w) and any(
            r is not None and r[0] == "gantry" for r in furniture.wire_ends(w))]
        self.assertEqual(len(span), len(powerlines.ARMS))
        hung = {tuple(r) for w in span for r in furniture.wire_ends(w) if r is not None and r[0] == "gantry"}
        yard_props = []
        powerlines.tile_lines(FRAME, yard_tx, 0, md, flat, yard_props, [])
        built = {tuple(r) for r in yard_props if r[0] == "gantry"}
        self.assertEqual(len(built), 2)
        self.assertEqual(hung, built)

    def test_uses_actual_mapped_width(self):
        # The pivot is outside the class-default 8 m road, but not a source-mapped 24 m carriageway.
        wide = (12., np.array([[110., 0., 0.], [110., 256., 0.]]))
        props, wires = self.place([wide])
        self.assertEqual(len(props), 1)
        self.assertGreater(props[0][1], 190.)
        self.assertTrue(wires)

    def test_real_clearance_below_a_high_bridge_is_preserved(self):
        xy = np.array([[0., 100.], [256., 100.]])
        high = (np.column_stack([xy, np.full(2, 50.)]), 4.)
        low = (np.column_stack([xy, np.full(2, 20.)]), 4.)
        self.assertEqual(len(self.place(decks=[high])[0]), 2)
        self.assertEqual(self.place(decks=[low])[0], [])

    def test_per_segment_taper_keeps_the_other_tower_and_every_conductor(self):
        # A 10 m offset clears the stock tower beside the narrow segment, but not the widened continuation.
        line = np.array([[0., 110., 0.], [150., 110., 0.], [256., 110., 0.]])
        _, old_wires = self.place()
        before, _ = self.place([(4., line)])
        after, wires = self.place([(np.array([4., 12.]), line)])
        self.assertEqual(len(before), 2)
        self.assertEqual(len(after), 1)
        self.assertLess(after[0][1], 110.)
        self.assertEqual(len(wires), len(old_wires))
        for (a, ar), (b, br) in zip(wires, old_wires):
            np.testing.assert_array_equal(a, b)
            self.assertEqual(ar, br)


class CacheErrorTest(unittest.TestCase):
    """A cache file the server cannot open (PermissionError, PR #861's review at 50.8468, 7.0975): the tile goes on
    without its power lines, never with half of them; the caller's rows (its decks' furniture) are left as they were."""

    def run_with(self, fail_on=None, ground_fails=False):
        from test_roads import FRAME
        good = TowerFootprintTest.place.__get__(TowerFootprintTest())   # (the same one-line infrastructure)
        props_ok, wires_ok = good()
        self.assertTrue(props_ok and wires_ok)

        def cell(key, layers, kind):
            if fail_on in layers:
                raise PermissionError(13, "Access is denied", "overture/infrastructure/x.npz")
            return real.cell(key, layers, kind)

        import mapgeom
        la, lo, _ = FRAME.enu_to_geodetic(np.array([100., 200.]), np.array([100., 100.]), 0.)
        infra = mapgeom.Features(
            layer="infrastructure", ids=np.array(["line"]), kind=np.array([mapgeom.LINE], np.uint8),
            part_off=np.array([0, 1], np.int64), ring_off=np.array([0, 1], np.int64),
            coord_off=np.array([0, 2], np.int64), lonlat=np.column_stack([lo, la]),
            attrs={"class": np.array(["power_line"])}, rules={})

        class Real:
            def cell(self, key, layers, kind):
                from types import SimpleNamespace
                return SimpleNamespace(layers={"infrastructure": infra} if "infrastructure" in layers else {"roads": None})
        real = Real()
        md = type("Md", (), {"cell": staticmethod(cell)})()
        def ground(la, lo):
            if ground_fails:
                raise PermissionError(13, "Access is denied", "dem/12_2130_1366.npz")
            return np.zeros(np.shape(la))
        props, wires = [["deck_rail", 1.0]], [("deck wire", 0.1)]
        with self.assertLogs("earthd.powerlines", "WARNING") as logged:
            powerlines.tile_lines(FRAME, 0, 0, md, ground, props, wires)
        self.assertEqual(props, [["deck_rail", 1.0]])
        self.assertEqual(wires, [("deck wire", 0.1)])
        self.assertIn("PermissionError", logged.output[0])

    def test_infrastructure_unreadable(self):
        self.run_with("infrastructure")

    def test_ground_unreadable_after_lines_were_read(self):
        """The failure after the lines were read and laid out (the ground under the towers): nothing of it is added."""
        self.run_with(ground_fails=True)


if __name__ == "__main__":
    unittest.main()
