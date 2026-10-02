import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
from receiver import Inbox


class ContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def add(self, identifier, stamp):
        with self.inbox.connect() as db:
            db.execute('INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,transcript) VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier, 'a'*64, 'phone', stamp, 30, '/not/audio', 0, 'complete', 'project budget'))

    def test_manual_single_clip_and_midnight_identity(self):
        self.add('clip_a', '2026-09-22T23:00:00Z')
        one = self.inbox.save_event_edit({'title': 'Single', 'start_chunk_id': 'clip_a', 'end_chunk_id': 'clip_a', 'expected_revision': 0})
        self.assertEqual(self.inbox.viewer_day('2026-09-22')['display_blocks'][0]['id'], one['id'])
        self.add('clip_b', '2026-09-23T03:59:30Z')
        self.add('clip_c', '2026-09-23T04:01:00Z')
        overnight = self.inbox.save_event_edit({'title': 'Overnight', 'start_chunk_id': 'clip_b', 'end_chunk_id': 'clip_c', 'expected_revision': 0})
        portions = []
        for day in ('2026-09-22', '2026-09-23'):
            block = next(b for b in self.inbox.viewer_day(day)['display_blocks'] if b['id'] == overnight['id'])
            self.assertTrue(block['day_portion'])
            portions += block['chunk_ids']
        self.assertEqual(portions, ['clip_b', 'clip_c'])
        with self.inbox.connect() as db:
            members = db.execute('SELECT m.chunk_id FROM agent_event_members m JOIN agent_events e ON e.id=m.event_id WHERE e.tombstoned=0').fetchall()
            self.assertTrue({'clip_b', 'clip_c'}.issubset({row[0] for row in members}))

    def test_manual_place_and_timeline_wrappers(self):
        self.add('clip_a', '2026-09-22T23:00:00Z')
        edit = self.inbox.save_event_edit({'title': 'Place meeting', 'start_chunk_id': 'clip_a', 'end_chunk_id': 'clip_a', 'expected_revision': 0})
        place = self.inbox.place_mutation('save', {'name': 'Test place', 'latitude': 0, 'longitude': 0, 'radius_m': 100})
        tagged = self.inbox.place_mutation('tag', {'event_id': edit['id'], 'place_id': place['id'], 'revision': 0})
        self.assertEqual(tagged['revision'], 1)
        report = self.inbox.places_list()
        self.assertEqual(report['events'][0]['place_id'], place['id'])
        self.assertEqual(self.inbox.timeline_list()['items'][0]['id'], edit['id'])


if __name__ == '__main__':
    unittest.main()
