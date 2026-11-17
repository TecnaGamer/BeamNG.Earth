"""earthfast's host (the Rust port's phase 1: the game connection in Rust, protocol.HostServer / HostStream) through
real loopback connections: framing and its errors, the write queue's drain / close / abort as asyncio's, several
connections, the listener's close, frame order and the lane-wait numbers, and I/O that keeps moving while the GIL is
held. No game; the asyncio path is the default (BNGE_HOST off) and these tests use the host directly."""
import asyncio
import errno
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast
import protocol

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= protocol.HOST_VERSION


def frame(payload):
    return struct.pack("<I", len(payload)) + payload


def recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        b = s.recv(min(1 << 20, n - len(buf)))
        if not b:
            break
        buf += b
    return bytes(buf)


def recv_frame(s):
    head = recv_exact(s, 4)
    if len(head) < 4:
        return None
    return recv_exact(s, struct.unpack("<I", head)[0])


def recv_all(s):
    buf = bytearray()
    while True:
        b = s.recv(1 << 20)
        if not b:
            return bytes(buf)
        buf += b


def host_threads():
    """Linux: this process's threads named by the host (bnge-io, bnge-watch, bnge-rd-N, bnge-wr-N)."""
    out = []
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/comm") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name.startswith("bnge-"):
            out.append(name)
    return out


