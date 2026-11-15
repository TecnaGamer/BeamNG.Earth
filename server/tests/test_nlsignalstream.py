from concurrent.futures import Future
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import json

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nlsignalstream import Tracker


class ConnectionOwnership(unittest.TestCase):
    def test_surface_dependencies_are_scoped_to_each_complete_junction(self):
        with tempfile.TemporaryDirectory() as folder:
            t=Tracker();t.state.reset(True,1);ticket=t.state.begin()
            a=dict(key='a',pose=[0.,0.,0.]);b=dict(key='b',pose=[1000.,0.,0.])
            groups=dict(near=dict(parts=[a],owners=['a']),remote=dict(parts=[b],owners=['b']))
            result=dict(parts=[a,b],owners=['a','b'],groups=groups,statuses=[],signalSignature='[]',graphPath='/temp/test/g.json')
            f=Future();f.set_result(result)
            conn=SimpleNamespace(send=lambda msg:None,s=SimpleNamespace(out_dir=folder,vfs_dir='/temp/test'))
            surfaces=[dict(key=key,h='h',skin='s',exact={},bounds=[[x-10,-10],[x+10,-10],[x+10,10],[x-10,10]])
                      for key,x in [('near',0),('remote',1000)]]
            t._built(conn,ticket,1,'g.ter',f,surfaces)
            packet=json.loads(next(Path(folder).glob('nl_signals_*.json')).read_text())
            self.assertEqual([(g['first'],g['count'])for g in packet['groups']],[(1,1),(2,1)])
            self.assertEqual([[r['key']for r in g['surfaces']]for g in packet['groups']],[['near'],['remote']])

    def test_incomplete_geometry_waits_for_changed_inputs_not_a_busy_retry(self):
        with tempfile.TemporaryDirectory()as folder:
            t=Tracker();t.state.reset(True,1);ticket=t.state.begin()
            conn=SimpleNamespace(send=lambda msg:None,s=SimpleNamespace(out_dir=folder,vfs_dir='/temp/test'))
            result=dict(parts=[],owners=[],groups={},statuses=[dict(pending=True)],
                        signalSignature='[]',graphPath='/temp/test/g.json',contextComplete=True)
            f=Future();f.set_result(result);t._built(conn,ticket,1,'g.ter',f)
            self.assertIsNone(t.retry_at)
            ticket=t.state.begin();result['contextComplete']=False
            f=Future();f.set_result(result);t._built(conn,ticket,1,'g.ter',f)
            self.assertIsNotNone(t.retry_at,'public shard arrivals still need polling')
    def test_partial_acknowledgement_cannot_split_one_junction(self):
        t=Tracker();t.state.reset(True,1)
        p=t.state.offer(t.state.begin(),[dict(key='mast')],['a','b','c'])
        t.offered_groups=(frozenset(['a','b']),frozenset(['c']))
        ack=dict(epoch=p['epoch'],anchorId=1,revision=p['revision'],owners=['a'])
        self.assertIsNone(t.acknowledge(ack))
        ack['owners']=['a','b']
        self.assertEqual(t.acknowledge(ack),('a','b'))
        ack['owners']=['a','b','c']
        self.assertEqual(t.acknowledge(ack),('a','b','c'))
    def test_preparing_next_block_does_not_block_committed_terrain(self):
        t=Tracker();t.source=SimpleNamespace(known=lambda:'known');t.future=object()
        conn=SimpleNamespace(active=1,pending_block=object(),block=SimpleNamespace(cx=0.,cy=0.),
            roads=SimpleNamespace(geo=True,sent=set(),skins=set(),building=set(),info={},skin_h={}),
            nav=SimpleNamespace(aid=1,path='/temp/graph.json',serial=1,ready_serial=1),
            s=SimpleNamespace(hm={(1,0.,0.):dict(ter='committed.ter')},
                anchors={1:SimpleNamespace(frame=object())}))
        with patch('nlsignalstream.geotiles.tiles_near',return_value=[]):
            t.on_pose(conn,dict(nlSignals=1,nlGround='committed.ter'))
        self.assertEqual(t.stats['waiting'],'planning')
        self.assertIsNotNone(t.signature)

    def test_disabled_pose_creates_no_worker_or_source(self):
        t=Tracker();t.on_pose(SimpleNamespace(active=1),{})
        self.assertIsNone(t.source);self.assertIsNone(t.pool)
        self.assertFalse(t.state.enabled)

    def test_late_worker_never_sends_or_suppresses(self):
        t=Tracker();t.state.reset(True,1);old=t.state.begin();t.state.begin()
        f=Future();f.set_result(dict(pending=True))
        sent=[];t._built(SimpleNamespace(send=sent.append),old,1,'ground.ter',f)
        self.assertEqual(sent,[]);self.assertEqual(t.state.suppression(),())
        self.assertEqual(t.retry_at,0.)

    def test_switching_off_restores_stock_graph_and_releases_context(self):
        t=Tracker();t.state.reset(True,1);t.source=object()
        sent=[]
        conn=SimpleNamespace(active=1,send=sent.append,nav=SimpleNamespace(aid=1,path='/stock.json',serial=4,ready_serial=3))
        t.on_pose(conn,{})
        self.assertEqual(sent,[dict(type='navgraph',anchorId=1,serial=3,path='/stock.json',nodes=0,edges=0)])
        self.assertIsNone(t.source);self.assertFalse(t.state.enabled)
        t.on_pose(conn,{})
        self.assertEqual(len(sent),1,'off mode has no recurring graph traffic')

    def test_packet_is_accepted_only_after_client_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            t=Tracker();t.state.reset(True,1);ticket=t.state.begin()
            sent=[];conn=SimpleNamespace(send=sent.append,s=SimpleNamespace(out_dir=folder,vfs_dir='/temp/test'))
            f=Future();f.set_result(dict(parts=[],owners=[],groups={},statuses=[],signalSignature='[]',graphPath='/temp/test/graph.json'))
            t._built(conn,ticket,2,'ground.ter',f)
            self.assertEqual([m['type'] for m in sent],['navgraph','nl_signals'])
            self.assertTrue(list(Path(folder).glob('nl_signals_*.json')))
            self.assertEqual(t.stats['accepted'],0)
            p=sent[-1]
            self.assertEqual(t.acknowledge(dict(epoch=p['epoch'],anchorId=1,revision=p['revision'])),())
            self.assertEqual(t.stats['accepted'],1)
            t.close();self.assertFalse(t.state.enabled)


if __name__=='__main__':unittest.main()
