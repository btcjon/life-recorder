"""Local participant-change candidates, never evidence that a meeting ended.

Two adjacent clips on each side must agree on nonempty, disjoint sets of
human-confirmed people. Unknown/overlapping identities do not create a boundary.
Refresh runs during reconciliation; timeline reads never call this rule.
"""
import hashlib
import json
import math

import meetings
import timeline

MODEL = "local-participant-rule-v1"
PROMPT_VERSION = "participant-change-v1"
MAX_CLIPS = 400
MAX_GAP_SECONDS = 120


def _groups(rows):
    devices = {}
    for row in rows:
        devices.setdefault(row["device"], []).append(row)
    for rows in devices.values():
        group = []
        previous_end = previous_day = None
        for row in sorted(rows, key=lambda r: (r["started"], r["id"])):
            try:
                start = meetings.parse_utc(row["started"])
                duration = float(row["duration"])
                if not math.isfinite(duration) or duration <= 0 or row["status"] != "complete":
                    raise ValueError()
                day = start.astimezone(meetings.DISPLAY_ZONE).date()
                end = meetings.chunk_span(row)[1]
            except (TypeError, ValueError, OverflowError):
                if group:
                    yield group
                group, previous_end, previous_day = [], None, None
                continue
            gap = (start - previous_end).total_seconds() if previous_end else None
            if group and (day != previous_day or gap is None or not 0 <= gap <= MAX_GAP_SECONDS):
                yield group
                group = []
            group.append(row)
            previous_end, previous_day = end, day
        if group:
            yield group


def _confirmed_sets(db, ids):
    placeholders = ",".join("?" for _ in ids)
    sets = {identifier: set() for identifier in ids}
    for row in db.execute(f"""SELECT DISTINCT chunk_id,person_id FROM speaker_turns
        WHERE chunk_id IN ({placeholders}) AND label_source='confirmed'
        AND person_id IS NOT NULL""", ids):
        sets[row["chunk_id"]].add(row["person_id"])
    return sets


def _segments(rows, identities):
    sets = [identities[r["id"]] for r in rows]
    cuts = []
    for at in range(2, len(rows) - 1):
        left, right = sets[at - 1], sets[at]
        if left and right and left.isdisjoint(right) and sets[at - 2] == left and sets[at + 1] == right:
            cuts.append(at)
    if not cuts:
        return []
    boundaries = [0] + cuts + [len(rows)]
    return [{"start_clip_id": rows[first]["id"], "end_clip_id": rows[last - 1]["id"],
             "title": "Participant change candidate"}
            for first, last in zip(boundaries, boundaries[1:])]


def refresh(db):
    """Publish at most 400 clips' local proposals, keeping every source clip.

    Schemas must already exist. Human edits protect their complete containing
    group; proposals remain revisioned suggestions requiring explicit acceptance.
    """
    rows = db.execute("""SELECT id,device,started,duration,status,transcript FROM chunks
        ORDER BY started DESC,id DESC LIMIT ?""", (MAX_CLIPS,)).fetchall()
    if not rows:
        return {"group_count": 0, "suggestion_count": 0, "changes": 0}
    identities = _confirmed_sets(db, [r["id"] for r in rows])
    # Indexed anchor joins avoid enumerating the full clip ledger just to detect
    # existing human spans; comparisons include the deterministic ID tie-break.
    protected = set()
    for row in rows:
        saved = db.execute("""SELECT 1 FROM event_edits e JOIN chunks first ON first.id=e.start_chunk_id
            JOIN chunks last ON last.id=e.end_chunk_id WHERE (first.started,first.id)<=(?,?)
            AND (last.started,last.id)>=(?,?) LIMIT 1""",
            (row["started"], row["id"], row["started"], row["id"])).fetchone()
        if saved:
            protected.add(row["id"])
    before = db.total_changes
    group_count = suggestion_count = 0
    for group in _groups(rows):
        ids = [r["id"] for r in group]
        parts = [] if protected.intersection(ids) else _segments(group, identities)
        # Include confirmed identities in provenance even when a deleted label
        # removes a previously proposed boundary.
        evidence = {"source": timeline.source_fingerprint(group), "identities": [
            [identifier, sorted(identities[identifier])] for identifier in ids],
            "model": MODEL, "prompt": PROMPT_VERSION}
        fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not parts:
            # Empty publications supersede stale local proposals, without
            # inventing a whole-group candidate where there is no change.
            old = db.execute("SELECT 1 FROM topic_suggestions WHERE model=? AND state='proposed' AND fingerprint!=? LIMIT 1",
                             (MODEL, fingerprint)).fetchone()
            if not old:
                continue
        published = timeline.publish_suggestions(db, fingerprint=fingerprint, rows=group, segments=parts,
            model=MODEL, prompt_version=PROMPT_VERSION)
        group_count += 1
        suggestion_count += len(published)
    return {"group_count": group_count, "suggestion_count": suggestion_count, "changes": db.total_changes - before}
