"""Actual loopback transfer, isolated clients and failed activation of road-plan bundles."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
import plancache
import worldbundle
import world_cache


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cache = plancache.PlanCache(str(self.root / "prepared"), "w0s0")
        self.assertTrue(cache.store((14, 4000, 6000), [], [], [], [], persist=True))
        self.assertTrue(cache.store((14, 4001, 6000), [], [], [], [], persist=True))
        self.bundle = self.root / "bundle"
        self.manifest = worldbundle.build_bundle(cache.dir, self.bundle, {}, "1" * 64, ["Synthetic fixture"])
        self.assertEqual(len(self.manifest["artifacts"]), 2)
        self.assertEqual(len({a["sha256"] for a in self.manifest["artifacts"]}), 1)
        self.server = world_cache.make_server(self.bundle, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_two_isolated_clients_and_offline_warm_reuse(self):
        paths = []
        for name in ("client-a", "client-b"):
            path, stats = world_cache.fetch(self.url, self.root / name, self.manifest["id"])
            self.assertEqual(stats["requests"], 2)
            self.assertFalse(stats["warm"])
            self.assertEqual(worldbundle.validate_bundle(path)["id"], self.manifest["id"])
            paths.append(path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(self.server.stats["requests"], 4)
        with mock.patch.object(world_cache, "download", side_effect=AssertionError("must be offline")):
            path, stats = world_cache.fetch(self.url, self.root / "client-a", self.manifest["id"])
        self.assertEqual(path, paths[0])
        self.assertEqual(stats, {"requests": 0, "bytes": 0, "warm": True})

    def test_hash_mismatch_never_activates_or_replaces_previous(self):
        dest = self.root / "client"
        previous = dest / "previous"
        previous.mkdir(parents=True)
        (previous / "keep").write_text("old world")
        original = world_cache.download
        def corrupt(base, path, limit, timeout=20):
            data = original(base, path, limit, timeout)
            return b"corrupt" if path.startswith("blobs/") else data
        with mock.patch.object(world_cache, "download", side_effect=corrupt):
            with self.assertRaises(ValueError):
                world_cache.fetch(self.url, dest, self.manifest["id"])
        self.assertEqual((previous / "keep").read_text(), "old world")
        self.assertFalse((dest / self.manifest["id"]).exists())
        self.assertEqual([p.name for p in dest.iterdir()], ["previous"])

    def test_wrong_id_and_quota_reject_before_artifact_request(self):
        for digest, cap in (("0" * 64, 1 << 20), (self.manifest["id"], 1)):
            with self.subTest(digest=digest, cap=cap):
                before = self.server.stats["requests"]
                with self.assertRaises(ValueError):
                    world_cache.fetch(self.url, self.root / "client", digest, cap)
                self.assertEqual(self.server.stats["requests"] - before, 1)
                self.assertFalse((self.root / "client" / digest).exists())

    def test_request_count_recorded_before_response_is_visible(self):
        handler = self.server.RequestHandlerClass
        original = handler.end_headers
        counts = []
        def end_headers(request):
            counts.append(self.server.stats["requests"])
            return original(request)
        with mock.patch.object(handler, "end_headers", end_headers):
            data = world_cache.download(self.url, "manifest.json", worldbundle.MAX_MANIFEST_BYTES)
        self.assertEqual(worldbundle.parse_manifest(data)["id"], self.manifest["id"])
        self.assertEqual(counts, [1])

    def test_inflight_install_refused_and_existing_lock_kept(self):
        dest = self.root / "client"
        dest.mkdir()
        (dest / ".fetch.lock").write_text("another writer")
        with self.assertRaises(FileExistsError):
            world_cache.fetch(self.url, dest, self.manifest["id"])
        self.assertTrue((dest / ".fetch.lock").exists())

    def test_serving_has_no_filesystem_traversal_or_directory_listing(self):
        for path in ("../manifest.json", "", "%2e%2e/manifest.json", "manifest.json?token=x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                world_cache.download(self.url, path, 1024)

    def test_modified_served_blob_is_rejected(self):
        blob = self.bundle / "blobs" / (self.manifest["artifacts"][0]["sha256"] + ".npz")
        blob.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            world_cache.fetch(self.url, self.root / "client", self.manifest["id"])
        self.assertFalse((self.root / "client" / self.manifest["id"]).exists())

    def test_url_authority_and_redirect_restrictions(self):
        for url in ("http://example.com", "ftp://127.0.0.1", "https://user:pass@example.com",
                    "https://example.com?key=secret", "http://localhost", "https://example.com/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                world_cache.endpoint(url)
        response = mock.Mock(status=302)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(world_cache.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(ValueError):
                world_cache.download("https://example.com", "manifest.json", 1024)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()


class DeadlineTest(unittest.TestCase):
    def test_deadline_interrupts_headers_and_detached_body_socket(self):
        # Trigger the watchdog explicitly after a real socket read starts. This
        # reproduces a stalled/trickling peer without sleep-sensitive timing.
        for stage in ("headers", "body"):
            with self.subTest(stage=stage):
                sent, reading, release, finished = (threading.Event() for _ in range(4))
                timers, failures, detached = [], [], []

                class Timer:
                    def __init__(self, interval, callback):
                        self.callback = callback
                        self.cancelled = self.joined = False
                        timers.append(self)
                    def start(self):
                        pass
                    def cancel(self):
                        self.cancelled = True
                    def join(self):
                        self.joined = True

                class Handler(world_cache.BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_GET(self):
                        prefix = (b"HTTP/1.0 200" if stage == "headers" else
                                  b"HTTP/1.0 200 OK\r\nContent-Length: 1\r\n\r\n")
                        self.connection.sendall(prefix)
                        sent.set()
                        release.wait(5)

                server = world_cache.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                serving = threading.Thread(target=server.serve_forever,
                                           kwargs={"poll_interval": .01}, daemon=True)
                serving.start()
                original_response = world_cache.http.client.HTTPConnection.getresponse
                original_read = world_cache.http.client.HTTPResponse.read1

                def getresponse(connection):
                    if stage == "headers":
                        reading.set()
                    result = original_response(connection)
                    detached.append(connection.sock is None)
                    return result

                def read1(response, *args):
                    reading.set()
                    return original_read(response, *args)

                def receive():
                    try:
                        world_cache.download(f"http://127.0.0.1:{server.server_port}",
                                             "manifest.json", 1024, timeout=30)
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        finished.set()

                worker = threading.Thread(target=receive, daemon=True)
                try:
                    with mock.patch.object(world_cache.threading, "Timer", Timer), \
                            mock.patch.object(world_cache.http.client.HTTPConnection, "getresponse", getresponse), \
                            mock.patch.object(world_cache.http.client.HTTPResponse, "read1", read1):
                        worker.start()
                        self.assertTrue(sent.wait(3), "server did not send response prefix")
                        self.assertTrue(reading.wait(3), "client did not begin the blocking read")
                        self.assertEqual(len(timers), 1)
                        timers[0].callback()
                        self.assertTrue(finished.wait(3), "deadline did not interrupt the socket")
                        self.assertEqual(len(failures), 1)
                        self.assertIsInstance(failures[0], TimeoutError)
                        self.assertTrue(timers[0].cancelled)
                        self.assertTrue(timers[0].joined)
                        if stage == "body":
                            self.assertEqual(detached, [True])
                finally:
                    release.set()
                    worker.join(3)
                    server.shutdown()
                    server.server_close()
                    serving.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(serving.is_alive())

    def test_invalid_timeouts_rejected_without_connecting(self):
        for timeout in (0, -1, float("nan"), float("inf"), "20"):
            with self.subTest(timeout=timeout), \
                    mock.patch.object(world_cache.http.client, "HTTPSConnection") as connection:
                with self.assertRaises(ValueError):
                    world_cache.download("https://example.com", "manifest.json", 1024, timeout)
                connection.assert_not_called()

    def test_source_versions_cli_uses_bundle_snapshot_and_closes_resources(self):
        import mapdata
        import netfetch
        for failure in (None, RuntimeError("metadata unavailable")):
            with self.subTest(failure=failure), \
                    mock.patch.object(mapdata, "MapData") as make_md, \
                    mock.patch.object(netfetch, "Fetcher") as make_fetcher, \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                md = make_md.return_value
                md.bundle_versions.return_value = {"rail": "cached-rail", "lanes": "cached-lanes"}
                md.bundle_versions.side_effect = failure
                result = world_cache.main(["source-versions", "fixture-cache"])
                self.assertEqual(result, 2 if failure else 0)
                md.bundle_versions.assert_called_once_with()
                md.versions.assert_not_called()
                md.close.assert_called_once_with()
                make_fetcher.assert_called_once_with(offline=True)
                make_fetcher.return_value.close.assert_called_once_with()
                if failure is None:
                    self.assertEqual(json.loads(output.getvalue()), md.bundle_versions.return_value)


if __name__ == "__main__":
    unittest.main()
