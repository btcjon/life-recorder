import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import asr as asr_mod
import decoded_audio
from receiver import Inbox, record_processing_failure
import receiver as receiver_mod


PARAKEET_JSON = {"text": "hello once", "wordTimings": []}


class ClassificationTests(unittest.TestCase):
    def test_valueerror_is_not_treated_as_unrecoverable(self):
        stage, code, kind = asr_mod.classify_processing_failure(ValueError("something unexpected"))
        self.assertEqual((stage, code, kind), ("processing", "processing_failed", "transient"))
        self.assertEqual(
            asr_mod.classify_processing_failure(ValueError("Malformed Parakeet output"))[2],
            "malformed",
        )
        self.assertEqual(
            asr_mod.classify_processing_failure(ValueError("Missing Parakeet output"))[1],
            "asr_missing_output",
        )
        self.assertEqual(
            asr_mod.classify_processing_failure(json.JSONDecodeError("bad", "x", 0))[2],
            "malformed",
        )
        self.assertEqual(
            asr_mod.classify_processing_failure(ValueError("ffmpeg is required"))[2],
            "permanent",
        )
        self.assertEqual(
            asr_mod.classify_processing_failure(subprocess.TimeoutExpired("ffmpeg", 1))[2],
            "transient",
        )
        self.assertEqual(
            asr_mod.classify_processing_failure(FileNotFoundError("gone"))[:2],
            ("decode", "source_missing"),
        )


