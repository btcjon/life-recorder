#!/usr/bin/env python3
"""Read-only consented held-out voice evaluation; emit aggregate results only.

Manifest: {"consent": true, "references": [{"turn_id": "...", "identity":
"pseudonym", "session": "training-session"}], "cases": [{"turn_id": "...",
"identity": "pseudonym or null", "session": "held-out-session", "category":
"known|unknown|noise|overlap|short"}]}. No audio or transcript is emitted.
Saving this output does not activate propagation; importing it is explicit.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import speaker_identity
import voice_id

CATEGORIES = {'known', 'unknown', 'noise', 'overlap', 'short'}


def _vectors(db, turn_id):
    rows = db.execute("""SELECT embedding_json,duration FROM voice_vectors WHERE turn_id=?
        AND timed=1 AND legacy=0 AND overlap=0 AND extraction_version=? AND duration>=?""",
                      (turn_id, voice_id.EXTRACTION_VERSION, voice_id.MIN_CLEAN_SECONDS))
    return [vector for row in rows if (vector := voice_id.normalize_vector(json.loads(row['embedding_json']))) is not None]


def evaluate(db, manifest):
    unavailable = {'status': 'unavailable', 'algorithm': speaker_identity.ALGORITHM,
                   'extraction_version': voice_id.EXTRACTION_VERSION, 'consent': False,
                   'reason': 'explicit_consent_and_held_out_manifest_required'}
    if not isinstance(manifest, dict) or manifest.get('consent') is not True:
        return unavailable
    refs, cases = manifest.get('references'), manifest.get('cases')
    if not isinstance(refs, list) or not isinstance(cases, list) or not 2 <= len(refs) <= 200 or not 1 <= len(cases) <= 1000:
        return dict(unavailable, consent=True, reason='invalid_manifest')
    entries = refs + cases
    if any(not isinstance(e, dict) or not isinstance(e.get('turn_id'), str) or not isinstance(e.get('session'), str) or not e['session'] or len(e['session']) > 100 for e in entries):
        return dict(unavailable, consent=True, reason='invalid_manifest')
    if len({e['turn_id'] for e in entries}) != len(entries):
        return dict(unavailable, consent=True, reason='duplicate_turn')
    if any(not isinstance(e.get('identity'), str) or not e['identity'] for e in refs):
        return dict(unavailable, consent=True, reason='invalid_reference_identity')
    if any(e.get('category') not in CATEGORIES or (e.get('identity') is not None and not isinstance(e['identity'], str)) for e in cases):
        return dict(unavailable, consent=True, reason='invalid_case')
    clips = {}
    for e in entries:
        row = db.execute('SELECT chunk_id FROM speaker_turns WHERE id=?', (e['turn_id'],)).fetchone()
        if not row:
            return dict(unavailable, consent=True, reason='missing_turn')
        clips[e['turn_id']] = row[0]
    separated_clips = not ({clips[e['turn_id']] for e in refs} & {clips[e['turn_id']] for e in cases})
    separated_sessions = not ({e['session'] for e in refs} & {e['session'] for e in cases})
    if not separated_clips or not separated_sessions:
        return dict(unavailable, consent=True, separated_clips=separated_clips,
                    separated_sessions=separated_sessions, reason='holdout_leakage')
    profiles = {}
    for e in refs:
        profiles.setdefault(e['identity'], []).extend(_vectors(db, e['turn_id']))
    if len(profiles) < 2 or any(not p for p in profiles.values()):
        return dict(unavailable, consent=True, reason='need_two_eligible_reference_identities')
    correct = false = abstained = unknown = 0
    categories = {category: 0 for category in sorted(CATEGORIES)}
    for e in cases:
        categories[e['category']] += 1
        if e['identity'] is None:
            unknown += 1
        vectors = _vectors(db, e['turn_id'])
        prediction = None
        if vectors:
            ranked = sorted(((max(voice_id.cosine(left, right) for left in vectors for right in samples), identity)
                             for identity, samples in profiles.items()), reverse=True)
            score, best = ranked[0]
            margin = score - ranked[1][0]
            if score >= voice_id.AUTO_MIN_SCORE and margin >= voice_id.AUTO_MIN_MARGIN:
                prediction = best
        if prediction is None:
            abstained += 1
        elif prediction == e['identity']:
            correct += 1
        else:
            false += 1
    passed = false == 0 and correct >= 10 and unknown >= 10 and all(categories[c] >= 2 for c in ('noise', 'overlap', 'short', 'unknown'))
    return {'status': 'passed' if passed else 'not_passed', 'algorithm': speaker_identity.ALGORITHM,
            'extraction_version': voice_id.EXTRACTION_VERSION, 'consent': True,
            'separated_clips': True, 'separated_sessions': True,
            'thresholds': {'score': voice_id.AUTO_MIN_SCORE, 'margin': voice_id.AUTO_MIN_MARGIN},
            'correct_assignments': correct, 'false_assignments': false, 'abstentions': abstained,
            'unknown_cases': unknown, 'categories': categories, 'case_count': len(cases),
            'manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    if args.db is None or args.manifest is None or not args.db.is_file() or not args.manifest.is_file():
        print(json.dumps({'status': 'unavailable', 'reason': 'consented_real_data_not_supplied'}))
        return
    try:
        if args.manifest.stat().st_size > 1_000_000:
            raise ValueError('manifest too large')
        manifest = json.loads(args.manifest.read_text())
        with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            report = evaluate(db, manifest)
    except (OSError, ValueError, sqlite3.Error):
        report = {'status': 'unavailable', 'reason': 'input_or_database_unavailable'}
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
