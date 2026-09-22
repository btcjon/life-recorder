"""Turn-local voice evidence, anonymous tracks, and voice matching.

A manual tag names the stretch the user chose. When that stretch has at least
5 seconds of clean speech, it also saves a voice sample. After 2 such samples
from 2 recordings and 10 seconds total, a background pass writes a strong match
as automatic. Day and speaker reads do not enroll or auto-tag.
Automatic names are not samples and do not train later matches.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

EXTRACTION_VERSION = 3
EMBEDDING_DIM = 256
MIN_CLEAN_SECONDS = 5.0
PROFILE_MIN_SAMPLES = 2
PROFILE_MIN_CLIPS = 2
PROFILE_MIN_SECONDS = 10.0
SUGGEST_MIN_SCORE = 0.60
AUTO_MIN_SCORE = 0.85
AUTO_MIN_MARGIN = 0.10
_SCORE_TOLERANCE = 1e-6
JOB_BATCH = 4
RECOVER_BATCH = 20
MAX_JOB_ATTEMPTS = 8
_recover_tokens = set()
_recover_lock = threading.Lock()
COMPLETED_OUTCOMES = ("success", "low_coverage", "no_speech", "complete")


def normalize_vector(vector):
    try:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
            return None
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            return None
        norm = math.sqrt(sum(value * value for value in values))
    except (TypeError, ValueError, OverflowError):
        return None
    if not norm:
        return None
    return [value / norm for value in values]


def cosine(left, right) -> float:
    if len(left or []) != EMBEDDING_DIM or len(right or []) != EMBEDDING_DIM:
        return -1.0
    try:
        if not all(math.isfinite(float(value)) for value in list(left) + list(right)):
            return -1.0
        dot = sum(float(left[index]) * float(right[index]) for index in range(EMBEDDING_DIM))
        left_norm = math.sqrt(sum(float(left[index]) ** 2 for index in range(EMBEDDING_DIM)))
        right_norm = math.sqrt(sum(float(right[index]) ** 2 for index in range(EMBEDDING_DIM)))
    except (TypeError, ValueError, OverflowError):
        return -1.0
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _overlap(start: float, end: float) -> float:
    return max(0.0, end - start)


def automation_enabled(db) -> bool:
    row = db.execute("SELECT status FROM voice_calibration WHERE id=1").fetchone()
    return bool(row and row["status"] == "passed")


def parse_embedding_export(exported, offset: float, duration: float):
    """Keep 256-d rows that overlap the current clip. Neighbor-only audio is dropped."""
    window_start = float(offset)
    window_end = window_start + float(duration)
    context_clusters = set()
    current_clusters = set()
    context_count = 0
    current_count = 0
    seen = set()
    parsed = []
    for item in exported or []:
        if not isinstance(item, dict):
            continue
        cluster = item.get("cluster")
        vector = normalize_vector(item.get("embedding256"))
        if isinstance(cluster, int):
            context_clusters.add(cluster)
        if not isinstance(cluster, int) or vector is None:
            continue
        context_count += 1
        start = _finite(item.get("startTime"))
        if start is None:
            start = _finite(item.get("startTimeSeconds"))
        end = _finite(item.get("endTime"))
        if end is None:
            end = _finite(item.get("endTimeSeconds"))
        timed = start is not None and end is not None and end > start
        if timed:
            local_start = max(start, window_start) - window_start
            local_end = min(end, window_end) - window_start
            if local_end <= local_start:
                continue
        elif offset > 0:
            continue
        else:
            local_start = None
            local_end = None
        digest = (
            cluster,
            None if local_start is None else round(local_start, 3),
            None if local_end is None else round(local_end, 3),
            tuple(round(value, 5) for value in vector),
        )
        if digest in seen:
            continue
        seen.add(digest)
        current_count += 1
        current_clusters.add(cluster)
        parsed.append({
            "cluster": cluster,
            "speaker_key": f"S{cluster + 1}",
            "embedding": vector,
            "started": local_start,
            "ended": local_end,
            "timed": timed,
        })
    return parsed, {
        "context_embedding_count": context_count,
        "context_cluster_count": len(context_clusters),
        "context_clusters": context_clusters,
        "current_embedding_count": current_count,
        "current_clusters": current_clusters,
    }


def bind_turn_vectors(parsed, turns) -> None:
    """Attach each exported vector to one same-speaker turn. Never copy one centroid onto every turn."""
    for turn in turns:
        turn["vectors"] = []
        turn["embedding"] = None
    for item in parsed:
        speaker = item["speaker_key"]
        if not item["timed"]:
            same = [turn for turn in turns if turn.get("speaker_key") == speaker]
            if len(same) != 1:
                continue
            turn = same[0]
            turn["vectors"].append({
                "embedding": item["embedding"],
                "started": float(turn["started"]),
                "ended": float(turn["ended"]),
                "overlap": 0,
                "timed": 0,
                "legacy": 0,
                "quality": turn.get("quality"),
            })
            continue
        best = None
        best_overlap = 0.0
        other_speaker = False
        for turn in turns:
            start = _finite(turn.get("started"))
            end = _finite(turn.get("ended"))
            if start is None or end is None:
                continue
            overlap = _overlap(max(item["started"], start), min(item["ended"], end))
            if overlap <= 0:
                continue
            if turn.get("speaker_key") == speaker:
                if overlap > best_overlap:
                    best = turn
                    best_overlap = overlap
            else:
                other_speaker = True
        if best is None:
            continue
        best["vectors"].append({
            "embedding": item["embedding"],
            "started": float(item["started"]),
            "ended": float(item["ended"]),
            "overlap": 1 if other_speaker else 0,
            "timed": 1,
            "legacy": 0,
            "quality": best.get("quality"),
        })


def track_spans(db, chunk_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT v.started AS started, v.ended AS ended, a.track_id AS track_id
           FROM voice_assignments a
           JOIN voice_vectors v ON v.id=a.vector_id
           WHERE v.chunk_id=? AND a.active=1 AND a.state='assigned' AND a.track_id IS NOT NULL""",
        (chunk_id,),
    ).fetchall()
    return [{"started": float(row["started"]), "ended": float(row["ended"]), "track_id": row["track_id"]}
            for row in rows]


