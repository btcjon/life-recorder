import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import viewer as viewer_mod
from receiver import Inbox


PROBE = Path(__file__).resolve().parent / "viewer_layout_probe.py"


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


class ViewerDesktopLayoutTests(unittest.TestCase):
    def test_late_recording_keeps_header_and_detail_in_view(self):
        python = _playwright_python()
        if not python:
            self.fail("Playwright is required for the desktop layout regression test")
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            device = str(uuid.uuid4())
            start = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
            last_id = None
            for index in range(40):
                chunk_id = str(uuid.uuid4())
                dest = inbox.audio / (chunk_id + ".m4a")
                dest.write_bytes(b"audio-bytes")
                started = (start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                with inbox.connect() as db:
                    db.execute(
                        """INSERT INTO chunks (id,sha256,device,started,duration,path,received)
                           VALUES (?,?,?,?,?,?,?)""",
                        (chunk_id, "a" * 64, device, started, 60.0, str(dest), 0),
                    )
                inbox.complete(chunk_id, "Speaker turn transcript for recording %s" % (index + 1))
                last_id = chunk_id
            server = viewer_mod.start_viewer(inbox, port=0)
            self.assertIsNotNone(server)
            try:
                _host, port = server.server_address
                token = inbox.root.joinpath("viewer.token").read_text().strip()
                url = "http://127.0.0.1:%s/#%s" % (port, token)
                selector = "#recording-" + last_id
                result = subprocess.run(
                    [python, str(PROBE), url, selector],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=scratch,
                )
                if result.returncode != 0:
                    self.fail("layout probe failed: %s%s" % (result.stdout, result.stderr))
                metrics = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(metrics["pageErrors"], [])
                self.assertGreaterEqual(metrics["rowCount"], 20)
                self.assertEqual(metrics["scrollY"], 0)
                self.assertLessEqual(metrics["scrollHeight"], metrics["clientHeight"] + 2)
                self.assertIn("hidden", metrics["bodyOverflow"])
                self.assertTrue(metrics["header"]["inView"])
                self.assertGreater(metrics["header"]["visible"], 20)
                self.assertTrue(metrics["title"]["inView"])
                self.assertGreater(metrics["title"]["visible"], 10)
                self.assertGreater(metrics["paneChildCount"], 1)
                self.assertIn("Speaker turn transcript", metrics["paneText"])
                self.assertTrue(metrics["footer"]["inView"])
                self.assertGreater(metrics["list"]["scrollHeight"], metrics["list"]["clientHeight"])
                self.assertGreater(metrics["list"]["scrollTop"], 0)
                self.assertTrue(metrics["selected"]["inView"])
            finally:
                server.shutdown()
                server.server_close()

    def test_desktop_css_locks_viewport_height(self):
        desktop = viewer_mod.CSS.split("@media (min-width: 761px)", 1)[1]
        self.assertIn("html, body { height: 100%; overflow: hidden; }", desktop)
        self.assertIn("#library, #people-view { flex: 1 1 auto; min-height: 0; }", desktop)
        self.assertIn("overflow: auto; min-height: 0", viewer_mod.CSS)


if __name__ == "__main__":
    unittest.main()
