import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nlsignalsnapshot import Snapshot


def parts():
    return [dict(key='post', shape='native.dae', pose=[0.] * 12)]


class OwnershipTest(unittest.TestCase):
    def test_disabled_does_no_work(self):
        s = Snapshot()
        self.assertIsNone(s.begin())
        self.assertIsNone(s.offer(None, None, None))
        self.assertEqual(s.suppression(), ())

    def test_only_current_client_acceptance_replaces_stock(self):
        s = Snapshot(); s.reset(True, 1)
        p = s.offer(s.begin(), parts(), ['junction:incoming'])
        self.assertEqual(s.suppression(), ())
        for ack in [(p['epoch'], 2, p['revision']), (p['epoch'], 1, p['revision'] + 1)]:
            self.assertIsNone(s.acknowledge(*ack))
        self.assertEqual(s.acknowledge(p['epoch'], 1, p['revision']), ('junction:incoming',))
        self.assertEqual(s.suppression(), ('junction:incoming',))

    def test_late_workers_and_acknowledgements_cannot_resurrect_an_anchor(self):
        s = Snapshot(); s.reset(True, 1); old = s.begin()
        p = s.offer(old, parts(), ['old'])
        s.reset(True, 2)
        self.assertIsNone(s.offer(old, parts(), ['old']))
        self.assertIsNone(s.acknowledge(p['epoch'], 1, p['revision']))
        s.reset(False)
        self.assertEqual(s.suppression(), ())

    def test_pending_surface_rebuild_keeps_accepted_generation(self):
        s = Snapshot(); s.reset(True, 1)
        p = s.offer(s.begin(), parts(), ['old']); s.acknowledge(p['epoch'], 1, p['revision'])
        first = s.begin(); second = s.begin()
        self.assertIsNone(s.offer(first, parts(), ['late']))
        self.assertEqual(s.suppression(), ('old',))
        p = s.offer(second, parts(), ['new'])
        self.assertEqual(s.suppression(), ('old',))
        s.acknowledge(p['epoch'], 1, p['revision'])
        self.assertEqual(s.suppression(), ('new',))

    def test_packet_mutation_and_invalid_offer_do_not_damage_previous_result(self):
        s = Snapshot(); s.reset(True, 1); t = s.begin()
        rows = parts(); p = s.offer(t, rows, ['one'])
        rows[0]['key'] = 'changed'; p['parts'][0]['key'] = 'also_changed'
        self.assertEqual(s.pending[1]['parts'][0]['key'], 'post')
        with self.assertRaises(ValueError): s.offer(t, parts() * 2, ['one'])
        with self.assertRaises(ValueError): s.offer(t, parts(), ['one', 'one'])
        with self.assertRaises(ValueError): s.offer(t, [], ['one'])
        self.assertEqual(s.acknowledge(p['epoch'], 1, p['revision']), ('one',))

    def test_empty_complete_replacement_retires_ownership(self):
        s = Snapshot(); s.reset(True, 1)
        p = s.offer(s.begin(), parts(), ['one']); s.acknowledge(p['epoch'], 1, p['revision'])
        p = s.offer(s.begin(), [], [])
        self.assertEqual(s.acknowledge(p['epoch'], 1, p['revision']), ())
        self.assertEqual(s.suppression(), ())

    def test_complete_subsets_can_unlock_independently_in_one_packet(self):
        s=Snapshot();s.reset(True,1)
        p=s.offer(s.begin(),parts(),['near','bridge'])
        self.assertEqual(s.acknowledge(p['epoch'],1,p['revision'],['near']),('near',))
        self.assertEqual(s.suppression(),('near',))
        self.assertIsNone(s.acknowledge(p['epoch'],1,p['revision'],['unknown']))
        self.assertEqual(s.acknowledge(p['epoch'],1,p['revision'],['near','bridge']),('near','bridge'))
        s.begin()
        self.assertIsNone(s.acknowledge(p['epoch'],1,p['revision'],['near']))


if __name__ == '__main__': unittest.main()
