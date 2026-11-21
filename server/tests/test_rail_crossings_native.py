"""Cold rail crossing boundaries. Golden bytes from the Python planner at earthfast 548."""
import hashlib
import random
import struct
import unittest
from unittest.mock import patch

import numpy as np

from test_roads import FRAME, features
import railways


def record(h, value):
    def field(b):
        h.update(struct.pack('<Q', len(b)))
        h.update(b)

    field(type(value).__name__.encode())
    if isinstance(value, np.ndarray):
        field(value.dtype.str.encode())
        record(h, value.shape)
        record(h, value.strides)
        field(value.tobytes())
    elif isinstance(value, (list, tuple, set)):
        field(struct.pack('<Q', len(value)))
        for x in sorted(value) if isinstance(value, set) else value:
            record(h, x)
    elif isinstance(value, float):
        field(struct.pack('<d', value))
    else:
        field(str(value).encode())


def cut_cases():
    rng = random.Random(549)
    h = hashlib.sha256()
    classes = ('residential', 'motorway', 'trunk', 'trunk_link', 'track')
    for case in range(256):
        parts, wear, kept = [], {}, set()
        for i in range(rng.randrange(1, 7)):
            fid = str(i // 2)
            wear[fid] = rng.randrange(3)
            if rng.randrange(3) == 0:
                kept.add(fid)
            # Noncontiguous and reversed views, duplicate vertices, signed zero, both ends cut.
            coords = np.zeros((rng.randrange(3, 60), 4))
            xy = coords[:, ::2]
            xy[:, 0] = np.arange(len(xy)) * 2.5 - 50
            xy[:, 1] = [rng.randrange(-8, 9) * .125 for _ in xy]
            xy[1] = xy[0]
            xy[0, 1] = -0.
            parts.append((fid, 'standard_gauge', rng.choice(('ground', 'ground', 'bridge')),
                          xy if i % 2 else xy[::-1]))
        ways = []
        for j in range(rng.randrange(12)):
            x = rng.randrange(-50, 65)
            # At the shared vertex both road segments may be hits. Nearest-segment tie order matters.
            xy = np.array([[x - rng.randrange(8), -15.], [x, 0.], [x + rng.randrange(8), 15.]])
            ways.append((xy, rng.choice((0., 1.5, 4., 25.)), rng.choice(classes)))
        table = {1: set(classes), 2: set(classes)} if case % 2 else railways.HERITAGE_CUT_CLASSES
        ends = {(123, 456)}
        with patch.object(railways, 'WORN_CUT_CLASSES', table):
            result = railways._cut_worn_at_motorways(parts, wear, ways, kept, ends)
        record(h, (result, ends))
        for fid, _, _, xy in result:
            assert any(np.shares_memory(xy, r[3]) for r in parts if r[0] == fid)
    return h.hexdigest()


def boundary_cases():
    h = hashlib.sha256()
    # Intersections at the first and last vertex of a run, with adjacent floats on
    # either side. Include sloping, almost parallel and reversed road segments.
    for origin in (0., -12000., 2.**32):
        x = origin + 3. * np.arange(97)
        xy = np.column_stack((x, .25 * x))
        for i in (0, 15, 16, 31, 32, 95, 96):
            for toward in (-np.inf, np.inf):
                point = np.nextafter(xy[i], toward)
                for d in (np.array([0., 23.]), np.array([24., np.nextafter(6., toward)])):
                    line = np.array([point - d, point, point + 2*d])
                    for road in (line, line[::-1]):
                        ends = set()
                        result = railways._cut_worn_at_motorways(
                            [('a', 'rail', 'ground', xy)], {'a': 2}, [(road, 1., 'trunk')], cut_ends=ends)
                        record(h, (result, ends))
    return h.hexdigest()


def window_cases():
    rng = random.Random(490)
    h = hashlib.sha256()
    classes = ('motorway_link', 'trunk', 'residential', 'unknown', 'service', 'standard_gauge')
    for _ in range(32):
        layers = []
        for k in range(2):
            n = rng.randrange(5, 20)
            lines = [[(rng.randrange(-80, 100), rng.randrange(-30, 100)) for _ in range(rng.randrange(2, 6))]
                     for _ in range(n)]
            f = features(lines, [rng.choice(classes) for _ in lines], ids=[str(k*4+i) for i in range(n)],
                         subtypes=[rng.choice(('road', 'road', 'rail')) for _ in lines],
                         flags=[(1, 'is_bridge', .25, .65), (2, 'is_under_construction')])
            layers.append(f)
        for lifecycle in (False, True):
            for cells in (layers, layers[::-1]):
                box = (rng.randrange(-75, 0), -20, rng.randrange(20, 80), 55)
                with patch.object(railways, 'RAIL_LIFECYCLE', lifecycle):
                    record(h, railways.motorway_crossings(FRAME, cells, box, 3.))
                    record(h, railways.crossings(FRAME, cells, box, 3.))
        # A layer without a subtype is eligible on every row with lifecycle enabled.
        layers[0].attrs.pop('subtype')
        with patch.object(railways, 'RAIL_LIFECYCLE', True):
            record(h, railways.motorway_crossings(FRAME, layers, (-30, -30, 30, 30)))
    return h.hexdigest()


class CrossingParityTest(unittest.TestCase):
    def test_worn_runs_and_cut_end_mutations(self):
        self.assertEqual(cut_cases(), '10cf8ccb7d6a997630c551968c1152b626959cf6c64ed7b1b7bed28a73f61386')

    def test_segment_run_edges_and_nearly_parallel_streets(self):
        self.assertEqual(boundary_cases(), '66bf7a9d925a3131794e3b9929d4c2a3958cc2f7e902c125d5b8445a31f33618')

    def test_road_windows_and_first_copy_order(self):
        self.assertEqual(window_cases(), 'dfbc219f2f1fddd16ad258a2f9945b5e9b330ada4ca83a998d64a4064850a9e1')