def retire_chunk(db, chunk_id: str) -> None:
    db.execute(
        """UPDATE voice_assignments SET active=0, state='retracted'
           WHERE active=1 AND vector_id IN (SELECT id FROM voice_vectors WHERE chunk_id=?)""",
        (chunk_id,),
    )
    db.execute("DELETE FROM voice_vectors WHERE chunk_id=?", (chunk_id,))


def _vector_records(turn) -> list[dict]:
    records = list(turn.get("vectors") or [])
    if records:
        return records
    embedding = turn.get("embedding")
    started = _finite(turn.get("started"))
    ended = _finite(turn.get("ended"))
    if isinstance(embedding, list) and started is not None and ended is not None and ended > started:
        return [{
            "embedding": embedding,
            "started": started,
            "ended": ended,
            "overlap": 0,
            "timed": 1,
            "legacy": 0,
            "quality": turn.get("quality"),
        }]
    return []


def store_turn_vectors(db, turn_id: str, run_id: str, chunk_id: str, turn, now: float) -> None:
    speaker = str(turn.get("speaker_key") or "Unknown")
    for record in _vector_records(turn):
        embedding = normalize_vector(record.get("embedding"))
        started = _finite(record.get("started"))
        ended = _finite(record.get("ended"))
        if embedding is None or started is None or ended is None or ended <= started:
            continue
        legacy = 1 if record.get("legacy") else 0
        timed = 0 if legacy else (1 if record.get("timed", 1) else 0)
        overlap = 1 if record.get("overlap") else 0
        quality = _finite(record.get("quality"))
        interval_key = f"{chunk_id}:{started:.3f}:{ended:.3f}:{speaker}"
        db.execute(
            """INSERT INTO voice_vectors
               (id,turn_id,run_id,chunk_id,speaker_key,started,ended,duration,quality,overlap,timed,legacy,
                embedding_json,extraction_version,interval_key,enrolled,person_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?)""",
            (str(uuid.uuid4()), turn_id, run_id, chunk_id, speaker, started, ended, ended - started,
             quality, overlap, timed, legacy, json.dumps(embedding), EXTRACTION_VERSION, interval_key, now),
        )


