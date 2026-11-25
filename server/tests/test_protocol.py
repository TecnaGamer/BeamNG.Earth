import asyncio
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol  # noqa: E402


class FramingTest(unittest.TestCase):
    def test_encode_layout(self):
        b = protocol.encode({"type": "hello", "protocol": 1, "s": "é"})
        n = struct.unpack("<I", b[:4])[0]
        self.assertEqual(n, len(b) - 4)
        self.assertEqual(b[4:].decode("utf-8"), '{"type":"hello","protocol":1,"s":"é"}')   # UTF-8: the game's jsonDecode keeps \\u escapes as text

    def test_decoder_split_and_batched(self):
        msgs = [{"type": "pose", "x": i * 1.5} for i in range(5)]
        data = b"".join(protocol.encode(m) for m in msgs)
        d = protocol.Decoder()
        out = []
        for i in range(len(data)):          # one byte at a time
            out += d.feed(data[i:i + 1])
        self.assertEqual(out, msgs)
        self.assertEqual(protocol.Decoder().feed(data), msgs)

    def test_bad_frames(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.Decoder().feed(struct.pack("<I", 3) + b"{x}")
        with self.assertRaises(protocol.ProtocolError):
            protocol.Decoder().feed(struct.pack("<I", 2) + b"[]")
        with self.assertRaises(protocol.ProtocolError):
            protocol.Decoder().feed(struct.pack("<I", protocol.MAX_FRAME + 1))

    def test_async_reader(self):
        async def run():
            r = asyncio.StreamReader()
            r.feed_data(protocol.encode({"type": "a"}) + protocol.encode({"type": "b"}))
            r.feed_eof()
            return [await protocol.read_message(r) for _ in range(3)]
        self.assertEqual(asyncio.run(run()), [{"type": "a"}, {"type": "b"}, None])

    def test_raw_json_spliced_as_is(self):
        """A RawJson value (a tile file read in its worker) goes into the frame as its text; the frame decodes to the
        same message as with the parsed value."""
        text = '{"groups":["a"],"items":[[1,2.5,-0.125]],"name":"Côte"}'
        msg = {"type": "veg_tile", "key": "g17_1_2", "data": protocol.RawJson(text), "slots": {"shape": ["p", 3]}}
        frame = protocol.encode(msg)
        self.assertIn(text.encode("utf-8"), frame)
        want = {"type": "veg_tile", "key": "g17_1_2", "data": json.loads(text), "slots": {"shape": ["p", 3]}}
        self.assertEqual(protocol.decode_payload(frame[4:]), want)
        self.assertEqual(protocol.HEADER.unpack(frame[:4])[0], len(frame) - 4)
        self.assertEqual(msg["data"]["items"], [[1, 2.5, -0.125]], "read as the value it holds")
        self.assertEqual(json.loads(protocol.encode({"d": protocol.RawJson("[1]")})[4:]), {"d": [1]}, "no other key")

    def test_async_reader_truncated(self):
        async def run():
            r = asyncio.StreamReader()
            r.feed_data(protocol.encode({"type": "a"})[:-2])
            r.feed_eof()
            return await protocol.read_message(r)
        with self.assertRaises(protocol.ProtocolError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
