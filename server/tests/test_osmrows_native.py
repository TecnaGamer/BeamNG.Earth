"""osmtags._rows and tags_and_ways' tag dicts in Rust (earthfast 453, osmrows: the full Rust port) against the Python
they replaced: random decoded row groups (OsmTags._decode's layout: int64 ids and file rows, bool race / bus, text
columns of UTF-8 bytes with int32 offsets and NULL flags, float32 / float64 numbers with NaN, integer columns), any row
selection (repeated, reversed, empty): the same tuples, value for value and type for type; the same tag dicts; text
that is no UTF-8, an infinite integer, a missing column, an index past the table: the same exception class. Outside
the input contract (a column of another dtype, a dict subclass): TypeError.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import osmtags  # noqa: E402
from osmtags import COLUMNS, INT_COLUMNS, TEXT_COLUMNS, _ROW_VAL  # noqa: E402


# ---- the Python versions (before earthfast 453), verbatim ----

def ref_rows(t, idx):
    names = list(COLUMNS)
    out = []
    for i in idx:
        vals = []
        for c in names:
            if c in TEXT_COLUMNS:
                o = t[c + "_off"]
                vals.append(t[c][o[i]:o[i + 1]].tobytes().decode("utf-8") if t[c + "_ok"][i] else None)
            else:
                v = float(t[c][i])
                vals.append(None if v != v else (int(v) if c in INT_COLUMNS else v))
        out.append((int(t["id"][i]), bool(t["race"][i]), bool(t["bus"][i]), int(t["frn"][i]), *vals))
    return out


def ref_tags(rows):
    names = list(COLUMNS)
    return {int(r[0]): {k: v for k, v in zip(names, r[_ROW_VAL:]) if v is not None} for r in rows}


TEXTS = ["", "yes", "no", "both", "Rue de l'Église", "Ŝtrato", "大街", "😀 road", "a" * 40, "None"]


def table(rng, n, bad=False):
    t = {"id": np.array([rng.randrange(1, 2 ** 40) if rng.random() < 0.9 else 7 for _ in range(n)], np.int64),
         "race": np.array([rng.random() < 0.1 for _ in range(n)], bool),
         "bus": np.array([rng.random() < 0.1 for _ in range(n)], bool),
         "frn": np.array([rng.randrange(0, 10 ** 6) for _ in range(n)], np.int64)}
    for c in COLUMNS:
        if c in TEXT_COLUMNS:
            vals = [rng.choice(TEXTS) if rng.random() < 0.6 else None for _ in range(n)]
            enc = [b"" if v is None else v.encode("utf-8") for v in vals]
            if bad and n and rng.random() < 0.1:
                enc[rng.randrange(n)] = b"\xff\xfe"
                vals = [v if v is not None else "" for v in vals]
            t[c + "_ok"] = np.array([v is not None for v in vals], bool)
            t[c + "_off"] = np.concatenate([[0], np.cumsum([len(b) for b in enc])]).astype(np.int32)
            t[c] = np.frombuffer(b"".join(enc), np.uint8)
        else:
            big = [1e300] if c not in osmtags.EXACT32 else []     # (a float32 column's 1e300 is inf: int() of it raises)
            vals = [rng.choice([np.nan, 0.0, 2.0, 3.0, -1.0, 2.5, 60.0] + big) for _ in range(n)]
            if bad and n and rng.random() < 0.05 and c in INT_COLUMNS:
                vals[rng.randrange(n)] = np.inf
            t[c] = np.array(vals, np.float32 if c in osmtags.EXACT32 else np.float64)
    if bad and rng.random() < 0.1:
        t.pop(rng.choice(list(t)))
    return t


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


def same(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return repr(a) == repr(b)
    return a == b


class OsmRowsParityTest(unittest.TestCase):
    def check(self, seed, count, bad):
        rng = random.Random(seed)
        tally = {"ok": 0, "raised": 0, "rows": 0}
        for k in range(count):
            n = rng.randint(0, 12)
            t = table(rng, n, bad)
            idx = np.array([rng.randrange(n) for _ in range(rng.randint(0, 2 * n))] if n else [], np.int64)
            if bad and rng.random() < 0.1:
                idx = np.r_[idx, n + rng.randint(0, 3)].astype(np.int64)
            want = outcome(ref_rows, t, idx)
            got = outcome(osmtags._rows, t, idx)
            self.assertEqual(got[0], want[0], (k, got, want))
            if want[0] == "ok":
                self.assertTrue(same(got[1], want[1]), (k, got[1], want[1]))
                self.assertTrue(same(osmtags._tags_of(got[1]), ref_tags(want[1])), k)
                tally["rows"] += len(want[1])
            else:
                self.assertIs(got[1], want[1], k)
            tally[want[0]] += 1
        return tally

    def test_random_tables(self):
        t = self.check(453, 3000, False)
        self.assertEqual(t["raised"], 0)
        self.assertGreater(t["rows"], 15000)

    def test_damaged_tables_raise_alike(self):
        t = self.check(4531, 3000, True)
        self.assertGreater(t["raised"], 300)

    def test_contract(self):
        t = table(random.Random(1), 4)

        class D(dict):
            pass
        for bad in (lambda t: t.__setitem__("id", t["id"].astype(np.float64)),
                    lambda t: t.__setitem__("lanes", t["lanes"].astype(np.int64)),
                    lambda t: t.__setitem__("name", t["name"].astype(np.int8))):
            u = dict(t)
            bad(u)
            self.assertEqual(outcome(osmtags._rows, u, np.arange(4, dtype=np.int64)), ("raised", TypeError))
        self.assertEqual(outcome(osmtags._rows, D(t), np.arange(4, dtype=np.int64)), ("raised", TypeError))
        for bad in (lambda t: t.__setitem__("id", t["id"].astype(np.int32)),
                    lambda t: t.__setitem__("frn", t["frn"].astype(np.int32))):
            u = dict(t)
            bad(u)
            self.assertEqual(outcome(osmtags._rows, u, np.arange(4, dtype=np.int64)), ("raised", TypeError))
        for idx in ([0.9], [0, 1.0], (np.int64(1),), "01", np.arange(4, dtype=np.int32)):
            self.assertEqual(outcome(osmtags._rows, t, idx), ("raised", TypeError), idx)
        self.assertTrue(same(osmtags._rows(t, [3, 0, -1]), ref_rows(t, [3, 0, -1])))

        class Idx(np.ndarray):
            pass
        self.assertEqual(outcome(osmtags._rows, t, np.arange(4, dtype=np.int64).view(Idx)), ("raised", TypeError))
        # a column of another dtype refused where it is looked up, whether or not a value of it is read
        texts = [k[:-4] for k in t if k.endswith("_off")]
        for c in texts:
            u = dict(t)
            u[c + "_ok"] = np.zeros(len(t["id"]), bool)
            u[c + "_off"] = t[c + "_off"].astype(np.float32)
            self.assertEqual(outcome(osmtags._rows, u, np.arange(4, dtype=np.int64)), ("raised", TypeError), c)
            u = dict(t)
            u[c + "_ok"] = np.zeros(len(t["id"]), np.uint8)
            self.assertEqual(outcome(osmtags._rows, u, np.arange(4, dtype=np.int64)), ("raised", TypeError), c)
        self.assertTrue(same(osmtags._rows(t, []), ref_rows(t, [])))

    def test_noncanonical_bool_bytes(self):
        """A bool column whose bytes are not 0 / 1 (a byte buffer's view): numpy reads any nonzero byte as True."""
        t = table(random.Random(2), 4)
        idx = np.arange(4, dtype=np.int64)
        for c in [k for k, v in t.items() if v.dtype == np.bool_]:
            u = dict(t)
            u[c] = np.frombuffer(bytes([0, 2, 255, 1]), dtype=np.bool_)
            self.assertEqual(outcome(osmtags._rows, u, idx)[0], outcome(ref_rows, u, idx)[0], c)
            if outcome(ref_rows, u, idx)[0] == "ok":
                self.assertTrue(same(osmtags._rows(u, idx), ref_rows(u, idx)), c)


class VersionSkewTest(unittest.TestCase):
    def test_a_452_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 453)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=452)):
            with self.assertRaises(fast.NativeMissing):
                osmtags._rows(table(random.Random(1), 2), np.arange(2, dtype=np.int64))


if __name__ == "__main__":
    unittest.main()
