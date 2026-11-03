"""The host bundle must contain verified upstream binary, source, notices and no AuthKey."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import build_host


class HostPackageTest(unittest.TestCase):
    def test_wrong_binary_is_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "digest"):
                build_host.build(d, lambda url: b"wrong binary")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_bundle_keeps_verified_binary_source_and_notices(self):
        binary = b"MZ test fixture"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("upstream/LICENSE", "fixture license")
            z.writestr("upstream/CMakeLists.txt", "fixture build source")
        def download(url):
            if url == build_host.EXE_URL:
                return binary
            if url.endswith("/LICENSE"):
                return b"GNU AFFERO GENERAL PUBLIC LICENSE fixture"
            return archive.getvalue()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(build_host, "EXE_SIZE", len(binary)), \
                mock.patch.object(build_host, "EXE_SHA256", build_host.sha256(binary)):
            host, source = build_host.build(d, download)
            with zipfile.ZipFile(host) as z:
                self.assertEqual(z.read("BeamMP-Server.exe"), binary)
                self.assertIn('AuthKey = ""', z.read("ServerConfig.toml").decode())
                self.assertIn('Private = true', z.read("ServerConfig.toml").decode())
                self.assertIn("Resources/Client/beamng_earth.zip", z.namelist())
                self.assertIn("Resources/Server/BeamNGEarth/main.lua", z.namelist())
                self.assertEqual(z.read(source.name), source.read_bytes())
                provenance = json.loads(z.read("UPSTREAM-PROVENANCE.json"))
                self.assertFalse(provenance["locally_compiled"])
                self.assertEqual(len(provenance["sources"]), 4)
                self.assertIsNone(z.testzip())
            with zipfile.ZipFile(source) as z:
                self.assertEqual(len(json.loads(z.read("sources.json"))), 4)
                self.assertIn("BUILD-SOURCE.txt", z.namelist())


if __name__ == "__main__":
    unittest.main()
