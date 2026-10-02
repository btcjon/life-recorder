import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_support import ViewerCase
from test_viewer_layout import _playwright_python


BROWSER = r'''
import json,sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    errors=[]
    metrics=[]
    for width in (1280,390):
        page=browser.new_page(viewport={"width":width,"height":844})
        page.on("pageerror",lambda error: errors.append(str(error)))
        page.goto(sys.argv[1])
        page.locator("#tab-context").click()
        page.get_by_role("heading",name="Known places",exact=True).wait_for()
        editor=page.locator("#place-tools section").filter(has=page.get_by_role("heading",name="Add known place",exact=True))
        editor.get_by_label("Place name",exact=True).fill("Synthetic place "+str(width))
        editor.get_by_label("Latitude",exact=True).fill("42")
        editor.get_by_label("Longitude",exact=True).fill("-71")
        editor.get_by_label("Radius in metres",exact=True).fill("100")
        with page.expect_response(lambda response: response.url.endswith('/v1/places/save')) as response:
            editor.get_by_role("button",name="Save place",exact=True).click()
        assert response.value.status == 200
        page.locator("#context-status").filter(has_text="Saved").wait_for()
        metrics.append(page.evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})"))
        page.locator("#tab-people").click()
        page.get_by_role("heading",name="Voice profiles and anonymous groups",exact=True).wait_for()
        page.get_by_text("Cross-recording suggestions are off",exact=False).wait_for()
        if width == 1280:
            with page.expect_response(lambda response: response.url.endswith('/v1/identities/remove_sample')) as removal:
                page.get_by_role("button",name="Remove voice sample",exact=True).first.click()
            assert removal.value.status == 200
            with page.expect_response(lambda response: response.url.endswith('/v1/identities/undo')) as undo:
                page.get_by_role("button",name="Undo latest identity edit",exact=True).click()
            assert undo.value.status == 200
        page.close()
    browser.close()
    print(json.dumps({"errors":errors,"metrics":metrics}))
'''


class NewViewerRoutesTests(ViewerCase):
    def human(self, method, path, body=None):
        return self.request(method, path, {'Authorization': 'Bearer ' + self.token,
                                         'Content-Type': 'application/json'}, None if body is None else json.dumps(body).encode())

    def test_context_reads_human_only_and_invalid_day(self):
        for path in ('/v1/timeline', '/v1/places', '/v1/identity-profiles', '/v1/speaker-clusters'):
            self.assertEqual(self.human('GET', path)[0], 200)
            self.assertEqual(self.request('GET', path, {})[0], 401)
        self.assertEqual(self.human('GET', '/v1/timeline?day=bad')[0], 400)
        self.assertEqual(self.human('GET', '/v1/timeline?day=2026-10-01&day=2026-10-02')[0], 400)

    def test_machine_cannot_read_or_mutate_context(self):
        with mock.patch.dict('os.environ', {'LIFE_RECORDER_AGENT_CLIENT_IDS': self.client_id}):
            headers = {'Host': 'lr.genr8ive.ai', 'Origin': 'https://lr.genr8ive.ai',
                       'Cf-Access-Jwt-Assertion': self.machine_token(), 'Content-Type': 'application/json'}
            for path in ('/v1/timeline', '/v1/places', '/v1/identity-profiles', '/v1/speaker-clusters'):
                self.assertEqual(self.request('GET', path, headers)[0], 403)
            for path in ('/v1/timeline/accept', '/v1/places/save', '/v1/identities/merge'):
                self.assertEqual(self.request('POST', path, headers, b'{}')[0], 403)
            self.assertEqual(self.request('POST', '/v1/meeting-markers', headers, b'{}')[0], 403)

    def test_viewer_meeting_markers_reuse_idempotent_existing_store(self):
        import uuid
        from datetime import datetime, timezone
        event = {'version': 1, 'event_id': str(uuid.uuid4()), 'meeting_id': str(uuid.uuid4()),
                 'device_id': str(uuid.uuid4()), 'kind': 'start', 'occurred_at': datetime.now(timezone.utc).isoformat()}
        self.assertEqual(self.human('POST', '/v1/meeting-markers', event)[0], 201)
        self.assertEqual(self.human('POST', '/v1/meeting-markers', event)[0], 200)
        self.assertEqual(self.human('POST', '/v1/meeting-markers', dict(event, kind='end'))[0], 409)
        self.assertEqual(self.human('POST', '/v1/meeting-markers', {})[0], 400)

    def test_place_update_revision_and_malformed_requests(self):
        values = {'name': 'Synthetic place', 'latitude': 42, 'longitude': -71, 'radius_m': 100}
        status, raw, _ = self.human('POST', '/v1/places/save', values)
        self.assertEqual(status, 200)
        place = json.loads(raw)
        edit = dict(values, id=place['id'], revision=place['revision'])
        self.assertEqual(self.human('POST', '/v1/places/save', edit)[0], 200)
        self.assertEqual(self.human('POST', '/v1/places/save', edit)[0], 409)
        for path in ('/v1/timeline/accept', '/v1/timeline/split', '/v1/timeline/merge', '/v1/places/delete', '/v1/places/tag', '/v1/places/clear'):
            self.assertEqual(self.human('POST', path, {})[0], 400)
        self.assertEqual(self.human('POST', '/v1/places/save', dict(values, ignored=True))[0], 400)

    def test_context_and_profiles_render_desktop_mobile_and_place_save(self):
        from test_speaker_review import _label, _open_clip, _vec
        person = self.inbox.create_person('Synthetic profile')
        for i in range(2):
            clip = _open_clip(self.inbox, _vec(0), f'2026-09-20T12:0{i}:00.000Z', transcript='Synthetic test words')
            _label(self.inbox, clip, person['id'])
        python = _playwright_python()
        self.assertIsNotNone(python, 'Playwright required for rendered viewer verification')
        url = f'http://127.0.0.1:{self.port}/#{self.token}'
        result = subprocess.run([python, '-c', BROWSER, url], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-1600:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report['errors'], [])
        for frame in report['metrics']:
            self.assertLessEqual(frame['scrollWidth'], frame['width'] + 2)


if __name__ == '__main__':
    import unittest
    unittest.main()
