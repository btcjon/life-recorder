"""Read-only speaker review queue and held-out auto-label evaluation.

Suggestions are ranked with voice_id's existing score and margin rules.
Reading the queue does not assign a name, enroll a sample, or mark calibration
passed. Review payloads carry turn, group, clip, and time references only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from pathlib import Path

import voice_id

PAGE_DEFAULT = 20
PAGE_MAX = 50
_CURSOR_VERSION = 1
_ITEM_KEYS = (
    "turn_id", "group_id", "group_turn_ids", "chunk_id", "speaker_key",
    "clip_started", "started", "ended", "audio_usable", "quality", "clean_seconds",
    "label_source", "stored_person_id", "stored_name", "suggestions",
    "suggestion_score", "suggestion_margin", "reasons", "confirmed",
)


def reject_suggestion(db, turn_id: str, person_id: str) -> bool:
    """Remember that this person is not a suggestion for this turn.

    The stored label, samples, and calibration are left untouched.
    """
    if not isinstance(turn_id, str) or not isinstance(person_id, str):
        raise ValueError("rejection")
    if not turn_id or not person_id or len(turn_id) > 80 or len(person_id) > 80:
        raise ValueError("rejection")
    turn = db.execute("SELECT id FROM speaker_turns WHERE id=?", (turn_id,)).fetchone()
    person = db.execute("SELECT id FROM people WHERE id=?", (person_id,)).fetchone()
    if not turn or not person:
        return False
    db.execute(
        """INSERT OR IGNORE INTO speaker_suggestion_rejections(turn_id, person_id, created_at)
           VALUES (?,?,?)""",
        (turn_id, person_id, time.time()),
    )
    return True


def review_queue(db, limit: int = PAGE_DEFAULT, cursor: str | None = None) -> dict:
    """Page unlabeled and unconfirmed turns. Usable retained audio comes first.

    Order inside that band is clip start, offset, then turn id. The cursor is
    an opaque resume token for that order. Nothing is written.
    """
    bounded = _bounded_limit(limit)
    cursor_key = _decode_cursor(cursor) if cursor else None
    previous = _query_only(db)
    db.execute("PRAGMA query_only=ON")
    try:
        return _review_queue(db, bounded, cursor_key)
    finally:
        _restore_query_only(db, previous)


def evaluate_held_out(db) -> dict:
    """Score confirmed stretches with gallery samples from other recordings only.

    A probe recording never contributes samples or turns to its own gallery.
    Counts are coverage, matches, false matches, and rejections at the current
    auto-label thresholds. This does not lower those thresholds, write labels,
    or set voice_calibration to passed.
    """
    score = voice_id.AUTO_MIN_SCORE
    margin = voice_id.AUTO_MIN_MARGIN
    recorded = _calibration_row(db)
    previous = _query_only(db)
    db.execute("PRAGMA query_only=ON")
    try:
        report = _evaluate_held_out(db, recorded)
    finally:
        _restore_query_only(db, previous)
    if voice_id.AUTO_MIN_SCORE != score or voice_id.AUTO_MIN_MARGIN != margin:
        raise RuntimeError("auto-label thresholds changed")
    if _calibration_row(db) != recorded:
        raise RuntimeError("calibration record changed")
    return report


def diagnose_held_out(db) -> dict:
    """Count-only diagnostics for the existing auto-label rule; never alter it."""
    previous = _query_only(db)
    db.execute("PRAGMA query_only=ON")
    try:
        return _diagnose_held_out(db)
    finally:
        _restore_query_only(db, previous)


def _quantiles(values: list[float]) -> dict:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return {"p10": None, "p50": None, "p90": None}
    def pick(fraction):
        return round(ordered[round((len(ordered) - 1) * fraction)], 3)
    return {"p10": pick(0.1), "p50": pick(0.5), "p90": pick(0.9)}


def _diagnose_held_out(db) -> dict:
    reasons = {key: 0 for key in (
        "accepted", "no_clean_vector", "version_mismatch", "dimension_mismatch",
        "not_ready", "rank1_wrong", "score_below_0.85", "margin_below_0.10",
    )}
    true_scores, other_scores, group_true, group_other = [], [], [], []
    same_recording, raw_norms = [], []
    groups = vectors_seen = rank1_vectors = rank1_groups = 0
    chunk_ids = [row[0] for row in db.execute(
        "SELECT DISTINCT chunk_id FROM speaker_turns WHERE label_source='confirmed' AND person_id IS NOT NULL"
    )]
    for chunk_id in chunk_ids:
        turns = list(db.execute(
            "SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started,ended,id", (chunk_id,)
        ))
        profiles = voice_id.manual_profiles(db, exclude_chunk_ids={chunk_id})
        own_samples: dict[str, list[list[float]]] = {}
        for sample in db.execute(
            """SELECT s.person_id,s.embedding_json FROM voice_samples s
               JOIN speaker_turns t ON t.id=s.turn_id
               WHERE t.chunk_id=? AND COALESCE(s.legacy,0)=0
                 AND COALESCE(s.status,'accepted')='accepted'""", (chunk_id,),
        ):
            vector = voice_id.normalize_vector(json.loads(sample["embedding_json"]))
            if vector is not None:
                own_samples.setdefault(sample["person_id"], []).append(vector)
        for group in voice_id._review_groups(turns):
            people = {turn["person_id"] for turn in group}
            sources = {turn["label_source"] for turn in group}
            if sources != {"confirmed"} or len(people) != 1 or None in people:
                continue
            groups += 1
            truth = next(iter(people))
            turn_ids = [turn["id"] for turn in group]
            placeholders = ",".join("?" for _ in turn_ids)
            all_vectors = list(db.execute(
                f"SELECT embedding_json,extraction_version,legacy,timed,overlap FROM voice_vectors "
                f"WHERE turn_id IN ({placeholders})", turn_ids,
            ))
            current = voice_id._match_vectors(db, group)
            if not current:
                if all_vectors and all(int(row["extraction_version"]) != voice_id.EXTRACTION_VERSION for row in all_vectors):
                    reasons["version_mismatch"] += 1
                else:
                    reasons["no_clean_vector"] += 1
                continue
            if not (profiles.get(truth) or {}).get("ready"):
                reasons["not_ready"] += 1
                continue
            local_true, local_other = [], []
            local_rank1 = True
            malformed = False
            for row in current:
                try:
                    raw = json.loads(row["embedding_json"])
                    vector = voice_id.normalize_vector(raw)
                    if vector is None:
                        malformed = True
                        break
                    raw_norms.append(math.sqrt(sum(float(value) ** 2 for value in raw)))
                except (ValueError, TypeError, OverflowError):
                    malformed = True
                    break
                scores = {
                    person_id: max(voice_id.cosine(vector, sample) for sample in profile["vectors"])
                    for person_id, profile in profiles.items() if profile["vectors"]
                }
                if truth not in scores:
                    malformed = True
                    break
                own = scores[truth]
                other = max((score for person_id, score in scores.items() if person_id != truth), default=0.0)
                local_true.append(own)
                local_other.append(other)
                vectors_seen += 1
                if own > other:
                    rank1_vectors += 1
                else:
                    local_rank1 = False
                own_gallery = own_samples.get(truth) or []
                if own_gallery:
                    same_recording.append(max(voice_id.cosine(vector, sample) for sample in own_gallery))
            if malformed:
                reasons["dimension_mismatch"] += 1
                continue
            if local_rank1:
                rank1_groups += 1
            true_scores.extend(local_true)
            other_scores.extend(local_other)
            group_true.append(min(local_true))
            group_other.append(max(local_other))
            if not local_rank1:
                reasons["rank1_wrong"] += 1
            elif any(score < voice_id.AUTO_MIN_SCORE for score in local_true):
                reasons["score_below_0.85"] += 1
            elif any(own - other < voice_id.AUTO_MIN_MARGIN for own, other in zip(local_true, local_other)):
                reasons["margin_below_0.10"] += 1
            else:
                reasons["accepted"] += 1
    return {
        "groups": groups,
        "vectors": vectors_seen,
        "rank1_correct_vectors": rank1_vectors,
        "rank1_correct_groups": rank1_groups,
        "rejection_reasons": reasons,
        "true_score_quantiles": _quantiles(true_scores),
        "other_score_quantiles": _quantiles(other_scores),
        "group_min_true_quantiles": _quantiles(group_true),
        "group_max_other_quantiles": _quantiles(group_other),
        "same_recording_score_quantiles": _quantiles(same_recording),
        "raw_vector_norm_quantiles": _quantiles(raw_norms),
        "vector_source_provenance": "not_stored_per_vector",
        "min_score": voice_id.AUTO_MIN_SCORE,
        "min_margin": voice_id.AUTO_MIN_MARGIN,
    }


def _review_queue(db, limit: int, cursor_key) -> dict:
    rows = list(db.execute(
        """SELECT t.id, t.run_id, t.chunk_id, t.speaker_key, t.started, t.ended, t.quality,
                  t.person_id, t.label_source, c.started AS clip_started, c.path AS clip_path,
                  c.audio_state, p.name AS stored_name
           FROM speaker_turns t
           JOIN chunks c ON c.id = t.chunk_id
           LEFT JOIN people p ON p.id = t.person_id
           WHERE t.run_id = (
               SELECT r.id FROM speaker_runs r
               WHERE r.chunk_id = t.chunk_id
               ORDER BY r.created_at DESC, r.id DESC
               LIMIT 1
           )
             AND IFNULL(t.label_source, '') != 'confirmed'"""
    ))
    ranked = sorted(((_sort_key(row), row) for row in rows), key=lambda pair: pair[0])
    if cursor_key is not None:
        ranked = [pair for pair in ranked if pair[0] > cursor_key]
    page = ranked[:limit]
    more = len(ranked) > limit
    chunk_ids = list(dict.fromkeys(row["chunk_id"] for _key, row in page))
    groups = _group_index(db, chunk_ids)
    scratch = [{"id": row["id"]} for _key, row in page]
    details: dict = {}
    voice_id.annotate_turns(db, scratch, details)
    annotated = {item["id"]: item for item in scratch}
    clean = voice_id.eligible_clean_seconds(db, [row["id"] for _key, row in page])
    items = []
    for _key, row in page:
        group_id, members = groups.get(row["id"], (_group_id([row["id"]]), [row["id"]]))
        items.append(_item(row, group_id, members, annotated.get(row["id"], {}), details.get(row["id"], {}), clean.get(row["id"], 0.0)))
    _hide_rejections(db, items)
    _reject_private(items)
    next_cursor = _encode_cursor(page[-1][0]) if more and page else None
    return {"items": items, "next_cursor": next_cursor}


def _evaluate_held_out(db, recorded) -> dict:
    chunk_ids = [row[0] for row in db.execute(
        """SELECT DISTINCT chunk_id FROM speaker_turns
           WHERE label_source='confirmed' AND person_id IS NOT NULL
           ORDER BY chunk_id"""
    )]
    recordings = 0
    groups_seen = 0
    covered = 0
    matches = 0
    false_matches = 0
    rejections = 0
    for chunk_id in chunk_ids:
        turns = list(db.execute(
            """SELECT id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source
               FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id""",
            (chunk_id,),
        ))
        probes = []
        for group in voice_id._review_groups(turns):
            people = {turn["person_id"] for turn in group}
            sources = {turn["label_source"] for turn in group}
            if sources != {"confirmed"} or len(people) != 1 or None in people:
                continue
            probes.append((next(iter(people)), group))
        if not probes:
            continue
        recordings += 1
        profiles = voice_id.manual_profiles(db, exclude_chunk_ids={chunk_id})
        for truth, group in probes:
            decision = voice_id.score_group(db, group, profiles)
            groups_seen += 1
            if (profiles.get(truth) or {}).get("ready"):
                covered += 1
            if decision == truth:
                matches += 1
            elif decision is None:
                rejections += 1
            else:
                false_matches += 1
    status = recorded[0] if recorded is not None else None
    return {
        "split": "recording",
        "recordings": recordings,
        "groups": groups_seen,
        "covered": covered,
        "coverage": (covered / groups_seen) if groups_seen else 0.0,
        "matches": matches,
        "false_matches": false_matches,
        "rejections": rejections,
        "min_score": voice_id.AUTO_MIN_SCORE,
        "min_margin": voice_id.AUTO_MIN_MARGIN,
        "passed": False,
        "recorded_status": status,
    }


def _item(row, group_id: str, members: list[str], annotated: dict, info: dict, clean_seconds: float) -> dict:
    automatic = row["label_source"] == "automatic"
    item = {
        "turn_id": row["id"],
        "group_id": group_id,
        "group_turn_ids": list(members),
        "chunk_id": row["chunk_id"],
        "speaker_key": row["speaker_key"],
        "clip_started": row["clip_started"],
        "started": float(row["started"]),
        "ended": float(row["ended"]),
        "audio_usable": _audio_usable(row),
        "quality": None if row["quality"] is None else float(row["quality"]),
        "clean_seconds": round(float(clean_seconds), 3),
        "label_source": row["label_source"],
        "stored_person_id": row["person_id"] if automatic else None,
        "stored_name": row["stored_name"] if automatic else None,
        "suggestions": [dict(suggestion) for suggestion in info.get("suggestions") or ()],
        "suggestion_score": annotated.get("suggestion_score"),
        "suggestion_margin": annotated.get("suggestion_margin"),
        "reasons": list(annotated.get("suggestion_reasons") or info.get("reasons") or ()),
        "confirmed": False,
    }
    return {key: item[key] for key in _ITEM_KEYS}


def _group_index(db, chunk_ids: list[str]) -> dict[str, tuple[str, list[str]]]:
    index = {}
    for chunk_id in chunk_ids:
        turns = list(db.execute(
            """SELECT id, run_id, chunk_id, speaker_key, started, ended, person_id, label_source
               FROM speaker_turns WHERE chunk_id=? ORDER BY started, ended, id""",
            (chunk_id,),
        ))
        for group in voice_id._review_groups(turns):
            members = sorted(turn["id"] for turn in group)
            digest = _group_id(members)
            for turn_id in members:
                index[turn_id] = (digest, members)
    return index


def _group_id(turn_ids: list[str]) -> str:
    joined = ",".join(sorted(str(turn_id) for turn_id in turn_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _audio_usable(row) -> bool:
    if row["audio_state"] != "present":
        return False
    path = row["clip_path"]
    return bool(path) and Path(path).is_file()


def _sort_key(row):
    clip = row["clip_started"] if isinstance(row["clip_started"], str) and row["clip_started"] else "~"
    try:
        offset = float(row["started"])
    except (TypeError, ValueError):
        offset = 0.0
    if not math.isfinite(offset):
        offset = 0.0
    return (0 if _audio_usable(row) else 1, clip, round(offset, 6), str(row["id"]))


def _bounded_limit(limit) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit")
    if limit < 1:
        raise ValueError("limit")
    return min(limit, PAGE_MAX)


def _encode_cursor(key) -> str:
    payload = {"v": _CURSOR_VERSION, "a": key[0], "c": key[1], "s": key[2], "t": key[3]}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ValueError("cursor")
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("cursor") from error
    if not isinstance(payload, dict) or set(payload) != {"v", "a", "c", "s", "t"}:
        raise ValueError("cursor")
    if payload["v"] != _CURSOR_VERSION or payload["a"] not in (0, 1):
        raise ValueError("cursor")
    if not isinstance(payload["c"], str) or not isinstance(payload["t"], str) or not payload["t"]:
        raise ValueError("cursor")
    if isinstance(payload["s"], bool) or not isinstance(payload["s"], (int, float)):
        raise ValueError("cursor")
    offset = float(payload["s"])
    if not math.isfinite(offset):
        raise ValueError("cursor")
    return (payload["a"], payload["c"], offset, payload["t"])


def _query_only(db) -> int:
    return int(db.execute("PRAGMA query_only").fetchone()[0])


def _restore_query_only(db, previous: int) -> None:
    db.execute("PRAGMA query_only=" + ("ON" if previous else "OFF"))


def _calibration_row(db):
    row = db.execute("SELECT status, updated_at FROM voice_calibration WHERE id=1").fetchone()
    if row is None:
        return None
    return (row["status"], row["updated_at"])


def _hide_rejections(db, items: list[dict]) -> None:
    """Drop a rejected person from later reads. Does not change stored labels."""
    if not items:
        return
    turn_ids = [item["turn_id"] for item in items]
    placeholders = ",".join("?" for _ in turn_ids)
    blocked: dict[str, set[str]] = {turn_id: set() for turn_id in turn_ids}
    for row in db.execute(
        f"SELECT turn_id, person_id FROM speaker_suggestion_rejections WHERE turn_id IN ({placeholders})",
        turn_ids,
    ):
        blocked.setdefault(row["turn_id"], set()).add(row["person_id"])
    for item in items:
        denied = blocked.get(item["turn_id"]) or set()
        if not denied:
            continue
        item["suggestions"] = [
            dict(suggestion) for suggestion in item["suggestions"]
            if suggestion.get("person_id") not in denied
        ]
        if item.get("stored_person_id") in denied:
            item["stored_person_id"] = None
            item["stored_name"] = None
        if item["suggestions"]:
            item["suggestion_score"] = item["suggestions"][0]["score"]
            item["suggestion_margin"] = item["suggestions"][0]["margin"]
        else:
            item["suggestion_score"] = None
            item["suggestion_margin"] = None
            item["reasons"] = [
                reason for reason in item["reasons"]
                if reason not in {
                    "needs_confirmation", "score_below_0.85",
                    "margin_below_0.10", "score_below_0.60",
                }
            ]


def _reject_private(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if "embedding" in lowered or "transcript" in lowered or lowered in {"text", "words", "word"}:
                raise RuntimeError("private review field")
            _reject_private(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private(item)