class EmptyDecodeTests(unittest.TestCase):
    def test_empty_cached_wav_is_replaced_and_source_stays(self):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            source = work / "clip.m4a"
            source.write_bytes(b"source-audio")
            row = {"id": "clip", "path": str(source)}
            cache = decoded_audio.cache_for(work)
            cache.path_for(cache.key_for("clip", source)).write_bytes(b"")
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv[0])
                if argv[0] == "ffmpeg":
                    Path(argv[-1]).write_bytes(b"RIFF")
                else:
                    Path(argv[argv.index("--output-json") + 1]).write_text(json.dumps(PARAKEET_JSON))
                return mock.Mock(returncode=0)

            config = asr_mod.AsrConfig(
                engine="parakeet", ffmpeg="ffmpeg",
                parakeet_cli=Path("/bin/fluidaudiocli"),
                parakeet_model_dir=Path("/models/parakeet"),
            )
            with mock.patch("asr.subprocess.run", side_effect=fake_run):
                text, provenance = asr_mod.transcribe_chunk(row, config, work, lambda item: item)
            self.assertEqual(text, "hello once")
            self.assertEqual(provenance["engine"], "parakeet")
            self.assertEqual(calls.count("ffmpeg"), 1)
            self.assertEqual(source.read_bytes(), b"source-audio")

    def test_second_empty_decode_is_retryable_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            source = work / "clip.m4a"
            source.write_bytes(b"source-audio")
            row = {"id": "clip-2", "path": str(source)}

            def fake_run(argv, **kwargs):
                return mock.Mock(returncode=0)

            config = asr_mod.AsrConfig(
                engine="parakeet", ffmpeg="ffmpeg",
                parakeet_cli=Path("/bin/fluidaudiocli"),
                parakeet_model_dir=Path("/models/parakeet"),
            )
            with mock.patch("asr.subprocess.run", side_effect=fake_run):
                with self.assertRaises(asr_mod.ProcessingFailure) as caught:
                    asr_mod.transcribe_chunk(row, config, work, lambda item: item)
            self.assertEqual(caught.exception.code, "decode_empty")
            self.assertEqual(caught.exception.kind, "transient")
            self.assertTrue(source.exists())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.chunk_id = str(uuid.uuid4())
        self.audio = self.inbox.audio / (self.chunk_id + ".m4a")
        self.audio.write_bytes(b"kept-audio")
        with self.inbox.connect() as db:
            db.execute(
                """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-22T15:13:54.000Z",
                 60.0, str(self.audio), time.time()),
            )

    def tearDown(self):
        self.temp.cleanup()

    def _fail(self, error):
        record_processing_failure(self.inbox, self.inbox.receipt(self.chunk_id), error)

    def test_malformed_output_reaches_attention_and_can_be_retried(self):
        for _ in range(receiver_mod.MAX_PROCESSING_ATTEMPTS - 1):
            self._fail(ValueError("Malformed Parakeet output"))
            row = self.inbox.receipt(self.chunk_id)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["error_code"], "asr_malformed_output")
            self.assertNotEqual(row["error"], "ValueError")
        self._fail(ValueError("Malformed Parakeet output"))
        record = self.inbox.processing_record(self.chunk_id)
        self.assertEqual(record["status"], "needs_attention")
        self.assertEqual(record["error_code"], "asr_malformed_output")
        self.assertEqual(record["error_stage"], "asr")
        self.assertEqual(record["attempts"], receiver_mod.MAX_PROCESSING_ATTEMPTS)
        self.assertTrue(record["retry_eligible"])
        self.assertEqual(record["retry_mode"], "manual")
        self.assertIsNotNone(record["attention_at"])
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")
        retried = self.inbox.retry_chunk(self.chunk_id)
        self.assertEqual(retried["status"], "pending")
        self.assertEqual(retried["attempts"], 0)
        self.assertEqual(retried["retry_mode"], "automatic")
        self.assertIsNone(retried["error_code"])
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")

    def test_transient_error_backs_off_and_unknown_valueerror_stays_pending(self):
        before = time.time()
        self._fail(subprocess.TimeoutExpired("fluidaudiocli", 1))
        row = self.inbox.receipt(self.chunk_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_code"], "asr_timeout")
        self.assertEqual(row["error_stage"], "asr")
        self.assertGreater(row["retry_at"], before)
        self.assertEqual(self.inbox.processing_record(self.chunk_id)["retry_mode"], "automatic")
        self.inbox.retry_chunk(self.chunk_id)
        self._fail(ValueError("not a known parser message"))
        row = self.inbox.receipt(self.chunk_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_code"], "processing_failed")
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")

    def test_permanent_error_stops_automatic_retry_but_stays_manually_retryable(self):
        self._fail(FileNotFoundError("missing source"))
        record = self.inbox.processing_record(self.chunk_id)
        self.assertEqual(record["status"], "needs_attention")
        self.assertEqual(record["error_code"], "source_missing")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["retry_eligible"])
        self.assertEqual(record["retry_mode"], "manual")
        self.assertTrue(self.audio.exists())
        self.assertEqual(self.inbox.retry_chunk(self.chunk_id)["status"], "pending")

    def test_manual_retry_then_completion_is_idempotent_across_restart(self):
        self._fail(ValueError("ffmpeg is required"))
        self.assertEqual(self.inbox.receipt(self.chunk_id)["status"], "needs_attention")
        stop = threading.Event()
        calls = {"n": 0}

        def fake_transcribe(*args, **kwargs):
            calls["n"] += 1
            return "hello once", {"engine": "parakeet", "model": "parakeet", "summary": {}}

        with mock.patch.object(receiver_mod, "transcribe", side_effect=fake_transcribe):
            thread = threading.Thread(
                target=receiver_mod.worker,
                args=(self.inbox, stop, Path("/tmp/model"), "whisper", "ffmpeg"),
                daemon=True,
            )
            thread.start()
            time.sleep(0.4)
            self.assertEqual(calls["n"], 0)
            self.assertEqual(self.inbox.receipt(self.chunk_id)["status"], "needs_attention")
            self.inbox.retry_chunk(self.chunk_id)
            deadline = time.time() + 8
            while time.time() < deadline:
                if self.inbox.receipt(self.chunk_id)["status"] == "complete":
                    break
                time.sleep(0.05)
            stop.set()
            thread.join(timeout=2)
        row = self.inbox.receipt(self.chunk_id)
        self.assertEqual(row["status"], "complete")
        self.assertIsNone(row["error_code"])
        self.assertEqual((self.inbox.root / "life.md").read_text().count("hello once"), 1)
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")
        again = self.inbox.retry_chunk(self.chunk_id)
        self.assertEqual(again["status"], "complete")
        self.assertFalse(again["retry_eligible"])
        reopened = Inbox(self.inbox.root)
        self.assertEqual(reopened.receipt(self.chunk_id)["status"], "complete")
        self.assertEqual((reopened.root / "life.md").read_text().count("hello once"), 1)
        self.assertEqual((reopened.audio / (self.chunk_id + ".m4a")).read_bytes(), b"kept-audio")
        self.assertEqual(calls["n"], 1)

    def test_failure_after_commit_does_not_rewind_or_duplicate(self):
        snapshot = self.inbox.receipt(self.chunk_id)
        self.inbox.complete(self.chunk_id, "hello once", {"engine": "parakeet", "model": "m", "summary": {}})
        record_processing_failure(self.inbox, snapshot, ValueError("Malformed Parakeet output"))
        self.assertEqual(self.inbox.receipt(self.chunk_id)["status"], "complete")
        self.assertEqual((self.inbox.root / "life.md").read_text().count("hello once"), 1)
        self.assertTrue(self.audio.exists())

    def test_needs_attention_audio_survives_retention_and_restart(self):
        expired_id = str(uuid.uuid4())
        expired = self.inbox.audio / (expired_id + ".m4a")
        expired.write_bytes(b"expired-audio")
        now = time.time()
        with self.inbox.connect() as db:
            db.execute(
                """UPDATE chunks SET status='needs_attention', error_code='asr_missing_output',
                   error_stage='asr', attention_at=?, audio_state='present', audio_bytes=?,
                   audio_expires_at=? WHERE id=?""",
                (now, self.audio.stat().st_size, now - 10, self.chunk_id),
            )
            db.execute(
                """INSERT INTO chunks
                   (id,sha256,device,started,duration,path,status,transcript,received,completed_at,
                    audio_state,audio_bytes,audio_expires_at,audio_pinned)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (expired_id, "b" * 64, str(uuid.uuid4()), "2026-09-01T12:00:00.000Z",
                 60.0, str(expired), "complete", "old", now - 100, now - 100,
                 "present", expired.stat().st_size, now - 10, 0),
            )
        self.inbox.cleanup_completed()
        self.assertFalse(expired.exists())
        self.assertEqual(self.audio.read_bytes(), b"kept-audio")
        reopened = Inbox(self.inbox.root)
        self.assertEqual(reopened.receipt(self.chunk_id)["status"], "needs_attention")
        self.assertEqual((reopened.audio / (self.chunk_id + ".m4a")).read_bytes(), b"kept-audio")
        self.assertFalse((reopened.audio / (expired_id + ".m4a")).exists())


