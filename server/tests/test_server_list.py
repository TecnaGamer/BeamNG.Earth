"""The BeamMP list can use a self-hosted collector, with the same network limits."""
import asyncio
import importlib
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import netfetch  # noqa: E402


class ServerListTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(earthd)         # restore the environment as it was
        importlib.reload(netfetch)

    def test_url_override_and_default(self):
        for url in (None, "https://collector.example.org:8443/custom/servers"):
            with self.subTest(url=url), mock.patch.dict(os.environ):
                os.environ.pop("BNGE_SERVER_LIST_URL", None)
                if url is not None:
                    os.environ["BNGE_SERVER_LIST_URL"] = url
                importlib.reload(netfetch)
                importlib.reload(earthd)
                want = url or "https://earth.tgforever.net/servers"
                self.assertEqual(earthd.SERVER_LIST_URL, want)
                net = mock.Mock()
                net.get.return_value.body = b'[{"name":"test server"}]'
                conn = types.SimpleNamespace(s=types.SimpleNamespace(net=net, ui_pool=None), send=mock.Mock())
                async def request():
                    loop = asyncio.get_running_loop()

                    def run(_pool, fn):
                        future = loop.create_future()
                        future.set_result(fn())
                        return future

                    with mock.patch.object(loop, "run_in_executor", side_effect=run):
                        await earthd.Connection.server_list(conn)

                asyncio.run(request())
                net.get.assert_called_once_with(want, kind="demand")
                conn.send.assert_called_once_with({"type": "server_list", "servers": [{"name": "test server"}]})
                host = "collector.example.org" if url else "earth.tgforever.net"
                self.assertEqual(netfetch.Fetcher(transport=mock.Mock()).policy(host), netfetch.POLICIES["earth.tgforever.net"])

    def test_override_keeps_known_host_policy(self):
        with mock.patch.dict(os.environ, {"BNGE_SERVER_LIST_URL": "https://overpass-api.de/servers"}):
            importlib.reload(netfetch)
            self.assertTrue(netfetch.POLICIES["overpass-api.de"].denied)


if __name__ == "__main__":
    unittest.main()
