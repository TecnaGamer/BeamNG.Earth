"""The Rust port's phase 2a (native/earthfast/src/host/pyjson.rs, msgs.rs, pyjob.rs): protocol.encode's frames made in
Rust are byte for byte Python's json.dumps (separators=(",", ":"), ensure_ascii=False) over a fuzz corpus; the messages
built in Rust (far_tile, far_swap, far_remove, far_done, status, load_progress) are byte for byte, key for key, what the
Python server built (its builders kept here as the reference); PyJobs behaves as a concurrent.futures executor."""
import collections
import enum
import json
import math
import os
import random
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

import protocol  # noqa: E402

ef = protocol.ef
HEADER = struct.Struct("<I")


def py_encode(msg):
    """protocol.encode as Python made it (before phase 2a)."""
    raw = [k for k, v in msg.items() if type(v) is protocol.RawJson]
    if raw:
        rest = {k: v for k, v in msg.items() if type(v) is not protocol.RawJson}
        head = json.dumps(rest, separators=(",", ":"), ensure_ascii=False)
        body = ",".join(json.dumps(k, ensure_ascii=False) + ":" + msg[k].text for k in raw)
        payload = (head[:-1] + ("," if rest else "") + body + "}").encode("utf-8")
    else:
        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return HEADER.pack(len(payload)) + payload


def rand_float(r):
    k = r.random()
    if k < 0.3:
        while True:
            x = struct.unpack("<d", r.getrandbits(64).to_bytes(8, "little"))[0]
            if not math.isnan(x) or r.random() < 0.01:
                return x
    if k < 0.6:
        return round(r.uniform(-1e5, 1e5), r.randint(0, 6))
    if k < 0.8:
        return r.choice([0.0, -0.0, 1.0, -1.0, 1e16, 1e15, 1e-4, 1e-5, 9.999999999999999e15, 1e17, 0.1, 5e-324,
                         1.7976931348623157e308, math.inf, -math.inf, math.nan, 123456789.0, 2.5, 0.125])
    return r.uniform(-1, 1) * 10 ** r.randint(-30, 30)


def rand_str(r):
    alphabet = "abcXYZ019 _-/.:\"\\\n\r\t\b\f\x00\x01\x1f\x7f\x80éßπ漢字  😀"
    return "".join(r.choice(alphabet) for _ in range(r.randint(0, 12)))


class Sub(float):
    pass


class Num(enum.IntEnum):
    A = 7


def rand_value(r, depth=0):
    k = r.random()
    if depth > 3 or k < 0.55:
        return r.choice([
            lambda: None, lambda: True, lambda: False, lambda: r.randint(-10 ** 6, 10 ** 6),
            lambda: r.randint(-2 ** 70, 2 ** 70), lambda: rand_float(r), lambda: rand_str(r),
            lambda: np.float64(rand_float(r)), lambda: Sub(rand_float(r)), lambda: Num.A,
        ])()
    if k < 0.7:
        return [rand_value(r, depth + 1) for _ in range(r.randint(0, 5))]
    if k < 0.8:
        return tuple(rand_value(r, depth + 1) for _ in range(r.randint(0, 4)))
    if k < 0.97:
        return {rand_str(r): rand_value(r, depth + 1) for _ in range(r.randint(0, 5))}
    if k < 0.985:
        return collections.OrderedDict((rand_str(r), rand_value(r, depth + 1)) for _ in range(3))
    return {r.choice([1, 2.5, True, None, "k"]): rand_value(r, depth + 1) for _ in range(3)}


