import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import grok_topics as route


class GrokRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = Path(self.temp.name) / 'auth.json'
        self.write_auth({'type': 'api_key', 'key': 'fixture-secret'})

    def write_auth(self, credential):
        self.auth.write_text(json.dumps({'xai': credential, 'other-provider': {'untouched': True}}))
        self.auth.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def envelope(self, **changes):
        return {'id': 'resp_fixture', 'model': 'grok-4.7', 'status': 'completed', 'store': False, 'tools': [],
                'output': [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': '{"segments":[]}'}]}]} | changes

    def test_no_tools_web_store_files_or_secret_in_input_and_per_request_proof(self):
        request = Mock(return_value=(self.envelope(), 'request_fixture'))
        raw, proof = route.run(self.auth, 'grok-4.7', 'synthetic prompt', request=request)
        endpoint, wire, headers = request.call_args.args
        body = json.loads(wire)
        self.assertEqual(endpoint, 'https://api.x.ai/v1/responses')
        self.assertEqual(body['tools'], [])
        self.assertIs(body['store'], False)
        self.assertIs(body['stream'], False)
        self.assertNotIn('previous_response_id', body)
        self.assertNotIn('fixture-secret', wire.decode())
        self.assertEqual(set(body['input'][0]), {'role', 'content'})
        self.assertEqual(headers['Authorization'], 'Bearer fixture-secret')
        self.assertEqual(proof['effective_model'], 'grok-4.7')
        self.assertEqual(proof['response_id'], 'resp_fixture')
        self.assertEqual(proof['provider_request_id'], 'request_fixture')
        self.assertEqual(proof['input_sha256'], hashlib.sha256(b'synthetic prompt').hexdigest())
        self.assertFalse(proof['tools'])
        self.assertFalse(proof['web_search'])
        self.assertEqual(raw, '{"segments":[]}')

    def test_provider_metadata_not_model_prose_and_no_fallback(self):
        for envelope, code in [(self.envelope(model='different-model'), 'model_mismatch'),
            (self.envelope(id=None), 'response_unverified'),
            (self.envelope(status='incomplete'), 'response_incomplete'),
            (self.envelope(store=True), 'storage_policy_mismatch'),
            (self.envelope(output=[{'type': 'web_search_call'}]), 'unexpected_tool_output')]:
            with self.subTest(code=code), self.assertRaises(route.RouteError) as caught:
                route.run(self.auth, 'grok-4.7', 'x', request=Mock(return_value=(envelope, None)))
            self.assertEqual(caught.exception.code, code)

    def test_invalid_oversized_text_and_input_rejected(self):
        envelope = self.envelope()
        envelope['output'][0]['content'][0]['text'] = 'x' * (route.MAX_TEXT_BYTES + 1)
        with self.assertRaises(route.RouteError) as caught:
            route.run(self.auth, 'grok-4.7', 'x', request=Mock(return_value=(envelope, None)))
        self.assertEqual(caught.exception.code, 'output_limit')
        request = Mock()
        with self.assertRaises(route.RouteError):
            route.run(self.auth, 'grok-4.7', 'x' * (route.MAX_INPUT_BYTES + 1), request=request)
        request.assert_not_called()

    def test_http_capture_is_bounded_and_transport_failures_sanitized(self):
        response = Mock(status=200)
        response.read.return_value = b'x' * (route.MAX_RESPONSE_BYTES + 1)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(route.RouteError) as caught:
            route._request(route.ENDPOINT, b'{}', {}, timeout=1)
        response.read.assert_called_once_with(route.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(caught.exception.code, 'output_limit')
        connection.close.assert_called_once()
        connection.reset_mock()
        connection.request.side_effect = OSError('private unsafe exception')
        with patch.object(http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(route.RouteError) as caught:
            route._request(route.ENDPOINT, b'{}', {}, timeout=1)
        self.assertEqual(str(caught.exception), 'transport_unavailable')

    def test_http_errors_and_redirects_do_not_follow_or_log_response(self):
        for status, code in [(302, 'provider_unavailable'), (401, 'authentication_unavailable'),
                             (403, 'authentication_unavailable'), (429, 'provider_rate_limited')]:
            response = Mock(status=status)
            response.read.return_value = b'private failure content'
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(route.RouteError) as caught:
                route._request(route.ENDPOINT, b'{}', {}, timeout=1)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.http_status, status)
            self.assertEqual(connection.request.call_count, 1)
        with self.assertRaises(route.RouteError):
            route._request('https://untrusted.example/v1/responses', b'{}', {}, timeout=1)

    def test_overall_deadline_interrupts_socket_and_is_cancelled(self):
        response = Mock(status=200)
        response.read.return_value = b'{}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(http.client, 'HTTPSConnection', return_value=connection), \
                patch.object(route.threading, 'Timer') as timer:
            route._request(route.ENDPOINT, b'{}', {}, timeout=3)
            self.assertEqual(timer.call_args.args[0], 3)
            timer.call_args.args[1]()
            connection.sock.shutdown.assert_called_once_with(route.socket.SHUT_RDWR)
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()

    def test_expired_auth_never_refreshes_or_writes_shared_credentials(self):
        self.write_auth({'type': 'oauth', 'access': 'expired', 'refresh': 'old-refresh', 'expires': 0})
        request = Mock(return_value=({'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}, None))
        before = self.auth.read_bytes()
        with self.assertRaises(route.RouteError) as caught:
            route._access_token(self.auth, request=request)
        self.assertEqual(caught.exception.code, 'authentication_expired_renew_in_pi')
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        request.assert_not_called()
        self.write_auth({'type': 'oauth', 'access': 'new-access', 'refresh': 'new-refresh', 'expires': (time.time() + 600) * 1000})
        before = self.auth.read_bytes()
        self.assertEqual(route._access_token(self.auth, request=request), 'new-access')
        self.assertEqual(self.auth.read_bytes(), before)
        request.assert_not_called()

    def test_concurrent_writer_is_never_overwritten(self):
        real_read = route._read_auth
        def concurrent(path):
            result = real_read(path)
            self.write_auth({'type': 'api_key', 'key': 'other-writer'})
            return result
        with patch.object(route, '_read_auth', side_effect=concurrent):
            self.assertEqual(route._access_token(self.auth), 'fixture-secret')
        self.assertEqual(json.loads(self.auth.read_text())['xai']['key'], 'other-writer')
        self.assertEqual(json.loads(self.auth.read_text())['other-provider'], {'untouched': True})

    def test_unsafe_auth_nonfinite_expiry_missing_provider_and_symlink_rejected(self):
        self.auth.chmod(0o644)
        with self.assertRaises(route.RouteError):
            route.preflight(self.auth, 'grok-4.7')
        self.write_auth({'type': 'oauth', 'access': 'a', 'refresh': 'r', 'expires': float('nan')})
        with self.assertRaises(route.RouteError):
            route.preflight(self.auth, 'grok-4.7')
        self.write_auth({'type': 'api_key', 'key': 'fixture'})
        link = self.auth.with_name('symlink')
        link.symlink_to(self.auth)
        with self.assertRaises(route.RouteError):
            route.preflight(link, 'grok-4.7')
