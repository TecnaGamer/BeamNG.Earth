"""The far state machine in Rust (earthfast.FarState, the Rust port's phase 2b): swap groups against the Python
farfield.swap_groups they replaced (kept here as the reference), over random LOD changes with global tiles and pieces;
the state's Python view."""
import os
import random
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import fast  # noqa: E402


def ref_swap_groups(old, new, polys=None):
    """farfield.swap_groups as it was in Python (try598, before phase 2b)."""
    adds, removes = [k for k in new if k not in old], [k for k in old if k not in new]
    side = {k: 0 for k in adds}
    side.update({k: 1 for k in removes})
    by_node = {ff.tile_of(k).node: k for k in side if not ff.is_special_key(k)}
    parent = {k: k for k in side}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    special = [k for k in side if ff.is_special_key(k)]
    if special and polys is not None:
        keys, ps = [], []
        for k in side:
            if ff.is_special_key(k):
                p = polys(k)
            else:
                x0, y0, x1, y1 = ff.tile_of(k).bbox
                p = (x0, y0, x1, y0, x1, y1, x0, y1)
            if p is not None:
                keys.append(k)
                ps.append([float(v) for v in p])
        for a, b in fast._ef.far_overlap_pairs(ps, [side[k] for k in keys], ff.GLOBAL_MIN_AREA, ff.GLOBAL_OVERLAP_REL):
            ka, kb = keys[a], keys[b]
            if ff.is_special_key(ka) or ff.is_special_key(kb):
                parent[find(ka)] = find(kb)
    for k in side:
        if ff.is_special_key(k):
            continue
        lvl, i, j = ff.tile_of(k).node
        while lvl < ff.HORIZON_MAX_LEVEL:
            lvl, i, j = lvl + 1, i >> 1, j >> 1
            a = by_node.get((lvl, i, j))
            if a is not None and side[a] != side[k]:
                parent[find(k)] = find(a)
    comps = {}
    for k in side:
        comps.setdefault(find(k), []).append(k)
    groups = [(sorted(k for k in ks if side[k] == 0), sorted(k for k in ks if side[k] == 1)) for ks in comps.values()]
    groups.sort(key=lambda g: (g[0] or g[1])[0])
    return groups


def random_quadtree(rng, n_split, root=(4, 0, 0)):
    """Leaves of a random refinement of root (plain keys)."""
    leaves = {root}
    for _ in range(n_split):
        cand = [n for n in leaves if n[0] > -2]
        if not cand:
            break
        lv, i, j = rng.choice(sorted(cand))
        leaves.discard((lv, i, j))
        for di in (0, 1):
            for dj in (0, 1):
                leaves.add((lv - 1, 2 * i + di, 2 * j + dj))
    return {f"{lv}_{i}_{j}" for lv, i, j in leaves}


class SwapGroupsTest(unittest.TestCase):
    def test_plain_against_python(self):
        rng = random.Random(7)
        for _ in range(400):
            a = random_quadtree(rng, rng.randrange(0, 30), (rng.randrange(3, 6), rng.randrange(-3, 3), rng.randrange(-3, 3)))
            b = random_quadtree(rng, rng.randrange(0, 30), (rng.randrange(3, 6), rng.randrange(-3, 3), rng.randrange(-3, 3)))
            old, new = sorted(a), sorted(b)
            rng.shuffle(old)
            rng.shuffle(new)
            self.assertEqual(ff.swap_groups(set(old), {k: 1 for k in new}), ref_swap_groups(set(old), {k: 1 for k in new}))
            self.assertEqual(ff.swap_groups(old, new), ref_swap_groups(old, new))

    def test_global_and_pieces_against_python(self):
        """Special keys (global tiles, pieces) join by their footprints (polys), plain ones by their squares."""
        rng = random.Random(11)
        for _ in range(300):
            polys = {}
            keys_a, keys_b = set(), set()
            for n in range(rng.randrange(1, 8)):
                x0, y0 = rng.uniform(-3000, 3000), rng.uniform(-3000, 3000)
                s = rng.choice([256.0, 512.0, 1024.0, 2048.0])
                q = (x0, y0, x0 + s, y0 + rng.uniform(-5, 5), x0 + s, y0 + s, x0, y0 + s)
                k = f"m{rng.randrange(10, 14)}_{n}_{rng.randrange(0, 5)}"
                polys[k] = q if rng.random() > 0.1 else None
                (keys_a if rng.random() < 0.5 else keys_b).add(k)
            for n in range(rng.randrange(0, 5)):
                lv, i, j = rng.randrange(0, 3), rng.randrange(-6, 6), rng.randrange(-6, 6)
                s = ff.level_size(lv)
                k = f"{lv}_{i}_{j}_c12_{n}_0"
                polys[k] = (i * s, j * s, (i + 1) * s, j * s, (i + 1) * s, (j + 1) * s, i * s, (j + 1) * s)
                (keys_a if rng.random() < 0.5 else keys_b).add(k)
            keys_a |= random_quadtree(rng, rng.randrange(0, 12), (3, rng.randrange(-2, 2), rng.randrange(-2, 2)))
            keys_b |= random_quadtree(rng, rng.randrange(0, 12), (3, rng.randrange(-2, 2), rng.randrange(-2, 2)))
            old, new = sorted(keys_a), sorted(keys_b)
            rng.shuffle(old)
            self.assertEqual(ff.swap_groups(old, new, polys.get), ref_swap_groups(old, new, polys.get))
            self.assertEqual(ff.swap_groups(old, new), ref_swap_groups(old, new))


