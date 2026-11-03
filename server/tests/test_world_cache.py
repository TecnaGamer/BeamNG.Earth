"""Actual loopback transfer, isolated clients and failed activation of road-plan bundles."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
