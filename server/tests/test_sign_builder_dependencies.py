"""The data repo's curl-only sign build must keep working without app artwork/native code."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class BuilderDependencyTest(unittest.TestCase):
    def test_weekly_downloaded_modules_import_and_index_through_labels(self):
        root = Path(__file__).resolve().parents[2]
        # BeamNG.Earth-data/.github/workflows/rail.yml, signs job.
        files = ('tools/npyfirst.py', 'server/npyguard.py', 'tools/build_sign_tags.py',
                 'tools/build_rail_tags.py', 'server/exitsigns.py', 'server/throughsigns.py',
                 'server/milemarkers.py', 'server/routeshields.py', 'server/signshards.py')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name in files:
                shutil.copyfile(root / name, path / Path(name).name)
            (path / 'dmath.py').write_text('import math\ndef __getattr__(n): return getattr(math,n)\n')
            (path / 'dnp.py').write_text('import numpy as np\ndef __getattr__(n): '
                                       'return getattr(np,n) if hasattr(np,n) else getattr(np.linalg,n)\n')
            code = ("import build_sign_tags,throughsigns; "
                    "label=throughsigns.label(dict(highway='motorway',oneway='yes',ref='I 80',"
                    "destination='Laramie'),[dict(network='US:I',ref='80',direction='west')]); "
                    "assert label['routes']==['I 80'] and label['direction']=='WEST'; "
                    "print('curl-only builder modules and through-label indexing passed')")
            env = dict(os.environ, PYTHONPATH='')
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=path, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
