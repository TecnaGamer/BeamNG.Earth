"""Default signs on the three arms of a roundabout, with and without surveyed designs."""
import sys
from pathlib import Path
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

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

    def test_roundabout_is_one_instance_across_both_tile_layers(self):
        groups = guardrails.polish_groups()
        def survey(code, x, y, yaw=0.):
            return [groups.index(plsigns.group(code)), x, y, 0., yaw]
        yield_sign = survey("A-7", 3., -2.)
        circle = survey("C-12", 3., 1.)
        anchor = [None, 3., -5., None, 0.]
        for observations in ([], [yield_sign], [circle], [yield_sign, circle], [yield_sign, circle, circle]):
            with self.subTest(observations=observations), \
                    mock.patch.object(plstock, "ring_entries", return_value=[anchor]), \
                    mock.patch.object(plsigns, "tile_rows", return_value=observations), \
                    mock.patch.object(plsigns, "ON", True):
                placed = []
                props = [["giveway", 3., -4., 0., 0., 1., 3., 0.]]
                for box in ((-10., -20., 20., 0.), (-10., 0., 20., 20.)):
                    self.assertFalse(plsigns.road_layer(self.frame, box, [], self.ground, None, props, placed))
                self.assertEqual([groups[r[0]] for r in placed], [plstock.POST])
                near = plstock.sign_stream(None, self.frame, (-20., -20., 20., 20.), self.ground, None, False, False)
                signs = np.array([[3, -4, 0, 1, self.rules.index(role), 0, 0, 0, 1, i, 0]
                                  for i, role in enumerate(("mand.roundabout", "prio.give_way", "warn.roundabout"))],
                                 dtype=float)
                with mock.patch.object(roadsigns, "place", return_value=signs):
                    mesh = roadsigns.build_tile(None, self.frame, 0, 0, self.ground, None, country="PL", pl=near)
                self.assertEqual(set(mesh[3]), {"pl_a_8", "bne_cable_steel"})

    def test_circle_snapped_to_ring_matches_its_arm_and_keeps_other_directions(self):
        groups = guardrails.polish_groups()
        entries = [[None, -2.7, -7., None, 17.], [None, -13., 26., None, 174.]]
        def row(code, x, y, yaw):
            return [groups.index(plsigns.group(code)), x, y, 0., yaw]
        observed = [row("C-12", 0., 0., -28.), row("A-7", 0.3, 0., -28.), row("A-7", 0., 0., 150.)]
        layout = plstock.roundabout_layout(entries, observed, self.frame, self.ground)
        self.assertCountEqual([groups[r[0]] for r in layout.rows], [plstock.POST, plstock.POST, "pl_a_7"])
        self.assertIn((-2.7, -7.), [tuple(r[1:3]) for r in layout.rows])
        stop = row("B-20", -2.7, -7., 17.)
        layout = plstock.roundabout_layout(entries[:1], [stop], self.frame, self.ground)
        self.assertCountEqual([groups[r[0]] for r in layout.rows], ["pl_c_12", "pl_b_20"])

    def test_combined_model_contains_one_post_and_separated_fronts(self):
        directory = Path(__file__).resolve().parents[2] / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/pl_signs"
        def geometry(name):
            mesh = ET.parse(directory / (name + "_plate.dae")).find(".//{*}mesh")
            source = mesh.find("{*}vertices/{*}input").get("source")[1:]
            positions = np.fromstring(mesh.find(f"{{*}}source[@id='{source}']/{{*}}float_array").text, sep=" ").reshape(-1, 3)
            return {t.get("material"): positions[np.fromstring(t.find("{*}p").text, dtype=int, sep=" ")]
                    for t in mesh.findall("{*}triangles")}
        parts = geometry(plstock.POST)
        self.assertGreater(parts["pl_a_7-mat"][:, 2].min(), parts["pl_c_12-mat"][:, 2].max())
        steel = "bne_cable_steel-mat"
        for group in ("pl_a_7", "pl_c_12"):
            single = geometry(group)
            np.testing.assert_allclose(parts[group+"-mat"], single[group+"-mat"] + [0, 0, .85 if group=="pl_a_7" else 0],
                                       atol=1e-5)
            self.assertEqual(sum(parts[steel][:, 2] < 0), sum(single[steel][:, 2] < 0))

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
            md = mock.Mock(highway_signs=None)
            rows = plsigns.road_layer(self.frame, box, [], self.ground, md, props, measured)
            self.assertEqual([r[0] for r in rows], ["pl_a_7"])
            self.assertFalse(measured)
            self.assertFalse(plstock.sign_stream(md, self.frame, box, self.ground, None, False, False).items)
            self.assertEqual(plsigns.cache_parts(self.frame, box)[0][-1], "road-rules")
