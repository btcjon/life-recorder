import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Inbox
import topic_analysis as topics
import timeline


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.inbox = Inbox(root / "inbox")
        self.executable = root / "mock-grok"
        self.executable.write_bytes(b"test fixture, never executed")
        self.executable.chmod(0o700)
        receipt = {"executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                   "provider": "xai", "effective_model": "grok-4.7", "tools": False, "web_search": False,
                   "verified_at": time.time()}
        self.config = topics.Config(True, self.executable, "grok-4.7", receipt)
        with self.inbox.connect() as db:
            topics.ensure_schema(db)
            for index in range(4):
                db.execute("""INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,transcript,completed_at)
                    VALUES (?,?,?,?,?,?,?,'complete',?,?)""",
                    ("clip" + str(index), "private checksum", "private device", f"2026-10-01T12:0{index}:00.000Z", 60,
                     "private audio path", 1000, "Project discussion " + str(index), 1000))

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, fail=False):
        def invoke(command, **kwargs):
            if "--help" in command:
                return SimpleNamespace(returncode=0, stdout="--model --disable-tools --disable-web-search --prompt-file")
            if fail:
                return SimpleNamespace(returncode=1, stdout="private unsafe output", stderr="private failure")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"segments": [
                {"start_clip_id": "clip0", "end_clip_id": "clip1", "title": "Plan"},
                {"start_clip_id": "clip2", "end_clip_id": "clip3", "title": "Delivery"}]}))
        return Mock(side_effect=invoke)

    def test_disabled_missing_and_unverified_routes_do_not_send_text(self):
        runner = self.runner()
        self.assertEqual(topics.run_once(self.inbox, topics.Config(), runner=runner)["state"], "disabled")
        self.assertEqual(topics.run_once(self.inbox, topics.Config(True, Path("/missing"), "grok-4.7"), runner=runner)["state"], "unavailable")
        self.assertEqual(topics.run_once(self.inbox, topics.Config(True, self.executable, "grok-4.7"), runner=runner)["error_code"], "model_unverified")
        runner.assert_not_called()

    def test_settle_one_job_stdin_provenance_and_no_model_on_read(self):
        runner = self.runner()
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1119, runner=runner)["state"], "idle")
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1120, runner=runner)["suggestion_count"], 2)
        model_calls = [c for c in runner.call_args_list if "--help" not in c.args[0]]
        self.assertEqual(len(model_calls), 1)
        call = model_calls[0]
        self.assertIn("--model", call.args[0])
        self.assertIn("--disable-tools", call.args[0])
        self.assertIn("--disable-web-search", call.args[0])
        self.assertIn("/dev/stdin", call.args[0])
        self.assertNotIn("Project discussion", " ".join(call.args[0]))
        for private in ("private device", "private audio path", "private checksum"):
            self.assertNotIn(private, call.kwargs["input"])
        with self.inbox.connect() as db:
            self.assertEqual(len(timeline.list_timeline(db)["suggestions"]), 2)
            self.assertEqual(db.execute("SELECT model FROM topic_jobs").fetchone()[0], "grok-4.7")
        self.assertEqual(topics.run_once(self.inbox, self.config, now=2000, runner=runner)["state"], "idle")

    def test_retry_budget_is_bounded_and_errors_sanitized(self):
        runner = self.runner(fail=True)
        for now in (1120, 2020, 2920):
            result = topics.run_once(self.inbox, self.config, now=now, runner=runner)
            self.assertEqual(result, {"state": "failed", "error_code": "model_failed"})
        self.assertEqual(topics.run_once(self.inbox, self.config, now=4000, runner=runner)["state"], "idle")
        self.assertEqual(len([c for c in runner.call_args_list if "--help" not in c.args[0]]), 3)
        with self.inbox.connect() as db:
            row = db.execute("SELECT attempts,error_code FROM topic_jobs").fetchone()
            self.assertEqual(tuple(row), (3, "model_failed"))

    def test_unknown_missing_nonadjacent_duplicate_or_extra_boundaries_rejected(self):
        ids = ["a", "b", "c"]
        for segments in ([{"start_clip_id": "a", "end_clip_id": "unknown", "title": "One"}],
                         [{"start_clip_id": "a", "end_clip_id": "a", "title": "One"}, {"start_clip_id": "c", "end_clip_id": "c", "title": "Two"}],
                         [{"start_clip_id": "a", "end_clip_id": "b", "title": "One"}],
                         [{"start_clip_id": "a", "end_clip_id": "c", "title": "One", "coordinates": []}]):
            with self.assertRaises(topics.TopicError):
                topics.validate_result(json.dumps({"segments": segments}), ids)

    def test_fingerprint_changes_with_model_source_and_prompt(self):
        with self.inbox.connect() as db:
            rows = list(topics.windows(db))[0]
        self.assertNotEqual(topics.fingerprint(rows, "one"), topics.fingerprint(rows, "two"))
        changed = [dict(r) for r in rows]
        changed[0]["transcript"] = "Different source"
        self.assertNotEqual(topics.fingerprint(rows, "one"), topics.fingerprint(changed, "one"))

    def test_failed_window_does_not_block_other_windows(self):
        with self.inbox.connect() as db:
            db.execute("""INSERT INTO chunks(id,sha256,device,started,duration,path,received,status,transcript,completed_at)
                VALUES ('other','hash','second','2026-09-30T12:00:00Z',60,'private',1000,'complete','Other topic',1000)""")
        runner = self.runner(fail=True)
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1120, runner=runner)["state"], "failed")
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1121, runner=runner)["state"], "failed")
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM topic_jobs").fetchone()[0], 2)

    def test_stale_route_receipt_never_runs_cli(self):
        receipt = dict(self.config.route_receipt, verified_at=time.time() - 301)
        runner = self.runner()
        result = topics.run_once(self.inbox, topics.Config(True, self.executable, "grok-4.7", receipt), runner=runner)
        self.assertEqual(result["error_code"], "model_unverified")
        runner.assert_not_called()
