"""Call fixtures preserve bytes, types and order and never share the oracle's writable state."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools' / 'portcheck'))
import replay


class ReplayTest(unittest.TestCase):
    def test_first_difference(self):
        cases = [
            ({'rows': [1, False]}, {'rows': [1, 0]}, "['rows'][1] type"),
            ({'a': 1, 'b': 2}, {'b': 2, 'a': 1}, '.keys[0]'),
            ([0.0], [-0.0], '[0]'),
            ((b'abc',), (b'abd',), '[0] byte 2'),
            (np.array([[0., -0.]]), np.zeros((1, 2)), '(0, 1) byte'),
        ]
        for expected, actual, location in cases:
            with self.subTest(location=location):
                self.assertIn(location, replay.difference(expected, actual))

    def test_candidate_rejects_single_changed_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / 'frozen'
            cache.mkdir()
            (cache / 'seed').write_bytes(b'pristine')
            for name, delta in [('base', 0), ('candidate', 0), ('changed', 1)]:
                tree = root / name
                tree.mkdir()
                (tree / 'kernel.py').write_text(
                    'def evaluate(x):\n'
                    f'    result = x.copy()\n    result[1, 0] += {delta}\n'
                    '    return {"array": result, "raw": b"\\x00abc", "scalar": x[0,0]}\n')
            adapter = root / 'adapter.py'
            adapter.write_text(
                'import numpy as np\nfrom kernel import evaluate\n'
                'def invoke(x, cache):\n'
                '    assert (cache / "seed").read_bytes() == b"pristine"\n'
                '    (cache / "seed").write_bytes(b"used")\n'
                '    return evaluate(x)\n'
                'def capture(record, cache):\n'
                '    x = np.arange(4, dtype="<i4").reshape(2, 2)\n'
                '    record("production", x, invoke(x, cache))\n')
            def run(mode, tree):
                return subprocess.run(
                    [sys.executable, str(ROOT / 'tools/portcheck/replay.py'), mode,
                     '--adapter', str(adapter), '--server-dir', str(root / tree),
                     '--cache', str(cache), '--cases', str(root / 'cases'), '--work', str(root / 'work')],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for tree in ('base', 'candidate', 'changed'):
                (root / tree / 'npyguard.py').write_text('def apply(): pass\n')
            captured = run('capture', 'base')
            self.assertEqual(captured.returncode, 0, captured.stdout)
            candidate = run('replay', 'candidate')
            self.assertEqual(candidate.returncode, 0, candidate.stdout)
            changed = run('replay', 'changed')
            self.assertNotEqual(changed.returncode, 0)
            for field in ("'production'", 'input=', 'call.json', "['array'](1, 0) byte 0"):
                self.assertIn(field, changed.stdout)
            self.assertEqual((cache / 'seed').read_bytes(), b'pristine')
            self.assertNotEqual(run('capture', 'candidate').returncode, 0)

    def test_hook_freezes_before_mutation(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = replay.Recorder(root)
            array = np.array([-0.0])
            def mutate(x):
                x[0] = 1.
                return x
            self.assertIs(record.call('mutating', mutate, array), array)
            call = json.loads((root / '0/call.json').read_bytes())
            with np.load(root / '0/arrays.npz', allow_pickle=False) as arrays:
                args, _ = replay.unpack(call['input'], arrays, root / '0')
                result = replay.unpack(call['output'], arrays, root / '0')
            self.assertTrue(np.signbit(args[0][0]))
            self.assertEqual(result[0], 1.)

    def test_no_pickle(self):
        with self.assertRaises(TypeError):
            replay.pack(np.array([object()], dtype=object), {}, {})
