import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Handler, Inbox, Receiver


class ProcessingStatusAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.server = Receiver(("127.0.0.1", 0), Handler)
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device = str(uuid.uuid4())
        self.chunk = str(uuid.uuid4())
        audio = self.inbox.audio / (self.chunk + ".m4a")
        audio.write_bytes(b"private audio")
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO chunks
                   (id,sha256,device,started,duration,path,received,status,transcript)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (self.chunk, "a" * 64, self.device, "2026-09-24T12:00:00.000Z", 60.0,
                 str(audio), 1000.0, "pending", "private transcript"),
            )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, token=None, device=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {
            "Authorization": "Bearer " + (self.inbox.token if token is None else token),
            "X-Device-ID": self.device if device is None else device,
        }
        client.request("GET", path, headers=headers)
        response = client.getresponse()
        status, body = response.status, response.read()
        client.close()
        return status, body

    def test_device_scoped_status_omits_private_fields(self):
        status, body = self.request("/v1/chunks/status?ids=" + self.chunk)
        self.assertEqual(status, 200)
        record = json.loads(body)["chunks"][0]
        self.assertEqual(record["id"], self.chunk)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["received_at"], 1000.0)
        self.assertEqual(record["retry_mode"], "automatic")
        report = json.loads(body)["health"]
        self.assertEqual(report["processing"]["received"], 1)
        self.assertEqual(report["processing"]["complete"], 0)
        self.assertEqual(set(report), {"version", "checked_at", "processing"})
        for private in (b"private audio", b"private transcript", b"path", b"sha256", self.device.encode()):
            self.assertNotIn(private, body)
        self.inbox.complete(self.chunk, "private transcript")
        _, body = self.request("/v1/chunks/status?ids=" + self.chunk)
        self.assertEqual(json.loads(body)["chunks"][0]["status"], "complete")
        self.assertNotIn(b"private transcript", body)

    def test_wrong_device_and_unknown_id_do_not_disclose_existence(self):
        other = str(uuid.uuid4())
        _, wrong_device = self.request("/v1/chunks/status?ids=" + self.chunk, device=other)
        _, unknown = self.request("/v1/chunks/status?ids=" + other)
        self.assertEqual(json.loads(wrong_device)["chunks"][0]["status"], "unknown")
        self.assertEqual(json.loads(wrong_device)["health"]["processing"]["received"], 0)
        self.assertEqual(json.loads(unknown)["chunks"][0]["status"], "unknown")

    def test_attention_is_visible_as_recoverable_not_complete(self):
        with self.inbox.connect() as db:
            db.execute(
                """UPDATE chunks SET status='needs_attention', attempts=5,
                   error='decode_empty', error_code='decode_empty', error_stage='decode', attention_at=1234
                   WHERE id=?""", (self.chunk,),
            )
        status, body = self.request("/v1/chunks/status?ids=" + self.chunk)
        self.assertEqual(status, 200)
        record = json.loads(body)["chunks"][0]
        self.assertEqual(record["status"], "needs_attention")
        self.assertEqual(record["error_code"], "decode_empty")
        self.assertEqual(record["retry_mode"], "manual")
        self.assertTrue(record["retry_eligible"])
        self.assertNotIn(b"private transcript", body)

    def test_auth_and_bounds(self):
        self.assertEqual(self.request("/v1/chunks/status?ids=" + self.chunk, token="wrong")[0], 401)
        self.assertEqual(self.request("/v1/chunks/status?ids=bad")[0], 400)
        self.assertEqual(self.request("/v1/chunks/status?ids=" + self.chunk + "," + self.chunk)[0], 400)
        many = ",".join(str(uuid.uuid4()) for _ in range(11))
        self.assertEqual(self.request("/v1/chunks/status?ids=" + many)[0], 400)
        self.assertEqual(self.request("/v1/chunks/status?ids=" + self.chunk, device="bad")[0], 400)


if __name__ == "__main__":
    unittest.main()
