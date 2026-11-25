"""Private stock preparation never runs during streaming or when disabled."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch
import zipfile

import cliffstock
import install


class StockCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "game"
        self.user = self.root / "user"
        self.native = Mock(TEXDDS_ID="test-encoder")
        self.native.cliff_contact_maps.return_value = (b"DDS coverage", b"DDS threshold")
        self.native.cliff_contact_color_factor.return_value = [2., 3., 4., 1.]
        self.native.cliff_ground_maps.return_value = (b"DDS detail", b"DDS ao", b"DDS roughness")
        self.messages = []

    def archive(self, height=b"height"):
        path = self.game / "content/assets/materials/terrain.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for suffix in cliffstock.COPIES:
                archive.writestr(cliffstock.PREFIX + suffix, b"DDS " + suffix.encode())
            archive.writestr(cliffstock.PREFIX + "_h.png", height)
            archive.writestr(cliffstock.PREFIX + "_b.png", b"color")
            archive.writestr(cliffstock.PREFIX + "_ao.png", b"ao")
            archive.writestr(cliffstock.PREFIX + "_r.png", b"roughness")

    def prepare(self, enabled=True):
        return cliffstock.prepare(self.game, self.user, enabled=enabled, native=self.native,
                                  diagnostic=self.messages.append)

    def test_disabled_and_missing_stock_do_not_generate(self):
        self.assertIsNone(self.prepare(False))
        self.assertEqual(self.messages, [])
        self.assertIsNone(self.prepare())
        self.assertIn("contact decoration unavailable", self.messages[0])
        self.native.cliff_contact_maps.assert_not_called()
        self.assertFalse(self.user.exists())

    def test_only_master_opt_in_prepares_stock_assets(self):
        for natural, cuts in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(natural=natural, cuts=cuts), patch.object(cliffstock, "prepare") as prepare, \
                    patch.object(cliffstock, "prepare_ground") as ground, \
                    patch.object(cliffstock, "prepare_models") as models:
                resolve = Mock(return_value=self.game)
                env = {"BNE_CLIFFS": str(int(natural)), "BNE_CLIFF_ROAD_CUTS": str(int(cuts))}
                cliffstock.prepare_enabled(resolve, self.user, env=env)
                self.assertEqual(prepare.call_count, int(natural))
                self.assertEqual(resolve.call_count, int(natural))
                self.assertEqual(ground.call_count, int(natural))
                self.assertEqual(models.call_count, int(natural))
        with patch.object(cliffstock, "prepare") as prepare:
            cliffstock.prepare_enabled(None, self.user, env={})
            prepare.assert_not_called()

    def test_ground_cache_is_independent_and_repairs_missing_maps(self):
        self.archive()
        target = cliffstock.prepare_ground(self.game, self.user, enabled=True, native=self.native)
        self.assertEqual(target.name, "cliff_ground_v1")
        self.assertEqual((target / "detail.data.dds").read_bytes(), b"DDS detail")
        cliffstock.prepare_ground(self.game, self.user, enabled=True, native=self.native)
        self.native.cliff_ground_maps.assert_called_once()
        self.native.cliff_contact_maps.assert_not_called()
        (target / "ao.data.dds").unlink()
        cliffstock.prepare_ground(self.game, self.user, enabled=True, native=self.native)
        self.assertEqual(self.native.cliff_ground_maps.call_count, 2)

    def test_cached_install_repairs_damage_and_invalidates_changed_inputs(self):
        self.archive()
        target = self.prepare()
        self.assertIsNotNone(target)
        self.assertEqual(self.prepare(), target)
        self.assertEqual(self.native.cliff_contact_maps.call_count, 1)
        (target / "coverage.data.dds").write_bytes(b"damaged")
        self.prepare()
        self.assertEqual(self.native.cliff_contact_maps.call_count, 2)
        self.archive(b"new-height")
        self.prepare()
        self.assertEqual(self.native.cliff_contact_maps.call_count, 3)
        self.native.TEXDDS_ID = "new-encoder"
        self.prepare()
        self.assertEqual(self.native.cliff_contact_maps.call_count, 4)
        self.assertEqual(self.messages, [])

    def test_material_uses_private_dds_and_opaque_cutouts(self):
        self.archive()
        target = self.prepare()
        material = json.loads((target / "contact.materials.json").read_text())[cliffstock.MATERIAL]
        self.assertTrue(material["alphaTest"])
        self.assertFalse(material["translucent"])
        self.assertEqual(material["alphaRef"], 224)
        stage = material["Stages"][0]
        self.assertEqual(stage["baseColorFactor"], [2., 3., 4., 1.])
        for key, value in stage.items():
            if key.endswith("Map"):
                self.assertTrue(value.startswith(cliffstock.VIRTUAL_ROOT))
                self.assertTrue((target / Path(value).name).exists())

    def test_incomplete_stamp_cannot_hide_missing_outputs(self):
        self.archive()
        target = self.prepare()
        stamp_file = target / "sources.json"
        stamp = json.loads(stamp_file.read_text())
        del stamp["outputs"]["normal.dds"]
        stamp_file.write_text(json.dumps(stamp))
        (target / "normal.dds").unlink()
        self.prepare()
        self.assertEqual(self.native.cliff_contact_maps.call_count, 2)
        self.assertTrue((target / "normal.dds").exists())


class InstallerContactTest(unittest.TestCase):
    def test_source_start_prepares_each_enabled_configuration_before_streaming(self):
        import launcher
        import start
        for natural,cuts in ((False,False),(False,True),(True,False),(True,True)):
            events = []
            enabled = natural
            env = {'BNE_CLIFFS': str(int(natural)), 'BNE_CLIFF_ROAD_CUTS': str(int(cuts))}
            def prepare(*args, **kwargs):
                events.append('prepared')
            def run(cmd, path, child_env):
                self.assertEqual(child_env['BNE_CLIFFS'],env['BNE_CLIFFS'])
                self.assertEqual(child_env['BNE_CLIFF_ROAD_CUTS'],env['BNE_CLIFF_ROAD_CUTS'])
                if enabled:
                    self.assertEqual(events[-1],'prepared')
                events.append('started')
                return start._srvrestart().EXIT_CODE if events.count('started') == 1 else 0
            with self.subTest(natural=natural,cuts=cuts), tempfile.TemporaryDirectory() as user, \
                    patch.object(launcher,'game_install_dir',return_value='stock-game') as locate, \
                    patch.object(cliffstock,'prepare',side_effect=prepare) as contact, \
                    patch.object(cliffstock,'prepare_ground') as ground, \
                    patch.object(start,'server_log_path',return_value=None), patch.object(install,'say'):
                self.assertEqual(start.run_restarting(['server'],user,env,run=run),0)
            self.assertEqual(locate.call_count,2 if enabled else 0)
            self.assertEqual(contact.call_count,2 if enabled else 0)
            self.assertEqual(ground.call_count,2 if enabled else 0)
            self.assertEqual(events, ['prepared','started','prepared','started'] if enabled else ['started','started'])

    def test_launcher_prepares_before_each_child_with_server_environment(self):
        import launcher
        events = []
        def prepared(game, user, **kwargs):
            self.assertEqual(kwargs['env']['BNE_CLIFFS'], '1')
            self.assertEqual(kwargs['env']['BNE_CLIFF_ROAD_CUTS'], '0')
            events.append('prepared')
        def launched(*args, **kwargs):
            self.assertEqual(events[-1], 'prepared')
            events.append('launched')
            return Mock()
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'BNE_CLIFFS': '0', 'BNE_CLIFF_ROAD_CUTS': '0',
                                       'BNGE_SERVER_ENV': 'BNE_CLIFFS=1'}), \
                patch.object(launcher, 'game_install_dir', return_value='stock-game'), \
                patch.object(launcher, 'experimental_env'), patch.object(launcher, 'restart_env'), \
                patch.object(launcher.threading, 'Thread'), \
                patch.object(cliffstock, 'prepare_enabled', side_effect=prepared), \
                patch.object(launcher.subprocess, 'Popen', side_effect=launched):
            for times in ((), (123.,)):
                launcher.start_server(folder, [], str(Path(folder)/'server.log'), restart_times=times)
        self.assertEqual(events, ['prepared','launched','prepared','launched'])

    def test_installer_prepares_cached_install_but_respects_dry_run(self):
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                args = ["install.py", "--no-venv", "--game-dir", "stock-game"] + (["--dry-run"] if dry else [])
                with patch.object(sys, "argv", args), patch.object(install, "choose_user_dir", return_value="user"), \
                        patch.object(install, "install_mod", return_value="current"), \
                        patch.object(install, "save_config"), patch.object(install, "say"), \
                        patch.object(cliffstock, "prepare_enabled") as prepare:
                    install.main()
                if dry:
                    prepare.assert_not_called()
                else:
                    self.assertEqual(prepare.call_args.args, ("stock-game", "user"))
