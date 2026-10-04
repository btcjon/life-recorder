"""Private static viewer bundle. No runtime credentials or arbitrary file serving."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from health import viewer_asset_name

MAX_ASSETS = 64
MAX_FILE = 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_PROVENANCE = 64 * 1024
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.woff2': 'font/woff2', '.png': 'image/png'}
PAGES = {'', 'index.html', 'recordings', 'review', 'people', 'context', 'health'}
SHA = re.compile(r'^[0-9a-f]{64}$')
REVISION = re.compile(r'^[0-9a-f]{40}$')


class Unavailable(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _safe_file(root, relative, maximum):
    """Validate lexical components, then read an owner-owned regular file."""
    root = Path(root).absolute()
    for path in (root, *root.parents):
        if path.is_symlink():
            raise Unavailable('viewer_bundle_symlink')
    path = root / relative
    for component in path.relative_to(root).parts[:-1]:
        root = root / component
        if root.is_symlink() or not root.is_dir():
            raise Unavailable('viewer_bundle_symlink')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or not 0 < info.st_size <= maximum):
        raise Unavailable('viewer_bundle_invalid')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
            raise Unavailable('viewer_bundle_changed')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            content = handle.read(maximum + 1)
        if len(content) != info.st_size:
            raise Unavailable('viewer_bundle_changed')
        return content
    finally:
        os.close(fd)


def _hashes(value, *, assets=False):
    if not isinstance(value, dict) or not value or len(value) > (MAX_ASSETS if assets else 256):
        raise Unavailable('viewer_provenance_invalid')
    for name, sha in value.items():
        if (not isinstance(name, str) or len(name) > 240 or name.startswith('/')
                or '\\' in name or any(p in ('', '.', '..') for p in name.split('/'))
                or (assets and not viewer_asset_name(name))
                or not isinstance(sha, str) or not SHA.fullmatch(sha)):
            raise Unavailable('viewer_provenance_invalid')
    return dict(value)


def provenance(root, revision=None):
    raw = _safe_file(root, 'build-provenance.json', MAX_PROVENANCE)
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise Unavailable('viewer_provenance_invalid') from error
    if (not isinstance(value, dict) or set(value) != {
            'version', 'source_revision', 'source_files', 'vendor_files', 'asset_files'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['source_revision'], str)
            or not REVISION.fullmatch(value['source_revision'])
            or (revision is not None and value['source_revision'] != revision)):
        raise Unavailable('viewer_provenance_invalid')
    result = {'version': 1, 'source_revision': value['source_revision'],
              'source_files': _hashes(value['source_files']),
              'vendor_files': _hashes(value['vendor_files']),
              'asset_files': _hashes(value['asset_files'], assets=True)}
    if ('index.html' not in result['asset_files'] or 'web/package-lock.json' not in result['source_files']
            or any(not name.startswith('web/') or name.startswith(('web/vendor/', 'web/dist/', 'web/node_modules/'))
                   for name in result['source_files'])):
        raise Unavailable('viewer_provenance_invalid')
    return result, raw


def read_build(root, revision=None):
    """Reconcile every file, including unlisted files; used before packaging."""
    metadata, raw = provenance(root, revision)
    expected = set(metadata['asset_files']) | {'build-provenance.json'}
    actual = set()
    for count, path in enumerate(Path(root).rglob('*'), 1):
        if count > MAX_ASSETS * 2 + 16 or path.is_symlink():
            raise Unavailable('viewer_bundle_invalid')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise Unavailable('viewer_bundle_invalid')
    if actual != expected:
        raise Unavailable('viewer_bundle_unlisted_file')
    files, total = {}, 0
    for name, sha in metadata['asset_files'].items():
        data = _safe_file(root, name, MAX_FILE)
        total += len(data)
        if total > MAX_TOTAL or digest(data) != sha:
            raise Unavailable('viewer_bundle_changed')
        files[name] = data
    files['build-provenance.json'] = raw
    return metadata, files


class Bundle:
    """Cache the pinned build inventory; hash bytes again on each asset read."""
    def __init__(self, root, revision=None):
        self.root = Path(root)
        self.metadata, _files = read_build(root, revision)

    def status(self):
        return {'state': 'ready', 'source_revision': self.metadata['source_revision'],
                'asset_count': len(self.metadata['asset_files']), 'delivery': 'private_same_origin'}

    def read(self, path):
        if not path.startswith('/ui-v2/'):
            return None
        relative = path.removeprefix('/ui-v2/')
        if relative in PAGES:
            relative = 'index.html'
        if (not viewer_asset_name(relative) or relative not in self.metadata['asset_files']):
            return None
        data = _safe_file(self.root, relative, MAX_FILE)
        if digest(data) != self.metadata['asset_files'][relative]:
            raise Unavailable('viewer_bundle_changed')
        return data, MIME[Path(relative).suffix]
