"""Strict Silero VAD adapter and speech-event assembly. Originals unchanged."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meetings import chunk_span, format_utc, parse_utc

PAD_SECONDS = 0.400
MIN_SPEECH_SECONDS = 0.250
GAP_SECONDS = 0.800
CROSS_CHUNK_SILENCE = 8.0
MAX_EVENT_SECONDS = 180.0
ALGORITHM_VERSION = "vad-events-v1"
ENHANCE_VERSION = "deepfilternet3-48k-v1"
MAX_ENHANCE_ATTEMPTS = 8

_event_locks = {}
_event_locks_guard = threading.Lock()


class VadError(ValueError):
    pass


def event_lock(event_id: str) -> threading.Lock:
    with _event_locks_guard:
        lock = _event_locks.get(event_id)
        if lock is None:
            lock = threading.Lock()
            _event_locks[event_id] = lock
        return lock


def event_fingerprint(parts, algorithm_version: str = ALGORITHM_VERSION) -> str:
    payload = {
        "algorithm_version": algorithm_version,
        "parts": [
            {
                "chunk_id": part["chunk_id"],
                "start": round(float(part["start"]), 3),
                "end": round(float(part["end"]), 3),
            }
            for part in parts
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def event_asset_paths(root: Path, event_id: str) -> dict[str, Path]:
    folder = root / "events"
    return {
        "original": folder / f"{event_id}.wav",
        "enhanced": folder / f"{event_id}.enhanced.wav",
        "tmp": folder / f"{event_id}.tmp.wav",
    }


def delete_event_assets(root: Path, event_id: str, extra=None) -> int:
    removed = 0
    paths = list(event_asset_paths(root, event_id).values())
    if extra:
        paths.append(Path(extra))
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            removed += path.stat().st_size
            path.unlink(missing_ok=True)
    return removed


def derived_bytes_for(root: Path, event_id: str, extra=None) -> int:
    total = 0
    paths = list(event_asset_paths(root, event_id).values())
    if extra:
        paths.append(Path(extra))
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen or path.name.endswith(".tmp.wav") or not path.is_file():
            continue
        seen.add(key)
        total += path.stat().st_size
    return total


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_vad_output(path: Path, duration: float) -> list[dict]:
    if not path.is_file():
        raise VadError("Missing VAD output")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise VadError("Malformed VAD output") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise VadError("Malformed VAD output")
    if payload.get("backend") not in (None, "silero"):
        raise VadError("Unsupported VAD backend")
    spans = []
    for item in payload["segments"]:
        if not isinstance(item, dict):
            raise VadError("Malformed VAD segment")
        start = _finite(item.get("startTime"))
        end = _finite(item.get("endTime"))
        if start is None or end is None or end <= start:
            raise VadError("Invalid VAD timing")
        spans.append({"start": max(0.0, start), "end": min(float(duration), end)})
    return normalize_spans(spans, duration)


def normalize_spans(spans, duration: float, pad: float = PAD_SECONDS,
                    min_speech: float = MIN_SPEECH_SECONDS, gap: float = GAP_SECONDS) -> list[dict]:
    duration = float(duration)
    cleaned = []
    for span in spans:
        start = _finite(span.get("start") if isinstance(span, dict) else span[0])
        end = _finite(span.get("end") if isinstance(span, dict) else span[1])
        if start is None or end is None:
            raise VadError("Invalid VAD timing")
        start = max(0.0, start - pad)
        end = min(duration, end + pad)
        if end - start >= min_speech:
            cleaned.append([start, end])
    cleaned.sort()
    merged = []
    for start, end in cleaned:
        if not merged or start - merged[-1][1] > gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [{"start": round(start, 3), "end": round(end, 3)} for start, end in merged if end - start >= min_speech]


def process_chunk(row, cli: Path, ffmpeg: str, work: Path) -> list[dict]:
    token = uuid.uuid4().hex
    wav = work / f"{row['id']}-{token}-vad.wav"
    result = work / f"{row['id']}-{token}-vad.json"
    duration = float(row["duration"] or 0)
    try:
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", row["path"],
                        "-ar", "16000", "-ac", "1", str(wav)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        completed = subprocess.run(
            [str(cli), "vad-analyze", str(wav),
             "--min-speech-ms", "0",
             "--min-silence-ms", str(int(GAP_SECONDS * 1000)),
             "--pad-ms", "0",
             "--output-json", str(result)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if completed.returncode:
            raise VadError("VAD failed")
        if not result.is_file() or result.stat().st_size == 0:
            raise VadError("Missing VAD output")
        return parse_vad_output(result, duration)
    finally:
        wav.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _turn_get(turn, key, default=None):
    if hasattr(turn, "keys") and key in turn.keys():
        return turn[key]
    if isinstance(turn, dict):
        return turn.get(key, default)
    return default


def speaker_keys_for_span(turns, start: float, end: float) -> set[str]:
    keys = set()
    for turn in turns or []:
        turn_start = float(turn["started"])
        turn_end = float(turn["ended"])
        if _overlap(start, end, turn_start, turn_end) <= 0:
            continue
        person_id = _turn_get(turn, "person_id")
        if person_id:
            keys.add("person:" + person_id)
            continue
        run_id = _turn_get(turn, "run_id") or _turn_get(turn, "chunk_id") or "unknown"
        keys.add("anon:" + str(run_id) + ":" + str(turn["speaker_key"]))
    return keys


def _manual_bounds(intervals, device_id: str) -> list[datetime]:
    bounds = []
    for row in intervals or []:
        if row["source"] != "manual" or row["device_id"] != device_id:
            continue
        bounds.append(parse_utc(row["started_at"]))
        if row["ended_at"]:
            bounds.append(parse_utc(row["ended_at"]))
    return sorted(bounds)


def split_span_at_manual_bounds(start: datetime, end: datetime, bounds) -> list[tuple[datetime, datetime]]:
    pieces = []
    cursor = start
    for bound in bounds:
        if cursor < bound < end:
            pieces.append((cursor, bound))
            cursor = bound
    if end > cursor:
        pieces.append((cursor, end))
    return pieces or [(start, end)]


def assemble_events(chunks, spans_by_chunk, turns_by_chunk, intervals=None,
                    max_event: float = MAX_EVENT_SECONDS,
                    cross_silence: float = CROSS_CHUNK_SILENCE) -> list[dict]:
    """Build device-local events from original-relative spans."""
    items = []
    for chunk in chunks:
        started, _ended = chunk_span(chunk)
        bounds = _manual_bounds(intervals, chunk["device"])
        for span in spans_by_chunk.get(chunk["id"], []):
            abs_start = started + timedelta(seconds=float(span["start"]))
            abs_end = started + timedelta(seconds=float(span["end"]))
            for piece_start, piece_end in split_span_at_manual_bounds(abs_start, abs_end, bounds):
                rel_start = max(0.0, (piece_start - started).total_seconds())
                rel_end = max(rel_start, (piece_end - started).total_seconds())
                if rel_end - rel_start < MIN_SPEECH_SECONDS:
                    continue
                items.append({
                    "chunk": chunk,
                    "start": piece_start,
                    "end": piece_end,
                    "rel_start": rel_start,
                    "rel_end": rel_end,
                    "keys": speaker_keys_for_span(turns_by_chunk.get(chunk["id"], []),
                                                  rel_start, rel_end),
                })
    items.sort(key=lambda item: (item["chunk"]["device"], item["start"], item["chunk"]["id"]))
    events = []
    current = None
    for item in items:
        device = item["chunk"]["device"]
        bounds = _manual_bounds(intervals, device)
        if current and current["device"] == device:
            gap = (item["start"] - current["end"]).total_seconds()
            overlap = (current["end"] - item["start"]).total_seconds()
            duration = (item["end"] - current["start"]).total_seconds()
            anonymous = {key for key in current["keys"] | item["keys"] if key.startswith("anon:")}
            cross_anon = (item["chunk"]["id"] != current["chunks"][-1]["chunk_id"]
                          and anonymous and current["keys"] and item["keys"]
                          and current["keys"].isdisjoint(item["keys"]))
            crossed_manual = any(current["end"] <= bound <= item["start"] for bound in bounds)
            if (gap <= cross_silence and overlap < 0.05 and duration <= max_event
                    and not cross_anon and not crossed_manual):
                current["end"] = max(current["end"], item["end"])
                current["keys"] |= item["keys"]
                current["chunks"].append({
                    "chunk_id": item["chunk"]["id"],
                    "start": item["rel_start"],
                    "end": item["rel_end"],
                })
                continue
        if current:
            events.append(current)
        current = {
            "device": device,
            "start": item["start"],
            "end": item["end"],
            "keys": set(item["keys"]),
            "chunks": [{
                "chunk_id": item["chunk"]["id"],
                "start": item["rel_start"],
                "end": item["rel_end"],
            }],
        }
    if current:
        events.append(current)
    output = []
    for event in events:
        duration = (event["end"] - event["start"]).total_seconds()
        playable_duration = sum(max(0.0, part["end"] - part["start"]) for part in event["chunks"])
        output.append({
            "device_id": event["device"],
            "started": format_utc(event["start"]),
            "ended": format_utc(event["end"]),
            "duration": round(duration, 3),
            "playable_duration": round(playable_duration, 3),
            "chunks": event["chunks"],
            "source": "vad",
            "algorithm_version": ALGORITHM_VERSION,
            "content_fingerprint": event_fingerprint(event["chunks"]),
        })
    return output


def save_chunk_spans(inbox, chunk_id: str, spans: list[dict]) -> None:
    now = time.time()
    with inbox.lock, inbox.connect() as db:
        db.execute("DELETE FROM chunk_speech_spans WHERE chunk_id=?", (chunk_id,))
        for span in spans:
            db.execute("""INSERT INTO chunk_speech_spans (id,chunk_id,start_seconds,end_seconds,source,created_at)
                VALUES (?,?,?,?,?,?)""",
                (str(uuid.uuid4()), chunk_id, span["start"], span["end"], "vad", now))
        db.execute("""UPDATE chunks SET vad_status='complete',vad_error=NULL,vad_retry_at=0 WHERE id=?""",
                   (chunk_id,))


def rebuild_events(inbox) -> None:
    with inbox.connect() as db:
        chunks = list(db.execute("SELECT * FROM chunks WHERE status='complete' ORDER BY started,id"))
        spans = list(db.execute("SELECT * FROM chunk_speech_spans ORDER BY chunk_id,start_seconds"))
        turns = list(db.execute("SELECT * FROM speaker_turns"))
        intervals = list(db.execute("SELECT * FROM intervals"))
    spans_by_chunk = {}
    for row in spans:
        spans_by_chunk.setdefault(row["chunk_id"], []).append(
            {"start": row["start_seconds"], "end": row["end_seconds"]})
    turns_by_chunk = {}
    for row in turns:
        turns_by_chunk.setdefault(row["chunk_id"], []).append(row)
    events = assemble_events(chunks, spans_by_chunk, turns_by_chunk, intervals)
    now = time.time()
    with inbox.lock, inbox.connect() as db:
        existing = {row["id"]: row for row in db.execute("SELECT * FROM speech_events")}
        used_ids = set()
        db.execute("DELETE FROM speech_event_chunks")
        db.execute("DELETE FROM speech_events")
        for event in events:
            match = next((row for row in existing.values()
                          if row["id"] not in used_ids
                          and row["content_fingerprint"] == event["content_fingerprint"]), None)
            event_id = match["id"] if match else str(uuid.uuid4())
            used_ids.add(event_id)
            same = bool(match)
            db.execute("""INSERT INTO speech_events
                (id,device_id,started,ended,duration,playable_duration,source,algorithm_version,status,
                 enhancement_status,enhancement_path,enhancement_version,derived_bytes,created_at,
                 content_fingerprint,enhancement_attempts,enhancement_retry_at,enhancement_error)
                VALUES (?,?,?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?)""",
                (event_id, event["device_id"], event["started"], event["ended"], event["duration"],
                 event["playable_duration"],
                 event["source"], event["algorithm_version"],
                 (match["enhancement_status"] if same else "pending"),
                 (match["enhancement_path"] if same else None),
                 (match["enhancement_version"] if same else None),
                 (match["derived_bytes"] if same else 0), now, event["content_fingerprint"],
                 (match["enhancement_attempts"] if same else 0),
                 (match["enhancement_retry_at"] if same else 0),
                 (match["enhancement_error"] if same else None)))
            for part in event["chunks"]:
                db.execute("""INSERT INTO speech_event_chunks
                    (event_id,chunk_id,start_seconds,end_seconds) VALUES (?,?,?,?)""",
                    (event_id, part["chunk_id"], part["start"], part["end"]))
        for row in existing.values():
            if row["id"] not in used_ids:
                delete_event_assets(inbox.root, row["id"], row["enhancement_path"])


def extract_event_audio(inbox, event_id: str, ffmpeg: str, dest: Path) -> Path | None:
    with event_lock(event_id):
        with inbox.connect() as db:
            parts = list(db.execute("""SELECT c.path, c.audio_state, e.start_seconds, e.end_seconds, e.chunk_id
                FROM speech_event_chunks e JOIN chunks c ON c.id=e.chunk_id
                WHERE e.event_id=? ORDER BY c.started,e.start_seconds""", (event_id,)))
            row = db.execute("SELECT content_fingerprint FROM speech_events WHERE id=?", (event_id,)).fetchone()
        if not parts or any(part["audio_state"] != "present" or not Path(part["path"]).is_file() for part in parts):
            return None
        fingerprint = row["content_fingerprint"] if row else event_fingerprint([
            {"chunk_id": part["chunk_id"], "start": part["start_seconds"], "end": part["end_seconds"]}
            for part in parts
        ])
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        work = tempfile.mkdtemp(prefix=f"event-{event_id}-", dir=dest.parent)
        slices = []
        listing = Path(work) / "concat.txt"
        tmp = Path(work) / "out.wav"
        try:
            for index, part in enumerate(parts):
                slc = Path(work) / f"{index}.wav"
                subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                                "-ss", str(part["start_seconds"]), "-t",
                                str(float(part["end_seconds"]) - float(part["start_seconds"])),
                                "-i", str(part["path"]), "-ar", "48000", "-ac", "1", str(slc)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                slices.append(slc)
            listing.write_text("".join(f"file '{path}'\n" for path in slices))
            subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-ar", "48000", "-ac", "1", str(tmp)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            with inbox.lock, inbox.connect() as db:
                current = db.execute("SELECT content_fingerprint FROM speech_events WHERE id=?",
                                     (event_id,)).fetchone()
                current_parts = list(db.execute("""SELECT c.path,c.audio_state FROM speech_event_chunks e
                    JOIN chunks c ON c.id=e.chunk_id WHERE e.event_id=?""", (event_id,)))
                sources_ok = bool(current_parts) and all(
                    part["audio_state"] == "present" and Path(part["path"]).is_file()
                    for part in current_parts
                )
                if not current or current["content_fingerprint"] != fingerprint or not sources_ok:
                    return None
                os.replace(tmp, dest)
                db.execute("UPDATE speech_events SET derived_bytes=? WHERE id=?",
                           (derived_bytes_for(inbox.root, event_id), event_id))
            return dest
        finally:
            shutil.rmtree(work, ignore_errors=True)


def enhance_event(inbox, event_id: str, ffmpeg: str, enhance_cli: Path | None) -> None:
    assets = event_asset_paths(inbox.root, event_id)
    original = assets["original"]
    enhanced = assets["enhanced"]
    try:
        extracted = extract_event_audio(inbox, event_id, ffmpeg, original)
        if extracted is None:
            with inbox.connect() as db:
                db.execute("""UPDATE speech_events SET enhancement_status='unavailable',
                    enhancement_error='missing_source' WHERE id=?""", (event_id,))
            return
        if not enhance_cli or not Path(enhance_cli).is_file():
            with inbox.connect() as db:
                db.execute("""UPDATE speech_events SET enhancement_status='unavailable',
                    enhancement_path=NULL, enhancement_error='cli_unavailable', enhancement_retry_at=0 WHERE id=?""",
                           (event_id,))
            return
        with event_lock(event_id):
            with tempfile.TemporaryDirectory(prefix="enhance-", dir=original.parent) as scratch:
                completed = subprocess.run(
                    [str(enhance_cli), "--compensate-delay", "--output-dir", scratch, str(original)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
                output = Path(scratch) / original.name
                if completed.returncode or not output.is_file():
                    raise VadError("enhance failed")
                with wave.open(str(original), "rb") as source:
                    source_duration = source.getnframes() / source.getframerate()
                with wave.open(str(output), "rb") as result:
                    result_duration = result.getnframes() / result.getframerate()
                if abs(source_duration - result_duration) > max(0.10, source_duration * 0.02):
                    raise VadError("enhance duration mismatch")
                tmp = Path(scratch) / "publish.wav"
                shutil.copyfile(output, tmp)
                with inbox.lock, inbox.connect() as db:
                    current_parts = list(db.execute("""SELECT c.path,c.audio_state FROM speech_event_chunks e
                        JOIN chunks c ON c.id=e.chunk_id WHERE e.event_id=?""", (event_id,)))
                    sources_ok = bool(current_parts) and all(
                        part["audio_state"] == "present" and Path(part["path"]).is_file()
                        for part in current_parts
                    )
                    if not sources_ok or not original.is_file():
                        db.execute("""UPDATE speech_events SET enhancement_status='unavailable',
                            enhancement_path=NULL, enhancement_error='expired_source' WHERE id=?""", (event_id,))
                        return
                    os.replace(tmp, enhanced)
                    db.execute("""UPDATE speech_events SET enhancement_status='complete',
                        enhancement_path=?, enhancement_version=?, derived_bytes=?,
                        enhancement_error=NULL, enhancement_retry_at=0 WHERE id=?""",
                               (str(enhanced), ENHANCE_VERSION,
                                derived_bytes_for(inbox.root, event_id, enhanced), event_id))
    except Exception as error:
        assets["tmp"].unlink(missing_ok=True)
        with inbox.connect() as db:
            row = db.execute("SELECT enhancement_attempts FROM speech_events WHERE id=?",
                             (event_id,)).fetchone()
            attempts = int((row["enhancement_attempts"] if row else 0) or 0) + 1
            if attempts >= MAX_ENHANCE_ATTEMPTS:
                db.execute("""UPDATE speech_events SET enhancement_status='failed',
                    enhancement_attempts=?, enhancement_retry_at=0, enhancement_error=? WHERE id=?""",
                           (attempts, type(error).__name__, event_id))
            else:
                delay = min(3600, 30 * 2 ** min(attempts, 7))
                db.execute("""UPDATE speech_events SET enhancement_status='pending',
                    enhancement_attempts=?, enhancement_retry_at=?, enhancement_error=? WHERE id=?""",
                           (attempts, time.time() + delay, type(error).__name__, event_id))


def reconcile_enhancement(inbox, enhance_cli: Path | None) -> None:
    cli_ready = bool(enhance_cli and Path(enhance_cli).is_file())
    now = time.time()
    with inbox.lock, inbox.connect() as db:
        if cli_ready:
            db.execute("""UPDATE speech_events SET enhancement_status='pending', enhancement_retry_at=0,
                enhancement_error=NULL WHERE enhancement_status='unavailable'
                AND enhancement_error='cli_unavailable'""")
        rows = list(db.execute("SELECT id,enhancement_status FROM speech_events"))
        for row in rows:
            if row["enhancement_status"] == "complete" and not event_asset_paths(inbox.root, row["id"])["enhanced"].is_file():
                db.execute("""UPDATE speech_events SET enhancement_status='pending', enhancement_retry_at=0
                    WHERE id=?""", (row["id"],))


def worker(inbox, stop, cli: Path, ffmpeg: str, enhance_cli: Path | None = None) -> None:
    work = inbox.root / "processing"
    work.mkdir(exist_ok=True, mode=0o700)
    last_reconcile = 0.0
    while not stop.is_set():
        now = time.time()
        if now - last_reconcile >= 30:
            reconcile_enhancement(inbox, enhance_cli)
            last_reconcile = now
        with inbox.connect() as db:
            row = db.execute("""SELECT * FROM chunks WHERE status='complete' AND audio_state='present'
                AND vad_status='pending' AND vad_retry_at<=? ORDER BY started LIMIT 1""",
                             (now,)).fetchone()
        if not row:
            with inbox.connect() as db:
                pending = db.execute("""SELECT id FROM speech_events WHERE enhancement_status='pending'
                    AND enhancement_retry_at<=? LIMIT 1""", (now,)).fetchone()
            if pending:
                try:
                    enhance_event(inbox, pending["id"], ffmpeg, enhance_cli)
                except Exception:
                    pass
            stop.wait(3)
            continue
        try:
            spans = process_chunk(row, cli, ffmpeg, work)
            save_chunk_spans(inbox, row["id"], spans)
            rebuild_events(inbox)
        except Exception as error:
            attempts = int(row["vad_attempts"] or 0) + 1
            with inbox.connect() as db:
                db.execute("""UPDATE chunks SET vad_attempts=?,vad_retry_at=?,vad_error=?,vad_status='pending'
                    WHERE id=?""",
                    (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)),
                     type(error).__name__, row["id"]))
