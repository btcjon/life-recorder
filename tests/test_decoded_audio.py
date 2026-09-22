import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import asr as asr_mod
import decoded_audio
import diarization as diarization_mod
import vad as vad_mod
from receiver import Inbox


class SharedDecodeTests(unittest.TestCase):
    def test_concurrent_callers_decode_once_and_failure_cleans_up(self):
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            source = work / "clip.m4a"
            source.write_bytes(b"audio")
            calls = []
            started = threading.Barrier(2)

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                Path(argv[-1]).write_bytes(b"RIFF")
                return mock.Mock(returncode=0)

            cache = decoded_audio.DecodeCache(work)

            def borrow():
                lease = cache.acquire("ffmpeg", source, "clip", run=fake_run)
                started.wait(2)
                lease.release()

            threads = [threading.Thread(target=borrow) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(cache.root.glob("*.wav")), [])

            def fail_run(argv, **kwargs):
                raise subprocess.CalledProcessError(1, argv)

            with self.assertRaises(subprocess.CalledProcessError):
                cache.acquire("ffmpeg", source, "clip", run=fail_run)
            self.assertEqual(list(cache.root.glob("*.wav")), [])
            self.assertEqual(list(cache.root.glob("*.partial.wav")), [])

    def test_pin_keeps_one_wav_across_asr_vad_and_diarization(self):
        with tempfile.TemporaryDirectory() as scratch:
            inbox = Inbox(Path(scratch))
            chunk_id = str(uuid.uuid4())
            audio = inbox.audio / (chunk_id + ".m4a")
            audio.write_bytes(b"audio")
            with inbox.connect() as db:
                db.execute(
                    """INSERT INTO chunks
                       (id,sha256,device,started,duration,path,received,status,audio_state,word_count,
                        diarization_status,vad_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, "a" * 64, str(uuid.uuid4()), "2026-09-10T12:00:00.000Z", 10.0,
                     str(audio), 0, "pending", "present", 2, "pending", "pending"),
                )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv[0])
                if argv[0] == "ffmpeg":
                    Path(argv[-1]).write_bytes(b"RIFF")
                elif "transcribe" in argv:
                    Path(argv[argv.index("--output-json") + 1]).write_text(
                        '{"text":"hello","words":[{"word":"hello","startTime":0.1,"endTime":0.4}]}'
                    )
                elif "vad-analyze" in argv:
                    Path(argv[argv.index("--output-json") + 1]).write_text('{"segments":[]}')
                elif "process" in argv:
                    Path(argv[argv.index("--output") + 1]).write_text(
                        '{"segments":[],"speakerCount":0}'
                    )
                    Path(argv[argv.index("--export-embeddings") + 1]).write_text("[]")
                return mock.Mock(returncode=0, stderr=b"")

            work = inbox.root / "processing"
            row = {"id": chunk_id, "path": str(audio), "duration": 10.0, "word_count": 2}
            config = asr_mod.AsrConfig(
                engine="parakeet", ffmpeg="ffmpeg",
                parakeet_cli=Path("/bin/fluidaudiocli"),
                parakeet_model_dir=Path("/models/parakeet"),
            )
            with mock.patch("asr.subprocess.run", side_effect=fake_run), \
                 mock.patch("vad.subprocess.run", side_effect=fake_run), \
                 mock.patch("diarization.subprocess.run", side_effect=fake_run):
                diarization_mod.refresh_decoded_pins(inbox)
                asr_mod.transcribe_chunk(row, config, work, lambda text: text)
                vad_mod.process_chunk(row, Path("/bin/fluidaudiocli"), "ffmpeg", work)
                diarization_mod.process_chunk(
                    row, Path("/bin/fluidaudiocli"), "ffmpeg", work, neighbors=[row],
                )
            self.assertEqual(calls.count("ffmpeg"), 1)
            self.assertTrue(list((work / "decoded").glob("*.wav")))
            with inbox.connect() as db:
                db.execute(
                    "UPDATE chunks SET status='complete', vad_status='complete', diarization_status='success' WHERE id=?",
                    (chunk_id,),
                )
            diarization_mod.refresh_decoded_pins(inbox)
            self.assertEqual(list((work / "decoded").glob("*.wav")), [])


if __name__ == "__main__":
    unittest.main()
