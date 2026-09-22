"""One 16 kHz mono decode per clip, shared by transcription, VAD, and diarization."""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import uuid
from pathlib import Path

FORMAT_VERSION = 1
SAMPLE_RATE = 16000


class Lease:
    def __init__(self, cache: "DecodeCache", key: str, path: Path):
        self.cache = cache
        self.key = key
        self.path = path
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.cache.release(self.key)


class DecodeCache:
    def __init__(self, work: Path):
        self.work = work
        self.root = work / "decoded"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._guard = threading.Lock()
        self._keys: dict[str, threading.Lock] = {}
        self._leases: dict[str, int] = {}
        self._pins: dict[str, int] = {}

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._keys.get(key)
            if lock is None:
                lock = threading.Lock()
                self._keys[key] = lock
            return lock

    def fingerprint(self, source: Path) -> str:
        try:
            stat = source.resolve().stat()
            raw = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{FORMAT_VERSION}|{SAMPLE_RATE}|mono"
        except OSError:
            raw = f"{source}|missing|{FORMAT_VERSION}|{SAMPLE_RATE}|mono"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def key_for(self, chunk_id: str, source: Path) -> str:
        return f"{chunk_id}-{self.fingerprint(source)}"

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.wav"

    def acquire(self, ffmpeg: str, source: Path, chunk_id: str, run=None) -> Lease:
        source = Path(source)
        key = self.key_for(chunk_id, source)
        lock = self._lock(key)
        with lock:
            path = self.path_for(key)
            if not path.is_file():
                partial = self.root / f"{key}.{uuid.uuid4().hex}.partial.wav"
                runner = run or subprocess.run
                try:
                    runner(
                        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
                         "-ar", str(SAMPLE_RATE), "-ac", "1", str(partial)],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
                    )
                    if partial.is_file():
                        os.replace(partial, path)
                    elif not path.is_file():
                        path.write_bytes(b"")
                finally:
                    partial.unlink(missing_ok=True)
            with self._guard:
                self._leases[key] = self._leases.get(key, 0) + 1
            return Lease(self, key, path)

    def release(self, key: str) -> None:
        lock = self._lock(key)
        with lock:
            with self._guard:
                count = self._leases.get(key, 0) - 1
                if count <= 0:
                    self._leases.pop(key, None)
                else:
                    self._leases[key] = count
                pinned = self._pins.get(key, 0)
                leased = self._leases.get(key, 0)
            if leased <= 0 and pinned <= 0:
                self.path_for(key).unlink(missing_ok=True)

    def pin(self, chunk_id: str, source: Path) -> None:
        source = Path(source)
        if not source.is_file():
            return
        key = self.key_for(chunk_id, source)
        with self._guard:
            self._pins[key] = self._pins.get(key, 0) + 1

    def sync_pins(self, pairs: list[tuple[str, Path]]) -> None:
        wanted: dict[str, Path] = {}
        for chunk_id, source in pairs:
            source = Path(source)
            if source.is_file():
                wanted[self.key_for(chunk_id, source)] = source
        with self._guard:
            self._pins = {key: 1 for key in wanted}
            leased = dict(self._leases)
        for path in self.root.glob("*.wav"):
            key = path.name[:-4]
            if key not in wanted and leased.get(key, 0) <= 0:
                path.unlink(missing_ok=True)

    def sweep_abandoned(self) -> None:
        for path in self.work.glob("*-diar.wav"):
            path.unlink(missing_ok=True)
        for path in self.work.glob("*-part.wav"):
            path.unlink(missing_ok=True)
        for path in self.work.glob("*-diar.json"):
            path.unlink(missing_ok=True)
        for path in self.work.glob("*-embeddings.json"):
            path.unlink(missing_ok=True)
        for path in self.root.glob("*.partial.wav"):
            path.unlink(missing_ok=True)


_caches: dict[str, DecodeCache] = {}
_caches_guard = threading.Lock()


def cache_for(work: Path) -> DecodeCache:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(work.resolve())
    with _caches_guard:
        cache = _caches.get(key)
        if cache is None:
            cache = DecodeCache(work)
            _caches[key] = cache
        return cache


def acquire(ffmpeg: str, source: Path, chunk_id: str, work: Path, run=None) -> Lease:
    return cache_for(work).acquire(ffmpeg, source, chunk_id, run=run)


def sync_pins(work: Path, pairs: list[tuple[str, Path]]) -> None:
    cache_for(work).sync_pins(pairs)


def sweep_abandoned(work: Path) -> None:
    cache_for(work).sweep_abandoned()
