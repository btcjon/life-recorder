import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import asr as asr_mod
import detector as detector_mod
import diarization as diarization_mod
import meetings as meetings_mod
import receiver as receiver_mod
import viewer as viewer_mod
from receiver import Handler, Inbox, Receiver

EASTERN = ZoneInfo("America/New_York")
PARAKEET_FIXTURE = {
    "audioFile": "/tmp/jfk.wav",
    "confidence": 0.98,
    "durationSeconds": 11,
    "mode": "batch",
    "modelVersion": "v3",
    "processingTimeSeconds": 0.26,
    "rtfx": 40.9,
    "text": "hello from a public fixture",
    "wordTimings": [
        {"word": "hello", "startTime": 0.0, "endTime": 0.4, "confidence": 0.9},
        {"word": "from", "startTime": 0.4, "endTime": 0.7, "confidence": 0.9},
    ],
}


class Row(dict):
    def __getitem__(self, key):
        return dict.get(self, key)


def chunk(device, started, duration=60.0, text="", chunk_id=None, status="complete"):
    return Row({
        "id": chunk_id or str(uuid.uuid4()),
        "device": device,
        "started": started,
        "duration": duration,
        "transcript": text,
        "word_count": len(text.split()) if text else 0,
        "status": status,
        "error": None,
        "engine": "parakeet",
        "path": "/tmp/missing.m4a",
    })