class MigrationTests(unittest.TestCase):
    def test_failure_columns_migrate_twice_without_dropping_rows(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            chunk_id = str(uuid.uuid4())
            audio = root / "keep.m4a"
            audio.write_bytes(b"migrate-audio")
            db_path = root / "inbox.sqlite3"
            con = sqlite3.connect(db_path)
            con.execute(
                """CREATE TABLE chunks (
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, device TEXT NOT NULL,
                    started TEXT NOT NULL, duration REAL NOT NULL, path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', transcript TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT, received REAL NOT NULL)"""
            )
            con.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (chunk_id, "c" * 64, str(uuid.uuid4()), "2026-09-22T15:13:54.000Z",
                 60.0, str(audio), "pending", None, 49, 0, "ValueError", 1),
            )
            con.execute("PRAGMA user_version=8")
            con.commit()
            con.close()
            first = Inbox(root)
            with first.connect() as db:
                cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
                self.assertTrue({"error_code", "error_stage", "attention_at"} <= cols)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT error FROM chunks").fetchone()[0], "ValueError")
            second = Inbox(root)
            with second.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertEqual(db.execute("SELECT id, attempts, error FROM chunks").fetchone()[:],
                                 (chunk_id, 49, "ValueError"))
            self.assertEqual(audio.read_bytes(), b"migrate-audio")
            self.assertEqual(second.processing_record(chunk_id)["attempts"], 49)


if __name__ == "__main__":
    unittest.main()
