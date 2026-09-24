import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import viewer
from agent_support import ViewerCase


class ProcessingRetryViewerTests(ViewerCase):
    def setUp(self):
        super().setUp()
        self.chunk_id = str(uuid.uuid4())
        self.audio = self.inbox.audio / (self.chunk_id + ".m4a")
        self.audio.write_bytes(b"kept-audio")
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO chunks
                   (id,sha256,device,started,duration,path,received,status,attempts,
                    error,error_code,error_stage,attention_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-22T15:00:00Z", 60.0,
                 str(self.audio), time.time(), "needs_attention", 9,
                 "asr_missing_output", "asr_missing_output", "asr", time.time()),
            )

    def _retry(self, body=None):
        return self.request("POST", f"/v1/chunks/{self.chunk_id}/retry", {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }, json.dumps({} if body is None else body).encode())

    def test_explicit_retry_preserves_audio_and_resets_budget(self):
        status, raw, _ = self._retry()
        self.assertEqual(status, 200)
        report = json.loads(raw)
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["attempts"], 0)
        self.assertIsNone(report["error_code"])
        self.assertNotIn("transcript", report)
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")
        self.assertIn("Retry processing", viewer.JS)
        self.assertIn('chunk.status !== "needs_attention"', viewer.JS)
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET status='complete',transcript='do not overwrite' WHERE id=?", (self.chunk_id,))
        status, raw, _ = self._retry()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "complete")
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT transcript FROM chunks WHERE id=?", (self.chunk_id,)).fetchone()[0],
                             "do not overwrite")

    def test_missing_audio_denies_retry_without_changing_state(self):
        self.audio.unlink()
        status, raw, _ = self._retry()
        self.assertEqual(status, 409)
        with self.inbox.connect() as db:
            row = db.execute("SELECT status, attempts FROM chunks WHERE id=?", (self.chunk_id,)).fetchone()
        self.assertEqual(tuple(row), ("needs_attention", 9))
        self.assertFalse(self.inbox.processing_record(self.chunk_id)["retry_eligible"])

    def test_retry_requires_human_auth_and_empty_body(self):
        status, _, _ = self.request("POST", f"/v1/chunks/{self.chunk_id}/retry", {
            "Content-Type": "application/json",
        }, b"{}")
        self.assertEqual(status, 401)
        status, _, _ = self._retry({"force": True})
        self.assertEqual(status, 400)
        with mock.patch.dict("os.environ", {"LIFE_RECORDER_AGENT_CLIENT_IDS": self.client_id}):
            status, _, _ = self.request("POST", f"/v1/chunks/{self.chunk_id}/retry", {
                "Host": "lr.genr8ive.ai", "Cf-Access-Jwt-Assertion": self.machine_token(),
                "Content-Type": "application/json",
            }, b"{}")
        self.assertEqual(status, 403)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM chunks WHERE id=?", (self.chunk_id,)).fetchone()[0],
                             "needs_attention")


if __name__ == "__main__":
    unittest.main()
