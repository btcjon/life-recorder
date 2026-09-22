import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import event_summaries
from receiver import Inbox


def _words(count, token="alpha"):
    return " ".join(f"{token}{index}" for index in range(count))


class SummaryPolicyTests(unittest.TestCase):
    def test_fingerprint_changes_with_content_not_speakers(self):
        pairs = [("a", "hello there"), ("b", "second clip")]
        original = event_summaries.fingerprint(pairs)
        self.assertEqual(original, event_summaries.fingerprint(list(pairs)))
        self.assertNotEqual(original, event_summaries.fingerprint([("a", "hello there!"), ("b", "second clip")]))
        self.assertNotEqual(original, event_summaries.fingerprint([("b", "second clip"), ("a", "hello there")]))
        self.assertNotEqual(original, event_summaries.fingerprint(pairs, prompt="other"))
        self.assertNotEqual(original, event_summaries.fingerprint(pairs, sampling="other"))
        self.assertNotEqual(original, event_summaries.fingerprint(pairs, model="other"))

    def test_sixteen_minute_transcript_is_sent_whole(self):
        text = _words(1833, "clip")
        prompt, coverage = event_summaries.build_prompt([("a", text)])
        self.assertEqual(coverage, "full")
        self.assertIn("clip0", prompt)
        self.assertIn("clip1832", prompt)
        self.assertNotIn("[beginning]", prompt)
        self.assertLessEqual(len(prompt.encode()), event_summaries.MAX_INPUT_BYTES)

    def test_ninety_seven_minute_transcript_under_the_budget_is_sent_whole(self):
        text = " ".join(["talk"] * 12650)
        prompt, coverage = event_summaries.build_prompt([("a", text)])
        self.assertEqual(coverage, "full")
        self.assertIn(text, prompt)
        self.assertLessEqual(len(prompt.encode()), event_summaries.MAX_INPUT_BYTES)

    def test_over_budget_transcript_is_sampled_within_the_input_cap(self):
        text = "word " * 40000
        prompt, coverage = event_summaries.build_prompt([("a", text)])
        self.assertEqual(coverage, "sampled")
        self.assertIn("[beginning]", prompt)
        self.assertIn("[middle]", prompt)
        self.assertIn("[end]", prompt)
        self.assertLessEqual(len(prompt.encode()), event_summaries.MAX_INPUT_BYTES)
        self.assertNotIn(text, prompt)

    def test_output_limits(self):
        self.assertEqual(event_summaries.validate_summary("  They compared notes.  "), "They compared notes.")
        self.assertIsNone(event_summaries.validate_summary("   "))
        self.assertIsNone(event_summaries.validate_summary("One. Two. Three. Four."))
        self.assertIsNone(event_summaries.validate_summary(" ".join(["word"] * 61)))
        self.assertIsNotNone(event_summaries.validate_summary(" ".join(["word"] * 60)))

    def test_command_keeps_the_transcript_off_the_argument_list(self):
        secret = "private household sentence"
        command = event_summaries.grok_command(Path("/tmp/lr-summary-test"))
        self.assertIn("/dev/stdin", command)
        self.assertNotIn(secret, command)
        self.assertIn("--max-turns", command)
        self.assertIn("--disable-web-search", command)

    def test_timeout_reaps_the_process(self):
        with self.assertRaises(event_summaries.SummaryError) as caught:
            event_summaries.run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                "synthetic",
                0.2,
            )
        self.assertEqual(caught.exception.code, "timeout")


class SummaryWorkerTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("LIFE_RECORDER_REMOTE_SUMMARIES", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.tmp.name))
        self.device = str(uuid.uuid4())

    def tearDown(self):
        self.tmp.cleanup()
        if self._saved is None:
            os.environ.pop("LIFE_RECORDER_REMOTE_SUMMARIES", None)
        else:
            os.environ["LIFE_RECORDER_REMOTE_SUMMARIES"] = self._saved

    def _chunk(self, started, transcript, received, chunk_id=None):
        chunk_id = chunk_id or str(uuid.uuid4())
        path = self.inbox.audio / (chunk_id + ".m4a")
        path.write_bytes(b"audio")
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO chunks
                   (id, sha256, device, started, duration, path, received, status, transcript, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (chunk_id, uuid.uuid4().hex, self.device, started, 60, str(path), received,
                 "complete", transcript, received),
            )
        return chunk_id

    def _person(self, name, chunk_id, source, speaker_key="S1"):
        person_id = str(uuid.uuid4())
        now = time.time()
        with self.inbox.connect() as db:
            db.execute(
                "INSERT INTO people (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                (person_id, name, now, now),
            )
            db.execute(
                """INSERT INTO speaker_turns
                   (id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), str(uuid.uuid4()), chunk_id, speaker_key, 0, 10, person_id, source),
            )
        return person_id

    def test_badges_prefer_confirmed_and_ignore_unnamed_keys(self):
        os.environ["LIFE_RECORDER_REMOTE_SUMMARIES"] = "1"
        first = self._chunk("2026-09-22T13:00:00Z", "hello from the porch", 1)
        second = self._chunk("2026-09-22T13:01:00Z", "and the garden", 1)
        self._person("Jon Bennett", first, "confirmed", "S1")
        self._person("Nicole Bennett", second, None, "S2")
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO speaker_turns
                   (id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), str(uuid.uuid4()), first, "S9", 10, 20, None, None),
            )
            blocks = event_summaries.decorate(
                db,
                event_summaries.viewer_mod.display_blocks([
                    {"id": first, "started": "2026-09-22T13:00:00Z", "duration": 60, "transcript": "hello from the porch"},
                    {"id": second, "started": "2026-09-22T13:01:00Z", "duration": 60, "transcript": "and the garden"},
                ]),
                {
                    first: {"transcript": "hello from the porch"},
                    second: {"transcript": "and the garden"},
                },
            )
        event = blocks[0]
        self.assertEqual([(item["name"], item["confirmed"]) for item in event["speakers"]], [
            ("Jon Bennett", True),
            ("Nicole Bennett", False),
        ])
        self.assertNotIn("S9", str(event["speakers"]))

    def test_day_load_does_not_write_or_call_the_model(self):
        first = self._chunk("2026-09-22T13:00:00Z", "hello from the porch", 1)
        self._chunk("2026-09-22T13:01:00Z", "and the garden", 1)
        self._person("Karen Strickland", first, "confirmed")
        with mock.patch.object(event_summaries, "run_grok", side_effect=AssertionError("model")):
            payload = self.inbox.viewer_day("2026-09-22")
        with self.inbox.connect() as db:
            stored = db.execute("SELECT COUNT(*) FROM event_summaries").fetchone()[0]
        self.assertEqual(stored, 0)
        self.assertNotIn("speakers", payload["chunks"][0])
        event = next(block for block in payload["display_blocks"] if block["kind"] == "event")
        self.assertEqual(event["speakers"][0]["name"], "Karen Strickland")
        self.assertEqual(event["summary_state"], "off")

    def test_worker_caches_one_newest_event_and_retries_failures(self):
        os.environ["LIFE_RECORDER_REMOTE_SUMMARIES"] = "1"
        early = time.time() - 1000
        self._chunk("2026-09-22T13:00:00Z", "morning porch", early)
        self._chunk("2026-09-22T13:01:00Z", "morning garden", early)
        self._chunk("2026-09-22T15:00:00Z", "afternoon tools", early)
        self._chunk("2026-09-22T15:01:00Z", "afternoon paint", early)
        calls = []

        def runner(prompt):
            calls.append(prompt)
            if prompt.count("afternoon paint") and sum("afternoon paint" in item for item in calls) == 1:
                raise event_summaries.SummaryError("exit")
            return "They talked about paint."

        now = time.time()
        self.assertEqual(event_summaries.step(self.inbox, runner=runner, now=now), "failed")
        self.assertIn("afternoon paint", calls[0])
        self.assertNotIn("morning porch", calls[0])
        self.assertEqual(event_summaries.step(self.inbox, runner=runner, now=now), "ready")
        self.assertIn("morning porch", calls[1])
        self.assertEqual(event_summaries.step(self.inbox, runner=runner, now=now), "idle")
        later = now + event_summaries.RETRY_SECONDS + 1
        self.assertEqual(event_summaries.step(self.inbox, runner=runner, now=later), "ready")
        self.assertIn("afternoon paint", calls[2])
        self.assertEqual(event_summaries.step(self.inbox, runner=runner, now=later), "idle")
        with self.inbox.connect() as db:
            rows = db.execute("SELECT status, coverage, summary FROM event_summaries").fetchall()
        self.assertEqual([row["status"] for row in rows], ["ready", "ready"])
        self.assertEqual({row["coverage"] for row in rows}, {"full"})
        self.assertEqual({row["summary"] for row in rows}, {"They talked about paint."})

    def test_fresh_and_changed_events_are_not_published(self):
        os.environ["LIFE_RECORDER_REMOTE_SUMMARIES"] = "1"
        now = time.time()
        fresh_a = self._chunk("2026-09-22T18:00:00Z", "still recording", now)
        self._chunk("2026-09-22T18:01:00Z", "still going", now)
        calls = []
        self.assertEqual(
            event_summaries.step(self.inbox, runner=lambda prompt: calls.append(prompt), now=now),
            "idle",
        )
        self.assertEqual(calls, [])
        settled = now - 1000
        with self.inbox.connect() as db:
            db.execute("UPDATE chunks SET received=?, completed_at=? WHERE id=?", (settled, settled, fresh_a))
            db.execute(
                "UPDATE chunks SET received=?, completed_at=? WHERE started=?",
                (settled, settled, "2026-09-22T18:01:00Z"),
            )

        def change(prompt):
            calls.append(prompt)
            with self.inbox.connect() as db:
                db.execute("UPDATE chunks SET transcript=? WHERE id=?", ("a different conversation", fresh_a))
            return "They discussed tools."

        self.assertEqual(event_summaries.step(self.inbox, runner=change, now=now), "stale")
        with self.inbox.connect() as db:
            ready = db.execute("SELECT COUNT(*) FROM event_summaries WHERE status='ready'").fetchone()[0]
        self.assertEqual(ready, 0)

    def test_disabled_flag_skips_the_model(self):
        self._chunk("2026-09-22T13:00:00Z", "hello", 1)
        self._chunk("2026-09-22T13:01:00Z", "there", 1)
        calls = []
        self.assertEqual(event_summaries.step(self.inbox, runner=lambda prompt: calls.append(prompt)), "off")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
