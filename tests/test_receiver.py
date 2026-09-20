import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import receiver as receiver_mod
from receiver import Handler, Inbox, Receiver


class ReceiverTests(unittest.TestCase):
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

    def upload(self, body=b"test audio", chunk_id=None, started="2026-09-10T12:00:00.000Z", **overrides):
        chunk_id = chunk_id or str(uuid.uuid4())
        headers = {
            "Authorization": "Bearer " + self.inbox.token,
            "X-Device-ID": self.device,
            "X-Started-At": started,
            "X-Duration-Seconds": "60.0",
            "X-Audio-SHA256": hashlib.sha256(body).hexdigest(),
            "Content-Type": "audio/mp4",
        }
        headers.update(overrides)
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("POST", "/v1/chunks/" + chunk_id, body, headers)
        response = client.getresponse()
        result = response.status, json.loads(response.read()), chunk_id
        client.close()
        return result

    def test_acknowledgment_survives_receiver_restart(self):
        status, receipt, chunk_id = self.upload()
        self.assertEqual(status, 201)
        self.assertTrue(receipt["durable"])
        reopened = Inbox(self.inbox.root)
        row = reopened.receipt(chunk_id)
        self.assertEqual(Path(row["path"]).read_bytes(), b"test audio")
        self.assertEqual(row["sha256"], receipt["sha256"])

    def test_lost_receipt_retry_is_idempotent_with_retained_audio(self):
        _, _, chunk_id = self.upload()
        self.inbox.complete(chunk_id, "A test sentence.")
        status, receipt, _ = self.upload(chunk_id=chunk_id)
        self.assertEqual(status, 200)
        self.assertTrue(receipt["durable"])
        self.assertEqual(self.inbox.status(), {"complete": 1})
        self.assertEqual((self.inbox.root / "life.md").read_text().count("A test sentence."), 1)
        self.assertEqual(len(list(self.inbox.audio.iterdir())), 1)
        self.assertEqual(self.inbox.receipt(chunk_id)["audio_state"], "present")

    def test_id_collision_rejected(self):
        _, _, chunk_id = self.upload()
        status, _, _ = self.upload(body=b"different recording", chunk_id=chunk_id)
        self.assertEqual(status, 409)
        self.assertEqual(Path(self.inbox.receipt(chunk_id)["path"]).read_bytes(), b"test audio")

    def test_checksum_failure_keeps_no_receipt(self):
        status, _, chunk_id = self.upload(**{"X-Audio-SHA256": "0" * 64})
        self.assertEqual(status, 422)
        self.assertIsNone(self.inbox.receipt(chunk_id))
        self.assertEqual(list(self.inbox.audio.iterdir()), [])

    def test_unauthorized_upload_rejected(self):
        status, _, _ = self.upload(**{"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(self.inbox.status(), {})

    def test_session_headings_follow_capture_gaps(self):
        _, _, first = self.upload(started="2026-09-10T16:00:00.000Z")
        self.inbox.complete(first, "Morning room talk.")
        _, _, same = self.upload(started="2026-09-10T16:08:00.000Z")
        self.inbox.complete(same, "Still the same sitting.")
        _, _, later = self.upload(started="2026-09-10T16:30:00.000Z")
        self.inbox.complete(later, "After a long pause.")
        text = (self.inbox.root / "life.md").read_text()
        self.assertIn("Sessions split after 15 minutes without captured audio.", text)
        self.assertIn("Speaker identity is not inferred yet.", text)
        self.assertEqual(text.count("## Session 1"), 1)
        self.assertEqual(text.count("## Session 2"), 1)
        self.assertLess(text.index("## Session 1"), text.index("Morning room talk."))
        self.assertLess(text.index("Still the same sitting."), text.index("## Session 2"))
        self.assertLess(text.index("## Session 2"), text.index("After a long pause."))

    def test_offline_backlog_is_exported_by_capture_time(self):
        _, _, later = self.upload(started="2026-09-10T16:00:00.000Z")
        self.inbox.complete(later, "Later audio.")
        _, _, earlier = self.upload(started="2026-09-09T16:00:00.000Z")
        self.inbox.complete(earlier, "Earlier audio.")
        text = (self.inbox.root / "life.md").read_text()
        self.assertLess(text.index("Earlier audio."), text.index("Later audio."))
        self.assertTrue((self.inbox.days / "2026-09-09.md").exists())
        self.assertTrue((self.inbox.days / "2026-09-10.md").exists())

    def test_export_splits_sessions_after_fifteen_minute_gap(self):
        _, _, first = self.upload(started="2026-09-10T12:00:00.000Z")
        self.inbox.complete(first, "First conversation.")
        _, _, second = self.upload(started="2026-09-10T12:10:00.000Z")
        self.inbox.complete(second, "Same conversation.")
        _, _, third = self.upload(started="2026-09-10T12:40:00.000Z")
        self.inbox.complete(third, "New conversation.")
        text = (self.inbox.days / "2026-09-10.md").read_text()
        self.assertEqual(text.count("## Session "), 2)
        self.assertIn("America/New_York", (self.inbox.root / "life.md").read_text())

    def test_untranscribed_audio_retained(self):
        _, _, chunk_id = self.upload()
        self.inbox.cleanup_completed()
        self.assertTrue(Path(self.inbox.receipt(chunk_id)["path"]).exists())

    def test_bad_metadata_and_path_rejected(self):
        for override in ({"X-Duration-Seconds": "NaN"}, {"X-Duration-Seconds": "601"},
                         {"X-Started-At": "2026-09-10"}, {"X-Device-ID": "../escape"}):
            self.assertEqual(self.upload(**override)[0], 400)
        self.assertEqual(self.upload(chunk_id="../escape")[0], 400)

    def test_concurrent_duplicate_uploads_get_one_receipt(self):
        chunk_id = str(uuid.uuid4())
        responses = []
        jobs = [threading.Thread(target=lambda: responses.append(self.upload(chunk_id=chunk_id)[0])) for _ in range(4)]
        for job in jobs: job.start()
        for job in jobs: job.join()
        self.assertEqual(sorted(responses), [200, 200, 200, 201])
        self.assertEqual(self.inbox.status(), {"pending": 1})

    def test_interrupted_upload_never_acknowledged(self):
        chunk_id = str(uuid.uuid4())
        headers = (f"POST /v1/chunks/{chunk_id} HTTP/1.1\r\nHost: localhost\r\n"
                   f"Authorization: Bearer {self.inbox.token}\r\nX-Device-ID: {self.device}\r\n"
                   "X-Started-At: 2026-09-10T12:00:00.000Z\r\nX-Duration-Seconds: 60\r\n"
                   f"X-Audio-SHA256: {hashlib.sha256(b'abcd').hexdigest()}\r\nContent-Length: 4\r\n\r\nab")
        with socket.create_connection(self.server.server_address) as client:
            client.sendall(headers.encode())
            client.shutdown(socket.SHUT_WR)
            response = client.recv(4096)
        self.assertIn(b"400", response)
        self.assertIsNone(self.inbox.receipt(chunk_id))


    def _fd_count(self):
        return len(os.listdir("/dev/fd"))

    def test_repeated_connect_does_not_leak_fds(self):
        before = self._fd_count()
        for _ in range(80):
            with self.inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)
        self.assertLessEqual(self._fd_count() - before, 4)

    def test_connect_closes_after_pragma_exception(self):
        opened = []
        real_connect = receiver_mod.sqlite3.connect

        class Stub:
            def __init__(self):
                self.closed = False
                self.row_factory = None

            def execute(self, sql, *args, **kwargs):
                if str(sql).startswith("PRAGMA"):
                    raise sqlite3.OperationalError("injected")
                return self

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def wrapped(*args, **kwargs):
            db = Stub()
            opened.append(db)
            return db

        receiver_mod.sqlite3.connect = wrapped
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with self.inbox.connect() as db:
                    db.execute("SELECT 1")
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
        finally:
            receiver_mod.sqlite3.connect = real_connect


if __name__ == "__main__":
    unittest.main()
