from datetime import datetime, timezone
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import context_observations as ctx
import mac_context
from receiver import Inbox, Receiver, Handler
from agent_support import ViewerCase, add_chunk, sync

DEVICE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SESSION = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
NOW = datetime(2026, 10, 4, 14, tzinfo=timezone.utc).timestamp()


def envelope(at=NOW, state='active', signal='mac_output', metadata=None, sequence=0, session=SESSION):
    return {'device_id': DEVICE, 'observation': {
        'version': 1, 'id': str(uuid.uuid4()), 'producer_id': DEVICE, 'session_id': session,
        'sequence': sequence, 'observed_at': ctx.format_utc(datetime.fromtimestamp(at, timezone.utc)),
        'signal': signal, 'state': state, 'provenance': 'test-observed-state',
        'metadata': metadata if metadata is not None else {'app': 'com.fixture.browser'}}}


class ContextObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.server = Receiver(('127.0.0.1', 0), Handler)
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def store(self, **kwargs):
        body = envelope(**kwargs)
        self.inbox.store_context_observation(DEVICE, body, now=kwargs.get('at', NOW))
        return body

    def request(self, body, authorized=True, device=DEVICE):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {'X-Device-ID': device, 'Content-Type': 'application/json'}
        if authorized:
            headers['Authorization'] = 'Bearer ' + self.inbox.token
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request('POST', '/v1/context-observations', raw, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_authenticated_durable_idempotent_retry_and_conflict(self):
        body = envelope(at=time.time())
        self.assertEqual(self.request(body, authorized=False)[0], 401)
        code, receipt = self.request(body)
        self.assertEqual(code, 201)
        self.assertEqual(receipt, {'durable': True, 'observation_id': body['observation']['id']})
        with self.inbox.connect() as db:
            received = db.execute('SELECT received_at FROM context_observations').fetchone()[0]
        self.assertEqual(self.request(body), (200, receipt))
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT received_at FROM context_observations').fetchone()[0], received)
        changed = copy.deepcopy(body)
        changed['observation']['state'] = 'inactive'
        self.assertEqual(self.request(changed)[0], 409)
        self.assertEqual(self.request(body, device=str(uuid.uuid4()))[0], 400)
        self.assertEqual(self.request(b'x' * 4097)[0], 413)
        self.assertEqual(self.request(b'{')[0], 400)

    def test_validation_rejects_unsafe_and_ambiguous_fields(self):
        changes = [('version', True), ('version', 1.0), ('sequence', True), ('sequence', -1),
                   ('signal', 'made_up'), ('state', 'made_up'), ('observed_at', '2026-10-04T14:00:00'),
                   ('metadata', {'title': 'https://example.test/private'}),
                   ('metadata', {'call_state': '+15551234567'}), ('metadata', {'call_id': 'private'}),
                   ('metadata', {'title': 'x' * 201}), ('metadata', {'title': 'line\nbreak'})]
        for key, value in changes:
            body = envelope()
            body['observation'][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.request(body)[0], 400)
        body = envelope()
        body['extra'] = 'not allowed'
        self.assertEqual(self.request(body)[0], 400)

    def test_closed_transitions_are_renderable_and_bounded(self):
        self.store()
        self.store(at=NOW + 15, sequence=1)
        self.store(at=NOW + 300, state='inactive', sequence=2)
        report = self.inbox.context_evidence(now=NOW + 310)
        active = next(x for x in report['intervals'] if x['state'] == 'active')
        self.assertEqual(active['ended_at'], ctx.format_utc(datetime.fromtimestamp(NOW + 45, timezone.utc)))
        self.assertFalse(active['boundary_exact'])
        self.assertTrue(active['uncertain'])
        self.assertEqual(active['relationship'], 'overlapping_context')

    def test_sampled_transition_is_not_an_exact_boundary(self):
        self.store()
        self.store(at=NOW + 2, state='inactive')
        report = self.inbox.context_evidence(now=NOW + 3)
        self.assertTrue(all(not x['boundary_exact'] for x in report['intervals']))

    def test_missing_heartbeat_does_not_bridge_sleep_or_observer_failure(self):
        self.store()
        self.store(at=NOW + 120, sequence=1)
        report = self.inbox.context_evidence(now=NOW + 121)
        active = [x for x in report['intervals'] if x['state'] == 'active']
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0]['ended_at'], ctx.format_utc(datetime.fromtimestamp(NOW + 30, timezone.utc)))

    def test_out_of_order_is_deterministic_and_restart_cannot_extend_old_activity(self):
        old = envelope(at=NOW, sequence=1)
        stop = envelope(at=NOW + 10, state='inactive', sequence=2)
        self.inbox.store_context_observation(DEVICE, stop, now=NOW + 20)
        self.inbox.store_context_observation(DEVICE, old, now=NOW + 20)
        first = self.inbox.context_evidence(now=NOW + 21)
        self.assertEqual([x['state'] for x in first['intervals']], ['active', 'inactive'])
        self.assertEqual(first, self.inbox.context_evidence(now=NOW + 21))
        self.store(at=NOW + 25, session=str(uuid.uuid4()))
        self.assertFalse(first['intervals'][0]['fresh'])
        report = self.inbox.context_evidence(now=NOW + 70)
        self.assertFalse(any(x['fresh'] for x in report['intervals']))

    def test_clock_skew_excludes_precise_association(self):
        body = envelope(at=NOW - 600)
        self.inbox.store_context_observation(DEVICE, body, now=NOW)
        item = self.inbox.context_evidence(now=NOW)['intervals'][0]
        self.assertFalse(item['precise_transcript_association'])
        self.assertIn('clock_skew', item['uncertainties'])
        self.assertFalse(item['fresh'])
        body = envelope(at=NOW + 60, session=str(uuid.uuid4()))
        self.inbox.store_context_observation(DEVICE, body, now=NOW)
        self.assertFalse(any(item['fresh'] for item in self.inbox.context_evidence(now=NOW)['intervals']))

    def test_gap_survives_failed_resume_unknown_and_relaunch_then_closes(self):
        self.store(signal='capture', metadata={}, state='active')
        self.store(at=NOW + 1, signal='capture', state='inactive', metadata={'reason': 'interruption'})
        self.store(at=NOW + 2, signal='capture', state='inactive', metadata={'reason': 'startup_failure'})
        self.store(at=NOW + 20, signal='capture', state='unknown', metadata={'reason': 'unknown_termination'}, session=str(uuid.uuid4()))
        gap = self.inbox.context_evidence(now=NOW + 60)['open_capture_gaps'][0]
        self.assertEqual(gap['started_at'], ctx.format_utc(datetime.fromtimestamp(NOW + 1, timezone.utc)))
        self.assertTrue(gap['uncertain'])
        self.assertIn('no_fresh_observer', gap['uncertainties'])
        self.store(at=NOW + 61, signal='capture', metadata={}, session=str(uuid.uuid4()))
        report = self.inbox.context_evidence(now=NOW + 62)
        self.assertEqual(report['open_capture_gaps'], [])
        self.assertEqual(len(report['capture_gaps']), 1)
        self.assertFalse(report['capture_gaps'][0]['reason_assigns_cause'])

    def test_unknown_termination_can_start_a_nonexact_gap(self):
        self.store(signal='capture', state='unknown', metadata={'reason': 'unknown_termination'})
        gap = self.inbox.context_evidence(now=NOW + 1)['open_capture_gaps'][0]
        self.assertFalse(gap['boundary_exact'])
        self.assertTrue(gap['uncertain'])

    def test_day_filter_reads_historical_evidence_before_recent_scan_limit(self):
        self.store()
        for offset in range(10):
            self.store(at=NOW + 86400 * 2 + offset, state='inactive')
        with self.inbox.connect() as db:
            result = ctx.present(db, day='2026-10-04', now=NOW + 86400 * 3, scan_limit=2)
        self.assertEqual(len(result['intervals']), 1)
        self.assertEqual(result['intervals'][0]['state'], 'active')

    def test_gap_recovery_after_selected_day_is_not_called_still_open(self):
        self.store(signal='capture', state='inactive', metadata={'reason': 'quiet_hours'})
        self.store(at=NOW + 86400, signal='capture', metadata={})
        report = self.inbox.context_evidence(day='2026-10-04', now=NOW + 86401)
        self.assertEqual(report['open_capture_gaps'], [])
        self.assertEqual(report['capture_gaps'][0]['duration_seconds'], 86400)

    def test_audio_identity_search_and_citation_tables_unchanged(self):
        with self.inbox.connect() as db:
            add_chunk(db, ctx.format_utc(datetime.fromtimestamp(NOW, timezone.utc)), 'fixture budget')
        sync(self.inbox)
        with self.inbox.connect() as db:
            names = [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'context_%' ORDER BY name")]
            before = {n: [tuple(x) for x in db.execute('SELECT * FROM "' + n + '"')] for n in names}
        self.store()
        with self.inbox.connect() as db:
            after = {n: [tuple(x) for x in db.execute('SELECT * FROM "' + n + '"')] for n in names}
        self.assertEqual(before, after)


class ContextViewerRouteTests(ViewerCase):
    def test_human_context_only_machine_and_unauthenticated_rejected(self):
        self.inbox.store_context_observation(DEVICE, envelope(), now=NOW)
        self.assertEqual(self.request('GET', '/v1/context-evidence', {})[0], 401)
        headers = {'Authorization': 'Bearer ' + self.token}
        self.assertEqual(self.request('GET', '/v1/context-evidence', headers)[0], 200)
        self.assertEqual(self.request('GET', '/v1/context-evidence?day=bad', headers)[0], 400)
        with patch.dict(os.environ, {'LIFE_RECORDER_AGENT_CLIENT_IDS': self.client_id}):
            machine = {'Host': 'lr.genr8ive.ai', 'Cf-Access-Jwt-Assertion': self.machine_token()}
            for method, path in [('GET', '/v1/context-evidence'), ('POST', '/v1/context-observations')]:
                self.assertEqual(self.request(method, path, machine, b'{}')[0], 403)


class MacContextTests(unittest.TestCase):
    def test_process_transitions_heartbeat_failure_recovery_and_no_false_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            inbox = Inbox(Path(folder))
            samples = iter([{'ok': True, 'processes': ['com.fixture.browser'], 'reason': None}] * 3
                           + [{'ok': False, 'reason': 'probe_failed', 'processes': []},
                              {'ok': True, 'reason': None, 'processes': []}])
            observer = mac_context.MacContextObserver(inbox, threading.Event(), device_id=DEVICE,
                session_id=SESSION, probe=lambda: next(samples))
            for stamp in [NOW, NOW + 2, NOW + 16, NOW + 18, NOW + 20]:
                observer.tick(now=stamp)
            with inbox.connect() as db:
                rows = db.execute('SELECT state FROM context_observations ORDER BY observed_at').fetchall()
            self.assertEqual([x[0] for x in rows], ['active', 'active', 'unknown', 'inactive'])
            self.assertFalse(inbox.mac_context_status['measures_audible_sound'])
            self.assertFalse(inbox.mac_context_status['browser_adapter']['available'])
            self.assertTrue(inbox.context_evidence(now=NOW + 21)['intervals'])

    def test_helper_cache_compiles_once_and_tampering_recompiles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'probe.swift'
            source.write_text('fixture source')
            calls = []
            def compile_fn(src, target):
                calls.append(src)
                target.write_bytes(b'fixture executable')
            binary = mac_context.helper_binary(root / 'cache', source, compile_fn=compile_fn)
            self.assertEqual(mac_context.helper_binary(root / 'cache', source, compile_fn=compile_fn), binary)
            self.assertEqual(len(calls), 1)
            binary.write_bytes(b'tampered')
            mac_context.helper_binary(root / 'cache', source, compile_fn=compile_fn)
            self.assertEqual(len(calls), 2)

    def test_probe_validation_rejects_bounded_invalid_and_incomplete_coverage(self):
        for payload in [b'', b'{}', b'x' * 65537, b'{"version":true,"ok":true,"processes":[]}']:
            with self.assertRaises(ValueError):
                mac_context.parse_probe(payload)
        raw = json.dumps({'version': 1, 'ok': True, 'processes': [
            {'bundle_id': 'com.fixture.browser', 'output_io': True},
            {'bundle_id': 'https://bad.test/private', 'output_io': True}]}).encode()
        with self.assertRaises(ValueError):
            mac_context.parse_probe(raw)
        over_limit = json.dumps({'version': 1, 'ok': True, 'processes': [
            {'bundle_id': 'com.fixture.browser' + str(n), 'output_io': True} for n in range(65)]}).encode()
        with self.assertRaises(ValueError):
            mac_context.parse_probe(over_limit)


if __name__ == '__main__':
    unittest.main()
