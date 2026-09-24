import contextlib
import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diarization as diarization_mod
import speaker_review
import voice_id
import viewer
from agent_support import ViewerCase
from receiver import Inbox

TOKEN = "SECRET_TRANSCRIPT_TOKEN"
SENTINEL = "0.123456789"
ITEM_KEYS = {
    "turn_id", "group_id", "group_turn_ids", "chunk_id", "speaker_key",
    "clip_started", "started", "ended", "audio_usable", "quality", "clean_seconds",
    "label_source", "stored_person_id", "stored_name", "suggestions",
    "suggestion_score", "suggestion_margin", "reasons", "confirmed",
}


def _vec(index=0, value=1.0):
    vector = [0.0] * 256
    vector[index] = value
    return vector


def _sentinel_vec():
    vector = _vec(0)
    vector[2] = float(SENTINEL)
    return vector


def _insert_chunk(inbox, chunk_id, started="2026-09-20T12:00:00.000Z", duration=20.0, transcript=""):
    dest = inbox.audio / (chunk_id + ".m4a")
    dest.write_bytes(b"audio")
    with inbox.connect() as db:
        db.execute(
            """INSERT INTO chunks
               (id,sha256,device,started,duration,path,received,status,audio_state,transcript,words_json,diarization_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chunk_id, uuid.uuid4().hex, str(uuid.uuid4()), started, duration, str(dest), 0,
             "complete", "present", transcript, "[]", "success"),
        )
    return dest


def _result(turns):
    return {
        "turns": turns,
        "speaker_count": len({turn["speaker_key"] for turn in turns}),
        "processing_seconds": 0.1,
        "outcome": "success",
        "speech_seconds": sum(turn["ended"] - turn["started"] for turn in turns),
        "coverage": 0.5,
        "turn_count": len(turns),
        "embedding_count": len(turns),
        "cluster_count": len({turn["speaker_key"] for turn in turns}),
        "asr_words": 4,
    }


def _turn(speaker, start, end, embedding, quality=1.0):
    return {"speaker_key": speaker, "started": start, "ended": end, "quality": quality, "embedding": embedding}


def _open_clip(inbox, embedding, started, turns=None, transcript=""):
    chunk_id = str(uuid.uuid4())
    _insert_chunk(inbox, chunk_id, started, transcript=transcript)
    diarization_mod.save_result(inbox, chunk_id, _result(turns or [_turn("S1", 0.0, 8.0, embedding)]))
    return chunk_id


def _label(inbox, chunk_id, person_id, speaker="S1"):
    with inbox.connect() as db:
        turn_id = db.execute(
            "SELECT id FROM speaker_turns WHERE chunk_id=? AND speaker_key=? ORDER BY started",
            (chunk_id, speaker),
        ).fetchone()[0]
    inbox.label_turn(turn_id, person_id, use_sample=True)
    return turn_id


def _person(inbox, person_id, name):
    now = time.time()
    with inbox.connect() as db:
        db.execute(
            "INSERT INTO people(id,name,created_at,updated_at) VALUES(?,?,?,?)",
            (person_id, name, now, now),
        )


def _snapshot(db):
    return {
        "turns": [
            (row["id"], row["person_id"], row["label_source"])
            for row in db.execute("SELECT id, person_id, label_source FROM speaker_turns ORDER BY id")
        ],
        "samples": db.execute("SELECT count(*) FROM voice_samples").fetchone()[0],
        "enrolled": db.execute("SELECT count(*) FROM voice_vectors WHERE enrolled=1").fetchone()[0],
        "calibration": [
            (row["id"], row["status"], row["updated_at"])
            for row in db.execute("SELECT id, status, updated_at FROM voice_calibration")
        ],
        "jobs": db.execute("SELECT count(*) FROM voice_jobs").fetchone()[0],
    }


def _group_id(turn_ids):
    joined = ",".join(sorted(turn_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


class SpeakerReviewTests(unittest.TestCase):
    def test_held_out_diagnostics_are_aggregate_and_read_only(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Private Name")
            for minute in (0, 5, 10):
                clip = _open_clip(inbox, _vec(0), f"2026-09-20T12:{minute:02d}:00.000Z")
                _label(inbox, clip, person["id"])
            with inbox.connect() as db:
                before = _snapshot(db)
                report = speaker_review.diagnose_held_out(db)
                after = _snapshot(db)
            self.assertEqual(before, after)
            self.assertEqual(report["groups"], 3)
            self.assertEqual(report["rank1_correct_groups"], 3)
            self.assertEqual(report["rejection_reasons"]["accepted"], 3)
            self.assertEqual(report["same_recording_score_quantiles"]["p50"], 1.0)
            self.assertNotIn("Private Name", json.dumps(report))
            self.assertNotIn("embedding", json.dumps(report))

    def test_order_pagination_ties_and_group_ids(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            stamp = "2026-09-20T12:00:00.000Z"
            first = _open_clip(inbox, _vec(0), stamp, [
                _turn("S1", 0.0, 4.0, _vec(0)),
                _turn("S1", 4.0, 8.0, _vec(0)),
            ])
            second = _open_clip(inbox, _vec(1), stamp)
            third = _open_clip(inbox, _vec(2), stamp)
            with inbox.connect() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM speaker_turns ORDER BY started, id"
                )]
                self.assertEqual(len(ids), 4)
                page = speaker_review.review_queue(db, limit=1)
                again = speaker_review.review_queue(db, limit=1)
                self.assertEqual(page, again)
                walked = []
                cursor = None
                seen_cursors = []
                for _ in range(6):
                    current = speaker_review.review_queue(db, limit=1, cursor=cursor)
                    repeated = speaker_review.review_queue(db, limit=1, cursor=cursor)
                    self.assertEqual(current, repeated)
                    self.assertLessEqual(len(current["items"]), 1)
                    walked.extend(item["turn_id"] for item in current["items"])
                    if current["next_cursor"] is None:
                        break
                    self.assertNotIn(current["next_cursor"], seen_cursors)
                    seen_cursors.append(current["next_cursor"])
                    cursor = current["next_cursor"]
                self.assertEqual(walked, ids)
                full = speaker_review.review_queue(db, limit=50)
                self.assertIsNone(full["next_cursor"])
                self.assertEqual([item["turn_id"] for item in full["items"]], ids)
                with mock.patch.object(speaker_review, "PAGE_MAX", 2):
                    capped = speaker_review.review_queue(db, limit=10)
                self.assertEqual(len(capped["items"]), 2)
                self.assertIsNotNone(capped["next_cursor"])
                stretch = [item for item in full["items"] if item["chunk_id"] == first]
                self.assertEqual(len(stretch), 2)
                self.assertEqual(stretch[0]["group_id"], stretch[1]["group_id"])
                self.assertEqual(stretch[0]["group_id"], _group_id(stretch[0]["group_turn_ids"]))
                self.assertEqual(sorted(stretch[0]["group_turn_ids"]), sorted(item["turn_id"] for item in stretch))
                self.assertNotEqual(
                    next(item["group_id"] for item in full["items"] if item["chunk_id"] == second),
                    next(item["group_id"] for item in full["items"] if item["chunk_id"] == third),
                )
                with self.assertRaises(ValueError):
                    speaker_review.review_queue(db, limit=0)
                with self.assertRaises(ValueError):
                    speaker_review.review_queue(db, cursor="%%%")
                with self.assertRaises(ValueError):
                    speaker_review.review_queue(db, cursor="x" * 513)

    def test_usable_audio_sorts_ahead_of_expired_audio(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            early = _open_clip(inbox, _vec(0), "2026-09-20T08:00:00.000Z")
            late = _open_clip(inbox, _vec(1), "2026-09-20T18:00:00.000Z")
            with inbox.connect() as db:
                db.execute("UPDATE chunks SET audio_state='deleted' WHERE id=?", (early,))
                items = speaker_review.review_queue(db)["items"]
            self.assertEqual([item["chunk_id"] for item in items], [late, early])
            self.assertTrue(items[0]["audio_usable"])
            self.assertFalse(items[1]["audio_usable"])

    def test_read_does_not_assign_or_treat_suggestion_as_confirmed(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            _person(inbox, "person-a", "Ann")
            _person(inbox, "person-b", "Bea")
            _label(inbox, _open_clip(inbox, _vec(0), "2026-09-20T12:00:00.000Z"), "person-a")
            _label(inbox, _open_clip(inbox, _vec(0), "2026-09-20T12:05:00.000Z"), "person-b")
            probe = _open_clip(inbox, _vec(0), "2026-09-20T12:10:00.000Z")
            jon = inbox.create_person("Jon")
            _label(inbox, _open_clip(inbox, _vec(3), "2026-09-20T13:00:00.000Z"), jon["id"])
            _label(inbox, _open_clip(inbox, _vec(3), "2026-09-20T13:05:00.000Z"), jon["id"])
            target = _open_clip(inbox, _vec(3), "2026-09-20T13:10:00.000Z")
            with inbox.connect() as db:
                before = _snapshot(db)
                with mock.patch.object(voice_id, "enroll_turns", side_effect=AssertionError("enroll")), \
                     mock.patch.object(voice_id, "auto_tag_chunks", side_effect=AssertionError("tag")), \
                     mock.patch.object(voice_id, "_write_automatic", side_effect=AssertionError("write")):
                    page = speaker_review.review_queue(db, limit=50)
                    report = speaker_review.evaluate_held_out(db)
                after = _snapshot(db)
            self.assertEqual(before, after)
            self.assertFalse(report["passed"])
            tied = next(item for item in page["items"] if item["chunk_id"] == probe)
            self.assertFalse(tied["confirmed"])
            self.assertIsNone(tied["label_source"])
            self.assertIsNone(tied["stored_person_id"])
            self.assertEqual([item["person_id"] for item in tied["suggestions"]], ["person-b", "person-a"])
            self.assertIn("margin_below_0.10", tied["reasons"])
            strong = next(item for item in page["items"] if item["chunk_id"] == target)
            self.assertFalse(strong["confirmed"])
            self.assertIsNone(strong["stored_person_id"])
            self.assertEqual(strong["suggestions"][0]["person_id"], jon["id"])
            self.assertGreaterEqual(strong["suggestion_score"], 0.85)
            self.assertIn("needs_confirmation", strong["reasons"])
            self.assertNotEqual(strong["label_source"], "confirmed")
            with mock.patch.object(voice_id, "SUGGESTION_LIMIT", 1), inbox.connect() as db:
                capped = speaker_review.review_queue(db, limit=50)
            capped_tie = next(item for item in capped["items"] if item["chunk_id"] == probe)
            self.assertEqual([item["person_id"] for item in capped_tie["suggestions"]], ["person-b"])

    def test_payload_and_logs_omit_embeddings_and_transcripts(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = _open_clip(
                inbox, _sentinel_vec(), "2026-09-20T12:00:00.000Z", transcript=TOKEN,
            )
            captured = io.StringIO()
            with inbox.connect() as db:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    page = speaker_review.review_queue(db)
                    report = speaker_review.evaluate_held_out(db)
            rendered = json.dumps({"page": page, "report": report})
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn(TOKEN, captured.getvalue())
            self.assertNotIn(SENTINEL, rendered)
            self.assertNotIn(SENTINEL, captured.getvalue())
            self.assertNotIn("embedding", rendered)
            item = page["items"][0]
            self.assertEqual(set(item), ITEM_KEYS)
            self.assertEqual(item["chunk_id"], chunk_id)
            self.assertLessEqual(len(page["items"]), speaker_review.PAGE_MAX)
            self.assertLess(len(rendered), 8000)
            for suggestion in item["suggestions"]:
                self.assertEqual(set(suggestion), {"person_id", "name", "score", "margin"})

    def test_automatic_guess_stays_in_review_and_unconfirmed(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            _label(inbox, _open_clip(inbox, _vec(0), "2026-09-20T12:00:00.000Z"), jon["id"])
            _label(inbox, _open_clip(inbox, _vec(0), "2026-09-20T12:05:00.000Z"), jon["id"])
            target = _open_clip(inbox, _vec(0), "2026-09-20T12:10:00.000Z")
            voice_id.drain_voice_work(inbox)
            with inbox.connect() as db:
                before = _snapshot(db)
                stored = db.execute(
                    "SELECT person_id, label_source FROM speaker_turns WHERE chunk_id=?",
                    (target,),
                ).fetchone()
                page = speaker_review.review_queue(db)
                after = _snapshot(db)
            self.assertEqual(stored["label_source"], "automatic")
            self.assertEqual(before, after)
            item = next(row for row in page["items"] if row["chunk_id"] == target)
            self.assertEqual(item["label_source"], "automatic")
            self.assertEqual(item["stored_person_id"], jon["id"])
            self.assertEqual(item["stored_name"], "Jon")
            self.assertFalse(item["confirmed"])

    def test_held_out_withholds_same_recording_evidence(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            alone = _open_clip(inbox, _vec(0), "2026-09-20T12:00:00.000Z")
            _label(inbox, alone, jon["id"])
            shared = _open_clip(inbox, _vec(0), "2026-09-20T12:10:00.000Z", [
                _turn("S1", 0.0, 8.0, _vec(0)),
                _turn("S2", 8.0, 16.0, _vec(0)),
            ])
            _label(inbox, shared, jon["id"], "S1")
            _label(inbox, shared, jon["id"], "S2")
            seen = []
            original = voice_id.manual_profiles

            def wrapped(db, exclude_chunk_ids=None):
                excluded = set(exclude_chunk_ids or ())
                profiles = original(db, exclude_chunk_ids=excluded)
                for profile in profiles.values():
                    self.assertTrue(excluded.isdisjoint(profile["clips"]))
                seen.append(excluded)
                return profiles

            with inbox.connect() as db:
                before = _snapshot(db)
                with mock.patch.object(voice_id, "manual_profiles", wraps=wrapped):
                    report = speaker_review.evaluate_held_out(db)
                after = _snapshot(db)
            self.assertEqual(before, after)
            self.assertIn({shared}, seen)
            self.assertEqual(report["matches"], 0)
            self.assertEqual(report["false_matches"], 0)
            self.assertEqual(report["covered"], 0)
            self.assertEqual(report["rejections"], 3)
            self.assertEqual(report["coverage"], 0.0)
            self.assertFalse(report["passed"])
            self.assertIsNone(report["recorded_status"])

    def test_held_out_counts_do_not_pass_calibration_or_lower_thresholds(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            with inbox.connect() as db:
                empty = speaker_review.evaluate_held_out(db)
            self.assertEqual(empty["groups"], 0)
            self.assertEqual(empty["coverage"], 0.0)
            self.assertFalse(empty["passed"])
            self.assertEqual(empty["min_score"], 0.85)
            self.assertEqual(empty["min_margin"], 0.10)
            _person(inbox, "jon", "Jon")
            _person(inbox, "mia", "Mia")
            for stamp in ("2026-09-20T12:00:00.000Z", "2026-09-20T12:05:00.000Z"):
                _label(inbox, _open_clip(inbox, _vec(0), stamp), "jon")
            _label(inbox, _open_clip(inbox, _vec(1), "2026-09-20T12:20:00.000Z"), "jon")
            for stamp in ("2026-09-20T13:00:00.000Z", "2026-09-20T13:05:00.000Z"):
                _label(inbox, _open_clip(inbox, _vec(2), stamp), "mia")
            _label(inbox, _open_clip(inbox, _vec(2), "2026-09-20T13:10:00.000Z"), "jon")
            with inbox.connect() as db:
                report = speaker_review.evaluate_held_out(db)
                status = list(db.execute("SELECT status FROM voice_calibration"))
            self.assertEqual(voice_id.AUTO_MIN_SCORE, 0.85)
            self.assertEqual(voice_id.AUTO_MIN_MARGIN, 0.10)
            self.assertEqual(report["min_score"], 0.85)
            self.assertEqual(report["min_margin"], 0.10)
            self.assertFalse(report["passed"])
            self.assertEqual(status, [])
            self.assertEqual(report["matches"], 2)
            self.assertEqual(report["rejections"], 3)
            self.assertEqual(report["false_matches"], 1)
            self.assertEqual(report["groups"], 6)
            self.assertEqual(report["covered"], 4)
            self.assertAlmostEqual(report["coverage"], 4 / 6)
            self.assertEqual(report["split"], "recording")


class SpeakerReviewRouteTests(ViewerCase):
    def _local(self, method, path, payload=None):
        headers = {"Authorization": "Bearer " + self.token}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        return self.request(method, path, headers, body)

    def test_review_get_is_bounded_read_only_and_machine_forbidden(self):
        clip = _open_clip(self.inbox, _vec(0), "2026-09-20T12:00:00.000Z", transcript=TOKEN)
        with self.inbox.connect() as db:
            before = _snapshot(db)
        status, raw, _ = self._local("GET", "/v1/speaker-review?limit=1")
        self.assertEqual(status, 200)
        page = json.loads(raw)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["chunk_id"], clip)
        self.assertTrue(page["items"][0]["audio_usable"])
        self.assertNotIn(TOKEN, raw.decode())
        status, raw, _ = self._local("GET", "/v1/speaker-review?limit=0")
        self.assertEqual(status, 400)
        status, raw, _ = self.request("GET", "/v1/speaker-review")
        self.assertEqual(status, 401)
        with mock.patch.dict("os.environ", {"LIFE_RECORDER_AGENT_CLIENT_IDS": self.client_id}):
            status, raw, _ = self.request("GET", "/v1/speaker-review", {
                "Host": "lr.genr8ive.ai", "Cf-Access-Jwt-Assertion": self.machine_token(),
            })
        self.assertEqual(status, 403)
        with self.inbox.connect() as db:
            self.assertEqual(before, _snapshot(db))
        self.assertIn("/v1/audio/", viewer.JS)
        self.assertIn("player.currentTime = startAt", viewer.JS)
        self.assertIn("clipStopTime = stopAt", viewer.JS)
        self.assertIn("#people-view, #review-view { padding: 16px", viewer.CSS)

    def test_confirm_requires_explicit_sample_opt_in(self):
        person = self.inbox.create_person("Jon")
        first = _open_clip(self.inbox, _vec(0), "2026-09-20T12:00:00.000Z")
        second = _open_clip(self.inbox, _vec(0), "2026-09-20T12:05:00.000Z")
        with self.inbox.connect() as db:
            turn_ids = [db.execute(
                "SELECT id FROM speaker_turns WHERE chunk_id=?", (clip,),
            ).fetchone()[0] for clip in (first, second)]
        status, _, _ = self._local("POST", f"/v1/turns/{turn_ids[0]}/label", {"person_id": person["id"]})
        self.assertEqual(status, 200)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_samples").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT label_source FROM speaker_turns WHERE id=?", (turn_ids[0],)
            ).fetchone()[0], "confirmed")
        status, _, _ = self._local("POST", f"/v1/turns/{turn_ids[1]}/label", {
            "person_id": person["id"], "use_sample": True,
        })
        self.assertEqual(status, 200)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_samples").fetchone()[0], 1)
        status, _, _ = self.request("POST", f"/v1/turns/{turn_ids[1]}/label", {
            "Host": "lr.genr8ive.ai", "Cf-Access-Jwt-Assertion": self.machine_token(),
            "Content-Type": "application/json",
        }, json.dumps({"person_id": person["id"]}).encode())
        self.assertEqual(status, 403)

    def test_rejection_persists_without_relabeling_or_retraining(self):
        person = self.inbox.create_person("Jon")
        _label(self.inbox, _open_clip(self.inbox, _vec(0), "2026-09-20T12:00:00.000Z"), person["id"])
        _label(self.inbox, _open_clip(self.inbox, _vec(0), "2026-09-20T12:05:00.000Z"), person["id"])
        probe = _open_clip(self.inbox, _vec(0), "2026-09-20T12:10:00.000Z")
        with self.inbox.connect() as db:
            before = _snapshot(db)
            turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (probe,)).fetchone()[0]
        status, raw, _ = self._local("GET", "/v1/speaker-review")
        self.assertEqual(status, 200)
        item = next(item for item in json.loads(raw)["items"] if item["turn_id"] == turn_id)
        self.assertIn(person["id"], [s["person_id"] for s in item["suggestions"]])
        status, raw, _ = self._local("POST", f"/v1/turns/{turn_id}/reject", {"person_id": person["id"]})
        self.assertEqual(status, 200)
        status, raw, _ = self._local("GET", "/v1/speaker-review")
        item = next(item for item in json.loads(raw)["items"] if item["turn_id"] == turn_id)
        self.assertNotIn(person["id"], [s["person_id"] for s in item["suggestions"]])
        with self.inbox.connect() as db:
            self.assertEqual(before, _snapshot(db))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM speaker_suggestion_rejections WHERE turn_id=? AND person_id=?",
                (turn_id, person["id"]),
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
