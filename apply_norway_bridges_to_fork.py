#!/usr/bin/env python3
"""Apply missing Norwegian NVDB bridge fixes to the EXISTING feature branch.

    py apply_norway_bridges_to_fork.py --check
    py apply_norway_bridges_to_fork.py

No junction files, marking modules, or cached world data are modified. Git
is the rollback mechanism: inspect Changes in GitHub Desktop before committing.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Two standalone Python sources below; no separate ZIP files are needed.
PAYLOAD_FILES = {
    'server/nvdb_bridge_fixes_v059.py': '''"""Norway-only NVDB bridge deck/abutment fixes, ported from local v0.5.6-0.5.8.

Uses existing nonblocking NVDB width records. Does not change junction geometry,
road markings, NPCs, or the way world data is downloaded. Unknown widths leave
BeamNG.Earth's original bridge geometry in place.
"""
from __future__ import annotations

import logging
import math
import numpy as np

LOG = logging.getLogger("earthd.nvdb_bridge_fixes_v059")
SENTINEL_M = 18.75  # must fall outside supported NVDB measurements (2.5..15m)
MIN_COVERAGE = 0.75
MAX_BRIDGE_DEVIATION_M = 1.10
DECK_MARGIN_PER_SIDE_M = 0.45


class MatchedWidths(np.ndarray):
    """Marker for confident NVDB carriageway measurement (not deck shoulder)."""


def _tag(values):
    out = np.ascontiguousarray(values, dtype=np.float64).view(MatchedWidths)
    out._nvdb_matched = True
    return out


def _norway(lon, lat):
    return (lon.ndim == lat.ndim == 1 and lon.shape == lat.shape and len(lon) >= 2
            and np.isfinite(lon).all() and np.isfinite(lat).all()
            and 57.5 <= float(np.mean(lat)) <= 72.0
            and 3.9 <= float(np.mean(lon)) <= 32.5)


def _native_taper(width, tapers, lon, lat):
    """Original bridge-to-bridge transition, WITHOUT Norwegian road-width overlay.

    This must not call roads.taper_widths(), which now performs an independent
    NVDB lookup; using that result as a fallback reintroduced patchy flares.
    """
    if not tapers:
        return None
    out = np.full(lon.shape, float(width), dtype=np.float64)
    touched = False
    for tlon, tlat, target, span in tapers:
        if span <= 0:
            continue
        d = np.hypot((lon - tlon) * 111320.0 * math.cos(math.radians(tlat)),
                     (lat - tlat) * 110540.0)
        near = d < span
        if near.any():
            touched = True
            t = np.clip(d / span, 0.0, 1.0)
            out += np.where(near, (target - width) * (1 - t * t * (3 - 2 * t)), 0.0)
    return out if touched else None


def bridge_widths(lon, lat, original_width, native_tapers=()):
    """Confident NVDB carriageway widths on a simple Norwegian bridge.

    >=75% coverage, max 1.1m deviation from the median; never combine a
    partial NVDB measurement with the original independent bridge taper.
    Return the original bridge taper (or None) if uncertain or outside Norway.
    """
    lon, lat = np.asarray(lon, np.float64).reshape(-1), np.asarray(lat, np.float64).reshape(-1)
    if lon.shape != lat.shape or not np.isfinite(original_width) or not 2.5 <= original_width <= 28:
        return None
    old = _native_taper(original_width, native_tapers, lon, lat)
    if not _norway(lon, lat):
        return old
    try:
        from nvdb_width_profile_v03 import at_points
        measurements = at_points(lon, lat, SENTINEL_M)
        if measurements is None:
            return old
        a = np.asarray(measurements, np.float64)
        if a.shape != lon.shape:
            return old
        good = (np.isfinite(a) & (2.5 <= a) & (a <= 15.0)
                & (np.abs(a - SENTINEL_M) > 0.25))
        if good.mean() < MIN_COVERAGE:
            return old
        median = float(np.median(a[good]))
        if np.max(np.abs(a[good] - median)) > MAX_BRIDGE_DEVIATION_M:
            return old
        # Small consistent gradients can be real; missing samples are filled
        # from the common median rather than generating a sudden funnel.
        chosen = a if good.all() and len(a) >= 3 and np.ptp(a[good]) <= 0.5 else np.full_like(a, median)
        return _tag(np.round(chosen, 3))
    except (ImportError, ValueError, TypeError, AttributeError, FloatingPointError, OverflowError) as exc:
        LOG.debug('NVDB bridge profile fallback: %s', exc)
        return old


def structural_deck_widths(widths, chunk_points, bridge_points):
    """Add a constant 45cm *each side* to slab, collision and parapet width.

    Marking offsets continue to use the original measured carriageway widths.
    No centre hump/taper is added to the structure.
    """
    w = np.asarray(widths, np.float64)
    part = np.asarray(chunk_points, np.float64)
    whole = np.asarray(bridge_points, np.float64)
    if (w.ndim != 1 or len(w) < 2 or part.ndim != 2 or whole.ndim != 2
            or part.shape[0] != len(w) or len(whole) < 2
            or part.shape[1] < 2 or whole.shape[1] < 2
            or not np.isfinite(w).all() or not np.isfinite(part[:, :2]).all()
            or not np.isfinite(whole[:, :2]).all()):
        return widths
    return np.round(w + 2 * DECK_MARGIN_PER_SIDE_M, 3).tolist()


def wall_width_core(frame, points, spec, default_width, default_core, shoulder_m):
    """Keep Norwegian rural retaining-wall probes aligned with NVDB grading.

    Uses the same reliable-width estimator as nvdb_terrain_v0514.
    No changes on urban, mountain, median, or ambiguous roads.
    """
    if (spec is None or spec.kind not in ('paved', 'cobble') or spec.urban
            or spec.median or spec.mountain):
        return default_width, default_core
    if not (57.5 <= float(getattr(frame, 'lat0', -90)) <= 72
            and 3.9 <= float(getattr(frame, 'lon0', -180)) <= 32.5):
        return default_width, default_core
    try:
        pts = np.asarray(points, np.float64)
        if pts.ndim != 2 or pts.shape[1] < 2 or len(pts) < 2:
            return default_width, default_core
        if len(pts) > 24:
            pts = pts[np.unique(np.round(np.linspace(0, len(pts) - 1, 24)).astype(int))]
        lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        import nvdb_terrain_v0514
        width = nvdb_terrain_v0514.measured_width(np.column_stack((lon, lat)))
        if width is None:
            return default_width, default_core
        # Terrain v0.5.14 repairs only excessively broad grading cores.
        candidate = width / 2 + min(max(0.0, float(shoulder_m)),
                                    nvdb_terrain_v0514.MAX_FLAT_SHOULDER_M)
        if candidate >= float(default_core) - nvdb_terrain_v0514.MIN_EDIT_M:
            return default_width, default_core
        return width, candidate
    except (ImportError, ValueError, TypeError, AttributeError, FloatingPointError, OverflowError) as exc:
        LOG.debug('NVDB retaining wall width fallback: %s', exc)
        return default_width, default_core


def clean_walls(frame, walls, decks):
    """Remove only short disconnected concrete nubs beside bridge abutments.

    Never remove true parapets, stone walls or long retaining walls.
    """
    if not (57.5 <= float(getattr(frame, 'lat0', -90)) <= 72
            and 3.9 <= float(getattr(frame, 'lon0', -180)) <= 32.5):
        return walls
    tips = []
    for deck in decks or ():
        xy = np.asarray(deck.get('n', []), np.float64)
        if deck.get('deck') and xy.ndim == 2 and xy.shape[1] >= 2 and len(xy) >= 2:
            tips.extend((xy[0, :2], xy[-1, :2]))
    if not tips:
        return walls
    ends = np.asarray(tips, np.float64)
    out = []
    for wall in walls:
        xy = np.asarray(wall.get('base', []), np.float64)
        if wall.get('parapet') or not wall.get('concrete') or xy.ndim != 2 or xy.shape[1] < 2 or len(xy) < 2:
            out.append(wall)
            continue
        length = float(np.linalg.norm(np.diff(xy[:, :2], axis=0), axis=1).sum())
        nearest = float(np.min(np.linalg.norm(xy[:, None, :2] - ends[None, :, :], axis=2)))
        if length >= 22.0 or nearest >= 10.0:
            out.append(wall)
    return out
''',
    'tests/test_nvdb_bridge_fixes_v059.py': '''"""Offline tests of the Norway-only bridge follow-up (no NVDB network needed)."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

