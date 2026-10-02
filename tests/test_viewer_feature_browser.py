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
from test_speaker_review import _label, _open_clip, _vec
from test_viewer_layout import _playwright_python


PROBE = Path(__file__).with_name("viewer_feature_probe.py")


class ViewerFeatureBrowserTests(unittest.TestCase):
    def test_synthetic_review_and_event_edit_at_desktop_and_mobile(self):
        python = _playwright_python()
        if not python:
            self.fail("Playwright is required for the viewer feature browser probe")
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            person = inbox.create_person("Synthetic person")
            clips = [
                _open_clip(inbox, _vec(0), f"2026-09-20T12:0{index}:00.000Z",
                           transcript=f"Synthetic words in clip {index}")
                for index in range(3)
            ]
            for clip in clips[:2]:
                _label(inbox, clip, person["id"])
            server = viewer.start_viewer(inbox, port=0)
            try:
                token = (inbox.root / "viewer.token").read_text().strip()
                url = f"http://127.0.0.1:{server.server_address[1]}/#{token}"
                output = Path("/tmp/life-recorder-feature-probe-20260924")
                result = subprocess.run(
                    [python, str(PROBE), url, str(output)],
                    capture_output=True, text=True, timeout=60, cwd=scratch,
                )
                if result.returncode:
                    self.fail(f"feature probe failed: {result.stdout[-800:]} {result.stderr[-1200:]}")
                metrics = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(metrics["page_errors"], [])
                self.assertTrue(metrics["health_rendered"])
                self.assertTrue(metrics["no_preselected_name"])
                self.assertTrue(metrics["confirmed_via_ui"])
                self.assertTrue(metrics["event_edited_via_ui"])
                for size in ("desktop", "mobile"):
                    frame = metrics[size]
                    self.assertTrue(frame["reviewVisible"])
                    self.assertGreaterEqual(frame["cardCount"], 1)
                    self.assertGreater(frame["cardWidth"], 0)
                    self.assertLessEqual(frame["scrollWidth"], frame["width"] + 2)
                self.assertGreaterEqual(metrics["mobile"]["playHeight"], 44)
                with inbox.connect() as db:
                    label = db.execute(
                        "SELECT label_source FROM speaker_turns WHERE chunk_id=?", (clips[2],)
                    ).fetchone()[0]
                    self.assertEqual(label, "confirmed")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM voice_samples").fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT title FROM event_edits").fetchone()[0], "Synthetic meeting")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
