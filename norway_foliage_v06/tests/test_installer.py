"""Norway v0.6 installer safety and migration tests."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest


PACK=Path(__file__).resolve().parents[1]
WORK=PACK.parent
SCRIPT=PACK/'apply.py'
VEG=Path('server/vegetation.py')
TREES=Path('mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua')
SR16=Path('server/norway_sr16.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestV06Install(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for relative,source in ((VEG,WORK/'upload/vegetation.py'),(TREES,WORK/'upload/trees.lua')):
            (self.root/relative).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,self.root/relative)
        (self.root/'server/grading.py').write_text('USER ROAD GRADING',encoding='utf-8')

    def run_script(self,script,*args,good=True):
        r=subprocess.run([sys.executable,str(script),*args],cwd=self.root,
                         capture_output=True,text=True)
        if good:
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        else:
            self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
        return r.stdout+r.stderr

    def test_install_undo_twice(self):
        before={p:sha(self.root/p) for p in (VEG,TREES)}
        self.run_script(SCRIPT,'--check')
        self.run_script(SCRIPT,'--install')
        self.assertEqual(sha(self.root/VEG),sha(PACK/VEG))
        self.assertEqual(sha(self.root/TREES),sha(PACK/TREES))
        self.assertFalse((self.root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/norway_foliage').exists())
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(before,{p:sha(self.root/p) for p in (VEG,TREES)})
        self.assertFalse((self.root/SR16).exists())
        self.assertEqual((self.root/'server/grading.py').read_text(),'USER ROAD GRADING')
        self.run_script(SCRIPT,'--install')
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(before,{p:sha(self.root/p) for p in (VEG,TREES)})

    def test_refuses_unknown_source(self):
        (self.root/TREES).write_text('changed custom tree code',encoding='utf-8')
        self.run_script(SCRIPT,'--install',good=False)
        self.assertFalse((self.root/'norway_foliage_v06_backup').exists())

    def test_accepts_old_sr16_and_old_lua(self):
        shutil.copy2(WORK/'norway_foliage_v02/server/vegetation.py',self.root/VEG)
        shutil.copy2(WORK/'norway_foliage_v01/mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua',self.root/TREES)
        shutil.copy2(WORK/'norway_foliage_v02/server/norway_sr16.py',self.root/SR16)
        before={p:sha(self.root/p) for p in (VEG,TREES,SR16)}
        self.run_script(SCRIPT,'--install')
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(before,{p:sha(self.root/p) for p in (VEG,TREES,SR16)})

    def test_v03_must_undo_first_then_v06_migrates(self):
        v03=WORK/'norway_foliage_v03/apply.py'
        original={p:sha(self.root/p) for p in (VEG,TREES)}
        self.run_script(v03,'--install')
        self.run_script(SCRIPT,'--install',good=False)
        self.run_script(v03,'--undo')
        self.run_script(SCRIPT,'--install')
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(original,{p:sha(self.root/p) for p in (VEG,TREES)})

    def test_direct_upgrade_from_v04_and_reversible_layers(self):
        v04=WORK/'norway_foliage_v04/apply.py'
        baseline={p:sha(self.root/p) for p in (VEG,TREES)}
        self.run_script(v04,'--install')
        old={p:sha(self.root/p) for p in (VEG,TREES,SR16)}
        self.run_script(SCRIPT,'--install')
        self.assertNotEqual(sha(self.root/VEG),old[VEG])
        self.assertNotEqual(sha(self.root/TREES),old[TREES])
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(old,{p:sha(self.root/p) for p in (VEG,TREES,SR16)})
        self.run_script(v04,'--undo')
        self.assertEqual(baseline,{p:sha(self.root/p) for p in (VEG,TREES)})

    def test_direct_upgrade_from_v05_then_undo_v05(self):
        v05=WORK/'norway_foliage_v05/apply.py'
        baseline={p:sha(self.root/p) for p in (VEG,TREES)}
        self.run_script(v05,'--install')
        old={p:sha(self.root/p) for p in (VEG,TREES,SR16)}
        self.run_script(SCRIPT,'--check')
        self.run_script(SCRIPT,'--install')
        self.assertNotEqual(sha(self.root/VEG),old[VEG])
        self.assertNotEqual(sha(self.root/TREES),old[TREES])
        self.run_script(SCRIPT,'--undo')
        self.assertEqual(old,{p:sha(self.root/p) for p in (VEG,TREES,SR16)})
        self.assertTrue((self.root/'norway_foliage_v05_backup/manifest.json').is_file())
        self.run_script(v05,'--undo')
        self.assertEqual(baseline,{p:sha(self.root/p) for p in (VEG,TREES)})


if __name__=='__main__':
    unittest.main()
