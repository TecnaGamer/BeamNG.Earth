from concurrent.futures import Future
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nlsignalstream import Tracker


class ConnectionOwnership(unittest.TestCase):
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
