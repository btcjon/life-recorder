import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import uuid
import wave
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import asr as asr_mod
import diarization as diarization_mod
import meetings as meetings_mod
import receiver as receiver_mod
import vad as vad_mod
import viewer as viewer_mod
from receiver import Inbox


def write_tone(path: Path, seconds: float = 1.0, rate: int = 16000):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x10" * frames)


def chunk(device, started, duration=60.0, chunk_id=None, path="/tmp/missing.m4a"):
    return {
        "id": chunk_id or str(uuid.uuid4()),
        "device": device,
        "started": started,
        "duration": duration,
        "path": path,
        "status": "complete",
    }


class VadParseTests(unittest.TestCase):
    def test_parse_rejects_malformed_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as scratch:
            missing = Path(scratch) / "missing.json"
            with self.assertRaises(vad_mod.VadError):
                vad_mod.parse_vad_output(missing, 10)
            bad = Path(scratch) / "bad.json"
            bad.write_text("{not json")
            with self.assertRaises(vad_mod.VadError):
                vad_mod.parse_vad_output(bad, 10)
            payload = Path(scratch) / "nan.json"
            payload.write_text(json.dumps({
                "backend": "silero",
                "segments": [{"startTime": float("nan"), "endTime": 1.0}],
            }))
            with self.assertRaises(vad_mod.VadError):
                vad_mod.parse_vad_output(payload, 10)

    def test_parse_accepts_strict_json_and_pads(self):
        with tempfile.TemporaryDirectory() as scratch:
            payload = Path(scratch) / "ok.json"
            payload.write_text(json.dumps({
                "backend": "silero",
                "segments": [
                    {"startTime": 1.0, "endTime": 1.4},
                    {"startTime": 1.9, "endTime": 2.3},
                ],
            }))
            spans = vad_mod.parse_vad_output(payload, 10)
        self.assertEqual(spans, [{"start": 0.6, "end": 2.7}])

    def test_cli_failure_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as scratch:
            audio = Path(scratch) / "clip.wav"
            write_tone(audio, 0.5)
            row = {"id": "c1", "path": str(audio), "duration": 0.5}
            with mock.patch.object(vad_mod.subprocess, "run") as run:
                run.side_effect = [
                    mock.Mock(returncode=0),
                    mock.Mock(returncode=1, stderr=b"fail"),
                ]
                with self.assertRaises(vad_mod.VadError):
                    vad_mod.process_chunk(row, Path("/bin/false"), "ffmpeg", Path(scratch))


class EventAssemblyTests(unittest.TestCase):
    def test_merges_close_spans_and_splits_on_silence(self):
        device = str(uuid.uuid4())
        first = chunk(device, "2026-09-10T12:00:00.000Z", 10, "a")
        second = chunk(device, "2026-09-10T12:00:10.000Z", 10, "b")
        spans = {"a": [{"start": 8.0, "end": 9.5}], "b": [{"start": 0.2, "end": 2.0}]}
        merged = vad_mod.assemble_events([first, second], spans, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["duration"], 4.0)
        far = chunk(device, "2026-09-10T12:00:30.000Z", 10, "c")
        spans["c"] = [{"start": 0.0, "end": 1.0}]
        split = vad_mod.assemble_events([first, second, far], spans, {})
        self.assertEqual(len(split), 2)

    def test_splits_on_max_duration_and_anonymous_change(self):
        device = str(uuid.uuid4())
        first = chunk(device, "2026-09-10T12:00:00.000Z", 120, "a")
        second = chunk(device, "2026-09-10T12:02:00.000Z", 120, "b")
        spans = {"a": [{"start": 0.0, "end": 120.0}], "b": [{"start": 0.0, "end": 70.0}]}
        long_events = vad_mod.assemble_events([first, second], spans, {})
        self.assertEqual(len(long_events), 2)
        turns = {
            "a": [{"started": 0.0, "ended": 10.0, "person_id": None, "speaker_key": "S1"}],
            "b": [{"started": 0.0, "ended": 10.0, "person_id": None, "speaker_key": "S2"}],
        }
        close_second = chunk(device, "2026-09-10T12:00:10.000Z", 10, "b")
        anon = vad_mod.assemble_events(
            [chunk(device, "2026-09-10T12:00:00.000Z", 10, "a"), close_second],
            {"a": [{"start": 8.0, "end": 9.5}], "b": [{"start": 0.2, "end": 2.0}]},
            turns,
        )
        self.assertEqual(len(anon), 2)

    def test_manual_meeting_bound_splits_events(self):
        device = str(uuid.uuid4())
        first = chunk(device, "2026-09-10T12:00:00.000Z", 10, "a")
        second = chunk(device, "2026-09-10T12:00:10.000Z", 10, "b")
        intervals = [{
            "source": "manual",
            "device_id": device,
            "started_at": "2026-09-10T12:00:09.500Z",
            "ended_at": "2026-09-10T12:20:00.000Z",
        }]
        events = vad_mod.assemble_events(
            [first, second],
            {"a": [{"start": 8.0, "end": 9.5}], "b": [{"start": 0.2, "end": 2.0}]},
            {},
            intervals,
        )
        self.assertEqual(len(events), 2)


