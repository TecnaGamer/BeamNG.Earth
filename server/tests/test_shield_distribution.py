"""Player packages include automatic shields, without manually installed inventories."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import package

class DistributionTest(unittest.TestCase):
    def test_runtime_metadata_artwork_and_docs_ship(self):
        files=set(package.collect())
        for name in ('server/streamedroutes.py','server/routefaces.py',
                     'server/data/shields/route_relations.sqlite.gz',
                     'server/data/shields/wy.png','server/data/shields/networks.json',
                     'server/data/shields/NOTICE.md','docs/nationwide-highway-shields.md'):
            self.assertIn(name,files)
        self.assertFalse(any('signs.v3.sqlite' in name or 'wyoming_route_shields.json' in name for name in files))

if __name__=='__main__': unittest.main()