def _assignment(db, vector_id: str, track_id: str | None, state: str, evidence: dict, now: float) -> None:
    version = 1
    if track_id:
        row = db.execute(
            "SELECT COALESCE(MAX(version),0) AS version FROM voice_assignments WHERE track_id=?",
            (track_id,),
        ).fetchone()
        version = int(row["version"]) + 1
    db.execute(
        """INSERT INTO voice_assignments
           (id,vector_id,track_id,state,version,evidence_json,active,created_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        (str(uuid.uuid4()), vector_id, track_id, state, version, json.dumps(evidence), now),
    )


def _overlapping_tracks(inherited, started: float, ended: float) -> set[str]:
    found = set()
    for span in inherited:
        if _overlap(max(started, span["started"]), min(ended, span["ended"])) > 0:
            found.add(span["track_id"])
    return found


def _reject_cause(vector) -> str:
    if vector["legacy"]:
        return "legacy"
    if not vector["timed"]:
        return "untimed"
    if vector["overlap"]:
        return "overlap"
    return "short"


def assign_chunk_run(db, run_id: str, chunk_id: str, inherited: list[dict], now: float | None = None) -> None:
    """One track per qualified speaker inside this run. Cross-run joins are not automatic."""
    now = time.time() if now is None else now
    vectors = list(db.execute("SELECT * FROM voice_vectors WHERE run_id=?", (run_id,)))
    grouped: dict[str, list] = {}
    for vector in vectors:
        grouped.setdefault(vector["speaker_key"], []).append(vector)
    claims: dict[str, set[str]] = {}
    clean_by_speaker = {}
    for speaker, group in grouped.items():
        clean = [vector for vector in group if not vector["legacy"] and vector["timed"] and not vector["overlap"]]
        clean_by_speaker[speaker] = clean
        overlapped = set()
        for vector in clean:
            overlapped.update(_overlapping_tracks(inherited, float(vector["started"]), float(vector["ended"])))
        for track_id in overlapped:
            claims.setdefault(track_id, set()).add(speaker)
    for speaker, group in grouped.items():
        clean = clean_by_speaker[speaker]
        clean_seconds = sum(float(vector["duration"]) for vector in clean)
        reusable = None
        conflict = False
        if clean_seconds >= MIN_CLEAN_SECONDS:
            candidates = set()
            for vector in clean:
                candidates.update(_overlapping_tracks(inherited, float(vector["started"]), float(vector["ended"])))
            exclusive = {track_id for track_id in candidates if claims.get(track_id) == {speaker}}
            if len(exclusive) == 1:
                reusable = next(iter(exclusive))
            elif len(candidates) > 1:
                conflict = True
        if clean_seconds >= MIN_CLEAN_SECONDS and not conflict:
            track_id = reusable or str(uuid.uuid4())
            if reusable is None:
                db.execute(
                    """INSERT INTO voice_tracks (id,chunk_id,status,frozen_reason,anchor_person_id,created_at)
                       VALUES (?,?,'open',NULL,NULL,?)""",
                    (track_id, chunk_id, now),
                )
            evidence = {"rule": "within_run", "clean_seconds": round(clean_seconds, 3), "speaker_key": speaker}
            for vector in clean:
                _assignment(db, vector["id"], track_id, "assigned", evidence, now)
            for vector in group:
                if vector not in clean:
                    _assignment(db, vector["id"], None, "unknown",
                                {"rule": "rejected", "cause": _reject_cause(vector)}, now)
        else:
            cause = "conflict" if conflict else "short"
            for vector in group:
                reason = _reject_cause(vector) if vector not in clean else cause
                _assignment(db, vector["id"], None, "unknown", {"rule": "rejected", "cause": reason}, now)
    refresh_tracks(db, _track_ids_for_run(db, run_id))


def _track_ids_for_run(db, run_id: str) -> set[str]:
    rows = db.execute(
        """SELECT DISTINCT a.track_id FROM voice_assignments a
           JOIN voice_vectors v ON v.id=a.vector_id
           WHERE v.run_id=? AND a.track_id IS NOT NULL""",
        (run_id,),
    ).fetchall()
    return {row["track_id"] for row in rows}


def _tracks_for_turns(db, turn_ids: list[str]) -> set[str]:
    if not turn_ids:
        return set()
    placeholders = ",".join("?" for _ in turn_ids)
    rows = db.execute(
        f"""SELECT DISTINCT a.track_id FROM voice_assignments a
            JOIN voice_vectors v ON v.id=a.vector_id
            WHERE v.turn_id IN ({placeholders}) AND a.active=1 AND a.track_id IS NOT NULL""",
        turn_ids,
    ).fetchall()
    return {row["track_id"] for row in rows}


def refresh_tracks(db, track_ids: set[str]) -> None:
    for track_id in track_ids:
        labels = db.execute(
            """SELECT DISTINCT t.person_id FROM speaker_turns t
               JOIN voice_vectors v ON v.turn_id=t.id
               JOIN voice_assignments a ON a.vector_id=v.id AND a.active=1 AND a.state='assigned'
               WHERE a.track_id=? AND t.person_id IS NOT NULL""",
            (track_id,),
        ).fetchall()
        enrolled = db.execute(
            """SELECT DISTINCT v.person_id FROM voice_vectors v
               JOIN voice_assignments a ON a.vector_id=v.id AND a.active=1 AND a.state='assigned'
               WHERE a.track_id=? AND v.enrolled=1 AND v.person_id IS NOT NULL""",
            (track_id,),
        ).fetchall()
        label_ids = {row["person_id"] for row in labels}
        enrolled_ids = {row["person_id"] for row in enrolled}
        if len(label_ids) > 1 or len(enrolled_ids) > 1:
            db.execute(
                """UPDATE voice_tracks SET status='frozen', frozen_reason='conflicting_identity',
                   anchor_person_id=NULL WHERE id=?""",
                (track_id,),
            )
            continue
        anchor = next(iter(enrolled_ids)) if len(enrolled_ids) == 1 else None
        db.execute(
            """UPDATE voice_tracks SET status='open', frozen_reason=NULL, anchor_person_id=? WHERE id=?""",
            (anchor, track_id),
        )


def clear_enrollment(db, turn_ids: list[str]) -> None:
    if not turn_ids:
        return
    placeholders = ",".join("?" for _ in turn_ids)
    db.execute(
        f"UPDATE voice_vectors SET enrolled=0, person_id=NULL WHERE turn_id IN ({placeholders})",
        turn_ids,
    )


def _enrolled_ids_by_interval(db, vectors) -> dict[str, set[str]]:
    keys = [vector["interval_key"] for vector in vectors if vector["interval_key"]]
    found: dict[str, set[str]] = {}
    unique = list(dict.fromkeys(keys))
    for offset in range(0, len(unique), 500):
        batch = unique[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = db.execute(
            f"SELECT id, interval_key FROM voice_vectors WHERE enrolled=1 AND interval_key IN ({placeholders})",
            batch,
        )
        for row in rows:
            found.setdefault(row["interval_key"], set()).add(row["id"])
    return found


def _vector_already_enrolled(vector, enrolled: dict[str, set[str]]) -> bool:
    others = enrolled.get(vector["interval_key"], set())
    return any(vector_id != vector["id"] for vector_id in others)


def eligible_clean_seconds(db, turn_ids: list[str]) -> dict[str, float]:
    """Clean speech inside each selected turn.

    Embedding windows that hang outside the piece are clipped to it, and
    overlapping windows are counted once. Silence outside the piece is excluded.
    """
    totals = {turn_id: 0.0 for turn_id in turn_ids}
    if not turn_ids:
        return totals
    placeholders = ",".join("?" for _ in turn_ids)
    turns = db.execute(
        f"SELECT id, started, ended FROM speaker_turns WHERE id IN ({placeholders})",
        list(turn_ids),
    ).fetchall()
    vectors = db.execute(
        f"""SELECT id, turn_id, started, ended, interval_key FROM voice_vectors
            WHERE turn_id IN ({placeholders}) AND legacy=0 AND timed=1 AND overlap=0
              AND extraction_version=?""",
        (*list(turn_ids), EXTRACTION_VERSION),
    ).fetchall()
    enrolled = _enrolled_ids_by_interval(db, vectors)
    by_turn: dict[str, list] = {}
    for vector in vectors:
        if _vector_already_enrolled(vector, enrolled):
            continue
        by_turn.setdefault(vector["turn_id"], []).append(vector)
    for turn in turns:
        span_start = float(turn["started"])
        span_end = float(turn["ended"])
        intervals = []
        for vector in by_turn.get(turn["id"], []):
            start = max(span_start, float(vector["started"]))
            end = min(span_end, float(vector["ended"]))
            if end > start:
                intervals.append((start, end))
        intervals.sort()
        covered = 0.0
        cursor = None
        for start, end in intervals:
            if cursor is None or start > cursor:
                covered += end - start
                cursor = end
            elif end > cursor:
                covered += end - cursor
                cursor = end
        totals[turn["id"]] = covered
    return totals


def enroll_turns(db, turns, person_id: str, now: float | None = None) -> dict:
    """Save one manual sample when the stretch has enough clean speech. Does not name other clips."""
    now = time.time() if now is None else now
    turn_ids = [turn["id"] for turn in turns]
    if not turn_ids:
        return {"enrolled": False, "reason": "no_turn"}
    placeholders = ",".join("?" for _ in turn_ids)
    vectors = list(db.execute(
        f"""SELECT * FROM voice_vectors WHERE turn_id IN ({placeholders})
            AND legacy=0 AND timed=1 AND overlap=0 AND extraction_version=?""",
        (*turn_ids, EXTRACTION_VERSION),
    ))
    if not vectors:
        legacy = db.execute(
            f"SELECT 1 FROM voice_vectors WHERE turn_id IN ({placeholders}) AND legacy=1",
            turn_ids,
        ).fetchone()
        return {"enrolled": False, "reason": "legacy_centroid" if legacy else "no_clean_vector"}
    enrolled = _enrolled_ids_by_interval(db, vectors)
    accepted = []
    for vector in vectors:
        if _vector_already_enrolled(vector, enrolled):
            continue
        accepted.append(vector)
    duration = sum(eligible_clean_seconds(db, turn_ids).values())
    if duration < MIN_CLEAN_SECONDS or not accepted:
        reason = "duplicate" if vectors and not accepted else "need_5s"
        return {"enrolled": False, "reason": reason}
    accepted_ids = [vector["id"] for vector in accepted]
    id_placeholders = ",".join("?" for _ in accepted_ids)
    db.execute(
        f"UPDATE voice_vectors SET enrolled=1, person_id=? WHERE id IN ({id_placeholders})",
        [person_id, *accepted_ids],
    )
    db.execute(f"DELETE FROM voice_samples WHERE turn_id IN ({placeholders})", turn_ids)
    exemplar = normalize_vector(json.loads(accepted[0]["embedding_json"]))
    db.execute(
        """INSERT INTO voice_samples
           (id,person_id,turn_id,embedding_json,duration,confirmed_at,vector_id,status,legacy,source_key)
           VALUES (?,?,?,?,?,?,?,'accepted',0,?)""",
        (str(uuid.uuid4()), person_id, turns[0]["id"], json.dumps(exemplar), duration, now,
         accepted[0]["id"], accepted[0]["interval_key"]),
    )
    refresh_tracks(db, _tracks_for_turns(db, turn_ids))
    return {"enrolled": True, "reason": "accepted", "vectors": len(accepted)}


def _exemplars(db) -> dict[str, list[list[float]]]:
    grouped: dict[str, list[list[float]]] = {}
    rows = db.execute(
        """SELECT person_id, embedding_json FROM voice_vectors
           WHERE enrolled=1 AND legacy=0 AND person_id IS NOT NULL"""
    ).fetchall()
    for row in rows:
        vector = normalize_vector(json.loads(row["embedding_json"]))
        if vector is not None:
            grouped.setdefault(row["person_id"], []).append(vector)
    samples = db.execute(
        """SELECT s.person_id, s.embedding_json FROM voice_samples s
           WHERE s.legacy=0 AND COALESCE(s.status,'accepted')='accepted'
           AND NOT EXISTS (
             SELECT 1 FROM voice_vectors v WHERE v.turn_id=s.turn_id AND v.enrolled=1
           )"""
    ).fetchall()
    for row in samples:
        vector = normalize_vector(json.loads(row["embedding_json"]))
        if vector is not None:
            grouped.setdefault(row["person_id"], []).append(vector)
    return grouped


def annotate_turns(db, turns: list[dict]) -> None:
    """Rank suggestions onto turn dicts. This does not write person_id."""
    if not turns:
        return
    turn_ids = [turn["id"] for turn in turns]
    placeholders = ",".join("?" for _ in turn_ids)
    vector_rows = list(db.execute(
        f"SELECT * FROM voice_vectors WHERE turn_id IN ({placeholders})",
        turn_ids,
    ))
    vectors_by_turn: dict[str, list] = {}
    for row in vector_rows:
        vectors_by_turn.setdefault(row["turn_id"], []).append(row)
    vector_ids = [row["id"] for row in vector_rows]
    assignments = {}
    if vector_ids:
        id_placeholders = ",".join("?" for _ in vector_ids)
        for row in db.execute(
            f"""SELECT * FROM voice_assignments WHERE active=1 AND vector_id IN ({id_placeholders})""",
            vector_ids,
        ):
            assignments[row["vector_id"]] = row
    track_ids = {row["track_id"] for row in assignments.values() if row["track_id"]}
    tracks = {}
    if track_ids:
        id_placeholders = ",".join("?" for _ in track_ids)
        for row in db.execute(
            f"SELECT * FROM voice_tracks WHERE id IN ({id_placeholders})",
            list(track_ids),
        ):
            tracks[row["id"]] = row
    names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM people")}
    exemplars = _exemplars(db)
    for turn in turns:
        reasons = []
        turn_vectors = vectors_by_turn.get(turn["id"], [])
        track = None
        for vector in turn_vectors:
            assignment = assignments.get(vector["id"])
            if assignment and assignment["track_id"] and assignment["state"] == "assigned":
                track = tracks.get(assignment["track_id"])
                if track:
                    break
        if track:
            turn["track_id"] = track["id"]
            turn["track_status"] = track["status"]
        if turn.get("person_id"):
            reasons.append("confirmed")
            turn["suggestion_reasons"] = reasons
            continue
        if track and track["status"] == "frozen":
            reasons.append("track_frozen")
            turn["suggestion_reasons"] = reasons
            continue
        clean = [vector for vector in turn_vectors if not vector["legacy"] and vector["timed"] and not vector["overlap"]]
        if not turn_vectors:
            reasons.append("no_embedding")
        elif not clean:
            reasons.append("legacy_centroid" if any(vector["legacy"] for vector in turn_vectors) else "no_clean_vector")
        elif not exemplars:
            reasons.append("no_enrolled_voiceprints")
        else:
            ranked = []
            for person_id, samples in exemplars.items():
                best = max(cosine(json.loads(vector["embedding_json"]), sample)
                           for vector in clean for sample in samples)
                ranked.append((best, person_id))
            ranked.sort(reverse=True)
            score = ranked[0][0]
            margin = score - ranked[1][0] if len(ranked) > 1 else score
            turn["suggestion_score"] = round(score, 3)
            turn["suggestion_margin"] = round(margin, 3)
            if score < SUGGEST_MIN_SCORE - _SCORE_TOLERANCE:
                reasons.append("score_below_0.60")
            else:
                turn["suggested_person_id"] = ranked[0][1]
                turn["suggested_name"] = names.get(ranked[0][1])
                turn["suggestion_basis"] = "ranked"
                if score < AUTO_MIN_SCORE - _SCORE_TOLERANCE:
                    reasons.append("score_below_0.85")
                elif margin < AUTO_MIN_MARGIN - _SCORE_TOLERANCE:
                    reasons.append("margin_below_0.10")
                else:
                    reasons.append("needs_confirmation")
        turn["suggestion_reasons"] = reasons


def enrollment_reasons(sample_count: int, clip_count: int, sample_seconds: float, calibrated: bool = False) -> list[str]:
    """Profile gate for auto-tag. Calibration is recorded separately and does not block this."""
    del calibrated
    reasons = []
    if sample_count < PROFILE_MIN_SAMPLES:
        reasons.append("need_2_samples")
    if clip_count < PROFILE_MIN_CLIPS:
        reasons.append("need_2_clips")
    if sample_seconds < PROFILE_MIN_SECONDS:
        reasons.append("need_10s")
    return reasons


def manual_profiles(db) -> dict[str, dict]:
    """Accepted samples saved from confirmed manual tags."""
    grouped: dict[str, dict] = {}
    rows = db.execute(
        """SELECT s.person_id, s.embedding_json, s.duration, t.chunk_id
           FROM voice_samples s
           JOIN speaker_turns t ON t.id=s.turn_id
           WHERE COALESCE(s.legacy,0)=0 AND COALESCE(s.status,'accepted')='accepted'
             AND t.label_source='confirmed' AND t.person_id=s.person_id"""
    )
    for row in rows:
        vector = normalize_vector(json.loads(row["embedding_json"]))
        if vector is None:
            continue
        item = grouped.setdefault(row["person_id"], {
            "vectors": [], "clips": set(), "seconds": 0.0, "samples": 0, "ready": False,
        })
        item["vectors"].append(vector)
        item["clips"].add(row["chunk_id"])
        item["seconds"] += float(row["duration"] or 0)
        item["samples"] += 1
    for item in grouped.values():
        item["ready"] = (
            item["samples"] >= PROFILE_MIN_SAMPLES
            and len(item["clips"]) >= PROFILE_MIN_CLIPS
            and item["seconds"] >= PROFILE_MIN_SECONDS
        )
    return grouped


def _review_groups(turns):
    from receiver import review_groups
    return review_groups(turns)


def recover_confirmed_samples(db, now: float | None = None) -> int:
    """Enroll finished manual stretches that already qualify and have no sample yet."""
    pending = [row[0] for row in db.execute(
        """SELECT t.id FROM speaker_turns t
           WHERE t.label_source='confirmed' AND t.person_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM voice_vectors v WHERE v.turn_id=t.id AND v.enrolled=1
             )
           ORDER BY t.id"""
    )]
    token = tuple(pending)
    with _recover_lock:
        if token in _recover_tokens:
            return 0
    if not pending:
        with _recover_lock:
            _recover_tokens.add(token)
        return 0
    chunk_ids = [row[0] for row in db.execute(
        f"""SELECT DISTINCT chunk_id FROM speaker_turns
            WHERE id IN ({",".join("?" for _ in pending)})""",
        pending,
    )]
    saved = 0
    for chunk_id in chunk_ids:
        turns = list(db.execute(
            "SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id",
            (chunk_id,),
        ))
        for group in _review_groups(turns):
            people = {turn["person_id"] for turn in group if turn["person_id"]}
            sources = {turn["label_source"] for turn in group}
            if len(people) != 1 or sources != {"confirmed"} or any(not turn["person_id"] for turn in group):
                continue
            if enroll_turns(db, group, next(iter(people)), now).get("enrolled"):
                saved += 1
    with _recover_lock:
        if saved:
            _recover_tokens.clear()
        else:
            _recover_tokens.add(token)
    return saved


def _stretch_match(db, group, profiles: dict[str, dict]) -> str | None:
    if not profiles or any(turn["person_id"] for turn in group):
        return None
    turn_ids = [turn["id"] for turn in group]
    placeholders = ",".join("?" for _ in turn_ids)
    vectors = list(db.execute(
        f"""SELECT * FROM voice_vectors
            WHERE turn_id IN ({placeholders}) AND legacy=0 AND timed=1 AND overlap=0
              AND extraction_version=?""",
        (*turn_ids, EXTRACTION_VERSION),
    ))
    if not vectors:
        return None
    vector_ids = [vector["id"] for vector in vectors]
    id_placeholders = ",".join("?" for _ in vector_ids)
    frozen = db.execute(
        f"""SELECT 1 FROM voice_assignments a
            JOIN voice_tracks t ON t.id=a.track_id
            WHERE a.vector_id IN ({id_placeholders}) AND a.active=1 AND a.state='assigned'
              AND t.status='frozen'""",
        vector_ids,
    ).fetchone()
    if frozen:
        return None
    track_ids = [row[0] for row in db.execute(
        f"""SELECT DISTINCT a.track_id FROM voice_assignments a
            WHERE a.vector_id IN ({id_placeholders}) AND a.active=1 AND a.state='assigned'
              AND a.track_id IS NOT NULL""",
        vector_ids,
    )]
    confirmed = set()
    if track_ids:
        track_placeholders = ",".join("?" for _ in track_ids)
        confirmed = {row[0] for row in db.execute(
            f"""SELECT DISTINCT t.person_id FROM speaker_turns t
                JOIN voice_vectors v ON v.turn_id=t.id
                JOIN voice_assignments a ON a.vector_id=v.id AND a.active=1 AND a.state='assigned'
                WHERE a.track_id IN ({track_placeholders})
                  AND t.label_source='confirmed' AND t.person_id IS NOT NULL""",
            track_ids,
        )}
    winners = set()
    for vector in vectors:
        embedding = normalize_vector(json.loads(vector["embedding_json"]))
        if embedding is None:
            return None
        ranked = []
        for person_id, profile in profiles.items():
            if not profile["vectors"]:
                continue
            score = max(cosine(embedding, sample) for sample in profile["vectors"])
            ranked.append((score, person_id))
        if not ranked:
            return None
        ranked.sort(reverse=True)
        best_score, best_person = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score < AUTO_MIN_SCORE - _SCORE_TOLERANCE or best_score - second < AUTO_MIN_MARGIN - _SCORE_TOLERANCE:
            return None
        if not profiles[best_person]["ready"]:
            return None
        if confirmed and confirmed != {best_person}:
            return None
        winners.add(best_person)
    if len(winners) != 1:
        return None
    return next(iter(winners))


def _write_automatic(db, group, person_id: str) -> bool:
    turn_ids = [turn["id"] for turn in group]
    placeholders = ",".join("?" for _ in turn_ids)
    rows = list(db.execute(
        f"SELECT id, person_id FROM speaker_turns WHERE id IN ({placeholders})",
        turn_ids,
    ))
    if len(rows) != len(turn_ids) or any(row["person_id"] for row in rows):
        return False
    if _stretch_match(db, group, manual_profiles(db)) != person_id:
        return False
    db.execute(
        f"""UPDATE speaker_turns SET person_id=?, label_source='automatic'
            WHERE id IN ({placeholders}) AND person_id IS NULL""",
        (person_id, *turn_ids),
    )
    changed = db.execute(
        f"""SELECT COUNT(*) FROM speaker_turns
            WHERE id IN ({placeholders}) AND person_id=? AND label_source='automatic'""",
        (*turn_ids, person_id),
    ).fetchone()[0]
    if changed != len(turn_ids):
        db.execute(
            f"""UPDATE speaker_turns SET person_id=NULL, label_source=NULL
                WHERE id IN ({placeholders}) AND label_source='automatic' AND person_id=?""",
            (*turn_ids, person_id),
        )
        return False
    return True



def identity_match_decision(db, group, profiles=None):
    """Automatic-name gate. Only current 256-d evidence can clear 0.85 and a 0.10 margin."""
    if profiles is None:
        profiles = manual_profiles(db)
    return _stretch_match(db, group, profiles)


def auto_tag_chunks(db, chunk_ids: list[str] | None = None) -> int:
    """Write one automatic name onto each wholly unlabeled stretch that clears the match rule."""
    profiles = manual_profiles(db)
    if not any(profile["ready"] for profile in profiles.values()):
        return 0
    if chunk_ids is None:
        chunk_ids = [row[0] for row in db.execute(
            "SELECT DISTINCT chunk_id FROM speaker_turns WHERE person_id IS NULL"
        )]
    written = 0
    for chunk_id in chunk_ids:
        turns = list(db.execute(
            "SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id",
            (chunk_id,),
        ))
        if not any(turn["person_id"] is None for turn in turns):
            continue
        for group in _review_groups(turns):
            person_id = identity_match_decision(db, group, profiles)
            if person_id and _write_automatic(db, group, person_id):
                written += 1
    return written



def enqueue_chunk(db, chunk_id: str, reason: str) -> None:
    """Queue one recording for background recovery and auto-tag. Duplicate pending jobs stay put."""
    if not chunk_id:
        return
    db.execute(
        """INSERT INTO voice_jobs (chunk_id, reason, enqueued_at, attempts)
           VALUES (?,?,?,0)
           ON CONFLICT(chunk_id) DO UPDATE SET
             reason=excluded.reason,
             enqueued_at=excluded.enqueued_at,
             attempts=0,
             last_error=NULL
           WHERE voice_jobs.attempts>=?""",
        (chunk_id, reason, time.time(), MAX_JOB_ATTEMPTS),
    )


def enqueue_unlabeled(db, reason: str) -> int:
    """Schedule every unlabeled recording once after a sample changes a ready profile."""
    if not any(profile["ready"] for profile in manual_profiles(db).values()):
        return 0
    ids = [row[0] for row in db.execute(
        "SELECT DISTINCT chunk_id FROM speaker_turns WHERE person_id IS NULL"
    )]
    for chunk_id in ids:
        enqueue_chunk(db, chunk_id, reason)
    return len(ids)


def recover_chunk(db, chunk_id: str, now: float | None = None) -> int:
    """Enroll fully confirmed stretches on one recording. Partial stretches are left alone."""
    turns = list(db.execute(
        "SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id",
        (chunk_id,),
    ))
    saved = 0
    for group in _review_groups(turns):
        people = {turn["person_id"] for turn in group if turn["person_id"]}
        sources = {turn["label_source"] for turn in group}
        if len(people) != 1 or sources != {"confirmed"} or any(not turn["person_id"] for turn in group):
            continue
        if enroll_turns(db, group, next(iter(people)), now).get("enrolled"):
            saved += 1
    return saved


def recover_confirmed_batch(db, limit: int = RECOVER_BATCH, now: float | None = None) -> bool:
    """Backfill a bounded set of historical confirmed recordings. Each recording is tried once."""
    rows = db.execute(
        """SELECT DISTINCT t.chunk_id FROM speaker_turns t
           WHERE t.label_source='confirmed' AND t.person_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM voice_vectors v WHERE v.turn_id=t.id AND v.enrolled=1
             )
             AND NOT EXISTS (
               SELECT 1 FROM voice_recover_skip s WHERE s.chunk_id=t.chunk_id
             )
           ORDER BY t.chunk_id
           LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return False
    for row in rows:
        chunk_id = row[0]
        recover_chunk(db, chunk_id, now)
        db.execute("INSERT OR IGNORE INTO voice_recover_skip (chunk_id) VALUES (?)", (chunk_id,))
    return True