class StateViewTest(unittest.TestCase):
    def test_python_view(self):
        """What Python reads and sets of the state (Server.in_use, credits, the tests)."""
        far = earthd.msgs.FarState(earthd._far_cfg())
        t, g = ff.tile_of("0_1_2"), ff.GTile(12, 1, 1, 3, (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0), 1.0)
        far.set_desired({t.key: t, g.key: g})
        far.set_shown({(5, "1_0_0"): (0, 0, 512, 512)})
        far.set_sent({t.key: t})
        self.assertEqual(far.shown, {(5, "1_0_0"): (0.0, 0.0, 512.0, 512.0)})
        self.assertEqual(far.sent, {t.key: t})
        self.assertEqual(far.backlog(7), 2)
        self.assertEqual(far.backlog(None), 0)
        self.assertEqual(far.names_in_use(7), {(7, t.key), (earthd.GLOBAL_AID, g.key)})
        far.retire_at((7, "x"), 100.0)
        self.assertEqual(far.in_use(7, 130.0, 60.0), {(5, "1_0_0"), (7, t.key), (earthd.GLOBAL_AID, g.key), (7, "x")})
        self.assertNotIn((7, "x"), far.in_use(7, 170.0, 60.0))
        far.prune_retired(170.0, 60.0)
        self.assertEqual(far.retired, {})
        self.assertTrue(far.is_sent(t.key) and far.is_shown((5, "1_0_0")) and not far.is_inflight((7, t.key)))
        self.assertEqual(far.reveal_keys(7), [t.key])
        far.commit(7, True)
        self.assertEqual((far.sent, far.groups, far.queue, far.qi), ({}, [], [], 0))
        self.assertEqual(far.retired, {(7, t.key): far.retired[(7, t.key)]})

    def test_callbacks_after_the_state(self):
        """Messages go out with the state let go: a send that reads it (the status's backlog) works."""
        far = earthd.msgs.FarState(earthd._far_cfg())
        t = ff.tile_of("0_0_0")
        far.set_desired({t.key: t})

        got = []

        class Conn:
            def send(self, m):
                got.append((m, far.backlog(1)))

        far.set_shown({(1, t.key): t.bbox})
        far.set_sent({t.key: t})
        far.regroup(Conn(), 1, 0.0, 0.0, None, [], 0, None)        # (far_remove sent after the state is let go)
        self.assertEqual(far.sent, {})
        self.assertEqual(got, [({"type": "far_remove", "anchorId": 1, "key": t.key}, 0)])

    def test_rebase_removes_old_key_after_its_new_anchor_swap(self):
        far = earthd.msgs.FarState(earthd._far_cfg())
        tile, outer = ff.tile_of("0_0_0"), ff.tile_of("6_5_4")
        far.set_shown({(1, tile.key): tile.bbox, (1, outer.key): outer.bbox})
        far.commit(1, True)
        far.set_desired({tile.key: tile})
        events = []
        conn = SimpleNamespace(send=events.append, credits=SimpleNamespace(poke=lambda conn: None))
        server = SimpleNamespace(tiles={})
        far.regroup(conn, 2, 0., 0., None, [], 0, None)
        far.dirty = True

        # No workers: step the actual pump on either side of the replacement's send.
        far.pump(conn, server, object(), 2, False, 0, 0, 0, (), False)
        self.assertEqual(events, [{"type": "far_remove", "anchorId": 1, "key": outer.key}])
        self.assertFalse(far.whole())
        far.set_sent({tile.key: tile})
        far.pump(conn, server, object(), 2, False, 0, 0, 0, (), False)
        _, swap, remove = events
        self.assertEqual((swap["type"], swap["anchorId"], swap["add"]), ("far_swap", 2, [tile.key]))
        self.assertEqual(remove, {"type": "far_remove", "anchorId": 1, "key": tile.key})
        self.assertEqual(far.shown, {(2, tile.key): tile.bbox})
        self.assertEqual(set(far.retired), {(1, tile.key), (1, outer.key)})
        self.assertTrue(far.whole())



class ConnSendTest(unittest.TestCase):
    """earthfast.conn_send (Connection.send): nothing, and nothing encoded, for a closing connection, one that lost its
    lease or one whose writer is closing; past the limit the connection is aborted; else the frame protocol.encode
    makes."""

    class Writer:
        def __init__(self, closing=False):
            self.data, self.closing = [], closing

        def write(self, b):
            self.data.append(b)

        def is_closing(self):
            return self.closing

    class Conn:
        def __init__(self, writer, closing=False, lease=None):
            self.writer, self.closing, self.lease, self.aborted = writer, closing, lease, []

        def abort_stream(self, why):
            self.aborted.append(why)

    def test_checks_before_encoding(self):
        import protocol
        bad = {"type": "x", "v": object()}         # (not JSON: encoding it raises)
        w = self.Writer()
        self.assertFalse(fast._ef.conn_send(self.Conn(w, closing=True), bad, 1 << 20))
        other = type("L", (), {"connection": None})()
        self.assertFalse(fast._ef.conn_send(self.Conn(w, lease=other), bad, 1 << 20))
        self.assertFalse(fast._ef.conn_send(self.Conn(self.Writer(closing=True)), bad, 1 << 20))
        self.assertEqual(w.data, [])
        with self.assertRaises(TypeError):
            fast._ef.conn_send(self.Conn(w), bad, 1 << 20)
        msg = {"type": "status", "text": "é", "n": 1.5}
        c = self.Conn(w)
        c.lease = type("L", (), {"connection": c})()
        self.assertTrue(fast._ef.conn_send(c, msg, 1 << 20))
        self.assertEqual(w.data, [protocol.encode(msg)])
        self.assertFalse(fast._ef.conn_send(c, msg, 4))
        self.assertEqual(c.aborted, ["outgoing buffer limit exceeded"])


if __name__ == "__main__":
    unittest.main()
