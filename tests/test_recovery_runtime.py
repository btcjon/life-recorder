import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import asr
import processing_runtime
import receiver


class RecoveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = receiver.Inbox(Path(self.temp.name))

    def add_recording(self, received, status='pending'):
        identifier = str(uuid.uuid4())
        path = self.inbox.audio / (identifier + '.m4a')
        path.write_bytes(b'original')
        with self.inbox.connect() as db:
            db.execute('INSERT INTO chunks(id,sha256,device,started,duration,path,received,status) VALUES(?,?,?,?,?,?,?,?)',
                       (identifier, 'a' * 64, 'test', '2026-10-01T10:00:00Z', 60, str(path), received, status))
        return identifier

    def test_unavailable_engine_never_consumes_recording_attempt(self):
        identifier = self.add_recording(time.time())
        stop = threading.Event()
        def unavailable(config):
            stop.set()
            raise subprocess.CalledProcessError(-9, ['engine'])
        with patch('processing_runtime.preflight', side_effect=unavailable), patch('receiver.transcribe') as transcribe:
            receiver.worker(self.inbox, stop, Path('model'), 'whisper', 'ffmpeg')
        self.assertFalse(self.inbox.processing_runtime['engine_ready'])
        self.assertEqual(self.inbox.receipt(identifier)['attempts'], 0)
        transcribe.assert_not_called()

    def test_three_recent_then_backlog_and_no_idle_starvation(self):
        now = time.time()
        old = self.add_recording(now - 100000)
        recent = [self.add_recording(now + i) for i in range(4)]
        selected = []
        run = 0
        with self.inbox.connect() as db:
            for _ in range(5):
                row, run = processing_runtime.next_recording(db, now + 10, run)
                selected.append(row['id'])
                db.execute("UPDATE chunks SET status='complete' WHERE id=?", (row['id'],))
        self.assertEqual(selected, recent[:3] + [old] + recent[3:])

    def test_cache_oserror_has_cache_attribution_without_private_text(self):
        with self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(asr.ProcessingFailure) as caught:
                with processing_runtime.operation(self.inbox, 'cache_maintenance'):
                    raise PermissionError(13, 'private-path-and-transcript')
        self.assertEqual(caught.exception.code, 'cache_maintenance_failed')
        self.assertIn('errno=13', logs.output[0])
        self.assertNotIn('private-path', logs.output[0])

    def test_human_stall_and_engine_warning_do_not_leak_into_phone_report(self):
        self.add_recording(time.time() - 1200)
        self.inbox.processing_runtime['engine_ready'] = False
        report = self.inbox.health_snapshot()
        self.assertTrue(report['processing']['stalled'])
        self.assertEqual(report['processing']['backlog_duration_seconds'], 60)
        self.assertEqual(report['processing']['state'], 'engine_unavailable')
        self.assertNotIn('engine_ready', self.inbox.health_snapshot(device_id='test')['processing'])

    def test_recovery_uncertain_post_does_not_retry_failed_item(self):
        spec = importlib.util.spec_from_file_location('recover_backlog', Path(__file__).resolve().parents[1] / 'scripts/recover-backlog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = {'state': 'posting'}
        self.assertEqual(module.reconcile(entry, {'status': 'needs_attention'}), 'unresolved_attention')
        self.assertEqual(module.reconcile(entry, {'status': 'complete'}), 'recovered')
        entry['state'] = 'uncertain'
        self.assertEqual(module.reconcile(entry, {'status': 'pending'}), 'queued')


if __name__ == '__main__':
    unittest.main()
