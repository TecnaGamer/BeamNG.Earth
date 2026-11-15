"""Prevent the public NBd generic N category being treated as an automobile face."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('audit_nbd', Path(__file__).resolve().parents[2]/'tools/audit_nbd.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class Types(unittest.TestCase):
    def test_bicycle(self):
        self.assertEqual(audit.classify('NFF'), {'traffic':'bicycle','support':'small_post'})
        self.assertEqual(audit.classify('VFG')['support'], 'mushroom_base')

    def test_shared_supports(self):
        self.assertEqual(audit.classify('TLA')['support'], 'signal_post')
        self.assertEqual(audit.classify('VEB')['support'], 'signal_cantilever')
        self.assertEqual(audit.classify('VRP')['traffic'], 'road_guide')

    def test_unknown_is_not_automobile(self):
        for code in (None, '', 'N', 'NBA', 'XXX', 'N?F'):
            self.assertEqual(audit.classify(code)['traffic'], 'unknown')