def _maintenance(db) -> tuple[int, int]:
    row = db.execute(
        "SELECT recover_complete, startup_sweep_complete FROM voice_maintenance WHERE id=1"
    ).fetchone()
    if not row:
        db.execute(
            "INSERT INTO voice_maintenance (id, recover_complete, startup_sweep_complete) VALUES (1, 0, 0)"
        )
        return 0, 0
    return int(row["recover_complete"]), int(row["startup_sweep_complete"])


def chunk_voice_pending(db, chunk_id: str) -> bool:
    """True while background work can still name this recording."""
    if db.execute("SELECT 1 FROM voice_jobs WHERE chunk_id=?", (chunk_id,)).fetchone():
        return True
    row = db.execute("SELECT recover_complete FROM voice_maintenance WHERE id=1").fetchone()
    if row and not row["recover_complete"]:
        return bool(db.execute(
            "SELECT 1 FROM speaker_turns WHERE chunk_id=? AND person_id IS NULL LIMIT 1",
            (chunk_id,),
        ).fetchone())
    return False


def step_voice_maintenance(inbox) -> bool:
    """Run one short maintenance step. Returns False when there is nothing left to do."""
    with inbox.connect() as db:
        recover_complete, sweep_complete = _maintenance(db)
        if not recover_complete:
            if recover_confirmed_batch(db, limit=1):
                return True
            db.execute("UPDATE voice_maintenance SET recover_complete=1 WHERE id=1")
            return True
        if not sweep_complete:
            enqueue_unlabeled(db, "startup")
            db.execute("UPDATE voice_maintenance SET startup_sweep_complete=1 WHERE id=1")
            return True
        jobs = list(db.execute(
            """SELECT chunk_id FROM voice_jobs
               WHERE attempts < ? ORDER BY enqueued_at, chunk_id LIMIT ?""",
            (MAX_JOB_ATTEMPTS, JOB_BATCH),
        ))
        if not jobs:
            return False
        ids = [row["chunk_id"] for row in jobs]
        placeholders = ",".join("?" for _ in ids)
        db.execute(
            f"UPDATE voice_jobs SET attempts=attempts+1 WHERE chunk_id IN ({placeholders})",
            ids,
        )
    saved = 0
    try:
        for chunk_id in ids:
            with inbox.connect() as db:
                saved += recover_chunk(db, chunk_id)
                auto_tag_chunks(db, [chunk_id])
                db.execute("DELETE FROM voice_jobs WHERE chunk_id=?", (chunk_id,))
        if saved:
            with inbox.connect() as db:
                enqueue_unlabeled(db, "recover")
    except Exception as exc:
        print(f"voice maintenance failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        try:
            with inbox.connect() as db:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE voice_jobs SET last_error=? WHERE chunk_id IN ({placeholders})",
                    (type(exc).__name__, *ids),
                )
        except Exception:
            print("voice maintenance could not record the failure", file=sys.stderr, flush=True)
    return True


