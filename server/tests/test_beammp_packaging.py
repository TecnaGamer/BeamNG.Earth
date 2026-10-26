"""The server update must deliver its restore runtime without replacing player data."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import package


class BeamMPPackageTest(unittest.TestCase):
    def test_restore_runtime_and_bootstrap_ship_without_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            package.beammp_zip(tmp, 'test')
            with zipfile.ZipFile(Path(tmp) / 'BeamNG.Earth-BeamMP-Server-test.zip') as outer:
                self.assertEqual(set(outer.namelist()), {
                    'Resources/Server/BeamNGEarth/main.lua',
                    'Resources/Client/beamng_earth.zip', 'BeamNG.Earth-README.md'})
                self.assertEqual(outer.read('Resources/Server/BeamNGEarth/main.lua'),
                                 (ROOT / 'beammp/Server/BeamNGEarth/main.lua').read_bytes())
                with zipfile.ZipFile(io.BytesIO(outer.read('Resources/Client/beamng_earth.zip'))) as client:
                    bootstrap = 'scripts/beamng_earth_server/modScript.lua'
                    self.assertEqual(client.read(bootstrap), (ROOT / 'beammp/Client' / bootstrap).read_bytes())
                    for name in ('core', 'mpsession', 'mpidentity', 'resume', 'resumevehicle', 'runtime', 'trees', 'settings'):
                        path = f'lua/ge/extensions/beamngEarth/{name}.lua'
                        self.assertEqual(client.read(path), (ROOT / 'mod/beamng_earth' / path).read_bytes())
                    self.assertFalse(any('bnge_player_positions' in p or 'beamngEarthMPGuests' in p
                                         for p in client.namelist()))


if __name__ == '__main__':
    unittest.main()
