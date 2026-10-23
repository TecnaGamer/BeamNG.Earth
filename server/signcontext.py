"""Stable, bounded source neighbourhoods for sign placement; road generation is unchanged."""
from collections import OrderedDict
import threading

import numpy as np

import mapcells
import mapgeom


MARGIN_M = 320.                 # neighbouring pavement around the full sign approach
MAX_CONTEXTS = 12
MAX_BYTES = 64 * 1024 * 1024


def merge(cells):
    """Whole source features and their rules, once by ID, in deterministic cell order."""
    rows = {}
    for feats in cells:
        if feats is not None:
            for i, fid in enumerate(feats.ids):
                rows.setdefault(str(fid), (feats, i))
    ordered = [rows[fid] for fid in sorted(rows)]
    out = mapgeom.build('roads', np.array(sorted(rows), dtype=str),
                        [(int(f.kind[i]), f.parts(i)) for f, i in ordered])
    for name in sorted({k for f, _ in ordered for k in f.attrs}):
        dtype = np.result_type(*[f.attrs[name].dtype for f, _ in ordered if name in f.attrs])
        missing = '' if dtype.kind in 'US' else (np.nan if dtype.kind in 'fc' else 0)
        out.attrs[name] = np.array([f.attrs[name][i] if name in f.attrs else missing
                                    for f, i in ordered], dtype=dtype)
    for name in sorted({k for f, _ in ordered for k in f.rules}):
        fi, values, starts, ends = [], [], [], []
        dtype = np.result_type(*[f.rules[name].value.dtype for f, _ in ordered if name in f.rules])
        for j, (f, i) in enumerate(ordered):
            table = f.rules.get(name)
            if table is None:
                continue
            r = table.rows(i)
            fi.extend([j] * len(r))
            values.extend(table.value[r])
            starts.extend(table.start[r])
            ends.extend(table.end[r])
        out.rules[name] = mapgeom.RuleTable(np.array(fi, np.int32), np.array(values, dtype=dtype),
                                          np.array(starts, np.float32), np.array(ends, np.float32))
    return out


class Context:
    def __init__(self, cell):
        self.cell = cell
        self.cache = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()

    def around(self, points):
        """One stable neighbourhood for a whole source line or an OSM exit approach."""
        points = np.asarray(points, float)
        if not len(points):
            return mapgeom.empty('roads')
        pad = MARGIN_M / 110540.
        lon_pad = pad / max(.01, float(np.cos(np.radians(points[:, 1])).min()))
        keys = tuple(mapcells.cells_near_points(points[:, 1], points[:, 0], pad, lon_pad))
        with self.lock:
            got = self.cache.get(keys)
            if got is None:
                context = merge([self.cell(key) for key in keys])
                # Reserve space for derived pavement outlines as well as source arrays.
                size = 3 * sum(a.nbytes for a in context.to_arrays('').values())
                got = context, size
                self.cache[keys] = got
                self.bytes += size
                while (len(self.cache) > MAX_CONTEXTS or self.bytes > MAX_BYTES) and len(self.cache) > 1:
                    _, (_, old_size) = self.cache.popitem(last=False)
                    self.bytes -= old_size
            else:
                self.cache.move_to_end(keys)
            return got[0]
