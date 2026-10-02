import importlib.util
import sqlite3
import unittest
from pathlib import Path


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class TrialGateTests(unittest.TestCase):
    def test_quiet_requires_days_reviews_conditions_and_zero_speech(self):
        tool = module('evaluate-quiet-upload')
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE chunks(id,started,status,activity_decision,vad_status)')
        db.execute('CREATE TABLE chunk_speech_spans(chunk_id,start_seconds,end_seconds)')
        conditions = ['soft_speech', 'distant_speech', 'music', 'noise', 'incomplete', 'noise', 'noise']
        reviews = []
        for day, condition in enumerate(conditions, 1):
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (str(day), f'2026-10-{day:02d}T12:00:00Z', 'complete', 'would_hold', 'complete'))
            reviews.append({'chunk_id': str(day), 'condition': condition, 'consented': True, 'speech_present': False})
        self.assertFalse(tool.evaluate(db, [])['evaluation_gate_passed'])
        self.assertTrue(tool.evaluate(db, reviews)['evaluation_gate_passed'])
        db.execute('INSERT INTO chunk_speech_spans VALUES(?,?,?)', ('1', 0, 1))
        report = tool.evaluate(db, reviews)
        self.assertFalse(report['evaluation_gate_passed'])
        self.assertFalse(report['suppression_enabled'])
        db.close()

    def test_listening_gate_rejects_missing_approval(self):
        from unittest.mock import patch
        tool = module('evaluate-playback-pairs')
        pairs = [{'original': f'a{i}', 'enhanced': f'b{i}', 'consented': True} for i in range(12)]
        with patch.object(tool, 'duration', return_value=1):
            self.assertFalse(tool.evaluate(pairs, 'probe')['default_change_ready'])
            for pair in pairs:
                pair.update({key: True for key in ('level_matched', 'alignment_checked', 'no_clipping', 'speech_boundaries_preserved', 'user_approved')})
            result = tool.evaluate(pairs, 'probe')
            self.assertTrue(result['default_change_ready'])
            self.assertFalse(result['playback_default_changed'])
            self.assertFalse(tool.evaluate([pairs[0]] * 12, 'probe')['default_change_ready'])

    def test_unrelated_history_and_reviews_cannot_pass_quiet_gate(self):
        tool = module('evaluate-quiet-upload')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE chunks(id,started,status,activity_decision,vad_status)')
        db.execute('CREATE TABLE chunk_speech_spans(chunk_id,start_seconds,end_seconds)')
        for day in range(1, 8):
            decision = 'would_hold' if day == 1 else None
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (str(day), f'2026-10-{day:02d}T12:00:00Z', 'complete', decision, 'complete'))
        reviews = [{'chunk_id': '1', 'condition': 'noise', 'consented': True, 'speech_present': False}]
        reviews += [{'chunk_id': 'absent' + c, 'condition': c, 'consented': True, 'speech_present': False}
                    for c in ('soft_speech', 'distant_speech', 'music', 'incomplete')]
        report = tool.evaluate(db, reviews)
        self.assertEqual(report['observed_days'], 1)
        self.assertFalse(report['condition_coverage_complete'])
        self.assertFalse(report['evaluation_gate_passed'])
        reviews.append(dict(reviews[0], speech_present=True))
        self.assertEqual(tool.evaluate(db, reviews)['conflicting_reviews'], 1)


if __name__ == '__main__':
    unittest.main()
