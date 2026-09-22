"""Offline speaker diarization using FluidAudio; no network calls."""
from __future__ import annotations

import json
import math
import subprocess
import time
import uuid
from pathlib import Path

from meetings import SESSION_GAP, chunk_span, parse_utc
import decoded_audio
import voice_id

LOW_COVERAGE = 0.20
MAX_CONTEXT_SECONDS = 180.0
MAX_NEIGHBORS = 2
ONSET_THRESHOLD = 0.30
OFFSET_THRESHOLD = 0.30
MIN_SEGMENT_DURATION = 0.30
MIN_GAP_DURATION = 0.80
CLUSTER_THRESHOLD = 0.45


def _overlap(start: float, end: float) -> float:
    return max(0.0, end - start)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _span(turn):
    start = _finite(turn["started"])
    end = _finite(turn["ended"])
    if start is None or end is None or end <= start:
        return None
    return start, end


LABEL_OVERLAP = 0.5


def _strong_overlap(old, new) -> float:
    old_span = _span(old)
    new_span = _span(new)
    if not old_span or not new_span:
        return 0.0
    overlap = _overlap(max(old_span[0], new_span[0]), min(old_span[1], new_span[1]))
    old_dur = old_span[1] - old_span[0]
    new_dur = new_span[1] - new_span[0]
    if overlap / old_dur >= LABEL_OVERLAP and overlap / new_dur >= LABEL_OVERLAP:
        return overlap
    return 0.0


def _speech_seconds(turns) -> float:
    if not turns:
        return 0.0
    intervals = sorted((float(turn["started"]), float(turn["ended"])) for turn in turns)
    merged = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def classify_outcome(turns, duration: float, asr_words: int = 0, no_speech: bool = False) -> str:
    coverage = _speech_seconds(turns) / duration if duration > 0 else 0.0
    if no_speech or not turns:
        return "low_coverage" if asr_words > 0 else "no_speech"
    if coverage < LOW_COVERAGE and asr_words > 0:
        return "low_coverage"
    return "success"


def context_window(rows, current) -> tuple[list, float]:
    """Return contiguous same-device neighbors totaling at most 2-3 minutes, plus offset of current clip."""
    current_start, current_end = chunk_span(current)
    selected = [current]
    remaining = MAX_CONTEXT_SECONDS - float(current["duration"] or 0)
    previous = [row for row in rows if row["id"] != current["id"]
                and row["device"] == current["device"]
                and chunk_span(row)[1] <= current_start]
    previous.sort(key=lambda row: chunk_span(row)[0], reverse=True)
    added = 0
    cursor = current_start
    for row in previous:
        if added >= MAX_NEIGHBORS or remaining <= 1:
            break
        start, end = chunk_span(row)
        if cursor - end > SESSION_GAP:
            break
        duration = float(row["duration"] or 0)
        if duration <= 1 or duration > remaining:
            break
        selected.append(row)
        remaining -= duration
        cursor = start
        added += 1
    selected.sort(key=lambda row: chunk_span(row)[0])
    offset = sum(float(row["duration"] or 0) for row in selected
                 if chunk_span(row)[0] < current_start)
    return selected, max(0.0, offset)


def _concat_wavs(ffmpeg: str, sources: list[Path], dest: Path) -> None:
    if len(sources) == 1:
        dest.write_bytes(sources[0].read_bytes())
        return
    listing = dest.with_suffix(".txt")
    listing.write_text("".join(f"file '{path}'\n" for path in sources))
    try:
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-ar", "16000", "-ac", "1", str(dest)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    finally:
        listing.unlink(missing_ok=True)


