"""Read-only shadow evaluation. This command never enables suppression."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def evaluate(db, reviews):
    rows = list(db.execute("SELECT id,started,status,activity_decision,vad_status FROM chunks"))
    held = [row for row in rows if row['activity_decision'] == 'would_hold']
    span_ids = {row[0] for row in db.execute("SELECT DISTINCT chunk_id FROM chunk_speech_spans WHERE end_seconds>start_seconds")}
    missed = sum(row['id'] in span_ids for row in held)
    reviewed = {item['chunk_id']: item for item in reviews if item.get('consented') is True
                and isinstance(item.get('speech_present'), bool)}
    human_misses = sum(reviewed.get(row['id'], {}).get('speech_present') is True for row in held)
    missing_review = sum(row['id'] not in reviewed for row in held)
    dates = set()
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row['started'].replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                dates.add(stamp.date())
        except (ValueError, TypeError):
            pass
    conditions = {item.get('condition') for item in reviewed.values()}
    required = {'soft_speech', 'distant_speech', 'music', 'noise', 'incomplete'}
    complete_vad = all(row['vad_status'] == 'complete' for row in held)
    gate = bool(held) and len(dates) >= 7 and not missed and not human_misses and not missing_review and complete_vad and required <= conditions
    return {'clips': len(rows), 'observed_days': len(dates), 'would_hold': len(held),
            'vad_speech_in_holds': missed, 'human_speech_in_holds': human_misses,
            'unreviewed_holds': missing_review, 'held_vad_complete': complete_vad,
            'condition_coverage_complete': required <= conditions,
            'evaluation_gate_passed': gate, 'suppression_enabled': False,
            'remaining': ['Battery, storage and network comparison plus explicit activation remain required.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, help='Private JSON list: chunk_id, consented, speech_present, condition')
    args = parser.parse_args()
    reviews = json.loads(args.reviews.read_text()) if args.reviews else []
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        print(json.dumps(evaluate(db, reviews), sort_keys=True))
