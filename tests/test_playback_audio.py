import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import playback_audio


class PlaybackTests(unittest.TestCase):
    def test_fixed_decoder_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.m4a"
            source.write_bytes(b"unchanged")
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"wav")
            with patch.object(playback_audio.subprocess, "run", side_effect=run):
                with playback_audio.browser_audio([{"path": str(source)}], root) as decoded:
                    self.assertTrue(decoded.is_file())
                    self.assertEqual(decoded.parent.stat().st_mode & 0o777, 0o700)
                self.assertFalse(decoded.exists())
            self.assertEqual(source.read_bytes(), b"unchanged")
            self.assertLess(commands[0].index("aac_fixed"), commands[0].index("-i"))

    def test_failure_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(playback_audio.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                with self.assertRaises(subprocess.CalledProcessError):
                    with playback_audio.browser_audio([{"path": "broken.m4a"}], folder):
                        pass
            self.assertEqual(list((Path(folder) / "playback-pcm").iterdir()), [])

    def test_orphan_sweep_and_duration_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = playback_audio.scratch_root(folder)
            orphan = root / "pcm-abandoned"
            orphan.mkdir()
            (orphan / "audio.wav").write_bytes(b"private")
            keep = root / "not-owned"
            keep.mkdir()
            playback_audio.sweep_orphans(folder)
            self.assertFalse(orphan.exists())
            self.assertTrue(keep.exists())
            with self.assertRaises(RuntimeError):
                with playback_audio.browser_audio([{"path": "test.m4a", "start_seconds": 0, "end_seconds": 181}], folder):
                    pass

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_real_aac_and_event_slices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "tone.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac", str(source)], check=True)
            before = hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns
            parts = [{"path": str(source), "start_seconds": 0.2, "end_seconds": 0.7},
                     {"path": str(source), "start_seconds": 1, "end_seconds": 1.5}]
            with playback_audio.browser_audio(parts, root) as audio:
                with wave.open(str(audio)) as wav:
                    self.assertEqual(wav.getframerate(), 48000)
                    self.assertAlmostEqual(wav.getnframes() / 48000, 1, places=2)
            self.assertEqual(before, (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns))
