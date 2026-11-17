"""Lane flow's per-layer rows and lists (earthfast 382, roadnet::laneflow: the road revamp's R2a) against the Python
they replaced, bit for bit: roads._lane_flow_rows with _ramp_candidates (one-way signs from numeric and text rule
values, generated counts, links, the classes' default lanes, the ramp lane rule's motorway links with and without a
mapped width, turn:lanes columns or lane tag parts) and _lane_flow_local's lists of lane_flow_cells' counts.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import mapgeom  # noqa: E402
import railtags  # noqa: E402
import roadlanes  # noqa: E402
import roads  # noqa: E402
from roads import LANE_FLOW_CLASSES, Spec, carriageway, lane_layout  # noqa: E402

_TURN_LANES = ("turn_lanes", "turn_lanes_forward", "turn_lanes_backward", "turn_lanes_both_ways")
_DFLT = {}


# ---- the Python versions (before earthfast 382), verbatim (module globals by name) ----

def ref_ramp_candidates(f, idx, cls, lanes, link):
    out = np.zeros(len(idx))
    if not roads.RAMP_LANE_RULE:
        return out
    sel = (cls[idx] == "motorway") & link & (np.nan_to_num(lanes, nan=0.0) >= 3)
    if not sel.any():
        return out
    n = len(f)
    has_w, _ = roads._rule_cols(roads.width_table(f), n)
    osm_w = roads._osm_widths(f, None, None)
    parts = f.attrs.get(railtags.LANE_PARTS)
    turn = [f.attrs.get("osm_" + k) for k in _TURN_LANES]
    for k in np.flatnonzero(sel).tolist():
        i = int(idx[k])
        if has_w[i] or np.isfinite(osm_w[i]):
            continue
        if any(c is not None and c[i] not in (None, "") for c in turn):
            continue
        if parts is not None and parts[i] and any(t and any(str(key).startswith("turn:lanes") for key in t)
                                                  for _, _, _, t in parts[i]):
            continue
        out[k] = 1.0
    return out


def ref_lane_flow_rows(f):
    n = len(f)
    po = np.asarray(f.part_off, np.int64)
    starts = np.asarray(f.coord_off, np.int64)[np.asarray(f.ring_off, np.int64)[po[:n]]]
    ends = np.asarray(f.coord_off, np.int64)[np.asarray(f.ring_off, np.int64)[po[1:n + 1]]]
    npts = np.ascontiguousarray(ends - starts, np.int64)
    pts = np.zeros((n, 4))
    some = npts > 0
    if some.any():
        ll = np.asarray(f.lonlat, np.float64)
        pts[some, :2] = ll[starts[some]]
        pts[some, 2:] = ll[ends[some] - 1]
    attrs = np.zeros((n, 5))
    attrs[:, 1] = np.nan
    attrs[:, 3] = np.nan
    cls_a, sub_a = f.attrs.get("class"), f.attrs.get("subtype")
    counts = roadlanes.generated_counts(f)
    table = f.rules.get("oneway")
    if cls_a is not None and n and table is not None and len(table.feature):
        cls = np.asarray(cls_a).astype(str)
        sel = np.isin(cls, sorted(LANE_FLOW_CLASSES))
        if sub_a is not None:
            sel &= np.asarray(sub_a).astype(str) == "road"
        maj = table.majors(n)
        sel &= maj >= 0
        idx = np.flatnonzero(sel)
        if len(idx):
            v = np.asarray(table.value)[maj[idx]].astype(np.float64)
            attrs[idx, 0] = np.where(v < 0, -1.0, 1.0)
            attrs[idx, 1] = [np.nan if counts[i] is None else float(int(counts[i])) for i in idx.tolist()]
            attrs[idx, 2] = np.where(roadlanes.links(f)[idx], 1.0, 0.0)
            for c in np.unique(cls[idx]).tolist():
                if c not in _DFLT:
                    _DFLT[c] = lane_layout(Spec(carriageway(c, "paved", False, None, True, None), "paved",
                                                False, c, True))[1]
                attrs[idx[cls[idx] == c], 3] = float(_DFLT[c])
            attrs[idx, 4] = ref_ramp_candidates(f, idx, cls, attrs[idx, 1], attrs[idx, 2] > 0.5)
    return pts, npts, attrs


def ref_lists(got, ramp, attrs):
    got, ramp = np.asarray(got), np.asarray(ramp)
    out = [None] * len(got)
    for i in np.flatnonzero(np.isfinite(got) & ~(attrs[:, 2] > 0.5)).tolist():
        out[i] = int(got[i])
    fixed = [None] * len(got)
    for i in np.flatnonzero(np.isfinite(ramp)).tolist():
        fixed[i] = int(ramp[i])
    return out, fixed


CLASSES = ["motorway", "motorway", "trunk", "primary", "secondary", "tertiary", "residential", "Motorway", ""]


def random_layer(rng):
    n = rng.randint(0, 10)
    po, ro, co, ll = [0], [0], [0], []
    for _ in range(n):
        k = rng.choice([0, 1, 2, 5])
        lon, lat = rng.uniform(-10, 10), rng.uniform(40, 50)
        for _ in range(k):
            ll.append((lon, lat))
            lon += rng.uniform(-1e-3, 1e-3)
            lat += rng.uniform(-1e-3, 1e-3)
        co.append(len(ll))
        ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    attrs = {}
    if rng.random() < 0.95:
        attrs["class"] = np.array([rng.choice(CLASSES) for _ in range(n)], object if rng.random() < 0.5 else str)
    if rng.random() < 0.7:
        attrs["subtype"] = np.array([rng.choice(["road", "road", "rail"]) for _ in range(n)], str)
    if rng.random() < 0.6:
        attrs["osm_lanes"] = np.array([rng.choice([np.nan, 1.0, 2.0, 3.0, 4.0]) for _ in range(n)])
    if rng.random() < 0.5:
        attrs["osm_width"] = np.array([rng.choice([None, "", "12", 7.5]) for _ in range(n)] + [None], object)[:n]
    for k in _TURN_LANES:
        if rng.random() < 0.3:
            attrs["osm_" + k] = np.array([rng.choice([None, "", "left|through", np.nan]) for _ in range(n)] + [None],
                                         object)[:n]
    if rng.random() < 0.5:
        attrs[railtags.LANE_PARTS] = np.array(
            [rng.choice([None, [], [(0.0, 1.0, "1", None)], [(0.0, 0.5, "1", {"lanes": "3"}),
                                                              (0.5, 1.0, "2", {"turn:lanes": "left|"})],
                         [(0.0, 1.0, "3", {"turn:lanes:forward": "x"})]]) for _ in range(n)] + [None], object)[:n]
    rules = {}

    def table(vals):
        m = rng.randint(0, 2 * n)
        return mapgeom.RuleTable(np.array([rng.randrange(0, n + 1) for _ in range(m)], np.int32),
                                 np.array([rng.choice(vals) for _ in range(m)]),
                                 np.zeros(m, np.float32), np.array([rng.random() for _ in range(m)], np.float32))
    if rng.random() < 0.9:
        rules["oneway"] = table(rng.choice([[1.0, -1.0, 0.0], ["1", "-1", " 1 "], [1, -1]]))
    if rng.random() < 0.5:
        rules["subclass"] = table(["link", "road"])
    if rng.random() < 0.3:
        rules["width"] = table([7.0, 12.5])
    return mapgeom.Features("roads", np.array([f"r{i:03d}" for i in range(n)]), np.full(n, 2, np.uint8),
                            np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                            np.array(ll, np.float64).reshape(-1, 2), attrs=attrs, rules=rules)


class LaneFlowParityTest(unittest.TestCase):
    def test_rows(self):
        rng = random.Random(380)
        for k in range(1500):
            f = random_layer(rng)
            want = ref_lane_flow_rows(f)
            f.__dict__.pop("_lane_flow_rows", None)
            got = roads._lane_flow_rows(f)
            for g, w in zip(got, want):
                self.assertEqual(g.dtype, w.dtype, k)
                self.assertEqual(g.shape, w.shape, k)
                self.assertEqual(g.tobytes(), w.tobytes(), (k, g, w))

    def test_ramp_rule(self):
        """Motorway links of 3+ mapped lanes, each feature's one-way and link rows its own: the ramp lane rule's every
        test (a width rule, an OSM width, each turn:lanes column, lane tag parts with and without turn:lanes keys)."""
        rng = random.Random(3802)
        cands = 0
        for k in range(800):
            f = random_layer(rng)
            n = len(f)
            f.attrs["class"] = np.array(["motorway"] * n, object)
            f.attrs.pop("subtype", None)
            f.attrs["osm_lanes"] = np.array([rng.choice([3.0, 4.0, 2.0]) for _ in range(n)])
            one = lambda v: mapgeom.RuleTable(np.arange(n, dtype=np.int32), np.array([v] * n),  # noqa: E731
                                              np.zeros(n, np.float32), np.ones(n, np.float32))
            f.rules["oneway"] = one(1.0)
            f.rules["subclass"] = one("link")
            want = ref_lane_flow_rows(f)
            cands += int((want[2][:, 4] > 0).sum())
            f.__dict__.pop("_lane_flow_rows", None)
            f.__dict__.pop("_generated_lanes", None)
            got = roads._lane_flow_rows(f)
            for g, w in zip(got, want):
                self.assertEqual(g.tobytes(), w.tobytes(), (k, g, w))
        self.assertGreater(cands, 100)

    def outcome(self, fn, f):
        """("ok", the rows) or ("raised", the exception's class); a Rust panic (PanicException, a BaseException
        `except Exception` does not catch) fails the test."""
        f.__dict__.pop("_lane_flow_rows", None)
        try:
            return "ok", fn(f)
        except Exception as e:      # noqa: BLE001 (the class is the point)
            return "raised", type(e)
        except BaseException as e:  # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")

    def test_columns_of_other_lengths_raise_as_python_did(self):
        """Attribute columns of other lengths than the layer (Features.from_arrays does not check them): class,
        subtype (one value broadcasts, as numpy did), osm_width (a ramp candidate past it), the lane tag parts and
        turn columns: the same rows or the same exception class as the Python, never a panic (Codex review of
        3eba68d9: a two-value subtype column on a three-road layer panicked)."""
        rng = random.Random(3803)
        cases = 0
        for k in range(300):
            f = random_layer(rng)
            n = len(f)
            if n == 0:
                continue
            f.attrs["class"] = np.array(["motorway"] * n, object)
            f.attrs["osm_lanes"] = np.array([rng.choice([3.0, 4.0]) for _ in range(n)])
            one = lambda v: mapgeom.RuleTable(np.arange(n, dtype=np.int32), np.array([v] * n),  # noqa: E731
                                              np.zeros(n, np.float32), np.ones(n, np.float32))
            f.rules["oneway"] = one(1.0)
            f.rules["subclass"] = one("link")
            f.rules.pop("width", None)
            what = rng.choice(["class", "subtype", "osm_width", "turn", "parts", "none"])
            m = rng.choice([0, 1, max(n - 1, 0), n + 1, n + 3])
            if what == "class":
                f.attrs["class"] = np.array(["motorway"] * m, object)
            elif what == "subtype":
                f.attrs["subtype"] = np.array([rng.choice(["road", "rail"]) for _ in range(m)], str)
            elif what == "osm_width":
                f.attrs["osm_width"] = np.array([rng.choice([None, "", "x"]) for _ in range(m)] + [None], object)[:m]
            elif what == "turn":
                f.attrs["osm_turn_lanes"] = np.array([None] * m + [None], object)[:m]
            elif what == "parts":
                f.attrs[railtags.LANE_PARTS] = np.array([None] * m + [None], object)[:m]
            want = self.outcome(ref_lane_flow_rows, f)
            got = self.outcome(roads._lane_flow_rows, f)
            self.assertEqual(got[0], want[0], (k, what, n, m))
            if want[0] == "raised":
                self.assertIs(got[1], want[1], (k, what, n, m))
            else:
                for g, w in zip(got[1], want[1]):
                    self.assertEqual(g.tobytes(), w.tobytes(), (k, what, n, m))
            cases += 1
        self.assertGreater(cases, 200)

    def test_lists(self):
        rng = random.Random(3801)
        for k in range(500):
            n = rng.randint(0, 10)
            got = np.array([rng.choice([np.nan, 2.0, 3.9, 4.0]) for _ in range(n)])
            ramp = np.array([rng.choice([np.nan, 1.0, 2.0]) for _ in range(n)])
            attrs = np.zeros((n, 5))
            attrs[:, 2] = [rng.choice([0.0, 1.0]) for _ in range(n)]
            self.assertEqual(fast._ef.lane_flow_lists(got, ramp, attrs), ref_lists(got, ramp, attrs))


if __name__ == "__main__":
    unittest.main()
