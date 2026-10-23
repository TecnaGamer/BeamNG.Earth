"""Exercise file-backed server assets with the frozen app's data layout."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import exitsigns
import milemarkers
from tools import build_app


def frozen_data(tmp):
    with mock.patch.object(build_app.subprocess, "run") as run:
        build_app.pyinstaller(tmp, "icon.ico", onefile=True, ver="0.0.0")
    command = run.call_args.args[0]
    bundle = Path(tmp) / "bundle"
    bundle.mkdir()
    for i, arg in enumerate(command[:-1]):
        if arg != "--add-data":
            continue
        source, destination = command[i + 1].split(build_app.SEP, 1)
        if Path(source).suffix in (".json", ".geojson"):
            target = bundle / destination / Path(source).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return bundle


class PackagedExitSignsTest(unittest.TestCase):
    def test_bundled_lettering_builds_sign_mesh(self):
        # Populate a fake extraction folder from the actual PyInstaller command.
        # The source checkout has this JSON even when the download omits it.
        with tempfile.TemporaryDirectory() as tmp:
            bundle = frozen_data(tmp)

            with mock.patch.object(exitsigns, "__file__", str(bundle / "exitsigns.pyc")):
                exitsigns.lettering.cache_clear()
                try:
                    label = {"ref": "36", "names": ["Auburn"], "routes": []}
                    signs = []
                    for kind, distance in (("ramp", ""), ("advance", "1 MILE")):
                        width, height = exitsigns.dimensions(label, kind)
                        signs.append({"kind": kind, "label": label, "distance": distance,
                                      "centre": [0., 0., 0.], "travel": [1., 0.],
                                      "width": width, "height": height, "bottom": 2., "side": 1})
                    positions, triangles, uvs, materials, indices = exitsigns.build(signs, (0., 0., 0.))
                    self.assertEqual(uvs.shape, (len(positions), 2))
                    self.assertTrue(np.isfinite(positions).all())
                    self.assertGreater(len(triangles), 0)
                    self.assertTrue(np.any(indices == materials.index("bne_exit_letters")))
                finally:
                    exitsigns.lettering.cache_clear()




class PackagedMileMarkersTest(unittest.TestCase):
    def test_reference_snapshot_and_lettering_are_in_the_frozen_data_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = frozen_data(tmp)
            with mock.patch.object(milemarkers,'DATA',bundle/'mile_markers_us.json'), \
                 mock.patch.object(exitsigns,'__file__',str(bundle/'exitsigns.pyc')):
                milemarkers.references.cache_clear()
                exitsigns.lettering.cache_clear()
                try:
                    references,_ = milemarkers.references()
                    obj = next(o for o in references if o['mile_marker']['state']=='MI' and
                               o['mile_marker']['route']==69 and o['mile_marker']['mile']==116 and
                               o['mile_marker']['direction']=='W')
                    sign = dict(centre=[0.,0.,0.],travel=[-1.,0.],width=milemarkers.WIDTH,
                                height=milemarkers.HEIGHT,bottom=1.15,direction='WEST',route=69,mile=116)
                    self.assertEqual(obj['mile_marker']['source'],'dot:mi')
                    self.assertIsNotNone(milemarkers.build([sign],(0.,0.,0.)))
                finally:
                    milemarkers.references.cache_clear()
                    exitsigns.lettering.cache_clear()


if __name__ == "__main__":
    unittest.main()
