"""Private resumable recovery through the human retry route. Default is dry-run.

The ledger is read-only. An uncertain POST is reconciled from the ledger before
any further action; an attention row after an uncertain POST requires review.
Run a ten-item canary first. Expansion requires an explicit --limit after gates.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import urllib.request


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, manifest):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(manifest, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def reconcile(entry, row):
    if row is None:
        entry['state'] = 'unresolved_missing_row'
    elif row['status'] == 'complete':
        entry['state'] = 'recovered'
    elif row['status'] == 'pending':
        entry['state'] = 'queued'
    elif entry['state'] in ('posting', 'queued', 'uncertain'):
        entry['state'] = 'unresolved_attention'
    return entry['state']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.data_dir.resolve(strict=True)
    manifest_path = args.manifest.absolute()
    if args.limit < 1 or manifest_path.is_symlink() or not manifest_path.parent.is_dir():
        parser.error('Private manifest and positive limit required')
    if manifest_path.parent.stat().st_mode & 0o077:
        parser.error('Manifest parent must be owner-private')
    with manifest_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect((root / 'inbox.sqlite3').as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('database_check_failed')
        if shutil.disk_usage(root).free < 1024 ** 3:
            raise RuntimeError('storage_safety_failed')
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest['data_dir'] != str(root) or manifest['version'] != 1:
                raise RuntimeError('manifest_identity_failed')
        else:
            entries = []
            for row in db.execute("SELECT * FROM chunks WHERE status!='complete' ORDER BY started,id"):
                path = Path(row['path']).resolve()
                if not path.is_relative_to(root / 'audio') or not path.is_file():
                    continue
                sha = digest(path)
                if sha != row['sha256']:
                    raise RuntimeError('source_hash_failed')
                entries.append({'id': row['id'], 'started': row['started'], 'sha256': sha, 'state': 'retained'})
            manifest = {'version': 1, 'data_dir': str(root), 'created_at': time.time(), 'entries': entries}
            # Spread the first ten across affected dates, keeping the rest chronological.
            entries.sort(key=lambda e: e['started'])
            if len(entries) > 10:
                chosen = [entries[round(i * (len(entries) - 1) / 9)] for i in range(10)]
                manifest['entries'] = chosen + [e for e in entries if e not in chosen]
            save(manifest_path, manifest)
        health = None
        submitted = 0
        for entry in manifest['entries']:
            row = db.execute('SELECT * FROM chunks WHERE id=?', (entry['id'],)).fetchone()
            state = reconcile(entry, row)
            save(manifest_path, manifest)
            if state != 'retained' or not args.apply or submitted >= args.limit:
                continue
            path = Path(row['path']).resolve()
            if not path.is_relative_to(root / 'audio') or not path.is_file() or digest(path) != entry['sha256']:
                raise RuntimeError('source_preservation_failed')
            token = (root / 'viewer.token').read_text().strip()
            headers = {'Authorization': 'Bearer ' + token}
            if health is None:
                with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8767/v1/health', headers=headers), timeout=10) as response:
                    health = json.load(response)
                if health.get('processing', {}).get('engine_ready') is not True:
                    raise RuntimeError('engine_safety_failed')
            entry['state'] = 'posting'
            save(manifest_path, manifest)
            try:
                request = urllib.request.Request('http://127.0.0.1:8767/v1/chunks/' + entry['id'] + '/retry',
                                                 data=b'{}', headers={**headers, 'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read(65536)
                entry['state'] = 'queued'
            except Exception:
                entry['state'] = 'uncertain'
                reconcile(entry, db.execute('SELECT * FROM chunks WHERE id=?', (entry['id'],)).fetchone())
                save(manifest_path, manifest)
                raise RuntimeError('retry_outcome_requires_reconciliation') from None
            submitted += 1
            save(manifest_path, manifest)
        counts = {}
        for entry in manifest['entries']:
            counts[entry['state']] = counts.get(entry['state'], 0) + 1
        print(json.dumps({'mode': 'apply' if args.apply else 'dry-run', 'submitted': submitted, 'counts': counts}))


if __name__ == '__main__':
    main()
