"""Default signs on the three arms of a roundabout, with and without surveyed designs."""
import sys
from pathlib import Path
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import guardrails
import plsigns
import plstock
import roadsigns
import speedsigns


class PolishDefaults(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(53.69, 17.56)
        self.ground = lambda la, lo: np.zeros_like(la)
        self.empty = plstock.Nearby([])
        # Going north: yaw 0 designates a sign facing drivers arriving from the south.
        self.survey = plstock.Nearby([[guardrails.polish_groups().index("pl_c_12"), 3., 5., 0., 0.]])
        self.rules = roadsigns.tables()[1]["rules"]

    def test_direction_and_role_at_roundabout(self):
        self.assertTrue(self.survey.matched("C-12", 3., 0., 0.))
        self.assertFalse(self.survey.matched("C-12", 3., 0., 180.))
        self.assertFalse(self.survey.matched("C-12", 8., 5., 0.))
        self.assertFalse(self.survey.matched("A-8", 3., 5., 0.))
        self.assertFalse(self.survey.matched("A-7", 3., 5., 0.))
        rows = np.array([[3, 0, 0, 1, self.rules.index(role), 0, 0, 0, 1, i, 0]
                         for i, role in enumerate(("mand.roundabout","warn.roundabout","prio.give_way"))], dtype=float)
        def render(frame, near):
            with mock.patch.object(roadsigns, "place", return_value=rows):
                return roadsigns.build_tile(None, frame, 0, 0, self.ground, None, country="PL", pl=near)
        self.assertEqual(set(render(self.frame, self.empty)[3]), {"pl_a_7","pl_a_8","pl_c_12","bne_cable_steel"})
        self.assertEqual(set(render(self.frame, self.survey)[3]), {"pl_a_7","pl_a_8","bne_cable_steel"})
        for frame, near in ((self.frame,None),(geodesy.EnuFrame(52.52,13.4),self.survey)):
            self.assertIn("bne_signs_vienna_y", render(frame, near)[3])

    def test_junction_furniture_and_speed(self):
        def prop(g, yaw=0):
            return [g, 3., 5., 2., yaw, 1., 3., 0.]
        observed = plstock.Nearby([[guardrails.polish_groups().index("pl_b_20"), 3., 5., 2., 0.],
                                   [guardrails.polish_groups().index("pl_b_33_20"), 3., 5., 2., 0.]])
        rows = plstock.furniture([prop("giveway"), prop("stop",180.), prop("signal")], self.frame, observed)
        self.assertEqual([r[0] for r in rows], ["pl_b_20", "signal"])
        self.assertEqual(rows[1], prop("signal"))
        self.assertEqual(plstock.furniture([prop("giveway")],self.frame,self.empty)[0][0], "pl_a_7")
        s = np.array([[3.,0.,0.,1.,30.,0.,3.,0.,1.],[3.,0.,0.,-1.,50.,0.,3.,0.,1.]])
        mesh = speedsigns._speed_mesh(self.frame,(0.,0.,50.,50.),self.ground,s,[],speedsigns.STYLE_VIENNA,observed)
        self.assertEqual(set(mesh[3]), {"pl_b_33_50","bne_cable_steel"})

    def test_empty_or_failed_survey_keeps_generated_junction_sign(self):
        box = (-10., -10., 10., 10.)
        stock = [["giveway", 3., 5., 2., 0., 1., 3., 0.]]
        for effect in (lambda *a: [], OSError("offline")):
            with mock.patch.object(plsigns, "ON", True), mock.patch.object(plsigns, "tile_rows", side_effect=effect):
                measured = []
                rendered = plsigns.road_layer(self.frame, box, [], self.ground, None, stock, measured)
                self.assertEqual([r[0] for r in rendered], ["pl_a_7"])
                self.assertFalse(measured)
        with mock.patch.object(plsigns, "ON", True), mock.patch.object(plsigns, "signature", side_effect=AssertionError):
            self.assertEqual(plsigns.cache_parts(self.frame, box, rsigns=False), ())

    def test_no_token_uses_rules_even_inside_bundled_survey_coverage(self):
        box = (-20., -20., 20., 20.)
        props = [["giveway", 3., 5., 2., 0., 1., 3., 0.]]
        with mock.patch.object(plsigns, "ON", True), mock.patch.object(plsigns.plmapillary, "token", return_value=""), \
                mock.patch.object(plsigns, "index", side_effect=AssertionError), \
                mock.patch.object(plsigns.plmapillary, "fetch_cell", side_effect=AssertionError):
            measured = []
            md = mock.Mock()
            rows = plsigns.road_layer(self.frame, box, [], self.ground, md, props, measured)
            self.assertEqual([r[0] for r in rows], ["pl_a_7"])
            self.assertFalse(measured)
            self.assertFalse(plstock.sign_stream(md, self.frame, box, self.ground, None, False, False).items)
            self.assertEqual(plsigns.cache_parts(self.frame, box)[0][-1], "road-rules")
