"""Near/far photo processing stays isolated even when they share cached sources."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import imagery, farfield, earthd


class Deshadow:
    tone_lift=0
    source='usgs'
    mode='full'
    version=1
    def bind(self, im): pass
    def relight_final(self, key, tile, im):
        return np.full_like(tile.rgb,180),'final'


class PhotoShadowsTest(unittest.TestCase):
    def test_shared_raw_but_distinct_finished_pixels_and_cache_paths(self):
        with tempfile.TemporaryDirectory() as d:
            im=imagery.Imagery(d,chain=['usgs'],deshadow=Deshadow())
            try:
                key=(imagery.SOURCES['usgs'],8,10,10)
                tile=SimpleNamespace(rgb=np.full((256,256,3),65,np.uint8),uniform=True)
                im.tiles=lambda keys: {k:tile for k in keys}
                far=im.with_shadows()
                self.assertIs(far,im.with_shadows())
                self.assertIs(far.pool,im.pool)
                self.assertIs(far._lru,im._lru)
                self.assertIsNot(far._fin,im._fin)
                self.assertNotEqual(far._fin_path(key),im._fin_path(key))
                near=im.finished([key])[key]
                distant=far.finished([key])[key]
                self.assertTrue(near.relit)
                self.assertFalse(distant.relit)
                self.assertGreater(np.mean(near.data),np.mean(distant.data))
                self.assertIs(im.finished([key])[key],near)
                self.assertIs(far.finished([key])[key],distant)
                self.assertIsNotNone(im.deshadow)
            finally:
                im.pool.shutdown();im.urgent_pool.shutdown()

    def test_shadow_transition_keeps_near_and_far_endpoints(self):
        s=earthd.Server.__new__(earthd.Server)
        s.tile_where=lambda a,t: (None,0,0,t.size)
        shade=SimpleNamespace(render=Mock(return_value=(np.full((16,16,3),50,np.uint8),{'usgs':256})))
        im=SimpleNamespace(deshadow=object(),with_shadows=lambda:shade,
            render=Mock(return_value=(np.full((16,16,3),180,np.uint8),{'usgs':256,'deshadow':256})))
        s.imagery=im
        values=[]
        for level in (0,1,2,3,7):
            rgb,used=s.render_tile(None,farfield.Tile(level,0,0),16,set())
            values.append(float(rgb.mean()))
            self.assertEqual(used['usgs'],256)
        self.assertEqual(values[0],180)
        self.assertEqual(values[-1],50)
        self.assertEqual(values[-2],50)
        self.assertTrue(values[0]>values[1]>values[2]>values[3])
        self.assertEqual(im.render.call_count,3,'fully distant renders must not run near de-shadowing')

if __name__=='__main__': unittest.main()
