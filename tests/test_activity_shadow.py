import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import meetings as meetings_mod
import vad as vad_mod
import viewer as viewer_mod
from receiver import Handler, Inbox, Receiver


class ActivityShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.server = Receiver(("127.0.0.1", 0), Handler)
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device = str(uuid.uuid4())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def upload(self, body=b"test audio", extra=None, started="2026-09-10T16:00:00.000Z"):
        chunk_id = str(uuid.uuid4())
        headers = {
            "Authorization": "Bearer " + self.inbox.token,
            "X-Device-ID": self.device,
            "X-Started-At": started,
            "X-Duration-Seconds": "60.0",
            "X-Audio-SHA256": hashlib.sha256(body).hexdigest(),
            "Content-Type": "audio/mp4",
        }
        if extra:
            headers.update(extra)
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("POST", "/v1/chunks/" + chunk_id, body, headers)
        response = client.getresponse()
        result = response.status, json.loads(response.read()), chunk_id
        client.close()
        return result

    def test_schema_v6_has_activity_columns(self):
        with self.inbox.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
        self.assertEqual(version, 8)
        self.assertIn("activity_decision", cols)
        self.assertIn("voice_extract_version", cols)

    def test_valid_hold_is_stored_and_invalid_does_not_reject(self):
        hold = "v=1;d=would_hold;c=1;w=3000;e=3000;rms=-70.00;pk=-50.00;r=quiet"
        status, receipt, chunk_id = self.upload(extra={"X-Activity-Shadow": hold, "X-Activity-Version": "1"})
        self.assertEqual(status, 201)
        self.assertTrue(receipt["durable"])
        row = self.inbox.receipt(chunk_id)
        self.assertEqual(row["activity_decision"], "would_hold")
        status, _, bad_id = self.upload(body=b"second audio", extra={"X-Activity-Shadow": "not-valid"})
        self.assertEqual(status, 201)
        self.assertIsNone(self.inbox.receipt(bad_id)["activity_decision"])

    def test_idempotent_retry_ignores_conflicting_telemetry(self):
        hold = "v=1;d=would_hold;c=1;w=10;e=10;rms=-80.00;pk=-55.00;r=quiet"
        status, _, chunk_id = self.upload(extra={"X-Activity-Shadow": hold})
        self.assertEqual(status, 201)
        status, receipt, _ = self.upload(extra={"X-Activity-Shadow": "v=1;d=would_upload;c=1;w=10;e=10;rms=-10.00;pk=-5.00;r=activity"})
        # same body/id collision uses original checksum from first upload helper? new id each time.
        self.assertEqual(status, 201)
        retry_headers = {
            "Authorization": "Bearer " + self.inbox.token,
            "X-Device-ID": self.device,
            "X-Started-At": "2026-09-10T16:00:00.000Z",
            "X-Duration-Seconds": "60.0",
            "X-Audio-SHA256": hashlib.sha256(b"test audio").hexdigest(),
            "Content-Type": "audio/mp4",
            "X-Activity-Shadow": "v=1;d=would_upload;c=1;w=10;e=10;rms=-10.00;pk=-5.00;r=activity",
        }
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("POST", "/v1/chunks/" + chunk_id, b"test audio", retry_headers)
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        json.loads(response.read())
        client.close()
        self.assertEqual(self.inbox.receipt(chunk_id)["activity_decision"], "would_hold")

    def test_hold_without_thresholds_is_ignored(self):
        parsed = meetings_mod.parse_activity_header("v=1;d=would_hold;c=1;w=10;e=10;rms=-10.00;pk=-5.00;r=quiet")
        self.assertIsNone(parsed)
        parsed = meetings_mod.parse_activity_header("v=1;d=unknown;c=0;w=1;e=2;r=incomplete")
        self.assertEqual(parsed["decision"], "unknown")

    def test_hold_requires_complete_nonempty_window_coverage(self):
        for header in (
            "v=1;d=would_hold;c=1;w=1;e=3000;rms=-80.00;pk=-55.00;r=quiet",
            "v=1;d=would_hold;c=1;w=0;e=0;rms=-80.00;pk=-55.00;r=quiet",
        ):
            self.assertIsNone(meetings_mod.parse_activity_header(header))

    def test_viewer_summary_compares_completed_vad_only(self):
        hold = "v=1;d=would_hold;c=1;w=10;e=10;rms=-80.00;pk=-55.00;r=quiet"
        _, _, quiet_id = self.upload(extra={"X-Activity-Shadow": hold})
        _, _, speech_id = self.upload(
            body=b"speech",
            started="2026-09-10T16:01:00.000Z",
            extra={"X-Activity-Shadow": "v=1;d=would_upload;c=1;w=10;e=10;rms=-20.00;pk=-10.00;r=activity"},
        )
        self.inbox.complete(quiet_id, "quiet")
        self.inbox.complete(speech_id, "speech")
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET vad_status='complete' WHERE id=?", (quiet_id,))
            db.execute("UPDATE chunks SET vad_status='complete' WHERE id=?", (speech_id,))
        vad_mod.save_chunk_spans(self.inbox, quiet_id, [{"start": 0.5, "end": 1.0}])
        payload = self.inbox.viewer_day("2026-09-10")
        summary = payload["activity_shadow"]
        self.assertEqual(summary["evaluated"], 2)
        self.assertEqual(summary["would_hold"], 1)
        self.assertEqual(summary["hold_vad_positive"], 1)
        self.assertEqual(summary["vad_complete"], 2)
        self.assertIn("shadow-hold", viewer_mod.JS)
        self.assertIn("hold/VAD disagreement", viewer_mod.JS)


if __name__ == "__main__":
    unittest.main()