def process_chunk(row, cli: Path, ffmpeg: str, work: Path, neighbors=None) -> dict:
    token = uuid.uuid4().hex
    wav = work / f"{row['id']}-{token}-diar.wav"
    result = work / f"{row['id']}-{token}-diar.json"
    embeddings = work / f"{row['id']}-{token}-embeddings.json"
    parts = []
    leases = []
    started = time.monotonic()
    neighbors = list(neighbors or [row])
    offset = 0.0
    duration = float(row["duration"] or 0)
    try:
        asr_words = int(row["word_count"] or 0)
    except (KeyError, TypeError, ValueError):
        asr_words = 0
    try:
        for neighbor in neighbors:
            lease = decoded_audio.acquire(
                ffmpeg, Path(neighbor["path"]), str(neighbor["id"]), work, run=subprocess.run,
            )
            leases.append(lease)
            parts.append(lease.path)
        offset = 0.0
        for neighbor in neighbors:
            if neighbor["id"] == row["id"]:
                break
            offset += float(neighbor["duration"] or 0)
        _concat_wavs(ffmpeg, parts, wav)
        completed = subprocess.run([str(cli), "process", str(wav), "--mode", "offline",
                                    "--onset-threshold", str(ONSET_THRESHOLD),
                                    "--offset-threshold", str(OFFSET_THRESHOLD),
                                    "--min-segment-duration", str(MIN_SEGMENT_DURATION),
                                    "--min-gap-duration", str(MIN_GAP_DURATION),
                                    "--threshold", str(CLUSTER_THRESHOLD),
                                    "--output", str(result), "--export-embeddings", str(embeddings)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        no_speech = bool(completed.returncode and b"noSpeechDetected" in (completed.stderr or b""))
        if completed.returncode and not no_speech:
            raise subprocess.CalledProcessError(completed.returncode, completed.args)
        payload = {"segments": [], "speakerCount": 0}
        exported = []
        if not no_speech:
            payload = json.loads(result.read_text())
            if not isinstance(payload.get("segments"), list):
                raise ValueError("Malformed diarization output")
            exported = json.loads(embeddings.read_text()) if embeddings.is_file() else []
            if not isinstance(exported, list):
                raise ValueError("Malformed embedding export")
        parsed, embedding_stats = voice_id.parse_embedding_export(exported, offset, duration)
        context_embedding_count = embedding_stats["context_embedding_count"]
        current_embedding_count = embedding_stats["current_embedding_count"]
        current_clusters = set(embedding_stats["current_clusters"])
        turns = []
        for item in payload.get("segments") or []:
            start = _finite(item.get("startTimeSeconds"))
            end = _finite(item.get("endTimeSeconds"))
            if start is None or end is None:
                raise ValueError("Invalid diarization timing")
            start -= offset
            end -= offset
            clipped_start = max(0.0, start)
            clipped_end = min(duration, end)
            if clipped_end - clipped_start < 0.05:
                continue
            speaker_key = str(item.get("speakerId") or "Unknown")
            if clipped_start < 0 or clipped_end <= clipped_start or clipped_end > duration + 2:
                raise ValueError("Invalid diarization timing")
            turns.append({"speaker_key": speaker_key,
                          "started": clipped_start, "ended": clipped_end,
                          "quality": float(item.get("qualityScore") or 0),
                          "embedding": None, "vectors": []})
        voice_id.bind_turn_vectors(parsed, turns)
        speech = _speech_seconds(turns)
        coverage = speech / duration if duration else 0.0
        outcome = classify_outcome(turns, duration, asr_words=asr_words, no_speech=no_speech)
        if not current_clusters:
            current_clusters = {turn["speaker_key"] for turn in turns}
            current_embedding_count = current_embedding_count if exported else 0
        return {
            "turns": turns,
            "speaker_count": len({turn["speaker_key"] for turn in turns}),
            "processing_seconds": time.monotonic() - started,
            "outcome": outcome,
            "speech_seconds": round(speech, 3),
            "coverage": round(coverage, 4),
            "turn_count": len(turns),
            "embedding_count": current_embedding_count,
            "cluster_count": len(current_clusters) if turns else 0,
            "context_embedding_count": context_embedding_count,
            "context_cluster_count": embedding_stats["context_cluster_count"],
            "context_clips": len(neighbors),
            "context_offset": round(offset, 3),
            "asr_words": asr_words,
        }
    finally:
        wav.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
        embeddings.unlink(missing_ok=True)
        for lease in leases:
            lease.release()


def _attach_sample(db, sample, turn_id: str) -> None:
    if not sample:
        return
    existing = db.execute("SELECT id FROM voice_samples WHERE turn_id=?", (turn_id,)).fetchone()
    if existing:
        return
    vector_id = sample["vector_id"] if "vector_id" in sample.keys() else None
    if vector_id and not db.execute("SELECT 1 FROM voice_vectors WHERE id=?", (vector_id,)).fetchone():
        vector_id = None
    status = sample["status"] if "status" in sample.keys() and sample["status"] else "accepted"
    legacy = int(sample["legacy"]) if "legacy" in sample.keys() and sample["legacy"] is not None else 0
    source_key = sample["source_key"] if "source_key" in sample.keys() else None
    db.execute("""INSERT INTO voice_samples
        (id,person_id,turn_id,embedding_json,duration,confirmed_at,vector_id,status,legacy,source_key)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), sample["person_id"], turn_id, sample["embedding_json"],
         sample["duration"], sample["confirmed_at"], vector_id, status, legacy, source_key))


def _restore_labels(db, chunk_id: str, previous_turns, previous_samples) -> None:
    new_turns = list(db.execute("SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started", (chunk_id,)))
    labeled_old = [turn for turn in previous_turns if turn["person_id"] and _span(turn)]
    candidates = {turn["id"]: [] for turn in new_turns}
    overlapping_people = {turn["id"]: set() for turn in new_turns}
    for old in labeled_old:
        for new in new_turns:
            old_span = _span(old)
            new_span = _span(new)
            if old_span and new_span:
                raw_overlap = _overlap(max(old_span[0], new_span[0]), min(old_span[1], new_span[1]))
                if raw_overlap >= 0.2:
                    overlapping_people[new["id"]].add(old["person_id"])
            overlap = _strong_overlap(old, new)
            if overlap:
                candidates[new["id"]].append((old, overlap))
    ineligible = set()
    for new in new_turns:
        if len(overlapping_people[new["id"]]) > 1:
            ineligible.add(new["id"])
    migrated_old = set()
    assigned_new = {}
    for new in new_turns:
        if new["id"] in ineligible:
            continue
        matches = candidates[new["id"]]
        if not matches:
            continue
        best_old = max(matches, key=lambda pair: pair[1])[0]
        db.execute("UPDATE speaker_turns SET person_id=?,label_source=? WHERE id=?",
                   (best_old["person_id"], best_old["label_source"] or "confirmed", new["id"]))
        assigned_new[new["id"]] = best_old
        for old, _overlap_s in matches:
            migrated_old.add(old["id"])
    for old in labeled_old:
        sample = previous_samples.get(old["id"])
        if old["id"] in migrated_old:
            targets = [(new, _strong_overlap(old, new)) for new in new_turns
                       if new["id"] in assigned_new
                       and assigned_new[new["id"]]["person_id"] == old["person_id"]]
            targets = [pair for pair in targets if pair[1] > 0]
            if sample and targets:
                target = max(targets, key=lambda pair: pair[1])[0]
                _attach_sample(db, sample, target["id"])
            continue
        preserved_id = str(uuid.uuid4())
        db.execute("""INSERT INTO speaker_turns
            (id,run_id,chunk_id,speaker_key,started,ended,quality,embedding_json,person_id,label_source)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (preserved_id, old["run_id"], chunk_id, old["speaker_key"], old["started"],
             old["ended"], old["quality"], old["embedding_json"], old["person_id"],
             old["label_source"] or "confirmed"))
        _attach_sample(db, sample, preserved_id)


def save_result(inbox, chunk_id: str, result: dict) -> None:
    run_id = str(uuid.uuid4())
    now = time.time()
    diagnostics = {
        "outcome": result["outcome"],
        "speech_seconds": result["speech_seconds"],
        "coverage": result["coverage"],
        "turn_count": result["turn_count"],
        "embedding_count": result["embedding_count"],
        "cluster_count": result["cluster_count"],
        "context_embedding_count": result.get("context_embedding_count"),
        "context_cluster_count": result.get("context_cluster_count"),
        "context_clips": result.get("context_clips") or 1,
        "context_offset": result.get("context_offset") or 0,
        "asr_words": result.get("asr_words") or 0,
    }
    with inbox.lock, inbox.connect() as db:
        previous_turns = list(db.execute("SELECT * FROM speaker_turns WHERE chunk_id=?", (chunk_id,)))
        previous_ids = [row["id"] for row in previous_turns]
        previous_samples = {}
        inherited = voice_id.track_spans(db, chunk_id)
        if previous_ids:
            placeholders = ",".join("?" for _ in previous_ids)
            for sample in db.execute(f"SELECT * FROM voice_samples WHERE turn_id IN ({placeholders})",
                                     previous_ids):
                previous_samples[sample["turn_id"]] = sample
            db.execute(f"DELETE FROM voice_samples WHERE turn_id IN ({placeholders})", previous_ids)
        voice_id.retire_chunk(db, chunk_id)
        db.execute("DELETE FROM speaker_turns WHERE chunk_id=?", (chunk_id,))
        db.execute("""INSERT INTO speaker_runs
            (id,chunk_id,engine,status,speaker_count,processing_seconds,created_at,
             outcome,speech_seconds,coverage,turn_count,embedding_count,cluster_count,diagnostics_json)
            VALUES (?,?,?,'complete',?,?,?,?,?,?,?,?,?,?)""",
            (run_id, chunk_id, "FluidAudio-VBx", result["speaker_count"],
             result["processing_seconds"], now, result["outcome"], result["speech_seconds"],
             result["coverage"], result["turn_count"], result["embedding_count"],
             result["cluster_count"], json.dumps(diagnostics)))
        for turn in result["turns"]:
            turn_id = str(uuid.uuid4())
            db.execute("""INSERT INTO speaker_turns
                (id,run_id,chunk_id,speaker_key,started,ended,quality,embedding_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (turn_id, run_id, chunk_id, turn["speaker_key"], turn["started"],
                 turn["ended"], turn["quality"], json.dumps(turn.get("embedding"))))
            voice_id.store_turn_vectors(db, turn_id, run_id, chunk_id, turn, now)
        _restore_labels(db, chunk_id, previous_turns, previous_samples)
        voice_id.assign_chunk_run(db, run_id, chunk_id, inherited, now)
        db.execute("""UPDATE chunks SET diarization_status=?,diarization_error=NULL,
            voice_extract_version=? WHERE id=?""",
                   (result["outcome"], voice_id.EXTRACTION_VERSION, chunk_id))
        voice_id.enqueue_chunk(db, chunk_id, "result")


def neighbor_rows(inbox, row) -> list:
    with inbox.connect() as db:
        rows = db.execute("""SELECT * FROM chunks WHERE status='complete' AND audio_state='present'
            AND device=? ORDER BY started,id""", (row["device"],)).fetchall()
    selected, _offset = context_window(rows, row)
    usable = []
    for item in selected:
        path = Path(item["path"])
        if path.is_file():
            usable.append(item)
        elif item["id"] == row["id"]:
            return [row]
    return usable or [row]


def worker(inbox, stop, cli: Path, ffmpeg: str) -> None:
    work = inbox.root / "processing"
    work.mkdir(exist_ok=True, mode=0o700)
    while not stop.is_set():
        with inbox.connect() as db:
            row = db.execute("""SELECT * FROM chunks WHERE status='complete'
                AND audio_state='present' AND diarization_status='pending'
                AND diarization_retry_at<=? ORDER BY started LIMIT 1""", (time.time(),)).fetchone()
        if not row:
            stop.wait(3); continue
        try:
            refresh_decoded_pins(inbox)
            neighbors = neighbor_rows(inbox, row)
            save_result(inbox, row["id"], process_chunk(row, cli, ffmpeg, work, neighbors=neighbors))
            refresh_decoded_pins(inbox)
        except Exception as error:
            attempts = int(row["diarization_attempts"] or 0) + 1
            with inbox.connect() as db:
                db.execute("""UPDATE chunks SET diarization_attempts=?,diarization_retry_at=?,
                    diarization_error=? WHERE id=?""",
                    (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)),
                     type(error).__name__, row["id"]))


def refresh_decoded_pins(inbox) -> None:
    """Keep the shared wav while this clip, or a neighbor that still needs it, is in the pipeline."""
    work = inbox.root / "processing"
    with inbox.connect() as db:
        rows = list(db.execute("SELECT * FROM chunks WHERE audio_state='present'"))
    keep = []
    pending = []
    for row in rows:
        path = Path(row["path"])
        if not path.is_file():
            continue
        if row["status"] == "pending" or row["vad_status"] == "pending" or row["diarization_status"] == "pending":
            keep.append((row["id"], path))
        if row["status"] == "complete" and row["diarization_status"] == "pending":
            pending.append(row)
    for row in pending:
        for neighbor in neighbor_rows(inbox, row):
            path = Path(neighbor["path"])
            if path.is_file():
                keep.append((neighbor["id"], path))
    decoded_audio.sync_pins(work, keep)
