"""The harness must restore settings and refuse invalid gameplay comparisons."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
import benchmark_nl_signals as bench

class FakeGame:
    def __init__(self,owners=2,focused=True):
        self.on=True;self.owners=owners;self.focused=focused;self.settings=[]
    def lua(self,code):
        if 'function B.info()'in code:return 'ok'
        if "get('dutchSignals')"in code:return json.dumps(self.on)
        if "set('dutchSignals',"in code:
            self.on=',true)'in code;self.settings.append(self.on);return ''
        if 'BNGE_BENCH.info()'in code:return json.dumps(dict(mod=True,net='connected',active=True,
            speed=0,saved={'present':True},player=dict(lat=51.92,lon=4.46),perfmon=True))
        if 'signalAudit()'in code:return json.dumps(dict(liveOwners=self.owners if self.on else 0))
        if 'BNGE_BENCH.progress()'in code:return '{}'
        if 'BNGE_BENCH.perfRead()'in code:return json.dumps(dict(focused=self.focused,p50=12))
        return '{}'

class ActualGameGuard(unittest.TestCase):
    def perform(self,game):
        clock=[0.]
        def sleep(dt):clock[0]+=dt
        with tempfile.TemporaryDirectory()as folder:
            out=Path(folder)/'report.json'
            args=SimpleNamespace(pid=123,warmup=0,seconds=1,out=str(out))
            with patch.object(bench,'Game',return_value=game),patch.object(bench,'readiness',return_value=[]),\
                patch.object(bench,'working_set',return_value=1024),\
                patch.object(bench.time,'monotonic',side_effect=lambda:clock[0]),\
                patch.object(bench.time,'sleep',side_effect=sleep):
                bench.run(args)
            return json.loads(out.read_text())
    def test_abba_records_both_modes_and_restores_setting(self):
        game=FakeGame();report=self.perform(game)
        self.assertEqual([p['enabled']for p in report['phases']],[False,True,True,False])
        self.assertTrue(report['complete']);self.assertTrue(game.on)
        self.assertEqual(report['phases'][0]['working_set_mean_bytes'],1024)
    def test_missing_native_assemblies_cannot_pass_as_enabled(self):
        game=FakeGame(owners=0)
        with self.assertRaisesRegex(RuntimeError,'No native assemblies'):self.perform(game)
        self.assertTrue(game.on)
    def test_background_frame_cap_invalidates_measurement(self):
        game=FakeGame(focused=False)
        with self.assertRaisesRegex(RuntimeError,'lost focus'):self.perform(game)
        self.assertTrue(game.on)

if __name__=='__main__':unittest.main()
