"""Offline tests for the NVDB real-position sign pilot. No game or network needed."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch
import math
import numpy as np

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SERVER))
import norway_nvdb_signs_v04 as no


class FakeFrame:
    def geodetic_to_enu(self,lat,lon,z):
        lat,lon=np.asarray(lat,dtype=float),np.asarray(lon,dtype=float)
        return (lon-7.67)*50800.0,(lat-62.89)*111320.0,np.asarray(z,dtype=float)

    def enu_to_geodetic(self,x,y,z):
        x,y=np.asarray(x,dtype=float),np.asarray(y,dtype=float)
        return y/111320.0+62.89,x/50800.0+7.67,np.asarray(z,dtype=float)


class TestSigns(TestCase):
    def setUp(self):
        no.records.cache_clear()
        self.frame=FakeFrame()
        self.items=no.projected(self.frame)
        self.box=(-1000.,-1000.,1000.,1000.)
        self.sys_row={'atlas':'vienna_w','mount':[2.2,1.5]}

    def test_eight_unique_real_signs(self):
        self.assertEqual(len(self.items),8)
        self.assertEqual(len(set(x[0] for x in self.items)),8)

    def test_physical_offsets_are_reasonable(self):
        for _id,q,ref,face in self.items:
            dist=np.linalg.norm(q-ref)
            self.assertGreaterEqual(dist,no.MIN_OFFSET_M)
            self.assertLessEqual(dist,no.MAX_OFFSET_M)
            self.assertAlmostEqual(float(np.linalg.norm(face)),1.,places=6)
            self.assertGreater(float(np.dot(face,q-ref)),0.)

    def test_no_global_sign_spam(self):
        self.assertIsNone(self._build(box=(20000,20000,20500,20500)))

    def test_half_open_tile_ownership(self):
        self.assertTrue(no.in_box(10,20,(10,20,100,100)))
        self.assertFalse(no.in_box(100,20,(10,20,100,100)))
        self.assertFalse(no.in_box(10,100,(10,20,100,100)))

    def test_off_by_default(self):
        with patch.dict(os.environ,{no.ENV:'off'}):
            self.assertIsNone(self._build())

    def test_real_sign_mounted_on_tiny_native_mesh(self):
        with patch.dict(os.environ,{no.ENV:'on'}):
            mesh=self._build()
        self.assertIsNotNone(mesh)
        self.assertEqual(mesh[3],['bne_signs_vienna_w','bne_exit_metal'])
        table=self.captured
        self.assertEqual(table.shape,(8,10))
        self.assertTrue(np.all(table[:,7] == 2.))  # triangle pointed down
        self.assertTrue(np.all(table[:,8] == .9))
        self.assertTrue(np.all(table[:,9] == 1.5))
        self.assertTrue(np.isfinite(table).all())
        for row,(_id,q,ref,face) in zip(table,self.items):
            self.assertAlmostEqual(float(row[4]),-float(face[0]))
            self.assertAlmostEqual(float(row[5]),-float(face[1]))

    def test_all_decks_prevent_signs(self):
        with patch.dict(os.environ,{no.ENV:'on'}):
            self.assertIsNone(self._build(on_deck=lambda *args: True))

    def test_explicit_deck_filter_not_ground_terrain(self):
        with patch.dict(os.environ,{no.ENV:'on'}):
            mesh=self._build(on_deck=lambda xy,*args: xy[0]>450)
        self.assertIsNotNone(mesh)
        self.assertLess(len(self.captured),8)

    def test_no_other_signs_suppressed(self):
        q=self.items[0][1]
        rows=np.zeros((3,11))
        rows[0,:2]=q
        rows[0,4]=20 # yield
        rows[1,:2]=q
        rows[1,4]=21 # STOP
        rows[2,:2]=q+np.array([300,300])
        rows[2,4]=20
        with patch.dict(os.environ,{no.ENV:'on'}), self._fake_libs():
            got=no.suppress_inferred(rows,self.frame)
        self.assertEqual(len(got),2)
        self.assertEqual(list(got[:,4]),[21.0,20.0])

    def test_furniture_only_yield_is_suppressed(self):
        q=self.items[0][1]
        rows=[['giveway',float(q[0]),float(q[1]),0.,0.,1.,3.,0.],
              ['stop',float(q[0]),float(q[1]),0.,0.,1.,3.,0.],
              ['giveway',5000.,5000.,0.,0.,1.,3.,0.]]
        with patch.dict(os.environ,{no.ENV:'on','BNE_ROADSIGNS':'on'}):
            got=no.suppress_furniture_yields(rows,self.frame)
        self.assertEqual(len(got),2)
        self.assertEqual(got[0][0],'stop')
        self.assertEqual(got[1][0],'giveway')

    def test_furniture_never_suppressed_when_road_sign_renderer_off(self):
        q=self.items[0][1]
        rows=[['giveway',float(q[0]),float(q[1]),0.,0.,1.,3.,0.]]
        with patch.dict(os.environ,{no.ENV:'on','BNE_ROADSIGNS':'off'}):
            self.assertEqual(no.suppress_furniture_yields(rows,self.frame),rows)

    def test_furniture_off_keeps_all_rows(self):
        q=self.items[0][1]
        rows=[['giveway',float(q[0]),float(q[1]),0.,0.,1.,3.,0.]]
        with patch.dict(os.environ,{no.ENV:'off'}):
            self.assertEqual(no.suppress_furniture_yields(rows,self.frame),rows)

    def test_when_off_no_inferred_signs_suppressed(self):
        q=self.items[0][1]
        rows=np.zeros((1,11))
        rows[0,:2]=q;rows[0,4]=20
        with patch.dict(os.environ,{no.ENV:'off'}), self._fake_libs():
            got=no.suppress_inferred(rows,self.frame)
        self.assertEqual(len(got),1)

    def test_malformed_offset_rejected(self):
        pts=[(10001,62.893,7.679,62.893,7.679)]
        self.assertEqual(no.projected(self.frame,pts),[])

    def test_signs_always_upright_and_grounded(self):
        with patch.dict(os.environ,{no.ENV:'on'}):
            self._build()
        self.assertTrue(np.allclose(self.captured[:,2:4],0.0))

    def _fake_libs(self,on_deck=None):
        import contextlib
        @contextlib.contextmanager
        def fake():
            fake_roles={'cols':8,'rows':16}
            fake_systems={'rules':(['unused']*20)+['prio.give_way','prio.stop'],
                          'sizes':{'default':{'tri_down':[.75,.9,1.25]}}}
            fake_by_id={'prio.give_way':(2,{'shape':'tri_down'})}
            def renderer(table, cols,rows,origin):
                self.captured=np.array(table,copy=True)
                n=len(table)
                return np.zeros((3*n,3)),np.zeros((n,3),np.int64),np.zeros((3*n,2)),np.zeros(n,np.int64)
            mock_roadsigns=SimpleNamespace(tables=lambda:(fake_roles,fake_systems,fake_by_id),
                                            SHAPES={'tri_down':2},METAL='bne_exit_metal')
            mock_fast=SimpleNamespace(_ef=SimpleNamespace(road_sign_mesh=renderer))
            mock_exitsigns=SimpleNamespace(_on_deck=on_deck or (lambda *a:False))
            with patch.dict(sys.modules,{'roadsigns':mock_roadsigns,'fast':mock_fast,'exitsigns':mock_exitsigns}):
                yield
        return fake()

    def _build(self,box=None,on_deck=None):
        with self._fake_libs(on_deck):
            return no.build_tile(self.frame,self.box if box is None else box,
                                 lambda lat,lon: np.zeros_like(lat),[],self.sys_row)


if __name__=='__main__':
    main()