def drain_voice_work(inbox, limit: int = 50) -> int:
    """Run maintenance to completion. Used by tests and a restarted receiver's worker."""
    steps = 0
    while steps < limit and step_voice_maintenance(inbox):
        steps += 1
    return steps


def voice_worker(inbox, stop) -> None:
    """One coalescing worker. Refreshes do not call this."""
    while not stop.is_set():
        try:
            worked = step_voice_maintenance(inbox)
        except Exception as exc:
            print(f"voice maintenance failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            stop.wait(2.0)
            continue
        if not worked:
            stop.wait(0.5)


def queue_reextract(inbox) -> int:
    """Queue completed clips that still lack turn-local vectors. Audio files are left in place."""
    queued = 0
    with inbox.connect() as db:
        rows = db.execute(
            """SELECT id, path FROM chunks
               WHERE status='complete' AND audio_state='present'
               AND voice_extract_version < ?
               AND diarization_status IN ('success','low_coverage','no_speech')""",
            (EXTRACTION_VERSION,),
        ).fetchall()
        for row in rows:
            if not Path(row["path"]).is_file():
                continue
            db.execute(
                """UPDATE chunks SET diarization_status='pending', diarization_retry_at=0, diarization_error=NULL
                   WHERE id=?""",
                (row["id"],),
            )
            queued += 1
    return queued
