import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer
from receiver import Inbox
from test_review_cards import add_chunk, vec
from test_viewer_layout import _playwright_python


class BriefTurnsTests(unittest.TestCase):
    def test_brief_turns_remain_reviewable_on_desktop_and_mobile(self):
        python = _playwright_python()
        self.assertIsNotNone(python, "Playwright is required")
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Synthetic person")
            other = inbox.create_person("Synthetic other")
            turns = [("S1", 0, 19), ("S2", 20.1, 20.4), ("S3", 21.4, 47.4),
                     ("S2", 48.6, 48.9), ("S4", 51.3, 52.3)]
            mixed = add_chunk(inbox, "2026-09-28T13:42:00.000Z", 60, [],
                              [{"speaker_key": s, "started": a, "ended": b,
                                "quality": 1, "embedding": vec(i)} for i, (s, a, b) in enumerate(turns)],
                              "Synthetic conversation with brief replies")
            tiny = add_chunk(inbox, "2026-09-28T13:50:00.000Z", 3, [],
                             [{"speaker_key": s, "started": a, "ended": b,
                               "quality": 1, "embedding": vec(i)}
                              for i, (s, a, b) in enumerate([("S1", 0, .3), ("S2", .5, .8)])],
                             "Synthetic short replies")
            with inbox.connect() as db:
                before = [tuple(r) for r in db.execute("SELECT id,person_id,label_source,started,ended FROM speaker_turns ORDER BY id")]
            server = viewer.start_viewer(inbox, port=0)
            try:
                token = (inbox.root / "viewer.token").read_text().strip()
                url = f"http://127.0.0.1:{server.server_address[1]}/#{token}"
                output = Path(__file__).resolve().parents[1] / "work/brief-turns-browser"
                result = subprocess.run([python, str(Path(__file__).with_name("brief_turns_probe.py")),
                                         url, mixed, tiny, str(output)], capture_output=True,
                                        text=True, timeout=60, cwd=scratch)
                self.assertEqual(result.returncode, 0, result.stdout[-700:] + result.stderr[-1400:])
                metrics = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(metrics["errors"], [])
                for size in ["desktop", "mobile"]:
                    self.assertEqual(metrics[size]["ordinary_pills"], 3)
                    self.assertEqual(metrics[size]["brief_pills"], 2)
                    self.assertEqual(metrics[size]["all_cards"], 5)
                    self.assertTrue(metrics[size]["editor_visible"])
                    self.assertTrue(metrics[size]["only_brief_open"])
                    self.assertFalse(metrics[size]["overflow"])
                    self.assertTrue(metrics[size]["playback_stops"])
                with inbox.connect() as db:
                    after = [tuple(r) for r in db.execute("SELECT id,person_id,label_source,started,ended FROM speaker_turns ORDER BY id")]
                self.assertEqual([r[0:1] + r[3:] for r in before], [r[0:1] + r[3:] for r in after])
                changed = [r for r, old in zip(after, before) if r[1:3] != old[1:3]]
                self.assertEqual(len(changed), 1)
                self.assertEqual(changed[0][1:3], (other["id"], "confirmed"))
                self.assertAlmostEqual(changed[0][3], 20.1)
                with inbox.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_samples").fetchone()[0], 0)
            finally:
                server.shutdown()
                server.server_close()
