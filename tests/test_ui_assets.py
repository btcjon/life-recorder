import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
sys.path.insert(0, str(Path(__file__).parent))
import ui_assets
import health
from agent_support import ViewerCase


def fixture(root, revision='a' * 40, sources=None):
    root.mkdir(mode=0o700, parents=True)
    files = {'index.html': b'<html><script src="/ui-v2/assets/app-abc.js"></script></html>',
             'assets/app-abc.js': b'console.log("synthetic")',
             'assets/app-abc.css': b'body {color:black}',
             'fonts/geist/test.woff2': b'fixture font'}
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    metadata = {'version': 1, 'source_revision': revision,
                'source_files': sources or {'web/package-lock.json': 'b' * 64},
                'vendor_files': {'src/components/ui/button.tsx': 'c' * 64},
                'asset_files': {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    (root / 'build-provenance.json').write_text(json.dumps(metadata))
    return metadata, files


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'bundle'
        self.metadata, self.files = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_bundle_inventory_pages_and_only_declared_assets(self):
        bundle = ui_assets.Bundle(self.root, 'a' * 40)
        self.assertEqual(bundle.status()['asset_count'], 4)
        for page in ('', 'index.html', 'recordings', 'review', 'people', 'context', 'health'):
            self.assertEqual(bundle.read('/ui-v2/' + page)[0], self.files['index.html'])
        self.assertEqual(bundle.read('/ui-v2/assets/app-abc.js')[1], 'text/javascript; charset=utf-8')
        self.assertEqual(bundle.read('/ui-v2/fonts/geist/test.woff2')[1], 'font/woff2')
        for name in ('../receiver.py', 'assets/../../receiver.py', '%2e%2e/receiver.py',
                     'assets/unknown.js', 'build-provenance.json', 'assets/app-abc.js.map',
                     'assets/app..js', 'assets/app.svg', 'v1/audio/foo', 'unknown'):
            self.assertIsNone(bundle.read('/ui-v2/' + name), name)
        self.assertIsNone(bundle.read('/v1/audio/foo'))

    def test_tampered_assets_and_added_files_cannot_be_served(self):
        bundle = ui_assets.Bundle(self.root)
        (self.root / 'assets/app-abc.js').write_bytes(b'changed script')
        with self.assertRaises(ui_assets.Unavailable):
            bundle.read('/ui-v2/assets/app-abc.js')
        (self.root / 'assets/unlisted.js').write_bytes(b'not in pinned inventory')
        self.assertIsNone(bundle.read('/ui-v2/assets/unlisted.js'))
        with self.assertRaises(ui_assets.Unavailable):
            ui_assets.read_build(self.root)

    def test_symlinked_root_directory_file_and_inventory_are_rejected(self):
        link = self.base / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ui_assets.Unavailable):
            ui_assets.Bundle(link)
        script = self.root / 'assets/app-abc.js'
        script.unlink()
        script.symlink_to(self.root / 'index.html')
        with self.assertRaises(ui_assets.Unavailable):
            ui_assets.Bundle(self.root)

    def test_provenance_is_strict_bounded_and_revision_matched(self):
        with self.assertRaises(ui_assets.Unavailable):
            ui_assets.Bundle(self.root, 'd' * 40)
        original = dict(self.metadata)
        for change in ({'version': True}, {'extra': 'private'}, {'source_revision': 'main'},
                       {'vendor_files': {}}, {'asset_files': {'../bad.js': 'f' * 64}},
                       {'source_files': {'web/vendor/paid.tsx': 'f' * 64}},
                       {'source_files': {'web/package-lock.json': 'z' * 64}}):
            (self.root / 'build-provenance.json').write_text(json.dumps(original | change))
            with self.assertRaises(ui_assets.Unavailable, msg=str(change)):
                ui_assets.Bundle(self.root)

    def test_release_filename_policy_does_not_allow_arbitrary_assets(self):
        self.assertTrue(health.allowed_release_asset('assets/viewer-v2/assets/app-Ab123.css'))
        self.assertTrue(health.allowed_release_asset('assets/viewer-v2/build-provenance.json'))
        for name in ('assets/viewer-v2/.env', 'assets/viewer-v2/assets/../foo.js',
                     'assets/viewer-v2/custom.html', 'assets/other.js',
                     'assets/viewer-v2/assets/secret.token'):
            self.assertFalse(health.allowed_release_asset(name), name)


class UiHttpTests(ViewerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name).resolve() / 'ui-bundle'
        self.metadata, self.files = fixture(self.root)
        self.server.ui_bundle = ui_assets.Bundle(self.root)
        self.server.ui_status = self.server.ui_bundle.status()

    def test_local_bootstrap_and_remote_human_auth_for_every_asset(self):
        for name, data in self.files.items():
            path = '/ui-v2/' + ('' if name == 'index.html' else name)
            status, body, headers = self.request('GET', path)
            self.assertEqual((status, body), (200, data))
            self.assertEqual(headers['cache-control'], 'no-store')
            self.assertEqual(headers['content-security-policy'], __import__('viewer').UI_CSP)
            self.assertEqual(headers['x-content-type-options'], 'nosniff')
            status, _, _ = self.request('GET', path, {'Host': self.config.host})
            self.assertEqual(status, 401)
            status, body, _ = self.request('GET', path, {
                'Host': self.config.host, 'Cf-Access-Jwt-Assertion': self.human_token()})
            self.assertEqual((status, body), (200, data))
        status, _, _ = self.request('GET', '/v1/days')
        self.assertEqual(status, 401)

    def test_ui_csp_only_admits_exact_scrollbar_style_not_inline_script_or_attributes(self):
        import base64
        import viewer
        css = b'.base-ui-disable-scrollbar{scrollbar-width:none}.base-ui-disable-scrollbar::-webkit-scrollbar{display:none}'
        sha = base64.b64encode(hashlib.sha256(css).digest()).decode()
        directives = dict((part.strip().split(' ', 1) for part in viewer.UI_CSP.split(';') if part.strip()))
        self.assertEqual(directives['style-src-elem'], "'self' 'sha256-" + sha + "'")
        self.assertIn("script-src 'self';", viewer.UI_CSP)
        self.assertIn("style-src 'self';", viewer.UI_CSP)
        for unsafe in ("'unsafe-inline'", "'unsafe-eval'", 'https:', 'data:'):
            self.assertNotIn(unsafe, viewer.UI_CSP)
        status, _, headers = self.request('GET', '/legacy/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['content-security-policy'], viewer.CSP)

    def test_default_cutover_legacy_and_controlled_deep_links(self):
        status, legacy, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotEqual(legacy, self.files['index.html'])
        self.server.ui_v2_default = True
        status, body, _ = self.request('GET', '/')
        self.assertEqual((status, body), (200, self.files['index.html']))
        self.assertEqual(self.request('GET', '/legacy/')[1], legacy)
        self.assertEqual(self.request('GET', '/ui-v2/recordings?day=2026-10-04')[1], self.files['index.html'])
        self.assertEqual(self.request('GET', '/ui-v2/')[1], self.files['index.html'])
        self.assertEqual(self.request('GET', '/ui-v2')[1], self.files['index.html'])
        self.assertEqual(self.request('GET', '/v1/does-not-exist', {
            'Authorization': 'Bearer ' + self.token})[0], 404)

    def test_head_and_forbidden_origin_unknown_traversal_and_provenance(self):
        status, body, headers = self.request('HEAD', '/ui-v2/assets/app-abc.js')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(int(headers['content-length']), len(self.files['assets/app-abc.js']))
        self.assertEqual(self.request('GET', '/ui-v2/', {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/ui-v2/', {'Host': 'evil.example'})[0], 403)
        for name in ('assets/../index.html', '%2e%2e/receiver.py', 'build-provenance.json', 'missing.js',
                     'assets/app-abc.js.map', 'v1/audio/nope'):
            self.assertEqual(self.request('GET', '/ui-v2/' + name)[0], 404, name)

    def test_machine_tokens_cannot_fetch_frontend_or_legacy(self):
        with patch.dict(os.environ, {'LIFE_RECORDER_AGENT_CLIENT_IDS': self.client_id}):
            for method in ('GET', 'HEAD'):
                for path in ('/', '/ui-v2/', '/ui-v2/assets/app-abc.js', '/legacy/'):
                    self.assertEqual(self.request(method, path, {
                        'Host': self.config.host, 'Cf-Access-Jwt-Assertion': self.machine_token()})[0], 403)

    def test_tampered_or_unavailable_bundle_fails_closed_but_legacy_remains(self):
        (self.root / 'assets/app-abc.js').write_bytes(b'new bytes')
        status, body, _ = self.request('GET', '/ui-v2/assets/app-abc.js')
        self.assertEqual(status, 503)
        self.assertNotIn(b'new bytes', body)
        self.server.ui_bundle = None
        self.server.ui_v2_default = True
        self.assertEqual(self.request('GET', '/')[0], 503)
        self.assertEqual(self.request('GET', '/legacy/')[0], 200)


if __name__ == '__main__':
    unittest.main()
