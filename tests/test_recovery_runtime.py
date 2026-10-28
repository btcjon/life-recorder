import importlib.util
import hashlib
import io
import json
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
                       (identifier, hashlib.sha256(b'original').hexdigest(), 'test', '2026-10-01T10:00:00Z', 60, str(path), received, status))
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

    def test_engine_disappearing_after_preflight_preserves_attempt_budget(self):
        identifier = self.add_recording(time.time())
        stop = threading.Event()
        calls = 0
        def preflight(config):
            nonlocal calls
            calls += 1
            if calls > 1:
                stop.set()
                raise OSError(5, 'unavailable')
        with patch('processing_runtime.preflight', side_effect=preflight), patch('receiver.transcribe', side_effect=asr.ProcessingFailure('engine_launch', 'engine_launch_failed', 'transient')):
            receiver.worker(self.inbox, stop, Path('model'), 'whisper', 'ffmpeg')
        self.assertEqual(self.inbox.receipt(identifier)['attempts'], 0)
        self.assertFalse(self.inbox.processing_runtime['engine_ready'])

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

    def test_recovery_resumes_after_uncertain_post_without_replaying_or_touching_complete(self):
        identifier = self.add_recording(time.time(), 'needs_attention')
        completed = self.add_recording(time.time(), 'complete')
        spec = importlib.util.spec_from_file_location('recover_backlog', Path(__file__).resolve().parents[1] / 'scripts/recover-backlog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.inbox.root / 'viewer.token').write_text('private-test-token')
        manifest = self.inbox.root / 'manifest.json'
        arguments = ['recover-backlog', '--data-dir', str(self.inbox.root), '--manifest', str(manifest), '--apply']
        calls = []
        def request(request, **kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith('/v1/health'):
                return io.BytesIO(b'{"processing":{"engine_ready":true}}')
            self.inbox.retry_chunk(identifier)
            raise TimeoutError('response lost after acceptance')
        with patch.object(sys, 'argv', arguments), patch.object(module.urllib.request, 'urlopen', side_effect=request):
            with self.assertRaisesRegex(RuntimeError, 'retry_outcome_requires_reconciliation'):
                module.main()
            module.main()
        data = json.loads(manifest.read_text())
        self.assertEqual([entry['id'] for entry in data['entries']], [identifier])
        self.assertEqual(data['entries'][0]['state'], 'queued')
        self.assertEqual(sum(url.endswith('/retry') for url in calls), 1)
        self.assertEqual(self.inbox.receipt(completed)['status'], 'complete')

    def test_day_counts_are_scoped_and_original_bytes_are_available_at_receipt(self):
        self.add_recording(time.time())
        other = self.add_recording(time.time(), 'needs_attention')
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET started='2026-10-02T10:00:00Z' WHERE id=?", (other,))
        report = self.inbox.viewer_day('2026-10-01')
        self.assertEqual(report['day_processing']['pending'], 1)
        self.assertEqual(report['day_processing']['needs_attention'], 0)
        self.assertEqual(report['global_processing']['needs_attention'], 1)
        source = self.inbox.root / 'upload'
        source.write_bytes(b'new-original')
        identifier = str(uuid.uuid4())
        self.inbox.accept(source, identifier, hashlib.sha256(b'new-original').hexdigest(), 'test', '2026-10-03T10:00:00Z', 60)
        self.assertEqual(self.inbox.receipt(identifier)['audio_bytes'], len(b'new-original'))

    def test_recovery_rerun_cannot_expand_canary_without_explicit_gate(self):
        for _ in range(12):
            self.add_recording(time.time(), 'needs_attention')
        spec = importlib.util.spec_from_file_location('recover_backlog', Path(__file__).resolve().parents[1] / 'scripts/recover-backlog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.inbox.root / 'viewer.token').write_text('private-test-token')
        manifest = self.inbox.root / 'manifest.json'
        arguments = ['recover-backlog', '--data-dir', str(self.inbox.root), '--manifest', str(manifest), '--apply', '--limit', '12']
        submitted = []
        def request(request, **kwargs):
            if request.full_url.endswith('/v1/health'):
                return io.BytesIO(b'{"processing":{"engine_ready":true}}')
            identifier = request.full_url.split('/')[-2]
            submitted.append(identifier)
            self.inbox.retry_chunk(identifier)
            return io.BytesIO(b'{}')
        with patch.object(sys, 'argv', arguments), patch.object(module.urllib.request, 'urlopen', side_effect=request):
            module.main()
            module.main()
        self.assertEqual(len(submitted), 10)
        self.assertEqual(len(set(submitted)), 10)
        with patch.object(sys, 'argv', arguments + ['--expand']):
            with self.assertRaisesRegex(RuntimeError, 'thirty_minute_gate_not_passed'):
                module.main()


if __name__ == '__main__':
    unittest.main()