class PersistenceAndPlaybackTests(unittest.TestCase):
    def test_schema_v5_and_event_roundtrip(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            with inbox.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                event_cols = {row[1] for row in db.execute("PRAGMA table_info(speech_events)")}
            self.assertEqual(version, 8)
            self.assertIn("vad_status", cols)
            self.assertIn("speech_events", tables)
            self.assertIn("voice_vectors", tables)
            self.assertIn("voice_tracks", tables)
            self.assertIn("chunk_speech_spans", tables)
            self.assertIn("playable_duration", event_cols)
            self.assertIn("derived_bytes", event_cols)
            self.assertIn("content_fingerprint", event_cols)
            self.assertIn("enhancement_retry_at", event_cols)
            device = str(uuid.uuid4())
            first = str(uuid.uuid4())
            second = str(uuid.uuid4())
            for chunk_id, started in ((first, "2026-09-10T16:00:00.000Z"), (second, "2026-09-10T16:00:10.000Z")):
                dest = inbox.audio / (chunk_id + ".m4a")
                dest.write_bytes(b"audio")
                with inbox.connect() as db:
                    db.execute(
                        """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (chunk_id, "a" * 64, device, started, 10.0, str(dest), 0, "complete", "present"),
                    )
            vad_mod.save_chunk_spans(inbox, first, [{"start": 8.0, "end": 9.5}])
            vad_mod.save_chunk_spans(inbox, second, [{"start": 0.2, "end": 2.0}])
            vad_mod.rebuild_events(inbox)
            payload = inbox.viewer_day("2026-09-10")
            self.assertEqual(len(payload["events"]), 1)
            event = payload["events"][0]
            self.assertTrue(event["audio_playable"])
            self.assertAlmostEqual(event["playable_duration"], 3.3)
            self.assertFalse(event["enhanced_playable"])
            self.assertEqual(event["enhancement_status"], "pending")
            original = Path(scratch) / "events" / (event["id"] + ".wav")
            original.write_bytes(b"RIFFORIG")
            enhanced = Path(scratch) / "events" / (event["id"] + ".enhanced.wav")
            enhanced.write_bytes(b"RIFFENH")
            with inbox.connect() as db:
                db.execute(
                    """UPDATE speech_events SET enhancement_status='complete', enhancement_path=? WHERE id=?""",
                    (str(enhanced), event["id"]),
                )
            self.assertEqual(inbox.event_audio_path(event["id"], "original").read_bytes(), b"RIFFORIG")
            self.assertEqual(inbox.event_audio_path(event["id"], "enhanced").read_bytes(), b"RIFFENH")
            server = viewer_mod.start_viewer(inbox, port=0)
            try:
                host, port = server.server_address
                token = inbox.root.joinpath("viewer.token").read_text().strip()
                auth = {"Host": "127.0.0.1", "Authorization": "Bearer " + token}
                client = http.client.HTTPConnection(host, port, timeout=5)
                client.request("GET", "/v1/events/" + event["id"] + "/audio?kind=original", headers=auth)
                response = client.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(body, b"RIFFORIG")
                self.assertIn("audio/wav", response.getheader("Content-Type"))
                client.close()
                client = http.client.HTTPConnection(host, port, timeout=5)
                client.request("GET", "/v1/events/" + event["id"] + "/audio?kind=enhanced", headers=auth)
                response = client.getresponse()
                self.assertEqual(response.read(), b"RIFFENH")
                client.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_enhance_without_cli_leaves_originals_and_asr_path(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".wav")
            write_tone(dest, 1.0)
            event_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "b" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(dest), 0, "complete", "present"),
                )
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
                db.execute(
                    "INSERT INTO speech_event_chunks VALUES (?,?,?,?)",
                    (event_id, chunk_id, 0.0, 1.0),
                )
            before = dest.read_bytes()
            vad_mod.enhance_event(inbox, event_id, "ffmpeg", None)
            self.assertEqual(dest.read_bytes(), before)
            with inbox.connect() as db:
                status = db.execute("SELECT enhancement_status FROM speech_events WHERE id=?",
                                    (event_id,)).fetchone()[0]
            self.assertEqual(status, "unavailable")
            self.assertTrue((inbox.root / "events" / (event_id + ".wav")).is_file())
            asr_source = Path(asr_mod.__file__).read_text()
            self.assertIn('row["path"]', asr_source)
            self.assertNotIn("enhanced.wav", asr_source)
            self.assertIn('neighbor["path"]', Path(diarization_mod.__file__).read_text())
            self.assertIn("play-original", viewer_mod.APP)
            self.assertIn("play-enhanced", viewer_mod.APP)
            self.assertIn("/v1/events/", viewer_mod.JS)
            self.assertNotIn("innerHTML", viewer_mod.JS)

    def test_enhance_uses_deep_filter_cli_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            event_id = str(uuid.uuid4())
            chunk_id = str(uuid.uuid4())
            source = inbox.audio / (chunk_id + ".wav")
            write_tone(source, 1.0)
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "h" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(source), 0, "complete", "present"),
                )
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)",
                           (event_id, chunk_id, 0.0, 1.0))

            def fake_extract(_inbox, _event_id, _ffmpeg, dest):
                write_tone(dest, 1.0, 48000)
                return dest

            def fake_run(argv, **_kwargs):
                self.assertIn("--compensate-delay", argv)
                output_dir = Path(argv[argv.index("--output-dir") + 1])
                write_tone(output_dir / Path(argv[-1]).name, 1.0, 48000)
                return mock.Mock(returncode=0)

            cli = Path(scratch) / "deep-filter"
            cli.write_text("binary")
            before = None
            with mock.patch.object(vad_mod, "extract_event_audio", side_effect=fake_extract), \
                    mock.patch.object(vad_mod.subprocess, "run", side_effect=fake_run):
                vad_mod.enhance_event(inbox, event_id, "ffmpeg", cli)
                original = inbox.root / "events" / (event_id + ".wav")
                before = original.read_bytes()
            self.assertEqual(original.read_bytes(), before)
            enhanced = inbox.root / "events" / (event_id + ".enhanced.wav")
            self.assertTrue(enhanced.is_file())
            with inbox.connect() as db:
                row = db.execute("SELECT enhancement_status,enhancement_path FROM speech_events WHERE id=?",
                                 (event_id,)).fetchone()
            self.assertEqual(row["enhancement_status"], "complete")
            self.assertEqual(Path(row["enhancement_path"]), enhanced)

    def test_extract_concatenates_original_slices(self):
        ffmpeg = "ffmpeg"
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            first = str(uuid.uuid4())
            second = str(uuid.uuid4())
            event_id = str(uuid.uuid4())
            for chunk_id, started in ((first, "2026-09-10T16:00:00.000Z"), (second, "2026-09-10T16:00:01.000Z")):
                dest = inbox.audio / (chunk_id + ".wav")
                write_tone(dest, 1.0)
                with inbox.connect() as db:
                    db.execute(
                        """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (chunk_id, "c" * 64, str(uuid.uuid4()), started,
                         1.0, str(dest), 0, "complete", "present"),
                    )
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.200Z",
                     1.2, "vad", vad_mod.ALGORITHM_VERSION),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, first, 0.5, 1.0))
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, second, 0.0, 0.7))
            dest = inbox.root / "events" / (event_id + ".wav")
            result = vad_mod.extract_event_audio(inbox, event_id, ffmpeg, dest)
            self.assertEqual(result, dest)
            with wave.open(str(dest), "rb") as handle:
                self.assertGreater(handle.getnframes(), 0)
                self.assertEqual(handle.getframerate(), 48000)

    def test_fingerprint_invalidates_stale_cache(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            device = str(uuid.uuid4())
            first = str(uuid.uuid4())
            dest = inbox.audio / (first + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (first, "a" * 64, device, "2026-09-10T16:00:00.000Z", 10.0, str(dest), 0, "complete", "present"),
                )
            vad_mod.save_chunk_spans(inbox, first, [{"start": 1.0, "end": 2.0}])
            vad_mod.rebuild_events(inbox)
            with inbox.connect() as db:
                event_id = db.execute("SELECT id FROM speech_events").fetchone()[0]
            original = inbox.root / "events" / (event_id + ".wav")
            enhanced = inbox.root / "events" / (event_id + ".enhanced.wav")
            original.write_bytes(b"OLDORIG")
            enhanced.write_bytes(b"OLDENH")
            with inbox.connect() as db:
                db.execute("UPDATE speech_events SET enhancement_status='complete', enhancement_path=? WHERE id=?",
                           (str(enhanced), event_id))
            vad_mod.save_chunk_spans(inbox, first, [{"start": 3.0, "end": 4.5}])
            vad_mod.rebuild_events(inbox)
            with inbox.connect() as db:
                rows = list(db.execute("SELECT id,content_fingerprint,enhancement_status FROM speech_events"))
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0]["id"], event_id)
            self.assertEqual(rows[0]["enhancement_status"], "pending")
            self.assertFalse(original.exists())
            self.assertFalse(enhanced.exists())

    def test_same_fingerprint_preserves_id(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            device = str(uuid.uuid4())
            first = str(uuid.uuid4())
            dest = inbox.audio / (first + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (first, "a" * 64, device, "2026-09-10T16:00:00.000Z", 10.0, str(dest), 0, "complete", "present"),
                )
            vad_mod.save_chunk_spans(inbox, first, [{"start": 1.0, "end": 2.0}])
            vad_mod.rebuild_events(inbox)
            with inbox.connect() as db:
                first_id = db.execute("SELECT id FROM speech_events").fetchone()[0]
            vad_mod.rebuild_events(inbox)
            with inbox.connect() as db:
                second_id = db.execute("SELECT id FROM speech_events").fetchone()[0]
            self.assertEqual(first_id, second_id)

    def test_anonymous_keys_are_scoped_by_run(self):
        device = str(uuid.uuid4())
        first = chunk(device, "2026-09-10T12:00:00.000Z", 10, "a")
        second = chunk(device, "2026-09-10T12:00:10.000Z", 10, "b")
        turns = {
            "a": [{"started": 8.0, "ended": 9.5, "person_id": None, "speaker_key": "S1", "run_id": "run-a", "chunk_id": "a"}],
            "b": [{"started": 0.2, "ended": 2.0, "person_id": None, "speaker_key": "S1", "run_id": "run-b", "chunk_id": "b"}],
        }
        events = vad_mod.assemble_events(
            [first, second],
            {"a": [{"start": 8.0, "end": 9.5}], "b": [{"start": 0.2, "end": 2.0}]},
            turns,
        )
        self.assertEqual(len(events), 2)

    def test_span_crossing_manual_bound_is_split(self):
        device = str(uuid.uuid4())
        first = chunk(device, "2026-09-10T12:00:00.000Z", 10, "a")
        intervals = [{
            "source": "manual",
            "device_id": device,
            "started_at": "2026-09-10T12:00:05.000Z",
            "ended_at": "2026-09-10T12:20:00.000Z",
        }]
        events = vad_mod.assemble_events(
            [first],
            {"a": [{"start": 1.0, "end": 8.0}]},
            {},
            intervals,
        )
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(events[0]["playable_duration"], 4.0)
        self.assertAlmostEqual(events[1]["playable_duration"], 3.0)

    def test_derived_assets_count_and_orphans_are_collected(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"x" * 100)
            event_id = str(uuid.uuid4())
            original = inbox.root / "events" / (event_id + ".wav")
            original.write_bytes(b"y" * 50)
            orphan = inbox.root / "events" / "orphan.wav"
            orphan.write_bytes(b"z" * 25)
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state,audio_bytes,audio_expires_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "d" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(dest), 0, "complete", "present", 100, time.time() + 3600),
                )
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,derived_bytes,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','unavailable',50,0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, chunk_id, 0.0, 1.0))
            inbox.cleanup_completed()
            self.assertFalse(orphan.exists())
            self.assertTrue(original.exists())
            with inbox.connect() as db:
                db.execute("UPDATE chunks SET audio_expires_at=0, audio_pinned=0 WHERE id=?", (chunk_id,))
            inbox.cleanup_completed()
            self.assertFalse(dest.exists())
            self.assertFalse(original.exists())
            with inbox.connect() as db:
                status = db.execute("SELECT enhancement_status FROM speech_events WHERE id=?",
                                    (event_id,)).fetchone()[0]
            self.assertEqual(status, "unavailable")

    def test_over_cap_includes_derived_wavs(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"x" * 80)
            event_id = str(uuid.uuid4())
            derived = inbox.root / "events" / (event_id + ".wav")
            derived.write_bytes(b"y" * 40)
            orphan = inbox.root / "events" / "orphan.wav"
            orphan.write_bytes(b"z" * 25)
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state,audio_bytes,audio_expires_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "e" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(dest), 0, "complete", "present", 80, time.time() + 3600),
                )
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,derived_bytes,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','unavailable',40,0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, chunk_id, 0.0, 1.0))
            with mock.patch.object(receiver_mod, "MAX_RETAINED_BYTES", 100):
                inbox.cleanup_completed()
            self.assertTrue(dest.exists())
            self.assertFalse(derived.exists())
            self.assertFalse(orphan.exists())

    def test_concurrent_extraction_is_atomic(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".wav")
            write_tone(dest, 1.0)
            event_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "f" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(dest), 0, "complete", "present"),
                )
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at,content_fingerprint)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0,?)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION,
                     vad_mod.event_fingerprint([{"chunk_id": chunk_id, "start": 0.0, "end": 1.0}])),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, chunk_id, 0.0, 1.0))
            target = inbox.root / "events" / (event_id + ".wav")
            results = []
            errors = []

            def run():
                try:
                    results.append(vad_mod.extract_event_audio(inbox, event_id, "ffmpeg", target))
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            self.assertTrue(all(path == target for path in results if path))
            self.assertTrue(target.is_file())
            leftovers = [path for path in (inbox.root / "events").iterdir() if path.is_dir() or path.suffix == ".txt"]
            self.assertEqual(leftovers, [])

    def test_event_audio_auth_range_and_head(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            event_id = str(uuid.uuid4())
            audio = inbox.root / "events" / (event_id + ".wav")
            audio.write_bytes(b"RIFF0123456789")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,enhancement_error,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','unavailable','cli_unavailable',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".wav")
            dest.write_bytes(b"src")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "g" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     1.0, str(dest), 0, "complete", "present"),
                )
                db.execute("INSERT INTO speech_event_chunks VALUES (?,?,?,?)", (event_id, chunk_id, 0.0, 1.0))
            server = viewer_mod.start_viewer(inbox, port=0)
            try:
                host, port = server.server_address
                token = inbox.root.joinpath("viewer.token").read_text().strip()
                auth = {"Host": "127.0.0.1", "Authorization": "Bearer " + token}
                client = http.client.HTTPConnection(host, port, timeout=5)
                client.request("GET", "/v1/events/" + event_id + "/audio?kind=original")
                self.assertEqual(client.getresponse().status, 401)
                client.close()
                client = http.client.HTTPConnection(host, port, timeout=5)
                client.request("HEAD", "/v1/events/" + event_id + "/audio?kind=original", headers=auth)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(int(response.getheader("Content-Length")), audio.stat().st_size)
                client.close()
                client = http.client.HTTPConnection(host, port, timeout=5)
                client.request("GET", "/v1/events/" + event_id + "/audio?kind=original",
                               headers={**auth, "Range": "bytes=0-3"})
                response = client.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"RIFF")
                client.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_enhance_retries_then_fails_closed(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            event_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
            cli = Path(scratch) / "deep-filter"
            cli.write_text("binary")
            with mock.patch.object(vad_mod, "extract_event_audio", return_value=inbox.root / "events" / (event_id + ".wav")),                     mock.patch.object(vad_mod.subprocess, "run", return_value=mock.Mock(returncode=1)):
                (inbox.root / "events" / (event_id + ".wav")).write_bytes(b"orig")
                vad_mod.enhance_event(inbox, event_id, "ffmpeg", cli)
            with inbox.connect() as db:
                row = db.execute("SELECT enhancement_status,enhancement_attempts,enhancement_retry_at FROM speech_events WHERE id=?",
                                 (event_id,)).fetchone()
            self.assertEqual(row["enhancement_status"], "pending")
            self.assertEqual(row["enhancement_attempts"], 1)
            self.assertGreater(row["enhancement_retry_at"], time.time())
            with inbox.connect() as db:
                db.execute("UPDATE speech_events SET enhancement_attempts=? WHERE id=?",
                           (vad_mod.MAX_ENHANCE_ATTEMPTS - 1, event_id))
            with mock.patch.object(vad_mod, "extract_event_audio", return_value=inbox.root / "events" / (event_id + ".wav")),                     mock.patch.object(vad_mod.subprocess, "run", return_value=mock.Mock(returncode=1)):
                vad_mod.enhance_event(inbox, event_id, "ffmpeg", cli)
            with inbox.connect() as db:
                row = db.execute("SELECT enhancement_status,enhancement_attempts FROM speech_events WHERE id=?",
                                 (event_id,)).fetchone()
            self.assertEqual(row["enhancement_status"], "failed")
            self.assertEqual(row["enhancement_attempts"], vad_mod.MAX_ENHANCE_ATTEMPTS)

    def test_extraction_failure_uses_bounded_retry(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            event_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','pending',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
            cli = Path(scratch) / "deep-filter"
            cli.write_text("binary")
            with mock.patch.object(vad_mod, "extract_event_audio", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                vad_mod.enhance_event(inbox, event_id, "ffmpeg", cli)
            with inbox.connect() as db:
                row = db.execute("SELECT enhancement_status,enhancement_attempts,enhancement_retry_at FROM speech_events WHERE id=?",
                                 (event_id,)).fetchone()
            self.assertEqual(row["enhancement_status"], "pending")
            self.assertEqual(row["enhancement_attempts"], 1)
            self.assertGreater(row["enhancement_retry_at"], time.time())

    def test_reconcile_requeues_when_cli_appears(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            event_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO speech_events
                       (id,device_id,started,ended,duration,source,algorithm_version,status,
                        enhancement_status,enhancement_error,created_at)
                       VALUES (?,?,?,?,?,?,?,'complete','unavailable','cli_unavailable',0)""",
                    (event_id, "device", "2026-09-10T16:00:00.000Z", "2026-09-10T16:00:01.000Z",
                     1.0, "vad", vad_mod.ALGORITHM_VERSION),
                )
            cli = Path(scratch) / "deep-filter"
            cli.write_text("binary")
            vad_mod.reconcile_enhancement(inbox, cli)
            with inbox.connect() as db:
                status = db.execute("SELECT enhancement_status FROM speech_events WHERE id=?",
                                    (event_id,)).fetchone()[0]
            self.assertEqual(status, "pending")

    def test_viewer_defaults_to_recordings_and_shows_playable_duration(self):
        self.assertIn('if (!isMobile() && !selectedId && chunks[0])', viewer_mod.JS)
        self.assertNotIn('if (!isMobile() && !selectedId && events[0])', viewer_mod.JS)
        self.assertIn("playable_duration", viewer_mod.JS)
        self.assertIn("seconds playable", viewer_mod.JS)
        self.assertIn("Search recordings", viewer_mod.APP)
        self.assertIn("const events = [];", viewer_mod.JS)
        self.assertNotIn("const stillEvent", viewer_mod.JS)
        self.assertLess(viewer_mod.JS.index('for (const chunk of chunks)'),
                        viewer_mod.JS.index('list.appendChild(eventRows)'))


if __name__ == "__main__":
    unittest.main()