class PyJsonConformance(unittest.TestCase):
    def test_fuzz_corpus_is_json_dumps(self):
        r = random.Random(528)
        for i in range(20000):
            msg = {"type": rand_str(r)}
            for _ in range(r.randint(0, 6)):
                msg[rand_str(r)] = rand_value(r)
            self.assertEqual(protocol.encode(msg), py_encode(msg), msg)
            j = rand_value(r)
            self.assertEqual(ef.pyjson_dumps(j), json.dumps(j, separators=(",", ":"), ensure_ascii=False), j)

    def test_floats_are_python_repr(self):
        r = random.Random(1)
        xs = [rand_float(r) for _ in range(200000)]
        xs += [float(f"{m}e{e}") for m in (1, 15, 9999999999999999, 123) for e in range(-330, 310)]
        for x in xs:
            self.assertEqual(ef.pyjson_dumps(x), json.dumps(x), x)
        self.assertEqual(ef.pyjson_dumps_j(xs[:5000]), json.dumps(xs[:5000], separators=(",", ":")))

    def test_round_is_python_round(self):
        r = random.Random(2)
        xs = [rand_float(r) for _ in range(100000)]
        xs += [k / 8 for k in range(-2000, 2000)] + [k / 1000 + 0.0005 for k in range(-3000, 3000)]
        for x in xs:
            for n in (1, 2, 3):
                want = round(x, n)
                got = ef.pyjson_round(x, n)
                self.assertTrue(got == want or (math.isnan(got) and math.isnan(want)), (x, n, got, want))
                self.assertEqual(math.copysign(1, got), math.copysign(1, want), (x, n))

    def test_raw_json_spliced_after_the_rest(self):
        raw = protocol.RawJson('{"a": [1, 2.50]}')
        for msg in ({"type": "t", "data": raw, "x": 1}, {"data": raw}, {"type": "t", 1: 2, "data": raw},
                    {"type": "é\n", "d": raw, "e": protocol.RawJson("[]"), "f": None}):
            self.assertEqual(protocol.encode(msg), py_encode(msg))

    def test_container_subclasses_as_python_reads_them(self):
        # (Codex port2a r2: a dict subclass's items() order with RawJson; a list / tuple subclass's own iteration)
        class Rev(list):
            def __iter__(self):
                return iter([list.__getitem__(self, i) for i in range(len(self) - 1, -1, -1)])
        od = collections.OrderedDict([("type", "t"), ("a", 1), ("raw", protocol.RawJson("[1]")), ("b", 2)])
        od.move_to_end("a")
        od2 = collections.OrderedDict([("type", "t"), ("a", 1), ("b", 2)])
        od2.move_to_end("a")
        Pt = collections.namedtuple("Pt", "x y")
        for msg in (od, od2, {"type": "t", "v": Rev([1, 2, 3])}, {"type": "t", "p": Pt(1.5, 2)}):
            self.assertEqual(protocol.encode(msg), py_encode(msg), msg)
        self.assertIn(b"[3,2,1]", protocol.encode({"type": "t", "v": Rev([1, 2, 3])}))

    def test_errors_are_python_errors(self):
        a = []
        a.append(a)
        for bad, exc in (({"type": "t", "s": {1, 2}}, TypeError), ({"type": "t", "a": a}, ValueError),
                         ({"type": "t", "s": "\ud800"}, UnicodeEncodeError), ({"type": "t", "n": np.int64(3)}, TypeError),
                         ({"type": "t", "x": {"r": protocol.RawJson("1")}}, TypeError)):
            with self.assertRaises(exc):
                py_encode(bad)
            with self.assertRaises(exc):
                protocol.encode(bad)

    def test_deep_nesting(self):
        v = 1
        for i in range(200):
            v = [v] if i % 2 else {"k": v}
        self.assertEqual(protocol.encode({"type": "t", "v": v}), py_encode({"type": "t", "v": v}))


# --- the builders as the Python server had them (earthd.Connection, before phase 2a) -----------------------------

class _NS:
    def __init__(self, **k):
        self.__dict__.update(k)


def ref_far_tile(aid, key, level, shape, info, kind, quad_poly, serial, texture, tex):
    m = {"type": "far_tile", "anchorId": aid, "key": key, "level": level, "shape": shape,
         "pos": [round(v, 3) for v in info.pos], "err": round(info.err, 3), "skirt": round(info.skirt, 2),
         "state": "group"}
    if info.sink is not None:
        m["sink"] = round(info.sink, 2)
    if kind == "g":
        m["frame"] = [info.frame[0], info.frame[1]]
        m["pos"] = [0, 0, round(info.pos[2], 3)]
        m["poly"] = [round(v, 2) for v in quad_poly]
    elif kind == "p":
        m["poly"] = [round(v, 2) for v in quad_poly]
    if serial is not None:
        m["slotSerial"] = serial
    if info.material is not None:
        m["material"] = info.material
        m["texture"] = texture
        m["detail"] = True
        if tex is not None:
            m["tex"] = tex
    return m


def ref_status(text, note, n, radius):
    if note:
        text = f"{text} | {note}" if text else note
    m = {"type": "status", "text": text, "backlog": n}
    if radius is not None:
        m["radius"] = round(radius)
    return m


