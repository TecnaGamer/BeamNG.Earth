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

    def test_wrong_linux_binary_is_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "digest"):
                build_host.build(d, lambda url: b"wrong ELF", platform="linux")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_linux_has_executable_modes_and_same_source_payload(self):
        binary = b"\x7fELF test fixture"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("upstream/CMakeLists.txt", "fixture build source")
        def download(url):
            if url == build_host.LINUX_URL:
                return binary
            if url.endswith("/LICENSE"):
                return b"GNU AFFERO GENERAL PUBLIC LICENSE fixture"
            return archive.getvalue()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(build_host, "LINUX_SIZE", len(binary)), \
                mock.patch.object(build_host, "LINUX_SHA256", build_host.sha256(binary)):
            host, source = build_host.build(d, download, platform="linux")
            source_bytes = source.read_bytes()
            with zipfile.ZipFile(host) as z:
                self.assertEqual(len(z.namelist()), len(set(z.namelist())), "host members must be unique")
                self.assertEqual(z.read("BeamMP-Server"), binary)
                for name in ("BeamMP-Server", "run_host.sh"):
                    self.assertEqual(z.getinfo(name).external_attr >> 16, 0o100755)
                self.assertIn(b'exec ./BeamMP-Server "$@"', z.read("run_host.sh"))
                self.assertEqual(json.loads(z.read("UPSTREAM-PROVENANCE.json"))["platform"], "Linux-x86_64")
            build_host.build(d, download, platform="linux")
            self.assertEqual(source.read_bytes(), source_bytes, "source archive is reproducible across host builds")

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
                self.assertEqual(len(z.namelist()), len(set(z.namelist())), "host members must be unique")
                self.assertEqual(z.read("BeamMP-Server.exe"), binary)
                self.assertIn('AuthKey = ""', z.read("ServerConfig.toml").decode())
                self.assertIn('Private = true', z.read("ServerConfig.toml").decode())
                self.assertIn("Resources/Client/beamng_earth.zip", z.namelist())
                self.assertIn("Resources/Server/BeamNGEarth/main.lua", z.namelist())
                self.assertEqual(z.read("LICENSE-Earth.txt"), (build_host.ROOT / "LICENSE").read_bytes())
                self.assertEqual(z.read("CREDITS-Earth.md"), (build_host.ROOT / "CREDITS.md").read_bytes())
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
