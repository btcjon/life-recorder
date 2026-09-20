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


if __name__ == "__main__":
    unittest.main()
