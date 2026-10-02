import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Inbox
import health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, identifier, device, status, received, attempts=0, text="private words"):
        with self.inbox.connect() as db:
            db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,attempts,transcript)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (identifier, "secret hash", device, "2026-10-02T12:00:00Z", 60,
                        "private path", received, status, attempts, text))

    def test_idle_and_quiet_completion_are_healthy(self):
        report = self.inbox.health_snapshot(now=1000)
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["processing"]["state"], "idle")
        self.assertIsNotNone(report["agent_index"]["last_reconciled_at"])
        self.insert("quiet", "device", "complete", 900, text="")
        report = self.inbox.health_snapshot(now=1000)
        self.assertEqual(report["processing"]["complete"], 1)
        self.assertEqual(report["state"], "ok")

    def test_delay_and_retry_are_distinct_from_receipts(self):
        self.insert("a", "device", "pending", 400, attempts=2)
        report = self.inbox.health_snapshot(now=999)["processing"]
        self.assertFalse(report["delayed"])
        report = self.inbox.health_snapshot(now=1000)["processing"]
        self.assertTrue(report["delayed"])
        self.assertEqual(report["retrying"], 1)
        self.assertEqual(report["received"], 1)
        self.assertEqual(report["complete"], 0)
        self.assertEqual(report["oldest_pending_age_seconds"], 600)
        self.insert("b", "device", "needs_attention", 500)
        self.assertEqual(self.inbox.health_snapshot(now=1000)["state"], "needs_attention")

    def test_device_report_contains_only_own_queue(self):
        self.insert("a", "own", "complete", 900)
        self.insert("b", "other", "needs_attention", 300)
        report = self.inbox.health_snapshot(device_id="own", now=1000)
        self.assertEqual(report["processing"]["received"], 1)
        self.assertEqual(report["processing"]["needs_attention"], 0)
        self.assertEqual(set(report), {"version", "checked_at", "processing"})
        body = json.dumps(report)
        for secret in ("private words", "private path", "secret hash", "other"):
            self.assertNotIn(secret, body)

    def test_storage_and_revision_are_observed_facts(self):
        revision = self.inbox.health_source_revision
        with patch.object(health, "source_revision", return_value="changed"), \
             patch.object(health.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1})()):
            report = self.inbox.health_snapshot()
        self.assertEqual(report["runtime"]["source_revision"], revision)
        self.assertTrue(report["storage"]["low_space"])
        self.assertEqual(report["state"], "needs_attention")
        with patch.object(health.shutil, "disk_usage", side_effect=OSError):
            self.assertEqual(self.inbox.health_snapshot()["storage"]["state"], "unavailable")

    def test_failed_transaction_does_not_claim_reconciliation(self):
        previous = self.inbox.last_index_reconciled_at
        with self.assertRaises(RuntimeError):
            with self.inbox.connect() as db:
                self.inbox._reconcile_index(db)
                raise RuntimeError("rollback")
        self.assertEqual(self.inbox.last_index_reconciled_at, previous)

    def test_audio_usage_uses_managed_originals_and_cached_sizes(self):
        self.insert("a", "own", "complete", 900)
        self.insert("b", "own", "complete", 900)
        audio = self.inbox.audio / "original.m4a"
        audio.write_bytes(b"123456")
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET path=?,audio_pinned=1 WHERE id='a'", (str(audio),))
            db.execute("UPDATE chunks SET audio_bytes=4 WHERE id='b'")
        usage = self.inbox.health_snapshot()["storage"]
        self.assertEqual(usage["original_audio_bytes"], 10)
        self.assertEqual(usage["pinned_audio_bytes"], 6)
        self.assertEqual(usage["pinned_count"], 1)
        self.assertTrue(usage["audio_usage_complete"])

    def test_unknown_audio_size_does_not_probe_outside_runtime(self):
        self.insert("a", "own", "complete", 900)
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET path=? WHERE id='a'", (str(Path(__file__).resolve()),))
        usage = self.inbox.health_snapshot()["storage"]
        self.assertEqual(usage["original_audio_bytes"], 0)
        self.assertFalse(usage["audio_usage_complete"])
