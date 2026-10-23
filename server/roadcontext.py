"""Source-based road neighbourhoods, independent of the cell that asks for a curve.

Road features are whole polylines repeated in each cell they touch. Fitting those copies
against their own cell's neighbours gives different curves at a cell boundary. Use the
cells around the complete source line instead, also retaining the approach to a ramp.
The bounded cache is shared by rendering, grading, surface masks and navigation.
"""
from collections import OrderedDict
import threading

import numpy as np

import mapcells
import mapgeom


MARGIN_M = 320.                 # full auxiliary lane and taper, before clipping
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

    def feature(self, feats, i):
        """(complete neighbourhood, source row) for this feature, regardless of caller/load order.

        Only source vertices need context: a long two-node track does not fetch every cell
        in its bounding box. Failed reads propagate for the caller to retry; incomplete
        neighbourhoods must not become permanent, differently fitted roads.
        """
        rings = [r[:, :2] for p in feats.parts(i) for r in p if len(r)]
        if not rings:
            source = merge([feats])
            return source, source.index(str(feats.ids[i]))
        points = np.concatenate(rings)
        pad = MARGIN_M / 110540.
        lon_pad = pad / max(.01, float(np.cos(np.radians(points[:, 1])).min()))
        keys = tuple(mapcells.cells_near_points(points[:, 1], points[:, 0], pad, lon_pad))
        with self.lock:
            got = self.cache.get(keys)
            if got is None:
                context = merge([self.cell(key) for key in keys])
                if context.index(str(feats.ids[i])) < 0:
                    raise ValueError(f'road context is missing source feature {feats.ids[i]}')
                # Reserve space for fitted curves and derived layouts as well as source arrays.
                size = 3 * sum(a.nbytes for a in context.to_arrays('').values())
                got = context, size
                self.cache[keys] = got
                self.bytes += size
                while (len(self.cache) > MAX_CONTEXTS or self.bytes > MAX_BYTES) and len(self.cache) > 1:
                    _, (_, old_size) = self.cache.popitem(last=False)
                    self.bytes -= old_size
            else:
                self.cache.move_to_end(keys)
            return got[0], got[0].index(str(feats.ids[i]))
