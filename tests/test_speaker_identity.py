import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
from receiver import Inbox
import diarization
import speaker_identity as identity

spec = importlib.util.spec_from_file_location('evaluate_identity', Path(__file__).resolve().parents[1] / 'scripts' / 'evaluate-speaker-identity.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class SpeakerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = Inbox(Path(self.temp.name))
        self.a = self.inbox.create_person('A')['id']
        self.b = self.inbox.create_person('B')['id']
        self.db = sqlite3.connect(self.inbox.db, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        identity.migrate(self.db)
        self.db.commit()

    def turn(self, axis=0, duration=8):
        chunk = str(uuid.uuid4())
        with self.inbox.connect() as db:
            db.execute("INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,words_json,diarization_status) VALUES(?,?,?,?,?,?,0,'complete','[]','success')",
                       (chunk, uuid.uuid4().hex, str(uuid.uuid4()), '2026-10-01T12:00:00Z', 20, '/nonexistent'))
        vector = [0.0] * 256
        vector[axis] = 1.0
        diarization.save_result(self.inbox, chunk, {'turns': [{'speaker_key': 'S1', 'started': 0, 'ended': duration, 'quality': 1, 'embedding': vector}], 'outcome': 'success', 'speaker_count': 1, 'speech_seconds': duration, 'coverage': duration / 20, 'turn_count': 1, 'embedding_count': 1, 'cluster_count': 1, 'asr_words': 0, 'processing_seconds': 0.1})
        return self.db.execute('SELECT id FROM speaker_turns WHERE chunk_id=?', (chunk,)).fetchone()[0]

    def change(self, turns, person, sample=False):
        return identity.change_turns(self.db, turns, person, sample, identity.revision(self.db))

    def test_stale_revision_has_no_partial_writes(self):
        turn = self.turn()
        self.change([turn], self.a)
        with self.assertRaises(identity.Conflict):
            identity.change_turns(self.db, [turn], self.b, True, 0)
        self.assertEqual(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (turn,)).fetchone()[0], self.a)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 0)

    def test_same_person_label_without_sample_choice_preserves_enrollment(self):
        turn = self.turn()
        self.change([turn], self.a, True)
        sample = dict(self.db.execute('SELECT * FROM voice_samples').fetchone())
        self.change([turn], self.a, False)
        self.assertEqual(dict(self.db.execute('SELECT * FROM voice_samples').fetchone()), sample)
        self.assertEqual(self.db.execute('SELECT enrolled FROM voice_vectors WHERE turn_id=?', (turn,)).fetchone()[0], 1)

    def test_correcting_member_of_shared_sample_withdraws_whole_sample(self):
        one = self.turn()
        original = self.db.execute('SELECT * FROM speaker_turns WHERE id=?', (one,)).fetchone()
        two = str(uuid.uuid4())
        self.db.execute("INSERT INTO speaker_turns SELECT ?,run_id,chunk_id,speaker_key,8,16,quality,embedding_json,NULL,NULL FROM speaker_turns WHERE id=?", (two, one))
        import voice_id
        vector = [0.0] * 256
        vector[0] = 1
        voice_id.store_turn_vectors(self.db, two, original['run_id'], original['chunk_id'], {'speaker_key': 'S1', 'started': 8, 'ended': 16, 'embedding': vector}, 0)
        self.change([one, two], self.a, True)
        self.change([two], self.b)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_vectors WHERE enrolled=1').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (one,)).fetchone()[0], self.a)

    def test_sample_removal_invalidates_profile_auto_and_suggestions_preserves_human_and_undo(self):
        one, two, human, automatic = [self.turn() for _ in range(4)]
        self.change([one], self.a, True)
        self.change([two], self.a, True)
        self.change([human], self.a)
        self.db.execute("UPDATE speaker_turns SET person_id=?,label_source='automatic' WHERE id=?", (self.a, automatic))
        self.assertTrue(identity.profiles(self.db)['people'][0]['enrollment_ready'])
        sample = self.db.execute('SELECT id FROM voice_samples WHERE turn_id=?', (one,)).fetchone()[0]
        removed = identity.remove_sample(self.db, sample, identity.revision(self.db))
        self.assertFalse(identity.profiles(self.db)['people'][0]['enrollment_ready'])
        self.assertIsNone(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (automatic,)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT label_source FROM speaker_turns WHERE id=?', (human,)).fetchone()[0], 'confirmed')
        self.assertEqual(self.db.execute('SELECT enrolled FROM voice_vectors WHERE turn_id=?', (one,)).fetchone()[0], 0)
        identity.undo(self.db, removed['change_id'], removed['revision'])
        self.assertTrue(identity.profiles(self.db)['people'][0]['enrollment_ready'])
        self.assertEqual(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (automatic,)).fetchone()[0], self.a)

    def test_remove_label_and_undo_restore_enrollment(self):
        turn = self.turn()
        self.change([turn], self.a, True)
        removed = self.change([turn], None)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 0)
        identity.undo(self.db, removed['change_id'], removed['revision'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (turn,)).fetchone()[0], self.a)

    def test_background_human_change_prevents_undo(self):
        turn = self.turn()
        change = self.change([turn], self.a)
        self.db.execute('UPDATE speaker_turns SET person_id=? WHERE id=?', (self.b, turn))
        with self.assertRaises(identity.Conflict):
            identity.undo(self.db, change['change_id'], change['revision'])
        self.assertEqual(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (turn,)).fetchone()[0], self.b)

    def test_merge_split_undo_and_selected_naming_do_not_enroll(self):
        one, two = self.turn(), self.turn()
        tracks = [self.db.execute("SELECT a.track_id FROM voice_assignments a JOIN voice_vectors v ON v.id=a.vector_id WHERE v.turn_id=? AND a.active=1", (t,)).fetchone()[0] for t in (one, two)]
        merged = identity.merge(self.db, tracks, 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 0)
        split = identity.split(self.db, tracks[0], [two], merged['revision'])
        identity.undo(self.db, split['change_id'], split['revision'])
        identity.name_selected(self.db, tracks[0], [one], self.a, identity.revision(self.db))
        self.assertIsNone(self.db.execute('SELECT person_id FROM speaker_turns WHERE id=?', (two,)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM voice_samples').fetchone()[0], 0)

    def test_conflicting_confirmed_merge_rejected(self):
        one, two = self.turn(), self.turn()
        self.change([one], self.a)
        self.change([two], self.b)
        tracks = [c['id'] for c in identity.clusters(self.db)['clusters']]
        with self.assertRaises(identity.Conflict):
            identity.merge(self.db, tracks, identity.revision(self.db))

    def test_proposals_default_off_and_failed_report_stays_off(self):
        self.turn()
        self.turn()
        self.assertEqual(identity.proposals(self.db)['proposals'], [])
        identity.record_evaluation(self.db, {'status': 'passed'})
        self.assertFalse(identity.evaluation_gate(self.db)['enabled'])

    def test_consent_and_session_holdout_required_for_evaluation(self):
        one, two, case = self.turn(), self.turn(1), self.turn()
        manifest = {'consent': True, 'references': [{'turn_id': one, 'identity': 'a', 'session': 'train'}, {'turn_id': two, 'identity': 'b', 'session': 'train'}], 'cases': [{'turn_id': case, 'identity': 'a', 'session': 'train', 'category': 'known'}]}
        self.assertEqual(evaluation.evaluate(self.db, manifest)['reason'], 'holdout_leakage')
        manifest['cases'][0]['session'] = 'test'
        report = evaluation.evaluate(self.db, manifest)
        self.assertEqual(report['correct_assignments'], 1)
        self.assertEqual(report['false_assignments'], 0)
        self.assertEqual(report['status'], 'not_passed')
        self.assertNotIn(one, json.dumps(report))
        manifest['consent'] = False
        self.assertEqual(evaluation.evaluate(self.db, manifest)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
