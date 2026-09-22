import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import diarization as diarization_mod
import voice_id
from receiver import Inbox


def _vec(index=0, value=1.0):
    vector = [0.0] * 256
    vector[index] = value
    return vector


def _insert_chunk(inbox, chunk_id, started="2026-09-10T12:00:00.000Z", duration=20.0):
    dest = inbox.audio / (chunk_id + ".m4a")
    dest.write_bytes(b"audio")
    with inbox.connect() as db:
        db.execute(
            """INSERT INTO chunks
               (id,sha256,device,started,duration,path,received,status,audio_state,words_json,diarization_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (chunk_id, uuid.uuid4().hex, str(uuid.uuid4()), started, duration, str(dest), 0,
             "complete", "present", "[]", "success"),
        )
    return dest


def _result(turns, **overrides):
    payload = {
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
    payload.update(overrides)
    return payload


def _turn(speaker, start, end, embedding, quality=1.0):
    return {"speaker_key": speaker, "started": start, "ended": end, "quality": quality, "embedding": embedding}


class VoiceIdentityTests(unittest.TestCase):
    def test_neighbor_audio_is_not_stored_on_the_current_turn(self):
        device = str(uuid.uuid4())
        current = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 12,
                   "started": "2026-09-10T12:01:00.000Z", "device": device}
        earlier = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 8,
                   "started": "2026-09-10T12:00:00.000Z", "device": device}
        neighbor = _vec(0)
        local = _vec(1)
        duplicate = {"cluster": 1, "embedding256": local, "startTime": 62.0, "endTime": 68.0}
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            audio = work / "clip.m4a"
            audio.write_bytes(b"audio")
            current["path"] = str(audio)
            earlier["path"] = str(audio)

            def fake_run(argv, **kwargs):
                if "process" in argv:
                    Path(argv[argv.index("--output") + 1]).write_text(json.dumps({
                        "segments": [{"speakerId": "S2", "startTimeSeconds": 60.0, "endTimeSeconds": 70.0,
                                      "qualityScore": 1.0}],
                        "speakerCount": 1,
                    }))
                    Path(argv[argv.index("--export-embeddings") + 1]).write_text(json.dumps([
                        {"cluster": 0, "embedding256": neighbor, "startTime": 10.0, "endTime": 20.0},
                        duplicate,
                        dict(duplicate),
                    ]))
                else:
                    Path(argv[-1]).write_bytes(b"RIFF")
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch("diarization.subprocess.run", side_effect=fake_run):
                result = diarization_mod.process_chunk(
                    current, Path("/bin/fluidaudiocli"), "ffmpeg", work, neighbors=[earlier, current])
        self.assertEqual(len(result["turns"][0]["vectors"]), 1)
        stored = result["turns"][0]["vectors"][0]
        self.assertEqual(stored["started"], 2.0)
        self.assertEqual(stored["ended"], 8.0)
        self.assertEqual(stored["overlap"], 0)
        self.assertNotEqual(stored["embedding"][0], 1.0)
        self.assertEqual(result["embedding_count"], 1)

    def _tag(self, inbox, person_id, embedding, started, seconds=8.0, speaker="S1"):
        chunk_id = str(uuid.uuid4())
        _insert_chunk(inbox, chunk_id, started, duration=max(20.0, seconds + 1))
        diarization_mod.save_result(inbox, chunk_id, _result([
            _turn(speaker, 0.0, seconds, embedding),
        ]))
        with inbox.connect() as db:
            turn_id = db.execute(
                "SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,),
            ).fetchone()[0]
        self.assertTrue(inbox.label_turn(turn_id, person_id))
        return chunk_id, turn_id

    def _open_clip(self, inbox, embedding, started, turns=None):
        chunk_id = str(uuid.uuid4())
        _insert_chunk(inbox, chunk_id, started)
        diarization_mod.save_result(inbox, chunk_id, _result(turns or [
            _turn("S1", 0.0, 8.0, embedding),
        ]))
        return chunk_id

    def test_manual_tags_auto_name_a_ready_match(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:00:00.000Z")
            target = self._open_clip(inbox, _vec(0), "2026-09-10T12:10:00.000Z")
            suggested = inbox.speaker_turns(target)[0]
            self.assertIsNone(suggested["person_id"])
            self.assertEqual(suggested["suggested_name"], "Jon")
            self.assertEqual(suggested["suggestion_basis"], "ranked")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:05:00.000Z")
            person = next(row for row in inbox.people() if row["name"] == "Jon")
            self.assertTrue(person["enrollment_ready"])
            self.assertEqual(person["sample_count"], 2)
            self.assertEqual(person["clip_count"], 2)
            voice_id.drain_voice_work(inbox)
            named = inbox.speaker_turns(target)[0]
            self.assertEqual(named["person_id"], jon["id"])
            self.assertEqual(named["label_source"], "automatic")
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 2)
                enrolled = db.execute(
                    "SELECT count(*) FROM voice_vectors WHERE turn_id=? AND enrolled=1",
                    (named["id"],),
                ).fetchone()[0]
            self.assertEqual(enrolled, 0)
            self.assertTrue(inbox.label_turn(named["id"], mia["id"]))
            replaced = inbox.speaker_turns(target)[0]
            self.assertEqual(replaced["person_id"], mia["id"])
            self.assertEqual(replaced["label_source"], "confirmed")
            refreshed = inbox.speaker_turns(target)[0]
            self.assertEqual(refreshed["person_id"], mia["id"])
            self.assertEqual(refreshed["label_source"], "confirmed")

    def test_profile_gate_and_clean_second_boundaries(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            same = self._open_clip(inbox, _vec(0), "2026-09-10T12:00:00.000Z", [
                _turn("S1", 0.0, 6.0, _vec(0)),
                _turn("S2", 6.0, 7.0, _vec(1)),
                _turn("S1", 7.0, 13.0, _vec(0)),
            ])
            with inbox.connect() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM speaker_turns WHERE chunk_id=? AND speaker_key='S1' ORDER BY started",
                    (same,),
                )]
            self.assertTrue(inbox.label_turn(ids[0], jon["id"]))
            self.assertTrue(inbox.label_turn(ids[1], jon["id"]))
            person = next(row for row in inbox.people() if row["name"] == "Jon")
            self.assertEqual(person["sample_count"], 2)
            self.assertEqual(person["clip_count"], 1)
            self.assertFalse(person["enrollment_ready"])
            self.assertIn("need_2_clips", person["enrollment_reasons"])
            short = self._open_clip(inbox, _vec(0), "2026-09-10T12:20:00.000Z", [
                _turn("S1", 0.0, 4.99, _vec(0)),
            ])
            with inbox.connect() as db:
                short_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (short,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(short_id, jon["id"]))
            with inbox.connect() as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM voice_samples WHERE turn_id=?", (short_id,),
                ).fetchone()[0], 0)
            five = self._open_clip(inbox, _vec(0), "2026-09-10T12:30:00.000Z", [
                _turn("S1", 0.0, 5.0, _vec(0)),
            ])
            with inbox.connect() as db:
                five_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (five,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(five_id, jon["id"]))
            with inbox.connect() as db:
                sample = db.execute(
                    "SELECT duration FROM voice_samples WHERE turn_id=?", (five_id,),
                ).fetchone()
            self.assertAlmostEqual(sample["duration"], 5.0)
            ready = next(row for row in inbox.people() if row["name"] == "Jon")
            self.assertEqual(ready["clip_count"], 2)
            self.assertGreaterEqual(ready["sample_seconds"], 10.0)
            self.assertTrue(ready["enrollment_ready"])

    def test_match_thresholds_margin_and_stretch_shape(self):
        def toward(score):
            vector = [0.0] * 256
            vector[0] = score
            vector[1] = (1.0 - score * score) ** 0.5
            return vector

        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:00:00.000Z")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:05:00.000Z")
            weak = self._open_clip(inbox, toward(0.50), "2026-09-10T12:10:00.000Z")
            close = self._open_clip(inbox, toward(0.80), "2026-09-10T12:15:00.000Z")
            strong = self._open_clip(inbox, toward(0.90), "2026-09-10T12:20:00.000Z")
            voice_id.drain_voice_work(inbox)
            self.assertNotIn("suggested_name", inbox.speaker_turns(weak)[0])
            self.assertIn("score_below_0.60", inbox.speaker_turns(weak)[0]["suggestion_reasons"])
            close_turn = inbox.speaker_turns(close)[0]
            self.assertIsNone(close_turn["person_id"])
            self.assertEqual(close_turn["suggested_name"], "Jon")
            self.assertIn("score_below_0.85", close_turn["suggestion_reasons"])
            strong_turn = inbox.speaker_turns(strong)[0]
            self.assertEqual(strong_turn["person_id"], jon["id"])
            self.assertEqual(strong_turn["label_source"], "automatic")
            mixed = self._open_clip(inbox, _vec(0), "2026-09-10T12:50:00.000Z", [
                _turn("S1", 0.0, 6.0, _vec(0)),
                _turn("S1", 8.0, 14.0, _vec(1)),
            ])
            voice_id.drain_voice_work(inbox)
            mixed_turns = inbox.speaker_turns(mixed)
            self.assertEqual(len(mixed_turns), 2)
            self.assertTrue(all(turn["person_id"] is None for turn in mixed_turns))
            split = self._open_clip(inbox, _vec(0), "2026-09-10T13:00:00.000Z", [
                _turn("S1", 0.0, 6.0, _vec(0)),
                _turn("S2", 6.0, 8.0, _vec(1)),
                _turn("S1", 8.0, 14.0, _vec(0)),
            ])
            voice_id.drain_voice_work(inbox)
            split_turns = inbox.speaker_turns(split)
            self.assertEqual([turn["speaker_key"] for turn in split_turns], ["S1", "S2", "S1"])
            self.assertEqual([turn["label_source"] for turn in split_turns], ["automatic", None, "automatic"])
            paused = self._open_clip(inbox, _vec(0), "2026-09-10T13:10:00.000Z", [
                _turn("S1", 0.0, 4.0, _vec(0)),
                _turn("S1", 6.0, 10.0, _vec(0)),
            ])
            voice_id.drain_voice_work(inbox)
            paused_turns = inbox.speaker_turns(paused)
            self.assertEqual(len(paused_turns), 2)
            self.assertTrue(all(turn["label_source"] == "automatic" for turn in paused_turns))
            self._tag(inbox, mia["id"], toward(0.95), "2026-09-10T12:25:00.000Z", seconds=6.0)
            self._tag(inbox, mia["id"], toward(0.95), "2026-09-10T12:26:00.000Z", seconds=6.0)
            blocked = self._open_clip(inbox, _vec(0), "2026-09-10T12:40:00.000Z")
            voice_id.drain_voice_work(inbox)
            blocked_turn = inbox.speaker_turns(blocked)[0]
            self.assertIsNone(blocked_turn["person_id"])
            self.assertEqual(blocked_turn["suggested_name"], "Jon")
            self.assertIn("margin_below_0.10", blocked_turn["suggestion_reasons"])
            before = None
            with inbox.connect() as db:
                before = db.execute("SELECT count(*) FROM voice_samples").fetchone()[0]
            inbox.speaker_turns(strong)
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], before)

    def test_confirmed_stretch_recovers_one_sample(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            chunk_id = self._open_clip(inbox, _vec(0), "2026-09-10T12:00:00.000Z")
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
                db.execute(
                    "UPDATE speaker_turns SET person_id=?, label_source='confirmed' WHERE id=?",
                    (jon["id"], turn_id),
                )
            self.assertEqual(inbox.speaker_turns(chunk_id)[0]["label_source"], "confirmed")
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 0)
            voice_id.drain_voice_work(inbox)
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 1)
            voice_id.drain_voice_work(inbox)
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 1)

    def test_short_overlap_and_legacy_audio_stay_unnamed(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            chunk_id = str(uuid.uuid4())
            _insert_chunk(inbox, chunk_id)
            diarization_mod.save_result(inbox, chunk_id, _result([
                _turn("S1", 0.0, 4.0, _vec(0)),
            ]))
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
                assigned = db.execute(
                    """SELECT state FROM voice_assignments a JOIN voice_vectors v ON v.id=a.vector_id
                       WHERE v.turn_id=? AND a.active=1""",
                    (turn_id,),
                ).fetchone()
            self.assertEqual(assigned["state"], "unknown")
            self.assertTrue(inbox.label_turn(turn_id, person["id"], use_sample=True))
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 0)
                db.execute("UPDATE voice_vectors SET legacy=1, timed=0 WHERE turn_id=?", (turn_id,))
            diarization_mod.save_result(inbox, chunk_id, _result([
                {"speaker_key": "S1", "started": 0.0, "ended": 8.0, "quality": 1.0, "embedding": None,
                 "vectors": [{"embedding": _vec(0), "started": 0.0, "ended": 8.0, "overlap": 1, "timed": 1,
                              "legacy": 0, "quality": 1.0}]},
            ]))
            with inbox.connect() as db:
                turn_id = db.execute(
                    "SELECT id FROM speaker_turns WHERE chunk_id=? AND run_id=(SELECT id FROM speaker_runs ORDER BY created_at DESC LIMIT 1)",
                    (chunk_id,),
                ).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, person["id"], use_sample=True))
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 0)

    def test_overlapping_windows_do_not_enroll_outside_the_piece(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            chunk_id = str(uuid.uuid4())
            _insert_chunk(inbox, chunk_id)
            diarization_mod.save_result(inbox, chunk_id, _result([
                {"speaker_key": "S1", "started": 0.0, "ended": 4.0, "quality": 1.0, "embedding": None,
                 "vectors": [
                     {"embedding": _vec(0), "started": 0.0, "ended": 10.0, "overlap": 0, "timed": 1,
                      "legacy": 0, "quality": 1.0},
                 ]},
                {"speaker_key": "S1", "started": 4.0, "ended": 10.0, "quality": 1.0, "embedding": None,
                 "vectors": [
                     {"embedding": _vec(0), "started": 0.0, "ended": 10.0, "overlap": 0, "timed": 1,
                      "legacy": 0, "quality": 1.0},
                     {"embedding": _vec(0), "started": 4.0, "ended": 14.0, "overlap": 0, "timed": 1,
                      "legacy": 0, "quality": 1.0},
                 ]},
            ]))
            turns = inbox.speaker_turns(chunk_id)
            self.assertAlmostEqual(turns[0]["clean_seconds"], 4.0)
            self.assertAlmostEqual(turns[1]["clean_seconds"], 6.0)
            self.assertTrue(inbox.label_turn(turns[0]["id"], person["id"], use_sample=True))
            labeled = inbox.speaker_turns(chunk_id)
            self.assertEqual(labeled[0]["person_id"], person["id"])
            self.assertEqual(labeled[1]["person_id"], person["id"])
            with inbox.connect() as db:
                sample = db.execute("SELECT duration FROM voice_samples").fetchone()
                enrolled = {row[0] for row in db.execute(
                    "SELECT DISTINCT turn_id FROM voice_vectors WHERE enrolled=1")}
            self.assertAlmostEqual(sample["duration"], 10.0)
            self.assertEqual(enrolled, {turns[0]["id"], turns[1]["id"]})

    def test_short_stretch_names_every_member_without_enrolling(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            chunk_id = str(uuid.uuid4())
            _insert_chunk(inbox, chunk_id)
            diarization_mod.save_result(inbox, chunk_id, _result([
                _turn("S1", 0.0, 2.0, _vec(0)),
                _turn("S1", 3.0, 5.0, _vec(0)),
            ]))
            turns = inbox.speaker_turns(chunk_id)
            self.assertAlmostEqual(sum(turn["clean_seconds"] for turn in turns), 4.0)
            self.assertTrue(inbox.label_turn(turns[1]["id"], person["id"], use_sample=True))
            labeled = inbox.speaker_turns(chunk_id)
            self.assertEqual([turn["person_id"] for turn in labeled], [person["id"], person["id"]])
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM voice_vectors WHERE enrolled=1").fetchone()[0], 0)

    def test_conflicting_names_freeze_the_track_and_keep_explicit_labels(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            chunk_id = str(uuid.uuid4())
            _insert_chunk(inbox, chunk_id)
            diarization_mod.save_result(inbox, chunk_id, _result([
                _turn("S1", 0.0, 4.0, _vec(0)),
                _turn("S2", 4.0, 6.0, _vec(1)),
                _turn("S1", 6.0, 12.0, _vec(0)),
            ]))
            turns = inbox.speaker_turns(chunk_id)
            self.assertTrue(inbox.label_turn(turns[0]["id"], jon["id"]))
            self.assertTrue(inbox.label_turn(turns[2]["id"], mia["id"]))
            with inbox.connect() as db:
                status = db.execute("SELECT status, frozen_reason FROM voice_tracks").fetchone()
            self.assertEqual(status["status"], "frozen")
            self.assertEqual(status["frozen_reason"], "conflicting_identity")
            labeled = inbox.speaker_turns(chunk_id)
            self.assertEqual(labeled[0]["person_id"], jon["id"])
            self.assertEqual(labeled[2]["person_id"], mia["id"])
            self.assertIsNone(labeled[1]["person_id"])
            self.assertNotIn("suggested_name", labeled[1])
            self.assertTrue(inbox.label_turn(labeled[2]["id"], jon["id"]))
            with inbox.connect() as db:
                status = db.execute("SELECT status, frozen_reason FROM voice_tracks").fetchone()
            self.assertEqual(status["status"], "open")
            self.assertIsNone(status["frozen_reason"])

    def test_rerun_reuses_the_track_without_duplicate_evidence(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            _insert_chunk(inbox, chunk_id)
            diarization_mod.save_result(inbox, chunk_id, _result([_turn("S1", 0.0, 8.0, _vec(0))]))
            with inbox.connect() as db:
                track_id = db.execute("SELECT id FROM voice_tracks").fetchone()[0]
            diarization_mod.save_result(inbox, chunk_id, _result([_turn("S2", 0.2, 8.2, _vec(0))]))
            with inbox.connect() as db:
                active = db.execute(
                    """SELECT DISTINCT a.track_id FROM voice_assignments a
                       JOIN voice_vectors v ON v.id=a.vector_id
                       WHERE v.chunk_id=? AND a.active=1 AND a.state='assigned'""",
                    (chunk_id,),
                ).fetchall()
                vectors = db.execute("SELECT count(*) FROM voice_vectors WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertEqual([row["track_id"] for row in active], [track_id])
            self.assertEqual(vectors, 1)

    def test_calibration_stays_off_and_blocks_inference_readiness(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            with inbox.connect() as db:
                self.assertFalse(voice_id.automation_enabled(db))
            reasons = inbox.people()[0]["enrollment_reasons"]
            self.assertNotIn("calibration_required", reasons)
            self.assertIn("need_2_samples", reasons)
            self.assertIn("need_2_clips", reasons)
            self.assertIn("need_10s", reasons)
            self.assertFalse(inbox.people()[0]["enrollment_ready"])
            self.assertEqual(person["name"], "Jon")

    def test_day_and_speaker_reads_do_not_enroll_or_auto_tag(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:00:00.000Z")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:05:00.000Z")
            target = self._open_clip(inbox, _vec(0), "2026-09-10T12:10:00.000Z")
            historical = self._open_clip(inbox, _vec(0), "2026-09-10T12:15:00.000Z")
            with inbox.connect() as db:
                turn_id = db.execute(
                    "SELECT id FROM speaker_turns WHERE chunk_id=?", (historical,)
                ).fetchone()[0]
                db.execute(
                    "UPDATE speaker_turns SET person_id=?, label_source='confirmed' WHERE id=?",
                    (jon["id"], turn_id),
                )
                jobs = db.execute("SELECT count(*) FROM voice_jobs").fetchone()[0]
                samples = db.execute("SELECT count(*) FROM voice_samples").fetchone()[0]
            with mock.patch.object(voice_id, "recover_confirmed_samples", side_effect=AssertionError("recover")), \
                 mock.patch.object(voice_id, "recover_confirmed_batch", side_effect=AssertionError("batch")), \
                 mock.patch.object(voice_id, "recover_chunk", side_effect=AssertionError("chunk")), \
                 mock.patch.object(voice_id, "auto_tag_chunks", side_effect=AssertionError("tag")):
                day = inbox.viewer_day("2026-09-10")
                again = inbox.viewer_day("2026-09-10")
                turns = inbox.speaker_turns(target)
                review = inbox.chunk_review(target)
            self.assertGreaterEqual(len(day["chunks"]), 4)
            self.assertNotIn("speakers", day["chunks"][0])
            self.assertNotIn("words", day["chunks"][0])
            self.assertNotIn("diarization", day["chunks"][0])
            self.assertEqual(len(again["chunks"]), len(day["chunks"]))
            self.assertIsNone(turns[0]["person_id"])
            self.assertEqual(turns[0]["suggested_name"], "Jon")
            self.assertIsNone(review["speakers"][0]["person_id"])
            self.assertTrue(review["voice_pending"])
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_jobs").fetchone()[0], jobs)
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], samples)
            voice_id.drain_voice_work(inbox)
            self.assertEqual(inbox.speaker_turns(target)[0]["label_source"], "automatic")
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], samples + 1)

    def test_day_list_skips_per_recording_helpers(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            for index in range(201):
                hour = 12 + index // 60
                minute = index % 60
                _insert_chunk(
                    inbox,
                    str(uuid.uuid4()),
                    f"2026-09-22T{hour:02d}:{minute:02d}:00.000Z",
                )
            inbox.speaker_turns = mock.Mock(side_effect=AssertionError("speaker_turns"))
            inbox.diarization_summary = mock.Mock(side_effect=AssertionError("diarization"))
            payload = inbox.viewer_day("2026-09-22")
            self.assertEqual(len(payload["chunks"]), 201)
            self.assertNotIn("words", payload["chunks"][0])
            self.assertIn("speaker_turn_count", payload["chunks"][0])
            inbox.speaker_turns.assert_not_called()
            inbox.diarization_summary.assert_not_called()

    def test_restart_resumes_unfinished_voice_jobs(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            inbox = Inbox(root)
            jon = inbox.create_person("Jon")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:00:00.000Z")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:05:00.000Z")
            target = self._open_clip(inbox, _vec(0), "2026-09-10T12:10:00.000Z")
            with inbox.connect() as db:
                db.execute(
                    "UPDATE voice_maintenance SET recover_complete=1, startup_sweep_complete=1 WHERE id=1"
                )
                db.execute("DELETE FROM voice_jobs")
                voice_id.enqueue_chunk(db, target, "result")
            restarted = Inbox(root)
            voice_id.drain_voice_work(restarted)
            named = restarted.speaker_turns(target)[0]
            self.assertEqual(named["label_source"], "automatic")
            self.assertEqual(named["person_id"], jon["id"])


    def test_upgrade_drops_rho128_and_keeps_confirmed_names(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            chunk_id = self._open_clip(inbox, _vec(0), "2026-09-10T12:00:00.000Z")
            with inbox.connect() as db:
                turn_id = db.execute(
                    "SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)
                ).fetchone()[0]
                db.execute(
                    "UPDATE speaker_turns SET person_id=?, label_source='confirmed' WHERE id=?",
                    (person["id"], turn_id),
                )
                other = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO speaker_turns
                       (id,run_id,chunk_id,speaker_key,started,ended,quality,person_id,label_source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (other, str(uuid.uuid4()), chunk_id, "S2", 10.0, 12.0, 1.0, person["id"], "automatic"),
                )
                db.execute(
                    """INSERT INTO voice_vectors
                       (id,turn_id,run_id,chunk_id,speaker_key,started,ended,duration,quality,overlap,timed,
                        legacy,embedding_json,extraction_version,interval_key,enrolled,person_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("old-vector", turn_id, str(uuid.uuid4()), chunk_id, "S1", 0.0, 8.0, 8.0, 1.0, 0, 1,
                     0, json.dumps([1.0] + [0.0] * 127), 2, "old", 1, person["id"], 1.0),
                )
                db.execute(
                    """INSERT INTO voice_samples
                       (id,person_id,turn_id,embedding_json,duration,confirmed_at,vector_id,status,legacy,source_key)
                       VALUES (?,?,?,?,?,?,?,'accepted',0,?)""",
                    ("old-sample", person["id"], turn_id, json.dumps([1.0] + [0.0] * 127), 8.0, 1.0,
                     "old-vector", "old"),
                )
                db.execute("PRAGMA user_version=7")
                import meetings
                meetings.migrate_schema(db)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertEqual(db.execute("SELECT count(*) FROM voice_vectors").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 0)
                labels = {
                    row["id"]: row["label_source"]
                    for row in db.execute("SELECT id, label_source, person_id FROM speaker_turns")
                }
            self.assertEqual(labels[turn_id], "confirmed")
            self.assertIsNone(labels[other])
            kept = inbox.speaker_turns(chunk_id)
            confirmed = next(turn for turn in kept if turn["id"] == turn_id)
            self.assertEqual(confirmed["person_id"], person["id"])

    def test_match_decision_rejects_a_different_dimension(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            jon = inbox.create_person("Jon")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:00:00.000Z")
            self._tag(inbox, jon["id"], _vec(0), "2026-09-10T12:05:00.000Z")
            target = self._open_clip(inbox, _vec(0), "2026-09-10T12:10:00.000Z")
            with inbox.connect() as db:
                db.execute(
                    "UPDATE voice_vectors SET embedding_json=? WHERE chunk_id=?",
                    (json.dumps([1.0] + [0.0] * 127), target),
                )
                turns = list(db.execute(
                    "SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id",
                    (target,),
                ))
                groups = voice_id._review_groups(turns)
                self.assertIsNone(voice_id.identity_match_decision(db, groups[0]))



if __name__ == "__main__":
    unittest.main()
