"""Frozen HTTPS must work, not only the launcher's argument parser."""
import importlib.util
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import build_app


class HttpsPackagingTest(unittest.TestCase):
    def test_runtime_policy_is_packaged_beside_ssl_and_context_stays_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(build_app.subprocess, 'run') as run:
                build_app.pyinstaller(temp, 'icon.ico', onefile=True, ver='1.6.3-test')
            command = run.call_args.args[0]
            bundle = Path(temp) / 'bundle'
            bundle.mkdir()
            # Recreate ssl.py's frozen location using the production add-data
            # arguments. Previously this constructor failed before any socket.
            ssl_file = bundle / 'ssl.py'
            shutil.copyfile(ssl.__file__, ssl_file)
            packaged = set()
            for i, arg in enumerate(command[:-1]):
                if arg != '--add-data':
                    continue
                source, destination = command[i + 1].split(build_app.SEP, 1)
                if Path(source).name in build_app.SSL_SUPPORT_FILES:
                    target = bundle / destination / Path(source).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    packaged.add(Path(source).name)
            for name in build_app.SSL_SUPPORT_FILES:
                if (Path(ssl.__file__).parent / name).exists():
                    self.assertIn(name, packaged)
            name = '_bnge_frozen_ssl_test'
            spec = importlib.util.spec_from_file_location(name, ssl_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                context = module.create_default_context()
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            finally:
                sys.modules.pop(name, None)

    def test_broken_build_runtime_fails_before_pyinstaller(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(build_app.ssl, 'create_default_context', side_effect=FileNotFoundError('TLS policy')), \
                    patch.object(build_app.subprocess, 'run') as run:
                with self.assertRaises(FileNotFoundError):
                    build_app.pyinstaller(temp, 'icon.ico', onefile=True, ver='1.6.3-test')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
