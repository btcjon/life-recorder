import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("voice_diagnostics", Path(__file__).resolve().parents[1] / "scripts/voice-diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class DiagnosticsTests(unittest.TestCase):
    def test_aggregate_diagnostic_is_read_only_and_redacts_error_text(self):
        from receiver import Inbox
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            with inbox.connect() as db:
                db.execute("INSERT INTO voice_jobs VALUES ('private-clip','private-reason',10,2,'private transcript / path')")
            with sqlite3.connect(inbox.db.resolve().as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                before = db.total_changes
                report = diagnostics.diagnose(db, now=20)
                self.assertEqual(db.total_changes, before)
                self.assertEqual(report["error_classes"], {"redacted": 1})
                self.assertEqual(report["queue"][0]["oldest_seconds"], 10)
                self.assertEqual(report["queue"][0]["reason"], "other")
                self.assertNotIn("private", json.dumps(report))
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("DELETE FROM voice_jobs")
