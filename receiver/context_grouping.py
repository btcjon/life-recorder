"""Read-only sidebar projection of saved boundaries and current topic evidence.

Showing an inferred group never accepts it. Topic source_ids describes the full
model window, so validate that window before selecting the segment's anchors.
Participant-change proposals remain advisory in Context, not competing topics.
"""
from datetime import timedelta
import hashlib
import json

import event_edits
import meetings
import timeline
import viewer

MAX_TOPIC_WINDOW = 40
MAX_PROPOSALS = 500


def _event(identifier, source, title, rows):
    first, last = rows[0], rows[-1]
    return {
        'kind': 'event', 'id': identifier, 'source': source, 'title': title,
        'chunk_ids': [r['id'] for r in rows], 'clip_count': len(rows),
        'start_chunk_id': first['id'], 'end_chunk_id': last['id'],
        'started_local': viewer._local_text(viewer._chunk_start(first)),
        'ended_local': viewer._local_text(viewer._chunk_start(last)
                                        + timedelta(seconds=viewer._duration(last))),
        'duration': sum(viewer._duration(r) for r in rows),
        'preview': next((r['transcript'].strip() for r in rows if r['transcript'].strip()), ''),
        'revision': None,
    }


def _topic_blocks(db, names):
    if 'topic_suggestions' not in names:
        return []
    blocks, windows = [], {}
    query = """SELECT * FROM topic_suggestions WHERE state='proposed'
        AND model!='local-participant-rule-v1'
        ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT ?"""
    for row in db.execute(query, (MAX_PROPOSALS,)):
        try:
            ids = json.loads(row['source_ids'])
            if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TOPIC_WINDOW
                    or any(not isinstance(i, str) or not i for i in ids)
                    or len(set(ids)) != len(ids)):
                continue
            key = tuple(ids)
            if key not in windows:
                current = timeline.current_source(db, ids)
                windows[key] = (current, timeline.source_fingerprint(current))
            current, fingerprint = windows[key]
            if ([r['id'] for r in current] != ids or fingerprint != row['source_fingerprint']
                    or any(r['status'] != 'complete' for r in current)):
                continue
            # A topic worker window is device/day scoped. Never turn malformed
            # persisted metadata into a cross-day inferred conversation.
            days = {meetings.parse_utc(r['started']).astimezone(meetings.DISPLAY_ZONE).date()
                    for r in current}
            if len(days) != 1:
                continue
            title = row['title']
            if (not isinstance(title, str) or not title.strip() or len(title) > 80
                    or any(ord(c) < 32 for c in title)):
                continue
            start, end = ids.index(row['start_chunk_id']), ids.index(row['end_chunk_id'])
            if start > end:
                continue
            segment = [dict(r) for r in current[start:end + 1]]
            blocks.append(_event(row['id'], 'topic', ' '.join(title.split()), segment))
        except (ValueError, TypeError, KeyError, OverflowError):
            # Optional corrupt/stale topic metadata cannot hide source clips.
            continue
    return blocks


def project_blocks(db):
    """Each source occurs once, before day clipping or user filters. No writes."""
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = [dict(r) for r in db.execute(
        'SELECT id,device,started,duration,status,coalesce(transcript,\'\') AS transcript '
        'FROM chunks ORDER BY started,id')]
    ordered = event_edits._ordered(rows)
    # Preserve the established saved-edit metadata, stable IDs and cross-day
    # semantics; do not alter the machine index's grouping contract.
    edits = event_edits.load(db) if 'event_edits' in names else []
    manuals = [b for b in event_edits.overlay(ordered, edits) if b.get('source') == 'manual']
    claimed = set()
    chosen = []

    def choose(block):
        ids = block['chunk_ids']
        if not ids or claimed.intersection(ids):
            return False
        chosen.append(block)
        claimed.update(ids)
        return True

    for block in manuals:
        choose(block)
    for block in _topic_blocks(db, names):
        # Reject whole segments on any stronger overlap, never retitle scraps.
        choose(block)

    if 'intervals' in names:
        for interval in db.execute("SELECT * FROM intervals WHERE source='manual' ORDER BY started_at,id"):
            try:
                start = meetings.parse_utc(interval['started_at'])
                end = meetings.parse_utc(interval['ended_at'] or interval['deadline'])
                if end <= start:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            runs, run = [], []
            for row in ordered:
                stamp = viewer._chunk_start(row)
                contained = (stamp is not None and row['device'] == interval['device_id']
                             and stamp >= start
                             and stamp + timedelta(seconds=viewer._duration(row)) <= end)
                if contained and row['id'] not in claimed:
                    # Separate local days and device/interleaved/claimed gaps.
                    if run and meetings.parse_utc(run[-1]['started']).astimezone(meetings.DISPLAY_ZONE).date() != stamp.astimezone(meetings.DISPLAY_ZONE).date():
                        runs.append(run)
                        run = []
                    run.append(row)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            for span in runs:
                evidence = interval['id'] + ':' + ','.join(r['id'] for r in span)
                identifier = 'meeting_' + hashlib.sha256(evidence.encode()).hexdigest()[:32]
                label = interval['label'] or 'Marked meeting'
                choose(_event(identifier, 'meeting', label, span))

    positions = {r['id']: i for i, r in enumerate(ordered)}
    groups = {b['chunk_ids'][0]: b for b in chosen}
    output, uncovered = [], []

    def flush():
        for block in event_edits.overlay(uncovered, []):
            # Keep the legacy source='suggestion' contract for proximity-only
            # blocks. New clients label it Time group, never AI inference.
            if block.get('kind') == 'event':
                block['grouping_basis'] = 'time'
            output.append(block)
        uncovered.clear()

    for row in ordered:
        identifier = row['id']
        if identifier in claimed:
            flush()
            if identifier in groups:
                output.append(groups[identifier])
        else:
            if uncovered and row['device'] != uncovered[-1]['device']:
                flush()
            uncovered.append(row)
    flush()
    output.sort(key=lambda b: positions[(b.get('chunk_ids') or [b['chunk_id']])[0]])
    return output