class Builders(unittest.TestCase):
    def setUp(self):
        self._err = np.seterr(all="ignore")    # (numpy's round of a huge float64 overflows, as it did in Python)

    def tearDown(self):
        np.seterr(**self._err)

    def same(self, got, want):
        self.assertEqual(list(got), list(want))
        self.assertEqual(protocol.encode(got), protocol.encode(want))
        self.assertEqual(protocol.encode(got), py_encode(want))

    def test_far_tile(self):
        r = random.Random(3)
        for i in range(20000):
            num = lambda: r.choice([rand_float(r), r.uniform(-5e3, 5e3), r.randint(-9, 9), np.float64(r.uniform(-9, 9))])
            info = _NS(pos=r.choice([[num(), num(), num()], (num(), num(), num()), np.array([num(), num(), num()], np.float64)]),
                       err=num(), skirt=num(), sink=r.choice([None, num()]), frame=[num(), num()],
                       material=r.choice([None, "bngEarthFar_ab12_00007"]))
            kind = r.choice(["g", "p", "t"])
            qp = [num() for _ in range(r.choice([4, 8, 10]))] if kind != "t" else None
            serial = r.choice([None, r.randint(0, 10 ** 6)])
            tex = r.choice([None, 256, 1024]) if info.material is not None else None
            key = rand_str(r)
            want = ref_far_tile(r.randint(0, 99), key, r.randint(0, 9), "/temp/x.cdae", info, kind, qp, serial,
                                "/temp/t.dds", tex)
            got = ef.msg_far_tile(want["anchorId"], key, want["level"], "/temp/x.cdae", info.pos, info.err, info.skirt,
                                  info.sink, info.frame if kind == "g" else None, qp, serial, info.material,
                                  "/temp/t.dds", tex)
            self.same(got, want)

    def test_far_swap_remove_done(self):
        r = random.Random(4)
        for i in range(5000):
            adds, removes = [rand_str(r) for _ in range(3)], [rand_str(r) for _ in range(r.randint(0, 4))]
            d_in, d_out = rand_float(r), r.choice([rand_float(r), np.float64(rand_float(r)), r.randint(0, 3)])
            want = {"type": "far_swap", "anchorId": 3, "id": i, "add": adds, "remove": removes,
                    "dIn": round(d_in, 2), "dOut": round(d_out, 2)}
            got = ef.msg_far_swap(3, i, adds, removes, d_in, d_out)
            self.same(got, want)
            self.assertIs(got["add"], adds)
        self.same(ef.msg_far_remove(7, "3_1_2"), {"type": "far_remove", "anchorId": 7, "key": "3_1_2"})
        self.same(ef.msg_far_done(7, 812), {"type": "far_done", "anchorId": 7, "count": 812})

    def test_status_and_load_progress(self):
        r = random.Random(5)
        for i in range(5000):
            text = r.choice([None, "", "Far field ready (12 tiles)", rand_str(r), 5])
            note = r.choice([None, "", "offline", rand_str(r)])
            radius = r.choice([None, rand_float(r) if r.random() < 0.2 else r.uniform(0, 1e5), 2.5, 3.5,
                               np.float64(r.uniform(0, 1e5)), 4000])
            if isinstance(radius, float) and not math.isfinite(radius):
                radius = 1.5
            want = ref_status(text, note, i, radius)
            self.same(ef.msg_status(text, note, i, radius), want)
            net = (r.randint(0, 9), round(r.uniform(0, 99), 1), round(r.uniform(0, 9), 2), rand_str(r), r.random() < .5)
            counts = ((1, 1), (r.randint(0, 9), 9), (0, 0), (3, 4), net)
            want = {"type": "load_progress", "anchorId": i, "ground": list(counts[0]), "roads": list(counts[1]),
                    "trees": list(counts[2]), "far": list(counts[3]), "net": list(net)}
            self.same(ef.msg_load_progress(i, *counts), want)

    def test_fields_are_the_protocol_tables(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "protocol_table.json"), encoding="utf-8") as f:
            table = json.load(f)["server_to_mod"]
        for kind, fields in ef.msg_fields():
            self.assertEqual(fields[0], "type")
            self.assertEqual(set(fields[1:]), set(table[kind]["fields"]), kind)


class PyJobsExecutor(unittest.TestCase):
    def test_results_errors_order_and_threads(self):
        jobs = ef.PyJobs(2, "pyjobtest")
        try:
            futs = [jobs.submit(lambda a, b: (a, b, threading.current_thread().name), i, i * 2) for i in range(50)]
            res = [f.result(10) for f in futs]
            self.assertEqual([r[:2] for r in res], [(i, i * 2) for i in range(50)])
            self.assertLessEqual(jobs.threads(), 2)

            def boom():
                raise KeyError("x")
            with self.assertRaises(KeyError):
                jobs.submit(boom).result(10)
            gate = threading.Event()
            first = [jobs.submit(gate.wait, 10) for _ in range(2)]
            late = jobs.submit(lambda: 1)
            self.assertTrue(late.cancel())
            gate.set()
            self.assertTrue(all(f.result(10) for f in first))
            self.assertTrue(late.cancelled())
            self.assertEqual(jobs.submit(lambda: 5).result(10), 5)
        finally:
            jobs.shutdown()
        with self.assertRaises(RuntimeError):
            jobs.submit(lambda: 1)

    def test_shutdown_waits_and_cancels(self):
        jobs = ef.PyJobs(1, "pyjobtest")
        futs = [jobs.submit(time.sleep, 0.05) for _ in range(3)]
        jobs.shutdown(wait=True, cancel_futures=True)
        self.assertTrue(futs[0].done())
        self.assertTrue(all(f.done() for f in futs))


if __name__ == "__main__":
    unittest.main()
