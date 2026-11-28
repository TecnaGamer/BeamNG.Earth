"""Placement from Polish survey points, including the unmapped side of a border tile."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import furniture
import geodesy
import polishpoles
import powerlines
import mappedpoles
from test_furniture import road
from test_mapped_poles import infrastructure, line, pole


class PolishPolesTest(unittest.TestCase):
    def setUp(self):
        mock = patch("mapillarypoles.enabled", return_value=False)
        mock.start()
        self.addCleanup(mock.stop)

    def test_border_and_roadside_guesses(self):
        self.assertTrue(polishpoles.inside(52.35, 14.56))    # Słubice
        self.assertFalse(polishpoles.inside(52.34, 14.54))   # Frankfurt (Oder)
        args = ([road("c", "tertiary", False)], [], (0., 0., 256., 256.))
        frame = geodesy.EnuFrame(52.34, 14.54)
        self.assertEqual(furniture.place(*args, pole_frame=frame), furniture.place(*args))
        wires = []
        rows = furniture.place(*args, pole_frame=geodesy.EnuFrame(52.35, 14.56), wires=wires)
        self.assertFalse(any(row[0] == furniture.G_POLE for row in rows))
        self.assertEqual(wires, [])

    def test_one_road_crosses_the_polish_german_border(self):
        west, east = 14.54, 14.56
        for _ in range(26):
            middle = (west + east) / 2
            if polishpoles.inside(52.35, middle):
                east = middle
            else:
                west = middle
        frame = geodesy.EnuFrame(52.35, middle)
        args = ([road("c", "tertiary", False, -480., 480.)], [], (-500., -100., 500., 100.))
        candidates = [r for r in furniture.place(*args) if r[0] == furniture.G_POLE]
        expected = [r for r in candidates if not polishpoles.at_position(frame, *r[1:3])]
        self.assertTrue(0 < len(expected) < len(candidates))
        actual = furniture.place(*args, pole_frame=frame)
        self.assertEqual([r for r in actual if r[0] == furniture.G_POLE], expected)

    def test_survey_point_without_a_line(self):
        frame = geodesy.EnuFrame(51.4141825, 15.615475)
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={}))
        with patch("roads.tile_bbox", return_value=(-10., -10., 10., 10.)):
            props, wires = [], []
            powerlines.tile_lines(frame, 0, 0, md, lambda la, lo: np.zeros_like(la), props, wires)
        self.assertEqual([(r[0], r[1], r[2]) for r in props], [("pylon", 0., 0.)])
        self.assertEqual(wires, [])

    def test_only_confirmed_high_line_vertices_and_spans(self):
        frame = geodesy.EnuFrame(52.23, 21.01)
        points = [[25., 30.], [125., 30.], [230., 30.]]
        layer = infrastructure(frame, [pole("n1", *points[0]), pole("n2", *points[1]),
                                       line(points, "power_line", voltage="15000")])
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"infrastructure": layer}))
        box = (0., 0., 256., 256.)
        elev = lambda la, lo: np.zeros_like(la)
        with patch("roads.tile_bbox", return_value=box), patch("roads.cells_of_tile", return_value=[]):
            # Exercise the native layout, with the road relocation proposing a shifted position.
            evidence = polishpoles.Supports(frame, polishpoles.bounds(frame, box), [layer])
            ll = layer.parts(2)[0][0]
            x, y, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            props, wires = [], []
            with patch.object(powerlines, "_placed", return_value=(x+5., y+3.)):
                powerlines._lay(frame, md, elev, [], [("way", ll)], powerlines.G_POLE, powerlines.POLE_H,
                                powerlines.POLE_ARMS, 1., .012, box, props, wires, None, evidence)
        self.assertEqual([r[1:3] for r in props], points[:2])
        self.assertEqual(len(wires), len(powerlines.POLE_ARMS))

    def test_mapped_pole_in_road_is_skipped_with_its_wires(self):
        frame = geodesy.EnuFrame(52.23, 21.01)
        layer = infrastructure(frame, [pole("n1", 10.), pole("n2", 50.), line([[10., 10.], [50., 10.]])])
        props, wires = [], []
        clearance = SimpleNamespace(clear=lambda row: row[1:3] != [10., 10.])
        mappedpoles.place(frame, (0., 0., 100., 100.), [layer], lambda la, lo: np.zeros_like(la),
                          props, wires, None, everywhere=True, clearance=clearance)
        self.assertEqual([r[1:3] for r in props], [[50., 10.]])
        self.assertEqual(wires, [])
