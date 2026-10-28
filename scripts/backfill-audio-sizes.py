"""Backfill original byte metadata only, without changing processing or retention."""
import argparse
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.data_dir.resolve(strict=True)
    db = sqlite3.connect((root / 'inbox.sqlite3').as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
    changes = []
    for identifier, original in db.execute("SELECT id,path FROM chunks WHERE audio_state='present' AND audio_bytes IS NULL"):
        path = Path(original).resolve()
        if path.is_relative_to(root / 'audio') and path.is_file():
            changes.append((path.stat().st_size, identifier))
    if args.apply:
        with db:
            db.executemany('UPDATE chunks SET audio_bytes=? WHERE id=? AND audio_bytes IS NULL', changes)
    print(json.dumps({'mode': 'apply' if args.apply else 'dry-run', 'recordings': len(changes), 'bytes': sum(c[0] for c in changes)}))


if __name__ == '__main__':
    main()
