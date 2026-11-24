"""Compiled stock models stay private and retain authored mesh/LOD/collision bytes."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile
import numpy as np
import cdae
import cliffstock
import fast

class CompiledStockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.game, self.user = self.root/'game', self.root/'user'
        self.native = Mock(wraps=fast._ef)
        self.messages = []
        p = np.array([[0.,0,0],[1,0,0],[0,1,0]])
        self.plain = cdae.encode(p, [[0,1,2]], material='utah_rocks_b', packed=False,
                                 collisions=[(p, [[0,1,2]], None)])
        for level,names in cliffstock.MODEL_SOURCES.items():
            path = self.game/'content/levels'/f'{level}.zip'
            path.parent.mkdir(parents=True,exist_ok=True)
            with zipfile.ZipFile(path,'w') as z:
                for name in names:
                    z.writestr(f'levels/{level}/art/shapes/rocks/{name}.cdae',self.plain)
    def prepare(self, enabled=True):
        return cliffstock.prepare_models(self.game,self.user,enabled=enabled,native=self.native,
                                         diagnostic=self.messages.append)
    def test_preserves_bytes_and_validates_cache(self):
        target = self.prepare()
        self.assertIsNotNone(target,self.messages)
        count = sum(map(len,cliffstock.MODEL_SOURCES.values()))
        self.assertEqual(self.native.cdae_unpack.call_count,count)
        manifest=json.loads((target/'models.json').read_text())
        for name,spec in manifest['models'].items():
            data=(target/(name+'.cdae')).read_bytes()
            self.assertEqual(data,self.plain)
            self.assertEqual(spec['bytes'],len(data))
            for offset,material in spec['slots']:
                self.assertEqual(data[offset-1:offset-1+len(material)].decode(),material)
        self.assertEqual(self.prepare(),target)
        self.assertEqual(self.native.cdae_unpack.call_count,count)
        (target/'utah_boulder_1.cdae').write_bytes(b'broken')
        self.assertEqual(self.prepare(),target)
        self.assertEqual((target/'utah_boulder_1.cdae').read_bytes(),self.plain)
        self.assertEqual(self.native.cdae_unpack.call_count,count*2)
    def test_disabled_missing_and_invalid_models(self):
        self.assertIsNone(self.prepare(False))
        self.assertFalse(self.user.exists())
        (self.game/'content/levels/Utah.zip').unlink()
        self.assertIsNone(self.prepare())
        self.assertIn('compiled rock models unavailable',self.messages[-1])
        self.assertFalse(self.user.exists())
        with self.assertRaises(ValueError): cliffstock.model_slots(self.plain[:-1])

if __name__=='__main__':unittest.main()
