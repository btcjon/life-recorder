"""Revisioned human identity edits and explicitly gated anonymous proposals.

Call migrate during schema migration, and mutations inside the caller's locked
SQLite transaction. These functions do not commit, enroll from clusters, or
perform background propagation. Private undo evidence stays in SQLite.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager

import voice_id

MAX_SELECTION = 200
MAX_AFFECTED = 5000
ALGORITHM = "eligible-vector-max-cosine-v1"


class Conflict(ValueError):
    """The caller's revision or undo evidence is stale."""


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS speaker_identity_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)")
    db.execute("INSERT OR IGNORE INTO speaker_identity_state VALUES(1,0)")
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_identity_changes (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL,
        scope_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        created_at REAL NOT NULL, undone_by TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_cluster_evaluation (
        id INTEGER PRIMARY KEY CHECK(id=1), report_json TEXT NOT NULL, updated_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_sample_vectors (
        id TEXT PRIMARY KEY, sample_id TEXT NOT NULL, vector_id TEXT NOT NULL,
        UNIQUE(sample_id,vector_id))""")


def revision(db):
    row = db.execute("SELECT revision FROM speaker_identity_state WHERE id=1").fetchone()
    return int(row[0])


def _ids(values):
    if not isinstance(values, list) or not values or len(values) > MAX_SELECTION:
        raise ValueError("Select 1 to 200 items")
    if any(not isinstance(v, str) or not v or len(v) > 100 for v in values):
        raise ValueError("Invalid selection")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate selection")
    return values


def _rows(db, table, field, ids):
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {field} IN ({marks}) ORDER BY id", ids)]


def _turns(db, ids):
    rows = _rows(db, "speaker_turns", "id", _ids(ids))
    if len(rows) != len(ids):
        raise ValueError("Speaker turn not found")
    return rows


@contextmanager
def _transaction(db, expected):
    if type(expected) is not int or expected < 0:
        raise ValueError("Revision required")
    if not db.in_transaction:
        db.execute('BEGIN')
    db.execute("SAVEPOINT identity_mutation")
    try:
        # The write locks before checking a revision, including deferred transactions.
        db.execute("UPDATE speaker_identity_state SET revision=revision WHERE id=1")
        if revision(db) != expected:
            raise Conflict("Speaker identities changed; reload")
        yield
    except Exception:
        db.execute("ROLLBACK TO identity_mutation")
        db.execute("RELEASE identity_mutation")
        raise
    else:
        db.execute("RELEASE identity_mutation")


def _scope(db, turns, tracks=()):
    vectors = _rows(db, "voice_vectors", "turn_id", turns)
    vector_ids = [v['id'] for v in vectors]
    assignments = _rows(db, "voice_assignments", "vector_id", vector_ids)
    track_ids = sorted(set(tracks) | {a['track_id'] for a in assignments if a['track_id']})
    external = []
    for track_id in track_ids:
        external.extend(dict(r) for r in db.execute("SELECT * FROM voice_assignments WHERE track_id=? AND active=1 ORDER BY id", (track_id,)) if r['vector_id'] not in vector_ids)
    return {'turns': sorted(turns), 'tracks': track_ids, 'external_assignments': external}


def _snapshot(db, scope):
    vectors = _rows(db, "voice_vectors", "turn_id", scope['turns'])
    samples = _rows(db, 'voice_samples', 'turn_id', scope['turns'])
    return {
        'speaker_turns': _rows(db, 'speaker_turns', 'id', scope['turns']),
        'voice_samples': samples,
        'speaker_sample_vectors': _rows(db, 'speaker_sample_vectors', 'sample_id', [s['id'] for s in samples]),
        'voice_vectors': vectors,
        'voice_assignments': _rows(db, 'voice_assignments', 'vector_id', [v['id'] for v in vectors]),
        'voice_tracks': _rows(db, 'voice_tracks', 'id', scope['tracks']),
    }


def _save(db, kind, scope, before):
    change_id = str(uuid.uuid4())
    new_revision = revision(db) + 1
    after = _snapshot(db, scope)
    db.execute("INSERT INTO speaker_identity_changes VALUES(?,?,?,?,?,?,?,NULL)",
               (change_id, new_revision, kind, json.dumps(scope), json.dumps(before), json.dumps(after), time.time()))
    db.execute("UPDATE speaker_identity_state SET revision=? WHERE id=1", (new_revision,))
    return {'change_id': change_id, 'revision': new_revision,
            'chunk_ids': sorted({t['chunk_id'] for t in before['speaker_turns']}), 'kind': kind}


def _invalidate_automatic(db, people):
    turns = []
    for person in people:
        if person:
            turns.extend(r[0] for r in db.execute("SELECT id FROM speaker_turns WHERE person_id=? AND label_source='automatic'", (person,)))
    if len(turns) > MAX_AFFECTED:
        raise ValueError("Too many dependent labels; use maintenance")
    return sorted(set(turns))


def _clear_auto(db, ids):
    for turn_id in ids:
        db.execute("UPDATE speaker_turns SET person_id=NULL,label_source=NULL WHERE id=? AND label_source='automatic'", (turn_id,))


def change_turns(db, turn_ids, person_id, use_sample=False, expected_revision=None):
    """Correct or remove selected labels. Enrollment needs an explicit boolean opt-in."""
    if type(use_sample) is not bool or (person_id is None and use_sample):
        raise ValueError("Invalid enrollment choice")
    if person_id is not None and (not isinstance(person_id, str) or not db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone()):
        raise ValueError("Person not found")
    with _transaction(db, expected_revision):
        turns = _turns(db, turn_ids)
        withdrawing = [t['id'] for t in turns if use_sample or t['person_id'] != person_id or person_id is None]
        old_people = {t['person_id'] for t in turns if t['id'] in withdrawing}
        affected_samples = {s['id']: s for s in _rows(db, 'voice_samples', 'turn_id', withdrawing)}
        for turn_id in withdrawing:
            for s in db.execute("""SELECT DISTINCT s.* FROM voice_samples s JOIN speaker_sample_vectors p ON p.sample_id=s.id
                JOIN voice_vectors v ON v.id=p.vector_id WHERE v.turn_id=?""", (turn_id,)):
                affected_samples[s['id']] = dict(s)
        enrollment_turns = set(withdrawing) | {s['turn_id'] for s in affected_samples.values()}
        for sample_id in affected_samples:
            enrollment_turns.update(r[0] for r in db.execute("""SELECT DISTINCT v.turn_id FROM voice_vectors v JOIN speaker_sample_vectors p ON p.vector_id=v.id WHERE p.sample_id=?""", (sample_id,)))
        old_people.update(s['person_id'] for s in affected_samples.values())
        auto = _invalidate_automatic(db, old_people)
        scope = _scope(db, sorted(set(turn_ids) | set(auto) | enrollment_turns))
        before = _snapshot(db, scope)
        for sample_id in affected_samples:
            db.execute('DELETE FROM speaker_sample_vectors WHERE sample_id=?', (sample_id,))
            db.execute('DELETE FROM voice_samples WHERE id=?', (sample_id,))
        for turn_id in turn_ids:
            db.execute("UPDATE speaker_turns SET person_id=?, label_source=? WHERE id=?", (person_id, 'confirmed' if person_id else None, turn_id))
        voice_id.clear_enrollment(db, sorted(enrollment_turns))
        _clear_auto(db, auto)
        enrollment = {'enrolled': False, 'reason': 'not_requested'}
        if use_sample:
            if len({t['chunk_id'] for t in turns}) != 1:
                raise ValueError("Enroll one recording at a time")
            enrollment = voice_id.enroll_turns(db, turns, person_id)
            if enrollment.get('enrolled'):
                sample = db.execute('SELECT id FROM voice_samples WHERE turn_id=?', (turns[0]['id'],)).fetchone()
                for vector in _rows(db, 'voice_vectors', 'turn_id', turn_ids):
                    if vector['enrolled'] and vector['person_id'] == person_id:
                        db.execute('INSERT INTO speaker_sample_vectors VALUES(?,?,?)', (str(uuid.uuid4()), sample['id'], vector['id']))
        for chunk_id in {t['chunk_id'] for t in turns}:
            db.execute("INSERT OR IGNORE INTO voice_recover_skip VALUES(?)", (chunk_id,))
        voice_id.refresh_tracks(db, set(scope['tracks']))
        result = _save(db, 'label' if person_id else 'remove_label', scope, before)
        result['enrollment'] = enrollment
        return result


def remove_sample(db, sample_id, expected_revision=None):
    with _transaction(db, expected_revision):
        sample = db.execute("SELECT * FROM voice_samples WHERE id=?", (sample_id,)).fetchone()
        if not sample:
            raise ValueError("Sample not found")
        # Existing enrollment can cover several adjacent turns in one review stretch.
        from receiver import review_groups
        turn = db.execute("SELECT * FROM speaker_turns WHERE id=?", (sample['turn_id'],)).fetchone()
        if not turn:
            raise ValueError("Sample provenance missing")
        peers = list(db.execute("SELECT * FROM speaker_turns WHERE chunk_id=? ORDER BY started,ended,id", (turn['chunk_id'],)))
        selected = next((g for g in review_groups(peers) if any(t['id'] == turn['id'] for t in g)), [turn])
        selected_ids = [t['id'] for t in selected]
        owned = [r[0] for r in db.execute('SELECT vector_id FROM speaker_sample_vectors WHERE sample_id=?', (sample_id,))]
        others = [s for s in _rows(db, 'voice_samples', 'turn_id', selected_ids) if s['id'] != sample_id]
        if owned:
            selected_ids = sorted({turn['id']} | {v['turn_id'] for v in _rows(db, 'voice_vectors', 'id', owned)})
        elif others:
            selected_ids = [turn['id']]
        auto = _invalidate_automatic(db, {sample['person_id']})
        scope = _scope(db, sorted(set(selected_ids) | set(auto)))
        before = _snapshot(db, scope)
        db.execute("DELETE FROM speaker_sample_vectors WHERE sample_id=?", (sample_id,))
        db.execute("DELETE FROM voice_samples WHERE id=?", (sample_id,))
        # Clear the enrolled vectors backing this sample but preserve all human labels.
        if owned:
            for vector_id in owned:
                db.execute('UPDATE voice_vectors SET enrolled=0,person_id=NULL WHERE id=?', (vector_id,))
        else:
            voice_id.clear_enrollment(db, selected_ids)
        _clear_auto(db, auto)
        db.execute("INSERT OR IGNORE INTO voice_recover_skip VALUES(?)", (turn['chunk_id'],))
        voice_id.refresh_tracks(db, set(scope['tracks']))
        return _save(db, 'remove_sample', scope, before)


def undo(db, change_id, expected_revision=None):
    with _transaction(db, expected_revision):
        row = db.execute("SELECT * FROM speaker_identity_changes WHERE id=?", (change_id,)).fetchone()
        if not row or row['undone_by'] or row['revision'] != revision(db):
            raise Conflict("Only the latest unchanged identity edit can be undone")
        scope = json.loads(row['scope_json'])
        after, before = json.loads(row['after_json']), json.loads(row['before_json'])
        if _snapshot(db, scope) != after:
            raise Conflict("Identity evidence changed; undo unavailable")
        # Protect tracks from new members written by background processing.
        vector_ids = {v['id'] for v in after['voice_vectors']}
        external = []
        for track_id in scope['tracks']:
            external.extend(dict(r) for r in db.execute("SELECT * FROM voice_assignments WHERE track_id=? AND active=1 ORDER BY id", (track_id,)) if r['vector_id'] not in vector_ids)
        if external != scope.get('external_assignments', []):
            raise Conflict("Cluster membership changed; undo unavailable")
        for table in ('voice_assignments', 'voice_samples', 'speaker_sample_vectors', 'voice_vectors', 'speaker_turns', 'voice_tracks'):
            for item in after[table]:
                if not any(old['id'] == item['id'] for old in before[table]):
                    db.execute(f"DELETE FROM {table} WHERE id=?", (item['id'],))
            for item in before[table]:
                columns = list(item)
                marks = ','.join('?' for _ in columns)
                updates = ','.join(f'{col}=excluded.{col}' for col in columns if col != 'id')
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", [item[c] for c in columns])
        result = _save(db, 'undo', scope, after)
        db.execute("UPDATE speaker_identity_changes SET undone_by=? WHERE id=?", (result['change_id'], change_id))
        return result


def profiles(db):
    """Read readiness and sample provenance without exposing voice vectors."""
    manual = voice_id.manual_profiles(db)
    result = []
    for person in db.execute("SELECT id,name FROM people ORDER BY name COLLATE NOCASE,id"):
        p = manual.get(person['id'], {'samples': 0, 'clips': set(), 'seconds': 0, 'ready': False})
        samples = [dict(r) for r in db.execute("""SELECT s.id,s.turn_id,t.chunk_id,c.started AS clip_started,s.duration,s.confirmed_at,s.status,s.legacy
            FROM voice_samples s LEFT JOIN speaker_turns t ON t.id=s.turn_id LEFT JOIN chunks c ON c.id=t.chunk_id WHERE s.person_id=? ORDER BY s.confirmed_at,s.id LIMIT 200""", (person['id'],))]
        result.append({'id': person['id'], 'name': person['name'], 'sample_count': p['samples'],
                       'clip_count': len(p['clips']), 'sample_seconds': round(p['seconds'], 3),
                       'enrollment_ready': p['ready'], 'enrollment_reasons': voice_id.enrollment_reasons(p['samples'], len(p['clips']), p['seconds']), 'samples': samples})
    return {'revision': revision(db), 'people': result}


def _eligible(db, track_id):
    return [dict(r) for r in db.execute("""SELECT DISTINCT v.* FROM voice_vectors v
        JOIN voice_assignments a ON a.vector_id=v.id AND a.active=1 AND a.state='assigned'
        WHERE a.track_id=? AND v.timed=1 AND v.legacy=0 AND v.overlap=0
        AND v.duration>=? AND v.extraction_version=? ORDER BY v.created_at,v.id LIMIT 64""", (track_id, voice_id.MIN_CLEAN_SECONDS, voice_id.EXTRACTION_VERSION))
            if voice_id.normalize_vector(json.loads(r['embedding_json'])) is not None]


def clusters(db, limit=100):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid limit")
    result = []
    for track in db.execute("SELECT * FROM voice_tracks ORDER BY created_at DESC,id LIMIT ?", (limit,)):
        members = [dict(r) for r in db.execute("""SELECT DISTINCT t.id AS turn_id,t.chunk_id,t.started,t.ended,t.person_id,t.label_source
            FROM speaker_turns t JOIN voice_vectors v ON v.turn_id=t.id JOIN voice_assignments a ON a.vector_id=v.id
            WHERE a.track_id=? AND a.active=1 AND a.state='assigned' ORDER BY t.chunk_id,t.started,t.id""", (track['id'],))]
        if members:
            result.append({'id': track['id'], 'status': track['status'], 'members': members,
                           'eligible_vectors': len(_eligible(db, track['id']))})
    return {'revision': revision(db), 'clusters': result}


def evaluation_gate(db):
    row = db.execute("SELECT report_json FROM speaker_cluster_evaluation WHERE id=1").fetchone()
    if not row:
        return {'enabled': False, 'reason': 'evaluation_unavailable'}
    report = json.loads(row[0])
    if (not isinstance(report, dict) or not isinstance(report.get('categories'), dict)
            or any(type(report.get(k)) is not int for k in ('false_assignments', 'correct_assignments', 'unknown_cases'))
            or any(type(report['categories'].get(c)) is not int for c in ('noise', 'distance', 'overlap', 'short', 'unknown'))):
        return {'enabled': False, 'reason': 'evaluation_not_passed'}
    valid = (report.get('algorithm') == ALGORITHM and report.get('extraction_version') == voice_id.EXTRACTION_VERSION
             and report.get('consent') is True and report.get('separated_clips') is True
             and report.get('separated_sessions') is True and report.get('status') == 'passed'
             and report.get('false_assignments') == 0 and report.get('correct_assignments', 0) >= 10
             and report.get('unknown_cases', 0) >= 10
             and report.get('thresholds') == {'score': voice_id.AUTO_MIN_SCORE, 'margin': voice_id.AUTO_MIN_MARGIN}
             and isinstance(report.get('manifest_sha256'), str)
             and re.fullmatch('[0-9a-f]{64}', report['manifest_sha256']) is not None
             and report.get('enrollment_references_ready') is True
             and all(report.get('categories', {}).get(c, 0) >= 2 for c in ('noise', 'distance', 'overlap', 'short', 'unknown')))
    return {'enabled': bool(valid), 'reason': 'evaluated' if valid else 'evaluation_not_passed'}


def record_evaluation(db, report):
    """Explicit administrator integration point; never called by a day read."""
    if not isinstance(report, dict) or len(json.dumps(report)) > 16000:
        raise ValueError("Invalid evaluation report")
    db.execute("INSERT INTO speaker_cluster_evaluation VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET report_json=excluded.report_json,updated_at=excluded.updated_at", (json.dumps(report), time.time()))


def proposals(db, limit=100):
    gate = evaluation_gate(db)
    if not gate['enabled']:
        return {'revision': revision(db), 'gate': gate, 'proposals': []}
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid limit")
    # Bound pair work independently of the list endpoint; these are optional
    # suggestions, never a complete inventory or a background mutation.
    tracks = clusters(db, min(limit, 30))['clusters']
    vectors = {t['id']: _eligible(db, t['id']) for t in tracks if t['status'] == 'open'}
    normalized = {track_id: [voice_id.normalize_vector(json.loads(v['embedding_json'])) for v in records[:16]]
                  for track_id, records in vectors.items()}
    result, seen = [], set()
    for track in tracks:
        left = vectors.get(track['id'], [])
        if not left:
            continue
        ranked = []
        for other in tracks:
            right = vectors.get(other['id'], [])
            if other['id'] == track['id'] or not right or {v['chunk_id'] for v in left} & {v['chunk_id'] for v in right}:
                continue
            identities = {m['person_id'] for m in track['members'] + other['members'] if m['person_id'] and m['label_source'] == 'confirmed'}
            if len(identities) > 1:
                continue
            score = max(sum(l[i] * r[i] for i in range(voice_id.EMBEDDING_DIM))
                        for l in normalized[track['id']] for r in normalized[other['id']])
            ranked.append((score, other['id']))
        ranked.sort(reverse=True)
        if not ranked:
            continue
        score, other_id = ranked[0]
        margin = score - ranked[1][0] if len(ranked) > 1 else score
        pair = tuple(sorted((track['id'], other_id)))
        if score >= voice_id.AUTO_MIN_SCORE and margin >= voice_id.AUTO_MIN_MARGIN and pair not in seen:
            seen.add(pair)
            result.append({'track_ids': list(pair), 'score': round(score, 3), 'margin': round(margin, 3), 'state': 'proposed', 'enrolls': False})
    return {'revision': revision(db), 'gate': gate, 'proposals': result}


def merge(db, track_ids, expected_revision=None):
    """A human accepts a grouping, which never labels or enrolls its members."""
    with _transaction(db, expected_revision):
        track_ids = _ids(track_ids)
        if len(track_ids) < 2:
            raise ValueError("Select at least two clusters")
        tracks = _rows(db, 'voice_tracks', 'id', track_ids)
        if len(tracks) != len(track_ids):
            raise ValueError("Cluster not found")
        members = []
        for track_id in track_ids:
            members.extend(r[0] for r in db.execute("""SELECT DISTINCT v.turn_id FROM voice_vectors v JOIN voice_assignments a ON a.vector_id=v.id WHERE a.track_id=? AND a.active=1 AND a.state='assigned'""", (track_id,)))
        members = sorted(set(members))
        turns = _turns(db, members)
        identities = {t['person_id'] for t in turns if t['person_id'] and t['label_source'] == 'confirmed'}
        if len(identities) > 1 or any(t['status'] == 'frozen' for t in tracks):
            raise Conflict("Conflicting human identities; correct or split first")
        scope = _scope(db, members, track_ids)
        before = _snapshot(db, scope)
        target = track_ids[0]
        for assignment in before['voice_assignments']:
            if assignment['active'] and assignment['state'] == 'assigned' and assignment['track_id'] in track_ids:
                db.execute("UPDATE voice_assignments SET active=0,state='retracted' WHERE id=?", (assignment['id'],))
                voice_id._assignment(db, assignment['vector_id'], target, 'assigned', {'rule': 'human_merge'}, time.time())
        voice_id.refresh_tracks(db, set(track_ids))
        return _save(db, 'merge', scope, before)


def split(db, track_id, turn_ids, expected_revision=None):
    """Move selected complete turns to a new anonymous cluster."""
    with _transaction(db, expected_revision):
        selected = set(_ids(turn_ids))
        assignments = [dict(r) for r in db.execute("""SELECT a.*,v.turn_id FROM voice_assignments a JOIN voice_vectors v ON v.id=a.vector_id WHERE a.track_id=? AND a.active=1 AND a.state='assigned'""", (track_id,))]
        members = {a['turn_id'] for a in assignments}
        if not selected < members:
            raise ValueError("Select a proper subset of cluster turns")
        new_id = str(uuid.uuid4())
        scope = _scope(db, sorted(members), [track_id, new_id])
        before = _snapshot(db, scope)
        chunk = next(t['chunk_id'] for t in before['speaker_turns'] if t['id'] in selected)
        db.execute("INSERT INTO voice_tracks VALUES(?,?,'open',NULL,NULL,?)", (new_id, chunk, time.time()))
        for assignment in assignments:
            if assignment['turn_id'] in selected:
                db.execute("UPDATE voice_assignments SET active=0,state='retracted' WHERE id=?", (assignment['id'],))
                voice_id._assignment(db, assignment['vector_id'], new_id, 'assigned', {'rule': 'human_split'}, time.time())
        voice_id.refresh_tracks(db, {track_id, new_id})
        result = _save(db, 'split', scope, before)
        result['track_id'] = new_id
        return result


def name_selected(db, track_id, turn_ids, person_id, expected_revision=None):
    selected = _ids(turn_ids)
    with _transaction(db, expected_revision):
        for turn_id in selected:
            if not db.execute("""SELECT 1 FROM voice_vectors v JOIN voice_assignments a ON a.vector_id=v.id WHERE v.turn_id=? AND a.track_id=? AND a.active=1 AND a.state='assigned'""", (turn_id, track_id)).fetchone():
                raise ValueError("Turn is not a cluster member")
        return change_turns(db, selected, person_id, use_sample=False, expected_revision=expected_revision)
