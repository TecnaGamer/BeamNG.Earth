import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import nvdb_marking_overlay_v046 as paint
import nvdb_nationwide_v05 as nvdb
import roadmesh
import roads


LINE = np.array([[0., 0., 10.], [36., 0., 10.]])
BOX = (-1., -10., 37., 10.)
FRAME = geodesy.EnuFrame(60.4, 7.2, 0.)
ROAD = roads.NorwegianTunnelSpec(*roads.Spec(8.5, 'paved', False, 'trunk', False, lanes=2))


def intervals(chunks, material):
    return sorted((c['n'][0][0], c['n'][-1][0]) for c in chunks if c['m'] == material)


class TunnelPaint(unittest.TestCase):
    def export(self, sources, box=BOX, country=None):
        t = SimpleNamespace(frame=FRAME, height=lambda fid: 6.8,
                            parts=lambda cells, bounds: [('road#0', ROAD, LINE[:, :2], LINE[:, 2], 7.2)])
        with (patch.object(nvdb, 'read_tile', return_value=[]),
              patch.object(paint, '_indexed', return_value=object()),
              patch.object(paint, '_lookup', return_value=sources)):
            return roads._tunnel_chunks(t, [], box, country=country)

    def test_norwegian_tunnel_keeps_warning_paint_when_anchor_country_or_source_is_missing(self):
        absent = {role: [None] * 13 for role in ('M', 'H', 'V')}
        chunks = self.export(absent, country='SE')
        self.assertEqual(intervals(chunks, 'line_yellow'), [(0., 9.), (12., 21.), (24., 33.)])
        self.assertTrue(all(c.get('fid') == 'road#0' for c in chunks if c.get('fixed')))

    def test_mapped_solid_run_replaces_only_its_own_part_and_keeps_dash_phase(self):
        samples = np.arange(0., 37., 3.)
        checked = {'M': [('S', .15, 'solid', p, 0.) if 12. <= p <= 24. else None for p in samples],
                   'H': [None] * len(samples), 'V': [None] * len(samples)}
        left = self.export(checked, (-1., -10., 28., 10.))
        right = self.export(checked, (28., -10., 37., 10.))
        self.assertEqual(intervals(left, 'line_yellow'), [(0., 9.), (12., 24.), (24., 28.)])
        self.assertEqual(intervals(right, 'line_yellow'), [(28., 33.)])
        self.assertEqual(intervals(left, 'line_white'), [(0., 28.), (0., 28.)])

    def test_recorded_dashed_edges_replace_the_generic_centre_and_solid_edges(self):
        samples = np.arange(0., 37., 3.)
        checked = {side: [('dashed', .10, side, p, 0.) for p in samples] for side in ('H', 'V')}
        checked['M'] = [None] * len(samples)
        chunks = self.export(checked)
        self.assertEqual(intervals(chunks, 'line_yellow'), [])
        self.assertEqual(intervals(chunks, 'line_white'),
                         [(float(p), p + 3.) for p in range(0, 36, 6) for _ in range(2)])

    def test_ordinary_source_centre_keeps_the_tunnel_warning_pattern(self):
        samples = np.arange(0., 37., 3.)
        checked = {'M': [('F', .1, 'ordinary', p, 0.) for p in samples],
                   'H': [None] * len(samples), 'V': [None] * len(samples)}
        self.assertEqual(intervals(self.export(checked), 'line_yellow'), [(0., 9.), (12., 21.), (24., 33.)])

    def test_paint_follows_its_own_deck_at_an_overlapping_tunnel(self):
        floors = [('road#0', ROAD, LINE[:, :2], LINE[:, 2], 7.2),
                  ('road#1', ROAD, LINE[:, :2], LINE[:, 2] + 15., 7.2)]
        tunnel = SimpleNamespace(frame=FRAME, height=lambda fid: 6.8, parts=lambda cells, box: floors)
        with patch.object(paint, 'adjust', side_effect=lambda items, *_args, **_kw: items):
            chunks = roads._tunnel_chunks(tunnel, [], BOX)
        ribbons = roadmesh._overlays_on_decks(roadmesh.ribbon_pairs(chunks))
        z = {c['fid']: float(r[5][0]) for r, c in ribbons if c['m'] == 'line_yellow'}
        self.assertEqual(z, {'road#0': 10., 'road#1': 25.})


if __name__ == '__main__':
    unittest.main()
