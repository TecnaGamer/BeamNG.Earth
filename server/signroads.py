"""Sign-only pavement outlines from the current road renderer's widths and centre lines.

These helpers never alter a road, lane count, terrain mask or bridge profile. A sign's
complete source neighbourhood is reused across tiles so its position has one owner.
"""
from collections import namedtuple

import numpy as np

import mapgeom
import roads
import signcontext

Spec = namedtuple('SignRoad', 'width cls oneway dir link', defaults=(False,))


def spec(value, link=False):
    return Spec(value.width, value.cls, value.oneway, value.dir, link)


def cross(points):
    return (points[:, 3:5] if points.shape[1] == 5 else
            roads.offset_polyline(points, 1.)[:, :2] - points[:, :2])


def pavement(value, points, frame=None, left_hand=False):
    return points, value.width


class Context(signcontext.Context):
    def __init__(self, cell, urban=None, widths=None):
        super().__init__(cell)
        self.urban, self.widths = urban, widths

    def pavements(self, frame, points, left_hand=False):
        feats = self.around(points)
        key = (frame.lat0, frame.lon0, frame.h0, bool(left_hand))
        with self.lock:
            memo = feats.__dict__.setdefault('_sign_pavements', {})
            if key not in memo:
                rows = []
                specs = roads.road_specs(feats, self.urban, self.widths, left_hand)
                for i, value in enumerate(specs):
                    if value is None:
                        continue
                    link = roads._rule_value(feats.rules.get('subclass'), i) == 'link'
                    value = spec(value, link)
                    for ring in roads.ground_parts(feats, i):
                        if len(ring) < 2:
                            continue
                        e, n = mapgeom.enu_xy(feats, frame, ring)
                        line = np.c_[e, n, np.zeros(len(ring))]
                        rows.append((str(feats.ids[i]), value, line, value.width))
                # Anchors change while the bounded feature cache can survive a teleport.
                if len(memo) >= 4:
                    memo.pop(next(iter(memo)))
                memo[key] = rows
            return memo[key]