@unittest.skipUnless(HAVE, "needs earthfast with the host (protocol.HOST_VERSION)")
class HostTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.servers = []
        self.socks = []

    async def asyncTearDown(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        for srv in self.servers:
            srv.close()
            for t in list(srv._tasks):
                t.cancel()
            srv.shutdown()

    async def serve(self, handler, port=0):
        srv = await protocol.start_server(handler, "127.0.0.1", port, rust=True)
        self.servers.append(srv)
        return srv

    def connect(self, srv):
        s = socket.create_connection(("127.0.0.1", protocol.listen_port(srv)), timeout=20)
        self.socks.append(s)
        return s

    async def in_thread(self, fn, *a):
        return await self.loop.run_in_executor(None, fn, *a)

    async def echo_server(self, got=None):
        async def handler(r, w):
            while True:
                try:
                    m = await protocol.read_message(r)
                except (protocol.ProtocolError, ConnectionError) as e:
                    if got is not None:
                        got.append(e)
                    break
                if got is not None:
                    got.append(m)
                if m is None:
                    break
                w.write(protocol.encode({"type": "echo", "m": m}))
                await w.drain()
            w.close()
        return await self.serve(handler)

    async def test_round_trip_and_peer(self):
        peers = []

        async def handler(r, w):
            peers.append(w.get_extra_info("peername"))
            m = await protocol.read_message(r)
            w.write(protocol.encode({"type": "echo", "text": m["text"]}))
            await w.drain()
            w.close()
        srv = await self.serve(handler)
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "hello", "text": "Provence-Alpes-Côte d'Azur"}))
        got = await self.in_thread(recv_frame, s)
        self.assertEqual(got, '{"type":"echo","text":"Provence-Alpes-Côte d\'Azur"}'.encode())
        self.assertIsNone(await self.in_thread(recv_frame, s), "closed after the reply")
        self.assertEqual(peers, [s.getsockname()], "the peer as asyncio gives it: (host, port)")

    async def test_frame_sizes_and_the_limit(self):
        got = []
        srv = await self.echo_server(got)
        # a frame of exactly MAX_FRAME bytes is read (and echoed: 16 MiB + its envelope)
        body = b'{"type":"big","pad":"' + b"x" * (protocol.MAX_FRAME - 23) + b'"}'
        self.assertEqual(len(body), protocol.MAX_FRAME)
        s = self.connect(srv)
        await self.in_thread(s.sendall, frame(b'{"type":"one"}') + frame(body))
        self.assertEqual(json.loads(await self.in_thread(recv_frame, s))["m"], {"type": "one"})
        echoed = json.loads(await self.in_thread(recv_frame, s))
        self.assertEqual(len(echoed["m"]["pad"]), protocol.MAX_FRAME - 23)
        # one byte more: ProtocolError with asyncio's text, the connection closed
        await self.in_thread(s.sendall, struct.pack("<I", protocol.MAX_FRAME + 1))
        self.assertIsNone(await self.in_thread(recv_frame, s))
        self.assertIsInstance(got[-1], protocol.ProtocolError)
        self.assertEqual(str(got[-1]), "frame too large: 16777217 bytes")

    async def test_eof_cases_and_bad_json(self):
        cases = [(b"", None),
                 (b"\x05\x00", "connection closed inside a frame header"),
                 (frame(b'{"type":"x"}')[:7], "connection closed inside a frame"),
                 (frame(b"{not json"), "bad JSON frame"),
                 (frame(b"[1, 2]"), "frame is not an object with a 'type'")]
        for data, want in cases:
            got = []
            srv = await self.echo_server(got)
            s = self.connect(srv)
            await self.in_thread(s.sendall, data)
            s.shutdown(socket.SHUT_WR)
            self.assertIsNone(await self.in_thread(recv_frame, s), data)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.01)
            if want is None:
                self.assertEqual(got, [None], "a clean EOF reads as None")
            else:
                self.assertIsInstance(got[-1], protocol.ProtocolError, data)
                self.assertTrue(str(got[-1]).startswith(want), (str(got[-1]), want))

    async def test_peer_reset_raises_connection_reset(self):
        got = []
        srv = await self.echo_server(got)
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "a"}))
        await self.in_thread(recv_frame, s)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()                   # (RST)
        for _ in range(200):
            if len(got) > 1:
                break
            await asyncio.sleep(0.01)
        self.assertIsInstance(got[-1], ConnectionResetError)
        self.assertTrue(str(got[-1]).startswith(f"[Errno {errno.ECONNRESET}]"), str(got[-1]))

    async def test_drain_blocks_and_resumes(self):
        streams = []
        go = asyncio.Event()

        async def handler(r, w):
            streams.append(w)
            await go.wait()
        srv = await self.serve(handler)
        s = self.connect(srv)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        for _ in range(100):
            if streams:
                break
            await asyncio.sleep(0.01)
        w = streams[0]
        big = frame(b"y" * (32 << 20))
        w.write(big)
        await asyncio.sleep(0.3)
        pending = w.transport.get_write_buffer_size()
        self.assertGreater(pending, 1 << 20, "the client reads nothing: most of it waits in the queue")
        drain = asyncio.ensure_future(w.drain())
        await asyncio.sleep(0.3)
        self.assertFalse(drain.done(), "over the high-water mark: drain waits")
        reader = self.loop.run_in_executor(None, recv_exact, s, len(big))
        await asyncio.wait_for(drain, 20)
        self.assertLessEqual(w.transport.get_write_buffer_size(), 16 * 1024, "resumed at the low-water mark")
        self.assertEqual(await reader, big)
        # under the high-water mark drain returns at once
        w.write(frame(b"z" * 1000))
        await asyncio.wait_for(w.drain(), 1)
        go.set()

    async def test_close_flushes_abort_drops(self):
        streams = []

        async def handler(r, w):
            streams.append(w)
            await asyncio.sleep(30)
        srv = await self.serve(handler)
        a, b = self.connect(srv), self.connect(srv)
        for x in (a, b):
            x.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        for _ in range(100):
            if len(streams) == 2:
                break
            await asyncio.sleep(0.01)
        payload = frame(b"q" * (16 << 20))
        for w in streams:
            w.write(payload)
        await asyncio.sleep(0.2)
        wa, wb = streams
        wa.close()
        self.assertTrue(wa.is_closing())
        wa.write(b"ignored after close")
        wb.transport.abort()
        self.assertTrue(wb.is_closing())
        self.assertEqual(wb.transport.get_write_buffer_size(), 0, "abort drops the queue")
        got_a = await self.in_thread(recv_all, a)
        self.assertEqual(got_a, payload, "close sends everything queued, then closes")
        try:
            got_b = await self.in_thread(recv_all, b)
        except ConnectionResetError:
            got_b = b""
        self.assertLess(len(got_b), len(payload), "abort: the rest never goes")
        with self.assertRaises(ConnectionResetError):
            await wb.drain()

    async def test_two_connections_and_close_keeps_them(self):
        srv = await self.echo_server()
        a, b = self.connect(srv), self.connect(srv)
        a.sendall(protocol.encode({"type": "a"}))
        b.sendall(protocol.encode({"type": "b"}))
        self.assertEqual(json.loads(await self.in_thread(recv_frame, b))["m"], {"type": "b"})
        self.assertEqual(json.loads(await self.in_thread(recv_frame, a))["m"], {"type": "a"})
        srv.close()
        self.assertFalse(srv.is_serving())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", srv.port), timeout=2).close()
        a.sendall(protocol.encode({"type": "after"}))
        self.assertEqual(json.loads(await self.in_thread(recv_frame, a))["m"], {"type": "after"},
                         "close() stops accepting; open connections stay")
        a.close()
        b.close()
        await asyncio.wait_for(srv.wait_closed(), 5)

    async def test_address_in_use(self):
        srv = await self.echo_server()
        with self.assertRaises(OSError) as e:
            await protocol.start_server(lambda r, w: None, "127.0.0.1", srv.port, rust=True)
        self.assertEqual(e.exception.errno, errno.EADDRINUSE)
        self.assertIn("error while attempting to bind on address ('127.0.0.1', %d)" % srv.port, str(e.exception))

    @unittest.skipIf(sys.platform == "win32", "SO_REUSEADDR is POSIX's (asyncio sets it there only)")
    async def test_rebind_inside_time_wait(self):
        async def handler(r, w):
            w.close()               # (this side closes first: its socket waits in TIME_WAIT)
        srv = await self.serve(handler)
        port = srv.port
        s = self.connect(srv)
        self.assertIsNone(await self.in_thread(recv_frame, s))
        s.close()
        srv.close()
        srv.shutdown()
        self.servers.remove(srv)
        again = await self.serve(handler, port)
        self.assertEqual(again.port, port)

    async def test_order_of_10k_frames_under_busy_threads(self):
        got = []
        done = asyncio.Event()

        async def handler(r, w):
            while True:
                m = await protocol.read_message(r)
                if m is None:
                    break
                got.append(m["i"])
            done.set()
        srv = await self.serve(handler)
        stop = threading.Event()

        def busy():
            x = 0
            while not stop.is_set():
                x = (x * 31 + 7) % 1000003
        threads = [threading.Thread(target=busy, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        try:
            s = self.connect(srv)
            data = b"".join(protocol.encode({"type": "n", "i": i}) for i in range(10000))
            await self.in_thread(s.sendall, data)
            s.shutdown(socket.SHUT_WR)
            await asyncio.wait_for(done.wait(), 60)
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(got, list(range(10000)), "frames reach Python in the order read")
        st = srv.stats()
        self.assertEqual(st["frames_in"], 10000)
        self.assertEqual(st["waits"], 10000, "each frame's lane wait counted")

    async def test_lane_wait_numbers(self):
        got = []
        srv = await self.echo_server(got)
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "warm"}))
        await self.in_thread(recv_frame, s)
        srv.stats(reset=True)
        s.sendall(protocol.encode({"type": "late"}))
        time.sleep(0.5)             # (the loop busy: the frame waits for it)
        await self.in_thread(recv_frame, s)
        st = srv.stats(reset=True)
        self.assertEqual(st["waits"], 1)
        self.assertGreater(st["wait_max_ms"], 400)
        self.assertLess(st["wait_max_ms"], 5000)
        self.assertEqual(srv.stats()["waits"], 0, "reset")
        self.assertIsNone(srv.summary(), "no traffic: no host line")
        s.sendall(protocol.encode({"type": "again"}))
        await self.in_thread(recv_frame, s)
        line = srv.summary()
        self.assertTrue(line.startswith("host: 1 frames in (0.0 MB), 1 out (0.0 MB); loop wait p50 "), line)
        self.assertIn(" ms, p99 ", line)

    async def test_poses_merged_while_the_lane_is_busy(self):
        got = []

        async def handler(r, w):
            while True:
                m = await protocol.read_message(r)
                if m is None:
                    break
                got.append(m)
                if m["type"] == "warm":
                    w.write(protocol.encode({"type": "ok"}))
        srv = await self.serve(handler)
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "warm"}))
        await self.in_thread(recv_frame, s)

        def pose(aid, x, gone=None):
            m = {"type": "pose", "anchorId": aid, "x": x, "cam": {"x": x}}
            if gone is not None:
                m["slotsGone"] = gone
            return protocol.encode(m)
        frames = [pose(1, 0, [[1, 1]]), pose(1, 1, []), pose(1, 2, [[2, 2]]),
                  protocol.encode({"type": "tile_request", "anchorId": 1}),
                  pose(1, 3), pose(2, 4, {"1": [3, 3]}), pose(2, 5, {})]

        def send_slowly():
            for f in frames:
                s.sendall(f)
                time.sleep(0.05)
        t = threading.Thread(target=send_slowly)
        t.start()
        time.sleep(1.0)             # (the loop busy while they come)
        t.join()
        for _ in range(100):
            if len(got) >= 5:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        self.assertEqual(got[1:], [
            {"type": "pose", "anchorId": 1, "x": 2, "cam": {"x": 2}, "slotsGone": [[1, 1], [2, 2]]},
            {"type": "tile_request", "anchorId": 1},
            {"type": "pose", "anchorId": 1, "x": 3, "cam": {"x": 3}},
            {"type": "pose", "anchorId": 2, "x": 5, "cam": {"x": 5}, "slotsGone": [[3, 3]]},
        ], "consecutive poses of one anchor merged (the later one, slotsGone joined); none across another frame or "
           "another anchor")
        self.assertEqual(srv.stats()["merged"], 3)
        self.assertIn("; 3 poses merged", srv.summary())

    async def test_recording_round_trip(self):
        import tempfile

        import earthfast
        tmp = tempfile.TemporaryDirectory(prefix="bnge-hostrec-")
        self.addCleanup(tmp.cleanup)
        done = asyncio.Event()

        async def handler(r, w):
            while True:
                m = await protocol.read_message(r)
                if m is None:
                    break
                w.write(protocol.encode({"type": "echo", "m": m}))
            w.close()
            done.set()
        srv = await protocol.start_server(handler, "127.0.0.1", 0, rust=True, record=tmp.name, record_out=True)
        self.servers.append(srv)
        s = self.connect(srv)
        sent = [protocol.encode({"type": "hello", "i": i}) for i in range(3)]
        for f in sent:
            s.sendall(f)
            await self.in_thread(recv_frame, s)
        s.shutdown(socket.SHUT_WR)
        await asyncio.wait_for(done.wait(), 5)
        self.assertIsNone(await self.in_thread(recv_frame, s))
        ins = [n for n in os.listdir(tmp.name) if n.endswith(".in.zst")]
        self.assertEqual(len(ins), 1, os.listdir(tmp.name))
        got = earthfast.host_read_recording(os.path.join(tmp.name, ins[0]))
        self.assertEqual([d for _, d in got], [f[4:] for f in sent], "every frame read, as read")
        self.assertEqual([t for t, _ in got], sorted(t for t, _ in got))
        out = []
        for _ in range(100):        # (the writer thread finishes its file as it ends)
            try:
                out = earthfast.host_read_recording(os.path.join(tmp.name, ins[0].replace(".in.", ".out.")))
            except OSError:
                out = []
            if len(out) == 3:
                break
            await asyncio.sleep(0.02)
        self.assertEqual([json.loads(d[4:])["m"]["i"] for _, d in out], [0, 1, 2], "every chunk written")

    async def test_reading_pauses_for_a_handler_that_does_not_read(self):
        """Codex review of 28c35fc0: what the handler has not consumed is bounded (asyncio StreamReader's 128 / 64 KiB),
        however long it does not read, and reading goes on once it does: nothing lost, in order."""
        streams = []
        go = asyncio.Event()
        got = []

        async def handler(r, w):
            streams.append(r)
            await go.wait()
            while True:
                m = await protocol.read_message(r)
                if m is None:
                    break
                got.append(m["i"])
        srv = await self.serve(handler)
        s = self.connect(srv)
        pad = "x" * 2000
        frames = [protocol.encode({"type": "n", "i": i, "pad": pad}) for i in range(10000)]   # ~20 MB
        sent = []

        def send_all():
            for f in frames:
                s.sendall(f)
                sent.append(1)
            s.shutdown(socket.SHUT_WR)
        t = threading.Thread(target=send_all, daemon=True)
        t.start()
        await asyncio.sleep(1.5)
        st = streams[0]
        held = sum(len(f) + 4 for f in st._frames)
        self.assertLessEqual(st._wire.unread(), 128 * 1024 + len(frames[0]),
                             "read and not consumed: at most the high-water mark and one frame")
        self.assertLessEqual(held, st._wire.unread())
        self.assertLess(len(sent), len(frames), "the rest waits in the kernel: the sender is held back")
        go.set()
        await self.in_thread(t.join, 60)
        for _ in range(500):
            if len(got) == len(frames):
                break
            await asyncio.sleep(0.02)
        self.assertEqual(got, list(range(len(frames))), "reading went on: every frame, in order")
        self.assertEqual(st._wire.unread(), 0)

    async def test_asyncio_by_default(self):
        if "BNGE_HOST" not in os.environ:
            self.assertFalse(protocol.HOST, "BNGE_HOST is off unless set (the asyncio path stays the default)")
        srv = await protocol.start_server(lambda r, w: None, "127.0.0.1", 0, rust=False)
        self.assertIsInstance(srv, asyncio.Server)
        self.assertGreater(protocol.listen_port(srv), 0)
        srv.close()
        await srv.wait_closed()

    async def test_a_busy_lane_gets_one_batch(self):
        got = []
        srv = await self.echo_server(got)
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "warm"}))
        await self.in_thread(recv_frame, s)
        before = srv.batches

        def send_slowly():
            for i in range(5):
                s.sendall(protocol.encode({"type": "n", "i": i}))
                time.sleep(0.1)
        t = threading.Thread(target=send_slowly)
        t.start()
        time.sleep(1.0)             # (the loop busy while the five come one by one)
        t.join()
        for _ in range(5):
            await self.in_thread(recv_frame, s)
        self.assertEqual([m["i"] for m in got[1:]], [0, 1, 2, 3, 4])
        self.assertEqual(srv.batches - before, 1, "what came while the lane was busy is handed over as one batch")

    async def test_io_moves_while_the_gil_is_held(self):
        import earthfast
        hold_s = 3.0
        marks = {}

        async def handler(r, w):
            w.write(protocol.encode({"type": "go"}))
            w.write(frame(b"b" * (8 << 20)))
            await asyncio.sleep(0.2)
            marks["hold_from"] = time.time()
            earthfast.host_hold_gil(hold_s)      # (a kernel that keeps the GIL: the loop and every Python thread wait)
            marks["hold_to"] = time.time()
            m = await protocol.read_message(r)
            marks["msg"] = m
            w.close()
        srv = await self.serve(handler)
        # the client in another process (this one's threads wait for the GIL too): it reads "go", sends a frame
        # while the GIL is held, reads the 8 MB and says when it had all of it
        client = subprocess.Popen([sys.executable, "-I", "-c", CLIENT, str(srv.port)], stdout=subprocess.PIPE,
                                  text=True)
        out, _ = await self.in_thread(client.communicate)
        self.assertEqual(client.returncode, 0, out)
        res = json.loads(out)
        self.assertEqual(res["n"], 8 << 20)
        self.assertLess(res["read_all"], marks["hold_to"] - 0.5, "the 8 MB went out while the GIL was held")
        self.assertGreater(res["sent"], marks["hold_from"])
        for _ in range(250):
            if "msg" in marks:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(marks.get("msg"), {"type": "during"})
        st = srv.stats()
        self.assertGreater(st["wait_max_ms"], 1500, "the frame was read during the hold and waited for the lane")

    @unittest.skipUnless(sys.platform.startswith("linux"), "thread names from /proc")
    async def test_threads_end(self):
        srv = await self.echo_server()
        s = self.connect(srv)
        s.sendall(protocol.encode({"type": "a"}))
        await self.in_thread(recv_frame, s)
        names = host_threads()
        self.assertIn("bnge-io", names)
        self.assertIn("bnge-watch", names)
        self.assertTrue(any(n.startswith("bnge-rd-") for n in names), names)
        s.close()
        srv.close()
        await asyncio.wait_for(srv.wait_closed(), 5)
        for _ in range(300):        # (closed and every connection over: the host's threads end by themselves)
            if not host_threads():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(host_threads(), [])
        srv.shutdown()


CLIENT = r"""
import json, socket, struct, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=20)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
def exact(n):
    b = bytearray()
    while len(b) < n:
        c = s.recv(min(1 << 20, n - len(b)))
        if not c:
            raise SystemExit("short read")
        b += c
    return bytes(b)
n = struct.unpack("<I", exact(4))[0]
assert json.loads(exact(n)) == {"type": "go"}
time.sleep(0.6)
p = json.dumps({"type": "during"}).encode()
s.sendall(struct.pack("<I", len(p)) + p)
sent = time.time()
n = struct.unpack("<I", exact(4))[0]
exact(n)
print(json.dumps({"n": n, "sent": sent, "read_all": time.time()}))
"""


if __name__ == "__main__":
    unittest.main()