import numpy as np

MODULE = pathlib.Path(__file__).resolve().parents[1] / 'server' / 'nvdb_bridge_fixes_v059.py'
spec = importlib.util.spec_from_file_location('nvdb_bridge_fixes_v059', MODULE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.lon = np.array([7.6700, 7.6701, 7.6702, 7.6703])
        self.lat = np.full(4, 62.89)

    def widths(self, sample):
        m = types.ModuleType('nvdb_width_profile_v03')
        m.at_points = lambda lon, lat, original: None if sample is None else np.array(sample)
        return mock.patch.dict(sys.modules, {'nvdb_width_profile_v03': m})

    def test_straight_bridge_measured(self):
        with self.widths([7.4, 7.4, 7.4, 7.4]):
            result = b.bridge_widths(self.lon, self.lat, 9.0)
        self.assertTrue(result._nvdb_matched)
        np.testing.assert_allclose(result, 7.4)

    def test_poor_coverage_keeps_original(self):
        with self.widths([7.4, 18.75, 18.75, 7.4]):
            self.assertIsNone(b.bridge_widths(self.lon, self.lat, 9.0))

    def test_partial_good_coverage_has_constant_width(self):
        with self.widths([7.4, 18.75, 7.5, 7.5]):
            result = b.bridge_widths(self.lon, self.lat, 9.0)
        np.testing.assert_allclose(result, 7.5)

    def test_conflicting_widths_rejected(self):
        with self.widths([7.2, 7.2, 10.8, 10.8]):
            self.assertIsNone(b.bridge_widths(self.lon, self.lat, 9.0))

    def test_fallback_native_tapers_when_missing(self):
        with self.widths(None):
            result = b.bridge_widths(self.lon, self.lat, 9.0,
                                     [(self.lon[0], self.lat[0], 10.5, 30.0)])
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 10.5)
        self.assertGreater(result[0], result[-1])
        self.assertFalse(getattr(result, '_nvdb_matched', False))

    def test_outside_norway_untouched(self):
        with self.widths([7.4] * 4):
            self.assertIsNone(b.bridge_widths(self.lon, np.full(4, 51.0), 9.0))

    def test_structural_margin_constant(self):
        p = np.column_stack((np.arange(4), np.zeros(4), np.ones(4)))
        out = b.structural_deck_widths([7.4] * 4, p, p)
        self.assertEqual(out, [8.3] * 4)

    def test_short_concrete_nub_removed_not_parapet(self):
        fr = types.SimpleNamespace(lon0=7.67, lat0=62.89)
        deck = [{'deck': True, 'n': [[0., 0., 1.], [15., 0., 1.]]}]
        nub = {'concrete': True, 'base': [[0., 1.], [10., 1.]]}
        parapet = {'concrete': True, 'parapet': True, 'base': [[0., 1.], [10., 1.]]}
        stone = {'concrete': False, 'base': [[0., 1.], [10., 1.]]}
        long = {'concrete': True, 'base': [[0., 1.], [30., 1.]]}
        out = b.clean_walls(fr, [nub, parapet, stone, long], deck)
        self.assertEqual(out, [parapet, stone, long])

    def test_grade_core_urban_unchanged(self):
        fr = types.SimpleNamespace(lon0=7.67, lat0=62.89)
        spec = types.SimpleNamespace(kind='paved', urban=True, median=False, mountain=False)
        self.assertEqual(b.wall_width_core(fr, [], spec, 12., 7., 1.), (12., 7.))


