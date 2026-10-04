import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
from receiver import Inbox
import context_grouping
import event_edits
import timeline


class ContextGroupingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        with self.inbox.connect() as db:
            for i in range(6):
                self.insert(db, f'clip{i}', f'2026-10-04T12:0{i}:00.000Z')

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, db, identifier, stamp, device='phone', status='complete', text='Synthetic discussion'):
        db.execute("""INSERT INTO chunks
            (id,sha256,device,started,duration,path,received,status,transcript)
            VALUES (?,?,?,?,20,'synthetic',1000,?,?)""",
            (identifier, 'synthetic', device, stamp, status, text))

    def propose(self, db, spans=None, ids=None, fingerprint='first', now=1000, model='grok-4.7'):
        ids = ids or ['clip0', 'clip1', 'clip2', 'clip3']
        return timeline.publish_suggestions(db, fingerprint=fingerprint,
            rows=timeline.current_source(db, ids),
            segments=spans or [
                {'start_clip_id': 'clip0', 'end_clip_id': 'clip1', 'title': 'Project launch'},
                {'start_clip_id': 'clip2', 'end_clip_id': 'clip3', 'title': 'Garden irrigation'}],
            model=model, prompt_version='synthetic-v1', now=now)

    def assert_exact(self, db, blocks):
        ids = [i for b in blocks for i in (b.get('chunk_ids') or [b['chunk_id']])]
        expected = {r[0] for r in db.execute('SELECT id FROM chunks')}
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), expected)

    def test_semantic_segments_not_time_groups_and_stable_ids(self):
        with self.inbox.connect() as db:
            identifiers = self.propose(db)
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            topics = [b for b in blocks if b.get('source') == 'topic']
            self.assertEqual([b['title'] for b in topics], ['Project launch', 'Garden irrigation'])
            self.assertEqual([b['chunk_ids'] for b in topics], [['clip0', 'clip1'], ['clip2', 'clip3']])
            self.assertEqual({b['id'] for b in topics}, set(identifiers))
            self.assertEqual(blocks, context_grouping.project_blocks(db))

    def test_reads_never_write_accept_or_call_model(self):
        with self.inbox.connect() as db:
            self.propose(db)
            before = db.total_changes
            db.execute('PRAGMA query_only=ON')
            with patch('topic_analysis.run_model', side_effect=AssertionError('No model on reads')):
                context_grouping.project_blocks(db)
                self.inbox.viewer_day('2026-10-04')
            self.assertEqual(db.total_changes, before)
            self.assertEqual(db.execute("SELECT count(*) FROM topic_suggestions WHERE state='accepted'").fetchone()[0], 0)
            self.assertEqual(event_edits.load(db), [])

    def test_full_window_change_outside_segment_invalidates_both(self):
        with self.inbox.connect() as db:
            self.propose(db)
            db.execute("UPDATE chunks SET transcript='New source' WHERE id='clip3'")
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            self.assertFalse(any(b.get('source') == 'topic' for b in blocks))

    def test_inserted_pending_source_invalidates_window(self):
        with self.inbox.connect() as db:
            self.propose(db)
            self.insert(db, 'late', '2026-10-04T12:01:30.000Z', status='pending', text='')
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            self.assertFalse(any(b.get('source') == 'topic' for b in blocks))

    def test_deleted_anchor_or_source_invalidates_window(self):
        for identifier in ('clip0', 'clip1', 'clip3'):
            with self.subTest(identifier=identifier), self.inbox.connect() as db:
                self.propose(db, fingerprint=identifier)
                old = dict(db.execute('SELECT * FROM chunks WHERE id=?', (identifier,)).fetchone())
                db.execute('DELETE FROM chunks WHERE id=?', (identifier,))
                blocks = context_grouping.project_blocks(db)
                self.assert_exact(db, blocks)
                self.assertFalse(any(b.get('source') == 'topic' for b in blocks))
                self.insert(db, identifier, old['started'])

    def test_manual_overlap_suppresses_entire_proposed_segment(self):
        with self.inbox.connect() as db:
            self.propose(db)
            saved = event_edits.save(db, {'title': 'Human title', 'start_chunk_id': 'clip1',
                'end_chunk_id': 'clip1', 'expected_revision': 0}, db.execute('SELECT * FROM chunks').fetchall())
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            manual = next(b for b in blocks if b.get('source') == 'manual')
            self.assertEqual(manual['id'], saved['id'])
            self.assertEqual(manual['chunk_ids'], ['clip1'])
            self.assertEqual([b['title'] for b in blocks if b.get('source') == 'topic'], ['Garden irrigation'])
            self.assertFalse(any(b.get('title') == 'Project launch' for b in blocks))

    def test_singleton_topic_and_quiet_pending_are_not_lost(self):
        with self.inbox.connect() as db:
            self.propose(db, [{'start_clip_id': 'clip0', 'end_clip_id': 'clip0', 'title': 'Short decision'}], ids=['clip0'])
            db.execute("UPDATE chunks SET transcript='',status='pending' WHERE id='clip4'")
            db.execute("UPDATE chunks SET transcript='' WHERE id='clip5'")
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            topic = next(b for b in blocks if b.get('source') == 'topic')
            self.assertEqual(topic['chunk_ids'], ['clip0'])

    def test_rejected_superseded_and_local_participant_do_not_control_groups(self):
        with self.inbox.connect() as db:
            self.propose(db)
            db.execute("UPDATE topic_suggestions SET state='rejected'")
            self.propose(db, fingerprint='local', model='local-participant-rule-v1')
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            self.assertFalse(any(b.get('source') == 'topic' for b in blocks))

    def test_competing_proposals_resolve_newest_first_without_duplication(self):
        with self.inbox.connect() as db:
            self.propose(db)
            self.propose(db, [{'start_clip_id': 'clip1', 'end_clip_id': 'clip2', 'title': 'New topic'}],
                         ids=['clip1', 'clip2'], fingerprint='new', now=2000, model='second-approved-model')
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            self.assertEqual([b['title'] for b in blocks if b.get('source') == 'topic'], ['New topic'])
            self.assertEqual(blocks, context_grouping.project_blocks(db))

    def test_same_title_window_seams_do_not_merge(self):
        with self.inbox.connect() as db:
            for index, ids in enumerate((['clip0', 'clip1'], ['clip2', 'clip3'])):
                self.propose(db, [{'start_clip_id': ids[0], 'end_clip_id': ids[-1], 'title': 'Same title'}],
                             ids=ids, fingerprint=str(index))
            topics = [b for b in context_grouping.project_blocks(db) if b.get('source') == 'topic']
            self.assertEqual(len(topics), 2)
            self.assertNotEqual(topics[0]['id'], topics[1]['id'])

    def test_malformed_windows_fail_safe(self):
        with self.inbox.connect() as db:
            self.propose(db)
            for value in ('broken-json', '{}', '["clip0","clip0"]', '[1,2]'):
                db.execute('UPDATE topic_suggestions SET source_ids=?', (value,))
                blocks = context_grouping.project_blocks(db)
                self.assert_exact(db, blocks)
                self.assertFalse(any(b.get('source') == 'topic' for b in blocks))

    def test_missing_optional_topic_tables_falls_back_without_writes(self):
        with self.inbox.connect() as db:
            db.execute('DROP TABLE topic_suggestions')
            before = db.total_changes
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            self.assertEqual(db.total_changes, before)

    def test_meeting_uses_only_its_device_and_contained_uncovered_recordings(self):
        with self.inbox.connect() as db:
            self.insert(db, 'other', '2026-10-04T12:01:30.000Z', device='other-phone')
            db.execute("""INSERT INTO intervals
                (id,device_id,source,label,started_at,ended_at,closed,status,chunk_ids)
                VALUES ('meeting','phone','manual','Marked meeting',
                '2026-10-04T12:00:00.000Z','2026-10-04T12:03:20.000Z',1,'closed','[]')""")
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            meetings = [b for b in blocks if b.get('source') == 'meeting']
            self.assertTrue(meetings)
            self.assertEqual({i for b in meetings for i in b['chunk_ids']}, {'clip0', 'clip1', 'clip2', 'clip3'})

    def test_possible_meeting_does_not_force_context(self):
        with self.inbox.connect() as db:
            db.execute("""INSERT INTO intervals
                (id,device_id,source,label,started_at,ended_at,closed,status,chunk_ids)
                VALUES ('possible','phone','possible','Possible event',
                '2026-10-04T12:00:00.000Z','2026-10-04T12:04:00.000Z',1,'closed','[]')""")
            self.assertFalse(any(b.get('source') == 'meeting' for b in context_grouping.project_blocks(db)))

    def test_time_fallback_never_bridges_devices(self):
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET device='other' WHERE id='clip2'")
            blocks = context_grouping.project_blocks(db)
            self.assert_exact(db, blocks)
            for block in blocks:
                if block.get('source') == 'suggestion':
                    devices = {db.execute('SELECT device FROM chunks WHERE id=?', (i,)).fetchone()[0] for i in block['chunk_ids']}
                    self.assertEqual(len(devices), 1)

    def test_cross_day_saved_identity_and_portions_preserved(self):
        with self.inbox.connect() as db:
            db.execute('DELETE FROM chunks')
            self.insert(db, 'before', '2026-10-05T03:59:00.000Z')
            self.insert(db, 'after', '2026-10-05T04:00:00.000Z')
        saved = self.inbox.save_event_edit({'title': 'Cross midnight', 'start_chunk_id': 'before',
                                         'end_chunk_id': 'after', 'expected_revision': 0})
        for day, clip in (('2026-10-04', 'before'), ('2026-10-05', 'after')):
            block = next(b for b in self.inbox.viewer_day(day)['display_blocks'] if b.get('source') == 'manual')
            self.assertEqual(block['id'], saved['id'])
            self.assertEqual(block['chunk_ids'], [clip])
            self.assertTrue(block['day_portion'])


if __name__ == '__main__':
    unittest.main()
