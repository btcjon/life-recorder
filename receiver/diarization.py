"""Offline speaker diarization using FluidAudio; no network calls."""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path


def process_chunk(row, cli: Path, ffmpeg: str, work: Path) -> dict:
    token = uuid.uuid4().hex
    wav = work / f"{row['id']}-{token}-diar.wav"
    result = work / f"{row['id']}-{token}-diar.json"
    embeddings = work / f"{row['id']}-{token}-embeddings.json"
    started = time.monotonic()
    try:
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", row["path"],
                        "-ar", "16000", "-ac", "1", str(wav)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        completed = subprocess.run([str(cli), "process", str(wav), "--mode", "offline",
                                    "--output", str(result), "--export-embeddings", str(embeddings)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if completed.returncode and b"noSpeechDetected" in completed.stderr:
            return {"turns": [], "speaker_count": 0,
                    "processing_seconds": time.monotonic() - started}
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, completed.args)
        payload = json.loads(result.read_text())
        if not isinstance(payload.get("segments"), list):
            raise ValueError("Malformed diarization output")
        turns = []
        for item in payload["segments"][:10000]:
            start = float(item["startTimeSeconds"]); end = float(item["endTimeSeconds"])
            embedding = item.get("embedding")
            if start < 0 or end <= start or end > float(row["duration"]) + 2:
                raise ValueError("Invalid diarization timing")
            turns.append({"speaker_key": str(item.get("speakerId") or "Unknown"),
                          "started": start, "ended": end,
                          "quality": float(item.get("qualityScore") or 0),
                          "embedding": embedding if isinstance(embedding, list) else None})
        return {"turns": turns, "speaker_count": int(payload.get("speakerCount") or 0),
                "processing_seconds": time.monotonic() - started}
    finally:
        wav.unlink(missing_ok=True); result.unlink(missing_ok=True); embeddings.unlink(missing_ok=True)


def save_result(inbox, chunk_id: str, result: dict) -> None:
    run_id = str(uuid.uuid4())
    now = time.time()
    with inbox.lock, inbox.connect() as db:
        db.execute("DELETE FROM speaker_turns WHERE chunk_id=?", (chunk_id,))
        db.execute("""INSERT INTO speaker_runs
            (id,chunk_id,engine,status,speaker_count,processing_seconds,created_at)
            VALUES (?,?,?,'complete',?,?,?)""",
            (run_id, chunk_id, "FluidAudio-VBx", result["speaker_count"],
             result["processing_seconds"], now))
        for turn in result["turns"]:
            db.execute("""INSERT INTO speaker_turns
                (id,run_id,chunk_id,speaker_key,started,ended,quality,embedding_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), run_id, chunk_id, turn["speaker_key"], turn["started"],
                 turn["ended"], turn["quality"], json.dumps(turn["embedding"])))
        db.execute("UPDATE chunks SET diarization_status='complete',diarization_error=NULL WHERE id=?",
                   (chunk_id,))


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
            save_result(inbox, row["id"], process_chunk(row, cli, ffmpeg, work))
        except Exception as error:
            attempts = int(row["diarization_attempts"] or 0) + 1
            with inbox.connect() as db:
                db.execute("""UPDATE chunks SET diarization_attempts=?,diarization_retry_at=?,
                    diarization_error=? WHERE id=?""",
                    (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)),
                     type(error).__name__, row["id"]))