class EngineAndAsrTests(unittest.TestCase):
    def test_legacy_engine_selection(self):
        self.assertEqual(asr_mod.resolve_engine(None, None, None), "whisper")
        self.assertEqual(asr_mod.resolve_engine(None, "/mlx", "/model"), "mlx")
        self.assertEqual(asr_mod.resolve_engine("parakeet", "/mlx", "/model"), "parakeet")
        self.assertEqual(asr_mod.resolve_engine("whisper", "/mlx", "/model"), "whisper")

    def test_parakeet_parser_accepts_public_schema(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "out.json"
            path.write_text(json.dumps(PARAKEET_FIXTURE))
            text, summary = asr_mod.parse_parakeet_output(path)
        self.assertEqual(text, "hello from a public fixture")
        self.assertEqual(summary["modelVersion"], "v3")
        self.assertEqual(summary["wordCount"], 2)
        self.assertIn("processingTimeSeconds", summary)

    def test_parakeet_parser_rejects_malformed_or_missing(self):
        with tempfile.TemporaryDirectory() as scratch:
            missing = Path(scratch) / "nope.json"
            with self.assertRaises(ValueError):
                asr_mod.parse_parakeet_output(missing)
            bad = Path(scratch) / "bad.json"
            bad.write_text("{")
            with self.assertRaises(ValueError):
                asr_mod.parse_parakeet_output(bad)
            empty = Path(scratch) / "empty.json"
            empty.write_text(json.dumps({"mode": "batch"}))
            with self.assertRaises(ValueError):
                asr_mod.parse_parakeet_output(empty)

    def test_adapter_uses_argv_and_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            wav = work / "clip.m4a"
            wav.write_bytes(b"audio")
            row = {"id": "clip", "path": str(wav)}
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                self.assertFalse(kwargs.get("shell"))
                if argv[0] == "ffmpeg":
                    Path(argv[-1]).write_bytes(b"RIFF")
                    return mock.Mock(returncode=0)
                out = Path(argv[argv.index("--output-json") + 1])
                out.write_text(json.dumps(PARAKEET_FIXTURE))
                return mock.Mock(returncode=0)

            config = asr_mod.AsrConfig(
                engine="parakeet", ffmpeg="ffmpeg",
                parakeet_cli=Path("/bin/fluidaudiocli"),
                parakeet_model_dir=Path("/models/parakeet"),
            )
            with mock.patch("asr.subprocess.run", side_effect=fake_run):
                text, provenance = asr_mod.transcribe_chunk(row, config, work, lambda s: s)
            self.assertIn("hello", text)
            self.assertEqual(provenance["engine"], "parakeet")
            self.assertEqual(calls[1][:2], ["/bin/fluidaudiocli", "transcribe"])
            self.assertIn("--output-json", calls[1])
            leftovers = [p for p in work.iterdir() if p.suffix in {".wav", ".json"}]
            self.assertEqual(leftovers, [])

    def test_malformed_asr_keeps_audio_for_retry(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            audio = inbox.audio / "keep.m4a"
            audio.write_bytes(b"clip")
            chunk_id = str(uuid.uuid4())
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                       VALUES (?,?,?,?,?,?,?)""",
                    (chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     60.0, str(audio), 0),
                )
            row = inbox.receipt(chunk_id)
            config = asr_mod.AsrConfig(engine="parakeet", ffmpeg="ffmpeg",
                                       parakeet_cli=Path("/bin/false"),
                                       parakeet_model_dir=Path(scratch))
            with mock.patch("asr.subprocess.run", side_effect=RuntimeError("bad asr")):
                with self.assertRaises(RuntimeError):
                    asr_mod.transcribe_chunk(row, config, inbox.root / "processing", lambda s: s)
            self.assertTrue(audio.exists())
            self.assertEqual(inbox.receipt(chunk_id)["status"], "pending")


class MeetingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.server = Receiver(("127.0.0.1", 0), Handler)
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device = str(uuid.uuid4())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def post_event(self, payload, token=None, origin=None):
        headers = {
            "Authorization": "Bearer " + (token or self.inbox.token),
            "Content-Type": "application/json",
        }
        if origin:
            headers["Origin"] = origin
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        body = json.dumps(payload).encode()
        client.request("POST", "/v1/meeting-events", body, headers)
        response = client.getresponse()
        data = json.loads(response.read() or b"{}")
        client.close()
        return response.status, data

    def event(self, kind, occurred_at, meeting_id=None, event_id=None, device=None):
        return {
            "version": 1,
            "event_id": event_id or str(uuid.uuid4()),
            "meeting_id": meeting_id or str(uuid.uuid4()),
            "device_id": device or self.device,
            "kind": kind,
            "occurred_at": occurred_at,
        }

    def test_durable_duplicate_and_conflict(self):
        payload = self.event("start", "2026-09-10T16:00:00.000Z")
        status, body = self.post_event(payload)
        self.assertEqual(status, 201)
        self.assertTrue(body["durable"])
        status, body = self.post_event(payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["durable"])
        conflict = dict(payload)
        conflict["kind"] = "end"
        status, _ = self.post_event(conflict)
        self.assertEqual(status, 409)

    def test_out_of_order_and_offline_replay(self):
        meeting = str(uuid.uuid4())
        end = self.event("end", "2026-09-10T16:20:00.000Z", meeting_id=meeting)
        start = self.event("start", "2026-09-10T16:00:00.000Z", meeting_id=meeting)
        self.assertEqual(self.post_event(end)[0], 201)
        self.assertEqual(self.post_event(start)[0], 201)
        self.inbox.rebuild_derived(now=datetime(2026, 9, 10, 16, 30, tzinfo=timezone.utc))
        with self.inbox.connect() as db:
            row = db.execute("SELECT * FROM intervals WHERE source='manual'").fetchone()
        self.assertEqual(row["closure_reason"], "explicit")
        self.assertEqual(row["started_at"], "2026-09-10T16:00:00.000Z")

    def test_rejects_future_and_oversize(self):
        future = self.event("start", "2099-01-01T00:00:00.000Z")
        self.assertEqual(self.post_event(future)[0], 400)
        headers = {"Authorization": "Bearer " + self.inbox.token, "Content-Type": "application/json"}
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("POST", "/v1/meeting-events", b"x" * 5000, headers)
        self.assertEqual(client.getresponse().status, 413)
        client.close()

    def test_health_has_no_transcript_api(self):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("GET", "/v1/days", headers={"Authorization": "Bearer " + self.inbox.token})
        self.assertEqual(client.getresponse().status, 404)
        client.close()


class ClosureAndDetectorTests(unittest.TestCase):
    def test_manual_deadline_four_hours_or_quiet_hours(self):
        start = datetime(2026, 9, 10, 14, 0, tzinfo=EASTERN)
        deadline = meetings_mod.manual_deadline(start)
        self.assertEqual(deadline.astimezone(EASTERN).hour, 18)
        evening = datetime(2026, 9, 10, 20, 0, tzinfo=EASTERN)
        quiet = meetings_mod.manual_deadline(evening)
        self.assertEqual(quiet.astimezone(EASTERN).hour, 22)

    def test_dst_quiet_hours_boundary(self):
        spring = datetime(2026, 3, 8, 20, 0, tzinfo=EASTERN)
        self.assertEqual(meetings_mod.manual_deadline(spring).astimezone(EASTERN).isoformat(),
                         "2026-03-08T22:00:00-04:00")
        fall = datetime(2026, 11, 1, 20, 0, tzinfo=EASTERN)
        self.assertEqual(meetings_mod.manual_deadline(fall).astimezone(EASTERN).isoformat(),
                         "2026-11-01T22:00:00-05:00")

    def test_capture_gap_requires_later_chunk(self):
        device = str(uuid.uuid4())
        meeting = str(uuid.uuid4())
        events = [Row({
            "event_id": str(uuid.uuid4()), "meeting_id": meeting, "device_id": device,
            "kind": "start", "occurred_at": "2026-09-10T16:00:00.000Z",
        })]
        only = [chunk(device, "2026-09-10T16:00:00.000Z")]
        now = datetime(2026, 9, 10, 17, 0, tzinfo=timezone.utc)
        open_interval = meetings_mod.rebuild_manual_intervals(events, only, now=now)[0]
        self.assertEqual(open_interval["status"], "open")
        later = only + [chunk(device, "2026-09-10T16:20:00.000Z")]
        closed = meetings_mod.rebuild_manual_intervals(events, later, now=now)[0]
        self.assertEqual(closed["closure_reason"], "capture_gap")

    def test_empty_transcript_does_not_invent_gap(self):
        device = str(uuid.uuid4())
        rows = [
            chunk(device, "2026-09-10T16:00:00.000Z", text="alpha beta gamma delta epsilon"),
            chunk(device, "2026-09-10T16:01:00.000Z", text=""),
            chunk(device, "2026-09-10T16:02:00.000Z", text="zeta eta theta iota kappa"),
        ]
        self.assertEqual(meetings_mod.capture_gaps(rows), [])

    def test_detector_positive_negative_and_tv(self):
        device = str(uuid.uuid4())
        words = " ".join(["alpha"] * 40)
        meeting = "this meeting will review the agenda item now. " + words
        action = "our next steps and action item are listed here. " + words
        rows = []
        for index in range(8):
            text = meeting if index == 0 else action if index == 1 else words
            started = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
            rows.append(chunk(device, meetings_mod.format_utc(started), text=text))
        found = detector_mod.detect_possible_events(rows, now=datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
        self.assertTrue(found)
        self.assertEqual(found[0]["label"], "Possible event")
        bland = [chunk(device, meetings_mod.format_utc(datetime(2026, 9, 10, 16, tzinfo=timezone.utc) + timedelta(minutes=i)),
                       text="yes work okay sure fine " * 20) for i in range(8)]
        self.assertEqual(detector_mod.detect_possible_events(bland), [])
        tv = [chunk(device, meetings_mod.format_utc(datetime(2026, 9, 10, 16, tzinfo=timezone.utc) + timedelta(minutes=i)),
                    text="welcome back after the break, more sports highlights " * 20) for i in range(8)]
        self.assertEqual(detector_mod.detect_possible_events(tv), [])

    def test_manual_precedence_and_late_chunks(self):
        device = str(uuid.uuid4())
        words = " ".join(["alpha"] * 40)
        rows = []
        for index in range(8):
            text = ("this meeting starts now. " + words) if index == 0 else ("next steps and action item. " + words)
            started = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
            rows.append(chunk(device, meetings_mod.format_utc(started), text=text))
        possible = detector_mod.detect_possible_events(rows, now=datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
        manuals = [{
            "device_id": device, "started_at": "2026-09-10T16:02:00.000Z",
            "ended_at": "2026-09-10T16:05:00.000Z", "source": "manual",
        }]
        clipped = detector_mod.apply_manual_precedence(possible, manuals)
        for item in clipped:
            start = meetings_mod.parse_utc(item["started_at"])
            end = meetings_mod.parse_utc(item["ended_at"]) if item["ended_at"] else None
            self.assertFalse(meetings_mod.overlap(start, end, meetings_mod.parse_utc("2026-09-10T16:02:00.000Z"),
                                                  meetings_mod.parse_utc("2026-09-10T16:05:00.000Z")))
        early = rows[2:]
        late = rows
        first = detector_mod.detect_possible_events(early, now=datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
        later = detector_mod.detect_possible_events(late, now=datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
        self.assertTrue(later)

    def test_mid_chunk_boundary_overlap_not_whole_minute(self):
        start = datetime(2026, 9, 10, 16, 0, 30, tzinfo=timezone.utc)
        end = start + timedelta(seconds=60)
        meeting_at = start + timedelta(seconds=20)
        self.assertTrue(meetings_mod.overlap(start, end, meeting_at, meeting_at + timedelta(minutes=10)))
        self.assertFalse(meetings_mod.overlap(start, meeting_at, meeting_at, meeting_at + timedelta(minutes=10)))


    def test_four_hour_expiry_uses_injected_clock(self):
        device = str(uuid.uuid4())
        meeting = str(uuid.uuid4())
        events = [Row({
            "event_id": str(uuid.uuid4()), "meeting_id": meeting, "device_id": device,
            "kind": "start", "occurred_at": "2026-09-10T14:00:00.000Z",
        })]
        now = datetime(2026, 9, 10, 18, 5, tzinfo=timezone.utc)
        interval = meetings_mod.rebuild_manual_intervals(events, [], now=now)[0]
        self.assertEqual(interval["closed"], 1)
        self.assertEqual(interval["closure_reason"], "deadline")


class WorkerRetryTests(unittest.TestCase):
    def test_worker_retries_then_exports_once(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                       VALUES (?,?,?,?,?,?,?)""",
                    (chunk_id, "f" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     60.0, str(dest), 0),
                )
            stop = threading.Event()
            calls = {"n": 0}

            def fake_transcribe(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient")
                return "once only", {"engine": "parakeet", "model": "parakeet", "summary": {"processingTimeSeconds": 0.2}}

            with mock.patch.object(receiver_mod, "transcribe", side_effect=fake_transcribe):
                thread = threading.Thread(
                    target=receiver_mod.worker,
                    args=(inbox, stop, Path("/tmp/model"), "whisper", "ffmpeg"),
                    daemon=True,
                )
                thread.start()
                deadline = datetime.now(timezone.utc) + timedelta(seconds=8)
                while datetime.now(timezone.utc) < deadline:
                    row = inbox.receipt(chunk_id)
                    if row["status"] == "complete":
                        break
                    if row["attempts"] >= 1 and row["status"] == "pending":
                        with inbox.connect() as db:
                            db.execute("UPDATE chunks SET retry_at=0 WHERE id=?", (chunk_id,))
                    import time as time_mod
                    time_mod.sleep(0.05)
                stop.set()
                thread.join(timeout=2)
            row = inbox.receipt(chunk_id)
            self.assertEqual(row["status"], "complete")
            self.assertEqual(row["engine"], "parakeet")
            self.assertEqual((inbox.root / "life.md").read_text().count("once only"), 1)
            self.assertTrue(dest.exists())
            reopened = Inbox(Path(scratch))
            self.assertEqual((reopened.root / "life.md").read_text().count("once only"), 1)


class PersistenceAndViewerTests(unittest.TestCase):
    def test_export_failure_does_not_delete_source(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                       VALUES (?,?,?,?,?,?,?)""",
                    (chunk_id, "b" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     60.0, str(dest), 0),
                )
            with mock.patch.object(receiver_mod, "atomic_write", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    inbox.complete(chunk_id, "kept audio")
            self.assertTrue(dest.exists())
            self.assertEqual(inbox.receipt(chunk_id)["status"], "complete")

    def test_completed_audio_is_retained_after_restart(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            inbox = Inbox(root)
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,status,transcript,received)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "c" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     60.0, str(dest), "complete", "done", datetime.now(timezone.utc).timestamp()),
                )
            reopened = Inbox(root)
            self.assertTrue(dest.exists())
            self.assertEqual(reopened.receipt(chunk_id)["status"], "complete")

    def test_migration_on_synthetic_copy(self):
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / "inbox.sqlite3"
            con = sqlite3.connect(db)
            con.execute("""CREATE TABLE chunks (
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, device TEXT NOT NULL,
                started TEXT NOT NULL, duration REAL NOT NULL, path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', transcript TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                error TEXT, received REAL NOT NULL)""")
            con.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "d" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                 60.0, str(Path(scratch) / "x.m4a"), "complete", "hello", 0, 0, None, 1),
            )
            con.commit()
            con.close()
            inbox = Inbox(Path(scratch))
            with inbox.connect() as dbh:
                version = dbh.execute("PRAGMA user_version").fetchone()[0]
                cols = {row[1] for row in dbh.execute("PRAGMA table_info(chunks)")}
                tables = {row[0] for row in dbh.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertGreaterEqual(version, 1)
            self.assertTrue({"engine", "word_count"} <= cols)
            self.assertIn("meeting_events", tables)
            self.assertIn("intervals", tables)

    def test_viewer_auth_origin_and_html_inert(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                       VALUES (?,?,?,?,?,?,?)""",
                    (chunk_id, "e" * 64, str(uuid.uuid4()), "2026-09-10T16:00:00.000Z",
                     60.0, str(dest), 0),
                )
            inbox.complete(chunk_id, "<script>alert(1)</script> hello")
            server = viewer_mod.start_viewer(inbox, port=0)
            self.assertIsNotNone(server)
            try:
                host, port = server.server_address
                token = inbox.root.joinpath("viewer.token").read_text().strip()
                def get(path, headers=None):
                    client = http.client.HTTPConnection(host, port, timeout=5)
                    client.request("GET", path, headers=headers or {})
                    response = client.getresponse()
                    body = response.read()
                    header = dict(response.getheaders())
                    client.close()
                    return response.status, body, header
                status, page, page_headers = get("/", {"Host": "127.0.0.1"})
                self.assertEqual(status, 200)
                self.assertIn("textContent", viewer_mod.JS)
                self.assertNotIn("innerHTML", viewer_mod.JS)
                self.assertIn("Choose a person…", viewer_mod.JS)
                self.assertIn("data-identity-editor", viewer_mod.JS)
                self.assertIn("tab-recordings", viewer_mod.APP)
                self.assertIn("tab-people", viewer_mod.APP)
                self.assertIn("Content-Security-Policy", {k.title(): k for k in page_headers} | {k: k for k in page_headers})
                status, _, _ = get("/v1/days", {"Host": "127.0.0.1"})
                self.assertEqual(status, 401)
                auth = {"Host": "127.0.0.1", "Authorization": "Bearer " + token}
                status, body, headers = get("/v1/days", auth)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Cache-Control"), "no-store")
                days = json.loads(body)["days"]
                self.assertIn("2026-09-10", days)
                status, payload, _ = get("/v1/days/2026-09-10", auth)
                data = json.loads(payload)
                self.assertTrue(any("<script>" in (c["transcript"] or "") for c in data["chunks"]))
                status, audio, headers = get("/v1/audio/" + chunk_id, {**auth, "Range": "bytes=1-3"})
                self.assertEqual(status, 206)
                self.assertEqual(audio, b"udi")
                self.assertEqual(headers.get("Accept-Ranges"), "bytes")
                status, _, _ = get("/v1/days", {**auth, "Origin": "https://evil.example"})
                self.assertEqual(status, 403)
                status, _, _ = get("/v1/days", {**auth, "Host": "example.com"})
                self.assertEqual(status, 403)
                self.assertTrue((inbox.root / "open-viewer.command").exists())
                helper = (inbox.root / "open-viewer.py").read_text()
                self.assertIn("127.0.0.1:8767/#", helper)
                self.assertNotIn("?", helper.split("127.0.0.1:8767/#", 1)[1][:20].strip())
            finally:
                server.shutdown()
                server.server_close()


class DiarizationDiagnosticsTests(unittest.TestCase):
    def test_outcome_semantics(self):
        turns = [{"started": 1.0, "ended": 4.6, "speaker_key": "S1"}]
        self.assertEqual(diarization_mod.classify_outcome([], 60, asr_words=0, no_speech=True), "no_speech")
        self.assertEqual(diarization_mod.classify_outcome([], 60, asr_words=40, no_speech=True), "low_coverage")
        self.assertEqual(diarization_mod.classify_outcome(turns, 113.2, asr_words=80), "low_coverage")
        self.assertEqual(diarization_mod.classify_outcome(
            [{"started": 0, "ended": 50, "speaker_key": "S1"}], 60, asr_words=80), "success")

    def test_context_window_keeps_current_offset(self):
        device = str(uuid.uuid4())
        earlier = chunk(device, "2026-09-10T12:00:00.000Z", duration=60, chunk_id=str(uuid.uuid4()))
        current = chunk(device, "2026-09-10T12:01:00.000Z", duration=60, chunk_id=str(uuid.uuid4()))
        selected, offset = diarization_mod.context_window([earlier, current], current)
        self.assertEqual([row["id"] for row in selected], [earlier["id"], current["id"]])
        self.assertEqual(offset, 60.0)

    def test_context_offset_uses_concatenated_audio_not_wall_clock_gap(self):
        device = str(uuid.uuid4())
        earlier = chunk(device, "2026-09-10T12:00:00.000Z", duration=60, chunk_id=str(uuid.uuid4()))
        current = chunk(device, "2026-09-10T12:01:30.000Z", duration=60, chunk_id=str(uuid.uuid4()))
        selected, offset = diarization_mod.context_window([earlier, current], current)
        self.assertEqual([row["id"] for row in selected], [earlier["id"], current["id"]])
        self.assertEqual(offset, 60.0)

    def _run_process(self, payload, exported, row, neighbors=None, stderr=b"", returncode=0):
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "ffmpeg" and argv[1] == "-nostdin" and "-i" in argv and argv[-1].endswith(".wav"):
                Path(argv[-1]).write_bytes(b"RIFF")
                return mock.Mock(returncode=0, stderr=b"")
            if argv[0] == "ffmpeg" and "-concat" in argv or (len(argv) > 3 and argv[3] == "concat"):
                Path(argv[-1]).write_bytes(b"RIFF")
                return mock.Mock(returncode=0, stderr=b"")
            if "process" in argv:
                Path(argv[argv.index("--output") + 1]).write_text(json.dumps(payload))
                Path(argv[argv.index("--export-embeddings") + 1]).write_text(json.dumps(exported))
                return mock.Mock(returncode=returncode, stderr=stderr)
            return mock.Mock(returncode=0, stderr=b"")
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            audio = work / "clip.m4a"
            audio.write_bytes(b"audio")
            row = dict(row)
            row["path"] = str(audio)
            ready_neighbors = None
            if neighbors is not None:
                ready_neighbors = []
                for item in neighbors:
                    item = dict(item)
                    if item.get("id") == row["id"] or "path" not in item:
                        item["path"] = str(audio)
                    ready_neighbors.append(item)
            with mock.patch("diarization.subprocess.run", side_effect=fake_run):
                result = diarization_mod.process_chunk(row, Path("/bin/fluidaudiocli"), "ffmpeg", work,
                                                       neighbors=ready_neighbors)
            leftovers = [path.name for path in work.iterdir() if path.suffix in {".wav", ".json"} and path.name != "clip.m4a"]
            self.assertEqual(leftovers, [])
        return result, calls

    def test_one_and_two_speaker_fixtures(self):
        row = {"id": str(uuid.uuid4()), "duration": 10.0, "word_count": 20,
               "started": "2026-09-10T12:00:00.000Z", "device": str(uuid.uuid4())}
        one = self._run_process(
            {"segments": [{"speakerId": "S1", "startTimeSeconds": 0.2, "endTimeSeconds": 8.0, "qualityScore": 0.9}],
             "speakerCount": 1},
            [{"cluster": 0, "rho128": [1.0] + [0.0] * 127}],
            row)[0]
        self.assertEqual(one["outcome"], "success")
        self.assertEqual(one["turn_count"], 1)
        self.assertEqual(one["cluster_count"], 1)
        self.assertEqual(one["embedding_count"], 1)
        self.assertGreater(one["coverage"], 0.7)
        two = self._run_process(
            {"segments": [
                {"speakerId": "S1", "startTimeSeconds": 0.0, "endTimeSeconds": 4.0, "qualityScore": 0.9},
                {"speakerId": "S2", "startTimeSeconds": 4.0, "endTimeSeconds": 9.5, "qualityScore": 0.8},
            ], "speakerCount": 2},
            [{"cluster": 0, "rho128": [1.0] + [0.0] * 127},
             {"cluster": 1, "rho128": [0.0, 1.0] + [0.0] * 126}],
            row)[0]
        self.assertEqual(two["speaker_count"], 2)
        self.assertEqual(two["turn_count"], 2)
        self.assertEqual({turn["speaker_key"] for turn in two["turns"]}, {"S1", "S2"})

    def test_tuned_segmentation_arguments_are_applied(self):
        row = {"id": str(uuid.uuid4()), "duration": 10.0, "word_count": 20,
               "started": "2026-09-10T12:00:00.000Z", "device": str(uuid.uuid4())}
        _result, calls = self._run_process({"segments": [], "speakerCount": 0}, [], row)
        command = next(call for call in calls if "process" in call)
        self.assertEqual(command[command.index("--onset-threshold") + 1], "0.3")
        self.assertEqual(command[command.index("--offset-threshold") + 1], "0.3")
        self.assertEqual(command[command.index("--min-segment-duration") + 1], "0.3")
        self.assertEqual(command[command.index("--min-gap-duration") + 1], "0.8")
        self.assertEqual(command[command.index("--threshold") + 1], "0.45")

    def test_nospeech_with_asr_text_is_low_coverage(self):
        row = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 40,
               "started": "2026-09-10T12:00:00.000Z", "device": str(uuid.uuid4())}
        result = self._run_process({"segments": [], "speakerCount": 0}, [], row,
                                   stderr=b"noSpeechDetected", returncode=1)[0]
        self.assertEqual(result["outcome"], "low_coverage")
        self.assertEqual(result["turn_count"], 0)

    def test_context_turns_are_clipped_to_current_clip(self):
        device = str(uuid.uuid4())
        current = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 12,
                   "started": "2026-09-10T12:01:00.000Z", "device": device}
        earlier = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 8,
                   "started": "2026-09-10T12:00:00.000Z", "device": device,
                   "path": "/tmp/missing-earlier.m4a"}
        result = self._run_process(
            {"segments": [{"speakerId": "S1", "startTimeSeconds": 50.0, "endTimeSeconds": 70.0, "qualityScore": 1.0}],
             "speakerCount": 1},
            [{"cluster": 0, "rho128": [1.0] + [0.0] * 127}],
            current, neighbors=[earlier, current])[0]
        self.assertEqual(result["turns"][0]["started"], 0.0)
        self.assertEqual(result["turns"][0]["ended"], 10.0)
        self.assertEqual(result["context_offset"], 60.0)

    def test_malformed_output_rejected(self):
        row = {"id": str(uuid.uuid4()), "duration": 10.0, "word_count": 4,
               "started": "2026-09-10T12:00:00.000Z", "device": str(uuid.uuid4())}
        with self.assertRaises(ValueError):
            self._run_process({"speakerCount": 1}, [], row)

    def test_rerun_preserves_manual_labels_and_samples(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,transcript,word_count,audio_state)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     20.0, str(dest), 0, "complete", "hello there", 2, "present"))
            person = inbox.create_person("Jon")
            first = {"turns": [{"speaker_key": "S1", "started": 1.0, "ended": 8.0, "quality": 1.0,
                                "embedding": [1.0] + [0.0] * 127}],
                     "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                     "speech_seconds": 7.0, "coverage": 0.35, "turn_count": 1,
                     "embedding_count": 1, "cluster_count": 1, "asr_words": 2}
            diarization_mod.save_result(inbox, chunk_id, first)
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, person["id"], use_sample=True))
            second = {"turns": [{"speaker_key": "S1", "started": 1.2, "ended": 8.4, "quality": 0.9,
                                 "embedding": [0.9] + [0.0] * 127}],
                      "speaker_count": 1, "processing_seconds": 0.2, "outcome": "success",
                      "speech_seconds": 7.2, "coverage": 0.36, "turn_count": 1,
                      "embedding_count": 1, "cluster_count": 1, "asr_words": 2}
            diarization_mod.save_result(inbox, chunk_id, second)
            turns = inbox.speaker_turns(chunk_id)
            self.assertEqual(turns[0]["person_id"], person["id"])
            self.assertEqual(turns[0]["label_source"], "confirmed")
            with inbox.connect() as db:
                samples = db.execute("SELECT * FROM voice_samples").fetchall()
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["person_id"], person["id"])
            self.assertEqual(inbox.receipt(chunk_id)["diarization_status"], "success")

    def test_rerun_preserves_label_when_cluster_number_changes(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "c" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     20.0, str(dest), 0, "complete", "present"))
            person = inbox.create_person("Jon")
            common = {"speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 7.0, "coverage": 0.35, "turn_count": 1,
                      "embedding_count": 1, "cluster_count": 1, "asr_words": 2}
            diarization_mod.save_result(inbox, chunk_id, {
                **common, "turns": [{"speaker_key": "S1", "started": 1.0, "ended": 8.0,
                                      "quality": 1.0, "embedding": [1.0] + [0.0] * 127}]})
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, person["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {
                **common, "turns": [{"speaker_key": "S2", "started": 1.1, "ended": 8.1,
                                      "quality": 1.0, "embedding": [1.0] + [0.0] * 127}]})
            turns = inbox.speaker_turns(chunk_id)
            self.assertEqual(turns[0]["speaker_key"], "S2")
            self.assertEqual(turns[0]["person_id"], person["id"])

    def test_suggestion_and_enrollment_reasons(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Jon")
            people = inbox.people()
            self.assertFalse(people[0]["enrollment_ready"])
            self.assertIn("need_3_samples", people[0]["enrollment_reasons"])
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state,words_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "b" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     10.0, str(dest), 0, "complete", "present", "[]"))
            diarization_mod.save_result(inbox, chunk_id, {
                "turns": [{"speaker_key": "S1", "started": 0.0, "ended": 4.0, "quality": 1.0,
                           "embedding": [1.0] + [0.0] * 127}],
                "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                "speech_seconds": 4.0, "coverage": 0.4, "turn_count": 1,
                "embedding_count": 1, "cluster_count": 1, "asr_words": 3,
            })
            turns = inbox.speaker_turns(chunk_id)
            self.assertIn("no_enrolled_voiceprints", turns[0]["suggestion_reasons"])
            summary = inbox.diarization_summary(chunk_id)
            self.assertEqual(summary["outcome"], "success")
            self.assertEqual(summary["turn_count"], 1)
            payload = inbox.viewer_day("2026-09-10")
            self.assertEqual(payload["chunks"][0]["diarization"]["coverage"], 0.4)

    def test_context_window_skips_neighbor_that_exceeds_remaining(self):
        device = str(uuid.uuid4())
        oversized = chunk(device, "2026-09-10T11:57:00.000Z", duration=150, chunk_id=str(uuid.uuid4()))
        current = chunk(device, "2026-09-10T12:00:00.000Z", duration=60, chunk_id=str(uuid.uuid4()))
        selected, offset = diarization_mod.context_window([oversized, current], current)
        self.assertEqual([row["id"] for row in selected], [current["id"]])
        self.assertEqual(offset, 0.0)

    def test_empty_clipped_turns_have_zero_speaker_count(self):
        row = {"id": str(uuid.uuid4()), "duration": 10.0, "word_count": 8,
               "started": "2026-09-10T12:01:00.000Z", "device": str(uuid.uuid4())}
        earlier = {"id": str(uuid.uuid4()), "duration": 60.0, "word_count": 4,
                   "started": "2026-09-10T12:00:00.000Z", "device": row["device"]}
        result = self._run_process(
            {"segments": [{"speakerId": "S1", "startTimeSeconds": 1.0, "endTimeSeconds": 8.0, "qualityScore": 1.0}],
             "speakerCount": 3},
            [{"cluster": 0, "rho128": [1.0] + [0.0] * 127, "startTime": 1.0, "endTime": 8.0}],
            row, neighbors=[earlier, row])[0]
        self.assertEqual(result["turns"], [])
        self.assertEqual(result["speaker_count"], 0)
        self.assertEqual(result["cluster_count"], 0)
        self.assertEqual(result["context_cluster_count"], 1)

    def test_nan_segment_times_are_rejected(self):
        row = {"id": str(uuid.uuid4()), "duration": 10.0, "word_count": 4,
               "started": "2026-09-10T12:00:00.000Z", "device": str(uuid.uuid4())}
        with self.assertRaises(ValueError):
            self._run_process({"segments": [
                {"speakerId": "S1", "startTimeSeconds": float("nan"), "endTimeSeconds": 4.0, "qualityScore": 1.0}
            ], "speakerCount": 1}, [], row)

    def _labeled_inbox(self):
        scratch = tempfile.TemporaryDirectory()
        inbox = Inbox(Path(scratch.name))
        chunk_id = str(uuid.uuid4())
        dest = inbox.audio / (chunk_id + ".m4a")
        dest.write_bytes(b"audio")
        with inbox.connect() as db:
            db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (chunk_id, "d" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                 20.0, str(dest), 0, "complete", "present"))
        return scratch, inbox, chunk_id

    def test_label_migration_rejects_merge_ambiguity(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            common = {"speaker_count": 2, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 14.0, "coverage": 0.7, "turn_count": 2,
                      "embedding_count": 2, "cluster_count": 2, "asr_words": 4}
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 8.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
                {"speaker_key": "S2", "started": 8.0, "ended": 16.0, "quality": 1.0, "embedding": [0.0, 1.0] + [0.0] * 126},
            ]})
            with inbox.connect() as db:
                first, second = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=? ORDER BY started", (chunk_id,)).fetchall()
            self.assertTrue(inbox.label_turn(first[0], jon["id"], use_sample=True))
            self.assertTrue(inbox.label_turn(second[0], mia["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 16.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
            ]})
            turns = inbox.speaker_turns(chunk_id)
            people = {turn["person_id"] for turn in turns if turn["person_id"]}
            self.assertEqual(people, {jon["id"], mia["id"]})
            self.assertTrue(any(turn["started"] == 0.0 and turn["ended"] == 16.0 and not turn["person_id"] for turn in turns))

    def test_label_migration_rejects_unequal_merge_ambiguity(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            common = {"speaker_count": 2, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 10.0, "coverage": 0.5, "turn_count": 2,
                      "embedding_count": 2, "cluster_count": 2, "asr_words": 4}
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 8.0, "quality": 1.0,
                 "embedding": [1.0] + [0.0] * 127},
                {"speaker_key": "S2", "started": 8.0, "ended": 10.0, "quality": 1.0,
                 "embedding": [0.0, 1.0] + [0.0] * 126},
            ]})
            with inbox.connect() as db:
                first, second = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=? ORDER BY started",
                                           (chunk_id,)).fetchall()
            self.assertTrue(inbox.label_turn(first[0], jon["id"], use_sample=True))
            self.assertTrue(inbox.label_turn(second[0], mia["id"], use_sample=False))
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 10.0, "quality": 1.0,
                 "embedding": [1.0] + [0.0] * 127},
            ]})
            turns = inbox.speaker_turns(chunk_id)
            merged = [turn for turn in turns if turn["started"] == 0.0 and turn["ended"] == 10.0][0]
            self.assertIsNone(merged["person_id"])
            self.assertEqual({turn["person_id"] for turn in turns if turn["person_id"]},
                             {jon["id"], mia["id"]})

    def test_labeling_new_run_does_not_overwrite_preserved_same_key(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            mia = inbox.create_person("Mia")
            result = {"turns": [{"speaker_key": "S1", "started": 0.0, "ended": 8.0,
                                  "quality": 1.0, "embedding": [1.0] + [0.0] * 127}],
                      "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 8.0, "coverage": 0.4, "turn_count": 1,
                      "embedding_count": 1, "cluster_count": 1, "asr_words": 4}
            diarization_mod.save_result(inbox, chunk_id, result)
            with inbox.connect() as db:
                old_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(old_id, jon["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {**result, "turns": [] , "speaker_count": 0,
                                                            "speech_seconds": 0.0, "coverage": 0.0,
                                                            "turn_count": 0, "embedding_count": 0,
                                                            "cluster_count": 0, "outcome": "no_speech"})
            with inbox.connect() as db:
                preserved = db.execute("SELECT * FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()
                new_run = str(uuid.uuid4())
                new_turn = str(uuid.uuid4())
                db.execute("""INSERT INTO speaker_runs
                    (id,chunk_id,engine,status,speaker_count,processing_seconds,created_at)
                    VALUES (?,?,?,'complete',1,0.1,0)""", (new_run, chunk_id, "test"))
                db.execute("""INSERT INTO speaker_turns
                    (id,run_id,chunk_id,speaker_key,started,ended,quality,embedding_json)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (new_turn, new_run, chunk_id, "S1", 10.0, 15.0, 1.0,
                     json.dumps([0.0, 1.0] + [0.0] * 126)))
            self.assertTrue(inbox.label_turn(new_turn, mia["id"], use_sample=False))
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT person_id FROM speaker_turns WHERE id=?",
                                            (preserved["id"],)).fetchone()[0], jon["id"])
                self.assertEqual(db.execute("SELECT person_id FROM speaker_turns WHERE id=?",
                                            (new_turn,)).fetchone()[0], mia["id"])
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples WHERE person_id=?",
                                            (jon["id"],)).fetchone()[0], 1)

    def test_label_migration_split_maps_to_overlapping_parts(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            common = {"speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 12.0, "coverage": 0.6, "turn_count": 1,
                      "embedding_count": 1, "cluster_count": 1, "asr_words": 4}
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 12.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
            ]})
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, jon["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 6.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
                {"speaker_key": "S1", "started": 6.0, "ended": 12.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
            ]})
            turns = inbox.speaker_turns(chunk_id)
            self.assertEqual([turn["person_id"] for turn in turns], [jon["id"], jon["id"]])
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 1)

    def test_zero_turn_rerun_keeps_unmatched_confirmation(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            diarization_mod.save_result(inbox, chunk_id, {
                "turns": [{"speaker_key": "S1", "started": 1.0, "ended": 8.0, "quality": 1.0,
                           "embedding": [1.0] + [0.0] * 127}],
                "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                "speech_seconds": 7.0, "coverage": 0.35, "turn_count": 1,
                "embedding_count": 1, "cluster_count": 1, "asr_words": 2})
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, jon["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {
                "turns": [], "speaker_count": 0, "processing_seconds": 0.1, "outcome": "no_speech",
                "speech_seconds": 0.0, "coverage": 0.0, "turn_count": 0,
                "embedding_count": 0, "cluster_count": 0, "asr_words": 0})
            turns = inbox.speaker_turns(chunk_id)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["person_id"], jon["id"])
            self.assertEqual(turns[0]["started"], 1.0)
            with inbox.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], 1)

    def test_weak_overlap_keeps_old_confirmation(self):
        scratch, inbox, chunk_id = self._labeled_inbox()
        with scratch:
            jon = inbox.create_person("Jon")
            common = {"speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                      "speech_seconds": 10.0, "coverage": 0.5, "turn_count": 1,
                      "embedding_count": 1, "cluster_count": 1, "asr_words": 4}
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S1", "started": 0.0, "ended": 10.0, "quality": 1.0, "embedding": [1.0] + [0.0] * 127},
            ]})
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, jon["id"], use_sample=True))
            diarization_mod.save_result(inbox, chunk_id, {**common, "turns": [
                {"speaker_key": "S2", "started": 9.0, "ended": 19.0, "quality": 1.0, "embedding": [0.0, 1.0] + [0.0] * 126},
            ]})
            turns = inbox.speaker_turns(chunk_id)
            labeled = [turn for turn in turns if turn["person_id"] == jon["id"]]
            unlabeled = [turn for turn in turns if not turn["person_id"]]
            self.assertEqual(len(labeled), 1)
            self.assertEqual(labeled[0]["started"], 0.0)
            self.assertEqual(labeled[0]["ended"], 10.0)
            self.assertTrue(any(turn["started"] == 9.0 for turn in unlabeled))

    def test_turn_json_includes_run_and_preserved_flag(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            dest = inbox.audio / (chunk_id + ".m4a")
            dest.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute("""INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state,words_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "e" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z",
                     10.0, str(dest), 0, "complete", "present", "[]"))
            diarization_mod.save_result(inbox, chunk_id, {
                "turns": [{"speaker_key": "S1", "started": 0.0, "ended": 4.0, "quality": 1.0,
                           "embedding": [1.0] + [0.0] * 127}],
                "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                "speech_seconds": 4.0, "coverage": 0.4, "turn_count": 1,
                "embedding_count": 1, "cluster_count": 1, "asr_words": 1,
            })
            person = inbox.create_person("Jon")
            with inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            self.assertTrue(inbox.label_turn(turn_id, person["id"]))
            diarization_mod.save_result(inbox, chunk_id, {
                "turns": [{"speaker_key": "S2", "started": 5.0, "ended": 8.0, "quality": 1.0,
                           "embedding": [0.0, 1.0] + [0.0] * 126}],
                "speaker_count": 1, "processing_seconds": 0.1, "outcome": "success",
                "speech_seconds": 3.0, "coverage": 0.3, "turn_count": 1,
                "embedding_count": 1, "cluster_count": 1, "asr_words": 1,
            })
            turns = inbox.speaker_turns(chunk_id)
            self.assertTrue(any(turn.get("run_id") for turn in turns))
            self.assertTrue(any(turn.get("preserved") and turn.get("person_id") == person["id"] for turn in turns))

    def test_identity_editor_starts_empty_for_zero_or_one_person(self):
        self.assertIn('blank.textContent = "Choose a person…"', viewer_mod.JS)
        self.assertIn("save.disabled = true", viewer_mod.JS)
        self.assertIn("No people yet", viewer_mod.JS)
        self.assertNotIn("if (person.id === turn.person_id) option.selected = true", viewer_mod.JS)
        self.assertIn('Unknown · " + key', viewer_mod.JS)
        self.assertIn("Possibly ", viewer_mod.JS)
        self.assertIn(" · Confirmed", viewer_mod.JS)
        self.assertIn(" · Earlier label", viewer_mod.JS)
        self.assertIn("Mixed labels", viewer_mod.JS)
        self.assertIn("Unknown · No speaker turns available", viewer_mod.JS)
        self.assertIn("aria-expanded", viewer_mod.JS)
        self.assertIn("One more confirmed voice sample needed for ", viewer_mod.JS)
        self.assertIn("speakers-label", viewer_mod.JS)


if __name__ == "__main__":
    unittest.main()
