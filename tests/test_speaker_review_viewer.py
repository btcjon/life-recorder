import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diarization as diarization_mod
import speaker_review
import viewer as viewer_mod
from agent_support import ViewerCase
from receiver import Inbox

TOKEN = "SECRET_REVIEW_TRANSCRIPT"


def _vec(index=0):
    vector = [0.0] * 256
    vector[index] = 1.0
    return vector


def _turn(speaker, start, end, embedding):
    return {"speaker_key": speaker, "started": start, "ended": end, "quality": 1.0, "embedding": embedding}


def _clip(inbox, started, turns, transcript=""):
    chunk_id = str(uuid.uuid4())
    dest = inbox.audio / (chunk_id + ".m4a")
    dest.write_bytes(b"audio-bytes")
    with inbox.connect() as db:
        db.execute(
            """INSERT INTO chunks
               (id,sha256,device,started,duration,path,received,status,audio_state,transcript,words_json,diarization_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chunk_id, uuid.uuid4().hex, str(uuid.uuid4()), started, 20.0, str(dest), 0,
             "complete", "present", transcript, "[]", "success"),
        )
    diarization_mod.save_result(inbox, chunk_id, {
        "turns": turns,
        "speaker_count": len({turn["speaker_key"] for turn in turns}),
        "processing_seconds": 0.1,
        "outcome": "success",
        "speech_seconds": sum(turn["ended"] - turn["started"] for turn in turns),
        "coverage": 0.5,
        "turn_count": len(turns),
        "embedding_count": len(turns),
        "cluster_count": len({turn["speaker_key"] for turn in turns}),
        "asr_words": 1,
    })
    return chunk_id


def _snapshot(inbox):
    with inbox.connect() as db:
        return {
            "turns": [
                (row["id"], row["person_id"], row["label_source"])
                for row in db.execute("SELECT id, person_id, label_source FROM speaker_turns ORDER BY id")
            ],
            "samples": db.execute("SELECT count(*) FROM voice_samples").fetchone()[0],
            "enrolled": db.execute("SELECT count(*) FROM voice_vectors WHERE enrolled=1").fetchone()[0],
            "calibration": list(db.execute("SELECT status FROM voice_calibration")),
            "jobs": db.execute("SELECT count(*) FROM voice_jobs").fetchone()[0],
        }


class ReviewViewerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.temp.name))
        self.server = viewer_mod.start_viewer(self.inbox, port=0)
        self.host, self.port = self.server.server_address
        self.token = (self.inbox.root / "viewer.token").read_text().strip()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=None):
        import http.client
        client = http.client.HTTPConnection(self.host, self.port, timeout=5)
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        raw = response.read()
        client.close()
        return response.status, raw

    def auth(self, extra=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    def test_pagination_auth_and_read_does_not_assign(self):
        ids = [
            _clip(self.inbox, "2026-09-20T12:00:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))], TOKEN),
            _clip(self.inbox, "2026-09-20T12:05:00.000Z", [_turn("S1", 1.0, 9.0, _vec(1))]),
            _clip(self.inbox, "2026-09-20T12:10:00.000Z", [_turn("S1", 2.0, 4.0, _vec(2))]),
        ]
        status, _ = self.request("GET", "/v1/speaker-review")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/v1/speaker-review?limit=0", self.auth())
        self.assertEqual(status, 400)
        before = _snapshot(self.inbox)
        status, raw = self.request("GET", "/v1/speaker-review?limit=1", self.auth())
        self.assertEqual(status, 200)
        page = json.loads(raw)
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["next_cursor"])
        self.assertNotIn(TOKEN, raw.decode())
        self.assertNotIn("embedding", raw.decode())
        self.assertEqual(before, _snapshot(self.inbox))
        walked = []
        cursor = None
        for _ in range(5):
            path = "/v1/speaker-review?limit=1" + (("&cursor=" + cursor) if cursor else "")
            status, raw = self.request("GET", path, self.auth())
            self.assertEqual(status, 200)
            current = json.loads(raw)
            walked.extend(item["chunk_id"] for item in current["items"])
            cursor = current["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(walked, ids)
        self.assertEqual(_snapshot(self.inbox), before)
        status, day = self.request("GET", "/v1/days/2026-09-20", self.auth())
        self.assertEqual(status, 200)
        self.assertNotIn(b"rank1_correct_groups", day)
        self.assertNotIn(b"speaker-review", day)

    def test_suggestions_stay_unconfirmed_until_explicit_confirm_and_opt_in(self):
        jon = self.inbox.create_person("Jon")
        _clip(self.inbox, "2026-09-20T11:00:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        _clip(self.inbox, "2026-09-20T11:05:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        with self.inbox.connect() as db:
            seeds = [row[0] for row in db.execute("SELECT id FROM speaker_turns ORDER BY started")]
        self.assertTrue(self.inbox.label_turn(seeds[0], jon["id"], use_sample=True))
        self.assertTrue(self.inbox.label_turn(seeds[1], jon["id"], use_sample=True))
        target = _clip(self.inbox, "2026-09-20T11:10:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        short = _clip(self.inbox, "2026-09-20T11:20:00.000Z", [_turn("S1", 0.0, 3.0, _vec(0))])
        with self.inbox.connect() as db:
            target_turn = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (target,)).fetchone()[0]
            short_turn = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (short,)).fetchone()[0]
        status, raw = self.request("GET", "/v1/speaker-review?limit=50", self.auth())
        page = json.loads(raw)
        item = next(row for row in page["items"] if row["turn_id"] == target_turn)
        self.assertFalse(item["confirmed"])
        self.assertNotEqual(item["label_source"], "confirmed")
        self.assertEqual(item["suggestions"][0]["name"], "Jon")
        body = json.dumps({"person_id": jon["id"]}).encode()
        before_samples = _snapshot(self.inbox)["samples"]
        status, _ = self.request("POST", "/v1/turns/" + target_turn + "/label", self.auth(), body)
        self.assertEqual(status, 200)
        with self.inbox.connect() as db:
            stored = db.execute("SELECT person_id, label_source FROM speaker_turns WHERE id=?", (target_turn,)).fetchone()
            samples = db.execute("SELECT count(*) FROM voice_samples").fetchone()[0]
        self.assertEqual(stored["label_source"], "confirmed")
        self.assertEqual(stored["person_id"], jon["id"])
        self.assertEqual(samples, before_samples)
        status, _ = self.request(
            "POST", "/v1/turns/" + short_turn + "/label", self.auth(),
            json.dumps({"person_id": jon["id"], "use_sample": True}).encode(),
        )
        self.assertEqual(status, 200)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_samples").fetchone()[0], before_samples)
            self.assertEqual(db.execute("SELECT label_source FROM speaker_turns WHERE id=?", (short_turn,)).fetchone()[0], "confirmed")
        other = _clip(self.inbox, "2026-09-20T11:30:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        with self.inbox.connect() as db:
            other_turn = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (other,)).fetchone()[0]
        status, _ = self.request(
            "POST", "/v1/turns/" + other_turn + "/label", self.auth(),
            json.dumps({"person_id": jon["id"], "use_sample": True}).encode(),
        )
        self.assertEqual(status, 200)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_samples WHERE turn_id=?", (other_turn,)).fetchone()[0], 1)
            status_row = list(db.execute("SELECT status FROM voice_calibration"))
        self.assertEqual(status_row, [])
        self.assertEqual(speaker_review.evaluate_held_out.__defaults__, None)
        with self.inbox.connect() as db:
            report = speaker_review.evaluate_held_out(db)
        self.assertFalse(report["passed"])
        self.assertEqual(report["min_score"], 0.85)

    def test_rejection_persists_without_relabeling(self):
        jon = self.inbox.create_person("Jon")
        mia = self.inbox.create_person("Mia")
        for stamp, person in (("2026-09-20T10:00:00.000Z", jon), ("2026-09-20T10:05:00.000Z", jon),
                              ("2026-09-20T10:10:00.000Z", mia), ("2026-09-20T10:15:00.000Z", mia)):
            clip = _clip(self.inbox, stamp, [_turn("S1", 0.0, 8.0, _vec(0 if person is jon else 1))])
            with self.inbox.connect() as db:
                turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (clip,)).fetchone()[0]
            self.inbox.label_turn(turn_id, person["id"], use_sample=True)
        probe = _clip(self.inbox, "2026-09-20T10:20:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        with self.inbox.connect() as db:
            turn_id = db.execute("SELECT id FROM speaker_turns WHERE chunk_id=?", (probe,)).fetchone()[0]
            db.execute(
                "UPDATE speaker_turns SET person_id=?, label_source='automatic' WHERE id=?",
                (jon["id"], turn_id),
            )
        before = _snapshot(self.inbox)
        status, _ = self.request(
            "POST", "/v1/turns/" + turn_id + "/reject", self.auth(),
            json.dumps({"person_id": jon["id"]}).encode(),
        )
        self.assertEqual(status, 200)
        after = _snapshot(self.inbox)
        self.assertEqual(before["turns"], after["turns"])
        self.assertEqual(before["samples"], after["samples"])
        self.assertEqual(before["calibration"], after["calibration"])
        self.assertEqual(before["jobs"], after["jobs"])
        for _ in range(2):
            status, raw = self.request("GET", "/v1/speaker-review?limit=50", self.auth())
            item = next(row for row in json.loads(raw)["items"] if row["turn_id"] == turn_id)
            self.assertFalse(item["confirmed"])
            self.assertNotIn(jon["id"], [suggestion["person_id"] for suggestion in item["suggestions"]])
            self.assertIsNone(item["stored_person_id"])
        with self.inbox.connect() as db:
            stored = db.execute("SELECT person_id, label_source FROM speaker_turns WHERE id=?", (turn_id,)).fetchone()
        self.assertEqual(stored["person_id"], jon["id"])
        self.assertEqual(stored["label_source"], "automatic")

    def test_layout_keyboard_and_segment_contract(self):
        script = viewer_mod.JS
        css = viewer_mod.CSS
        self.assertIn('id="tab-review"', viewer_mod.APP)
        self.assertIn('id="review-view"', viewer_mod.APP)
        self.assertIn("Unconfirmed suggestion", script)
        self.assertIn("Unconfirmed automatic guess", script)
        self.assertIn('choice.value = reviewChoices[item.turn_id] || ""', script)
        self.assertIn('blank.textContent = "Choose a name"', script)
        self.assertIn('confirm.textContent = "Confirm"', script)
        self.assertIn('reject.textContent = "Reject"', script)
        self.assertIn("Save a voice sample", script)
        self.assertIn('use_sample: useSample', script)
        self.assertIn('const audioPath = "/v1/audio/" + item.chunk_id', script)
        self.assertIn('fetch(audioPath, { headers: authHeaders()', script)
        self.assertIn("clipStartTime = startAt", script)
        self.assertIn("clipStopTime = stopAt", script)
        self.assertIn('play.setAttribute("aria-label", "Play this speaker span")', script)
        self.assertIn("Open recording", script)
        self.assertIn('fetch("/v1/speaker-review/diagnostics"', script)
        day = script[script.find("async function loadDay"):script.find("function setView")]
        self.assertNotIn("speaker-review", day)
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".review-card button, .review-card select, .review-card label { min-height: 44px;", css)
        self.assertIn("button:focus-visible, select:focus-visible, input:focus-visible", css)
        self.assertIn('type="button"', viewer_mod.APP)
        self.assertNotIn("option.selected = true", script)


class ReviewMachineTests(ViewerCase):
    def test_machine_credential_cannot_open_review_or_audio(self):
        chunk_id = _clip(self.inbox, "2026-09-20T12:00:00.000Z", [_turn("S1", 0.0, 8.0, _vec(0))])
        previous = os.environ.get("LIFE_RECORDER_AGENT_CLIENT_IDS")
        os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = self.client_id
        try:
            headers = {
                "Host": "lr.genr8ive.ai",
                "Cf-Access-Jwt-Assertion": self.machine_token(),
            }
            for path in ("/v1/speaker-review", "/v1/speaker-review/diagnostics", "/v1/audio/" + chunk_id):
                status, body = self.request("GET", path, headers)[:2]
                self.assertEqual(status, 403, path)
                self.assertNotIn(b"items", body)
                self.assertNotIn(b"audio-bytes", body)
            for path in (
                "/v1/event-edits", "/v1/chunks/" + chunk_id + "/retry",
                "/v1/turns/" + chunk_id + "/reject",
                "/v1/turns/" + chunk_id + "/label",
            ):
                status, body = self.request("POST", path, {
                    **headers, "Content-Type": "application/json",
                }, b'{}')[:2]
                self.assertEqual(status, 403, path)
                self.assertNotIn(b"rejected", body)
        finally:
            if previous is None:
                os.environ.pop("LIFE_RECORDER_AGENT_CLIENT_IDS", None)
            else:
                os.environ["LIFE_RECORDER_AGENT_CLIENT_IDS"] = previous


if __name__ == "__main__":
    unittest.main()
