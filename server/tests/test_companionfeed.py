"""companionfeed.py: the read-only position feed for programs beside the game. /v1/info, /v1/pose (none yet, then the
latest with its age and the velocity's bearing), /v1/stream (the pose as server-sent events, as they come), only GET,
only requests that name this computer (a page whose name points at 127.0.0.1 is refused: DNS rebinding), a web page
of another origin allowed to read only when listed, and the registry's origins."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import addons  # noqa: E402
import companionfeed  # noqa: E402


async def get(port, path, host=None, method="GET", origin=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    hdr = f"{method} {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
    if origin:
        hdr += f"Origin: {origin}\r\n"
    w.write((hdr + "\r\n").encode())
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    head, _, body = data.decode().partition("\r\n\r\n")
    return head, body


class FeedTest(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 20))

    def test_endpoints(self):
        async def go():
            f = companionfeed.Feed("1.9.0-beta", origins=["http://127.0.0.1:47700"], clock=lambda: 100.0)
            srv, port = await f.start(0)
            async with srv:
                head, body = await get(port, "/v1/info")
                self.assertIn("200 OK", head)
                self.assertEqual(json.loads(body), {"api": 1, "app": "1.9.0-beta"})
                self.assertEqual(json.loads((await get(port, "/v1/pose"))[1]), {"ok": False})
                f.publish(45.5, 7.25, 12.0, 0.0, 12.0, 3)
                p = json.loads((await get(port, "/v1/pose", host=f"localhost:{port}"))[1])
                self.assertEqual((p["ok"], p["lat"], p["lon"], p["speed"], p["bearing"], p["anchorId"], p["age"]),
                                 (True, 45.5, 7.25, 12.0, 0.0, 3, 0.0))
                f.publish(45.5, 7.25, 0.1, 0.1, 0.0)
                self.assertIsNone(json.loads((await get(port, "/v1/pose"))[1])["bearing"], "standing: no bearing")
                f.publish(95, 0)
                self.assertEqual(json.loads((await get(port, "/v1/pose"))[1])["lat"], 45.5, "a bad pose is ignored")
                head, _ = await get(port, "/v1/pose", host="evil.example")
                self.assertIn("403", head, "another name for this computer: refused")
                self.assertIn("405", (await get(port, "/v1/pose", method="POST"))[0])
                self.assertIn("404", (await get(port, "/v1/../etc/passwd"))[0])
                head, _ = await get(port, "/v1/pose", origin="http://127.0.0.1:47700")
                self.assertIn("Access-Control-Allow-Origin: http://127.0.0.1:47700", head)
                head, _ = await get(port, "/v1/pose", origin="https://evil.example")
                self.assertNotIn("Access-Control-Allow-Origin", head, "other web pages cannot read it")
        self.run_async(go())

    def test_stream(self):
        async def go():
            f = companionfeed.Feed()
            srv, port = await f.start(0)
            async with srv:
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(f"GET /v1/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                await w.drain()
                head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
                self.assertIn(b"text/event-stream", head)
                for i in range(50):
                    if f._streams:
                        break
                    await asyncio.sleep(0.01)
                f.publish(10.0, 20.0, 5.0, 5.0, 0.0)
                line = await asyncio.wait_for(r.readuntil(b"\n\n"), 5)
                p = json.loads(line.decode()[len("data: "):])
                self.assertEqual((p["lat"], p["lon"], p["bearing"]), (10.0, 20.0, 90.0))
                w.close()
        self.run_async(go())

    def test_registry_origins(self):
        good = {"format": 1, "addons": [{"id": "gps-tool", "name": "G", "author": "a", "repo": "a/b", "kind": "companion",
                                         "summary": "s", "licence": "l", "asset": "x", "feedOrigins": ["http://127.0.0.1:47700"]}]}
        self.assertEqual(addons.parse_registry(good)[0]["feedOrigins"], ["http://127.0.0.1:47700"])
        for bad in ("https://evil.example", "http://192.168.1.2:47700", "http://127.0.0.1:47700/x", "*"):
            g = json.loads(json.dumps(good))
            g["addons"][0]["feedOrigins"] = [bad]
            self.assertEqual(addons.parse_registry(g), [], bad)
        self.assertIsInstance(addons.feed_origins(), list)


if __name__ == "__main__":
    unittest.main()
