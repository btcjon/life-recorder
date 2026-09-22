import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import diarization as diarization_mod
import viewer as viewer_mod
from receiver import Inbox


PROBE = Path(__file__).resolve().parent / "review_cards_probe.py"
SHOT_DIR = Path("/Users/jonbennett/Library/CloudStorage/Dropbox/Projects/Codex/2026-09-21/life-recorder-review-cards/work")


def _playwright_python():
    candidates = [
        sys.executable,
        "/opt/homebrew/Caskroom/miniconda/base/bin/python",
        shutil.which("python3.12"),
        shutil.which("python3"),
    ]
    seen = set()
    for exe in candidates:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        try:
            result = subprocess.run(
                [exe, "-c", "from playwright.sync_api import sync_playwright"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return exe
    return None


def write_tone(path: Path, seconds: float = 60.0, rate: int = 8000):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x10" * frames)


def add_chunk(inbox: Inbox, started: str, duration: float, words, turns, transcript: str):
    chunk_id = str(uuid.uuid4())
    dest = inbox.audio / (chunk_id + ".wav")
    write_tone(dest, duration)
    with inbox.connect() as db:
        db.execute(
            """INSERT INTO chunks (id,sha256,device,started,duration,path,received,status,audio_state,words_json,transcript)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (chunk_id, "a" * 64, str(uuid.uuid4()), started, duration, str(dest), 0,
             "complete", "present", json.dumps(words), transcript),
        )
    diarization_mod.save_result(inbox, chunk_id, {
        "turns": turns,
        "speaker_count": len({turn["speaker_key"] for turn in turns}),
        "processing_seconds": 0.1,
        "outcome": "success",
        "speech_seconds": sum(turn["ended"] - turn["started"] for turn in turns),
        "coverage": 0.8,
        "turn_count": len(turns),
        "embedding_count": len(turns),
        "cluster_count": len({turn["speaker_key"] for turn in turns}),
        "asr_words": len(words),
    })
    return chunk_id


def vec(index: int):
    values = [0.0] * 256
    values[index] = 1.0
    return values


class ReviewCardTests(unittest.TestCase):
    def test_screenshot_fixture_and_browser_interaction(self):
        python = _playwright_python()
        if not python:
            self.fail("Playwright is required for the review-card interaction test")
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        desktop_shot = SHOT_DIR / "review-card-desktop.png"
        mobile_shot = SHOT_DIR / "review-card-mobile.png"
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            fixture_id = add_chunk(
                inbox,
                "2026-09-10T16:00:00.000Z",
                60.0,
                [
                    {"word": "second", "startTime": 5.0, "endTime": 6.0},
                    {"word": "speaker", "startTime": 6.2, "endTime": 7.0},
                    {"word": "keeps", "startTime": 20.0, "endTime": 20.6},
                    {"word": "talking", "startTime": 30.0, "endTime": 31.0},
                    {"word": "first", "startTime": 43.0, "endTime": 43.6},
                    {"word": "speaker", "startTime": 50.0, "endTime": 50.8},
                    {"word": "closes", "startTime": 58.0, "endTime": 59.0},
                ],
                [
                    {"speaker_key": "S2", "started": 4.0, "ended": 20.0, "quality": 1.0, "embedding": vec(1)},
                    {"speaker_key": "S2", "started": 22.0, "ended": 39.6, "quality": 1.0, "embedding": vec(1)},
                    {"speaker_key": "S1", "started": 42.7, "ended": 51.0, "quality": 1.0, "embedding": vec(0)},
                    {"speaker_key": "S1", "started": 53.0, "ended": 60.0, "quality": 1.0, "embedding": vec(0)},
                ],
                "second speaker keeps talking first speaker closes",
            )
            named_id = add_chunk(
                inbox,
                "2026-09-10T16:04:00.000Z",
                20.0,
                [
                    {"word": "duty", "startTime": 4.5, "endTime": 5.0},
                    {"word": "trash", "startTime": 13.8, "endTime": 14.1},
                ],
                [
                    {"speaker_key": "S1", "started": 4.3, "ended": 6.5, "quality": 1.0, "embedding": vec(0)},
                    {"speaker_key": "S1", "started": 10.6, "ended": 11.0, "quality": 1.0, "embedding": vec(0)},
                    {"speaker_key": "S1", "started": 13.7, "ended": 14.2, "quality": 1.0, "embedding": vec(0)},
                ],
                "duty trash",
            )
            person = inbox.create_person("John Phelan")
            with inbox.connect() as db:
                rows = db.execute(
                    "SELECT id FROM speaker_turns WHERE chunk_id=? ORDER BY started",
                    (named_id,),
                ).fetchall()
                db.execute(
                    "UPDATE speaker_turns SET person_id=?, label_source='confirmed' WHERE id IN (?,?)",
                    (person["id"], rows[0][0], rows[1][0]),
                )
                still_open = db.execute(
                    "SELECT person_id FROM speaker_turns WHERE id=?",
                    (rows[2][0],),
                ).fetchone()[0]
            self.assertIsNone(still_open)
            aba_id = add_chunk(
                inbox,
                "2026-09-10T16:02:00.000Z",
                12.0,
                [
                    {"word": "hello", "startTime": 1.0, "endTime": 1.4},
                    {"word": "there", "startTime": 5.0, "endTime": 5.5},
                    {"word": "again", "startTime": 9.0, "endTime": 9.5},
                ],
                [
                    {"speaker_key": "S1", "started": 0.0, "ended": 4.0, "quality": 1.0, "embedding": vec(0)},
                    {"speaker_key": "S2", "started": 4.0, "ended": 8.0, "quality": 1.0, "embedding": vec(1)},
                    {"speaker_key": "S1", "started": 8.0, "ended": 12.0, "quality": 1.0, "embedding": vec(0)},
                ],
                "hello there again",
            )
            server = viewer_mod.start_viewer(inbox, port=0)
            self.assertIsNotNone(server)
            try:
                _host, port = server.server_address
                token = inbox.root.joinpath("viewer.token").read_text().strip()
                url = "http://127.0.0.1:%s/#%s" % (port, token)
                result = subprocess.run(
                    [python, str(PROBE), url, "#recording-" + fixture_id,
                     str(desktop_shot), str(mobile_shot), "#recording-" + aba_id,
                     "#recording-" + named_id],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    cwd=scratch,
                )
                if result.returncode != 0:
                    self.fail("review card probe failed: %s%s" % (result.stdout, result.stderr))
                metrics = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(metrics["pageErrors"], [])
                desktop = metrics["desktopCards"]
                expected = [
                    ("S2", 4.0, 39.6, ("second", "talking")),
                    ("S1", 42.7, 60.0, ("first", "closes")),
                ]
                self.assertEqual(len(desktop), 2)
                self.assertEqual(metrics["transcriptPills"], [card["pill"] for card in desktop])
                self.assertGreaterEqual(metrics["pieceTime"], 3.9)
                self.assertLess(metrics["pieceTime"], 20.0)
                for card, (key, start, end, words) in zip(desktop, expected):
                    self.assertAlmostEqual(float(card["start"]), start)
                    self.assertAlmostEqual(float(card["end"]), end)
                    self.assertAlmostEqual(card["progressMax"], end - start, places=2)
                    self.assertIn("Unknown · %s · %s–%ss" % (key, start, end), card["pill"])
                    for word in words:
                        self.assertIn(word, card["text"])
                    self.assertEqual(card["play"], "Play")
                    self.assertEqual(card["replay"], "Replay")
                    self.assertTrue(card["hasPicker"])
                    self.assertRegex(card["clock"], r"0:00 / 0:\d\d")
                self.assertLess(float(desktop[0]["end"]), float(desktop[1]["start"]))
                self.assertTrue(metrics["afterFirstPlay"][0]["active"])
                self.assertEqual(metrics["afterFirstPlay"][0]["play"], "Pause")
                self.assertFalse(metrics["afterSecondPlay"][0]["active"])
                self.assertTrue(metrics["afterSecondPlay"][1]["active"])
                self.assertEqual(metrics["afterSecondPlay"][1]["play"], "Pause")
                self.assertFalse(any(card["active"] for card in metrics["afterFull"]))
                aba = metrics["abaCards"]
                self.assertEqual(len(aba), 3)
                self.assertEqual([card["pill"].split(" · ")[0] for card in aba],
                                 ["Unknown", "Unknown", "Unknown"])
                self.assertAlmostEqual(float(aba[0]["start"]), 0.0)
                self.assertAlmostEqual(float(aba[1]["start"]), 4.0)
                self.assertAlmostEqual(float(aba[2]["start"]), 8.0)
                mobile = metrics["mobileCards"]
                self.assertEqual(len(mobile), 2)
                named = metrics["namedPills"]
                self.assertEqual(len(named), 1)
                self.assertIn("John Phelan", named[0])
                self.assertNotIn("✓", named[0])
                self.assertIn("4.3–14.2s", named[0])
                self.assertNotIn("Unknown", named[0])
                self.assertEqual(metrics["labelPosts"], 1)
                self.assertGreaterEqual(mobile[0]["playHeight"], 44)
                self.assertGreaterEqual(mobile[0]["replayHeight"], 44)
                self.assertTrue(desktop_shot.is_file())
                self.assertTrue(mobile_shot.is_file())
            finally:
                server.shutdown()
                server.server_close()

    def test_css_keeps_mobile_playback_controls_large(self):
        self.assertIn(".play-toggle", viewer_mod.CSS)
        self.assertIn(".group-progress", viewer_mod.CSS)
        self.assertIn(".play-toggle, .replay { min-height: 44px; font-size: 16px; }", viewer_mod.CSS)


if __name__ == "__main__":
    unittest.main()
