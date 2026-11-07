"""Native metadata credits: bounded FIFO, adversarial acknowledgements and real Connection.send integration."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast
import earthd
import protocol


class StreamDispatchTest(unittest.TestCase):
    def setUp(self):
        fast.require(169, "dispatch tests")

    def test_binding_fifo_and_limits(self):
        q = fast._ef.StreamDispatch(2, 6, 2)
        self.assertEqual(q.enqueue(1, b"a"), [b"a"])
        self.assertEqual(q.enqueue(2, b"b"), [b"b"])
        self.assertEqual(q.enqueue(3, b"ccc"), [])
        self.assertEqual(q.enqueue(4, b"ddd"), [])
        self.assertEqual((q.inflight, q.queued_messages, q.queued_bytes), (2, 2, 6))
        with self.assertRaises(ValueError):
            q.enqueue(5, b"e")
        self.assertEqual(q.next_seq, 5)
        with self.assertRaises(ValueError):
            q.acknowledge(3)  # queued is not transmitted
        self.assertEqual(q.acknowledge(1), [b"ccc"])
        self.assertEqual(q.acknowledge(1), [])
        self.assertEqual(q.acknowledge(0), [])
        self.assertEqual(q.acknowledge(3), [b"ddd"])
        self.assertEqual((q.inflight, q.queued_bytes), (1, 0))

    def test_invalid_limits(self):
        for args in [(0, 10, 10), (129, 10, 10), (2, 0, 10), (2, 10, 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                fast._ef.StreamDispatch(*args)

    def connection(self, window=None):
        c = earthd.Connection.__new__(earthd.Connection)
        c.closing, c.lease, c.streamer, c.tasks, c.peer = False, None, None, set(), "fixture"
        self.frames, self.closed = [], False
        c.writer = SimpleNamespace(
            is_closing=lambda: self.closed, write=self.frames.append,
            close=lambda: setattr(self, "closed", True), transport=None)
        c.stream_dispatch = None if window is None else fast._ef.StreamDispatch(window, earthd.SEND_BUFFER_MAX, 4096)
        return c

    def messages(self):
        d = protocol.Decoder()
        return [m for frame in self.frames for m in d.feed(frame)]

    def test_connection_paces_normal_messages_but_not_critical_answers(self):
        c = self.connection(2)
        for k in range(20):
            self.assertTrue(c.send(dict(type="roads_tile", key=k)))
        for kind in ("terrain_ready", "terrain_refresh", "block_base", "anchor_ready", "route_result", "status"):
            self.assertTrue(c.send(dict(type=kind)))
        initial = self.messages()
        self.assertEqual([m["key"] for m in initial if m["type"] == "roads_tile"], [0, 1])
        self.assertTrue(all("streamSeq" not in m for m in initial if m["type"] != "roads_tile"))
        for seq in range(2, 21, 2):
            c.dispatch(dict(type="stream_ack", seq=seq))
            self.assertLessEqual(c.stream_dispatch.inflight, 2)
        self.assertEqual([m["key"] for m in self.messages() if m["type"] == "roads_tile"], list(range(20)))
        self.assertEqual(c.stream_dispatch.queued_bytes, 0)

    def test_legacy_sender_and_stale_owner(self):
        c = self.connection()
        for k in range(20):
            self.assertTrue(c.send(dict(type="roads_tile", key=k)))
        self.assertEqual(len(self.messages()), 20)
        self.assertTrue(all("streamSeq" not in m for m in self.messages()))
        c.lease = SimpleNamespace(connection=object())
        self.assertFalse(c.send(dict(type="roads_tile", key=999)))
        self.assertEqual(len(self.frames), 20)

    def test_queue_limit_closes_instead_of_dropping_messages_silently(self):
        c = self.connection(1)
        c.stream_dispatch = fast._ef.StreamDispatch(1, 100, 1)
        self.assertTrue(c.send(dict(type="roads_tile", key=1)))
        self.assertTrue(c.send(dict(type="roads_tile", key=2)))
        self.assertFalse(c.send(dict(type="roads_tile", key=3)))
        self.assertTrue(c.closing and self.closed)

    def test_replaced_lease_cannot_flush_queued_metadata(self):
        c = self.connection(1)
        c.send(dict(type="roads_tile", key="first"))
        c.send(dict(type="roads_tile", key="queued"))
        c.lease = SimpleNamespace(connection=object())
        c.dispatch(dict(type="stream_ack", seq=1))
        self.assertEqual([m["key"] for m in self.messages()], ["first"])


if __name__ == "__main__":
    unittest.main()
