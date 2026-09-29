"""Disposable, bounded browser PCM decoding; never changes recognition sources."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

_slots = threading.BoundedSemaphore(2)


def scratch_root(root):
    folder = Path(root) / "playback-pcm"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def sweep_orphans(root):
    folder = scratch_root(root)
    for child in folder.iterdir():
        if child.name.startswith("pcm-") and child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)


@contextmanager
def browser_audio(parts, root):
    if not _slots.acquire(timeout=10):
        raise RuntimeError("Playback decoder busy")
    try:
        if not parts or len(parts) > 32:
            raise RuntimeError("Playback span exceeds limit")
        duration = sum(float(p["end_seconds"]) - float(p["start_seconds"])
                       for p in parts if "start_seconds" in p.keys())
        if duration > 180.1:
            raise RuntimeError("Playback duration exceeds limit")
        deadline = time.monotonic() + 30
        def run(command):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Playback decoder timed out")
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=remaining)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Playback decoder unavailable")
        with tempfile.TemporaryDirectory(prefix="pcm-", dir=scratch_root(root)) as scratch:
            slices = []
            for index, part in enumerate(parts):
                source = Path(part["path"])
                dest = Path(scratch) / f"{index}.wav"
                command = [ffmpeg, "-nostdin", "-v", "error", "-y"]
                if source.suffix.lower() in (".m4a", ".aac", ".mp4"):
                    command += ["-c:a", "aac_fixed"]
                command += ["-i", str(source)]
                if "start_seconds" in part.keys():
                    command += ["-ss", str(part["start_seconds"]), "-t",
                                str(float(part["end_seconds"]) - float(part["start_seconds"]))]
                else:
                    command += ["-t", "180"]
                command += ["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)]
                run(command)
                slices.append(dest)
            if not slices:
                raise RuntimeError("Playback source unavailable")
            if len(slices) == 1:
                yield slices[0]
            else:
                listing = Path(scratch) / "concat.txt"
                listing.write_text("".join(f"file '{p.name}'\n" for p in slices))
                dest = Path(scratch) / "complete.wav"
                run([ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "concat",
                                "-safe", "1", "-i", str(listing), "-c:a", "pcm_s16le", str(dest)],
                    )
                yield dest
    finally:
        _slots.release()
