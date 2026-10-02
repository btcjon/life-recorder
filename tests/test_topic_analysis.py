import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import Inbox
import topic_analysis as topics
import timeline
import grok_topics


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.inbox = Inbox(root / "inbox")
        self.auth_file = root / "auth.json"
        self.auth_file.write_text(json.dumps({"xai": {"type": "api_key", "key": "synthetic fixture, never transmitted"}}))
        self.auth_file.chmod(0o600)
        self.config = topics.Config(True, self.auth_file, "grok-4.7")
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
        def invoke(auth_file, model, prompt):
            if fail:
                raise grok_topics.RouteError("provider_unavailable")
            provenance = grok_topics.preflight(auth_file, model) | {"effective_model": model,
                "response_id": "fixture-response", "verified_at": time.time(),
                "input_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
            return json.dumps({"segments": [
                {"start_clip_id": "clip0", "end_clip_id": "clip1", "title": "Plan"},
                {"start_clip_id": "clip2", "end_clip_id": "clip3", "title": "Delivery"}]}), provenance
        return Mock(side_effect=invoke)

    def test_disabled_missing_and_unverified_routes_do_not_send_text(self):
        runner = self.runner()
        self.assertEqual(topics.run_once(self.inbox, topics.Config(), runner=runner)["state"], "disabled")
        self.assertEqual(topics.run_once(self.inbox, topics.Config(True, Path("/missing"), "grok-4.7"), runner=runner)["state"], "unavailable")
        self.assertEqual(topics.run_once(self.inbox, topics.Config(True, self.auth_file, "grok-4.7", "legacy-cli"), runner=runner)["error_code"], "route_unavailable")
        runner.assert_not_called()

    def test_settle_one_job_transcript_only_provenance_and_no_model_on_read(self):
        runner = self.runner()
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1119, runner=runner)["state"], "idle")
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1120, runner=runner)["suggestion_count"], 2)
        self.assertEqual(runner.call_count, 1)
        call = runner.call_args
        self.assertEqual(call.args[:2], (self.auth_file, "grok-4.7"))
        payload = json.loads(call.args[2])
        self.assertEqual(set(payload), {"instruction", "clips"})
        self.assertTrue(all(set(clip) == {"clip_id", "text"} for clip in payload["clips"]))
        for private in ("private device", "private audio path", "private checksum"):
            self.assertNotIn(private, call.args[2])
        with self.inbox.connect() as db:
            self.assertEqual(len(timeline.list_timeline(db)["suggestions"]), 2)
            self.assertEqual(db.execute("SELECT model FROM topic_jobs").fetchone()[0], "grok-4.7")
            proof = json.loads(db.execute("SELECT provenance FROM topic_jobs").fetchone()[0])
            self.assertEqual(proof["effective_model"], "grok-4.7")
            self.assertEqual(proof["response_id"], "fixture-response")
            self.assertIn("source_fingerprint", proof)
        self.assertEqual(topics.run_once(self.inbox, self.config, now=2000, runner=runner)["state"], "idle")

    def test_retry_budget_is_bounded_and_errors_sanitized(self):
        runner = self.runner(fail=True)
        for now in (1120, 2020, 2920):
            result = topics.run_once(self.inbox, self.config, now=now, runner=runner)
            self.assertEqual(result, {"state": "failed", "error_code": "provider_unavailable"})
        self.assertEqual(topics.run_once(self.inbox, self.config, now=4000, runner=runner)["state"], "idle")
        self.assertEqual(runner.call_count, 3)
        with self.inbox.connect() as db:
            row = db.execute("SELECT attempts,error_code FROM topic_jobs").fetchone()
            self.assertEqual(tuple(row), (3, "provider_unavailable"))

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

    def test_unsafe_auth_and_legacy_receipts_never_send_text(self):
        self.auth_file.chmod(0o644)
        runner = self.runner()
        result = topics.run_once(self.inbox, self.config, runner=runner)
        self.assertEqual(result["error_code"], "unsafe_auth_file")
        runner.assert_not_called()

    def test_mismatched_request_provenance_rejected(self):
        good = self.runner()
        def runner(*args):
            raw, proof = good(*args)
            proof['effective_model'] = 'different-model'
            return raw, proof
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1120, runner=runner),
                         {'state': 'failed', 'error_code': 'model_unverified'})
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_suggestions').fetchone()[0], 0)

    def test_stale_source_and_global_running_job_preserve_other_work(self):
        good = self.runner()
        def runner(*args):
            with self.inbox.connect() as db:
                db.execute("UPDATE chunks SET transcript='changed' WHERE id='clip0'")
            return good(*args)
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1120, runner=runner),
                         {'state': 'failed', 'error_code': 'source_changed'})
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM topic_suggestions').fetchone()[0], 0)
            db.execute("UPDATE topic_jobs SET state='running',updated_at=1120")
        not_called = Mock()
        self.assertEqual(topics.run_once(self.inbox, self.config, now=1121, runner=not_called), {'state': 'busy'})
        not_called.assert_not_called()