if __name__ == '__main__':
    unittest.main()
''',
}

BRANCH = 'feature/norway-nvdb-roads'


def once(s, before, after, label):
    n = s.count(before)
    if n != 1:
        raise ValueError(f'{label}: expected one clean PR anchor, found {n}; no changes made')
    return s.replace(before, after, 1)


def patch_roads(s):
    s = once(s,
        '        if not tp or bs is None:\n'
        '            return None\n'
        '        pts = np.asarray(pts, np.float64)\n'
        '        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)\n'
        '        return taper_widths(bs.width, tp, lo, la)\n',
        '        if bs is None:\n'
        '            return None\n'
        '        pts = np.asarray(pts, np.float64)\n'
        '        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)\n'
        '        # Norwegian NVDB bridge width, including untapered bridge decks.\n'
        '        import nvdb_bridge_fixes_v059\n'
        '        return nvdb_bridge_fixes_v059.bridge_widths(lo, la, bs.width, tp)\n',
        'bridge width function')
    s = once(s,
        '                        ch["ws"] = [round(float(x + dw - spec.width), 3) for x in ws]\n',
        '                        ch["ws"] = [round(float(x + dw - spec.width), 3) for x in ws]\n'
        '                        if getattr(ws, "_nvdb_matched", False):\n'
        '                            import nvdb_bridge_fixes_v059\n'
        '                            ch["ws"] = nvdb_bridge_fixes_v059.structural_deck_widths(ch["ws"], c, pts)\n',
        'bridge context deck slab/collision')
    s = once(s,
        '                                ch["ws"] = [round(float(x + w - spec.width), 3) for x in ws]\n',
        '                                ch["ws"] = [round(float(x + w - spec.width), 3) for x in ws]\n'
        '                                if getattr(ws, "_nvdb_matched", False):\n'
        '                                    import nvdb_bridge_fixes_v059\n'
        '                                    ch["ws"] = nvdb_bridge_fixes_v059.structural_deck_widths(ch["ws"], c, pts)\n',
        'bridge visible deck/collision')
    s = once(s,
        '        width = spec.width if spec.mountain else max(spec.width, carriageway(spec.cls, spec.kind, True, None, spec.oneway))\n'
        '        core = width / 2 + grading.shoulder(spec)\n',
        '        width = spec.width if spec.mountain else max(spec.width, carriageway(spec.cls, spec.kind, True, None, spec.oneway))\n'
        '        core = width / 2 + grading.shoulder(spec)\n'
        '        import nvdb_bridge_fixes_v059\n'
        '        width, core = nvdb_bridge_fixes_v059.wall_width_core(\n'
        '            frame, pts, spec, width, core, grading.shoulder(spec))\n',
        'retaining wall/terrain-width agreement')
    begin = s.index('def retaining_walls(')
    end = s.index('\nTWIN_SHARE =', begin)
    section = s[begin:end]
    section = once(section,
        '    return out\n',
        '    # Conservative removal of short artificial concrete wall nubs at bridge ends.\n'
        '    import nvdb_bridge_fixes_v059\n'
        '    return nvdb_bridge_fixes_v059.clean_walls(frame, out, decks)\n',
        'bridge-end retaining wall cleanup')
    return s[:begin] + section + s[end:]


def git(repo, *args):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if p.returncode:
        raise ValueError(f'Git check failed: git {" ".join(args)}\n{p.stderr.strip()}')
    return p.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', default=Path.cwd(), type=Path, help='Path to cloned fork checkout (default: current directory)')
    ap.add_argument('--check', action='store_true', help='Verify compatibility without writes')
    args = ap.parse_args()
    repo = args.repo.resolve()
    if not (repo / '.git').exists():
        ap.error('This must be your GitHub Desktop cloned fork (.git required).')
    branch = git(repo, 'branch', '--show-current')
    if branch != BRANCH:
        ap.error(f'Please switch to {BRANCH} in GitHub Desktop (current: {branch}).')
    for rel in ('server/roads.py', 'server/nvdb_width_profile_v03.py',
                'server/nvdb_terrain_v0514.py', 'server/nvdb_marking_overlay_v046.py'):
        if not (repo / rel).is_file():
            ap.error(f'Missing required Norwegian PR file: {rel}')
    if git(repo, 'status', '--porcelain', '--', 'server/roads.py'):
        ap.error('server/roads.py has uncommitted edits; commit/stash/revert them before applying.')
    work = {}
    roads = repo / 'server/roads.py'
    raw = roads.read_bytes()
    if b'\r\n' in raw and raw.count(b'\n') != raw.count(b'\r\n'):
        ap.error('roads.py has mixed line endings; refusing to edit it.')
    eol = b'\r\n' if b'\r\n' in raw else b'\n'
    source = raw.replace(b'\r\n', b'\n').decode('utf-8')
    new = patch_roads(source).encode('utf-8')
    work[roads] = new.replace(b'\n', eol) if eol != b'\n' else new
    for rel, body in PAYLOAD_FILES.items():
        dst = repo / rel
        if dst.exists():
            ap.error(f'Won\'t overwrite existing file: {dst}')
        work[dst] = body.encode('utf-8')
    for path, body in work.items():
        if path.suffix == '.py':
            compile(body, str(path), 'exec')
    if args.check:
        print('CHECK PASSED: bridge-only follow-up ready (%d files). No files changed.' % len(work))
        return
    for path, body in work.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        print('Updated', path.relative_to(repo))
    print('\nBridge changes applied. Run tests and inspect your GitHub Desktop diff before committing.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, UnicodeDecodeError, SyntaxError) as exc:
        sys.exit('ERROR: ' + str(exc))
