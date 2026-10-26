"""Exercise file-backed server assets with the frozen app's data layout."""
import shutil
import json
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
import signstates
from tools import build_app, package


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
                exitsigns.guide_lettering.cache_clear()
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
                    exitsigns.guide_lettering.cache_clear()




class PackagedMileMarkersTest(unittest.TestCase):
    def test_state_styles_and_client_assets_are_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = frozen_data(tmp)
            with mock.patch.object(signstates, 'DATA', bundle / 'highway_marking_states.geojson'):
                signstates._polygons.cache_clear()
                try:
                    self.assertEqual(str(signstates.states(42.73, -84.55)), 'MI')
                    self.assertEqual(str(signstates.states(38.25, -85.76)), 'KY')
                finally:
                    signstates._polygons.cache_clear()
        client = set(package.tree_files('mod/beamng_earth'))
        scenery = 'levels/beamng_earth/art/shapes/beamngEarth/roadside/'
        for name in ('exit.materials.json', 'merge.materials.json', 'exit_letters_o.data.png',
                     'guide_letters_o.data.png', 'Roadgeek-LICENSE.txt'):
            self.assertIn(scenery + name, client)
        for name in ('exit.materials.json', 'merge.materials.json'):
            mats = json.loads((ROOT / 'mod/beamng_earth' / scenery / name).read_text())
            for mat in mats.values():
                for stage in mat.get('Stages', []):
                    for key, value in stage.items():
                        if isinstance(value, str) and value.endswith('.png'):
                            self.assertIn(value.lstrip('/'), client, (name, key))

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
